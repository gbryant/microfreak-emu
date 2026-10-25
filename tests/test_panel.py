"""Does the virtual front panel save the same Init the instrument does?

Boots Arturia's 5.0.0.2084 on a flash image with every preset slot erased,
selects an empty preset over MIDI, presses Save through `mfemu.frontpanel`,
and reads the preset back over SysEx. The result must be byte-identical to
tests/fixtures/init-patch.mfpz, which a real instrument saved the same way.

    CONTROL   before Save, the slot reads back as empty. Without it, a match
              could come from the slot already holding an Init.

    uv run python tests/test_panel.py        # under a minute
"""
import os
import sys
import time

import mfemu.mfrender as mfrender
import mfemu.mfbank as mfbank
import mfemu.paths as PATHS
import mfemu.vehicle as vehicle
from mfemu.frontpanel import CONTROLS, Panel
from mfemu.midilink import EmulatedSynth
from microfreak.midi import checksum_free_seq
from microfreak.preset.read import read_name_record, read_preset
from microfreak.preset.select import select

WORK = os.path.join(PATHS.CACHE, "test-panel")
SLOT = 299              # preset 300: outside the factory range Mem protect covers
PORT = 3470
fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-52s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


def main():
    if PATHS.MAIN_RAW is None:
        print("SKIP - no firmware. Drop Arturia's update in %s" % PATHS.FIRMWARE_DIR)
        return 0
    if not os.path.exists(vehicle.INIT_PATCH):
        print("SKIP - %s is not present. See NOTICE." % vehicle.INIT_PATCH)
        return 0
    os.makedirs(WORK, exist_ok=True)
    flash = mfrender.build_image({}, None, WORK, quiet=True)
    want = mfbank.read_item(vehicle.INIT_PATCH).wire()
    print(__doc__.splitlines()[0])
    with EmulatedSynth(firmware=PATHS.MAIN_RAW, flash=flash,
                       renode_dir=PATHS.RENODE, port=PORT) as synth:
        synth.boot()
        synth.attach_usb()
        seq = checksum_free_seq()
        before = read_name_record(synth, SLOT, seq)
        chk(bool(before[12] & 0x08), "preset %d starts empty" % (SLOT + 1))
        select(synth, SLOT)
        time.sleep(3)
        panel = Panel(synth)
        shown = panel.press(CONTROLS["Save"])
        chk(any("Preset saved" in t or "Preset %d" % (SLOT + 1) in t for t in shown),
            "the display answers the Save press", " | ".join(shown)[:60])
        record = read_name_record(synth, SLOT, seq)
        name, payload = read_preset(synth, SLOT, seq)
    chk(not record[12] & 0x08, "the slot is no longer empty")
    chk(name == "Init", "it is named Init", repr(name))
    chk(record[20] == 0x33, "with 5.0's schema byte, 0x33", "0x%02X" % record[20])
    chk(payload == want, "byte-identical to the instrument's Init patch",
        "%d bytes differ" % sum(a != b for a, b in zip(payload, want)))
    print("\n%s" % ("FAIL - %d check(s) failed" % len(fails) if fails
                    else "PASS - the virtual panel saves the instrument's own Init"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
