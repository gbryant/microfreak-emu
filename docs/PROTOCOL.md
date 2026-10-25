# The protocol: the bytes on the wire

How to talk to a MicroFreak: the SysEx frame, how MIDI Control Center finds
and interrogates the device, how a preset is read and written, and every
opcode the firmware dispatches.

`FORMATS.md` is the other half -- the shape of the data these messages carry.

**Slot numbers here are 0-based.** That is correct for the wire, which is
what this document is about: a preset read sends `divmod(slot, 128)`, so
preset 1 on the panel goes out as 0. It is *not* correct for the commands
quoted in the log entries below. Since 2026-10-19 the tools take the number
the instrument shows -- presets 1-512 -- and convert once at the argument
boundary (`microfreak/panel.py`), so a line like `mfreak preset read 0` was
run as written at the time and would be **refused today**; the equivalent is
`mfreak preset read 1`. Add one to any slot number in a command here. The
byte dumps and the prose are unchanged and still right, because they
describe the wire.

**The numbered sections keep their original numbers.** They came from capture
notes that other documents cite by number ("independent agreement with
sections 3 and 10"), so renumbering them would break references that are still
correct.

**There are two opcode tables here and they are not duplicates.** "Every
entry" and the byte-for-byte map are read off the firmware's own dispatch
tables -- every opcode the firmware will accept. "14. Opcode reference" is what
MIDI Control Center was observed to send. The first is the possibility space,
the second is the subset anyone has seen used.


## Provenance

The opcode map was decoded from the v5.0.0.2084 main image on 2026-10-07,
from the dispatcher and its jump table rather than from observation -- which
is why it covers opcodes MIDI Control Center has never been seen to send.

The capture-derived sections come from a 31,334-message capture taken with MCC
launched against an attached synth. **The device reported its own firmware
revision as `05 00 00 24`** in that session. Captures live outside this
repository -- see `NOTICE`.

Observed facts are kept separate from guesses throughout; where a section says
CONFIRMED it means a capture or the device showed it, not that it looked
plausible.


## The frame

### SysEx frame layout

The device buffers everything after the manufacturer ID into one block. The
ID-reply handler pre-loads `00 20 6B 07` into that buffer and sets its length
to 4, so the wire bytes and the buffer indices line up like this:

```
F0 | 00 20 6B 07 | 01 | seq | cat | opcode | args... | F7
     buf[0..3]     [4]  [5]   [6]    [7]      [8][9]
```

- `0x0804D270` is the per-byte SysEx state machine (43 states, `tbh` jump
  table at `0x0804D282`). States 1-4 match `00`, `20`, `6B`, `07` one byte at
  a time -- which is why no contiguous `F0 00 20 6B` constant exists in the
  image to grep for.
- `0x0804D8E8` runs at `F7` and hands the buffer to a callback.
- `0x0804ACC8` is the opcode dispatcher: `opcode = buf[7]`, biased by `0x15`,
  range `0x15..0x5D`, `tbh` table at `0x0804AD20`. **Only 16 of the 73 table
  entries are implemented**; the rest fall to a default.

Implemented opcodes: `0x15 0x16 0x17 0x18 0x19 0x52 0x54 0x55 0x56 0x57 0x58
0x59 0x5A 0x5B 0x5C 0x5D`.

`0x18` is the generic response; every reply the device builds writes `0x18`
into the outgoing opcode field.

### Frame

```
F0 | 00 20 6B 07 | 01 | seq | cat | opcode | args... | F7
     buf[0..3]     [4]  [5]   [6]    [7]      [8][9]
```

`0x0804D270` is the per-byte state machine (43 states, `tbh` at `0x0804D282`).
States 1-4 match `00 20 6B 07` one byte at a time, which is why no contiguous
`F0 00 20 6B` constant exists in the image to grep for. `0x0804D8E8` runs at
`F7` and hands the buffer to a callback.

### 3. Arturia SysEx frame — CONFIRMED

All device-specific traffic shares this frame:

    F0 00 20 6B 07 <dd> <seq> <cmd> <sub> ...payload... F7

    byte 4  07     constant across every message seen
    byte 5  dd     01 on all preset traffic; 7F on a separate class of message
    byte 6  seq    7-bit sequence counter, wraps at 0x80
    byte 7  cmd    command (see below)
    byte 8  sub    varies per command

Commands observed:

| cmd  | length | count  | meaning                          |
|------|--------|--------|----------------------------------|
| 0x23 | 45     | 512    | preset name record — decoded      |
| 0x20 | 42     | 30,576 | preset parameter data — not decoded |
| 0x00 | 10     | 208    | short reply, likely ack           |
| —    | 12     | 31     | `07 7F` variant, purpose unknown  |


## Finding and identifying the device

### 1. Device discovery — CONFIRMED

MCC finds hardware by **broadcasting a standard MIDI Identity Request to every
MIDI destination on the system**, about once per second, retrying until
something answers. It does not use port names — a virtual port with any name
gets probed identically.

    F0 7E 7F 06 01 F7

    7E  Universal Non-Realtime
    7F  device ID 7F = broadcast
    06  sub-ID#1 General Information
    01  sub-ID#2 Identity Request

This is the MIDI spec's standard request, nothing Arturia-specific — which is
why a MIDI proxy standing in for the instrument can answer it.

### 2. Identity reply — CONFIRMED

The MicroFreak answers:

    F0 7E 7F 06 02 | 00 20 6B | 06 00 | 06 01 | 05 00 00 24 | F7

    00 20 6B     manufacturer — Arturia, now confirmed from hardware
    06 00        family  (LSB, MSB)
    06 01        member  (LSB, MSB)
    05 00 00 24  software revision

The reply is built at `0x08023960` from immediates, one `movs` per byte, and
queued through `0x0804C69C`. Found in the emulator with write watchpoints on
the queued copy, then the caller's return address. The revision is
major.minor.patch, then the build number cut to 7 bits: `0x24` is 2084 & 0x7F,
loaded by `movs r4, #0x24` at `0x0802398A`. Over USB the emulated firmware
answers with the same bytes as the hardware; over DIN alone it does not answer.

Changing that one immediate changes only the last byte of the reply: an
emulated build with `0x61` there answered `05 00 00 61`.

The patch byte cannot be changed the same way. It is stored from `r3`, which
also supplies the zeros at offsets 2, 3, 4, 9 and 13 of the record. The six
stores that end the builder, `0x080239A8`–`0x080239BF`, are all `strb.w` to
`sp`. At that point `r1` holds `sp` (`mov r1, sp` at `0x0802398C`), so they fit
as 16-bit `strb` through `r1`, which leaves room in the same 24 bytes to load
two values into `r7` and `r4`. Both are callee-saved and popped on return.

Rev 18 and earlier of microfreak-tools' patches used this to answer
`05 00 <letter> <rev>`. MCC reads the version only from this reply, and for
that one it shows "firmware revision unknown" and will not open the
instrument's editors, though it still offers a firmware update. Patched builds
now answer exactly as stock and identify themselves through opcode `0x48`
instead (below). `mfreak` still decodes the old form.

#### Opcode 0x48: which patch is running

    request  F0 00 20 6B 07 01 <seq> 2A 48 F7
    reply    F0 <s0> <s1> <s2> <s3> <s4> <seq> 00 18 F7

The reply has the same shape as a peek reply. The five septets carry a 32-bit
word, least significant first, of which the low 16 bits are used:

    bits 0-6    the plugin's letter, ASCII (S speech, P peek, W words)
    bits 7-13   the patch's revision
    bits 14-15  format, 1

Speech rev 19 answers `53 13 01 00 00`. Stock firmware sends nothing back, so
a host waits briefly and treats silence as stock. `0x48` bails in both stock
dispatchers (section 8). The handler is a `movw` of the word followed by the
peek handler's septet loop, and a stock image with only embedded words carries
it as a 40-byte stand-alone handler at the end of the vocabulary space.
Verified in the emulator for all three builds and stock, and on hardware for
speech rev 19, where MCC opens normally.

### 9. The connect-time dump: structure, and what it is not

**Correction:** this section originally asserted the 42-byte `cmd 0x20` records
in the macOS reply-only capture were per-preset patch parameters. They are not —
section 10 identifies them as shared wavetable/oscillator data. The structural
findings below still hold and are what led to the request-side capture; only the
"these are patch parameters" reading was wrong. Left in place because the
reasoning that follows is still sound and still load-bearing.

Independently reproduced from the macOS side afterwards, which corroborates
section 10 from different data: 7-to-8 decoding the macOS capture's chunk stream
gives 208 blocks of 147 chunks (4704 wire → 4116 real bytes each) in a strict
period-5 pattern — one name block followed by four frame blocks. The names are
`Convergence`, `Funky Bell`, `Pluck Additive`, `Pluck Odd`, `Pluck Resonant`:
factory wavetables, and none of them appear in `presets.txt`. The 4116-byte
block size is the sample/wavetable size, not the 4088 presets use.

That also retroactively explains an oddity from the very first day's analysis:
raw wire bytes read `Co.nverge n.ce`, with unexplained `0x00`s every 8 bytes.
Those were the 7-to-8 mask bytes.

Two findings constrain how these records must be read:

**One request per record.** Grouping replies by their echoed sequence byte gives
31,088 transactions, essentially all of them a single reply. MCC does not pull a
bulk dump; it issues roughly 30,576 individual requests, one per 42-byte chunk.

**The records carry no addressing.** Bytes 9 onward are pure payload — every
position spans the full 0x00–0x7F range with no field that behaves like a slot
or offset, and records of all-`00` and all-`7F` occur. Nothing in the reply says
which preset or which part of it the chunk belongs to.

Together these mean the addressing lives **entirely in the request**. Section 11
resolves this: the request side has now been captured.


## What MIDI Control Center asks for

### 6. MCC → device requests — CONFIRMED

Captured with a man-in-the-middle MIDI proxy (a private-workbench tool, not
in either published repository): synth disconnected, proxy answering the identity
request from `identity.hex`, so MCC believed it had found the synth and issued
its full opening sequence.

MCC's startup conversation is, in order:

1. `F0 7E 7F 06 01 F7` — identity request (section 1)
2. **31 × config reads**, then
3. **a walk through every preset slot in order**

#### Config read (type 0x01, sub 0x43) — request and reply both CONFIRMED

    ->  F0 00 20 6B 07 01 <seq> 01 43 <id> F7
    <-  F0 00 20 6B 07 7F <seq> 02 42 <id> <value> F7

31 requests, each with a distinct `<id>` spanning 0x20–0x4D, each answered by a
12-byte reply carrying the same id and one value byte. Note the reply uses
`07 7F` where the request uses `07 01`.

This also resolves what the `07 7F` message class is — it was previously listed
as unknown. It was identified by replaying recorded replies to MCC and seeing
the ids line up in request order.

Two corrections to the above, both measured on 5.0.0.2084 (2026-10-18):

* **The device answers 46 ids, not 31.** Every id from 0x20 to 0x4D replies.
  MCC asks for the 31 that have a control on its DEVICE page.
* **The reply does NOT echo the request's `<seq>`.** It carries the device's
  own counter, which steps by 2 — `0x5B, 0x5D, 0x5F, 0x61, 0x63`. The preset
  name request *does* echo ours, so a reply matcher written from that habit
  matches almost nothing here: a probe of 46 ids filtering on the echo got one
  answer, and the same probe matching on the id got 46. Match config replies
  on the id.

#### Config WRITE (type 0x02, sub 0x42) — CONFIRMED

    ->  F0 00 20 6B 07 01 <seq> 02 42 <id> <value> F7

The reply's own form, sent the other way: `07 01` is the host's header and
`07 7F` the device's, so a write and an answer differ only in which end sent
them. **The device acknowledges nothing** — a capture of 62 writes contains no
inbound frame at all — so the only confirmation is to read the id back.

From a capture of MCC's DEVICE page worked through one control at a time, each
setting moved off its value and put straight back, left column top to bottom
then right: 62 writes, 31 pairs, for the page's 31 controls. Every restored
value equals what the instrument reads back today, 31/31. Two rows confirm
their own position independently — `CV 0V Reference` = C1 is 36 and
`CV 1V Reference` = C0 is 24, MIDI note numbers an octave apart. The id↔name
table is in `microfreak.config.SETTINGS`; `mfreak config read` prints it.

The 15 ids that answer with no control on the page are 0x22, 0x23, 0x29, 0x2A,
0x2C, 0x2F, 0x30, 0x32, 0x33, 0x34, 0x35, 0x40, 0x43, 0x48, 0x4B. Unknown.

#### Preset name request (type 0x03, sub 0x19) — the important one

    F0 00 20 6B 07 01 <seq> 03 19 <hi> <lo> 00 F7

    slot = hi * 128 + lo

MCC walks slots 0, 1, 2, … in order, one request each, `<seq>` incrementing and
wrapping at 0x80.

**This uses the same slot encoding as the name reply in section 4**, derived
independently: the reply's encoding came from 512 replies covering 0–511 with
no gaps, the request's from watching MCC count upward. Request and reply confirm
each other.

So the exchange is:

    ->  F0 00 20 6B 07 01 <seq> 03 19 <hi> <lo> 00 F7
    <-  F0 00 20 6B 07 01 <seq> 23 52 .. <hi> <lo> .. <name> 00 .. F7

`mfreak preset read N` implements exactly this, and it is **verified against
hardware**: 20 slots queried live returned names matching the passive capture
exactly, 20/20, with the device echoing our `<seq>` byte in every reply. The
sequence byte is therefore a transaction id, not a counter the device maintains.

The transcript that established the echo came from the workbench tool of the
time, which printed the request bytes alongside the reply:

    slot 0: sending F0 00 20 6B 07 01 2A 03 19 00 00 00 F7
      slot 0: "NervousKeys"   slot=0  seq echoed: yes

`mfreak preset read` sends the same bytes and reports the result instead:

    $ mfreak preset read 0
    slot 0: 'NervousKeys'  (4672 bytes)

#### Hypothesis, untested

Reply type appears to be request type + 0x20 (request 0x03 → reply 0x23). If it
holds, the 42-byte `0x20` parameter-data replies answer a request of type 0x00,
which has not been seen — MCC never got that far without a synth to answer.

### 10. The request side — CONFIRMED (new capture)

Captured on Windows: the workbench's shell tooling doesn't run there, so this
used Wireshark + USBPcap
(captures real host↔device USB traffic directly, no proxy/impersonation needed)
during a full MCC sync. Source: `captures/mcc-sync.pcapng`, decoded to
`captures/mcc-sync.jsonl` via the new `pcap_to_jsonl.py` (Windows/USBPcap
captures have Wireshark's own USB-MIDI dissector already split each event
packet into fields, rather than leaving raw bytes in `usb.capdata` the way the
originally-planned decode path expected — `usbmidi.py` now tries `usb.capdata`
first and falls back to reassembling from those dissected fields, so it works
on either style of capture).

MCC's full sync, in order, is:

1. Identity request (section 1).
2. 31 config reads (section 6).
3. **A shared-resource dump** (new — this section; see section 11 for why this
   is *not* per-preset patch data, despite that being the original guess).
4. The 512-slot preset-name walk (section 6), done last, packed into well
   under a second since no dump is involved.

#### The dump itself

Each "block" of patch data starts with a **select** request:

    ->  F0 00 20 6B 07 01 <seq> 03 <sub> <hi> <lo> 01 F7      sub ∈ {0x55, 0x57, 0x5B}
    <-  F0 00 20 6B 07 01 <seq> 00 15 F7                       ack, 10 bytes

`<hi>`/`<lo>` look like the same two-byte value used for preset-name addressing
(section 4/6: value = `hi*128 + lo`), but the three sub-values don't share a
slot range in the way that would suggest they're all indexing the same 512
presets — `sub=0x57` appears 16 times (`lo` always 0), `sub=0x55` 64 times
(`lo` cycling 0–3 for each of 16 `hi` values), `sub=0x5B` 66 times (`lo` always
0, `hi` sparse up to 127). Likely three different parameter categories or
memory regions, addressed at different granularities — **not yet confirmed
which**.

After the ack, MCC pulls the block with a plain, unaddressed "next" command:

    ->  F0 00 20 6B 07 01 <seq> 01 18 00 F7                    11 bytes
    <-  F0 00 20 6B 07 01 <seq> 20 16 <32 bytes payload> F7    42 bytes

Critically, **`<seq>` stays fixed for the whole block** (unlike every other
request type, which increments `<seq>` per message) — it behaves like a
session/stream id rather than a per-message transaction id here. The device
just returns "whatever comes next" each time this exact same 11-byte message
is repeated; there is no offset field anywhere in the exchange. Addressing
within a block is therefore purely positional: the Nth `01 18` request gets
the Nth 32-byte page of that block, counting from 0.

**Block end:** the final chunk of a block has sub-byte `0x17` instead of
`0x16` (still cmd `0x20`), and every block observed was exactly **147 chunks**
(147 × 32 = 4704 payload bytes), immediately followed by the next `03`
select request for the next block. Verified across all 146 blocks in this
capture (a couple of edge-case block sizes — 127, 135, 209 — appeared once
each, cause not yet investigated; likely dropped/retried transactions rather
than a real variable block size).

One anomalous single request, `F0 00 20 6B 07 01 5F 01 47 0A F7`, appeared
once near the end of the dump phase. Section 12 identifies this: it's a
generic status/handshake message, not specific to this dump.

#### What this dump actually is

At first this looked like the per-preset patch-parameter dump (30,576 records
estimated from the macOS reply-only capture, ~60/preset). It is not. Section
11's dedicated single-purpose captures prove it's a **shared resource** —
almost certainly wavetable/oscillator data used by all presets, not data
belonging to any one preset:

- `captures/mcc-wavetables.pcapng` (MCC's *Wavetables* tab, exporting the
  wavetable library — nothing preset-specific involved) uses the exact same
  transport (`03 55`/`03 57` select → `01 18` pull → `20 16`/`20 17` chunks),
  and its very first block (`sub=57 hi=0 lo=0`) is **byte-for-byte identical**
  to `mcc-sync`'s first block, "Convergence" text and all. Two different
  operations, same bytes back — that only happens if both are reading the
  same underlying resource, not something that varies per preset.
- `mcc-wavetables` never touches `sub=0x5B` — only `mcc-sync`'s connect-time
  dump does. So `0x5B` blocks are something else again (still unidentified;
  possibly genuine per-preset data, given they're the one sub-type with
  *variable* block sizes — 127–209 chunks, not the fixed 147 the wavetable
  blocks use).

So: `03 55`/`03 57` = wavetable/shared-oscillator data (confirmed by direct
comparison); `03 5B` = unknown, possibly per-preset (size varies, unlike the
others) but not addressed by preset slot number the way section 11 shows real
per-preset reads are. The "Convergence" text is presumably a factory wavetable
name, not a preset name.

#### `03 55` / `03 57` fully decoded, from the dedicated wavetable-export capture

`captures/mcc-wavetables.pcapng` (MCC's Wavetables page → export all, a
genuine one-item-at-a-time transfer) makes the addressing exact:

    sub=0x57, hi=<wavetable slot 0-15>, lo=0, tail=01           NAME block (one 147-chunk block)
    sub=0x55, hi=<wavetable slot 0-15>, lo=<frame 0-3>, tail=01  WAVEFORM DATA (one 147-chunk block per frame)

16 wavetable slots (`hi` 0–15), matching the device's 16-slot user-wavetable
capacity. Each has one name block and up to 4 waveform-data blocks (`lo`
0–3 — almost certainly the 4 single-cycle frames a MicroFreak wavetable is
built from). Names read directly out of the first chunk of the `sub=0x57`
block and matched hardware exactly: slot 0 "Convergence", 1 "Funky Bell", 2
"Pluck Additive", 3 "Pluck Odd", 4 "Pluck Resonant", 5 "Iris", 6 "FM
Triangle", 7 "Mellovox", 8 "Shrrom", 10 "My DM", 11 "SawQ", 12 "Potion", 13
"Subt[ract?]", 14 "Little Monster", 15 "Sine Bells" — slot 9 wasn't present
in this capture at all (no `0x57` or `0x55` block for it), and slot 8's
`lo=0` frame came back with **0 chunks** and `lo=1..3` weren't even
requested — both looked like "this slot has no wavetable loaded," not
protocol gaps, at the time.

**Correction from the live tool:** slot 9 is *not* actually empty — reading
it live came back `'Nache'` with all 4 frames present. So that capture had a
dropped/incomplete transaction for slot 9 specifically, the same failure
mode already identified for the preset-read stragglers in section 12 (a
clean write-side capture showed zero size variance where an earlier
read-side capture showed a few). Passive captures apparently lose the odd
transaction; live queries with this project's own retry-free but
correctly-sequenced code don't seem to, at least not yet — worth remembering
if something looks unexpectedly missing again.

**Live tool:** `mfreak wavetable read SLOT` implements the exchange above (select
→ ack → drain 147 chunks, once for the name block and once per frame).
Verified against real hardware: slot 0 ("Convergence") and slot 9 ("Nache")
both read back cleanly, 4/4 frames each. An empty slot's select still gets
acked normally, but the device never answers the first data pull — observed
directly in the capture as MCC waiting about a second before giving up and
moving to the next item — so the tool treats a timeout on a block's *first*
chunk as "no data here" rather than an error (a timeout after that point
would be a real problem and still raises).

#### Writing a wavetable — CONFIRMED, and verified safe on real hardware

`captures/mcc-wave-sent-to-synth.pcapng`: MCC sending a wavetable *to* the
device. Per slot:

    ->  F0 00 20 6B 07 01 <seq> 03 56 <slot> 00 00 F7      select name write (tail=00)
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7
    ->  F0 00 20 6B 07 01 <seq> 00 15 F7                    ready
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7
    ->  F0 00 20 6B 07 01 <seq> 20 16 <32 bytes> F7         ONE chunk only
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7
    ->  F0 00 20 6B 07 01 <seq> 08 17 00 00 00 00 00 00 00 00 F7   fixed marker, purpose unknown
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7

    ->  F0 00 20 6B 07 01 <seq> 03 54 <slot> <frame 0-3> 01 F7   select frame write (tail=01)
    <-  ... same ready + 147-chunk stream as any other data block, once per frame ...

Read and write use **different sub-bytes for the same role** — name is
`0x57` to read, `0x56` to write; data is `0x55` to read, `0x54` to write,
i.e. write = read − 1 for both. (Presets don't follow this pattern — their
write-side data-select was `0x52`, unrelated numerically to the read-side
`0x19` — so this is a wavetable-specific coincidence, not a general rule.)

The single write chunk for the name is a genuine simplification, not a
partial send: reading that same block back returns 147 chunks, but only 3 of
them are ever distinct — the read side is mostly redundant padding, and the
device is fine with receiving just the one chunk that actually matters. Data
frames don't get this shortcut — writing one requires the full 147 chunks,
confirmed by exact count reconciliation across the whole capture (16 name
chunks + 4×147×16 = 9408 data chunks = 9424 total write-direction chunks,
matching exactly).

MCC also **reads every wavetable back after writing it** — 5 read blocks
per slot (735 chunks × 16 = 11,760, also reconciling exactly) — its own
verification pass, conceptually identical to what `--no-verify` disables in
the tools below.

The `08 17 00 00 00 00 00 00 00 00` marker (18 bytes total) is sent
byte-for-byte identical every single time (16 occurrences, zero variation) —
clearly a fixed structural marker rather than content-carrying, but its
actual purpose is unknown. Replayed verbatim since nothing more specific is
understood about it.

**Live tools, verified against real hardware:** `mfreak wavetable write
DEST_SLOT` implements the write side exactly as above — dry run by default,
`--yes` to send, `--from`/`--name` like `mfreak preset write`. Tested live:
`mfreak wavetable write 0 --yes` (round trip to itself) wrote the name chunk,
marker, and all 4 frames, and the post-write verify read came back
byte-identical.

The name-block layout (comparing chunk 0 across all 16 slots): byte 10 *and*
byte 19 of the 32-byte payload both echo the wavetable slot number (`hi`)
redundantly; the name string starts at byte 23, but isn't a clean
null-terminated run — there's a stray `0x00` at name-relative offset 2 and
another at offset 10 in every single example regardless of the actual name
text (e.g. "Fu[0]nky Bel[0]l", "Co[0]nvergen[0]ce", "Pl[0]uck Add[0]itive" —
delete just those two bytes and every name reads correctly). Consistent
enough across 15 different names that it's clearly structural, not
coincidental, but *why* those two positions specifically — unconfirmed.

#### Emptying a wavetable

`captures/wavetable-1-6--10-13.pcapng`: MCC sending a 16-slot bank with
slots 7–9 and 14–16 (panel numbers) empty. MCC writes all 16 slots the same
way, the empty ones included. An empty slot's name chunk sets byte 4 of the
32-byte payload to `0x08`, the same value as the preset name record's empty
flag, and leaves the name field all zero:

    00 06 00 00 08 00 00 00 00 00 06 00 01 01 00 00 ...   slot index 6, empty
    00 05 00 00 00 00 00 00 00 00 05 00 01 01 49 72 ...   slot index 5, "Iris"

Its four frame blocks hold a placeholder, identical for all six empty slots:
32 single cycles of 256 samples, a sine at the first that morphs to a
full-scale triangle by the last. It matches none of the factory wavetables.

An emptied slot still answers reads. The name block comes back with the flag
set and the frames come back in full, unlike a slot that was never written,
which never answers the first data pull. Readers have to check the flag.

On the instrument, a name chunk with the flag set and no frame writes
empties a slot. The flag reads back and the old frames stay as they were.
`mfreak wavetable erase` does this.


## Reading

### 11. Reading a single preset — CONFIRMED

This directly answers "how do we read one preset without a full sync,"
motivated by discovering MCC's own UI has no such option — Presets, Wavetables
and Samples pages only support "get everything" or "send everything to the
device," never a single slot. So the only way to see a real single-item
transfer was to capture MCC's *dedicated* per-domain export operations
(`captures/mcc-presets.pcapng`, 512-preset export via the Presets page) rather
than the connect-time sync, and look at their addressing directly.

The preset-name request (section 4/6) has an unused trailing byte that
section 6 assumed was always `0x00` padding. It isn't — it's a mode selector:

    ->  F0 00 20 6B 07 01 <seq> 03 19 <hi> <lo> 00 F7     tail=00: get name  (section 6, unchanged)
    ->  F0 00 20 6B 07 01 <seq> 03 19 <hi> <lo> 01 F7     tail=01: select this slot's patch data

    slot = hi*128 + lo, same encoding as the name request

`tail=01` is answered with the same ack seen in section 10 (`00 15`, 10
bytes), then MCC pulls the block exactly as in section 10 (`01 18 00` →
`20 16`/`20 17` chunks, 32-byte payload each). **This is per-preset**,
verified directly: in `mcc-presets.pcapng`, slot and select-block alternate
1:1 for all 512 slots in order (name request → name reply → select `tail=01`
→ block), and 399 of the 512 slots got a block at all — the other 113 are
presumably `Init` slots with nothing to dump (close to, though not exactly,
the 63 `Init` slots noted in section 4 from a different capture/preset bank;
not reconciled).

Block size is essentially constant: **146 chunks** for 391 of the 399 blocks
(4672 payload bytes), with 8 one-off outliers (13, 76, 91, 159, 214, 219, 224,
234 chunks) not yet explained — dropped/retried transactions during capture
are the leading guess, not real per-preset size variation, since even
maximally complex-sounding presets (arp+sequencer patches) got exactly 146.

**Practical upshot:** reading one preset's raw block is now a fully specified,
previously-observed (hence safety-note-compliant, section 8) 3-message
exchange:

    ->  F0 00 20 6B 07 01 <seq> 03 19 <hi> <lo> 01 F7
    <-  F0 00 20 6B 07 01 <seq> 00 15 F7
    ->  F0 00 20 6B 07 01 <seq> 01 18 00 F7      (repeat until a 0x17-sub chunk arrives)
    <-  F0 00 20 6B 07 01 <seq> 20 16/17 <32 bytes> F7

The 32-byte payload layout within a block is still opaque (see section 13) —
this resolves *how to ask for one preset's data*, not yet *what the bytes in
it mean*, so there's no decoder for it yet, only a fetcher.

**Live tool:** `mfreak preset read SLOT` implements exactly this exchange over
an ordinary MIDI port -- the instrument is class-compliant, so no USB capture
and no Arturia driver are needed for this. (The backend was `python-rtmidi`
when this was written and is `rtmidi2` now; the exchange is unchanged.) **Verified against real hardware**: slot 0 came back as
`'NervousKeys'`, 146 chunks / 4672 bytes, matching `mcc-presets.pcapng`
exactly.

That live read also gave the first real look inside a preset's payload, and
it's a good lead for section 13's "main remaining prize": the block is not
opaque binary — it's substantially **self-describing**. Readable parameter
names run through it in sequence: `VCODType`, `Param1`-`3`, `BendRng`,
`VCFDType`, `Cutoff`, `Reso`, `EG1DMode`, `RiseLvl`, `RiseSlp`, `FallLvl`,
`FallSlp`, `Amount`, `KbdEGlide`, `Octave`, `Velocity`, `Hold`, `GlMode`,
`PresAmp`, `ArpEnable`, `Range`, `CDiv`, `Rate`, `Swing`, `Sync`, `Dir`,
`SeqOn`, `LFODShape`, `Div`, `Rate`, `Sync`, `Retrig`, `EG2DMode`, `Attack`,
`Dec`, `Rel`, `Sustain`, `Legato`, `GenPara`, `Panel`, `PolCnt`, `PrstVol`,
`Volume`, `SysPrset`, `ID`, `PrsetBt`, `Save`, `Utility`, `MatFMatEnc`,
`MatBtn`, `Assign1`-`3`, then seven repeats of `Co<N>` (`N`=1–7) each
followed by `EG1`, `EG2`, `CLFO`, `Xpr`, `Key` — almost certainly the 7-slot
modulation matrix, one connection per `Co<N>` block with its 5 possible
sources — then `SeqLength`, `GateLen`, `Smooth1`-`4`.

**Update — this reading was of still-7-bit-packed data; the real structure
is cleaner.** Section 12 (samples/wavetables) later established the general
`cmd 0x20` chunk payload is MIDI SysEx 7-to-8 packed (see
[SAMPLE_ENCODING.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/SAMPLE_ENCODING.md)). That turns out to apply to presets too, confirmed
directly: decoding a real captured preset block (slot 0, "NervousKeys")
with `decode_7to8()` (146 chunks / 4672 wire bytes → 4088 real bytes, since
4672/8=584 groups exactly) makes the parameter table dramatically cleaner —
compare raw `".#VCODTy.pec.._F.Param1c.n .FPar"` against decoded
`"#VCODTypec.._FParam1c...FParam2c"` — names read correctly with the
7-to-8 header-byte noise removed, instead of appearing fragmented.

Byte-level pattern once decoded, from the first ~90 bytes of slot 0:
`23 56 43 4F 44 54 79 70 65 63 0C FF 5F 46 50 61 72 61 6D 31 63 ...` =
`# V C O D T y p e c [0C FF] _ F P a r a m 1 c ...`. Reads as: **`#` starts
a new parameter group** (seen before `VCODType`, `VCFDType`, `EG1...`);
each field is **`<type-tag>` + `<name>` + `c` + `<value bytes>`**, where the
type tag (`F`, `D`, `G`, `_`, `C`, ... seen so far) is a single character
immediately before the name, `c` is a constant one-byte delimiter after
every name with no exception seen, and the value's byte width appears to
vary with the type tag (2 bytes after `VCODType`'s tag, 4 after `BendRng`'s,
3 after `Cutoff`'s — not yet mapped to which tag means which width). This
looks like a genuine parameter-reflection/serialization scheme, structurally
similar in spirit to the `.mfsz`/`.mfwz` local files, just binary instead of
Boost's text archive format. **Not decoded further than this** — the
type-tag meanings, exact value widths, group boundaries beyond `#`, and full
field count are all still open; this is recorded as a confirmed starting
point for that work, not a finished decoder.

### 4. Preset name record (cmd 0x23) — CONFIRMED

    F0 00 20 6B 07 01 <seq> 23 52 .. <hi> <lo> .. <b19> <b20> <name...> 00 .. F7
                                      byte9 byte10        byte21 onward

- **Slot number = `byte9 * 128 + byte10`.** Verified: 512 records produced
  exactly the values 0–511, no gaps and no duplicates. That is not chance.
- **Name** is plain ASCII from byte 21, NUL-terminated, padded with zeros.
- 449 of the 512 slots carry a real name; the rest read `Init`.

Decoded list is in `presets.txt`, regenerated by a private-workbench script
(not in either published repository).


## Writing

### 12. Writing — CONFIRMED for presets, first-look skeleton for samples

#### Presets: fully confirmed, and verified safe on real hardware

`captures/mcc-sent-to-synth.pcapng`: MCC sending a full preset bank *to* the
device (Presets page → send-to-device). Critically, **the bank sent was the
same one just read back from the device** — a content-preserving round trip,
so this capture exercises the real write commands on real hardware without
risking any actual data loss. This is the cleanest capture yet: every count
reconciles exactly.

Per slot, in order:

    ->  F0 00 20 6B 07 01 <seq> 03 19 <hi> <lo> 00 F7          read current name first (sanity check)
    <-  F0 00 20 6B 07 01 <seq> 23 52 .. <hi><lo> .. <name> .. F7

    ->  F0 00 20 6B 07 01 <seq> 23 52 .. <hi><lo> .. <name> .. F7   write name — byte-identical record format
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7                             generic ack

    ->  F0 00 20 6B 07 01 <seq> 03 52 <hi> <lo> 01 F7          select this slot's data for WRITE
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7
    ->  F0 00 20 6B 07 01 <seq> 00 15 F7                        MCC-sent "ready"
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7
    ->  F0 00 20 6B 07 01 <seq> 20 16 <32 bytes> F7  ×145        data chunks, each individually acked `00 18`
    ->  F0 00 20 6B 07 01 <seq> 20 17 <32 bytes> F7               final chunk (146th)

The name record (`23 52 ...`) is **exactly** the format section 4 documented
for the device's name *replies* — MCC just sends the identical structure back
to write one, no separate write-specific format. The data-select reuses the
same shape as the read-side select (section 11's `03 19 <hi> <lo> 01`) but
with sub-byte `0x52` instead of `0x19` — read and write are otherwise
symmetric.

**Counts, and what they resolve:**

- **512** name-writes — every slot, `Init` ones included.
- **449** data-selects (`03 52`), each followed by **exactly 146** chunks, no
  exceptions. This is the real per-preset patch-data size, confirmed with no
  variance at all. It resolves section 13's open question about the 8
  non-146/147-sized blocks seen in the original read-only `mcc-presets`
  capture — those were dropped/incomplete transactions during that capture,
  not real size variation, now that a clean capture shows zero variance.
- 449 also matches section 4's independently-counted "449 of 512 slots carry
  a real name" exactly — data only gets written for real presets; `Init`
  slots get a name write and nothing else, matching the read side (section
  11) where `Init` slots got no data block either.
- The 1024 name-*read* requests (`03 19 ... tail=00`, double the slot count)
  are MCC reading each name twice — once in an initial pass over all 512
  slots, once again immediately before writing each one. Not fully explained,
  but harmless (a pre-write consistency check is the obvious guess).

**Live tool, verified against real hardware:** `mfreak preset write DEST_SLOT`
implements exactly this sequence. Requires `--yes` to actually send (bare
invocation is a dry run); defaults to reading the destination slot's own
current content as the source, so the default invocation is a no-op round
trip. Also supports `--from SRC` (clone a preset to a new slot) and
`--name NEWNAME` (rename in place, only touching the printable-name bytes of
the 45-byte record — every other byte, including the still-unexplained
fields at bytes 19/20, is carried through unchanged from what was read).
Verifies by reading the slot back after writing and comparing.

Tested live: `mfreak preset write 0 --yes` (slot 0 written back to its own
current content) completed all 146 chunks and the post-write verify read
came back byte-identical to what was sent. First confirmed round trip
through this project's own code, not just observed from MCC.

#### Samples: first look (CONFIRMED skeleton, not yet safe to replay)

`captures/mcc-samples.pcapng`: MCC sending Arturia's factory sample set *to*
the device (Samples page → send-to-device; there is no read-back/export for
samples, matching the "no single-item ops" finding above — this whole capture
is a write). 309,350 host→device SysEx messages. This is the first look at
the write direction at all, for anything.

**Startup handshake**, seen both here and once more at the very end of the
`mcc-sync` dump phase (the `01 47` anomaly from section 10):

    ->  F0 00 20 6B 07 01 <seq> 01 47 0A F7                              11 bytes
    <-  F0 00 20 6B 07 7F <seq> 09 48 47 7D 40 7F 19 00 20 0A 00 F7      19 bytes, `07 7F` class

Sent twice at the very start of the sample upload (~0.6s apart), then not
again for the rest of the transfer — a status/readiness query, not specific to
writing. Not decoded (the `07 7F` class is the same one section 6 already
flagged as unexplained).

**Per-item write:**

    ->  F0 00 20 6B 07 01 <seq> 03 5D <hi> <lo> 00 F7     select (purpose A)
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7                   ack (generic — see below)
    ->  F0 00 20 6B 07 01 <seq> 00 15 F7                   MCC-sent "ready" (mirrors the read-side device ack)
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7
    ->  F0 00 20 6B 07 01 <seq> 20 17 <32 bytes> F7        single-chunk record — sample name/metadata
    <-  F0 00 20 6B 07 01 <seq> 01 16 01 F7                 non-generic ack, purpose unclear
    <-  F0 00 20 6B 07 01 <seq> 00 18 F7
    ->  F0 00 20 6B 07 01 <seq> 03 5A <hi> <lo> 00 F7     select (purpose B) — same metadata chunk resent
    ->  F0 00 20 6B 07 01 <seq> 03 58 <hi> <lo> 01 F7     select (purpose C, tail=01 like the preset-data select)
    ->  F0 00 20 6B 07 01 <seq> 20 16 <32 bytes> F7  ×N   the actual sample audio data, chunked
        ...
    ->  F0 00 20 6B 07 01 <seq> 20 17 <32 bytes> F7        final chunk

Every single message MCC sends in this whole flow — select, ready, and every
data chunk — gets a `00 18` ack back from the device with the seq echoed,
sent before MCC's next message. This looks like a generic per-message
transport ack that the read side simply doesn't need (there, the *reply*
either was the ack in another sense, so nothing extra follows it) — cmd
`0x01 sub 0x18` on the read side ("give me next") and cmd `0x00 sub 0x18`
here (receiver's ack) share sub-byte `0x18`; plausibly not a coincidence, but
unconfirmed.

One embedded, readable name showed up directly in a chunk MCC *sent*:
`...Pian.o Pop.` → "Piano Pop", confirming (independent of section 11's
`Convergence` sighting) that human-readable names really are embedded
directly in these 32-byte chunk payloads, not fetched separately.

#### `hi`/`lo` addressing, decoded from the full request stream — and a correction

**Correction:** the rest of this section originally described `hi` as
addressing a "bank" containing multiple samples, with `03 58` selecting each
one. That was wrong, caught by direct comparison against the real MCC UI
(a flat list of 128 slots, 1-indexed there, so this document's `hi`=`N`
is MCC's "slot `N+1`"). **Each slot holds exactly one sample.** What looked
like "6 to 75 samples in a bank" is really the number of sequential transfer
*blocks* one sample needed — repeated `03 58` reselects for the *same* slot,
each covering up to 147 chunks, chained until the whole (potentially long)
sample has been sent, not 6 to 75 separate short recordings. Confirmed
directly: slot 50 ("Vinyl") is 41 blocks chained together, 6027 chunks /
192,864 bytes for one sample — not 41 different sounds.

`03 5D` and `03 5A` each occur **exactly 128 times**, always `lo=0`, `hi`
running the complete 0–127 range, always paired (every `hi` that gets a `5D`
also gets a matching `5A` immediately after). Reads as "prepare" +
"confirm/clear" for **all 128 slots**, done unconditionally regardless of
whether a slot ends up with content — plausibly clearing the destination
before any real upload begins.

`03 58 <hi> <lo=0> tail=01` — same `hi` as the slot being filled — starts (or
continues) that slot's sample transfer; a longer sample means more `03 58`
reselects for the same `hi`, each one just picking up where the last block
left off (no per-block addressing at all — see `mfreak sample write`'s
`send_audio`). Only slots `hi=0..51` got any `58` selects — the other 76
(`52..127`) got their `5D`/`5A` prepare/clear pair and nothing else, i.e.
empty slots. Block counts per slot vary realistically (6 to 75), matching
real, differently-long factory recordings rather than a fixed-size structure:

    hi= 0: 36 blocks   hi= 9: 49   hi=18: 11   hi=27: 55   hi=36: 67   hi=45: 25
    hi= 1: 32          hi=10: 61   hi=19: 30   hi=28: 59   hi=37: 40   hi=46: 44
    hi= 2: 36          hi=11: 40   hi=20: 36   hi=29: 59   hi=38: 23   hi=47: 32
    hi= 3: 17          hi=12: 49   hi=21:  8   hi=30: 24   hi=39: 20   hi=48: 75
    hi= 4: 15          hi=13: 44   hi=22:  8   hi=31: 43   hi=40: 32   hi=49: 38
    hi= 5: 31          hi=14: 43   hi=23:  8   hi=32: 55   hi=41: 46   hi=50: 41
    hi= 6: 32          hi=15: 59   hi=24:  6   hi=33: 36   hi=42: 21   hi=51: 32
    hi= 7: 37          hi=16: 64   hi=25:  8   hi=34: 58   hi=43: 69
    hi= 8: 35           hi=17: 35   hi=26: 70   hi=35: 22   hi=44: 27

(1943 total `03 58` selects across all slots, i.e. 1943 transfer blocks —
matches the raw count exactly.) Each populated slot's name, read from its
`03 5D` name-write chunk: 0 "Piano Pop", 1 "Piano Lof", 2 "Piano Clen", 3
"Shamisen", 4 "String Stc", 5 "E Piano", 6 "Acc Guita", 7 "PGTS Keys", 8
"Harp", 9 "Braam", 10 "Cyber Pad", 11 "SQ Choir", 12 "Synth Layr", 13 "Unison
Pa", 14 "Voice Aah", 15 "Wind Pad", 16 "Acc Strins", 17 "Morph Lea", 18 "808
Drive", 19 "Neuro", 20 "Pluck", 21 "Formant", 22 "Harmonic", 23 "Spector",
24 "Spectral", 25 "Vocal", 26 "Blue Jinge", 27 "Chord Jinl", 28 "Major
Jinl", 29 "Dub Jingl", 30 "Harp Arp", 31 "Synth Arp", 32 "Emotion", 33 "Eth
Bells", 34 "Lofi Chors", 35 "Voice Rif", 36 "Bird Pad", 37 "Old FX", 38
"Water Dro", 39 "Zap", 40 "Future Drm", 41 "Drum Brea", 42 "Harsh FM", 43
"Three Wors", 44 "Digital Vi", 45 "Furniturei", 46 "Water Stra", 47
"Aliasing", 48 "Birds", 49 "ProVS Noie", 50 "Vinyl", 51 "Disco Loo". Slots
52–127 carry only placeholder names (`Factory-A0` through `Factory-A9`,
cycling) and no audio.

#### Reading samples back — investigated, negative result

Before writing anything, checked whether `0x5B` (flagged unexplained in
section 10's original writeup — variable-size blocks in `mcc-sync`, unlike
the fixed wavetable blocks) might be a sample *read* path, by analogy with
the write side's `0x58`. Confirmed using only already-captured traffic, no
new hardware queries: `0x5B` is a **slot name/metadata read-back**, not a way
to read sample audio. Direct evidence: right after MCC finished writing slot
0 in `mcc-samples.pcapng`, it read back a 147-chunk block via `03 5B 00 00
01` whose first chunk contains the exact "Piano Pop" name bytes it had just
written — MCC verifying its own write, the same role `0x5B` plays scattered
through `mcc-sync`'s connect-time traffic (different content each time,
consistent with "whatever the current slot's name is" rather than fixed/
garbage data as first guessed). There is no equivalent for sample *audio* —
nothing in any capture reads raw sample bytes back off the device. Given
that, and MCC's UI having no export-samples feature at all, this looks like
a real protocol gap rather than something not yet found: **there does not
appear to be a way to transfer individual samples off the synth.**

#### Writing a sample — protocol replays cleanly, but content doesn't land on a new slot

Built `mfreak sample write`, implementing the sequence above (now generalized to
chain as many `03 58` blocks as the sample needs — see the correction above)
plus a post-write check using the `0x5B` read-back. First tested live with a
single-block sample: extracted "Piano Pop"'s exact name-chunk and 147-chunk
audio payload from `mcc-samples.pcapng` (known-good, real content — not
invented, since the real audio encoding this format expects isn't understood;
see below) and wrote it to slot 53, confirmed empty beforehand.

**The write completed with zero protocol errors** — every select got its
expected ack, the special `01 16 01` ack arrived exactly once as expected,
all 147 chunks were individually acked. But the `0x5B` read-back afterward
showed slot 53 **unchanged** — still the exact same all-zero-plus-`04 00 00
7F` signature every other empty slot shows, not "Piano Pop". Sanity-checked
the read-back mechanism itself against slot 0 (known populated) in the same
session to rule out a parsing bug — that one reads back correctly. So this
looks like a real negative result, not a verification-tool bug: **the device
accepted and acked the write transport-wise but did not store the content.**

Nothing was lost — slot 53 was empty before and is empty after. Leading
hypothesis: the ~52 populated sample slots may be a fixed, firmware-defined
set (built-in categories) that MCC's upload path fills in but can't actually
expand — slot 53 may have been silently rejected as out-of-range rather than
accepted as newly populated. Not yet tested against a slot already known to
be populated (which would actually distinguish "writes never take" from
"only new slots are rejected") — see the next entry, which does exactly that.

**Retested after fixing the block-chaining bug**, with "Vinyl" (41 real
blocks) instead of "Piano Pop" (1 block), to rule out the single-block write
itself being the problem rather than the slot being new. Same result: all 41
blocks acked cleanly, `0x5B` read-back still shows slot 53 empty afterward.
Reproducible across two different samples, one single-block and one
41-block — this is specifically about the slot having never been populated,
not an artifact of sample length or the earlier chaining bug.

#### A full 128-slot touch sweep doesn't unlock a new slot either

`captures/send-1-sample-bank.pcapng` (MCC sending just *one* new sample)
shows MCC doesn't only touch the target slot — it does `03 5D` + resend name
+ `03 5A` + resend name for **all 128 slots**, not just the one getting new
content, then a `03 5B` verify for each. Order: the target slot's full
write+verify happens first, then the sweep continues through every other
slot with just the touch-and-verify (no audio). Since this project has no
cached copy of every slot's name the way MCC does, `write_sample_new_slot.py`
reads each of the other 127 slots live via `03 5B` first and resends that
unchanged, to replicate the same shape of session.

> **RESOLVED (2026-09-04) — this whole line of investigation was a dead end.**
> The sweep is not required and never was. Writing to an empty sample slot
> failed because the name chunk's decoded byte 23 — the slot index the device
> actually routes on — was left at 0, so every write landed on slot 0
> regardless of the slot named in `03 5D`/`03 5A`/`03 58`. That also explains
> why slot 0 kept getting clobbered. Fixed in `mfreak sample write` and verified
> live; `write_sample_new_slot.py` has been removed. See FORMATS.md.

Tested against slot 53 with a fresh 41-block "Vinyl": the full sweep
completed (all 128 slots touched, target written), but the post-sweep
`0x5B` check still showed slot 53 empty. **The full sweep alone isn't
sufficient either** — narrows things down: it's not simply "MCC always
touches every slot and that's the missing ingredient."

(Ambiguous data point from the same run, noted for completeness rather than
explained: slot 0 read back `'bee buzz'` mid-sweep — correctly reflecting
its live content at the time, since the user had deleted the whole library
and loaded only that one sample — yet a later independent check showed slot
0 back to `'Vinyl'`. Slot 0 wasn't the sweep's target and the code path for
untouched slots only ever resends what it just read, so there's no
mechanism in the script that should cause this. Not reproduced deliberately,
not chased further — flagged here in case it recurs.)

#### Ruling out "fixed 52 slots" — it's current occupancy, not index

The leading hypothesis until this point was that slots 0–51 form a fixed,
firmware-defined writable set and 52+ are permanently locked out. Testing
this required a slot that (a) currently has no content, but (b) has an
index that *used to* hold real factory content, to separate "index range"
from "current state" as the actual variable. With the sample library
deleted down to a single entry, slot 1 (`hi=1`, MCC's "slot 2", historically
"Piano Lof") was exactly that.

Result: same silent failure as slot 53. `mfreak sample write 1 --name Vinyl
--audio-file ...` acked every message cleanly, but the post-write `0x5B`
check came back empty, not `'Vinyl'`. **This rules out the fixed-52-slots
theory** — `hi=1` is well within the historically-populated range and still
didn't take. The real distinguishing factor looks like *whether the slot is
currently occupied*, not which index it is: an already-populated slot
(section below, slot 0) accepts overwrites; any currently-empty slot,
regardless of index or history, doesn't accept new content through this
write path alone. What specifically re-arms an empty slot for writing —
if anything short of MCC's full UI-driven import flow — is still unknown.

#### Writing a multi-block sample to an already-populated slot — CONFIRMED, audio and all

With the block-chaining bug fixed, tried the real test the single-block one
couldn't answer: cloning slot 50 ("Vinyl", 41 blocks / 192,864 bytes — a
genuinely multi-block sample, unlike "Piano Pop"'s single block) onto slot 0
(currently "Piano Pop"), deliberately overwriting real content this time to
find out whether writes to an *already-populated* slot behave differently
than the empty-slot case above.

**It worked.** All 41 blocks (6027 chunks) sent and acked cleanly, and the
`0x5B` read-back afterward confirms slot 0's name is now `'Vinyl'`, not
`'Piano Pop'` — a real, verified state change, unlike slot 53's silent
no-op. This strongly supports the leading hypothesis above: **the ~52
populated slots form a fixed, writable set; write access to genuinely new
(previously-empty) slots is what's blocked, not writing in general.**

One limit worth being explicit about: the `0x5B` check only confirms the
*name* changed, not the audio — there's still no read-back for that. So this
was checked by ear: listening to slot 1 (MCC's 1-indexed UI) on the device
confirmed it — **it sounds right**, i.e. it now plays "Vinyl"-like content,
not "Piano Pop." That's full end-to-end confirmation, both metadata and
actual waveform data, on a real already-populated slot.

#### The audio encoding — SOLVED

`mfreak sample write` above could only replay a *known-good* sample's exact
captured bytes; it couldn't write a genuinely new recording, because the
raw audio encoding — sample rate, bit depth, endianness, and how 8-bit-plus
data fits into 7-bit-clamped SysEx bytes — wasn't known. **This is now
fully solved.** Full writeup in [SAMPLE_ENCODING.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/SAMPLE_ENCODING.md); summary:

- **16-bit signed PCM, little-endian, 32kHz, mono** — exactly matching the
  product's own spec ("26-bit" was indeed a mishearing of 16-bit).
- Audio is grouped into 2048-sample (4096-byte) blocks, each padded with 20
  zero bytes to 4116 "real" bytes.
- Each 4116-byte block is packed with the **standard MIDI SysEx 7-to-8
  scheme** (7 real bytes → 8 wire bytes, one header byte carrying the 8th
  bit of each of the following 7) into exactly 4704 wire bytes — the fixed
  147-chunk block size used everywhere in this protocol. Very likely the
  general encoding for the whole `cmd 0x20` chunk family, not
  sample-specific — worth checking against preset data (section 11) too.

Cracked via a known-plaintext attack using MCC's own `.mfsz`/`.mfwz` local
export files, which turned out to be ZIP archives containing a
human-readable Boost.Serialization text array of the internal PCM data —
compared directly against real captured wire bytes for both a wavetable
(this project's own already-verified "Convergence" data) and a sample
("bee buzz"), giving 100% exact matches once the block-padding and 7-to-8
packing were accounted for. `mfreak sample encode` implements the encoder and
was verified **live on real hardware**: a WAV encoded from scratch (not
replayed from any capture) was written to a slot and confirmed correct by
ear.

### The write sequence (from the capture, not inferred)

```
--> 03 19  <hi> <lo> 00           read destination's current name record
<-- 23 52  ...                     device returns it
--> 23 52  <45-byte record>        write it back, empty flag CLEARED
<-- 00 18
--> 03 52  <hi> <lo> 01            arm transfer
--> 00 15                          begin
--> 20 16  <32 bytes>   x145       data chunks
--> 20 17  <32 bytes>   x1         final chunk + commit
```

Every message is acked with `00 18` carrying the same `seq` -- 146 acks in
the transfer window, strictly lock-step. `seq` is a 7-bit counter that wraps
127 -> 0 (the 146 chunks run `1..127, 0..18`).

### Why writes to an empty slot silently fail

The status byte is checked on both paths, and **they behave differently**:

- **Read** (`0x08046708`) hard-fails:
  `ldrb r2,[rec,#11]` / `cmp r2,#255` / `beq 0x8046782` -> `movs r0,#1; bx lr`.
- **Write** (`0x08046790`, reached from opcode `0x17`) does *not* fail. It
  branches to `0x0804680A`, which loads `r7 = 0xCD0000` -- the region base,
  ignoring `rec+4` -- and proceeds with the write.

So writing to an unallocated slot **looks successful**: the device ACKs every
chunk with `00 18`. But the data lands at the region base instead of the
slot's own offset, the status byte stays `0xFF`, and the slot still reads
back empty. Every unallocated slot writes to the same address, so repeated
attempts overwrite each other.

This is the root cause of "can't write unless MCC populated the slot first".

### Live hardware results (2026-09-04)

Run against a real MicroFreak on firmware 5.x, with `python-rtmidi 1.5.8` as
the MIDI backend -- what the tool used at the time. It uses `rtmidi2` now; on
2026-10-17 one slot was read through both backends and the 4,672 bytes were
identical, which is the evidence for the swap, not for these results.

#### Read-only probe: the model holds

Presets read back exactly as documented -- slot 0 occupied
(`byte12=0x00`, `NervousKeys`), slots 1-9 empty (`byte12=0x08`, `Init`).
Samples decode too: slot 0 reported `('buzzOK', 144812)` and slots 1-9
`('', 0)`. Note 144812 is within 512 bytes of the old hardcoded 'Piano Pop'
length (145324) and nowhere near the audio that was actually sent -- the
hardcoded header's length is what had landed.

#### Presets: FIXED and hardware-verified

- **Regression** (write populated slot 0 back to itself): `verify OK:
  read-back matches exactly`. The new bytes 17/18 writes break nothing.
- **Blank slot** (clone slot 0 -> empty slot 5, renamed): `verify OK:
  read-back matches exactly` on name *and* all 4672 payload bytes. Slot 5
  afterwards reads `byte12=0x00`, `b17=5`, `b18=0`.

Writing a preset to a never-populated slot now works.

#### Samples: NOT fixed -- and the reason is worse than a wrong length

Three separate attempts to write a sample to an empty slot (3, 3, then 1),
including one replaying MCC's exact name-chunk bytes byte-for-byte, all
behaved identically: the target slot stayed `('', 0)` and **the data landed in
slot 0**, overwriting whatever was there.

That is the `0xFF`-gate fallback from `0x08046790` observed live: an
unallocated slot's write goes to `0xCD0000 + 0`, which *is* slot 0's region.
The slot argument in `03 5D` / `03 5A` / `03 58` is effectively ignored when
the directory entry does not exist.

Two hypotheses tested and **disproven**:

1. **Wrong declared length.** Replaying MCC's exact chunk for 'bee buzz'
   (`audio_len=371712`, `unknown=f5 2b`) to slot 3 failed the same way. The
   length fix is necessary but not sufficient.
2. **decoded `[0:4]` is the flash offset.** It is 0 in every chunk ever
   observed, and every observed chunk was for slot 0 -- whose offset would be
   0 -- so this looked promising. Writing slot 1 with `[0:4] = 372736` still
   landed on slot 0. Not the allocator.
3. **The 128-slot "touch sweep"** (`write_sample_new_slot.py`) also failed.
   That script's founding hypothesis is disproven; it can be retired.

#### Why the capture could not have shown this

`captures/send-1-sample-bank.pcapng` writes its sample to **slot 0** -- the one
slot whose correct destination is identical to the fallback address. So it
demonstrates a successful write without ever demonstrating allocation. Nothing
in it distinguishes "allocated correctly" from "fell back to base".

**The missing artifact is a capture of MCC writing a sample to a non-zero,
previously-empty slot** (e.g. MCC UI slot 4 = index 3, with slot 0 left
occupied so the two are distinguishable). Whatever command allocates the
directory entry will be in it and is in none of the captures we have.

### Write protection: a Utility setting, and it is UI-only

The device UI refusing to save over a preset is **`Mem protect`**, a setting in
the Utility menu with three options -- pointer table at `0x08075A1C`:

```
OFF            no protection
Factory only   slots 0-127 protected  (device presets 1-128)
All            every slot protected
```

The blocked-save message is `Memory Protect\nON` at `0x08075234`, shown from
three call sites, each gated on `0x08054DC0`:

```
0x08054DC0  is_write_protected(preset):
    r3 = settings[0x5B]          ; Mem protect, in the globals at 0x200007A8
    if r3 == 2: return 1         ; "All"
    if r3 == 1:                  ; "Factory only"
        slot = preset->slot      ; signed halfword at +2
        return slot <= 127        ; 0x7F boundary, hardcoded
    return 0                     ; "OFF"
```

So the factory range is **hardcoded as slot <= 127**, exactly as the symptom
suggested: slot 5 (device 6) is protected, slot 128 (device 129) is not. A
device set to `Factory only` blocks the first 128 and allows the rest.

**SysEx ignores this entirely.** Verified live: a write to slot 5 succeeded
(`verify OK: read-back matches exactly`) while the UI was refusing to save
there. The check lives in the UI save path, not in the SysEx handler -- which
is consistent with `captures/blank-all-presets.pcapng`, where MCC rewrites all
512 slots including the factory range.

Practical consequence: our tooling can overwrite factory presets regardless of
the device's Mem protect setting. That is convenient and also a footgun -- the
factory presets are not recoverable from this repo, only from MCC.


## The opcode space

### THERE ARE TWO DISPATCHERS — read this before claiming an opcode

**An opcode that bails in the table below is not necessarily free.** A second
dispatcher reads the same opcode byte out of the same message, with its own
table, bias and bound:

```
0x08046F8A  bls   <bail>            a message of 7 bytes or fewer bails
0x08046F8C  ldrb  r2, [r1, #0x7]    the opcode -- same byte, same pointer
0x08046F8E  subs  r2, #0x1c         bias 0x1C, not 0x15
0x08046F94  sub.w r3, r5, #8        r3 = length - 8, then checked per case
0x08046F98  cmp   r2, #0x37         bound 0x37, not 0x48
0x08046F9A  bhi   <bail>            -> 0x08047060, add sp,#0x50 / pop
0x08046F9C  tbh   [pc, r2, lsl #1]  table base 0x08046FA0, bail halfword 0x0060
```

**Ten opcodes bail in dispatcher 1 and have a real handler here**, and each
takes its arguments out of the same bytes a custom handler would want:

| opcode | handler | length guard | message bytes it reads |
|---|---|---|---|
| `0x1C` | `0x0804718A` | none seen | — |
| `0x1D` | `0x08047066` | none seen | — |
| `0x40` | `0x0804716A` | `cmp r3,#4` | `buf[9]` `buf[10]` `buf[11]` `buf[12]` |
| `0x41` | `0x08047146` | `cmp r3,#2` | `buf[9]` `buf[10]` |
| `0x42` | `0x080470D2` | `cmp r3,#1` | `buf[8]` `buf[9]` |
| `0x43` | `0x08047120` | `cmp r3,#0` | `buf[8]` `buf[9]` |
| `0x47` | `0x08047010` | `cmp r3,#0xb` | `buf[8]`, as a 10-way sub-opcode (`ldr.w pc,[r2,r3,lsl#2]`) |
| `0x49` | `0x080470E2` | `cmp r3,#1` | `buf[8]`, likewise |
| `0x4C` | `0x0804712E` | `cmp r3,#1` | `buf[8]` `buf[9]` `buf[10]` |
| `0x53` | `0x08047072` | none seen | — |

`0x43` being here is the resolution of a loose end this file has carried for a
while: **MCC sends `0x43` 93 times** while dispatcher 1 bails on it. It was
never a free opcode anyone was getting away with; its handler is just somewhere
else.

**This cost four freezes and most of two days.** v12–v15 claimed `0x40` for the
vocabulary upload and `0x41` for peek, both on that list, so every message also
ran Arturia's handler with our address bytes as its arguments. `0x41`'s indexes
a pointer array at `0x2001C8C4` by `buf[9]` with no bounds check: `buf[9] >=
0x48` loads `0x1F001F00`, which is unmapped, and every fault vector in this
firmware is `b .`. Smaller values find a live pointer and the handler *does its
job*, over the memory being uploaded into. Full account in
`captures/validated-payloads/README.md`.

**Do not eyeball either table.** `speech_sysex.assert_opcode_free()` reads both
out of the image and is wired into both patch paths; `free_opcodes()` lists the
reachable opcodes that bail in both. Reachable means `0x15..0x5D`, since that
is the range dispatcher 1's table covers and its entry is what gets repointed.

The scan for a third dispatcher is complete: of the **139** TBB/TBH
instructions in the image, exactly three have an index register tracing back to
a byte read at `+7`, and the third (`0x08026214`, `ldrb r2,[sp,#7]`, bias
`0xE1`, bound `0x0E`, with `0xD7`/`0xEF`/`0xF0`/`0xF1` compares around it) is
the MIDI status-byte parser, not SysEx. The backward trace had to find both
known dispatchers before its result was believed — its first version found
neither and would have said "no second dispatcher exists".

**Retracted:** this file used to say "`cmp r2,#0x48` is the only comparison on
the opcode anywhere". `0x08046F98` compares the same byte against `0x37`.

### Dispatcher 1

`sysex_op_dispatch` at `0x0804ACC8`:

```
ldrb  r2, [r1, #0x7]     opcode = buf[7]
subs  r2, #0x15          biased
cmp   r2, #0x48          range check: 73 entries, 0x15..0x5D
bhi.w 0x0804AE60         out of range -> the bail
tbh   [pc, r2, lsl #1]   table base 0x0804AD20
```

**16 of the 73 entries are implemented. 57 fall to this table's bail** — of
which only 47 are actually free, once dispatcher 2 is taken into account.

### The bail, where all 57 free opcodes land

```
0x0804AE60  add   sp, #0x2c
0x0804AE62  pop.w {r4, r5, r6, r7, r8, r9, pc}
0x0804AE66  <- opcode 0x5D's handler starts here
```

Six bytes, a bare epilogue: no reply, no error, nothing. At that point `r2`
still holds the biased opcode and `r5` still points at the message, which is
what makes a trampoline there cheap.

### Every entry

| opcode | handler | status | role |
|---|---|---|---|
| `0x15` | `0x0804B0B0` | confirmed live | ready / read-select ack |
| `0x16` | `0x0804AF60` | confirmed live | data chunk, more follow |
| `0x17` | `0x0804AF9E` | confirmed live | data chunk, final in block; carries the commit path |
| `0x18` | `0x0804AFFC` | confirmed live | generic response -- every reply writes 0x18 into the outgoing opcode |
| `0x19` | `0x0804B018` | confirmed live | preset select, tail 00 = name / 01 = data (read) |
| `0x52` | `0x0804B290` | confirmed live | preset data select (write) |
| `0x54` | `0x0804AEB2` | confirmed live | wavetable waveform-data select (write) |
| `0x55` | `0x0804B2FA` | confirmed live | wavetable waveform-data select (read), lo = frame 0-3 |
| `0x56` | `0x0804AED0` | confirmed live | wavetable name select (write) |
| `0x57` | `0x0804B190` | confirmed live | wavetable name select (read) |
| `0x58` | `0x0804AF0A` | confirmed live | sample audio-block select (write), one per block, chained |
| `0x59` | `0x0804B206` | UNKNOWN | **UNKNOWN** -- implemented, never observed, no recorded role |
| `0x5A` | `0x0804AF28` | role unclear | sample slot clear/confirm -- addressing confirmed, role unclear |
| `0x5B` | `0x0804B116` | confirmed | sample slot name/metadata read-back (verify) |
| `0x5C` | `0x0804AF32` | UNKNOWN | **UNKNOWN** -- implemented, never observed, no recorded role |
| `0x5D` | `0x0804AE66` | role unclear | sample slot prepare (write) -- role vs 0x5A unclear |

**Bails in THIS table (57), which is not the same as free:** `0x1A` `0x1B`
`0x1C` `0x1D` `0x1E` `0x1F` `0x20`–`0x3F` `0x40` `0x41` `0x42` `0x43` `0x44`
`0x45` `0x46` `0x47` `0x48` `0x49` `0x4A` `0x4B` `0x4C` `0x4D`–`0x51` `0x53`

**Actually free — bails in BOTH dispatchers (47):** `0x1A` `0x1B` `0x1E` `0x1F`
`0x20` `0x21` `0x22` `0x23` `0x24` `0x25` `0x26` `0x27` `0x28` `0x29` `0x2A`
`0x2B` `0x2C` `0x2D` `0x2E` `0x2F` `0x30` `0x31` `0x32` `0x33` `0x34` `0x35`
`0x36` `0x37` `0x38` `0x39` `0x3A` `0x3B` `0x3C` `0x3D` `0x3E` `0x3F` `0x44`
`0x45` `0x46` `0x48` `0x4A` `0x4B` `0x4D` `0x4E` `0x4F` `0x50` `0x51`

Regenerate rather than trust this list: `speech_sysex.free_opcodes(img)`.

**Currently spent by our patch:** `0x45` claimed (vocabulary upload), `0x44`
claimed (peek), `0x46` claimed in `--peek-only` builds (nibble peek), `0x48`
claimed by every patched build (which patch is running, section 2), and
`0x35`/`0x36` **sacrificed** — their dispatcher-1 entries hold the trampoline's
instruction bytes, so their `tbh` still jumps for them and lands mid-branch.
They are free in both tables and unobserved on the wire, which is the
mitigation, not a guarantee.

### The dispatcher, mapped byte for byte — 2026-10-10

Established by disassembly and confirmed by four hardware freezes. Read this
before patching anything near it.

    0x0804ACC8  e92d 43f0   push.w {r4,r5,r6,r7,r8,r9,lr}   ; entry
    0x0804ACD0  79ca        ldrb  r2, [r1, #0x7]            ; opcode = buf[7]
    0x0804ACE4  2300        movs  r3, #0x0                  ; r3 = 0, then ~10
    0x0804ACE6  3a15        subs  r2, #0x15                 ;   zero-stores to
    0x0804AD0E  f88d 3027   strb.w r3, [sp, #0x27]          ;   the stack frame
    0x0804AD12  f88d 1010   strb.w r1, [sp, #0x10]
    0x0804AD16  2a48        cmp   r2, #0x48                 ; the ONLY compare
    0x0804AD18  f200 80a2   bhi.w 0x0804AE60                ;   on the opcode
    0x0804AD1C  e8df f012   tbh   [pc, r2, lsl #1]          ; table 0x0804AD20
    ...
    0x0804AE60  b00b        add   sp, #0x2c                 ; SHARED EPILOGUE
    0x0804AE62  e8bd 83f0   pop.w {r4,r5,r6,r7,r8,r9,pc}

#### It is a `switch` with a `default:`, not a comparison chain

The table is **dense**: 73 entries for indices `0x00`-`0x48`, no gaps, **17
distinct targets** -- 16 handlers plus `0x0804AE60`, which **57 entries share**.
Nothing compares the opcode against any particular value. The bias also means
opcodes below `0x15` wrap to huge unsigned values and are caught by the same
`bhi`. So nothing in the firmware "knows" which opcodes are unused; the
duplicate table entries are the only thing that makes them unused.

#### There is no upstream filter on the opcode

Traced in full: the dispatcher is registered at startup from the **reset
handler** (`FUN_0805F848` -> `FUN_0802479C`, which registers four MIDI callbacks
on one object at `0x08024AB8`). Ours is reached through a 3-instruction thunk:

    0x0804B888  mov r1, r0           ; the message buffer
    0x0804B88A  ldr r0, [0x0804B890] ; a context object
    0x0804B88C  b.w 0x0804ACC8       ; tail-call the dispatcher

That thunk examines nothing. Confirmed empirically as well: v6-v10 all claimed
`0x40` and all **froze**, which only happens if those messages reach the
dispatcher. A filter would have produced silence.

#### NEVER detour `0x0804AE60`

It is not a bail *branch*, it is the function's **shared epilogue**, and **eight**
sites branch to it: `0x0804AFFA 0x0804B0AE 0x0804B114 0x0804B204 0x0804B286
0x0804B378 0x0804B4D4 0x0804B516`. A detour there dispatches on `r2`, which by
then holds whatever the finished handler left -- the reply builder does
`ldrb r2,[r4,#0x6]`. v6-v8 did this and froze the instrument every time. A
4-byte detour there also splits the 4-byte `pop.w`.

#### The reach limit is architectural

`TBH` does `halfwords = MemU[R[n] + LSL(R[m],1), 2]` then
`BranchWritePC(PC + 2*halfwords)`. Entries are **unsigned**, so targets are
forward only, max `0x0804AD20 + 2*0xFFFF` = **`0x0806AD1E`**.

**Changing `R[n]` does not extend reach.** `tbh [r3, r2, lsl #1]` (`E8D3 F012`)
is the same 4 bytes and relocates the *table*, but the branch target stays
PC-relative. Verified against the ARMv7-M pseudocode. Do not chase this.

And there is no code cave in range: all 131070 bytes of
`0x0804AD20`-`0x0806AD1E` are live. The longest run of any single byte value in
the whole window is 190 zeros at `0x08061C16`, which is the zero-crossing slack
of an int16 lookup table (its neighbour at `0x08061CD4` is referenced from
`0x0800DC70`/`0x0800DCAC`). Small zero runs in the code region are float literal
pools. Reclaiming the entire 10.8 KB factory vocabulary does not help either --
it sits at `0x08078860`+, `0xDB42` bytes **above** the limit.

#### THE SOLUTION: the table is in reach by construction

The tbh table **is** the base, so it is inside the window by definition. 57 of
its entries hold the identical redundant halfword `0x00A0`. Overwrite two
adjacent ones with a real `b.w` to a far handler, and point the opcode you claim
at those bytes:

    0x0804AD60  (entries 0x20/0x21)  ->  b.w 0x0807B264     30 f0 80 ba
    0x0804AD76  (entry 0x2B = 0x40)  ->  halfword 0x0020

Those 4 bytes then serve two roles: table data for the two sacrificed opcodes,
and executable code when the claimed opcode dispatches there. The dispatcher,
its bounds check and its epilogue stay **byte-for-byte stock**, and our code runs
only when our own opcode arrives -- the property that makes this safe where
v6-v10 were not.

Checked before use: **0** literals point into the table; of **139** TBB/TBH
instructions only `0x0804AD1C` uses base `0x0804AD20`; **0** branches target the
table region.

**Cost: the two sacrificed opcodes stop being harmless bails** -- their entries
now hold instruction bytes, which decode as offsets to arbitrary in-image
addresses. This cannot be made benign; the `b.w` encoding is fixed by
(source, target), and none of the 55 candidate adjacent pairs decode to anything
safe. The justification is that **unimplemented opcodes provably do nothing
today**, so nothing can depend on their effect.

#### Opcode cost, by approach

| approach | spends | role of what is spent |
|---|---|---|
| v1-v5 handler-body hijack of `0x5A` | `0x5A` | partially known: sample slot clear/confirm |
| v11 handler-body hijack of `0x5C` | `0x5C` | **unknown** -- never observed, possibly firmware update |
| **v12 table trampoline** | `0x35`, `0x36` | **provably nothing** -- they bail |

Note on "never observed": our captures cover MCC's preset, sample and wavetable
traffic. **We never captured MCC performing a firmware update.** An implemented
opcode absent from our captures is exactly what an update-protocol opcode would
look like, which is why spending `0x5C` is riskier than it appears.

### The reach limit, and why a handler body was detoured

A `tbh` entry is an **unsigned 16-bit halfword offset** from the table base, so
the furthest target is `0x0806AD1E`:

```
table base       0x0804AD20
furthest target  0x0806AD1E   = base + 2*0xFFFF
our handler      0x0807B264   OUT OF REACH by 0x10546
```

So a free opcode's table entry **cannot** point at anything in the vocabulary
region. `b.w` reaches +/-16 MB; a `tbh` entry about 128 KB. That is why the
original patch detoured an existing handler body instead of redirecting the
table -- the mechanism was forced. **Which opcode it spent was not.**

### Spending no opcode at all

Detour the bail instead, which costs 4 bytes in place plus ~12 in our own
region:

```
bail[0..3]  ->  b.w trampoline

trampoline:
    cmp   r2, #(OP - 0x15)        r2 is still the biased opcode
    beq.w vocab_handler           ~193 KB away; beq.w reaches 1 MB
    add   sp, #0x2c               otherwise the original epilogue,
    pop.w {r4,r5,r6,r7,r8,r9,pc}  byte for byte
```

All 57 free opcodes then behave exactly as before except the one claimed, so
nothing MCC sends changes. **`0x5A` and the whole sample family stay intact.**

**A second opcode is nearly free.** v14 claims `0x41` for peek as well, and the
only cost is one more redundant table entry pointed at the same trampoline plus
6 bytes at the handler entry -- `cmp r2, #(OP - 0x15)` / `beq.w`, because `tbh`
leaves `r2` holding the biased opcode and a `b.w` trampoline does not disturb
it. Put the test after the magic guard and one guard covers both opcodes.

This matters more than it first looks: **`0x59` and `0x5C` are implemented and
have no recorded role**, and `0x5A`'s own role is recorded as unclear. Spending
`0x5A` while two of its neighbours are complete unknowns was a worse bet than
it appeared. Pick a mid-dead-zone opcode such as `0x40` rather than `0x53`,
which is wedged between `0x52` and `0x54` and may be reserved.

> **Retracted 2026-10-13.** "Mid-dead-zone" is reasoning about the shape of one
> table, and both opcodes named here dispatch at `0x08046FA0`: `0x40` to
> `0x0804716A`, `0x53` to `0x08047072`. The dead zone in this table is not a
> dead zone in the firmware. The only sound way to pick is
> `speech_sysex.free_opcodes()`, which reads both tables out of the image.

### A firmware update cannot collide with us — measured 2026-10-13

`captures/firmware-update.pcapng` is a complete successful update to 5.0.0.2084,
and it contains **no SysEx**: 1,469,970 payload bytes, not one `0xF0` outside the
control pipe. The update is USB DFU on interface 0, driven by three Arturia class
requests (`0xF2` reboot, `0xF4` select target, `0xF3` finish) plus standard
`DFU_DNLOAD`/`GETSTATUS`/`GETSTATE`. See `FIRMWARE_UPDATE.md`.

So claiming an opcode risks nothing from the update path. The `0x43`-is-sent-93-
times problem is a *runtime MCC* problem, and the magic-argument guard is aimed
at the right thing.

### v16 claims 0x45 and 0x44 — current, hardware-verified 2026-10-13

    0x0804AD60   4 bytes   the trampoline, INSIDE the tbh table
    0x0804AD7E   2 bytes   opcode 0x44's entry -> the trampoline  (peek)
    0x0804AD80   2 bytes   opcode 0x45's entry -> the trampoline  (upload)
    0x0805B1A8   4 bytes   SpeechEngine::Init argument setup -> init stub
    0x0807B264 162 bytes   vocab handler
    0x0807B306  44 bytes   init stub
    0x0807B332  50 bytes   peek handler

The dispatcher, its bounds check and its shared epilogue are **byte-for-byte
stock** -- our code runs only when our opcode dispatches. 69 table entries are
identical to the factory image and the 4 touched ones were all redundant bails
in stock; `test_dispatch.py` checks all of that, plus (section 8) that
every claimed and sacrificed opcode bails in **both** dispatchers, with the
rejection of `0x40`/`0x41` as its negative control.

Hardware, 2026-10-13: the 15-message vocabulary upload and the 2-message
reproduction that used to freeze the instrument both complete with a liveness
probe after every message, every written byte reads back correctly through
peek, and custom vocabulary speaks -- including in paraphonic mode, surviving
mode toggles and preset switches.

### v6 claimed 0x40 this way — SUPERSEDED, and it froze

    0x0804AE60   4 bytes   bail -> trampoline
    0x0804AF28   4 bytes   opcode 0x5A RESTORED to factory
    0x0807B326  14 bytes   the trampoline

Verified against the factory image: `0x5A`'s and `0x5D`'s handler bodies are
byte-identical to stock, and **all 16 implemented handlers are untouched**. Only
the bail differs. So the firmware's footprint on stock behaviour is one factory
word (`bank_4`'s 22nd) and nothing else -- no opcode, no capability, no handler.

**Payload files are version-specific, and one direction is destructive.**

| payload built for | sent to | result |
|---|---|---|
| v1-v5 (`0x5A`) | **v6** | hits the real **sample-directory-clear** handler |
| v6 (`0x40`) | v1-v5 | falls to the bail, silently does nothing |

So payload files are stamped (`MFU1` + the opcode) and `unpack_upload` refuses a
mismatch, and refuses unstamped pre-v6 files by name. Without that, an old
`.bin` left in a directory would erase a user's samples -- the opposite of the
"load it and nothing existing breaks" property this firmware is for.

**Caveat.** The 57 free opcodes are bails *in 5.0.0.2084*. That is a fact about
this image, not a guarantee about the product family: if Arturia ever implements
`0x40`, a future firmware would collide.

### Not opcode 0x5C

`0x5C` -> `0x08044F68` -> `0x080469DC` reads *and writes* the directory status
byte for **both** of its two 7-bit arguments. It is a slot **swap** (MCC's
drag-to-reorder), not a parameter write and not an allocator.

### 14. Opcode reference

Every `cmd`/`sub` pair observed, for quick lookup — narrative and evidence
for each is in the sections linked. Frame is always
`F0 00 20 6B 07 <dd> <seq> <cmd> <sub> ...payload... F7` (`dd`=`01` normally,
`7F` for one reply class).

**Select/request family, `cmd 0x03`** (13-byte requests, `<hi> <lo> <tail>`):

| sub | role | status | section |
|---|---|---|---|
| `0x19` | preset select — tail=`00` name / tail=`01` data (read) | confirmed, live | 4, 6, 11 |
| `0x52` | preset data select (write) | confirmed, live | 12 |
| `0x57` | wavetable name select (read) | confirmed, live | 10 |
| `0x55` | wavetable waveform-data select, `lo`=frame 0–3 (read) | confirmed, live | 10 |
| `0x56` | wavetable name select (write) | confirmed, live | 10 |
| `0x54` | wavetable waveform-data select (write) | confirmed, live | 10 |
| `0x5B` | sample-slot name/metadata read-back (verify) | confirmed | 12 |
| `0x5D` | sample slot prepare (write) | addressing confirmed; role vs `0x5A` unclear | 12 |
| `0x5A` | sample slot clear/confirm (write) | addressing confirmed; role unclear | 12 |
| `0x58` | sample slot audio-block select (write), one per up-to-147-chunk block, chained for longer samples | confirmed working on an already-populated slot; didn't stick on empty slot 53 | 12 |

**Data/record types:**

| cmd | sub | len | role | status | section |
|---|---|---|---|---|---|
| `0x20` | `0x16` | 42 | data chunk, more follow | confirmed | 10 |
| `0x20` | `0x17` | 42 | data chunk, final in block | confirmed | 10 |
| `0x23` | `0x52` | 45 | preset name record — identical read reply and write request | confirmed | 4, 12 |

**Flow control / acks:**

| cmd | sub | len | role | status | section |
|---|---|---|---|---|---|
| `0x01` | `0x18` | 11 | "give me next chunk" (read pull, positional, no address) | confirmed | 10 |
| `0x00` | `0x15` | 10 | "ready" — device sends as a read-select ack; MCC sends as a write-side ready signal | confirmed | 10, 12 |
| `0x00` | `0x18` | 10 | generic per-message ack (mostly dev→MCC) | confirmed | 12 |
| `0x01` | `0x16` | 11 | non-generic ack, once per sample-slot name write | seen, unclear | 12 |

**Config reads:**

| cmd | sub | dir/frame | len | role | status | section |
|---|---|---|---|---|---|---|
| `0x01` | `0x43` | MCC→dev, `dd=01` | 11 | config read request, `<id>` 0x20–0x4D (31 total) | confirmed; per-id meaning unknown | 6 |
| `0x02` | `0x42` | dev→MCC, `dd=7F` | 12 | config read reply | confirmed | 6 |

**Handshake / unexplained:**

| cmd | sub | dir/frame | len | role | status | section |
|---|---|---|---|---|---|---|
| `0x01` | `0x47` | MCC→dev, `dd=01` | 11 | status/handshake query, payload `0x0A` | seen, meaning unknown | 12 |
| `0x09` | `0x48` | dev→MCC, `dd=7F` | 19 | reply to the above | seen, undecoded | 12 |
| `0x08` | `0x17` | MCC→dev, `dd=01` | 18 | fixed marker during wavetable write, always byte-identical | seen, purpose unknown | 10 |

**Non-Arturia** (standard MIDI, not part of the `07`-frame family, section 1–2):

- `F0 7E 7F 06 01 F7` — Identity Request (MCC→dev)
- `F0 7E 7F 06 02 00 20 6B 06 00 06 01 05 00 00 24 F7` — Identity Reply (dev→MCC)


## Safety

### 8. Safety note

Command space beyond the reads above is **unexplored, and probing it is not
risk-free** — an unknown opcode could write or erase presets rather than read
them. The reads documented here are known-safe because they were observed being
issued by MCC during normal startup. Do not fuzz opcodes on a synth whose
presets matter without a way to restore them.

> **Updated 2026-09-04.** Two things in this section have changed:
>
> - "the write format is unknown" is no longer true. The preset write sequence
>   is documented and hardware-verified (see `mfreak preset write` and
>   FORMATS.md), so a capture of a slot *is* now a restorable backup:
>   `mfreak preset read SLOT --out backup.mfpz`, then `mfreak preset write
>   SLOT --file backup.mfpz --yes`.
> - The device's `Mem protect` setting does **not** protect you from this
>   tooling. It gates the front-panel save path only; SysEx writes bypass it,
>   including to the factory range (slots 0-127). Verified live. Back up before
>   writing below slot 128 — the factory presets are not in this repo.

### Safety note, still current

`PROTOCOL.md` warns against fuzzing opcodes on a synth with presets worth
keeping: an unknown opcode could write or erase. That warning applies to the
**implemented** 16 -- and, as of 2026-10-13, to **ten more**: the opcodes that
bail in dispatcher 1 and dispatch at `0x08046FA0`. This section used to say
"the 57 free ones are bails in this firmware version and do nothing". That is
false for `0x1C 0x1D 0x40 0x41 0x42 0x43 0x47 0x49 0x4C 0x53`, and sending one
of them is how four freezes happened. 47 are bails in both. Either way it is a
fact about 5.0.0.2084, not about the family.


## Method, and what is still open

### 7. A note on method

Two mistakes worth recording, since both produced confident wrong answers:

**Attributing MCC's crash to hot-plug.** MCC segfaulted shortly after the synth
was plugged in, and that was reported as the cause. It was not evidence, just
timing. The crash reports show the same faulting address (`+4717122`) in a crash
predating any of this work, and again in runs with no hot-plug involved. MCC
crashes there routinely. Do not build plans around that assumption.

**Trusting a passing test.** The USB-MIDI decoder passed 10/10 while containing
a stray token in a code path the tests never exercised; it would have failed on
the first real capture. Green tests only cover what they touch.

### 5. Open questions

- **byte 19** takes 12 values (0–11) with a plausible-looking distribution.
  A category or oscillator-type field is the obvious guess, but it is only a
  guess — nothing has been checked against the synth yet.
- **byte 20** takes 6 values (0x10, 0x11, 0x30–0x33). A bank field was the
  obvious reading, and it is **wrong**: the slot ranges for each value overlap
  heavily (0x10 covers slots 0–206, 0x11 covers 112–175). Unexplained.
- **bytes 12–15** have small value sets; **byte 17** takes 128 values and may
  be a second counter.
- **cmd 0x20 (42-byte) records** are high-entropy — the actual patch parameters.
  30,576 of them for 512 presets is ~60 per preset, consistent with a chunked
  dump. Not yet decoded.
- The `07 7F` messages are a separate class, only 31 seen.

### 13. Still open

- Which setting each config-read `<id>` (0x20–0x4D) refers to.
- ~~What the `03 5B` blocks in `mcc-sync` actually are~~ — resolved (section
  12): a sample-slot name/metadata read-back, same mechanism confirmed
  directly in `mcc-samples.pcapng` verifying its own writes.
- The 32-byte chunk payload layout itself, for both presets and wavetables —
  still the main remaining prize for a real decoder (as opposed to a fetcher).
  Section 11's parameter-name sighting and section 10's wavetable-name decode
  (name starts at byte 23, two stray `0x00`s at relative offsets 2 and 10 —
  cause unknown) are the only byte-level layout leads so far.
- Whether `03 5D` is "prepare" and `03 5A` is "clear/confirm" or vice versa
  for sample writes, and what they actually do to the device — addressing is
  solved (section 12) but semantics aren't.
- Why writing to a currently-empty slot doesn't stick, while writing to an
  already-populated one does — confirmed real and reproducible across three
  attempts (slot 53 twice, slot 1 once), and no longer explained by a
  fixed-52-slots theory (ruled out: slot 1's index held real content before
  being deleted, and still failed once empty — section 12). A full 128-slot
  touch sweep matching MCC's own single-sample-send traffic didn't unlock it
  either. Whatever re-arms an empty slot for writing hasn't been found —
  possibly something else in MCC's own import flow this project hasn't
  captured, possibly not achievable outside MCC's own UI at all.
- Whether reading actual sample *audio* back is possible at all — investigated
  and the answer looks like no (section 12): `0x5B` only reads back a slot's
  name/metadata, and nothing in any capture reads raw sample bytes. No longer
  blocks confidence in writes, though — the "Vinyl"-to-slot-0 write (section
  12) was confirmed correct by ear, not just by the `0x5B` name check.
- ~~What raw audio encoding a genuinely new sample recording needs to be
  converted into~~ — resolved (section 12, full detail in
  [SAMPLE_ENCODING.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/SAMPLE_ENCODING.md)): 16-bit signed LE PCM, 32kHz, mono, packed 7-to-8
  per 4116-byte (2048-sample + 20-byte-pad) block. Verified live.
- ~~Whether the same 7-to-8 packing applies to *preset* `cmd 0x20` chunk
  data~~ — confirmed yes (section 11): 4672 wire bytes → 4088 real bytes
  (4672/8=584 groups exactly — note presets are 146-chunk blocks, not the
  147-chunk blocks samples/wavetables use, so the real-byte count differs
  from the 4116-byte case). Decoding reveals a real
  `<name>c<value>` field structure — 41 fields have a confirmed fixed
  layout (widths verified across 399 real presets), but the schema turns
  out to be genuinely conditional/polymorphic past that point (one field,
  `EPanel`, appears in only 38/399 presets, and its trigger isn't found —
  checked every earlier field as a potential single-field predictor,
  none work). **This is now the main open item for the whole project**:
  cracking it is what would let new presets be generated from scratch
  instead of only read/cloned/renamed. Full detail, including a dead-end
  worth not repeating (a preset's own local `.mfpz` export, unlike
  `.mfsz`/`.mfwz`, turns out to just be the raw wire bytes again, not a
  labeled model), in [PRESET_FORMAT.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/PRESET_FORMAT.md).
- Why the 20-byte trailing pad exists in every 4116-byte decoded block —
  confirmed present and always zero, purpose (alignment? reserved?) unknown.
- ~~A `WAV → wavetable frame` encoder doesn't exist yet~~ — resolved:
  `mfreak wavetable encode`, verified live (byte-exact read-back, since
  wavetables — unlike samples — have a real read path). Includes a
  cycle-aware mapping mode (`--cycle-samples`) matching MCC's documented
  behavior for real multi-cycle wavetable sources, though that mode is only
  sanity-checked against synthetic data, not a real file yet — see
  [SAMPLE_ENCODING.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/SAMPLE_ENCODING.md).
- Why MCC reads each preset name twice before writing it (section 12) —
  functionally harmless, but the reason is a guess.
- byte 19 of the name record (12 values) — category or oscillator type, unverified.
- byte 20 of the name record — not a bank field; slot ranges overlap. Unexplained.

