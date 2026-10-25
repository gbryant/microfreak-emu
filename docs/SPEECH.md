# The Speech engine, and arbitrary vocabulary

**The MicroFreak's Speech engine is Mutable Instruments' Plaits LPC speech
synth, lifted whole — data included.** Established 2026-10-04 against
`pichenettes/eurorack` (MIT, Emilie Gillet). That turns this from reverse
engineering into reading published source, and it is why arbitrary vocabulary is
a tractable feature rather than a research project.

This file is in two halves. **The engine** — the evidence that it is Plaits,
where everything lives in the image, the macro mapping, the bitstream format
and the tooling — is reference material and comes first. **Replacing the
vocabulary** is the feature built on top of it: what works today and how to
use it, why the patch is shaped the way it is, what the firmware does with a
word bank, the limits, and last, how it got there — the superseded steps and
the retractions, kept because the reasoning is worth more than the outcome.

If you only want to put your own words on the instrument, read "What works
today" and stop.

## The evidence, which is byte-exact

| what | Plaits | firmware | result |
|---|---|---|---|
| `Frame` struct | `// 14 bytes` | ×14 stride decoded from `rsb.w r3,r3,r3,lsl #3; lsls r3,#1` at `0x0805E852` | matches |
| `phonemes_[]` entries 1-3 | `{192,80,-18368,11584,52,29,23,14,-17,79,37,4}` … | `0x08078764` | **byte-identical** |
| `bank_0` first 16 bytes | `30 4a 57 d8 23 2c c9 a9 …` | found at `0x08078860` | **byte-identical** |
| `bank_1` first 16 bytes | `69 fc 59 dd 51 d5 d7 a1 …` | found at `0x08078D34` | **byte-identical**, and exactly the 4-byte-aligned end of bank_0 |
| quantisation LUTs | `energy_lut_[16]`, `period_lut_[64]`, `k0_lut_[32]`, `k1_lut_[32]`, `k2..k6[16]`, `k7..k9[8]` | the tables in `microfreak/lpc.py`, transcribed from the image | **byte-identical**, all 10 ladders and both scalars, checked 2026-10-14 |
| `lut_lpc_excitation_pulse` | 640 `int8`, `plaits/resources.cc` | `0x0807C84C` | **byte-identical**, all 640 entries, checked 2026-10-14 |

A fifth, independent check closed it. `FIRMWARE.md` recorded — live on
hardware in September, for an unrelated purpose — that the engine started at
frame `0xAE` (174) and parked at `0xBC` (188). Decoding bank_1 gives word 8
spanning frames **174..188**, the only match among all 92 words, and the owner
heard that preset say *"eight"*. bank_1 holds 11 words and is the digits.

## Provenance, and what that means for redistribution

Everything the tooling reproduces in source is Plaits' data, and Plaits is MIT
(Copyright 2016 Emilie Gillet). Verified 2026-10-14 by fetching
`plaits/resources.cc` and `plaits/dsp/speech/lpc_speech_synth_controller.cc`
and comparing value for value:

* the quantisation ladders in `microfreak/lpc.py` — `ENERGY`, `PERIOD`, `K0..K9` —
  match Plaits' `energy_lut_`, `period_lut_`, `k0_lut_`…`k9_lut_` exactly, with
  the same sizes (16/64/32/32/16×5/8×3)
* the 640-entry excitation pulse at `0x0807C84C` matches
  `lut_lpc_excitation_pulse` exactly

So the numbers in this repository are redistributable, provided the MIT notice
travels with them. It does, in `NOTICE`. Arturia compiled Gillet's code; that
does not create a new copyright in Gillet's tables.

**What is read from the image and never committed:** the five factory word
banks. Those are MIT-wrapped in Plaits, but upstream of that they are, in
Plaits' own words, *"LPC10-encoded speech data extracted from Texas Instruments
ROMs"*. The MIT grant around the code is not an obvious grant for the ROM
speech, and nothing here needs them redistributed — so they stay in the image
the user supplies. `data/` is gitignored for this reason among others.

One consequence worth having: because the excitation pulse now comes from the
MIT source rather than from `paths.MAIN_RAW`, `mfreak speech preview` runs on a
machine with no Arturia firmware present at all. Passing `--image` still reads
it from an image and warns if a revision ever differs.

## Map (fw 5.0.0.2084, load base `0x0800C000`)

    0x08078764  phonemes_[15]          15 x 14 bytes, 5 vowels + 10 consonants
    0x08078838  word_banks_[5]         5 x {const uint8_t* data; size_t size}
    0x08078860  bank_0  1233 bytes      7 words,  229 frames
    0x08078D34  bank_1   900 bytes     11 words,  229 frames   the digits
    0x080790B8  bank_2  1552 bytes     26 words,  524 frames
    0x080796C8  bank_3  2524 bytes     26 words,  604 frames
    0x0807A0A4  bank_4  4802 bytes     22 words,  926 frames
    0x0807B367  — NOT slack. Seven referenced objects. See "The slack does not exist" —
    0x0807C84C  excitation pulse LUT   640 int8, a glottal pulse

**92 words and 15 phonemes, total.** Code, for reference:

    0x0805B18C  SpeechEngine::Init                 word bank object at engine+0x168
    0x0805B200  literal 0x08078838                 the ONLY reference to the table
    0x0805DD24  lpc_frame_interpolate(rec,prev,cur,blend)
    0x0805DFD0  LPCSpeechSynthWordBank::Init
    0x0805E656  ldr.w r1,[r5,#0x94]                frames_ pointer, word path
    0x0805E7E2  ldr r1,[0x0805E8EC] = phonemes_    phoneme path
    0x0805E820  utterance_start                    [r4+0x84] playback_frame_ (an INDEX)

**Arturia restructured the class.** Plaits puts `frames_` at +0x18; the firmware
reads `[r5+0x94]`. Do not reuse Plaits' member offsets — they do not hold.

## Macro mapping

**The panel names are Wave, Timbre and Shape** -- confirmed on hardware
2026-10-06 and against Arturia's manual. Earlier revisions of this file called
the bank selector the "Type knob" and the word selector the "Word knob"; both
were wrong and the second is the dangerous one, because "do not change bank"
plus "turn the Word knob" reads as a contradiction. The quoted names below are
the *engine-specific functions* the manual tabulates, not panel legends.

    VCO/Param1  osc_wave    panel "Wave"   -> bank
                            (group = Wave/100*6; <2 is naive/SAM/phoneme,
                             else word_bank = quantizer((group-2)*0.275)-1)
    VCO/Param2  osc_timbre  panel "Timbre" -> formant_shift
    VCO/Param3  osc_shape   panel "Shape"  -> address
                            word = int(address * num_words), clamped

`GetWordBoundaries`: `start = bounds[word]`, `end = bounds[word+1] - 1`.

**The sub-bank Wave region is a real mode, not a gap.** `group < 2` is the
naive/SAM/phoneme path, rendering from `phonemes_[15]` at `0x08078764` -- 5
vowels and 10 consonants. Arturia's manual describes the same thing from the
outside: Wave "scans through formants, then through libraries of colors,
numbers, letters and words". An earlier note in this session called the low
region "unmapped"; it is not.

**Bank identities, manual-confirmed 2026-10-06:**

| bank | words | library |
|---|---|---|
| `bank_0` | 7 | **colors** |
| `bank_1` | 11 | numbers (the digits) |
| `bank_2` | 26 | letters -- 26 is the alphabet |
| `bank_3` | 26 | |
| `bank_4` | 22 | words |

**The Wave→bank boundaries are still NOT pinned, and now we know why.**
`HysteresisQuantizer2`'s header is absent from that repo, so whether it rounds
or floors was unknown; the derived boundaries (bank 0..4 at roughly Wave 39-51,
51-64, 64-76, 76-88, 88-100) could be half a step out. **Measured on hardware
2026-10-06: the boundary genuinely lands in a different place depending on which
direction you approach it from.** That is hysteresis doing its job, so a single
boundary number does not exist to be found. Navigate by ear, or by "the lowest
Wave position that still speaks words" -- which is `bank_0`.

## The bitstream

Per frame, fields MSB-first from a stream that takes each byte's bits
**LSB-first**:

    4 bits  energy index    0 -> silent frame; 0xF -> end of word, then flush
    1 bit   repeat          reuse the previous frame's coefficients
    6 bits  period index
    repeat == 0:   5,5 -> k0,k1 (int16 LUTs);  4,4 -> k2,k3
    and period != 0 (voiced):  4,4,4 -> k4,k5,k6;  3,3,3 -> k7,k8,k9

A frame carries forward anything the stream does not set. Words end byte-aligned,
so a bank is the concatenation of its words. Interpretation:
`energy/256` is amplitude, `period == 0` means **unvoiced** (noise excitation),
`k0,k1` scale by 1/32768 and `k2..k9` by 1/128.

**F0 comes from the played note, not the frame.** The controller passes
`pitch_shift`; `period` only supplies a prosody contour. `period` is in 8 kHz
samples — `base_f0 = 100.0f / 8000.0f` pins the rate — and the controller runs
frames at `kLPCSpeechSynthFPS = 40`, so analysis is 10th-order LPC at 8 kHz in
200-sample frames.

**Reflection-coefficient sign is `+1`, measured not assumed.** `mfreak speech analyze
--selftest` resynthesises factory words, re-analyses them and correlates against
the known coefficients under both signs: `+1` gives +0.539/+0.687, `-1` gives
−0.539/−0.606, margin 2.37. The test fails loudly if the margin collapses.

## Tooling

In microfreak-tools, with the speech plugin in `plugins/speech/mfreak_speech/`:

    microfreak/lpc.py        bitstream encode/decode, frame pack/unpack
    mfreak_speech/render.py  a Python port of the lattice, so vocabulary can be
                             heard before anything is flashed; holds the
                             excitation LUT as Plaits' MIT data, so this path
                             needs no firmware image
    mfreak speech analyze    audio -> frames (ffmpeg, 8 kHz, Levinson-Durbin), --selftest
    mfreak speech banks      replace bank contents, fix the descriptor table,
                             repackage with the correct checksum, --verify

**The encoder is proven against the firmware's own data**: all five factory banks
decode and re-encode with frames and word boundaries identical, four of five at
byte-identical length (bank_4 comes out 9 bytes smaller because the encoder finds
more `repeat` opportunities than Arturia's did — equally valid, decodes the same).

## Replacing the vocabulary

Two ways. `mfreak speech say` and `speech banks` upload words to the speech
build over MIDI; they last until power-off. `mfreak speech embed` writes them
into a firmware file in place of the built-in banks, to flash.


### In flash: `speech embed`, verified in the emulator 2026-10-23 and on hardware 2026-10-24

`embed` zeroes `0x08078860` up to the end of the banks, writes the new
bitstreams from `0x08078860` 4-byte aligned, and rewrites all five
`word_banks_` entries. A position with no words gets the entry of the nearest
bank below it with words, or of the first bank with words if none is below.
It shares that bank's bytes, so it costs no space. On a
patched build the banks end at `CODE_BASE` (`0x0807B264`), below the patch
code; on stock they run to `0x0807B367`. Factory words fill that space
exactly: 92 words, 62.7 s, 11,011 bytes, about 176 bytes a second.

Booted in the emulator with presets at Wave 45, 57 and 70 (banks 1, 2, 3), the
firmware's `WordBank` after the first note held exactly what the image holds:

| image | bank 1 | bank 2 | bank 3 |
|---|---|---|---|
| stock (control) | 7 words, 229 frames | 11 words, 229 frames | 26 words, 524 frames |
| stock + embed, banks 1 and 3 | 2 words, 60 frames | 0 words, 0 frames | 4 words, 100 frames |
| speech18 + embed, bank 1 | 2 words, 57 frames | 0 words, 0 frames | 0 words, 0 frames |

Frame bytes and word boundaries matched as well. Those runs used `{0, 0}` for
an empty bank, which loads as 0 words. With repeats, the speech18 file with
banks 1 and 3 filled loads bank 1's 3 words at Wave 57.

**An empty entry is not silent on hardware.** Flashed to the instrument with
`{0, 0}` at bank 2, the preset at Wave 57 played a steady tone at the key's
pitch for as long as the key was held. Its spectrum is close to Wave 25, the
vowel mode below the banks (correlation 0.71, against 0.27 with a word). The
emulator renders the same preset as silence. That is why empty positions now
repeat a bank.

On hardware 2026-10-24, speech18 with apple, banana, cherry in bank 1 and
microfreak, robot in bank 3, recorded from the audio output and matched by
DTW against the emulator rendering each word:

| preset | Wave | Shape | expected | closest |
|---|---|---|---|---|
| 1, 2, 3 | 45 (bank 1) | 15, 50, 85 | apple, banana, cherry | apple, banana, cherry |
| 4 | 57 (bank 2, repeats 1) | 50 | banana | banana |
| 5, 6 | 70 (bank 3) | 25, 75 | microfreak, robot | microfreak, robot |
| 8 | 82 (bank 4, repeats 3) | 25 | microfreak | microfreak |
| 9 | 94 (bank 5, repeats 3) | 75 | robot | robot |

The same presets on stock matched none of the five words.

### What works today

    mfreak speech analyze --selftest

The descriptor table is the single point of truth, each bank pointer is
referenced exactly once, and the container's only integrity check is a one-byte
XOR stored twice — so replacing vocabulary needs **no code changes at all**.

**Budget. CORRECTED 2026-10-04: 11,015 bytes, not 16,364.** The five banks fill
`0x08078860`..`0x0807B367` **exactly** (11,011 bytes of data plus 4 bytes of
alignment) and there is no slack after them — see below. Six words of `say`
output came to 894 bytes, ~149 bytes a word, so the realistic ceiling is
**about 70 words**, not a hundred. Still more than half of what Arturia ships,
and the per-bank limits may bind first:

- `kLPCSpeechSynthMaxFrames = 1024` — **at most 1024 frames per bank** (25.6 s)
- `kLPCSpeechSynthMaxWords = 32` — at most 32 words per bank

Both are heap allocations on the device, so `mfreak speech banks` enforces them as
errors. Flashing is a separate deliberate act; the bootloader at
`0x08000000`-`0x0800C000` is not in this image and is never modified. **That the
bootloader can always be re-entered after a bad main image is an assumption about
the hardware, not something verified here** — it is the assumption that decides
whether a mistake is recoverable, and it is worth confirming before a first write.

### What it does now, and how to use it

#### Current state — v16, hardware-verified 2026-10-13

    mfreak fw patch speech stock.mff --out custom_fw_v16.mff          # once; a flashable container
    mfreak fw flash custom_fw_v16.mff
    mfreak speech upload table  --bank w1.wav w2.wav --out vocab.bin
    mfreak speech upload send   vocab.bin --probe                  # as often as you like
    mfreak speech upload verify vocab.bin                          # did every byte land?

**Opcode `0x45` for the upload, `0x44` for peek, claimed through the `tbh`
table itself. `0x5A` and `0x5C` stay factory.**

    0x0804AD60   4 bytes   trampoline, INSIDE the tbh table (spends 0x35/0x36)
    0x0804AD7E   2 bytes   opcode 0x44 -> trampoline   (peek)
    0x0804AD80   2 bytes   opcode 0x45 -> trampoline   (upload)
    0x0805B1A8   4 bytes   b.w init_stub   (was `movs r2,#5 ; ldr r1,[pc,#0x54]`)
    0x0807B264 162 bytes   vocab_handler
    0x0807B306  44 bytes   init_stub
    0x0807B332  50 bytes   peek_handler
                           256 of 259 reserved bytes used, 3 spare

    frames_+0x0000  decoded frames        the engine renders from here
    frames_+0x2000  bitstreams            placed per upload, not fixed
    frames_+0x37D4  magic 'MFVC'          present once an upload has happened
    frames_+0x37D8  5 x {ptr,size}        descriptor table, in RAM

Both the magic and the table live inside `frames_`, which is zeroed at boot, so
**a cold start finds no magic and takes the flash branch** — factory vocabulary
behaves normally until something uploads, and a bad upload costs a power cycle
rather than a reflash. The permanent cost of the patch is `bank_4`'s 22nd word,
21 of 22 remaining, spent to reserve the top of the vocabulary region.

**Picking the opcode is the one thing not to do by eye.** There are two SysEx
opcode dispatchers (`0x0804AD20` and `0x08046FA0`), ten opcodes bail in the
first and dispatch in the second, and claiming one of those is the cause of
every upload freeze from v6 to v15. `speech_sysex.assert_opcode_free()` reads
both tables out of the image at build time. See `PROTOCOL.md`.

Hardware, 2026-10-13: the 15-message upload and the 2-message reproduction that
used to freeze the instrument both complete, `verify` reads back 354 of 354
data bytes, and the instrument speaks custom vocabulary — **including in
paraphonic mode**, surviving paraphonic toggles and preset switches. Word and
Wave selection both give the same utterance, which is the payload's doing, not
a limitation: that upload repeated the one bank into all five Wave positions,
and the bank held one word. Uploads now leave unused positions empty; 32 words per bank is the hard ceiling (`word_boundaries_[32]`
at `bank+0x14` ends exactly at the `frames_` pointer).

#### HARDWARE VERIFIED: the feature works on the instrument — 2026-10-06

**Flashed and confirmed.** The owner flashed `custom_fw_sysex.mff`, the device
booted normally, preset 180 spoke a factory word, and a SysEx upload then
replaced the vocabulary of a live bank. Controlled: the colors bank
(`bank_0`, 7 words) was swept by ear **first**, giving 7 distinct colors, then
uploaded onto without touching the Wave knob, then swept again -- **exactly 4
slots, our 4 words, in order.** 7 → 4 on an untouched bank selector.

| | |
|---|---|
| container | `.mff` = plain stored ZIP; `microfreak/firmware/mff.py` round-trips all 10 shipped versions byte-identically |
| upload | 2 runs, 53 and 45 messages, 250 ms spacing, **zero losses** |
| `num_words` | written and honoured on hardware -- the `REGION_BANK` path works, not just frames |
| audio | all 4 words audible, distinct, correctly ordered |

**The emulator result transferred intact.** No hardware-only transport problem
appeared at 250 ms.

**`send` had no pacing at all and would have failed.** The loop blasted every
message back to back with no `time.sleep`; only the `sentence` path *printed*
"250 ms is proven". Fixed 2026-10-06 with a `--spacing` default of 250 ms and a
refusal below the per-message wire time. This was the one code path that had
never run against hardware, and it was broken in exactly the way 71af894
predicted.

#### v5: a RAM bank table, so uploads survive everything — 2026-10-07

**`data/speech/sysex/custom_fw_v5.mff`.** Handler 150 bytes + init stub 44 = 194
of 259 reserved. Three revisions, each load-bearing:

| | |
|---|---|
| v3 | window floor to `+0x00`, so the host can write `word_banks_` |
| v4 | init stub, so `Init` cannot undo it |
| v5 | writes both, per-bank banks, metadata at the top, v2's inert code removed |

**Measured with CPU hooks, not inferred:** `Init` and `Load` fire as **four
pairs, one per voice**, on an **ENGINE CHANGE**. A program change to the *same*
engine re-reads the preset page (`MFRENDER-LOADED` fires) and does **not**
re-init. `Init` rewrites `num_banks` AND `word_banks_`, which is why v3's
`num_banks = 0` protected voice 0 (bank switching did work on hardware) but not
a voice coming into service.

**BOTH halves are required, and they cover different moments:**

    host writes word_banks_ (+0x00)    table live IMMEDIATELY, any trigger
    init stub re-installs from magic   survives every later Init

Without the first, the table is inert until the next `Init` -- `Init` is the only
thing that writes `word_banks_`, and it already ran at the program change before
the magic existed. **A Wave change calls `Load`, not `Init`**, so `Load` keeps
reading the flash table. Measured: a Wave-only probe showed the upload having
*zero* effect while the engine-switch probe passed. Same patch, opposite result,
and v4 would have failed on the owner's instrument for exactly this reason.

**Layout, and what is NOT baked into the image.** `STREAM_OFF` never reaches the
firmware: a table entry carries an absolute pointer and the firmware decodes
from wherever it points. So the split between decode space and stream space is a
**per-upload** decision, placed automatically from the content:

    max_bank_frames * 14  +  sum(all bitstreams)  <=  MAGIC_OFF

Only `MAGIC_OFF`/`TABLE_OFF` are baked (into the stub's `movw`/`movt`). They sit
at the TOP of `frames_` (`0x37D4`), which recovered 468 dead bytes. With three
short banks the stream budget came out at **12,696 bytes**, against 6,100 under
the old fixed `0x2000` -- the fixed layout was reserving decode space nothing
used.

**Two hard limits.**

- **32 words per bank is an overflow boundary, not a soft cap.**
  `word_boundaries_` is at `bank+0x14` and `0x14 + 32*4 = 0x94`, which is exactly
  the `frames_` pointer. A 33rd word makes the **firmware's own** decode
  overwrite it. Refused host-side.
- **One factory word is spent, unavoidably.** `bank_4` occupies
  `0x0807A0A4..0x0807B366` and live objects begin at `0x0807B367` -- **one byte**
  of slack. No run of `0xFF`/`0x00` as long as 194 bytes exists anywhere in the
  image. So the handler's flash must come from vocabulary: `bank_4` loses its
  22nd word, 1 of 92. The owner accepted this 2026-10-07. Everything else --
  the other four banks, the phoneme mode, all 22 engines, all presets -- is
  untouched, which is the point: load it and nothing existing breaks.

**An empty bank loads 0 frames.** `Load` zeroes the frame count before the
`size == 0` check (rms 57 vs ~800 for a real bank in the emulator). On
hardware the same entry plays a steady vowel-like tone (see "In flash" above),
so uploads point an unused position at the bank it repeats instead.

**End-to-end verified** (`--bank` x3: one phrase / five words / two words):

| bank | Wave | uploaded | control |
|---|---|---|---|
| 0 | 43 | 1896.8 | 825.6 |
| 1 | 57 | 2177.8 | 846.2 |
| 2 | 70 | 901.2 | 1075.9 |

All three differ from the control and from each other, so the Wave knob really
selects and the table indexing is right -- which the earlier five-identical-
entries layout could not have shown. Durability: after an engine switch the
vocabulary is still **audible (455/867) and different from the control**.

**Method, and it cost most of the day.** Five test designs had success conditions
a broken system would satisfy:

- `X1 vs X2` "reproduction" -- differing note history, not the variable. Retracted.
- `T1 vs T2` -- T2 has no reload, so the table never engages; not an "ours" reference.
- `T1 vs T4` -- different voice warm-up either side of the switch.
- a durability run reported **PASS on an output of 0.0**, because the criterion
  was "differs from control" and silence differs.
- mean-absolute-difference read as an interpolation between anchors; it exceeded
  100%.

What works: **hold the whole sequence fixed and vary only the payload content**,
and require **audible AND different**. Also: derive MIDI timings from the actual
payload length -- a hard-coded switch time put the engine change *inside* the
upload twice.

#### THE ENCODER WORKS — validated against the firmware, 2026-10-06

Standing claim in this file was that the bitstream encoder "produces streams the
firmware mostly will not play" -- a 28-word bank decoded in Python but six of
seven Word positions were silent on the device. **That is no longer supported.**
Tested through the firmware's own `Load()`, with our decoder nowhere in the
comparison:

| test | result |
|---|---|
| factory `bank_4` re-encoded, rendered at 5 Shape positions | **byte-identical audio** to the factory image |
| 12 **novel** words (one..twelve), 12 Shape positions | **12/12 audible, 66/66 pairs distinct** |

The factory re-encode is the strong one: our bytes are **not** Arturia's (4793
vs 4802, different `repeat` choices), and the firmware still lands on identical
audio. That is agreement, not the self-consistency this file warned about.

**Why the old failure is still unexplained.** Two defects found today would each
produce silent slots and were both present then, so that test was probably
contaminated rather than measuring the encoder:

- `mfreak preset encode` wrote `osc_type` with `set_field_raw` (below) -- the preset
  was not on the engine it claimed.
- **The first render after boot is silent.** The bank load is lazy and inside
  `Render`, so whichever preset renders first has no bank yet. Demonstrated by
  moving it: rendering w00 first makes slot 0 silent, rendering w00 last makes
  slot 11 silent instead. Third sighting of this path today, with the cold-boot
  first keypress and the first paraphonic chord.

**Do not attribute the old failure to padding.** The encoder padded word tails
with 0s where Arturia pads with 1s, which is why a re-encode was never
byte-identical, and it is now fixed (`microfreak/lpc.py` `flush_to_byte`) -- bank_0
to bank_3 reproduce exactly. But **padding is cosmetic**: 1-pad and 0-pad
rendered identically at 12 of 12 custom slots. It was claimed here as the cause
before it was tested; that claim is withdrawn.

`bank_4` is byte-identical only under a *never-repeat* rule, while banks 1-3 are
byte-identical only under *repeat-when-coefficients-match*. The five banks were
simply not all authored with the same encoder settings, so our repeat choice was
never wrong either.

**What this unblocks.** The RAM-table redirect: point `word_banks_` at a RAM
table holding our bitstream. `Reset` preserves the table pointer (see "The patch
design is sound"), so every reload -- voice wake, bank switch, engine switch --
re-decodes *our* vocabulary instead of destroying it. The paraphonic overwrite
stops existing rather than being suppressed, and the Wave knob keeps working.
**Blocked on two measurements, both of which defeated the old `init_stub`:**
where genuinely-free RAM is, and how to get code to run at `SpeechEngine::Init`.

#### 7-to-8 packing: 31 bytes a message instead of 18 — 2026-10-05

The nibble encoding spent two wire bytes per data byte. The firmware's own
format packs **7 data bytes into 8 wire bytes** — one byte carrying the seven
MSBs, then seven 7-bit bodies — so `wire(C) = ceil(C/7) + C`.

The payload window is 36 bytes (`buf[11..46]`, from the CHUNK=18 measurement),
and `C = 31` needs exactly 36. That is the largest that fits and **1.72x** the
old 18.

| | nibbles | 7-to-8 |
|---|---|---|
| bytes per message | 18 | **31** |
| four-word upload | 79 messages | **46** |
| 28-word sentence | ~517 | **~300** |

**VERIFIED.** The packed path reads back byte-for-byte clean: **0 bad of 346
words** over all 1,386 frames bytes, all **7 bank fields** correct, and the
fan-out reaching voice 3 — in **46 messages instead of 79**.

**The handler grew to 150 bytes** and `STUB_RESERVE` to 256 — which **costs no
extra vocabulary words**, because `bank_4`'s truncation already landed on
`0x0807B264`, exactly where a 256-byte reserve begins. Still 21 words, still
4,544 bytes.

Implementation notes worth keeping:

- The MSB byte is consumed with `lsrs r2,r2,#1` and a carry test, so bit 0
  applies to the **first** body byte. `pack78` therefore writes the MSBs
  **LSB-first** within each group. Getting that backwards would corrupt every
  high bit.
- `r8`/`r9` joined `r4`/`r7` on the push, since the unpacker needs a group
  counter and a byte counter at once. The reply path at `0x0804AE6E` only
  reloads `r7, r6, r5, r0-r2`, so everything else must be restored.
- The image diff is 155 bytes in four regions (the handler splits into two runs
  because two of its bytes happen to match factory). `bank_4` 4802 -> 4544 is
  the only descriptor change, exactly as before.

#### VERIFIED RECIPE: arbitrary vocabulary uploads correctly — 2026-10-05

**`CHUNK = 18` payload bytes per message, spaced 250 ms.** With that, a
four-word arbitrary-vocabulary upload lands completely, verified by reading the
device back:

| | result |
|---|---|
| frames, **byte for byte over all 1,386 bytes** | **0 bad of 346 words** |
| bank fields (count, `num_words`, 5 boundaries) | **all 7 words correct** |
| fan-out to voice 3 | **confirmed** — `[99, 4, 0, 21]` |

So the whole mechanism works: arbitrary word counts, arbitrary word lengths,
frames and boundaries, replicated across all four voices, with the `frames_`
pointer protected by the region bound.

**Two independent faults were in the way, and I conflated them in both
directions before separating them.**

1. **Per-message truncation at 18 bytes** — deterministic, explained above,
   fixed by `CHUNK = 18`.
2. **A variable, rate-dependent message loss.** At 60-80 ms spacing the upload
   is *unreliable*: one run verified 0 bad of 346, two later runs at the same
   spacing lost 3 of 6 spot-checked offsets in the back half. At **250 ms** it
   is clean. Releasing the held note made no difference, so this is transport,
   not the engine racing the buffer.

I had asserted a rate effect, then retracted it on finding the truncation, then
had to accept it again. The retraction was right about the *evidence* — those
measurements were reading truncation — and wrong to conclude the effect was not
there. **Two faults with overlapping symptoms: fix one, re-measure, do not
assume the other is gone.**

**The ordering question is now moot operationally.** `build_sentence` emits the
bank fields first and that is verified complete at 250 ms. Whether fields-last
also works at 250 ms is **untested** — it failed at 60-80 ms, which the rate
effect would explain, but I have not confirmed that.

**Throughput is the remaining cost.** At 18 bytes and 250 ms a four-word upload
is 79 messages (~20 s) and a 28-word sentence would be ~517 messages (~129 s).
The fix is to replace the nibble encoding with the firmware's own 7-to-8
packing: `(48-11) x 7/8` is about 32 bytes a message, which would roughly halve
both counts. There are 19 spare bytes in `STUB_RESERVE` and it can grow.

**Not yet done: hearing it.** The data is proven to land; whether a custom
vocabulary *sounds* like the intended words still needs ears, and the automated
identification test in this file is recorded as failing at chance.


### Why the patch is shaped this way

#### The patch design is sound, and this is why

Reading `LPCSpeechSynthWordBank::Init` (`0x0805DFD0`) settles a question the patch
depended on without having checked it — whether the knob-nudge reload would undo
the redirect:

    str r2,[r0,#0x4]      // num_banks = 5
    str r1,[r0,#0x0]      // word_banks_ = r1   <- the table pointer, STORED
    cmp.w r2,#0x3800      // allocator needs 14,336 bytes, else frames_ = NULL
    str.w r4,[r0,#0x94]   // frames_            (corroborates the map's [r5+0x94])
    str r3,[r0,#0x8]      // loaded_bank_ = -1

Slot 1 of each vtable is the reload the player triggers; for Speech it is a thunk
`add.w r0,r0,#0x168 ; b.w 0x0805E00C`, and `0x0805E00C` is `WordBank::Reset` —
`Init`'s tail **without** the two stores to `[r0+0x0]` and `[r0+0x94]`. It
invalidates `loaded_bank_` and clears the cache, and **preserves the table
pointer**. So `Init` is the one place the redirect has to happen, and a reload
cannot revert it. That is the design `mfreak speech upload` already has.

The engine switch itself is `0x0805F53E`:

    ldr.w r3,[r6,#0x65C]        // cached engine index
    add.w r1,r6,r7,lsl #0x2
    ldr.w r8,[r1,#0x6E4]        // engine_objects[index]
    cmp r3,r7 ; beq ...         // unchanged -> skip, which is why nudging works
    ldr.w r3,[r8,#0x0] ; ldr r3,[r3,#0x4] ; blx r3    // slot 1

**Consequence: the SysEx-patched image is not safe to flash yet.** If the
descriptor table stays zeroed, Speech falls silent until an upload arrives —
recoverable, but not the intended behaviour. `mfreak speech banks`'s flash
replacement carries no such caveat and is the path that is ready.

#### The slack does not exist, and two tools write into live data

**RETRACTED: "5,348 bytes of unreferenced slack between the end of `bank_4`
(`0x0807B367`) and the excitation LUT (`0x0807C84C`), which nothing
references."** It is referenced, seven times, and this is the *same* mistake
already retracted once for the zero runs at `0x08061C16`: a scan for quiet-looking
bytes measured nothing about whether they were used.

Method: build the set of words that a PC-relative `LDR` actually **reads**
(5,018 of them image-wide), rather than bucketing every 4-byte-aligned word.
The naive version is worthless here — `D8 F8 00 10` is `ldr.w r1,[r8]`, not the
address `0x1000F8D8` — and it is what makes a dense region look empty.

Real literals pointing into the supposed slack:

| target | loaded at | note |
|---|---|---|
| `0x0807B368` | `0x0805ECCA` | **live code**: `mul`/`vldr.32` around it, in the speech formant path |
| `0x0807B464` | `0x0805EFE6` | |
| `0x0807B4A4` | `0x0805EFE2` | |
| `0x0807B510` | `0x0805CECC` | |
| `0x0807BF20` | `0x0805D662` | |
| `0x0807C228` | `0x0807C5D0` | load site is itself inside the region — treat as unconfirmed |
| `0x0807C834` | `0x0805CEC6` | |

And the content is plainly structured, not filler: `0x0807BC00` holds a smooth
float32 curve (−0.8933, −0.8969, −0.9003, …) and `0x0807C700` a smooth int16
ramp.

**Two consequences, both concrete.**

1. **`mfreak speech upload`'s stubs are inside a live object.** `STUB_BASE =
   0x0807B380`, 176 bytes, sits 0x18 bytes into the object that begins at
   `0x0807B368` and is read by `0x0805ECCA`. That placement is unsafe and is a
   second, independent reason not to flash the SysEx-patched image.
2. **`mfreak speech banks` wipes all seven on every run.** It zeroes
   `VOCAB_START`..`VOCAB_END` = `0x08078860`..`0x0807C84B` — 16,364 bytes —
   *before* laying banks out, "so no stale bytes survive between banks". The
   vocabulary only occupies the first ~900 bytes, so every patched image so far
   has had those seven objects zeroed regardless.

**This confounds the 2026-10-04 control.** "The identical preset renders
completely differently under patched vs factory firmware, difference 123–128% of
signal RMS" was read as proof the vocabulary patch reaches the audio. The patched
image did **two** things: replaced vocabulary *and* zeroed seven referenced
objects, at least one of them in the speech signal path. The difference is real;
its attribution is not. **Re-run it with `VOCAB_END` fixed before relying on it.**

**The fix is a one-line bound, and it needs no new space.** `VOCAB_END` should be
`0x0807B367`, the true end of the five banks. No code references the bank regions
themselves — the three literals that appear to point there load from
`0x08078748`..`0x0807875A`, which is data just before the phoneme table, decoded
as instructions — so the banks are reached only through the descriptor table we
rewrite, and the region is genuinely ours. **That also tells us where the stubs
belong: the unused tail of the vocabulary region**, which `mfreak speech banks` should
reserve, rather than imaginary slack. With a 900-byte vocabulary there is ~10 KB
of it free.

**Runtime flash programming is not an option.** The region is not erased (no run
of 48 or more `0xFF` bytes exists anywhere in the image, nor any 256-byte zero
run), and STM32 flash only clears bits without a sector erase — the sector here
holds live code. Uploaded vocabulary must live in RAM.

#### Flash risk assessment: the bootloader question is largely moot — 2026-10-05

The standing worry has been that bootloader re-entry after a bad main image is
an unverified assumption. It still is — the bootloader at
`0x08000000`-`0x0800C000` is not in the update bundle and reading it would need
SWD. **But the assumption carries much less weight than it looks, for two
measured reasons.**

**1. Our container is byte-identical to Arturia's.** `microfreak.firmware.image.package()`
applied to the *unmodified* payload reproduces the shipped
`nanowave_main__fw5_0_0_2084__2023_05_30.bin` **exactly** — all 473,760 bytes.
So the 64-byte header, both XOR copies and the payload framing are not an
approximation of Arturia's format; they are that format. A patched image differs
from a factory one only in the 155 payload bytes we intend.

**2. Nothing we change is on the boot path.** Checked explicitly:

| region | status |
|---|---|
| vector table | **clean** |
| reset handler | **clean** |
| C runtime literals (`_sdata`..`_ebss`) | **clean** |
| `.data` initialiser source | **clean** |
| `init_array` constructor region | **clean** |

Initial SP and reset vector are unchanged. The three changed regions are a
4-byte detour in SysEx opcode `0x5A`'s handler (runs only when such a message
arrives), 2 bytes of `bank_4`'s size field (read only when Speech loads a bank),
and 149 bytes of handler inside `bank_4`'s old data (unreachable except through
the detour). **The device boots identically with or without the patch**, which
the emulator already shows as byte-identical audio with no upload.

**So the realistic failure mode is not "the image is invalid" — it is "the
transfer was interrupted"**, and that risk is identical to every normal Arturia
firmware update the instrument has already survived. Verifying the bootloader
would protect against a class of failure our patch does not introduce.

**A free incremental step, if more confidence is wanted:** flash the
*repackaged factory* image first. Since it is byte-identical to Arturia's own
file, that is literally flashing Arturia's file — zero added risk — and it
confirms the update path works on this machine before any of our bytes are
involved.

**What genuinely remains.** If the handler faults while processing a `0x5A`
message, the device can crash — but that is a RAM-only fault, recoverable by a
power cycle, and it cannot corrupt flash. The handler's writes are bounded to
`frames_` and a bank window that stops short of the `frames_` pointer, and an
out-of-range write is refused (verified).

#### Handler placement: reclaimed from the vocabulary budget — 2026-10-05

**Decided with the owner.** With custom vocabulary the built-in words are
irrelevant, so the handler comes out of the **vocabulary budget** rather than
costing a whole bank.

    0x08078860 .. 0x0807B2E4   vocabulary, 10,884 bytes   (was 11,015)
    0x0807B2E4 .. 0x0807B367   reserved for patch code, 131 bytes
    0x0807B367                 live objects -- NEVER write here

`CODE_BASE` in `microfreak.firmware.patching` owns the split, and `speech embed`
ends the banks there on a patched build, so the vocabulary can never run into
the code. The handler is 58 bytes, so
73 are spare for growth.

**Cost: one word.** The five factory banks need 11,011 bytes and only 10,884
remain, so `apply_patch` truncates the overlapping bank to a clean **word
boundary** — `bank_4` goes 4802 -> 4544 bytes, 22 -> 21 words. The loader
decodes words until the declared size runs out, so a cut mid-word would leave a
partial bitstream; `speech_lpc.word_byte_offsets()` exists to find the boundary.
That is one word against the seven an earlier version spent by emptying
`bank_0`.

**Order matters inside `apply_patch`.** Truncation is computed *before* the
handler is written, because the handler lands inside `bank_4`'s data and
corrupts the very bitstream the decoder has to walk to find a boundary. Writing
first made `decode_word` run off the end of the buffer.

**Verified.**

- **64 bytes change, in three regions**: the 4-byte detour, 2 bytes of
  `bank_4`'s size field, 58 bytes of handler. **Banks 0-3, `bank_4`'s kept
  portion, the live objects past the region and the phoneme table are all
  byte-identical** to factory.
- **The end-to-end result is unchanged** by the move: same RMS (1348.7 /
  2636.7 / 2982.4), same SHAs, same **75.4%** specificity, and the baseline is
  byte-identical to the previous placement's — word 7 is not the word that got
  truncated.
- **The two tools compose.** Running `mfreak speech banks` over a SysEx-patched
  image leaves the handler bytes, the whole reserved tail and the detour intact,
  and reports "6468 of 10884 bytes".

#### There is no free SRAM, so `0x2000C000` has to move

**`0x2000C000` is 48,392 bytes inside `.bss`.** Read from the C runtime's own
literals at `0x0805F884`..`0x0805F898`:

| symbol | value | |
|---|---|---|
| `_sdata` | `0x20000000` | |
| `_edata` | `0x200002F4` | `.data`, 756 bytes |
| `_sbss` | `0x200002F8` | |
| `_ebss` | `0x2001F230` | `.bss`, **126,776 bytes** |
| `_estack` | `0x20020000` | initial SP, from the vector table |

756 + 4 + 126,776 + 3,536 = **131,072 = exactly 128 KB**. Every byte of SRAM is
allocated; the stack gets the 3,536 bytes above `_ebss` and there is no heap gap
and no free region. So the earlier argument — "the only writer of `0x2000C000`
is the .bss clear, so it is written once before `main()`" — shows only that it is
unwritten *on the tested path*. It is a real `.bss` object. A 4 KB write there
corrupts it.

Note the literal map cannot prove the converse: the largest gaps between SRAM
address literals are 48,487 and 28,217 bytes, which are obviously big buffers
reached through a base pointer, not free space. **Gaps in the literal map are not
evidence of freeness.** `0x2000C000` sits in the 28,217-byte gap.

**Recommended: backup SRAM, `0x40024000`, 4,096 bytes.** The argument is
structural rather than observational:

- **The linker never allocates it.** It is outside `0x20000000`..`0x20020000`
  entirely, so it cannot collide with `.data`, `.bss` or the stack by
  construction — unlike anything inside SRAM.
- **No literal in the image points into it.** Zero of the 5,018 real literals
  land in `0x40024000`..`0x40024FFF`. For a peripheral-region RAM, code would
  have to load the base as a literal to reach it at all.
- **The clock is apparently never enabled.** `RCC_AHB1ENR` is reached as base
  `0x40023800` + `0x30`; the masks OR'd into it union to **`0x00400018` (bits 3,
  4 and 22 = GPIOD, GPIOE, DMA2EN)** and **bit 18 (`BKPSRAMEN`) is not among
  them**. There is no `0x40023830` literal and no bit-band alias for bit 18. If
  the clock is never enabled the firmware cannot be using it.
  *(Corrected 2026-10-05: this was first reported as `0x40000018`, bits 3/4/30,
  from a buggy `ThumbExpandImm`. The conclusion about bit 18 is unchanged.)*
- **It is an exact fit.** The design needs 40 bytes of descriptor table + 4 of
  magic + 4,052 of bitstream = 4,096.

**What is NOT vetted about it, and must be before flashing:**

- **The enable sequence is new code.** Backup SRAM needs the PWR clock, `PWR_CR`
  `DBP` set, and `RCC_AHB1ENR` bit 18. The firmware sets none of these — there
  are no `ORR.W #0x100` sites at all, so `DBP` is never set today.
- **Renode does not model it.** `microfreak.repl` declares no region at
  `0x40024000`. Adding a plain `Memory.MappedMemory` would make the address
  respond but would **not** model the clock/DBP gating, so a passing emulator
  test would not prove the enable sequence is right on hardware. This is the one
  part the emulator cannot vet.
- **The `AHB1ENR` mask scan is partial** — it only follows a 64-byte window
  after an RCC base load and only `ORR.W` immediates, and it found just 3 sites,
  which cannot be all of them. Treat "bit 18 is never set" as strong but not
  conclusive.
- **The part number should be confirmed against the chip marking.** Backup SRAM
  at `0x40024000` is present across STM32F4, but the F446 identification in this
  repository is asserted, not traced to primary evidence. The image is
  consistent with 512 KB of flash (it ends at `0x0807FA5F`, just under
  `0x08080000`, with `.data`'s source at the very top).
- **Zero headroom.** 4,096 bytes exactly, so the vocabulary cannot grow without
  a different home.

Rejected alternatives: the 3,536-byte stack gap (shared with the stack, and too
small); CCM at `0x10000000` (no evidence of use, but the two apparent literals
are misdecoded instructions and the F446 has no CCM — the Ghidra block came from
a setup assumption, not the device); and anything inside `.bss`, which is fully
allocated.

#### Where the RAM came from, and what was not proven

`0x2000C000` was chosen by measurement. A pattern written there **after** boot
survived a note and three further seconds intact at 8 of 8 sampled offsets, as did
`0x20013000`, `0x20019000` and `0x2001A000`; it also carries no literal references.

**And then it was explained, which beats evidenced.** A corrected watchpoint
caught exactly one writer of `0x2000C000`, at `0x0805F86C`:

    0805f86a  movs r3, #0
    0805f86c  str  r3, [r2], #4        ; r2 = 0x200002F8 .. 0x2001F230
    0805f87a  bl   0x8061420           ; __libc_init_array

That is the C runtime's **.bss clear**. `0x2000C000` lies inside .bss and is
written once, before `main()` — which is why a pattern written *after* boot
survives, and makes the address safe for a runtime buffer by mechanism rather than
by sampling. It also means `init_stub` cannot be losing its copy to startup: the
clear runs first.

Two caveats remain. The .bss span is the firmware's own static data, so a buffer
inside it must sit where no C object claims it; survival after boot is good
evidence, not proof, and preset loads, sample and wavetable uploads and long
running were never exercised. And `../renode/NOTES.md` notes the platform declares 256 KB of
SRAM while the F446 has 128 KB, so **`0x20030000+` is free in the emulator and does
not exist on hardware** — a trap this deliberately avoids.

An earlier version of this survey "showed" four regions were busy; the control
proved it had written nothing at all, because `LoadBinary` into CCM is silently
dropped — CCM is absent from the Renode platform, a block list from Ghidra's
address map that I had mistaken for the emulator's.

Nibble encoding is used rather than the firmware's 7-to-8 packing because SysEx is
7-bit and the unpacker is then three instructions instead of a loop carrying
state. It doubles transfer size, which is not the constraint.

#### Design notes for the reader

- QSPI is not an option: the firmware holds no pointers into a memory-mapped QSPI
  window — 274 word-aligned values land in `0x90000000` and 155 in `0xA0000000`,
  both *below* chance for random data, so they are noise.
- The dispatcher at `0x0804ACC8` biases `buf[7]` by `0x15` over `0x15..0x5D`
  through a `tbh` table at `0x0804AD20`; 16 of 73 entries are implemented. A `tbh`
  entry cannot reach the end-of-flash slack (unsigned halfword x2, so below
  `0x0806AD1E`), which is why a detour from an implemented handler is used instead
  of a new table entry.
- The frame is `F0 | 00 20 6B 07 | 01 | seq | cat | opcode | args | F7`, so the
  opcode is the **ninth** wire byte. An earlier draft of `messages()` put it at
  `buf[6]` and would have dispatched as a different command entirely.


### What the firmware does with a word bank

#### `osc_type` must go through `set_osc_engine` — 2026-10-06

`osc_type` is a TLV record with a count byte and a split 15-bit value, not a
bit-packed control field. `mfreak preset encode` wrote it with
`set_field_raw(rows, field, field["inverse"][value])`, which puts the value in
the wrong place: the preset reads back as the requested engine through
`_OSC_TYPE_NAMES` but **is not on it**, and renders silent.

Cost: a silent preset written to the owner's panel 223, and two voided emulator
runs. Fixed, and the fix asserts `PF.osc_engine(rows)` afterwards.

**Anything that sets the engine through `set_osc_engine` was never affected**,
and a generator that excludes `osc_type` from the controls it varies cannot hit
this at all.

#### RESOLVED: `utterance_start` needs a gate edge — 2026-10-06

Open since the probe work (this file, "whether `utterance_start` needs a gate
edge the probe did not produce"). **Measured on hardware: it does.** Holding a
key and crossing a word boundary with the Shape knob does **not** sound the new
word; it sounds on the next keypress.

**Consequence: no ramp can drive a sentence.** The cycling envelope → Shape
design is dead -- a smooth modulator changes the word *index* but never supplies
the edge that starts an utterance. Any sentence feature must deliver **one gate
edge per word**. The surviving candidate is `Key/Arp` → `Assign1` →
`VCO/Param3` with the arpeggiator running over a held chord: every arp step is a
new note, hence an edge, and `key_arp` emits a different value per step. It is
also monophonic, which dodges the multi-voice reload below. **Untested.**

#### A held key keeps playing the last frame — 2026-10-25

Holding a key through the end of an uploaded word sustains a buzzing tone.
Factory words fall silent there. All 91 words in factory banks 1-4 end on an
energy-0 frame, and our words ended wherever the analysis stopped, so the
engine went on playing that last voiced frame. A three-word phrase uploaded
with `speech banks --voice Fred` ended on energy 20, read back from the
instrument.

Every word now gets an energy-0 final frame (`lpc.end_with_silence`), costing
one frame and about a byte per word. Confirmed on hardware: a held key now
falls silent after the word.

#### Paraphonic at the bottom edge of bank 1 — 2026-10-25

In paraphonic, with Wave just above the vowel region, one voice can speak bank 1
while the others play vowels: a held chord gives the word once and three
sustained tones, and single presses alternate between the word and a short vowel.
Read back from the instrument in that state, voice 0 had `loaded_bank_ = 0`
with the uploaded word, and voices 1-3 had `loaded_bank_ = -1`, 0 frames and
0 words, the state a voice keeps while it requests no bank.

With the same upload and Wave turned down into bank 1 from above, a 4-key chord
spoke the word on all four voices, and all four read back `loaded_bank_ = 0`
with 56 frames. A bank of 105 frames at Wave position 5 also loaded on all four
voices, so bank size is not the cause. The likely mechanism is the Wave
quantizer's hysteresis (above) being kept per voice, so a voice that starts
fresh can land on the other side of the boundary. That part is inferred, not
read from the firmware.

#### Per-voice vocabulary is real, and why our upload did not reach all 4 voices

Found by the owner on a whim: with Shape on our word *freak*, pressing Paraphonic
and then playing keys gave *freak* on the previously-played key and the **factory**
word *indigo* on every other key.

**The mechanism, and it is not a bug.** Each voice owns a bank object and a
`frames_` buffer (stride `0x59E4`):

    voice 0   bank 0x200093AC   frames_ 0x20004880
    voice 1   bank 0x2000ED90   frames_ 0x2000A264
    voice 2   bank 0x20014774   frames_ 0x2000FC48
    voice 3   bank 0x2001A158   frames_ 0x2001562C

The handler **does** fan every write out to all four, so all four did receive our
frames and our boundaries. What they did not receive is `loaded_bank_`, which
lives at **bank+0x08** -- *below* the handler's writable window, which starts at
**+0x0C** by design. The handler physically cannot set it.

So at upload time voice 0 had `loaded_bank_ = 0` (a note had loaded it) and
voices 1-3 were still at `-1`. The first note on each of those voices hit the
lazy-load guard at `0x0805E646`, saw `-1 != 0`, and **reloaded `bank_0` from
flash**, decoding the factory colors straight over our frames. Voice 0's guard
matched and short-circuited, so it kept our word. Word *index* is shared across
voices, which is why index 3 gave our *freak* on one voice and factory *indigo*
on the others.

**Operational rule: play a CHORD before uploading.** That loads all four voices
from flash first, setting every `loaded_bank_`, after which the upload sticks on
all of them and none will reload. A single note loads voice 0 only.

**The lead this opens.** Four independent `frames_` buffers means four voices
could hold *different* vocabulary -- different notes saying different words, a
per-voice vocabulary split, which the instrument has no native concept of. The
blocker is that the handler's fan-out writes **identical** bytes to all four and
the bounds check (`FRAMES_SIZE = 0x3800`, stride `0x59E4`) prevents reaching one
voice's buffer from another's base. Per-voice addressing is a **firmware change**,
not a SysEx one. Worth its own session.

#### ANSWERED 2026-10-04: every engine's Init is called, unconditionally

**The open question was "does the firmware call every engine's Init at startup or
only the selected engine's". It calls every one.** Read, not emulated, and the
reading is cross-checked below.

**There is no table walk.** `0x08078578`..`0x0807861C` are seven **C++ vtables**,
not descriptor records reached by a loop. Each is referenced by one inline
`ldr r3,[pc]` in the constructor `FUN_080359b0`, which installs the seven vptrs
into one object:

    0x08035DE8  ldr r3,[0x080361B4] ; str.w r3,[r10,#0x674]   vt0
    0x08035DEE  ldr r3,[0x080361B8] ; str.w r3,[r10,#0x7C4]   vt1
    0x08035DF4  ldr r3,[0x080361BC] ; str.w r3,[r10,#0x7FC]   vt2
    0x08035DFA  ldr r3,[0x080361C0] ; str.w r3,[r10,#0x880]   vt3
    0x08035E00  ldr r3,[0x080361C4] ; str.w r3,[r10,#0x9C4]   vt4  <- Speech
    0x08035E06  ldr r3,[0x080361C8] ; str.w r3,[r10,#0xBD4]   vt5
    0x08035E0C  ldr r3,[0x080361CC] ; str.w r3,[r10,#0xC78]   vt6

`0x080361B4` therefore has no reference because it is a **literal pool**, not a
table. Nothing was ever supposed to walk it.

**The vtables are Itanium-ABI, four virtuals each.** `0x08078570`/`0x08078574`
are the zero header (offset-to-top, typeinfo — built `-fno-rtti`), the vptr
points at slot 0, and **`SpeechEngine::Init` is slot 0**. Slot 3 is the same
address `0x08058369` in vt0, vt2, vt3 and vt4 — a shared base-class default,
which is what confirms these are vtables rather than data.

**`FUN_0805F2C4` is the Init-all-engines loop**, reached as
`osc vtable (0x08074D14) slot 3 -> FUN_08030744 -> FUN_0800D49C -> FUN_0805F2C4`:

    *(param_1+0x6E4..0x6FC) = the seven engine `this` pointers
    *(param_1+0x724)        = 7                       // count, a hardcoded literal
    do {                                              // 0x0805F36A
      puVar5 = *++puVar8;                             // next engine object
      pcVar6 = **puVar5;                              // vptr slot 0 = Init
      *param_2 = param_2[1];                          // reset the allocator, so
      param_2[2] = param_2[3];                        //   all seven share a pool
      (*pcVar6)(puVar5, param_2, ...);                // Init(this, allocator)
    } while (++iVar7 < *(param_1+0x724));
    *(param_1+0x65C) = 0xFFFFFFFF;                    // loaded index = -1

No conditional guards the body and the count is the literal 7, so **all seven
Inits run, once, when the voice object is initialised.**

**Cross-check, and it is exact.** The constructor's vptr offsets and the loop's
engine-object offsets are two independent readings of the same seven objects. The
delta is `0x674` for **all seven** — `FUN_080359B0`'s base is `param_1-0x674`:

| array slot | engine `this` | vptr offset | vtable | Init |
|---|---|---|---|---|
| `+0x6E4` | `param_1+0x560` | `0xBD4` | vt5 `0x08078604` | `0x0805B4DC` |
| `+0x6E8` | `param_1+0x604` | `0xC78` | vt6 `0x0807861C` | `0x0805C950` |
| `+0x6EC` | `param_1+0x150` | `0x7C4` | vt1 `0x08078590` | `0x080591FC` |
| `+0x6F0` | `param_1+0x188` | `0x7FC` | vt2 `0x080785A8` | `0x080599C4` |
| `+0x6F4` | `param_1+0x000` | `0x674` | vt0 `0x08078578` | `0x0805836C` |
| `+0x6F8` | `param_1+0x350` | `0x9C4` | vt4 `0x080785D8` | **`0x0805B18C` Speech** |
| `+0x6FC` | `param_1+0x20C` | `0x880` | vt3 `0x080785C0` | `0x0805B14C` |

**Speech is index 5 of seven**, so its Init is the sixth iteration. Which engines
the other six are is still unknown; only the structure is established here.

**The detour site is in Init's entry basic block.** `0x0805B18C`..`0x0805B1A8` is
straight-line — `push; mov; adds; mov; add.w; bl; add.w; bl; mov; mov` — with no
conditional branch. The two `bl`s leave and return. Init's own three internal
branches (`bls 0x0805B1F8`, `bls 0x0805B1E4`, `b 0x0805B1E4`) all land *after*
the site, and an image-wide `BL`/`B.W` scan finds **nothing** branching into
`0x0805B18D`..`0x0805B1AA` — nor any `BL` to the entry itself, which is the
already-recorded fact that it is vtable-dispatched only. (Short `B`/`B<cond>`
were not scanned image-wide, but their range only reaches adjacent code, and
Init's own body is accounted for.) **Entering `SpeechEngine::Init` at all
reaches `0x0805B1A8` unconditionally.**

**So reading says the detour site IS reached, and that contradicts the watchpoint
run.** Both cannot stand. The reading is firm on everything except one link it
cannot resolve: osc vtable slot 3 is an indirect call, so whether it fires on the
tested boot path is unproven. Resolve that before touching `init_stub` — see
"What to test next" below.

#### Is the truncated boundary respected, or is code read as data? — 2026-10-05

Owner's safety question: with 58 bytes of handler sitting where `bank_4`'s last
word used to be, what happens if the UI selects that word? **The boundary is
respected and the code is never reached.** Measured, not reasoned.

**Why structurally.** The loader reads only `ptr`..`ptr+size`, which after
truncation is `0x0807A0A4`..`0x0807B264`. The handler is at `0x0807B2E4` — **128
bytes beyond the declared end** — so the bitstream decoder never walks into it.
`num_words` then falls out of the decode, and the Word knob is
`int(address * num_words)` clamped, so it cannot index past the words that were
actually decoded.

**What the running firmware reports**, patched image, owner's preset:

| | factory | patched |
|---|---|---|
| `loaded_bank_` | 4 | 4 |
| frame count | 926 | **880** |
| `num_words` | 22 | **21** |

**And the audio**, Word at maximum versus a surviving word:

| | rms | peak | sha |
|---|---|---|---|
| patched, Word=100 → word 20 | 570.2 | 6016 | `1126ba2bbbb9` |
| factory, Word=100 → word 21 | 1095.4 | 11684 | `216d8d2c6fe1` |
| patched, Word=35.7 → word 7 | 886.8 | 10458 | `3b117431fa5f` |
| factory, Word=35.7 → word 7 | 886.8 | 10458 | `3b117431fa5f` |

- **max Word on the patched image plays a real word** — audible, peak well
  inside int16, no runaway or noise burst, which is what decoding code as frame
  data would look like;
- **every surviving word is byte-identical** to factory (word 7 matches exactly
  on rms, peak and hash), so nothing shifted;
- max Word legitimately differs between the two, because factory has 22 words
  and the patched image has 21 — that is the one word spent, not a fault.

**The only user-visible effect** of the patch is that `bank_4` has 21 words
instead of 22, so turning Word to maximum lands on word 20. The dropped word's
bitstream bytes are still physically present in the gap between the declared end
and the handler; they are simply never declared and never read.

#### Ghidra settles it: ONE engine set, built once -- 2026-10-08

The owner's point, and he was right: paraphonic is a condition in code, and the
decompiler was available the whole time. The chain, all from the existing `~/MFC`
Ghidra project:

    FUN_08037c90        calls FUN_080359B0 (installs the 7 engine vptrs) ONCE,
                        at 0x08037CC2, on one ~94 KB object
    FUN_0803a670        calls FUN_08037c90 ONCE, no loop:
                            FUN_08037c90(DAT_0803a728);
    0x0807F654          FUN_0803a670+1 sits in a table of ascending Thumb code
                        pointers -- a .init_array static-constructor table, so
                        it runs exactly once at startup

MFRefs says FUN_0803a670 has "no references -- reached indirectly only", which
METHOD.md warns can be a false negative; the init_array membership is what
actually establishes "once", and it was checked rather than assumed.

**Therefore there is one Speech engine and one `LPCSpeechSynthWordBank`, and
paraphonic cannot give each note its own word bank.** Every paraphonic note
shares the single bank. This is the premise v8 rests on, now confirmed from the
code instead of inferred from emulator behaviour, and it means v7's fan-out to
`BANK_BASE + k*VOICE_STRIDE` for k=1,2,3 was writing unrelated memory on every
upload message.

It also reframes the owner's original hardware observation -- our word on one
keypress, a factory word on the next -- as the single bank being reloaded
between keypresses, not per-voice state.

**Already documented, and missed.** EMULATION.md has carried
"paraphonic presets render silent ... **resolved on Renode** (2026-09-07) --
does not reproduce on this harness" and "Paraphony follows the preset's
`Parafon` parameter in both directions" since September. A day of emulator
archaeology re-derived that. Read the gap table before chasing an emulator
behaviour.

#### The engines DO share a buffer pool — mapped 2026-10-05

Asked whether the engines address shared buffers with uneven use, leaving slack.
**They do share one, and the use is wildly uneven — but the slack is in the wrong
place, because Speech is the engine that needs nearly all of it.**

**The pool: `audio_obj+0x50`, 16,384 bytes.** Set up at the osc vtable slot-3
call site:

    0x08038876  ldr r3,[r0,#0x0]       ; vptr
    0x0803887A  ldr r7,[r3,#0xc]       ; slot 3 = FUN_08030744
    0x0803887C  add.w r2,r4,#0x50      ; pool base
    0x08038880  mov.w r3,#0x4000       ; pool size = 16,384
    0x08038884  blx r7

`FUN_08030744` forwards both to `FUN_0800D49C`, which builds a four-word
BufferAllocator `{ptr, base, remaining, total}` and runs the Init loop. The
loop resets `ptr = base` and `remaining = total` **before each engine**, so all
seven engines' buffers occupy the *same* memory — Plaits' shared-scratch trick.

**Absolute addresses.** `audio_obj = 0x20004830`, derived from the live
observation in FIRMWARE.md (`HAL_SAI_Transmit_DMA` buffer `0x2001B6A2`, which
is `audio_obj+0x16E72`) and independently confirmed: `0x20004830` is a real
literal referenced seven times, one of them at `0x0803883C`, immediately beside
the slot-3 call above.

    pool        0x20004880 .. 0x20008880    16,384 bytes
    Speech uses 0x20004880 .. 0x200081A0    14,624 bytes
    UNTOUCHED   0x200081A0 .. 0x20008880     1,760 bytes

**Per-engine consumption**, from the allocator-advance sites (a store to
`[alloc,#8]` preceded by an immediate subtract, which is the size):

| engine | bytes | where |
|---|---|---|
| **vt4 Speech** | **14,624** | `0x3800` in `WordBank::Init` + `0x60` + `0xC0` |
| vt0 | 220 | `0x0805847C` |
| vt3 | 96 | `0x0805B156` |
| vt5 | 96 | `0x0805B54E` |
| vt1, vt2, vt6 | 0 | no allocation, and no `BL` at all |

Speech is the **only** engine that calls anything; the other six are
straight-line, so their totals are complete rather than merely unsearched.

**Two things make the untouched tail trustworthy, and one does not.**

- **The allocator is a stack local.** `0x0800D49E sub sp,#0x18` then
  `0x0800D4A4 add r1,sp,#0x8` — the struct lives at `sp+8` and dies when
  `FUN_0800D49C` returns. **Nothing can allocate from this pool after the Init
  loop**, so the tail above the high-water mark is never reached by any
  allocation, ever.
- **No address literal lies inside the pool.** Zero, against 5,018 real
  literals image-wide; the nearest are `0x20004848` below and `0x20008A90`
  above. That is what allocator-managed space should look like.
- **But this is still `.bss` inside the audio object.** It is "space no
  allocation reaches", not "space outside every object" — a weaker argument
  than backup SRAM's, though far stronger than the original `0x2000C000` guess.

**1,760 bytes is not enough.** The design needs 40 + 4 + 4,052 = 4,096. The tail
holds the 44-byte descriptor table and magic comfortably, and the bitstream not
at all.

**The lever that does exist: Speech's own frames buffer.** The 14,336 bytes is
exactly `kLPCSpeechSynthMaxFrames (1024) x 14 bytes per frame`, and it is three
patchable immediates in `WordBank::Init`:

    0x0805DFD8  cmp.w r2,#0x3800
    0x0805DFE0  sub.w r2,r2,#0x3800
    0x0805DFE4  add.w r1,r4,#0x3800

Lowering the frame cap trades maximum frames per bank for pool space, 14 bytes a
frame:

| cap | frames buffer | freed | total free tail |
|---|---|---|---|
| 1024 (stock) | 14,336 | 0 | 1,760 |
| 857 | 11,998 | 2,338 | 4,098 — just enough |
| 768 | 10,752 | 3,584 | 5,344 |
| **512** | **7,168** | **7,168** | **8,928** |

**512 is the recommendation if this route is taken**: 8,928 contiguous bytes at
the top of the pool, more than double what is needed, and 512 frames a bank is
~50 words — far beyond the ~27 that 4,052 bytes of RAM bitstream can hold anyway.

**The cost is explicit:** factory `bank_4` holds 926 frames and would no longer
fit, so a lowered cap means the factory vocabulary cannot be kept in that bank.
`mfreak speech banks` would need its `kLPCSpeechSynthMaxFrames` check lowered to
match, which it already enforces as an error.

**Not yet verified, and cheap to check:** the per-engine figures come from a
static scan. The firmware itself keeps a usage counter at `0x20000314` (written
by `FUN_0800D49C` as `total - remaining`), but because the allocator is reset
before each Init it records only the **last** engine in the loop — index 6,
which this scan says allocates nothing. **So `0x20000314` reading 0 at runtime
would corroborate the vt6 figure**, and a watchpoint on `0x200081A0` across a
boot that exercises every engine would test the tail directly.


### Limits, and what is still open

#### Source audio is unchecked, and a TTS artifact shipped

`say "I"` renders the phrase *"capital I"* -- a lone uppercase letter is
announced, not spoken as a word. It reached the instrument and was audible as
such. `say "i"`, `"aye"` and `"eye"` all normalise to the same correct 21-frame
utterance.

The 39-frame analysis (vs 21 for *am*) was visible in the build output and read
as "long diphthong". **Nothing in the chain inspects what the audio says**, and
the identification test earlier in this file failed at chance, so no automated
check is worth adding. Listen to the source audio before uploading it.

#### Maximum custom vocabulary: the space is there, the ENCODER is not — 2026-10-05

Goal (owner): fill all five banks with custom words, so a whole sentence sits in
one bank and the cycling envelope can step the Word knob through it on a single
held key. Built it, and found a hard blocker in our own encoder.

**The capacity answer.**

| | |
|---|---|
| flash for vocabulary | **10,884 bytes** (131 reserved for the handler) |
| encoded cost | **4.2-4.6 bytes per frame**, ~120-145 bytes per word |
| total | **~80 words** across the five banks |
| per bank | **31 words**, 1024 frames (~22 s at 40 FPS) |

Earlier notes said ~70 words at 149 bytes each; measured, it is better than that.

**NEW HAZARD, and `MAX_WORDS` is now 31.** `kLPCSpeechSynthMaxWords` is 32, but
32 is unsafe here. `word_boundaries_` is `bank+0x14`..`bank+0x94` — the memset in
`WordBank::Init` is `0x80` bytes, so exactly 32 `uint32` entries — and the table
needs `num_words+1` of them, because `0x0805E928` writes `bounds[num_words]` as
the final frame count. At 32 words that store lands on `bank+0x94`, **which is
the `frames_` pointer itself**. The factory banks peak at 26 words so nothing
ships near it. `speech_lpc.MAX_WORDS` is now 31.

**A 28-word sentence bank builds and decodes correctly.** 28 words, 655 frames,
2,616 bytes, "vocabulary region uses 8816 of 10884". Decoding it back the way
`WordBank::Load` does gives 28 words and every word's first frame has non-zero
energy (81, 161, 20, 57, 161, 161, …).

**But the firmware will not play it.** Sweeping the Word knob across the custom
bank, **six of seven positions are silent**:

    Word   2  15  30  45  60  75  90
    rms   0.0 0.0 0.0 0.0 49.5 0.0 0.0     factory bank_4 at Word 35.7: 1348.7

Factory banks in the same image, same preset, same renderer play normally. So
**our bitstream encoder produces streams the firmware largely cannot play**, even
though they round-trip through our own decoder. Self-consistency is not
agreement: the decoder is proven against the firmware's five banks, the
**encoder has only ever been proven against our own decoder**.

**Leading silence was NOT the cause here**, though it is a real bug and is now
fixed in both paths via `speech_lpc.trim_leading_silence()` (words with
zero-energy first frames render at exactly 0.0 RMS; every factory word starts
non-zero). Trimming changed the encoding by 12 bytes and changed nothing about
the silence, and the decoded first frames are all non-zero. Something else in
the encoder is wrong.

**This explains why the SysEx path works and the flash path does not**, and it
vindicates the decoded-frames design twice over: **writing `frames_` bypasses the
encoder entirely.** The flash path has to encode a bitstream; the SysEx path
ships the decoded 14-byte frames the firmware would have produced.

**So the route to the sentence feature is SysEx, not flash.** What it needs,
which is now unblocked because the bank object addresses are known
(`0x200093AC`, `0x2000ED90`, `0x20014774`, `0x2001A158`): write
`word_boundaries_` at `+0x14`, `num_words` at `+0x10` and the frame count at
`+0x0C` alongside the frames, instead of inheriting the factory boundaries. That
gives fully arbitrary vocabulary up to 31 words / 1024 frames per bank with no
encoder involved. Volatile, which is what auditioning wants anyway.

**The encoder is NOT a blocker for the product, and the owner was right to
question the priority I gave it.** The dependency split is clean:

| path | uses from `speech_lpc` |
|---|---|
| **SysEx (the product)** | `decode_bank`, `frames_to_bytes`, `trim_leading_silence`, `word_byte_offsets` |
| **flash (`mfreak speech banks`)** | **`encode_bank`** — the only real consumer |

The goal is to ship firmware whose vocabulary is **live-loaded over SysEx**, so
nothing we encode ever reaches flash and the encoder bug cannot reach the
product. And the two things the SysEx path does depend on are verified *against
the firmware*, not merely against each other:

- `decode_bank` matched the firmware's own `word_boundaries_` across all 12
  entries checked, on top of the existing proof against all five factory banks;
- `frames_to_bytes` matched the live `frames_` buffer **byte-for-byte** at every
  probe inside word 7, including `frames_[0]` = `0xD8C045A1`.

That is the distinction the encoder lacks. `mfreak speech banks` is therefore an
optional side tool, not part of the deliverable.

**The shipping image is 64 bytes of change**: 58 of handler, 4 of detour, 2 of
`bank_4`'s size field. Boot gives Arturia's vocabulary — their bitstreams, which
play fine — and an upload replaces the decoded frames live.

**Next, in order.**

1. **Write boundaries over SysEx** for arbitrary word counts and lengths, which
   is what the sentence feature needs. The bank addresses are known; only voice
   0 loads for a single note, but write all four for paraphonic safety.
2. **Fix the encoder** only if persistent custom vocabulary in flash ever
   becomes a goal. It is not one now. If it does: the test must be a round trip
   *through the firmware* — encode, flash, read `frames_` back, compare against
   the frames that went in, with our decoder nowhere in the loop.

#### What to test next, in order

The reading above says the detour site is reached; the watchpoint run said
`init_stub` never wrote. One of the two is wrong, and these separate them.

1. **Does `FUN_0805F2C4` run at boot?** This is the one link reading cannot
   close, because osc vtable slot 3 is an indirect call. Watchpoint the data,
   not the PC: the loop writes the literal `7` to `*(param_1+0x724)` and
   `0xFFFFFFFF` to `*(param_1+0x65C)` on every run. **Include a positive
   control** — the .bss clear at `0x0805F86C` is already proven to fire.
   If this does not run, the detour is fine and the dispatch is the problem.
2. **If it does run, `init_stub` itself is the bug.** It is then reached and
   not writing, so suspect the stub body rather than the branch: the magic
   check at `0x2000C028`, the copy width, or the return to `0x0805B1AC`.
3. **Residual risk, unquantified: `0x2000C000` is inside the .bss clear range**
   (`0x200002F8`..`0x2001F230`), so it is *allocated* .bss, not space past
   `_ebss`. "The only writer is the .bss clear" shows it is unwritten **on the
   tested path**, which is not the same as unowned — a variable belonging to an
   untested feature would look identical. Before flashing, watchpoint the whole
   `0x2000C000`..`0x2000D000` for **any** access across a boot that exercises
   more than Speech. Cheap, and it is the one thing that could corrupt
   unrelated state.


### How it got here: superseded steps and retractions

#### First generation — superseded, kept for the reasoning

**Two 4-byte detours, two stubs, 176 bytes of the 5,324-byte slack.**

    0x0807B380  init_stub       118 bytes
    0x0807B3F6  vocab_handler    58 bytes
    0x0805B1A8  b.w init_stub       (was `movs r2,#5 ; ldr r1,[pc,#0x54]`)
    0x0804AF28  b.w vocab_handler   (was opcode 0x5A's body)

    0x2000C000  5 x {data, size}   descriptor table, now in RAM
    0x2000C028  magic 'MFVC'       present once an upload has happened
    0x2000C02C  bitstreams         4,052 bytes, about 27 words

The `0x2000C000` relocation was abandoned: the address was inside `.bss` and the
stub that seeded it never ran. See "The memory hunt was mostly unnecessary".

`init_stub` takes over `SpeechEngine::Init`'s argument setup, points `r1` at RAM
instead of the flash table, and copies the factory descriptors in **only when the
magic is absent** — so a cold boot gets the factory 92 words and a re-Init cannot
wipe an upload. `vocab_handler` takes over opcode `0x5A`, whose own job was to
clear the sample directory; that is the single capability this spends. It is a
bounded memory write — `buf[8..9]` offset, `buf[10]` count, then nibble pairs,
refusing anything past the buffer — and it falls into the original handler's tail
at `0x0804AF2E` so the device's reply is unchanged.

> **Superseded, 2026-10-13.** This paragraph is the first generation. The
> payload is 7-to-8 packed now, not nibble pairs, and the opcode is **`0x45`**
> (claimed through the `tbh` table, so `0x5A` stays factory).
>
> **There are TWO SysEx opcode dispatchers.** The table at `0x0804AD20` is not
> the only one: `0x08046FA0` reads the same opcode byte with bias `0x1C` and
> bound `0x37`, and ten opcodes bail in the first while dispatching in the
> second — `0x1C 0x1D 0x40 0x41 0x42 0x43 0x47 0x49 0x4C 0x53`. v12 through v15
> claimed `0x40` and `0x41`, both on that list, so every message ran an Arturia
> handler on our bytes; `0x41`'s indexes a pointer array at `0x2001C8C4` with no
> bounds check and bus-faults into a `b .`. **That is the upload freeze.** The
> check that matters is `speech_sysex.assert_opcode_free`, which reads both
> tables out of the image. See `captures/validated-payloads/README.md`.

A reload is forced by the player: `Load()` early-returns while
`bank == loaded_bank_`, so nudging the Type knob off the bank and back makes the
firmware re-read the descriptors. That is why the handler needs no knowledge of
the word-bank object layout, which Arturia restructured away from Plaits'.

**Register safety was checked, not assumed.** The reply path at `0x0804AE6E`
reloads `r7, r6, r5, r0, r1, r2` from the outgoing buffer before using them, and
reads `[r5,#8]` before overwriting `r5`, so the handler's use of `r6` and `r0-r3`
cannot corrupt the response.

#### v2's `loaded_bank_` propagation is INERT — 2026-10-06

Flashed, and it does nothing. The handler copies voice 0's `loaded_bank_` to the
other three on every upload (emulator-verified: sentinel in, four values out,
with v1 leaving them untouched as the negative control). **But a voice Resets
when it is brought into service, which is always AFTER an upload**, so the
propagated value is overwritten before it can matter. Confirmed on hardware:
paraphonic chords still spoke factory words on every voice but the one that had
already played.

It is harmless and costs 24 bytes. It is kept only so the next session does not
re-derive it. **Setting `loaded_bank_` at upload time cannot work** -- the fix
has to survive Reset, which is what the RAM-table redirect does.

The emulator test that passed it (`test_voice_propagate.py`) was a unit test
of the instruction with no Reset in the loop. It was labelled as such and the
pass was still over-read. An end-to-end claim needs an end-to-end vehicle.

#### Verified in the emulator

- **The handler runs and writes RAM.** A real SysEx message
  (`F0 00 20 6B 07 01 00 00 5A 02 00 04 0D 0E 0A 0D 0B 0E 0E 0F F7`) put
  `DE AD BE EF` at `0x2000C100` on the patched firmware. New machine code, real
  firmware, real MIDI path.
- **The `b.w` encoder is verified against the image**, reproducing two existing
  branches byte-for-byte (`0x0805B208` forward, `0x080450AA` backward) before
  being trusted to emit any.
- **Both detours and both stubs disassemble as intended** at their final
  addresses, including the ten-word descriptor copy landing exactly on
  `movs r2,#5`.

#### `init_stub` is NOT verified, and three diagnostics of it were void

The upload half works. The boot-seeding half does not, or cannot yet be shown to:
`0x2000C000` reads zero after a boot with a Speech preset loaded and a note
played, so the factory descriptors are not reaching RAM.

Three attempts to diagnose it produced nothing usable, each for a reason already
written down in this repository:

1. **`cpu AddHook` on five addresses fired zero times** while `main()` fired, so
   the run proved only that hooks work *somewhere*. `METHOD.md` records the same
   failure on `osc_set_gate` and says to prefer a watchpoint on the data.
2. **Two watchpoint runs fired zero times, including on a write known to
   happen.** The positive control is what caught it. The hooks threw instead of
   logging: in a watchpoint hook `self` is the **bus**, not the CPU — PC comes
   from `self.GetCurrentCPU().PC.RawValue` — and `../renode/NOTES.md` adds that every .NET
   value must be wrapped in `int()` or IronPython throws fatally. Both rules are
   in `../renode/NOTES.md` and both were violated.

A fourth run with the idiom corrected did work — control and subject both fired —
and it settles the question: the only writer of `0x2000C000` is the .bss clear.
Since that precedes `__libc_init_array`, a copy by `SpeechEngine::Init` would have
survived, and distinct PCs produce distinct log text so suppression cannot hide a
second writer. **`init_stub` does not run on the path tested.**

What the firmware reading did establish: **`SpeechEngine::Init` has no `bl`
caller.** Its Thumb-bit pointer `0x0805B18D` is referenced exactly once, at
`0x080785D8`, inside the engine descriptor table at `0x08078578` — so it is
dispatched indirectly through that record, which is also why a direct hook on it
would be an unreliable probe.

#### Test render: four arbitrary words, individually addressable — 2026-10-05

Clips in `data/speech/sysex/` (`0-BASELINE-factory`, then `1-the` .. `4-speaks`),
and the flashable image alongside them as `custom_fw_sysex.bin`.

A four-word vocabulary with its **own boundaries** uploaded over SysEx into the
owner's preset 180, then rendered at four Word-knob positions:

| Word position | expected | rms |
|---|---|---|
| — (no upload) | factory word | 1333.4 |
| 0 | *the* | 1501.8 |
| 1 | *micro* | 1720.0 |
| 2 | *freak* | 958.4 |
| 3 | *speaks* | 716.8 |

**All four audible, all four distinct from each other, all four distinct from
the no-upload baseline.** So the Word knob indexes our boundary table and each
slot carries different audio — which is what the cycling-envelope sentence idea
needs.

**What this does and does not establish.** It shows the data arrives, the
boundaries take effect and the words are separately addressable. It does **not**
show the words are intelligible: that needs ears, and the identification test
recorded earlier in this file failed at chance, so no automated check here is
worth anything. The clips are for the owner to judge.

#### THE BUG: 18 payload bytes per message, and three retractions — 2026-10-05

**One limit explains every transport symptom in this file.** The firmware's
SysEx receive buffer gives us **48 bytes**. Our payload starts at `buf[11]` and
the nibble encoding costs two bytes per byte, so exactly

    (48 - 11) / 2 = 18

whole bytes per message are decodable. Everything past byte 18 is stale buffer
content. `CHUNK` is now 18, measured rather than chosen.

**The measurement**, byte-for-byte over a 240-byte contiguous region:

| CHUNK | bad words | bad offsets mod CHUNK |
|---|---|---|
| 48 | **40 of 60** | exactly `{16,20,24,28,32,36,40,44}` |
| **18** | **0 of 60** | — |

At 48 the first 16 bytes of every message land and the rest is garbage, in a
perfectly periodic pattern. At 18, nothing is wrong at all.

**How the garbage gave it away.** The first bad word of a region-1 write was
`0x4D300032`, whose low bytes `32 00` are the start of our own fifth value
(50 = `0x32`). So the write failed *mid-value at payload byte 18*, not at a
16-byte structure boundary — which is what pointed at `(48-11)/2` rather than at
a buffer of 16.

**RETRACTED, all three from earlier today, all the same root cause.**

- ~~"Exactly 19 of 29 messages land; the tail of a burst is lost."~~
- ~~"The burst loss is a rate problem and batching fixes it — 29 of 29, and
  75 of 75 in a stress run."~~
- ~~"It is a cumulative byte budget, not a message count — forty 21-byte
  messages all landed."~~

**Every one of those came from a bad verification**: I probed a single 4-byte
word near the *start* of each 48-byte message, which falls inside the good 18
bytes whichever way the bug behaves. The forty tiny messages "all landed"
because their payloads were 4 bytes, comfortably under 18. The batching that
"fixed" it fixed nothing — it just never checked the corrupted region.

*Rule, and this is the second time today the same mistake has cost a
conclusion: when verifying a bulk transfer, check the WHOLE region byte for
byte. One probe per chunk, placed at the chunk boundary, is blind to exactly
the failure mode a chunked transfer is most likely to have.*

**Cost: throughput.** 18 bytes per message against 48 means a four-word upload
goes from 30 messages to 79, and a 28-word sentence would be ~510. The obvious
fix is to replace the nibble encoding with the firmware's own 7-to-8 packing,
which would carry `(48-11) x 7/8` = about 32 bytes per message — nearly double
— at the cost of a longer unpacker in the handler. There are 19 spare bytes in
the current reserve and `STUB_RESERVE` can grow.

#### Arbitrary vocabulary over SysEx: works, but the transport loses late messages

`build_sentence` in `mfreak_speech/sysex.py`, which `mfreak speech say` uses, installs a **fully arbitrary** vocabulary — any word
count, any word lengths — by writing the bank's own bookkeeping alongside the
frames. No bitstream, so the encoder is not in the loop.

**The handler is now 112 bytes** (of the 131 reserved) and takes a region flag in
`buf[10]` bit 6, with the count in bits 0..5:

| region | window | destination |
|---|---|---|
| 0 | `0x3800` | `frames_` |
| `0x40` | `0x88` | `bank+0x0C` .. `+0x93` |

The bank window **deliberately stops at `+0x94`**, which is the `frames_`
pointer, so the handler physically cannot corrupt it — verified: after an upload
it still reads `0x20004880`. Writes **fan out to all four voices** by adding
`VOICE_STRIDE` (`0x59E4`), because a chord spreads across voices. `r4` and `r7`
are pushed since the reply path only reloads `r7, r6, r5, r0-r2`, and the count
is re-read from the buffer per voice rather than held in a register, which is
what makes the fan-out fit in the free registers.

**Verified working.**

- **Bank fields land and survive.** A four-word upload sets frame count 99,
  `num_words` 4, boundaries `0, 21, 50, 73, 99` — read back exactly — and they
  survive the whole 29-message frames burst that follows.
- **Words are individually addressable.** With `num_words` 4, Word at 12.5 and
  37.5 play word 0 and word 1: audible (rms 1080.5, 1004.3) and distinct.
- **Region isolation is clean**: region 0 short, region 1 tiny and region 1
  28-byte writes all land at their intended addresses.

**BROKEN: later messages in a long burst are lost.** One cause explains two
symptoms that looked unrelated:

- the bank-field message does **not** land when sent last, after 29 frames
  messages — but lands and persists when sent **first**;
- word 3, the last of four, renders silent, and reading `frames_` at its offset
  and at the end of the blob shows **none of the last four probes match what was
  sent**.

So the tail of a burst does not arrive, at **250 ms spacing**, which is already
7x a message's 35 ms wire time. Rate alone does not explain it.

**RETRACTED hypothesis: it is not the unread replies.** The guess was that our
handler falls into the original opcode's tail so every message replies, and
`mfrender.py` never reads the device's output. Tested through `midilink.py`,
which enumerates USB and streams the device's output to a file so replies do
drain: **the tail was still lost, identically.** Dropped.

**What the midilink runs did establish.**

- **The burst loss is a rate problem and batching fixes it completely.** Sent
  flat at 80 ms, 19 of 29 frames messages landed, with a clean cutoff — the
  first 19 spans correct, the last 10 wrong. Sent in batches of 12 with a 3 s
  pause, **29 of 29 land**; a stress run of 75 messages in three batches of 25
  landed 75 of 75. So the "19" was not a fixed budget, and this is not a
  per-boot cap.
- **It is a byte budget, not a message count.** Forty 21-byte messages (840
  wire bytes) all landed in one flat burst, where 29 109-byte messages did not.
- **Frames uploads are therefore reliable.** Batched, every span lands.

**STILL BROKEN, and it is a separate fault from the burst loss: the region-1
(bank fields) write loses its tail.** With the fields sent first — the shipped
order — a single 28-byte write comes back:

    want  [99, 4, 0, 21,         50,         73,         99]
    got   [99, 4, 0, 21, 1294991410, 4115972564, 1077707860]

The **first four words are correct** (frame count, `num_words`, `bounds[0]`,
`bounds[1]`) and the remaining three are frame-like garbage. So the region
decode, the address and the write loop all work; one 28-byte region-1 write is
losing its last 12 bytes, while 48-byte region-0 writes in the same burst are
byte-perfect.

Three things are ruled out: it is not timing (a 3 s pause before it changes
nothing), not message length (109-byte region-0 messages are fine), and not the
burst loss (the frames immediately around it all land). Sent *last* after the
frames the write does not take at all; sent *first* it takes partially. **The
difference between those two cases is not explained**, and that is the next
thing to find.

**Consequence for the feature.** Arbitrary word *counts* work — `num_words` 4
and frame count 99 land and hold — so a short vocabulary is addressable. But the
boundary table beyond `bounds[1]` is not yet reliable, so a full 31-word
sentence is not deliverable until this is fixed. The frames half is done.

**Workaround in place:** `build_sentence` emits the bank fields **first**, which
is measured to be the better of the two (partial rather than no effect). The transient that creates is benign — briefly `num_words`
describes the new vocabulary while `frames_` still holds the old, and uploads
happen between notes. It is a workaround, not an explanation, and wants settling
before hardware use.

**Throughput, for scale.** A 28-word sentence is 655 frames, 9,170 bytes of
frames plus 124 of fields = **195 messages**; at 250 ms that is 49 s of upload.
`CHUNK` may rise to 63 (the count is six bits), which would cut it by about a
quarter. Finding the real minimum spacing — only 60 ms (fails) and 250 ms
(works) have been tried — matters more.

#### WORKING, end to end in the emulator — 2026-10-05

**A SysEx upload of custom frames changes the word the instrument speaks, and
the audio tracks which word was uploaded.** Clips in `data/speech/sysex/`.

| | note1 rms | note2 rms | note2 sha |
|---|---|---|---|
| no upload | 686.9 | 1348.7 | `5a831fda…` |
| upload "filter" | 686.9 | **2636.7** | `ec24f631…` |
| upload "envelope" | 686.9 | **2982.4** | `bd75ff7b…` |

- upload reaches the audio: **219.6%** and **242.7%** of baseline RMS
- **specificity passes: the two words differ from each other by 75.4%** — this
  is the test that failed every previous attempt, and it is the one that
  distinguishes "our data is being played" from "something changed"
- note 1 is identical across all three, so the upload cannot be credited with a
  difference that was already there

**RETRACTS the whole "emulator gap" conclusion above.** There is no gap. Every
earlier negative came from a bad test vehicle, and the owner was right to keep
pushing back.

**The test vehicle is what mattered.** Factory presets 9, 25, 57, 92 and 12 never
load a word bank, whatever `osc_engine()` says about them. The owner built a
preset from the init patch with nothing changed but the engine (panel 180, read
back with `mfreak preset read 179`, decodes as engine `Speech`, Type 100.0, Word
35.7, and says *frequency* on the instrument). With that preset the bank loads
immediately:

    v0  loaded_bank_ = 4   frame count = 926   num_words = 22   frames_[0] = 0xD8C045A1
    v1-v3  loaded_bank_ = -1                    (a single note uses voice 0 only)

926 frames over 22 words is exactly factory `bank_4`, `frames_[0]` is
**byte-identical to our Python decode of the flash bitstream**, and the
firmware's `word_boundaries_` match `speech_lpc`'s word splitting exactly over
all 12 checked entries. So the decoder, the frame layout, the bank map and
`frames_ = 0x20004880` are all confirmed against the running firmware.

**Two real bugs were in the way, both ours.**

1. **Leading silent frames kill the utterance.** `say` output starts with
   zero-energy frames, and a frame with energy 0 at the *start* of a word plays
   as nothing — the engine does not skip ahead to the audible part. Every factory
   word begins non-zero (`bank_4` word 7 starts at energy 3). `build_upload()`
   now trims leading zero-energy frames.
2. **SysEx volume silences the audio engine.** This is the one that cost the
   most, because it looks exactly like "the write does not work". Sixteen
   110-byte messages at 60 ms spacing silence the next note **wherever they
   write** — proven by writing 728 bytes to `frames_+0x3000`, nowhere near the
   word being played, and getting the same silence. The bisect that found it:

   | shape | where | result |
   |---|---|---|
   | 1 msg x 4 bytes | harmless offset | fine, byte-identical to baseline |
   | 16 msgs x 4 bytes | harmless offset | fine |
   | 1 msg x 48 bytes | harmless offset | fine |
   | **16 msgs x 48 bytes** | **harmless offset** | **silent** |
   | 16 msgs x 48 bytes | the live word | silent |

   So it is neither size nor count nor location on its own, but the sustained
   rate. **At 250 ms spacing the same upload works.** A message is ~35 ms on the
   wire at 31250 baud (`WIRE_MS_PER_MESSAGE`), so 60 ms was not even overlapping
   — the firmware simply cannot absorb that much SysEx while synthesising.

   *Do not read a silent render as a failed write.* Write a few bytes to a
   harmless offset first and check the audio is untouched; that separates the
   transport from everything else in one run.

**Method note worth keeping.** The no-op control is what cracked this: uploading
the factory frames *back over themselves* also produced silence, which proved the
problem could not be the frame content and sent the search to the transport. A
specificity test (two different payloads) and a no-op test (identical payload)
together pin down which half of a pipeline is broken.

#### ANSWERED: the emulator's Speech engine does not read frames_ — 2026-10-05

The forced-state test the owner asked for. **Result: negative, and well
controlled.** Custom vocabulary cannot be auditioned in this emulator by any
route.

**RETRACTED, same day: `voice+0x658` is NOT the engine selector.** I claimed
from it that `osc_engine()` mislabels 28 of 29 Speech presets and that preset 92
is the only real one. **Both are withdrawn.** Sweeping one representative preset
per named engine, the value does not track the engine name at all — `Sample`,
`Waveshaper`, `Wavetable` and `Speech` all read 0; `Harmonic`, `Superwave`,
`Formant`, `Hit Grains`, `Harm` and `Cloud Grains` all read 4 — and **index 5
never appears for any of the 22**. `osc_engine()` reads the *measured*
`ENGINE_VALUES` table, fixed 2026-09-11 after a round-trip assertion, so it is
the better-evidenced of the two and there is no reason to doubt it.

What `voice+0x658` actually is remains unknown. The code at `0x0805F542` does
store a value there and index the 7-pointer array at `+0x6E4` by it, and that
array is confirmed live, so the *mechanism* is real — but the value I read 0.9 s
after a program change is not a stable engine identity. `FIRMWARE.md`'s open
question stands untouched: what the seven are, and how the 22 map onto them, is
still unknown.

*Rule this cost: test the instrument before trusting its readings. One sweep
across all 22 engines would have invalidated the index in a single run, before I
built two conclusions and a whole test design on top of it.*

**The one error that does stand.**
The first forced-state run was void. The pattern read back as `0xBF800000`
(float -1.0) rather than our frames — something overlapping `frames_` in the
shared pool trampled it. A readback caught it. On preset 92 the pattern survives
intact, which is a real difference between the two presets even though the
engine-index explanation for it is withdrawn.

**The voice layout is confirmed live**, which validates the static reading from
earlier. `voice+0x6E4` holds exactly the predicted seven pointers —
`+0x560, +0x604, +0x150, +0x188, +0x000, +0x350, +0x20C` — and `voice+0x724`
reads 7. Voice 0 base is `0x20008EF4` (= bank - 0x4B8), stride `0x59E4`.

**The test.** Preset 92 — chosen for the now-withdrawn index reason, but it is
the one preset where a forced frame table demonstrably survives to render time,
which is what the test needs. After the program change, all four voices had `frames_` filled with 64 synthetic frames,
`loaded_bank_ = 4`, frame count 64, `num_words = 1` (so the Word knob is
irrelevant) and boundaries 0..64. Then a note, then capture. Two patterns chosen
for maximum contrast rather than two spoken words, so a null cannot be blamed on
similarity:

| | A | B |
|---|---|---|
| excitation | voiced, period 60 | **unvoiced**, period 0 |
| k0 / k1 | +25000 / -20000 | **-25000 / +20000** |
| k2..k9 | +60 | **-60** |

**The forced state survived to render time**, verified by readback: `loaded_bank_`
= 4, count = 64, `num_words` = 1, and `frames_[0]` = `0x61A83CC8` for A against
`0x9E5800C8` for B — the two patterns, in place, distinct.

**And the audio was byte-identical.** Same RMS (600.9), same peak (2295), same
SHA. 0.0% difference. Audio *is* produced, so Speech renders — but not from
`frames_`.

**Conclusion: the word-bank path is inert in the emulator downstream of the
frame table.** This matches the anomaly already recorded in `FIRMWARE.md`,
"why `s0` into `speech_formant_generator` is always `0.0`".

**What this means for the feature.**

- **The SysEx patch cannot be validated in the emulator**, and neither can
  `mfreak speech banks`'s flash replacement. Both are now in the same position, and
  both of the results that once looked like emulator proof are accounted for
  otherwise.
- **The remaining validation path is hardware.** The September hardware evidence
  (frames 174..188, the owner hearing *eight*) says the word path does work on
  the instrument, so this is an **emulator fidelity gap**, not a firmware
  misunderstanding.
- **What is still verified for the SysEx patch**, independent of audio: the
  64-byte diff with banks 1-4 and the live objects byte-identical; the handler
  decoding correctly at its final address; the handler writing real RAM with an
  out-of-range write refused; and factory-vs-patched rendering byte-identical
  with no upload.

**Minor correction to chase.** `playback_frame_` is recorded here as
`[r4+0x84]`; reading `speech_engine+0x84` (`0x200092C8`) returns a float
(`0xBE8560C3`), so that offset is relative to some other base. It does not
affect the result above, which used only the bank and `frames_` addresses, both
validated.

#### The emulator renders Speech but never loads a word bank — 2026-10-05

**Speech does make audio in the emulator, and these presets do reach it. What
does not happen is the word-bank decode.** That distinction is narrow and it is
what blocks auditioning custom vocabulary here.

**The measurement, on FACTORY firmware, all four voices, addresses validated:**

| | v0 | v1 | v2 | v3 |
|---|---|---|---|---|
| `word_banks_` | `0x08078838` | `0x08078838` | `0x08078838` | `0x08078838` |
| `num_banks` | 5 | 5 | 5 | 5 |
| `loaded_bank_` | **-1** | **-1** | **-1** | **-1** |

The first two rows are the control: these are live, correctly initialised bank
objects, so the `-1` is a fact about the firmware rather than a stale address.
Tried with presets 9, 57 and 25 (derived banks 4, 1 and 3), three notes each at
1.5 s, on the unpatched image. `loaded_bank_` never leaves `-1`, frame count and
`num_words` stay 0, and `frames_` is never populated with decoded frames.

**Provenance of the earlier renders, since it is the obvious objection.**
`data/speech/catalogue/*.wav` are **8 kHz mono** — `plugins/speech/mfreak_speech/render.py`, the Python
port of the lattice, not the emulator. `data/speech/custom/*.wav` are **96 kHz
stereo**, so those *are* genuine emulator renders and the emulator certainly
produces Speech audio. But audio coming out does not show the word path ran:
with `loaded_bank_` at `-1` the word path renders from an empty frame table, so
the sound has to be arriving by another route (the naive/SAM/phoneme path, or the
formant generator running on whatever is in the buffer). `FIRMWARE.md`
already carries a matching anomaly — why `s0` into `speech_formant_generator` is
always `0.0`.

**Consequence: custom vocabulary cannot currently be auditioned in the emulator
by either path.** Not the flash path (`mfreak speech banks`) and not the SysEx path.
The two emulator results that looked like evidence are both accounted for
otherwise: the 123-128% RMS difference by `mfreak speech banks` zeroing seven live
objects, and the 132% difference in the SysEx test by the cost of handling the
messages. **The September hardware evidence is untouched** — frames 174..188 and
the owner hearing *eight* were the real instrument, which is also why this is an
emulator gap rather than a firmware misunderstanding.

**RETRACTED from earlier today: the claim that Speech's Render never runs.** That
came from `cpu AddHook` on `0x0805B20C`, `0x0805E020` and `0x0805E7E2` firing
zero times, which `METHOD.md` says proves nothing — it names `0x0805B3D2`, in
this same Speech region, among addresses where hooks have silently failed before.
A positive control only shows hooks work somewhere. The findings above are
Monitor **data reads** and stand; the hook-based claim does not.

**The live hypothesis, and it is cheap to test.** The guard is
`cmp r11,num_banks ; blt load`, so a requested bank index of 5 or more never
loads and leaves `loaded_bank_` at `-1` forever — exactly the observed state. The
Type-to-bank boundaries in this file are explicitly **unverified**, so the index
the emulator computes may simply be out of range. Two tests, in order:

1. **Force the state.** Write a distinctive pattern into all four voices'
   `frames_`, set `loaded_bank_`, frame count and `num_words` by hand, then
   render. If the audio changes with the pattern, the word path works and the
   only defect is that the bank never auto-loads.
2. **Read the requested index** rather than deriving it, by watchpointing
   `loaded_bank_` instead of hooking the guard.

#### The requested-index theory is dead; and v8 — 2026-10-08

**Closing the hypothesis above.** It proposed that the emulator's
`loaded_bank_ = -1` comes from a requested bank index of 5 or more, which the
guard `cmp r11,num_banks ; blt load` would refuse forever. Tested by widening
the guard instead of reading the index: on **factory** firmware, writing
`num_banks = 0x40` and then playing a note still leaves `loaded_bank_` at -1,
frame count 0, `num_words` 0, at Wave 50 and Wave 100 alike. The write survives
(it reads back as 64), because factory has no init stub to re-install it.

So the request is **not** an out-of-range index. The engine asks for no bank at
all -- consistent with the `beq` arm of the guard, which skips the load when the
request equals the current `loaded_bank_` of -1:

    0x0805E642  ldr.w r5, [r4, #0x90]   ; the WordBank, through a POINTER, not
    0x0805E646  ldr   r3, [r5, #0x8]    ;   an inline member
    0x0805E648  cmp   r11, r3           ; request == loaded?
    0x0805E64A  beq   +6                ;   yes -> skip the load
    0x0805E64C  ldr   r3, [r5, #0x4]    ; num_banks
    0x0805E64E  cmp   r11, r3
    0x0805E650  blt.w ...               ;   request < num_banks -> load

A hook on `0x0805E646` fires **519 times in a 7 s render**, so the guard is
reached constantly and refused every time. (Hooks were given a positive control
here: `0x0805B18C` fires 4 times in the same run. METHOD.md's warning about
silent hook failures is why.) Note the offsets are confirmed correct by this
disassembly: `+0x04` num_banks, `+0x08` loaded_bank_, `+0x0C` frame count,
`+0x94` frames_.

**What this means for the standing rule.** "Test on the emulator before
flashing" holds for anything expressible as a memory or protocol assertion, and
that is how `test_fanout.py` is built. It does **not** hold for vocabulary
audio, and has not since 2026-10-05. A v8 end-to-end audio run was built and
executed before this was remembered; it reported utterance lengths identical to
its control (0.81/0.81/0.77/0.78 vs 0.81/0.80/0.77/0.78) and that result is
void, not evidence against v8.

**Paraphonic WORKS in the emulator. The claim that it does not was wrong and is
retracted** -- see "Paraphonic works; mfrender silences it" below. The silence
was `mfrender`, not the firmware.

#### Paraphonic works; mfrender silences it -- 2026-10-08

**RETRACTED: "paraphonic voices are silent in the emulator."** They are not. The
measurement was made with `mfrender`, and `mfrender` is what silences them.

Driving the same firmware and the same paraphonic preset through
`midilink.EmulatedSynth`, with audio captured per note, **every note sounds**:

    sequence 60 64 60 64 67 60 67   ->  all seven SPOKE, peak ~0.122 each
    sequence 60 64 67 72            ->  all four SPOKE, in several variants

**The control that makes this worth believing.** All the peaks came out
0.121-0.123, which is exactly what a constant background would look like, so the
captures were checked for pitch content. Zero-crossing rate per second:

| note | zero crossings/s | ratio to note 60 | equal temperament |
|---|---|---|---|
| 60 | 2349.7 | 1.000 | 1.000 |
| 64 | 2896.7 | 1.233 | 1.260 |
| 67 | 3452.2 | 1.469 | 1.498 |
| 72 | 4521.8 | 1.924 | 2.000 |

Monotonic in pitch and close to the right intervals, and no two captures are
byte-identical. The notes really are being rendered.

#### The new technique: audio AND memory in one run

`mfrender` captures audio with two Monitor commands, `sai2 ClearCapture` and
`sai2 SavePCM @path`. Those can be issued from `EmulatedSynth`, so one session
can record each note separately **and** read memory between notes. Everything
note-related should be measured this way. Also worth having:

    sysbus ReadBytes 0x20000000 4096     0.46 s   -> ~15 s for a 128 KB snapshot
    sysbus ReadDoubleWord <addr>         403 ms   -> 3.7 hours for the same

#### The mfrender defect: not yet identified

What is established is the interaction, not the cause:

| harness | paraphonic | mixed pitches |
|---|---|---|
| mfrender | **On** | notes after the first pitch are **SILENT** |
| mfrender | Off | all sound (0.81/0.80/0.77/0.78) |
| EmulatedSynth | **On** | all sound |

Six candidate differences were each tested alone and **none** reproduces it:

1. per-note capture versus one capture for the whole sequence
2. memory reads between notes (which pause and resume the machine)
3. transport: USB MIDI versus DIN/`usart1 WriteChar`
4. zero time between the note-off and the next note-on (mfrender's `--notes`
   emits exactly that shape; tested 2x2 against a 1 s gap, all four sound)
5. all six bytes of an off/on pair delivered in ONE virtual instant with
   `emulation Pause` ... `emulation Start`
6. `timer8 Frequency 100000`, which mfrender sets on every run and the harness
   never did

**Blast radius.** `mfrender --phrase` is `C3 G3 D#4 A#3` -- four different
pitches. Any render of a **paraphonic**
preset rendered through it has only the first pitch audible. Monophonic presets
render all pitches correctly and are not in doubt.

##### v8: the fan-out is removed

`FUN_080359B0`, which installs the seven engine vptrs, has **exactly one
caller**, at `0x08037CC2`, on one ~94 KB object. One Speech engine, one
`LPCSpeechSynthWordBank`. v7's writes at `BANK_BASE + k*VOICE_STRIDE` for
k=1,2,3 therefore went into unrelated memory -- three bad writes per upload
message. v8 writes the single bank only.

`test_fanout.py`, with both controls and every target poisoned with
`0xDEADBEEF` first:

| target | address | v7 | v8 |
|---|---|---|---|
| bank k=0 | `0x200093AC` | WRITTEN | WRITTEN |
| bank k=1 | `0x2000ED90` | WRITTEN | untouched |
| bank k=2 | `0x20014774` | WRITTEN | untouched |
| bank k=3 | `0x2001A158` | WRITTEN | untouched |

POSITIVE: k=0 written under both, so the test can see a write. NEGATIVE: v7
writes k=1..3, so it can see the bug.

**`0x2000ED90` is one of the addresses peek faulted on from hardware**, and v7
was writing there on every upload message. That is the likeliest account of the
v6/v7 upload freezes.

> **Retracted 2026-10-13.** `0x2000ED90` is not a dangerous address and nothing
> faults on reaching it. The peek that "faulted" there was opcode `0x41`, which
> has a real Arturia handler at `0x08047146`; `0x2000ED90`'s middle septet is
> `0x5B`, over the `0x48` that selects an unmapped pointer in the array at
> `0x2001C8C4`. The address was never the variable — see `PROTOCOL.md`,
> "THERE ARE TWO DISPATCHERS". v16 peeks it fine.

The frames-side targets could not be poisoned: `FRAMES_BASE + k*VOICE_STRIDE`
reads back as +-1.0f within milliseconds under every k. The fan-out was one
shared loop, so the bank side settles it.

**Untested, and only hardware can test it:** that paraphonic now speaks our
vocabulary on every voice. The prediction is that it does, because the voices
share the one bank. Full session detail in `captures/emu-2026-10-08/FINDINGS.md`.

#### Implementation state, 2026-10-05: the write works, the audio path does not

The decoded-frames design is implemented in `mfreak speech upload`. **The firmware
patch and the write primitive are verified; the end-to-end audio test fails, and
the reason is located.** Do not read the earlier "verified in the emulator"
claims as covering this.

**Verified in the emulator.**

- **The patch is 64 bytes in three regions** — the 4-byte detour at
  `0x0804AF28`, 2 bytes of `bank_0`'s size field, and 58 bytes of handler at
  `0x08078860`. Banks 1-4, the seven live objects past them, and the phoneme
  table are **byte-identical** to factory.
- **The installed handler decodes correctly at its final address**: detour ->
  `0x08078860`, tail -> `0x0804AF2E`, `movw`/`movt` -> `0x20004880`, bound
  `0x3800`.
- **The handler writes real RAM.** A pattern landed at `frames_+0x100`, a
  control offset never written stayed unchanged, and a deliberately
  out-of-range write was **refused**.
- **Factory and patched firmware render byte-identical audio with no upload.**
  That is the control the 2026-10-04 result lacked, and it means any later
  difference is attributable to the upload rather than the patch.

**FAILED: the frame content does not reach the audio.** Uploading two different
words (29 and 31 frames, different audio) produced **byte-identical** output —
0.0% difference. So the 132% change against the un-uploaded render is a side
effect of *handling* the SysEx, not of the frames. The specificity test is what
caught this; the B-vs-C difference alone looked like success.

**Why, from direct reads rather than inference.**

- **`frames_` IS at `0x20004880`, and the bank layout is confirmed live.**
  Reading voice 0's bank object: `word_banks_` = `0x08078838`, `num_banks` = 5,
  `frames_` = `0x20004880`. The map in this file is right.
- **But `loaded_bank_` is `-1` for every Speech preset tried** (9, 25, 57, 12)
  and **`frames_` is entirely zero** — 0 of 224 probes nonzero after a program
  change and a note. No bank is ever decoded.
- **The Speech word path does not run in the emulator.** The bitstream decoder
  (`0x0805E020`), Speech's Render (`0x0805B20C`) and the phoneme path
  (`0x0805E7E2`) all fired **zero** times while the proven control hook
  (`0x080454D4`) fired. A hook that does not fire proves nothing on its own,
  which is why the control is there.
- **There are FOUR voices, not one.** `WordBank::Init` fires four times:

      bank 0x200093AC  frames_ 0x20004880
      bank 0x2000ED90  frames_ 0x2000A264
      bank 0x20014774  frames_ 0x2000FC48
      bank 0x2001A158  frames_ 0x2001562C      stride 0x59E4

  So a single-voice write is wrong even once the path runs: a note may be
  assigned to any voice. The upload must either cover all four or know which
  voice sounds. `WordBank::Init` also **re-runs on program change** for voice 0,
  which resets `loaded_bank_`.

**This also undercuts the 2026-10-04 result further.** That control was already
confounded by `mfreak speech banks` zeroing seven live objects. If Speech's word path
does not run in the emulator at all, then the 123-128% RMS difference cannot have
been the vocabulary either. Treat "the patch reaches the audio" as **unproven in
the emulator**, by two independent arguments. The September *hardware* evidence
(frames 174..188, the owner hearing "eight") is unaffected — that was the real
instrument.

**The open question is now narrow and answerable:** what makes the firmware take
the word path? Candidates, in order of cheapness: the engine index actually
selected for these presets (the seven-engine sub-dispatch is a different space
from the 22 named engines, and the Type->bank boundaries in this file are
explicitly unverified); whether the emulator reaches Speech's Render at all; and
whether `utterance_start` needs a gate edge the probe did not produce.

#### The memory hunt was mostly unnecessary — write the DECODED frames instead

Owner's reframing, 2026-10-05: if we are replacing the vocabulary anyway, we do
not care about the factory banks, so why not just overwrite the RAM they were
decoded into? **That is right, and it removes the `init_stub` blocker entirely.**

**The 14,336-byte buffer is not a place to put a bitstream. It is where the
bitstream has already been decoded to.** `frames_` holds
`kLPCSpeechSynthMaxFrames (1024) x 14-byte Frame`, for the **currently loaded
bank only**. So the vocabulary already lives in RAM, in a more convenient form
than the format we were planning to ship.

**The bank object, now fully mapped** (`bank = speech_engine+0x168`):

| offset | field | evidence |
|---|---|---|
| `+0x00` | `word_banks_` -> flash descriptor table | `str r1,[r0,#0x0]` in `WordBank::Init` |
| `+0x04` | `num_banks` = 5 | `str r2,[r0,#0x4]`; `cmp r11,r3` range check at `0x0805E64C` |
| `+0x08` | `loaded_bank_` | `cmp r11,r3 ; beq` at `0x0805E646`; set at `0x0805E932` |
| `+0x0C` | total frame count | zeroed at `0x0805E8B8`, read by render at `0x0805E654` |
| `+0x10` | `num_words` | zeroed at `0x0805E8BA`, incremented at `0x0805E91E` |
| `+0x14`..`+0x94` | `word_boundaries_[32]`, uint32 | `add.w lr,r5,r1,lsl #2 ; str.w r0,[lr,#0x14]` at `0x0805E902` |
| `+0x94` | `frames_` | `str.w r4,[r0,#0x94]`; read by render at `0x0805E656` |

**The load is lazy, inside Render, and guarded:**

    0x0805E642  ldr.w r5,[r4,#0x90]   ; the word bank
    0x0805E646  ldr r3,[r5,#0x8]      ; loaded_bank_
    0x0805E648  cmp r11,r3
    0x0805E64A  beq 0x0805E654        ; already loaded -> SKIP, render straight from frames_
    0x0805E64C  ldr r3,[r5,#0x4]      ; num_banks
    0x0805E650  blt.w 0x0805E8A8      ; else decode the bank from flash
    0x0805E654  ldr r0,[r5,#0xc]      ; frame count
    0x0805E656  ldr.w r1,[r5,#0x94]   ; frames_

Once a bank is loaded the guard skips the reload on **every** subsequent render,
so a write into `frames_` persists until the *requested* bank changes.

**`frames_` is at `0x20004880`, and that is a link-time constant.** Speech's
`WordBank::Init` is its first allocation from the freshly reset pool, so
`frames_` == pool base == `audio_obj+0x50` == `0x20004880`. The two other Speech
Init callees (`0x0805EF70`, `0x0805E9E8`) allocate nothing — `0x0805EF70` only
zeroes fields of `this+0xb0`. `.bss` addresses are fixed at link time, and the
allocator always resets to the same base, so this address is stable across boots
and across repeat calls to `FUN_0800D49C`.

**What this design needs, and does not need.**

- **Does not need** a relocated descriptor table, 4 KB of free RAM, backup SRAM,
  a frame-cap reduction, or `init_stub`. **There is no boot-seeding problem**,
  because the factory banks load themselves normally and we overwrite the result
  afterwards. The bug that has blocked this for three sessions stops mattering.
- **Needs** the decoded `Frame` format rather than the bitstream, which is
  *easier*: `{uint8 energy; uint8 period; int16 k0; int16 k1; int8 k2..k9}` = 14
  bytes, confirmed by the phoneme table entries (12 fields, 14 bytes). No
  bitstream encoder needed on the upload path. Size is a wash — the six-word
  test was 894 bitstream bytes against 840 as frames.
- **Still needs** the SysEx RAM-write primitive. That is `vocab_handler`, 58
  bytes, already verified writing real RAM. But only that one, not `init_stub`
  as well, so the flash stub requirement drops from 176 bytes to 58 — and it
  must still move out of `0x0807B380`, which is live data. The vocabulary
  region's unused tail is the candidate.

**Two variants, and the cheap one needs no unknown addresses.**

1. **Frames only.** Overwrite `frames_` at `0x20004880` and keep the factory word
   boundaries, so custom word *K* occupies exactly factory word *K*'s frame span
   (pad short words with silent frames). Needs **one known address and nothing
   else** — no bank-object address, no firmware knowledge of the object layout.
   `bank_4` gives 22 words over 926 frames, ~42 frames a word on average, against
   ~10 frames a word measured, so the spans are generous.
2. **Frames plus boundaries.** Also write `+0x0C`, `+0x10` and `+0x14`, for
   arbitrary word counts and lengths. Needs the bank object's absolute address,
   which is runtime-dependent (`[osc+0x5c]+0x28+0x350+0x168`) and would have to
   be read once in the emulator.

**The trade against the original design, stated honestly.** The descriptor-table
relocation survives bank changes and reboots; this does not — any change of the
requested bank, or an engine switch (slot 1 calls `WordBank::Reset`, which sets
`loaded_bank_ = -1`), reloads from flash and discards the upload. But for the
actual goal — auditioning arbitrary vocabulary before committing — upload-then-play
is the whole workflow, and permanent vocabulary is already covered by
`mfreak speech banks`'s flash replacement. **The relocation design was solving a
persistence problem neither use case has.**

#### Historical: the gap before this was implemented

Replacing banks needs a reflash per change. True runtime loading needs the bank
data in RAM, and three facts shape it:

1. **The descriptor table is relocatable by a 4-byte patch.** `0x08078838` is
   referenced exactly once, from the literal at `0x0805B200`. Repointing that
   literal at RAM makes both pointers *and* sizes writable, and a self-describing
   blob (5 descriptors then the bitstreams) needs nothing else.
2. **QSPI is not an option.** The firmware holds no pointers into a
   memory-mapped QSPI window — 274 word-aligned values land in `0x90000000` and
   155 in `0xA0000000`, both *below* chance for random data, so they are noise.
   External flash is not directly addressable as `const uint8_t*`.
3. **There are 57 free SysEx opcodes.** The dispatcher at `0x0804ACC8` biases
   `buf[7]` by `0x15` over `0x15..0x5D` through a `tbh` table at `0x0804AD20`,
   and only 16 of 73 entries are implemented.

The obstacle is reach: a `tbh` entry is an unsigned halfword scaled by 2, so a
handler must sit within 131,070 bytes of `0x0804AD20`, i.e. below `0x0806AD1E` —
which excludes the slack at the end of flash.

**RETRACTED: the "free" in-range space is not free.** An earlier pass here
proposed a trampoline in the 190-byte zero runs at `0x08061C16` and `0x08061D16`,
on the strength of a filler scan that found them. They carry no literal
references, but the bytes *around* them settle it: `0x08061C00` reads
`6a21 6c84 6ed9 7123 7360 7593 77bb 79da 7bef 7dfb 7fff` — an int16 table
climbing to full scale — and data resumes at `0x08061CD4`. The zeros are the
**zero-crossing region of a lookup table**, and code written there would corrupt
it. The same holds at `0x08061D16`. Treating a run of zeros as free space is the
exact mistake this project keeps paying for; the scan that found it measured
nothing about whether it was used.

**What works instead: `b.w`, not `tbh`.** A wide branch reaches ±16 MB, so an
*implemented* opcode's handler — already in `tbh` range by construction — can have
its first instruction overwritten with `b.w <handler in the end-of-flash slack>`.
That costs 4 bytes at the hijack site, needs no in-range free space at all, and
spends one expendable opcode out of the 16 implemented.

So the honest state: the data path is solved, the hook is located, and the reach
problem has a clean answer. The handler is not written, and what it still needs is
the RAM addresses to write and a decision about which opcode to spend — reading
tasks, not research.
