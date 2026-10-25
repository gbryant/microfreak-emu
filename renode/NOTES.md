# The Renode harness: notes

> Investigating something new? Read `../docs/METHOD.md` first. It records the
> tools that fail silently, the measurements that do not reproduce, and the
> retractions -- all learned the expensive way in this file.

The working notebook for the harness: what each peripheral model had to do,
which measurements reproduced, and which were retracted. `../docs/EMULATION.md`
is what the harness has to model; this is how it came to model it.

**A note on paths.** This is a log, kept in the order things were learned, and
the older entries were written before the harness was extracted into this
repository. Commands in them name the layout of the private workbench it came
out of -- `emu/mkflash.py`, `emu/renode/*.py`, `emu/qspiflash.py`. The current
equivalents are `mfemu flash`, `src/mfemu/*.py` and `renode/*.cs`; the
`mfemu` CLI is what to run today. The historical command lines are left as
they were written so each measurement still says what produced it.

Where it got to: boot completes with no stubs, notes reach the voice, and after
three DMA/SAI model fixes (see "The gate is main()'s heartbeat") note 60
renders a C4 at 260.9 Hz with a silent no-note control and a silent unfixed
control.

## Running it

**`mfrender.py` is the entry point.** It picks presets by name, plays a MIDI
file or a note spec, and writes WAVs:

    mfemu render --list --grep bass
    mfemu render --preset Buzzsaw --notes "60:1,67:1" --out buzz.wav
    mfemu render --preset 0 --preset 9 --preset Buzzsaw \
                           --midi phrase.mid --outdir renders/

A batch pays the boot once: presets are selected at run time with a MIDI
Program Change (see "Runtime preset selection"), so N presets cost
~40 s + N x (audio seconds x 5.4) rather than a boot each. `--dry-run` prints
the generated Renode script.

**All 512 slots are selectable** since 2026-09-10: `mfrender.py` sends Bank
Select MSB (`B0 00 <bank>`) before each `C0 <program>`, so `slot = bank*128 +
program`. CC#32 is ignored by this firmware -- only CC#0 works; see
FORMATS.md. This retired the old `MAX_PC_SLOT = 127` restriction
and the slot-borrowing it forced. Borrowing now happens only for a preset
imported from a **file**, which has no slot of its own.

**Batch position changes the render, and that is faithful.** The same preset
file at positions 2/3/4 of one boot measured RMS 823 / 521 / 639. It is not a
state leak: output is byte-identical given an identical prefix, and a
position-1 render is byte-identical across separate boots. The cause is
**free-running modulators** -- a cycling envelope in `Run`/`Loop` or a routed
LFO has a different phase at note-on depending on elapsed firmware time. With
those off, positions 2/3/4 agree to 0.05% of RMS. So: never A/B two presets
at different positions, and treat a free-running preset as a distribution --
render it at several positions rather than one.

`render.sh` is the older single-note path and still works:

    python3 emu/mkflash.py -o /tmp/mf/flash.img --presets-from captures/mcc-presets.jsonl --max-presets 512
    emu/renode/render.sh /tmp/mf/flash.img 60 /tmp/mf/note60.wav      # [boot lead length] optional

`render.resc` behind it carries **no hooks and no watchpoints**; the hooks
below are for experiment scripts only, because each one on hot code costs
~100 us of wall per hit. The long way, by hand:

    R=~/Downloads/Renode.app/Contents/MacOS
    S=/tmp/mf                       # anywhere for scratch output
    python3 emu/mkflash.py -o $S/flash.img --presets-from captures/mcc-presets.jsonl --max-presets 512

    cat > $S/run.resc <<EOF
    include @$PWD/emu/renode/boot.resc
    quadspi LoadImage @$S/flash.img
    emulation RunFor "14"                 # boot needs 9-14 s of firmware time
    sysbus WriteByte 0x200007E4 0x7F      # MIDI channel gate: omni, or nothing arrives
    usart1 WriteChar 0x90
    usart1 WriteChar 0x3C
    usart1 WriteChar 0x64
    emulation RunFor "1"
    sai2 ClearCapture
    emulation RunFor "2"
    sai2 SavePCM @$S/out.raw
    EOF

    cd $R && mono bin/Renode.exe --disable-xwt --console --plain $S/run.resc -e "quit"

Output is raw little-endian signed 16-bit stereo at 96 kHz.

**Expect 80-110 seconds of wall clock** for a 17 s render, depending on host
load: ~36 s to the first main-loop iteration, then ~5.4 s per second of
firmware. It was 25-35 minutes before the two findings under "Speed" below. Every `cpu AddHook` on a hot
address multiplies that -- see the hook notes below -- and a
`sysbus AddWatchpointHook` on the audio object's page makes a run effectively
unfinishable. Machine save/load does not yet restore the audio path (see
"Machine save/load"), so each experiment is a full boot.

`--console` is not optional: without it, Monitor output (`cpu PC`,
`sysbus ReadDoubleWord`, peripheral properties) goes to the telnet Monitor on
port 1234 and a scripted run looks silent.

### Diagnostics available

| command | what |
|---|---|
| `sai2 CapturedSamples` / `Ticks` | samples captured, SAI pacing-timer ticks |
| `sai2 SavePCM @file` / `ClearCapture` | write / reset the capture |
| `dma2 AcceptedStream4` / `RefusedStream4` | DMA requests taken vs dropped on the audio stream |
| `quadspi LoadImage @file` | back the external flash |
| `logLevel 0 quadspi` | log every flash command the firmware issues |
| `sysbus LogPeripheralAccess dma2 true` | register-level trace |

Values print in hex. Note `sysbus LogPeripheralAccess` reports **nothing** for a
`Memory.MappedMemory` region, so it cannot prove absence of access to a
memory-backed stub -- that mistake cost a debugging cycle here.

## Setup

Renode 1.16.1, the macOS x86_64 Mono build, unpacked to `~/Downloads/Renode.app`
rather than installed system-wide. It runs headless:

    R=~/Downloads/Renode.app/Contents/MacOS
    cd $R && mono bin/Renode.exe --disable-xwt --console --plain script.resc -e "quit"

`--console` matters: without it, Monitor command output (`cpu PC`, `sysbus
ReadDoubleWord`) goes to the telnet Monitor on port 1234 and never reaches
stdout, so a scripted run looks silent.

Files here: `microfreak.repl` (platform) and `boot.resc` (load + milestones).

## What worked immediately

Reached `main()` **with no stubs at all**, where the earlier emulator needed seven. Renode's
stock STM32F4 platform already provides the CPU, a real NVIC, RCC, the flash
controller, DMA1/DMA2, the USARTs, GPIO and the timers.

| | earlier emulator | Renode |
|---|---|---|
| stubs to reach `main()` | 7 | 0 |
| throughput | 0.93 M insn/s | ~94 M insn/s |
| 2 s of firmware time | ~25 min | 2.1 s wall |

The firmware ran on past `main()` through its whole peripheral init -- RCC PLL,
DMA1/DMA2 stream setup including a circular DMA2 stream (the SAI audio path),
GPIO, SPI2 and timers 2/3/5/8/14 -- matching the init order recorded in
`EMULATION.md`.

Only two things had to be told to it, and both were *declarations*, not
fabrications:

- the image loads at `0x0800C000`, not `0x08000000` (a bootloader occupies the
  first 48 KB on hardware), so `cpu VectorTableOffset` has to be set;
- the F446 runs at **180 MHz**.

## The clock, for contrast

On the earlier emulator, firmware time was a fabricated constant (`tick_insns`) that had
to be derived by hand and was wrong by 36x, which is what broke multi-message
MIDI.

Under Renode the firmware drives its own clock off SysTick. The counter at
`0x2001E1D8` initially advanced 2,000 per half second instead of 10,000,
because the stock platform declares `systickFrequency: 72000000` while the
firmware programs a SysTick reload of `0x464F` (18,000 cycles) expecting a
100 us tick -- 18,000 x 10,000 = 180 MHz. Declaring 180 MHz gave exactly 5,000
ticks per half second. **No constant was tuned; the CPU speed was stated and
the clock followed.**

**That rate does not last, which was missed here until 2026-10-16.** The
firmware reprograms SysTick as it starts up: by the time `main()` is running,
`SYST_RVR` reads `0x63F` -- a reload of 1,600, not 18,000 -- so the counter
advances at 180 MHz / 1600 = 112,500/s rather than 10,000/s. Measured 112,538
ticks per virtual second against `emulation GetTimeSourceInfo`, which is the
same number within noise. The 10,000/s above is correct for the phase it was
measured in and misleading as a general statement, and `midilink.boot()`
believed it: dividing by 10,000 made `seconds=14` wait 1.24 emulated seconds.
It reads virtual time from the harness now, which no firmware timer can skew.

## MIDI transport works, at gaps the earlier emulator could not reach

Feeding `usart1 WriteChar` and reading the firmware's receive ring directly
(head `0x2001E09C`, tail `0x2001E0A0`):

| | head | tail |
|---|---|---|
| before any MIDI | 0 | 0 |
| after note 60 | 3 | 0 |
| after note 72, **1.5 s later** | 6 | 0 |

Both messages were fully received, 1.5 s apart. On the earlier emulator a second message
more than ~250 ms after the first was dropped entirely.

## QUADSPI: modelled, and the flash init completes

`STM32_QUADSPI.cs` is a port of `emu/qspiflash.py` -- controller plus the NOR
flash behind it. The firmware now runs its whole documented flash init:

| command | meaning |
|---|---|
| `0x66`, `0x99` | reset enable + reset, in QPI mode |
| `0x66`, `0x99` | again, in SPI mode |
| `0x05` x6 | read status, as auto-polling and indirect |
| `0x06` | write enable |
| `0x01` | write status register (sets quad-enable) |
| `0x35` | read status 2 back to confirm |
| `0xAF` | read ID in QPI mode, 3 bytes |

That is exactly the sequence recorded from hardware in `EMULATION.md`, in order.

### Two things the port had to learn about Renode

**Access width.** The data register is byte-addressable and DMA moves it one
item at a time, so both reads and writes have to honour the access size. The
Python model already knew this for reads -- popping four bytes per 1-byte item
silently truncated every preset to noise -- and the same applies to writes: a
driver pushing a page program byte by byte must contribute one byte, not a
zero-padded word.

**Renode's DMA moves one item per rising edge of the request line.** Holding a
level high transferred exactly one byte of the three-byte JEDEC ID and stopped.
So the peripheral drives one edge per item, in a loop, from the register-write
path -- *not* from inside the data-register read, because the DMA services the
request synchronously and re-arming from the read would recurse once per item.
A 4096-byte preset read would be 4096 stack frames deep. Driving the edges from
a loop keeps it two deep however long the transfer is.

The ordering also matters: the driver enables the stream, issues the command,
and only then sets DMAEN, so a request pulse emitted when the command ran would
arrive before DMAEN and be lost.

`quadspi LoadImage @flash.img` loads a backing image built by `emu/mkflash.py`.

## I2C: a logging slave was enough, and no teardown was needed

Boot stalled at `0x0801BC6E` writing an address byte to `I2C_DR` and polling
SR1 for ADDR/AF with a timeout. A register hook gave `r0 = 0x40005400`
(**I2C1**) and `DR = 0x30`, i.e. **7-bit address 0x18, write**. Renode has the
controller but nothing was on the bus, so nothing acknowledged.

`MicroFreakCodec.cs` is a slave that acknowledges everything, remembers what is
written to each register, and answers reads from that. That was sufficient.

**The part number turned out not to matter**, which is worth recording because
the obvious next move was to go looking for a teardown. Over a full init the
firmware issues **96 writes and exactly one read** -- `read reg 0x67 -> 0x5A`,
reading back a value it had itself just written. It is a presence check, not an
identification, so any slave that stores writes satisfies it. The firmware never
asks the device what it is.

What it writes is still informative: an 8-bit config block (`0x00`-`0x06`,
`0x65`-`0x67`), then 16-bit pairs at `0x1B`-`0x33` with values like `7FFD`,
`8003`, `7FFB`, `283A` -- Q1.15 fixed point, i.e. biquad coefficients -- then a
block of single-byte writes around `0x2D`-`0x56` that look like gains. If a
read-back check ever appears that the stub cannot satisfy, that sequence is the
evidence to identify the part from.

## Boot completes, and two spaced MIDI notes both reach the voice

With QUADSPI and the codec in place the firmware finishes booting. It takes
**between 9 and 14 seconds of firmware time** -- measured: at 9 s the MIDI ring
was filled but undrained (head 3, tail 0), and after a 14 s boot each note
drains in its own window.

The spike's MIDI criterion is met. After a 14 s boot, with the channel gate at
`0x200007E4` set to `0x7F`:

| | ring head | ring tail | firmware time | voice call |
|---|---|---|---|---|
| after note 60 | 3 | **3** | 15,911 ms | `msg=0x643C90` |
| after note 72, 2 s later | 6 | **6** | 17,911 ms | `msg=0x644890` |

`0x643C90` is `90 3C 64` and `0x644890` is `90 48 64` -- note-on 60 and note-on
72, each dispatched to the synth voice in its own window, two seconds apart in
firmware time. The two log lines are 80 seconds apart in wall clock, so they are
genuinely separate events and not one flush.

**On the earlier emulator this was impossible**: any second message more than ~250 ms
after the first was dropped outright, and that limitation is what motivated the
migration.

## SAI: modelled and capturing, but blocked by Renode's DMA

`STM32_SAI.cs` models SAI block A's transmit path. Every 16-bit item the DMA
pushes into ADR is captured, and `sai2 SavePCM @out.raw` writes them out as
little-endian signed 16-bit -- the signal the hardware hands its DAC.

**The base address is SAI2 at `0x40015C00`, not SAI1.** Established by letting
the access fault rather than by reading a datasheet: with sysbus warnings on,
the only unmapped access in a full boot was `<0x40015C20, 0x40015C21>`, i.e.
block A's data register accessed 16 bits wide. That agrees with the earlier
work, where the audio interrupts were DMA2 streams 4 and 6 -- on the F446 those
are SAI2_A and SAI2_B.

The plumbing works: the firmware enables the block (`block A transmit started`),
the DMA delivers items into ADR, and 128 samples were captured and written to a
file. They are all zero, which is correct for a pre-note startup buffer.

### Renode's STM32DMA had no circular mode -- fixed, and there was prior art

Capture first stopped dead at 128 samples. Renode said why itself, in warnings
that are easy to skim past:

    dma2: Unhandled write to offset 0x70 ... Tags: CIRC (0x1)
    dma2: Unhandled write to offset 0xA0 ... Tags: CIRC (0x1)
    dma2: Unhandled write to offset 0xB8 ... Tags: HTIE (0x1)

Offset `0x70` is stream 4's CR and `0xA0` is stream 6's -- SAI2_A and SAI2_B,
the two audio streams. Renode's stock model implements neither circular mode
nor the half-transfer interrupt, and transfers NDTR items once and stops.

**This is a known gap with public prior art**, which was worth finding before
writing a model from scratch:

- [renode/renode#194](https://github.com/renode/renode/issues/194), "Implement
  STM32 DMA Peripheral Requests" -- describes exactly this: the stock model
  starts a transfer as soon as the config register is written, which is right
  for memory-to-memory but not for peripheral-driven transfers.
- [renode/renode#615](https://github.com/renode/renode/issues/615) carries a
  working replacement, `STM32DMA_Fixed.cs` in
  [RallySmith/renode-issue-stm32f2-spi](https://github.com/RallySmith/renode-issue-stm32f2-spi)
  branch `fixed`. It adds circular mode and `OnGPIO` peripheral requests. It is
  an unmerged third-party fix -- the author says a PR will follow "at a point
  when the maintainers are happy with the fixes".

`STM32F4DMA.cs` here is that file plus the half-transfer interrupt, which
neither the stock nor the fixed model implements, and half-buffer chunking for
circular memory-to-peripheral streams. HTIF sits one bit below TCIF for every
stream (4/10/20/26 against 5/11/21/27). The chunking matters because the
firmware fills one half while the DMA plays the other and learns which from HT
and TC: moving the whole buffer per request and raising only TC would make it
refill one half and repeat the other.

Adopting it also removed the pacing cost. The SAI no longer needs one request
edge per sample -- one request now delivers half a buffer -- so `OnTick` loops
on samples captured rather than on a fixed edge count and works with either DMA
model.

### The platform is now standalone

`microfreak.repl` no longer does `using "platforms/cpus/stm32f4.repl"`. A repl
cannot redeclare a peripheral with a different type, and `dma1: none` is not
valid syntax in 1.16.1, so the stock description is inlined and the DMA type
changed at source. The header lists what differs from the original.

## SAI fidelity: two real bugs fixed, audio still not flowing

The firmware enables and disables SAI block A repeatedly. That is not a new
failure -- it is `main()`'s audio watchdog, documented in EMULATION.md: if the
engine's sample counter at `0x2001C7B0` does not move for eleven consecutive
~10 ms checks, it resets the subsystem and re-runs the SAI2 init. The strike
counter at `0x200009E0` sits pinned at 11.

So the question is not "why does it tear down" but "why does the counter not
advance". Measured, it advances **twice a second** where it needs roughly
3,000.

Two bugs found on the way, both in models written here, both caught by
measurement rather than reasoning:

**1. The SAI transmit clock was gated on SAIEN && DMAEN.** Logging every ACR1
write over 12 s of boot showed the firmware setting *both* bits exactly once,
and SAIEN alone twice. So the clock was stopped almost always. Gating on SAIEN
alone is also the more faithful reading -- the block's clock runs when the block
is enabled, and DMAEN only controls whether it asserts the request line. A
request the stream refuses costs nothing.

**2. Half-transfer splitting only applied to circular streams -- and this
firmware does not use circular DMA.** The stream 4 CR writes are decisive:

    0x6022D40    CIRC set
    0x6022C40    CIRC *cleared*
    0x6022C59    enabled, CIRC still clear, NDTR = 0x80

The firmware clears CIRC and re-arms from the completion callback instead, which
matches the earlier harness seeing `HAL_SAI_Transmit_DMA` run 103 times in 90 s.
So the audio stream took the other path: one whole-buffer transfer, TC only,
then self-disable, after which every request was refused. Halving now applies to
any memory-to-peripheral stream, with the stream disarming at the end only when
CIRC is clear.

That doubled the rate -- renders per second went from 2 to 4, and the counter
with it -- which confirms HT and TC are now both landing. It is still three
orders of magnitude short.

### The audio loop, traced end to end

Tracing the firmware rather than poking the emulator found the bug. The chain is:

| address | what it does |
|---|---|
| `0x0802F04C` | `HAL_SAI_DMAStop(hsai)` then `HAL_SAI_Transmit_DMA(hsai, buf, size>>1)`; the SAI handle is at `obj + 0x88` |
| `0x0801F18C` | `HAL_SAI_Transmit_DMA`. **Gates on `hsai->State == 1` (READY)**, else returns HAL_BUSY without arming. Sets `State = 0x12` (BUSY_TX), installs the DMA callbacks, calls `HAL_DMA_Start_IT`, then sets SAI `DMAEN` (bit 17) and `SAIEN` (bit 16) |
| `0x0801B1E4` | `HAL_DMA_Start_IT`. Gates on `hdma->State == 1`, sets it to 2, writes NDTR and the addresses, then `CR \|= 0x16` (TCIE/TEIE/DMEIE), `CR \|= 8` (HTIE, if a half callback exists), `CR \|= 1` (EN) |
| `0x0802F008` | the DMA2 stream 4 vector: `HAL_DMA_IRQHandler(hsai->hdmatx)` |
| `0x0801B31C` | `HAL_DMA_IRQHandler`. On TC in **normal** mode: clears TCIF, clears TCIE, sets `hdma->State = READY`, calls the complete callback |
| `0x0801F304` | `SAI_DMATxCplt`. In normal mode clears SAI `DMAEN`, sets **`hsai->State = READY`**, then calls the firmware's TxCplt callback |

So the loop is self-sustaining by design: TC hands both handles back to READY and the
firmware re-arms. Note it also explains the ACR1 pattern that looked wrong earlier --
`DMAEN` legitimately toggles off after *every* completion, which is why gating the SAI
clock on `SAIEN` alone was right.

**A correction to an earlier note here:** the `State == 5` test in `HAL_DMA_IRQHandler`
is `HAL_DMA_STATE_ABORT`, not the completion path. The claim that "the HAL disables the
completion interrupt on TC and that is why only one ISR fires" was reading the abort
branch. It does clear TCIE on TC, but it also sets State back to READY and calls the
callback, so the loop should continue.

### The bug: an incomplete register read-back

`HandleConfigurationRead` in the inherited DMA model does not report **EN (bit 0)** or
**CIRC (bit 8)** -- the original carries the comment *"regarding enable bit - our
transfer is always finished"*.

The HAL does read-modify-writes on CR constantly: `HAL_DMA_Start_IT` alone does four
(clear DBM, set TCIE/TEIE/DMEIE, set HTIE, set EN) and the IRQ handler does more. Every
one of them read back a CR with EN clear and wrote it straight back, so the stream was
disarmed underneath the driver on the first RMW after arming. Reporting both bits fixed
it:

| | before | after |
|---|---|---|
| stream 4 ISRs in 13 s | 1 | **8,366** |
| `HAL_DMA_Start_IT` calls | -- | **3,306**, almost all with `hdma->State == READY` |
| engine sample counter | 2 | **25,086** |

This is with `KeepAudioStreamsArmed` **off**. The workaround is not needed and stays off.

### The audio path is now correct, and silent

A 2-second capture yields **384,000 samples = 192,000 frames = exactly 96,000 frames per
second**, which is the independently verified sample rate. The MIDI note arrives
(ring head/tail 3/3) and reaches the synth voice (`VOICE msg=0x643C90`, note-on 60
velocity 100).

**Every sample is zero.** The transport is right and the engine is not playing.

That is the same place the earlier emulator reached before presets loaded correctly --
"the digital audio tap works and the synth is simply not playing anything". So the next
question is a synthesis question, not a peripheral one.

## Why the engine renders silence: it is idling by design

Everything downstream of the engine is verified correct:

| checked | value |
|---|---|
| DMA2 stream 4 `M0AR` | `0x2001B6A2` -- the engine's audio buffer, matching the earlier work |
| DMA2 stream 4 `PAR` | `0x40015C20` -- SAI2 block A data register |
| DMA2 stream 4 `CR` | `0x06022D59` -- CIRC set, EN set (CIRC now survives the RMWs) |
| capture rate | 384,000 samples per 2 s = **exactly 96,000 frames/s** |
| preset load | 3,301 `0xEB` quad reads including `addr 0x081000` = slot 0 |
| MIDI note | ring head/tail 3/3, reaches the voice as `msg=0x643C90` |
| render callback | **24,508 calls**, `r0 = 0x20004830`, the audio object |
| synthesis entry `0x08036B38` | **75 calls** |

The render callback runs; it just stops synthesising. Decompiling `0x08038DF8`
shows a two-state machine:

    *(param_1 + 0x17F80)++;                       // the counter main()'s watchdog checks
    if (state == 0) {
        if (*(int *)(param_1 + 0x17F8C) < 1) {
            if (0x4A < counter++) { state = 1; goto zero_fill; }   // after 75 blocks
        } else counter = 0;
    }
    LAB_08038e5e:
        *(param_1 + 0x17F8C) = 0;                 // consumed
        FUN_08036b38(param_1 + 0x18, param_1 + 0x16E28);   // synthesis

**`param_1 + 0x17F8C` = `0x2001C7BC` is the gate.** While it reads >= 1 the engine
synthesises; while it reads < 1 for 75 consecutive blocks the engine drops to state 1
and only zero-fills. Exactly 75 synthesis calls were observed, so the engine ran its
75 startup blocks and then idled -- permanently, because nothing ever sets the flag.

It is consumed and reset to 0 on every synthesised block, so something has to set it
per block. That producer is the missing piece. Note also `param_1 + 0x17F80` is the
counter `main()`'s audio watchdog polls, which is why the watchdog is satisfied now
even though the output is silent: the render callback increments it whether or not it
synthesises.

Preset choice is not the cause -- `Buzzsaw` (slot 71, which rendered 114,992 non-zero
samples on the earlier emulator) is equally silent here.

## The gate is main()'s heartbeat, and two DMA-model bugs were starving it

The producer of `0x2001C7BC` is **`FUN_0803A750`**, called from exactly one
place: `0x08024F16`, inside `main()`'s loop body, once per iteration and
unconditionally (`ldr r3,[r2,#0x18C]; adds r3,#1; str r3,[r2,#0x18C]` are its
first instructions, with `r2 = audio_obj + 0x17E00`). Found by grepping the
objdump listing for stores to offset `0x18C` -- the address never appears as a
literal, so a data-reference search cannot find it. Ghidra's project has wrong
function boundaries around both `main()` and this function, so `MFDecompile`
returns garbage for them; the `arm-none-eabi-objdump -D -b binary -marm
-Mforce-thumb --adjust-vma=0x0800C000` listing is what was read.

So the flag is a **heartbeat**: "main() has iterated since the last block".
The full state machine in the render callback (`state` byte at `obj+0x17F84`
= `0x2001C7B4`, counter at `+0x17F88`, flag at `+0x17F8C`):

| state | flag on this block | effect |
|---|---|---|
| 0 (running) | >= 1 | counter = 0, synthesise |
| 0 | 0 | counter++; synthesise; after 75 in a row -> state 1, zero-fill |
| 1 (idle) | 0 | counter = 0, zero-fill |
| 1 | >= 1 | counter++; zero-fill; after **25 in a row** -> state 0, synthesise |

Both paths consume the flag. Idle is not permanent, which the earlier note
here got wrong: recovery needs 25 *consecutive* blocks that each saw at least
one main-loop iteration. Two other writers of a `+0x184` byte (`0x0804A5D4`
region, `0x0802B8BE`) are other objects, not this one.

### Why the engine idles at boot, on hardware too

Measured with hooks (firmware time from the 100 us counter at `0x2001E1D8`):

| firmware time | event |
|---|---|
| 0.419 s | SAI block A starts, first render callback |
| 0.444-0.456 s | 75 blocks with no heartbeat -> state 1 |
| 0.4-9.9 s | main() still in its init: tables of messages to the co-processors, each followed by `delay_ms(20)` (`0x0802D6B0`, `0x0802D744`, `0x0802D7D2`), plus 250/1000-iteration receive polls. This is firmware behaviour, not an emulation artifact -- the delays are the firmware's own millisecond clock |
| 9.95 s | first main-loop iteration (`TICK n=1 t=99490`) |
| 11.46 s | loop settles: **~630 instructions per iteration (3.5 us)** |
| 11.48 s | 25 heartbeat blocks later, state 1 -> 0, synthesis resumes (`SYN n=76 t=114802`) |

So the design tolerates a 25 ms gap and re-arms itself once main() is fast.
That much works under Renode only after fixing two model bugs, found by
reading the DMA request trace (`logLevel 0 dma2` over a 7 ms window at the
audio start) rather than by guessing:

### Bug 1: `DoHalfTransfer` moved half as many bytes as it should

`CreateRequest`'s size argument is in **bytes** -- the peripheral-to-memory
path passes `(int)memoryTransferType` per item -- but the half-transfer path
passed `half`, an item count. With 16-bit items each "half" moved 64 bytes =
32 samples, the first 32 of each 64-sample half. The SAI's tick loop then
compensated by issuing requests until it had a millisecond's worth:

| | per 1 kHz tick |
|---|---|
| stream 4 requests accepted | **6** (three were due) |
| render callbacks | 2, **44 us apart** (HT then TC in one ISR) |
| firmware render rate | 2,000 halves/s = 64 kHz, against a 96 kHz capture |

So the earlier "exactly 96,000 frames per second" was a property of the SAI
model's arithmetic, not evidence that the firmware was producing audio at
that rate, and every capture was stitched from the first half of each half.
Fix: `CreateRequest(half * (int)memoryTransferType, ...)`.

### Bug 2: the SAI tick bunched the interrupts, which defeats the heartbeat

Even with the size right, a 1 kHz tick delivers three half-buffers in one
burst: the DMA moves them synchronously inside the timer callback, the NVIC
latches one interrupt, and `HAL_DMA_IRQHandler` runs the HT and TC callbacks
back to back with **no main-loop time between them**. The second block of a
burst therefore always sees the flag at 0, which in state 1 resets the
25-block count -- the engine can never leave idle. That is the whole reason
the previous session saw exactly 75 synthesis calls in 24,508 callbacks.

Fix: tick at **3 kHz, one 64-item half-buffer per tick**, so HT and TC land
333 us apart as the hardware FIFO would space them. Verified on a short boot:
callbacks alternate half=1/half=0 exactly 60,000 instructions apart, 64
samples per request, 14 requests -> 896 samples.

### Bug 3: clearing EN did not disable the stream

With 1 and 2 fixed the engine resumed at 11.48 s and then **stalled about a
second later**: the SAI logged `transmit stopped`/`started`, the codec's
`0x67` read-back reappeared, and main()'s tick count froze for 700 ms -- the
audio watchdog's `delay(100); delay(500); delay(100)` recovery. `HAL_DMA_Abort`
(`0x0801B268`) clears TCIE/HTIE, clears EN, then polls CR until EN reads 0
with a 5 ms timeout. The model's EN=0 write reset `transferredSize` but never
cleared `Enabled`, so the read-back stayed 1, the abort timed out,
`HAL_SAI_DMAStop` returned an error with the SAI handle still BUSY_TX, and the
`HAL_SAI_Transmit_DMA` after it refused to re-arm. The stream kept running
with its interrupts masked, the callback stopped, and the watchdog cycled.
Fix: `Enabled = false` on an EN=0 write.

The trace that pinned it (fixes 1 and 2 only, hooks on the HAL entry points):

    SYN n=76            t=114802            synthesis resumes
    SYN n=77            t=114805
    DMASTOP_START       t=114806  lr=8031e53   the engine's rate reconfigure (FUN_08031df4) restarts its stream
    SAI_Transmit_DMA    t=114860  lr=8031e53   5.4 ms later = the abort timeout; no HAL_DMA_Start_IT follows
    WATCHDOG_STOP       t=116070  lr=802515d   121 ms of stalled counter -> main()'s watchdog
    AUDIO_REINIT        t=123100               after its 700 ms of delays
    ...                                        and round again

So the trigger is the firmware itself: the moment synthesis resumes, the
engine reconfigures its audio geometry and restarts the DMA through
`HAL_SAI_DMAStop`, and that restart is what the model could not survive. With
all three fixes the same restart logs `transmit stopped` and `started` 0.5 ms
apart and the watchdog never fires.

### Control matrix

Four full independent runs (no snapshot), identical scripts, 14 s boot then
note-on 60 at 14 s (or nothing), capture 15-17 s:

| arm | models | callbacks 14 s / 17 s | synthesis calls | state at 14 s | samples in 2 s | non-zero | pitch |
|---|---|---|---|---|---|---|---|
| base, note 60 | HEAD | 27,086 / 33,086 | **75** | 1 (idle) | 384,000 | 0 | -- |
| base, no note | HEAD | 27,086 / 33,086 | **75** | 1 (idle) | 384,000 | 0 | byte-identical to the row above |
| fixed, note 60 | all three fixes | 40,628 / 49,628 | 7,521 / 16,521 | 0 (running) | 432,000 | **431,924** | period 368 frames = **260.9 Hz**, C4, r = 0.96; L == R |
| fixed, no note | all three fixes | 40,628 / 49,628 | 7,521 / 16,521 | 0 (running) | 432,000 | 0 | -- |

The base pair reproduces the previous session exactly (75 synthesis calls and
silence, note or no note, the two captures one SHA1). The fixed pair differs
*only* in the note: the engine synthesises 3,000 blocks a second either way,
and the note-on turns silence into a C4 with the same period the earlier
harness measured for note 60 (367 frames there, 368 here). Rendered from slot
0, `NervousKeys`; the pitch wanders 365-370 frames across 0.25 s windows,
which is the preset, not the transport, and the no-note arm shows the
transport adds nothing of its own. L == R throughout: a mono engine feeding a
stereo SAI, as before.

**The 9:8 excess, resolved.** The fixed arms captured 432,000 samples in 2 s
where 3,000 ticks/s x 64 items = 384,000 were due, while the render-callback
count was exactly 3,000/s. A run printing the counters over the window gave
6,000 ticks, 6,000 requests, 6,000 callbacks and 432,000 samples: **72 items
per request**, and DMA2 stream 4's NDTR read `0x90` = 144 at 14 s, not the
128 it was armed with at boot. When synthesis resumes the engine re-arms its
stream through `FUN_08031df4`, and the geometry it picks is 36 stereo frames
per half (288-byte buffer), the branch EMULATION.md recorded as the
`rate == 32000` case. And the audio object agrees: read at 11.3 s and 14 s
(`0x2001AFDC + 0x22C..0x230`):

| | 11.3 s (idle, boot geometry) | 14 s (running) |
|---|---|---|
| rate bytes `+0x22C`, `+0x22D` | 96, 96 | **48, 32** |
| current rate `+0x230` | 96,000 | **32,000** |
| DMA2 stream 4 NDTR | 128 | **144** |
| SAI2 ACR1 | `0x00232082` | `0x00232082` -- unchanged |

So the engine's own "rate" flips to 32,000 when it resumes, and the DMA
geometry follows -- but the SAI's clock configuration does not move, and the
rendered note still reads as C4 at a 96 kHz interpretation (a C4 rendered at
32 kHz would show a 122-frame period, not 368). **What that field means is
unresolved**; it is not the SAI sample rate. It is worth settling before any
sample-exact comparison, because it decides what a "frame" of capture is.

Either way, a tick that always asks for "one half" at 3 kHz is wrong once
the half is 72 items, so the SAI now paces by **items delivered**: the timer
counts at 192,000 items/s and its limit is whatever the last request moved,
so the next request falls due exactly when that chunk would have drained.
Verified with the final pair (item-paced model, full boots, same script):

| arm | 2 s window: ticks / requests / callbacks / synthesis | samples | non-zero | pitch |
|---|---|---|---|---|
| note 60 | 5,333 / 5,333 / 5,333 / 5,333 | 383,976 = 5,333 x 72 | 383,892 | period 368 = **260.87 Hz**, r = 0.97, L == R |
| no note | 5,333 / 5,333 / 5,333 / 5,333 | 383,976 | 0 | -- |

Synthesis on every block, 2,667 blocks/s of 36 frames = 96,000 frames/s, and
the sample count is now exactly the item rate. Nothing repeats in the capture
(no 64-item chunk equals its predecessor); the 9:8 was a cadence error, not
stale audio. Renders: `out_final_60.raw` / `out_final_none.raw` in the
session scratch directory, raw LE s16 stereo.

Producer confirmed at runtime as well: `PRODUCER pc=803a770 wrote flag=1 at
t=99490` on the first main-loop iteration, in every arm.

### Renode hook API, as it actually behaves in 1.16.1

- `cpu AddHook <addr> "<python>"`: `self` is the CPU. `self.PC.RawValue`
  (`int(self.PC)` throws), `self.GetRegister(n).RawValue`, `self.LR.RawValue`,
  `self.Bus.ReadDoubleWord(addr)`, `self.ExecutedInstructions` (180 per us at
  the declared 180 MIPS), `self.Log(LogLevel.Error, ...)`.
- `sysbus AddWatchpointHook <addr> <1|2|4> Write "<python>"`: `self` is the
  bus, `value` is the datum, PC via `self.GetCurrentCPU().PC.RawValue`.
- **Every .NET unsigned value must be wrapped in `int()` before `%d`/`%x`**, or
  IronPython throws "int argument required" and the exception is fatal.
- Hooks share no scope; pass counters through spare SRAM (the platform
  declares 256 KB, the F446 has 128 KB, so `0x20030000+` is free).
- **A watchpoint on a hot page slows the run 50-100x**, not the hook itself:
  runs with a watchpoint on the audio object reached 0.5 s of firmware time in
  15 minutes of wall clock. Use a `cpu AddHook` at the store's PC instead.
- Wall clock without watchpoints: ~10 min per 14 s of firmware, and it
  degrades quickly with several Renode processes on the same machine.

### Machine save/load: Save works, Load is silently broken

`Save @file` writes a 22 MB snapshot at 14 s. `Load` in a **fresh** process is
refused outright ("incompatible or corrupted") -- the peripherals compiled
from the `.cs` includes get a new assembly identity each launch. `Load` in the
**same** process resumes the CPU (the firmware clock advances) but the SAI
path is dead afterwards: `block A transmit stopped`, the watchdog re-inits the
audio about once a second, and the capture is empty. Nothing warns. Do not use
it until the SAI's `LimitTimer` and the DMA request line survive a Load;
until then every result must come from a full boot, as the earlier notes
already said.

## Speed: 24x slower than real time was three PWM timers, not the audio

The render took 25-35 minutes. Measured in 2 s boots, which are enough because
the audio starts at 0.46 s:

| arm | wall for 2.0 s of firmware |
|---|---|
| unchanged | 46 s |
| SAI request line disconnected (no DMA, no audio interrupts) | 45 s |
| SAI tick that does nothing at all | 45 s |
| SAI tick at 1 kHz instead of 3 kHz (HEAD models) | 44 s |
| global quantum 25 us / 333 us / 1 ms | 47 / 45 / 47 s |
| `AdvanceImmediately true` | 45 s |
| log level Error everywhere | 45 s |

So nothing in the audio path, the quantum or logging mattered. A per-100 ms
profile put the cliff at **0.4-0.5 s**: 0.3 s of wall per 100 ms before, 2.4 s
after, identical with the SAI timer never running. At 0.5 s even a parked
branch-to-self, which Renode idles through with zero instructions, cost 6.1 s
of wall per 0.3 s of virtual time, against 0.7 s at 0.3 s -- so the cost was
in *advancing virtual time*, i.e. time-source events. Interrupt counts (10 kHz
SysTick, 3 kHz DMA, nothing else) and NVIC pending state ruled out a storm.
Dumping every timer at 0.3 s and 0.5 s named it:

| timer | started | PSC / ARR | events/s at the platform's 10 MHz | interrupt |
|---|---|---|---|---|
| TIM9 | between 0.3 and 0.5 s | 0 / 0x55 | **116,000** | none |
| TIM3 | at boot | 4 / 0xFE | 7,800 | none |
| TIM14 | at boot | 0 / 0x1FFE | 1,200 | none |
| SysTick | at boot | -- | 10,000 | the firmware's clock |

All three are PWM generators (CCER set, DIER clear) on pins nothing here
models. Renode on this Mono/macOS build charges **~100-200 us of wall per
time-source event**, and the event totals reproduce the slowdowns: ~19k/s
before TIM9 (3x) and ~138k/s after (24x). The Mono sampling profile agreed in
its way -- 84% of samples in wait primitives, tlib barely visible.

**Change:** `microfreak.repl` clocks TIM3, TIM9 and TIM14 at 1 kHz. Verified
on a full 17 s render with read-watchpoints on each one's SR, CNT, DIER, CCR1
and ARR: **zero reads** in 17 s, engine state 0, and the note 60 capture
**byte-identical** to the 10 MHz render (SHA1 `82fb2e505745`, 383,976
samples, 260.87 Hz). Wall for that run: 158 s against ~30 min. A clean
`render.sh` run then took **156 s** for 17 s of firmware -- ~84 s of boot, then
22 s per second of live synthesis -- i.e. ~2x real time while idle and ~22x
while rendering. The next section takes the 22x apart; it was not the
synthesis.

### The remaining 22x was the USB stack polling a peripheral that does not exist

With the timers fixed, the boot idles at ~2x real time but the main-loop
phase (everything after 9.95 s, so all rendering) ran at ~22x. Eliminated
with controls, each a full run with the audio SHA1 checked: floating-point
helpers (a dozen samples of thousands in the native profile), SysTick
exceptions (stopped: no change, engine still rendered 2,667 blocks/s), the
MPU (off), the main loop's 175,000 GPIO port C reads a second (the whole GPIO
page served as direct memory: no change, byte-identical), Debug/Noisy logging
in our models (stripped: no change), the sync quantum, and QUADSPI traffic
(none in the phase; the 9,906 stream 7 warnings per render are the preset
load at the first loop iteration).

Two things did move it. Removing `ApplySVD` from the platform (keeping the
two `Tag` overrides) halved the phase, 62 s -> 28 s per 2 s of firmware,
byte-identical audio. And a sysbus-level trace, which had been invisible
because `boot.resc` holds `sysbus` at log level Error, showed what the SVD
was being consulted for: **~930,000 accesses a second to the USB OTG_FS core
at 0x50000000**, which the platform does not model at all.

    552  [cpu: 0x80206E8] (tag: 'USB:RESET') ReadDoubleWord from non existing peripheral at 0x50000010
    552  [cpu: 0x80206DE] WriteDoubleWord ... unimplemented register OTG_FS_GLOBAL:FS_GRSTCTL (0x50000010)
    426  [cpu: 0x8047E6E] ReadDoubleWord ... OTG_FS_DEVICE:DTXFSTS1 (0x50000938), returning a value from SVD: 0x0
    426  [cpu: 0x8047E66] ReadDoubleWord ... OTG_FS_DEVICE:DIEPINT1 (0x50000928), returning a value from SVD: 0x80
    (3 ms of the main-loop phase)

The code (`0x08047E60`, called from main's loop, and its twin `0x0804C79C`)
is the USB-MIDI transmit attempt: read DIEPINT1, return unless TXFE (bit 7)
is set; read DTXFSTS1, and if the FIFO has 127 words or fewer of space, count
a retry; after five retries read GRSTCTL and, unless a flush is already in
progress (bit 5), flush TX FIFO 1 through `0x08020542`, which writes GRSTCTL
and polls it. The SVD reset values hand the firmware an impossible core -- FIFO
empty *and* no space -- so it flushes forever, and each access is a
non-existing-peripheral warning: an SVD lookup and an eager `string.Format`,
about 20 us each, roughly 20 s of wall per firmware second. That is the
managed hot path and the constant re-entry of tlib's loop the native samples
showed.

**Adopted.** A page-aligned `Memory.MappedMemory` over 0x50000000-0x5003FFFF
(`usbfs` in `microfreak.repl`), pre-filled by `otg_fs_reset.resc` with the
SVD's non-zero OTG_FS reset values and then the 0x80000000 that the platform's
former `USB:RESET` tag returned for 0x50000010-0x5000003F, in that order, so
every read returns exactly what it returned before -- but through tlib instead
of a managed lookup. `boot.resc` includes the script right after the platform
loads. Verified: **zero** non-existing-peripheral or unimplemented-register
accesses in a 5 ms window of the synthesis phase, engine state 0, 383,976
samples captured.

| | before | after |
|---|---|---|
| 17 s render, total | 156 s | **81 s** |
| boot to the first main-loop iteration (9.9 s) | ~62 s | ~36 s (~3.6x real time) |
| main loop, per firmware second | ~22 s | **~5.4 s** |
| audio | 383,892 non-zero, SHA1 `82fb2e505745` | 383,906 non-zero, SHA1 `38cb172ddb3a` |

Wall times move with host load: the same render measured 66 s idle and 111 s
under load, so treat these as a band, not a constant.

The audio is the same sound but **not byte-identical**: per-window RMS and
peak agree within 0.2%, and the difference is a phase shift of the preset's
vibrato (best alignment r = 0.59 at a 62-frame lag), because the note lands at
a different point in the engine's control cycle -- the main loop no longer
spends most iterations in a flush, so whatever it advances per iteration
advances faster. Neither render is hardware truth: on the real board the USB
core answers and the flush storm never happens, so this is closer, not
further.

**The one semantic change** is that writes to the range now stick where they
were dropped. `GRSTCTL` reads back `0x60` after a render: the firmware set
TXFFLSH (bit 5) once, and because the transmit path skips the flush when that
bit is already set, it never storms again. Before, the write vanished and the
tag returned bit 5 clear, so it flushed on every pass. Either way USB never
transmits, and neither matches a real core, which would clear the bit when the
flush completed. If USB-MIDI is ever wanted (SysEx preset transfer over USB is
the plausible reason), this region has to become a real OTG_FS model rather
than being extended.

Cost that remains at 81 s: the 9.9 s of pre-loop boot at ~3.6x
(SysTick's 10,000 events/s at Renode's per-event price, plus the firmware's
own co-processor init delays) and the main loop at ~5.4x (the port C polls and
DMA register reads that still cross into C#, plus SysTick).

Three things that did *not* find it, recorded so nobody repeats them: the
`wfe` opcodes in the image are literal-pool data; `sysbus LogPeripheralAccess`
printed nothing at any log level in this build; Mono's `--jitmap` wrote no map
on macOS, so JIT frames in `sample` output stay unsymbolicated.

## Note sequences, note-off and paraphony all work

Three arms, full independent boots, identical but for the note-off. Each plays
note 60 at 14 s and note 67 one second later, capturing four seconds from the
first note-on. Hooks on the note dispatch (`0x080478A4`) and the voice
(`0x08048524`) record what reaches the engine, which separates "MIDI did not
parse it" from "the engine ignored it"; both are cheap, a handful of calls per
run.

| arm | note-off sent |
|---|---|
| `off80` | `80 nn 40`, the status-byte form |
| `off90` | `90 nn 00`, note-on with velocity zero |
| `noff` | none -- the control |

Every message reached the voice, exactly one second apart in firmware time:

    on 60   t=139622   VOICE r1=643c90
    off 60  t=149624   VOICE r1=403c80      (off80)
    off 60  t=149624   VOICE r1=3c90        (off90)
    on 67   t=159623   VOICE r1=644390
    off 67  t=169624   VOICE r1=404380      (off80)

**Note number tracks pitch, exactly.** This is the earlier finding "a MIDI
note changes the audio but the note *number* does not" -- three note numbers,
one SHA1 -- now closed. It was an artifact of rendering against an erased
preset region:

| played | measured | nominal | error |
|---|---|---|---|
| note 60 | 261.6 Hz | C4 261.63 | **0 cents** |
| note 67 | 391.8 Hz | G4 392.00 | **-1 cent** |

**Note-off works, in both encodings, and they are byte-identical.** `off80` and
`off90` differ in *zero* samples across all sixteen windows, which is what the
MIDI spec requires and what `EMULATION.md` suspected from the parser handling
`0x80` and `0x90` alike. Level after the note-off, RMS per 250 ms:

| t (s) | 1.00 | 1.25 | 1.50 | 1.75 |
|---|---|---|---|---|
| with note-off | 441 | 225 | 114 | 59 |
| held (control) | 565 | 524 | 495 | 479 |

A clean release, roughly halving every 250 ms, against a control that sustains.
The two arms are **identical up to the window the note-off lands in** and
differ in every window after, which is the isolation this measurement needed:
nothing else about the runs changed.

**Two notes sound together.** In the held arm the second note-on does not steal
the first. Autocorrelation on that stretch reads 130.4 Hz, which is *not* a
component -- the spectrum at 2.4-2.9 s has **261.6 Hz at 47% and 392.0 Hz at
100% of peak, and nothing at 130.8 Hz**. It is the common subharmonic of two
simultaneous pitches, so the period is the greatest common divisor of theirs.
The same window in the note-off arm has 392.0 Hz alone and *zero* at 261.6 Hz.
So the engine is paraphonic here and the release genuinely frees the voice.

A pitch reading from autocorrelation alone would have called that stretch a
C3. Check the spectrum before believing a period.

Caveat worth keeping: all of this is one preset (slot 0, `NervousKeys`). A
preset whose envelope has infinite sustain or whose oscillator drones would
answer the note-off question differently, and that would be the preset, not
the transport.

### Both behaviours are the preset's, not the emulator's

Two held notes sounding is not an extra capability -- the MicroFreak is
paraphonic by design -- and the preset says so itself. Decoding slot 0's
parameter stream: `Gen/Parafon` = **32767 (On)**. Across the 399 captured
presets, 220 have it On and 178 Off.

Swapping slot 0 for `Buzzsaw` (slot 71, `Gen/Parafon` = 0) with
`mkflash.py --preset 0=FILE`, which overrides `--presets-from`, and playing
the same sequence:

| | slot 0 = NervousKeys (On) | slot 0 = Buzzsaw (Off) |
|---|---|---|
| both notes held, first note's fundamental | **47% of peak** | **0.0%** |
| second note's fundamental | 100% | present |
| RMS across the run | 500-1400 | ~6100 |
| note-off changes the audio | yes | **not one sample** |

So with paraphony off the second note *steals* the voice, and with it on both
sound. Confirmed by the same single-variable control the earlier work used,
run in reverse: `Buzzsaw` re-encoded with **only** `Parafon` flipped to On --
**two bytes** changed in the whole 4088-byte payload -- brings the first note
back from 0.0% to **76.8%** of peak, everything else about the preset equal.

`Buzzsaw` ignoring note-off is also its own parameter, not a gap: its
`EG2/Mode` (the `amp_mod` switch) is 0, so the amplitude is not envelope-gated
and the patch drones -- level is flat at RMS ~6100 from before the first note
to after the last, and only the pitch moves. `NervousKeys` has `EG2/Mode` =
32767 and releases. Two presets, two behaviours, both correct.

### This resolves gap 2: paraphonic presets are no longer silent

`EMULATION.md` gap 2 recorded that paraphonic presets render silent --
"every silent preset tried had it at 32767, every sounding one at 0",
confirmed earlier by flipping `Trance`'s switch to 0 and going from 0 to
114,988 non-zero samples. That was **217 of 391 presets, the majority of the
library**.

It does not reproduce here. `NervousKeys` renders with `Parafon` = 32767, and
the reverse single-variable control above -- a preset that already sounded,
with two bytes changed to turn paraphony *on* -- still renders: 767,672 of
768,000 samples non-zero, peak 8743. Whatever the allocation path was waiting
on the earlier emulator, it is satisfied here.

### The preset really is driving the engine, and it agrees with the earlier measurement

The swap is also the control that had been missing for "are presets loaded, or
is this a firmware default?". `Buzzsaw` in slot 0 sounds nothing like
`NervousKeys` -- RMS ~6100 against ~500, peak 8781 against 3054, a bright saw
whose strongest components are at 754, 1538 and 2110 Hz.

And it matches the earlier measurement of the same preset, from
`EMULATION.md`'s table, which was taken with a different harness, a different
DMA model and a different capture tap:

| | earlier emulator | Renode |
|---|---|---|
| `Buzzsaw` at note 60 | 127.66 Hz | **127.5 Hz** (~2 cents) |
| RMS | 5747 | 5975 |

Two independent rehostings agreeing on a preset's pitch to a couple of cents
is the first cross-harness corroboration in this project. It is not the
byte-exact comparison -- that is still not done -- but it is evidence that
neither harness is inventing the sound.

## Runtime preset selection, MIDI files, and the render tool

Four probes, each a control against its own arm, decided the shape of the
tooling before any of it was written.

**Program Change selects a preset.** `C0 47` makes the firmware read page
(71+129)*4096 out of the emulated flash -- caught on a hook at `FUN_080454D4`,
which takes the slot in r1 -- and the audio becomes that preset:

| | with `C0 47` | without |
|---|---|---|
| preset page read | `slot=71` at t=13.96 s | none |
| RMS / pitch of the same note | 6100 / 127.5 Hz (Buzzsaw) | 500 / 261.6 Hz (NervousKeys) |
| samples differing | 100% of every window | -- |

It matches, sound for sound, rebuilding the flash image with that preset in
slot 0. On the earlier emulator this produced no flash traffic at all, but it could
not deliver a second MIDI message, so that result never carried over.

**Several in one boot work**, which is what makes batching worth anything:
selecting 71, 9, 1 and 0 in a single run read each page in turn and rendered
four different sounds. Three of them line up with `EMULATION.md`'s earlier
measurements of the same presets, taken with a different harness, DMA model
and capture tap:

| slot | preset | Renode | earlier |
|---|---|---|---|
| 71 | Buzzsaw | 127.5 Hz, RMS 5828 | 127.66 Hz, RMS 5747 |
| 1 | Punisher | 262.3 Hz | 261.58 Hz |
| 0 | NervousKeys | 261.6 Hz | -- |
| 9 | Raverz | **no pitch found** | 130.79 Hz, r = 1.00 |

`Raverz` is the open one: its spectrum here is dominated by 4-200 Hz content
with no periodicity in the audio band, where the earlier emulator measured a clean 130.79
Hz. It is a Speech-engine preset. Whether that is a real engine difference, an
onset-alignment artifact (the earlier numbers used an onset-aligned window), or
something unmodelled is unresolved, and it is the first thing to look at when
the per-engine survey starts.

**Pitch bend works.** `E0 00 7F` reaches the voice handler (`r1=7f00e0`) and
moves the note from 260.9 Hz to 516.1 Hz -- an octave, which is this preset's
own `Kbd/BendRng`. Centring it returns the pitch exactly.

**Control change is parsed, and changes nothing audible here.** `B0 4A 7F`
reaches the dispatcher and not the voice, which is right -- it is not a voice
message. A clean sweep of CC 1, 5, 71, 74 and 102 at value 127, half a second
apart, against a control that sends none, is **byte-identical across the whole
2.2 s**, not one sample different.

For cutoff and resonance (74, 71) that is the *expected* answer rather than a
gap: the MicroFreak's filter is analog and sits after the DAC, so it is not in
the firmware at all and cannot appear in a digital capture. For the mod wheel
(1) and glide (5) the likely reason is that this preset's mod matrix does not
route them; the test that would settle it is a preset that does. Nothing in
the tooling depends on the answer -- CC bytes are passed through either way --
but it means **CC cannot currently be used as a search dimension** without
first finding a preset where one demonstrably acts.

**`.mfpz` reads as the native format.** MCC's single-preset export unzips to a
Boost text archive whose one 4672-byte array is the wire form of the parameter
stream. Decoded, it is **byte-identical** to what the capture holds for the
same preset, which is the check `presetlib.py` was written against.

### The tools

| file | what |
|---|---|
| `emu/renode/mfrender.py` | the CLI: presets in, WAVs out, one boot per batch |
| `emu/renode/presetlib.py` | preset by name, slot, `.mfpz` or raw dump; `--list` |
| `emu/renode/midifile.py` | dependency-free SMF reader -> `[(seconds, bytes)]` |

`midifile.py` expands running status, follows the tempo map, and rejects SMPTE
division rather than guessing; verified on a hand-built file with a mid-stream
tempo change. Timing end to end is exact: a 1.25 s file with a 0.75 s tail
gives 2.000 s of audio.

Imported presets borrow a slot that the batch is not otherwise using and
overwrite only its parameter regions, keeping that slot's region 0 -- the
firmware skips a slot whose region 0 is erased, and nothing in the audio path
reads the name. So an imported preset renders correctly under a wrong
displayed name, which is why `--list` shows the *base image's* names.

## Presets with header schema 0x33 are rejected by this firmware

The header byte at region 0 +8 (name-record byte 20) is a payload schema
version. The firmware dispatches on it, and the range check is the whole story:

    8055144:  ldrb.w r2, [ip, #8]      the schema byte
    8055148:  sub.w  r3, r2, #17       table 1 covers 0x11..0x32
    805514c:  cmp    r3, #33
    805514e:  bhi.w  0x805533e
    805533e:  subs   r2, #16           table 2 covers 0x10..0x32
    8055340:  cmp    r2, #34
    8055342:  bhi.n  0x805538e         default
    805538e:  movs   r3, #0            zero the parameters, return

`0x33` is off the end of **both** tables by exactly one, lands in the default
case, and the parameters come out zeroed -- silence. Found by tracing
execution (`cpu CreateExecutionTracing "t" @file PC`) through two loads that
differed only in that byte and diffing: they run identically for 37,849
instructions and split at `0x805514E`.

Measured, and the byte changes *interpretation*, not just acceptance -- the
same parameter bytes give three different sounds:

| schema | result |
|---|---|
| 0x10, 0x11, 0x30, 0x31, 0x32 | renders |
| 0x33 | **silent**, 3 of 3 tried |

61 of the 398 captured presets carry `0x33`, and all of them sit above slot
127 where plain Program Change cannot reach, which is why this stayed hidden.

**Unresolved, and it matters.** The user's device runs the *same* 5.0.0.2084
firmware, writes `0x33`, and plays those presets. So either the byte stored in
the flash page is not the byte the SysEx name record reports -- the mapping
"region 0 = name-record bytes [12:44]" may be wrong for this byte -- or the
device plays from its edit buffer and would fail the same way on a genuine
reload. The cheap hardware test for the second: select the preset, switch
away, switch back.

Two corrections fell out of chasing this. Nothing ever reads region 0 at
`0x200018B8+8`, so the quick-reference claim that the storage object *is* the
page buffer is wrong or incomplete. And `mfrender.py`'s slot borrowing was
writing presets under a donor's header, i.e. under the wrong schema, so any
imported render from before this is a wrong sound rather than a mislabelled
one -- discard them; native-slot renders are unaffected.

## SysEx over DIN is received and dispatched -- the reply is what is missing

`emu/renode/midilink.py` connects USART1 to a TCP socket and the Monitor to a
second one, so the machine free-runs while Python talks to it. It exposes
`send()`/`recv()` with the same shape as the `Synth` class in
`read_preset.py`, so the hardware-verified protocol tools drive emulated
firmware unchanged. That is the testbed the custom-firmware goal needs.

With it, a name request traced through the firmware by hooked counters:

| stage | calls added |
|---|---|
| USART1 IRQ `0x08058214` | 13 -- exactly the message length |
| MIDI byte parser `0x080480A8` | 13 |
| SysEx dispatcher `0x0804ACC8` | **1** |

So **`EMULATION.md`'s claim that preset SysEx only arrives over USB is wrong**:
DIN delivers it to the dispatcher. No reply comes back, and the firmware
demonstrably can transmit on DIN (its own control-change output arrives at the
socket), so the reply most likely goes to USB -- which this platform replaces
with plain memory. Hence the USB work.

Dead ends, recorded so they are not repeated: the table at `0x0804AD20` is not
function pointers (the values are not code addresses), and the SysEx header
appears nowhere in the image as a literal, so replies are assembled byte by
byte and cannot be found by searching for the manufacturer ID.

## USB works: the firmware enumerates and answers SysEx

`read_preset.py`, the tool written against real hardware, now drives emulated
firmware with only its transport swapped:

    name: 'NervousKeys'
    146 chunks, 4672 bytes in 1 s
    payload identical to the capture for slot 0: True

Byte-identical to what the device returned when `captures/mcc-presets.jsonl`
was recorded. That is the testbed the custom-firmware goal needs.

### Why USB at all

The firmware answers SysEx over USB. A request sent to the DIN port is
received, parsed and dispatched correctly -- measured with hooked counters:
13 bytes in, 13 through the MIDI parser, one dispatcher call -- and the reply
*is* built, but it never comes back on DIN. **`EMULATION.md`'s claim that
preset SysEx arrives only over USB is wrong for the receive direction**; it is
the reply that needs USB.

### The model

`STM32_OTGFS.cs`: OTG_FS in device mode, plus a small host state machine.
Renode ships a device-side USB framework, but built the other way round --
virtual devices attaching to an emulated host -- and has no Synopsys/STM32 OTG
model, so this is written from what the firmware does. The `Trace` property
records every register access and is how each fault below was found. Develop
it that way: run, read the trace, implement the next thing the firmware waits
for. Seven faults, each located in one run:

| symptom | cause |
|---|---|
| USB init never started | `GRSTCTL.CSRST` did not self-clear, so `USB_CoreReset` span forever |
| ISR storm, 2,900 `GINTSTS` reads in 3 s | `DAINT` and `IEPINT`/`OEPINT` stored rather than derived; the firmware writes all-ones to `DAINT`, which is read-only |
| device answered nothing | `DOEPINT.STUP` raised when the SETUP was queued, not when its completion status was popped, so the setup handler ran before the bytes arrived |
| config descriptor truncated at 56 of 119 bytes | IN transfers completed the instant `EPENA` was written; completion must follow the programmed transfer size |
| endpoint armed but no data written | `TXFE` is gated by `DIEPEMPMSK`, not `DIEPMSK` |
| last two requests silently lost | the host queued SETUPs behind unread entries and advanced on its own flag |
| entries nothing would read | the status stage was pushed unconditionally |

The host is driven by device state -- the SETUP packet count in `DOEPTSIZ0`,
and the programmed transfer size being written -- never by timers, and stalls
with a diagnosis rather than timing out into false progress. That last point
matters: an earlier version had a timeout fallback and reported success it had
not earned, twice.

Not modelled, deliberately: OUT data endpoints beyond control, isochronous
transfers, suspend/resume, and any descriptor validation. The host consumes
what the device sends and discards it.

### Using it

    from midilink import EmulatedSynth
    with EmulatedSynth(flash="flash.img") as synth:
        ...wait for the firmware clock at 0x2001E1D8 to pass ~15 s...
        synth.monitor("sysbus WriteByte 0x200007E4 0x7F")   # MIDI channel gate
        synth.attach_usb()                                  # enumerate
        name, payload = read_preset.read_preset(synth, 0, seqgen)

`midilink.py` puts USART1 on a TCP socket and the Monitor on another, so the
machine free-runs while Python talks to it, and exposes `send()`/`recv()`
matching `read_preset.py`'s `Synth`. Requests go out over DIN; replies come
back from the USB endpoint, decoded from USB-MIDI event packets.

**The transport must stream, not poll.** The model appends each packet to a
file as it is sent and the host tails it. Polling `SaveCapture` through the
Monitor costs a round trip and rewrites the whole capture per chunk: fine for
one 45-byte name record, and it timed out on the 146-chunk block transfer.
Streaming took that from a timeout to one second.

### Host-to-device: the endpoint works, the firmware does not act on it

`usbfs Send "F0 .."` packetises MIDI and delivers it to the MIDI OUT endpoint
through the receive FIFO, the same machinery SETUP uses. It demonstrably
arrives: traced, the device reads the status word `0x00040141` and pulls the
packets back out of the FIFO, decoding to exactly the bytes sent.

**The transport is correct and the firmware still does not act on the data.**
Verified at register level, after `SET_CONFIGURATION` the class init does run
and does open its endpoints -- `DAINTMSK` becomes `0x00050003`, `DIEPCTL1`
opens the MIDI IN side and `DOEPCTL2` the OUT side, armed via `DOEPTSIZ2` for
a 64-byte packet. (An earlier note here said the endpoints were never opened;
that was wrong, and came from traces that ended before the event. The class
init happens ~7,600 accesses into the enumeration window, so trace with a
large `TraceLimit` or you will miss it.)

A delivery then behaves exactly as it should: `GRXSTSP` reads `0x00040142`
(endpoint 2, 20 bytes, OUT data), the device reads all five packets,
`GRXSTSP` reads the completion status, `DOEPINT2` shows `XFRC`, `DOEPTSIZ2`
reads `0x0008002C` so the HAL computes 64 - 44 = 20 bytes received, and the
endpoint is re-armed. Nothing is left dangling.

Four fixes made along the way, all independently right: the OUT endpoint is
number **2**, not 1 (`09 05 02 02 40 00` in the device's own descriptor); the
receive queue must be dequeued on OUT completion or `RXFLVL` stays asserted
forever; `DOEPTSIZ` must be decremented by the bytes delivered, because the
HAL derives the received length from what is left in it; and the packetiser
labelled every message as SysEx, which is wrong for channel messages -- the
code index number is the status nibble.

### Host to device now works, and the last bug was ours

For a while the reading above was "the class layer receives the data and does
not forward it to the MIDI engine", because a note-on sent over USB produced
silence where the same note over DIN played. That was wrong, and the way it
was wrong is worth recording: every piece of evidence for it was real, and the
conclusion drawn from it was not.

Reading the firmware settles the receive chain with no ambiguity:

    OTG_FS_IRQHandler          0x080254C0   -> HAL_PCD_IRQHandler(0x2001EBF0)
    HAL_PCD_IRQHandler         0x0801C9E0   RXFLVL copies into ep->xfer_buff,
                                            OEPINT/XFRC -> DataOutStageCallback
    HAL_PCD_DataOutStageCallback 0x08057180 Arturia's own, not the weak one:
                                            calls USBD_LL_DataOutStage, then
                                            passes (0x2001EFEC, rxcount) to
                                            the app, then re-arms for 64 bytes
    usb midi unpacker          0x08047B18   cable number must be 0, length is
                                            rounded down to a multiple of 4,
                                            >= 80 bytes must be free; CIN
                                            lengths come from a table at
                                            0x0807539C; MIDI bytes go into a
                                            256-byte ring at *(0x2001DDE4)
    usb ring drain             0x08047CC4   called every main-loop iteration
                                            from 0x08024F2A, feeds the parser
                                            at 0x080480A8 with ctx 0x2001DD20
    din ring drain             0x08047C6C   the same, six bytes later, ring at
                                            0x2001E08C, ctx 0x2001DC5C
    message callback           0x080478A4   both parsers share it
    dispatch                   0x08048524   per-port, see below

Two gates in there are worth knowing about because they look like the answer
and are not. `0x200000E4` is a **source tag**, not an enable: each drain sets
it (USB `0x00`, DIN `0x80`) before parsing and back to `0xFF` after, and every
callback bails if it reads `0xFF`. The dispatch then jumps on the port -- USB
is port 1 at `0x08048722`, DIN is port 2 at `0x0804870A` -- and each checks
the channel against `0x200007A8+0x3C` (which is `0x200007E4`, the byte this
repo already pokes to `0x7F`) and then a per-port enable byte, USB at
`0x20003778+0x2DD` and DIN at `+0x2DE`. Both enables already read non-zero
under emulation (`0x0E` and `0x0C`), and poking the USB one produced a
byte-identical render, so neither was ever the blocker.

What the instrumented run actually showed: the bytes reached the ring
(`wr = 3`, `rd = 0`, contents `90 3C 64`) and sat there for a full firmware
second while the drain -- which runs every main-loop iteration -- never
consumed them. Sampling the PC explained why. Before the note it moves around
the firmware; after it, every sample is inside the USB interrupt
(`0x8020C6C`, `0x801CCFE`, `0x801D15E`, `0x80208F6`), with `CFSR` and `HFSR`
both clean. Not a fault -- an interrupt storm. The main loop never ran again.

The cause was in this model. `WriteInner` listed `DIEPINT0/1` and
`DOEPINT0/1` as write-1-to-clear and nothing else, so a write to `DOEPINT2`
-- the MIDI OUT endpoint, moved from 1 to 2 late in the previous session --
fell through to the plain `regs[offset / 4] = value` at the bottom. The
firmware's attempt to clear `XFRC` therefore left it set, `DAINT` kept
reporting endpoint 2, `OEPINT` never dropped, and the ISR re-entered forever.
The endpoint registers are now handled by bank and offset (`IsEndpointInterrupt`,
`IsInControl`) rather than by an enumerated list, so this cannot come back for
endpoint 3.

The control that settles it, since the emulated synth holds a middle C of its
own and note 60 is therefore indistinguishable from silence -- the note-60
arms were byte-identical to a no-note run and proved nothing:

    no note at all      261 Hz drone, AC RMS 2952
    note 67 over DIN    392 Hz,       AC RMS 2950
    note 67 over USB    392 Hz,       AC RMS 2950   byte-identical to DIN

A whole preset also reads back with every request sent over USB
(`send_over_usb = True`, slot 4, name and all 4672 payload bytes), so the
path is good for SysEx in both directions, not just notes.

### Preset writes work

A whole preset now writes over USB and reads back byte for byte. The control,
because every slot in a `mkflash` image reads as `Init` and comparing a write
against an identical slot would prove nothing:

    before: slot 4 == slot 7 payload            (identical, so a match proves nothing)
    written: slot 4's payload with two bytes flipped
    after : slot 7 == what we wrote             True
            slot 7 != what was there before     True

Three things were in the way, and none of them was the firmware's SysEx
dispatch -- which is where the previous note pointed, wrongly.

**1. The write hung inside the QUADSPI model.** Following the `23 52` handler
out of the firmware: the sub-opcode jump table at `0x0804AD20` is keyed on the
*sub* byte, not the command (`sub - 0x15`, 73 entries), and `0x52` has a real
handler at `0x0804B290`. It reads the slot from bytes 8/9, loads the page,
branches on byte 10, stores the record and calls the page write at
`0x08045238`, then falls into the reply tail at `0x0804AF3C` which builds the
`00 18` ack and sends it through `0x0804C718`. Tracing that showed the page
write happening and the reply tail never being reached. The PC was spinning at
`0x0801DD94`-`0x0801DDAA`, writing bytes to `QUADSPI->DR` (offset 0x20) and
polling `QUADSPI->SR` bit 2, FTF, which never came.

`HAL_QSPI_Transmit` re-writes CCR with `FMODE` set to indirect-write just
before pushing data and -- unlike `HAL_QSPI_Receive` -- does **not** re-write
AR afterwards. This model treated any CCR write as a brand new transaction, so
it discarded the write already armed on the AR write. A CCR write that changes
nothing but FMODE now re-arms instead, keeping the programmed address.

**2. Host-to-device delivery raced the CPU.** `usbfs Send` runs on Renode's
Monitor thread. It was enqueuing into the receive FIFO and editing `DOEPTSIZ`
directly, underneath a running CPU doing the same. That wedged the machine
part way through a transfer -- intermittently, and the giveaway was that
Renode's own Monitor stopped answering too, not just the firmware. Send now
only appends to a queue under a lock; delivery happens from the 1 kHz host
pump, which is a `LimitTimer` on the machine's clock source and therefore the
emulation thread. Delivery also waits for the endpoint to actually be armed
(`EPENA` set, `DOEPTSIZ` big enough), because the HAL derives the received
length from what is left in `DOEPTSIZ` and delivering into an unarmed endpoint
made the class layer read 64 bytes out of a buffer holding twenty.

The pump is the right home for this and the GINTSTS read is not: after
enumeration the endpoint is purely interrupt-driven, so with nothing pending
the firmware never reads GINTSTS at all, and a delivery hook there is never
reached. Chicken and egg -- the thing that would wake it is the thing waiting.

**3. Two bugs on the host side of `midilink.py`.** Renode's stdout was a pipe
nobody read; at 64 KB it blocks the writer and the machine simply stops. It
goes to a file now (`midilink-renode-<port>.log` in the temp directory, worth
reading when something stalls). And `_next_sysex` read the DIN socket and only
fell through to USB when that socket had nothing -- but the firmware sends
active sensing on DIN continuously, so replies waiting on USB could go unread
until the caller timed out. The two transports now have separate buffers and
both are drained every pass; separate matters, because DIN carries real-time
bytes that would otherwise land inside a USB SysEx and corrupt its framing.

The stored name was never a problem: a slot only reports a name once it holds
a valid preset. The `UsbClone` test wrote slot 4's payload with two bytes
flipped, which corrupts the TLV, so the firmware kept reporting the slot as
`Init`. Writing a real preset gives the real name back.

### Writing one of the repo's saved patches

`presetlib` hands back the 4088-byte *decoded* stream; `write_preset.py` sends
the 4672-byte *wire* form, and `preset_tlv` only has `decode_7to8`. The
inverse is four lines and lives in the scratch test for now -- it belongs in
`preset_tlv.py` if writing saved patches becomes a normal workflow.

Doing that turned up a real flash bug. Writing a patch into an **occupied**
slot came back as the bitwise AND of the old and the new: `Nervous` written
over `Aft-WavPad` read back as `@dp$Gar`. The NOR model is deliberately
faithful here -- `Program` ANDs, so a missing erase shows as corruption rather
than silently working -- and the erase really was missing. Logging the QUADSPI
commands during a write shows what the firmware actually issues:

    cmd 0x05  x34      read status
    cmd 0x06  x17      write enable      (1 erase + 16 page programs)
    cmd 0xEB  0x086000 read the page     (slot 5 == (5+129)*4096)
    cmd 0xD7  0x086000 *** erase ***

`0xD7` is the other 4 KB sector-erase opcode and it is the one this firmware
uses; the model knew only `0x20`, so the erase was a no-op. Fixed. Writes into
an *erased* slot were always exact, which is why every earlier test passed --
every slot in a bare `mkflash` image is erased.

**A bare `mkflash` image has no presets in it.** Every slot reads as `Init`
and renders the same default drone, AC RMS ~2950 at the note's pitch. Any
experiment that compares presets needs `--presets-from
captures/mcc-presets.jsonl`; with that, slots render distinctly (slot 0 at
AC RMS ~540, slot 3 at ~85, not byte-identical). `--preset N=FILE` alone is
not enough: it writes only regions 1+2, leaving region 0 erased, and the
firmware treats such a slot as empty.

**Resolved: the write path is general and correct; one file is not.** The
1483-byte difference was never in the emulator. `nervous-keys.mfpz` and the
capture's slot 0 are both called NervousKeys and are *different schema
versions of it* -- the capture's is schema 1 with 19 modules, the export is
schema 3 with 20, the extra one being `Voc`, exactly as `write_preset.py`
already documents (high nibble 1 = no Voc, 3 = with Voc). `presetlib`'s claim
that a `.mfpz` is byte-identical to the capture's copy was wrong and is now
corrected in place.

The full matrix, all rendered deterministically under `emulation RunFor`,
note 67, destination slot 3:

    payload                        route          AC RMS   fundamental
    slot 3, untouched              --               84.5      384 Hz
    capture slot 0 (schema 1)      flash           553.7      398 Hz
    capture slot 0 (schema 1)      SysEx write     553.7      398 Hz
    Hollows (schema 3 + Voc)       flash          3828.4      272 Hz
    Hollows (schema 3 + Voc)       SysEx write    4385.7      200 Hz
    nervous-keys.mfpz (schema 3)   SysEx write       0.0        --

Writing the capture's slot 0 into slot 3 reproduces slot 0's own render to
the decimal, so the write path carries a preset faithfully. Schema 3 is not
the problem either -- Hollows carries `Voc` and is loud by both routes. The
silence belongs to that one exported file, and `read_mfpz`'s alignment is not
obviously the culprit: both payloads start `23 56 43 4f` (`#VCO`) with the
same record structure and differ in the values, not the framing.

### The Speech preset: it is the engine, not the schema

`Snit` -- the preset that says "eight" on the user's device at UI position 5 --
still renders silence, and the reason is now pinned down and is *not* what
this file previously said.

What it is not. Not the write path: writing the capture's slot 0 into slot 3
reproduces slot 0's own render to the decimal. Not the header byte: `Hollows`
is schema 3, carries `Voc`, and is loud by both placement routes. Not the
`.mfpz` or anything about that one file: Snit placed straight into flash by
`mkflash`, with the loader hook at `0x080454D4` confirming slot 4 loads,
renders 240,000 samples of digital zero.

What it is: **the oscillator engine**. Snit's `VCO`/`Type` first byte is
`0x16`. Placing a *different* captured preset that uses the same engine
(`Revert to Iron`, capture slot 384) at a Program-Change-reachable slot in an
otherwise working image renders silence too, while `NervousKeys` in the same
image plays at AC RMS 703.9. Sixty-one of the 398 captured presets use engine
`0x16` and none of them make a sound here.

And the "schema 0x33" story was a red herring built on a perfect correlation.
Byte 20 of the name record is not a payload schema version; it tracks the
engine, one to one, across the whole capture:

    engine 0x0C  byte20 0x10  140      engine 0x11  byte20 0x31   79
    engine 0x0D  byte20 0x11   62      engine 0x12  byte20 0x32   37
    engine 0x0E  byte20 0x30   19      engine 0x16  byte20 0x33   61

The set of "schema 0x33" presets *is* the set of engine-`0x16` presets --
identical, all 61, slots 384..448. Hours went into byte 20 as a format
version. Its high nibble does track whether a `Voc` module is present (1 = no,
3 = yes), so `write_preset.py`'s note is not wrong, but the low nibble
identifies the engine and that is what mattered all along.

Where to look next: not at flash data. Logging every QUADSPI command from the
Program Change through the note shows both the engine-`0x16` preset and the
control reading exactly one thing -- their own 4 KB preset page, `0xEB` at
`0x088000` and `0x081000` -- and nothing from the sample region at
`0xCD0000` or the wavetable bank. Whatever engine `0x16` needs, it is not
fetching it over QUADSPI at load or at note time. The next move is the
firmware's per-engine render dispatch: find where the engine index selects a
generator and see whether `0x16`'s is reached at all.

#### The engine decoder was wrong, and that is why nothing here added up

> **RETRACTED IN PART, 2026-09-10.** The diagnosis below -- that the record
> is three bytes and only one was being read -- is right. The *fix* was not.
> The count byte is **not** used to rescale the table on read: the device
> decodes every preset against its own engine list, and the stored value is a
> 15-bit fraction of that list, not a 0..127 raw. The corrected model, with
> nine hardware panel confirmations behind it, is in FORMATS.md
> under "Oscillator engine encoding". Read that instead of the paragraphs
> below, which are kept only so the wrong turn stays visible.
>
> The tell that should have been caught here: this section's own fix was
> validated entirely against *this repo's* tables and the capture's internal
> consistency. Two of its three worked examples were correct by luck --
> `Snit` and `Raverz` both sit at value 16384, and the count-scaled model
> happens to give the right answer for one of them.

The `VCO`/`Type` TLV record body is three bytes and `preset_fields.py` read
only one of them. Byte 0 is the **number of engines the saving firmware
had**; bytes 1-2 are the little-endian value whose high byte is the 0..127
raw. Reading the raw alone and mapping it through the current 22-engine table
mis-decodes every preset saved by an older firmware -- 337 of the 398 in the
capture.

    Snit    body 16 00 40  -> count 22, raw 64 -> Speech
    Raverz  body 0c 00 40  -> count 12, raw 64 -> V. Analog
    Nervous body 0c ff 5f  -> count 12, raw 95 -> Formant   (was read as "Harm")

Same raw, different engine, because the count differs. Both lines are
hardware-confirmed on the owner's 5.0.0.2084 device: Snit's panel says Speech,
and Raverz -- written to their UI preset 2 from this repo's capture
specifically to settle it -- reads V. Analog. Counts across the capture are
12, 13, 14, 17, 18 and 22, which is the engine list growing across releases;
engines are appended, so an older table is a prefix of the current one.
`engine_from()` and `osc_engine()` in `preset_fields.py` now do this properly.

This invalidated three earlier conclusions in a row, all recorded above and
all wrong: that the tag byte was an engine index, that Snit was a Sample
preset, and that "Speech works because Raverz plays" -- Raverz is V. Analog.
The lesson is the one this project keeps relearning: a correlation across a
capture is not a decoder, and the owner's panel settles in one message what
three trace sessions could not.

#### Why Knit and Snit are silent: the generator returns a constant

Established from the binary and from Knit alone. An earlier version of this
section leaned on `Voidom` as a "working Speech preset" -- that label came
from this repo's own interpolated engine table and was **never confirmed on
hardware**, so every conclusion drawn from it is withdrawn. The only
hardware-confirmed engine labels are Snit = Speech, Knit = Speech (both
count 22) and Raverz = V. Analog (count 12).

The render chain, read out of the firmware:

    0x08034394  bx r3            virtual call, vtable[9] of the oscillator
                                 at object+0x50ac
    0x0803099C  render wrapper   two arms on the block size at [r1+0x48];
                                 Knit takes the ==32 arm at 0x08030A24
    0x0800D480  thunk            -> 0x0805F458 with the output buffer in r3
    0x0805F458  oscillator entry engine object array at obj+0x6E4, indexed by
                                 a value scaled against the engine count at
                                 obj+0x724
    0x0805DA8C  the generator    a formant filter bank: a chain of vfma.f32
                                 into state at [r0+0x54..0x64], excited by an
                                 LCG (multiplier 0x0019660D at 0x0805DD20)

Everything up to and including the generator works for Knit. It is reached
(`SPEECHGEN r0=0x20009330 r3=1`), it is asked for 16 samples a block, its
formant table at `0x0807C84C` is in the firmware image, and `0x2000011C` --
which looked like a data pointer -- is just the RNG state, different on every
call.

**The generator returns a constant.** Reading the six samples the wrapper
loads back at `0x08030A4E`, every one is `1`:

    SAMPLES 1 1 1 1 1 1
    SAMPLES 1 1 1 1 1 1

So this is not dispatch, not preset data, not the write path and not a
missing table: the formant DSP is running and producing DC. The next step is
its inputs -- `s0` and `s1` on entry, and the object fields at `[r0+4]` and
`[r0+16]` it mixes them with. `s1` is clamped to `[0, 0.5]` at the top of the
function and multiplied into the excitation, so an `s1` of zero would give
exactly this.

#### The filter runs with zero excitation

Four things settled, all measured on Knit and Snit alone.

**Note length is not it.** The note-on is sent and never released, so the key
is held for the whole capture. Held for a full **6.00 s** (576,000 samples at
96 kHz) the output is zero across all twelve envelope segments; a harness
check over the same 6 s produces a normally evolving envelope, so the capture
path is fine at that length.

**The note reaches the oscillator.** Reading the object at `0x20009330`
across a note-on, field 0 changes `0x3F1F743E -> 0x3D5CCCB0` (0.624 ->
0.0539, a frequency term) and `+0x14` changes `0x266 -> 0x52`. So dispatch
and note routing work.

**The generator's output does not depend on the note.** The six samples the
wrapper loads back at `0x08030A4E` read `1 1 1 1 1 1` *before* any note-on and
`1 1 1 1 1 1` after it, unchanged over hundreds of blocks.

**The filter state is live but stuck at zero.** A watchpoint on `[r0+8]` and
`[r0+16]` shows them written constantly from inside the generator itself --
4761 writes from `0x0805DCF6`, plus `0x0805DDC0`, `0x0805DD80`, `0x0805DFB8`,
`0x0805DFA2`, `0x0805DA64/68`. So those fields are filter *state*, not
coefficients, and the bank is integrating zero: zero excitation into a filter
with zero state stays zero forever.

One hypothesis tested and disproved: the per-engine init at `0x0805F55C` is
skipped unless the engine index changes (`cmp r3, r7; beq 0x805f572`), but
selecting a different preset first and switching into Knit leaves the fields
zero and the output silent, so a missed init is not the cause.

What remains is the excitation itself. The generator's head computes
`s1 = ((([r0+4] - 0.0125) * s0) + 0.0125) * s1`, clamps `s1` to `[0, 0.5]`
and multiplies it into the excitation; `[r0+4]` is `0x3C14F209` (0.00909),
non-zero. So `s0` or `s1` on entry is the thing to read, and both are VFP
registers -- which needs either VFP access from a Renode hook or finding the
memory they are loaded from in the caller at `0x0805F458`.

#### Five hypotheses tested and killed

All measured on Knit/Snit, no reference presets used to label anything.

**Not the note length.** Note-on is sent and never released; held a full
6.00 s the output is zero across all twelve envelope segments.

**Not missing flash data.** The boot scan walks `0x281000` -> `0xF4F000`,
sixteen addresses per 64 KB -- that is the firmware building the *sampler*
directory over storage that a `mkflash` image leaves erased, which explains
the sample and granular engines and nothing else. Speech's formant table is
at `0x0807C84C`, inside `main.raw`, and there are no flash reads at all
during Program Change or note-on.

**Not an init that never runs.** `[obj+0x660]`, the zero float in the
sub-engine index computation, *is* written -- five times, always with 0, from
`0x0805F86C`, `0x0805F3A8` and `0x0805F748`. It is deliberately zero, which
makes the sub-index come straight from `[r1+28]`. `[r0+4]` is likewise
generator state, written 77 times from `0x0805DD44`.

**Not dead inputs.** The caller at `0x0805E754` computes a float per formant
(`vsub`, then `vdiv.f32 s22, s15, s18`) and stores it into the generator's
record with `str lr, [r0], #24`. Read back at the call site those values are
live and varying: 0.1335, 0.1073, 0.0453, 0.0012, 0.1164.

**Not a broken fused multiply-add.** The generator is built on `vfma.f32`,
which made a mis-translated FMA tempting. But the DSP regions that *do*
produce audio here use it at least as heavily -- 42 and 20 occurrences
against the Speech generator's 29 -- so if it were wrong they would be silent
too.

So: live varying inputs go in, the accumulators at `[r0+0x54..0x64]` stay at
zero, and the output is a constant `1` whether or not a key is held. That is
still unexplained. The one measurement not yet made is the generator's own
float arguments `s0` and `s1` at entry, which the head multiplies into the
excitation and clamps to `[0, 0.5]`; reading them needs VFP register access
from a Renode hook (`GetRegisters()` did not work) or single-stepping the
entry. That is the next step, and nothing above substitutes for it.

#### The generator's float arguments, measured

`GetRegisters()` is not available from a Renode hook, but the values can be
read without VFP access. The caller sets `s1 = s19` and `s0 = s20` at
`0x0805E6F0/F4`, and the generator's prologue does `vpush {d8-d15}`, spilling
s16-s31 to the stack -- so at `0x0805DAA4`, s19 is at `[sp+12]` and s20 at
`[sp+16]`. Measured, stable across calls:

    s1 (s19)   = 2.73517
    s0 (s20)   = 0.0
    [r0+4]     = 0.00909091

Working the head through: `s14 = 0.00909 - 0.0125 = -0.00341`;
`s15 = s14*s0 + 0.0125 = 0.0125` (the `s0` term vanishes); `s1 = 0.0125 *
2.735 = 0.0342`. Positive, well under the 0.5 clamp, so the function does
**not** take its `bmi` bail and the excitation amplitude is non-zero. The
"zero excitation" reading was wrong.

The one anomalous value is `s0 = 0.0`, exactly, every call. It is written
once, at `0x0805E5D4` (`vmov.f32 s20, s1`), from the *caller's own* incoming
`s1` -- so the zero arrives from further up. That caller is reached from
`0x0805B2DA` and `0x0805B40A`, both inside the methods of engine descriptor
`0x080785D8`, which is record 5 of 7 and matches the sub-index this engine
selects (`[obj+0x658] = 5`).

So the chain is `0x0805B2xx/0x0805B4xx -> 0x0805E554 -> 0x0805DA8C`, and
tracing `s0`'s zero further means the same spill-read trick one level up.
Whether `s0 = 0` is a fault or simply correct for default parameters is not
established -- with `s0` zero the amplitude still comes out non-zero, so it
does not on its own explain a constant output.

#### Name-record byte 19 is the preset category

Not "no known meaning", as `write_preset.py` had it. It is the category from
the save screen: `0x02` is Keys (Snit) and `0x03` is Lead (Knit), both from
the owner's device, and it takes the twelve values `0x00`-`0x0B` across the
capture. The other ten are unmapped.

`snit_slot5.bin` is a bad capture -- it decodes as `Harm`. Use `snit.bin`,
confirmed byte-identical to a fresh device read.

#### The Speech engine speaks (2026-09-09)

`Snit` renders as an understandable **"eight"**, confirmed by the owner
against their hardware. No poking, no forced values -- the only requirement is
that the note has a rising gate edge:

    note-on, note-off, note-on   ->  the utterance fires

The engine had never been broken. The voice gate is left at 1.0 when the
firmware finishes booting, so a *first* note-on writes 1.0 over 1.0, which is
not an edge, and the Speech engine triggers only on the edge. Every silent
render in the long investigation above used a single note-on after boot and so
could never have worked. Full chain and addresses in `FIRMWARE.md`.

**This is not an emulator bug -- the hardware does the same.** The owner
checked: from a cold power-on, selecting Snit and pressing a key produces no
sound on the real device either; it speaks on the *second* press, and from
then on the first press always works until the next power cycle. Renode
reproduces that, power-cycle dependency included, which is a sharper
correctness check than any render comparison in this file.

It looked as though `mfrender.py` would need a priming note-on/off before each
capture to supply that edge. **It does not** -- measured, not assumed. Stock
`mfrender.py` renders `Snit` speaking:

    mfrender.py --preset snit_page.bin --notes 60            AC RMS 466.2
    mfrender.py --preset snit_page.bin --notes 60 --settle 2.5   AC RMS 469.0

Priming was implemented and then reverted, because it earned nothing and cost
something. With the default settle the primed and unprimed renders were
**byte-identical**, and with `--settle 2.5` the primed run failed outright --
"no audio captured". Whatever cycles the gate in `mfrender.py`'s own boot and
program-change sequence is already enough; adding a second edge is at best a
no-op and at worst wedges the capture.

The hand-written `.resc` experiments earlier in this file *did* need the
priming, because they sent a bare note-on and nothing else. That is a
difference between those scripts and `mfrender.py`, not a gap in the tool.

**Two warnings about measuring this.** Renders driven live over MIDI with
wall-clock sleeps are *not* reproducible: slot 3 measured 84.5, ~150 and
2067 in different sessions depending on what was rendered before it, and slot
56 measured 3828 from a script and 0.0 live. State leaks across a Program
Change. Every number above comes from a one-shot `emulation RunFor` script,
which reproduces exactly. And a slot rendering silent proves nothing on its
own: slots 5 and 6 are silent untouched, because `Aft-WavPad` wants
aftertouch and `Brd But Arp` wants the arpeggiator. Three separate arms of
this investigation were confounded by writing into a slot that was already
silent.

Two more things found by accident, both worth knowing. `sai2 SavePCM`
enumerates the capture list while the SAI appends to it, so calling it on a
free-running machine throws `Collection was modified` and takes Renode down;
pause around it, or give the model a lock. And some captured presets render
silent under emulation regardless of any writing -- slot 5, `Aft-WavPad`, is
one -- which is a separate question from anything here.

### Still open

The port capacity field at `0x20003EF0+0x2D0` is *still zero* even with the
device enumerated and answering. Hours went into that field on the assumption
it was the configured-state gate; it is not, and the reply reaches the host by
a route that does not depend on it. Anyone picking up the `0x33` schema
question should not start there.

## Next

1. Compare a rendered preset against the instrument: pitch first, then
   waveform. Hardware is now the only reference that matters, and
   `filter_cal/` already does the magnitude half of this for the filter --
   extending it to a whole-render comparison is the open piece.
2. Make the SAI and DMA models survive `Load` -- the `LimitTimer` and the
   request line are the suspects -- and then use one process per batch of
   experiments (`Save` once at 14 s, `Load` per arm). Cross-process `Load`
   needs the peripherals built as a real assembly rather than `include`d
   sources. Until then, full boots only.
3. Cut wall clock further if it matters: a render is 80-110 s, split ~36 s
   pre-loop boot (~3.6x real time) and ~5.4 s per firmware second in the main
   loop. Both are dominated by Renode's ~100-200 us per time-source event on
   this Mono build -- SysTick alone is 10,000 events/s -- so the biggest
   remaining lever is a Linux or .NET Renode build rather than anything in
   this repo. Measure before optimising: the last two wins were a PWM timer
   and a USB poll, neither of which was where anyone would have looked. Never
   hook the main loop (285,000 iterations/s) or watchpoint the audio object
   for a full run.
4. `HandleConfigurationRead` still does not report the NDTR count-down
   (`NDTR` reads the latched 0x80 throughout); nothing here depends on it yet.

## The per-engine survey (2026-09-11)

The first systematic answer to "which oscillator engines render?". 22 presets,
one per engine, built from the **device's own Init** (panel slot 200, read over
SysEx: 110 fields, `Basic Waves`, engine-count byte 22, region 0 header 0x33).
Each arm differs from the Init in exactly two ways: `VCO/Type` set to the
measured `ENGINE_VALUES` entry, and `VCO/Param1..3` set to `ENGINE_DEFAULTS`
where those were measured. `make_engine_presets.py` builds them and asserts the
engine round-trips before writing anything -- which is how the `osc_engine()`
bug was caught (see FORMATS.md).

Rendered in one boot, C4 for 1 s plus 1 s of tail, at 96 kHz.

**Result: 17 of 22 engines produce audio. 5 are silent.**

| engine | RMS | peak | f0 | note |
|--------|-----|------|-----|------|
| Basic Waves | 2952 | 4572 | 261.2 | |
| Superwave | 3027 | 12031 | 261.2 | |
| **Wavetable** | **0** | **0** | -- | silent, see below |
| Harmonic | 2929 | 4262 | 261.2 | |
| KarplusStrong | 184 | 3994 | -- | quiet: a pluck with the Init's decay |
| V. Analog | 3770 | 7787 | 261.2 | |
| Waveshaper | 5111 | 7987 | 1830 | strongest partial is not the fundamental |
| Two Op. FM | 6208 | 8780 | 261.2 | |
| Formant | 2988 | 10340 | 261.2 | |
| Chords | 4442 | 11256 | 261.2 | |
| Speech | 7754 | 11704 | 261.2 | |
| Modal | 1945 | 14636 | 1046 | 4th harmonic dominant |
| Noise | 1595 | 3591 | 261.2 | centroid 11.5 kHz |
| **Vocoder** | 1215 | 2460 | 261.2 | **renders** -- and is selectable |
| Bass | 7188 | 12145 | 261.2 | |
| SawX | 4502 | 7464 | 261.2 | |
| Harm | 8005 | 10684 | 261.2 | |
| WaveUser | 5101 | 7678 | 261.2 | |
| **Sample** | **0** | **0** | -- | no sample data in the image |
| **Scan Grains** | **0** | **0** | -- | ditto |
| **Cloud Grains** | **0** | **0** | -- | ditto |
| **Hit Grains** | **0** | **0** | -- | ditto |

Pitch is 261.2 Hz against C4's 261.63 -- 2.8 cents flat, which is the FFT bin
spacing at this window length, not a tuning error.

### Controls

- **Position.** The unmodified Init was rendered at position 1 and again at
  position 24: RMS 2952.6 vs 2952.5 (0.003%), peak 4571 vs 4567. `Speech` was
  repeated at position 12 and position 25: 7754.2 vs 7753.7 (0.006%). Both
  pairs hash differently, as expected -- so arms at different positions in
  this batch are comparable, and the batch is valid.
- **Silence is real silence.** The five silent renders are *byte-identical to
  each other* and contain zero non-zero samples in 384,000. Byte-identical
  output between arms means neither did anything, which here is the point:
  they produced nothing at all rather than something quiet.
- **The engine took effect.** No two sounding arms share a hash, and arm 0
  (`Basic Waves`) matches the Init control to 0.003% -- which it must, since
  the Init *is* Basic Waves and that arm changes nothing.
- **Slot loading** was checked by `mfrender.py`'s own `FUN_080454D4` hook on
  every job; no job rendered a stale preset.
- **On hardware:** the same 22 presets were written to device slots 0-21
  (panel 1-22) and read back. All 22 round-trip with the right name, the right
  `VCO/Type` value and header 0x33. The ear check is the owner's.

### Wavetable is silent for want of data, not for want of a table

`Wavetable` renders nothing at table position 0, 8 or 15 of 16 -- three arms,
all byte-identical silence. The paired control is `WaveUser` at the same three
positions, which sounds at all three and *differs* between them (RMS 5102 /
5394 / 4419), so the discrete Param1 selector works and the user-wavetable
region is populated. `mkflash.py` fills that region from the firmware bundle's
`userwavetable_factory_v1_3__20211015.bin` at `(index + 0xF5F) << 12`.

So the `Wavetable` engine reads a **different** region, and the image leaves it
erased. That puts it in the same class as the four sample engines: an emulator
data gap, not an engine defect. Unverified on hardware -- if panel 3 sounds on
the device, the engine is fine and only the image is short.

### `mfrender.py --preset N=FILE`

New: an imported preset can be placed in a named slot instead of borrowing one
from the top of the library, so the emulator's slots can match the hardware's
for an A/B. `--preset 5=engines/05-VAnalog.page` puts that file in slot 5.

## Closing the data gap: samples and wavetables in the image (2026-09-11)

The owner confirmed on hardware that panel 3 (`Wavetable`) and panels 19-22
(`Sample`, the three Grains) all sound on the device, so the five silent
engines in the survey were an image problem, not an engine problem.

### How the layouts were measured

Not by reading the firmware, and not by guessing: **by letting the emulated
firmware write them.** `midilink.py` boots the machine and exposes a
`Synth`-shaped object, so `write_sample.py` and `write_wavetable.py` send over
the emulated link exactly as they do to hardware. Two new Monitor commands on
the flash model make the rest work:

    quadspi SaveImage @after.img     # read the whole 16 MB back
    quadspi TraceReads true          # count reads per 4 KB page
    quadspi ClearReadTrace
    quadspi SaveReadTrace @trace.txt

A wavetable ingest takes 8.7 s, a small sample 12.8 s. Diff before against
after and the layout falls straight out. Formats are in FORMATS.md.

**The control that matters:** `mkflash.py`'s `place_samples` and
`place_wavetables` output is compared byte-for-byte against what the firmware
itself wrote, over 4 samples and 3 wavetables. Identical. The first attempt
was not -- it left the tail of each sample record erased where the firmware
zero-fills it -- and the comparison is what caught that.

### Read tracing answers "where does this engine look?"

With a note held, the trace is tiny and completely legible:

    Wavetable     0x083000 (its preset page)  0x000000
    Scan Grains   0x094000 (its preset page)  0x030000
    Sample        0x093000 (its preset page)  0x281000  x8

That is how the `Wavetable` engine's base was found. Boot reads, for the
record: a 3280-page linear sweep of the sample region from 0x281000 to
0xF50000, then 16 single-page reads at 0xFAF000 spaced 0x4000 apart (the live
wavetable slots), then 0x080000-0x081000 and 0xFFF000.

### Result

    engine        before      after      note
    Wavetable        0.0     5101.4      audio mirrored to offset 0 (see the
                                    retraction below: 3561.4 was the wrong layout)
    Sample           0.0      356.6      52-sample chain from 0x281000
    Scan Grains      0.0        0.0      still silent
    Cloud Grains     0.0        0.0      still silent
    Hit Grains       0.0        0.0      still silent
    Init control  2952.6     2952.6      unchanged, to the last digit
    Speech        7754.2     7905.3      position, not content

Usage:

    emu/renode/mfrender.py --samples data/banks/factory_sample_bank.mfsbz \
                           --wavetables data/banks/factory_wavetable_bank.mfwbz \
                           --preset 2=engines/02-Wavetable.page --out wt.wav

`mkflash.py` takes the same two flags. The wavetable flag is optional: with no
bank given the bundle's own factory wavetables are mirrored into the other two
copies, which is enough to make `Wavetable` sound.

### A wrong turn, recorded because the control caught it

The first "all five engines now sound" reading was **wrong**. The image under
test had been built by `mkflash.py` directly from the capture, so slots 2 and
18-22 held *capture* presets, not the survey's engine presets -- the renders
were of whatever engine those presets use. Nothing in the audio said so. What
said so was the Init control coming back at RMS 983 instead of 2952.6: the
same preset, the same note, a different number. Re-run against the image that
actually contains the survey presets, two of the five sound and three do not.

*Keep an invariant arm in every batch whose number you already know. It is the
only thing that catches "right measurement, wrong subject".*

## All 22 engines render (2026-09-11, second survey)

The three granular engines were the last holdouts, and the cause was one
16-bit field. Full story in FORMATS.md; the short version is that
the sample record's `+0x08` is the **content hash**, not the zero this repo
had it down as, and `FUN_08015c88` -- the granular render -- caches the
sample's length only when that field differs from its own cached copy. Zero
against zero means the length stays 0, the grain scheduler never schedules a
grain, and the engine renders silence while running happily at ~30 lookups a
second without ever touching flash.

How it was found, in order, because the order is the point:

1. **A QSPI address-register watchpoint** (`sysbus AddWatchpointHook
   0xA0001018 4 Write`, PC and LR from `self.GetCurrentCPU()`) showed `Sample`
   streaming from 0x281618 downward and `Scan Grains` issuing **no flash read
   at all** on note-on. So it bails before reading, rather than reading
   nothing useful.
2. **The factory control.** Factory granular presets -- `Trence`, `PianoseK`,
   `Slow Amen`, `Enigma` -- were silent too, at exactly 0.0, while factory
   `Sample` presets played (1711, 1491). That ruled out the generated presets
   and the parameter defaults in one run.
3. **The lookup hook.** `sample_record_lookup` (0x08045150) is called from
   0x08016e49 for `Sample` and from **0x08015ca1** for granular -- two
   different callers, which is what made the granular render findable.
4. **The decompile** of the function containing 0x08015ca1 showed the gate.
5. **`write_sample.py` had already written the answer down** and not known it:
   "real samples carry something non-zero ... 'Piano Pop' a2 66". That is
   0x66A2 little-endian, exactly the hash function solved earlier the same
   day.

A `cpu AddHook` on `osc_entry` was tried first and fired once, then not at
all -- the documented trap. The watchpoint worked immediately. Prefer the
watchpoint.

    engine          survey 1   survey 2      engine          survey 1  survey 2
    Basic Waves       2952.5     2952.5      Vocoder           1214.6    1214.4
    Superwave         3026.5     2967.1      Bass              7187.8    7187.8
    Wavetable            0.0     5101.4      SawX              4502.0    4502.0
    Harmonic          2928.5     2928.5      Harm              8004.7    8004.7
    KarplusStrong      183.7      183.7      WaveUser          5101.3    5101.3
    V. Analog         3769.7     3769.7      Sample               0.0    1836.4
    Waveshaper        5110.7     5110.7      Scan Grains          0.0    1700.6
    Two Op. FM        6207.6     6207.6      Cloud Grains         0.0    2658.4
    Formant           2988.4     2988.4      Hit Grains           0.0    2610.0
    Chords            4441.7     4441.7      Init control      2952.6    2952.4
    Speech            7754.2     7754.2      Init control (24)          2952.3
    Modal             1944.5     1944.5      Noise             1594.7    1594.7

**Nothing silent, 22 distinct hashes, and every engine that already worked
reproduces its previous number** -- the arms that did not depend on sample or
wavetable data are byte-identical to survey 1. The Init control is 2952.4 at
position 1 and 2952.3 at position 24.

`Sample` moving from 356 to 1836 is worth noticing: without the hash it was
finding *something* to play. A number that is not zero is not the same as a
number that is right.

## Retraction: the offset-0 wavetable layout was wrong, and "it made sound" is why

The `Wavetable` engine reads flash page 0x000000 -- that was measured with the
read trace and it stands. What was put *there* was a guess: 16 name pages then
64 audio pages, copying the bank at 0xF5F000 because it was the only
wavetable-shaped thing available. The preset went from silence to RMS 3561,
the Init control was unchanged, and that was written up as a fix.

It was reading the name pages as audio. The evidence was sitting in the render
the whole time and went unexamined: energy at the 18th harmonic, spectral
centroid 16 kHz, against a `WaveUser` render of the same table at 1542 Hz.

The discriminating test is obvious in hindsight and cost one render: **the same
table, played by the other engine.** `WaveUser` reads the 0xF6F000 copy and
gives table 0 as RMS 5101.3, centroid 1542 Hz, partials 260/263/523. Put the
audio at 0x000000 with no name pages and `Wavetable` gives 5101.4, centroid
1527 Hz, the same partials. Two engines, two storage areas, one table, 0.002%
apart. That is a cross-check. RMS 3561 against a baseline of zero was not.

*When a fix turns silence into sound, the question is not "is it louder than
zero" -- it is "does it match something independently known to be right".*

This one was caught by the owner asking whether the copy was a workaround of
ours. It was. The layout is now pinned by the control above; what writes that
area on real hardware remains open.

## LATENT DEFECT: Renode's `STM32_Timer` SR is not write-0-to-clear

Found while chasing the BPF stall, where it turned out to be inert. Recording
it because it is a real defect that is currently harmless *by luck*, and the
luck is one `DIER` bit wide.

On an STM32 timer, the status-register flags are **rc_w0**: writing 1 to a bit
leaves it alone, writing 0 clears it. So the idiomatic way to clear one flag is
to write all ones except that bit, and the firmware does exactly that --
`SR = 0xFFFFFFFB` to clear CC2IF, `0xFFFFFFF7` for CC3IF, `0xFFFFFFFD` for
CC1IF. Renode's `Timers.STM32_Timer` does not implement that:

    sysbus WriteDoubleWord 0x40010410 0xFFFFFFFB     # clear CC2IF
    sysbus ReadDoubleWord  0x40010410 -> 0xFFFFE1A0

The CC flags in bits 1-4 do clear, which is why nothing has broken. But
**COMIF (bit 5), BIF (bit 7) and the reserved bits above 8 are left set** by
the firmware's own clear idiom. On hardware they would be untouched.

It is inert today only because the firmware never enables those interrupts:
`DIER` reads 0x0000 under LPF and 0x000E under BPF -- CC1/2/3 only. The
platform does wire them, though:

    timer8: Timers.STM32_Timer @ sysbus 0x40010400
        BreakInterrupt -> nvic@43
        [TriggerInterrupt, CommutationInterrupt] -> nvic@[45, 45]

so the moment any firmware path sets BIE or COMIE, every flag-clearing write
will re-assert a spurious interrupt and the machine will storm. Verified today
that it does *not* currently: NVIC ISPR1 reads 0 across the write.

**This is the same class of bug as the `DOEPINT2` one** that made the USB
endpoint storm -- a status register whose clear-on-write polarity is not
modelled -- and that one cost a session. If a timer-related storm ever appears,
look here first. The fix is a corrected SR in a timer model of our own, in the
manner of `STM32_SAI.cs`; nothing needs it yet.

## `filter_type = BPF`: DIAGNOSED 2026-09-12 -- not a stall, a soft PWM

**It is not a hang and not a modelling error. BPF switches on a software PWM
service that costs about 4000x real time to emulate.**

The trail, because three plausible hypotheses were wrong before the right one:

1. **Where.** Sampling `cpu PC` through the monitor during the stall: every PC
   seen only under BPF falls in 0x0802555C-0x08025652, plus sightings of
   0xFFFFFFE9 (an EXC_RETURN), so it is an interrupt handler. `FUN_0802555C`
   is TIM8's capture/compare ISR -- the register offsets identify it exactly
   (+0x0C DIER, +0x10 SR, +0x24 CNT, +0x34/38/3C CCR1/2/3).
2. **What it does.** Three blocks, one per channel, each rescheduling its
   compare by adding a delta until it is ahead of CNT:
   `uVar6 = CCR + delta; while ((int16)(uVar6 - CNT) < 0) uVar6 += delta;`
   CC2's delta is a constant 20, CC3's a stored half-period of 10, CC1's comes
   from a state machine. The CC3 worker toggles **GPIOE pin 12** through BSRR:
   this is a bit-banged PWM, almost certainly the analog filter's control line.
3. **Why only BPF.** `DIER` is **0x0000 under LPF and 0x000E under BPF** -- the
   capture/compare interrupts are only enabled in bandpass. Under LPF the ISR
   never runs, which is why LPF was always fine.

**Three wrong hypotheses, each killed by a measurement:**

- *SR is not write-0-to-clear.* The firmware clears a flag by writing
  0xFFFFFFFB, all ones but one, and Renode's `STM32_Timer` stores
  0xFFFFE1A0 -- so reserved and COMIF/BIF bits do get set, which is wrong.
  But the CC flags themselves clear correctly, and `DIER` does not enable
  COMIF or BIF, so it causes nothing here.
- *Those spurious flags assert an interrupt line.* NVIC ISPR1 reads 0 before
  and after the write. They do not.
- *One of the catch-up loops never terminates.* All three CCRs advance and
  wrap freely during the stall. Nothing is stuck.

**What it actually is.** The ISR reschedules 10-20 ticks ahead, so it fires
every 1-2 us of virtual time at the modelled 10 MHz -- on the order of a
million interrupts per virtual second. Measured: 0.7 ms of virtual time in 3 s
of wall clock, about 4000x slower than real time, which is exactly that
arithmetic. Rendering two seconds of BPF audio would take some hours.

And our model is the *forgiving* case. Reading the RCC the firmware programmed
-- PLL M=8 N=360 P=2, SYSCLK 180 MHz, APB2 /2, TIMPRE=1 -- the real TIM8 clock
is 90-180 MHz against the 10 MHz the platform declares. On hardware this
service runs even faster and the ISR takes about as long as its own delta, so
the loop is self-pacing: a hard-real-time soft PWM with a core largely
dedicated to it. Perfectly sensible on the device, hopeless under emulation.

**Consequences.** The GPIO it drives is the analog filter's control, which is
outside the emulated domain entirely and is modelled by `filter_model.py`
instead. So the emulator does not need this service at all, and suppressing
TIM8's compare interrupts would make BPF renderable -- a deviation in the
class of not emulating the filter, but a deviation nonetheless, and it would
need a control showing LPF renders byte-identically with it in place. Not done
yet; excluding BPF is the workaround meanwhile.

## `filter_type = BPF` stalls the emulator (2026-09-11) -- superseded above

Found by the generator, which is the point of a generator: a hand-built test
set never varies the one switch nobody thought to vary. Every preset in the
engine survey descends from the device Init, which is LPF, and all the
bandpass work in the filter calibration was done on hardware -- so the
emulator had most likely never rendered a BPF preset until a generated
candidate did.

    LPF   renders
    HPF   renders
    BPF   spins: no audio, no progress, 196% CPU, and it takes the rest of
          the batch with it

Isolated by stepping one field at a time from a candidate that rendered to one
that did not. The stall survives to a minimal case: take a preset that renders
and change nothing but `filter_type` to BPF. The cutoff value is innocent --
the same preset at cutoff 21198 in LPF renders fine.

**This cannot be a synthesis problem.** The filter is analog and sits after
the DAC, so the firmware only *writes* the mode change; nothing about BPF
changes a sample the engine computes. The suspicion is therefore the control
path -- most likely a write to the I2C codec that the peripheral model does
not acknowledge the way the firmware waits for, leaving it spinning. The
codec already logs `I2C WRITE` traffic through boot.

Reproducing costs one preset and 75 seconds, so this is a cheap bug to chase:
watchpoint the I2C peripheral, walk the stack from the hook the way the
wavetable fetch was found, and read the function that spins.

**Hardware is unaffected.** All three modes were measured at length during the
filter calibration, and the cutoff curve was verified in every one of them.
Anything generating presets for this emulator should therefore treat BPF as
an *emulator* limitation to be excluded by default and re-enabled for presets
headed to real hardware, rather than pretending the instrument cannot do it.

### FIXED 2026-09-12: slow TIM8's modelled clock, and BPF renders

Three ways out, and the least invasive is the right one.

**Suppress TIM8's compare interrupts.** Rejected. The ISR does not only toggle
the filter pin: its CC1 block drives a state machine (`FUN_08027D80`) that
calls three further functions, and stopping the interrupt silently stops all of
it. Buying speed by skipping work whose consequences are unknown is how a
faithful emulator becomes an unfaithful one.

**Patch the firmware.** Rejected, though it is tempting -- `FUN_0802AFEC` is
literally `return 0x14`, so widening that constant is a two-byte edit that cuts
the interrupt rate by any factor wanted. But the whole value of this rehosting
is that it runs the *unmodified* image; a patch has to be re-derived for every
firmware version, and it edits the object under study. Kept as a fallback.

**Lower TIM8's modelled clock.** Taken. `timer8 Frequency 100000` is settable
at runtime, so it needs no platform edit and no patch. Every instruction still
executes, in the same order, with the same values; only the interval between
soft-PWM ticks changes, and the GPIO that tick drives is the analog filter's
control line, which is outside the emulated domain and modelled by
`filter_model.py`.

There is also an honesty argument. TIM8 was **already** wrong: the platform
declares 10 MHz and the RCC the firmware programs implies 90-180 MHz. This
does not introduce a deviation, it resizes one that was already there.

**The control.** The same LPF preset rendered at 10 MHz and at 100 kHz is
**byte-identical** -- sha1 `6d57eea215b9233d` both -- so TIM8's rate has no
influence on the audio path whatsoever. At 100 kHz a two-preset run including
BPF finished in 121 s; at 10 MHz the same run hit a 300 s cap with BPF never
completing.

BPF and LPF pre-filter renders are *not* byte-identical to each other, but
agree to 0.003% of RMS (2952.3 against 2952.4). That is the documented
batch-position effect -- these presets inherit the Init's free-running cycling
envelope and BPF rendered second -- not an influence of `filter_type` on the
digital path, which there cannot be.

`mfrender.py --timer8-hz` carries the default and the reasoning; pass
`10000000` to restore the platform's value and watch BPF stop rendering.
BPF no longer needs excluding.
