"""Record a cutoff sweep by WRITING PRESETS instead of sending CC.

    uv run --extra hardware python sweep_presets.py            # the default sweep
    uv run --extra hardware python sweep_presets.py 60 60 60   # a repeatability check

Writes `cutoff_sweep_presets.pkl` as {knob_percent: (rate, f0, magnitude, n)},
the same shape `als.py` reads, and the knob value is the preset's own stored
display value rather than a CC number.

**Why preset writes rather than CC.** README's fourth trap is that a CC-driven
measurement is meaningless unless Utility knob catch is `Jump`: in `Scaled`
mode an incoming CC is a relative nudge whose effect depends on accumulated
state, and one target approached four times gave 1671, 1327, 2046 and 1719 Hz.
A preset write stores an absolute value, so the knob-catch mode cannot affect
it, nothing has to be set on the instrument first, and the sweep needs no
hands. It costs about 1.5 s per point to write and reload.

Four things this script does that look optional and are not. Each one produced
a wrong answer first.

1. **The source is built from the Init patch.** Not from a factory preset. A
   designed preset is not a stationary source: `16 V.Analog` has four
   mod-matrix cells routed to cutoff and a mid-note amplitude dip, and the
   SAME setting re-measured gave rms 0.0085, 0.0238 and 0.0649 -- a 7.6x
   spread, which no curve survives. The Init patch has no mod-matrix cell
   routed anywhere; the same setting then repeats to 0.4%. The script still
   zeroes the five cutoff cells and `cycling_env_amount` explicitly, so it is
   correct even if handed a different template.

2. **The engine is SawX.** The filter can only be measured where the source
   has energy. `Basic Waves` -- the Init patch's own engine -- puts 90% of its
   energy below 335 Hz and can say nothing about a corner above about 500 Hz.
   SawX gives 189 harmonics up to 11.5 kHz. The `Noise` engine is stationary
   and tempting, but it is not white (-3.2 dB/octave measured) and has no
   harmonics to track, so `als.py` cannot consume it.

3. **A program change to the already-selected slot does not reload the voice.**
   `preset select` documents the short-circuit. Writing slot 5 repeatedly and
   sending PC 5 each time measures the FIRST preset every time, silently. So
   each point selects another slot first, then the scratch slot.

4. **The first note after a reload can be wrong, so it plays twice.**
   `speech/cli.py` says this about a power cycle; it applies to a preset
   reload too. Measuring the first note gave a non-monotonic amplitude curve
   with knob 30 louder than knob 100. The measured note is the LAST burst in
   the recording, found by energy, not the first.

And f0 comes from `point.f0_of` (autocorrelation), never from an FFT peak. A
peak-max in 50-90 Hz on this very source reported 58.0 Hz for a note whose
real f0 is 65.419 -- a sideband lock, which is README's second trap.
"""
import os
import pickle
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from capture import record, mono
from point import f0_of

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.environ.get("MFREAK_REPO", os.path.expanduser("~/github/microfreak-tools"))
WORK = os.environ.get("SWEEP_WORK", "/tmp/mf-sweep")
OUT = os.path.join(HERE, "cutoff_sweep_presets.pkl")

SLOT = int(os.environ.get("SWEEP_SLOT", "5"))
OTHER = (SLOT + 1) % 128          # any other slot, to force a reload
# The note is a real experimental variable, not a detail. A low note gives
# many harmonics packed closely; a high note gives fewer, spread wider. What
# matters is whether the harmonics REACH past the filter's corner: with C2 and
# als's NMAX=140 they stop at 140 x 65.4 = 9.2 kHz, which is below where the
# corner was fitted, so the fit never saw it.
NOTE = int(os.environ.get("SWEEP_NOTE", "36"))
NOMINAL = 440.0 * 2.0 ** ((NOTE - 69) / 12.0)
HOLD = 1.6
ENGINE = os.environ.get("SWEEP_ENGINE", "SawX")
# Resonance. 0 gives a clean corner; high gives a PEAK at the cutoff,
# which can be located directly instead of fitted -- see find_peak.py.
RESO = float(os.environ.get("SWEEP_RESO", "0"))

# f0 of the first (widest) point, used to sanity-check the rest.
REFERENCE_F0 = [None]

# Everything that could modulate the filter, pinned off. The Init patch
# already has all of these clear; setting them anyway makes the measurement
# independent of the template it is handed.
CLEAN = ["--set", "osc_type=%s" % ENGINE,
         "--set", "mod:cyc_env:cutoff=0", "--set", "mod:env:cutoff=0",
         "--set", "mod:lfo:cutoff=0", "--set", "mod:press:cutoff=0",
         "--set", "mod:key_arp:cutoff=0",
         "--set", "cycling_env_amount=0", "--set", "envelope_sustain=100",
         "--set", "filter_resonance=%g" % RESO, "--set", "filter_type=LPF"]

DEFAULT_KNOBS = [100, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75, 80, 85, 90]


def mfreak(*args):
    r = subprocess.run(["uv", "run", "mfreak"] + list(args),
                       cwd=TOOLS, capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("mfreak %s failed:\n%s" % (" ".join(args), r.stderr[-800:]))
    return r.stdout


def template():
    """The Init patch, the one preset microfreak-emu ships."""
    p = os.path.join(os.path.dirname(HERE), "tests", "fixtures", "init-patch.mfpz")
    if not os.path.exists(p):
        raise SystemExit(
            "%s is missing. It is the instrument's Init patch and this\n"
            "measurement needs a source with nothing modulating it. See NOTICE\n"
            "for how to regenerate it." % p)
    return p


def write_point(knob):
    out = os.path.join(WORK, "preset-%03d-r%03d.mfpz" % (int(knob), int(RESO)))
    mfreak("preset", "encode", "--template-file", template(), *CLEAN,
           "--set", "filter_cutoff=%g" % knob, "--out", out, "--json")
    mfreak("preset", "write", str(SLOT), "--file", out,
           "--name", "FCal%03d" % int(knob), "--yes", "--no-verify", "--json")
    return out


def analyse(path):
    """The analysis half of a measurement, on a recording already on disk."""
    import wave
    with wave.open(path) as w:
        rate, ch = w.getframerate(), w.getnchannels()
        a = np.frombuffer(w.readframes(w.getnframes()), "<i2")
    x = a.reshape(-1, ch).astype(float) / 32768.0
    return _analyse(mono(x), rate)


def _analyse(s, rate):
    w = int(0.05 * rate)
    env = np.array([np.sqrt((s[i:i + w] ** 2).mean())
                    for i in range(0, len(s) - w, w)])
    if env.max() < 1e-5:
        return None
    loud = np.where(env > env.max() * 0.35)[0]
    gaps = np.where(np.diff(loud) > 4)[0]
    on = int(loud[gaps[-1] + 1]) if len(gaps) else int(loud[0])   # LAST burst
    a = int((on + 3) * w)
    seg = s[a:min(len(s), a + rate)]
    # Autocorrelation (trap 2). At a low cutoff the harmonics it would lock to
    # have been filtered away and it can find the wrong period outright: knob
    # 20 reported 49.841 Hz for a note whose f0 is 65.4. The widest setting
    # has the best signal-to-noise, so its f0 is the reference and a per-point
    # estimate is only kept when it agrees with that.
    #
    # Do NOT "improve" this by maximising a harmonic sum over a fine f0 grid.
    # That was tried and it is biased: it moved knob 100 from 65.536 to
    # 66.811 Hz (+33 cents), and locating each harmonic's own peak and
    # dividing by its index showed 65.536 agreeing to 0.49% while 66.811
    # scattered 2.20%. The comb has spurious alignments and the sum finds them.
    f0 = f0_of(seg, rate, NOMINAL)
    if REFERENCE_F0[0] is None:
        REFERENCE_F0[0] = f0
    elif abs(f0 - REFERENCE_F0[0]) / REFERENCE_F0[0] > 0.02:
        f0 = REFERENCE_F0[0]
    sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
    return rate, float(f0), sp, len(seg), float(np.sqrt((seg ** 2).mean()))


def measure(knob):
    path = os.path.join(WORK, "hw-%03d-r%03d.wav" % (int(knob), int(RESO)))

    def play(midi):
        midi.send_message([0xC0, OTHER])         # force a reload (trap 3)
        time.sleep(0.3)
        midi.send_message([0xC0, SLOT])
        time.sleep(0.9)                          # let the page decode into RAM
        midi.send_message([0x90, NOTE, 100])     # priming note (trap 4)
        time.sleep(0.5)
        midi.send_message([0x80, NOTE, 64])
        time.sleep(0.5)
        midi.send_message([0x90, NOTE, 100])     # the one that is measured
        time.sleep(HOLD)
        midi.send_message([0x80, NOTE, 64])

    x, rate = record(path, 1.2 + 1.2 + 1.0 + HOLD + 0.4, play)
    return _analyse(mono(x), rate)              # differential codec (trap 1)


def rebuild_from_wavs():
    """Re-derive the pickle from recordings already in WORK.

    An analysis change should never need the instrument again. This also
    exists because an f0 refinement step DID need undoing: see the README's
    note on harmonic-sum refinement.
    """
    import glob
    import re as _re
    d = {}
    paths = glob.glob(os.path.join(WORK, "hw-*.wav"))
    # Widest setting FIRST, so it sets REFERENCE_F0 -- it has the best
    # signal-to-noise. Taking them in filename order would seed the reference
    # from the lowest cutoff, which is the least reliable point there is.
    def knob_of(p):
        # measure() writes hw-%03d-r%03d.wav. This pattern wanted
        # hw-NNN.wav with no -rNNN, so .group(1) hit None and --from-wavs
        # raised AttributeError on its own output, every time.
        # find_peak.py had the right pattern for the same files all along.
        return float(_re.search(r"hw-(\d+)-r(\d+)\.wav", p).group(1))
    for path in sorted(paths, key=knob_of, reverse=True):
        k = knob_of(path)
        got = analyse(path)
        if got is None:
            print("%5g   SILENT" % k)
            continue
        rate, f0, sp, n, rms = got
        d[k] = (rate, f0, sp, n)
        print("%5g  %.5f  %8.3f" % (k, rms, f0))
    pickle.dump(d, open(OUT, "wb"))
    print("\n-> %s  (%d points, rebuilt from %s)" % (OUT, len(d), WORK))


def main():
    os.makedirs(WORK, exist_ok=True)
    if sys.argv[1:2] == ["--from-wavs"]:
        print("knob    rms      f0 Hz")
        return rebuild_from_wavs()
    knobs = [float(a) for a in sys.argv[1:]] or DEFAULT_KNOBS
    d = {}
    print("source: Init patch + %s, slot %d, note %d (%.2f Hz nominal), "
          "resonance %g" % (ENGINE, SLOT, NOTE, NOMINAL, RESO))
    print("knob    rms      f0 Hz    harmonics")
    for k in knobs:
        write_point(k)
        got = measure(k)
        if got is None:
            print("%5.0f   SILENT -- nothing recorded" % k)
            continue
        rate, f0, sp, n, rms = got
        d[k] = (rate, f0, sp, n)
        floor = float(np.median(sp[int(16000 * n / rate):int(22000 * n / rate)]))
        df = rate / n
        nh = sum(1 for j in range(1, 240) if f0 * j < 15500 and
                 sp[max(1, int(f0 * j / df) - 2):int(f0 * j / df) + 3].max() > floor * 10)
        print("%5.0f  %.5f  %8.3f   %4d%s"
              % (k, rms, f0, nh,
                 "" if REFERENCE_F0[0] is None or abs(f0 - REFERENCE_F0[0]) < 1e-9
                 else ""))
        sys.stdout.flush()
    pickle.dump(d, open(OUT, "wb"))
    print("\n-> %s  (%d points)" % (OUT, len(d)))
    if len(set(knobs)) == 1 and len(knobs) > 1:
        print("repeatability check: the rms values above should agree to ~1%.")


if __name__ == "__main__":
    main()
