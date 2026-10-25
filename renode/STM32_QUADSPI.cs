//
// STM32 QUADSPI controller plus the serial NOR flash behind it.
//
// Ported from this project's earlier Python model of the same controller,
// which was derived from a boot trace of the real firmware rather than from
// the reference manual alone.  The comments that explain *why* a behaviour is
// the way it is are carried over, because each one records a bug that cost
// real time.
//
// `renode/boot.resc` loads this with `include $mf/STM32_QUADSPI.cs` before the
// platform file, where $mf is the renode/ directory.
//
using System;
using System.IO;
using Antmicro.Renode.Core;
using Antmicro.Renode.Core.Structure.Registers;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;

namespace Antmicro.Renode.Peripherals.SPI
{
    public class STM32_QUADSPI :
        IDoubleWordPeripheral, IWordPeripheral, IBytePeripheral, IKnownSize
    {
        public STM32_QUADSPI(IMachine machine)
        {
            this.machine = machine;
            IRQ = new GPIO();
            DmaRequest = new GPIO();
            flash = new NorFlash(FlashSize);
            regs = new uint[0x40 / 4];
            Reset();
        }

        public GPIO IRQ { get; private set; }

        // Renode's STM32DMA is an IGPIOReceiver: a peripheral raises a request
        // line and the GPIO number selects the stream.  Wire this to the stream
        // the firmware uses for QUADSPI (DMA2 stream 7 on the F446).
        public GPIO DmaRequest { get; private set; }
        public long Size => 0x1000;

        // Called from the monitor: `quadspi LoadImage @flash.img`
        public void LoadImage(string path)
        {
            var bytes = File.ReadAllBytes(path);
            var n = Math.Min(bytes.Length, flash.Data.Length);
            Array.Copy(bytes, flash.Data, n);
            this.Log(LogLevel.Info, "loaded {0} bytes of flash image from {1}", n, path);
        }

        // Called from the monitor: `quadspi SaveImage @flash-after.img`.
        // The firmware writes its own directories (samples, wavetables) when
        // it ingests data, so the cheapest way to learn a layout we have not
        // reversed is to let the device build it and then read the flash back.
        public void SaveImage(string path)
        {
            File.WriteAllBytes(path, flash.Data);
            this.Log(LogLevel.Info, "wrote {0} bytes of flash image to {1}",
                     flash.Data.Length, path);
        }

        // Which 4 KB pages the firmware actually READS.  Counting them is how
        // "where does this engine look for its data" becomes a measurement
        // instead of a guess -- render one preset, dump the trace, diff it
        // against a preset that works.  Off by default; it costs a counter
        // increment per flash read command when on.
        public void TraceReads(bool enabled) => flash.TraceReads = enabled;

        public void ClearReadTrace() => flash.ClearReadTrace();

        public void SaveReadTrace(string path)
        {
            using(var w = new StreamWriter(path))
            {
                var counts = flash.ReadCounts;
                for(var page = 0; page < counts.Length; page++)
                {
                    if(counts[page] != 0)
                    {
                        w.WriteLine("{0:X6} {1}", page * PageSize, counts[page]);
                    }
                }
            }
            this.Log(LogLevel.Info, "wrote flash read trace to {0}", path);
        }

        public void Reset()
        {
            Array.Clear(regs, 0, regs.Length);
            rx = new byte[0]; rxPos = 0;
            tx = new byte[0]; txLen = 0;
            tcf = false; smf = false;
            pendingValid = false; awaitingValid = false;
            IRQ.Set(false);
            DmaRequest.Set(false);
        }

        // --- bus access ---------------------------------------------------
        //
        // All three widths are implemented because the data register is
        // byte-addressable and DMA moves it one item at a time.  Popping four
        // bytes for an 8-bit item drains the FIFO four times too fast and
        // throws away three quarters of the data, which silently truncated
        // every preset read to noise under the Python harness.

        public byte ReadByte(long offset) => (byte)Read(offset, 1);
        public ushort ReadWord(long offset) => (ushort)Read(offset, 2);
        public uint ReadDoubleWord(long offset) => Read(offset, 4);

        public void WriteByte(long offset, byte value) => Write(offset, value, 1);
        public void WriteWord(long offset, ushort value) => Write(offset, value, 2);
        public void WriteDoubleWord(long offset, uint value) => Write(offset, value, 4);

        private uint Read(long offset, int size)
        {
            switch(offset)
            {
            case SR:
                return StatusValue();
            case DR:
                return ReadData(size);
            default:
                return offset < 0x40 ? regs[offset / 4] : 0;
            }
        }

        private void Write(long offset, uint value, int size)
        {
            if(offset >= 0x40)
            {
                return;
            }
            regs[offset / 4] = value;

            switch(offset)
            {
            case CR:
                // ABORT is self-clearing and completes the transfer: it flushes
                // the FIFO and raises TCF.  Storing the bit instead leaves the
                // driver waiting for a TCF no command is coming to produce, and
                // reading ABORT back as permanently set.
                if((value & CR_ABORT) != 0)
                {
                    rx = new byte[0]; rxPos = 0;
                    txLen = 0;
                    tcf = true;
                    regs[CR / 4] = value & ~CR_ABORT;
                }
                break;

            case FCR:
                if((value & SR_TCF) != 0) { tcf = false; }
                if((value & SR_SMF) != 0) { smf = false; }
                break;

            case CCR:
                // Writing CCR starts a new transaction, so anything half set up
                // is abandoned.  The driver programs CCR twice for a read --
                // HAL_QSPI_Command writes the command's functional mode, then
                // HAL_QSPI_Receive rewrites it with FMODE=indirect-read to start
                // the data phase.  Treating the first write as a data write
                // parks the transaction forever and leaves the driver stuck in
                // BUSY_INDIRECT_RX, so every later command returns HAL_BUSY.
                var wasArmed = awaitingValid;
                var previousCcr = lastCcr;
                awaitingValid = false;
                txLen = 0;
                pendingInstr = value & 0xFF;
                pendingFmode = (value >> 26) & 3;
                pendingAdmode = (value >> 10) & 3;
                pendingDmode = (value >> 24) & 3;
                pendingValid = true;
                lastCcr = value;
                if(pendingAdmode == 0)
                {
                    Begin();
                }
                else if(wasArmed && pendingFmode == 0 && pendingDmode != 0
                        && ((value ^ previousCcr) & ~FmodeMask) == 0)
                {
                    // HAL_QSPI_Transmit re-writes CCR with FMODE set to
                    // indirect-write just before pushing the data, and --
                    // unlike HAL_QSPI_Receive -- does not re-write AR
                    // afterwards.  Treating that as a brand new transaction
                    // therefore threw away the write this model had already
                    // armed on the AR write, FTF never came back, and the
                    // driver span in QSPI_WaitFlagStateUntilTimeout.  Every
                    // preset write hung there: the SysEx handler reached the
                    // page program and never returned to send its ack, which
                    // read from outside as the firmware ignoring writes.
                    // Re-arm instead, keeping the address already programmed.
                    Begin();
                }
                break;

            case AR:
                if(pendingValid)
                {
                    Begin();
                }
                break;

            case DR:
                // Width matters as much as it does on the read side: the data
                // register is byte-addressable and a driver pushing a page
                // program one byte at a time must contribute one byte, not a
                // zero-padded word.
                AppendTx(value, size);
                if(awaitingValid && txLen >= awaitingLength)
                {
                    awaitingValid = false;
                    FinishWrite();
                }
                break;
            }

            // Interrupt enables are level sensitive: arming one while its flag
            // is already set must raise the interrupt.  That is how a DMA
            // receive finishes -- the DMA completion callback arms the transfer
            // interrupt after the data has already moved, so updating only when
            // a command runs is always too early.
            UpdateInterrupts();
            ServiceDma();
        }

        // --- transaction ---------------------------------------------------

        private void Begin()
        {
            pendingValid = false;
            var addr = pendingAdmode != 0 ? regs[AR / 4] : 0;
            var length = regs[DLR / 4] + 1;
            if(length > (1 << 24))
            {
                length = 1;
            }
            if(pendingFmode == 0 && pendingDmode != 0)
            {
                awaitingInstr = pendingInstr;
                awaitingAddr = addr;
                awaitingLength = length;
                awaitingValid = true;
                return;
            }
            Execute(pendingInstr, pendingFmode, addr, length);
        }

        private void Execute(uint instr, uint fmode, uint addr, uint length)
        {
            var data = flash.Command(instr, addr, (int)length);
            if(fmode == 2)
            {
                // Auto-polling: the controller compares the byte the flash
                // returns against PSMAR under PSMKR and raises SMF on a match.
                var mask = regs[PSMKR / 4];
                var match = regs[PSMAR / 4];
                var status = data.Length > 0 ? data[0] : 0;
                smf = (status & mask) == (match & mask);
                tcf = true;
            }
            else
            {
                rx = data; rxPos = 0;
                // TCF means the programmed length has moved between the flash
                // and the FIFO, which is complete as soon as the command runs.
                // It does not mean the firmware has drained the FIFO -- FTF says
                // that -- and the driver waits on TCF *before* reading DR, so
                // latching it on drain instead deadlocks.
                tcf = true;
            }
            this.Log(LogLevel.Debug, "cmd 0x{0:X2} addr 0x{1:X6} len {2}", instr, addr, length);
            UpdateInterrupts();
        }

        private void FinishWrite()
        {
            if(awaitingInstr == WRSR || awaitingInstr == WRSR2)
            {
                flash.WriteStatus(tx, (int)awaitingLength);
            }
            else
            {
                flash.Program(awaitingInstr, awaitingAddr, tx, (int)awaitingLength);
            }
            txLen = 0;
            tcf = true;
            UpdateInterrupts();
        }

        private void AppendTx(uint value, int size)
        {
            if(tx.Length < txLen + size)
            {
                Array.Resize(ref tx, Math.Max(64, (txLen + size) * 2));
            }
            for(var i = 0; i < size; i++)
            {
                tx[txLen++] = (byte)(value >> (8 * i));
            }
        }

        // --- status ---------------------------------------------------------

        private uint StatusValue()
        {
            // FTF means the FIFO needs attention: bytes to read on an indirect
            // read, or room to write on an indirect write.  The write case
            // matters -- the driver waits on FTF before pushing the data phase,
            // so a model that only sets it for reads hangs every write.
            var available = rx.Length - rxPos;
            uint val = (available > 0 || awaitingValid) ? SR_FTF : 0;
            if(tcf) { val |= SR_TCF; }
            if(smf) { val |= SR_SMF; }
            return val | ((uint)Math.Min(available, 0x3F) << 8);
        }

        private uint ReadData(int size)
        {
            uint outv = 0;
            for(var i = 0; i < size; i++)
            {
                byte b = 0xFF;
                if(rxPos < rx.Length)
                {
                    b = rx[rxPos++];
                }
                outv |= (uint)b << (8 * i);
            }
            if(rxPos >= rx.Length)
            {
                tcf = true;
            }
            UpdateInterrupts();
            return outv;
        }

        private void UpdateInterrupts()
        {
            var cr = regs[CR / 4];
            var status = StatusValue();
            var set = false;
            // CR bits 16..20 enable TEF, TCF, FTF, SMF, TOF in that order.
            if((cr & (1u << 17)) != 0 && (status & SR_TCF) != 0) { set = true; }
            if((cr & (1u << 18)) != 0 && (status & SR_FTF) != 0) { set = true; }
            if((cr & (1u << 19)) != 0 && (status & SR_SMF) != 0) { set = true; }
            IRQ.Set(set);
        }

        // Renode's STM32DMA moves exactly one item per rising edge of the
        // request line, so draining a FIFO means one edge per item.  Measured:
        // holding the line high transferred a single byte of the three-byte
        // JEDEC ID and then stopped.
        //
        // The edges are driven in a loop rather than from inside ReadData,
        // because the DMA services the request synchronously -- re-arming from
        // the read would recurse once per item, which a 4096-byte preset read
        // would turn into 4096 stack frames.  Driving them here keeps it two
        // deep however long the transfer is.
        //
        // Timing note: the driver enables the stream, issues the command, and
        // only then sets DMAEN, so the data is already in the FIFO by the time
        // the request is allowed to matter.  A pulse emitted when the command
        // ran would arrive before DMAEN and be lost -- hence servicing on the
        // register write instead.
        private void ServiceDma()
        {
            if((regs[CR / 4] & CR_DMAEN) == 0 || servicingDma)
            {
                return;
            }
            servicingDma = true;
            try
            {
                while(rxPos < rx.Length)
                {
                    var before = rxPos;
                    DmaRequest.Set(false);
                    DmaRequest.Set(true);
                    if(rxPos == before)
                    {
                        // The stream did not take it -- disabled, or its item
                        // count is exhausted.  Stop rather than spin.
                        break;
                    }
                }
            }
            finally
            {
                DmaRequest.Set(false);
                servicingDma = false;
            }
        }

        private readonly IMachine machine;
        private readonly NorFlash flash;
        private uint[] regs;
        private byte[] rx; private int rxPos;
        private byte[] tx; private int txLen;
        private bool tcf, smf;
        private bool pendingValid, awaitingValid;
        private bool servicingDma;
        private uint pendingInstr, pendingFmode, pendingAdmode, pendingDmode;
        private uint lastCcr;
        private uint awaitingInstr, awaitingAddr, awaitingLength;

        private const int FlashSize = 16 * 1024 * 1024;
        private const int PageSize = 0x1000;   // the flash's own sector size,
                                               // and the preset slot stride

        private const long CR = 0x00, DCR = 0x04, SR = 0x08, FCR = 0x0C;
        private const long DLR = 0x10, CCR = 0x14, AR = 0x18, ABR = 0x1C;
        private const long DR = 0x20, PSMKR = 0x24, PSMAR = 0x28, PIR = 0x2C;

        private const uint SR_TEF = 1, SR_TCF = 2, SR_FTF = 4, SR_SMF = 8;
        private const uint FmodeMask = 3u << 26;
        private const uint CR_EN = 0x01, CR_ABORT = 0x02, CR_DMAEN = 0x04;
        private const uint WRSR = 0x01, WRSR2 = 0x31;

        // --- the flash itself ------------------------------------------------

        private class NorFlash
        {
            public NorFlash(int size)
            {
                Data = new byte[size];
                for(var i = 0; i < size; i++) { Data[i] = 0xFF; }
                ReadCounts = new uint[size / PageSize];
            }

            public byte[] Data { get; private set; }

            public bool TraceReads { get; set; }
            public uint[] ReadCounts { get; private set; }

            public void ClearReadTrace()
            {
                Array.Clear(ReadCounts, 0, ReadCounts.Length);
            }

            public byte[] Command(uint instr, uint addr, int length)
            {
                var n = Math.Max(length, 1);
                switch(instr)
                {
                case RSTEN: case RST: case WRDI:
                    wel = false; return Empty;
                case WREN:
                    wel = true; return Empty;
                case EN4B: fourByte = true; return Empty;
                case EX4B: fourByte = false; return Empty;
                case RDSR:
                    return Fill(Status(), n);
                case RDSR2: case RDSR3:
                    return Fill(sr2, n);
                case RDID: case QPIID:
                    // 0xAF is "read ID in QPI mode", a Macronix/ISSI idiom
                    // rather than the universal 0x9F.  The capacity agrees with
                    // the storage map: user wavetables live at
                    // (index + 0xF5F) << 12, which reaches ~15.4 MB.
                    var id = new byte[Math.Max(n, 3)];
                    for(var i = 0; i < id.Length; i++) { id[i] = Jedec[i % 3]; }
                    return id;
                case REMS:
                    return new byte[] { Jedec[0], Jedec[2] };
                case READ: case FAST_READ: case QREAD: case QIOREAD:
                case READ4: case FAST_READ4:
                    {
                        var start = (int)(addr % (uint)Data.Length);
                        var take = Math.Min(n, Data.Length - start);
                        if(TraceReads)
                        {
                            var first = start / PageSize;
                            var last = (start + take - 1) / PageSize;
                            for(var p = first; p <= last && p < ReadCounts.Length; p++)
                            {
                                ReadCounts[p]++;
                            }
                        }
                        var outv = new byte[take];
                        Array.Copy(Data, start, outv, 0, take);
                        return outv;
                    }
                case SE: case SE_ALT: case BE32: case BE64:
                    {
                        var span = (instr == SE || instr == SE_ALT) ? 0x1000
                                   : (instr == BE32 ? 0x8000 : 0x10000);
                        var start = (int)((addr / (uint)span) * (uint)span) % Data.Length;
                        for(var i = 0; i < span && start + i < Data.Length; i++)
                        {
                            Data[start + i] = 0xFF;
                        }
                        wel = false; return Empty;
                    }
                case CE1: case CE2:
                    for(var i = 0; i < Data.Length; i++) { Data[i] = 0xFF; }
                    wel = false; return Empty;
                default:
                    return Empty;
                }
            }

            public void WriteStatus(byte[] payload, int length)
            {
                if(length > 0) { sr1 = payload[0]; }
                if(length > 1) { sr2 = payload[1]; }
                wel = false;
            }

            public void Program(uint instr, uint addr, byte[] payload, int length)
            {
                if((instr != PP && instr != QPP) || length == 0) { return; }
                for(var i = 0; i < length; i++)
                {
                    // Real NOR can only clear bits without an erase; keep that,
                    // so a missing erase shows up as corruption here too rather
                    // than silently working.
                    var at = (int)((addr + (uint)i) % (uint)Data.Length);
                    Data[at] &= payload[i];
                }
                wel = false;
            }

            // Never busy: writes complete instantly here.
            private byte Status() => (byte)((sr1 & ~0x01) | (wel ? 0x02 : 0x00));

            private static byte[] Fill(byte value, int n)
            {
                var a = new byte[n];
                for(var i = 0; i < n; i++) { a[i] = value; }
                return a;
            }

            private byte sr1, sr2;
            private bool wel, fourByte;

            private static readonly byte[] Empty = new byte[0];
            private static readonly byte[] Jedec = { 0xC2, 0x20, 0x18 };

            private const uint RSTEN = 0x66, RST = 0x99, WRDI = 0x04, WREN = 0x06;
            private const uint RDSR = 0x05, RDSR2 = 0x35, RDSR3 = 0x15;
            private const uint RDID = 0x9F, QPIID = 0xAF, REMS = 0x90;
            private const uint READ = 0x03, FAST_READ = 0x0B, QREAD = 0x6B, QIOREAD = 0xEB;
            private const uint READ4 = 0x13, FAST_READ4 = 0x0C;
            private const uint PP = 0x02, QPP = 0x32;
            // 0xD7 is the other 4 KB sector-erase opcode, and it is the one
            // this firmware uses: writing a preset issues 0xD7 at the slot's
            // page address, then sixteen 256-byte page programs -- seventeen
            // write-enables in all.  Without it the erase was a no-op and the
            // program ANDed into the old contents, so a preset written over an
            // occupied slot came back as the bitwise AND of the two: writing
            // "Nervous" over "Aft-WavPad" read back as "@dp$Gar".  Only writes
            // into already-erased slots looked correct.
            private const uint SE = 0x20, SE_ALT = 0xD7;
            private const uint BE32 = 0x52, BE64 = 0xD8, CE1 = 0x60, CE2 = 0xC7;
            private const uint EN4B = 0xB7, EX4B = 0xE9;
        }
    }
}
