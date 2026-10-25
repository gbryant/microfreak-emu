# The MicroFreak bootloader, read out of flash — 2026-10-13

    data/firmware/bootloader/bootloader_5_0_0_2084.bin
    49152 bytes   0x08000000..0x0800BFFF
    sha1 ed1296ad6dfc8dd87d2f6e2d3ce7a63c3cdcaa42

**The `.bin` itself is NOT committed**, because `data/` is gitignored by
design. It is reproducible, and the sha1 above is the check:

    mfreak fw patch speech stock.mff --peek-base 0x08000000 --out fp.mff
    mfreak fw flash fp.mff
    # then the two-pass dump described below, and reflash custom_fw_v16.mff

That costs two flashes, so ask before assuming it is cheap to regenerate.

**Not from a file — read off the instrument with peek.** No Arturia release
contains the bootloader; the five `.mff` members are the application and four
co-processors. This is `mfreak fw patch speech --peek-base 0x08000000`, which
bakes that base into the handler's `orr`, and 12,288 word reads at ~335/s.

**Read twice, the second pass in reverse address order, and the two agree
word-for-word with zero unanswered reads.** That control is not optional:
`peek_reply` cannot detect a stale reply — every path out of the dispatcher
builds one, and a blocked peek echoes whatever the last successful read left in
the septet slots. A stale read returns the *previous address's* word, which
differs between a forward and a reverse pass. Flash is full of repeated values,
so "it looks plausible" proves nothing.

## Layout, and why it is a good one

| | |
|---|---|
| `0x08000000`–`0x08007FFF` | bootloader code (sectors 0+1, 16 KB each). Non-`0xFF` to `0x08007???` |
| `0x08008000`–`0x080081E6` | **factory data page** (sector 2, 16 KB) |
| `0x0800C000`– | the application (sectors 3–7: 16 KB at `0x0800C000`, 64 KB at `0x08010000`, then three 128 KB) |

Every region is sector-aligned on the STM32F446 map, so the data page can be
rewritten without touching code or the application. That matters: the validity
markers live there and are updated on every firmware update.

## It boots under emulation, 2026-10-15

With a dump of your own instrument's low 48 KB in `data/bootloader/`,
`microfreak-emu` boots from `0x08000000` and all three gates run for real:
PB10 reads low, both validity records are found intact in the data page, and
gate 3 hands over to the application, which reaches `main()`. Before this the
harness always entered the application directly, with the vector table pointed
at `0x0800C000` -- which is what the bootloader would have done, but meant
none of the gates below had ever actually executed.

The dump is not in this repository and never will be: it is Arturia's code,
and sector 2 holds the instrument's serial number. `mfreak peek dump-bootloader`
reads it off a device you own, over MIDI, using a `--peek-only` build's
nibble-addressed peek -- the only one that reaches flash. See
`docs/FIRMWARE_UPDATE.md` for what can and cannot be emulated once you have it.

## THE ANSWER: an interrupted update is recoverable, and there are three gates

This was the one assumption the whole flashing workflow rested on —
`mfreak fw enter --help` flags it as "the one thing standing between this tool
and a brick, and it is not knowable from `main.raw`". It is knowable from here.

`main` at `0x08001696` boots the application only if **all three** pass:

```
080016a8  bl 0x8001328      gate 1: the hardware pin
080016ac  cbnz r0, ...                 nonzero -> do NOT boot
080016ae  bl 0x800135c      gate 2: image 0's validity record
080016b2  cmp r0, #0x4                 != 4 -> do NOT boot
080016b6  movs r0, #0x1
080016b8  bl 0x800135c      gate 2: image 1's validity record
080016bc  cmp r0, #0x4                 != 4 -> do NOT boot
080016c0  movs r3, #0x1
080016ce  cbz r3, 0x80016da            any failure -> fall through to the DFU loop
080016d4  ldr r0, [pc, #0x44]          = 0x0800C000
080016d6  bl 0x800133c      gate 3: the handover
```

### Gate 1 — a hardware forced-DFU pin

```
08001328  push {r3, lr}
0800132a  bl   0x80012ec
0800132e  ldr  r3, [pc, #0x8]        ; = 0x40020400
08001330  ldr  r0, [r3, #0x10]       ; GPIOB->IDR
08001332  ubfx r0, r0, #0xa, #0x1    ; bit 10 -> PB10
```

`0x40020400` is **GPIOB** and `+0x10` is **IDR**, so this reads **PB10**. High
means do not boot the application. **This is a hardware escape that does not
depend on flash contents at all** — whatever state the application is in, PB10
gets you a flashable device.

**The control is SHIFT + ARP + OCT/MOD, held while powering on.** The utility
light comes on and the instrument is in DFU. Found in community documentation
and **verified on the instrument by the owner, 2026-10-13**. That is the
recovery procedure to publish, and it is what made a public release of
`mfreak fw` defensible.

Note the mismatch: PB10 is **one** GPIO and that is **three** buttons, so they
are not wired to it directly. The likely mechanism is that the **matrix
co-processor** (`nanowave_iochip_matrix`, DFU image 2) scans the panel, decodes
the combination and asserts a line into the main MCU's PB10, which the
bootloader samples — one pin, three buttons, and a separate always-powered
scanner. **That is an inference, not a measurement.** It is confirmable with a
`--peek-base 0x40000000` build, which can reach `GPIOB->IDR` at `0x40020410`
(the default SRAM build's 21-bit window cannot): poll bit 10 while the
combination is held. Not urgent — the procedure works regardless of which pin
carries it.

### Gate 2 — per-image validity markers in the data page

```
0800135c  cbz  r0, ...               ; arg 0 = image 0, arg 1 = image 1
0800136e  ldrh r2, [r3]              ; the TAG
08001370  movw r3, #0xa55a
08001374  cmp  r2, r3
08001376  bne  -> return 1           ; tag absent
08001378  ldrh r3, [r1]              ; the VALUE
0800137e  cmp  r3, #0xffff -> return 2   ; erased
08001382  cbz  r3          -> return 3   ; zero
08001384  movw r2, #0x5aa5
0800138a  cmp  r3, r2 ; bne -> return 0   ; anything else
0800138c  movs r0, #0x4              ; tag A55A + value 5AA5 -> VALID
```

| image | tag address | value address | read on this instrument |
|---|---|---|---|
| 0 (main) | `0x080080B4` | `0x080080B0` | `0xA55A` / `0x5AA5` → **4, valid** |
| 1 (oled) | `0x080080BC` | `0x080080B8` | `0xA55A` / `0x5AA5` → **4, valid** |

The page holds six such slots (`0x080080B0`–`0x080080D8`), alternating
`0x5AA5`/`0xA55A` with `0xFFFF` between — written as updates happen, which is
why they are in a sector of their own. Seven call sites write them
(`0x08001442`–`0x080014EA`, all in the manager function below `0x08001438`).

**So an update that stops before `ARTURIA_FINISH` leaves the marker invalid and
the bootloader does not boot the application.** That is the designed-in safety,
and it is why `enter` + a failed download is recoverable rather than fatal.

### Gate 3 — the handover will not jump into an erased image

```
0800133c  push {r3, r4, r5, lr}
0800133e  ldr  r5, [r0, #0x4]          ; the image's reset vector
08001340  and  r3, r5, #0xff000000
08001344  cmp.w r3, #0x8000000         ; top byte must be 0x08
08001348  bne  0x8001358               ; -> pop and RETURN, staying in the bootloader
0800134a  mov  r4, r0
0800134c  bl   0x8003d04               ; deinit
08001350  ldr  r3, [r4]                ; the image's initial SP
08001352  msr  msp, r3
08001356  blx  r5
```

One check: the reset vector's top byte. An **erased** application region reads
`0xFFFFFFFF`, so this gate alone makes "erased + a stale valid marker"
non-fatal. It is a weak check — **a download interrupted *after* block 0 has a
valid-looking vector table**, because block 0 *is* the vector table and the DFU
blocks go out in ascending order. That case rests entirely on gate 2.

Note what is absent: no `VTOR` write (`0xE000ED08` appears nowhere as a
literal), so the application sets its own vector table base.

## Corrections to what this repo recorded

**`0x08008000` is not "the 512-byte record that marks a DFU request".** It is
the **factory data page**:

```
08008000  "NNNNNNNNNNNNNNNN"            the device serial -- 16 ASCII digits,
                                        identical to the USB serial string.
                                        Redacted: it identifies one instrument
08008010  00 ff ff ff ff ff 5a a5       a record terminator (tag 0xA55A)
08008060  00 00 00 00 ff ff 10 27 ... 5a a5    five 16-byte records, index 0..4
08008070  01 00 00 00 ff ff 88 13 ... 5a a5    (the five DFU image numbers),
08008080  02 00 00 00 ff ff 10 27 ... 5a a5     values 0x2710 = 10000 / 0x1388 = 5000
08008090  03 00 af 94 7f 8a 10 27 ... 5a a5
080080a0  04 00 00 00 65 e6 10 27 ... 5a a5
080080b0  the six validity slots gate 2 reads
080080e0  a 240-entry monotonic 16-bit ramp, 0x0000 -> 0xF961, step ~0x1CF
          -- a calibration curve, not code
```

`0x5AA5`/`0xA55A` is a generic **record-valid tag** used throughout the page,
not a one-off DFU flag. `FIRMWARE_UPDATE.md`'s "the app marks flash sector 2
with `0xFFFF`/`0x5A`/`0xA5`" is the right sector and the right byte values, but
it is the image-validity marker being cleared, not a "please enter DFU" request.

**The bootloader's fault vectors are distinct**, unlike the application's:

```
Reset 0x080014F5  NMI 0x08001549  HardFault 0x0800154D
MemManage 0x08001551  BusFault 0x08001555  UsageFault 0x08001559
```

58 plausible vectors over 12 distinct targets. The application's 70 fault
vectors all point at one `b .`, which is why an application fault presents as a
total hang — the bootloader does not share that defect.

USB descriptor strings confirm what this is: `"MicroFreak Firmware Updater"`,
`"Arturia"`, `"DFU Config"`, `"DFU Interface"`, `"Device Memory"`.

## Still open

* **Whether PB10 really is the SHIFT + ARP + OCT/MOD line**, as opposed to the
  combination reaching DFU by some other route. The procedure is verified; the
  pin's attribution to it is not. One `--peek-base 0x40000000` build settles it.
* **Whether `ARTURIA_SELECT_TARGET` invalidates the marker before erasing**, or
  whether `ARTURIA_FINISH` is the only writer. Gate 2 protects the
  interrupted-download case either way *if* SELECT clears it first; if only
  FINISH writes, there is a window after block 0 where the old marker is still
  valid. Seven call sites are identified but not yet attributed to requests.
* ~~A safe empirical confirmation: `enter`, `SELECT_TARGET 0`, then stop and
  power-cycle.~~ **No longer needed.** That test existed to prove the recovery
  path without trusting the marker; the button combination proves it without
  touching flash at all, and does not leave the instrument without an
  application.
