# Formats: the bytes at rest

Every format the MicroFreak stores or exchanges as data: the flash layout, the
preset page and its three regions, the parameter stream, the mod matrix, the
sequencer, samples, wavetables, and MIDI Control Center's bank files.

`PROTOCOL.md` is the other half -- the messages that carry these bytes.
`FIRMWARE.md` is the code that reads and writes them. `ARCHITECTURE.md` is the
one-page picture to read first.

Dates on a heading are when that section was established. Nothing here is
inferred from another project's work; see `RELATED_PROJECTS.md`.

**What "verified" means in this file.** A format is called proven when a
round-trip has been demonstrated -- bytes written, read back by the firmware,
and the firmware's own behaviour confirming the interpretation -- not when the
bytes merely parse. Where that has not been done, the section says so.


## The image header, and what an address means here

Carried over from the preamble of the notes this file was assembled from,
because it is a format like any other.

The firmware images are wrapped in a **64-byte Arturia header**: `[0]` is the
image index, `[1]` is an XOR checksum of the payload, `[2:0x40]` are zero, the
payload starts at `0x40`, and the checksum is repeated as the final byte.
Strip that and the main image loads as raw ARM Cortex-M little-endian at
**`0x0800C000`**; the three I/O co-processor images load at **`0x08000000`**.
`src/mfemu/image.py` does exactly this, and `FIRMWARE_UPDATE.md` covers the
`.mff` container the headered images are shipped inside.

Every address in this file is a load address.

**Provenance.** The static analysis is of the v5.0.0.2084 main image
(`nanowave_main__fw5_0_0_2084__2023_05_30.bin`), cross-checked against the
`captures/send-single-preset.pcapng` and `captures/blank-all-presets.pcapng`
traffic. The point of separating this file from `FIRMWARE.md` is to keep what
is confirmed in the device's own code apart from what is inferred from
observed traffic.


## Where things are stored

### Storage addressing

Confirmed by reading the address arithmetic in each handler:

| What | Opcode | Flash address |
|---|---|---|
| Preset | `0x19` | `(slot + 0x81) << 12` -- i.e. `(slot + 129) * 4096` |
| Wavetable / sample region | `0x57`, `0x5B` | `(index + 0xF5F) << 12` or `(index + 0xFEF) << 12` |
| Directory-backed store | `0x17` commit path | `0xCD0000 + record.offset` |

Two independent confirmations fall out of this:

- **Preset blocks are 4096 bytes.** Every read/write is length `0x1000`, and
  the staging buffer is cleared with `memset(buf, 0xFF, 4096)` at
  `0x080450A4`. This corroborates [PRESET_FORMAT.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/PRESET_FORMAT.md)'s 146 chunks x 32 bytes
  = 4672 wire bytes -> 4088 decoded, inside a 4096-byte page.
- **`0xF5F000` is the user-wavetable region.** That exact value is the `u32`
  at offset 4 of the `userwavetable_factory_v1_3` firmware-bundle file header
  -- the file declares its own flash destination, and the handler computes the
  same address.

### The flash page layout, from the firmware (2026-09-05)

The 4096-byte flash page and the 4088-byte SysEx payload are **not** the same
thing, and that mismatch is why every emulator image built before this wrote a
preset the firmware then ignored or choked on.

The firmware keeps a table of region start offsets at **`0x0807531C`**:

    0x0807531C:  0, 32, 2012, 0, 0, ...        (u16 each)

`FUN_08044fe0(obj, n)` returns `table[n]` for `n < 3`. `FUN_0804500c(obj, n)`
returns the length: `table[n+1] - table[n]`, except `n == 2`, which returns the
literal `0x824`. That gives:

| region | page offset | length | contents |
|---|---|---|---|
| 0 | `0` | 32 | name / metadata record |
| 1 | `32` | 1980 | parameter TLV |
| 2 | `2012` | 2084 | sequencer and matrix data |
| | | **4096** | |

`FUN_08044fdc` is a bare `bx lr` -- the identity. So "the page buffer" is the
storage object itself, at **`0x200018B8`**, and `FUN_08045238(obj, slot)`
erases and programs all `0x1000` bytes of it to `(slot + 0x81) * 0x1000`.
`FUN_080454d4(obj, slot)` reads the same 4096 bytes back.

**The SysEx parameter stream fills regions 1 and 2 only.** `caseD_15` state 2
(`0x0804B0B0`) seeds the running write offset with `FUN_08044fe0(obj, 1)` = 32,
and `caseD_17` (`0x0804AF9E`) copies 28 decoded bytes per chunk from there to
the end of the page -- 4064 bytes. MCC sends 146 chunks x 28 = 4088 because 145
would be four bytes short; **the firmware consumes 4064 and discards the
24-byte tail**. So a decoded payload maps to page offset 32, truncated to 4064.

**Region 0 arrives separately, as the 45-byte `23 52` name record**, and it is
bytes `[12:44]` of that message verbatim. Three independent alignments fix the
slice:

| in the name record | in region 0 | what |
|---|---|---|
| byte 17 (`NR_SLOT_LO2`) | `+5` | slot index, low 7 bits |
| byte 20 | `+8` | high nibble is the payload schema version |
| byte 21 | `+9` | start of the NUL-terminated name |

`+9` is confirmed against the code, not just the corpus: `FUN_08054598`
(`0x08054598`) copies the 32 bytes of region 0 to the stack and calls `strcpy`
with `sp + 9` as the source. It also reads the word at `+0` and treats a
negative value as "slot empty" -- which is exactly what an erased `0xFF` page
gives, so a page carrying regions 1 and 2 but no real region 0 is silently
skipped. It reads the signed byte at `+6` and reduces it mod 12 (an `smull`
by `0x2AAAAAAB`); on all bank-0 presets that byte is 0.

This supersedes the guesses recorded in `../src/mfemu/mkflash.py` (`mfemu flash`): neither "payload at
offset 0" (the TLV gets read as a name, `strcpy` runs off the end and over a
live SAI DMA handle) nor "payload at offset 8" (the word at `+0` reads
`0xFFFFFFFF`, so the slot reads as empty) was right.

### The 128-slot directory (`0x200028BC`)

The store behind the commit path keeps a directory in RAM. Layout confirmed
by its constructor at `0x08046224`, which does `memset(B+12, 0, 3072)` then
writes `0xFF` at `+35, +59, +83, ...` (stride 24):

```
B      = 0x200028BC
B+0..11             12-byte header
B+12 + 24*i         record[i], i = 0..127        (128 * 24 = 3072 bytes)
   rec+0    u32     ordering key (the directory sort at 0x08046870 sorts on it)
   rec+4    u32     flash offset; data lives at 0xCD0000 + this
   rec+23   u8      status -- 0xFF means FREE
B+3084...           metadata: u16 @3084 (dirty slot), u16 @3086, u32 @3088,
                    u16 @3092 (cursor slot), u32 @3096 (byte cursor)
```

`B+3083` is `record[127]`'s status byte and `B+3084` is the first metadata
field, which is a clean arithmetic check on the stride and origin.

### Sample and wavetable storage (2026-09-11)

Measured by making the **emulated firmware write them** and diffing the flash:
`midilink.py` boots the machine, `mfreak sample write` / `mfreak wavetable write` send
over the emulated link exactly as they do to hardware, and a new
`quadspi SaveImage` reads the result back. Nothing below is inferred from
code; it is what the firmware produced. `mkflash.py` reproduces every byte of
it -- the control is a byte-for-byte comparison of `place_samples` /
`place_wavetables` output against the firmware's own writes, over the 4
samples and 3 wavetables of one ingest: **identical**.

#### Sample records: a chain from 0x281000

0x281000 is where the preset slots end (129 + 512 pages), and it is where the
boot-time scan of the sample region starts.

    +0x00  u32  the record's own flash address
    +0x04  u32  allocated size: header + audio, rounded up to a 4 KB page
    +0x08  u16  zero
    +0x0A  13B  name, NUL-padded
    +0x17  u8   slot index
    +0x18  ...  audio: 16-bit LE PCM at 32 kHz

The body is **zero-filled to the allocated size**, not left erased -- the
firmware writes whole 4116-byte transport blocks and the last one is padded.
The next record starts **one erased page after this one ends**
(`next = own + size + 0x1000`). The scan stops at 0xF50000.

The name field is really 13 bytes even though `mfreak sample write` accepts 18: a
14-character name writes its last character over the slot byte. Observed, not
theorised -- `ABCDEFGHIJKLMNOPQR` came back as `ABCDEFGHIJKLMN` with no slot
index behind it.

#### Wavetables exist in flash three times

    names                audio                what reads it
    0xF5F000 + n*0x1000  0xF6F000 + n*0x4000  the `WaveUser` engine
    0xFEF000 + n*0x1000  0xFAF000 + n*0x4000  written by MCC's upload
    (none known)         0x000000 + n*0x4000  the `Wavetable` engine

Audio is 0x4000 bytes per slot -- 8192 int16, exactly one `.mfw` payload, and
byte-identical to it. The name page is one 4 KB page per slot; the live copy
the firmware wrote is byte-identical to the factory one, so mirroring is
exact. Within a name page the name appears twice, at +0x0C and +0x28; the rest
of the page is constant across all 16 slots and is not decoded.

The firmware bundle's `userwavetable_factory_v1_3` image carries its own
destination (0xF5F000) and is 80 pages: 16 name pages then 64 audio pages.

**The copy at offset 0 is measured behaviour, not a decoded format**, and the
first version of this section got its layout wrong in a way worth recording.

With a note held on a `Wavetable` preset the flash model logs a read of page
0x000000 and nothing else. That much is solid. The first attempt then put 16
name pages at 0x000000 and the audio at 0x010000 -- copying the shape of the
bank at 0xF5F000, because that was the only wavetable-shaped thing to hand --
and it took the preset from silence to RMS 3561 with the Init control
unchanged. That was accepted as success. **It was wrong.** The engine was
reading the *name* pages as audio: the render's energy sat at the 18th
harmonic with a spectral centroid of 16 kHz, which nobody looked at, because
"it makes a noise where it used to be silent" had already been treated as the
answer.

The control that settles the layout is the other engine. `WaveUser` plays the
same 16 tables from 0xF6F000 and renders table 0 at RMS 5101.3, centroid
1542 Hz, partials at 260/263/523. With the audio at 0x000000 and **no name
pages in front of it**, `Wavetable` renders 5101.4, centroid 1527 Hz, the same
partials -- two independent engines agreeing on the same table to 0.002% of
RMS. So the layout is `0x000000 + n*0x4000`, audio only.

#### Decoded: the wavetable fetch, `FUN_080456A4`

Reading the firmware settles the placement, so it no longer rests on a render
matching another render:

    FUN_080456A4(obj, dest, 0x20, table, frame, cache_slot, user_flag)

        addr = (frame + table * 0x20) * 0x200;        // 0x4000 per table
        if (user_flag) {
            addr += ((*(ushort *)(obj + 0x1c5a) >> table) & 1) ? 0xFA0000
                                                               : 0xF60000;
            addr += 0xF000;                            // 0xFAF000 or 0xF6F000
        }
        FUN_08044260(handle, dest, addr, 0x400);       // 1 KB at a time

Three things fall out of those six lines:

- **Offset 0 is not a region we invented.** With `user_flag` clear the
  firmware adds *no base at all*, so the factory wavetables are addressed from
  flash offset 0 with a 0x4000 stride and nothing in front of them. That is
  the layout the `WaveUser` cross-check had already pinned, now confirmed by
  the code that computes it.
- **The two user bases are chosen per table**, by a 16-bit mask at
  `obj + 0x1c5a`: bit set picks the live copy MCC writes (0xFAF000), bit clear
  picks the factory-restore copy (0xF6F000). That is why `WaveUser` read
  0xF6F000 on an image whose live area was erased and 0xFAF000 once it was
  populated -- the same engine, the same call, a different bit.
- **The boot scan is the mask being built.** The 16 single-page reads at
  0xFAF000 spaced 0x4000 apart, logged at every boot, are the firmware
  checking which live slots are programmed.

What *writes* the offset-0 bank on a real device is still unknown -- MCC's
upload only ever touches the live copy, verified by diffing the flash after
one -- and it is presumably factory programming rather than anything reachable
over SysEx. But that is a question about provenance, not about whether the
placement is right.

#### The granular engines: the record's hash field is what they gate on

`Scan Grains`, `Cloud Grains` and `Hit Grains` rendered silence even with all
52 samples in the image, while `Sample` on the same image played -- and the
factory granular presets were silent too, which ruled out the generated
presets. The cause is the **u16 at record +0x08**, which an earlier reading of
this file called "zero".

It is the sample's **content hash** -- the same `sum(signed bytes) & 0xFFFF`
that `Gen/SmpHash` carries. Two independent lines of evidence:

- `mfreak sample write` already recorded MCC sending `a2 66` there for
  `Piano Pop` and `f5 2b` for `bee buzz`, calling it "something non-zero that
  is not derivable". Little-endian, `a2 66` is **0x66A2** -- exactly what the
  hash function gives for `Piano Pop`.
- The granular render, `FUN_08015c88`, caches the sample's length only when
  that field changes:

        iVar4 = FUN_08045150(table, *(byte *)(engine + 0x14b));
        if (*(char *)(iVar4 + 0x17) == -1) return;        // slot unused
        if (*(short *)(engine + 0x13c) != *(short *)(iVar4 + 8)) {
            *(short *)(engine + 0x13c) = *(short *)(iVar4 + 8);
            *(uint *)(engine + 0x140) = *(uint *)(iVar4 + 4);   // the length
            ...
        }

  With 0 in the record and 0 in the engine's cache the branch never runs, so
  the length at `engine + 0x140` stays 0, the grain scheduler takes its
  `fVar33 <= fVar32` no-grain branch, and the engine renders silence while
  running perfectly happily -- ~30 lookups a second, and not one flash read.
  `Sample` reaches its audio by a different path (caller 0x08016e49 rather
  than 0x08015ca1) and never notices.

Writing the hash into +0x08 takes all three granular engines from exactly
0.0 RMS to 1700-2660, and the factory granular presets with them
(`Slow Amen` 2070, `Trence` 1855, `PianoseK` 2058), with the Init control
unchanged at 2952.6. It also changes `Sample` from 356 to 1836: without a
hash it was finding *something* to play, but not the right thing.

So `Gen/SmpHash` is matched against this field, which is what "the hash
identifies the sample by content" means mechanically. A sample written into a
flash image without it is half-present: addressable, playable by one engine,
invisible to the three that granulate it.

#### The in-RAM sample directory

`sample_record_lookup(table, idx)` at 0x08045150 indexes a 128-entry table of
24-byte records at **0x200018B8 + 0x1010**, built by the boot scan:

    +0x00  u32  the record's flash address
    +0x04  u32  allocated size
    +0x08  u16  content hash
    +0x0A  13B  name
    +0x17  u8   slot index, 0xFF when the entry is unused

`idx` is the **slot** number, not the position in the flash chain: with the
factory bank's 52 samples at bank slots 1-23 and 100-128, entries 0-22 and
99-126 are populated and 23-98 read 0xFF. Dumped live from a booted machine.


## What a blank patch contains

Read off an instrument on 2026-10-15: panel 181 initialised and saved, then
pulled back with `mfreak preset read 180`. It is the reference for "what does
a default look like", and it settles a question that matters to anyone
generating presets.

    4,672 wire bytes -> 4,088 decoded
      region 1   1,980 bytes: a 1,188-byte TLV stream, then 0xFF to the end
      region 2   2,084 bytes, 1,048 of them non-zero -- sequencer and matrix
      tail          24 bytes, all zero
    20 module groups, 110 parameters, engine `Basic Waves`, Wave 25.6

`validate_loadable` reports no problems, and all 20 module tags are present
including `SyA` with zero parameters -- which is why `tlv.build` takes grouped
modules rather than flat triples.

**64 of the 110 parameter values are NON-ZERO.** That is the number to know.
A preset generated with zeros everywhere is accepted by the loader -- it
validates framing and never rejects a value -- and it is **silent**, because
among those 64 are `VCF/Cutoff` wide open, `EG1/Amount`, `EG2/Sustain` and
`Gen/Volume`. Zero them and the filter is shut, the envelope has no depth and
there is no output. The preset looks perfect and makes no sound, which is the
worst failure mode available.

So a generator should start from a blank patch as a TEMPLATE and overwrite
only what it means to change, which is what
`microfreak.preset.encode.build_payload` does. Region 2 is the concrete reason
its docstring says it carries over "the structural bytes nothing here
understands yet": 1,048 non-zero bytes of sequencer and matrix state that no
parameter table would reproduce.

For the record, switching that patch's engine to Speech and its Wave knob to
100 changes **five bytes** of 4,672 -- the same five a player changes by
turning the Type knob on a fresh patch. `src/mfemu/vehicle.py` does exactly
that to build the preset `tests/test_encoder_roundtrip.py` needs.


## The preset page

### The three regions of a preset block

A decoded 4088-byte block is not one structure but three:

| region | offset | contents |
|---|---|---|
| 1 | `0` .. ~`1040` | the TLV parameter stream |
| 2 | ~`1040` .. `1980` | `0xFF` fill |
| 3 | `1980` .. `4088` | 2108 bytes of sequencer data -- **not** TLV |

Region 1 ends with `0x20` (type 1, length 0) -- an end-of-stream marker,
distinct from the `0x40` (type 2, length 0) end-of-module. Region 3 starts at
1980 in 353 of 390 standard presets and 1984 in 29 more; the six outliers are
presets whose TLV stream runs long enough to leave no `0xFF` gap.

#### Sequencer: structure found, semantics partial

Region 3 is a flat array of **16-byte step records**, two sequences of 64
steps -- matching the hardware's two 64-step sequences:

```
steps   0..63    sequence 1
step    64       separator: ff ff ff ff ff ff ff ff 32 XX 00 01
steps  65..128   sequence 2 (offset by 2 bytes within the record grid)
```

Each step record:

```
bytes 0-3    four note slots   -- MIDI note number, or 0xFB / 0xFF for empty
bytes 4-7    four velocity slots -- 0x64 (100) is the default on empty steps
bytes 8-11   four bytes, purpose unknown (non-zero in presets with automation)
bytes 12-15  four flag bytes   -- values 0x01, 0x02, 0x04 seen
```

Four note slots per step is the paraphonic voice count. Confirmed by
inspection across presets: `Trance` carries three-note chords
(`2c 3c 43` with velocities `50 53 59`), `Punisher` a single note
(`18` velocity `75`), `Ether` mostly rests (`fb fb fb fb`).

**What is not known:** the meaning of bytes 8-15, whether the separator's
varying byte 9 is a length or a mode, how automation tracks are stored, and
what governs the 2-byte shift between the two sequences. This is a structural
map, not a decode -- enough to locate and copy a sequence, not to synthesise
one confidently.

### Region 0, byte by byte (2026-09-11)

Measured by reading all 512 name records off a 5.0.0.2084 device
(`read_preset.read_name_record`, bytes [12:44] of the 45-byte `23 52` reply).

    +0   bank bit 0x10 = (slot // 128) & 1, plus 0x08 = SLOT IS EMPTY
    +1..+4  zero on all 512
    +5   slot % 128
    +6   slot // 256
    +7   category, as chosen on the save screen (0x00-0x0B)
    +8   payload schema: high nibble 1 = no Voc module, 3 = with Voc
    +9.. NUL-terminated name

The bank is encoded twice over, in +0 bit 4 and +6; both agree on all 512.
The empty flag is what a built record leaves clear.

**+6 is `slot // 256`, and it contradicted the record table below until
2026-10-16.** That table gives record byte 18 -- which is region 0 +6 -- as
`(slot >> 7) * 2`, from `blank-all-presets.pcapng`. Both are accurate about
different things: `(slot >> 7) * 2` is what MCC SENDS, and `slot // 256` is
what the device KEEPS. They agree only in bank 0, which is where almost all
of this project's work has happened, so the disagreement went unnoticed.

Settled by asking the firmware. Writing a slot over SysEx with each value and
reading the record back:

    slot 200 (bank 1)   sent 0 -> device reports 0     slot//256 = 0
                        sent 2 -> device reports 0     (slot>>7)*2 = 2
    slot 318 (bank 2)   sent 1 -> device reports 1     slot//256 = 1
                        sent 4 -> device reports 1     (slot>>7)*2 = 4

The device normalises whatever arrives to `slot // 256`, so sending either is
harmless over SysEx. It is NOT harmless in a page written straight into a
flash image, which nothing normalises: `build_name_record` ended with a
`record[18] = hi * 2` left over from the retargeting path, so every preset
this project wrote outside bank 0 carried a wrong +6 -- measured as 21
bank-1 presets holding 2 in a built factory image. That line is gone.

MCC also sends +0 without the bank bit. All 512 records of a project send
(2026-10-22 capture) match `build_name_record` exactly, apart from the
sequence byte and name, and all 512 read-backs match its `stored=True` form.
SysEx writes now send MCC's form; `mkflash` and `vehicle` lay the stored form
into flash. Written to the instrument 2026-10-24 in slots 200 and 300, MCC's
form read back as the stored form and the presets played.

**Unsupported: "byte 20 of the name record tracks the engine."** That claim
(from the author's working notes, after the `0x33`-rejection retraction) does
not survive
contact with the device. Byte 20 of the record is region 0 +8, and it reads
`0x33` on **509 of 512 slots** -- the `Basic Waves` Init, the `Speech`
presets, and every empty slot alike. The three exceptions are `0x10`, and
they are exactly the three slots this project wrote earlier under the
*capture's* region 0. In the capture itself the byte takes six values and each
spans ten or more different engines. It is a schema/category pair, as
`mfreak preset write` already documented; it does not track the engine.

### Region 0 is built, not borrowed (2026-09-12)

`mfreak preset write` used to take its name record from the destination slot or
from `--from` (then spelled `--from-slot`), because the format was unknown.
That was a workaround with
a real failure mode -- a payload written under a header of the wrong vintage
does not render at all, silently, with the oscillator never called.

The layout below is enough to build one, and `build_name_record()` does. The
schema byte in particular is now derived from **the payload it describes**:
the TLV stream is parsed and the high nibble is 3 if a `Voc` module is present
and 1 if not, instead of being inherited from whatever happened to occupy the
destination. Verified on hardware: ten presets written with built headers, read
back, all ten carrying the right slot bytes, `0x33`, and the intended engine.
`--borrow-header` kept the old behaviour for cloning a preset whose category
should be preserved; it was removed on 2026-10-16, because the category is now
read from the source's own name record -- one exchange, already made -- and
inherited by default, which is what the flag was for.

The schema's low nibble is still not understood. Every one of the 509 saved
slots on a 5.0.0.2084 device reads 3, so 3 is what gets written.

### Presets: the empty flag in the name record

The 45-byte `23 52` name record is a standalone directory write.
`captures/blank-all-presets.pcapng` proves this: it contains **only** 512
`cat=23 op=52` messages and no `0x15`/`0x16`/`0x17` data transfer at all.

Record byte offsets (message-relative; args start at byte 9):

| Byte | Meaning |
|---|---|
| 9 | `slot >> 7` |
| 10 | `slot & 0x7F` |
| 12 | flags -- **bit `0x08` set means the slot is EMPTY** |
| 17 | `slot & 0x7F` (second copy) |
| 18 | `(slot >> 7) * 2` **as MCC sends it -- the device does not keep it** |
| 19, 20 | per-preset metadata, not reliably an occupancy flag (see below) |
| 21..43 | name, 23 bytes, NUL-padded |

Byte 12 bit `0x08` was verified across **1024 slot reads**: every one of the
574 records with the bit set is named `Init` (an empty slot); every record
with it clear is a real preset. Values `0x08`/`0x18` are empty, `0x00`/`0x10`
are occupied -- bit `0x10` is something else and appears in both states.

The slot encoding in bytes 9/10/17/18 was verified against **all 512** of
MCC's own name-record writes in `blank-all-presets.pcapng`: 512/512 match.

Bytes 19/20 look like they should be occupancy markers -- empty slots always
report `(0x00, 0x33)` -- but populated presets carry those same values
(e.g. `Yggdrasil`), so they are **not** a reliable flag and are passed
through unchanged rather than guessed at.

#### The bug this caused

`mfreak preset write`'s `patch_name_record()` -- removed on 2026-10-16, along
with the `--borrow-header` flag that reached it -- read the destination slot's
record and patched only the slot and name, preserving byte 12. Against a populated
slot the bit was already clear and everything worked; against a blank slot
the bit stayed set, so the device never allocated the directory entry and the
data went to the region base.

MCC clears it: in `send-single-preset.pcapng` the device reported slot 0 as
`byte12=0x08` / name `Init`, and MCC wrote back `byte12=0x00` /
`NervousKeys`. That capture is therefore a successful write to a blank slot,
and the fix is to do the same.

### Name record byte 12, fully decoded

```
bit 0x08   slot is EMPTY
bit 0x10   bit 7 of the slot index  (slots 128-255 and 384-511 set it)
```

Verified live across slots 0/1/5/50/100/126/127/128/129/200/255/256/300/383/
384/400/511: bit `0x10` tracks `(slot >> 7) & 1` with no exceptions. It is a
redundant copy of a slot bit, **not** a protection or bank flag -- which is why
it appeared in both empty and occupied records and resisted interpretation for
so long.

#### Scope of the protection check -- not fully established

`0x08054DC0` takes a **preset** object and reads its slot from `+2`. Whether
the same setting gates sample and wavetable writes is **untested**: those go
through different opcodes (`03 5D`/`5A`/`58` and `03 56`/`54`) and we have not
found an equivalent check on those paths, nor tried saving a protected sample
from the front panel. Do not assume samples and wavetables are covered, in
either direction.

What *is* established: SysEx bypasses the check on the preset path, verified
live.

#### Backing up before an unprotected write

Verified end to end on hardware:

```
mfreak preset read 5 --out backup.mfpz
mfreak preset write 5 --file backup.mfpz --yes
```

The `.mfpz` carries the payload, the name and the category, and `preset
write --file` restores all three.

### What makes a preset byte-valid, from the loader (2026-10-01)

The right question is not what the UI offers but what the *loader* accepts.
`FORMATS.md` already named the reader -- `0x0805522E`, one of the two
callers of the module-tag accessor -- and it sits in **`FUN_08054E08`**. Its
parse loop is the whole answer.

#### Hard requirements -- violate these and the load fails

The loop validates framing only, with `header = (type << 5) | length`:

| record | type | length | on mismatch |
|---|---|---|---|
| module tag | 1 | 3 | -- |
| parameter name | 2 | `strlen(name)` | `return 1` |
| value | 3 | **exactly 3** | `return 2` if the length is wrong, `return 1` if the type is |
| end-of-module | 2 | 0 | -- |

Those two error returns are the only ways a preset is refused.

#### Soft -- silently skipped, the value is lost

```c
module = FUN_080111fc(obj, tag);        // find_module_by_tag
id     = FUN_08054388(module, name);    // find_param_by_name
if ((id >> 8) == 0x1f) ...              // 0x1f = NOT FOUND sentinel
if (module >= module_count || not_found) break;
```

**Parameters are addressed by name, not by index.** The preset carries the
module tag and the parameter's short name as strings, and the firmware looks
the id up at load time. An unrecognised tag or name does not fail the load --
that parameter is skipped and its value discarded.

#### Values are never rejected, only clamped

The loader reads `*(ushort *)(buf + start + 1)` -- **byte 0 of the 3-byte value
is ignored, bytes 1-2 are a little-endian u16**. This independently confirms
the layout from the `VCO/Type` work of 2026-09-10 and the `Mat/Assign` work
earlier today, now from the parser rather than from a round trip.

The u16 then goes through that parameter's scaling code:

| code | effect |
|---|---|
| `0` or `0xee` | used verbatim |
| `1`-`0x7f` | `v' = (code*v + v) >> 15`, then **clamped to `code`** -- the code is the max |
| `0x80`-`0xd7` | forced to `0` |
| `0xe1`-`0xef` | right shift by 13 down to 1 |

So **any u16 0..32767 is acceptable for any parameter.** Out-of-range values
are scaled and clamped into the parameter's range; nothing is refused.

#### What this settles for generation

Validity is purely a property of the TLV framing and the name strings, both of
which `microfreak/preset/tlv.py` and `microfreak/preset/fields.py` already get right -- they
round-trip byte-exact against the hardware. Therefore:

- **Any value may be written into any field.** There is no value the loader
  will reject, so no validity reason to cap the mod matrix route count, the
  depths, or the assign target ids.
- The assign slots are not enum-limited. `FUN_08011258` gives ids `0x902`-`0x904`
  a dynamic case (stride `-4`, table `0x08062304`, keyed on the current engine
  via `FUN_0800E644`) rather than a fixed count, which is consistent with the
  target list depending on the engine's macro names.

**The remaining question is effect, not validity**, and that is an emulator
question: the modulation applier has no per-parameter whitelist, and the
per-voice module mask `0x000e01be` gates which modules it writes. Firmware
work on this is finished; the open item is one controlled render.


## The parameter stream

### The preset payload format, from the serializer (`0x08054A68`)

Found by looking for callers of the module-tag accessor: exactly two exist,
`0x08054B44` (writer) and `0x0805522E` (reader). The writer builds each
record's header with two bitfield inserts:

```
8054B4A  bl   0x0800C21C        ; strlen(tag)
8054B50  bfi  r3, r0, #0, #5    ; bits[4:0] = length
8054B58  bfi  r2, r3, #5, #3    ; bits[7:5] = type   (1 here = module)
8054B6C  strb r3,[r2,r5]        ; emit the header byte
8054B6E  adds r5,#1             ; ...then the name/value bytes follow
```

**`header = (type << 5) | length`.** That single line is the whole format, and
it dissolves both "magic constants" in PRESET_FORMAT.md:

| byte | type | len | meaning |
|---|---|---|---|
| `0x23` `'#'` | 1 | 3 | module tag -- every module tag is 3 chars, so it *always* encodes as `#` |
| `0x63` `'c'` | 3 | 3 | value -- every value is exactly 3 bytes, so it *always* encodes as `c` |
| `0x40` `'@'` | 2 | 0 | end-of-module terminator |
| `0x44` `0x46` `0x47` ... | 2 | n | parameter name of n chars (`0x44`=`Type`, `0x46`=`Cutoff`, `0x47`=`BendRng`) |

Neither `#` nor `c` is a delimiter. They are headers whose length field happens
to make them print as ASCII.

#### Verified against a real preset

Parsing `captures/send-single-preset.pcapng`'s payload with this one rule:
**264 records, zero parse errors**, stream ending cleanly at offset 2025 of
4088. Modules come out in exactly the firmware table order:

```
VCO VCF EG1 Kbd Arp LFO EG2 Gen Sys Mat Co1 Co2 Co3 Co4 Co5 Co6 Co7 SyA Seq
```

-- including **`SyA`**, which `decode_preset.py`'s `FIELD_ANCHORS` does not
have, and with `EG1` third / `EG2` seventh rather than adjacent. 97 parameters
extracted with their raw values.

#### The 3 / 5 / 8-byte "field widths" were a measurement artifact

All 97 values are **exactly 3 bytes**, header always `0x63`. There is no
per-field width and nothing to predict.

`decode_preset.py` measured the gap between name anchors, which swept up the
framing bytes:

- **3** = the 3 value bytes (a normal parameter)
- **5** = 3 value bytes + the `0x40` terminator -- i.e. the **last parameter in
  its module**. All nine of the fields PRESET_FORMAT.md lists as 5-wide
  (`BendRng` `Reso` `Amount` `PresAmp` `SeqOn` `Retrig` `Legato` `Volume`
  `Utility`) are confirmed to be exactly that, each followed by `0x63` then
  `0x40`.
- **8** = the same plus the following `0x23` module header and its 3-char tag,
  which is why `Assign3` and the `Co1`-`Co7` `Key` fields showed 8.

So PRESET_FORMAT.md's "Not yet understood: what determines a field's width" has
no answer because there was never a question -- the widths were an artifact of
anchor-gap measurement, not a property of the schema.

#### `microfreak/preset/tlv.py`

New: parses a decoded block with this rule and yields
`(module, name, value_bytes)`. It supersedes the anchor walk, so it does not
bail at `EPanel` or any other schema divergence -- there is no fixed field list
to diverge from.

#### Still open in the payload

- Bytes **2025-4088** are not TLV. Roughly half are non-zero, with a visible
  repeating ~8-byte structure -- consistent with PRESET_FORMAT.md's note that
  sequencer step/note/gate data has no known mapping.
- The 3 value bytes' **meaning** is still the `microfreak/preset/fields.py` model's
  business; the TLV framing says nothing about scaling.

### Preset format: what the firmware string tables answer

The v5 image carries the parameter vocabulary as plain ASCII at
**`0x08062778`-`0x08062FF4`**, laid out as short-tag / display-name pairs
(`Reso`/`Resonance`, `RiseLvl`/`Rise Time`, `Parafon`/`Paraphony`), followed by
the enum value tables and the oscillator engine table. This is the same
vocabulary `decode_preset.py` sees in a decoded payload, so several
PRESET_FORMAT.md gaps close by inspection.

**Caveat on all of it:** these are string tables. Order is memory order, which
almost certainly matches index order but is *not* verified against a raw
value -> name mapping. Treat the names as authoritative and the ordering as
strong evidence, not proof.

#### `EPanel` is `Panel`

At `0x080629B0`, sitting between `Parafon`/`Paraphony` (`0x0806299C`) and
`PolyCnt`/`Para Voices` (`0x080629B8`). Its neighbours are the paraphony
fields, which is direct support for PRESET_FORMAT.md's leading hypothesis that
`GenGParafon` gates it. Not proof -- adjacency in a string table is not the
same as a conditional in the schema -- but it says where to look.

#### The unmapped groups now have names

PRESET_FORMAT.md lists `Sys`/`Utility` and the sequencer as having "no known
mapping at all". The firmware names them:

- **Sys/Utility**: `Save`, `Utility`, `Shift`, `ShfLED`, `MatEnc`, `MatBtn`,
  `Assign1`-`Assign3`, `KyMdSrc`, `PrsetID`/`Preset Id`, `PrsetBt`,
  `SmpHash`/`SH`, `HissMod`, `HissVol`/`Hiss Volume`
- **Panel buttons/LEDs**: `Button1`-`Button8` (`SyASw1`-`SyASw8`),
  `Led1`-`Led8` (`SyALed1`-`SyALed8`)
- **Sequencer**: `GateLen`/`Gate Len`, `Smooth1`-`Smooth4`
  (`SqSmot1`-`SqSmot4`)
- **Voice/chord**: `PrstVol`/`Preset Vol`, `UniSprd`/`UniSpread`, `UniCnt`,
  `Chord`, `ChOffs1`-`ChOffs3`/`ChordOffset1`-`3`

#### Enum value tables (replaces guesswork in `microfreak/preset/fields.py`'s SWITCH)

| Field | Values, in table order |
|---|---|
| Scale | `Major` `Minor` `HarmoMinor` `Dorian` `Mixolydian` `Blues` `Penta` |
| Division | `1/1` `1/2` `1/2T` `1/4` `1/4T` `1/8` `1/8T` `1/16` `1/16T` `1/32` `1/32T` `8/1` `4/1` `2/1` |
| Arp direction | `Up` `Order` `Random` `Pattern` |
| LFO shape | `Sin` `Tri` `Saw` `Sqr` `SnH` `SnHF` |
| Filter type | `LP` `BP` `HP` |
| Glide / mod | `Off` `Switched` `Pass` `Time` `Linear` `Aftertouch` `Run` `Loop` `Idle` |
| Chord offsets | `-3` `-2` `-1` `+1` `+2` `+3` |
| Root note | naturals plus `C#` `D#` `F#` `G#` `A#` |

The mod-matrix source names also appear -- `EG1`/`CyclEnv`, `EG2`, `LFO`/`Lfo`,
`Xpr`, `Key`/`Key/Arp`, with `Dest`/`MatDest` -- matching the `CEG1c` `CEG2c`
`CLFOc` `CXprc` `CKeyc` tags `MOD_MATRIX_WIRE` was derived from empirically.

#### Oscillator engines: real names, replacing the interpolated table

PRESET_FORMAT.md notes `_OSC_TYPE_ENGINES` was rebuilt from a knob sweep with
only **one** raw value confirmed (64 -> Speech) and the rest interpolated by
`round((i+1)*127/22)`. The firmware carries the engine names *and* each
engine's own parameter labels, at `0x08062DBC`-`0x08062FB8`:

```
BasicWaves   Morph, Sym, Sub          KarplusStr   Bow, Decay
SuperWave    Wave, Detune             V.Analog     Waveshaper, Asym
Wavetable    Table, Position          Two Op. FM   Ratio, Feedback
Harmo        Content, Sculpting       Formant      Interval, Formant
Chords       Inv/Trsp, Waveform       Speech       Timbre, Word
Modal        Inharm                   Noise        Balance
Vocoder      Bandwidth                Bass         Saturate, Fold
SawX         SawMod                   Harm         Spread, Rectify
WaveUser     Bitdepth, Start          Scan Grains  Scan, Density, Chaos
Cloud Grains                          Hit Grains
```

That is 21 engines plus `Sample` (`SmpIdx`/`Sample` at `0x08062800`) = 22,
matching the count from the physical sweep. It also explains why `Param1`-
`Param3` exist as generic tags with per-engine aliases (`VcoPar1`, `ParaX1`,
`VcoPaX1`): the same three preset bytes mean different things per engine.

#### Oscillator engine encoding: MEASURED on hardware (2026-09-10)

The `VCO`/`Type` record body is three bytes and every previous reading of it
was wrong. **Bytes 1-2 are a 15-bit value like every other CONTROL field**;
`microfreak/preset/fields.py` read byte 2 alone and discarded byte 1.

    value = ((body[2] & 0x7F) << 8) | body[1]

**RETRACTED: the count-scaled model.** `engine_from(count, raw)` scaled the
engine table by byte 0 on the theory that a preset saved under 12 engines
should be read against a 12-engine table. The device does not do that. It
decodes every preset against **its own** engine list regardless of byte 0.
The stored value is a *fraction of the engine list*, so the same fraction
lands on a different engine when the list grows -- which is exactly why old
presets display as the wrong engine on newer firmware.

    engine: look it up in ENGINE_VALUES -- see the measured table below

Byte 0 is the saving firmware's engine count. It is *not* used to rescale on
read; it is only useful for recovering authorial intent.

**CORRECTED AGAIN 2026-09-10 (third pass) -- and then replaced entirely by
the decompiled logic below. Read "Engine selection, from the code" further
down; the bin-division model in this section is WRONG.** It fails on two
hardware points: `Vocoder` saves as 20852 and `Bass` as 22341, which the bin
model puts in adjacent bins 13 and 14, while the knob places them at display
positions 21 and 13. No monotonic function of the stored value reproduces
that. Kept only to show what was tried.

**CORRECTED 2026-09-10 (second pass).** The first model here was
`floor(value * 21 / 32768)`, with an encode of `floor((index+1) * 32768 / 22)`.
Both agree with the corrected model for indices 0..16 and diverge from 17 up.
Every engine confirmed at the time sat at 16 or below, so the extrapolation
looked perfect until a write intended as `Sample` (17) came back off the panel
as `WaveUser` (16). Refitting against all fifteen panel points gives a plain
bin division with width w, constrained to 1577.06 < w <= 1580.84:

    index = value // 1579

Encoding must use the **bin centre**, `int((index + 0.5) * 1579)`, which is
correct for any width in that interval. The old `(index+1)*32768/22` form
lands near a boundary and is wrong above index 16 -- do not reuse it.

Bin 21 starts at 33159, past the 15-bit maximum, so **only indices 0..20 are
selectable: 21 engines, not 22**, even though `byte0` reads 22. Index 20 was
confirmed as `Hit Grains`, so the table order is right and `Vocoder` is simply
not reachable through this field. Whether it is selectable by another route is
OPEN.

**Ground truth.** Presets were written to a 5.0.0.2084 device, the engine read
off the **panel**, corrected by the owner on the device, and read back. The
(value -> engine) pairs below have names that came from the panel, not from
this repo:

| value | panel engine | index |
|-------|--------------|-------|
| 10922 | Waveshaper    |  6 |
| 13653 | Formant       |  8 |
| 21845 | Bass          | 13 |
| 27306 | Sample        | 17 |
| 30036 | Cloud Grains  | 19 |
|  7447 | KarplusStrong |  4 |
| 11915 | Two Op. FM    |  7 |
| 14894 | Chords        |  9 |
| 16384 | Speech        | 10 |
| 26810 | WaveUser      | 16 |
| 27632 | Sample        | 17 |
| 29211 | Scan Grains   | 18 |
| 32369 | Hit Grains    | 20 |
| 1489  | Basic Waves   |  0 |

Fifteen panel-confirmed points in total. **Thirteen of the 22 table entries
are now hardware-confirmed** (indices 0, 4, 6, 7, 8, 9, 10, 13, 16, 17, 18,
19, 20); the other nine names remain interpolated from the knob sweep.

One preset in the same batch decoded to index 16 when it was expected to
be `Harmonic` (index 3). It was **not** used in the fit, because its engine
name never came off the panel -- and the owner later confirmed it was a
mis-selection while resaving, not a decoder disagreement. Recorded here only
so the sighting is not mistaken for evidence later; index 16 was confirmed
separately afterwards, by the `26810 -> WaveUser` row above.

The table *order* is correct; only the scaling was wrong. The remaining names
are still interpolated and unverified. (An earlier revision of this paragraph
gave a second, lower count of hardware-confirmed entries that contradicted the
list above it. The list above is the one to trust, and the authoritative
account of what is confirmed and how is "Engine selection, from the code"
below.)

Note the decode divisor is **21**, not 22, which lines up with the firmware
string table above listing 21 engines plus `Sample`. Index 21 is not
reachable by the encode formula (it would need value 32768); whether the
22nd engine is selectable at all is **open**.

Re-decoding the capture with this model changes the engine of **344 of 390**
presets. 138 land on `Hit Grains` because every preset saved at the top of a
shorter engine list maps to the top of the current one.

**This also reconciles the `Raverz` contradiction.** `Raverz` (value 16384,
byte0 12) was *saved* as V. Analog -- what the owner read in 2026-09-03 --
and *displays* as Speech on current firmware, the same as `Snit` at the same
value. Both observations were always right; the docs had conflated "what it
was authored as" with "what it shows now".

#### Engine values: MEASURED (2026-09-10) -- use this, not a formula

One preset saved per engine from the Init on a 5.0.0.2084 device. These are
the device's own values and they are exact.

| idx | engine | value | | idx | engine | value |
|-----|--------|-------|-|-----|--------|-------|
|  0 | Basic Waves   |  1489 | | 11 | Modal        | 17873 |
|  1 | Superwave     |  2979 | | 12 | Noise        | 19362 |
|  2 | Wavetable     |  4468 | | **13** | **Vocoder** | **20852** |
|  3 | Harmonic      |  5958 | | 14 | Bass         | 22341 |
|  4 | KarplusStrong |  7447 | | 15 | SawX         | 23831 |
|  5 | V. Analog     |  8936 | | 16 | Harm         | 25320 |
|  6 | Waveshaper    | 10426 | | 17 | WaveUser     | 26809 |
|  7 | Two Op. FM    | 11915 | | 18 | Sample       | 28299 |
|  8 | Formant       | 13405 | | 19 | Scan Grains  | 29788 |
|  9 | Chords        | 14894 | | 20 | Cloud Grains | 31278 |
| 10 | Speech        | 16384 | | 21 | Hit Grains   | 32767 |

Evenly spaced by 32768/22; the top entry is clamped to the 15-bit maximum.

**STORAGE ORDER IS NOT DISPLAY ORDER.** `Vocoder` is stored at index 13,
between `Noise` and `Bass`, but appears **last** on the Type knob at display
position 21. Everything else matches. This single discrepancy is what broke
four successive fitted models: `Vocoder`'s 20852 looked like it collided with
`Bass`, and the apparent "22 engines but only 21 addressable" contradiction
came from fitting a display-ordered table to storage-ordered values.

`microfreak/preset/fields.py` keeps both: `ENGINE_ORDER` (storage) and
`_OSC_TYPE_ENGINES` (display, what the panel shows and what a user means).
`ENGINE_VALUES` is the table above; `set_osc_engine()` uses it directly.

**BUG, fixed 2026-09-11: `osc_engine()` was still using the retracted
model.** The measured table replaced the bin division in `set_osc_engine()`
but not in `osc_engine()`, so this repo *wrote* one encoding and *read*
another. Every preset stored at indices 13..16 came back one position wrong:
`Vocoder` read as `Bass`, `Bass` as `SawX`, `SawX` as `Harm`, `Harm` as
`WaveUser`. It was caught by a round-trip assertion while building the
per-engine survey -- `set_osc_engine(rows, "Vocoder")` read back as `Bass` --
not by inspection, which is the argument for asserting the round trip in any
builder. `osc_engine()` now goes through `engine_from_value()`. Re-decoding
the capture moves **54 of 391** labels; any engine label in this repo written
before 2026-09-11 should be re-derived, not trusted.

**Decoding an arbitrary value is approximate.** Nearest-measured-value is the
best available, but selection is hysteretic (below), so a value near a
boundary has no single correct answer -- the device's answer depends on what
was selected before. Decoding is exact only for values the device wrote.

#### Engine selection, from the code (2026-09-10)

Decompiled from `FUN_0805f458`, the oscillator entry, reached via
`FUN_0800d480` (`MFRefs` confirms the call; `../renode/NOTES.md` already had the thunk).
This supersedes every fitted formula above.

```c
count = *(int *)(obj + 0x724);                     // engine count
x     = (float)param_2[7] + (count - 1) * knob;    // knob is 0..1 from the preset
cur   = (float)*(int *)(obj + 0x658);              // currently selected index
bias  = (x < cur) ? -0.25 : +0.25;                 // hysteresis toward current
index = (int)(x + bias + 0.5);
clamp(index, 0, count - 1);
engine = *(int **)(obj + index * 4 + 0x6e4);       // array of engine objects
```

Consequences, and why four fitted models all failed:

* The scale is `(count - 1) * knob`, i.e. `21 * value/32768` at count 22.
  That part of the third model was right.
* **There is an additive offset, `param_2[7]`** (the int at `param_2 + 0x1C`),
  which no fitted model accounted for. Where it comes from is OPEN; it is the
  most likely explanation for `Vocoder` storing 20852.
* **The mapping is history-dependent.** The +-0.25 bias points toward the
  *currently selected* index, so near a boundary the same stored value
  resolves differently depending on what was selected before. **No pure
  function of the stored value can be correct there** -- a lookup table of the
  device's own saved values is the only reliable encoder.
* The engine array at `obj + 0x6E4` is in construction order, which is not
  known to match the knob/display order.

Values the device itself wrote, which are safe to reuse (name from the panel):

| engine | value | | engine | value |
|--------|-------|-|--------|-------|
| Basic Waves | 1489 | | Vocoder | 20852 |
| KarplusStrong | 7447 | | Bass | 22341 |
| Two Op. FM | 11915 | | | |
| Chords | 14894 | | | |
| Speech | 16384 | | | |

Probe values that read back correctly on the panel but were written by us, not
the device: WaveUser 26053, Sample 27632, Scan Grains 29211, Hit Grains 32369.

#### The value-record format: `[range, lo, hi]`

Every TLV value record is three bytes: a **range byte** and a 15-bit value
(`((body[2] & 0x7F) << 8) | body[1]`). Measured across 390 capture presets
and the device's own Init.

`byte0 == 0` means a continuous parameter. Otherwise it is
**(number of positions - 1)**, and the value uses an *inclusive* spacing:

    value = round(i * 32767 / byte0)          i = 0 .. byte0

Confirmed with 100% of observed values matching on 17 fields, including
`VCO/BendRng` (byte0=24 -> **0..24 semitones**), `Seq/Length` (60 -> 61),
`Seq/GateLen` (80 -> 81), `Gen/PrstVol` (24 -> 25), `Kbd/PresAmp` (10 -> 11),
`Kbd/Scale` (8 -> 9), `Arp/Range` (3 -> 4), `Voc/HissVol` (20 -> 21), and the
on/off pairs `EG2/Legato`, `Kbd/Veloc`, `Seq/Smooth1..4`, `Mat/KyMdSrc`.

**`VCO/Type` is the exception** -- byte0 is the engine count and the value is
bin-divided, not inclusively spaced. See the engine section above.

**The oscillator macros are engine-dependent.** `byte0 = 238` marks a
continuous macro; a small value means a discrete selector with `byte0 + 1`
positions, and it varies with the engine the preset was *saved* as:

| as-saved engine | `VCO/Param1` byte0 | meaning |
|---|---|---|
| Wavetable | 15 | 16 wavetables |
| WaveUser | 15 | 16 user wavetables |
| Chords | 10 | 11 chord types |
| Superwave | 3 | 4 waves |
| all others | 238 | continuous |

So sweeping `osc_wave` continuously on those four engines lands *between*
positions. Read the range from the preset's own byte0, never from a table.

Only **2 of 110 fields** drift across firmware generations (the corpus spans
engine counts 12, 13, 14, 17, 18 and 22): `VCO/Type`, whose byte0 is the
engine count, and `VCO/Param1`, which is engine-dependent rather than
version-dependent. Everything else is stable.

#### `Gen/ChOffs1-3`: chord offsets are semitones

    semitones = value / 128 - 128           range -128 .. +127

All 510 values across the 170 presets that carry these records are exact
multiples of 128, with no exceptions, and they decode to real voicings --
`(4,7,11)` major 7th, `(5,9,16)`, `(7,16,24)`, `(1,5,8)`. `(0,0,0)` is unison
(157 presets) and `(127,127,127)` appears to be "unused".

#### `Mat/Assign1-3`: a pointer to a parameter

    value = (module_index << 8) | parameter_index

`module_index` indexes the firmware module table at `0x08062478`
(VCO=0, VCF=1, EG1=2, Kbd=3, Arp=4, LFO=5, EG2=6, Gen=7, Sys=8, Mat=9,
Co1..Co7=10..16, SyA=17, Seq=18, Voc=19).

`parameter_index` is **0-based into the module's registration order**, which
is NOT the serialisation order and includes parameters that are never written
to a preset -- `VCO/ParaX1..3`, `VCO/Pitch`, `EG2/Amount`. That mismatch is
why byte-position tables never worked for this field.

Recovered by scanning the firmware for pointers into the parameter-name string
regions (`0x08062470-0x08062BA0`, `0x08075A70-0x08076240`); each registration
pool lists its module's parameters in order. The full tables are in
`microfreak/preset/fields.py` as `ASSIGN_PARAMS`.

Hardware-confirmed on 8 points: Assign1 set to Filter Cutoff -> `0x0101`,
Filter Resonance -> `0x0102`, Osc Timbre -> `0x0003`; and indices 1..6 of VCO
read back off the panel as `VcoPar1, VcoPaX1, VcoPar2, VcoPaX2, VcoPar3,
VcoPaX3`. **All 1170 assign values in the capture resolve**, and the top
destinations are what a musician would pick: `VCO/Param3` (Shape, 220),
`Co4/EG2` (envelope->cutoff amount, 213), `EG1/Amount` (176), `LFO/Rate` (52).

#### Sample selection: `Gen/SmpHash` decides, `VCO/SmpIdx` is a cache

    Gen/SmpHash  content hash of the sample -- AUTHORITATIVE
    VCO/SmpIdx   258 * (sample_number - 1) -- a cached position

Measured: writing a valid hash with a deliberately wrong `SmpIdx` (hash for
sample 33, index for sample 1) still selected **sample 33**. A hash that is
not present on the device falls back to the first sample, which is also what
`SmpHash = 0` does -- and `SmpHash=0, SmpIdx=0` is a real saved state, seen on
`Into Reverse` and `Fragments Bomb`.

The hash identifies the sample by content, so a preset survives the sample
list being reordered. The cost is that a sample can only be referenced if its
hash is known -- which used to mean harvesting hashes from the target device,
because the capture's 30 (hash, index) pairs are only partly transferable: on
the owner's freshly-loaded factory set, positions 33 and 6 matched the capture
and position 8 did not.

#### The hash function: SOLVED (2026-09-11)

    SmpHash = sum(internal signed 8-bit bytes) & 0xFFFF

A plain 16-bit sum of the sample's own PCM, in the signed representation the
device stores. Nothing more: no CRC, no shift, no length term.

**How it was found.** The owner exported a whole factory sample bank
(`.mfsbz`, 128 slots, 52 of them in use -- see the bank-export section below),
which gives the sample *content* for the first time. Summing the unsigned
bytes of each sample reproduced the low byte of the known hash for 7 of 7
samples -- a 1-in-2^56 coincidence -- while every high-byte candidate failed
(CRC-16 across five polynomials x init x reflection x xorout, Fletcher-16 mod
255 and mod 256, FNV-1a, djb2, java31, Adler, CRC-32 halves, weighted sums).
Summing the **signed** bytes instead fixes the high byte too: the low byte is
unchanged mod 256, which is why the unsigned sum looked half-right.

**The control.** All 30 (hash, index) pairs in the capture are present in the
owner's bank *by content*: 7 at the same index, 23 at a different one. So the
function is confirmed on 30 distinct 16-bit values, and the residual
disagreement between capture and device is entirely a **permuted sample
order**, not a different sample set -- which is exactly what the partial
transfer above was.

Consequence: any sample placed in a flash image can now be referenced from a
generated preset by computing its hash. Harvesting from the device is no
longer required.

#### Authoring trap: a device Init under a capture header does not render

Presets built from the device's current-schema Init and written into the
emulator under the *capture's* older region-0 headers **do not render at
all** -- not an init-patch sound, silence, with the oscillator never called.
Measured 2026-09-10: a 22-preset sweep image produced no audio and no
oscillator entry, while the stock mfrender image rendered normally and hit
the same code 47,804 times in 2 s.

`presetlib.py` already warned that a header/payload schema mismatch "comes out
sounding like an init patch"; this is the stronger form. Any generator that
authors from a device Init must write a matching region 0, or it will produce
silence and look like a broken emulator.

#### The sequencer region

The payload tail is, aligned from the **end** of the 4088-byte payload:

    [0xFF/0x00 pad] [seq 1: 64 x 16 B] [18 B header] [seq 2: 64 x 16 B] [42 B trailer]
                     at -2108                          at -1066

Each 16-byte step:

| bytes | meaning |
|-------|---------|
| 0-3   | note per voice 1-4; **255 = empty** |
| 4-7   | velocity per voice (100 = untouched default) |
| 8-11  | zero in a recorded monophonic sequence -- UNIDENTIFIED |
| 12    | **1 = step active**, 0 = empty |
| 13-15 | zero |

Confirmed by recording C3-E3-G3-C4 into a known-Init preset: the four steps
came back as notes 60, 64, 67, 72 with per-hit velocities and flag 1, steps
4-63 untouched, and the only TLV change was `Arp/SeqOn`. **The device labels
MIDI 60 as C3**, an octave below the common convention. 248 of 390 capture
presets parse under this layout.

OPEN: step bytes 8-11; the 18-byte inter-sequence header (it contains 50 and
16, plausibly default gate length and sequence length); the 42-byte trailer;
and why 142 presets do not fit the alignment.

#### Bank Select: CC#0 works, CC#32 is ignored (2026-09-10)

A Program Change data byte is 7-bit, so `C0 nn` alone reaches only slots
0-127. **Bank Select MSB (CC#0) selects the bank**; the LSB (CC#32) is read
and discarded by this firmware. Measured by hooking the preset page read
`FUN_080454D4` and reading the slot argument out of `r1`:

    plain     PC 10   -> loads slot  10
    CC0  = 1, PC 10   -> loads slot 138    (1*128 + 10)
    CC0  = 2, PC 10   -> loads slot 266    (2*128 + 10)
    CC32 = 1, PC 10   -> loads slot  10    (no effect)
    CC0  = 0, PC 20   -> loads slot  20    (bank state resets)

    slot = bank * 128 + program

Bank state persists until changed. Community sources say "CC#0 or CC#32";
for this firmware only CC#0 works. `mfrender.py` now sends `B0 00 <bank>`
before every `C0 <program>`, which retires its slot-borrowing workaround for
presets that live above 127.

#### Preset names are capped at 14 characters

The name record reserves 23 bytes (`NAME_FIELD_LEN`) and `mfreak preset write`
validates against that, but the device stores only **14**. Measured: a write
of `'WrongNumber Fix'` (15) read back as `'WrongNumber Fi'`, while
`'Glass Loom Fix'` (exactly 14) verified clean. The payload was byte-identical
in both cases -- only the name truncates, so a verify failure with matching
data means the name was too long.

#### Not answered by the firmware strings

- **Field widths** (3 / 5 / 8 bytes). The descriptor metadata is not a flat
  table -- `0x0800F2AC`-`0x0800F2F0` is a literal pool of name pointers inside
  a registration function, so widths and ranges are code immediates. Getting
  them means reversing that function, not reading a table.
- **The mod-matrix per-byte value formula** and the two
  `KNOWN_BAD_MOD_MATRIX_BYTES` pairs.
- **The 8/399 non-standard-length presets.**
- **Sample name chunk decoded `[8:10]`** and **decoded `[0:4]`** (always 0).
- **Preset name record bytes 19/20**, and byte 12's `0x10` bit.

### One value model for the whole preset

`CONTROL`, `SWITCH` and the mod matrix all use the **same** encoding. A field's
TLV value is three bytes forming a 24-bit little-endian fixed-point fraction
of full scale:

```
display = int24 * 100 / 2**23          (signed for the mod matrix)
```

`preset_fields`' 15-bit path is the same number seen through a smaller window:
`multibytes_value` builds `(MSB7 << 8) + LSB7 + (borrowed_bit << 7)`, which is
exactly `int24 >> 8`, and `raw_to_display(raw) = raw * 1000/32768` rounded is
identical to `int24 * 100 / 2**23` rounded, since `2**23 / 2**8 == 2**15`.

Checked on real values: `VCF/Cutoff` `00 da 3c` gives 47.5 both ways,
`VCF/Reso` `00 68 51` gives 63.6, `EG1/Amount` `00 82 51` gives 63.7.

So there is no separate "mod matrix formula" -- there is one fixed-point
format, and the mod matrix is the signed case.

### `CONTROL` addressing: two distinct faults

Comparing every `CONTROL` field's wire reading against the TLV record it
should equal, over the 390-preset corpus:

**Fault 1 -- a wrong borrowed bit.** `osc_wave` and `osc_timbre` disagree by
**exactly -128** on 161 and 175 presets respectively. -128 is the borrowed bit
(`msb_bit << 7`) read as 0 when it should be 1, i.e. their `msb` entry points
at the wrong 7-to-8 header bit. This is a plain bug, not layout dependence:
the affected presets are simply those where that bit happens to be set.

**Fault 2 -- layout dependence.** Several fields agree on most presets and
then fail with large arbitrary deltas on a minority: `envelope_attack`,
`envelope_decay`, `envelope_sustain`, `arp_seq_rate_free`, `arp_seq_rate_sync`,
`lfo_rate_free` each match ~91% of presets, `lfo_rate_sync` only 68%. That is
the same failure mode that broke the mod-matrix tables -- fixed byte positions
against a block the device reflows.

Fields that match **100%** of the corpus: `filter_cutoff`, `filter_resonance`,
`glide`, `osc_shape`, and all six `cycling_env_*`. Those are the ones safe to
rely on today, and they include three of the four ever tested on hardware.

#### Derived name mapping

Matching wire readings against TLV records across 120 presets recovers the
mapping automatically:

```
cycling_env_rise/fall/hold/amount/rise_shape/fall_shape -> EG1 RiseLvl FallLvl Hold Amount RiseSlp FallSlp
filter_cutoff/filter_resonance                          -> VCF Cutoff, Reso
osc_wave/osc_timbre/osc_shape                           -> VCO Param1, Param2, Param3
glide                                                   -> Kbd Glide
envelope_attack/decay/sustain                           -> EG2 Attack, DecRel, Sustain
arp_seq_rate_free/sync, lfo_rate_free/sync              -> Arp Rate/Div, LFO Rate/Div
```

Two caveats on that table: `osc_type` matches no TLV record (it is the one
`CONTROL` entry with `MSB: None`, read as a single byte), and `spice` and
`arp_seq_swing` both vote for `Kbd/Hold`, which cannot be right for both --
the vote is confounded when several fields hold the same value. `Arp/Spice`
and `Arp/Swing` exist as records, so at least one of those two wire entries is
misaddressed.

#### Not settled by disassembly

Worth being clear about method: the firmware's parameter table
(`0x08062778`, see above) confirms the *names* and gives the MIDI CC map, but
it describes the runtime parameter table, not the serialised addressing. The
faults above were found by corpus comparison against the TLV parser, not by
reading code. Fixing them properly means moving `get_field_raw`/`set_field_raw`
onto TLV name lookup, the way the mod matrix already was -- a contained change,
but one that wants the full name map pinned down first.

### `CONTROL`/`SWITCH` moved onto TLV addressing

`get_field_raw`/`set_field_raw` now look fields up by name in the TLV stream.
Public signatures are unchanged -- entries are identified by object identity,
so callers still pass `PF.CONTROL["filter_cutoff"]` -- and the old
`[chunk, offset]` code survives as `_get_field_raw_wire`/`_set_field_raw_wire`
for anything not yet in `FIELD_TLV` (currently nothing).

**`FIELD_TLV` covers all 33 `CONTROL` + `SWITCH` fields.** The mapping was
derived by matching wire readings against TLV records across the corpus and
then checked semantically against the firmware's own parameter names.

#### Why the old addressing had to go

Whether a table entry is correct is a property of the **preset**, not the
field. Measured on the 390-preset corpus: `filter_resonance`, `glide` and
`cycling_env_amount` agree with their TLV record on *precisely the same 332
presets*; `envelope_attack` and `lfo_rate_free` on *precisely the same 139*.
Fields in the same region of the block share an agreement set exactly. That is
positional drift -- a fixed table can only ever match one layout, and the
device writes several.

On top of that, `osc_wave` and `osc_timbre` were off by exactly -128 on 161
and 175 presets: the borrowed bit read as 0 when it should be 1, a plain
wrong-`msb`-entry bug independent of layout.

#### Verified

- All 33 fields read on all 391 standard presets: **no failures**, every value
  in 15-bit range.
- Write/read round trips exact for `filter_cutoff`, `osc_type`, `paraphonic`,
  `cycling_env_hold`, `lfo_shape`; block length preserved at 4672.
- Live on hardware: slot 5 reads cutoff 47.5, resonance 63.6, `osc_type` ->
  `Harm`.

#### Two details worth knowing

- **`osc_type` is the record's high byte alone**, not the 16-bit pair --
  confirmed against the old reading on 391/391 presets. It is the one field
  where the old wire path was right everywhere, because it reads a single
  positionally-stable byte.
- **The 15-bit read masks the high byte** (`(b2 & 0x7F) << 8 | b1`), matching
  `multibytes_value`. Without the mask, `lfo_rate_sync` overflows 15-bit range
  on 67 presets. Writes preserve `b2`'s top bit rather than clearing it.

### The registration function, reversed (`0x0800EB10`)

**What it is:** the runtime parameter-table initializer, not a preset-format
descriptor. `0x080616A8` -- which looked like a registrar -- is plain
`strncpy`. The function fills several *parallel arrays* (structure-of-arrays,
not array-of-structs) in one big object addressed off `r4`:

| array | stride | contents |
|---|---|---|
| CC | 1 | MIDI CC number, `127` = unassigned |
| tag | 8 | short tag (`Cutoff`, `Reso`, `Param1`) -- `char[8]` |
| display | 12 | display name (`Resonance`, `VcoPar1`) -- `char[12]` |
| value | 2 | 16-bit default, computed via `0x0800E4FC` |

Each module has its own set of these arrays. For VCF, for example: CC at
`r4+989`, tags at `r4+1005` (+8), displays at `r4+1033` (+12), values at
`r4+1060` (+2). The strncpy sizes come in as `r9`=8 and `r8`=12, which is
where the 8/12 field widths are confirmed.

The generated code per parameter looks like:

```
movs r2,#23              ; MIDI CC
strb r2,[r4,#989]
movw r1,#32767           ; default (pre-conversion)
bl   0x0800E4FC          ; -> 16-bit stored value
strh r0,[r4,#1060]
ldr  r1,=0x08062810      ; "Cutoff"
addw r0,r4,#1005
bl   0x080616A8          ; strncpy(tag, "Cutoff", 8)
ldr  r1,=0x08062810
addw r0,r4,#1033
bl   0x080616A8          ; strncpy(display, "Cutoff", 12)
```

#### The MIDI CC map, extracted from firmware

Every extracted value matches Arturia's published CC assignments, which is a
strong check on the extraction:

| Parameter | CC | | Parameter | CC |
|---|---|---|---|---|
| `Type` (osc type) | 9 | | `Hold` (cyc env) | 28 |
| `Param1` (wave) | 10 | | `Amount` (cyc env) | 24 |
| `Param2` (timbre) | 12 | | `Rate` (arp/seq) | 91 |
| `Param3` (shape) | 13 | | `Div` (arp/seq) | 92 |
| `Cutoff` | 23 | | `Spice` | 2 |
| `Reso` | 83 | | `Attack` | 105 |
| `Rise Time` | 102 | | `DecRel` | 106 |
| `Fall Time` | 103 | | unassigned | 127 |

`ParaX1`-`ParaX3`, `VcoBend`, `Pitch`, `SmpIdx`, `Rise Shape`, `Fall Shape`,
`Preset Vol`, `Preset Id`, `Gate Len` all carry `127` -- reachable in a preset
but not over CC.

#### Module table (`0x08062478`)

4-byte tags, indexed by module id. `0x080111F0` is
`module_tag(i) -> &table[i*4]`; `0x080111FC` is `find_module_by_tag(name)`
(strcmp loop, count at `[obj+588]`). Firmware order:

```
VCO VCF EG1 Kbd Arp LFO EG2 Gen Sys Mat Co1 Co2 Co3 Co4 Co5 Co6 Co7 SyA Seq Voc ?
```

Two things `decode_preset.py`'s `FIELD_ANCHORS` does not have: **`SyA`** (the
panel button/LED group -- `SyASw1`-`8`, `SyALed1`-`8`) and **`Voc`**
(vocoder). The order also differs from the assumed one: firmware puts `EG1`
third and `EG2` seventh, with `Kbd`/`Arp`/`LFO` between them, not `EG1`/`EG2`
adjacent. Worth checking against a decoded payload -- a wrong module order is
exactly the kind of thing that would make a schema walk diverge.

#### What it does NOT answer

**Preset payload field widths (3 / 5 / 8 bytes) are not here.** This function
initializes the *runtime* table. The 8- and 12-byte figures above are the
in-RAM name field sizes and have nothing to do with the `<tag>c<value>`
encoding in a saved preset. Finding the width rule means locating the
serializer that emits `#<module>` / `<tag>` `c` `<value>` into the 4096-byte
block -- a different function, not yet found. `0x63`/`0x23` are too common to
grep for usefully.

### Are there different preset formats?

Three separate axes, and only one of them is a real "format".

#### 1. Framing: one format, unchanged across firmware versions

`header = (type << 5) | length` is identical in 1.0.5.327 and 5.0.0.2084. The
module table is at `0x0804F3C0` in 1.0.5 and `0x08062478` in 5.0.0, and the
first **19 entries are identical and in the same order**:

```
VCO VCF EG1 Kbd Arp LFO EG2 Gen Sys Mat Co1 Co2 Co3 Co4 Co5 Co6 Co7 SyA Seq
```

#### 2. Vocabulary: additive by firmware version

5.0.0 adds the `Voc` module (vocoder) and the parameters `SmpIdx`, `UniSprd`,
`HissVol` -- the sampler / unison-spread / hiss features of the 5.x update.
1.0.5 has none of them; nothing was removed. So an old preset is a strict
subset, not a different encoding.

Because the stream is self-describing, `microfreak/preset/tlv.py` reads both without
knowing which firmware wrote them. Only *interpretation* needs the version.

#### 3. Content: variable per preset -- and this is what broke the anchor walk

Only records that exist get emitted. There is no fixed field list. This single
fact accounts for three separate puzzles in PRESET_FORMAT.md:

- the **"expanded vs compact" mod-matrix layout** and its ~1126-byte reflow --
  a preset with real connections simply carries more `Co<N>` records
- the **8 of 399 non-standard-length presets** -- more or fewer records means a
  different length; nothing is corrupt
- **`EPanel` appearing in only 38 of 399** -- a conditional record

None of these is a format variant. They are one variable-length stream.

#### `EPanel` is not a field name

The leading letter on every `FIELD_ANCHORS` entry is the **record header byte**
being read as text. For type-2 (name) records the header is `0x40 | length`,
so the letter literally encodes how many characters follow:

| header | char | name length | example anchor | real name |
|---|---|---|---|---|
| `0x43` | `C` | 3 | `CEG1` | `EG1` |
| `0x44` | `D` | 4 | `DReso`, `VCODType` | `Reso`, `Type` |
| `0x45` | `E` | 5 | **`EPanel`** | **`Panel`** |
| `0x46` | `F` | 6 | `FCutoff`, `FParam1` | `Cutoff`, `Param1` |
| `0x47` | `G` | 7 | `GBendRng`, `GenGParafon` | `BendRng`, `Parafon` |

So `EPanel` = a 5-character field called `Panel`, and `GenGParafon` = module
`Gen` + a 7-character field `Parafon`. The mod-matrix tags decode the same way:
`CEG1c` is `C`(len 3) + `EG1` + `c`(the value header), not a tag named
"CEG1c".

Confirmed live: parsing our captured preset yields `Gen` = `Parafon`, `Panel`,
`PolyCnt`, `PrstVol`, `Volume`, and `Co1` = `EG1`, `EG2`, `LFO`, `Xpr`, `Key`
-- exactly the five mod-matrix sources, with no invented names.

### Corpus validation: 398 presets from `captures/mcc-presets.jsonl`

`microfreak/preset/tlv.py` run over the full bank. **397 of 398** parse with every module
tag recognised and the stream starting at `VCO`. The one failure
(`Planetary Dial`) is one of the 8 non-standard-length blocks -- a truncated
capture, not a format problem.

Only **4 distinct module sequences** exist, and two account for 396 of them:

| count | modules |
|---|---|
| 202 | `VCO`…`SyA Seq` (19) |
| 194 | `VCO`…`SyA Seq Voc` (20) |
| 1 + 1 | truncated blocks |

Block sizes: 390 standard (146 chunks / 4672 wire), 8 non-standard (2432-7488)
-- exactly the split PRESET_FORMAT.md records.

`Panel` occurs in **38 of 398**, matching PRESET_FORMAT.md's "`EPanel` appears
in only 38 of 399" precisely -- independent confirmation that `EPanel` and
`Panel` are the same record.

#### There ARE two schema versions, and the name record marks them

**Name-record byte 20, high nibble = payload schema version.**

| byte 20 | payload | count |
|---|---|---|
| `0x10`, `0x11` | 19 modules, **no** `Voc` | 202 |
| `0x30`-`0x33` | 20 modules, **with** `Voc` | 194 |

396 of 398 obey this exactly; the 2 exceptions are truncated blocks. So the
device tells you which schema a preset uses *before* you parse it -- one of the
two "still-unexplained fields at bytes 19/20" is a version marker. Byte 19
ranges `0x05`-`0x0B` and does not correlate with the schema.

`Panel` only ever appears when byte 20 is `0x10` (38 of the 140 such presets),
so byte 20 constrains it but does not determine it. Nothing else does either:
of the 31 fields present in all 398 presets, **none** has a value that
partitions Panel-present from Panel-absent -- reproducing PRESET_FORMAT.md's
own negative result with a parser that no longer depends on a fixed field list.

#### Not factory-vs-user

The three populations (`Voc` 194, plain 166, `Panel` 38) are **interleaved
across slots** -- 52 alternating runs, starting `Panel`(0), plain(1-8),
`Panel`(9), plain(10-19)… So this is not a factory/user partition or a slot
range. It tracks each preset's own save history: whichever firmware last wrote
a preset determined its record set.

#### What this means practically

The framing is identical across both schemas, so `microfreak/preset/tlv.py` needs no
version knowledge to read either. Version only matters for *interpreting*
fields. The corpus confirms the model: one format, one framing rule, a record
set that grew with firmware, and per-preset variation because absent records
are simply not emitted.

### Round-trip: the format is proven, not inferred

`microfreak/preset/tlv.py` gained `serialize()` (records -> bytes) and `build()`
(grouped modules -> bytes). Neither replays captured header bytes; both
recompute each header from `(type << 5) | length`. So a byte-exact round trip
proves the header rule rather than assuming it.

Against all **390 standard presets** in `captures/mcc-presets.jsonl`:

| test | result |
|---|---|
| `serialize(parse(x)) == x[:tlv_end]` | **390/390** |
| `serialize(parse(x)) + remainder == x` (full 4088 bytes) | **390/390** |
| `build(groups(x)) == x[:tlv_end]` | **390/390** |
| `build(groups(x)) + remainder == x` (full 4088 bytes) | **390/390** |

The 8 non-standard blocks also serialize exactly (8/8); 6 of 8 build exactly,
the two failures being the truncated captures. Overall **398/398 serialize**
and **396/398 build**.

#### Two bugs the round trip caught

1. **`parse()` ran one record past the end.** The `0x20` end-of-stream marker
   must terminate the walk; without that the parser read the following `0xFF`
   fill as a record (in 40 of 60 sampled presets). Tests 1 and 2 pass either
   way -- parse and serialize agree with each other regardless -- so only the
   `build()` comparison exposed it. Worth remembering: a self-consistent round
   trip is not by itself proof the stop point is right.

2. **A module can have zero parameters.** `SyA` usually does: `0x23 'SyA'`
   followed immediately by `0x40`. A flat `(module, name, value)` list cannot
   represent that, so `build()` silently dropped 5 bytes. Hence `groups()`,
   which keeps modules and their (possibly empty) parameter lists.

#### What this means for generating presets

Region 1 is now fully solved: it can be taken apart into
`[(module, [(name, value), ...])]` and put back byte-identically. A generator
can synthesise it outright.

Regions 2 and 3 still cannot be synthesised -- only copied from a template.
Region 3 in particular (the sequencer) is mapped structurally but not decoded.
So preset generation is: **build region 1 freely, append a template's regions
2 and 3.** That is a real capability and it is not the same as generating a
preset from nothing.


## The mod matrix

### The mod matrix, mapped from the firmware (2026-10-01)

Ghidra, project `MFC` re-imported (both `~/MFC.gpr` and `~/MF.gpr` were gone;
the fresh import's `ram` block is `0x73A5F` = 473,695 bytes, matching
`main.raw`, so it is not the stale binary). The display function
**`FUN_08051064`** is the assign-slot label builder and it settles the format.

#### Parameter ids are `(module << 8) | parameter`

`FUN_08051064` computes the id of the assign slot it is drawing as

    uVar10 = slot_index + 0x902          // slot_index 0..2

`0x0902` is module 9 (`Mat`), parameter 2, and `ASSIGN_PARAMS["Mat"][2]` is
`Assign1`. So **`Mat/Assign1` = `0x0902`, `Assign2` = `0x0903`,
`Assign3` = `0x0904`**, and the `(module, parameter)` model in
`preset_fields.assign_decode()` is confirmed from the image rather than from a
single device round trip. `FUN_0804ec88(id)` is the global parameter getter
keyed on exactly this id.

#### A matrix cell is `(10 + destination) << 8 | source`

Three one-line helpers on the metadata object do the decomposition:

    FUN_080111c8(obj, v) = ((v - 0x0a00) & 0xffff) >> 8    // destination index
    FUN_080111d4(obj, p) = p                               // source index
    FUN_080111d8(obj, i) = *(DAT_080111e0 + i*4)           // source name
    FUN_080111e4(obj, i) = *(DAT_080111ec + i*4)           // destination name

`DAT_080111e0` -> `0x080622F0`, `DAT_080111ec` -> `0x080622D4`, the two label
tables. So `Co1..Co7` are modules 10..16 and map one-to-one onto the seven
destination columns, with the source as the parameter index:

| module | id high | destination | | source index | source |
|---|---|---|---|---|---|
| `Co1` | `0x0a` | `Pitch`   | | 0 | `CycEnv` (`EG1`) |
| `Co2` | `0x0b` | `Wave`    | | 1 | `Env` (`EG2`) |
| `Co3` | `0x0c` | `Timbre`  | | 2 | `LFO` |
| `Co4` | `0x0d` | `Cutoff`  | | 3 | `Press` (`Xpr`) |
| `Co5` | `0x0e` | `Assign1` | | 4 | `Key/Arp` (`Key`) |
| `Co6` | `0x0f` | `Assign2` | | 5 | `Dest` -- not a source, see below |
| `Co7` | `0x10` | `Assign3` | | | |

This confirms `preset_fields._MOD_DESTS`, `MOD_SOURCES` and `_MOD_SOURCE_TAG`,
and the `want_module = "Co%d" % (_MOD_DESTS.index(dest) + 1)` line in
`_mod_cell_record`, from the firmware. The owner's observed menu entry
`Press -> Wave` is source 3, destination 1 -> `Co2`, id **`0x0b03`** -- the
same value `assign_encode("Co2", "Xpr")` produces.

#### How the slot's label is drawn, and what the branches mean

```c
v = FUN_0804ec88(0x902 + slot);
if (v == 0x902 + slot)   -> the slot points at itself; show its own name
else if (v < 0x0a00)     -> module < 10: a plain parameter; show its name
else                     -> module >= 10: a matrix cell;
                            show "<source>" + "->" + "<destination>"
```

The `"->"` separator is `0x08075778`, read at `0x08051222`. **There is no
invalid branch** -- any stored value renders as one or the other.

#### `Co*/Dest` is runtime-only

`ASSIGN_PARAMS` gives each `Co` module six parameters, the sixth being `Dest`.
Checked against all 384 factory presets: **no `Co` module ever serialises a
`Dest` field** -- the TLV carries only the five source cells (`EG1` `EG2` `LFO`
`Xpr` `Key`). `Dest` is addressable as a parameter id (`Co5/Dest` = `0x0e05`)
but has no preset storage; a column's destination lives solely in
`Mat/Assign1-3`.

#### Enum tables, from `FUN_08011258`

`FUN_08011258(obj, id)` switches on the parameter id and returns that
parameter's value-label table plus its count. Extracted, it corroborates
`preset_fields.SWITCH`: `0x0100` (`VCF/Type`) 3 values, `0x0200` (`EG1/Mode`)
3, `0x0301` (`Kbd/Octave`) 7, `0x0500` (`LFO/Shape`) 6, `0x0306` (`Kbd/Scale`)
9 -- two more than the seven names found in the string table, so that one is
worth re-reading.

`0x0000` (`VCO/Type`) is special-cased: it calls `FUN_0800e644()`, clamps the
result at **0x16 = 22**, and indexes `DAT_08011434 + n*0x1c + 0xc`. That
independently confirms the oscillator engine descriptor stride of **28 bytes**
and the **22-engine** count.

#### Still open: what the assign menu offers

The format is now fully mapped; the *candidate pool* is not. The encoder-walk
code was not located. What is ruled out:

- **`CC != 127` is not the filter.** `ParaX1` carries CC 127 and is an assign
  target in 208 factory presets.
- **It is not a table of (module, param) pairs anywhere obvious.** A scan for
  runs of plausible u16 ids found only a monotonic index ramp at `0x0807C100`,
  which is not a candidate list.
- `0x08011090` (loads `0x902`/`0x903`/`0x904`) is inside `FUN_0800EB10`, the
  already-documented registrar, not a validator.

The handler to read next is the one keyed on `param_3 == 0x902..0x904` in the
UI dispatcher cluster around `FUN_08043224`. Note also that **nothing in the
firmware's display path validates the stored value**, which is consistent with
the SysEx probe: arbitrary ids store byte-exact. Whether they are *honoured*
by the modulation engine is a separate question and still unmeasured -- the
probe that would answer it needs a base preset with live infrastructure
(cycling envelope in `Run`, LFO actually routed), and the render has to be
screened for silence before anything is concluded from it.

### `Mat/Assign1-3`: the destination encoding, from the device (2026-10-01)

**The value is a 15-bit field in body bytes 1-2, like every other parameter**,
and it decodes as `(module_index << 8) | parameter_index`:

    module = (body[2] & 0x7F)        parameter = body[1]        body[0] = 0

This is the same rule the `VCO/Type` investigation of 2026-09-10 established
for record bodies generally -- bytes 1-2 are a 15-bit value -- applied to the
Mat module, which nobody had done. `preset_fields.assign_decode()` already
models the `(module, parameter)` split correctly; it has no callers, so no
project code was affected.

#### How it was pinned, and why the obvious reading is wrong

The owner selected **envelope Attack** as `Assign1` on the device (hold Assign1,
twist the Attack knob) and saved. Reading slot 511 back gave:

    Assign1  body = 00 01 06

`body[2]<<8 | body[1]` = `0x0601` -> module 6 (`EG2`), parameter 1 (`Attack`).
Correct. The competing reading `body[0]<<8 | body[1]` gives `0x0001` ->
`VCO/Param1`, which is not what was selected.

**A static audit over the factory bank cannot tell the two apart**: all 1,152
`Assign` records decode to valid `(module, parameter)` pairs under *both*
readings, because `body[0]` is always 0 and `body[1]` only reaches 6, which is
inside `VCO`'s 10 parameters. A self-consistent wrong model, with nothing
complaining -- the fifth instance of that failure mode here.

What does discriminate, short of the device: `body[2]` takes exactly
`{0..7, 9..15}`, the high bit never set. Under the correct reading those are
module indices, with **8 (`Sys`) absent** -- matching the manual's exclusion of
Master Volume and the Preset Encoder -- and 16-19 (`Co7`, `SyA`, `Seq`, `Voc`)
absent. Under the wrong reading that value set has no explanation.

#### The corrected factory census

With the right decode, the 384 factory presets use **33 distinct assign targets
across 15 modules**, not the 7 an earlier pass through the wrong reading
reported:

    VCO/Param3 228   Co4/EG2 220   EG1/Amount 183   Mat/Assign3 70
    Mat/Assign2 58   LFO/Rate 52   EG2/DecRel 50    EG1/FallLvl 44
    VCF/Reso 43      EG2/Sustain 33  Mat/Assign1 26  VCO/Type 19
    ... and 21 more, down to Co2/Key at 1

Two consequences worth stating because the wrong reading implied the opposite:

- **The factory does modulate the filter, the envelopes, glide and LFO rate.**
  `VCF/Reso` in 43 presets, `Kbd/Glide` in 6, `EG2/DecRel` in 50, `LFO/Rate`
  in 52.
- **Meta-modulation is standard practice, not unexplored.** `Co4/EG2` -- the
  env->cutoff matrix cell -- is an assign target in **220 of 384** presets, and
  `Mat/Assign1-3` (one assign slot pointing at another) in 154.

A base preset built from a device `Init` carries `VCO/Param3`, `EG1/Amount`,
`Co4/EG2` -- three of the four most common factory targets. Any generator
inheriting such a base starts from a reasonable assign configuration rather
than a poor one; what it will not have is variety, against 123 distinct
configurations in the factory bank.

#### Storage is not honoring

Six presets were written to slot 511 with deliberately out-of-range assign
bytes, including `Gen/Volume`, which the manual says cannot be assigned. **All
six round-tripped byte-identically**, `verify OK`. The SysEx path stores
whatever it is given. Any future claim that a target "works" because it
survives a write is worthless; it has to be heard or traced.

#### Firmware corroboration

`MODULE_TABLE` is confirmed from the image (`0x08062478`, 4-byte tags, already
documented 2026-09-05), so module 8 really is `Sys` and 13 really is `Co4`. The
per-module parameter orderings in `preset_fields.ASSIGN_PARAMS` match the
firmware's literal-pool registration order exactly for `VCO` (10), `VCF` (3),
`EG1` (7) and `Kbd` (8). The one entry the table flags as inferred -- `Sync`,
shared between `Arp` and `LFO` -- is indeed a shared literal, which is why it
could not be read off the pool; the surrounding order puts it at `Arp` index 5.

Cosmetic differences only, display names versus ours: firmware says `Sin`/`Sqr`
where `SWITCH['lfo_shape']` says `Sine`/`Sqa`, and `LP`/`BP`/`HP` where
`filter_type` says `LPF`/`BPF`/`HPF`. Values unaffected.

#### Still open

**What the assign menu will offer.** The encoding is settled; the reachable
*pool* is not. 125 targets are encodable, 33 are demonstrated by the factory
bank, and the manual says "(almost) every knob" with a short exclusion list.
The filter is not located, and `CC != 127` is ruled out -- `ParaX1` carries CC
127 and 208 factory presets assign to it. See FIRMWARE.md.

### How an assign target is applied, and the module gate (2026-10-01)

Found by following the two UI sites that load `0x902`. The modulation appliers
are **`FUN_080336xx`** and **`FUN_080344xx`** -- two paths, presumably the two
voice paths -- and each reads all three assign slots:

```c
id = FUN_08023c2c(voice, 0x902 + n);                  // n = 0..2
if (id != 0 && ((1 << (id >> 8)) & *DAT_080337dc) == 0) {
    vals = voice[(id >> 8) + 0x104f];                 // that module's value array
    v    = *(short *)(vals + (id & 0xff) * 2);        // read
    if (id < 0xa00) { ... }                           // plain parameter
    else            { ... }                           // matrix-cell target
    *(short *)(vals + (id & 0xff) * 2) = saturated;   // write back, 15-bit
}
```

Three things fall out:

- **There is no per-parameter whitelist.** The destination is looked up
  generically as `module_value_array[module][parameter]`. Any id resolves.
- **Only the three assign slots go through this generic path** -- exactly six
  call sites, three slots times two voice paths. The four hardwired
  destinations (`Pitch` `Wave` `Timbre` `Cutoff`) are applied by dedicated code,
  which is what makes them hardwired.
- **The `id < 0xa00` split appears here too**, so the modulation engine
  distinguishes plain-parameter targets from matrix-cell targets the same way
  the display does.

#### The gate is a module mask, and it is NOT the assign whitelist

`DAT_080337dc` -> **`0x080624dc`**, a static flash word **`0x000e01be`**. Bit
`module_index` set means that module is skipped by this path:

| allowed | excluded |
|---|---|
| `VCO` `EG2` `Mat` `Co1`-`Co7` | `VCF` `EG1` `Kbd` `Arp` `LFO` `Gen` `Sys` `SyA` `Seq` `Voc` |

**It must not be read as "what can be assigned", and the factory bank is the
proof.** Roughly a third of factory assign targets name excluded modules, and
`EG1/Amount` -- excluded here -- is the third most common assign target in the
whole bank at 183 of 384 presets. Arturia did not ship 183 presets with a dead
assignment.

The mask is referenced from exactly two addresses, `0x080337dc` and
`0x080347f4`, both inside these appliers. No UI or menu code touches it. So the
most likely reading is **which modules have a per-voice value array** reachable
at `voice + module*4 + 0x104f`: `VCF` is analog and driven by a control line,
`LFO`/`Arp`/`Seq`/`Gen`/`Sys` are global singletons. Modulating those must
happen on a different, global pass, which has not been found.

**This is a falsifiable prediction and the cheapest way to settle it is the
emulator**, not more disassembly: build arms whose assign target sits in an
allowed module against arms in an excluded one, with the infrastructure each
target needs actually live (cycling envelope in `Run`, LFO routed), render,
and compare, screening silence out first. If excluded-module targets move the
sound,
a second application path exists and the mask is only about this one.

#### Found en route: the sequencer automation structure

`FUN_08027fb4(obj, i) = *(u16 *)(obj + (page*0x209 + i)*2 + 0x404)` is not the
assign list, as first guessed -- it is the **sequencer automation table**:
**4 tracks** (four `u16` parameter ids at `+0x404`, initialised `0xffff` =
unassigned), **64 step records of 16 bytes** (`FUN_08027f48` clears them, step
values initialised to `0x64` = 100), and a per-step bitmask at record `+0x11`
of which tracks carry data at that step (`FUN_08028090`). `FUN_08027eec`
clears one track. Page stride `0x412`.

#### Still open

The **UI candidate walk** -- which ids the encoder will offer -- is still not
located. It is not the module mask. `FUN_0803f170`'s `0x902..0x904` branch is
assign-button press/release bookkeeping (a held-button bitmask at `+0x14`,
bit `slot + 2`), not a target list. The walk is somewhere in that state
machine, and given the modulation path has no whitelist, it may matter less
than it seemed: the menu is probably a curated subset of ids that all work.

### Mod matrix: what was confirmed, and what was not

Three separate claims were bundled together in PRESET_FORMAT.md. They have
different answers.

#### 1. Source/destination mapping -- CONFIRMED

`Co1`..`Co7` are destinations (pitch, wave, timbre, cutoff, assign1-3) and each
carries five fields in source order (`EG1`=cyc_env, `EG2`=env, `LFO`=lfo,
`Xpr`=press, `Key`=key_arp). Confirmed independently: on the 59 presets where
both addressing schemes are valid, name-based TLV lookup and
`MOD_MATRIX_WIRE`'s byte positions agree on **35 of 35 cells**, every time.

#### 2. `MOD_MATRIX_WIRE` addressing -- NOT confirmed, and layout-dependent

Across 390 presets, the two schemes agree on only **1411 of 7000 cells**. Full
35/35 agreement holds for **59 presets**; the rest disagree partially, and the
wire model returns impossible values there (55.7, 87.8 -- the display range is
+/-100 but those cells read 0.0 under TLV).

This is the "expanded vs compact layout" problem PRESET_FORMAT.md documents,
measured: fixed byte positions are only valid for presets in the layout the
table was derived from. **Name-based TLV addressing has no such constraint** --
it finds `Co<N>` and its fields by name, so it works on all 390 regardless of
layout, and it removes the requirement to start from a template that already
has a connection saved.

#### 3. The value formula -- WRONG, now corrected

The three bytes are **one 24-bit little-endian two's-complement fixed-point
fraction of full scale**:

```
display = int24 * 100 / 2**23
```

The per-byte model (`b0/81920 + b1/320 + b2*0.78125`) approximates this. It is
*exact* for byte2 -- `65536 * 100 / 2**23 == 0.78125` precisely -- which is why
it survived testing. But:

| term | per-byte model | true value |
|---|---|---|
| byte1 divisor | 320 | `2**23/(256*100)` = **327.68** |
| byte0 divisor | 81920 | `2**23/100` = **83886.08** |

Testing byte1 in isolation cannot distinguish 320 from 327.68: every multiple
of 32 rounds to the same 0.1 step either way. The error only appears once
byte2 is non-zero -- which is exactly the `KNOWN_BAD_MOD_MATRIX_BYTES`
symptom.

**The unified formula predicts both known-bad readings correctly:**

| bytes | per-byte model | unified | device actually showed |
|---|---|---|---|
| `(0,192,2)` | 2.2 | **2.1** | 2.1 |
| `(0,224,2)` | 2.3 | **2.2** | 2.2 |

It also reproduces the two independently confirmed encodings, `+1.0 =
(0,64,1)` and `-1.0 = (0,192,254)`, and round-trips all 2001 display values
from -100.0 to +100.0 in 0.1 steps with zero failures.

So `KNOWN_BAD_MOD_MATRIX_BYTES` is not a device quirk -- it is the visible
corner of a formula error. Sweeping the whole byte space finds **86**
combinations the per-byte model gets one step wrong, not 2.

`mod_matrix_bytes_to_display()` / `mod_matrix_display_to_bytes()` in
`microfreak/preset/fields.py` implement the unified model. The existing
`get_mod_matrix_raw`/`encode_mod_matrix_display` are left on the per-byte model
for now: they are hardware-verified for writes, and switching the write path
deserves one live confirmation first (write `(0,192,2)`, expect the device to
read 2.1).


## The sequencer

### Sequencer step bytes 8-15, decoded

Derived from 24960 steps (390 presets x 64 steps of sequence 0), not from
firmware -- the serializer at `0x08054A68` writes only region 1, and the
sequencer is written elsewhere.

| bytes | meaning | confidence |
|---|---|---|
| 8-11 | four **automation track values**, 8-bit `0x00`-`0xFF` | high |
| 12 | per-step state: `0` = rest, `1`/`2` with notes | **partial** |
| 13 | **bitmask of which automation tracks are live** this step | high |
| 14-15 | always zero across all 24960 steps -- reserved | high |

#### Byte 13 is the automation mask

`col13 == mask(cols 8-11 non-zero)` holds for **24849 of 24960** steps
(99.56%). Every one of the 111 mismatches is a track whose value is
legitimately `0` while its bit is set -- e.g. `auto = 00 7b e9 00` with
`col13 = 0x7`. That is exactly why the mask exists: **read byte 13 to know
which tracks are live; do not infer it from non-zero values.** Per-bit,
P(bit set | value non-zero) is 98.0 / 99.3 / 100 / 100 % for tracks 0-3.

Automation values run the full `0x00`-`0xFF`; 23% exceed `0x7F`, so they are
8-bit, not 7-bit MIDI.

#### Byte 12 is the gate state -- confirmed by differential test

`0` = rest, `1` = normal note, `2` = **tied into the next step**.

Adding a tie to one step on the hardware and diffing before/after:

```
before   step  9  F2   vel=1   b12=1      step 10  G2   vel=1   b12=1
after    step  9  F2   vel=30  b12=2      step 10  F2   vel=30  b12=1
```

Exactly the edited step's byte 12 flipped `1 -> 2`. Note what happened to the
following step: it was **rewritten to repeat the same note at the same
velocity**, with byte 12 = 1. A tie therefore occupies both steps and
overwrites whatever the next step previously held -- here it destroyed the
`G2`. A generator must write both steps, not just set the flag.

My earlier corpus statistic ("38% vs 75% held", read as too weak to be a tie
flag) was measuring the wrong direction: it asked whether a `b12=2` step
repeats the *previous* step's note, when the tie actually points *forward*.
The statistic was real; the inference from it was wrong.

Two incidental findings from the same capture:

- The empty-slot marker in a step changed `0xFB` -> `0xFF` when the device
  resaved the sequence. Both mean empty; `0xFB` appears in factory/MCC-written
  presets and `0xFF` in device-written ones. Decoders must accept both.
- Saving to a different slot **reflowed the whole block** -- the TLV stream
  grew from 1037 to 1188 bytes and 1064 bytes changed outside the sequencer
  region. The sequencer offset happened to stay at 1980, but that is not
  guaranteed: compute it per block from the end of the `0xFF` run.

#### Bytes 4-7 are velocity -- confirmed on hardware

Recording the MicroFreak's own MIDI output while slot 5's sequence played
reproduced **12 of 12** stored `(note, velocity)` pairs exactly, chord
pairings included:

```
stored  C2/72  D#3/89 | D3/2 | C3/21 | F2/1  D3/70 | C3/9 | A#2/57 ...
wire    C2 72  D#3 89 | D3 2 | C3 21 | F2 1  D3 70 | C3 9 | A#2 57 ...
```

The velocities of 1 and 2 transmit exactly as stored. They sound the same
volume as everything else because the MicroFreak's amplitude is
envelope-driven -- **velocity is a mod source that has to be routed before it
affects level**, and this preset does not route it. The audible test was
therefore misleading; the wire capture settled it.

Also ruled out on the way: byte 4 has no gate-like correlation (mean 65.9 when
the next step is a rest vs 75.0 when it is not -- weak and backwards for a
sustain reading), and its distribution across 10217 note steps peaks at
exactly `0x64` (100) and `0x7F` (127), velocity's two canonical defaults.

#### What would settle the remainder

A differential capture: set one step's gate/tie and one automation track to
known values via the device UI, save, and diff. That is the same method that
cracked the mod matrix, and it would resolve byte 12 and the 4-7 question in
one pass.


## Samples and wavetables

### Samples: the name chunk is 7-to-8 packed metadata

Established from `captures/send-1-sample-bank.pcapng` and
`captures/delete-sample.pcapng`, then confirmed byte-for-byte.

The 32-byte `20 17` name chunk is packed like every other chunk (4 groups of
8 wire bytes -> 28 real bytes). Decoded layout:

| Decoded | Size | Meaning |
|---|---|---|
| `[0:4]` | u32 | 0 in every chunk observed |
| `[4:8]` | u32 LE | **sample length in bytes** |
| `[8:10]` | 2B | unknown; `00 00` when empty, non-zero when populated |
| `[10:28]` | 18B | name, ASCII, NUL-padded |

`[4:8]` is confirmed two ways: `bee buzz` decodes to **371712**, exactly the
value in `captures/sample-encoding/bee-buzz-exported-archive.txt`; and
`build_name_chunk("bee buzz", audio_len=371712, unknown=b"\xf5\x2b")`
reproduces MCC's 32 wire bytes **exactly**.

Empty slots are unambiguous: in the bank capture, slot 0 (`bee buzz`, the only
slot getting real content) has a non-zero header, and all 127 placeholder
slots have an **all-zero** 12-byte header. A delete
(`captures/delete-sample.pcapng`) is just `03 5A <slot>` + `00 15` + one
`20 17` chunk with the same all-zero header -- i.e. length 0.

#### The bug this caused

`mfreak sample write` hardcoded 12 header bytes captured from one 'Piano Pop'
write, which decode to **length 145324**. Every sample written declared Piano
Pop's length regardless of the audio actually sent. On a slot MCC had already
populated the directory entry already carried a usable extent, so it appeared
to work; on an empty slot the declared length is what the allocation is built
from, so nothing usable landed -- matching the `0xFF`-gate behavior above.

`build_name_chunk()` now takes the real length. Note that
`write_sample_new_slot.py`'s 128-slot "touch sweep" was a hypothesis about
this failure and is probably not required -- the length was the actual cause.

#### Two loose ends

- **Decoded `[8:10]`** is not derivable from the two samples observed
  (`bee buzz` `f5 2b` at length 371712; `Piano Pop` `a2 66` at length 145324)
  and is **not** a checksum over the audio -- ruled out: sum16, xor, adler32,
  crc32 (both halves), CRC-16 CCITT-FALSE / XMODEM / MODBUS / ARC. It is a
  settable parameter defaulting to `00 00`, which is what the device itself
  reports for every empty slot.
- ~~**Decoded `[23]`** purpose unknown~~ -- **SOLVED: it is the slot index.**
  See "Samples: solved" below. The `0x01`/`0x00` reading was a coincidence of
  looking only at slots 1 and 0.

#### Length: block padding

`mfreak sample encode` emits whole 2048-sample blocks (4096 audio bytes each,
packed into 147 chunks = 4704 wire bytes). MCC declares the *pre-padding*
length (371712 for bee buzz) while the wire carries the block-padded amount
(372736, 91 blocks). `decoded_len()` returns the padded figure -- what is
actually transmitted -- a 1024-byte / 16 ms difference of trailing silence.
Pass `audio_len` explicitly to match MCC exactly.

#### Name parsing was subtly wrong

The old wire-level `STRAY_OFFSETS = {4, 10}` put the second skip at wire byte
22, but the 7-to-8 group headers inside the name field are at wire bytes 16
and **24** (name-relative 4 and 12). Any name of 10+ characters parsed wrong.
It never showed up because every name in the capture is <= 9 characters.
`name_of_sample_chunk()` now decodes properly; verified identical on all 128
chunks in the capture and round-trips an 18-character name.

### Samples: SOLVED -- decoded[23] is the slot index

`captures/write-sample-1-and-3.pcapng` (MCC writing real audio to slots 0 and
2, with the other 126 slots swept as empty placeholders) settles it. Across
all 128 name chunks, decoded byte 23 tracks the slot exactly:

```
slot 0 -> [23]=00    slot 1 -> [23]=01    slot 2 -> [23]=02
slot 3 -> [23]=03    ...                  slot 127 -> [23]=7f
```

**The device routes on this field, not on the select argument.** The firmware
says so directly: both the read op (`0x08046708`) and the write op
(`0x08046790`) begin `ldrb r3,[r1,#23]` / `cmp r3,#127` -- they take the slot
from byte 23 of the passed structure, which is the decoded name chunk. The
slot in `03 5D` / `03 5A` / `03 58` selects the transfer, but the directory
entry is addressed by `[23]`.

This is why every earlier attempt landed on slot 0: we always sent `[23]=0`,
so the device wrote slot 0's directory entry no matter which slot we selected.
Both symptoms -- "target slot stays empty" and "slot 0 gets clobbered" -- are
the same bug, and the `0xFF` fallback was a red herring: the slot was never
unallocated from the device's point of view, it was just always slot 0.

An earlier note here read `[23]` as an occupancy marker because the only two
values seen were `0x01` on a placeholder and `0x00` on a real sample -- that
was slots 1 and 0, a coincidence. Corrected.

#### Verified

- `build_name_chunk("Voicy_I Am A", audio_len=227328, unknown=b"\x19\x1e",
  slot=2)` reproduces MCC's slot-2 chunk **byte-for-byte**.
- Live: writing to previously-empty **slot 3** produced
  `('beeSLOT3', 372736)`, with slot 0 (`bee buzz`) and slot 2
  (`Voicy_I Am A`) **untouched**. Before the fix the same operation left the
  target empty and overwrote slot 0.

#### Still unknown

Decoded `[8:10]` -- three data points now (`bee buzz` len 371712 -> `f5 2b`;
`Piano Pop` len 145324 -> `a2 66`; `Voicy_I Am A` len 227328 -> `19 1e`) with
no relationship to the length and no checksum match. Writes succeed with
`00 00`, so it is not required for allocation. Whether it affects playback is
untested.

### Samples: audio confirmed by ear

The user auditioned a sample written to a previously-empty slot: it plays
correctly. That closes the last open question on the sample path -- decoded
`[8:10]` really is cosmetic, and sending `00 00` for it is safe.

### Wavetables: unaffected -- verified

`mfreak wavetable write` was already correct. Its name chunk is a different shape
from a sample's, and the slot index lives in different bytes:

| | sample | wavetable |
|---|---|---|
| slot index at | decoded `[23]` | decoded `[0]` **and** `[8]` (wire `[1]`, `[10]`) |
| name at | decoded `[10:28]` | decoded `[12:28]` |
| length field | decoded `[4:8]` | none -- always 4 x 4096 = 16384 |

`mfreak wavetable write` sets both slot positions already (`payload[1] = hi`,
`payload[10] = hi`), which is why it never had the sample bug.

Proof that wavetables cannot be routing on `[23]`: reading the factory slots
live shows decoded `[23]` is a **name character** -- `0x69` (`'i'`) for
`Pluck Additive`, `0x61` (`'a'`) for `Pluck Resonant`. If the device routed on
it, `Pluck Additive` would target slot 105, which does not exist in a 16-slot
bank.

Confirmed live: cloning slot 0 -> slot 5 (renamed `WTtest`) gave
`verify OK: read-back matches exactly`, slot 5 became `WTtest`, and slots
0-4 and 6 were untouched. No slot-0 clobbering.

Also note wavetables are **directly addressed** in firmware -- opcode `0x57`
computes `(index + 0xF5F) << 12` with no directory lookup -- so there is no
allocation step to get wrong in the first place.

### Characteristics: the tag string is stored reversed (2026-09-17)

The 18-character bit string in each preset archive is MCC's **Characteristics**
grid, and the bits run **backwards** relative to the order MCC's own UI lists
them: string position 0 is `Soundtrack`, position 17 is `Acid`.

    Acid Aggressive Ambient Bizarre Bright Complex
    Dark Digital Ensemble Funky Hard Long
    Noise Quiet Short Simple Soft Soundtrack

`Roid` stores `000101001000000000`. MCC shows **Funky, Noise, Short** for it.
The reversed reading reproduces that exactly; reading left to right gives
Bizarre, Complex, Ensemble, which is wrong. An earlier note here that guessed
the left-to-right order has been corrected.

One screenshot is one data point, so the ordering was confirmed a second way,
independent of it. Five pairs of the eighteen are opposites -- Soft/Hard,
Short/Long, Simple/Complex, Bright/Dark, Quiet or Ambient with Aggressive --
and a correct bit order should rarely assert both halves of a pair:

    reversed reading      0 of 38 tagged presets contradictory
    left-to-right         6 of 38
    random bit orders     3.7 on average, over 4000 permutations

P(a random bit order is at least as self-consistent as reversed) = 0.038,
while the left-to-right reading sits at the 89th percentile of random -- that
is, indistinguishable from an arbitrary permutation. The reversed reading also
pairs names with meanings: `Digi Express` reads Digital + Aggressive rather
than Hard + Soft, `Smooth IPA` reads Soft + Long + Ambient, `Raver` reads
Aggressive + Dark + Digital + Hard + Noise, `Wander Drum` reads Short.

`mfbank.characteristics(tags)` and `mfbank.tag_string(names)` implement it, and
are inverses on all 384 populated factory presets. `mfbank.py BANK
--characteristics` lists them.

#### Type: `scalars[0]`, a 0-based index into MCC's dropdown

Decoded the same day. The dropdown is alphabetical and `scalars[0]` indexes it
directly -- and unlike the tag string it is **not** reversed:

    0 Bass     1 Brass    2 Keys        3 Lead
    4 Organ    5 Pad      6 Percussion  7 Sequence
    8 SFX      9 Strings  10 Template   11 Vocoder

Three presets read off MCC fix it: `NervousKeys` = Keys (2), `Tubular` =
Percussion (6), `Disrespectful` = SFX (8). A fourth point comes free -- the
generated bank was written with 0 and imported as Bass. The factory bank uses
all twelve values and nothing outside them.

Corroborated without the screenshots: of the 75 factory presets whose name
contains exactly one type word, 38 carry the matching type (51%), against 6.2
of 75 expected under a random ordering of the twelve names (4000 permutations,
P(random >= observed) < 0.0001). The residual is curation, not decoding --
`ArpZap` is filed as Lead rather than Sequence, `Strings 2` as Template.

`mfbank.preset_type()` and `mfbank.type_value()` implement it;
`make_bank.py --type NAME` writes it, defaulting to `Template`.


## Bank files and containers

### MCC bank exports: `.mfprojz`, `.mfsbz`, `.mfwbz` (2026-09-11)

All three are ZIP archives of per-item **Boost.Serialization text archives** --
the same inner format as the single-item `.mfpz` / `.mfsz` / `.mfwz` exports
that SAMPLE_ENCODING.md already cracked. A bank is only a container; no new
content encoding is involved. `mfbank.py` reads all six.

    Factory6.0/01-factory_6-A/<slot>-<project>-A<n>.mbp     512 presets
    Factory/01-Factory-A/<slot>-Factory-A<n>.mfs            128 samples
    Factory/01-Factory-A/<slot>-Factory-A<n>.mfw             16 wavetables

The archive header, confirmed on all 656 items of one factory set:

    <22> serialization::archive <10> <0> <4>
    <L> <version>        "94", "16", "108", "48", "DEVBUILD"
    <L> <name>           MAY CONTAIN SPACES
    <n> <0> <0> <18> <18-character bit string>
    <a> <b> <c> <N> <N signed ints>

Three things that bite:

- **The name is length-prefixed, not whitespace-delimited.** `read_mfpz` in
  `presetlib.py` split on whitespace, so `Revert to Iron` was silently
  replaced by the filename. Fixed 2026-09-11; it now calls `mfbank.py`.
- **The 18-character bit string is MCC's "Characteristics" grid** -- 18 bits,
  18 boxes. It is all zeros on most presets but not all
  (`000000000001101000`), and it sits between the name and the array. A parser
  that scans for a zeros literal instead of reading the length prefix fails on
  38 of 512 presets. Decoded 2026-09-17; see below.
- **The slot lives only in the filename**, and MCC drops the separating dash
  on some entries -- 47 of 512 in this bank, e.g. `10600122021 17h55-A26.mbp`
  is slot 106. Recoverable, because a bank's slots are a permutation of
  1..N: 465 unambiguous names pin the rest by elimination. `mfbank._slots()`
  does it and raises rather than guess.

Payloads by extension:

| ext | values | meaning |
|-----|--------|---------|
| `.mbp` | 4672 | raw wire preset payload, exactly `mfreak preset read`'s output |
| `.mfw` | 16384 | 8192 x int16 LE -- one wavetable |
| `.mfs` | N | 16-bit LE PCM, 32 kHz mono |

An unused slot exports as a one-element array. In the factory set: 384 of 512
presets, 52 of 128 samples, 16 of 16 wavetables.

**Control:** wavetable slot 1 of the bank (`Convergence`) is **byte-identical**
to `captures/sample-encoding/convergence-exported-archive.txt`, the single-item
export this project already verified against hardware. The bank path is
therefore checked end to end against known-good data, not merely plausible.

A `.mbp` carries **no region 0** -- no name record, no schema byte -- so
placing one in a flash image needs a region 0 from elsewhere. See the
authoring trap above before borrowing one from the capture.

### Writing a bank MCC will load (2026-09-16)

`mfbank.py` can now write `.mfprojz` / `.mfsbz` / `.mfwbz`. The serialiser is
verified by `--round-trip`, which re-serialises every item of a bank and
requires byte-identical output: 512/512 presets, 128/128 samples, 16/16
wavetables of one factory set.

#### The archive structure is unconditional

After the 18-character tag string, every item is

    <a> <b> <c> <N> <N signed ints>

in 656 of 656 items. No container version marker ever precedes the count. The
earlier parser searched for "the count that fits" on the assumption one
sometimes did; it never does, and the search was wrong on empty slots, whose
tail `1 0 1 0` it read as one value of 0 rather than the zero values the count
states.

`abc[2]` is the **schema byte**. Its high nibble is 3 if the payload carries a
`Voc` module and 1 if not -- confirmed on all 384 populated factory presets,
the same rule region 0 follows. Its low nibble is a format vintage tracking the
version string (ver `48` is always 3, `108` almost always 2, `16` is 0 or 1);
the device writes 3 on every one of its 509 saved slots. `scalars[0]` is the
save-screen category.

#### How MCC assigns slots

A member's slot is in its filename only (`01-<project>-A1.mbp`); the archive
inside carries none.

**MCC places presets by position, not by the number** (owner, 2026-10-22). It
sorts the members by the number before the first dash, then puts the first in
slot 1, the second in slot 2, and so on:

    only `05-AtSlot5`                          loaded into slot 1
    empty entries 01-04, then `05-AtSlot5`     loaded into slot 5

So a project needs an entry for every slot before its last preset, empty where
nothing goes; MCC empties those slots on the instrument. Nothing is needed
after the last preset. Current MCC exports have all 512 entries; older ones
stop early and load correctly. MCC's own `%02d` numbering does not scramble
three-digit slots.

**One measurement disagrees** (2026-09-16): a 135-preset bank written by
`make_bank.py`, loaded through MCC and read back. All 135 payloads arrived
intact, but only 10 in the slot asked for, and a string sort of the member
filenames explained all 135. The filenames of that bank are not on record. If
the bank had gaps, position placement may explain it.

`write_bank()` fills the slots before the last preset with empty entries and
zero-pads the prefix to a fixed width. `_slots()` reads the number itself.

#### Preset names are truncated to 14 characters, not 23

From the same read-back, with names paired to payloads rather than to slots:

    intended <= 14 characters   45 of 45 survived intact
    intended >= 15 characters   90 of 90 truncated to exactly 14

Always a clean prefix; no corruption, no exceptions either side of the
boundary. `mfreak preset write --name` documents a 23-character limit, which is
what the name record has room for; what survives the **MCC-to-device** path is
14. `mfreak preset write`'s own direct-SysEx path has not been tested against this
and may well differ.


## Open questions

### Open

- **No SysEx opcode allocates a directory entry directly.** `rec+0` and
  `rec+4` are read-only at all 25 stride-24 indexing sites. Allocation appears
  to be a side effect of the name-record write, not a distinct command.
- **Samples and wavetables are unverified.** They share the same commit path
  (`20 17` -> `0x08046790`) and therefore the same `0xFF` gate, but their
  name/metadata goes as a 32-byte *chunk*, not a 45-byte record, and no
  capture in `captures/` contains sample or wavetable traffic. See
  SAMPLE_ENCODING.md for what would settle it.
- The device also accepts a second, non-Arturia 7-byte SysEx header --
  `5A 57 6E 28 3C 4E 44`, from a table at `0x080753AC`, matched in reverse by
  states 38-43. Purpose unknown; likely factory or diagnostic. Not touched.

### Still open

- Sequencer step byte 12's `1` vs `2` is solved, but bytes 8-11's automation
  **track ordering** (which of the four maps to which UI lane) is assumed, not
  checked.
- Preset block region 2 (the `0xFF` fill) -- whether its length carries
  meaning or is pure slack.
- Whether `Mem protect` also gates sample and wavetable writes.

