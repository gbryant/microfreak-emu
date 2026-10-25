#!/usr/bin/env python3
"""Talk MIDI/SysEx to the *emulated* MicroFreak, the way the MIDI backend
talks to the real one.

The point is a testbed: the repo already has hardware-verified protocol tools
(`microfreak.preset.read`, `microfreak.preset.write`, and the opcode table in
PROTOCOL.md), and
they all drive a `Synth` object with `send()` and `recv()`.  This provides the
same two methods backed by Renode instead of a MIDI port, so the same protocol
code can be pointed at emulated firmware -- which is what makes custom firmware
testable without flashing hardware.

Renode side: USART1 (the MIDI DIN port, 31250 baud) is connected to a TCP
server socket, so bytes flow *both* ways.  Everything before this drove the
emulator with `usart1 WriteChar`, which can only send; replies were invisible,
so no lock-step protocol could ever complete.

    from mfemu.midilink import EmulatedSynth
    with EmulatedSynth() as synth:
        synth.boot()                       # until it answers MIDI, not audio
        name, payload = read_preset.read_preset(synth, 4, seqgen)

Two differences from hardware worth knowing.  The emulator runs slower than
real time -- a measured 3.45 wall-seconds per emulated second -- so timeouts
are wall-clock generous by default.  And `boot()` returns when the firmware
will answer the protocol, after 1.25 emulated seconds, which is NOT when it
will make a sound: synthesis resumes around 11.5, and waiting that out is
`mfemu render`'s job.
"""
import os
import re
import selectors
import socket
import subprocess
import sys
import tempfile
import time

import mfemu.image as image
import mfemu.mfrender as mfrender                                                 # noqa: E402

import mfemu.paths as PATHS
import mfemu.startup as startup
import mfemu.frontpanel as frontpanel

# The Renode assets (boot.resc, the machine description, the peripherals)
# are NOT beside this file: they are not Python, so they live in renode/ at
# the repository root. Computed here rather than taken from PATHS because
# this runs above the import block.
_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.join(_REPO_ROOT, "renode")
_REPO = _REPO_ROOT

# PATHS.RENODE, not a hardcoded ~/Downloads. paths.py searches /Applications,
# ~/Downloads and /opt/homebrew and honours MF_RENODE, and the comment on
# `_first_existing` records that hardcoding one location is exactly what broke
# when Renode moved to /Applications. This default ignored all of that, so
# `EmulatedSynth()` with no arguments -- which is what its own docstring shows
# -- looked in one place the application probably is not.
DEFAULT_RENODE = PATHS.RENODE
DEFAULT_PORT = 3456
# 1.25 EMULATED seconds, which is what this has always actually waited.
# It read 14.0 until 2026-10-16, and got 1.24 s: `boot()` polled the firmware's
# tick counter at 0x2001E1D8 and divided by 10,000, on the documented
# assumption that it counts tenths of a millisecond. By the time main() runs
# the firmware has reprogrammed SysTick to a reload of 1,600 (SYST_RVR reads
# 0x63F), so the counter advances at 180 MHz / 1600 = 112,500/s -- measured
# 112,538 against Renode's own virtual time. The 14 was therefore 14/11.25.
#
# The number is kept at what was really being used, not raised to 14, because
# every caller has been working with it: nine call sites, the whole test suite,
# the DFU path and `serve`. Raising it would make all of them 8x slower to
# honour a figure nothing has needed.
#
# It is NOT enough for the audio engine. Synthesis resumes around 11.5 emulated
# seconds, which is why mfrender waits its own BOOT_SECONDS = 14 with
# `emulation RunFor` -- a real wait, and the one warm snapshots exist to skip.
# A machine booted through here answers SysEx and MIDI; it is not ready to
# render.
BOOT_SECONDS = 1.25


class EmulatedSynth:
    """A `Synth`-shaped object (send/recv) backed by a running Renode machine."""

    def __init__(self, flash=None, port=DEFAULT_PORT, monitor_port=None,
                 renode_dir=DEFAULT_RENODE, timeout=120.0, verbose=False,
                 firmware=None, bootloader=None):
        self.port = port
        self.monitor_port = monitor_port or (port + 1)
        self.renode_dir = renode_dir
        self.timeout = timeout
        self.verbose = verbose
        self.flash = flash
        # The main image to boot.  Defaults to the factory one; a patched raw
        # payload can be booted instead, which is how firmware patches are
        # tested before they are packaged.
        # Resolved LAZILY, on first read of `.firmware`, which in practice is
        # `_script()` and therefore only `start()`. Renode's LoadBinary needs
        # a BARE payload and what a caller has may be a .mff or a headered
        # .bin, so the resolve is still compulsory -- but it is not compulsory
        # HERE, and doing it in __init__ broke `attach()`.
        #
        # `attach()` binds to a machine someone else already started, with
        # whatever firmware they chose; it has no use for this value and never
        # reads it. Resolving eagerly meant that on a box where the machine
        # was started with `mfemu serve --firmware X.mff` and the drop folder
        # holds no firmware, `attach()` died with "no firmware" before it
        # touched a socket -- so every `MFREAK_EMULATOR=1 mfreak ...` and
        # test_preset_roundtrip.py failed, for a file none of them needed.
        # Quietly: the CLI prints the description once and a test does not
        # want it twice.
        self._firmware_arg = firmware
        self._firmware = None
        # With a bootloader the machine boots the way the instrument does:
        # from 0x08000000, through both of the bootloader's gates, and into
        # the app only if it decides the app is valid. Without one -- the
        # default, and all this repository can ship -- the app is entered
        # directly. Only the update procedure needs the difference.
        self.bootloader = bootloader
        self.proc = None
        self.sock = None
        self.mon = None
        self.buf = bytearray()          # bytes arriving on DIN
        self.usb_buf = bytearray()      # bytes arriving on USB
        self.usb_path = os.path.join(tempfile.gettempdir(),
                                     "midilink-usb-%d.bin" % port)
        # What the main chip sends the front-panel co-processors, including
        # the display text. `mfemu panel` reads it (frontpanel.py).
        self.bus_path = os.path.join(tempfile.gettempdir(),
                                     "midilink-bus-%d.bin" % port)
        self._usb_file = None
        # Where in the capture file this client's replies begin. Zero for a
        # machine we started ourselves -- the file is new and all of it is
        # ours. `attach()` moves it to the current end of file, because a
        # machine someone else booted has a capture going back to ITS boot.
        self._usb_start = 0
        self._log_file = None
        self.log_path = None
        self._usb_partial = b""
        self.usb_attached = False
        self.send_over_usb = False
        self._attached = False            # True when we joined a running machine
        self._log = []

    # -- lifecycle ---------------------------------------------------------
    @property
    def firmware(self):
        """The bare payload path, resolved on first use. See __init__."""
        if self._firmware is None:
            self._firmware = image.resolve(
                self._firmware_arg or PATHS.MAIN_RAW, quiet=True)
        return self._firmware

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # USB-MIDI event packets: four bytes each, the low nibble of the first
    # (the code index number) saying how many of the other three are MIDI.
    _CIN_LENGTHS = {0x2: 2, 0x3: 3, 0x4: 3, 0x5: 1, 0x6: 2, 0x7: 3,
                    0x8: 3, 0x9: 3, 0xA: 3, 0xB: 3, 0xC: 2, 0xD: 2,
                    0xE: 3, 0xF: 1}

    @classmethod
    def decode_usb_midi(cls, data):
        """USB-MIDI packets -> a flat MIDI byte stream."""
        out = bytearray()
        for i in range(0, len(data) - 3, 4):
            out += data[i + 1:i + 1 + cls._CIN_LENGTHS.get(data[i] & 0x0F, 0)]
        return bytes(out)

    def _script(self, path):
        lines = ["$mf = @%s" % _HERE,
                 "$fw = @%s" % PATHS.resc(self.firmware),
                 "\n".join(startup.resc_lines(self.firmware)
                           + ["include @%s" % os.path.join(_HERE, "boot.resc")])]
        if self.bootloader:
            # After the include, deliberately: boot.resc loads the app and
            # points the vector table at it, and this overrides the entry
            # point. The machine has not started yet, so the order is safe.
            # No `cpu PC` here. Renode takes the initial SP and PC from the
            # vector table that VectorTableOffset names, which is what the
            # hardware does on reset. Setting PC to the table's own address
            # instead makes the CPU execute the vector WORDS as instructions:
            # it HardFaulted 236 instructions in and sat in the bootloader's
            # own HardFault handler at 0x0800154D forever, which looked
            # convincingly like a gate failing.
            # Shared with mfrender's own script builder; the reasons each
            # line is the way it is live there.
            lines += mfrender.bootloader_lines(self.bootloader)
        if self.flash:
            lines.append("quadspi LoadImage @%s" % PATHS.resc(self.flash))
        lines += frontpanel.capture_lines(PATHS.resc(self.bus_path))
        lines += [
            # The socket must exist before the machine runs, or early output
            # is lost.  `false` means "do not wait for a client to connect".
            'emulation CreateServerSocketTerminal %d "midi" false' % self.port,
            "connector Connect usart1 midi",
            "start",
        ]
        open(path, "w").write("\n".join(lines) + "\n")
        return path

    def start(self):
        # gettempdir(), like usb_path, log_path and SESSION in this same
        # class. This one line hardcoded "/tmp" -- the odd sibling out of
        # four, and the only one that ignored TMPDIR.
        if os.path.exists(self.bus_path):
            os.remove(self.bus_path)
        script = self._script(os.path.join(tempfile.gettempdir(),
                                           "midilink-%d.resc" % self.port))
        # Renode's log goes to a file, not a pipe.  A pipe nobody reads fills
        # at 64 KB and then blocks the writer -- the emulated machine simply
        # stops, mid-transfer, and the symptom is a reply that never comes.
        # That was the intermittent timeout in the middle of a preset read:
        # the capture showed the device going quiet, not the host mis-parsing.
        self.log_path = os.path.join(tempfile.gettempdir(),
                                     "midilink-renode-%d.log" % self.port)
        self._log_file = open(self.log_path, "w")
        self.proc = subprocess.Popen(
            mfrender.renode_command(self.renode_dir) + ["--disable-xwt", "--plain",
             "--port", str(self.monitor_port), script],
            cwd=self.renode_dir, stdout=self._log_file, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, text=True)
        # If _connect() raises, the exception leaves start() and therefore
        # leaves __enter__ -- so Python never calls __exit__ and close() never
        # runs. The Renode just spawned stays alive holding both ports, which
        # is precisely the "a Renode left running from an earlier test" case
        # _connect()'s own error message explains: the leak manufactures the
        # symptom it reports. Tear down and re-raise.
        try:
            self._connect()
        except BaseException:
            # close(), which is what __exit__ would have called: it terminates
            # the process, waits, kills on timeout, and closes the log handle.
            # Its `_attached` guard is correct here -- start() spawned this one,
            # so it IS ours to stop.
            self.close()
            raise

    def _connect(self):
        """Attach to the machine's two sockets. Used after spawning Renode,
        and on its own by `attach()` when the machine is already running."""
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
                self.sock.setblocking(False)
                break
            except (ConnectionRefusedError, OSError):
                if self.proc is not None and self.proc.poll() is not None:
                    # Say WHY, with the ports named. The commonest cause by
                    # far is a Renode left running from an earlier test --
                    # these use fixed ports, and the monitor one is port+1, so
                    # the clash is often on a port the caller never mentioned.
                    # "Renode exited before opening the socket" on its own
                    # reads like a code regression and sent me looking at one.
                    tail = ""
                    try:
                        with open(self.log_path, errors="replace") as fh:
                            lines = [l.strip() for l in fh if l.strip()]
                        tail = next((l for l in reversed(lines)
                                     if "Unable to create" in l
                                     or "AddressAlreadyInUse" in l), "")
                        tail = tail or (lines[-1] if lines else "")
                    except OSError:
                        pass
                    raise RuntimeError(
                        "Renode exited before opening the socket. MIDI port "
                        "%d, monitor port %d.\n  %s\n  If the address is in "
                        "use, stop the old Renode with `pkill -f Renode` or "
                        "pass a different --port.\n  Full log: %s"
                        % (self.port, self.monitor_port, tail, self.log_path))
                time.sleep(0.5)
        else:
            raise RuntimeError("no socket on port %d after 60 s" % self.port)
        # The Monitor socket carries commands (poke the gate, read the clock,
        # save the capture); USART1's socket carries MIDI.  Two channels,
        # because the machine has to keep running while we talk to it.
        deadline = time.time() + 60
        while time.time() < deadline:
            try:
                self.mon = socket.create_connection(("127.0.0.1", self.monitor_port), timeout=5)
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.5)
        else:
            raise RuntimeError("no Monitor on port %d" % self.monitor_port)
        self.mon.settimeout(30)
        self._drain_monitor()
        if self.verbose:
            print("USART1 on %d, Monitor on %d" % (self.port, self.monitor_port),
                  file=sys.stderr)

    # -- attaching to a machine somebody else started ----------------------
    SESSION = os.path.join(tempfile.gettempdir(), "mfemu-session.json")

    def write_session(self, path=None):
        """Record where this machine's channels are, for a client to attach.

        The three endpoints a client needs are not interchangeable: MIDI goes
        OUT over the USART1 socket (or over USB through the Monitor), and
        replies come back through the USB capture FILE -- the device answers
        SysEx over USB, not over DIN. A client that knew only the socket would
        send fine and never hear anything.
        """
        import json
        path = path or self.SESSION
        with open(path, "w") as fh:
            json.dump({"midi_port": self.port, "monitor_port": self.monitor_port,
                       "usb_path": self.usb_path, "log_path": self.log_path,
                       "bus_path": self.bus_path,
                       "pid": self.proc.pid if self.proc else None,
                       "usb_attached": self.usb_attached}, fh, indent=2)
        return path

    def release(self):
        """Drop our own channels but leave the machine running.

        `mfemu serve` calls this after booting. Renode's socket terminal and
        its Monitor each take ONE client, so a server that kept holding them
        would boot a machine nobody else could talk to -- measured: the client
        attached, sent its request and timed out waiting for a reply that had
        nowhere to come from. The USB capture is written by Renode itself, so
        closing our handle on it costs nothing.
        """
        if self._usb_file:
            self._usb_file.close()
            self._usb_file = None
        if self.mon:
            self.mon.close()
            self.mon = None
        if self.sock:
            self.sock.close()
            self.sock = None

    @classmethod
    def attach(cls, session=None, timeout=120.0, verbose=False):
        """-> an EmulatedSynth bound to a machine that is already running.

        `session` is the file `write_session` wrote, or None for the default
        location. Closing an attached synth drops the sockets and leaves the
        machine alone -- it belongs to whoever started it.
        """
        import json
        path = session or cls.SESSION
        if not os.path.exists(path):
            raise SystemExit(
                "no emulator session at %s.\n"
                "  Start one with `mfemu serve`." % path)
        with open(path) as fh:
            info = json.load(fh)
        self = cls(port=info["midi_port"], monitor_port=info["monitor_port"],
                   timeout=timeout, verbose=verbose)
        self.usb_path = info["usb_path"]
        self.bus_path = info.get("bus_path")
        # Start reading replies at the END of the capture, not its start.
        # `usbfs StreamTo` appends for as long as the machine lives, so a
        # client attaching to a long-running `mfemu serve` finds every reply
        # the machine has EVER sent sitting in front of it. That is not
        # cosmetic: each tool invocation builds a fresh `checksum_free_seq()`,
        # so its sequence numbers repeat the previous invocation's, and a
        # predicate matching on `m[6] == seq` accepts the OLD reply.
        #
        # Measured 2026-10-15 against one `mfemu serve --preset 7=Speech`: the
        # first `mfreak preset read 7` answered 'Speech' correctly, and then
        # slots 8 and 200 -- both empty -- also answered 'Speech', because all
        # three were replaying the first read's bytes. Reading an empty slot
        # first poisoned it the other way: every later read, slot 7 included,
        # returned the power-on buffer's 'Init' and an erased page. Nothing in
        # the tools or the firmware was wrong; the transport was handing out
        # history. No test caught it because each test boots its own machine,
        # where the capture file starts empty.
        try:
            self._usb_start = os.path.getsize(self.usb_path)
        except (IOError, OSError):
            self._usb_start = 0
        self.log_path = info.get("log_path")
        self.usb_attached = bool(info.get("usb_attached"))
        self.send_over_usb = self.usb_attached
        self._attached = True
        self._connect()
        return self

    # Renode's Monitor prints this once a command is done and it wants the
    # next one. Reading up to it is the difference between a command costing
    # what it costs and a command costing 0.4 s.
    PROMPT = re.compile(r"\((?:[A-Za-z0-9_.-]+|monitor)\)\s*$")

    def _drain_monitor(self, quiet=0.4, limit=None):
        """Read the Monitor's answer, stopping at its prompt.

        It used to wait for a 0.4 s silence after every command. That is
        invisible when a run issues a handful of commands, and it was most of
        the cost of talking to the instrument: a 146-chunk preset read issues
        about 150 Monitor commands, one per `usbfs Send`, so the read took
        60 s to do 2 s of work. Renode prints a prompt when a command is done,
        which is a positive end-of-answer signal, so there was never anything
        to wait for.

        The quiet-wait is still the fallback: a command that prints no prompt
        (or a prompt this regex does not recognise) returns on `quiet` as
        before, so the worst case is the old behaviour rather than a hang.
        """
        text = ""
        deadline = time.time() + (limit if limit is not None else 30.0)
        self.mon.settimeout(quiet)
        try:
            while time.time() < deadline:
                chunk = self.mon.recv(4096)
                if not chunk:
                    break
                text += chunk.decode("utf-8", "replace")
                # The END of everything received so far, not the last chunk or
                # two: the answer arrives in however many pieces TCP feels
                # like, and a prompt split across a boundary -- or simply
                # preceded by more chunks than were being inspected -- was
                # missed, costing the full `quiet` fallback. Measured
                # 2026-10-16: checking only the last two chunks averaged
                # 164 ms per `RunFor "0.010"` where the work is 35 ms,
                # because some fraction of calls timed out instead of seeing
                # the prompt that had already arrived. Checking the whole tail
                # brought the same command to a few ms over its emulation.
                if self.PROMPT.search(text[-80:]):
                    break
        except (socket.timeout, TimeoutError):
            pass
        self.mon.settimeout(30)
        return text

    def close(self):
        if self._usb_file:
            self._usb_file.close()
            self._usb_file = None
        if self.mon:
            self.mon.close()
            self.mon = None
        if self.sock:
            self.sock.close()
            self.sock = None
        if self._attached:
            return                        # not ours to stop
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._log_file:
            self._log_file.close()
            self._log_file = None

    # -- the two methods the protocol tools need ---------------------------
    def send(self, message):
        """message: an iterable of MIDI bytes, SysEx included.

        DIN by default, only because it is the older of the two paths and
        every existing script assumes it.

        `send_over_usb` routes requests to the MIDI OUT endpoint instead, and
        that now works end to end: a note sent this way renders audio
        byte-identical to the same note over DIN, and `preset read` reads a
        whole preset with every request going out over USB.  What blocked it
        was in the OTG_FS model, not the firmware -- DOEPINT2 was not
        write-1-to-clear, so clearing XFRC never took and the device span in
        its USB interrupt instead of returning to the main loop.  See NOTES.md.

        Preset *writes* work too, as of 2026-10-16 -- this comment used to say
        they failed in the firmware's own SysEx dispatch, and that is no
        longer true. Against `mfemu serve`:

            mfreak preset write 20 --file init-patch.mfpz --name PROBE --yes
            -> wrote name record, 146 chunks, 4672 bytes
            -> verify OK: read-back matches exactly

        and the bytes are really in the emulated flash, not just the
        firmware's buffer: dumping the QUADSPI image afterwards shows slot 20
        region 0 reading `3PROBE` with the slot field 0x14. The firmware wrote
        its own flash through its own handler.

        Budget for it, though: 146 chunks each wait for an ack, and at this
        harness's 3.45x that is about 0.8 s of wall clock per round trip --
        121 s for one preset write, 60 s for a read. Faithful, and the right
        way to load a preset into a machine that is already running, but an
        order of magnitude dearer than the 12 s a warm render costs. For bulk
        work, put many presets in one flash image instead.
        """
        data = bytes(bytearray(message))
        if self.send_over_usb and self.usb_attached:
            self.monitor('usbfs Send "%s"' % " ".join("%02X" % b for b in data))
            return
        total = 0
        while total < len(data):
            try:
                total += self.sock.send(data[total:])
            except BlockingIOError:
                time.sleep(0.01)

    def recv(self, predicate, timeout=None):
        """Wait for a SysEx message satisfying `predicate`, as the real
        transport's `microfreak.midi.Synth.recv` does.

        Non-SysEx bytes are discarded: the firmware also emits active sensing
        and clock, and the protocol tools only ever match on SysEx.

        `timeout` is wall-clock seconds.
        """
        deadline = time.time() + (timeout if timeout is not None else self.timeout)
        while True:
            msg = self._next_sysex(deadline)
            if msg is None:
                raise TimeoutError("no matching reply from the emulated synth")
            if predicate(msg):
                return msg

    def drain(self):
        """Discard anything already buffered. -> how many SysEx were dropped.

        Mirrors `microfreak.midi.Synth.drain`, and exists for the same
        caller: a command that matches replies by CONTENT rather than by
        sequence number -- `mfreak config read`, because the device answers
        config requests with its own seq and not ours -- must begin each
        request with nothing stale queued. Without it that command reached
        into `Synth.inbox` directly and could not run against the emulator
        at all.
        """
        self._drain_usb()
        dropped = 0
        while True:                     # take what is here, wait for nothing
            msg = None
            for buf in (self.usb_buf, self.buf):
                msg = self._take_sysex(buf)
                if msg is not None:
                    break
            if msg is None:
                return dropped
            dropped += 1

    def _drain_usb(self):
        """Pull anything new the firmware has sent on the USB MIDI endpoint.

        The device answers SysEx over USB, not over DIN -- that is the whole
        reason the OTG_FS model exists -- so a transport that only listened to
        the serial port would never see a reply.  Renode writes the capture to
        a file rather than through the Monitor, because Monitor replies are
        text and a long hex dump does not survive the round trip cleanly.
        """
        if self._usb_file is None:
            try:
                self._usb_file = open(self.usb_path, "rb")
                self._usb_file.seek(self._usb_start)
            except (IOError, OSError):
                return
        fresh = self._usb_file.read()
        if not fresh:
            return
        # A packet is four bytes; a partial one at the end waits for the rest.
        self._usb_partial += fresh
        whole = len(self._usb_partial) // 4 * 4
        if whole:
            self.usb_buf.extend(self.decode_usb_midi(self._usb_partial[:whole]))
            self._usb_partial = self._usb_partial[whole:]

    @staticmethod
    def _take_sysex(buf):
        """Pull one complete SysEx out of `buf`, or return None."""
        start = buf.find(0xF0)
        if start < 0:
            # Drop it all. Nothing before a 0xF0 can belong to a SysEx, and
            # leaving it made the buffer grow without bound: the firmware
            # sends active sensing on DIN continuously (see _next_sysex), so
            # a long-running `mfemu serve` accumulated every one of those
            # bytes forever.
            del buf[:]
            return None
        end = buf.find(0xF7, start)
        if end < 0:
            del buf[:start]                   # keep the partial message only
            return None
        msg = bytes(buf[start:end + 1])
        del buf[:end + 1]
        return msg

    def _next_sysex(self, deadline):
        """The next SysEx from either transport.

        The two are buffered separately and both are drained every pass.  An
        earlier version read the DIN socket and only fell through to USB when
        that socket had nothing -- but the firmware sends active sensing on
        DIN continuously, so the socket rarely has nothing and replies waiting
        on USB could go unread until the caller timed out.  That was the
        intermittent lost reply in the middle of a preset read.  Keeping the
        buffers apart matters too: DIN carries real-time bytes that would
        otherwise land inside a USB SysEx and corrupt its framing.
        """
        while True:
            for buf in (self.usb_buf, self.buf):
                msg = self._take_sysex(buf)
                if msg is not None:
                    return msg
            if time.time() > deadline:
                return None
            moved = False
            try:
                chunk = self.sock.recv(4096)
                if chunk:
                    self.buf.extend(chunk)
                    moved = True
            except BlockingIOError:
                pass
            before = len(self.usb_buf)
            self._drain_usb()
            moved = moved or len(self.usb_buf) != before
            if not moved:
                time.sleep(0.005)

    # -- convenience -------------------------------------------------------
    def attach_usb(self, settle=25.0):
        """Enumerate the USB device, so the firmware will answer over it.

        `settle` is now a LIMIT, not a duration. This used to sleep for the
        whole of it, and that one `time.sleep(25)` was most of what `mfemu
        serve` cost: 25 s of a 32 s startup, spent waiting for something that
        had already happened. The model reports enumeration through
        `HostState`, which reaches `Done` when the last descriptor request
        completes, so there is a condition to test instead of a duration to
        guess.

        Measured 2026-10-16, from `usbfs Attach`:

            HostState Done              0.07 s
            first SysEx answered        0.02 s after Done, on the FIRST try
            serve startup               32.4 s -> 7.2 s

        The 0.02 s matters as much as the 0.07 s. The risk in replacing a
        sleep with a condition is that the condition is weaker than the wait
        -- "the host finished enumerating" need not mean "the device will
        answer" -- so that was measured too, by hammering requests from the
        instant `Done` appeared. The first one succeeded, so the firmware is
        ready as soon as its host is.
        """
        self.monitor("usbfs StreamTo @%s" % PATHS.resc(self.usb_path))
        self.monitor("usbfs Attach")
        deadline = time.time() + settle
        state = ""
        while time.time() < deadline:
            state = self.monitor("usbfs HostState")
            if "Done" in state:
                break
            time.sleep(0.02)
        else:
            # Proceed anyway, as the sleeping version did -- a caller's first
            # request will fail far more informatively than a raise here, and
            # the DFU path legitimately attaches without reaching Done.
            print("usb: enumeration did not report Done within %.1f s; "
                  "continuing, but replies may not arrive" % settle,
                  file=sys.stderr)
        self.usb_attached = True
        return state

    def boot(self, seconds=BOOT_SECONDS):
        """Let the firmware come up.

        MIDI reaches the firmware because the flash image's settings page sets
        the receive channel to omni (mkflash.SETTINGS_BASE). Nothing in the
        firmware's own memory is patched.
        """
        # Renode's own virtual time, not the firmware's tick counter. The
        # counter was the obvious clock to use and its scale is not fixed: the
        # firmware reprograms SysTick during start-up, from a reload of 18,000
        # to 1,600, so the same counter means tenths of a millisecond early on
        # and 8.89 us later. Anything dividing it by a constant is right for
        # one phase of the boot and wrong for the other, which is how
        # `seconds=14` came to mean 1.24.
        #
        # `emulation GetTimeSourceInfo` is the harness's own measure, exact
        # and independent of what the firmware does to its timers.
        deadline = time.time() + max(60.0, seconds * 60)
        while time.time() < deadline:
            if (self.virtual_time() or 0.0) >= seconds:
                break
            time.sleep(0.05)

    def virtual_time(self):
        """-> emulated seconds since the machine started, or None.

        The authority on how much time the machine thinks has passed. Host
        time and virtual time differ by this harness's slowdown (about 3.45x,
        and `GetTimeSourceInfo` reports it as "Current load"), so neither one
        substitutes for the other.
        """
        text = self.monitor("emulation GetTimeSourceInfo")
        m = re.search(r"Elapsed Virtual Time:\s+(\d+):(\d+):([\d.]+)", text)
        if not m:
            return None
        hours, minutes, secs = m.groups()
        return int(hours) * 3600 + int(minutes) * 60 + float(secs)

    def monitor(self, command):
        """Run a Monitor command while the machine keeps running.

        Anything still queued is discarded first, and that is not tidiness.
        `_drain_monitor` stops at the Monitor's prompt, so a prompt left over
        from an earlier command satisfies it instantly and the caller gets the
        PREVIOUS command's output. `monitor_read` then returns the previous
        command's value, which is a stale answer wearing a fresh command's
        clothes -- it showed up as a speech encoder reporting num_words 926
        where the firmware holds 22. Nothing left in the socket can be
        anything but a finished command's tail, because every command here is
        read to its prompt before the next one is sent, so dropping it is
        always right.

        It is also intermittent, which is why it survived two full passes of
        the suite before anything failed: it needs the leftover to still be in
        flight when the next command goes out.
        """
        self.mon.settimeout(0)
        try:
            while True:
                if not self.mon.recv(65536):
                    break
        except (BlockingIOError, socket.timeout, TimeoutError):
            pass
        finally:
            self.mon.settimeout(30)
        self.mon.sendall((command + "\n").encode())
        return self._drain_monitor()

    def monitor_read(self, command):
        """Run a Monitor command and return the value it printed.

        The Monitor echoes the command before answering, so match only lines
        that are a bare value -- otherwise the address in the command itself
        comes back as the answer, which is a silent and very confusing bug.
        """
        text = self.monitor(command)
        value = None
        for line in text.splitlines():
            line = line.strip()
            if re.fullmatch(r"0x[0-9A-Fa-f]+", line):
                value = int(line, 16)
            elif re.fullmatch(r"\d+", line):
                value = int(line)
        return value


def main():
    print(__doc__.strip().splitlines()[0])
    print("\nThis module is a library; see NOTES.md for the worked example.")


if __name__ == "__main__":
    main()
