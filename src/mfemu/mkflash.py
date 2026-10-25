#!/usr/bin/env python3
"""Build an image of the external SPI flash, for booting or rendering.

    mfemu flash -o flash.img
    mfemu flash -o flash.img --preset 5=bass.mfpz
    mfemu flash -o flash.img --presets-from BANK.mfprojz

The factory wavetables come from the firmware file. The layout is in
docs/FORMATS.md.
"""
import argparse
import os
import struct
import sys
import zipfile

import mfemu.image as image_file
import mfemu.paths as PATHS
import mfemu.panel as panel

SIZE = 16 << 20                     # 128 Mbit, per the wavetable address range
WT_MAGIC = 0x00007981
PRESET_BASE, PRESET_SIZE = 129 * 4096, 4096
# The global settings: 96 bytes the firmware reads at start-up into its
# settings block, byte 0 first (0x200007E4 in 5.0.0.2084). Byte 0 is the MIDI
# receive channel, and an erased 0xFF there makes the firmware ignore all
# MIDI. 0x7F is omni. The other settings stay erased.
SETTINGS_BASE = 128 * 4096
MIDI_RX_OMNI = 0x7F
NAME_RECORD_SLICE = slice(12, 44)   # of the 45-byte SysEx `23 52` reply

# The 4096-byte preset page is divided into three regions.  The firmware keeps
# a table of region start offsets at 0x0807531C, read by FUN_08044fe0(obj, n);
# region lengths come from FUN_0804500c, which subtracts adjacent entries
# except for region 2, whose length is the literal 0x824:
#
#     region 0     0 ..   32     32 bytes   name / metadata record
#     region 1    32 .. 2012   1980 bytes   parameter TLV
#     region 2  2012 .. 4096   2084 bytes   sequencer and matrix data
#
# The page buffer is the storage object itself at 0x200018B8 (FUN_08044fdc is
# a bare `bx lr`, i.e. the identity), and FUN_08045238 programs all 0x1000
# bytes of it straight to (slot + 0x81) * 0x1000.
#
# A SysEx parameter transfer starts at FUN_08044fe0(obj, 1) = 32 (caseD_15
# state 2) and runs to the end of the page, so it fills regions 1 and 2 --
# 4064 bytes.  MCC sends 146 chunks of 28 decoded bytes = 4088, because 145
# chunks would be 4 bytes short; the firmware consumes 4064 and drops the
# 24-byte tail.  Hence REGION1_OFF below and the truncation to PAGE - 32.
#
# Region 0 is *not* in that stream -- it arrives as the separate 45-byte
# `23 52` name record, whose bytes [12:44] are region 0 verbatim.  Three
# independent alignments confirm it: the slot-low byte (record 17 -> +5), the
# payload schema nibble (record 20 -> +8), and the NUL-terminated name
# (record 21 -> +9, which is exactly where FUN_08054598 strcpys it from).
# FUN_08054598 also reads the word at +0 and treats a negative value as "slot
# empty" -- which is what an erased 0xFF page gives, so a page without a real
# region 0 is silently ignored rather than loaded.
PRESET_HEADER = 32

# --- samples and wavetables ----------------------------------------------
#
# Measured 2026-09-11 by letting the EMULATED FIRMWARE write them and diffing
# the flash: `midilink.py` boots the machine, `write_sample.py` /
# `write_wavetable.py` send over the emulated MIDI link exactly as they do to
# hardware, and `quadspi SaveImage` reads the result back.  Nothing below is
# inferred from the firmware's code; it is what the firmware produced.
#
# WAVETABLES exist in flash TWICE.  The firmware bundle's
# `userwavetable_factory_v1_3` image lands at 0xF5F000 -- 16 name pages
# followed by 64 audio pages -- and that is a *factory restore* copy: the
# `WaveUser` engine reads it at 0xF6F000 and sounds without any help.  The
# `Wavetable` engine reads the LIVE copy, names at 0xFEF000 and audio at
# 0xFAF000, which is what MCC writes and what an image built only from the
# bundle leaves erased.  That is the whole reason `Wavetable` rendered
# silence while `WaveUser` rendered fine.
#
# There is a THIRD copy, at flash offset 0, and that is the one the
# `Wavetable` engine plays: with a note held on a Wavetable preset the flash
# model logs a read of page 0x000000 and nothing else.
#
# **CORRECTED 2026-09-11, same day.** The first attempt put 16 name pages at
# 0x000000 and the audio at 0x010000, copying the shape of the bank at
# 0xF5F000. It made sound -- RMS 3561 where there had been silence -- and
# that was accepted as success. It was wrong: the engine was reading the
# NAME pages as audio. The give-away was in the render nobody looked at,
# a spectrum with its energy at the 18th harmonic and a centroid of 16 kHz.
#
# The control that settles it: `WaveUser` plays the same 16 tables from
# 0xF6F000 and renders table 0 at RMS 5101.3, centroid 1542 Hz, partials at
# 260/263/523. With the audio at 0x000000 and no name pages in front,
# `Wavetable` renders 5101.4, centroid 1527 Hz, the same partials -- two
# independent engines agreeing on the same table to 0.002% of RMS. That is a
# cross-check; "it makes a noise" was not.
#
# CONFIRMED FROM THE CODE, 2026-09-11: FUN_080456A4 computes
#     addr = (frame + table * 0x20) * 0x200
# and adds 0xFAF000 or 0xF6F000 *only* when its user flag is set, per-table by
# a bit in the ushort at obj+0x1c5a. With the flag clear the base is 0, so the
# factory bank is addressed from offset 0 with nothing in front of it. The
# placement is the firmware's own arithmetic, not our guess.
#
# What WRITES that bank on a real device is still unknown -- MCC's upload only
# ever touches the live copy -- and is presumably factory programming.
WT_COUNT = 16
WT_NAME_FACTORY, WT_NAME_LIVE = 0xF5F000, 0xFEF000
WT_NAME_BOOT = None                 # no name pages in this copy
WT_AUDIO_FACTORY, WT_AUDIO_LIVE, WT_AUDIO_BOOT = 0xF6F000, 0xFAF000, 0x000000
WT_NAME_STRIDE, WT_AUDIO_STRIDE = 0x1000, 0x4000
WT_AUDIO_LEN = 0x4000               # 8192 int16 -- exactly one .mfw payload

# SAMPLES are a chain of variable-length records starting at 0x281000, which
# is where the preset slots end (129 + 512 pages).  Each record is:
#
#     +0x00  u32  the record's own flash address
#     +0x04  u32  allocated size: header + audio, rounded up to a page
#     +0x08  u16  the sample's content hash -- sum(signed bytes) & 0xFFFF
#     +0x0A  13B  name, NUL-padded
#     +0x17  u8   slot index
#     +0x18  ...  audio, 16-bit LE PCM at 32 kHz
#
# and the next record starts one page after this one ends.
#
# **+0x08 must not be left zero.** It is the same 16-bit content hash that
# `Gen/SmpHash` carries, and the granular engines gate on it: FUN_08015c88
# caches the sample's length only inside
#
#     if (*(short *)(engine + 0x13c) != *(short *)(record + 8)) { ... }
#
# so with 0 in the record and 0 in the engine's cache the length at
# `engine + 0x140` stays 0, `fVar33 <= fVar32` takes the no-grain branch, and
# the engine renders silence while running perfectly happily. `Sample` uses a
# different path and does not notice. MCC sends the hash here too -- `a2 66`
# for `Piano Pop`, which is 0x66A2, exactly what the hash function gives.  The name field is
# really 13 bytes even though write_sample.py accepts 18: a 14-character name
# writes its last character over the slot byte (observed, not theorised --
# "ABCDEFGHIJKLMNOPQR" came back as "ABCDEFGHIJKLMN" with no slot index).
SAMPLE_BASE = 0x281000
SAMPLE_HEADER = 0x18
SAMPLE_NAME_OFF, SAMPLE_NAME_LEN = 0x0A, 13
SAMPLE_SLOT_OFF = 0x17
SAMPLE_GAP = 0x1000                 # erased page between records
SAMPLE_LIMIT = 0xF50000             # where the boot scan stops; MCC's memory
                                    # bar draws against the same ceiling


# The two places a wavetable's name appears in its name page, measured on the
# factory bank and on a page the firmware wrote itself. The rest of the page
# is copied from the factory page verbatim -- its remaining bytes are constant
# across all 16 slots and are not decoded.
WT_NAME_OFFSETS = (0x0C, 0x28)
WT_NAME_FIELD = 16


def _patch_wt_name(page, name):
    text = name.encode("ascii", "replace")[:WT_NAME_FIELD]
    for off in WT_NAME_OFFSETS:
        page[off:off + WT_NAME_FIELD] = text.ljust(WT_NAME_FIELD, b"\x00")


def _page_round(n):
    return (n + PRESET_SIZE - 1) // PRESET_SIZE * PRESET_SIZE


def sample_hash(pcm):
    """The device's content hash for a sample: a 16-bit sum of its internal
    signed bytes. Measured against all 30 (hash, index) pairs in the capture;
    see FORMATS.md."""
    total = 0
    for b in pcm:
        total += b - 256 if b > 127 else b
    return total & 0xFFFF


def place_samples(img, items):
    """Write a chain of sample records. `items` are (name, pcm_bytes) in slot
    order; an empty slot is skipped, exactly as MCC's own upload does."""
    addr = SAMPLE_BASE
    placed = 0
    for slot, (name, pcm) in enumerate(items):
        if not pcm:
            continue
        size = _page_round(SAMPLE_HEADER + len(pcm))
        if addr + size > SAMPLE_LIMIT:
            raise ValueError("sample %d (%s) overruns the sample region" % (slot, name))
        rec = bytearray(b"\x00" * SAMPLE_HEADER)
        struct.pack_into("<IIH", rec, 0, addr, size, sample_hash(pcm))
        text = name.encode("ascii", "replace")[:SAMPLE_NAME_LEN]
        rec[SAMPLE_NAME_OFF:SAMPLE_NAME_OFF + len(text)] = text
        rec[SAMPLE_SLOT_OFF] = slot
        # The record is zero-filled to its allocated size, not left erased:
        # the firmware writes whole 4116-byte transport blocks and the last
        # one is zero-padded. Measured against a record the firmware wrote.
        body = bytearray(size)
        body[:SAMPLE_HEADER] = rec
        body[SAMPLE_HEADER:SAMPLE_HEADER + len(pcm)] = pcm
        img[addr:addr + size] = body
        addr += size + SAMPLE_GAP
        placed += 1
    return placed, addr


def place_wavetables(img, tables, live_only=False):
    """Write wavetable audio and names. `tables` are (name_page, pcm) by slot,
    where `name_page` may be None to leave the existing one alone."""
    placed = 0
    for slot, entry in enumerate(tables):
        if slot >= WT_COUNT or entry is None:
            continue
        name_page, pcm = entry
        if len(pcm) != WT_AUDIO_LEN:
            raise ValueError("wavetable %d is %d bytes, expected %d"
                             % (slot, len(pcm), WT_AUDIO_LEN))
        targets = [(WT_NAME_LIVE, WT_AUDIO_LIVE), (WT_NAME_BOOT, WT_AUDIO_BOOT)]
        # WT_NAME_BOOT is None: that copy is audio only.
        if not live_only:
            targets.append((WT_NAME_FACTORY, WT_AUDIO_FACTORY))
        for name_base, audio_base in targets:
            a = audio_base + slot * WT_AUDIO_STRIDE
            img[a:a + WT_AUDIO_LEN] = pcm
            if name_base is not None and name_page is not None:
                n = name_base + slot * WT_NAME_STRIDE
                img[n:n + len(name_page)] = name_page
        placed += 1
    return placed


def load_name_records(jsonl_path):
    """Returns {(hi, lo): 32-byte region 0} from the `23 52` name replies in an
    MCC capture.  Region 0 is bytes [12:44] of the 45-byte SysEx message."""
    import json
    out = {}
    with open(jsonl_path) as fh:
        for line in fh:
            r = json.loads(line)
            if r.get("type") != "sysex":
                continue
            b = bytes(int(x, 16) for x in r["raw"].split())
            if len(b) == 45 and b[7] == 0x23 and b[8] == 0x52:
                out[(b[9], b[10])] = b[NAME_RECORD_SLICE]
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 epilog="\n".join(__doc__.splitlines()[1:]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-o", "--out", default="flash.img",
                    help="output file (default: flash.img)")
    ap.add_argument("--firmware-dir",
                    default=PATHS.FIRMWARE_DIR,
                    help="folder holding the firmware (default: "
                         "data/firmware)")
    ap.add_argument("--preset", action="append", default=[], metavar="N=FILE",
                    help="put an MCC preset file (.mfpz) at preset N "
                         "(1-512). Repeatable")
    ap.add_argument("--preset-schema", action="append", default=[], metavar="N=BYTE",
                    help="override preset N's schema byte. A preset read off "
                         "a 5.0.0.2084 device needs 0x33; a wrong one renders "
                         "as something else")
    ap.add_argument("--presets-from", metavar="FILE",
                    help="fill presets from an MCC preset export (.mfprojz) "
                         "or a decoded capture (.jsonl)")
    # 512, the instrument's slot count, not 128. The default used to drop
    # every slot >= 128 from a full bank and say nothing: the slots were
    # `continue`d past without reaching the skipped counter, and the summary
    # only ever reported "skipped for a missing name record". mfrender has
    # always had to override it to MAX_PRESETS = 512, which was the standing
    # evidence that the default was wrong -- the module's own primary caller
    # did not want it. Dropped slots are now counted and named.
    ap.add_argument("--max-presets", type=int, default=512,
                    help="how many presets to fill from --presets-from "
                         "(default: 512)")
    ap.add_argument("--samples", metavar="FILE.mfsbz",
                    default=PATHS.SAMPLE_FILE,
                    help="an MCC sample export (default: the one in "
                         "data/samples/; \"\" for none)")
    ap.add_argument("--wavetables", metavar="FILE.mfwbz",
                    default=PATHS.WAVETABLE_FILE,
                    help="an MCC wavetable export (default: the one in "
                         "data/wavetables/, else the firmware's factory set)")
    ap.add_argument("--no-live-wavetables", action="store_true",
                    help="leave the live wavetable area erased (the "
                         "Wavetable engine is then silent)")
    ap.add_argument("--size", type=lambda x: int(x, 0), default=SIZE,
                    help="image size in bytes (default: 16 MiB)")
    args = ap.parse_args()

    img = bytearray(b"\xFF" * args.size)
    img[SETTINGS_BASE] = MIDI_RX_OMNI
    placed = ["settings: MIDI receive channel omni at 0x%06X" % SETTINGS_BASE]

    # From a loose `userwavetable*.bin` OR from image 4 of a .mff -- see
    # image.find_wavetable_bank. This scanned only for the loose file, which
    # exists only if the bundle was unpacked by hand, so a drop folder holding
    # just the .mff silently produced an image with no wavetables in it.
    data = image_file.find_wavetable_bank(args.firmware_dir)
    if data:
        magic, dest, payload = struct.unpack("<3I", data[:12])
        if magic != WT_MAGIC:
            sys.exit("%s: the wavetable bank image has magic 0x%X, expected "
                     "0x%X" % (args.firmware_dir, magic, WT_MAGIC))
        body = data[0x40:0x40 + payload]
        img[dest:dest + len(body)] = body
        placed.append("wavetable bank 0x%X bytes at 0x%06X (from the image's "
                      "own header)" % (len(body), dest))

    if not args.no_live_wavetables:
        # The live copy is what the `Wavetable` engine reads. Where a bank
        # export is given its audio wins; otherwise the factory copy is
        # mirrored across, which is exactly what the device does for itself.
        tables = []
        if args.wavetables:
            import mfemu.mfbank as mfbank
            by_slot = {it.slot: it for it in mfbank.read_bank(args.wavetables)}
            for slot in range(WT_COUNT):
                it = by_slot.get(slot + 1)          # bank slots are 1-based
                if it is None or it.is_empty():
                    tables.append(None)
                    continue
                page = bytearray(img[WT_NAME_FACTORY + slot * WT_NAME_STRIDE:
                                     WT_NAME_FACTORY + (slot + 1) * WT_NAME_STRIDE])
                _patch_wt_name(page, it.name)
                tables.append((bytes(page), it.pcm()))
        else:
            for slot in range(WT_COUNT):
                a = WT_AUDIO_FACTORY + slot * WT_AUDIO_STRIDE
                pcm = bytes(img[a:a + WT_AUDIO_LEN])
                if pcm.count(0xFF) == len(pcm):
                    tables.append(None)
                    continue
                n = WT_NAME_FACTORY + slot * WT_NAME_STRIDE
                tables.append((bytes(img[n:n + WT_NAME_STRIDE]), pcm))
        n = place_wavetables(img, tables, live_only=True)
        if n:
            # place_wavetables always writes the BOOT copy too; only the
            # factory copy is conditional. The log said "the live area".
            placed.append("%d wavetables into the live area at "
                          "0x%06X/0x%06X and the boot copy at 0x%06X%s"
                          % (n, WT_AUDIO_LIVE, WT_NAME_LIVE, WT_AUDIO_BOOT,
                             " (from %s)" % os.path.basename(args.wavetables)
                             if args.wavetables else " (mirrored from the factory copy)"))

    if args.samples:
        import mfemu.mfbank as mfbank
        by_slot = {it.slot: it for it in mfbank.read_bank(args.samples)}
        items = []
        for slot in range(max(by_slot) if by_slot else 0):
            it = by_slot.get(slot + 1)              # bank slots are 1-based
            items.append((it.name, it.pcm()) if it and not it.is_empty() else ("", b""))
        n, end = place_samples(img, items)
        placed.append("%d samples chained from 0x%06X to 0x%06X (from %s)"
                      % (n, SAMPLE_BASE, end, os.path.basename(args.samples)))

    if args.presets_from and zipfile.is_zipfile(args.presets_from):
        # A bank export. Regions 1+2 come from the bank; region 0 is BUILT
        # from the preset's name, which is what the device does when writing
        # to an empty slot -- `preset.write.build_name_record` carries region 0
        # at [12:44], the same slice a capture's name record gives.
        import microfreak.preset.tlv as preset_tlv
        import mfemu.mfbank as mfbank
        from microfreak.preset.write import build_name_record, NR_REGION0
        n = capped = 0
        for item in mfbank.read_bank(args.presets_from):
            if item.is_empty():
                continue
            try:
                wire = item.wire()
            except ValueError:
                continue                  # a sample or wavetable, not a preset
            slot = item.slot - 1          # bank slots are 1-based, PC is not
            if not 0 <= slot < args.max_presets:
                capped += 1
                continue
            # build_name_record takes the WIRE form and decodes it itself, to
            # derive the schema's high nibble from whether the payload carries
            # a Voc module. Handing it the already-decoded stream makes it
            # decode garbage, get the schema wrong, and the preset then loads
            # and renders SILENCE -- measured, and exactly the failure its own
            # docstring warns about.
            record = build_name_record(slot, item.name, wire, stored=True)
            region0 = bytes(record[NR_REGION0:NR_REGION0 + PRESET_HEADER])
            body = bytes(preset_tlv.decode_7to8(wire))[:PRESET_SIZE - PRESET_HEADER]
            off = PRESET_BASE + slot * PRESET_SIZE
            img[off:off + PRESET_HEADER] = region0
            img[off + PRESET_HEADER:off + PRESET_HEADER + len(body)] = body
            n += 1
        placed.append("%d presets from the bank %s at 0x%06X onwards, with "
                      "region 0 built from each name%s"
                      % (n, os.path.basename(args.presets_from), PRESET_BASE,
                         ", %d past --max-presets %d" % (capped, args.max_presets)
                         if capped else ""))
    elif args.presets_from:
        # A decoded capture. Regions 1+2 come from the parameter stream,
        # region 0 from the name record for the same slot.  A slot with no name record is skipped:
        # writing regions 1+2 over an erased region 0 makes the firmware read
        # the slot as empty, which is worse than leaving it erased.
        import microfreak.preset.tlv as preset_tlv
        decoded = preset_tlv.load_decoded_presets(args.presets_from)
        names = load_name_records(args.presets_from)
        n = skipped = capped = 0
        for key in sorted(decoded):
            hi, lo = key if isinstance(key, tuple) else (0, key)
            slot = hi * 128 + lo          # the capture keys by bank and index
            if slot >= args.max_presets:
                capped += 1
                continue
            region0 = names.get((hi, lo))
            if region0 is None:
                skipped += 1
                continue
            body = bytes(decoded[key])[:PRESET_SIZE - PRESET_HEADER]
            off = PRESET_BASE + slot * PRESET_SIZE
            img[off:off + PRESET_HEADER] = region0
            img[off + PRESET_HEADER:off + PRESET_HEADER + len(body)] = body
            n += 1
        placed.append("%d presets from %s at 0x%06X onwards (%d skipped for a "
                      "missing name record%s)"
                      % (n, os.path.basename(args.presets_from), PRESET_BASE,
                         skipped,
                         ", %d past --max-presets %d" % (capped, args.max_presets)
                         if capped else ""))

    # N is the number on the instrument in both flags; slots below are indices.
    schemas = {}
    for spec in args.preset_schema:
        k, _, v = spec.partition("=")
        try:
            schemas[panel.to_slot(k)] = int(v, 0) & 0xFF
        except ValueError as exc:
            sys.exit("--preset-schema %s: %s" % (spec, exc))

    for spec in args.preset:
        n, _, path = spec.partition("=")
        try:
            slot = panel.to_slot(n)
        except ValueError as exc:
            sys.exit("--preset %s: %s" % (spec, exc))
        off = PRESET_BASE + slot * PRESET_SIZE
        if path.lower().endswith(".mfpz"):
            # An MCC file carries its own schema byte, so it needs no
            # --preset-schema. `mfemu render` hands this a decoded payload in
            # a temporary file instead, with the schema passed explicitly.
            import mfemu.mfbank as mfbank
            from mfemu.presetlib import read_mfpz
            try:
                _, body = read_mfpz(path)
                schemas.setdefault(slot, mfbank.read_item(path).archive.schema)
            except (OSError, ValueError) as exc:
                sys.exit("--preset %s: not a readable MCC preset file. %s"
                         % (spec, exc))
        else:
            body = open(path, "rb").read()
        if len(body) == PRESET_SIZE:
            img[off:off + PRESET_SIZE] = body          # a whole page, region 0 included
            # --preset-schema still applies. A whole page brings its own
            # region 0, so there is no donor header to mismatch -- but +8 is
            # the payload schema either way, and a page lifted off one vintage
            # for use with another needs the same override the partial path
            # offers. It used to be accepted, parsed and then ignored here.
            want = schemas.get(slot)
            if want is not None:
                img[off + 8] = want
        else:
            body = body[:PRESET_SIZE - PRESET_HEADER]  # regions 1+2 only
            img[off + PRESET_HEADER:off + PRESET_HEADER + len(body)] = body
            # Region 0 is the DONOR's, and its +8 is the payload schema. A
            # payload under a header of the wrong vintage is parsed against the
            # wrong schema: presetlib.py calls it "sounding like an init patch"
            # and FORMATS.md's "Authoring trap" records the stronger
            # form, total silence with the oscillator never called. It cost a
            # day on 2026-10-08 -- every WordBank read came back empty and the
            # emulator looked broken -- so the mismatch is now reported.
            #
            # The high nibble is 3 when the payload carries a Voc module, else
            # 1 (write_preset.py, verified across 396 of 398 captured presets).
            # The low nibble differs by vintage: a 5.0.0.2084 device writes 3,
            # this repo's capture has 0, and **it is not cosmetic** -- measured
            # 2026-10-08, a device payload at 0x30 leaves the word bank
            # unloaded (loaded_bank_ -1, 0 frames) while the same payload at
            # 0x33 loads bank 4 with 880 frames and 21 words. So keeping the
            # donor's low nibble is NOT a fix; the whole byte has to match the
            # payload's vintage.
            #
            # It is still not DERIVED here, and that is a design decision
            # rather than unfinished work. This harness is meant to boot
            # ANY firmware -- 1.0, a custom build, somebody else's -- which
            # is why `mfemu check --firmware` accepts a build that is not the
            # supported one and `mfemu image` only annotates it. The low
            # nibble is that payload's vintage, it cannot be read off the
            # payload, and it is already observed to differ between the two
            # vintages above. Hardcoding the 5.0.0.2084 value would stamp this
            # firmware's vintage onto a payload from another one, silently.
            # Deriving it would also rewrite the byte on every capture render.
            # So it is checked, and `--preset-schema` sets it.
            want = schemas.get(slot)
            if want is None:
                try:
                    import microfreak.preset.tlv as preset_tlv
                    has_voc = any(m == "Voc" for m, _, _ in preset_tlv.fields(bytes(body)))
                    donor = img[off + 8]
                    if (0x30 if has_voc else 0x10) != (donor & 0xF0):
                        # 0x3 low nibble, not the donor's: measured above.
                        print("  WARNING preset %d: payload %s a Voc module but the "
                              "donor header's schema is 0x%02X, so it will not "
                              "render correctly. If it came off a 5.0.0 "
                              "device, pass --preset-schema %d=0x%02X."
                              % (slot + 1, "has" if has_voc else "has no", donor,
                                 slot + 1, (0x30 if has_voc else 0x10) | 0x3))
                except Exception as exc:
                    print("  WARNING preset %d: could not check the payload schema "
                          "(%s)" % (slot + 1, exc))
            else:
                img[off + 8] = want
        placed.append("preset slot %s: %d bytes at 0x%06X%s"
                      % (n, len(body), off,
                         "" if slot not in schemas else
                         "  schema 0x%02X" % schemas[slot]))

    with open(args.out, "wb") as fh:
        fh.write(img)
    print("wrote %s, %d bytes" % (args.out, len(img)))
    for line in placed:
        print("  " + line)
    if not placed:
        print("  (erased image -- no wavetable bank found)")


if __name__ == "__main__":
    main()
