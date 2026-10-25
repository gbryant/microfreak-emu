"""Two things the modulated filter rests on, checked without emulating.

1. **A file placed at preset N lands in preset N.** `render --preset N=FILE`
   converts N to an index and `build_image` handed that index to mkflash, which
   takes the number on the instrument and converted it AGAIN -- so the file
   landed one slot low and the render played the bank's own preset at N. It was
   silent where that neighbour was empty and wrong-but-plausible where it was
   not, and three different probe files rendered byte-identically before it was
   noticed (2026-10-20). Checked here by building two images that differ only
   in the placed file, and asserting the difference is inside N's page.

2. **The CV parser puts writes on the audio timeline.** `mfrender.cutoff_cv`
   reads the Monitor's hex instruction count at capture start and the
   watchpoint lines after it. Fed synthetic lines, it must hold the pre-capture
   value, switch at the right sample, and pair high and low bytes.

Needs no firmware and no Renode. Part 1 needs a preset export in
data/presets/ to borrow a donor page from, and skips without one.
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import mfemu.mfrender as R                                           # noqa: E402
import mfemu.mkflash as MK                                           # noqa: E402
import mfemu.paths as PATHS                                          # noqa: E402

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-58s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


def test_placement():
    print("\n1. a file placed at preset N lands in preset N")
    if PATHS.PRESET_FILE is None:
        chk(True, "placement", "skipped: no preset export in data/presets/")
        return
    lib = __import__("mfemu.presetlib", fromlist=["library"]).library(PATHS.PRESET_FILE)
    panel_n = 367
    slot = panel_n - 1
    donor = lib.get(slot) or next(iter(lib.values()))
    a = bytearray(donor[1])
    b = bytearray(donor[1])
    # Change one byte well inside the parameter regions, so both still parse.
    b[2000] ^= 0x01
    with tempfile.TemporaryDirectory() as work:
        ia = open(R.build_image({slot: bytes(a)}, PATHS.PRESET_FILE, work, quiet=True), "rb").read()
        ib = open(R.build_image({slot: bytes(b)}, PATHS.PRESET_FILE, work, quiet=True), "rb").read()
    diff = [i for i in range(len(ia)) if ia[i] != ib[i]]
    page = MK.PRESET_BASE + slot * MK.PRESET_SIZE
    inside = diff and all(page <= i < page + MK.PRESET_SIZE for i in diff)
    chk(bool(diff), "the two images differ at all", "%d bytes" % len(diff))
    chk(inside, "and only inside preset %d's page" % panel_n,
        "page 0x%06X; first difference 0x%06X (that is preset %d)"
        % (page, diff[0], (diff[0] - MK.PRESET_BASE) // MK.PRESET_SIZE + 1)
        if diff else "")


def test_cv_parser():
    print("\n2. the cutoff CV is put on the audio timeline")
    t0 = 1_000_000_000
    hz = R.CPU_HZ
    rate = 96000
    at = lambda seconds: t0 + int(seconds * hz)
    lines = [
        "[x] [ERROR] sysbus: MFCV H 116 %d" % (t0 - 5000),       # before capture
        "[x] [ERROR] sysbus: MFCV L 50 %d" % (t0 - 4000),        # -> 29746
        R.CV_T0_MARK,
        "",
        "0x%016X" % t0,
        "[x] [ERROR] sysbus: MFCV H 213 %d" % at(0.010),
        "[x] [ERROR] sysbus: MFCV L 172 %d" % at(0.0101),        # -> 54700 at 10.1 ms
    ]
    cv = R.cutoff_cv(lines, rate // 10, rate)
    edge = int(round(0.0101 * rate))
    chk(cv is not None, "a trace comes back")
    if cv is None:
        return
    chk(cv[0] == 29746 and cv[edge - 1] == 29746,
        "it holds the value written before the capture", "%d" % cv[0])
    chk(cv[edge] == 54700 and cv[-1] == 54700,
        "and switches when the low byte completes the pair", "sample %d" % edge)
    chk(R.cutoff_cv(["nothing here"], 100, rate) is None,
        "no CV written -> None, so the caller uses the stored cutoff")


if __name__ == "__main__":
    test_placement()
    test_cv_parser()
    print("\n%s" % ("PASS - placement and CV timing hold" if not fails
                    else "FAIL - %d check(s): %s" % (len(fails), "; ".join(fails))))
    sys.exit(1 if fails else 0)
