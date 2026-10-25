// STM32F4 DMA with circular mode, peripheral requests and half-transfer.
//
// Provenance, in two steps:
//
//  1. Renode's own 1.15.0 STM32DMA.cs (MIT, Antmicro), which implements neither
//     circular mode nor peripheral requests -- it starts a transfer as soon as
//     the stream config register is written.  See renode/renode issue #194.
//  2. STM32DMA_Fixed.cs from RallySmith/renode-issue-stm32f2-spi, branch
//     "fixed", attached to renode/renode issue #615.  That adds circular mode
//     and OnGPIO peripheral requests.  It is an unmerged third-party fix: the
//     author says a PR will follow "at a point when the maintainers are happy".
//
// Added here: the half-transfer interrupt (HTIF/HTIE), which neither of the
// above implements, and half-buffer chunking for memory-to-peripheral circular
// transfers.  The MicroFreak's audio DMA is a double-buffered circular stream
// and the firmware relies on HT and TC to know which half to render into next
// -- with TC only it would refill one half and repeat the other.
//
// Original header follows.
//
// Derived from 1.15.0 STM32DMA.cs
//
// Fix TX and RX transfers and maintain DMA pending request state.
// Fix DMA_SxNDTR access.
// Fix transferredSize reset on DMA stream disable.
// Fix DoPeripheralTransfer() to only increment memory when configured.
// Maintain FIFOControl to allow software access.
// Lots of LogLevel.Debug messages added for diagnostics.
//
// Copyright (c) 2010-2023 Antmicro
// Copyright (c) 2011-2015 Realtime Embedded
//
// This file is licensed under the MIT License.
// Full license text is available in 'licenses/MIT.txt'.
//
using System;
using Antmicro.Renode.Peripherals.Bus;
using Antmicro.Renode.Logging;
using Antmicro.Renode.Core;
using System.Collections.Generic;
using System.Linq;

namespace Antmicro.Renode.Peripherals.DMA
{
    public sealed class STM32F4DMA : IDoubleWordPeripheral, IKnownSize, IGPIOReceiver, INumberedGPIOOutput
    {
        public STM32F4DMA(IMachine machine)
        {
            streamFinished = new bool[NumberOfStreams];
            streamHalf = new bool[NumberOfStreams];
            streams = new Stream[NumberOfStreams];
            for(var i = 0; i < streams.Length; i++)
            {
                streams[i] = new Stream(this, i);
            }
            this.machine = machine;
            engine = new DmaEngine(machine.GetSystemBus(this));
            Reset();
        }

        public IReadOnlyDictionary<int, IGPIO> Connections
        {
           get
           {
              var i = 0;
              return streams.ToDictionary(x => i++, y => (IGPIO)y.IRQ);
           }
        }

        // DELIBERATE DEVIATION FROM HARDWARE, default OFF.
        //
        // Turning this on makes audio *look* like it works and it does not.
        // Measured with it on: the DMA ran at full rate (66,996 half-transfers
        // in 12 s) while the stream 4 ISR still fired once, so the firmware
        // never rendered and the same half-buffer was replayed -- a perfect
        // 64-frame repeat, correlation 1.00, at 1500 Hz instead of C4's 261.58.
        // It is left here switchable because the measurement is informative,
        // but it must not be the default: this project has been misled by
        // plausible-looking audio before.
        //
        // The firmware arms stream 4 with CIRC clear and re-arms it from its
        // transfer-complete callback.  Under this harness that re-arm does not
        // happen, so the audio stream fires once and dies.
        //
        // Root cause, from HAL_DMA_IRQHandler at 0x0801B31C: on TCIF it clears
        // the flag and then does `CR &= ~(DMEIE|TEIE|TCIE)` -- the HAL disables
        // the completion interrupt, as it should for a normal-mode transfer --
        // and leaves re-arming to HAL_SAI_Transmit_DMA.  So exactly one ISR
        // fires however the interrupt is signalled, which is what was observed
        // for level, deferred and edge signalling alike.  The unresolved
        // question is why the SAI transmit-complete callback does not re-arm.
        //
        // With this set, a completed memory-to-peripheral stream stays armed and
        // wraps instead of disarming.  Earlier work reached
        // working audio the same way, treating the SAI streams as permanently
        // active.  It is a workaround for an unexplained gap in the re-arm path,
        // not a model of the hardware, and it should be turned off once that
        // gap is understood.
        public bool KeepAudioStreamsArmed { get; set; } = false;

        // Diagnostics: how many peripheral requests each stream took or dropped.
        public int AcceptedStream4 => accepted[4];
        public int RefusedStream4 => refused[4];

        public long Size
        {
            get
            {
                return 0x400;
            }
        }

        public uint ReadDoubleWord(long offset)
        {
            //this.Log(LogLevel.Debug, "ReadDoubleWord: offset=0x{0:X}", offset);
            switch((Registers)offset)
            {
            case Registers.LowInterruptStatus:
            case Registers.HighInterruptStatus:
                //return HandleInterruptRead((int)(offset/4));
                uint rval = HandleInterruptRead((int)(offset/4));
                this.Log(LogLevel.Debug, "ReadDoubleWord: DMA_xISR: offset=0x{0:X} val 0x{1:X}", offset, rval);
                return rval;
            default:
                if(offset >= StreamOffsetStart && offset <= StreamOffsetEnd)
                {
                    offset -= StreamOffsetStart;
                    //return streams[offset / StreamSize].Read(offset % StreamSize);
                    uint srval = streams[offset / StreamSize].Read(offset % StreamSize);
                    this.Log(LogLevel.Debug, "ReadDoubleWord: offset=0x{0:X} (stream {1}) val 0x{2:X}", (StreamOffsetStart + offset), (offset / StreamSize), srval);
                    return srval;
                }
                this.LogUnhandledRead(offset);
                this.Log(LogLevel.Debug, "ReadDoubleWord: Unhandled: offset=0x{0:X} returning 0x0", offset);
                return 0;
            }
        }

        public void WriteDoubleWord(long offset, uint value)
        {
            this.Log(LogLevel.Debug, "WriteDoubleWord: offset 0x{0:X} value 0x{1:X}", offset, value);
            switch((Registers)offset)
            {
            case Registers.LowInterruptClear:
            case Registers.HighInterruptClear:
                HandleInterruptClear((int)((offset - 8)/4), value);
                break;
            default:
                if(offset >= StreamOffsetStart && offset <= StreamOffsetEnd)
                {
                    offset -= StreamOffsetStart;
                    streams[offset / StreamSize].Write(offset % StreamSize, value);
                }
                else
                {
                    this.LogUnhandledWrite(offset, value);
                }
                break;
            }
        }

        public void Reset()
        {
            streamFinished.Initialize();
            streamHalf.Initialize();
            foreach(var stream in streams)
            {
                stream.Reset();
            }
        }

        // This OnGPIO() method allows arbitrary stream# indication
        // for the controller instance. However the source peripheral
        // for each stream is actually defined by the DMA_SxCR:CHSEL
        // and the specific STM32 variant controller mapping
        // (e.g. RM0033 rev9 Table 22 and Table 23).
        //
        // CONSIDER:IMPLEMENT: We may be able to use IsConnected and
        // Endpoints and Connect methods to dynamically change the DMA
        // channel stream mapping at run-time to avoid hardwiring in
        // stm32f2.repl

        public void OnGPIO(int number, bool value)
        {
            this.Log(LogLevel.Debug, "OnGPIO: number {0} value {1}", number, value);
            if(number < 0 || number >= streams.Length)
            {
                this.Log(LogLevel.Error, "Attempted to start non-existing DMA stream number: {0}. Maximum value is {1}", number, streams.Length);
                return;
            }

            // Allow (for Set/Unset cases) the current pending DMA request state to be held:
            streams[number].DMARequest = value;

            if(value)
            {
                // direction is private to streams
                //this.Log(LogLevel.Debug, "DMA peripheral request on stream {0} {1} : direction {2}", number, value, streams[number].direction);
                this.Log(LogLevel.Debug, "DMA peripheral request on stream {0} {1} : Enabled {2}", number, value, streams[number].Enabled);
                if(streams[number].Enabled)
                {
                    accepted[number]++;
                    streams[number].SelectTransfer();
                }
                else
                {
                    refused[number]++;
                    this.Log(LogLevel.Debug, "DMA peripheral request on stream {0} ignored", number);
                }
            }
        }

        private uint HandleInterruptRead(int offset)
        {
            this.Log(LogLevel.Debug, "HandleInterruptRead: offset {0}", offset);
            lock(streamFinished)
            {
                var returnValue = 0u;
                for(var i = 4 * offset; i < 4 * (offset + 1); i++)
                {
                    this.Log(LogLevel.Debug, "HandleInterruptRead: streamFinished[{0}] {1}", i, streamFinished[i]);
                    if(streamFinished[i])
                    {
                        returnValue |= 1u << BitNumberForStream(i - 4 * offset);
                    }
                    // HTIF sits one bit below TCIF for every stream: the F4
                    // status layout per stream is FEIF, _, DMEIF, TEIF, HTIF,
                    // TCIF, so 4/10/20/26 against 5/11/21/27.
                    if(streamHalf[i])
                    {
                        returnValue |= 1u << (BitNumberForStream(i - 4 * offset) - 1);
                    }
                }
                return returnValue;
            }
        }

        private void HandleInterruptClear(int offset, uint value)
        {
            this.Log(LogLevel.Debug, "HandleInterruptClear: offset {0} value 0x{1:X}", offset, value);
            lock(streamFinished)
            {
                for(var i = 4 * offset; i < 4 * (offset + 1); i++)
                {
                    var bitNo = BitNumberForStream(i - 4 * offset);
                    if((value & (1 << bitNo)) != 0)
                    {
                        this.Log(LogLevel.Debug, "HandleInterruptClear: clearing streamFinished[{0}]", i);
                        streamFinished[i] = false;
                    }
                    if((value & (1 << (bitNo - 1))) != 0)
                    {
                        streamHalf[i] = false;
                    }
                    if(!streamFinished[i] && !streamHalf[i])
                    {
                        streams[i].IRQ.Unset();
                    }
                }
            }
        }

        private static int BitNumberForStream(int streamNo)
        {
            switch(streamNo)
            {
            case 0:
                return 5;
            case 1:
                return 11;
            case 2:
                return 21;
            case 3:
                return 27;
            default:
                throw new InvalidOperationException("Should not reach here.");
            }
        }

        private readonly bool[] streamFinished;
        private readonly bool[] streamHalf;
        private readonly int[] accepted = new int[NumberOfStreams];
        private readonly int[] refused = new int[NumberOfStreams];
        private readonly Stream[] streams;
        private readonly DmaEngine engine;
        private readonly IMachine machine;

        private const int NumberOfStreams = 8;
        private const int StreamOffsetStart = 0x10;
        private const int StreamOffsetEnd = 0xCC;
        private const int StreamSize = 0x18;

        private enum Registers
        {
            LowInterruptStatus = 0x0, // DMA_LISR
            HighInterruptStatus = 0x4, // DMA_HISR
            LowInterruptClear = 0x8, //DMA_LIFCR
            HighInterruptClear = 0xC // DMA_HIFCR
        }

        private class Stream
        {
            public Stream(STM32F4DMA parent, int streamNo)
            {
                this.parent = parent;
                this.streamNo = streamNo;
                IRQ = new GPIO();
            }

            public uint Read(long offset)
            {
                parent.Log(LogLevel.Debug, "STM32DMA:Stream:Read:[{0}] offset=0x{1:X}", streamNo, offset);
                switch((Registers)offset)
                {
                case Registers.Configuration:
                    return HandleConfigurationRead();
                case Registers.NumberOfData:
                    return (uint)numberOfData;
                case Registers.PeripheralAddress:
                    return peripheralAddress;
                case Registers.Memory0Address:
                    return memory0Address;
                case Registers.Memory1Address:
                    return memory1Address;
                case Registers.FIFOControl:
                    return fifoControl;
                default:
                    parent.Log(LogLevel.Warning, "Unexpected read access from not implemented register (offset 0x{0:X}).", offset);
                    return 0;
                }
            }

            public void Write(long offset, uint value)
            {
                parent.Log(LogLevel.Debug, "STM32DMA:Stream:Write:[{0}] offset=0x{1:X} value=0x{2:X}", streamNo, offset, value);
                switch((Registers)offset)
                {
                case Registers.Configuration:
                    HandleConfigurationWrite(value);
                    break;
                case Registers.NumberOfData: // TODO: This register is RO if DMA_SxCR:EN==1
                    // When PSIZE != MSIZE the DMA_SxNDTR value is the
                    // count of PSIZE transfers
                    numberOfData = (int)(value & 0xFFFF);
                    // For CIRC and re-enable we need to reload the original value:
                    numberOfDataLatch = numberOfData;
                    break;
                case Registers.PeripheralAddress: // TODO: This register is RO if DMA_SxCR:EN==1
                    peripheralAddress = value;
                    break;
                case Registers.Memory0Address: // TODO: This register is RO if DMA_SxCR:EN==1
                    memory0Address = value;
                    break;
                case Registers.Memory1Address: // TODO: This register is RO if DMA_SxCR:EN==1
                    memory1Address = value;
                    break;
                case Registers.FIFOControl:
                    if(0x00000000 != (value & 0xFFFFF400))
                    {
                        parent.Log(LogLevel.Warning, "Unexpected FIFOControl write to reserved bits (value 0x{0:X}).", value);
                    }
                    fifoControl = value;
                    break;
                default:
                    parent.Log(LogLevel.Warning, "Unexpected write access to not implemented register (offset 0x{0:X}, value 0x{1:X}).", offset, value);
                    break;
                }
            }

            public GPIO IRQ { get; private set; }

            public void Reset()
            {
                memory0Address = 0u;
                memory1Address = 0u;
                numberOfData = 0;
                numberOfDataLatch = 0;
                transferredSize = 0;
                fifoControl = 0x21; // RM0033 rev 9 9.5.10
                memoryTransferType = TransferType.Byte;
                peripheralTransferType = TransferType.Byte;
                memoryIncrementAddress = false;
                peripheralIncrementAddress = false;
                direction = Direction.PeripheralToMemory;
                interruptOnComplete = false;
                interruptOnHalf = false;
                Enabled = false;
                DMARequest = false;
            }

            private Request CreateRequest(int? size = null, int? destinationOffset = null, int sourceOffset = 0)
            {
                var sourceAddress = 0u;
                var destinationAddress = 0u;
                switch(direction)
                {
                case Direction.PeripheralToMemory:
                case Direction.MemoryToMemory:
                    sourceAddress = peripheralAddress;
                    destinationAddress = memory0Address;
                    break;
                case Direction.MemoryToPeripheral:
                    sourceAddress = memory0Address;
                    destinationAddress = peripheralAddress;
                    break;
                }

                var sourceTransferType = direction == Direction.PeripheralToMemory ? peripheralTransferType : memoryTransferType;
                var destinationTransferType = direction == Direction.MemoryToPeripheral ? peripheralTransferType : memoryTransferType;
                var incrementSourceAddress = direction == Direction.PeripheralToMemory ? peripheralIncrementAddress : memoryIncrementAddress;
                var incrementDestinationAddress = direction == Direction.MemoryToPeripheral ? peripheralIncrementAddress : memoryIncrementAddress;
                parent.Log(LogLevel.Debug, "CreateRequest: Request: source 0x{0:X} destination 0x{1:X} size {2} readTransferType {3} writeTransferType {4} incrementReadAddress {5} incrementWriteAddress {6}", sourceAddress, (uint)(destinationAddress + (destinationOffset ?? 0)), size ?? numberOfData, sourceTransferType, destinationTransferType, incrementSourceAddress, incrementDestinationAddress);
                return new Request((uint)(sourceAddress + sourceOffset), (uint)(destinationAddress + (destinationOffset ?? 0)), size ?? numberOfData, sourceTransferType, destinationTransferType,
                        incrementSourceAddress, incrementDestinationAddress);
            }


            // If doing 16- or 32-bit DMA transfers (PSIZE/MSIZE) then
            // we need to treat a single transfer as 1-item of PSIZE.
	    //
            // If circular then we do not auto-disable but should
            // reset numberOfData = numberOfDataLatch and continue DMA
            // requests

            // A memory-to-peripheral stream is an audio output: the firmware
            // fills one half of the block while the DMA plays the other, and it
            // learns which half from HT and TC.  Moving the whole buffer per
            // request and raising only TC would make it refill one half and
            // repeat the other, so this walks the buffer in halves.
            //
            // This applies to one-shot transfers as well as circular ones.
            // Measured on the MicroFreak: the firmware *clears* CIRC before
            // enabling stream 4 (CR 0x6022D40 -> 0x6022C40) and re-arms from
            // the completion callback instead -- HAL_SAI_Transmit_DMA ran 103
            // times in 90 s under the previous harness.  Restricting halving to
            // circular streams therefore missed the audio path entirely: the
            // stream took one whole-buffer transfer, disabled itself, and every
            // later request was refused, which starved the engine's sample
            // counter and left the audio watchdog cycling.
            // Signal as an edge, not a level.  GPIO.Set() on an already-high
            // line is a no-op, so a level that the firmware never clears the way
            // this model expects wedges the interrupt permanently: measured,
            // 66,996 half-transfers produced exactly one stream 4 ISR, so the
            // firmware never rendered and the DMA replayed one half-buffer --
            // audible as a perfect 64-frame repeat rather than the note.
            // Pulsing lets the NVIC latch each completion; the status flags stay
            // for the HAL to read and clear as it likes.
            private void Pulse()
            {
                IRQ.Set();
                IRQ.Unset();
            }

            public void DoHalfTransfer()
            {
                var half = numberOfDataLatch / 2;
                // CreateRequest's size is in *bytes* -- the peripheral-to-memory
                // path passes (int)memoryTransferType per item -- while `half`
                // is an item count.  Passing the item count moved 64 bytes = 32
                // halfwords per request, so each "half" of the 128-item audio
                // buffer carried only its first 32 samples and the SAI, looping
                // until it had a millisecond's worth, took six requests per
                // tick where three were due.  Measured: 6 stream-4 requests per
                // 1 kHz tick and 2 render callbacks 44 us apart.  The half
                // must be moved in full, so the size is scaled by the item width.
                var request = CreateRequest(half * (int)memoryTransferType, null, transferredSize * (int)memoryTransferType);
                if(request.Size == 0)
                {
                    return;
                }
                lock(parent.streamFinished)
                {
                    parent.engine.IssueCopy(request);
                    transferredSize += half;
                    if(transferredSize >= numberOfDataLatch)
                    {
                        transferredSize = 0;
                        if(!circular && !parent.KeepAudioStreamsArmed)
                        {
                            // One-shot: the stream disarms and the firmware
                            // re-arms it from its completion callback.
                            Enabled = false;
                        }
                        parent.streamFinished[streamNo] = true;
                        // Raised directly, not through
                        // ExecuteInNearestSyncedState.  The deferral in the
                        // inherited paths exists because those are entered from
                        // a CPU register write; this path is entered from the
                        // SAI's pacing timer, and deferring from there left the
                        // stream 4 handler running once in 12 s while the SAI
                        // was armed twice -- the audio was being driven by the
                        // watchdog rather than by transfer completion.
                        if(interruptOnComplete)
                        {
                            Pulse();
                        }
                    }
                    else
                    {
                        parent.streamHalf[streamNo] = true;
                        if(interruptOnHalf)
                        {
                            Pulse();
                        }
                    }
                }
            }

            public void DoTransfer()
            {
                parent.Log(LogLevel.Debug, "DoTransfer: entry: memoryTransferType {0} transferredSize {1} direction {2}", (int)memoryTransferType, transferredSize, direction);

                var request = CreateRequest();
                parent.Log(LogLevel.Debug, "DoTransfer: request.Size {0}", request.Size);
                if(request.Size > 0)
                {
                    lock(parent.streamFinished)
                    {
                        parent.engine.IssueCopy(request);
                        if (circular)
                        {
                            numberOfData = numberOfDataLatch;
                            parent.Log(LogLevel.Debug, "DoTransfer: circular: reset numberOfData {0}", numberOfData);
                        }
                        else
                        {
                            numberOfData -= (request.Size / (int)peripheralTransferType);
                            Enabled = false;
                        }
                        parent.streamFinished[streamNo] = true;
                        parent.Log(LogLevel.Debug, "DoTransfer: setting streamFinished[{0}] true : interruptOnComplete {1} numberOfData {2}", streamNo, interruptOnComplete, numberOfData);
                        if(interruptOnComplete)
                        {
                            parent.machine.LocalTimeSource.ExecuteInNearestSyncedState(_ => IRQ.Set());
                        }
                    }
                }
            }

            public void DoPeripheralTransfer()
            {
                parent.Log(LogLevel.Debug, "DoPeripheralTransfer: entry: memoryTransferType {0} transferredSize {1} direction {2}", (int)memoryTransferType, transferredSize, direction);
                // NOTE: Since we are only called for P2M the
                // transferredSize here is used as the
                // "destinationOffset" index when incrementing the
                // memory address
                var request = CreateRequest((int)memoryTransferType, (memoryIncrementAddress ? transferredSize : 0));
                transferredSize += (int)memoryTransferType;
                parent.Log(LogLevel.Debug, "DoPeripheralTransfer: request.Size {0} transferredSize {1} : numberOfData {2}", request.Size, transferredSize, numberOfData);
                if(request.Size > 0)
                {
                    lock(parent.streamFinished)
                    {
                        parent.engine.IssueCopy(request);
                        parent.Log(LogLevel.Debug, "DoPeripheralTransfer: request.Size {0} transferredSize {1} numberOfData {2}  peripheralTransferType {3} ({4})", request.Size, transferredSize, numberOfData, peripheralTransferType, (numberOfData * (int)peripheralTransferType));
                        // TODO:CONSIDER:OPTIMISE: We should be able
                        // to cache the (numberOfData * (int)peripheralTransferType)
                        // value when the DMA_SxCR is written since at
                        // that point it is fixed; and it will avoid
                        // the repeated multiplication operation here.
                        if(transferredSize == (numberOfData * (int)peripheralTransferType))
                        {
                            if (circular)
                            {
                                numberOfData = numberOfDataLatch;
                                parent.Log(LogLevel.Debug, "DoPeripheralTransfer: circular: reset numberOfData {0}", numberOfData);
                            }
                            else
                            {
                                numberOfData -= (transferredSize / (int)peripheralTransferType);
                                Enabled = false;
                            }
                            parent.Log(LogLevel.Debug, "DoPeripheralTransfer: setting streamFinished[{0}] true : interruptOnComplete {1} numberOfData {2}", streamNo, interruptOnComplete, numberOfData);
                            parent.streamFinished[streamNo] = true;
                            transferredSize = 0;
                            if(interruptOnComplete)
                            {
                                parent.machine.LocalTimeSource.ExecuteInNearestSyncedState(_ => IRQ.Set());
                            }
                        }
                    }
                }
            }

            public void SelectTransfer()
            {
                parent.Log(LogLevel.Debug, "SelectTransfer: direction {0}", direction);

                if(direction == Direction.MemoryToPeripheral && numberOfDataLatch >= 2)
                {
                    DoHalfTransfer();
                }
                else if((direction == Direction.MemoryToMemory) || (direction == Direction.MemoryToPeripheral))
                {
                    DoTransfer();
                }
                if(direction == Direction.PeripheralToMemory)
                {
                    DoPeripheralTransfer();
                }
            }

            public bool Enabled { get; private set; }

            public bool DMARequest { get; set; }

            private uint HandleConfigurationRead()
            {
                var returnValue = 0u;
                returnValue |= (uint)(channel << 25);
                returnValue |= (uint)(priority << 16);

                returnValue |= FromTransferType(memoryTransferType) << 13;
                returnValue |= FromTransferType(peripheralTransferType) << 11;
                returnValue |= memoryIncrementAddress ? (1u << 10) : 0u;
                returnValue |= peripheralIncrementAddress ? (1u << 9) : 0u;
                returnValue |= ((uint)direction) << 6;
                returnValue |= interruptOnComplete ? (1u << 4) : 0u;
                returnValue |= interruptOnHalf ? (1u << 3) : 0u;
                returnValue |= circular ? (1u << 8) : 0u;
                returnValue |= Enabled ? 1u : 0u;
                // EN and CIRC must read back, because the HAL does
                // read-modify-writes on this register constantly --
                // HAL_DMA_Start_IT alone does four (clear DBM, set TCIE/TEIE/
                // DMEIE, set HTIE, set EN) and HAL_DMA_IRQHandler does more.
                // Dropping a bit on read silently clears it on the write-back.
                // The inherited model returned neither, on the grounds that
                // "our transfer is always finished".
                parent.Log(LogLevel.Debug, "HandleConfigurationRead:[{0}] returning 0x{1:X}", streamNo, returnValue);
                return returnValue;
            }

            private void HandleConfigurationWrite(uint value)
            {
                parent.Log(LogLevel.Debug, "HandleConfigurationWrite:[{0}] value 0x{1:X}", streamNo, value);
                // we ignore channel selection and priority
                channel = (byte)((value >> 25) & 7);
                priority = (byte)((value >> 16) & 3);

                memoryTransferType = ToTransferType(value >> 13); // MSIZE
                peripheralTransferType = ToTransferType(value >> 11); // PSIZE
                memoryIncrementAddress = (value & (1 << 10)) != 0;
                peripheralIncrementAddress = (value & (1 << 9)) != 0;
                direction = (Direction)((value >> 6) & 3);
                interruptOnComplete = (value & (1 << 4)) != 0;
                interruptOnHalf = (value & (1 << 3)) != 0;

                // CIRC bit-8 support since used by ADC and Serial DMA
                //      When CIRC mode is active the number of data
                //      items to be transferred is automatically
                //      reloaded with the initial value programmed
                //
                //      When the PFCTRL==1 and stream EN==1 then CIRC is forced by H/W to 0
                //      It is auto forced to 1 if DBM bit is set as soon as stream is enabled: EN=1
                circular = (value & (1 << 8)) != 0;

                // TODO:CONSIDER: DBM bit-18 for double buffered mode
                // - e.g. eCosPro serial STM32 driver may use double-buffering and half-int support

                // 0E037ED5     0000 1110 0000 0011 0111 1110 1101 0101         <- is inverted
                //              1111 0001 1111 1100 1000 0001 0010 1010         <- the original mask
                // which includes
                //      bit1      DMEIE
                //      bit3      HTIE
                //      bit5      PFCTRL
                //      bit8      CIRC
                //      bit15     PINCOS
                //      bit18     DBM
                //      bit19     CT
                //      bits21-22 PBURST
                //      bits23-24 MBURST

                // we ignore transfer error interrupt enable as we never post errors
                //
                // 0xE037FDF, not Renode's 0xE037ED5. Two bits were added:
                //
                //   bit3 HTIE -- the half-transfer interrupt, which THIS model
                //     implements (see DoHalfTransfer and the HTIF layout note
                //     above). Warning that it is unsupported was simply wrong,
                //     and it was the single noisiest line in a run: measured
                //     2026-10-15, 9,897 of a boot's 10,066 log lines were this
                //     warning for DMA2 stream 7, the QUADSPI stream, at about
                //     500 a second of emulated time.
                //   bit1 DMEIE -- the direct-mode error interrupt, ignored for
                //     exactly the reason the line above gives for TEIE: this
                //     model never posts that error, so enabling its interrupt
                //     is a no-op rather than something unmodelled.
                //
                // Neither bit is anomalous: both are what the firmware's HAL
                // sets on EVERY arm. HAL_DMA_Start_IT at 0x0801B1E4 does
                // CR |= 0x16 (TCIE/TEIE/DMEIE) and then CR |= 8 (HTIE, when a
                // half callback is installed), which is why the warning fired
                // on every DMA transfer the instrument starts rather than on
                // some edge case. That is static analysis, independent of the
                // runtime values decoded above, and the two agree.
                //
                // bit 8 (CIRC) was already added when circular mode was
                // implemented, which is why the mask reads FD5 where the
                // comment block above still lists Renode's original ED5.
                if((value & ~0xE037FDF) != 0)
                {
                    parent.Log(LogLevel.Warning, "Channel {0}: unsupported bits written to configuration register. Value is 0x{1:X}.", streamNo, value);
                }

                if((value & 1) != 0)
                {
                    numberOfData = numberOfDataLatch; // as per RM0033 rev9 9.5.6

                    parent.Log(LogLevel.Debug, "HandleConfigurationWrite:[{0}] Enabled : direction {1} DMARequest {2} numberOfData {3} memoryIncrementAddress {4} peripheralIncremenetAddress {5}", streamNo, direction, DMARequest, numberOfData, memoryIncrementAddress, peripheralIncrementAddress);

                    if((direction != Direction.PeripheralToMemory) && DMARequest)
                    {
                        DoTransfer();
                    }
                    else
                    {
                        Enabled = true;
                    }
                }
                else
                {
                    parent.Log(LogLevel.Debug, "HandleConfigurationWrite: cstream {0} disable: Setting transferredSize to 0", streamNo);
                    transferredSize = 0;
                    // Clearing EN must stop the stream and read back as 0.
                    // HAL_DMA_Abort clears EN and then polls CR until EN reads
                    // 0, with a 5 ms timeout; the inherited model left Enabled
                    // untouched here, so every abort of a live stream timed
                    // out, HAL_SAI_DMAStop returned an error with the SAI
                    // handle still BUSY_TX, and the HAL_SAI_Transmit_DMA that
                    // follows it refused to re-arm.  Measured on the
                    // MicroFreak: the engine's restart of its own stream left
                    // the DMA running with its interrupts masked, the render
                    // callback stopped, and main()'s audio watchdog re-
                    // initialised the SAI about once a second thereafter.
                    Enabled = false;
                }
            }

            private TransferType ToTransferType(uint dataSize)
            {
                dataSize &= 3;
                switch(dataSize)
                {
                case 0:
                    return TransferType.Byte;
                case 1:
                    return TransferType.Word;
                case 2:
                    return TransferType.DoubleWord;
                default:
                    parent.Log(LogLevel.Warning, "Stream {0}: Non existitng possible value written as data size.", streamNo);
                    return TransferType.Byte;
                }
            }

            private static uint FromTransferType(TransferType transferType)
            {
                switch(transferType)
                {
                case TransferType.Byte:
                    return 0;
                case TransferType.Word:
                    return 1;
                case TransferType.DoubleWord:
                    return 2;
                }
                throw new InvalidOperationException("Should not reach here.");
            }

            private uint memory0Address;
            private uint memory1Address;
            private uint peripheralAddress;
            private uint fifoControl;
            private int numberOfData;
            private int numberOfDataLatch;
            private int transferredSize;
            private TransferType memoryTransferType;
            private TransferType peripheralTransferType;
            private bool memoryIncrementAddress;
            private bool peripheralIncrementAddress;
            private bool circular;
            private bool interruptOnHalf;
            private Direction direction;
            private bool interruptOnComplete;
            private byte channel;
            private byte priority;

            private readonly STM32F4DMA parent;
            private readonly int streamNo;

            private enum Registers
            {
                Configuration = 0x0, // DMA_SxCR
                NumberOfData = 0x4, // DMA_SxNDTR
                PeripheralAddress = 0x8, // DMA_SxPAR
                Memory0Address = 0xC, // DMA_SxM0AR
                Memory1Address = 0x10, // DMA_SxM1AR
                FIFOControl = 0x14, // DMA_SxFCR
            }

            private enum Direction : byte
            {
                PeripheralToMemory = 0,
                MemoryToPeripheral = 1,
                MemoryToMemory = 2
            }
        }
    }
}

