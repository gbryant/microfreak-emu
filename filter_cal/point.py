"""One filter setting, one recording. No timing alignment to get wrong."""
import os, sys, time
# Resolve the sibling modules from THIS file's directory, not the working
# directory: `sys.path.insert(0, ".")` meant the script only ran from inside
# filter_cal/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from capture import record

def mono(x):
    """The codec presents the synth differentially -- the channels are
    anti-phase (r = -0.998), so their DIFFERENCE is the signal and their mean
    is 30 dB of cancellation residue."""
    return (x[:, 0] - x[:, 1]) / 2.0

def capture_point(path, program, cc_cutoff, cc_reso, note=36, hold=1.6):
    def play(midi):
        midi.send_message([0xC0, program])
        time.sleep(0.35)
        midi.send_message([0xB0, 23, cc_cutoff])
        midi.send_message([0xB0, 83, cc_reso])
        time.sleep(0.25)
        midi.send_message([0x90, note, 100])
        time.sleep(hold)
        midi.send_message([0x80, note, 64])
    x, rate = record(path, 1.2 + 0.6 + hold + 0.4, play)
    s = mono(x)
    # find the note by energy, then take a steady window inside it
    w = int(0.05 * rate)
    env = np.array([np.sqrt((s[i:i + w] ** 2).mean()) for i in range(0, len(s) - w, w)])
    on = int(np.argmax(env > env.max() * 0.35))
    a = int((on + 3) * w)
    b = min(len(s), a + int(1.1 * rate))
    return s[a:b], rate

def f0_of(seg, rate, nominal):
    """Pitch by autocorrelation, refined -- an FFT peak can lock to a sideband."""
    d = seg - seg.mean()
    ac = np.correlate(d, d, "full")[len(d) - 1:]
    lo, hi = int(rate / (nominal * 1.6)), int(rate / (nominal / 1.6))
    k = lo + int(np.argmax(ac[lo:hi]))
    y0, y1, y2 = ac[k - 1], ac[k], ac[k + 1]
    return rate / (k + 0.5 * (y0 - y2) / (y0 - 2 * y1 + y2))
