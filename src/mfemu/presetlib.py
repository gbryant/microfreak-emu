#!/usr/bin/env python3
"""Find a preset by name, slot or file, and hand back its parameter bytes.

Three sources, all reduced to the same thing -- the 4088-byte decoded
parameter stream that `mfemu flash --preset N=FILE` writes into a slot:

  * an MCC capture (`captures/mcc-presets.jsonl`), which also carries the
    names, so it is the source that can be listed by name;
  * a `.mfpz`, MCC's own single-preset export.  PRESET_FORMAT.md establishes
    that it is a ZIP holding a Boost text archive whose one array is the raw
    4672 *wire* bytes.  This used to claim the bytes were identical to what
    the capture holds for the same preset, "verified against slot 0"; that is
    **wrong**.  `nervous-keys.mfpz` and the capture's slot 0 are both called
    NervousKeys and differ in 1483 of 4088 decoded bytes, because they are
    different schema versions of it: the capture's is schema 1 with 19
    modules, the export is schema 3 with 20, the extra one being `Voc`.  A
    `.mfpz` and a captured preset of the same name are not interchangeable;
  * a raw file, either 4672 wire bytes, 4088 decoded bytes, or a whole
    4096-byte flash page.

Only regions 1 and 2 -- the parameters -- come from these.  Region 0, the
32-byte header carrying the name, stays whatever the base image already has
at that slot, because the firmware treats a slot whose region 0 is erased as
empty and skips it.  Placing preset A's parameters under preset B's name
therefore renders A and *displays* B; nothing in the audio path reads the
name, and this was confirmed by relocating slot 71 to slot 0 and getting slot
71's sound.

`--source` is required and has no default -- this repository ships no preset
data -- and `--list` lists whatever that source holds, not the capture
specifically:

    python -m mfemu.presetlib --source PRESETS.mfprojz --list
    python -m mfemu.presetlib --source PRESETS.mfprojz --list --grep bass
"""
import argparse
import os
import sys
import mfemu.paths as PATHS

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import microfreak.preset.tlv as preset_tlv                                              # noqa: E402
import mfemu.panel as panel

PAYLOAD_LEN = 4088                 # decoded parameter stream, regions 1+2
WIRE_LEN = 4672                    # the same bytes before 7-to-8 decoding
PAGE_LEN = 4096                    # a whole flash page, region 0 included
NAME_OFFSET = 9                    # NUL-terminated name within region 0


def _name_from_region0(region0):
    raw = bytes(region0)[NAME_OFFSET:]
    return raw.split(b"\x00", 1)[0].decode("latin-1").strip()


def library(source):
    """-> {slot: (name, payload)} from an MCC bank export or a capture.

    A **bank export** (`.mfprojz`) is what anyone with the instrument can
    produce in two clicks, so it is the route that does not depend on this
    project's own recordings. Its slots are 1-based as the panel shows them
    and are converted here to the 0-based number a Program Change carries.

    A **decoded capture** (`.jsonl`) is the older route, kept because the
    recordings that bootstrapped this project are in that form.

    Either way a payload is the 4088-byte decoded parameter stream.
    """
    import zipfile
    if zipfile.is_zipfile(source):
        return library_from_bank(source)
    return library_from_capture(source)


def library_from_bank(path):
    """-> {slot: (name, payload)} from an MCC bank export.

    A bank carries regions 1 and 2 and the preset's NAME, but no region 0 --
    `mkflash` builds that from the name, which is what the device itself does
    when writing to an empty slot.
    """
    import mfemu.mfbank as mfbank
    out = {}
    for item in mfbank.read_bank(path):
        if item.is_empty():
            continue
        try:
            wire = item.wire()
        except ValueError:
            continue                      # a sample or wavetable, not a preset
        out[item.slot - 1] = (item.name, bytes(preset_tlv.decode_7to8(wire)))
    return out


def library_from_capture(jsonl):
    """-> {slot: (name, payload)} for every preset in an MCC capture.

    Slots are the capture's own (bank, index) flattened the way mkflash.py
    flattens them, so a slot number here is the number to send in a Program
    Change.
    """
    import mfemu.mkflash as mkflash                                             # noqa: E402

    decoded = preset_tlv.load_decoded_presets(jsonl)
    names = mkflash.load_name_records(jsonl)
    out = {}
    for key in sorted(decoded):
        hi, lo = key if isinstance(key, tuple) else (0, key)
        region0 = names.get((hi, lo))
        if region0 is None:
            # No name record means mkflash.py will not place it either: the
            # firmware would read the slot as empty.  Skip it here too so the
            # listing matches what is actually renderable.
            continue
        out[hi * 128 + lo] = (_name_from_region0(region0), bytes(decoded[key]))
    return out


def read_mfpz(path):
    """-> (name, payload) from MCC's single-preset export.

    Delegates to mfbank.py, which reads the Boost text archive by the
    length-prefixed string rules.  The old parser here split the archive on
    whitespace, so any name containing a space ("Revert to Iron") was missed
    and the filename was silently used instead.  Fixed 2026-09-11 while
    adding bank-export support.
    """
    import mfemu.mfbank as mfbank                                              # noqa: E402
    item = mfbank.read_item(path)
    payload = preset_tlv.decode_7to8(list(item.wire()))
    return (item.name or os.path.splitext(os.path.basename(path))[0],
            bytes(b & 0xFF for b in payload))


def resolve(spec, lib):
    """spec -> (label, payload, native_slot or None).

    Accepts a PRESET NUMBER as the instrument shows it (1-512), a preset name
    (case-insensitive, exact first then unique prefix), or a path to a `.mfpz`.  `native_slot` is the 0-based slot the preset already occupies
    in the base image, which lets the caller skip rewriting flash when nothing
    has to move.

    The number a caller passes is a panel number; the slot it returns is an
    index. Both callers of this are user-facing command lines -- see
    `mfemu.panel`.
    """
    if spec.lower().endswith(".mfpz"):
        if not os.path.exists(spec):
            raise KeyError("%s: no such file" % spec)
        name, payload = read_mfpz(spec)
        return name, payload, None

    if spec.isdigit():
        slot = panel.to_slot(spec)
        if slot not in lib:
            raise KeyError("preset %s is not in the library" % spec)
        name, payload = lib[slot]
        return name, payload, slot

    want = spec.lower()
    exact = [s for s, (n, _) in lib.items() if n.lower() == want]
    if len(exact) == 1:
        slot = exact[0]
        return lib[slot][0], lib[slot][1], slot
    if len(exact) > 1:
        raise KeyError("%r matches presets %s. Use a preset number."
                       % (spec, ", ".join(str(s + 1) for s in sorted(exact))))
    partial = [s for s, (n, _) in lib.items() if n.lower().startswith(want)]
    if len(partial) == 1:
        slot = partial[0]
        return lib[slot][0], lib[slot][1], slot
    if not partial:
        raise KeyError("no preset named %r (try --list)" % spec)
    raise KeyError("%r is ambiguous: %s"
                   % (spec, ", ".join("%d %s" % (s + 1, lib[s][0])
                                      for s in sorted(partial)[:8])))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--source",
                    required=True,
                    help="an MCC preset export (.mfprojz) or a decoded capture; "
                         "there is no default, because this repository ships "
                         "no preset data")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--grep", help="case-insensitive substring filter")
    args = ap.parse_args()
    lib = library(args.source)
    if args.list or args.grep:
        for slot in sorted(lib):
            name = lib[slot][0]
            if args.grep and args.grep.lower() not in name.lower():
                continue
            print("%4d  %s" % (slot + 1, name))
        return
    print("%d presets in %s" % (len(lib), args.source))


if __name__ == "__main__":
    main()
