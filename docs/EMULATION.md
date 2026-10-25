# Emulating the MicroFreak

The instrument's own firmware, unmodified, running on your computer under
[Renode](https://renode.io/): booting through its real bootloader, loading
presets out of emulated flash, rendering audio, answering SysEx over USB, and
taking a firmware update.

This file is the harness — what it models, how faithful each part is, and where
it provably is not. `FIRMWARE.md` is what the firmware does;
`ARCHITECTURE.md` is the one-page picture of the device.
`../renode/NOTES.md` is the working notebook behind this file: chronological,
longer, and including the retractions.

## What it does today

| | |
|---|---|
| boots the stock 5.0.0.2084 image to `main()` | with no stubs, in 6,915,574 instructions |
| boots every official release, 1.0.5 to 5.0.0 | each image's milestones are found from its own reset handler |
| takes front-panel input | buttons, the encoders and knobs, through `mfemu panel` |
| shows what the display shows | as text, read from the bus to the display co-processor |
| boots through the real bootloader | all three gates, if you supply a dump of your own |
| renders audio | a note comes out as a note: C4 at 260.9 Hz, 0 cents |
| loads presets from emulated flash | by Program Change, several in one boot |
| answers SysEx | over DIN and over USB, both directions |
| takes a firmware update | `SELECT_TARGET` erases and `DFU_DNLOAD` writes, for real |
| replays exactly | two cold boots agree instruction for instruction |

What it cannot do is the analog filter, which is after the DAC and therefore
not in the firmware at all. `src/mfemu/filter_model.py` is a measured model of
it, applied to every render — the one part of the signal path that is an
approximation rather than the firmware running. Its CONTROL is emulated,
though: the firmware's cutoff CV (TIM3 CCR4/CCR3, knob plus modulation) is
captured during the render and drives the model sample by sample.

## Running it

    uv run mfemu check          # is the rig complete? ~1 s, no emulation
    uv run mfemu boot           # boot it and report what happened
    uv run mfemu render --preset 71 --midi phrase.mid --out out.wav

The firmware is yours to supply, as a `.mff` package or any image inside one.
See the README for installing Renode and where to drop your firmware.

## The front panel

The panel's buttons, encoders and knobs are read by co-processors the
emulator does not run. `mfemu panel` stands in for them on the bus they
share with the main chip, and reads the display's text off the same bus:

    uv run mfemu serve                        # leave this running
    uv run mfemu panel screen
    uv run mfemu panel press Utility turn Preset 1 press PresetClick
    uv run mfemu panel memoff press Save

A boot with an erased settings page has Mem protect set to "Factory only",
which refuses Save on presets 1-128; `memoff` turns it off through the
menus. Saving from an empty slot writes the firmware's own Init patch, which
is how `tests/test_panel.py` checks the panel: the result is byte-identical
to the Init a real instrument saved.

Knob messages move the setting, but the value-to-setting mapping is not
worked out, so a knob does not reach a predictable value. The bus protocol is
in FIRMWARE.md, "The front-panel bus".

## The peripheral models

Renode's STM32F4 platform does not ship the peripherals this firmware needs,
and the one it does ship for flash is missing the half that matters. These six
models are this repository's own work, and they are the generically useful part
of it — none is MicroFreak-specific.

| file | what it is | lines |
|---|---|---|
| `STM32_OTGFS.cs` | OTG_FS in device mode, plus a minimal USB host that enumerates it and can issue arbitrary control transfers | 1205 |
| `STM32F4DMA.cs` | DMA with circular mode and peripheral requests, which Renode's own model has neither of | 784 |
| `STM32_QUADSPI.cs` | the QUADSPI controller and the serial NOR behind it, derived from a boot trace rather than the reference manual | 557 |
| `STM32F4_Flash.cs` | the embedded flash controller — unlock, sector erase, and programming that reaches the memory | 358 |
| `STM32_SAI.cs` | SAI2 block A, enough to carry the audio out, with the capture tap a render reads | 276 |
| `MicroFreakCodec.cs` | a logging I2C slave standing in for the codec at 0x18 | 82 |

Each file's header says why it exists and what it cost to get right; those
comments are the real documentation and are kept with the code deliberately.
Two are worth knowing about before you trust a result:

**`STM32F4_Flash.cs` replaces a model that ships with Renode.**
`MTD.STM32F4_FlashController` handles the unlock sequence and then refuses
every program, because `FLASH_CR.PG` is unimplemented — and it announces that
on each attempt while changing nothing. Any firmware that rewrites internal
flash therefore appeared to succeed and did not. A firmware update is nothing
but that.

**`STM32F4DMA.cs` carries a workaround, not a model.** A completed
memory-to-peripheral stream stays armed and wraps rather than disarming. That
is how the audio path works at all, and it is a deliberate deviation from the
hardware pending an explanation of the re-arm gap — the file says so where it
does it.

## Which harness

**Renode.** It boots the firmware, models the time base correctly, delivers
MIDI properly, loads presets from emulated flash, and renders a note: after
three DMA/SAI model fixes (2026-09-06) note 60 comes out as a C4 at 260.9 Hz,
with a silent no-note control and a silent unfixed control. A render takes
80-110 s of wall clock, down from 25-35 min after two platform fixes (PWM
timer clocks, and a memory region standing in for the unmodelled USB core),
and down again to roughly 12 s once a warm snapshot exists -- see below.

The firmware knowledge in `FIRMWARE.md`, `FORMATS.md` and `PROTOCOL.md` is
harness-independent: it describes the instrument, not this emulator, and would
still be true if the harness were thrown away.

## What a render actually costs

Measured 2026-10-15, on this machine, by timing `RunFor "2"` against
`RunFor "20"` so the shared boot cancels out of the difference:

| | |
|---|---|
| steady state | **3.45 wall-seconds per emulated second** |
| fixed Renode cost | ~3.6 s (launch, compiling the C# models, loading firmware) |
| SystemInit to `main()` | **0.053 s** -- 6,915,574 instructions, about 131 MIPS |
| 20 emulated seconds | 3,600,000,000 instructions, exactly 180 M per second |

Two things in that table are worth absorbing before trying to make it faster.

**The boot is not the cost.** Reaching `main()` is 53 milliseconds of wall
clock, 0.07% of a 72-second run. The 6.9 million instructions of C++ static
construction sound expensive and are nearly free, because they are
straight-line integer work that the translation cache eats.

**The instruction count is pinned by fidelity.** `PerformanceInMips` is 180,
matching the real 180 MHz part, and the CPU really does execute 180 million
instructions per emulated second -- nothing is skipped or idled past. The only
way to reduce that number is to lie about the clock, and the firmware's audio
deadlines are relative to it. Steady state runs at 51.8 MIPS against the
boot's 131, so the audio code costs about 2.5x more host work per instruction;
that is almost certainly softfloat VFP emulation of the DSP, inside Renode's
translation layer rather than anything here.

Things measured and found NOT to help: a larger global quantum (66.6 s against
a 69.5-72.9 s baseline -- inside the noise), and suppressing log output or the
milestone hooks (no difference at all; three hooks firing once each are
invisible against 70 s of execution).

## Driving the machine over the Monitor

The protocol path -- `serve`, and anything in microfreak-tools pointed at it --
was slow for a reason that had nothing to do with emulation. A 146-chunk
preset read took **60 s**; it now takes **2 s**.

`_drain_monitor` waited 0.4 s of wall clock for the Monitor to fall silent
after every command, and a preset read issues about 150 of them, one per
`usbfs Send`. Renode prints a prompt when a command is done, so there was
never anything to wait for. Reading to the prompt is the whole fix.

Worth recording how nearly this was missed, because the reasoning was
seductive and wrong. The suspicion was that the machine free-running while the
client polled was the waste, and the measurement supported it: a round trip
needs **9 ms** of emulated time (reproducible to the millisecond), while a
free-running machine spent about **240 ms** of emulated time on each one
synthesising audio nobody wanted -- 96% waste, and an instrument never sees
such gaps. So a stepped mode was built, pausing the machine between messages.
Both figures are real. Neither was the bottleneck:

| | 146-chunk preset read |
|---|---|
| free-running, 0.4 s quiet-wait per command | 60 s |
| **free-running, reading to the prompt** | **2 s** |
| stepped, reading to the prompt | 6 s |

Stepping is *slower*, because it adds a Monitor round trip per exchange. It
was built, measured, and then **removed**, and is not worth salvaging.
What killed it was not only the 3x: a control test read
the same preset both ways and got byte-identical payloads, so the one thing it
was argued to improve -- pacing the firmware the way real hardware does -- is
not observable. What remained was an idle machine that does not advance, which
a `pause` on idle would give for three lines instead of a second code path in
`recv` and a `timeout` whose units changed with the mode.

The lesson is the one this project keeps relearning: a plausible mechanism
with real numbers behind it is still a correlation until something rules out
the alternatives. Both measurements were sound and the conclusion drawn from
them was wrong, because nobody had measured the Monitor.

### Conditions, not durations

The same mistake twice in one file is worth naming. `attach_usb` slept a flat
`time.sleep(25)` after plugging the emulated cable in, which was 25 s of
`serve`'s 32 s startup. The model reports enumeration through `HostState`, so
there was a condition to test rather than a duration to guess:

    HostState reaches Done        0.07 s after `usbfs Attach`
    first SysEx answered          0.02 s after Done, on the FIRST attempt
    serve startup                 32.4 s -> 6.4, 7.0, 6.9 s over three runs

The 0.02 s was the figure worth measuring. Replacing a sleep with a condition
is only safe if the condition is not weaker than the wait -- "the host
finished enumerating" need not mean "the device will answer" -- so requests
were hammered from the instant `Done` appeared, and the first one succeeded.

Both this and the Monitor's quiet-wait were fixed wall-clock waits standing in
for states the harness could already see. Neither was slow because emulation
is slow.

`dfu` never goes through this path at all: it drives control transfers with
Monitor commands and polls `usbfs ControlDone`, so stepping never engaged
there even while it existed. It does benefit from the prompt fix, like
everything else that talks to the Monitor.

## Warm snapshots

What does cost is what the firmware does after `main()`: synthesis is up at
about 11.5 emulated seconds and the harness waits 14 s to be sure of it, which
is ~48 s of wall clock at 3.45x, on every invocation. So `mfemu render` pays it
once and saves the machine:

    cold boot, then a 2 s note        64 s
    warm restore, same note           12 s      byte-identical audio
                                                (12.36 and 12.78 s, 2026-10-19)

`mfemu warm` manages the cache (`--list`, `--clear`, `--rebuild`);
`render --no-warm` forces the full boot. `tests/test_warm_snapshot.py` renders
one preset both ways and requires the files to match exactly.

**This only works because Renode's compiler cache is on.** Renode compiles
each `include *.cs` into a dynamic assembly named after a fresh GUID and
records that name in the snapshot:

    STM32_QUADSPI, b41f99a7-8e12-4283-a1d3-47f5b331da7a.dll

A second process mints different GUIDs, so the load fails with *"This snapshot
is incompatible or the emulation's state is corrupted"* while printing two
identical version strings -- a thoroughly misleading way to say "I cannot find
your types". With `compiler-cache-enabled = True` the assembly is cached and
the GUID is stable, so the snapshot loads. `paths.renode_config()` writes that
setting into a config of our own and every Renode invocation passes
`--config`; we do not edit the user's Renode settings. A stock Renode platform
snapshots fine without any of this, which is what pinned the cause on our
dynamically compiled models rather than on Renode or on macOS.

**The external flash is part of the snapshot's key**, and this was learned the
hard way. Warming on an empty flash and swapping an image in afterwards looked
perfect on a Speech vehicle -- byte-identical audio -- and then rendered the
factory preset 'Casually' differently: 99.4% of samples changed, a difference
11.7 dB below the signal. Start-up reads samples and wavetables out of that
flash, so a machine warmed without them restores into a state the firmware
never would have reached. Warming with the image makes it byte-identical
again. One agreeable preset is not a control.

## Why

To get the instrument's own sound without the instrument -- so that presets can
be rendered in bulk, firmware changes can be tested before they touch hardware,
and anything the firmware does can be inspected while it does it. Doing that on
the device is slow and wearing: every preset write is 146 lock-step-acked flash
chunks onto one flash page.

Two ways to get audio without the device:

1. **Reimplement the synth.** 22 oscillator engines, each a separate DSP
   reverse-engineering job from ARM assembly. Months, and every engine is a new
   chance to diverge from hardware.
2. **Run the real firmware under emulation.** The engines come out *bit-exact*
   because it is the actual code. Only the peripherals need faking.

(2) is strictly better here, and most strongly so for any automated process
that measures renders and acts on what it measures. Such a process will find
and exploit an approximation's errors, and the better it gets at its own job
the worse that becomes. Rehosting removes that failure mode for everything
except the analog filter, which sits after the DAC and is modelled.

## The image

Get the payload (the distributed `.bin` has a 64-byte Arturia header). You do
not have to: every command takes the container directly and unpacks it in
memory. To do it explicitly:

    mfemu image MicroFreak_Firmware_Update_5_0_0_2084.mff            # report
    mfemu image MicroFreak_Firmware_Update_5_0_0_2084.mff --extract  # write it

A `.mff` container, a headered `.bin` and a bare `.raw` are all accepted, here
and by `--firmware` on `check`, `boot` and `render`.

| | |
|---|---|
| file | `nanowave_main__fw5_0_0_2084__2023_05_30.bin` -> `payloads/main.raw` |
| architecture | ARM Cortex-M4, little endian (Ghidra: `ARM:LE:32:Cortex`) |
| load address | **`0x0800C000`** (verified — derived from the image, not assumed) |
| size | `0x73A5F` |
| initial SP | `0x20020000` (128 KB SRAM) |
| reset vector | `0x0805F849` |
| vector table | 114 entries at the image base, 98 IRQs |
| `.data` | `0x20000000`–`0x200002F4`, loaded from `_sidata` = `0x0807F75C` |
| `.bss` | `0x200002F8`–`0x2001F230` |

The three I/O co-processors are separate STM32s, all loading at `0x08000000`:
`iochip_oled` (`0x13EEF`), `iochip_kbd` (`0x68EF`), `iochip_matrix` (`0x73EF`).

`MicroFreakVectors.py` (in the firmware bundle dir) sets up a Ghidra project:
names all 114 handlers, creates SRAM/PERIPH/PPB blocks, derives the base from
the image so a wrong load address is caught rather than silently mis-analysed.

## Known code locations

From this repo's analysis — useful for breakpoints and sanity checks:

| address | what |
|---|---|
| `0x0804D270` | SysEx byte state machine (43 states) |
| `0x0804ACC8` | SysEx opcode dispatcher (`tbh` table at `0x0804AD20`) |
| `0x08054A68` | preset serializer — writes the TLV parameter stream |
| `0x0800EB10` | runtime parameter table init (names, MIDI CC map, defaults) |
| `0x08062478` | module tag table (`VCO VCF EG1 Kbd Arp LFO EG2 …`) |
| `0x08062778`–`0x08062FF4` | parameter names, enum tables, 22 engine names |
| `0x08054DC0` | write-protect predicate |
| `0x08046708` / `0x08046790` | storage read / write |

## Peripherals to stub

Counted by scanning the image for literal peripheral addresses. Treat the
counts as indicative — the scan has false positives (a cluster of `0x…F800`
values are Thumb instruction pairs read as words, not pointers). The named
blocks below are the trustworthy part.

| base | peripheral | refs | notes |
|---|---|---|---|
| `0x40023800` | **RCC** | 30 | clock setup; must report PLL lock or boot spins |
| `0x40020000`–`0x40021000` | GPIO A–E | ~60 | mostly writes; ignoring them is usually fine |
| `0x40023C00` | FLASH interface | 13 | wait states, latency |
| **`0x40015800`** | **SAI1** | 6 | **the audio output path** |
| `0x40026000` / `0x40026400` | DMA2 | 13 | almost certainly feeding SAI |
| `0x40012000` | ADC1 | 7 | knobs / CV inputs |
| `0x40003800` | SPI2 / I2S2 | 2 | |
| `0x40007400` | internal DAC | 2 | |
| `0xE000E000` | NVIC / SysTick / SCB | many | Renode models these already |

**SAI1 + DMA2 is the audio tap.** Emulate to the point where the firmware fills
a DMA buffer and hands it to SAI, then read the buffer. That is the exact
digital signal the real hardware sends to its DAC.

## External SPI flash

Presets and samples live on external flash, and its layout is fully mapped
(FORMATS.md). Back it with a file and the emulator loads presets by
**writing that file** — no SysEx, no MIDI, no wear.

| region | address |
|---|---|
| global settings | `0x080000`, 96 bytes; byte 0 is the MIDI receive channel |
| preset slot *n* | `(n + 129) * 4096`, 4096 bytes each |
| sample data | `0xCD0000 + record.offset`, via the RAM directory at `0x200028BC` |
| user wavetables | `(index + 0xF5F) << 12` |

The RAM directory is 128 records of 24 bytes at `0x200028BC` + 12: `+0` u32
ordering key, `+4` u32 flash offset, `+23` u8 status (`0xFF` = free).

## What emulation does and does not give you

**Exact:** all 22 oscillator engines, envelopes, LFO, mod matrix, the entire
digital signal path.

**Not emulated:** the analog filter. It sits *after* the DAC, so it is not in
the firmware at all. The harness captures pre-filter audio and models the filter
separately. That is one well-understood 12 dB state-variable topology rather
than 22 unknown algorithms — a much better position, but it does mean emulated
audio is not the finished MicroFreak sound.

**Differs from the hardware:** SysEx on 1.0.5. Sent 5.0's preset request
(`03 19`), the emulated 1.0.5 answers it. A real 1.0.5 (2026-10-21) stopped
answering SysEx until it was power-cycled, and changed its cutoff while the
requests were arriving. Do not take an emulated old firmware's SysEx replies
as what the instrument does.

## What this repo already gives you

- `microfreak/preset/tlv.py` — parse and *generate* preset parameter blocks, byte-exact
  (round-trips 398/398 real presets), so presets can be synthesised directly
  into the flash-backing file.
- `microfreak/preset/fields.py` — 33 named parameters with ranges, enum tables and the
  value model (`display = int24 * 100 / 2**23`).
- `FORMATS.md` and `PROTOCOL.md` — the storage layout, the wire protocol, and everything
  above with its evidence.

## Unloaded memory reads as a fill pattern, not as zeros

Measured 2026-10-15, and worth knowing before you believe a memory read.
Memory that nothing has loaded does **not** read as `0x00` or as erased
`0xFF`. It reads as a repeating ASCII pattern:

    0x08008000 = 0x33323130   '0123'
    0x08008004 = 0x37363534   '4567'
    0x08008008 = 0x42413938   '89AB'
    0x0800800C = 0x46454443   'CDEF'

That is Renode filling a `MappedMemory` region, present immediately after the
machine starts and before the firmware has run, and unchanged afterwards. It
is not firmware content and not device data.

Two ways this misleads:

* **It looks like data.** `0x08008000` is the factory data page, whose first
  field is the device serial -- sixteen ASCII digits. Read it without a
  bootloader loaded and you get sixteen ASCII characters that are not the
  serial. An earlier note in this project treated `0x33323130` as a non-zero
  word "from the firmware image", which it is not.
* **"Is this region erased?" cannot be answered by comparing against
  `0xFF`.** A test that checks for erasure has to compare against what the
  harness actually puts there, or load something first and check for change.
  `tests/test_dfu_download.py` does the latter: it erases through the
  firmware's own controller and then watches a word change.

There is no fix to make here -- it is Renode's choice and arguably a good one,
since a fill pattern is obvious in a dump where zeros are not. It just has to
be known.

## Fidelity: a measured case where the emulator's own version mattered


Moving to the Mac Studio brought Renode 1.17.0, where 1.16.1 had produced
every render before it. The platform file needed four
changes before it would load at all:

    I2C.STM32F4_I2C          -> I2C.STM32F1_I2C
    Miscellaneous.STM32F4_RNG -> Miscellaneous.STM32_RNG
    SPI.STM32SPI              -> now requires `series: STM32Series.F4`
    DMARecieve                -> DMAReceive   (Renode fixed their typo)

The macOS packaging also changed: up to 1.16 it was a mono application started
as `mono bin/Renode.exe`, and from 1.17 it is a native `renode` binary.
`mfrender.renode_command()` handles both.

**The samples differ; the sound appears not to.** Two *static* presets --
no free-running modulator, so nothing about batch position or modulator phase
can explain it -- rendered on 1.17 and compared against their earlier 1.16.1
renders:

    007-Superwave-00    0.36% of samples identical, max difference  4.4% FS
    008-VAnalog-00      0.04% of samples identical, max difference 36.6% FS

Sample-level identity is the wrong measure here. Aligning and analysing them:

    20-band log spectrum  mean band difference 0.12 dB / 0.38 dB, corr 0.999
    peak frequency        identical to the FFT bin in both
    fundamental           see the correction below -- NOT a real difference
    envelope              correlation 0.997 / 0.982
    overall RMS           within 0.1%
    waveform correlation  0.996 for 007 once aligned by its 10.3 ms offset

So 1.17 renders the same sound with a ~10-16 ms timing offset at the start,
which drifts the two waveforms out of phase and collapses sample-level
agreement while every spectral measure stays put.

**Correction, 2026-09-24: the "couple of cents sharp" was an artefact.** Both
presets measured for it carry heavy portamento -- 86% and 69% glide -- so
their pitch sweeps continuously through the note. Sampling a moving pitch at
a fixed time, when one render is offset by 10-16 ms, produces an apparent
tuning difference that is really the timing offset. Measured again on presets
whose pitch is genuinely steady, the two versions agree exactly:

    056-VAnalog-01, settled partial   1.16.1 784.330 Hz   1.17.0 784.330 Hz   0.00 cents

while on the gliding presets the same comparison gives 0.00, +1.97, -2.99 and
-124.80 cents depending only on which window is chosen. There is no
established pitch difference between the two Renode versions.

A preset with a free-running modulator was tried first and had to be
discarded as evidence: `000-ScanGrains-00` differed too, but such a preset
differs between any two renders by design, so it proves nothing. Comparing
renders at all requires a preset whose output is reproducible.

**What this costs.** Renders made here are not comparable with renders made
on 1.16.1. Anything that measures one render against another across that
boundary is invalid, however the comparison is dressed up; audio made entirely
on one version is unaffected.

**Which is right is unknown.** The emulator was validated against hardware on
1.16.1, and nothing has re-validated 1.17. The host architecture also changed
in the same move, so 1.17-versus-1.16.1 and x86_64-versus-arm64 are not
separated by this measurement. Deciding needs either a hardware comparison or
a 1.16.1 install on this machine, which would also mean reverting the platform
changes above.

### Validated against the hardware (2026-09-24)

The Init patch is the right test signal for this: Basic Waves, glide 0, filter
wide open, arp and sequencer off, and **no mod-matrix route with any depth**,
so the cycling envelope sitting in `Run` modulates nothing. It renders a clean
harmonic series -- 130.7, 261.4, 392.1, 653.6, 915.0, 1176.4 Hz.

Written to a device slot, played with the standard phrase and captured through
the audio interface, measured at the 653.6 Hz partial where a 0.714 Hz bin is
1.89 cents and interpolation resolves a fraction of that:

    hardware, three takes   653.697   653.390   653.454 Hz
    hardware mean           653.513 Hz   (takes spread 0.81 cents)
    Renode 1.17.0           653.624 Hz

    Renode 1.17 vs hardware  +0.29 cents -- inside the take-to-take spread

So 1.17 reproduces the instrument's tuning. Nothing here compares 1.16.1
against hardware directly, because 1.16.1 is not installed on this machine;
what is established is that the two versions agree with each other on steady
pitches, so this result carries to 1.16.1 by transitivity rather than by
measurement.

**Choosing the test signal matters more than the estimator.** Four attempts on
randomly generated presets failed for four different reasons -- heavy glide
making pitch history-dependent, peak-picking flipping between a fundamental
and its second harmonic, an f0 tracker pinning at its 2 kHz ceiling, and
spectra too inharmonic to have a well-defined fundamental at all. The best of
those presets put only 29% of its energy in the first six harmonics. A
deliberately simple patch removed all four problems at once.

## The working notebook

`../renode/NOTES.md` is where this was worked out: which model had to do what,
which measurements reproduced, and which were retracted. It is chronological
and 2,000 lines long, so it is not the place to start — but it is the place to
look when something here seems arbitrary, because the reason is almost always
in there with the run that established it.
