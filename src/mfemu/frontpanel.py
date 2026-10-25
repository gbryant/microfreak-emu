#!/usr/bin/env python3
"""Press buttons, turn encoders and move knobs on a running `mfemu serve`.

    mfemu panel screen
    mfemu panel press Save
    mfemu panel turn Preset 3
    mfemu panel knob Cutoff 300
    mfemu panel press Utility turn Preset 1 press PresetClick

Each action prints the text the display showed after it. Controls are
named (`mfemu panel --list`) or given as a 4-digit hex id. A turn is in
encoder steps, negative to turn back. A knob value is sent as given: how
the firmware maps it to a setting is not worked out, so a knob moves the
setting but not to a predictable value.

`memoff` turns Mem protect off through the Utility menu. An erased settings
page boots with it on, which stops Save.

Works with every official firmware from 1.0.5 to 5.0.0. Needs `mfemu
serve` running in another shell.
"""
# The front panel is scanned by co-processors that the emulator does not run.
# This stands in for them on the bus they share with the main chip: USART2,
# 8-byte frames of AA, six bytes, and an 8-bit sum. docs/FIRMWARE.md has the
# protocol. What this sends, as chip 2:
#
#   20 01 id_hi id_lo 00 00   button down
#   20 02 id_hi id_lo 08 00   button up
#   20 0F id_hi id_lo s_hi s_lo   encoder, s = signed steps
#   20 03 id_hi id_lo v_hi v_lo   knob, v = position
#
# Control ids are the firmware's own parameter ids, module * 256 + param,
# which the main chip assigns to the panel at boot.
#
# Each co-processor has a request line into the main chip. Holding all of
# them high keeps the main chip sending display updates; it is a stand-in
# for the real handshake, which is not modelled.
import argparse
import os
import sys
import time

CONTROLS = {
    "Preset": 0x0800, "PresetClick": 0x0801, "Save": 0x0802,
    "Utility": 0x0803, "Shift": 0x0804,
    "Matrix": 0x0900, "MatrixClick": 0x0901,
    "Glide": 0x0300, "Cutoff": 0x0101, "Resonance": 0x0102,
    "Rise": 0x0201, "Fall": 0x0203, "Hold": 0x0204, "CycAmount": 0x0206,
    "Attack": 0x0601, "Decay": 0x0602, "Sustain": 0x0603, "FilterAmt": 0x0605,
    "Volume": 0x0704,
}
REQUEST_LINES = (("gpioPortC", 4), ("gpioPortC", 9), ("gpioPortC", 12),
                 ("gpioPortD", 14))
SOURCE = 0x20
QUIET = 2.5          # host seconds of display silence that end an action


def capture_lines(path):
    """Renode lines that record what the main chip sends on the bus."""
    return ["usart2 CreateFileBackend @%s true" % path]


def frame(*body):
    f = [0xAA] + [b & 0xFF for b in body]
    return f + [sum(f) & 0xFF]


def frames(data):
    """-> the valid 8-byte frames in a capture, in order."""
    out, i = [], 0
    while i + 8 <= len(data):
        if data[i] == 0xAA and sum(data[i:i + 7]) & 0xFF == data[i + 7]:
            out.append(bytes(data[i:i + 8]))
            i += 8
        else:
            i += 1
    return out


def screen_text(data):
    """-> the text sent to the display, one string per screen region."""
    texts, cur = [], None
    for f in frames(data):
        if f[1] != 0xF7:
            continue
        if f[2] == 0x70:
            if cur:
                texts.append(cur)
            cur = ""
        elif f[2] == 0x72 and cur is not None:
            cur += "".join(chr(c) for c in f[3:7] if 0x20 <= c < 0x7F or c == 10)
        elif f[2] == 0x71 and cur is not None:
            cur += "".join(chr(c) for c in f[3:6] if 0x20 <= c < 0x7F)
    if cur:
        texts.append(cur)
    return texts


def control(name):
    if name in CONTROLS:
        return CONTROLS[name]
    try:
        return int(name, 16)
    except ValueError:
        raise SystemExit("no control called %r. `mfemu panel --list` shows "
                         "them." % name)


class Panel:
    def __init__(self, synth=None, quiet=QUIET):
        if synth is None:
            from mfemu.midilink import EmulatedSynth
            synth = EmulatedSynth.attach()
        self.synth = synth
        self.quiet = quiet
        self.capture = getattr(synth, "bus_path", None)
        if not self.capture or not os.path.exists(self.capture):
            raise SystemExit("this emulator session records no panel bus. "
                             "Restart `mfemu serve`.")
        for port, pin in REQUEST_LINES:
            synth.monitor("%s OnGPIO %d true" % (port, pin))

    def _size(self):
        return os.path.getsize(self.capture)

    def settle(self):
        """Wait until the display has been quiet for `quiet` seconds."""
        last, since = self._size(), time.time()
        while time.time() - since < self.quiet:
            time.sleep(0.2)
            n = self._size()
            if n != last:
                last, since = n, time.time()
        return last

    def _send(self, *body):
        for b in frame(*body):
            self.synth.monitor("usart2 WriteChar 0x%02X" % b)

    def _act(self, *bodies, hold=0.0):
        start = self._size()
        for k, body in enumerate(bodies):
            if k and hold:
                time.sleep(hold)
            self._send(*body)
        end = self.settle()
        with open(self.capture, "rb") as fh:
            fh.seek(start)
            return screen_text(fh.read(end - start))

    def down(self, ident):
        return self._act((SOURCE, 0x01, ident >> 8, ident, 0x00, 0x00))

    def up(self, ident):
        return self._act((SOURCE, 0x02, ident >> 8, ident, 0x08, 0x00))

    def press(self, ident, hold=0.4):
        """A short press. On 5.0 a long Save press does something else."""
        return self._act((SOURCE, 0x01, ident >> 8, ident, 0x00, 0x00),
                         (SOURCE, 0x02, ident >> 8, ident, 0x08, 0x00),
                         hold=hold)

    def turn(self, ident, steps):
        s = steps & 0xFFFF
        return self._act((SOURCE, 0x0F, ident >> 8, ident, s >> 8, s))

    def knob(self, ident, value):
        value &= 0xFFFF
        return self._act((SOURCE, 0x03, ident >> 8, ident, value >> 8, value))

    def screen(self):
        with open(self.capture, "rb") as fh:
            data = fh.read()
        return screen_text(data)[-4:]

    def find(self, label, step=1, limit=30, cursor=True, shown=()):
        """Turn the preset encoder until the screen shows `label`, with the
        cursor on it when `cursor`. `shown` is what the screen already says.
        -> True if it got there."""
        want = (">" + label) if cursor else label
        if want in " ".join(shown):
            return True
        for _ in range(limit):
            if want in " ".join(self.turn(CONTROLS["Preset"], step)):
                return True
        return False

    def memory_protect_off(self):
        """Utility > ... Misc > Mem protect > OFF, found by its labels so it
        works on every firmware's menu layout. -> None, or what went wrong."""
        text = " ".join(self.press(CONTROLS["Utility"]))
        if "Utility" not in text:
            text = " ".join(self.press(CONTROLS["Utility"]))
        shown = [text]
        if "Global" in text:
            self.find("Global", shown=shown)
            shown = self.press(CONTROLS["PresetClick"])
        for label in ("Misc", "Mem protect"):
            if not self.find(label, shown=shown):
                return "no %s item" % label
            shown = self.press(CONTROLS["PresetClick"])
        if not self.find("OFF", step=-1, cursor=False, shown=shown):
            return "no OFF value"
        self.press(CONTROLS["PresetClick"])
        self.press(CONTROLS["Utility"])
        return None


def run(panel, words):
    """Run `press X`, `down X`, `up X`, `turn X N`, `knob X V`, `screen`,
    `memoff`. -> [(action, texts)]"""
    out, i = [], 0
    while i < len(words):
        w = words[i]
        if w in ("press", "down", "up"):
            label = "%s %s" % (w, words[i + 1])
            out.append((label, getattr(panel, w)(control(words[i + 1]))))
            i += 2
        elif w in ("turn", "knob"):
            label = "%s %s %s" % (w, words[i + 1], words[i + 2])
            out.append((label, getattr(panel, w)(control(words[i + 1]),
                                                 int(words[i + 2], 0))))
            i += 3
        elif w == "screen":
            out.append(("screen", panel.screen()))
            i += 1
        elif w == "memoff":
            problem = panel.memory_protect_off()
            out.append(("memoff", [problem or "Mem protect OFF"]))
            i += 1
        else:
            raise SystemExit("unknown action %r: use press, down, up, turn, "
                             "knob, screen or memoff" % w)
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("actions", nargs="*",
                    help="press NAME, down NAME, up NAME, turn NAME STEPS, "
                         "knob NAME VALUE, screen, memoff")
    ap.add_argument("--list", action="store_true", help="list the controls")
    args = ap.parse_args()
    if args.list:
        for name, ident in CONTROLS.items():
            print("  %-12s %04X" % (name, ident))
        return 0
    if not args.actions:
        ap.error("give at least one action, e.g. `mfemu panel screen`")
    for label, texts in run(Panel(), args.actions):
        print("%-22s %s" % (label, " | ".join(t.replace("\n", " / ") for t in texts) or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
