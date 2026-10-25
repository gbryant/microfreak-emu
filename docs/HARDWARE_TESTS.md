# Hardware test log

Things that need a MicroFreak in front of you. Analysis and corpus work can go
a long way, but some claims only resolve against the device.

Each open test says what it settles, how to run it, and what each outcome
means. Keep the "settled" section at the bottom so a fresh session does not
re-run work or re-open closed questions.

> **Numbers in this log.** Every preset, sample and wavetable number below
> is the number the **instrument shows** -- presets 1-512, samples 1-128,
> wavetables 1-16 -- which is what the tools have taken since 2026-10-19.
> Entries written before that date recorded 0-based slot indices, and the
> commands in them were run that way. Where an older entry quotes a
> number, it is one less than the panel number you would type today; the
> *conclusions* are unaffected, since nothing here depends on which slot
> was used. Commands have been updated in place, prose has not.

**Before any write test**, back up the slot — the device's own `Mem protect`
does not apply to SysEx (see PROTOCOL.md):

    mfreak preset read 5 --out /tmp/slot5.mfpz
    mfreak preset write 5 --file /tmp/slot5.mfpz --yes

Scratch slots used so far: preset **5** and **128**, sample **3**, wavetable
**5**. Presets 1-128 are the factory range.

---

## Open — high priority

### 1. `CONTROL`/`SWITCH` write path through TLV addressing

**Why.** `get_field_raw`/`set_field_raw` were moved onto TLV name lookup and
are verified for *reads* (33 fields x 391 presets, plus live reads) and for
in-memory round trips. **No field has been written to hardware through the new
path.** Everything downstream of it — `mfreak preset encode`, preset construction —
depends on this working.

**Run.**

    mfreak preset read 5 --out /tmp/slot5.mfpz
    mfreak preset encode --template-file /tmp/slot5.mfpz \
        --set filter_cutoff=25 --out /tmp/slot5-cutoff25.mfpz
    mfreak preset write 5 --file /tmp/slot5-cutoff25.mfpz --yes

The write goes through the shipping command rather than a hand-assembled
`write_preset` call, which is the point: it exercises what a user runs. The
name and category come from slot 5's own record, so only the cutoff changes.

This recipe used to import `read_preset`, `write_preset` and `preset_fields`
as flat modules and build its header with `patch_name_record`. All four are
gone -- the modules moved into the `microfreak.preset` package, and the
borrowing path was retired on 2026-10-16 because the record is built from
the format now (see FORMATS.md, "Region 0 is built, not borrowed").

Then read **Filter Cutoff** on the device.

**Pass.** Shows ~25. **Fail.** Shows the old value (write landed nowhere) or a
wildly different one (wrong bytes). Either way capture the slot afterwards and
diff against `/tmp/slot5.bin`.

### 2. Mod-matrix write path through TLV addressing

**Why.** Same gap: the unified 24-bit model and TLV addressing are confirmed by
*reading* the device display, but nothing has been written through the new
`set_mod_matrix_raw`.

**Run.** Cells are addressed by name through the TLV stream, so this needs no
special tooling:

    mfreak preset encode --template-preset 5 --set "mod:cyc_env:pitch=2.1" \
        --name ModProbe --out /tmp/mm.mfpz
    mfreak preset write 5 --file /tmp/mm.mfpz --yes

**Pass.** The device shows **2.1** for `pitch <- Cyc Env`. **Fail.** Shows 2.2,
which would mean the per-byte formula was right after all and the correction in
FORMATS.md is wrong.

### 3. Build a preset from scratch

**Why.** Region 1 can be synthesised and round-trips byte-exactly, but a
generated preset has never been sent. This is the actual goal of the project.

**Run.** Build region 1 with `preset_tlv.build()`, append a template's regions
2 and 3 unchanged, encode with `pack_7to8`, write to slot 5.

**Pass.** Device loads it and plays. **Fail.** Note whether it refuses, loads
silently wrong, or corrupts the slot — each points somewhere different.

---

## Open — lower priority

### 4. Sequencer automation track ordering

Step bytes 8-11 are four automation track values and byte 13 is the live-track
mask; both are solid. Which of the four maps to which **UI lane** is assumed,
not checked. Read the scratch slot with `mfreak preset read 5 --out before.bin`,
record one automation lane on the device, save, read it again, and diff the two
payloads to see which of bytes 8-11 moved.

### 5. Does `Mem protect` gate samples and wavetables?

`0x08054DC0` takes a *preset* and checks `slot <= 127`. Whether the same
setting blocks sample/wavetable saves from the front panel is untested, and no
equivalent check has been found on those opcodes. Set `Mem protect` to `All`
and try saving a sample from the device UI.

### 6. Oscillator engine value table -- ANSWERED 2026-09-10, but not by a formula

What is settled: `osc_type` is a **15-bit value**, not a 0..127 raw, and the
device decodes it against its own engine list rather than the saving
firmware's. That is why an old preset shows the wrong engine on newer
firmware.

**Every fitted formula is RETRACTED, including the two this entry used to
give.** `floor(value * 21 / 32768)` and the bin division `value // 1579` are
both wrong above index 16. The decompiled selector (`FUN_0805F458`) says why no
formula can work:

* it adds an offset, `param_2[7]`, that no fit accounted for, and
* it applies a +-0.25 hysteresis **toward the currently selected index**, so
  near a boundary the same stored value decodes differently depending on what
  was selected before.

So there is no pure function of the stored value to find. **Use the measured
lookup table** -- `ENGINE_VALUES` in microfreak-tools'
`microfreak/preset/fields.py`, which holds values the device itself wrote.
FORMATS.md, "Engine selection, from the code", has the decompilation, the
per-entry evidence and which entries came off the panel rather than from a
probe.

Also settled, and previously listed here as unknown: **the 22nd engine is
selectable.** `Vocoder` stores 20852 -- a value the device wrote itself -- and
FIRMWARE.md, "The audio-in consumer is gated on the raw value 20852", shows
`0x5174` is the literal the audio-input path compares against. What is open is
where the additive offset comes from, not whether the engine exists.

Still open: the names that have never come off the panel are interpolated from
the knob sweep's ordering and unconfirmed individually. FORMATS.md is the
place that tracks which.

### 7. The 8 non-standard-length presets

Sizes 2432-7488 rather than 4672. They serialize 8/8 but 2 fail to rebuild, and
both look like truncated captures rather than a real format. Re-read those
slots from the device to see whether they are genuinely odd or just a bad
capture.

### 8. Wavetable name-chunk constants

Decoded `[10:12]` is `0x01 0x01` on every factory wavetable. Unknown whether it
matters. Only worth chasing if a wavetable write ever misbehaves.

### 9. Does the device respond to Program Change?  ANSWERED 2026-09-24

**Yes, across the whole 0-511 range.** `mfreak preset select` does it.

`0xC0 <n>` reaches slots 0-127. Above that, Bank Select MSB (CC#0) supplies
the high bits, `slot = bank * 128 + program`, exactly as FORMATS.md
measured under emulation. Confirmed on the instrument by writing the Init
patch to slot 200 and a Chords preset to slot 72 -- which is what plays if the
bank is ignored -- and capturing:

    CC0 = 0, PC 72   -> slot  72, the Chords preset
    CC0 = 1, PC 72   -> slot 200, the Init patch

Selection is robust: verified with gaps of 0, 2 ms, 20 ms and 150 ms between
the CC and the Program Change, and immediately after a SysEx preset write.
CC#32 changes nothing, matching the emulated measurement.

**The bank latches and persists.** Once bank 1 is set, a later `C0 10` means
slot 138. A stale bank therefore looks exactly like a working selection, which
is why `mfreak preset select` always sends the bank, even for slots under 128.

An earlier version of this entry claimed Bank Select did not work on hardware.
That was wrong, and the cause was measurement rather than the device: the
slot-200 captures were analysed in a fixed time window while the slot-100 ones
used an onset-relative window, so the two landed on different notes of the
phrase and produced pitches that looked like the wrong preset.

### 10. Do the firmware CC assignments drive parameters live?

FORMATS.md extracted the MIDI CC map from the parameter table
(cutoff 23, resonance 83, osc type 9, wave 10, timbre 12, shape 13, cyc env
102/103/28/24, arp rate 91, div 92, spice 2, attack 105, decay 106). If those
change the sound live, a search loop can explore **without writing flash at
all**, which is the basis of the author's (unpublished) preset-search work.
Worth confirming a few by ear before building anything on it, because it
decides the whole architecture.

---

## Settled

| Question | Outcome | How |
|---|---|---|
| Preset write to an empty slot | Works — name-record byte 12 bit `0x08` is an "empty" flag that must be cleared | live write + read-back, exact |
| Sample write to an empty slot | Works — decoded byte 23 of the name chunk is the slot index | live write; slot 3 allocated, others untouched |
| Sample audio correctness | Correct | auditioned by ear |
| Sample name-chunk `[8:10]` | Cosmetic; `00 00` is safe | audition above |
| Wavetable writes | Work, unaffected by the sample bug | clone slot 0 -> 5, verify exact |
| Sequencer notes | MIDI numbers, `0 = C-2`, `0xFB`/`0xFF` empty | user's 12-step listing, 12/12 |
| Sequencer velocity (bytes 4-7) | Velocity; inaudible only because velocity is not routed to amp | MIDI wire capture, 12/12 |
| Sequencer byte 12 | `0` rest, `1` note, `2` tied into next step — a tie rewrites the next step too | differential test |
| Mod-matrix addressing + value model | TLV name lookup and `int24 * 100 / 2**23` both correct | read device display, all cells matched |
| Write protection | `Mem protect` Utility setting; `Factory only` = slots 0-127; SysEx bypasses it | firmware + live write to a protected slot |
