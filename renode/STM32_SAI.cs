//
// STM32 SAI block A, enough of it to carry the MicroFreak's audio out.
//
// The firmware drives SAI2 (base 0x40015C00) block A, writing 16-bit samples to
// ADR at +0x20 by DMA.  That was established by letting the access fault: the
// only unmapped access in a full boot was <0x40015C20, 0x40015C21>.  It agrees
// with earlier work, where the audio interrupts were DMA2 streams 4 and 6 --
// on the F446 those are SAI2_A and SAI2_B.
//
// This models the transmit path only.  Every 16-bit item the DMA pushes into
// ADR is captured; `sai2 SavePCM @out.raw` writes them out as little-endian
// signed 16-bit, which is what the hardware would hand its DAC.  The analog
// filter sits after that and is not in the firmware at all.
//
using System;
using System.Collections.Generic;
using System.IO;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Timers;
using Antmicro.Renode.Time;

namespace Antmicro.Renode.Peripherals.Sound
{
    public class STM32_SAI :
        IDoubleWordPeripheral, IWordPeripheral, IBytePeripheral, IKnownSize
    {
        public STM32_SAI(IMachine machine)
        {
            this.machine = machine;
            IRQ = new GPIO();
            DmaRequestA = new GPIO();
            samples = new List<short>();
            regs = new uint[0x20];

            // The SAI asks the DMA for data at the audio rate.  Nothing else
            // paces it, so without a clock here the transmit either never runs
            // or runs as fast as the host can go -- and the second is what made
            // early captures sound like a trill, because the capture
            // rate was tied to a harness constant instead of to time.
            //
            // Ticking per sample would mean 192,000 timer events a second, so
            // the timer ticks once per DMA request instead, and each tick is
            // as long as the chunk the previous request moved would take to
            // play: 64 items (32 stereo frames) at 192,000 items/s is 333 us.
            //
            // The cadence is not a free choice.  It ran at 1 kHz once, asking
            // for a millisecond of items per tick, and that bunched three
            // half-transfers into one burst: the DMA moves them synchronously
            // inside the timer callback, the NVIC latches a single interrupt,
            // and the HAL handler runs the HT and TC callbacks back to back
            // with no main-loop time between them.  The engine's render
            // callback treats a per-block flag that main() sets every loop
            // iteration as a heartbeat -- 75 blocks without it and the engine
            // idles, 25 *consecutive* blocks with it and it resumes -- so a
            // burst of two blocks always shows the second one an unset flag
            // and the engine never came back after its startup idle.  With one
            // half-buffer per tick the interrupts land 333 us apart, exactly as
            // the hardware's FIFO would space them, and main() runs between.
            //
            // And the half-buffer is not a fixed 64 items either: the engine
            // re-arms its stream with NDTR = 144 when synthesis resumes (36
            // stereo frames per half, the geometry FUN_08031df4 picks), and a
            // fixed 3 kHz tick then delivered 72 items every 333 us -- 9:8 too
            // many, measured as 432,000 samples in 2 s.  So the timer counts
            // *items* at the item rate, and its limit is however many items
            // the last request actually moved: the next request falls due
            // exactly when the FIFO would have drained that chunk, whatever
            // its size.
            tick = new LimitTimer(machine.ClockSource, ItemRate, this, "saiTick",
                                  limit: DefaultItemsPerRequest, direction: Direction.Ascending,
                                  enabled: false, eventEnabled: true);
            tick.LimitReached += OnTick;
            Reset();
        }

        public GPIO IRQ { get; private set; }
        public GPIO DmaRequestA { get; private set; }
        public long Size => 0x400;

        // Sample rate assumed for pacing.  96 kHz is not a guess: a rendered C4
        // measured 0 cents at that rate, and the firmware's own
        // rate byte reads 96.
        public uint SampleRate { get; set; }
        public uint Channels { get; set; }
        public int CapturedSamples
        {
            get { lock(sync) { return samples.Count; } }
        }

        public int Ticks => ticks;

        // The capture list is appended to by the CPU thread on every sample
        // and read by whoever types `sai2 SavePCM`, which is Renode's Monitor
        // thread.  Without this, saving from a *running* machine throws
        // "Collection was modified; enumeration operation may not execute"
        // part way through the foreach and takes Renode down with it.  It
        // never showed while every render used `emulation RunFor`, which
        // leaves the machine paused; a live session driving the firmware over
        // MIDI and capturing as it goes hits it immediately.
        public void SavePCM(string path)
        {
            short[] snapshot;
            lock(sync)
            {
                snapshot = samples.ToArray();
            }
            // Copy first, write afterwards: file I/O has no business holding
            // a lock the emulation thread wants for every sample.
            using(var f = new BinaryWriter(File.Create(path)))
            {
                foreach(var s in snapshot)
                {
                    f.Write(s);
                }
            }
            this.Log(LogLevel.Info, "wrote {0} samples ({1} frames) to {2}",
                     snapshot.Length, snapshot.Length / Math.Max(1, Channels), path);
        }

        public void ClearCapture()
        {
            lock(sync)
            {
                samples.Clear();
            }
        }

        public void Reset()
        {
            Array.Clear(regs, 0, regs.Length);
            lock(sync)
            {
                samples.Clear();
            }
            SampleRate = 96000;
            Channels = 2;
            tick.Enabled = false;
            tick.Limit = DefaultItemsPerRequest;
            IRQ.Set(false);
            DmaRequestA.Set(false);
        }

        public byte ReadByte(long offset) => (byte)Read(offset);
        public ushort ReadWord(long offset) => (ushort)Read(offset);
        public uint ReadDoubleWord(long offset) => Read(offset);

        public void WriteByte(long offset, byte value) => Write(offset, value);
        public void WriteWord(long offset, ushort value) => Write(offset, value);
        public void WriteDoubleWord(long offset, uint value) => Write(offset, value);

        private uint Read(long offset)
        {
            if(offset == ASR)
            {
                // FLVL = 0b000, "FIFO empty": the transmitter has always
                // drained what it was given, because this model consumes a
                // sample the instant the DMA hands it over.
                return 0;
            }
            return offset < 0x80 ? regs[offset / 4] : 0;
        }

        private void Write(long offset, uint value)
        {
            if(offset == ADR)
            {
                // A sample from the DMA.  Only the low 16 bits matter: the
                // firmware configures a 16-bit data size and the access that
                // faulted before this model existed was two bytes wide.
                lock(sync)
                {
                    samples.Add((short)(value & 0xFFFF));
                }
                return;
            }
            if(offset >= 0x80)
            {
                return;
            }
            regs[offset / 4] = value;
            if(offset == ACR1)
            {
                // Gate the transmit clock on SAIEN alone, not on SAIEN &&
                // DMAEN.  Measured: over 12 s of boot the firmware wrote ACR1
                // with both bits set exactly *once* and with SAIEN alone twice,
                // so requiring both left the clock stopped almost always. That
                // starved the engine's sample counter at 0x2001C7B0, which is
                // what the audio watchdog in main() checks -- eleven stalled
                // ~10 ms checks and it tears the SAI down and re-inits, which
                // is exactly the start/stop cycling that was observed.
                //
                // Gating on SAIEN is also the more faithful reading: the block's
                // clock runs when the block is enabled, and DMAEN only controls
                // whether it asserts the request line.  A request the stream
                // refuses costs nothing, so being permissive here is safe.
                var enabled = (value & ACR1_SAIEN) != 0;
                if(enabled != tick.Enabled)
                {
                    this.Log(LogLevel.Info, "block A transmit {0}",
                             enabled ? "started" : "stopped");
                }
                tick.Enabled = enabled;
            }
        }

        private void OnTick()
        {
            // One DMA request per tick.  How much it delivers depends on the
            // DMA: STM32F4DMA moves half a buffer, the stock model one item.
            // Whatever arrived sets the next tick's length, so pacing is
            // item-exact for either and for any buffer geometry.
            //
            // The edge is driven from here rather than from the ADR write
            // because the DMA services a request synchronously, so re-arming
            // from the write would recurse once per chunk.
            ticks++;
            if(servicing)
            {
                return;
            }
            servicing = true;
            try
            {
                // Both reads take the lock for the same reason SavePCM does:
                // a ClearCapture from the Monitor thread landing between them
                // would otherwise make `delivered` negative and set a nonsense
                // tick limit.
                int before, delivered;
                lock(sync)
                {
                    before = samples.Count;
                }
                DmaRequestA.Set(false);
                DmaRequestA.Set(true);
                lock(sync)
                {
                    delivered = samples.Count - before;
                }
                if(delivered > 0)
                {
                    // A refused request (stream disabled or exhausted) keeps
                    // the previous cadence and simply asks again next tick.
                    tick.Limit = (ulong)delivered;
                }
            }
            finally
            {
                DmaRequestA.Set(false);
                servicing = false;
            }
        }

        private readonly IMachine machine;
        private readonly LimitTimer tick;
        private readonly List<short> samples;
        private readonly object sync = new object();
        private uint[] regs;
        private bool servicing;
        private int ticks;

        // 96 kHz stereo, 16-bit items.  SampleRate/Channels above are
        // informational; Reset() runs after the timer is built, so the timer's
        // rate has to be a constant.
        private const long ItemRate = 96000 * 2;
        private const ulong DefaultItemsPerRequest = 64;

        private const long GCR = 0x00, ACR1 = 0x04, ACR2 = 0x08, AFRCR = 0x0C;
        private const long ASLOTR = 0x10, AIM = 0x14, ASR = 0x18, ACLRFR = 0x1C;
        private const long ADR = 0x20;

        private const uint ACR1_DMAEN = 1u << 17;
        private const uint ACR1_SAIEN = 1u << 16;
    }
}
