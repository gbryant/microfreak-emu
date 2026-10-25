#!/usr/bin/env python3
"""The analog filter the emulator does not have: a TPT state-variable model.

The MicroFreak's filter sits *after* the DAC, so it is not in the firmware and
never will be in the emulator. This is the missing block, applied to the
finished audio: `mfrender` runs every render through it at the preset's own
filter_type and filter_resonance, and at the cutoff the FIRMWARE asks for --
its cutoff CV, knob plus every modulation routed to cutoff, captured per
sample from the emulated PWM (`cv_to_fc`, and `mfrender.cutoff_cv`). `mfemu
render --no-filter` is how to see what the emulator itself produced.

Until 2026-10-20 the cutoff was the stored knob value, held for the whole
render. That is exact for a preset with nothing routed to cutoff and wrong for
everything else: Sausage Bass's cycling envelope keeps the real filter 24
semitones above its knob, and the static model was 14.3 dB off the instrument
across 250 Hz-16 kHz on Ode to Joy, against 1.6 dB with the CV.

Topology (owner-supplied, 2026-09-11): a 12 dB/octave state-variable filter of
the Oberheim SEM kind -- two integrators and an inverter in a feedback loop,
giving LP, BP and HP simultaneously. Discretised with the Topology-Preserving
Transform (Zavalishin), which resolves the delay-free feedback loop implicitly
rather than leaving the one-sample delay a bilinear transform introduces; that
delay is what warps the response at high cutoff and ruins resonance.

Per sample, with g = tan(pi*fc/fs) and R = 1/Q:

    hp = (x - (R + g)*s1 - s2) / (1 + R*g + g*g)
    bp = g*hp + s1
    lp = g*bp + s2
    s1 = g*hp + bp
    s2 = g*bp + lp

The device exposes a three-position switch (`filter_type`: LPF / BPF / HPF),
not the SEM's continuous blend pot, so the three taps are selected rather than
mixed. `morph()` is here anyway because the blend is what the topology
naturally does, and because a notch at the midpoint is a cheap check that the
LP and HP taps are correctly scaled relative to each other.

WHAT WAS NOT KNOWN, and was measured rather than guessed. This section was
headed "WHAT IS NOT KNOWN YET, and must not be guessed" and described three
placeholders and a `--fit` flag, none of which still exist: `CALIBRATED` is
True, the curves were recalibrated on 2026-10-17 by locating the corner
instead of fitting it, and the fitting lives in `filter_cal/` rather than
behind a flag here.

  * **The knob-to-frequency curve.** `filter_cutoff` is 0-100 on the panel;
    which hertz that is, is a property of this instrument and no amount of
    topology supplies it. Measured: see `cutoff_hz` and
    `ACCURACY_SEMITONES`, which is the RMS error over knob 30-80.
  * **The knob-to-Q curve**, likewise measured.
  * **Whether this filter self-oscillates.** The owner's notes say the
    original SEM does not, and cap Q at 10-12. Whether Arturia's
    implementation behaves the same way is a question about *this* device,
    and the calibration sweep at maximum resonance ANSWERED it: it does not
    self-oscillate, and `Q_MAX` is 40.0, measured. (This paragraph said the
    cap was 10-12 and provisional long after that measurement landed.)
  * **How hard the integrators saturate.** `drive` defaults to 0.0 -- the
    linear filter -- deliberately. A tanh in the integrator will always make
    output look more analog; adding one before a measurement says how much is
    how a model starts to flatter itself. This one IS still open.

Everything above the calibration line is structure
and can be trusted; everything below it is a placeholder and is printed as
such by `describe()`.

usage:
    filter_model.py IN.wav OUT.wav --type LPF --cutoff 60 --resonance 20
    filter_model.py --selftest
"""
import argparse
import math
import sys
import wave

import numpy as np

# --- structure: trustworthy ----------------------------------------------

Q_MIN = 0.5                 # below 1/sqrt(2) the response is overdamped
# Q_MAX is defined ONCE, with the resonance measurement it comes from, about
# 200 lines below. It used to be set here to 12.0 and marked PROVISIONAL, and
# then silently rebound to 40.0 in the recalibrated block -- the live value
# everywhere, since the module finishes loading before anything calls svf().
# The 12.0 was dead and the "provisional" note was wrong by two years of work.


def svf(x, fc, fs, q, drive=0.0):
    """-> (lp, bp, hp), the three simultaneous taps, sample by sample.

    `fc` may be a scalar or a per-sample array, which is what makes a swept
    filter possible -- the mod matrix can route an envelope at the cutoff, and
    a static filter would model that wrongly.
    """
    n = len(x)
    fc = np.broadcast_to(np.asarray(fc, dtype=np.float64), (n,))
    q = float(min(max(q, Q_MIN), Q_MAX))
    r = 1.0 / q
    # tan() blows up as fc approaches Nyquist; hold it just below.
    g_all = np.tan(np.pi * np.clip(fc, 1.0, fs * 0.49) / fs)

    lp = np.empty(n); bp = np.empty(n); hp = np.empty(n)
    s1 = 0.0; s2 = 0.0
    for i in range(n):
        g = g_all[i]
        denom = 1.0 + r * g + g * g
        h = (x[i] - (r + g) * s1 - s2) / denom
        b = g * h + s1
        l = g * b + s2
        s1 = g * h + b
        s2 = g * b + l
        if drive:
            # The CA3080 OTAs saturate when driven; the integrator states are
            # where that happens, not the output.
            s1 = math.tanh(s1 * drive) / drive
            s2 = math.tanh(s2 * drive) / drive
        hp[i] = h; bp[i] = b; lp[i] = l
    return lp, bp, hp


DC_BLOCK_HZ = 5.0


def dc_block(x, fs, fc=DC_BLOCK_HZ):
    """Remove DC, the way an AC-coupled output stage does.

    STRUCTURAL, not calibrated. Every analog audio output is AC-coupled and
    this one measurably is: a hardware recording of the device sits at a DC of
    -0.00000 against a peak of 0.1. The corner frequency is NOT measured --
    the calibration used harmonic ratios from 65 Hz up, where a corner down
    here is invisible -- so 5 Hz is a conservative stand-in whose effect on
    anything audible is negligible (-0.06 dB at 65 Hz).

    Without it the model passes through whatever DC the emulator's pre-filter
    signal carries, and a lowpass at a low cutoff then leaves DC as most of
    what remains. Measured consequence: screening 44 generated candidates
    rejected 7 for DC offset through the filter against 2 without it. Five of
    those were the model's doing, not the instrument's.
    """
    k = math.exp(-2.0 * math.pi * fc / fs)
    y = np.empty_like(x)
    prev_x = prev_y = 0.0
    for i in range(len(x)):
        prev_y = k * prev_y + x[i] - prev_x
        prev_x = x[i]
        y[i] = prev_y
    return y


def apply(x, mode, fc, fs, q, drive=0.0, block_dc=True):
    """One of the device's three switch positions, AC-coupled like the output."""
    lp, bp, hp = svf(x, fc, fs, q, drive)
    out = {"LPF": lp, "BPF": bp, "HPF": hp}[mode]
    return dc_block(out, fs) if block_dc else out


def morph(x, mix, fc, fs, q, drive=0.0):
    """The SEM blend: 0 = lowpass, 0.5 = notch, 1 = highpass.

    Not reachable from a MicroFreak preset -- the device switches rather than
    blends -- but a notch at 0.5 is a good check that the taps are scaled
    right relative to each other.
    """
    lp, _, hp = svf(x, fc, fs, q, drive)
    return (1.0 - mix) * lp + mix * hp


# --- calibration: MEASURED 2026-09-11 ------------------------------------
#
# Measured against the instrument. Method: the emulator renders the pre-filter
# signal for a preset; the device plays the same preset into a USB codec; the
# ratio of their harmonic spectra is the filter, with everything fixed in the
# analog path solved for as a term shared by every setting. Only magnitudes
# are used, so no time alignment is needed. `filter_cal/` has the rig.
#
# THE DEVICE MUST BE IN KNOB CATCH = JUMP. In the default "Scaled" mode a CC
# is not an absolute value but a relative nudge, so the same CC gives a
# different cutoff depending on history -- measured: one target approached
# four times gave 1671, 1327, 2046 and 1719 Hz, drifting upward. A whole sweep
# fitted beautifully in that mode and reproduced at 0.65-0.86x twenty minutes
# later. In Jump mode the same setting reproduces to 3.3%.
#
# Cutoff, SUPERSEDED 2026-10-17 -- kept because the reasoning is still how
# the current constants are anchored, and because the numbers it quotes are
# what the recalibration had to disagree with. The SLOPE came from a 13-point
# CC sweep (10 usable, 0.63 semitone residual); the ANCHOR came from presets
# with known stored cutoff fields, because that is the route a generated
# preset takes and it sits about 2.6 semitones below the CC scale at the same
# nominal percentage. Accuracy against the stored-field points was 1.43
# semitones RMS, worst 2.19.
#
# What replaced it: locating the corner from the resonance peak rather than
# fitting it, which disagreed with the above by up to 2.60 semitones in a
# SLOPE-shaped way. See the constants block below.
#
# MODE COVERAGE, because it is easy to assume this and wrong to:
#
#   * CUTOFF is verified in all three modes. Six presets, BPF and HPF at knob
#     60/70/80, fitted with each tap's own magnitude response: they follow the
#     same fc(knob) curve to 1.20 semitones RMS -- tighter than the LPF
#     calibration of the time, whose own figure was 1.43, because the BP and
#     HP taps pass the high
#     harmonics and those fits use 76-118 of them instead of 9-32. The SVF
#     topology predicts exactly this (same integrators, different tap), and it
#     is now measured rather than assumed.
#
#   * RESONANCE is measured per mode, through the stored field, at the
#     bandpass tap as well as the lowpass -- eight points each, 107-139
#     harmonics per fit in BPF against 39-84 in LPF, which is why the
#     bandpass is the right place to measure Q. Damping is linear in the knob
#     in both, reaching zero just past the knob's own maximum (102.4 in LPF,
#     101.0 in BPF), which is the picture of a filter that gets very resonant
#     without tipping over -- and it does not tip over, measured.
#
#     An earlier reading here said Q was out by 13-41% between modes and put
#     +-40% on it. That was mostly an under-conditioned fit: re-measured
#     properly, the LPF curve agrees with the original CC-derived one to
#     about 10% (0.63/0.76/1.00/1.29/1.83/5.34 against 0.61/0.76/1.01/1.34/
#     1.98/5.50). What survives is a real but smaller mode difference: BPF
#     runs about 20% hotter than LPF through the middle of the knob and
#     converges at both ends.
#
#     HPF was measured too, and agrees with the LPF curve to about 19% RMS
#     over knob 0-90, which is within its own noise rather than a confirmed
#     match: the highpass tap discards the low harmonics where a saw keeps
#     its energy, so those fits had 38-77 harmonics against the bandpass's
#     107-139, and the top point would not fit at all. HPF therefore borrows
#     the LPF curve -- now as a checked approximation rather than an
#     untested guess.
#
# LINEARITY, measured rather than assumed. `drive` stays 0.0 because the
# filter showed no saturation to look for. Sustain level is a digital gain
# (hardware level tracked the stored value to within 0.4 dB over 14 dB, and
# `Gen/PrstVol` by contrast does not appear in the emulator's render at all,
# so that one is analog and useless as a drive control). Driving the filter
# over a 14 dB range at BPF resonance 90 -- where its internal level peaks --
# gave Q = 5.69 / 5.87 / 5.78, a 3% spread with no trend, and fc constant at
# 2893 / 2874 / 2893 Hz. Saturation would show as Q falling at the top.
#
# That is a null result over the range this instrument actually produces at
# these settings; a hotter engine or a heavier unison might still find the
# limit. If a tanh is ever added, this is the measurement it has to beat.

CALIBRATED = True
ACCURACY_SEMITONES = 0.31            # cutoff, RMS over knob 30-80 (see below)
Q_ACCURACY = 0.08                    # resonance: 8% RMS within a measured mode

# RECALIBRATED 2026-10-17 by LOCATING the corner instead of fitting it.
#
# Turning the resonance up puts a magnitude peak at the cutoff, so the cutoff
# can be read off the spectrum. Each point is a ratio of two recordings at the
# same cutoff, resonance 90 over resonance 0, which cancels the source, the
# output stage, the codec and the lowpass's own baseline -- so nothing is
# assumed about the filter's order, its Q, or the analog path. +21 dB peak at
# every point. filter_cal/find_peak.py, and its README section.
#
# Measured peaks, corrected to fc by x1.00981: a resonant 2-pole peaks at
# fc*sqrt(1-1/(2Q^2)), and Q at LPF resonance 90 is 5.085 from RESO_R below,
# which is measured (8 points, 5.8% RMS), so the correction is +0.84% to
# +1.16% across Q's own uncertainty -- small and well determined.
#
#   knob      30     40     50     60     70     80
#   fc Hz  148.9  330.9  675.1 1397.0 2932.5 6243.9
#
# The previous constants (0.103701 / 4.0961) were fitted by alternating least
# squares against the emulator's pre-filter render over knob 44-82. Against
# these located corners they read low by +0.14, +1.51, +1.41, +1.56, +1.95
# and +2.60 semitones -- a SLOPE error, not an offset, and outside the 1.43
# semitones they claimed. The ALS method could not have found it: in
# als_presets.py the absolute fc slid from 1.9x to 3.6x purely from widening
# the search grid, because fc, Q and the analog path trade off against each
# other there. A peak frequency does not have that problem.
#
# RMS residual of this fit is 0.31 semitones, max 0.58. Repeatability of the
# peak was checked across two independent recording sessions at knob 60:
# 1383.5 Hz both times, to three figures.
#
# 10.0 octaves, 16.2 Hz to 16.6 kHz. Fitted over knob 30-80; outside that it
# is extrapolation. Below knob 30 is not reachable on this rig at all -- the
# capture chain attenuates the fundamental by 15 dB at 33 Hz, so a corner
# under ~50 Hz has no signal beneath it to measure. See filter_cal/README.md.
CUTOFF_LOG2_PER_PERCENT = 0.106975
CUTOFF_LOG2_AT_ZERO = 4.0434
CUTOFF_FITTED_RANGE = (30.0, 80.0)

# Damping, not Q, is what the knob controls, and it is linear in it: eleven
# points, R = 1/Q falling to zero at 101% of the knob -- just past its own
# maximum, which is why the filter gets very resonant without oscillating.
# Confirmed directly: full resonance, no note, output at the -90 dBFS floor.
# Above knob 94% the resonant peak is narrower than the harmonic spacing that
# measures it, so Q there is a LOWER BOUND.
# Damping per mode: R = 1/Q = slope * knob + intercept.
RESO_R = {
    "LPF": (-0.015845, 1.6227),     # 8 points, 5.8% RMS, R zero at knob 102.4
    "BPF": (-0.014195, 1.4334),     # 8 points, 7.2% RMS, R zero at knob 101.0
    "HPF": (-0.015845, 1.6227),     # NOT MEASURED -- borrows the LPF curve
}
RESO_RESOLVED_TO = 94.0
# The one definition. Measured, not provisional: at full resonance with no
# note the output sits at the -90 dBFS floor, so the filter does not
# self-oscillate, and R = 1/Q reaches zero just past the knob's own maximum.
Q_MAX = 40.0


def cutoff_hz(knob):
    """`filter_cutoff` (0-100) -> Hz. Measured; see ACCURACY_SEMITONES.

    The figure here said +-1.4 semitones, which was the pre-2026-10-17
    accuracy. ACCURACY_SEMITONES is the live number and is 0.31, so the
    docstring was overstating the error by more than 4x.
    """
    return 2.0 ** (CUTOFF_LOG2_PER_PERCENT * max(0.0, min(100.0, knob))
                   + CUTOFF_LOG2_AT_ZERO)


def cutoff_hz_array(knob):
    """`cutoff_hz` for an array of knob positions, e.g. a modulated cutoff."""
    k = np.clip(np.asarray(knob, dtype=np.float64), 0.0, 100.0)
    return 2.0 ** (CUTOFF_LOG2_PER_PERCENT * k + CUTOFF_LOG2_AT_ZERO)


# --- the modulated cutoff --------------------------------------------------
#
# The firmware writes the cutoff -- knob plus every modulation routed to it --
# as a 16-bit PWM value (`mfrender.cutoff_cv`), 65535 = knob 100, refreshed at
# its control rate. On the board an RC network turns that PWM into the
# filter's control voltage, and the RC is what decides how much of a fast
# modulation reaches the filter: a cycling envelope at its fastest toggles
# the value every control tick, and the instrument hears the RC's average of
# that square wave, not the square wave.
#
# CV_SMOOTH_TAU is that RC, as a time constant in seconds. It is applied in
# the knob domain, because the CV is linear in the knob and the exponential
# conversion to hertz happens after it, inside the filter.
# MEASURED 2026-10-20 between two bounds, from two presets on the instrument.
#
# FROM BELOW: Sausage Bass (factory 367) routes a cycling envelope at its
# fastest to cutoff, so the CV is a ~1 kHz square between knob 45.4 and 83.5.
# Rendered through the CV and compared harmonic by harmonic with the
# instrument over C2, C3 and C4, the spread of the per-band residual
# (100 Hz - 12.8 kHz) was
#
#     static knob 45.4 (the model before the CV)   36.3 dB
#     CV, tau 0                                     6.8 dB  (29 of 74
#                                                   harmonics usable: the raw
#                                                   square's sidebands)
#     CV, tau 0.3 ms                                5.7 dB
#     CV, tau 1 / 3 / 10 ms                         4.5 dB each
#
# and 4.5 dB is the method's floor -- the same preset with that routing
# removed spreads the same against the static model. So tau >= ~1 ms.
#
# FROM ABOVE: an LFO at 32.12 Hz (the instrument and the emulator agree to
# the hundredth) swinging the CV between knob 15 and 65 on a SawX C2. The
# modulation depth of the 400-3000 Hz band energy, emulator over instrument:
#
#     tau  0.3   1    2    3    5    8    12   20  ms
#     ratio 0.97 0.96 0.96 0.94 0.91 0.84 0.75 0.61
#
# The instrument is at least as deep as the least-smoothed render, so the RC
# is fast: tau <= ~3 ms. 2 ms is the middle of [1, 3]. A third preset should
# narrow it; nothing else in a render is sensitive to where in that range it
# sits (Buzzsaw's envelope sweep fitted identically from 0.3 to 30 ms).
CV_SMOOTH_TAU = 0.002


def cv_to_fc(cv, fs, tau=None):
    """16-bit cutoff CV per sample -> cutoff in Hz per sample."""
    knob = np.asarray(cv, dtype=np.float64) * (100.0 / 65535.0)
    tau = CV_SMOOTH_TAU if tau is None else tau
    if tau and tau > 0:
        a = math.exp(-1.0 / (tau * fs))
        out = np.empty_like(knob)
        y = knob[0]
        for i in range(len(knob)):
            y = a * y + (1.0 - a) * knob[i]
            out[i] = y
        knob = out
    return cutoff_hz_array(knob)


def resonance_q(knob, mode="LPF"):
    """`filter_resonance` (0-100) -> Q for a given tap. Measured.

    About 8% RMS within LPF and BPF, which were measured; HPF borrows the LPF
    curve and is unverified. Above knob 94 the resonant peak is narrower than
    the harmonic spacing that measured it, so Q there is a lower bound.
    """
    slope, icpt = RESO_R.get(mode, RESO_R["LPF"])
    r = icpt + slope * max(0.0, min(100.0, knob))
    return min(Q_MAX, 1.0 / max(r, 1.0 / Q_MAX))


def describe():
    return ("filter model: TPT state-variable, 12 dB/oct, SEM topology\n"
            "  structure   : trustworthy\n"
            "  calibration : cutoff measured 2026-10-17, Q 2026-09-11, +-%.2f semitones RMS\n"
            "                (device must be in knob catch = JUMP)\n"
            "    cutoff    %.0f Hz at knob 0 -> %.0f Hz at knob 100 "
            "(%.1f octaves), fitted over %.0f-%.0f\n"
            "    resonance  Q %.2f at knob 0 -> %.1f at knob %.0f (LPF; BPF "
            "runs ~20%% hotter mid-knob)\n"
            "    the device does NOT self-oscillate at full resonance "
            "(measured, no note: -90 dBFS)"
            % (ACCURACY_SEMITONES, cutoff_hz(0), cutoff_hz(100),
               CUTOFF_LOG2_PER_PERCENT * 100, CUTOFF_FITTED_RANGE[0],
               CUTOFF_FITTED_RANGE[1], resonance_q(0),
               resonance_q(RESO_RESOLVED_TO), RESO_RESOLVED_TO))


# --- self-test ------------------------------------------------------------

def _slope_db_per_octave(mode, fc, fs, q):
    """Measured roll-off two octaves away from cutoff, via white noise."""
    rng = np.random.default_rng(0)
    x = rng.standard_normal(1 << 17)
    y = apply(x, mode, fc, fs, q)
    f = np.fft.rfftfreq(len(y), 1 / fs)
    sp = np.abs(np.fft.rfft(y))

    def band(centre):
        m = (f > centre / 1.1) & (f < centre * 1.1)
        return 20 * math.log10(sp[m].mean())

    if mode == "LPF":
        return (band(fc * 4) - band(fc * 2)) / 1.0
    return (band(fc / 2) - band(fc / 4)) / 1.0


def selftest():
    fs = 96000.0
    ok = True

    for mode, want in (("LPF", -12.0), ("HPF", 12.0)):
        got = _slope_db_per_octave(mode, 1000.0, fs, 0.707)
        good = abs(got - want) < 2.5
        ok &= good
        print("  %-4s slope %+6.1f dB/oct (want %+.0f)   %s"
              % (mode, got, want, "ok" if good else "FAIL"))

    # A Butterworth-Q lowpass is 3 dB down at its cutoff.
    for fc in (200.0, 1000.0, 8000.0):
        n = 1 << 16
        t = np.arange(n) / fs
        ref = np.sin(2 * np.pi * fc * t)
        y = apply(ref, "LPF", fc, fs, 0.7071)
        half = slice(n // 2, None)          # let the transient pass
        db = 20 * math.log10(np.sqrt((y[half] ** 2).mean()) /
                             np.sqrt((ref[half] ** 2).mean()))
        good = abs(db + 3.0) < 0.8
        ok &= good
        print("  LPF at fc=%-6.0f %+.2f dB (want -3.00)      %s"
              % (fc, db, "ok" if good else "FAIL"))

    # The bandpass must peak at the cutoff, not beside it.
    n = 1 << 16
    rng = np.random.default_rng(1)
    y = apply(rng.standard_normal(n), "BPF", 1000.0, fs, 4.0)
    f = np.fft.rfftfreq(n, 1 / fs)
    peak = f[int(np.argmax(np.abs(np.fft.rfft(y))[1:]) + 1)]
    good = abs(peak - 1000.0) < 60.0
    ok &= good
    print("  BPF peak %.0f Hz (want 1000)              %s"
          % (peak, "ok" if good else "FAIL"))

    # LP + HP at the blend midpoint must notch, i.e. lose the cutoff tone.
    t = np.arange(n) / fs
    tone = np.sin(2 * np.pi * 1000.0 * t)
    notched = morph(tone, 0.5, 1000.0, fs, 0.7071)
    depth = 20 * math.log10(np.sqrt((notched[n // 2:] ** 2).mean()) /
                            np.sqrt((tone[n // 2:] ** 2).mean()))
    good = depth < -20.0
    ok &= good
    print("  notch at blend 0.5: %.1f dB               %s"
          % (depth, "ok" if good else "FAIL"))

    # A swept cutoff must stay finite and bounded.
    sweep = np.linspace(50.0, 18000.0, n)
    y = apply(np.sin(2 * np.pi * 300.0 * t), "LPF", sweep, fs, Q_MAX)
    good = np.all(np.isfinite(y)) and np.abs(y).max() < 50.0
    ok &= good
    print("  swept cutoff at Q=%.0f stays bounded      %s"
          % (Q_MAX, "ok" if good else "FAIL"))

    print("\n%s" % describe())
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("infile", nargs="?")
    ap.add_argument("outfile", nargs="?")
    ap.add_argument("--type", default="LPF", choices=("LPF", "BPF", "HPF"))
    ap.add_argument("--cutoff", type=float, default=100.0, help="knob, 0-100")
    ap.add_argument("--resonance", type=float, default=0.0, help="knob, 0-100")
    ap.add_argument("--drive", type=float, default=0.0,
                    help="integrator saturation; 0 = linear (default)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if not args.infile or not args.outfile:
        ap.error("give INFILE and OUTFILE, or --selftest")
    if not CALIBRATED:
        print("warning: %s" % describe().splitlines()[-1].strip(), file=sys.stderr)

    with wave.open(args.infile, "rb") as w:
        fs = w.getframerate()
        ch = w.getnchannels()
        data = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    data = data.reshape(-1, ch).astype(np.float64) / 32768.0

    fc = cutoff_hz(args.cutoff)
    q = resonance_q(args.resonance)
    out = np.stack([apply(data[:, c], args.type, fc, fs, q, args.drive)
                    for c in range(ch)], axis=1)
    peak = np.abs(out).max()
    if peak > 1.0:
        print("note: resonance gain, scaling by %.2f to avoid clipping" % (1 / peak),
              file=sys.stderr)
        out /= peak

    with wave.open(args.outfile, "wb") as w:
        w.setnchannels(ch); w.setsampwidth(2); w.setframerate(fs)
        w.writeframes((np.clip(out, -1, 1) * 32767).astype("<i2").tobytes())
    print("%s -> %s  (%s, fc=%.0f Hz, Q=%.2f)"
          % (args.infile, args.outfile, args.type, fc, q))
    return 0


if __name__ == "__main__":
    sys.exit(main())
