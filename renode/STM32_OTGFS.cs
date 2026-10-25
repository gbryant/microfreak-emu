//
// STM32 OTG_FS in *device* mode, with a minimal USB host driving enumeration.
//
// Why this exists: the firmware answers SysEx over USB.  A request sent to the
// DIN port (USART1) is received, parsed and dispatched correctly -- measured --
// and the reply is built and handed to an output port, but the port drops it
// because its transmit ring stays closed until the USB device reaches its
// configured state.  So no protocol transaction can complete against emulated
// firmware, which is the thing a custom-firmware testbed needs.
//
// Renode ships a device-side USB framework (USBDeviceCore, endpoints,
// descriptors), but it is built the other way round: virtual devices attaching
// to an emulated *host*.  Here the emulated machine is the device and we are
// the host, and the tree has no Synopsys/STM32 OTG model, so this is written
// from what the firmware actually does.
//
// `Trace` records every register access, and that is the intended way to
// develop this: watch what the firmware does, implement the next thing it
// waits for.  It earned its keep immediately -- the first trace showed the
// firmware spinning forever on GRSTCTL because this model did not self-clear
// CSRST, so USB init never started at all.
//
// The device side is driven by the firmware; the host side is a small state
// machine on a timer issuing the standard enumeration sequence.  Descriptors
// the device returns are consumed and discarded: the goal is to get the
// firmware's stack to its configured state, not to be a conformant host.
//
// 2026-10-15: the host can now issue ARBITRARY control transfers after
// enumeration -- `ControlRequest`, callable from the Monitor -- in both
// directions, with a data stage.  That is what the firmware update procedure
// is made of: Arturia's own class requests 0xF2 (REBOOT_TO_DFU), 0xF4
// (SELECT_TARGET) and 0xF3 (FINISH), plus standard DFU 1.1 DNLOAD, GETSTATUS
// and GETSTATE.  Before this, the host could only run the six-request
// enumeration array and then stop, so the whole update path was unreachable
// under emulation and could only be tested by writing the app's request flag
// into SRAM by hand.
//
// Endpoint 0's IN data is captured separately from the MIDI capture, because
// a control READ's reply has to be read back by whoever asked for it:
// `LastControlReply` is the hex of the last one.
//
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.USBDeprecated
{
    public class STM32_OTGFS : IDoubleWordPeripheral, IKnownSize
    {
        public STM32_OTGFS(IMachine machine)
        {
            this.machine = machine;
            IRQ = new GPIO();
            regs = new uint[Size / 4];
            packets = new List<byte>();
            fifoWrites = new List<uint>();
            trace = new List<Tuple<bool, long, uint>>();
            rx = new Queue<RxEntry>();

            // The host has to act on its own; the firmware never asks to be
            // reset.  1 kHz is far faster than enumeration needs and costs one
            // time-source event per millisecond, against the 10,000 a second
            // SysTick already raises.
            pump = new LimitTimer(machine.ClockSource, 1000, this, "usbHost",
                                  limit: 1, direction: Direction.Ascending,
                                  enabled: false, eventEnabled: true);
            pump.LimitReached += Pump;
            Reset();
        }

        public GPIO IRQ { get; private set; }
        public long Size => 0x40000;

        // --- diagnostics --------------------------------------------------
        public bool Trace { get; set; }
        public int TraceLimit { get; set; } = 6000;
        public string HostState => state.ToString();

        /// <summary>Why host-to-device data is or is not moving.</summary>
        public string OutState
        {
            get
            {
                var tsiz = (DOEPTSIZ0 + MidiOutEndpoint * 0x20) / 4;
                var ctl = (DOEPCTL0 + MidiOutEndpoint * 0x20) / 4;
                lock(pendingOut)
                {
                    return string.Format(
                        "queued {0}, rx {1}, DOEPCTL{2} 0x{3:X8}, DOEPTSIZ{2} 0x{4:X8}",
                        pendingOut.Count, rx.Count, MidiOutEndpoint,
                        regs[ctl], regs[tsiz]);
                }
            }
        }
        public bool Configured => configured;
        public int CapturedWords => fifoWrites.Count;
        public int CapturedBytes => packets.Count;

        // --- arbitrary control transfers ----------------------------------
        public int ControlPending { get { lock(pendingControl) { return pendingControl.Count; } } }
        public int ControlDone => controlDone;
        public int ControlStalled => controlStalled;
        public string ControlStatus => controlStatus;

        /// <summary>The device's data-stage reply to the last control READ.</summary>
        public string LastControlReply =>
            string.Join(" ", ep0In.Select(b => b.ToString("X2")));

        /// <summary>Queue one control transfer, to run once enumeration is done.
        ///
        /// `requestType` is bmRequestType -- 0x21 for Arturia's class requests
        /// host-to-device, 0xA1 for DFU's device-to-host ones -- and bit 7 is
        /// what picks the direction here, exactly as on the wire. `data` is a
        /// hex byte string for the OUT data stage and is ignored for an IN
        /// request, whose `length` says how many bytes to ask for instead.
        ///
        /// A zero-length request (0xF2 and 0xF3 are both wLength 0) needs
        /// neither: pass length 0 and an empty string.
        ///
        ///   usbfs ControlRequest 0x21 0xF2 0 0 0 ""        REBOOT_TO_DFU
        ///   usbfs ControlRequest 0xA1 0x03 0 0 6 ""        DFU_GETSTATUS
        ///   usbfs ControlRequest 0x21 0x01 0 0 2 "AA BB"   DFU_DNLOAD, 2 bytes
        /// </summary>
        public void ControlRequest(int requestType, int request, int value,
                                   int index, int length, string data)
        {
            var bytes = new List<byte>();
            foreach(var part in (data ?? "").Split(new[] { ' ', ',' },
                                                   StringSplitOptions.RemoveEmptyEntries))
            {
                bytes.Add(Convert.ToByte(part, 16));
            }
            var isIn = (requestType & 0x80) != 0;
            if(!isIn && length == 0 && bytes.Count > 0)
            {
                length = bytes.Count;       // convenience: OUT length from the data
            }
            if(!isIn && bytes.Count != length)
            {
                throw new ArgumentException(string.Format(
                    "host-to-device request 0x{0:X2} says wLength {1} but carries "
                    + "{2} data bytes; they have to agree", request, length,
                    bytes.Count));
            }
            lock(pendingControl)
            {
                pendingControl.Enqueue(new Pending {
                    R = new Request((byte)requestType, (byte)request,
                                    (ushort)value, (ushort)index, (ushort)length),
                    Data = bytes.ToArray() });
            }
            this.Log(LogLevel.Info,
                     "queued control request bmRT 0x{0:X2} bRequest 0x{1:X2} "
                     + "wValue 0x{2:X4} wLength {3}{4}",
                     requestType, request, value, length,
                     bytes.Count > 0 ? string.Format(" ({0} data bytes)", bytes.Count) : "");
        }

        public void SaveTrace(string path)
        {
            using(var w = new StreamWriter(path))
            {
                foreach(var e in trace)
                {
                    w.WriteLine("{0} {1:X4} {2:X8}", e.Item1 ? "W" : "R", e.Item2, e.Item3);
                }
            }
            this.Log(LogLevel.Info, "wrote {0} accesses to {1}", trace.Count, path);
        }

        public string TraceSummary()
        {
            return string.Join(" ", trace.GroupBy(e => e.Item2).OrderBy(g => g.Key)
                .Select(g => string.Format("{0:X4}:{1}r{2}w", g.Key,
                    g.Count(e => !e.Item1), g.Count(e => e.Item1))));
        }

        public void ClearCapture()
        {
            packets.Clear();
            fifoWrites.Clear();
        }

        public void SaveCapture(string path)
        {
            File.WriteAllBytes(path, packets.ToArray());
        }

        /// <summary>Append every USB packet to a file as it is sent.
        ///
        /// A host reading replies needs them promptly.  Polling SaveCapture
        /// through the Monitor costs a round trip and rewrites the whole
        /// capture each time, which is tolerable for one 45-byte reply and
        /// hopeless for a preset block: 146 chunks, each acknowledged.
        /// Streaming lets the other side just tail a file.</summary>
        public void StreamTo(string path)
        {
            CloseStream();
            stream = new FileStream(path, FileMode.Create, FileAccess.Write,
                                    FileShare.ReadWrite);
        }

        public void CloseStream()
        {
            if(stream != null)
            {
                stream.Dispose();
                stream = null;
            }
        }

        public string Dump()
        {
            return string.Join(" ", packets.Select(b => b.ToString("X2")));
        }

        /// <summary>Send MIDI to the device, host-to-device, as USB-MIDI.
        ///
        /// The reply direction was enough to read presets, but writing one is
        /// dispatched and then dropped when the request arrives over DIN, so
        /// the firmware evidently wants writes on the bus it answers on.  This
        /// is the same receive-FIFO machinery the SETUP stage uses, addressed
        /// to the MIDI OUT endpoint instead of endpoint zero.
        ///
        /// `hex` is a MIDI byte stream, SysEx included; it is packetised here
        /// because the code index numbers depend on where the message ends.
        /// </summary>
        public void Send(string hex)
        {
            var midi = new List<byte>();
            foreach(var part in hex.Split(new[] { ' ', ',' },
                                          StringSplitOptions.RemoveEmptyEntries))
            {
                midi.Add(Convert.ToByte(part, 16));
            }
            SendBytes(midi.ToArray());
        }

        public void SendBytes(byte[] midi)
        {
            var packets = new List<byte>();
            var i = 0;
            var sysex = midi.Length > 0 && midi[0] == 0xF0;
            while(i < midi.Length)
            {
                var left = midi.Length - i;
                byte cin;
                int take;
                if(sysex)
                {
                    if(left > 3)
                    {
                        cin = 0x4; take = 3;       // SysEx starts or continues
                    }
                    else
                    {
                        // The last packet says how many bytes it carries.
                        cin = (byte)(left == 1 ? 0x5 : left == 2 ? 0x6 : 0x7);
                        take = left;
                    }
                }
                else
                {
                    // A channel message: the code index number is its status
                    // nibble, and the length follows from that.  Labelling
                    // these as SysEx -- which this did -- makes the device
                    // discard them.
                    cin = (byte)((midi[i] >> 4) & 0x0F);
                    take = (cin == 0xC || cin == 0xD) ? 2 : 3;
                    take = Math.Min(take, left);
                }
                packets.Add(cin);                  // cable 0
                for(var k = 0; k < 3; k++)
                {
                    packets.Add(k < take ? midi[i + k] : (byte)0);
                }
                i += take;
            }
            // Queue rather than deliver: the endpoint has to be armed first.
            // The HAL derives how much arrived from what is left in DOEPTSIZ
            // (received = maxpacket * pktcnt - XFRSIZ), so delivering into an
            // endpoint the firmware has not re-armed yet leaves XFRSIZ at
            // zero and the class layer reads a full 64 bytes out of a buffer
            // holding far fewer -- garbage MIDI, and whatever real reply came
            // next was lost.  That was intermittent, because it depended on
            // whether the firmware had got back to DataOutStageCallback.
            // Enqueue only.  This runs on Renode's Monitor thread, not the
            // CPU thread, so it must not touch `rx` or `regs`: doing that
            // raced the firmware's own reads of the same state and wedged the
            // machine part way through a transfer, intermittently and always
            // under load.  Delivery happens from a register access instead,
            // which is always on the CPU thread.
            lock(pendingOut)
            {
                pendingOut.Enqueue(packets.ToArray());
            }
            this.Log(LogLevel.Debug, "queued {0} MIDI bytes as {1} USB packets",
                     midi.Length, packets.Count / 4);
        }

        /// <summary>Hand queued host-to-device data to an armed OUT endpoint.</summary>
        private void DeliverPendingOut()
        {
            var tsiz = (DOEPTSIZ0 + MidiOutEndpoint * 0x20) / 4;
            var ctl = (DOEPCTL0 + MidiOutEndpoint * 0x20) / 4;
            while(rx.Count == 0)
            {
                byte[] data;
                lock(pendingOut)
                {
                    if(pendingOut.Count == 0)
                    {
                        return;
                    }
                    var armed0 = regs[tsiz] & 0x7FFFF;
                    data = pendingOut.Peek();
                    if((regs[ctl] & EPENA) == 0)
                    {
                        return;                 // not armed yet
                    }
                    // A message larger than the armed transfer is delivered in
                    // installments on packet boundaries, which is what real USB
                    // does -- the endpoint's max packet size is 64 bytes and a
                    // SysEx of any length spans several transfers. Requiring the
                    // whole message to fit made the queue head-of-line block
                    // forever: a 49-byte SysEx packetises to 17 USB packets = 68
                    // bytes against an armed 64, so nothing was ever delivered
                    // and every later, smaller message was stuck behind it. The
                    // symptom was zero dispatches with the device configured,
                    // the endpoint armed and all messages queued.
                    var room = (int)(armed0 & ~3u);          // whole packets only
                    if(room == 0)
                    {
                        return;                 // no room yet
                    }
                    if(data.Length > room)
                    {
                        var head = new byte[room];
                        Array.Copy(data, head, room);
                        var rest = new byte[data.Length - room];
                        Array.Copy(data, room, rest, 0, rest.Length);
                        pendingOut.Dequeue();
                        // Put the remainder back at the FRONT, before anything
                        // queued after it, or a split message interleaves with
                        // the next one and the class layer sees garbage.
                        var tail = new Queue<byte[]>();
                        tail.Enqueue(rest);
                        foreach(var q in pendingOut)
                        {
                            tail.Enqueue(q);
                        }
                        pendingOut.Clear();
                        foreach(var q in tail)
                        {
                            pendingOut.Enqueue(q);
                        }
                        data = head;
                    }
                    else
                    {
                        pendingOut.Dequeue();
                    }
                }
                var armed = regs[tsiz] & 0x7FFFF;
                rx.Enqueue(new RxEntry {
                    Status = MakeStatus(MidiOutEndpoint, (uint)data.Length, PktStsOutData),
                    Data = data });
                // XFRC is deliberately not raised here: it belongs on the
                // completion status being popped, after the device has read
                // the bytes, exactly as STUP does.
                rx.Enqueue(new RxEntry {
                    Status = MakeStatus(MidiOutEndpoint, 0, PktStsOutDone) });
                regs[tsiz] = (regs[tsiz] & ~0x7FFFFu) | (armed - (uint)data.Length);
            }
            UpdateRxLevel();
        }

        /// <summary>Plug the cable in: start enumeration. Call after boot.</summary>
        public void Attach()
        {
            state = HostStep.Reset;
            settle = 0;
            request = 0;
            pump.Enabled = true;
            this.Log(LogLevel.Info, "USB host attached, enumeration starting");
        }

        public void Reset()
        {
            Array.Clear(regs, 0, regs.Length);
            packets.Clear();
            fifoWrites.Clear();
            rx.Clear();
            CloseStream();
            state = HostStep.Idle;
            configured = false;
            inComplete = false;
            dataStageWasIn = false;
            ep0In.Clear();
            ep0DataStage = false;
            controlDone = 0;
            controlStalled = 0;
            controlStatus = "none";
            lock(pendingControl)
            {
                pendingControl.Clear();
            }
            settle = 0;
            request = 0;
            inEndpoint = 0;
            inRemaining = 0;
            inWritten = 0;
            if(pump != null)
            {
                pump.Enabled = false;
            }
            IRQ.Set(false);

            regs[GRSTCTL / 4] = AHBIDL;
            regs[GINTSTS / 4] = 0x04000020;
            regs[DIEPINT1 / 4] = TXFE;
            regs[DSTS / 4] = 0x00000008;        // full speed, not suspended
        }

        // --- bus ----------------------------------------------------------
        public uint ReadDoubleWord(long offset)
        {
            var v = ReadInner(offset);
            Record(false, offset, v);
            return v;
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            Record(true, offset, value);
            WriteInner(offset, value);
            UpdateInterrupt();
        }

        private uint ReadInner(long offset)
        {
            if(offset >= FifoBase && offset < FifoBase + FifoWindowSize * FifoWindows)
            {
                // Reading any FIFO window pops the receive FIFO: that is how
                // the device collects SETUP and OUT data.
                return PopRx();
            }
            switch(offset)
            {
            case DTXFSTS0:
            case DTXFSTS1:
                // Free space in words.  Always plenty, because this model
                // drains the moment a word is written; the firmware refuses to
                // send when fewer than 128 words are free.
                return FifoWords;
            case GRSTCTL:
                // Every self-clearing bit reads back as done.  CSRST matters
                // most: USB_CoreReset writes it then polls, so leaving it set
                // wedges USB init before it starts.
                return (regs[GRSTCTL / 4]
                        & ~(CSRST | HSRST | FCRST | RXFFLSH | TXFFLSH)) | AHBIDL;
            case GINTSTS:
                // IEPINT/OEPINT are read-only summaries of the endpoint
                // interrupt registers, not bits anyone sets.  Storing them
                // left IEPINT stuck on with DAINTMSK zero, and the firmware
                // span in its ISR forever -- 2,900 GINTSTS reads in three
                // seconds, never getting as far as our SETUP packet.
                return (regs[GINTSTS / 4] & ~(IEPINT | OEPINT)) | EndpointSummary();
            case DAINT:
                // Also read-only, and derived.  The firmware writes all-ones
                // to it during init to clear pending state; honouring that
                // write is what made it read 0xFFFFFFFF forever.
                return DeviceAllEndpoints();
            case GRXSTSR:
                return rx.Count > 0 ? rx.Peek().Status : 0;
            case GRXSTSP:
                return PopStatus();
            }
            return (offset >= 0 && offset < Size) ? regs[offset / 4] : 0;
        }

        private void WriteInner(long offset, uint value)
        {
            if(offset >= FifoBase && offset < FifoBase + FifoWindowSize * FifoWindows)
            {
                // A push into a TX FIFO: the device sending a packet to us.
                fifoWrites.Add(value);
                for(var i = 0; i < 4; i++)
                {
                    packets.Add((byte)((value >> (8 * i)) & 0xFF));
                }
                // Endpoint 0's window separately: a control read's reply is
                // the answer to a specific question and has to be readable on
                // its own, not dug out of a capture that also holds MIDI,
                // descriptors and status stages.
                if((offset - FifoBase) / FifoWindowSize == 0)
                {
                    for(var i = 0; i < 4; i++)
                    {
                        ep0In.Add((byte)((value >> (8 * i)) & 0xFF));
                    }
                }
                // Only the MIDI IN endpoint's FIFO belongs in the capture.
                // Streaming every window meant the whole enumeration --
                // descriptors, string tables, status stages -- was written to
                // the same file and decoded on the host side as USB-MIDI
                // packets.  A stray 0xF0 in a descriptor starts a SysEx
                // message that then swallows the real reply after it, which
                // is an intermittent lost reply and a protocol timeout.
                if(stream != null
                   && (offset - FifoBase) / FifoWindowSize == MidiInEndpoint)
                {
                    for(var i = 0; i < 4; i++)
                    {
                        stream.WriteByte((byte)((value >> (8 * i)) & 0xFF));
                    }
                    stream.Flush();
                }
                inWritten += 4;
                if(inRemaining > 0 && inWritten >= inRemaining)
                {
                    CompleteIn(inEndpoint);
                }
                return;
            }
            if(offset < 0 || offset >= Size)
            {
                return;
            }
            // DIEPINTn/DOEPINTn are write-1-to-clear, for *every* endpoint.
            // Listing only 0 and 1 meant endpoint 2 -- the MIDI OUT endpoint --
            // stored the write instead, so the firmware's attempt to clear
            // XFRC left it set, DAINT kept reporting the endpoint, OEPINT
            // never went away and the ISR re-entered forever.  The main loop
            // then never ran again: a note sent over USB arrived, was copied
            // into the receive ring, and sat there while the CPU span in the
            // USB interrupt.  That looked exactly like the class layer
            // ignoring the data, which is what it was mistaken for.
            if(IsEndpointInterrupt(offset))
            {
                regs[offset / 4] &= ~value;
                regs[DIEPINT1 / 4] |= TXFE;     // this model's FIFO is never full
                return;
            }
            switch(offset)
            {
            case GINTSTS:
                regs[GINTSTS / 4] &= ~(value & Clearable);
                return;
            case DAINT:
                return;              // read-only
            default:
                break;
            }
            if(IsInControl(offset))
            {
                regs[offset / 4] = value;
                if((value & EPENA) != 0)
                {
                    // The device armed an IN transfer.  Completing it here
                    // would be wrong: the firmware writes one max-packet chunk
                    // at a time and waits for the FIFO-empty interrupt before
                    // writing the next, so declaring the whole transfer done
                    // after the first chunk truncated the configuration
                    // descriptor at 56 of its 119 bytes.  Instead record how
                    // much was programmed and let the FIFO writes complete it.
                    var ep = (int)((offset - DIEPCTL0) / 0x20);
                    inEndpoint = ep;
                    inRemaining = (int)(regs[(DIEPTSIZ0 + ep * 0x20) / 4]
                                        & (ep == 0 ? 0x7Fu : 0x7FFFFu));
                    inWritten = 0;
                    regs[(DIEPINT0 + ep * 0x20) / 4] |= TXFE;
                    if(inRemaining == 0)
                    {
                        // A zero-length packet: the status stage of a control
                        // write, which has nothing to send and is done at once.
                        CompleteIn(ep);
                    }
                }
                return;
            }
            regs[offset / 4] = value;
            if(IsOutControl(offset))
            {
                // Arming an OUT endpoint is the cue to hand it whatever the
                // host has queued.
                DeliverPendingOut();
            }
        }

        /// <summary>DOEPCTLn, for any endpoint this model has.</summary>
        private static bool IsOutControl(long offset)
        {
            return InBank(offset, DOEPCTL0) && (offset & 0x1F) == 0x00;
        }

        /// <summary>DIEPINTn or DOEPINTn, for any endpoint this model has.</summary>
        private static bool IsEndpointInterrupt(long offset)
        {
            return (InBank(offset, DIEPCTL0) || InBank(offset, DOEPCTL0))
                   && (offset & 0x1F) == 0x08;
        }

        /// <summary>DIEPCTLn, for any endpoint this model has.</summary>
        private static bool IsInControl(long offset)
        {
            return InBank(offset, DIEPCTL0) && (offset & 0x1F) == 0x00;
        }

        /// <summary>Is this offset inside an endpoint register bank?</summary>
        private static bool InBank(long offset, long first)
        {
            return offset >= first && offset < first + Endpoints * 0x20;
        }

        /// <summary>The programmed IN transfer has been written in full.</summary>
        private void CompleteIn(int ep)
        {
            regs[(DIEPINT0 + ep * 0x20) / 4] |= XFRC | TXFE;
            regs[(DIEPCTL0 + ep * 0x20) / 4] &= ~EPENA;
            inRemaining = 0;
            inComplete = true;
            UpdateInterrupt();
        }

        // --- receive FIFO --------------------------------------------------
        private class RxEntry
        {
            public uint Status;
            public byte[] Data = EmptyData;
            public int Read;
        }

        private uint PopStatus()
        {
            if(rx.Count == 0)
            {
                return 0;
            }
            var e = rx.Peek();
            if(e.Data.Length == 0)
            {
                rx.Dequeue();               // status-only entry, done with it
                var doneEp = (int)(e.Status & 0xF);
                if(((e.Status >> 17) & 0xF) == PktStsOutDone
                   && (doneEp != 0 || ep0DataStage))
                {
                    // A data endpoint's transfer is over.  Without dequeuing
                    // this the receive queue never empties, RXFLVL stays
                    // asserted, and the firmware -- having already taken the
                    // packets -- never gets back to normal processing.
                    //
                    // Endpoint 0 counts ONLY when the transfer carried a data
                    // stage, which is a control WRITE and nothing else. The
                    // HAL waits on XFRC before calling DataOutStage, so
                    // without this a DFU_DNLOAD's 64 bytes were read out of
                    // the FIFO and then nothing happened -- the device sat in
                    // its interrupt loop and the download looked like a stall.
                    // Raising it for every endpoint-0 OUT instead perturbs the
                    // control READ path, whose zero-length status stage
                    // StatusOut() already accounts for: that was tried, and
                    // the next request was never armed.
                    regs[(DOEPINT0 + doneEp * 0x20) / 4] |= XFRC;
                    ep0DataStage = false;
                }
                if(((e.Status >> 17) & 0xF) == PktStsSetupDone)
                {
                    // The SETUP stage is over only once the device has read
                    // the packet and then this completion status.  Raising
                    // STUP any earlier -- as this did -- makes the firmware
                    // run its setup handler before it has the bytes, so it
                    // answers nothing and then reads the request too late.
                    regs[DOEPINT0 / 4] |= STUP;
                }
            }
            UpdateRxLevel();
            if(rx.Count == 0)
            {
                // The previous transfer is fully accounted for; if the
                // endpoint is still armed, the next queued message can go.
                DeliverPendingOut();
            }
            return e.Status;
        }

        private uint PopRx()
        {
            if(rx.Count == 0)
            {
                return 0;
            }
            var e = rx.Peek();
            uint word = 0;
            for(var i = 0; i < 4; i++)
            {
                if(e.Read < e.Data.Length)
                {
                    word |= (uint)e.Data[e.Read++] << (8 * i);
                }
            }
            if(e.Read >= e.Data.Length)
            {
                rx.Dequeue();
            }
            UpdateRxLevel();
            return word;
        }

        private void UpdateRxLevel()
        {
            if(rx.Count > 0)
            {
                regs[GINTSTS / 4] |= RXFLVL;
            }
            else
            {
                regs[GINTSTS / 4] &= ~RXFLVL;
            }
            UpdateInterrupt();
        }

        private void PushSetup(byte[] setup)
        {
            // A SETUP transaction arrives as a status word saying "SETUP data
            // packet received" carrying the 8 bytes, then a status word saying
            // the SETUP stage finished, then endpoint 0's STUP interrupt.
            rx.Enqueue(new RxEntry {
                Status = MakeStatus(0, (uint)setup.Length, PktStsSetupData), Data = setup });
            rx.Enqueue(new RxEntry { Status = MakeStatus(0, 0, PktStsSetupDone) });
            UpdateRxLevel();
        }

        /// <summary>DAINT: which endpoints have an interrupt pending.</summary>
        private uint DeviceAllEndpoints()
        {
            var v = 0u;
            for(var ep = 0; ep < Endpoints; ep++)
            {
                var inInt = regs[(DIEPINT0 + ep * 0x20) / 4] & regs[DIEPMSK / 4];
                // TXFE is the exception: it is gated by DIEPEMPMSK, not
                // DIEPMSK.  Without that the firmware arms an IN transfer --
                // observed programming DIEPTSIZ0 for the device descriptor's
                // 18 bytes and setting EPENA -- and then never gets the
                // FIFO-empty interrupt that is its cue to write the data.
                if((regs[(DIEPINT0 + ep * 0x20) / 4] & TXFE) != 0
                   && (regs[DIEPEMPMSK / 4] & (1u << ep)) != 0)
                {
                    inInt |= TXFE;
                }
                if(inInt != 0)
                {
                    v |= 1u << ep;
                }
                if((regs[(DOEPINT0 + ep * 0x20) / 4] & regs[DOEPMSK / 4]) != 0)
                {
                    v |= 1u << (16 + ep);
                }
            }
            return v;
        }

        /// <summary>GINTSTS IEPINT/OEPINT, gated by DAINTMSK as the core does.</summary>
        private uint EndpointSummary()
        {
            var active = DeviceAllEndpoints() & regs[DAINTMSK / 4];
            var v = 0u;
            if((active & 0xFFFF) != 0)
            {
                v |= IEPINT;
            }
            if((active & 0xFFFF0000) != 0)
            {
                v |= OEPINT;
            }
            return v;
        }

        private static uint MakeStatus(uint ep, uint bytes, uint pktsts)
        {
            return (ep & 0xF) | ((bytes & 0x7FF) << 4) | ((pktsts & 0xF) << 17);
        }

        // --- host state machine --------------------------------------------
        //
        // Driven by device state, not by a timer's patience.  The firmware
        // signals it is ready for a SETUP by arming endpoint 0 with a SETUP
        // packet count in DOEPTSIZ0 (observed: 0x60080018, three packets, 24
        // bytes), and it signals a data phase is finished by writing the whole
        // programmed transfer size into the FIFO.  Waiting on those two rather
        // than on elapsed time is what makes this deterministic.
        private enum HostStep
        {
            Idle, Reset, WaitReset, EnumDone, WaitEnum, ArmSetup, WaitData, Done,
            CtlArm, CtlOut, CtlWait
        }

        /// <summary>A queued control transfer and its OUT data, if any.</summary>
        private struct Pending
        {
            public Request R;
            public byte[] Data;
        }

        private struct Request
        {
            public byte Type, Code;
            public ushort Value, Index, Length;
            public Request(byte t, byte c, ushort v, ushort i, ushort l)
            {
                Type = t; Code = c; Value = v; Index = i; Length = l;
            }
        }

        private static readonly Request[] Enumeration = {
            new Request(0x80, 0x06, 0x0100, 0, 0x0040),   // GET_DESCRIPTOR device
            new Request(0x00, 0x05, 0x0001, 0, 0x0000),   // SET_ADDRESS 1
            new Request(0x80, 0x06, 0x0100, 0, 0x0012),   // GET_DESCRIPTOR device, full
            new Request(0x80, 0x06, 0x0200, 0, 0x0009),   // GET_DESCRIPTOR config, header
            new Request(0x80, 0x06, 0x0200, 0, 0x0077),   // GET_DESCRIPTOR config, full
            new Request(0x00, 0x09, 0x0001, 0, 0x0000),   // SET_CONFIGURATION 1
        };

        private void Pump()
        {
            // Host-to-device data is handed over here, not from Send(): Send()
            // runs on Renode's Monitor thread and must not touch the receive
            // FIFO or the registers underneath a running CPU.  The pump is a
            // 1 kHz timer on the machine's own clock source, so this is the
            // emulation thread, and 1 ms of emulated latency is nothing.
            DeliverPendingOut();
            switch(state)
            {
            case HostStep.Reset:
                regs[GINTSTS / 4] |= USBRST;
                state = HostStep.WaitReset;
                settle = 0;
                break;

            case HostStep.WaitReset:
                if((regs[GINTSTS / 4] & USBRST) == 0)
                {
                    state = HostStep.EnumDone;
                    settle = 0;
                }
                else if(++settle > Patience)
                {
                    Stall("firmware never acknowledged the USB reset");
                }
                break;

            case HostStep.EnumDone:
                regs[GINTSTS / 4] |= ENUMDNE;
                state = HostStep.WaitEnum;
                settle = 0;
                break;

            case HostStep.WaitEnum:
                if((regs[GINTSTS / 4] & ENUMDNE) == 0)
                {
                    state = HostStep.ArmSetup;
                    settle = 0;
                }
                else if(++settle > Patience)
                {
                    Stall("firmware never acknowledged enumeration-done");
                }
                break;

            case HostStep.ArmSetup:
                // Wait until endpoint 0 is armed for a SETUP *and* the device
                // has drained the previous exchange.  Queueing behind unread
                // entries is what silently swallowed the last two requests:
                // the host believed it had sent them and walked off the end of
                // the sequence declaring success.
                if(rx.Count == 0 && (regs[DOEPTSIZ0 / 4] & StupCnt) != 0)
                {
                    var r = Enumeration[request];
                    dataStageWasIn = (r.Type & 0x80) != 0 && r.Length > 0;
                    Setup(r.Type, r.Code, r.Value, r.Index, r.Length);
                    state = HostStep.WaitData;
                    settle = 0;
                }
                else if(++settle > Patience)
                {
                    Stall("endpoint 0 was never armed for SETUP");
                }
                break;

            case HostStep.Done:
                // Enumeration is finished; serve queued control transfers.
                // Same two preconditions as ArmSetup: nothing unread in the
                // receive FIFO, and endpoint 0 armed for a SETUP. Queueing
                // behind unread entries is what silently swallowed requests
                // during enumeration, and a vendor request is no different.
                lock(pendingControl)
                {
                    if(pendingControl.Count == 0)
                    {
                        break;
                    }
                }
                if(rx.Count == 0 && (regs[DOEPTSIZ0 / 4] & StupCnt) != 0)
                {
                    lock(pendingControl)
                    {
                        current = pendingControl.Dequeue();
                    }
                    ep0In.Clear();
                    dataStageWasIn = (current.R.Type & 0x80) != 0 && current.R.Length > 0;
                    Setup(current.R.Type, current.R.Code, current.R.Value,
                          current.R.Index, current.R.Length);
                    inComplete = false;
                    state = (current.Data.Length > 0) ? HostStep.CtlOut : HostStep.CtlWait;
                    settle = 0;
                }
                break;

            case HostStep.CtlOut:
                // The OUT data stage of a host-to-device request: deliver it
                // on endpoint 0 once the device has armed that endpoint for
                // it, which is the same rule StatusOut() follows.
                //
                // And then DECREMENT DOEPTSIZ0's transfer size by what was
                // delivered, exactly as DeliverPendingOut does for the MIDI
                // endpoint. The HAL derives how much arrived from what is LEFT
                // (received = maxpacket * pktcnt - XFRSIZ), so an untouched
                // XFRSIZ means "nothing arrived": the class layer then reads a
                // 64-byte DFU_DNLOAD as empty and USBD_CtlError STALLS it.
                // Measured 2026-10-15 -- the bootloader answered GETSTATE and
                // erased on SELECT_TARGET, and stalled every single download
                // block until this line existed.
                //
                // XFRC is deliberately NOT raised here, for the same reason it
                // is not in DeliverPendingOut: it belongs on the completion
                // status being popped, after the device has read the bytes.
                // EPENA alone is NOT the cue. Endpoint 0 OUT is left armed
                // for the next SETUP after every transfer, so EPENA is set
                // almost always -- delivering on it put the data stage into
                // the arming meant for a SETUP packet, and the device then
                // never ran its DataOut handler and never sent the status
                // stage. The MIDI path has guarded against this since it was
                // written; the control path did not. Wait for the device to
                // arm an OUT transfer with ROOM for the data: for endpoint 0
                // XFRSIZ is seven bits, not nineteen.
                if((regs[DOEPCTL0 / 4] & EPENA) != 0
                   && (regs[DOEPTSIZ0 / 4] & Ep0XfrSize) >= (uint)current.Data.Length)
                {
                    var armed = regs[DOEPTSIZ0 / 4] & Ep0XfrSize;
                    var len = (uint)current.Data.Length;
                    rx.Enqueue(new RxEntry {
                        Status = MakeStatus(0, len, PktStsOutData),
                        Data = current.Data });
                    rx.Enqueue(new RxEntry { Status = MakeStatus(0, 0, PktStsOutDone) });
                    regs[DOEPTSIZ0 / 4] = (regs[DOEPTSIZ0 / 4] & ~Ep0XfrSize)
                                          | (armed - len);
                    ep0DataStage = true;
                    UpdateRxLevel();
                    state = HostStep.CtlWait;
                    settle = 0;
                }
                else if(++settle > Patience)
                {
                    ControlTimeout("endpoint 0 was never armed for the OUT data stage");
                }
                break;

            case HostStep.CtlWait:
                // A STALL first, because it is NOT a timeout and it is not a
                // completion. The device refuses a request by setting the
                // STALL bit on endpoint 0 -- USBD_CtlError does exactly that
                // for any class request outside 0x01/0x81/0xF2/0xF3-0xF5 --
                // and it writes no IN data. Without this check the host sat
                // waiting for an IN stage that was never coming, and
                // something else set `inComplete` anyway, so it reported
                // SUCCESS for a request the application had refused. A test
                // control caught that: 0xF0 "completed".
                //
                // The bits are cleared here. On hardware the device clears
                // them when the next SETUP arrives; nothing in this model's
                // register copy does, so a single stall would make every
                // later request look stalled too.
                //
                // CAVEAT: this arm has never been seen to fire. A request the
                // application definitely refuses (0xF0) still reaches the
                // `inComplete` arm below, so whatever USBD_CtlError does on
                // this HAL is not visible here as DIEPCTL0.STALL. Until that
                // is understood, do NOT treat a completion as proof the
                // device accepted anything -- tests/test_dfu_handoff.py
                // discriminates on what the FIRMWARE does instead, and found
                // this out the hard way.
                if(((regs[DIEPCTL0 / 4] | regs[DOEPCTL0 / 4]) & EpStall) != 0)
                {
                    regs[DIEPCTL0 / 4] &= ~EpStall;
                    regs[DOEPCTL0 / 4] &= ~EpStall;
                    controlStalled++;
                    controlStatus = string.Format("stalled 0x{0:X2}", current.R.Code);
                    this.Log(LogLevel.Info,
                             "control request 0x{0:X2} STALLED by the device",
                             current.R.Code);
                    state = HostStep.Done;
                    settle = 0;
                    break;
                }
                // Otherwise the device's side is done when it has written its
                // IN stage: the data for a control read, or the zero-length
                // status for a write.
                if(inComplete)
                {
                    inComplete = false;
                    if(dataStageWasIn)
                    {
                        StatusOut();
                    }
                    controlDone++;
                    controlStatus = string.Format("ok 0x{0:X2}", current.R.Code);
                    this.Log(LogLevel.Info,
                             "control request 0x{0:X2} complete{1}",
                             current.R.Code,
                             ep0In.Count > 0
                                 ? string.Format(", {0} bytes back", ep0In.Count) : "");
                    state = HostStep.Done;
                    settle = 0;
                }
                else if(++settle > Patience)
                {
                    ControlTimeout(string.Format(
                        "no IN stage for request 0x{0:X2} -- the device most "
                        + "likely stalled it", current.R.Code));
                }
                break;

            case HostStep.WaitData:
                if(inComplete)
                {
                    inComplete = false;
                    if(dataStageWasIn)
                    {
                        StatusOut();
                    }
                    if(++request >= Enumeration.Length)
                    {
                        configured = true;
                        state = HostStep.Done;
                        this.Log(LogLevel.Info,
                                 "USB enumeration complete: device configured");
                    }
                    else
                    {
                        state = HostStep.ArmSetup;
                    }
                    settle = 0;
                }
                else if(++settle > Patience)
                {
                    Stall(string.Format("request {0} of {1} got no answer",
                                        request + 1, Enumeration.Length));
                }
                break;
            }
            UpdateInterrupt();
        }

        /// <summary>Stop and say so, rather than timing out into false progress.</summary>
        /// <summary>A control transfer that never completed. Not fatal: the
        /// host returns to idle so the next request can still be tried, and
        /// the reason is left in ControlStatus for whoever asked.</summary>
        private void ControlTimeout(string why)
        {
            this.Log(LogLevel.Warning, "control request 0x{0:X2} timed out: {1}",
                     current.R.Code, why);
            controlStatus = string.Format("timeout 0x{0:X2}: {1}", current.R.Code, why);
            state = HostStep.Done;
            settle = 0;
        }

        private void Stall(string why)
        {
            this.Log(LogLevel.Warning, "USB enumeration stalled: {0}", why);
            state = HostStep.Idle;
            pump.Enabled = false;
        }

        /// <summary>The host's zero-length OUT that ends a control read.
        ///
        /// Only when the device has armed the OUT endpoint for it.  Pushing it
        /// unconditionally leaves entries in the receive FIFO that nothing will
        /// ever read, and everything queued behind them is lost.</summary>
        private void StatusOut()
        {
            if((regs[DOEPCTL0 / 4] & EPENA) == 0)
            {
                return;
            }
            rx.Enqueue(new RxEntry { Status = MakeStatus(0, 0, PktStsOutData) });
            rx.Enqueue(new RxEntry { Status = MakeStatus(0, 0, PktStsOutDone) });
            regs[DOEPINT0 / 4] |= XFRC;
            UpdateRxLevel();
        }

        private void Setup(byte requestType, byte request, ushort value, ushort index, ushort length)
        {
            PushSetup(new byte[] {
                requestType, request,
                (byte)(value & 0xFF), (byte)(value >> 8),
                (byte)(index & 0xFF), (byte)(index >> 8),
                (byte)(length & 0xFF), (byte)(length >> 8) });
            settle = 0;
        }

        private void UpdateInterrupt()
        {
            var status = (regs[GINTSTS / 4] & ~(IEPINT | OEPINT)) | EndpointSummary();
            IRQ.Set((status & regs[GINTMSK / 4]) != 0
                    && (regs[GAHBCFG / 4] & GintMaskEnable) != 0);
        }

        private void Record(bool write, long offset, uint value)
        {
            if(Trace && trace.Count < TraceLimit)
            {
                trace.Add(Tuple.Create(write, offset, value));
            }
        }

        private readonly IMachine machine;
        private readonly uint[] regs;
        private readonly List<byte> packets;
        private readonly List<uint> fifoWrites;
        private readonly List<Tuple<bool, long, uint>> trace;
        private readonly Queue<RxEntry> rx;
        private readonly LimitTimer pump;
        private readonly Queue<byte[]> pendingOut = new Queue<byte[]>();
        private readonly Queue<Pending> pendingControl = new Queue<Pending>();
        private readonly List<byte> ep0In = new List<byte>();
        private Pending current;
        private bool ep0DataStage;
        private int controlDone;
        private int controlStalled;
        private string controlStatus = "none";
        private FileStream stream;
        private static readonly byte[] EmptyData = new byte[0];

        private HostStep state;
        private bool configured;
        private bool inComplete;
        private bool dataStageWasIn;
        private int settle;
        private int request;
        private int inEndpoint;
        private int inRemaining;
        private int inWritten;

        // How many 1 ms pumps to wait before moving on regardless.  Enumeration
        // that stalls should not wedge the machine; a step that times out is
        // visible in HostState and in the trace.
        private const int Patience = 400;
        private const uint FifoWords = 0x200;

        private const long GAHBCFG = 0x008, GRSTCTL = 0x010, GINTSTS = 0x014;
        private const long GINTMSK = 0x018, GRXSTSR = 0x01C, GRXSTSP = 0x020;
        private const long DSTS = 0x808, DIEPMSK = 0x810, DOEPMSK = 0x814;
        private const long DIEPEMPMSK = 0x834;
        private const long DAINT = 0x818, DAINTMSK = 0x81C;
        private const int Endpoints = 4;
        private const long DIEPCTL0 = 0x900, DIEPINT0 = 0x908, DIEPTSIZ0 = 0x910;
        private const long DTXFSTS0 = 0x918;
        private const long DIEPCTL1 = 0x920, DIEPINT1 = 0x928, DTXFSTS1 = 0x938;
        private const long DOEPCTL0 = 0xB00, DOEPINT0 = 0xB08, DOEPTSIZ0 = 0xB10;
        // (DOEPTSIZ0 + ep * 0x20 gives the other endpoints' transfer sizes.)
        private const long DOEPINT1 = 0xB28;

        private const long FifoBase = 0x1000, FifoWindowSize = 0x1000;
        private const int FifoWindows = 4;

        private const uint AHBIDL = 1u << 31, TXFFLSH = 1u << 5, RXFFLSH = 1u << 4;
        private const uint FCRST = 1u << 2, HSRST = 1u << 1, CSRST = 1u << 0;
        private const uint RXFLVL = 1u << 4, USBRST = 1u << 12, ENUMDNE = 1u << 13;
        private const uint IEPINT = 1u << 18, OEPINT = 1u << 19;
        private const uint Clearable = USBRST | ENUMDNE | (1u << 15) | (1u << 14)
                                       | (1u << 11) | (1u << 10) | (1u << 3);
        private const uint GintMaskEnable = 1u << 0;
        private const uint EPENA = 1u << 31, XFRC = 1u << 0, TXFE = 1u << 7, STUP = 1u << 3;
        private const uint PktStsSetupData = 6, PktStsSetupDone = 4;
        // The MIDI streaming interface's OUT endpoint, read from the device's
        // own configuration descriptor: "09 05 02 02 40 00" is endpoint 2,
        // bulk, 64-byte packets.  It is not endpoint 1, which is the IN side.
        private const int MidiOutEndpoint = 2;
        // The IN side of the same interface: "09 05 81 02 40 00" in the
        // descriptor, so endpoint 1, and FIFO window 1.
        private const int MidiInEndpoint = 1;
        private const uint PktStsOutData = 2, PktStsOutDone = 3;
        private const uint StupCnt = 3u << 29;      // DOEPTSIZ0: SETUP packet count
        // DOEPTSIZ0's transfer size is XFRSIZ[6:0] -- endpoint 0 only, where
        // the other endpoints have nineteen bits.
        private const uint Ep0XfrSize = 0x7F;
        // DIEPCTL/DOEPCTL bit 21: the device's "refuse this request" bit.
        private const uint EpStall = 1u << 21;
    }
}
