# Where presets come from

Sources of preset data, and how to get it without hand-editing: public banks,
the factory corpus, and the device's own CC/NRPN surface. Nothing here is about
other people's tooling -- that is `RELATED_PROJECTS.md`.

## Public preset banks

Free banks, useful as a **larger and differently-distributed corpus** — most
community banks are *user* presets, whereas our 399 are factory, which is the
axis the format variance runs along:

- [Arturia forum — free community banks](https://forum.arturia.com/t/free-arturia-microfreak-retrowave-19-presets-bank/4351)
- [PresetPatch — MicroFreak](https://www.presetpatch.com/synth/arturia-microfreak)
- [Gab Lavoie — 25 free presets](https://www.gablavoie.com/servicesandwork/free-arturia-microfreak-presets)
- [Ultimate Patches — free pack](https://www.ultimatepatches.com/synth-patches-presets/zone-free/free-microfreak-sounds.html)
- [arturiamicrofreak.com — downloads](https://arturiamicrofreak.com/)

### Likely shortcut: read bank files offline, no hardware needed

Banks ship as `.mfbz` (bank) and `.mfprojz` (project); single presets are
`.mfpz`. We already know `.mfpz`/`.mfsz`/`.mfwz` are **ZIP + Boost.Serialization
text archives** ([SAMPLE_ENCODING.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/SAMPLE_ENCODING.md), [PRESET_FORMAT.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/PRESET_FORMAT.md)). `.mfbz` is very
likely the same container — **untested, but trivial to check** with the magic-byte
test already in this repo.

If it holds, a downloaded bank is hundreds of presets readable **offline** — no
synth, no MCC, no capture. That is the cheap way to grow the corpus.

**Caveat, from our own findings:** a `.mfpz` export turned out to be just the raw
wire bytes, not a labeled model. So banks give *more unlabeled samples* — good
for cracking the conditional schema, useless for learning what a value *means*.

## On reducing hand-editing

Banks don't solve value semantics, because they carry no ground truth. Getting
labeled (parameter, value) → bytes pairs needs a different source.

**Correction:** an earlier version of this file suggested setting parameters in
MCC's UI and capturing the write. That does not work — **MCC cannot edit
patches.** For the MicroFreak it is a librarian and configuration tool (device
settings, preset naming and ordering, sample and wavetable import); Arturia
ships no software editor for the sound parameters. Ignore that idea.

### The real lever: the MicroFreak's own CC/NRPN implementation

The MicroFreak has a documented MIDI CC **and NRPN** implementation covering
oscillator type and wave, filter cutoff and resonance, envelopes, LFO, and
arp/sequencer rates. See
[midi.guide](https://midi.guide/d/arturia/microfreak/), the
[nanassound reference](https://github.com/nanassound/midi_ctrl/blob/main/docs/microfreak_midi_reference.md),
and the official manual ("Using MIDI CC# codes for control").

That matters in both directions:

**Sending** CC/NRPN sets a named parameter to a known value with no hands on the
hardware. NRPN is the more interesting half — 14-bit resolution, which pairs
suggestively with the 15-bit signed value format described in
`RELATED_PROJECTS.md`.

**Receiving**: the manual documents the MicroFreak *transmitting* CC as panel
controls are moved. So even a manual sweep labels itself — turn one knob, and
the synth says which parameter changed and to what value, captured by
`mfreak fw watch`. No note-taking, no reading the display.

### The open question, and how to settle it

Does a preset read reflect **live, unsaved** edits, or only what is stored in
the slot? This has not been tested and it decides everything:

- **If a live edit is readable**, the whole thing automates: for each CC or
  NRPN, sweep the value, read, diff, and derive the byte mapping for every
  parameter with no hand-editing at all.
- **If only stored data is readable**, fall back to the self-labeling manual
  sweep above — still far less bookkeeping than editing blind.

**Settled, 2026-09-03:** ran the test — `mfreak preset read` slot 0 ("NervousKeys"),
sent CC 74 (cutoff) at value 127, read the same slot again. Bytes were
byte-for-byte identical (4672/4672). Repeated broadcasting CC 74=127 across all
16 MIDI channels to rule out a channel mismatch before concluding anything —
still identical. **A preset read reflects only stored (saved) data, not a live
edit buffer.** No distinct edit-buffer address was found in the `03 19 <hi>
<lo>` space either (same request form used for both reads).

This closes off the CC-sweep shortcut: there is no way to derive the value
mapping by sending CC and reading back without an intervening save. The
fallback plan applies — either the self-labeling manual sweep (turn a knob,
`mfreak fw watch` sees the synth transmit which CC/NRPN changed and to what value,
no reading the display), or the original plan of making the change on the
device UI and **saving it into the slot** before reading, which does persist.

Cheapest order overall: (1) check whether one of the projects in
`RELATED_PROJECTS.md` already documents the parameter table, (2) test whether `.mfbz` opens as ZIP, (3) ~~run the
edit-buffer test above~~ done, negative, (4) generate labeled data via a
save-then-read diff (manual UI change + save) or the self-labeling CC/NRPN
sweep.

## Every factory preset Arturia has shipped (2026-10-01)

Exported from a fresh MIDI Control Center install. **This corpus belongs to the
research repository, not here.** `paths.FACTORY_BANKS` and
`paths.factory_presets()` used to enumerate it from this package; they were
removed on 2026-10-19 because nothing in `src/` used them and a released
emulator should not carry constants naming files only one person has. The
table below is kept as a record of what was measured against what.

`data/presets/` is now a drop folder: one export in it becomes the default
`--source`, several and there is no default. It was `data/banks/` until
2026-10-19, when it took MIDI Control Center's name. Any of the files below works, and
so does one made with `mfreak preset backup` off your own instrument.

| bank | slots | presets | distinct |
|---|---|---|---|
| `factory_3_0_preset_bank.mfprojz` | 384 | 320 | 320 |
| `factory_4_0_new_presets.mfprojz` | 384 | 64 | 64 |
| `factory_5_0_new_presets.mfprojz` | 512 | 64 | 64 |
| `factory_6_0_preset_bank.mfprojz` | 512 | 384 | 384 |

Overlap, by sha256 of the wire payload:

- The 4.0 and 5.0 packs are **purely additive** -- zero overlap with 3.0 or with
  each other.
- **6.0 introduced nothing new.** Every one of its 384 payloads already existed
  in 3.0, 4.0 or 5.0. Attributing each preset to the earliest bank that shipped
  it gives 320 from 3.0, 64 from 4.0, 64 from 5.0, and **0 from 6.0**.
- So 6.0 is a *curated subset*: the union is **448 distinct presets** and 6.0
  dropped 64 of them -- 48 from 3.0, 8 from 4.0, 8 from 5.0. Those 64 exist only
  in the older banks.

`factory_6_0_preset_bank.mfprojz` holds the same 384 payloads as
`factory_6_preset_bank.mfprojz`; the files differ only in archive metadata.
The older copy is the one experiment 1's corpus-prior baseline, the mod-matrix
census and the assign-target census were measured against, which is why
`paths.PRESET_BANK` used to be pinned to it. That constant is gone; an
MCC import through `mfreak asset import` gives `Factory6.0.mfprojz`, with the
same 384 payloads.
