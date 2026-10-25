# The MicroFreak, in one page

What the instrument is made of, what runs where, and how a key press becomes a
sound. Read this before the address tables; everything else in `docs/` assumes
this picture.

Firmware **5.0.0.2084** throughout. Where a number here is a claim rather than
a definition, `FIRMWARE.md` has it with its confidence mark and how it was
established.

## Four processors, not one

```
                    ┌───────────────────────── the main chip ──────────────────┐
   MIDI DIN  ──────▶│ USART1  31250                                            │
                    │                                                          │
   USB  ───────────▶│ OTG_FS          STM32F446  Cortex-M4F  180 MHz           │
   (MIDI + DFU)     │                 512 KB internal flash                    │
                    │                 SRAM from 0x20000000, initial SP         │
                    │                 0x20020000                               │
                    │                                                          │
                    │ QUADSPI ───────▶ 16 MB serial NOR: presets, samples,     │
                    │                  wavetables                              │
                    │                                                          │
                    │ SAI2 block A ──▶ DMA2 stream 4 ──▶ codec (I2C 0x18)      │
                    │ SAI2 block B ◀── DMA2 stream 6 ◀── microphone in         │
                    │                                                          │
                    │ USART2  460800, multi-drop half-duplex                   │
                    └──────────────────────┬───────────────────────────────────┘
                                           │  one bus, three peers
                        ┌──────────────────┼──────────────────┐
                        ▼                  ▼                  ▼
                  keyboard chip      matrix chip         OLED chip
```

The three co-processors are separate microcontrollers with their own firmware
images, shipped in the same update package. They scan the keyboard, scan the
touch matrix and drive the display; the main chip does everything else.

**All three sit on one wire.** The main chip has exactly one port on the
co-processor bus — USART2, half-duplex at an intended 460800 baud — and the
three peers share it. That was established from the baud divisors rather than
assumed: 459,184 on the main chip and 461,538 on the co-processors are the same
intended rate reached from two different clocks. The main chip's USART3 is a
separate 115200 link whose purpose is not established.

The co-processors are **not** emulated. Nothing in the boot path waits on them,
which is why the harness gets to audio without them, and they are the leading
suspect whenever a timing-dependent result will not reproduce.

## Where the code lives

Internal flash is 512 KB in eight sectors, and the first three are not the
application:

| | | |
|---|---|---|
| `0x08000000`–`0x08007FFF` | sectors 0–1, 16 KB each | the **bootloader**, never shipped in an update |
| `0x08008000`–`0x0800BFFF` | sector 2, 16 KB | the **factory data page**: the serial, records for the five DFU images, six image-validity markers, a calibration ramp |
| `0x0800C000`– | sectors 3–7 | the **application** — this is `main.raw` |

Arturia's update package carries the application and the three co-processor
images. It does not carry the bootloader, so the only copy of that anywhere is
a dump of an instrument someone owns. `BOOTLOADER.md` covers what is in it;
`FIRMWARE_UPDATE.md` covers the protocol that writes the rest.

## What happens at power-on

1. The core takes its stack pointer and reset vector from the bootloader's
   vector table at `0x08000000`.
2. The bootloader checks **three gates**: a hardware forced-DFU pin (PB10),
   and the validity markers for images 0 and 1 in the factory data page. Any
   one failing drops it into its DFU loop instead of booting.
3. It hands over to the application at `0x0800C000`.
4. `SystemInit`, then clock setup, then `__libc_init_array` — **6.7 million
   instructions of C++ static construction**, which is most of the boot.
5. `main()` at `0x0802479C` configures RCC, GPIO and SAI, and enters its loop.

Under the harness that is 6,915,574 instructions to `main()`, and it reproduces
exactly: `tests/test_determinism.py` requires two cold boots to agree
instruction for instruction.

## How a key press becomes a sound

A note arrives over MIDI DIN, over USB, or from the keyboard chip on the
co-processor bus. The main chip's synthesis runs in software — there is no
sound chip — through **22 oscillator engines**, each a separate DSP
implementation sharing one engine object. The result goes out as 16-bit stereo
at **96 kHz**: the engine writes into a buffer, DMA2 stream 4 pushes it into
SAI2 block A's data register, and the codec turns it into analog.

Paraphony is four voices, so a dense passage drops notes the way the hardware
does.

**Then it passes through an analog filter that is not in the firmware at all.**
The filter sits after the DAC. No emulator can produce it, because there is
nothing to emulate — it is a state-variable filter in hardware, driven by a
control line the firmware wiggles. `src/mfemu/filter_model.py` is a measured
model of it, fitted to recordings of a real instrument, and every render goes
through it at the preset's resonance and at the cutoff the firmware writes to
that control line -- a 16-bit PWM on TIM3, captured per sample, so envelopes
and LFOs routed to cutoff are in the render. It is the one part of the
signal path that is an approximation rather than the firmware running, which is
why `mfemu render --no-filter` exists.

Audio also comes **in**: SAI2 block B on DMA2 stream 6, for the microphone and
the vocoder.

## Where a preset lives

Presets are in the external flash, not in the firmware. Slot *n* is a
**4096-byte page** at `(n + 129) × 4096`, and there are 512 slots in four banks
of 128. A page has three regions, and they are not interchangeable:

| region | offset | size | what |
|---|---|---|---|
| 0 | 0 | 32 | the name record and metadata, including the schema byte |
| 1 | 32 | 1980 | the parameter stream, a TLV format |
| 2 | 2012 | 2084 | sequencer and matrix data |

Region 0 arrives over the wire as a separate message from regions 1 and 2, and
a page whose region 0 is erased is read as an empty slot however valid the
parameters are. `FORMATS.md` has the byte-level detail; that asymmetry is the
single most common way to write a preset the instrument then ignores.

Samples and wavetables live in the same flash: samples as a chain of records
from `0x281000`, wavetables in two copies — a factory one the restore path
reads and a live one the `Wavetable` engine plays.

## What the emulator stands in for

Renode supplies the Cortex-M4, the buses, the timers and the NVIC. Six things
it does not supply are this repository's own models:

| | |
|---|---|
| `STM32_OTGFS.cs` | USB in device mode, plus a host that enumerates it and can issue arbitrary control transfers |
| `STM32F4DMA.cs` | DMA with circular mode and peripheral requests |
| `STM32_QUADSPI.cs` | the QUADSPI controller and the NOR flash behind it |
| `STM32F4_Flash.cs` | the internal flash controller, including the programming Renode's own model omits |
| `STM32_SAI.cs` | SAI2 block A, and the tap a render reads audio from |
| `MicroFreakCodec.cs` | a logging I2C slave standing in for the codec |

None of them is MicroFreak-specific. `EMULATION.md` says what each one had to
do and how faithful it is.

## Where to go next

| you want | read |
|---|---|
| to run it | the README, then `EMULATION.md` |
| an address, or what some function does | `FIRMWARE.md` |
| a file or wire format | `FORMATS.md` |
| the Speech engine and its LPC vocabulary | `SPEECH.md` |
| the bootloader, or how an update works | `BOOTLOADER.md`, `FIRMWARE_UPDATE.md` |
| to know how sure any of this is | `METHOD.md` — read it before investigating anything |
| what other people have published | `RELATED_PROJECTS.md` |
