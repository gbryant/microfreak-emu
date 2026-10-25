"""Fit cutoff per CC step against the emulator's pre-filter render.

    Render it with `mfemu render --no-filter`: the filter is applied by
    default now, and fitting against audio that already went through the model
    would fit the model to itself.

    hw_i(f)  =  a(f)  +  emu(f)  +  lp_db(f; fc_i, Q)          [all in dB]

`a(f)` is everything fixed in the analog path -- reconstruction filter, output
stage, the codec -- and is shared by every step, so it can be solved for
instead of assumed. Alternating least squares: given {fc_i} solve a(f) in
closed form, then re-fit each fc_i. Better conditioned than dividing by
another recording, where the widest-open step barely constrains anything.
"""
import os, pickle, sys, wave
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
# Defaults resolved from this file's directory rather than the working one, so
# the script runs from anywhere. Both are overridable on the command line:
#   als.py [SWEEP.pkl [EMU_NOFILTER.wav]]
SWEEP = os.path.join(HERE, "cutoff_sweep.pkl")
DEFAULT_EMU = os.path.join(HERE, "src36", "00-BasicWaves.wav")

NMAX = 140


def lp_db(f, fc, q):
    w = f / fc
    return -10.0 * np.log10((1 - w * w) ** 2 + (w / q) ** 2)


def hw_harmonics(entry):
    rate, f0, sp, n = entry
    df = rate / n
    f, mag, noi = [], [], []
    for k in range(1, NMAX + 1):
        fk = k * f0
        if fk > 15500:
            break
        j = int(round(fk / df)); jn = int(round((k + 0.5) * f0 / df))
        f.append(fk)
        mag.append(sp[max(1, j - 2):j + 3].max())
        noi.append(np.median(sp[max(1, jn - 4):jn + 5]))
    return np.array(f), np.array(mag), np.array(noi)


def emu_harmonics(path, nmax=NMAX):
    """Harmonic magnitudes of the emulator's pre-filter render.

    The pitch is MEASURED, not assumed. Twice in this calibration a fixed
    nominal f0 walked off the harmonics at high k -- a 1.2 cent error is 3
    bins by the 96th harmonic -- and both times the result looked like a real
    band limit in the signal. It was the analysis, not the signal.
    """
    with wave.open(path, "rb") as w:
        rate = w.getframerate(); ch = w.getnchannels()
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    x = x.reshape(-1, ch).mean(axis=1).astype(float)
    seg = x[int(0.15 * rate):int(1.15 * rate)]
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))); df = rate / len(seg)
    from point import f0_of
    f0 = f0_of(seg, rate, 65.41)
    out = {}
    for k in range(1, nmax + 1):
        fk = k * f0
        if fk > 15500: break      # the engine's own 32 kHz Nyquist
        j = int(round(fk / df))
        out[k] = sp[max(1, j - 2):j + 3].max()
    return out


def main():
    sweep = sys.argv[1] if len(sys.argv) > 1 else SWEEP
    emu_wav = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_EMU
    for path, what in ((sweep, "the recorded sweep"),
                       (emu_wav, "the emulator's --no-filter render")):
        if not os.path.exists(path):
            raise SystemExit(
                "%s is missing: %s\n"
                "  Neither is in this repository -- they are gigabytes of "
                "audio of one\n  instrument. capture.py and point.py record "
                "the sweep; render the\n  reference with `mfemu render "
                "--no-filter`.\n"
                "  usage: als.py [SWEEP.pkl [EMU_NOFILTER.wav]]" % (what, path))
    d = pickle.load(open(sweep, "rb"))
    emu = emu_harmonics(emu_wav)
    ccs = sorted(d, reverse=True)

    F, HW, USE = {}, {}, {}
    for v in ccs:
        f, mag, noi = hw_harmonics(d[v])
        k = min(len(f), len(emu))
        F[v] = f[:k]
        HW[v] = 20 * np.log10(mag[:k] + 1e-12)
        USE[v] = mag[:k] > 10 * noi[:k]
    # nH off the SHORTEST CC, not off the first one. Each CC's k is
    # min(len(f), len(emu)) computed independently, so a cutoff that lands on
    # a different harmonic count gives a shorter array -- and the boolean-mask
    # indexing below then runs off the end with an IndexError.
    # als_presets.py already does exactly this; als.py was never updated.
    nH = min(len(F[v]) for v in ccs)
    EMU = np.array([20 * np.log10(emu[k + 1] + 1e-12) for k in range(nH)])
    for v in ccs:
        F[v], HW[v], USE[v] = F[v][:nH], HW[v][:nH], USE[v][:nH]

    grid = np.exp(np.linspace(np.log(30), np.log(40000), 900))
    best = None
    for q in (0.4, 0.5, 0.6, 0.7071, 0.85, 1.0):
        fc = {v: 20000.0 * (v / 127.0) ** 3 + 60.0 for v in ccs}
        for _ in range(30):
            # a(f): the shared analog path, solved in closed form
            acc = np.zeros(nH); cnt = np.zeros(nH)
            for v in ccs:
                u = USE[v]
                acc[u] += HW[v][u] - EMU[u] - lp_db(F[v][u], fc[v], q)
                cnt[u] += 1
            ok = cnt > 0
            a = np.where(ok, acc / np.maximum(cnt, 1), 0.0)
            # each fc_i, given a(f)
            tot = 0.0
            for v in ccs:
                u = USE[v] & ok
                if u.sum() < 5:
                    continue
                target = HW[v][u] - EMU[u] - a[u]
                model = lp_db(F[v][u], grid[:, None], q)
                err = ((model - target) ** 2).mean(axis=1)
                i = int(np.argmin(err)); fc[v] = grid[i]; tot += err[i]
        if best is None or tot < best[0]:
            best = (tot, q, dict(fc), a.copy(), ok.copy())

    tot, q, fc, a, ok = best
    n_used = sum(1 for v in ccs if (USE[v] & ok).sum() >= 5)
    print("shared Q = %.4f      mean sq residual %.2f dB^2 over %d steps"
          % (q, tot / max(1, n_used), n_used))
    print("analog path a(f): %.1f dB at 65 Hz, %.1f at 1 kHz, %.1f at 10 kHz\n"
          % (a[0], a[min(15, nH - 1)], a[min(152, nH - 1)] if nH > 152 else a[-1]))
    print("   CC   knob%    fitted fc")
    rows = []
    for v in ccs:
        if (USE[v] & ok).sum() >= 5:
            rows.append((v, fc[v]))
            print("  %3d   %5.1f   %8.0f Hz" % (v, v / 127 * 100, fc[v]))
    np.save(os.path.join(HERE, "cutoff_points.npy"), np.array(rows))
    np.save(os.path.join(HERE, "q_value.npy"), np.array([q]))
    return rows, q


if __name__ == "__main__":
    main()
