# microfreak-emu

The Arturia MicroFreak's firmware (5.0.0.2084) running under
[Renode](https://renode.io/), and documentation of what is in it.

Not affiliated with, authorised by or endorsed by Arturia. **No Arturia code,
firmware or binaries are in this repository.** The emulator takes the path to
an image you already own, and `.gitignore` refuses those file types.

This is the research half of a pair. The user-facing tool is
**[microfreak-tools](https://github.com/gbryant/microfreak-tools)**: presets,
samples, wavetables, speech vocabulary and firmware flashing. This repository
answers the questions that tool relies on.

## What the emulator is for

A question like "does this opcode have a handler" should not cost a flash
cycle. Under emulation it is a breakpoint. In practice that gives three things:

- **Checked claims.** `tests/test_encoder_roundtrip.py` has the emulated
  firmware decode Arturia's own speech bank and compares it with an
  independent decode: 926 frames, 22 words, byte-identical.
- **Differential runs.** Send one MIDI message twice with one byte changed,
  and diff all of SRAM.
- **Audio.** Presets render to `.wav`, including a model of the analog filter
  that sits after the DAC.

## Documentation

`docs/` is the main product of this repository.

| | |
|---|---|
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) | **Start here.** The instrument in one page: four processors, what runs where, how a key press becomes a sound |
| [EMULATION.md](docs/EMULATION.md) | The emulator: what it models, how to run it, how faithful each part is |
| [FIRMWARE.md](docs/FIRMWARE.md) | The firmware: addresses with a confidence mark, and what the code does |
| [FORMATS.md](docs/FORMATS.md) | The bytes at rest: flash layout, presets, the parameter stream, the mod matrix, samples, bank files |
| [PROTOCOL.md](docs/PROTOCOL.md) | The bytes on the wire: the SysEx frame, every opcode, reading and writing presets |
| [SPEECH.md](docs/SPEECH.md) | The Speech engine: Plaits LPC, lifted whole, data included |
| [BOOTLOADER.md](docs/BOOTLOADER.md) | The low 48 KB, its three gates, and the recovery combination |
| [FIRMWARE_UPDATE.md](docs/FIRMWARE_UPDATE.md) | The update protocol: USB DFU with Arturia's own class requests |
| [METHOD.md](docs/METHOD.md) | How to investigate this without wasting days, including the retractions |
| [HARDWARE_TESTS.md](docs/HARDWARE_TESTS.md) | Claims only the device can settle, and what each outcome would mean |
| [RELATED_PROJECTS.md](docs/RELATED_PROJECTS.md) | Other people's public work, and where it agrees with this |
| [PRESET_SOURCES.md](docs/PRESET_SOURCES.md) | Where preset data comes from: public banks, the factory set, CC/NRPN |

The preset format and sample encoding are documented in microfreak-tools:
[PRESET_FORMAT.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/PRESET_FORMAT.md)
and [SAMPLE_ENCODING.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/SAMPLE_ENCODING.md).

Every document marks what was observed and what was inferred. Treat an address
with no stated evidence as a lead, not a fact. [METHOD.md](docs/METHOD.md)
explains the rule behind that: a cause with no control run is a guess.

## Setup

Install Renode from [renode.io](https://renode.io/) or with
`brew install renode`. It is an application, not a Python package.

Clone both repositories side by side. This one uses microfreak-tools from the
sibling checkout, so its tests run against that code, and `uv sync` fails
without it.

    git clone https://github.com/gbryant/microfreak-tools
    git clone https://github.com/gbryant/microfreak-emu
    cd microfreak-emu
    uv sync

Put your firmware in `data/firmware/`. The `.mff` works as downloaded:

    cp ~/Downloads/MicroFreak_Firmware_Update_5_0_0_2084.mff data/firmware/

Then check the setup and boot it:

    uv run mfemu check            # about a second, no emulation
    uv run mfemu boot             # boot and report what happened

## Presets, samples and wavetables

The firmware file contains no presets, so you supply them. There are two
ways to get a preset export (`.mfprojz`):

    # from MIDI Control Center's installed factory files
    uv run mfreak asset import --outdir data --by-kind

    # from your own instrument, with no Arturia software
    uv run mfreak preset backup data/presets/mine.mfprojz

`asset import` is also the only source of the factory samples. Without
samples, presets on the Sample and Grains engines render silent; that is 221
of the 384 in Factory6.0. Every render prints which samples and wavetables it
used. The factory wavetables come from the firmware file if
`data/wavetables/` is empty.

Each `data/` folder uses its export only when it holds exactly one.
`asset import` writes four preset exports, so move all but one into a
subfolder, which is never read:

    mkdir -p data/presets/disabled
    mv data/presets/Factory3.0.mfprojz data/presets/New\ Presets\ [45].0.mfprojz \
       data/presets/disabled/

## Rendering

    uv run mfemu render --list --grep bass
    uv run mfemu render --preset 319 --midi ode_to_joy --out out.wav
    uv run mfemu render --preset Buzzsaw --notes 60,67 --out buzz.wav

Presets are numbered as on the instrument, 1-512. The first render takes about
a minute, because synthesis is not running until about 14 emulated seconds
after boot. It then saves a snapshot, and later renders of the same export
restore it in about a second. A restored render is byte-identical to a cold
one. `mfemu warm --list` shows the snapshots and `--clear` deletes them.

Each render reports what the filter did:

    Buzzsaw  preset 319  2.00 s  a.wav  LPF fc=236Hz Q=0.93
      DC +0.053 -> +0.001 (the model is AC-coupled, as the output is)

The filter is a model of the analog stage, set from each preset's own filter
settings and the cutoff the firmware computes. It was calibrated on the
instrument; see `filter_cal/README.md`. A render warns when a preset's cutoff
falls outside the calibrated range. `--no-filter` writes the DAC signal
instead. The model also runs on its own:

    uv run python -m mfemu.filter_model in.wav out.wav --type LPF --cutoff 60 --resonance 20

`--cutoff` and `--resonance` are knob positions, 0-100.

To record the same thing from the instrument, `mfreak render` takes the same
flags and records through an audio interface:

    uv run mfreak render --preset 319 --midi data/midi/ode_to_joy.mid --out hw.wav

## Commands

| | |
|---|---|
| `mfemu check` | is this machine set up, and is the firmware the right build |
| `mfemu paths` | where everything resolves to, and what is missing |
| `mfemu image` | what is inside a `.mff`, headered `.bin` or `.raw` |
| `mfemu boot` | boot the firmware and report what happened |
| `mfemu render` | render presets to `.wav` from a MIDI file or notes |
| `mfemu flash` | build an image of the external SPI flash |
| `mfemu dfu` | run a firmware update against the emulator |
| `mfemu serve` | boot and keep running, for microfreak-tools to talk to |
| `mfemu panel` | press buttons, turn the encoder and read the display of a running `serve` |
| `mfemu warm` | manage the snapshots renders start from |

`mfemu <command> --help` shows each one's options. `boot`, `render` and `dfu`
take `--firmware FILE` to use another image. To try someone else's build:

    uv run mfemu check --firmware theirs.mff    # what it is
    uv run mfemu boot  --firmware theirs.mff    # does it boot

The addresses in `docs/` are for 5.0.0.2084 and may not fit another build.

## Driving it with microfreak-tools

The emulator answers the instrument's own SysEx, so `mfreak` works against it
unchanged:

    uv run mfemu serve --preset 8=Speech             # leave this running
    MFREAK_EMULATOR=1 uv run mfreak preset read 8    # in another shell

Run `mfreak` from this checkout, which has both packages installed. A preset
read takes about 2 seconds; a write with verification takes about 17.

## Emulating a firmware update

By default the emulator starts the application directly, as the bootloader
would. With a dump of your own bootloader it boots through the real one, and
`mfemu dfu` can run a whole update: reboot to DFU, erase, download and finish.

Arturia ship only the application, so the bootloader has to come from your
instrument. `mfreak peek dump-bootloader` reads it; its help gives the steps.
The file contains your serial number, so keep it to yourself.

    cp /path/to/your/bootloader.bin data/bootloader/
    uv run mfemu dfu --image update.mff --finish

`docs/FIRMWARE_UPDATE.md` has the details.

## The data folder

`data/` sits beside the code. Each subfolder is a drop folder: put a file in
and it is used.

    data/firmware/    Arturia's update: a .mff, a headered .bin or a payload
    data/bootloader/  optional: your own bootloader dump, for updates
    data/presets/     optional: an MCC preset export (.mfprojz)
    data/samples/     optional: an MCC sample export (.mfsbz)
    data/wavetables/  optional: an MCC wavetable export (.mfwbz)
    data/midi/        optional: MIDI files, found by bare name

`--firmware`, `--bootloader`, `--source`, `--samples` and `--wavetables`
override these for one command. `MF_RENODE` and `MF_GHIDRA` say where Renode
and Ghidra are installed, if not in the usual places. To keep the data
elsewhere, make `data` a symlink.

`data/` is not tracked by git. A fresh clone has none of it, and
`git clean -xdf` deletes it. Back it up.

## Tests

Tests are plain scripts. Each prints what it checked and exits non-zero on
failure. Run them in this order on a new machine; the first three take about a
second and need no boot.

    uv run python tests/test_cli_surface.py       # needs nothing
    uv run python tests/test_preflight.py         # checks the firmware hash
    uv run python tests/test_filter_model.py      # no firmware needed
    uv run python tests/test_boot.py              # boots it, with a control
    uv run python tests/test_peripherals.py       # the peripheral models
    uv run python tests/test_determinism.py       # boots twice, compares
    uv run python tests/test_encoder_roundtrip.py # needs a preset
    uv run python tests/test_warm_snapshot.py     # warm render == cold render
    uv run python tests/test_panel.py             # the front panel saves an Init
    uv run python tests/test_dfu_handoff.py       # app to bootloader, over USB
    uv run python tests/test_dfu_download.py      # a full update

They test the emulator on Arturia's unmodified firmware, and each has a
control that can fail. Optional settings for them:

    MF_TEST_PRESET=..   which preset test_warm_snapshot renders (default 6)
    MF_TEST_SOURCE=..   which preset export it renders from

## Layout

    docs/              the firmware documentation
    src/mfemu/         the emulator, importable as `mfemu`
    tests/             test scripts
    renode/            machine description, peripheral models, scripts
    filter_cal/        the rig that measured the analog filter
    ghidra/            headless disassembly and symbol scripts

The peripheral models in `renode/` are not MicroFreak-specific:
`STM32_SAI.cs`, `STM32_QUADSPI.cs`, `STM32_OTGFS.cs`, `STM32F4DMA.cs`,
`STM32F4_Flash.cs` and `MicroFreakCodec.cs`. They should make the next STM32F4
instrument much quicker to emulate.

## Licence

Apache-2.0 for this project's own code; see `LICENSE` and `NOTICE`. The
documentation describes Arturia's firmware without reproducing it: addresses,
layouts and behaviour, not code.
