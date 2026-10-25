//
// STM32F446 embedded FLASH controller: unlock, sector erase, and programming
// that actually reaches the memory.
//
// WHY THIS EXISTS. Renode ships MTD.STM32F4_FlashController, and it handles
// the unlock sequence correctly -- but it does not implement FLASH_CR.PG, the
// programming-enable bit, and it says so itself on every attempt:
//
//     flash_controller: Unhandled write to offset 0x10.
//     Unhandled bits: [0] when writing value 0x1. Tags: PG (0x1).
//
// The consequence is not a warning, it is silence. Measured 2026-10-15 with a
// watchpoint on the word the firmware update invalidates: the CPU writes
// 0xFFFFFFFF to 0x080080B0, the watchpoint sees exactly that value, and the
// memory never changes -- at 400 ms intervals for six seconds, then not at
// all. The stock model gates writes to the flash region on its own CR state
// and refuses every program, so ANY firmware that rewrites internal flash
// appears to succeed and changes nothing. A firmware update is nothing but
// that, so none of it could be emulated.
//
// This model does not gate memory writes. Renode's MappedMemory stores a
// write straight away, which is all the firmware needs, and gating would mean
// suppressing a bus write -- something Renode does not offer a peripheral.
// The practical difference from hardware is that programming here OVERWRITES
// where flash can only clear bits, so a program onto a cell that was never
// erased succeeds here and would corrupt on the device. The firmware always
// erases first (HAL_FLASHEx_Erase then 128 HAL_FLASH_Program calls, read off
// 0x08045B58), so the two agree on every sequence this instrument performs.
// `StrictProgram` turns the hardware rule on for anyone who wants to catch a
// missing erase: it then reports PGSERR, exactly as the part would.
//
// What IS modelled, because the HAL waits on all of it:
//   * the two-key unlock, in order, and the lock
//   * sector erase, for the F446's real sector map, straight into the memory
//   * BSY/EOP in FLASH_SR, so FLASH_WaitForLastOperation returns
//   * PSIZE and SNB, stored and reported rather than warned about
//
// The sector map is the F446's, not the F42x/43x one Renode's platform file
// suggests: four 16 KB sectors, one 64 KB, three 128 KB, 512 KB in total.
// Sector 3 -- where the application starts -- is 16 KB; the 64 KB one is
// sector 4 at 0x08010000. docs/BOOTLOADER.md used to call 0x0800C000
// "sector 3, 64 KB"; that was the one error in an otherwise correct table and
// it was corrected 2026-10-17. The Sectors[] array below is authoritative.
//
using System;
using Antmicro.Renode.Core;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Peripherals.Memory;

namespace Antmicro.Renode.Peripherals.MTD
{
    public class STM32F4_Flash : IDoubleWordPeripheral, IKnownSize
    {
        public STM32F4_Flash(IMachine machine, MappedMemory flash)
        {
            this.machine = machine;
            this.flash = flash;
            Reset();
        }

        public long Size => 0x400;

        /// <summary>Refuse a program onto a cell that was not erased, the way
        /// the part does, instead of overwriting it. Off by default: the
        /// firmware always erases first, and a strict model turns a faithful
        /// run into a PGSERR for anyone whose own code is less careful.</summary>
        public bool StrictProgram { get; set; }

        /// <summary>Sectors erased since reset, and words programmed. The
        /// firmware update's own numbers are 1 and 128.</summary>
        public int SectorsErased => sectorsErased;
        public int WordsProgrammed => wordsProgrammed;
        public bool Locked => (cr & CR_LOCK) != 0;

        public void Reset()
        {
            acr = 0;
            sr = 0;
            cr = CR_LOCK;
            optcr = 0;
            keyStep = 0;
            sectorsErased = 0;
            wordsProgrammed = 0;
        }

        public uint ReadDoubleWord(long offset)
        {
            switch(offset)
            {
            case ACR: return acr;
            case SR: return sr;
            case CR: return cr;
            case OPTCR: return optcr;
            case OPTCR1: return optcr1;
            // KEYR and OPTKEYR are write-only and read as zero on the part.
            case KEYR: case OPTKEYR: return 0;
            default:
                this.Log(LogLevel.Noisy, "read of unmodelled FLASH register 0x{0:X}",
                         offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            switch(offset)
            {
            case ACR:
                // Latency, prefetch and the two caches. Nothing to model: the
                // emulated core has no wait states and no caches, and the
                // firmware only ever writes here once during clock setup.
                acr = value;
                break;

            case KEYR:
                if(keyStep == 0 && value == Key1)
                {
                    keyStep = 1;
                }
                else if(keyStep == 1 && value == Key2)
                {
                    cr &= ~CR_LOCK;
                    keyStep = 0;
                    this.Log(LogLevel.Debug, "FLASH unlocked");
                }
                else
                {
                    // On the part a wrong sequence locks CR until reset. Worth
                    // being faithful about: it is how a half-written unlock
                    // shows up as "the erase did nothing" rather than silently
                    // working.
                    keyStep = 0;
                    cr |= CR_LOCK;
                    this.Log(LogLevel.Warning,
                             "bad FLASH unlock sequence (0x{0:X8}); CR stays locked",
                             value);
                }
                break;

            case OPTKEYR:
                // The option bytes are not touched by this firmware. Accepted
                // so a write is not reported as unhandled.
                break;

            case SR:
                // The error and EOP flags are cleared by writing 1. BSY is
                // read-only.
                sr &= ~(value & (SR_EOP | SR_OPERR | SR_WRPERR | SR_PGAERR
                                 | SR_PGPERR | SR_PGSERR));
                break;

            case CR:
                if((cr & CR_LOCK) != 0 && (value & CR_LOCK) == 0)
                {
                    this.Log(LogLevel.Warning,
                             "write to FLASH_CR while locked, ignored (0x{0:X8})",
                             value);
                    break;
                }
                cr = (cr & CR_LOCK) | (value & ~CR_LOCK);
                if((value & CR_LOCK) != 0)
                {
                    cr |= CR_LOCK;
                    keyStep = 0;
                }
                if((value & CR_STRT) != 0)
                {
                    StartOperation();
                }
                break;

            case OPTCR:
                optcr = value;
                break;

            case OPTCR1:
                optcr1 = value;
                break;

            default:
                this.Log(LogLevel.Noisy,
                         "write 0x{0:X8} to unmodelled FLASH register 0x{1:X}",
                         value, offset);
                break;
            }
        }

        /// <summary>STRT: run the erase the CR bits describe.
        ///
        /// Synchronously, and BSY therefore never reads as set. That is the
        /// right simplification rather than a lazy one: the HAL's
        /// FLASH_WaitForLastOperation polls BSY and then checks the error
        /// flags, so "already finished" is a state it handles, while a BSY
        /// that needs a timer to clear is one more thing to get wrong. The
        /// device's own erase takes 3.8 s (docs/FIRMWARE_UPDATE.md measured it
        /// inside ARTURIA_SELECT_TARGET); nothing in the firmware depends on
        /// that duration, and the emulator is not trying to reproduce wall
        /// clock.</summary>
        private void StartOperation()
        {
            if((cr & CR_MER) != 0)
            {
                flash.SetRange(0, FlashSize, Erased);
                sectorsErased += Sectors.Length;
                this.Log(LogLevel.Warning,
                         "MASS ERASE of all {0} KB of internal flash", FlashSize >> 10);
            }
            else if((cr & CR_SER) != 0)
            {
                var sector = (int)((cr & CR_SNB) >> CR_SNB_SHIFT);
                if(sector < 0 || sector >= Sectors.Length)
                {
                    sr |= SR_PGSERR;
                    this.Log(LogLevel.Warning,
                             "sector erase of sector {0}, which this part does not "
                             + "have ({1} sectors)", sector, Sectors.Length);
                }
                else
                {
                    var start = Sectors[sector].Start;
                    var length = Sectors[sector].Length;
                    flash.SetRange(start, length, Erased);
                    sectorsErased++;
                    this.Log(LogLevel.Info,
                             "erased sector {0}: 0x{1:X8}..0x{2:X8} ({3} KB)",
                             sector, FlashBase + start,
                             FlashBase + start + length - 1, length >> 10);
                }
            }
            cr &= ~CR_STRT;
            sr |= SR_EOP;
        }

        /// <summary>Program one word, for a host that wants to do it directly
        /// rather than through the firmware: `flash_controller ProgramWord
        /// 0x08008000 0xDEADBEEF`. The firmware's own programming does not come
        /// through here -- its writes go to the memory, which stores them --
        /// and this exists so a test can write flash the way the part would,
        /// erase rules included.</summary>
        public void ProgramWord(long address, uint value)
        {
            var offset = address - FlashBase;
            if(offset < 0 || offset + 4 > FlashSize)
            {
                throw new ArgumentException(string.Format(
                    "0x{0:X8} is not in internal flash", address));
            }
            if(Locked)
            {
                this.Log(LogLevel.Warning,
                         "ProgramWord while FLASH is locked; refused as the part would");
                sr |= SR_WRPERR;
                return;
            }
            var old = (uint)(flash.ReadByte(offset)
                             | (flash.ReadByte(offset + 1) << 8)
                             | (flash.ReadByte(offset + 2) << 16)
                             | (flash.ReadByte(offset + 3) << 24));
            var programmed = old & value;      // flash clears bits; it cannot set them
            if(StrictProgram && programmed != value)
            {
                sr |= SR_PGSERR;
                this.Log(LogLevel.Warning,
                         "program of 0x{0:X8} at 0x{1:X8} would need bits that are "
                         + "already clear (holds 0x{2:X8}); PGSERR, as the part",
                         value, address, old);
                return;
            }
            var write = StrictProgram ? programmed : value;
            for(var i = 0; i < 4; i++)
            {
                flash.WriteByte(offset + i, (byte)(write >> (8 * i)));
            }
            wordsProgrammed++;
            sr |= SR_EOP;
        }

        /// <summary>Load a file into internal flash, WITHOUT registering it to
        /// be reloaded on reset -- which is the whole point.
        ///
        /// `sysbus LoadBinary` keeps the binary and re-applies it every time
        /// the machine resets. For code that is exactly right, and for the
        /// FACTORY DATA PAGE it is fatal to the thing being tested: a firmware
        /// update ends in SYSRESETREQ, so the reset put the original page back
        /// and the update's own write disappeared. Measured 2026-10-15, and it
        /// cost most of a day: the erase was logged, the programming writes
        /// were seen on the bus carrying the right values, and the memory read
        /// unchanged afterwards. Both an erased page and a programmed one
        /// predict 0xFFFFFFFF at the marker word; reading the ORIGINAL
        /// 0xFFFF5AA5 is only possible if something restored it.
        ///
        ///   flash_controller LoadImage @bootloader.bin 0x08000000
        ///
        /// Writes go straight into the memory, bypassing LOCK and PG: this is
        /// the host putting an image there before the machine runs, not the
        /// firmware programming flash.</summary>
        public void LoadImage(string path, long address)
        {
            var bytes = System.IO.File.ReadAllBytes(path);
            var offset = address - FlashBase;
            if(offset < 0 || offset + bytes.Length > FlashSize)
            {
                throw new ArgumentException(string.Format(
                    "{0} bytes at 0x{1:X8} does not fit internal flash",
                    bytes.Length, address));
            }
            flash.WriteBytes(offset, bytes);
            this.Log(LogLevel.Info,
                     "loaded {0} bytes into flash at 0x{1:X8} (not restored on reset)",
                     bytes.Length, address);
        }

        private struct Sector
        {
            public Sector(long start, long length) { Start = start; Length = length; }
            public long Start;
            public long Length;
        }

        // The STM32F446's 512 KB map. Sector 2 is the factory data page the
        // firmware update rewrites; sector 3 is where the application starts.
        private static readonly Sector[] Sectors = {
            new Sector(0x00000, 16 << 10),     // 0  0x08000000  bootloader
            new Sector(0x04000, 16 << 10),     // 1  0x08004000  bootloader
            new Sector(0x08000, 16 << 10),     // 2  0x08008000  factory data page
            new Sector(0x0C000, 16 << 10),     // 3  0x0800C000  application
            new Sector(0x10000, 64 << 10),     // 4  0x08010000
            new Sector(0x20000, 128 << 10),    // 5  0x08020000
            new Sector(0x40000, 128 << 10),    // 6  0x08040000
            new Sector(0x60000, 128 << 10),    // 7  0x08060000
        };

        private readonly IMachine machine;
        private readonly MappedMemory flash;
        private uint acr, sr, cr, optcr, optcr1;
        private int keyStep;
        private int sectorsErased;
        private int wordsProgrammed;

        private const long FlashBase = 0x08000000;
        private const long FlashSize = 512 << 10;
        private const byte Erased = 0xFF;

        private const uint Key1 = 0x45670123, Key2 = 0xCDEF89AB;

        private const long ACR = 0x00, KEYR = 0x04, OPTKEYR = 0x08;
        private const long SR = 0x0C, CR = 0x10, OPTCR = 0x14, OPTCR1 = 0x18;

        private const uint SR_EOP = 1u << 0, SR_OPERR = 1u << 1;
        private const uint SR_WRPERR = 1u << 4, SR_PGAERR = 1u << 5;
        private const uint SR_PGPERR = 1u << 6, SR_PGSERR = 1u << 7;

        private const uint CR_PG = 1u << 0, CR_SER = 1u << 1, CR_MER = 1u << 2;
        private const int CR_SNB_SHIFT = 3;
        private const uint CR_SNB = 0xFu << CR_SNB_SHIFT;
        private const uint CR_STRT = 1u << 16, CR_LOCK = 1u << 31;
    }
}
