"""Locate the cutoff directly, from where resonance puts its peak.

    uv run --extra hardware python find_peak.py [WORK_DIR]

A resonant lowpass has a magnitude peak at (near) its cutoff. So the cutoff
can be LOCATED instead of fitted, which matters because fitting could not
determine it: in `als_presets.py` the absolute fc slid from 1.9x the model to
3.6x purely from widening the search grid, and the shared Q settled on
whatever the lowest offered value was. Those are the symptoms of a parameter
the data does not constrain. A peak frequency is not that kind of quantity --
it is read off the spectrum.

The measurement is a RATIO of two recordings at the same cutoff, one with
resonance high and one with resonance 0:

    H(f) = P_resonant(f) / P_flat(f)

Both share the source, the analog output stage, the codec and the lowpass's
own baseline rolloff, so all of it cancels and what remains is the resonant
boost alone. Its peak is the cutoff. Nothing is assumed about the filter's
order, its Q, or the shape of the analog path -- the three things the fit had
to solve for simultaneously and could not separate.

The source is the **Noise** engine. It is not white (-3.2 dB/octave measured,
see README) and that does not matter here: a smooth tilt is common to both
recordings and divides out. What matters is that noise is CONTINUOUS, so the
peak can be located at any frequency. A harmonic source samples the response
only at multiples of f0, which quantises the answer to +-f0/2 -- 33 Hz at C2.

Caveat worth keeping: for a resonant second-order lowpass the magnitude peak
sits slightly BELOW fc, at fc*sqrt(1 - 1/(2Q^2)). At Q = 10 that is 0.25%
low, at Q = 2 it is 3% low. Both are far smaller than the discrepancy being
tested, so the peak is reported as-is and the correction is noted rather than
applied -- applying it would need the Q this measurement is designed not to
assume.
"""
import glob
import os
import re
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from capture import mono

HERE = os.path.dirname(os.path.abspath(__file__))


def spectrum(path):
    """Power spectrum of the steady part of the LAST note in a recording."""
    with wave.open(path) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    s = mono(a.reshape(-1, ch).astype(float) / 32768.0)
    win = int(0.05 * rate)
    env = np.array([np.sqrt((s[i:i + win] ** 2).mean())
                    for i in range(0, len(s) - win, win)])
    if env.max() < 1e-6:
        return None, None
    loud = np.where(env > env.max() * 0.35)[0]
    gaps = np.where(np.diff(loud) > 4)[0]
    on = int(loud[gaps[-1] + 1]) if len(gaps) else int(loud[0])
    a0 = int((on + 3) * win)
    seg = s[a0:min(len(s), a0 + rate)]
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) ** 2
    return sp, np.fft.rfftfreq(len(seg), 1.0 / rate)


def log_bands(f, lo=40.0, hi=12000.0, n=60):
    edges = np.geomspace(lo, hi, n + 1)
    centres, masks = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (f >= a) & (f < b)
        if m.sum() >= 2:
            centres.append(float(np.sqrt(a * b)))
            masks.append(m)
    return np.array(centres), masks


def main():
    work = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mf-reso"
    pairs = {}
    for path in glob.glob(os.path.join(work, "hw-*-r*.wav")):
        m = re.search(r"hw-(\d+)-r(\d+)\.wav", path)
        if m:
            pairs.setdefault(int(m.group(1)), {})[int(m.group(2))] = path
    knobs = sorted(k for k, v in pairs.items() if 0 in v and len(v) > 1)
    if not knobs:
        raise SystemExit(
            "no resonance pairs in %s.\nRecord both arms at the same cutoff:\n"
            "  SWEEP_ENGINE=Noise SWEEP_RESO=0  SWEEP_WORK=%s "
            "python sweep_presets.py 40 50 60 70 80\n"
            "  SWEEP_ENGINE=Noise SWEEP_RESO=90 SWEEP_WORK=%s "
            "python sweep_presets.py 40 50 60 70 80" % (work, work, work))

    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
    import mfemu.filter_model as FM
    lo_band, hi_band = FM.CUTOFF_FITTED_RANGE

    print("peak of (resonant / flat), which is the cutoff itself\n")
    print("knob   reso   peak Hz   model fc   peak/model   boost dB   band")
    rows = []
    for knob in knobs:
        flat, f = spectrum(pairs[knob][0])
        if flat is None:
            print("%4d   flat arm is silent" % knob)
            continue
        centres, masks = log_bands(f)
        pf = np.array([flat[m].mean() for m in masks])
        for reso in sorted(r for r in pairs[knob] if r != 0):
            res, _ = spectrum(pairs[knob][reso])
            if res is None:
                continue
            pr = np.array([res[m].mean() for m in masks])
            with np.errstate(divide="ignore", invalid="ignore"):
                db = 10 * np.log10(np.where(pf > 0, pr / pf, np.nan))
            ok = np.isfinite(db)
            if ok.sum() < 5:
                continue
            i = int(np.nanargmax(np.where(ok, db, -np.inf)))
            peak, boost = centres[i], db[i]
            model = FM.cutoff_hz(knob)
            rows.append((knob, reso, peak, model, boost))
            print("%4d   %4d   %7.0f   %8.0f     %6.2f     %+6.1f    %s"
                  % (knob, reso, peak, model, peak / model, boost,
                     "fitted" if lo_band <= knob <= hi_band else "EXTRAP"))
    if len(rows) >= 3:
        r = np.array([(k, p, m) for k, _, p, m, _ in rows], dtype=float)
        slope = np.polyfit(r[:, 0], np.log2(r[:, 1]), 1)[0]
        ratios = r[:, 1] / r[:, 2]
        print("\nmeasured log2 slope  %.6f per knob  (%.3f oct/10knob)"
              % (slope, slope * 10))
        print("model    log2 slope  %.6f per knob  (%.3f oct/10knob)"
              % (FM.CUTOFF_LOG2_PER_PERCENT, FM.CUTOFF_LOG2_PER_PERCENT * 10))
        print("peak/model: mean %.3f, spread %.3f to %.3f"
              % (ratios.mean(), ratios.min(), ratios.max()))
        print("\nIf the mean is near 1.0 the model's absolute mapping is right.")
        print("A consistent offset is the correction to CUTOFF_LOG2_AT_ZERO:")
        print("  %+.4f in log2, i.e. %+.3f octaves" % (np.log2(ratios.mean()),
                                                       np.log2(ratios.mean())))


if __name__ == "__main__":
    main()
