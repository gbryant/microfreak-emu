"""Is the analog filter model still the one that was measured on hardware?

The MicroFreak's filter sits AFTER the DAC, so it is not in the firmware and
cannot be in the emulator: every clip the Renode render path produces is the
pre-filter signal.  `mfemu.filter_model` is the missing block, and since
2026-10-15 `mfemu render` applies it by default, at the preset's own setting --
so this test guards the last stage of every render, not an optional extra.

Two halves, and the second is the one that rots:

  STRUCTURE    the model's own selftest -- 12 dB/octave slopes, -3 dB at the
               corner in all three modes, a notch at the LP/HP midpoint, and
               stability under a swept cutoff at Q_MAX.  Pure arithmetic, no
               hardware, no firmware, no Renode.

  CALIBRATION  the knob-to-hertz and knob-to-Q curves are FITTED to recordings
               of this instrument (2026-09-11, filter_cal/).  They are not
               derivable from the topology and nothing else in the repository
               would notice if they were edited, so the measured anchors are
               pinned here.  A deliberate recalibration should update these
               numbers in the same commit that changes the constants; a
               surprise here means someone changed a measured value by hand.

The device must be in knob catch = JUMP for any of the calibration to mean
anything -- see filter_cal/README.md, where that cost a whole sweep twice.

    uv run python tests/test_filter_model.py
"""
import sys

import mfemu.filter_model as F

# Measured on the instrument, +-0.31 semitones RMS on cutoff and 8% on Q.
# Hertz first, because a semitone tolerance is multiplicative: 0.31 semitones
# is a factor of 2 ** (0.31 / 12) = 1.018.
#
# RECALIBRATED 2026-10-17. These anchors are the model's own numbers, so they
# move when the calibration legitimately moves -- which is the point: they
# catch an accidental edit, not a deliberate remeasurement. The previous set
# was (0, 17.102), (44, 404.205), (82, 6206.577), (100, 22633.938), from
# constants that read low by up to 2.6 semitones against a direct measurement
# of the corner. See filter_model.py's note and filter_cal/find_peak.py.
#
# 30 and 80 are included because they are the ends of the range the new
# constants were actually fitted over; 0 and 100 pin the extrapolated tails.
CUTOFF_ANCHORS = [(0, 16.489), (30, 152.498), (44, 430.629),
                  (80, 6214.403), (82, 7207.834), (100, 27381.237)]
Q_ANCHORS = [("LPF", 0, 0.6163), ("LPF", 94, 7.5036),
             ("BPF", 0, 0.6976), ("BPF", 94, 10.0939),
             # HPF deliberately borrows the LPF curve: its own fits had too
             # few harmonics to trust (19% RMS agreement, within noise).
             ("HPF", 94, 7.5036)]
TOL = 1e-3                      # these are the model's own numbers, not a fit


def passband():
    """Does the magnitude response match a textbook second-order lowpass?

    The model's own selftest checks the slopes and the -3 dB point AT the
    corner, which leaves the passband itself unchecked -- and that is where
    this model deliberately departs from the textbook: `apply` is AC-coupled,
    with a structural 5 Hz corner, "like the output". So the response is
    second-order from the corner down to about 50 Hz and then rolls off.

    Both halves are pinned here, because both were discovered the hard way.
    An LPF whose DC gain is 0.004 instead of 1.0 looks exactly like a broken
    integrator until you find the AC coupling, and a `--filter` flag that
    announced "DC kept" was written on the assumption it was one.
    """
    import numpy as np
    fs, fc, q = 96000.0, 1000.0, 0.70710678
    rows, ok = [], True
    for f, tol in ((50, 0.01), (100, 0.01), (250, 0.01), (500, 0.01),
                   (1000, 0.01), (2000, 0.02), (4000, 0.03)):
        n = int(max(20 * fs / f, 60000))
        t = np.arange(n) / fs
        y = F.apply(np.sin(2 * np.pi * f * t), "LPF", fc, fs, q, 0.0)
        got = float(np.sqrt(2) * y[n // 2:].std())
        r = f / fc
        want = 1.0 / np.sqrt((1 - r ** 2) ** 2 + (r / q) ** 2)
        good = abs(got - want) <= tol * want
        ok &= good
        rows.append((f, got, want, good))
    print("   %8s %10s %10s" % ("f (Hz)", "measured", "textbook"))
    for f, got, want, good in rows:
        print("   %8.0f %10.4f %10.4f  %s" % (f, got, want, "ok" if good else "OFF"))

    # And the AC coupling, where it is SUPPOSED to depart: a single pole at
    # DC_BLOCK_HZ, so the gain at f is f / sqrt(f^2 + fc_hp^2).
    hp = F.DC_BLOCK_HZ
    for f in (10.0, 1.0):
        n = int(max(40 * fs / f, 400000))
        t = np.arange(n) / fs
        y = F.apply(np.sin(2 * np.pi * f * t), "LPF", fc, fs, q, 0.0)
        got = float(np.sqrt(2) * y[n // 2:].std())
        want = f / (f ** 2 + hp ** 2) ** 0.5
        good = abs(got - want) <= 0.05 * want
        ok &= good
        print("   %8.0f %10.4f %10.4f  %s   (the %g Hz AC coupling)"
              % (f, got, want, "ok" if good else "OFF", hp))
    return ok


def main():
    print("1. structure (the model's selftest)")
    structural = F.selftest()

    # Two dates, because the two curves were measured on different days:
    # this said "measured 2026-09-11" for both, three lines under
    # CUTOFF_ANCHORS' own "RECALIBRATED 2026-10-17". Same wording as
    # filter_model.selftest(), so the two cannot drift apart again.
    print("\n2. calibration anchors "
          "(cutoff measured 2026-10-17, Q 2026-09-11)")
    ok = True
    for knob, want in CUTOFF_ANCHORS:
        got = F.cutoff_hz(knob)
        good = abs(got - want) <= TOL * max(1.0, want)
        ok &= good
        print("   cutoff knob %3d -> %10.3f Hz  (want %10.3f)  %s"
              % (knob, got, want, "ok" if good else "CHANGED"))
    for mode, knob, want in Q_ANCHORS:
        got = F.resonance_q(knob, mode)
        good = abs(got - want) <= TOL * max(1.0, want)
        ok &= good
        print("   Q %s knob %3d -> %8.4f      (want %8.4f)      %s"
              % (mode, knob, got, want, "ok" if good else "CHANGED"))

    print("\n3. the magnitude response, against the textbook")
    shape = passband()

    print("\n   fitted over knob %s, Q resolved to %s, Q_MAX %s"
          % (F.CUTOFF_FITTED_RANGE, F.RESO_RESOLVED_TO, F.Q_MAX))
    if not F.CALIBRATED:
        print("   !! CALIBRATED is False -- the curves are placeholders and "
              "every rendered filter sweep is decoration")
        ok = False

    print()
    if structural == 0 and ok and shape:
        print("PASS - structure holds, the calibration is the measured one, and")
        print("       the response matches a second-order lowpass across the")
        print("       passband and the 5 Hz AC coupling below it")
        return 0
    if not shape:
        print("FAIL - the magnitude response does not match a second-order")
        print("       lowpass. Check svf() and dc_block() before the constants.")
        return 1
    if structural != 0:
        print("FAIL - the model's own selftest failed; the topology is broken, "
              "which makes the calibration moot")
    else:
        print("FAIL - the calibration constants are not the measured ones")
    return 1


if __name__ == "__main__":
    sys.exit(main())
