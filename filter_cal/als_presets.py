"""Fit cutoff per KNOB setting against the emulator's pre-filter render.

The same model and the same alternating least squares as `als.py`, but keyed
on the preset's own stored knob value (0-100) rather than on a CC number, so
it consumes what `sweep_presets.py` records.

    hw_i(f)  =  a(f)  +  emu(f)  +  lp_db(f; fc_i, Q)          [all in dB]

`a(f)` is everything fixed in the analog path -- reconstruction filter, output
stage, codec -- and is shared by every setting, so it is solved for instead of
assumed. That is the whole point: dividing one recording by another leaves the
widest setting constraining almost nothing, and makes the result depend on a
reference that is itself filtered.

**One render serves every setting.** The MicroFreak's filter is analog and
sits after the DAC, so the cutoff knob cannot change what the firmware
computes. The emulator's `--no-filter` render of this preset is therefore the
same signal for all 17 points, which is why there is a single `emu` argument.

    uv run python als_presets.py EMU_NOFILTER.wav

Reports the fitted fc per setting next to what `mfemu.filter_model` currently
predicts, so the two can be compared without editing anything.

**What this fit does and does not determine.** Measured 2026-10-17 on a
17-point preset sweep with a SawX source:

  * The SLOPE is well determined and stable: 0.1160 log2-per-knob against the
    model's 0.1037, a ratio of 1.118. Widening the Q grid down to 0.15 and
    the fc grid up to 80 kHz moved it only to 1.141. So the model's
    octaves-per-knob is right to within about 15%.
  * The ABSOLUTE fc is NOT identified by this data. The same widening moved
    the fitted fc at knob 60 from 1.93x the model to 3.63x, and the two
    highest settings simply pinned to whatever the grid ceiling was. A
    parameter that slides with the search bounds has not been measured.
    The reason is visible in the numbers: the harmonics reach about 9 kHz
    (140 x 65 Hz) while the fitted corner sits above that, so the data
    constrains only the filter's gentle onset, never its corner. Q lands on
    the lowest value offered, which is the same symptom.
  * Settings BELOW knob 60 cannot be fitted at all -- fewer than five
    harmonics survive 10x the local noise. The harmonics that carry the
    information are precisely the ones the filter is removing, so closing the
    filter destroys the measurement. This is the method's floor, not a bug.

Do not update `filter_model.py` from this without fixing the first two: a
louder source, a higher note (more harmonic spacing per bin), or a longer
window would all extend the usable band.
"""
import os
import pickle
import sys
import wave

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from point import f0_of

HERE = os.path.dirname(os.path.abspath(__file__))
NMAX = 140
# 1 = 12 dB/oct (what filter_model assumes), 2 = 24 dB/oct. Set ALS_SECTIONS
# to compare: a 2-pole fit to a steeper filter shows up as a Q below the
# model's own floor and a cutoff pushed high, which is what prompted this.
SECTIONS = int(os.environ.get("ALS_SECTIONS", "1"))
MIN_HARMONICS = 5


def lp_db(f, fc, q, sections=1):
    """Magnitude of `sections` cascaded 2-pole lowpasses, in dB.

    `sections=1` is the 12 dB/octave the model assumes -- a topology the
    model's own header records as owner-supplied rather than measured, so it
    is worth being able to test. `sections=2` is 24 dB/octave.
    """
    w = f / fc
    one = -10.0 * np.log10((1 - w * w) ** 2 + (w / q) ** 2)
    return sections * one


def hw_harmonics(entry):
    """Harmonic magnitudes and the local noise beside each, from a sweep point."""
    rate, f0, sp, n = entry
    df = rate / n
    f, mag, noi = [], [], []
    for k in range(1, NMAX + 1):
        fk = k * f0
        if fk > 15500:
            break
        j = int(round(fk / df))
        jn = int(round((k + 0.5) * f0 / df))      # between harmonics: noise
        if j + 3 >= len(sp):
            break
        f.append(fk)
        mag.append(sp[max(1, j - 2):j + 3].max())
        noi.append(np.median(sp[max(1, jn - 4):jn + 5]))
    return np.array(f), np.array(mag), np.array(noi)


def emu_harmonics(path, nmax=NMAX, nominal=65.41):
    """Harmonic magnitudes of the emulator's pre-filter render.

    The pitch is MEASURED here too, and separately: the device and the
    emulator are not at the same pitch (README records 0.3 and 1.2 cents off
    nominal), and by the 96th harmonic that difference is several FFT bins.
    """
    with wave.open(path, "rb") as w:
        rate, ch = w.getframerate(), w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    x = x.reshape(-1, ch).mean(axis=1).astype(float)
    # Skip the attack: the window must be the steady part, as on the hardware.
    seg = x[int(0.25 * rate):int(1.25 * rate)]
    f0 = f0_of(seg, rate, nominal)
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    df = rate / len(seg)
    out = {}
    for k in range(1, nmax + 1):
        fk = k * f0
        if fk > 15500:
            break
        j = int(round(fk / df))
        if j + 3 >= len(sp):
            break
        out[k] = sp[max(1, j - 2):j + 3].max()
    return out, f0


def main():
    emu_path = sys.argv[1] if len(sys.argv) > 1 else None
    if not emu_path or not os.path.exists(emu_path):
        raise SystemExit(
            "usage: als_presets.py EMU_NOFILTER.wav\n"
            "  Render the SAME preset the sweep used, with --no-filter:\n"
            "    mfemu render --source BANK.mfprojz --preset 5=PRESET.mfpz \\\n"
            "                 --preset-schema 5=0x33 --notes 36:1.6 \\\n"
            "                 --no-filter --out emu.wav")
    d = pickle.load(open(os.path.join(HERE, "cutoff_sweep_presets.pkl"), "rb"))
    # Seed the emulator's pitch search from the hardware's own measured f0:
    # the sweep may have been recorded at any note, and a seed an octave or
    # two away is exactly how trap 2 gets you.
    hw_f0 = d[max(d)][1]
    emu, emu_f0 = emu_harmonics(emu_path, nominal=hw_f0)
    if abs(emu_f0 - hw_f0) / hw_f0 > 0.05:
        raise SystemExit(
            "the emulator render is at %.1f Hz and the sweep at %.1f Hz -- "
            "different notes.\nRender the reference with the same --notes the "
            "sweep used." % (emu_f0, hw_f0))
    knobs = sorted(d, reverse=True)

    F, HW, USE = {}, {}, {}
    for v in knobs:
        f, mag, noi = hw_harmonics(d[v])
        k = min(len(f), len(emu))
        F[v], HW[v] = f[:k], 20 * np.log10(mag[:k] + 1e-12)
        USE[v] = mag[:k] > 10 * noi[:k]           # 20 dB above local noise
    nH = min(len(F[v]) for v in knobs)
    EMU = np.array([20 * np.log10(emu[k + 1] + 1e-12) for k in range(nH)])
    for v in knobs:
        F[v], HW[v], USE[v] = F[v][:nH], HW[v][:nH], USE[v][:nH]

    print("emulator render  %s" % os.path.basename(emu_path))
    print("emulator f0      %.3f Hz   (hardware %.3f Hz)"
          % (emu_f0, d[knobs[0]][1]))
    print("harmonics        %d usable in common\n" % nH)

    grid = np.exp(np.linspace(np.log(30), np.log(40000), 900))
    best = None
    for q in (0.4, 0.5, 0.6, 0.7071, 0.85, 1.0):
        fc = {v: 20000.0 * (v / 100.0) ** 3 + 60.0 for v in knobs}
        for _ in range(30):
            acc, cnt = np.zeros(nH), np.zeros(nH)
            for v in knobs:
                u = USE[v]
                acc[u] += HW[v][u] - EMU[u] - lp_db(F[v][u], fc[v], q, SECTIONS)
                cnt[u] += 1
            ok = cnt > 0
            a = np.where(ok, acc / np.maximum(cnt, 1), 0.0)
            tot, used = 0.0, 0
            for v in knobs:
                u = USE[v] & ok
                if u.sum() < MIN_HARMONICS:
                    continue
                target = HW[v][u] - EMU[u] - a[u]
                model = lp_db(F[v][u], grid[:, None], q, SECTIONS)
                err = ((model - target) ** 2).mean(axis=1)
                i = int(np.argmin(err))
                fc[v] = grid[i]
                tot += err[i]
                used += 1
        if best is None or tot / max(used, 1) < best[0]:
            best = (tot / max(used, 1), q, dict(fc), a.copy(), ok.copy())

    resid, q, fc, a, ok = best
    print("sections = %d (%d dB/oct)   shared Q = %.4f   "
          "mean sq residual %.2f dB^2\n" % (SECTIONS, 12 * SECTIONS, q, resid))

    sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
    import mfemu.filter_model as FM
    lo, hi = FM.CUTOFF_FITTED_RANGE

    print("knob    fitted fc    model fc    fitted/model   harmonics   band")
    rows = []
    for v in sorted(knobs):
        u = (USE[v] & ok).sum()
        if u < MIN_HARMONICS:
            print("%5g        -- too few harmonics above noise (%d) --" % (v, u))
            continue
        m = FM.cutoff_hz(v)
        print("%5g   %9.0f   %9.0f      %6.2f        %4d     %s"
              % (v, fc[v], m, fc[v] / m, u, "fitted" if lo <= v <= hi else "EXTRAP"))
        rows.append((v, fc[v], m))
    if rows:
        r = np.array(rows)
        s_fit = np.polyfit(r[:, 0], np.log2(r[:, 1]), 1)[0]
        print("\nfitted log2 slope   %.6f per knob  (%.3f oct/10knob)"
              % (s_fit, s_fit * 10))
        print("model  log2 slope   %.6f per knob  (%.3f oct/10knob)"
              % (FM.CUTOFF_LOG2_PER_PERCENT, FM.CUTOFF_LOG2_PER_PERCENT * 10))
        print("ratio               %.3f" % (s_fit / FM.CUTOFF_LOG2_PER_PERCENT))
        np.save(os.path.join(HERE, "cutoff_points_presets.npy"), r)
        print("\n-> cutoff_points_presets.npy  (knob, fitted fc, model fc)")


if __name__ == "__main__":
    main()
