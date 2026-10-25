#!/usr/bin/env python3
"""A warm snapshot must be a shortcut, not an approximation.

`mfemu render` restores a saved machine instead of booting one, which takes
the fixed cost of a render from about 50 s down to about 1 s. That is only
worth having if the result is the same, so this renders one preset BOTH ways
and requires the two files to be byte-identical.

    render --no-warm        boot, wait out the firmware's start-up, play
    render                  restore the warm snapshot, play

Two things make this a real test rather than a ceremony.

**It uses a preset that makes a sound.** Most of the factory bank renders
silent under this harness, and two silent files are identical for reasons that
have nothing to do with snapshots. The first version of this check passed
against 'Lx1' while the warm path was genuinely broken, because 'Lx1' is
silent either way. So the preset is asserted to be audible first, and the
comparison only means something after that.

**It is the check that caught the bug.** Warming on an empty flash looked
perfect on a Speech vehicle and then rendered 'Casually' differently -- 99.4%
of samples changed, 11.7 dB below the signal. That is what sent the external
flash into the snapshot's cache key. A test that only ever ran one agreeable
preset would have shipped it.

Needs the firmware bundle and a bank export; it renders a real preset twice,
so it is the slowest test here -- roughly two minutes, most of it the cold
boot it exists to compare against.
"""
import os
import subprocess
import sys
import wave

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import mfemu.paths as PATHS                                          # noqa: E402

# 'Casually' in the 5.0 bank: picked because it is audible, which is the only
# thing that qualifies a preset for this test -- a silent one cannot tell a
# good snapshot from a broken one, and the check below enforces that.
#
# 6, not 5, and this is the panel change rather than a different preset. The
# number is what the instrument shows, so the audible one that used to be
# `MF_TEST_SLOT=5` (index 5) is `MF_TEST_PRESET=6`. Leaving the 5 in place
# pointed at index 4, which renders silent -- caught by that same check on the
# first run after the flip, which is what it is for.
# Against an MCC-imported `Factory6.0`, preset 6 is `Xoo0Ooo`, a Hit Grains
# preset: SILENT with no samples loaded, audible (peak 13462 on 2026-10-19)
# once `data/samples/` holds the factory sample export. So this test now needs
# samples as well as presets, and without them it fails its own audibility
# control rather than passing wrongly -- which is what that control is for.
# It was recorded here as an unexplained silence before the cause was found.
PRESET = int(os.environ.get("MF_TEST_PRESET", "6"))
# Whatever preset export is in data/presets/, not a hardcoded factory
# filename. This named `factory_5_0_new_presets.mfprojz`, which only exists in
# the research data set -- so on any other machine the test skipped for a file
# it had no way to get. MF_TEST_SOURCE still pins a specific one when a run
# needs to be comparable to an earlier one; it was MF_TEST_BANK until the
# folder took MCC's name.
SOURCE = os.environ.get("MF_TEST_SOURCE") or PATHS.PRESET_FILE

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-46s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)
    return ok


def samples_of(path):
    import struct
    with wave.open(path) as w:
        raw = w.readframes(w.getnframes())
    return struct.unpack("<%dh" % (len(raw) // 2), raw)


def render(out, warm):
    cmd = [sys.executable, "-m", "mfemu.cli", "render",
           "--source", SOURCE, "--preset", str(PRESET),
           "--notes", "60", "--out", out]
    if not warm:
        cmd.append("--no-warm")
    env = dict(os.environ)
    env["PYTHONPATH"] = os.path.join(ROOT, "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=env)
    if not os.path.exists(out):
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-8:])
        raise RuntimeError("render(warm=%s) produced nothing:\n%s" % (warm, tail))
    return out


def main():
    if PATHS.MAIN_RAW is None:
        print("SKIP - no firmware. Drop Arturia's update in %s"
              % PATHS.FIRMWARE_DIR)
        return 0
    if SOURCE is None:
        # Two different reasons, and the fix differs, so say which.
        avail = PATHS.presets_available()
        if avail:
            print("SKIP - %d preset exports in %s, so there is no default:"
                  % (len(avail), PATHS.PRESETS_DIR))
            for b in avail:
                print("         %s" % os.path.basename(b))
            print("       Move all but one into disabled/, or pin one for "
                  "this run:")
            print("         MF_TEST_SOURCE=%s \\" % avail[0])
            print("           uv run python tests/test_warm_snapshot.py")
        else:
            print("SKIP - no presets. Drop an MCC preset export, or one made "
                  "with\n       `mfreak preset backup`, in %s" % PATHS.PRESETS_DIR)
        return 0
    for path in (PATHS.MAIN_RAW, SOURCE):
        if not os.path.exists(path):
            print("SKIP - need %s" % path)
            return 0

    work = os.path.join(PATHS.CACHE, "test-warm")
    os.makedirs(work, exist_ok=True)
    cold = os.path.join(work, "cold.wav")
    hot = os.path.join(work, "warm.wav")

    print("\n1. the cold path: boot the firmware, then play preset %d" % PRESET)
    render(cold, warm=False)
    cs = samples_of(cold)
    peak = max(abs(x) for x in cs)
    # Before anything is compared: this preset must actually sound. Two silent
    # files match no matter how broken the snapshot is.
    if not chk(peak > 200, "the control preset is audible", "peak %d" % peak):
        print("\nFAIL: preset %d renders silent, so this test cannot tell a good\n"
              "      snapshot from a broken one. Pick an audible preset with\n"
              "      MF_TEST_PRESET, or MF_TEST_SOURCE for another preset export.\n"
              "      A Sample or Grains preset is silent without samples: is\n"
              "      there one export in data/samples/?" % PRESET)
        return 1

    print("\n2. the warm path: restore a snapshot instead, and play the same note")
    render(hot, warm=True)
    hs = samples_of(hot)
    chk(len(hs) == len(cs), "the same number of samples came back",
        "%d" % len(hs))

    print("\n3. the two must be the same audio, not merely similar")
    same = open(cold, "rb").read() == open(hot, "rb").read()
    if not same and len(hs) == len(cs):
        diff = [a - b for a, b in zip(cs, hs)]
        bad = sum(1 for d in diff if d)
        chk(False, "byte-identical to the cold render",
            "%d of %d samples differ, max %d"
            % (bad, len(diff), max(abs(d) for d in diff)))
    else:
        chk(same, "byte-identical to the cold render", "%d bytes" % os.path.getsize(hot))

    print()
    if fails:
        print("FAIL: %s" % ", ".join(fails))
        print("      A warm render that differs from a cold one means the")
        print("      snapshot is not the state the firmware would have reached.")
        print("      `mfemu warm --clear` and retry; if it persists, the cache")
        print("      key in mfemu/warm.py is missing something that matters.")
        return 1
    print("PASS - restoring a warm snapshot renders exactly what booting does,")
    print("       for a preset that makes a sound.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
