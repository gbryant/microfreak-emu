# The firmware update protocol — mapped 2026-10-13

**A MicroFreak firmware update sends no SysEx at all.** It is USB DFU over
control transfers on interface 0, and it cannot collide with any SysEx opcode we
claim, now or later. That is the headline, and it closes the question the
capture was taken to answer.

Source: `captures/firmware-update.pcapng` — a complete, successful update to
5.0.0.2084 captured with USBPcap on Windows, 88,252 packets over 249 s, device
attach through post-update re-enumeration. Decoded with `usbpcap.py` (there is
no `tshark` on this machine, and the existing `usbmidi.py` / `pcap_to_jsonl.py`
both shell out to it).

    python3 usbpcap.py transcript captures/firmware-update.pcapng
    python3 usbpcap.py verify     captures/firmware-update.pcapng \
                                  data/firmware/MicroFreak_Firmware_Update_5_0_0_2084
    python3 usbpcap.py midi       captures/firmware-update.pcapng

## No SysEx, measured not assumed

Across the whole capture there are **1,469,970 payload bytes and not one `0xF0`
outside the control pipe**. The only non-control traffic is 20 USB-MIDI event
packets, all Control Change, all device→host, all at t=249.3 — after the update
finished and someone touched the knobs. Zero SysEx-class event packets (CIN
4/5/6/7).

So an opcode we claim in the `tbh` table cannot be exercised by an update. The
risk that motivated the magic-argument guard (`PROTOCOL.md`, and MCC sending
`0x43` 93 times) is a *runtime MCC* risk, not an update risk.

Note the scope honestly: the capture begins at device attach and the update was
triggered 17.23 s later, with **nothing sent to the device in between** but
`SET_CONFIGURATION`. So the updater did not do MCC's usual SysEx identify/sync
handshake first. It is a different tool path, not MCC's normal session.

## The flow

```
   t=0.00   enumerate: 3 interfaces -- DFU + AudioControl + MIDIStreaming
                       bcdUSB 0x0200, bus-powered 250 mA
   t=17.23  ARTURIA_REBOOT_TO_DFU            bmRT 0x21  bRequest 0xF2
   t=18.56  re-enumerate: 1 interface -- DFU only
                       bcdUSB 0x0201, 400 mA   <- this is the bootloader
   t=18.61  SET_INTERFACE 0
   t=19.62  DFU_GETSTATE                 -> dfuIDLE
   t=19.62  ARTURIA_SELECT_TARGET 0           bmRT 0x21  bRequest 0xF4
   t=23.40  DFU_DNLOAD x7404                  473,760 bytes   (3.78 s erase gap)
            ... repeat SET_INTERFACE / GETSTATE / SELECT_TARGET / DNLOAD
                for targets 1, 2, 3, 4 ...
   t=222.99 ARTURIA_FINISH                    bmRT 0x21  bRequest 0xF3
   t=242.13 re-enumerate: 3 interfaces again, bcdUSB 0x0200 -- the new app
```

Nine request kinds shape the whole thing. Three are Arturia's own:

| request | bmRequestType | bRequest | what it does |
|---|---|---|---|
| `ARTURIA_REBOOT_TO_DFU` | `0x21` | `0xF2` | handled by the **running app**; marks flash and resets into the bootloader |
| `ARTURIA_SELECT_TARGET` | `0x21` | `0xF4` | `wValue` = which of the five images the next DNLOAD run carries |
| `ARTURIA_FINISH` | `0x21` | `0xF3` | end of update; the bootloader validates and boots the app |

Four are standard DFU 1.1: `DFU_DNLOAD` (`0x21`/`0x01`), `DFU_GETSTATUS`
(`0xA1`/`0x03`), `DFU_GETSTATE` (`0xA1`/`0x05`). The rest is plain USB —
`GET_DESCRIPTOR`, `SET_CONFIGURATION`, `SET_INTERFACE`.

**`DFU_DETACH` is never used**, which is why `0xF2` exists: Arturia replaced the
standard detach with their own request. `DFU_UPLOAD`, `DFU_CLRSTATUS` and
`DFU_ABORT` never appear either — there is no read-back of the installed
firmware in this protocol.

**Identification, corrected 2026-10-13.** This device is **`1C75:0601`**.
An earlier reading of this capture said `1C75:0106` -- `idProduct` read
big-endian -- and said `bcdDevice` changed between the two modes. It does not:
`bcdDevice` is `0x0200` in both, and the field that changes is **`bcdUSB`**
(`0x0200` -> `0x0201`), which is an odd thing to vary and a poor thing to depend
on. Tell the modes apart by **`bNumInterfaces`**: 3 for the application
(DFU + AudioControl + MIDIStreaming, 250 mA), 1 for the bootloader (DFU alone,
400 mA). Both errors were caught by `mfreak fw probe` finding nothing on a
connected instrument.

## The five targets

Selected in this order, each a separate `wBlockNum 0..n` run:

| `wValue` | blocks | bytes | `.mff` member |
|---|---|---|---|
| 0 | 7404 | 473,760 | `nanowave_main__fw5_0_0_2084__2023_05_30.bin` |
| 1 | 1278 | 81,712 | `nanowave_iochip_oled__fw1_0_0_1905__2023_05_29.bin` |
| 2 | 466 | 29,744 | `nanowave_iochip_matrix__fw1_0_0_1857__2023_05_29.bin` |
| 3 | 422 | 26,928 | `nanowave_iochip_kbd__fw1_0_1_1857__2023_05_29.bin` |
| 4 | 5122 | 327,744 | `userwavetable_factory_v1_3__20211015.bin` |

**Every one is byte-for-byte the shipped `.bin`, header included.** `verify`
checks this and all five match. The host recomputes nothing: it does not strip
Arturia's 64-byte header, does not recompute the XOR checksum, does not
rearrange anything. It opens the ZIP, reads a member, and pushes it out in
64-byte blocks — `wTransferSize` from the DFU functional descriptor is `0x0040`,
so block 0 *is* the file's own 64-byte header and block 1 is its first payload
word.

That is the practically important fact for `microfreak.firmware.image.package()` and
`microfreak/firmware/mff.py repack`: **get the 64-byte header right and the update path needs
nothing else.** The header layout is already in
`data/firmware/.../strip_headers.py` — `[0]` image index, `[1]` XOR checksum,
`[2:0x40]` zero, payload, `[-1]` a second copy of the checksum — and the wire
bytes confirm it: targets 0–3 begin `00 d2`, `01 ca`, `02 d0`, `03 95`. Target 4
is the wavetable bank, which carries its own `0x7981` magic instead.

The last block of each run is a **zero-length `DFU_DNLOAD`**, the DFU 1.1
end-of-transfer marker, which is why the block counts are one more than the
payload needs.

## Timing, and what paces it

| target | `SELECT_TARGET` (the erase) | download | per-block interval | rate |
|---|---|---|---|---|
| main | **3775 ms** | 74.0 s | 10.00 ms | 6.2 KB/s |
| iochip_oled | 690 ms | 38.3 s | 29.98 ms | 2.1 KB/s |
| iochip_matrix | 689 ms | 13.9 s | 29.95 ms | 2.1 KB/s |
| iochip_kbd | 690 ms | 12.6 s | 29.94 ms | 2.1 KB/s |
| userwavetable | 212 ms | 54.3 s | 10.61 ms | 5.9 KB/s |

**The erase happens inside the `ARTURIA_SELECT_TARGET` control transfer.** That
first column is the request's own duration, not idle time before block 0 — an
earlier version of this table called it an "erase gap", which made it look like
something the host chose to wait out. It is not: the device holds the transfer
open for 3.8 s on target 0. **libusb defaults to a 1000 ms control timeout, so
a tool that does not raise it dies exactly there**, which is what happened on
the first attempt to flash with `mfreak fw`.

**The device sets the pace, not the host.** This too was recorded backwards
here, on the grounds that every one of the 29,379 `DFU_GETSTATUS` replies
carries `bwPollTimeout = 0`. That is true, and the inference from it was wrong.
Measured per block, against the sum of the three control transfers' own
durations:

| target | interval | transfers | host idle |
|---|---|---|---|
| main | 10.00 ms | 9.80 ms | 0.20 ms |
| iochip_oled | 29.98 ms | 29.74 ms | 0.24 ms |
| iochip_matrix | 29.95 ms | 29.67 ms | 0.27 ms |
| iochip_kbd | 29.94 ms | 29.66 ms | 0.28 ms |
| userwavetable | 10.61 ms | 10.41 ms | 0.21 ms |

MCC inserts no delay at all — the time is inside the transfers.

**But that is not device time either, and this capture cannot tell you whose it
is.** `mfreak fw` wrote the same 473,760 bytes to the same instrument in about
**6 seconds**, of which ~3.8 s is the erase — so the download itself ran at
roughly **0.3 ms per block against MCC's 10.00 ms, a factor of 30**. USBPcap's
timestamps are taken in a kernel filter driver sitting in the URB path, so
"inside the transfer" includes the Windows USB stack and the capture overhead,
and nothing in the file separates those from the device.

So the honest statement is: **the 10 ms per block is per-transfer overhead in
that particular host environment** — a slower machine with Wireshark in the
path — and it is neither a device requirement nor a rate MCC chose. Two
successive readings of it here were wrong in opposite directions: first "the
host sets the pace" (inferring a deliberate sleep from `bwPollTimeout = 0`),
then "the device paces by latency" (inferring device time from where the
duration sat). The capture supports neither. An uncaptured host settles it.

What **is** device time: the erase inside `ARTURIA_SELECT_TARGET`. 3775 ms is
far too long to be stack overhead, it scales with the target the way flash
geometry would (3775 ms for the 512 KB application region, 690 ms for the
co-processors, 212 ms for the wavetable bank), and our own 6-second run still
spent about 3.8 s of it there.

Each `DFU_DNLOAD` is followed by exactly **two** `DFU_GETSTATUS`: the first
returns `dfuDNBUSY`, the second `dfuDNLOAD-IDLE`. No error status appears
anywhere in the capture — this is a clean run throughout, which also means it
says nothing about what the device does when something is wrong.

The 19 s between `ARTURIA_FINISH` and re-enumeration is the bootloader
finishing, validating, and starting the new application.

## The app side — who answers `0xF2`

The running firmware's own USB class driver, not the bootloader. The
`USBD_ClassTypeDef` is at `0x080763C0`, and its `Setup` slot is
`0x0805756C`. Decompiled, the class-request arm is:

| `bRequest` | what the app does |
|---|---|
| `0x01` `DFU_DNLOAD` | arms EP0 OUT into `pdev_class + 0x8B`, sets a flag at `+0x8A`, stores `wLength` and `wValue >> 8`. **The running app accepts downloads too** — never used in this capture |
| `0x81` | `memset(buf, 0, 0x40)` then sends `wLength` bytes back. Always returns zeros in this build |
| `0xF2` | `*(u8 *)0x2001E002 = 1` and returns. That is all it does |
| `0xF3`–`0xF5` | accepted, no-op — the bootloader's requests, which the app swallows rather than stalling |
| anything else | `USBD_CtlError`, stall |

Its standard-request arm answers `GET_DESCRIPTOR` for descriptor type `0x21`
(the DFU functional descriptor, capped at 9 bytes), `GET_INTERFACE` and
`SET_INTERFACE` (alt setting < 2), and stalls the rest.

So the device advertises DFU **all the time**, in its normal 3-interface
configuration, with `bInterfaceClass/SubClass/Protocol = FE/01/02` and a DFU
functional descriptor of `bmAttributes 0x0B`, `wDetachTimeOut 255`,
`wTransferSize 64`, `bcdDFUVersion 0x0110`.

## Under emulation, 2026-10-15

`microfreak-emu` can now drive this from the host side. `STM32_OTGFS.cs` grew
a `ControlRequest` entry point, so arbitrary class and vendor control
transfers can be issued after enumeration, and the bootloader can be loaded at
`0x08000000` so the machine boots the way the instrument does rather than
straight into the application.

**What runs.** `ARTURIA_REBOOT_TO_DFU` sent as a real control transfer --
`bmRT 0x21`, `bRequest 0xF2`, `wLength 0` -- is accepted by the application's
class driver and completes in 2.7 ms of wall clock. The boot path through the
bootloader works end to end: gate 1 reads PB10 low, gate 2 finds image 0's and
image 1's validity records intact in the data page, gate 3 hands over, and the
application reaches `main()`. After the request, the application resets and the
bootloader comes back up.

**The first phase runs end to end, and the end state is the proof.** After the
request the application stamps the data page and resets, and the device comes
back up in the **bootloader** rather than the application. That only happens
when gate 2 finds image 0's validity marker cleared, so where the core ends up
is evidence for the whole chain -- control transfer, request flag, page
rewrite, reset -- in a way no single memory read is.
`../tests/test_dfu_handoff.py` asserts exactly that, with the same run's
earlier arrival in `main()` as its control.

**Do not try to verify it by reading the data page.** It reads byte-identical
afterwards -- all 128 words -- and that cost most of a day. What happens is
that `erased sector 2` is logged **twice**: once by the application, and once
after the reset, so the bootloader evidently re-stamps the page on its way
into DFU. Both an erased page and a correctly programmed one predict
`0xFFFFFFFF` at the marker word, so reading the original `0xFFFF5AA5` is not
evidence that nothing happened -- and the intermediate states are gone before
a Monitor read can see them. A watchpoint does catch the write itself:

    WPVAL marker <- 0xFFFFFFFF

the right value, at the right address, from the CPU.

**Renode's stock flash controller does not program.** It handles the unlock
sequence and then refuses every program, announcing it on each attempt:

    flash_controller: Unhandled write to offset 0x10.
    Unhandled bits: [0] when writing value 0x1. Tags: PG (0x1).

Offset `0x10` is `FLASH_CR` and bit 0 is **PG**, the programming-enable bit.
With it in place no erase was ever logged at all. `renode/STM32F4_Flash.cs`
replaces it: the two-key unlock, sector erase over the F446's real sector map,
`FLASH_SR` handling so `FLASH_WaitForLastOperation` returns, and a
`ProgramWord` with the hardware's clear-bits-only rule behind a
`StrictProgram` switch. With ours the erase happens and is logged with the
right addresses.

**What that does and does not establish.** The erase demonstrably only works
with our model. It is NOT established that the first phase's end state needed
it: the device ended up in the bootloader both before and after the
replacement, so that outcome is not evidence for the controller. What the erase
made possible is the step after, and that step now works:
`../tests/test_dfu_download.py` drives `SELECT_TARGET` and `DFU_DNLOAD` and
the bytes land in flash. A clean A/B of the controller itself was not run,
because `midilink` loads the bootloader through the controller's own
`LoadImage` and reverting the model removes that method too; separating the
two is the next piece of work if the question matters.

## The download, 2026-10-15

`SELECT_TARGET 0` erases sectors 2 through 7 -- the factory data page and the
whole application region, `0x08008000` through `0x0807FFFF` -- and
`DFU_DNLOAD` programs what follows. Measured end to end:

    SELECT_TARGET 0   0x0800C000: 0x20020000 -> 0xFFFFFFFF
    DFU_DNLOAD        0x0800C000 = 0x43424140   the block we sent
                      0x0800C03C = 0x7F7E7D7C   its last word
                      0x0800C040 = 0xFFFFFFFF   nothing was sent there

**A `DFU_GETSTATUS` has to follow every block.** DFU 1.1 leaves the device in
`dfuDNLOAD_SYNC` after a block and the poll is what moves it to
`dfuDNLOAD_IDLE`; skipping it does not merely go faster, the next block is
refused and the download stops after one. The capture's two polls per block
were not redundancy.

**The first block is the HEADER, not image data.** Send two distinct blocks
and the SECOND lands at the image base. That is the capture's arithmetic as
well: 7,404 blocks for a 473,760-byte `.mff` member, which is Arturia's
64-byte container header, the 473,695-byte payload, and the single trailing
byte that repeats the header's XOR-8 (64 + 473,695 + 1 = 473,760 -- verified
against Arturia's own container 2026-10-17: the trailing byte is `0xD2`, it
equals `header[1]`, and it is the XOR-8 of the payload). So the bootloader
consumes block 0 as the header it validates and programs from block 1 onward
-- which is why `mfemu dfu --image` wants the container, header included, and
refuses a stripped payload.

**Why `LoadImage` rather than `sysbus LoadBinary`.** A binary loaded with
`LoadBinary` is re-applied every time the machine resets, and a firmware
update ends in `SYSRESETREQ`. Loading the bootloader that way therefore put
the original factory data page back on every reset.

One more thing that took two runs to find, recorded because it is the kind of
mistake that looks like a firmware bug: after `SYSRESETREQ` the core fetches
SP and PC from the vector table at the RESET value of `VTOR`, which is **0**.
The STM32F446 aliases main flash there; this platform did not, so the reset
locked the core at `PC 0xEFFFFFFE` and it read exactly like the bootloader
crashing. `microfreak.repl` now carries a `bootalias` region at `0x00000000`.
It is deliberately a SEPARATE region rather than registering `flash` at both
addresses: registering it twice gives `flash_controller`'s `flash:` reference
two registration points, and with that in place even the erase side stopped
working.

## The handoff: a marker in internal flash sector 2

> **Partly corrected 2026-10-13, after reading the bootloader.** The sector and
> the `0x5A`/`0xA5` byte values are right, but `0x08008000` is not "a record
> that marks a DFU request" — it is the **factory data page**: the device serial
> (identical to the USB serial string), five indexed records for the five DFU
> images, six validity slots, and a 240-entry calibration ramp. `0x5AA5`/
> `0xA55A` is a generic record-valid tag used throughout the page, and what the
> app clears on `0xF2` is image 0's **validity marker**, not a request flag. The
> bootloader reads those markers at `0x0800135C`. See `BOOTLOADER.md`.

`0x2001E002` is only a request flag. The main loop polls it at `0x080576A0`:

```c
if (*(u8 *)0x2001E002 == 0) return;
mark_dfu_request();              // 0x08045B58
delay_ms(500);
*(u8 *)0x2001E002 = 0;
DSB;
SCB->AIRCR = 0x05FA0004 | (SCB->AIRCR & 0x700);   // SYSRESETREQ
for (;;);
```

The 1.33 s between `0xF2` and re-enumeration in the capture is that 500 ms plus
reset and the bootloader's USB bring-up — an independent check that this is the
path that ran.

`0x08045B58` is where the persistence lives, and it is worth knowing about:

```
copy 0x200 bytes from 0x08008000 into a stack buffer
buf[0xB0] = 0xFFFF   (u16)
buf[0xB4] = 0x5A
buf[0xB5] = 0xA5
HAL_FLASH_Unlock()
HAL_FLASHEx_Erase({ TYPEERASE_SECTORS, bank 0, sector 2, 1 sector, range 3 })
for (i = 0; i < 128; i++) HAL_FLASH_Program(WORD, 0x08008000 + i*4, buf[i])
HAL_FLASH_Lock()
```

A Ghidra warning for this one: the project reports `0x08045B58` as living inside
`FUN_080244dc`, which is nonsense — `0x080244dc` is a two-instruction veneer
(`ldr r0, [pc]; b.w 0x08045F44`) whose tail call made Ghidra grow its function
body across 135 KB. The *reference* is right (`0x080576AA` is an
`UNCONDITIONAL_CALL` straight to `0x08045B58`); only the containing-function
attribution is wrong. This is the wrong-function-boundary trap
`../ghidra/README.md` warns about, caught in the wild.

So there is a **512-byte configuration record at `0x08008000`**, inside the
bootloader's own flash region below the application's `0x0800C000`, and stamping
`0xFFFF` / `0x5A` / `0xA5` into it at offsets `0xB0` / `0xB4` / `0xB5` is what
tells the bootloader to stay in DFU mode instead of chaining to the app. The
magic pair `5A A5` is the tell.

Two consequences:

* **The bootloader is reachable without MCC.** Any path that writes those bytes
  and resets gets you a DFU device. That is a recovery route for a bricked
  application image, and the reason to care is that custom firmware work has so
  far depended on the app being well enough to accept `0xF2`.
* **Requesting an update erases all of flash sector 2** (`0x08008000`–
  `0x0800BFFF`, 16 KB) and rewrites only the first 512 bytes. Whatever else
  lives in that sector is 0xFF afterwards. Either nothing does, or the
  bootloader tolerates it; the capture cannot tell which.

Confidence: the `0x0805756C` dispatch and the `0x08045B58` sequence are `D`,
read instruction by instruction. That the `5A A5` stamp is *what the bootloader
keys on* is `I` — the bootloader is at `0x08000000`–`0x0800BFFF` and is **not in
`main.raw`**, so nothing here reads that side of the contract.

## Flashing without MCC — `mfreak fw` (2026-10-13)

MCC is x86-only and Rosetta is being withdrawn, so the updater is on a clock
that has nothing to do with the instrument. The protocol above is nine request
kinds, so a replacement is a few hundred lines rather than a project.

    mfreak fw flash  update.mff --plan           # what would be sent
    mfreak fw probe                              # read-only
    mfreak fw enter                              # into the bootloader
    mfreak fw flash  update.mff --only main      # asks, then writes

> `verify` below is **not** in the released tool. It compared the plan against
> a `.pcapng` of MCC's own update, which needs a capture and the `usbpcap`
> reader, so it stayed behind as a development check. `mfreak fw flash --plan`
> is the half that shipped, and it prints the same sequence `verify` used to
> check.

**A flashing tool cannot be tested by trying it**, so it is tested against the
recording of a real update instead. `verify` builds the request sequence the
tool would send and compares it to MCC's, request by request:

    this tool: 44088 requests
    MCC      : 44088 requests
    0 unexplained
    939888 payload bytes compared
    IDENTICAL -- every DFU request, wValue and payload byte matches MCC

Five differences are reported and explained separately: all five
`SET_INTERFACE` requests reached the capture through
`URB_FUNCTION_SELECT_INTERFACE`, a URB Windows builds the setup packet for
itself, so USBPcap fabricates a stand-in with `bmRequestType = 0x00`. The
capture carries no wire evidence of that nibble and `0x01` is correct. Checked:
every `SET_INTERFACE` and `SET_CONFIGURATION` in the capture is synthesised;
every DFU request is a real `CONTROL_TRANSFER` or `CLASS_INTERFACE`.

The negative control matters as much: `verify` against `custom_fw_v14.mff`
reports **10** differing `DFU_DNLOAD` blocks, and those ten are exactly the
blocks holding v14's patch -- the header checksum, the table trampoline and its
two entries, the init detour, the `bank_4` descriptor, the three blocks of
handler/init-stub/peek, and the trailing checksum. So the comparison really is
reading payloads.

### Two things that make it safer than MCC

**It need not send all five images.** MCC always does. Only target 0 is the
firmware; 1-3 are co-processors and 4 is the factory wavetable bank.
`--target 0` writes the application alone -- 74 s instead of 193 s, and the
co-processors and wavetable bank are never touched.

**It checks before sending and stops on error.** Each image's own XOR checksum
and header image-index are verified against `info.json` before a byte goes out,
and every `DFU_GETSTATUS` is read: a non-`OK` status aborts immediately. MCC's
capture is all `OK`, so nothing there shows what the device does when something
is wrong -- which is a reason to check, not a reason to assume.

### Confirmed on the instrument, 2026-10-13 (read-only)

    found 1C75:0601  bcdUSB 0200  bcdDevice 0200
      serial       : <16 digits, redacted -- it identifies the instrument>
      configuration 1: 3 interfaces, 250 mA
        interface 0 alt 0  class FE/01/02  0 endpoints  <- DFU
        interface 1 alt 0  class 01/01/00  0 endpoints
        interface 2 alt 0  class 01/03/00  2 endpoints
    claimed interface 0
    DFU_GETSTATUS: stalled, as the application should
    class 0xF5   : accepted as a no-op, as the application should

Two things this settles.

**libusb can claim interface 0 on macOS 27 while the instrument is in normal
use.** This was the only real platform unknown -- `AppleUSBAudio` holds
interfaces 1 and 2, nothing claims a class `0xFE` interface, and the claim
succeeds with no kext unloading and no SIP changes. The MIDI port stays usable.

**The handler at `0x0805756C` is exactly the code running.** `probe` turns the
decompile into a control pair, both read-only: the application must *stall*
`DFU_GETSTATUS`, because that arm of `usbd_class_setup` reaches
`USBD_CtlError` for any class request that is not `0x01`, `0x81`, `0xF2` or
`0xF3`-`0xF5`; and it must *accept* class `0xF5`, which falls past the `0xF2`
test and the `> 0xF5` stall and returns having done nothing. Both behaved as
decompiled. MCC agrees about the first: all 29,384 of its status queries come
after the reboot, never before.

### The recovery question, and what the first test actually showed

`enter`, a power cycle, then `probe`, run on the instrument 2026-10-13:

    enter -> bootloader up: 1 interface, bcdUSB 0201
    (power cycle)
    probe -> application, 3 interfaces

**This is weaker evidence than it looks, and the framing it was run under was
wrong.** The test was proposed as "1 interface -> recoverable, 3 -> not". But
nothing had been erased, so the application in flash was **still perfectly
valid** -- and a bootloader that chains to a valid application is behaving
correctly whatever the marker says. The outcome is consistent with at least
three mechanisms:

1. the bootloader clears the `0x5A`/`0xA5` marker on entry;
2. the marker survives but the bootloader prefers a valid application;
3. the bootloader has an idle timeout and had already booted the application
   before the power cycle.

Only (3) is separable without writing anything -- `mfreak fw enter --watch 180`
does it, and the capture cannot, because MCC was continuously busy from 18.56 s
to 242.13 s and its longest idle in the bootloader was 4.8 s.

What the case that matters -- an **invalid** application -- does is still
unknown, and testing it directly means creating the situation we are trying to
avoid. Most bootloaders validate before chaining, which is exactly what
`ARTURIA_FINISH` sounds like it triggers, but "sounds like" is not a
measurement.

### The honest risk comparison

The thing that makes this tolerable is not that the risk is small in absolute
terms, it is that **it is the same risk MCC carries**:

* the request sequence is byte-identical, verified against MCC's own session;
* the protocol is the same, so the same bootloader does all the flash writing;
* nothing in this tool writes below `0x0800C000`, because nothing in this tool
  writes flash at all -- it feeds bytes to the code that does.

So an interruption here is the same event as an interruption of MCC, and
whatever recovery exists for one exists for the other. This tool does not make
the instrument easier to brick. In one respect it is better: it reads every
`DFU_GETSTATUS` and stops on a non-`OK` status, which MCC's capture gives no
evidence of doing.

### If certainty is wanted, the bootloader can be read

`peek` reads SRAM because `PEEK_BASE` is `0x20000000`, and three septets give a
21-bit offset -- 2 MB. A build with `PEEK_BASE = 0x08000000` would reach
`0x08000000`-`0x081FFFFF`, which covers the **whole** internal flash including
the bootloader at `0x08000000`-`0x0800BFFF` and the 512-byte record at
`0x08008000`. 12,288 words at roughly 10 ms each is about two minutes for a
complete bootloader dump, read-only, after which the validation question is a
disassembly rather than a gamble.

The ordering matters: flash that build with **MCC, while MCC still works**, and
use `mfreak fw` for everything afterwards.

### RECOVERY: hold SHIFT + ARP + OCT/MOD while powering on — 2026-10-13

**If an update is interrupted, hold SHIFT + ARP + OCT/MOD while powering the
instrument on.** The utility light comes on and the instrument is in DFU, ready
to re-flash. Verified on the instrument.

This section used to be titled "the one unknown that stands between this and a
brick" and said recoverability was not knowable from `main.raw`. True of
`main.raw` — but the bootloader can be read off the instrument with peek, and
it was, so the question is settled from its own code. **Three independent gates
have to pass before it boots the application:**

| gate | at | what it checks |
|---|---|---|
| 1 | `0x08001328` | `GPIOB->IDR` bit 10 (PB10). High → stay in DFU. **Reads no flash at all**, which is why the button combination works whatever a failed update left behind |
| 2 | `0x0800135C` | per-image validity markers in sector 2 — tag `0xA55A` and value `0x5AA5`, checked for images 0 **and** 1. An update that stops before `ARTURIA_FINISH` leaves them invalid |
| 3 | `0x0800133C` | the handover: the application's reset vector must have top byte `0x08`, or it returns and stays in the bootloader. An **erased** application reads `0xFFFFFFFF` and cannot be jumped into |

Full disassembly in `BOOTLOADER.md`.

**The one case still resting on gate 2 alone:** a download interrupted *after*
block 0. Block 0 is the vector table and the DFU blocks go out in ascending
order, so from then on gate 3 passes. Whether that is safe depends on whether
`ARTURIA_SELECT_TARGET` invalidates the marker before erasing, or whether only
`ARTURIA_FINISH` ever writes it — not yet attributed to a request. Gate 1 covers
it either way, which is the point of having a hardware path.

## Open

* ~~**The bootloader itself is unmapped.**~~ **Read 2026-10-13** — 48 KB of
  `0x08000000`–`0x0800BFFF`, off the instrument with a `--peek-base 0x08000000`
  peek build, two passes in opposite order agreeing word-for-word. The boot
  decision, the validity markers and the factory data page are all mapped; see
  `BOOTLOADER.md`. The DFU target table and the co-processor relay paths are in
  there and not yet attributed.
* **Does the bootloader verify anything beyond the XOR checksum?** No signature
  check is visible from the host side, and the clean run shows no rejection — but
  a clean run cannot demonstrate the absence of a check.
* **`bRequest 0x81`** returns 64 zero bytes in 5.0.0.2084. Either a stub or a
  capability this build does not implement.
* **The wavetable header's word at `+4`** is `0x00F5F000`. `strip_headers.py`
  calls it unknown and so does this. It is the right order of magnitude for a
  QSPI address (`FIRMWARE.md` has the user wavetable bases at `0xFAF000` and
  `0xF6F000`) but it matches neither, and target 4 is the *factory* bank, which
  `wavetable_fetch` reads from base 0.
