"""Fit fc for a single captured point against the emulator source."""
import os
import numpy as np, als

# Off __file__, as every sibling script here does. The bare relative path
# this used worked only when cwd happened to be filter_cal/.
_REF = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "src36", "00-BasicWaves.wav")
_EMU = None

def emu():
    global _EMU
    if _EMU is None:
        _EMU = als.emu_harmonics(_REF)
    return _EMU

def fit(entry, q=0.6, gate=10.0, minh=5):
    e = emu()
    f, mag, noi = als.hw_harmonics(entry)
    k = min(len(f), len(e))
    E = np.array([20*np.log10(e[i+1]+1e-12) for i in range(k)])
    H = 20*np.log10(mag[:k]+1e-12)
    u = mag[:k] > gate*noi[:k]
    if u.sum() < minh:
        return None, int(u.sum())
    grid = np.exp(np.linspace(np.log(30), np.log(40000), 900))
    t = H[u] - E[u]
    m = als.lp_db(f[:k][u], grid[:, None], q)
    m = m - m.mean(axis=1)[:, None] + t.mean()      # absorb the constant gain
    err = ((m - t[None, :])**2).mean(axis=1)
    i = int(np.argmin(err))
    return grid[i], int(u.sum())
