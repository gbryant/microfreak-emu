# The MicroFreak firmware, mapped

Everything known about the firmware of the Arturia MicroFreak, version
**5.0.0.2084**: where things are, what they do, and how sure we are of each
claim. The main image loads at `0x0800C000`.

Read this with `FORMATS.md`, which is the other half: this file is the code and
what it does when it runs, that one is the bytes it reads and writes.
`SPEECH.md` goes deeper on the one engine that needed it, `BOOTLOADER.md` and
`FIRMWARE_UPDATE.md` on the low 48 KB and the update protocol, and
`ARCHITECTURE.md` is the one-page picture to read before any of them.

**Nothing here was taken from anyone else's work.** See `RELATED_PROJECTS.md`
for the other public projects and where they independently agree.

## How to read this

**Be strict about the confidence column; a wrong name costs more than a
missing one.**

| Mark | Means |
|------|-------|
| `T`  | **Traced** — observed executing, arguments or effects read at runtime |
| `D`  | **Disassembled** — read the code, the name follows from what it does |
| `I`  | **Inferred** — from a call site, a literal pool or a vtable slot; not yet confirmed |

Names in `lower_snake` are ours. Names in `HAL_*`/`USBD_*` match ST's HAL,
where the code is recognisably ST's. Dates on a finding are when it was
established; where a claim came from a run rather than from reading, it says so.

See **`METHOD.md`** before starting a new investigation — which tools lie to
you, which measurements do not reproduce, and the order of operations that
actually works here.

### Correction to EMULATION.md

That doc said `main()` is "the third `bl` in the reset handler, around
`0x0804E428`". The address is wrong and so is the reasoning. The reset handler
at `0x0805F848` is:

    bl 0x0802EDAC      SystemInit
    .data copy / .bss zero
    bl 0x08025730      clock deinit
    bl 0x08061420      __libc_init_array
    bl 0x0802479C      main
    bx lr

`0x0804E428` is not any of them. The tempting read — that the three `bl`s are
`__libc_init_array` / `main` / `exit` — is also wrong: `0x08061420` *returns*
after 6.7M instructions, which is C++ static construction, and the function
that then configures RCC, GPIO and SAI is `0x0802479C`. **`main()` is
`0x0802479C`.**


## Addresses

### Entry points and system

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x0800C000` | D | image base | vector table; `VectorTableOffset` |
| `0x0802479C` | T | `main` | milestone hook in `boot.resc` |
| `0x0802EDAC` | T | `SystemInit` | |
| `0x08061420` | T | `__libc_init_array` | |
| `0x08024F00` | T | main-loop service block | mid-function; drains MIDI, see below |
| `0x08055ED8` | D | `millis` | reads `0x2001E1D8`, divides by 10 |
| `0x2001E1D8` | T | tick counter | **its scale changes during boot** -- see below |

### Quick reference: addresses and constants

Everything below was derived in this repo and is used by
the analysis. Firmware is `payloads/main.raw`, loaded at `0x0800C000`.

#### Code

| address | what |
|---|---|
| `0x0802479C` | `main()` -- the fourth `bl` in the reset handler |
| `0x08061420` | `__libc_init_array` |
| `0x08038DF8` | **audio render callback** -- the capture tap; object arrives in `r0`, and the `half` argument is inverted (`half=1` fills the *first* half) |
| `0x08036B38` | synthesis entry |
| `0x0802F04C` | HAL_SAI_DMAStop + HAL_SAI_Transmit_DMA |
| `0x0802F3BC` | SAI2 init |
| `0x08017F0A` | wavetable interpolation load -- faults with a null table base |
| `0x0801F100`, `0x0801F6BC` | the two code patches applied by `--patch` |

#### MIDI chain, in order

| address | what |
|---|---|
| `0x08058214` | USART1 ISR |
| `0x08058030` | ring push (`buf[head] = byte; head = (head+1) & 0xFF`) |
| `0x08047C6C` | ring drain |
| `0x080480A8` | MIDI byte state machine (`0x80` and `0x90` handled identically) |
| `0x080478A4` | note dispatch |
| `0x08048524` | voice |

#### Storage / presets

| address | what |
|---|---|
| `0x0807531C` | preset region offset table: `0, 32, 2012` (u16); region 2 length is the literal `0x824` |
| `0x200018B8` | the storage object -- **is** the 4096-byte page buffer |
| `FUN_08044FDC` | a bare `bx lr`; returns its argument unchanged |
| `FUN_08044FE0(obj, n)` | region *n*'s start offset |
| `FUN_0804500C(obj, n)` | region *n*'s length |
| `FUN_080454D4(obj, slot)` | read `(slot + 0x81) * 0x1000`, 0x1000 bytes |
| `FUN_08045238(obj, slot)` | erase + program the whole page back |
| `FUN_08054598` | reads the name from region 0 `+9`; a negative word at `+0` means "empty slot" |
| `0x0804AD20` | SysEx dispatch table, indexed by `opcode - 0x15` |

Preset slot *n* lives at `(n + 129) * 4096`. The page is region 0 (32 bytes,
name record = bytes `[12:44]` of the `23 52` SysEx reply), region 1 (1980
bytes, parameter TLV), region 2 (2084 bytes, sequencer).

#### RAM

| address | what |
|---|---|
| `0x200007E4` | MIDI receive channel gate; `0x7F` = omni. Reads `0xFF` (erased) after boot, so **nothing gets through until it is set** |
| `0x2001E1D8` | the tick counter -- `TARGETS["main"].tick_addr`; `FUN_08055ED8()` returns this `/ 10`. **Do not treat its rate as fixed**: the firmware reprograms SysTick during start-up, so the same counter advances at 10,000/s early (reload 18,000, a 100 us tick at 180 MHz) and at **112,500/s** once `main()` is running (SYST_RVR reads `0x63F`, reload 1,600). Measured 2026-10-16 as 112,538 ticks per virtual second against Renode's own clock, which is 112,500 within noise. Anything dividing this by a constant is right for one phase and wrong for the other -- `midilink.boot()` divided by 10,000 and so waited 1.24 s where it meant 14. It now asks the harness for virtual time instead |
| `0x2001E08C` | MIDI receive ring: head `+0x10`, tail `+0x14`, 256-byte buffer `+0x18` |
| `0x20004830` | audio object; its buffer is at `+0x16E72` = `0x2001B6A2` |
| `0x200028BC` | sample directory, 128 records of 24 bytes |

### Peripheral init order

Useful as a map. `first seen` is the instruction at which the block is first
touched.

    RCC #160199 · GPIOE #171326 · TIM13 #171442 · FLASH_IF #6875381
    PWR #6875744 · GPIOC/B/D/A #6876366-6876398 · SYSCFG #6876563
    EXTI #6876582 · DMA2 #6879429 · ADC1 #7728038 · DAC #7729842
    DMA1 #7730219 · I2C1 #8736029 · I2C3 #8736526 · SAI1 #8736841
    SAI2 #8783299 · SPI2/I2S2 #10752036 · TIM1/2/3/5/8/12/14 #10752105-10754255
    USART1 #10754924 · USART2 #10755462 · USART3 #10755957 · QUADSPI #11760067


## Boot and control flow

### Key-down to sound — traced

One execution trace from the note-on byte arriving to the DMA buffer being
written, 1,080,000 instructions, Knit (Speech) loaded. Instruction offsets are
from the start of the trace, so this is the real order, not an inferred one.

| Offset | Address | What |
|--------|---------|------|
| 804 | `0x08048524` | `midi_dispatch` — 15 times |
| 45,334 | `0x08034394` | oscillator virtual call — 18 |
| 45,335 | `0x0803099C` | `osc_render_wrapper` — 18 |
| 45,625 | `0x0805E554` | `speech_formant_caller` — 54 |
| 46,225 | `0x0805DA8C` | `speech_formant_generator` — 46 |
| 48,694 | `0x08030AA2` | mix writer, block-32 path — 18 |
| 49,707 | `0x08038F3C` | `audio_tap` — 576 |

Between dispatch and the oscillator, in order of first execution — the
note-on handling and voice allocation, **not yet named**, and the obvious next
thing to reverse:

```
0x08048736  0x08048740  0x08048782      dispatch port arms
0x08048580  0x080485C0  0x080487FC  0x08048600
0x08048B2A  0x0804A714  0x08048B40
0x0804A600  0x0804A662  0x0804A6F0  0x0804A680      subsystem A
0x08048918  0x08048640  0x08048B90
0x0804DA80  0x08049FE4  0x0804A00C
0x0804DAC2  0x0804DBE6  0x0804DB12  0x0804DB40
0x0804DB80  0x0804DDA6  0x0804DD24                  subsystem B
0x08048BC0  0x08048680  0x0800E4B0  0x08048E1E
0x08023750  0x08023804  0x08023790  0x0801BBD8
```

Reading this chain is what would settle the Speech question: the signal is
alive at `speech_formant_caller` (live per-formant floats) and dead by the mix
writer, so the loss is inside `speech_formant_generator` or in what configures
it during the block above.

### Interrupts

NVIC enable and pending state has to be **tracked, not stored**: `ICER` and
`ICPR` clear the bits `ISER` and `ISPR` set, so plain memory would record the
clear as a value rather than as an effect. That is a property of the registers
and applies to any model of them.

#### Sources: DMA and timers

An interrupt controller with nothing to raise an interrupt is inert, so two
minimal sources came with it.

**DMA (STM32F0).** On a write that enables a channel, the transfer is performed
immediately — the data really moves, honouring direction, increment flags and
transfer sizes — then `TCIF` is set and the channel's IRQ pended if `TCIE` is
on. Channels share lines on an F0 (CH1 → IRQ 9, CH2–3 → IRQ 10, CH4–7 → IRQ 11).
Instant completion is a simplification: real DMA is paced by the peripheral.
But the data movement and the interrupt are both real, which is all the driver
observes.

**Timers.** `TIMx_SR` `UIF` is driven from the instruction counter, and only
while `CEN` is set so a configured-but-stopped timer stays quiet.


## MIDI, SysEx and USB

### MIDI receive chain

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x08047B18` | T | `usb_midi_unpack(buf, len)` | cable must be 0; len rounded to /4; needs >=80 bytes free |
| `0x0807539C` | D | CIN length table | 16 bytes, USB-MIDI code index -> MIDI byte count |
| `0x2001DDE4` | T | -> USB MIDI ring | pointer to the ring at `0x2001DDE8` |
| `0x2001E08C` | D | DIN MIDI ring | header at `+0x10`/`+0x14`, data at `+0x18` |
| `0x08047CC4` | T | `usb_midi_ring_drain` | called from `0x08024F2A`; parser ctx `0x2001DD20` |
| `0x08047C6C` | T | `din_midi_ring_drain` | called from `0x08024F30`; parser ctx `0x2001DC5C` |
| `0x08047BE4` | D | `midi_parsers_init` | installs both parsers' callbacks |
| `0x08048090` | D | `midi_parser_init(ctx, cb1..cb4)` | |
| `0x080480A8` | T | `midi_parser_feed(ctx, byte)` | **hot** — also called with 0 as an idle tick |
| `0x080478A4` | T | `midi_channel_callback` | channel and system messages |
| `0x08047D4C` | D | `midi_sysex_callback` | |
| `0x080475C0` | D | `midi_realtime_callback` | |
| `0x080475F4` | D | `midi_realtime_callback_2` | |
| `0x08048524` | T | `midi_dispatch(state, msg, port, ...)` | port 1 = USB, 2 = DIN |
| `0x0804870A` | D | dispatch, DIN port arm | channel check then `[state+0x2DE]` |
| `0x08048722` | D | dispatch, USB port arm | channel check then `[state+0x2DD]` |
| `0x200000E4` | T | MIDI source tag | `0x00` USB, `0x80` DIN, `0xFF` idle; every callback bails on `0xFF` |
| `0x200007A8` | D | MIDI settings struct | |
| `0x200007E4` | T | receive channel gate | `= 0x200007A8+0x3C`; `0x7F` is omni. Reads `0xFF` under emulation, so this is poked |
| `0x20003778` | D | dispatch state | `+0x2DD` USB enable, `+0x2DE` DIN enable (both non-zero already) |

### SysEx

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x0804ACC8` | T | `sysex_op_dispatch(reply, msg)` | keyed on the **sub** byte, `sub - 0x15`, 73 entries |
| `0x0804AD20` | D | sub-opcode jump table | `tbh`; targets below |
| `0x0804B0B0` | D | `op_15` | |
| `0x0804AF60` | D | `op_16` | |
| `0x0804AF9E` | D | `op_17` | |
| `0x0804AFFC` | D | `op_18` | |
| `0x0804B018` | D | `op_19_name_read` | slot from msg bytes 8/9, max 512 |
| `0x0804B290` | T | `op_52_name_record_write` | branches on msg byte 10 |
| `0x0804B762` | T | `op_52`, byte10 == 0 arm | stores the record, then page write |
| `0x0804AF3C` | T | reply tail (success) | builds the `00 18` ack |
| `0x0804AE8A` | D | reply emit | fills the 10-byte reply and sends |
| `0x0804C718` | T | `sysex_send(port_obj, buf, n)` | port object `0x20003EF0` |
| `0x0804AE60` | D | dispatch bail | |

### USB device (OTG_FS)

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x080254C0` | T | `OTG_FS_IRQHandler` | IRQ 67, vector index 83 |
| `0x0801C9E0` | T | `HAL_PCD_IRQHandler(hpcd)` | `hpcd` = `0x2001EBF0` |
| `0x0801CE98` | D | RXFLVL OUT-data copy | reads FIFO into `ep->xfer_buff`, accumulates `xfer_count` |
| `0x0801CCA6` | D | OEPINT handler | XFRC -> `DataOutStageCallback` |
| `0x08057180` | T | `HAL_PCD_DataOutStageCallback` | Arturia's own, not the weak one; re-arms with buffer `0x2001EFEC`, 64 bytes |
| `0x0801D1A4` | D | `HAL_PCD_EP_GetRxCount` | returns `ep->xfer_count` (`+0x210`) |
| `0x0801D154` | I | `HAL_PCD_EP_Receive` | tail-called from the callback |
| `0x080763C0` | D | `USBD_ClassTypeDef` vtable | 14 slots; `Setup` is slot 2. Descriptor getters all return `0x080574B8` |
| `0x0805756C` | D | `usbd_class_setup(pdev, req)` | the DFU/vendor class dispatch; see FIRMWARE_UPDATE.md |
| `0x0807636C` | D | USB device descriptor | VID `0x1C75`, PID **`0x0601`**, bcdUSB `0x0200`, bcdDevice `0x0200`. The bootloader differs only in bcdUSB (`0x0201`); tell the modes apart by `bNumInterfaces` (3 vs 1) |
| `0x080763F8` | D | USB config descriptor FS | 119 bytes, 3 interfaces: DFU `FE/01/02`, AudioControl, MIDIStreaming |
| `0x08076470` | D | USB config descriptor other-speed | same 119 bytes, self-powered / 0 mA |
| `0x0807640A` | D | DFU functional descriptor | `bmAttributes 0x0B`, `wDetachTimeOut 255`, **`wTransferSize 64`**, `bcdDFUVersion 0x0110` |
| `0x2001E002` | D | `dfu_reboot_requested` | set to 1 by class request `0xF2`; the only thing that handler does |
| `0x080576A0` | D | `dfu_reboot_poll()` | if the flag is set: mark flash, 500 ms, clear, `SCB->AIRCR = SYSRESETREQ`, spin |
| `0x08045B58` | D | `mark_dfu_request()` | erases flash **sector 2** and rewrites the 512-byte record at `0x08008000` with `0xFFFF`/`0x5A`/`0xA5` at `+0xB0`/`+0xB4`/`+0xB5` |
| `0x0801B694` | D | `HAL_FLASH_Unlock()` | FLASH base `0x40023C00`; KEYR at `+4`, CR at `+0x10` |
| `0x0801B550` | D | `HAL_FLASH_Program(type, addr, lo, hi)` | `type 2` = word; busy-waits on SR BSY with a 50 s cap |
| `0x0801B72C` | D | `HAL_FLASHEx_Erase(init, err)` | `init` = `{TypeErase, Banks, Sector, NbSectors, VoltageRange}` |
| `0x0801B6BC` | D | `HAL_FLASH_Lock()` | |


## Audio

### Audio path

Verified by watching the DMA buffer backwards, which is the reliable direction.

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x2001B6A2` | T | SAI DMA buffer | DMA2 stream 4, 128 items, PAR `0x40015C20` |
| `0x2001B658` | T | mono mix buffer | |
| `0x08038F3C` | T | `audio_tap_mono_to_stereo` | `ldrsh [r5],#2` then `strh [r4],#4` twice |
| `0x080372F6` | T | mix writer (common) | runs for every preset |
| `0x08030AA2` | T | mix writer, block-32 voice path | the arm silent presets take |
| `0x0803261C` | T | mix writer, other voice path | the arm audible presets take |
| `0x2001C7B4` | T | engine idle flag | 0 = running, 1 = idle |

### The audio configuration path, and where it goes wrong

The full route into the fault, extracted from the instruction window rather
than guessed:

    0x080387A4  audio engine setup (runs)
      0x080388C6  bl 0x08015110          rate reconfigure gate
        0x08015144  blx r3               virtual dispatch -> 0x08031E8C
          0x08031DF4  cmp.w r1, #0x7D00  compare requested rate against 32000
            0x08031E4E  bl 0x0802F04C    stop SAI2
              0x0801F100  fault: handle->Instance is NULL
      0x080389E2  bl 0x0802F3BC          SAI2 *init* -- never reached

#### Sample rate, answered

`0x08015110` computes the rate as **1000 x a byte in the audio object**, taking
`[obj+0x22D]` or `[obj+0x22C]` depending on `[obj+0xFC] == 2`, and compares it
with the currently-configured rate at `[obj+0x230]`:

    ldrb.w r2, [r0, #0x22C or 0x22D]
    mov.w  r1, #1000
    mul.w  r1, r1, r2
    cmp    r3, r1                  ; r3 = [r0+0x230], the current rate
    str.w  r1, [r0, #0x230]
    beq.w  0x08015294              ; unchanged -> skip the reconfigure

Observed live: the object is at `0x2001AFDC`, both rate bytes hold **96**, the
current rate is 0, so it requests **96000 Hz** and takes the reconfigure. And
`0x08031DF4` branches away when the rate is `0x7D00` = **32000**, so the
firmware clearly distinguishes a 32 kHz path from others. That is open
question 4 in `EMULATION.md` largely answered: the rates in play are
32 kHz and 96 kHz, held as a kHz byte in the audio object.

#### Six hypotheses, all falsified

The null handle survives every cheap explanation:

| hypothesis | test | result |
|---|---|---|
| erased external flash | `DR` returns `0xFF` / `0x00` / `0x5A` | identical fault |
| a fully modelled flash | real device, correct boot sequence | identical fault |
| flash identity | `C2:20:18`, `EF:40:18`, `9D:60:18`, `FF:FF:FF` | byte-identical runs |
| a silent co-processor | with and without a live peer on USART2 | identical fault |
| wrong sample rate | force the rate byte to 32, 48, 96 | same fault, +33 insns |
| reconfigure ran when it should not | pre-set `[obj+0x230]` so the gate skips | same fault, +727 insns |
| constructors did not complete | count `init_array` entries executed | **22 of 22 ran** |

That last one is worth stating positively: `__libc_init_array` runs all 22
constructors to completion, including one at `0x0802F659` inside the SAI2
cluster. So the objects are constructed; they are simply not *initialised* with
their peripheral instances by the time the teardown runs.

Skipping the reconfigure entirely still reaches the same fault, which means the
SAI stop is reachable by more than one route. The pattern is systemic rather
than one misordered call, and patching past each null in turn manufactures a
path rather than finding one.

#### What the decompiler answered

Ghidra headless, driven by three Java scripts in `../ghidra/` (Java rather
than Python so no PyGhidra bootstrap is needed):

    # analyzeHeadless needs JAVA_HOME; paths.ghidra_env() supplies it, or:
    #   export JAVA_HOME=$(/usr/libexec/java_home -v21 2>/dev/null \
    #                      || echo /opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home)
    analyzeHeadless <proj> MicroFreak -import payloads/main.raw \
      -processor ARM:LE:32:Cortex -loader BinaryLoader \
      -loader-baseAddr 0x0800C000 -scriptPath ../ghidra -preScript MFSetup.java

`MFSetup.java` creates the RAM and peripheral blocks and marks the 96 real
vector handlers as entry points; `MFDecompile.java` prints C for given
addresses; `MFRefs.java` inverts references. (Headless `BinaryLoader` reports
an image base of 0 even when the block loads at `0x0800C000`, so the load
address is hardcoded rather than derived.)

**`FUN_0802f04c` is the audio DMA start:**

    void FUN_0802f04c(int obj, void *buf, uint size) {
      FUN_0801f100(obj + 0x88);                    // HAL_SAI_DMAStop
      FUN_0801f18c(obj + 0x88, buf, size >> 1);    // HAL_SAI_Transmit_DMA
    }

**`FUN_08031df4` chooses the audio geometry from the rate:**

    if (rate == 32000) { block = 0x24; bytes = 0x120; }
    else               { block = 0x20; bytes = 0x100; }
    *(short *)(obj + 0x17F7A) = block;
    FUN_0802f04c(sai_obj, obj + 0x16E72, bytes);

So **36 frames / 288 bytes at 32 kHz, 32 frames / 256 bytes otherwise**, from a
buffer at `audio_obj + 0x16E72`, handed to the SAI as `bytes >> 1` halfwords.
Observed live: `HAL_SAI_Transmit_DMA(hsai=0x2000471C, buffer=0x2001B6A2,
size=128)`. **That is open question 3 answered — the audio buffer is
`0x2001B6A2`.**

**`FUN_0802f3bc` is the SAI2 init.** Its first statements are
`*param_1 = <SAI block>` and `*(param_1 + 0x22) = 0x40015C04`, which is
precisely `0x2000471C->Instance = SAI2_Block_A`.

**`FUN_08038ab0` is not a boot-time init at all — it is main()'s audio
watchdog.** Decompiled from `main`:

    if (10 < now - last) {
      if (*(int *)(DAT_08025110 + 0x180) == *DAT_08025114) {   // counter stalled
        if (++*DAT_08025360 < 0xb) goto continue;              // 11 strikes
        FUN_0802f06c(...);  delay(100);
        RCC->x |= 0x80;     delay(500);
        RCC->x |= 0x800000; delay(100);
        *DAT_08025360 = 0;
        FUN_08038ab0(...);          // reset and re-initialise the audio
      }
    }

The firmware already knows how to recover from a dead audio engine: if its
sample counter does not move for eleven consecutive ~10 ms checks, it resets
the subsystem and re-runs the SAI2 init.

**And `FUN_080387a4` has no static callers** — it is reached only through a
vtable. Its tail calls `FUN_0802f04c` (DMA stop *and start*) **before**
`FUN_0802f3bc` (the init), unconditionally. That is why pre-setting the rate
got further and still faulted: skipping the reconfigure only reaches the tail's
restart instead.

#### Reaching the init

With that understood, the null-instance guards stop being a fabrication and
become a way of letting the firmware past a teardown of something that was
never set up — semantically a no-op. With both guards on (`--patch`):

    FUN_0802f3bc          RAN     the SAI2 init
    0x0802F242            RAN     the store that sets Instance
    HAL_SAI_Transmit_DMA  RAN x2

So SAI2 **is** initialised and `Instance` **is** set. What does not yet happen
is a DMA start *after* that: both `Transmit_DMA` calls occur while `Instance`
is still 0, from the tail that runs before the init. Nothing re-starts it
afterwards except the watchdog, which had not fired within the 1500 ms of
virtual time the run reached before stalling in a delay loop.

#### The audio path runs

Two more models were needed to get there.

**I2C.** The codec control bus. `I2C1_SR2` bit 1 is BUSY, which `0xFFFFFFFF`
pins high, so every transfer hit its 25 ms timeout and was retried forever.
Modelled as a peer that is always ready and always acknowledges.

**DMA half-transfer.** A HAL audio transfer is double buffered: the firmware
renders the first half of the block on HT and the second on TC, so raising only
TC asks for half the buffer. Both are raised now, in order.

With those and the two null-instance guards, over 90 s of virtual time:

    FUN_08038ab0  audio watchdog recovery   ran 50 times
    FUN_0802f3bc  SAI2 init                 ran 51 times
    0x0802F242    the store setting Instance ran 51 times
    HAL_SAI_Transmit_DMA                    ran 103 times
    DMA transfers                           254

and the transfers are the audio path itself:

    stream 4  128 items  0x2001B6A2 -> 0x40015C20   mem->periph   (SAI2_A DR, output)
    stream 6  128 items  0x40015C40 -> 0x2001B7C2   periph->mem   (SAI2_B DR, input)

**`0x40015C20` is SAI2 block A's data register.** A DMA store into it is the
digital signal the hardware hands to its DAC, so the harness captures it;
`--audio-out FILE` writes the samples as raw little-endian PCM.

The watchdog is what gets it there. It fires repeatedly because the engine's
sample counter never advances, and each firing re-initialises SAI2 and restarts
the DMA. That is the firmware recovering from a dead audio engine exactly as
designed.

#### The samples are silence

1152 captured words, every one zero, with or without the half-transfer
interrupt. So this is not a double-buffering gap: **the digital audio tap works
and the synth is simply not playing anything.** Nothing has triggered a note.

#### The inter-chip link is a multi-drop half-duplex bus

Baud rates identify every port, once you compute them against the right clock.

| chip | port | BRR | clock | baud | |
|---|---|---|---|---|---|
| main | USART1 | `0x0B40` | 90 MHz | 31250 | MIDI DIN |
| main | USART2 | `0x0062` | 45 MHz | 459,184 | **the co-processor bus** |
| main | USART3 | `0x0187` | 45 MHz | 115,090 | a separate 115200 link |
| all three co-processors | USART1 **and** USART2 | `0x0068` | 48 MHz | 461,538 | |

459,184 and 461,538 are the same intended rate -- **460800** -- reached from
two different clocks with 0.35% and 0.16% error. So the main chip has exactly
one port on that bus and all three co-processors sit on it.

That means it is **multi-drop**, and the co-processors confirm it in code.
`FUN_08004080(handle, enable)` in `iochip_kbd` is a direction switch:

    enable:   GPIO <- 2;  CR1 |= 8      (drive the line, TE on)
    disable:  CR1 &= ~8;  GPIO <- 0     (release it, TE off)

A driver-enable pin alongside the transmit enable is the RS-485 idiom. So a
co-processor listens by default and only drives the line when it has something
to say, which is why `TE` reads 0 in every capture and why the transmitter
being disabled is normal rather than a fault.

It also re-reads the frames the main chip sends. `AA F6 00`, `AA F6 01`,
`AA F6 02` with an incrementing payload byte is device addressing on a shared
bus, not an enumeration of parameters.

What does **not** work is provoking a reply by addressing a chip: feeding those
exact frames with each address in turn never reaches the TE-enable path. The
co-processors need their own input -- the keybed is capacitive, and that
sensing front end is unmodelled -- so this route to making one talk is blocked
behind a substantial piece of analog modelling.

#### MIDI DIN works in both directions

IRQ 37's handler at `0x08058214` reads the status register, checks for error
bits, and on `RXNE` hands the byte to `FUN_08058030`. Our USART model satisfies
it exactly -- status reads back `0xE0`, so `& 0xF` is zero (no errors) and
`& 0x20` is set -- which is why note-on bytes reach the engine at all.

And the firmware **transmits**: a run that fed it SysEx got `B0 09 0B` back on
USART1, a Control Change on channel 1. So the MIDI out path is alive and the
capture side of the USART model works in the outbound direction too.

#### MIDI notes reach the engine once the receive channel is set

The whole MIDI path works, and was verified at each stage rather than inferred:

    USART1 IRQ 0x08058214  ->  FUN_08058030   ring-buffer push
                           ->  FUN_08047C6C   drains it
                           ->  FUN_080480A8   parser, latches status 0x90
                           ->  FUN_080478A4   dispatch
                           ->  FUN_08048524   voice handler

`FUN_08048524` receives `0x00643090`, `0x00643C90`, `0x00644890` for notes 48,
60 and 72 -- **status 0x90 with the correct note number and velocity 100**.

What blocked it was the global MIDI receive channel at `0x200007E4`
(`DAT_08047b00 + 0x3c`). Dispatch accepts a message only on an exact channel
match or `0x7F` (omni), and the byte reads `0xFF` -- the erased value -- because
the MicroFreak's global settings live in internal flash, which starts erased
under emulation. Forcing it to `0x7F` is what finally let notes through.

The result is the first time note number has changed anything: renders of 48,
60 and 72 now have three **different** SHA1s, where every previous attempt gave
one.

Do not confuse that with pitch being right. Autocorrelation on those renders
locks onto background content rather than the note, and the energy at each
note's expected fundamental is *lower* than in a no-note render. Whether the
engine is producing the right pitch is an open question about analysis and
preset content, not about whether MIDI arrives -- that part is settled.

#### Two ways to ask for a preset, neither of which works

Nothing yet makes the firmware read a preset out of flash. The QUADSPI log
still contains no read command after either attempt:

- **MIDI Program Change** (`C0 05`): zero new QUADSPI transactions.
- **The documented SysEx preset read** (`F0 00 20 6B 07 01 2A 03 19 00 05 00 F7`,
  which `PROTOCOL.md` maps to opcode `0x19` and address
  `(slot + 129) * 4096` -- exactly where `mkflash.py` puts them): zero new
  QUADSPI transactions, though the firmware did emit that Control Change.

The storage read itself is reachable in principle: `FUN_08044ef8` has exactly
one caller, at `0x0804B82C`, inside the SysEx handling region around the
dispatcher at `0x0804ACC8`. So preset reads are driven by SysEx -- but by SysEx
arriving over **USB**, which is how every capture in this repo was taken, not
over DIN.

That makes the USB device stack, rather than the co-processor bus, the more
likely route to a loaded preset.

#### Correction: USART1 is MIDI, not a co-processor link

Earlier notes here assumed all three of the main chip's USARTs were inter-chip
links. The baud rates say otherwise.

> The figures in this table are computed against APB2 at 90 MHz for all three
> ports, which is wrong for USART2 and USART3 -- they are on APB1 at 45 MHz.
> "The inter-chip link is a multi-drop half-duplex bus" above has the corrected
> arithmetic: USART2 is 459,184 and USART3 is 115,090. The conclusion here --
> that USART1 is MIDI and the others are not -- is unaffected, which is why
> this is annotated rather than rewritten.


| port | BRR | baud (APB2 90 MHz) | |
|---|---|---|---|
| USART1 | `0x0B40` | **31250** | MIDI DIN |
| USART2 | `0x0062` | ~918k | inter-chip |
| USART3 | `0x0187` | ~230k | inter-chip |

31250 is exactly the MIDI rate, and the main firmware has `RXNEIE` set on
USART1, so bytes fed into that port's receive queue reach the MIDI parser. That
is the way to trigger a note, and it also explains why USART2 was the only port
carrying inter-chip frames.

#### Verified: the engine renders a correct note

A tap on what the DMA writes to the SAI data register produces audio that
checks out against something the harness does not choose. For preset 0
(`NervousKeys`) at MIDI note 60:

    period 367 frames, autocorrelation r = 0.99
    -> 261.58 Hz at 96 kHz, against C4 = 261.63 Hz  (0 cents)
    the rate that makes it exactly C4: 96,018 Hz

Two independent things agree. The firmware's own audio object holds a rate byte
of **96** (the rate is `1000 x` that byte, see the sample-rate section), and the
measured waveform period says 96,018 Hz. Neither was fitted to the other. The
waveform is a clean rising ramp with one sharp reset per cycle -- a sawtooth --
and `r = 0.99` says it is genuinely periodic rather than noise shaped by the
block structure.

**The sample rate is 96 kHz, not the 32 kHz assumed for most of this work.**
32 kHz appears in `FUN_08031df4` as a branch condition, not as the configured
rate.

#### Three bugs in the observation path, none in the firmware

Everything that looked like broken audio was in the tap or the DMA model. Each
was found by a listener describing the artifact, which was consistently more
informative than the measurements:

- **"Chugging."** Capturing at the DMA store made the capture rate a function
  of the harness's pacing constant rather than the SAI clock, so blocks
  repeated or were skipped. Fixed by tapping the render callback instead.
- **"Revving."** The half-mapping was inverted: a call with `half=1` fills the
  *first* half of the buffer and `half=0` the second, so every block captured
  was the one just replaced. Fresh and stale blocks alternated.
- **"A trill, rapid repeated beeps."** The real one. The DMA advanced a
  half-block every 4,000 instructions while the render callback takes ~5,800,
  so the emulated DMA outran the emulated CPU. Pending interrupts coalesce in
  the NVIC, the firmware falls behind, and the phase advances twice while it is
  still rendering -- filling one half twice and never filling the other. The
  half sequence read `1,1,0,0`; at a 12,000-instruction pace it reads
  `1,0,1,0` with no repeats.

That last one also explains the earlier measurements. The 329-frame period that
kept appearing is 10.3 x the 32-frame block: it was the underrun pattern, not
anything musical. **`DMA_PACE_INSNS` must stay comfortably above the render
cost** -- too fast is as wrong as too slow, and only the audible symptom
distinguishes them.

#### It produces a signal -- but not, so far, one that responds to anything

Booting the firmware, playing a MIDI note into USART1 and capturing what the
DMA writes to the SAI data register:

The firmware writes real, varying data into the audio buffer: 3584 distinct
values spanning -4570..4561, duplicated across both channels (L == R, a mono
engine feeding a stereo SAI). So the engine is running and producing a signal,
and the capture path is genuine -- these are the words the DMA hands to SAI2's
data register.

**A MIDI note changes the audio. The note *number* does not.** Two measurements,
which say different things and are easy to conflate:

| comparison | result |
|---|---|
| no note vs note 60 | **37.6% of samples differ**, from sample 195,536 on, mean absolute difference 2457 |
| note 48 vs 60 vs 72 | **byte-identical** -- one SHA1 across all three |
| note 60, with vs without presets in flash | byte-identical |

So the MIDI stream does reach the firmware and does change what it renders --
that is not timing jitter, it is a third of the output moving by thousands of
counts. But pitch does not track the note, and the preset region is not
consulted.

An earlier claim here that the note-on produced sound out of silence was wrong,
and so was the over-correction that followed it. The first capture showed
silence before the note and signal after; with the unpaced DMA model that
transition was the DMA starting, not the note arriving. The control -- an
identical run that never sends a note -- is the check that should have come
first, and it shows the truth is in between.

Two things follow, and both are checkable rather than inferred:

- **Presets are not being read.** The QUADSPI transaction log contains no read
  command at all: only reset (`0x66`/`0x99`), status (`0x05`/`0x35`), write
  enable (`0x06`), write status (`0x01`) and read ID (`0xAF`). The firmware
  initialises the flash and never loads a preset from it, so filling the slots
  changes nothing yet.
- **The firmware *does* reach a healthy state, once the DMA is paced.** An
  earlier note here said the audio watchdog fires permanently because the
  engine's sample counter never advances. Both halves of that are wrong now,
  and both were artifacts of the unpaced DMA model. With pacing, over 29 s of
  virtual time: the watchdog recovery **never runs at all**, the SAI2 init runs
  **once** rather than 51 times, and the counter reaches 5,249.

#### Pacing the DMA, and a retracted measurement

The first capture was misleading and the first pitch reading off it was wrong.
Completing a whole DMA block the instant the stream is enabled copies the
buffer *before* the firmware's HT and TC callbacks refill it, so the output is
stitched from stale fragments. The tell is unambiguous: the dominant
zero-crossing period was exactly **64 samples**, which is the DMA half-block,
not anything musical. Any pitch measured off that capture is an artifact of the
model, and the "~255 Hz, close to C4" reading is withdrawn.

The DMA model now advances a **half block at a time** -- copy half, raise HT,
let the firmware render, copy the rest, raise TC, and repeat for circular mode.
That is how the peripheral paces it on hardware. The difference:

| | instant | paced |
|---|---|---|
| words captured in one run | 8,704 | **544,000** |
| median step between samples | 58 | **17** |
| dominant jump spacing | 64 (the half-block) | 1-3 (adjacent) |

Circular DMA now actually runs rather than firing once, which is where the
62x more audio comes from, and the block-boundary artifact is gone.

#### Presets, and why the first renders were not musical

`../src/mfemu/mkflash.py --presets-from captures/mcc-presets.jsonl` fills the slots with
all 399 real presets from the capture. `preset_tlv.load_decoded_presets` reads
them without hardware, and the sizes line up with what
`FORMATS.md` already established: a decoded preset is 4088 bytes, a
flash slot is 4096, and the firmware stages writes in a buffer it memsets to
`0xFF`, so the payload sits at the start of the page with the tail erased.

This matters for interpreting the audio. Every render before this ran against
an **erased preset region**, so the engine's parameters were undefined. The
spectrum showed that plainly: the strongest components sat at 48-64 Hz in a
cluster rather than a harmonic series on the note played. Under those
conditions a pitch measurement says nothing about whether the synthesis is
correct, only that the engine is running.

It is also what makes bulk work practical here. Loading a preset becomes a
write to a file, instead of 146 lock-step-acked flash chunks over SysEx.

#### The audio chain, end to end

The counter question resolved into a map of the whole path. `0x2001C7B0` has
exactly one reference in the image -- a read, in `main()` -- so it is written
only through the object base `0x2001C630`, which is `audio_object + 0x17E00`.
Watching it in the emulator found the writer immediately:

    ldr.w   r3, [r8, #0x180]     the counter
    adds    r3, #1
    str.w   r3, [r8, #0x180]     0x08038E0E, 5,249 times in 29 s
    cmp     r1, #0               which half: HT or TC
    ldrsh.w r4, [r8, #0x17a]     block size
    add.w   r4, r2, #0x72        the DMA buffer

`0x08038DF8` is the **audio render callback**, and Ghidra had not found it
because nothing calls it statically -- it is a callback. The full chain:

| | |
|---|---|
| `0x0802F008` | DMA2_Stream4 IRQ handler (IRQ 60), fired 4,853 times |
| `0x0801B31C` | HAL_DMA_IRQHandler, 14,924 times |
| `0x08038DF8` | render callback: bumps the counter, takes a half index |
| `0x08036B38` | **the synthesis entry point**, `(obj + 0x18, obj + 0x16E28)` |
| `obj + 0x16E28` | scratch buffer the engine renders into |
| `obj + 0x16E72` | the DMA buffer it is converted into |
| `0x40015C20` | SAI2 block A data register |

That the callback takes a half index, and reads its block size from `+0x17A`,
confirms the half-transfer model has the right shape rather than merely
working.

#### What is actually left

The machine is healthy and rendering. Two things remain unexplained, and
neither is now attributable to a broken boot:

- **Presets are never read.** No QUADSPI read command is ever issued. Something
  has to ask for a preset, and on hardware that request comes from the UI --
  which means the co-processors, which are not talking (their transmitters are
  disabled; see the serial section).
- **The note number is ignored** while the note itself has an effect. With no
  preset loaded the engine has no oscillator configuration, so a fixed default
  pitch is a plausible consequence rather than a separate fault.

Those two may well be one problem. The next lever is making the firmware load a
preset, and the most likely trigger is the co-processor link rather than
anything on the audio side.

### Audio input -- SAI2 block B (2026-10-10)

**The instrument records continuously, on every boot, for every preset.**
Everything in this section is from Ghidra -- the decompiles and literals named
below -- so it is harness-independent.

**What is NOT established: anything measured.** Renode, the active harness since
2026-09-06, models **SAI block A only** (`../renode/STM32_SAI.cs`); there is no
block B, no receive path, and `microfreak.repl` wires only
`DmaRequestA -> dma2@4`. EMULATION.md's DMA table does show
`0x40015C40 -> 0x2001B7C2  periph->mem`, and a ramp feed confirmed the buffer
receives verbatim -- **both under emulation, not on hardware.**
Treat the addresses here as read from code and the dynamics as unmeasured until
block B exists in Renode.

`FUN_0802F3BC` initialises **two** SAI handles inside one object at
`0x20004694` -- block B at `+0x00`, block A at `+0x88` (the HAL handle is
0x88 bytes, so this is two handles back to back).

| | block A (`+0x00`) | block B (`+0x88`) |
|---|---|---|
| `Instance` | `0x40015C04` SAI2_A | `0x40015C24` SAI2_B |
| mode field (handle `+0x04`) | 2 = slave **TX** | 3 = slave **RX** |
| `Synchro` | 0 = asynchronous | 1 = synchronous to A |
| `OutputDrive` | `0x2000` enabled | 0 disabled |
| DMA stream | `0x40026470` = DMA2 **S4** | `0x400264A0` = DMA2 **S6** |
| handle slot | `+0x70` `hdmatx` | `+0x74` `hdmarx` |
| direction | `0x40` mem->periph | `0` periph->mem |
| NVIC IRQ | 60 `DMA2_Stream4` | 69 `DMA2_Stream6` |
| DMA handle | `0x200045D4` | `0x20004634` |

Both are armed by `FUN_08038DB8`, which `main()` calls at `0x0802519E`. There
is **no gate on the arming** -- the RX DMA runs regardless of engine, preset or
whether a vocoder exists. A capture routine does not need the vocoder.

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x2001B630` | T | audio engine buffer base | the literal at `0x08038A98`; `engine_base + 0x16E00` |
| `0x2001B6A2` | T | SAI2_A **TX** buffer | base `+0x72`, 256 bytes |
| `0x2001B7C2` | T | SAI2_B **RX** buffer | base `+0x192`, 256 bytes. Derived from code; EMULATION.md's matching DMA observation is from an earlier harness, so it corroborates rather than confirms |
| `0x20004830` | T | audio engine object base | `0x2001B630 - 0x16E00`. Size 92,048 (`0x08038AA8`) = exactly 4 x the `0x59E4` voice stride |
| `engine+0x17F7A` | T | frames per block | reads `0x20`; set at `0x0803899C` |
| `engine+0x170B4` | T | audio-in read cursor | written with the RX buffer pointer at `0x080380DA`, `0x08038A06`, `0x08039A96` |
| `0x0802F018` | T | `sai_transmit(obj, buf, bytes)` | adds `0x88` to reach block A, halves `bytes` to items, calls `HAL_SAI_Transmit_DMA` at `0x0801F18C` |
| `0x0802F298` | T | `sai_receive(obj, buf, items)` | block B at `+0x00`, calls `HAL_SAI_Receive_DMA` at `0x0801F244` |
| `0x08038DB8` | T | `audio_dma_arm(engine)` | starts TX then RX, sets state `2` at `engine+0x17F7E` |
| `0x08039A64` | T | `audio_in_process(engine, half)` | the audio-input consumer; see below |
| `0x08039D94` | T | RX **half**-transfer thunk | `movs r1,#0; b.w 0x08039A64` -- 4 bytes |
| `0x08039D9C` | T | RX **complete** thunk | `movs r1,#1; b.w 0x08039A64` -- 4 bytes |

Both buffers are 256 bytes, double buffered. Each half is **32 stereo int16
frames** -- from the consumer's 4x-unrolled loop, which steps `r5 += 0x10` for
`r12 += 4` and runs to the 32 at `engine+0x17F7A`, covering 128 bytes. The SAI
`AudioFrequency` argument is 96000, the literal at `0x08038AA4`.

The two 4-byte thunks are the natural hook point for a capture routine: the
same trampoline shape the v12 SysEx work already proved on hardware.

#### The audio-in consumer is gated on the raw value 20852

`FUN_08039A64` does its work only if a 16-bit value passes one comparison:

    08039a7a  ldr    r1,[0x08039d84]      -> 0x2001C898
    08039a7c  movw   r2,#0x1158
    08039a88  ldrsh  r2,[r1,r2]           -> 0x2001D9F0
    08039a90  movw   r3,#0x5174           -> 20852
    08039a94  cmp    r2,r3
    08039a9a  beq    0x08039aaa           -> the DSP loop

Below the gate it stores the RX pointer to `engine+0x170B4` and returns. Above
it, the loop reads `ldrsh [r5,#0/2/4/6]` -- real input samples -- against eight
floats at `engine+0x17F10..0x17F2C`.

**`0x5174` is 20852, and `microfreak/preset/fields.py:ENGINE_VALUES["Vocoder"]` is 20852** --
measured on hardware 2026-09-10 by saving one preset per engine off a 5.0.0.2084
device. The two numbers were derived completely independently, which identifies
the gate field as **`VCO/Type`** and settles the vocoder question outright.

**Run 2026-10-10 both ways, on an earlier emulator rather than this harness,
so this is corroboration and not a measurement on the harness that ships.**
Factory
flash image, a ramp fed into SAI2_B DR, the gate word forced on entry to
`FUN_08039A64`:

| forced value | `audio_in_process` entered | DSP loop reached |
|---|---|---|
| 20852 | 1551 | **1551** |
| 20851 | 1551 | **0** |

Identical in every other respect -- same feed, same buffer contents. So the gate
is exactly `== 20852` and nothing else about it is conditional.

#### What `0x2001D9F0` is

The address appears as a literal at `0x08037204` and `0x080374BC`. At
`0x08036FE4` it is the **source of a structure copy**: `memcpy(dst, src, 0x84)`
at `0x08036FDA` followed by an unrolled word copy, into a destination that
advances by `0x59E4` -- the voice stride -- once per voice.

So `0x2001D9F0` is **parameter[0] of the 132-byte per-voice parameter block**,
read by the gate as a signed 16-bit. `VCO/Type` being parameter 0 of an
oscillator parameter block is a strong fit, which is what would tie the gate to
`Vocoder`.

**The field is `VCO/Type`**, on the strength of the independent match to
`ENGINE_VALUES` above: a 132-byte oscillator parameter block whose parameter[0]
takes the exact value the device writes for `Vocoder`.

### The audio codec -- I2C 0x18, and only one input is connected

`../renode/MicroFreakCodec.cs` already established the bus: 7-bit address
**0x18** on **I2C1**, 96 writes and exactly one read (`reg 0x67`, reading back a
value it just wrote -- a presence check, not an identification). The firmware
never asks the device what it is.

**Decoded 2026-10-10** from an emulated run, reconstructing the transactions
from writes to `I2C1_DR` (`0x40005410`). The register map fits the **TI
TLV320AIC3x** family closely: page select at 0, PLL at 3-6, a coefficient page 1
holding Q1.15 biquads, MICBIAS at 25, scratch at 103. *The part is inferred from
that fit, not confirmed -- nothing reads a device ID, so if it is not an AIC3x
the bit-level reading below is wrong.*

The input routing is written twice, and exactly one register differs:

| reg | AIC3x name | first pass | second pass |
|---|---|---|---|
| `0x13` | LINE1L -> LEFT ADC | `7E` | `7E` |
| `0x15` | LINE1R -> LEFT ADC | `78` | `78` |
| **`0x14`** | **LINE2L -> LEFT ADC** | `78` | **`00`** |
| `0x11` | MIC3L/R -> LEFT ADC | `F0`, `FF` | `F0`, `FF` |
| `0x16` | LINE1R -> RIGHT ADC | `7E` | -- |
| `0x17` | LINE2R -> RIGHT ADC | `78` | -- |
| `0x18` | LINE1L -> RIGHT ADC | `78` | -- |
| `0x12` | MIC3L/R -> RIGHT ADC | `F0`, `FF` | `F0`, `FF` |
| `0x19` | MICBIAS control | `80` | -- |

On AIC3x these registers carry the input level in bits 6-3, where `1111` means
**disconnected** and `0000` is 0 dB, and bit 2 powers the ADC channel. Every
routing register keeps the `1111` nibble except `0x14`, rewritten to `0x00`.
`0x13` and `0x16` carry `7E` -- disconnected, bit 2 set -- so they power the left
and right ADC without routing anything.

Conclusions, all unconditional at boot and independent of engine or preset:

* **Exactly one analog input is connected: LINE2L into the left ADC.**
* **MICBIAS is powered**, `0x19 = 0x80` -> bits 7-6 = `10` = 2.5 V. Only an
  electret microphone needs that.
* **Capture is mono by construction.** Nothing is routed to the right ADC, so
  dropping the right channel of the stereo stream loses nothing.
* **The link runs at 96 kHz.** `reg 0x07 = 0x6A` sets fsref 48 kHz with the ADC
  dual-rate bit, and `FUN_0802F3BC` is called with `AudioFrequency = 96000`
  (the literal at `0x08038AA4`) -- two independent readings agreeing. The
  sampler's native rate is 32 kHz, so a capture path needs an exact **3:1
  decimation**, not a resampler.

### What the firmware does that limits a render

Engine behaviour, measured. The harness's own gaps are the capability table in
`EMULATION.md`, which is kept current; this list is about the firmware.

- **Paraphonic presets render silent** -- 217 of 391 captured presets.
- **Wavetable and sample engines fault** on a null table pointer.
- **Note-off is untested**, not known-broken: the runs it would have been
  measured on all used a message gap that the emulator of the day could not
  deliver a second message inside.
- **The analog filter is not in the firmware at all.** It sits after the DAC,
  so no emulator can produce it. Every audio figure in this document is
  therefore the pre-filter signal. `mfemu render` applies the measured model
  by default and `--no-filter` is the comparable output -- see
  `../src/mfemu/filter_model.py`. Its control voltage IS in the firmware;
  see "The analog outputs" below.

### The analog outputs (2026-10-20)

The firmware drives the analog section through one output layer. A command
table at `0x08073F7C` names the channels `pitch cutoff press reso vca`, and
`FUN_08046c60` (a SysEx calibration handler) sets per-channel calibration for
channels 0-4, with a 128-entry table for channel 1.

| address | T/D/I | what |
|---|---|---|
| `0x08036b38` | D | control loop; copies a 0x6C-byte output block from the engine every pass, then calls `0x08011844` |
| `0x08011844` | D | sends each output only when it changed; filter-mode pins via `0x080233a0` |
| `0x08023504` | D | per-channel output curve: channel 1 interpolates a 512-step table, 3 is linear inverted, 4 linear |
| `0x08023368` | T | **cutoff CV**: channel 1, 16 bits split across TIM3 CCR4 (high byte) and CCR3 (low byte) |
| `0x0802340c` | D | channel 4 to TIM14 CCR1, 13 bits; held while a note sounds and released after it -- the VCA, probably |
| `0x080233a0` | D | filter-mode switch: three GPIO lines from a 0-3 mode |
| DAC1 `DHR12R1` | D | channel 3, written once per preset load (2254 at resonance 0) -- resonance, probably |

The cutoff row is `T` because it was proved by elimination and then by
values: Sausage Bass at knob 45.4 writes 29746 (45.39% of 65535), the same at
knob 80 writes 52428 (80.00%), and with its cycling envelope routed to cutoff
it alternates 45.4 / 83.5 every control tick. Nothing else the firmware
writes changes between those three. `mfemu render` captures this CV and
drives the filter model with it; see `mfrender.cutoff_cv` and
`filter_model.CV_SMOOTH_TAU`.

**The first attempt at this concluded the opposite**, because `--preset N=FILE`
was placing the file one slot low (fixed the same day): every comparison
rendered the bank's own neighbour twice, and every output came back identical.
If a channel ever seems not to respond, check what was actually loaded first.

Two limits that used to be here are gone. Internal flash was modelled as RAM,
so a program with no preceding erase silently succeeded where hardware would
not; `../renode/STM32F4_Flash.cs` implements the controller, and its
`StrictProgram` enforces the clear-bits-only rule. And a different preset
needed a cold boot, which Program Change over a working MIDI path settled.


## Oscillators and engines

### Oscillator dispatch

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x08034394` | T | oscillator virtual call | `vtable[9]` of the object at `state+0x50ac` |
| `0x0803099C` | T | `osc_render_wrapper` | two arms on block size `[r1+0x48]`; `== 32` goes to `0x08030A24` |
| `0x0800D480` | D | osc thunk | -> `0x0805F458` with the output buffer in `r3` |
| `0x0805F458` | T | `osc_entry` | engine object array at `obj+0x6E4`, count at `obj+0x724` (reads 7), selected index at `obj+0x658` |
| `0x08078578` | D | engine **vtables** | seven, Itanium-ABI, **24 bytes each and the stride IS uniform**, through `0x0807861C`; zero header at `0x08078570`, four virtuals, vptr points at slot 0 = `Init`. `0x080785D8` is Speech, engine **index 5** of the seven. The apparent 0x2C gap before `0x08078604` is five interleaved floats at `0x080785E8` (0, 7.01, 12.01, 19.01, 24.01 — semitone intervals), not an irregular record. Installed as vptrs by `FUN_080359B0`; `Init` called on all seven by `FUN_0805F2C4`. See SPEECH.md |
| `0x0805DA8C` | T | `speech_formant_generator` | formant filter bank; state at `[r0+0x54..0x64]` |
| `0x0805E554` | T | `speech_formant_caller` | per-formant loop; writes a float per record with `str lr,[r0],#24` |
| `0x0805E764` | T | call into the generator | |
| `0x0805B2DA`, `0x0805B40A` | T | callers of the formant loop | inside descriptor `0x080785D8`'s methods |
| `0x0807C84C` | D | formant coefficient table | in-image, present |
| `0x0805DD20` | D | LCG multiplier `0x0019660D` | the generator's noise source |
| `0x2000011C` | T | LCG state | changes every call — *not* a data pointer |

### Engine data tables, and the pitch conversion — mapped 2026-10-05

Pure reading pass, no code changes. Builds on the same day's finding that
`0x08078578`..`0x0807861C` are seven C++ vtables.

#### The Chords engine is Plaits' `ChordEngine`, table included

**`0x08078484`: 11 chords x 4 notes, float32 semitone intervals.** Byte-exact
against Plaits' `chord_engine.cc`, including the `0.01`/`11.99` detune trick
that keeps unisons from beating:

| # | intervals | chord |
|---|---|---|
| 0 | 0, 0.01, 11.99, 12 | octave |
| 1 | 0, 7.01, 7, 12 | fifth |
| 2 | 0, 5, 7, 12 | sus4 |
| 3 | 0, 3, 7, 12 | minor |
| 4 | 0, 3, 7, 10 | m7 |
| 5 | 0, 3, 10, 14 | m9 |
| 6 | 0, 3, 10, 17 | m11 |
| 7 | 0, 2, 9, 16 | 69 |
| 8 | 0, 4, 11, 14 | M9 |
| 9 | 0, 4, 7, 11 | M7 |
| 10 | 0, 4, 7, 12 | major |

The table base is loaded at `0x08058498`, which is inside `0x08058494` —
**slot 1 of vt0**. So **vt0 is the Chords engine** (object at `voice+0x000`,
`Init` `0x0805836C`). That is the second of the seven identified, after Speech.

Note this is the **Chords engine**, one of the 22 named oscillator engines. It
is not necessarily the same thing as the paraphonic chord mode that
`Gen/Chord`/`Gen/ChOffs1` drive — that link is **not** established here.

#### The semitone -> frequency ratio conversion, fully decoded

`0x08058494` converts a chord row's four semitone values into frequency ratios,
and the method is reusable for **any** mod that needs to set a pitch:

    n     = semitones + 128.0
    ratio = coarse[int(n)] * fine[frac(n) * 256]

    coarse[i] = 2^((i-128)/12)    256 entries at 0x0807ECE8
    fine[i]   = 2^(i/3072)        256 entries at 0x0807E8E4

Both verified against the closed forms to float32 precision (max relative error
4.6e-8 and 5.9e-8). Range -128..+127 semitones at 1/256-semitone resolution.
The `128.0` bias is the literal at `0x080585D8`; the two table pointers are the
literals at `0x080585D4` and `0x080585DC`.

#### Which data tables each engine touches

Per-engine, scanning `Init`/slot1/slot2 with each function bounded by the next
known function start. (An earlier pass used a flat 0x300-byte window, which
overlapped neighbouring functions and wrongly attributed Speech's
`word_banks_` to vt3.)

| vtable | object | tables referenced | identity |
|---|---|---|---|
| vt0 | `voice+0x000` | `0x08076538`, `0x0807654C`, `0x08078484`, `0x08078534` | **Chords** |
| vt1 | `voice+0x150` | `0x0807CACC` | unknown |
| vt2 | `voice+0x188` | none | unknown |
| vt3 | `voice+0x20C` | none | unknown |
| vt4 | `voice+0x350` | `0x08078838` (`word_banks_`) | **Speech** |
| vt5 | `voice+0x560` | `0x080785E8` | unknown |
| vt6 | `voice+0x604` | `0x0807B510`, `0x0807C834`, `0x0807CCD4` | unknown |

Leads for the remaining five:

- **vt5 reads the five floats at `0x080785E8`: 0, 7.01, 12.01, 19.01, 24.01** —
  a stacked fifths/octaves voicing with the same `.01` detune signature as the
  chord table. These sit wedged between vt4 and vt5's vtables and are reached by
  computed address, not by literal, which is why an earlier pass found "no
  references" to them.
- **vt1's `0x0807CACC`** spans **-12.0 to +36.0** in groups of three identical
  values — four octaves of semitones, three at a time, so plausibly a 3-voice
  pitch spread.
- **vt6's `0x0807B510` and `0x0807CCD4`** both traverse -1.0..+1.0 in small
  even steps, the shape of sine/cosine LUT halves. `0x0807B510` is one of the
  seven live objects in the region once mislabelled "unreferenced slack".
- **vt3's `Init`** (`0x0805B14C`) allocates 96 bytes then compares its own
  vtable slot 1 against the literal `0x0805B01D` — a vtable identity check,
  RTTI being compiled out — and either tail-calls slot 1 or branches to
  `0x0805D1A0`. So vt3 is a subclass whose `Init` defers to a base.

### Granular sample lookup, reversed (2026-10-02)

Read while chasing why `172-HitGrains-v30` renders digital zero and is one of
the loudest presets on the instrument.

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x08015C88` | D | `grain_note_setup(engine, out)` | looks the sample up by the index at `engine+0x14B`, caches hash at `+0x13C` and length at `+0x140` |
| `0x08045150` | D | `sample_record(obj, idx)` | `idx > 127` -> the static empty record; else the RAM table |
| `0x08046548` | D | `sample_table_entry(base, idx)` | `idx * 0x18 + base + 0xC` -- a **24-byte-stride table of 128 entries** at `obj+0x1004` |
| `0x08075324` | D | empty sample record | all zero except **`+0x17` = `0xFF`**; `alloc` 0, `hash` 0 |

The table entries have the same 24-byte layout as a flash sample record --
`+0x04` alloc, `+0x08` hash, `+0x0A` name, `+0x17` slot -- so the boot scan
copies each flash record's header into `table[slot]`.

#### The grain-position walk, and the exact no-grain condition

Read from the body of `FUN_08015C88`:

```c
pos = engine[0x134];                       // float, running grain position
cnt = (float)(engine[0x140] >> 1);         // length in BYTES >> 1 = sample count
if (pos < cnt) engine[0x134] = pos + engine[0x138];    // advance by the step
...
if (cnt <= pos) engine[0x102] = 0xFF;      // NO GRAIN
else            engine[0x101] = 1;         // grain allocated
```

`Hit Grains` walks a position forward through the sample and **stops
permanently once it reaches the sample count**. If the position starts at or
past the end, `cnt <= pos` holds on the first call, no grain is ever allocated,
and the engine emits digital zero while running normally.

Also in the setup:

    engine[0x128] = (rec[0x04] >> 1) * ((short)engine[0x12C] >> 4) >> 11

which is sample-count scaled by `engine[0x12C]` over roughly 32768 -- the shape
of a length-proportional position, though `engine[0x12C]` is not identified.

#### The three offsets, found (2026-10-02)

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x08015B84` | D | `grain_set_start(engine, raw)` | `engine+0x12C = raw`; `engine+0x128 = (raw >> 4) * (engine[0x140] >> 1) >> 0xB` |
| `0x08015BB8` | D | `grain_clear_step(engine)` | `engine+0x138 = 0` |
| `0x08015BA8` | D | `grain_set_step(rate, engine)` | `engine+0x138 = rate / s14` |
| `0x08015AC0` | D | `grain_engine_reset(engine)` | zeroes `+0x14A`/`+0x14B` (so an unspecified sample index defaults to **0**) and presets `+0x102 = 0xFF` |

`grain_set_start` is the whole story for the emulator. Dropping the shifts, it is

    position = sample_count * raw / 32768

so at `raw` = 32767, i.e. `Start` = 100, the position is `count * 2047/2048` and
only `count/2048` samples remain -- about **35** on a long sample. A grain needs
more than that, so `cnt <= pos` holds, `+0x102` takes `0xFF`, and the engine
emits digital zero. **The emulator's behaviour is now fully accounted for, and
it looks correct rather than buggy.**

Note also that `+0x128` is recomputed inside `FUN_08015C88` **only when the
sample hash changes**, from the stored `+0x12C`. So the start position is
hash-gated and can go stale across preset changes that share a sample.

#### Which leaves the hardware as the anomaly

This inverts the question the investigation started from. The arithmetic is the
instrument's own, so `Start` = 100 should silence it there too -- and it does
not, playing flat at 0.0213-0.0230 across the entire sweep.

The stale-`+0x128` route is the obvious candidate and **does not survive the
test already run**: panel 95 was captured after priming with `SmpIdx` index 0,
index 48, and nothing, and all four captures agree to +0.993 against a +0.996
reproducibility control. A stale start position would have made those differ.

So something still unaccounted for keeps the instrument's grain position inside
the sample. Candidates, none tested:

- `Start` may not reach `grain_set_start` as the preset's raw 32767 on hardware
- `engine+0x140` may hold something other than `rec[0x04]` there
- a second writer of `+0x128` or `+0x134` outside the `0x08015xxx` cluster

#### The digital-zero path

```c
rec = sample_record(obj, engine[0x14B]);
if (rec[0x17] == 0xFF) return;                 // no audio at all, silently
if (engine[0x13C] != rec[0x08]) {              // hash changed?
    engine[0x13C] = rec[0x08];
    engine[0x140] = rec[0x04];                 // length := ALLOCATED size
    engine[0x128] = (rec[0x04] >> 1) * (engine[300] >> 4) >> 11;
}
```

**`rec[0x17] == 0xFF` returns with no audio and no complaint.** That is a
concrete mechanism for a preset rendering exactly zero while the engine runs
normally, and it fires for any sample index with no record behind it.

The mapping is faithful: `mkflash.py` writes the bank's own slot numbers,
0-based against `mfbank`'s 1-based, names aligned, all 52 hashes non-zero and
distinct.

**Corrected the same day:** the flash records carry slot bytes 0-22 and 99-127,
and I first read that as "indices 23-98 hit the early-out". Wrong -- the 64
presets in the 5.0 pack that set `VCO/SmpIdx` explicitly span indices 0-48 and
nearly all render fine, so the engine's index is a position in the 52-sample
*list*, not a slot number. The table is fully populated and there is no gap.

#### The cached length is the allocated size, not the audio length

`engine+0x140` takes `rec[0x04]`, which `mkflash.py` defines as header plus
audio **rounded up to a 4096-byte page**. Measured on our own image:

    Piano Pop      alloc 147456   hdr+audio 144836   overstates by 2620
    Piano Clean    alloc 147456   hdr+audio 144024   overstates by 3432
    String Stacca   alloc  61440   hdr+audio  57562   overstates by 3878

So grain positions are computed against a length up to a page too long, and the
tail of a grain window can run past the real audio into erased flash. That is a
genuine fidelity detail and it is **not** the panel 95 cause -- it would make a
high `Start` quieter rather than exactly zero, and it is the same on hardware if
the device rounds the same way.

#### Where panel 95 now stands

`172-HitGrains-v30` carries **no `VCO/SmpIdx` and no `Gen/SmpHash` in its TLV**,
so the sample index is not in the preset at all -- it comes from `engine+0x14B`,
i.e. from state. The emulator boots fresh for every render; the instrument has
been running and loading presets. **If that state differs, the two play
different samples, which fits every observation:** the emulator's audio matches
bank sample `Braam` at +0.860 while the hardware's matches no bank sample
(-0.094), the two are anti-correlated at every `Start`, and a shorter sample
would put the `Start` cliff exactly where the emulator shows it.

**Tested on hardware 2026-10-02, and falsified.** Panel 95 was captured after
priming with a preset at `SmpIdx` index 0, after one at index 48, after a
repeat of index 0, and with no priming at all:

    A1 vs A2  same priming, repeated     +0.996    <- reproducibility control
    A1 vs B   different priming          +0.993
    A1 vs C   no priming                 +0.993
    B  vs C                              +0.997

Between-condition similarity is indistinguishable from within-condition
reproducibility, and rms is 0.0444-0.0448 across all four. **Panel 95 sounds the
same regardless of what preceded it**, so its sample choice is not inherited
state.

So four hypotheses are now dead -- the sample bank, mix-writer selection,
`Start` as a hardware cause, and inherited sample index -- and the emulator bug
is still real and still uncaused. What remains unexamined:

- **What writes `engine+0x14B`** for a preset carrying no `VCO/SmpIdx`. Not
  located, and now the most direct question: the emulator reads *some* sample
  (matching `Braam` at +0.860), so whatever supplies that index is where the two
  sides can differ.
- **`build_page()` truncates 24 bytes.** The decoded block is 4088 bytes and the
  page body budget is `PAGE_LEN - PRESET_HEADER` = 4064, so the last 24 bytes of
  every preset we write are dropped. The TLV stream ends around 1188 so nothing
  named is lost, but what lives at 4064-4088 on a real preset has never been
  checked.

### The Speech engine, reversed — and why it is silent

`speech_formant_generator` (`0x0805DA8C`) is a **9th-order lattice filter** --
Kelly-Lochbaum / LPC, the architecture behind Speak & Spell style speech.
Reading it end to end:

```
[r0+0x00]        phase accumulator
[r0+0x04]        used in the head to derive the phase increment
[r0+0x0C]        EXCITATION GAIN          <-- zero, and this is the fault
[r0+0x10]        s3 seed
[r0+0x14]        LUT index, must stay < 640
[r0+0x18..0x3C]  ten lattice reflection coefficients
[r0+0x40..0x64]  ten filter states
```

Per sample: `[r0+0] += s1`; when it crosses 1.0 the phase wraps and the byte
LUT at `0x0807C84C` is indexed for a glottal pulse. That pulse becomes the
excitation via

    r6  = (int8)LUT[idx]
    s11 = (float)r6 * const
    s11 = -( [r0+0x0C] * s11 )        vnmul.f32 s11, s15, s11

then accumulates into `s3` -> `s14`. The ladder runs a backward pass
(`vfms` down) and a forward pass (`vfma` back up), storing state to
`[r0+0x40..0x64]`. Outputs: `s14` (excitation) to `[r1]`, `s15` (filtered) to
`[r2]`.

**`[r0+0x0C]` is zero**, so `s11 = 0`, `s14 = 0`, nothing excites the ladder,
and `s15` comes out as the denormal `0x00000001` (1.4e-45). That is exactly
the pair measured at the call site (`a = 0`, `b = 1.4e-45`).

Forcing it at the generator entry (`cpu AddHook 0x0805DA90` writing `r0+12`)
produces audio, scaling linearly with the value:

    [r0+0x0C] = 0.0    AC RMS    0.0    (control)
    [r0+0x0C] = 0.1    AC RMS  495.1
    [r0+0x0C] = 1.0    AC RMS 4953.3     10.004x -- exactly linear

**The vocal tract itself is fine.** The ten reflection coefficients read as
real LPC values, all non-zero and in range:

    0.5605 -0.1914 -0.3359 0.0078 -0.1875 -0.1562 -0.4141 0.4297 -0.0313 -0.3984

So the firmware *is* delivering LPC frame data to the filter. What it is not
delivering is the per-frame **gain**. Forcing the gain to a constant gives a
sustained buzz with a flat envelope over 3 s, not a spoken word -- a word needs
the gain to follow the frame envelope, which is precisely what `[r0+0x0C]`
should carry.

#### The gain is written, with a real envelope -- just never on a note

`[r0+0x0C]` is written from `0x0805DE2C`, the same frame loader that writes the
lattice coefficients at `0x0805DE24`. It does deliver a proper amplitude
contour. Splitting the writes around an explicit note-on marker:

    before the note-on:   0.4453 x4, 0.2227, 0.125, 0.0586, 0.0078  (+51 zeros)
    after the note-on:    36 writes, every one zero

Attack, sustain, decay -- that is an utterance's gain envelope, and it plays
**once, before the note**, presumably at preset load or engine init. The
note-on never re-triggers it, which is also why the generator's output was
byte-identical before and after a note in every earlier measurement.

So nothing is missing from the engine or its data. The LPC frames arrive, the
coefficients are real, the gain envelope exists and is correct. **What is
missing is the trigger**: whatever starts the frame sequencer runs early and
is not driven by note-on.

#### The frame sequencer, and the one-shot trigger

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x0805DD24` | D | `lpc_frame_interpolate(rec, prev, cur, s0)` | interpolates two frames by fraction `s0` |
| `0x0805DE22` | D | writes coefficient 0 -> `[r0+0x18]` | |
| `0x0805DE26` | D | writes coefficient 1 -> `[r0+0x1C]` | |
| `0x0805DE2A` | T | writes the **gain** -> `[r0+0x0C]` | the field whose zero silences the engine |
| `0x0805E820` | T | `utterance_start` | sets frame pointers `[r4+0x84]`/`[r4+0x88]`, resets position `[r4+0x8C] = 0` |
| `0x0805E7DA` | D | the gate | `cmp r9,#0; bne` -- `r9 != 0` selects the utterance path |
| `0x0805E860`, `0x0805E994` | D | call `lpc_frame_interpolate` | |

**The trigger fires exactly once.** Counted around explicit markers, with
`r4 = 0x20009318`:

    during boot      0
    at preset load   1     frame index 0xAE
    after note-on    0

After the utterance completes the sequencer parks: `[r4+0x84]` and
`[r4+0x88]` both read `0xBC` -- previous and current frame identical, so the
interpolation is static -- and the position sits at `0x60`.

Four ways of trying to re-fire it, all measured, none working:

- **note-on** -- never reaches `utterance_start`
- **re-sending the same Program Change** -- short-circuited, the firmware skips
  a reload when the slot index is unchanged
- **switching to another preset and back** -- the other preset sounds, the
  Speech one does not; `utterance_start` count 0
- **rewinding the position** (`[r4+0x8C] = 0`) -- silent, and correctly so:
  both frame pointers still point at the final frame, so the interpolator
  blends two identical frames

So a full restart needs the frame pointers reset to the word's start as well,
which is what `utterance_start` does from a word index (`lsls r3, r0, #13;
adds r2, r3, r0`).

#### ROOT CAUSE: the voice level is pinned at 1.0, so the note-start edge never happens

The full chain, every link measured:

```
0x200096F4   voice level      reads 1.0 before the note, 0.2 s after, 0.7 s
   |                          after -- permanently at maximum, never follows
   |                          an envelope
   v
0x0805F4C0   vcmpe s15, threshold; bgt 0x805f728
   |         the level crossed once, at preset load, and never fell back, so
   |         [r6+0x668] ("voice started", 0x2000955C) was set to 1 then and
   |         is never cleared -- the clear at 0x0805F70E needs the level to
   |         drop below the threshold
   v
no 0 -> 1 edge on note-on
   v
0x0805F71C   lr = 2 instead of 1        (1 only on the start edge)
0x0805F5C4   strb lr, [sp+24]           the flag byte the engine reads
   v
0x0805B3F0   r2 = [r9] & 1  ->  0       the gate, measured 1 in only 1 of 6874 calls
   v
0x0805E7DA   cmp r9,#0; bne  not taken
   v
0x0805E820   utterance_start never runs on a note
   v
frame sequencer stays parked (both pointers 0xBC, position 0x60)
   v
0x0805DE2A   gain [r0+0x0C] stays 0
   v
0x0805DA8C   lattice gets zero excitation, emits the denormal 0x00000001
   v
silence
```

Measured at each step: `THRESHOLD-CROSSED` fires once during preset load and
never after a note; `STARTED` goes `0 -> 1` once and never changes again; the
gate reads 1 in exactly one call out of 6874; the gain is written with a real
envelope (0.4453, 0.2227, 0.125, 0.0586, 0.0078) before the note and only
zeros after it.

**So the Speech engine and its data are entirely correct.** The defect is one
level upstream and is not specific to Speech: the voice level at `0x200096F4`
never moves. Other engines do not notice, because they do not gate on its
edge -- they simply play at whatever level is presented, which is why
everything else sounds. Speech is the engine that happens to depend on the
edge, so it is the one that exposed this.

#### The level is a gate, not an envelope

Following it upstream:

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x0802FE80` | T | `osc_set_gate(osc, on)` | writes **1.0** if `on != 0` else **0.0**, to `[osc->0x5c + 0x828]`; vtable slot 7, pointer at `0x08074D30` |
| `0x080349F8` | T | the only call site | `ldr r3,[r3,#28]; blx r3`, then caches the state: `strb r9, [r4+0x42B0]` |
| `0x08033D2E` | D | the only way in | `ldrb r9,[r4+0x42B2]; cmp r9,#1; beq 0x80349E8` |
| `0x08034304` | D | clears the cache | `strb #0, [r4+0x42B0]` |
| `[r4+0x42B2]` | D | **desired gate** | the note-on signal; `[r4+0x42B0]` is the cached copy |

So the value the Speech engine edge-detects is not an amplitude envelope at
all -- it is a **per-voice gate**, 1.0 or 0.0. Measured: `0x200096F4` is
written exactly **once** in a whole run, with `1.0`, from `0x0802FE98`, and
never again -- not on note-on, not on note-off.

That is why the engine fires once at preset load and never again, and it is
also why no other engine noticed: they read the level as a plain multiplier
and are content with a constant 1.0, while Speech is the only one that
triggers on its *edge*.

#### SOLVED: the gate is high from boot, so the first note has no rising edge

`r4` is `0x2000484C` -- recovered by reading `cpu.GetRegister(4)` from the
watchpoint on the level, since `r4` is callee-saved and survives into
`osc_set_gate`. So the desired gate is `0x20008AFE` and the cached copy
`0x20008AFC`.

Watching all four values across a note:

    while the note is held   desired = 1     level = 1.0
    after the release        desired = 2     level = 0.0

So the note path works perfectly: note-on sets the desired gate, the update
runs, `osc_set_gate` writes 1.0; note-off sets it to 2 and the level correctly
falls to 0.0.

**The problem was that the level is already 1.0 when the firmware finishes
booting.** Writing 1.0 again on the first note is not a rising edge, so the
Speech engine's edge detector never fires. Every silent render in this
investigation used a single note-on after boot, which cannot produce an edge.

Give it a real edge and it speaks, with nothing forced or poked:

    note-on, note-off, note-on   ->  UTTERANCE x1, AC RMS 388.2, peak 5906
                                     envelope [1496, 149, 2, 2, 2, ...]

a burst that decays into silence -- the shape of a spoken word.

**Hardware-confirmed (2026-09-09):** the owner listened to that render and it
says *"eight"*, matching what UI preset 5 does on their device. The emulator
reproduces the Speech engine from nothing but the firmware image and a preset
read over SysEx.

**Nothing remains -- the emulator is faithful.** An earlier version of this
section called the boot-time gate value "the only real defect left", on the
assumption that a first key press after power-on speaks on hardware. The owner
checked, and it does not:

> first turning on the MF and going to preset Snit and holding down a key
> produces no sound on actual hardware -- it only plays at the second press.
> This is just when the machine is first powered on; after that the first press
> is not needed, it always plays first try until there is a power cycle.

That is exactly what the emulator does. The gate being high after boot, so
that the first note-on is not a rising edge, is **firmware behaviour**, and
Renode reproduces it down to the power-cycle dependency. Anything rendering a
Speech preset must prime with a note-on/note-off first -- not as a workaround,
but because that is what the hardware requires too.

**A practical obstacle worth recording.** Several addresses in this region are
not translation-block boundaries, and `cpu AddHook` on them silently never
fires -- `0x0805B3D2`, `0x0802FE80` and `0x08033D10` all failed this way,
each costing a run before it was noticed. Watchpoints do not have this
problem, and a watchpoint hook can read `cpu.PC` **and `cpu.LR`**, which is
how `osc_set_gate`'s caller was found after the direct hook failed. Prefer a
watchpoint on the data over a hook on the code whenever there is a choice.

#### How the trigger was traced (the flag bit)

Following the gate upwards:

| Address | Conf | What |
|---------|------|------|
| `0x0805E7DA` | D | `cmp r9,#0; bne` -- `r9 != 0` takes the utterance path |
| `0x0805E5CA` | D | `mov r9, r2` -- so `r9` is the formant caller's **third argument** |
| `0x0805B2BE`, `0x0805B3F0` | D | both call sites pass `r2 = [r9] & 1` -- bit 0 of a flag byte |
| `0x0805B20C` | D | the engine's render, vtable slot 2 of descriptor `0x080785D8`; `mov r9, r1` at `0x0805B22E`, so the flag struct is **its second argument** |
| `0x0805F6A6` | T | `blx r7` -- the render call, with `r1 = sp+24` |

Measured at `0x0805F6A6`, with an explicit note-on marker:

    during preset load   argflag = 1  exactly once, out of 224 calls
    after note-on        argflag = 0  all 66 calls

and separately at the formant call site, `gate = 1` once in 6874 calls. So the
whole utterance hangs on one bit being set for one render call, at preset
load, and never again.

`[r8+12]` -- the object byte copied to `[sp+23]` at `0x0805F672/0x0805F690` --
is **not** it: measured 0 on every call. The flag the callee reads is at
`[sp+24]`, and the only byte store into that frame is the one at `[sp+23]`,
so `[sp+24]` is written elsewhere, outside `0x0805F458..0x0805F6C4`.

**Unresolved:** what sets `[sp+24]`. Note `r8` takes three values
(`0x2000907C`, `0x20009244`, `0x2000EA60`) -- several engine instances -- and
the stack address of the flag varies per call (`0x2001FB98`..`0x2001FBF8`),
so a single-address watchpoint will not catch the writer. That is the next
obstacle, not a lack of leads.

**Everything downstream is proven good**: the LPC lattice is correct, the
coefficients are real speech data, the gain envelope exists with the right
shape, and forcing the gain makes audio that scales linearly. Only the re-arm
is missing.

### Mod matrix and assign destinations

Added 2026-10-01 while chasing what an `Assign1/2/3` slot can point at.

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x080622D4` | D | matrix destination label table | 7 pointers: `Pitch` `Wave` `Timbre` `Cutoff` `Assign1` `Assign2` `Assign3` -- exactly `preset_fields._MOD_DESTS` order |
| `0x080622F0` | D | matrix source label table | 5 pointers: `CycEnv` `Env` `LFO` `Press` `Key/Arp` -- exactly `MOD_SOURCES` order, and the long names confirm `_MOD_SOURCE_TAG` (`EG1`=CycEnv, `EG2`=Env, `Xpr`=Press, `Key`=Key/Arp) |
| `0x08075778` | D | `"->"` separator | read at `0x08051222`; the assign menu joins source and destination with it, giving the `Press -> Wave` entries seen on the device |
| `0x08051064` | D | `assign_slot_label(ui)` | draws an assign slot's label; computes the slot's id as `0x902 + slot`, which is what proves parameter ids are `(module << 8) \| param` |
| `0x080111c8` | D | `cell_destination(obj, id)` | `((id - 0x0a00) & 0xffff) >> 8` |
| `0x080111d4` | D | `cell_source(obj, p)` | identity -- the source is the parameter index |
| `0x080111d8` | D | `source_name(obj, i)` | `*(0x080622F0 + i*4)` |
| `0x080111e4` | D | `dest_name(obj, i)` | `*(0x080622D4 + i*4)` |
| `0x0804ec88` | D | `param_get(id)` | global parameter getter keyed on `(module << 8) \| param`; applies the per-parameter scaling code |
| `0x08011258` | D | `param_enum_labels(obj, id)` | -> value-label table and count for a parameter; `id 0` special-cases the engine table (stride `0x1c`, clamp 22) |
| `0x080624dc` | D | assign module mask `0x000e01be` | bit `module` set = skipped by the generic assign modulation path; allowed are `VCO` `EG2` `Mat` `Co1`-`Co7`. **Not** an assign whitelist -- see FORMATS.md |
| `0x080336xx` | D | `apply_assign_mod` (voice path A) | reads `0x902`+n, writes `voice[(id>>8)+0x104f][id&0xff]`; mask ref at `0x080337dc` |
| `0x080344xx` | D | `apply_assign_mod` (voice path B) | same, mask ref at `0x080347f4` |
| `0x08023c2c` | D | `param_get_by_id(obj, id)` | the getter the modulation path uses |
| `0x0804ed7c` | D | `param_get_raw(id)` | `*(short *)(obj[module].values + param*2)` -- the stored value, unscaled |
| `0x08027fb4` | D | `seq_automation_track_id(obj, i)` | 4 tracks, 64 step records of 16 bytes, page stride `0x412`; `0xffff` = unassigned |
| `0x08027f48` | D | `seq_automation_clear_page` | step values initialise to `0x64` = 100 |
| `0x08028090` | D | `seq_automation_step_has(obj, step, track)` | per-step track bitmask at record `+0x11` |
| `0x0805228c` | D | UI dispatcher | calls `0x08051064`; reads `0x400`/`0x409` (`Arp/Enable`, `Arp/SeqOn`) |
| `0x08062ab8` | D | `"Assign1"` | `Assign2` `0x08062ac0`, `Assign3` `0x08062ac8`, `KyMdSrc` `0x08062ad0` |

The `Co*` modules' six parameters are `EG1` `EG2` `LFO` `Xpr` `Key` `Dest`,
with display names `CyclEnv` `Env` `Lfo` `Press` `Key/Arp` `MatDest` -- so a
`Co<n>` module is the destination column `n` and its five source fields are the
matrix cells. `Dest` is the column's own assignable target.

**Still open: what the assign menu will actually offer.** The manual says
"(almost) every knob", excluding Master Volume, the Preset Encoder, SHIFT+knob
parameters such as the Cycling Envelope shapes, and button controls. The filter
that implements that is not located. `CC != 127` was the obvious candidate and
is **wrong** -- `ParaX1` carries CC 127 and is used as an assign target by 208
factory presets. Finding the menu-walk code is the remaining job; it is near
whatever references `0x08075778`.


## Storage

### Preset storage

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x080454D4` | T | `preset_page_load(obj, slot)` | the load hook `mfrender.py` uses; `r1` is the absolute slot, so hooking it reads Bank Select + PC directly (see FORMATS.md) |
| `0x08045238` | T | `preset_page_write(obj, slot)` | issues erase + 16 page programs |
| `0x0804500C` | D | `region_length(obj)` | |
| `0x08044FDC` | I | region helper | |
| `0x08044FF4` | I | region helper | returns 0 on failure |
| `0x0804A8EC` | I | `store_region(reply, src, len)` | |
| `0x08045150` | T | `sample_record_lookup(table, idx)` | `base + 0x1004 + idx*24 + 12`; `>127` returns the default at `0x08075324` |
| `0x08046548` | D | record address helper | `base + idx*24 + 12` |
| `0x200018B8` | T | sample directory | records start at `+0x1010`, 24 bytes each, indexed by SLOT: `{u32 flash addr, u32 size, u16 content hash, 13B name, u8 slot}`; slot `0xFF` = unused. Dumped live 2026-09-11 |
| `0x080456A4` | D | `wavetable_fetch(obj, dest, 0x20, table, frame, slot, user)` | `addr = (frame + table*0x20) * 0x200`, then `+0xFAF000` or `+0xF6F000` **only if `user`** -- per-table bit in the `ushort` at `obj+0x1c5a` picks which. With `user` clear the base is **0**, which is where the factory wavetable bank lives. Reads 0x400 bytes via `FUN_08044260` |
| `0x08017D9C` | D | wavetable engine render | calls `wavetable_fetch`; table index at `engine+0x50`, destination at `engine+0x68`, the user flag is its last argument |
| `0x08015C88` | T | `granular_render(engine, out)` | the Scan/Cloud/Hit Grains engine. Bails if the record's slot byte is `0xFF`; caches the sample length at `engine+0x140` **only when `engine+0x13c` differs from the record's hash at `+8`**, so a record with hash 0 renders silence. `engine+0x14b` is the sample index |
| `0x08016E48` | T | sample playback caller | the `Sample` engine's path to `sample_record_lookup`; does not gate on the hash, which is why `Sample` played from a hash-less record and the granular engines did not |

### Preset name record (45-byte `23 52` message)

Extends the "not answered by the firmware strings" list in
`FORMATS.md`.

| Byte | Meaning | Conf |
|------|---------|------|
| 9/10 | slot hi/lo | D |
| 12 | flags; `0x08` = slot empty | D |
| 19 | **preset category** | T — `0x02` Keys, `0x03` Lead, from hardware; spans `0x00`-`0x0B` |
| 20 | schema/engine tag | T — high nibble tracks `Voc` presence, low nibble the engine |
| 21.. | name, NUL-padded | D |

### QUADSPI / external flash

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x0801DD80` | T | `HAL_QSPI_Transmit` byte loop | writes `DR` (`+0x20`), polls `SR` bit 2 (FTF) |
| `0x0801DD94` | T | FTF wait, timeout-bounded | where every preset write hung before the `0xD7` fix |

Flash opcodes this firmware uses: `0x05` read status, `0x06` write enable,
`0xEB` fast read quad, **`0xD7` 4 KB sector erase** (not `0x20`), `0x35`/`0x66`/
`0x99`/`0xAF` in init. A preset write is one `0xD7` plus sixteen 256-byte page
programs — seventeen write-enables.

### The external flash

The QUADSPI controller and the serial NOR device behind it both have to be
modelled; `mfemu flash` builds the backing image.

    mfemu flash -o flash.img

Modelling it needed the **F4 DMA model** first, because the QUADSPI data phase
is DMA-driven — the ID read never completed without it. Streams of `0x18` from
`0x10`, not the F0's channels of `0x14` from `0x08`. That model is needed by
the audio path too, since SAI is fed by DMA, so it was worth building for its
own sake.

#### The boot sequence, read off a trace

    0x66, 0x99   reset enable + reset, sent in QPI mode then again in SPI
    0x05         read status: first as hardware auto-polling for WIP, then indirect
    0x06, 0x01   write enable, then write status register (sets quad-enable)
    0x05, 0x35   read status 1 and 2 back to confirm
    0xAF         read ID, 6 dummy cycles, 3 bytes, quad mode

`0xAF` is "read ID in QPI mode" — a Macronix/ISSI idiom rather than the
universal `0x9F` — so the default identity is a Macronix 128-Mbit part. The
capacity agrees with the storage map independently: user wavetables at
`(index + 0xF5F) << 12` reach ~15.4 MB, so the device is 16 MB.

#### Three controller behaviours the driver depends on

Each of these hung boot until modelled, and none is guessable from the register
names alone:

- **`TCF` is set when the command runs, not when the FIFO drains.** It means
  the programmed length has moved between flash and FIFO. The driver waits on
  `TCF` *before* reading `DR`, so latching it on drain deadlocks.
- **`ABORT` is self-clearing and completes the transfer.** It flushes the FIFO
  and raises `TCF`. Storing the bit leaves the driver waiting for a `TCF` no
  command is coming to produce, and reading `ABORT` back as permanently set.
- **An indirect *write* must wait for its data phase.** `FTF` means "the FIFO
  needs attention" in both directions — bytes to read, or room to write. A
  model that sets `FTF` only for reads hangs every write, and the boot sequence
  contains one (`WRSR`).

#### The image

The factory wavetable bank shipped in the firmware bundle is already a flash
fragment: its header is `magic 0x7981`, then `0x00F5F000`, then the payload
size — and that second word is the destination address, exactly where the
wavetable formula puts index 0. So it drops straight in. Presets go at
`(n + 129) * 4096` via `--preset N=FILE`.

Programming honours NOR semantics — a byte is AND-ed, not replaced — so a
missing erase shows up as corruption here rather than silently working.

#### What it did not fix

Boot with a full flash model lands on **the same SAI2 null handle, at the same
instruction count** as with the flash returning zeros (9,873,786). And the
firmware **does not validate the ID**: `C2:20:18`, `EF:40:18`, `9D:60:18` and
even `FF:FF:FF` all give byte-identical runs.

So the "flash identity selects a degraded path" idea is closed too, alongside
the two hypotheses already tested. The flash is now correct and out of the way;
it is not what stands between boot and audio.

### Writing to the external flash

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x08044290` | T | `qspi_write(obj, src, addr, len)` | **general** RAM->flash write; computes `0x100 - (addr & 0xFF)` and chunks across 256-byte page boundaries itself |
| `0x08044560` | T | `qspi_erase_sector(obj, addr)` | write-enable `0x06`, erase `0xD7`, poll status `0x05` |
| `0x08044654` | T | `qspi_erase_and_write(obj, src, addr, len)` | erases the sector containing `addr & 0xFFFFF000`, then tail-calls `qspi_write`. **Arbitrary address** -- the one call a capture routine needs |
| `0x08045238` | T | `preset_page_write(obj, slot)` | `(slot + 0x81) * 0x1000` |
| `0x0804535C` | T | wavetable name write | `(slot + 0xFEF) * 0x1000` |
| `0x080452AC` | T | wavetable audio write | `(arg + 0xFAF + table*4) * 0x1000`, sets the per-table bit at `obj+0x1C5A` |
| `0x080453D0` | T | fixed-address write | always `0x80000`, length from the caller |
| `0x08045444` | T | marker write | one byte `0x51` at `arg + 0xFFF000` |
| `0x080464E8` | T | **sample directory builder** | the PC that writes the in-RAM records; see below |

**None of these targets the sample region.** The sample chain from `0x281000` has
no dedicated writer in this list, so a device-side capture would call
`0x08044654` directly rather than reuse a sample-specific routine.

#### Reading it back, and the rest of the storage layer

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x08044260` | D | `qspi_read(obj, dest, addr, len)` | the read primitive; `wavetable_fetch` pulls 0x400 bytes through it |
| `0x08045890` | D | `storage_read_request(obj, dest, addr, len)` | queued/async read. **Bounds-checks hard**: rejects unless `addr > 0x0028_0FFF` and `addr + len <= 0x00F5_1000`, i.e. the flash sample region and nothing else. An SRAM address fails it, which is why no engine can play a sample out of RAM |
| `0x08046468` | T | `sample_boot_scan(table)` | builds the in-RAM sample directory by a **linear page sweep** from `0x281000` to `0x00F5_0FFF`, advancing `(size & 0xFFFFF000) + 0x1000` on a hit and one page on a miss. Accepts a record when `record[0]` equals the address it was read from (or the magic `0xFA55AAEF`), `slot != 0xFF`, `size <= 0x177000` and `slot < 0x80`. Entered once; `r0` is `obj + 0x1004`. **Mid-function**: it sits inside `FUN_08045098`, which `main` calls at `0x08024F0C` |
| `0x08016E30` | T | `sample_engine_render(engine, out)` | the `Sample` engine, 3420 bytes. Looks the record up through `sample_record_lookup`, bails on slot `0xFF`, compares the record's hash at `+8` against its cache at `engine+0x84`, then fetches audio via `storage_read_request`. Called from `0x0803097E` |

#### The audio system's init and DMA arming

| Address | Conf | Name | Notes |
|---------|------|------|-------|
| `0x080387A4` | T | `audio_system_init` | 728 bytes; sets the four voices' sample rate through `vtable[0x30]` at `0x080389B2`, stepping `r7` by `0x5980 + 0x64 = 0x59E4` against a bound of `0x16790` -- which is **4 x `0x59E4`** and is where the voice stride is established in the image |
| `0x0802F13C` | T | `sai2_blockA_dma_init(obj, handle)` | DMA2 **stream 4** (`0x40026470`), direction `0x40` mem->periph, into `hdmatx` at handle `+0x70`, NVIC IRQ 60 |
| `0x0802F2DC` | T | `sai2_blockB_dma_init(obj, handle)` | DMA2 **stream 6** (`0x400264A0`), direction `0` periph->mem, into `hdmarx` at handle `+0x74`, NVIC IRQ 69. This is the audio **input** |
| `0x0801F18C` | D | `HAL_SAI_Transmit_DMA` | reached via `sai_transmit`, which adds `0x88` to reach block A and halves the byte count to items |
| `0x0801F244` | D | `HAL_SAI_Receive_DMA` | reached via `sai_receive` on block B at handle `+0x00` |
| `0x08036FE4` | D | `voice_param_block_copy` | copies the 132-byte parameter block from `0x2001D9F0` into each voice, destination advancing by `0x59E4`. `memcpy` at `0x08036FDA` then an unrolled word copy. Parameter[0] of that block is what the audio-in gate at `0x08039A90` tests against 20852. **Mid-function**: inside `FUN_08036B38`, reached from `FUN_08038DF8` |

#### The boot scan builds the directory from the flash chain -- measured

`../src/mfemu/mkflash.py` already implements the chain format exactly (`place_samples`:
`0x18`-byte header of `{u32 self address, u32 allocated size, u16 hash, 13B
name, u8 slot}`, page-rounded, `0x1000` erased gap between records, scan stops
at `0xF50000`). An emulated run with the factory bank confirms the scan
consumes it:

    flash image: mkflash.py --samples data/banks/factory_sample_bank.mfsbz
                 52 samples chained 0x281000 -> 0xA4D000
    400M instructions, first directory write at #46,394,031 by pc 0x080464E8
    21 of 52 records populated (the scan is progressive and had not finished)

    idx 3   addr 0x2FE000 size 61440  hash 0x8593 'Stamisen'
    idx 8   addr 0x399000 size 200704 hash 0x4FC6 'Brrp'
    idx 13  addr 0x44B000 size 86016  hash 0x42FC 'Harsh FM'
    idx 14  addr 0x461000 size 282624 hash 0x02CD 'Three Words'

Real factory names, addresses, sizes and hashes, read out of the in-RAM
directory at `0x200018B8 + 0x1010`. **So a sample present only as a flash chain
record does become a directory entry** -- no SysEx involved. That is the link
between a device-side capture and the `Sample` engine.

Free space: the factory chain ends at `0xA4D000` and the scan stops at
`0xF50000`, leaving **0x503000 = 5,255,168 bytes**, about **82 seconds** at
32 kHz mono 16-bit.

**MCC confirms `0xF50000` independently (2026-10-18).** Its memory bar on the
hardware reads `124.4s` used / `85.5s` free, so the ceiling it draws against
is 209.9 s = 13,430,784 bytes = `0xF50000 - 0x281000` exactly. `mfreak sample
list` walks the same chain over SysEx and reports 124.4 / 85.4 -- the 0.1 is
MCC subtracting its two rounded figures rather than the byte counts. So this
constant is no longer analysis alone. Arturia advertises 219 s; neither the
instrument nor its own editor agrees with that number.

**A hand-written record appeared not to be picked up -- and that was the
emulator, not the firmware.** Tracing the scan settles it. `FUN_08046468` is
entered once, at instruction #44,037,034, with `r0 = 0x200028BC` (`= obj +
0x1004`, which confirms the table base). It then makes 3,280 address compares
and matches **none**:

    ENTRY #1  insns=44037034  r0=0x200028BC
    cursor=0x281000  record[0]=0x00000000     <- our record IS here
    cursor=0x282000  record[0]=0x0293FE88     <- garbage
    cursor=0x283000  record[0]=0xFFFFFFFF     <- correct for erased flash
    cursor=0x284000  record[0]=0xFFFFFFFF
    ...

Our record at `0x281000` carries `addr = 0x00281000`, so the read should return
that. It returns zeros -- the stack buffer's initial contents -- and the next
read returns stale garbage. **the emulator used for this run failed to deliver the
first one or two flash reads after the scan starts**, because the storage layer
issues an async read and polls for it while a QSPI/DMA model that completes
transfers instantly never leaves it anything to poll.

Everything else then falls out consistently:

* a lone record at `0x281000` is always missed -- it is the dropped first read;
* a 3-record chain at `0x281000`/`0x284000`/`0x287000` lands the later ones, and
  the observed commit was slot 6 at `0x284000`;
* the factory bank's first populated index was 3, not 0 -- its `0x281000` record
  was dropped the same way.

So **the record format and the acceptance rule are correct, and `place_samples`
writes a valid record.** Nothing here constrains a device-side capture. The
defect was in the emulator that run used, not in the firmware or the record --
redo the measurement on this harness if it is ever needed again. Recorded so
the four runs that chased it are not repeated.


## The other three chips

### The other three firmware images

The bundle ships four code images, not one, and the harness so far only runs the
main chip. The other three are the I/O co-processors, and they are a *different
architecture* — this is established from the images, not assumed:

| | main | iochip_oled | iochip_kbd | iochip_matrix |
|---|---|---|---|---|
| size | `0x73A5F` | `0x13EEF` | `0x68EF` | `0x73EF` |
| load address | `0x0800C000` | `0x08000000` | `0x08000000` | `0x08000000` |
| initial SP | `0x20020000` | `0x200020F0` | `0x20001DE8` | `0x20001800` |
| vector entries | 114 | 45 | 45 | 45 |
| CBZ/CBNZ per 8 KB code | 51 | 0 | 0 | 0 |
| IT blocks per 8 KB code | 23 | 0 | 0 | 0 |

`CBZ`, `CBNZ` and `IT` are ARMv7-M-only encodings. The main image is dense with
them and the co-processors contain none, so **the co-processors are Cortex-M0
(ARMv6-M) and the main chip is Cortex-M4 (ARMv7E-M)**. The 45-entry vector
tables (16 system + 29 IRQs) and 6–8 KB stacks agree.

Their peripheral map is the STM32F0 family's, not the F4's — counted as literal
references in each image:

| | F0-class address | refs (oled / kbd / matrix) |
|---|---|---|
| RCC | `0x40021000` | 15 / 14 / 15 |
| FLASH interface | `0x40022000` | 11 / 11 / 11 |
| GPIOA–C | `0x48000000`+ | 12 / 9 / 3 |
| USART1 | `0x40013800` | 3 / 3 / 3 |
| USART2 | `0x40004400` | 3 / 3 / 3 |

One model served every target, parameterised rather than duplicated: what made
the main chip boot turned out to be architecture-independent, and only the
memory map, CPU model, DMA register layout and the handful of modelled status
registers differ between them.

The differences are not cosmetic. F0 DMA channels have a **0x14** stride from
`0x08`, against the F4's 0x18 from 0x10, so an F4 decode mislabels every
channel register. `FLASH_SR` BSY is bit 0, not bit 16. `RCC_CFGR` is at `+0x04`,
not `+0x08`. And `0x40021000` is RCC on an F0 but GPIOE on an F4 — the same
address means different things, which is exactly why the table is per-target.

#### Why this matters more than a completeness note

Every one of the three references **USART1 and USART2**, and the main firmware
configures USART1, USART2 and USART3 (at instructions #6897955, #6898493 and
#6898988). That is the inter-chip link, from both ends.

`EMULATION.md` framed the co-processors as a risk: if the main firmware
blocks on them, "you must either stub their inter-chip protocol or emulate all
three, and it is considerably longer". Having all three images turns that
around. **The protocol does not have to be reverse-engineered at all** — run
the co-processor cores alongside the main core and connect their UARTs, and the
protocol emerges by construction. Three Cortex-M0 images totalling 128 KB, with
no audio path and a handful of peripherals each, are a far smaller job than the
main chip already proved to be.

That is also the second hypothesis for the null dereference below: a SAI object
whose init is gated on something a co-processor was supposed to say.

#### The wavetable bank is not code

`userwavetable_factory_v1_3__20211015.bin` (327,744 bytes) is flash *content*,
and directly useful. Its header is `u32 magic 0x7981`, `u32 0x00F5F000`,
`u32 payload 0x50000` — and that second word is exactly the destination flash
address, confirming `(index + 0xF5F) << 12` from `FORMATS.md`. The
field `strip_headers.py` calls "unknown" is the load address.

Layout: 16 named slot records of `0x1000` from offset `0x40` (Convergence,
Funky Bell, Pluck Additive, Pluck Odd, Pluck Resonant, Iris, FM Triangle,
Mellovox, Shrrom, Nache, My DM, SawQ, Potion, Subt, Little Monster, Sine
Bells), then 64 waves of 2048 `int16` from `0x10040`.

For the flash device model this is a ready-made region to drop at `0xF5F000`,
alongside the 398 presets in `captures/mcc-presets.jsonl` at
`(n + 129) * 4096`. A realistic flash image can be built entirely from material
already in hand.

### Serial: the inter-chip protocol

Established by running all four cores together with their USARTs wired into
each other's receive queues, each core taking a fixed quantum of instructions
in turn. The wiring is `main:USART2` to `iochip_kbd:USART1`, `main:USART1` to
`iochip_oled:USART1`, and `main:USART3` to `iochip_matrix:USART1`.

All four boot and run together. And the main chip talks.

#### The frame format

The main firmware transmits on **USART2**, starting at instruction #8,047,168:

    aa f7 10 00 00 7f ff 2f
    aa f6 00 00 00 00 00 a0
    aa f6 01 00 00 00 00 a1
    aa f6 02 00 00 00 00 a2

Fixed 8-byte frames:

| field | bytes | |
|---|---|---|
| sync | 1 | always `0xAA` |
| opcode | 1 | `0xF7`, `0xF6` seen so far |
| payload | 5 | |
| checksum | 1 | **8-bit sum of the seven preceding bytes, sync included** |

The checksum holds on all four frames — `0xAA+0xF6 = 0x1A0` → `0xA0`,
`+0x01` → `0xA1`, `+0x02` → `0xA2`, and the `0xF7` frame sums to `0x32F` → `0x2F`.
The incrementing byte in the `0xF6` payload reads like an index being
enumerated.

This is **not** the SysEx protocol in `PROTOCOL.md` — that one runs over USB to
the host. This is the private link between the main chip and its
co-processors, and it fell out of running the firmware rather than from any
capture. No logic analyser, no probing.

#### What each co-processor does with it

Feeding those 32 bytes to each chip (`--feed USART1:<hex>`) separates them
cleanly:

| chip | result |
|---|---|
| `iochip_kbd` | consumes all 32 bytes, **32 USART1 interrupts**, one per byte |
| `iochip_matrix` | consumes all 32 bytes, 32 USART1 interrupts |
| `iochip_oled` | leaves all 32 **unread** — it must receive by DMA, which fits a display |

#### Why the co-processors stay silent — settled

Swept every opcode, one frame at a time, letting the chip run between frames
— feeding 256 frames back to back makes the receive path thrash, while paced
they are consumed cleanly.

`iochip_kbd` consumes **all 2,048 bytes of a full 0x00–0xFF sweep and replies to
none of them**. The reason is not the stimulus:

    USART1  CR1=0x00000025  UE=1 TE=0 RE=1 RXNEIE=1 TXEIE=0  tx=0
    USART2  CR1=0x00000025  UE=1 TE=0 RE=1 RXNEIE=1 TXEIE=0  tx=0

**`TE` is 0 — the transmitter is disabled.** No frame arriving on the receive
path can produce a reply, because the chip is not configured to transmit at
all. And it is deliberate: the CR1 write sequence is `0x0 → 0xC → 0xD → 0x5 →
0x25`, so `TE` (bit 3) is set during init and then explicitly cleared.

It is not half-duplex either — `CR3` `HDSEL` is 0 on both sides, so this is not
a shared wire being turned around. The main chip, by contrast, runs all three
ports with `TE=1` and `RE=1`:

    main  USART1  CR1=0x0000202C TE=1 RE=1   tx=0
          USART2  CR1=0x0000202C TE=1 RE=1   tx=32
          USART3  CR1=0x0000200C TE=1 RE=1   tx=0

So the link is currently one-way by the co-processor's own choice. Something
must enable `TE` before it will speak, and no amount of protocol guessing gets
there. The most likely trigger is real input activity — the keybed is
capacitive, and that sensing front end (ADC and timer capture) is not modelled.
That, not the protocol, is the next piece of work for making a co-processor
talk.

#### USART model

Transmit is always ready — `TXE` and `TC` read set — so a driver never blocks
on baud rate, while every byte it writes is captured in order. `TC` is latched
rather than pinned, because a permanently-set `TC` with `TCIE` enabled is an
interrupt storm. Receive queues bytes, raises `RXNE`, and pends the port's IRQ
while `RXNEIE` is set. A DMA store into a data register counts as a transmit,
so DMA-driven output is captured too.

The USART blocks needed `zero_regions` for the same reason DMA did, and it bit
harder: an unwritten `CR1` reading `0xFFFFFFFF` has *every* interrupt enable
set at once, which is not a boot but an interrupt storm.


### The front-panel bus (2026-10-21)

Established on 5.0.0.2084 by reading the handlers and by driving them in the
emulator; the same frames work on every official release from 1.0.5. This
supersedes the wiring assumed in the section above for the panel: panel input
and display output both travel on the main chip's **USART2**. USART1 is MIDI
DIN, and USART3's interrupt handler is a bare `bx lr`.

**Receive path.** The USART2 IRQ fills a 64-byte ring at `0x2001E004`.
`FUN_0802D530` drains it into the frame parser `FUN_0807F678` (sync `0xAA`,
then the 8-bit sum of the first seven bytes in byte 8), and hands complete
frames to `FUN_080219D4`, the bus object at `0x200005A0` (vtable
`0x08073500`).

**Frames.** After `AA`: byte 1 is `(chip << 4) | type`. Type 0 is panel input,
with byte 2 a subcommand, bytes 3-4 a control id, bytes 5-6 a value. The
control ids are the firmware's parameter ids, module * 256 + param, which the
main chip assigns to each co-processor's controls at boot (types 2, 3 and 4,
sent to chips 1 and 2). Sending as chip 2:

| subcommand | value | effect | evidence |
|---|---|---|---|
| `01` | `00` | button down | Utility opens its menu |
| `02` | `08` | button up | a release is ignored unless value bits 2-3 are 2 or 3 |
| `0F` | signed 16-bit | encoder steps | Preset moves the menu cursor and changes preset |
| `03` | 16-bit | knob | Cutoff redraws "Cutoff ..."; scaling not established |

The press and release both reach `FUN_08043224`. Its value bits 0-1 select
the event: 0 a button edge, 2 and 3 a step down or up.

**Display.** Text for the display co-processor goes out on USART2 as type-7
frames to chip 15 (`AA F7 ...`): `70` starts a region, `72` carries four
characters, `71` up to three. `10` frames set LEDs. Capturing USART2's output
therefore reads the screen as text.

**Request and select lines.** `FUN_080211A8` polls one request line per
co-processor (PC4, PC9, PC12, PD14) and answers on a select line (PC8, PC10,
PC13, PD15). With the request lines low the main chip sends no display
updates after boot; with them held high it does.

**Global settings** are 96 bytes at external-flash `0x080000`, read at boot
into RAM (`0x200007E4` in 5.0.0.2084). Byte 0 is the MIDI receive channel; on
1.0.5 byte 31 is Mem protect (1 Factory only, 0 OFF). 1.0.5 and 4.0.3 erase
and rewrite an invalid page with their defaults on first boot.

## Working on this file

### Using Ghidra with this image

The project is **`MFC`** (`~/MFC.gpr`), imported with the invocation in
EMULATION.md. **`~/MF.gpr` is stale -- it holds a different, shorter binary
(386,895 bytes against `main.raw`'s 473,695) and every address queried against
it decodes the wrong bytes.** It cost most of 2026-09-10; see METHOD.md.

Any address in this map that was derived from Ghidra *before* 2026-09-10 and
never confirmed in the emulator should be re-checked against `MFC`. Addresses
confirmed by a firing emulator hook (`0x080454D4`, the boot milestones) are
sound regardless -- those came from the machine running the real image.

Helper scripts in `../ghidra/`: `MFBlocks.java` (memory blocks -- check the
initialised block's size against the file before believing anything),
`MFDisasm.java` (Ghidra's own disassembly, to compare against capstone),
`MFRedisasm.java` (clear and re-disassemble a mis-aligned region),
`MFRange.java` (disassemble an address RANGE -- `MFDisasm` prints a fixed eight
instructions, which is the wrong shape for reading a window of code next to its
literal pool), `MFCallees.java` (transitive callees with address ranges -- needed
to attribute a resource to a caller, since an allocation made inside
`LPCSpeechSynthWordBank::Init` belongs to `SpeechEngine::Init` and sorting sites
by address gets that wrong).

**The invocation is `analyzeHeadless <dir> <projname>`, so it is
`analyzeHeadless ~ MFC`, not `analyzeHeadless ~/MFC.gpr MFC`** -- the latter
fails with "Could not find project", which reads like a corrupt project rather
than a path error. Add `-process -noanalysis` to reuse the existing analysis
instead of re-importing. `capstone` is NOT a dependency of this project, so Ghidra or a
hand-rolled decoder is the available route -- `uv run --with capstone python`
pulls it in for a one-off disassembly without adding it; `/usr/bin/objdump` is Apple LLVM and has a `thumb` target but will not
take a raw binary.

### How to add to this file

Every render or protocol investigation should leave symbols behind. The two
techniques that produced most of the above:

- **Work backwards from the sink.** Read the DMA buffer address out of the
  peripheral registers, watchpoint it, and read the writing PC. That found the
  real audio tap after a long detour spent guessing forwards from the engine.
- **Diff two execution traces** (`cpu CreateExecutionTracing ... PC`) and
  cluster the unique PCs into functions. Walking back to the last shared PC
  gives the branch that decides between them.

Watchpoint hooks get `cpu.PC` and `value`; `self.GetRegister(n)` gives integer
registers. VFP registers are **not** reachable — `GetRegisters()` does not
exist. Read floats either from memory, or from the stack after a `vpush`
(`vpush {d8-d15}` puts s16 at `[sp]`, s19 at `[sp+12]`, s20 at `[sp+16]`).

### Open questions

**Check `FORMATS.md` before working any of these.** This list is not
automatically synced with it, and the sample-hash entry below sat here as "open"
for nearly a month after being solved there. Audited 2026-10-06: the other
entries were cross-checked and are genuinely open (Bank Select's *behaviour* is
pinned but its code location is not; `Vocoder`'s unreachability is confirmed and
only "is there another route" remains; sequencer bytes 8-11 are marked OPEN in
both files).

- ~~What selects `0x08030AA2` versus `0x0803261C` — the two voice paths.~~
  **No longer the live question. 2026-10-02.** Traced `172-HitGrains-v30`
  (digital zero in the emulator, one of the loudest presets on the instrument)
  against `247-HitGrains-v30` (audible in both), engine held constant: both hit
  `0x08030AA2` twice and neither reaches `0x0803261C`. Two hits means that
  address is not a per-block mix writer at all here -- it runs at init -- so the
  "silent presets take the first" labels came from the Speech investigation and
  **do not generalise**. They may be mis-ascribed in general; treat the two rows
  in the audio-path table as Speech-specific until re-derived.

  `mfrender.py --hook ADDR=LABEL` now exists for this kind of tracing and keeps
  Renode's log. **Hook cold code only** -- `0x0803099C` (`osc_render_wrapper`,
  583 logged hits) stalls boot past 300 s at ~100 us per hit.

- Why `s0` into `speech_formant_generator` is always `0.0`. It comes from the
  caller's own `s1` via `vmov.f32 s20, s1` at `0x0805E5D4`.
- ~~The seven-entry engine descriptor array is a *sub*-dispatch, not the 22-engine
  list.~~ **Partly answered 2026-10-04, advanced 2026-10-05**: vt0 is Chords and
  vt4 is Speech, both confirmed by the data tables they read; five remain, with
  leads recorded above. They are seven C++ vtables, and the
  structure is now mapped: `FUN_0805F2C4` registers seven engine objects at
  `voice+0x6E4..0x6FC` with the count 7 at `voice+0x724`, then calls vtable
  slot 0 (`Init`) on every one. Speech is index 5. **Which engines the other
  six are, and how the 22 selectable engines map onto them, is still unknown.**
  Full table in SPEECH.md.
- **Where Bank Select (CC#0) is handled.** Measured behaviour is pinned
  (`slot = bank*128 + program`, CC#32 ignored -- FORMATS.md), but the
  code that latches the bank and combines it with the Program Change is not
  located. Finding it would also settle whether a 4th bank is real.
- **What `param_2[7]` is in `FUN_0805f458`** -- the additive offset in the
  engine index calculation (`x = param_2[7] + (count-1)*knob`). It comes from
  the caller `FUN_0800d480`. Unidentified, and was the most likely explanation
  for `Vocoder` storing 20852 rather than a value near the top of the range.
  **Superseded as the leading explanation 2026-10-10**: 20852 is the literal
  the audio-input consumer compares against at `0x08039A90`, so the value is
  probably not a mis-scaled index at all but an exact magic the audio-in path
  tests for. See "The audio-in consumer is gated on the raw value 20852".
- ~~**The order of the engine object array at `obj+0x6E4`** -- built at init, and
  not known to match the knob/display order.~~ **ANSWERED 2026-10-04**, by
  reading `FUN_0805F2C4`. The array is seven pointers at `voice+0x6E4`, written
  in this order: `voice+0x560, +0x604, +0x150, +0x188, +0x000, +0x350, +0x20C`.
  It is **not** sorted by object address, so it is not an accident of layout.
  Index 5 (`voice+0x350`) is Speech. The count is a hardcoded `7` at
  `voice+0x724`; `voice+0x658` is the requested engine index and `voice+0x65C`
  the cached one, compared at `0x0805F53E` to skip a redundant switch.
- ~~**Whether `Vocoder` is reachable at all.**~~ **ANSWERED, and this entry was
  stale for a month.** It claimed engine selection is `index = value // 1579`,
  so bin 21 starts at 33159, past the 15-bit maximum, therefore `Vocoder` is
  unreachable. **Every part of that rests on a formula `microfreak/preset/fields.py`
  retracted on 2026-09-10**, which says so in its own comment: "This replaces
  four successive fitted formulas, every one of which was wrong."

  The measured table is the authority:

      ENGINE_VALUES["Vocoder"] = 20852      storage index 13, between Noise and Bass

  and `microfreak/preset/fields.py` states the reason the fits all broke: **storage order
  is not display order.** `Vocoder` is stored at index 13 and appears *last* on
  the Type knob, at display position 21. So it is selectable, by hand, on a
  stock instrument -- nothing is unreachable.

  Confirmed twice over on 2026-10-10: 20852 is also the literal the audio-input
  consumer compares against at `0x08039A90`, derived from the firmware with no
  knowledge of the table. See "The audio-in consumer is gated on the raw value
  20852".

  *Cost: this session wrote "strong new lead, not yet measured" into two
  documents before checking `microfreak/preset/fields.py`, which had the answer measured on
  hardware the whole time. Same failure mode as the sample-hash entry above.*
- ~~**The sample hash function.**~~ **ANSWERED 2026-09-11 and this entry was
  stale** — the answer has been in `FORMATS.md` ("The hash function:
  SOLVED") the whole time:

      SmpHash = sum(internal signed 8-bit bytes) & 0xFFFF

  A plain 16-bit sum of the sample's own PCM in the device's signed
  representation: no CRC, no shift, no length term. Confirmed on 30 distinct
  16-bit values, with the control showing the residual capture-vs-device
  disagreement is a permuted sample *order*, not a different sample set. So
  **any sample's hash is computable** and nothing has to be harvested from the
  device — the premise of this question was already obsolete.

  *Caught 2026-10-06 only because the owner said "I thought we already had
  sample hashes figured out" when it was proposed as the next target. It would
  have cost a session.*
- **Sequencer step bytes 8-11**, the 18-byte inter-sequence header (contains
  50 and 16, plausibly gate length and sequence length) and the 42-byte
  trailer. See FORMATS.md, "The sequencer region".
- **The 8 off-spec presets** in `captures/mcc-presets.jsonl` (2128-7488 bytes
  rather than 4088). Every corpus analysis silently excludes them.

