"""Record the device's audio while driving it over MIDI."""
import sys, time
import numpy as np
import rtmidi2

from microfreak import audio
from microfreak.audio import mono          # noqa: F401  (used by the scripts)

RATE = 48000
DEVICE = "USB Audio CODEC"                 # the capture interface, by name
LEAD_IN = 1.2                              # seconds recorded before actions()


def open_midi(port="MicroFreak"):
    out = rtmidi2.MidiOut()
    for i, name in enumerate(out.ports):
        if port.lower() in name.lower():
            out.open_port(i); return out
    raise SystemExit("no MIDI port matching %r" % port)

def record(path, seconds, actions, rate=RATE, device=None):
    """Record `seconds` of stereo, running `actions(midi)` after LEAD_IN.

    Writes the capture to `path` and returns (float64 array, rate).
    """
    try:
        rec = audio.Recorder(device or DEVICE, rate, channels=2)
    except audio.AudioError as exc:
        raise SystemExit("%s\nInputs:\n%s" % (exc, audio.describe()))
    midi = open_midi()
    with rec:
        start = rec.mark()
        time.sleep(LEAD_IN)
        try:
            actions(midi)
        finally:
            del midi
        end = start + int(seconds * rate)
        time.sleep(max(0.0, (end - rec.mark()) / rate))
        rec.wait_for(end)
    if rec.overflows or rec.dropped():
        print("warning: %s dropped audio" % path, file=sys.stderr)
    x = rec.audio()[start:end].astype(np.float64)
    audio.write_wav(path, x, rate)
    return x, rate

def stats(x, rate, skip=0.3):
    seg = x[int(skip * rate):]
    if not len(seg) or not np.any(seg):
        return 0.0, 0.0
    m = mono(seg)
    sp = np.abs(np.fft.rfft(m * np.hanning(len(m))))
    f = np.fft.rfftfreq(len(m), 1 / rate)
    return float(np.sqrt((m ** 2).mean())), float((f * sp).sum() / sp.sum())

if __name__ == "__main__":
    def play(midi):
        midi.send_message([0xC0, 15])          # panel 16: SawX
        time.sleep(0.4)
        midi.send_message([0x90, 48, 100])
        time.sleep(2.0)
        midi.send_message([0x80, 48, 64])
    x, rate = record(sys.argv[1] if len(sys.argv) > 1 else "/tmp/test.wav", 4.0, play)
    rms, centroid = stats(x, rate)
    print("recorded %d frames at %d Hz  rms=%.4f  peak=%.3f  centroid=%.0f Hz"
          % (len(x), rate, rms, np.abs(x).max(), centroid))
