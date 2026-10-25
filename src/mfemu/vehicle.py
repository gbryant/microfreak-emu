#!/usr/bin/env python3
"""Build a preset page for the emulator out of the instrument's Init patch.

A test that needs the firmware to load a particular engine needs a preset, and
a preset is the one thing a firmware image does not contain -- presets live in
the external flash. So one has to come from somewhere, and the options are
worse than they look:

* **Synthesising one from zeros does not work.** The loader accepts any value
  (it validates framing only), so a zeroed preset loads perfectly -- and makes
  no sound. Measured against a real Init patch: 64 of its 110 parameters are
  NON-zero, among them `VCF/Cutoff` wide open, `EG1/Amount`, `EG2/Sustain` and
  `Gen/Volume`. Zero those and the filter is shut, the envelope has no depth
  and the volume is nothing. It loads; it is silent. That is the worst failure
  mode available, because the preset looks fine.
* **Nor can the page be regenerated from a parameter table.** Region 1 is a
  1,188-byte TLV stream and the rest 0xFF padding, so that part would rebuild
  cleanly -- but region 2 is 2,084 bytes of sequencer and matrix state, of
  which 1,048 are non-zero in a blank patch. A table would have to carry those
  bytes anyway, so it buys nothing over keeping the page.

So the page is a TEMPLATE and the fields that matter are overwritten in it,
which is what `microfreak.preset.encode.build_payload` was designed for:
everything not named by an assignment is carried over, "including the
structural bytes nothing here understands yet". Region 2 is what that phrase
means.

**"Our own writer read back by our own reader" -- and why that is fine here.**
An earlier version of the test preferred a preset authored on the instrument
over one retargeted in software, on the grounds that nothing but our own code
attested the retargeted one was really on the Speech engine. The vehicle built
here is retargeted, so it inherits that objection. The answer is that the
FIRMWARE attests it: switching the engine is only load-bearing if the firmware
then loads a word bank, and the test reads back 926 frames across 22 words. A
vehicle that was not on Speech produces `loaded_bank_ = -1` and zero frames,
which fails loudly. The measurement does not rely on our reader agreeing with
our writer.

`tests/fixtures/init-patch.mfpz` is the template: a preset read off an
instrument with `mfreak preset read`, being what the MicroFreak itself writes
when you press Init and Save. Switching its engine changes **five bytes** --
the same five a player changes by turning the Type knob on a fresh patch.
"""
import os

INIT_PATCH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "tests", "fixtures", "init-patch.mfpz")


def _wire(path):
    import mfemu.mfbank as mfbank
    return mfbank.read_item(path).wire()

PAGE = 4096
REGION0 = 32


def page(slot, engine=None, wave=None, name=None, template=INIT_PATCH):
    """-> a 4096-byte flash page: region 0, then the parameter stream.

    `engine` is an engine name as `preset.fields` spells it ("Speech"); `wave`
    is the Wave knob 0-100, which for Speech selects the word bank -- 100 is
    bank 4. Region 0 is built rather than borrowed, the way the device builds
    it when writing to an empty slot, so the page is self-contained and
    `mkflash --preset N=FILE` takes it whole.
    """
    import microfreak.preset.fields as PF
    import microfreak.preset.tlv as preset_tlv
    from microfreak.preset.write import build_name_record, NR_REGION0

    if not os.path.exists(template):
        raise FileNotFoundError(
            "%s is missing. It is the instrument's Init patch.\n"
            "To make it, press Init and Save to a spare slot, then:\n"
            "    mfreak preset read <that slot> --out %s"
            % (template, template))
    wire = _wire(template)
    rows = PF.rows_from_payload(wire)
    if engine is not None:
        PF.set_osc_engine(rows, engine)
    if wave is not None:
        PF.set_field_raw(rows, PF.CONTROL["osc_wave"],
                         PF.display_to_raw(float(wave), signed=False))
    wire = bytes(PF.payload_from_rows(rows))

    problems = [p for p in preset_tlv.validate_loadable(
        bytes(preset_tlv.decode_7to8(wire)), known=PF.ASSIGN_PARAMS,
        modules=PF.MODULE_TABLE) if p[0] == "fatal"]
    if problems:
        raise ValueError("the built preset would not load: %s" % problems)

    record = build_name_record(slot, name or (engine or "Init")[:14], wire, stored=True)
    out = bytearray(PAGE)
    out[:REGION0] = bytes(record[NR_REGION0:NR_REGION0 + REGION0])
    body = bytes(preset_tlv.decode_7to8(wire))[:PAGE - REGION0]
    out[REGION0:REGION0 + len(body)] = body
    return bytes(out)


def write(path, slot, **kw):
    """page() straight to a file. -> the path."""
    with open(path, "wb") as fh:
        fh.write(page(slot, **kw))
    return path


def main():
    """`python -m mfemu.vehicle` -- describe what the template contains."""
    import microfreak.preset.fields as PF
    import microfreak.preset.tlv as preset_tlv
    wire = _wire(INIT_PATCH)
    dec = bytes(preset_tlv.decode_7to8(wire))
    groups = preset_tlv.groups(dec)
    rows = PF.rows_from_payload(wire)
    nz = sum(1 for _, rs in groups for _, v in rs if int.from_bytes(v, "big"))
    total = sum(len(rs) for _, rs in groups)
    print("%s\n  %d wire bytes, engine %r, wave %.1f"
          % (INIT_PATCH, len(wire), PF.osc_engine(rows),
             PF.raw_to_display(PF.get_field_raw(rows, PF.CONTROL["osc_wave"]))))
    print("  %d modules, %d parameters, %d of them non-zero"
          % (len(groups), total, nz))
    for module, rs in groups:
        print("    %-4s %2d" % (module, len(rs)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
