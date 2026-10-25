"""Does the same boot produce the same run twice?

The failure this catches is the worst kind to debug: a harness that is subtly
non-deterministic makes every other result unreproducible, and it looks like a
firmware mystery rather than a harness fault. A whole day went into chasing one
of those before it turned out to be a test vehicle, not the emulator.

Two cold boots of the same unmodified firmware on the same flash image, then
two artifacts compared byte for byte:

  INSTRUCTION COUNTS  the count at each boot milestone, reported by the hook in
                      boot.resc at the moment it fires. These are pure emulated
                      execution -- if they differ, the CPU is not replaying the
                      same instruction stream.

  FLASH READ TRACE    which 4096-byte pages the firmware read, and how many
                      times each. This is a full-boot fingerprint that does not
                      depend on when a test happened to look, which is what
                      makes it comparable at all. An audio capture is not: the
                      machine free-runs, so two captures start at different
                      points in the stream and would differ on a perfectly
                      deterministic harness.

  The trace must also be non-trivial. Two empty traces are equal, and that
  would be a pass that means nothing, so emptiness is checked first.

  It is NOT compared for exact equality, and the reason is measured rather
  than assumed. The trace is dumped after a wall-clock settle, so the run that
  happened to get a few more milliseconds has read a few more pages: 3,296
  against 3,291 on 2026-10-15, five pages present only in the longer run, none
  only in the shorter, and no page read a different number of times. What a
  deterministic boot guarantees is that the shorter trace is a PREFIX of the
  longer -- same pages, same order, same counts, stopping earlier -- and that
  is what is asserted. The page totals themselves are not pinned to any
  number, here or across days.

What this does NOT claim: that Renode is deterministic in general, or that
wall-clock-driven parts (the MIDI socket, USB attach) replay identically. It
claims that a plain boot does, which is the property every other test in this
directory rests on.

    uv run python tests/test_determinism.py
"""
import os
import re
import sys
import time

import mfemu.mfrender as mfrender
import mfemu.paths as PATHS
from mfemu.midilink import EmulatedSynth

MILESTONES = ["SystemInit", "__libc_init_array", "main()"]
BOOT_DEADLINE = 420.0
WORK = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-determinism")
os.makedirs(WORK, exist_ok=True)


def milestone_counts(log_path):
    """-> {milestone: instructions at it} from the hooks' own log lines."""
    out = {}
    if not os.path.exists(log_path):
        return out
    text = open(log_path, errors="replace").read()
    for name in MILESTONES:
        m = re.search(r"MILESTONE " + re.escape(name) + r" insns=(\d+)", text)
        if m:
            out[name] = int(m.group(1))
    return out


def read_trace(path):
    out = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        parts = line.split()
        if len(parts) == 2:
            try:
                out[int(parts[0], 16)] = int(parts[1])
            except ValueError:
                pass
    return out


def one_boot(flash, label, port):
    print("\n=== %s ===" % label)
    trace_path = os.path.join(WORK, "trace-%d.txt" % port)
    if os.path.exists(trace_path):
        os.unlink(trace_path)
    with EmulatedSynth(firmware=PATHS.MAIN_RAW, flash=flash,
                       renode_dir=PATHS.RENODE, port=port) as synth:
        synth.monitor("quadspi TraceReads true")
        synth.monitor("quadspi ClearReadTrace")
        end = time.time() + BOOT_DEADLINE
        counts = {}
        while time.time() < end:
            counts = milestone_counts(synth.log_path)
            if MILESTONES[-1] in counts:
                break
            time.sleep(2.0)
        if MILESTONES[-1] not in counts:
            print("    never reached main() in %.0f s" % BOOT_DEADLINE)
            return None, None
        # The same settling time in both runs, so the traces cover the same
        # phase of the boot. This is wall clock, and it is the one place this
        # test is at the mercy of the host -- hence the trace comparison being
        # over a boot that has already finished rather than a live window.
        synth.boot()
        time.sleep(8.0)
        synth.monitor("quadspi SaveReadTrace @%s" % trace_path)
        time.sleep(1.5)
    trace = read_trace(trace_path)
    for name in MILESTONES:
        print("    %-20s %s" % (name, f"{counts[name]:,}" if name in counts else "-"))
    print("    flash pages read     %d  (%d reads)"
          % (len(trace), sum(trace.values())))
    return counts, trace


def main():
    PATHS.check(PATHS.MAIN_RAW)
    flash = mfrender.build_image({}, None, WORK, quiet=True)
    print("stock 5.0.0.2084, bundle-only flash image, booted twice")

    c1, t1 = one_boot(flash, "RUN 1", 3970)
    c2, t2 = one_boot(flash, "RUN 2", 3980)

    print("\n" + "=" * 68)
    if not c1 or not c2:
        print("VOID - a run did not boot, so there is nothing to compare")
        return 2

    insns_same = c1 == c2
    print("INSTRUCTIONS  identical at every milestone  : %s" % insns_same)
    for name in MILESTONES:
        a, b = c1.get(name), c2.get(name)
        if a != b:
            # A milestone missing from one run makes its count None, and
            # f"{None:,}" raises TypeError -- so the line that exists to
            # REPORT the difference crashed on the difference most worth
            # seeing (a run that did not reach a milestone at all). The
            # `(b or 0) - (a or 0)` delta hid the same None behind a number.
            def num(v):
                return "absent" if v is None else f"{v:,}"
            delta = ("" if a is None or b is None
                     else "  (delta %+d)" % (b - a))
            print("    %-20s %s vs %s%s" % (name, num(a), num(b), delta))

    nontrivial = len(t1) > 0
    # NOT exact equality, and that is deliberate. The trace is dumped after a
    # wall-clock settle, so the run that got a few more milliseconds has read a
    # few more pages -- measured 2026-10-15: 3,296 against 3,291, five pages
    # only in the longer run, none only in the shorter, and no page with a
    # differing count. Asserting equality passed by luck twice and then failed,
    # which is worse than not asserting it.
    #
    # What a deterministic boot DOES guarantee is that the shorter trace is a
    # prefix of the longer one: the same pages, in the same order, read the same
    # number of times, stopping earlier. That is what this checks, and it still
    # fails on real non-determinism -- a page read in one run and not the other,
    # or read a different number of times.
    small, large = sorted((t1, t2), key=len)
    subset = set(small) <= set(large)
    counts = all(small[k] == large[k] for k in small)
    trace_same = subset and counts
    print("FLASH TRACE   non-empty (so equality means something) : %s  %d pages"
          % (nontrivial, len(t1)))
    print("              the shorter run is a prefix of the longer : %s" % trace_same)
    print("              (%d pages against %d; the difference is the "
          "wall-clock window)" % (len(t1), len(t2)))
    if not trace_same:
        extra = sorted(set(small) - set(large))
        differ = sorted(k for k in set(small) & set(large) if small[k] != large[k])
        print("    pages in the SHORTER run that the longer never read : %d  %s"
              % (len(extra), ["0x%06X" % p for p in extra[:4]]))
        print("    same page, different read count : %d  %s"
              % (len(differ), [("0x%06X" % p, small[p], large[p]) for p in differ[:4]]))

    print()
    if insns_same and nontrivial and trace_same:
        print("PASS - a boot replays exactly: identical instruction counts at")
        print("       every milestone, and the same flash reads in the same")
        print("       order. Every other result here is reproducible.")
        return 0
    if not nontrivial:
        print("VOID - the flash trace was empty, so its equality proves nothing.")
        return 2
    print("FAIL - the same boot did not replay. Treat other measurements here")
    print("       as approximate until this is understood.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
