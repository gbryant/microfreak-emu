#!/usr/bin/env python3
"""Render MicroFreak presets to .wav files.

    mfemu render --list --grep bass
    mfemu render --preset Buzzsaw --notes 60,67 --out buzz.wav
    mfemu render --preset 71 --midi ode_to_joy --out out.wav
    mfemu render --preset 1 --preset 10 --midi p.mid --outdir renders/
    mfemu render --preset 5=yours.mfpz --notes 60 --out a.wav

Presets come from a preset export. If data/presets/ holds exactly one, it is
the default; otherwise pass --source. A preset given as N=FILE.mfpz needs
no export.

The first render boots the firmware and takes about a minute. It saves a
snapshot, so later renders start in about a second. `mfemu warm` manages
the snapshots.

The output includes a model of the analog filter, set from each preset.
--no-filter leaves it out.

The instrument has four voices, so dense MIDI drops notes as the hardware
would. For a multi-channel file, pick one with --channel.
"""
import argparse
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave

# The Renode assets (boot.resc, the machine description, the peripherals)
# are NOT beside this file: they are not Python, so they live in renode/ at
# the repository root. Computed here rather than taken from PATHS because
# this runs above the import block.
_REPO_ROOT = os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))
_HERE = os.path.join(_REPO_ROOT, "renode")
_REPO = _REPO_ROOT
import mfemu.image as image                                           # noqa: E402
import mfemu.midifile as midifile                                                # noqa: E402
import mfemu.paths as PATHS                                          # noqa: E402
import mfemu.startup as startup                                      # noqa: E402
import mfemu.presetlib as presetlib                                               # noqa: E402
import mfemu.panel as panel

RATE = 96000
CHANNELS = 2
DEFAULT_RENODE = PATHS.RENODE
BOOT_SECONDS = 14.0           # main loop is up at ~9.9 s, synthesis resumes ~11.5 s
SETTLE_SECONDS = 0.5          # after a program change, before the first note
USB_ENUM_SECONDS = 3.0        # after `usbfs Attach`, for the host state machine
                              # to walk the enumeration sequence to Configured
MAX_PRESETS = 512
BANK_SIZE = 128               # a Program Change data byte is 7-bit
MAX_SLOT = 511                # four banks of 128
# Bank Select: CC#0 (MSB) picks the bank; CC#32 (LSB) is IGNORED by this
# firmware.  Measured 2026-09-10 by hooking the preset page read
# (FUN_080454D4) and reading the slot argument straight out of r1:
#     plain    PC 10  -> loads  10
#     CC0=1  + PC 10  -> loads 138   (1*128 + 10)
#     CC0=2  + PC 10  -> loads 266   (2*128 + 10)
#     CC32=1 + PC 10  -> loads  10   (no effect)
# Community sources say "CC#0 or CC#32"; on this firmware only CC#0 works.


def _sanitise(name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "preset"


def build_image(assignments, capture, work_dir, quiet=False, samples=None,
                wavetables=None, schemas=None):
    """Build (or reuse) a flash image with `assignments` = {slot: payload}.

    The base fill comes from the capture so every slot has a valid region 0;
    only the parameter regions of the named slots are replaced.  Images are
    cached by content, because a 16 MB rebuild per render would be wasteful
    and a batch usually reuses one image.

    `capture=None` builds without any donor: the bundle's wavetable bank and
    nothing else, every preset slot erased.  That is enough to boot.

    `schemas` = {slot: byte} sets those slots' region-0 schema byte (+8)
    instead of keeping the capture's.  **A payload read off a device needs
    this.**  The capture is an older vintage, and a payload parsed against the
    wrong schema renders as something else entirely -- on 2026-10-08 a device
    preset under a capture header read back as an empty WordBank at every
    Wave setting and made the emulator look incapable of speech for a day.
    A 5.0.0.2084 preset with a Voc module wants 0x33.  mkflash.py warns when
    the payload and the donor header disagree, so a silent repeat is unlikely.
    """
    sig = hashlib.sha1()
    # The LAYOUT version, bumped whenever the bytes this writes change for the
    # same inputs. The signature is over inputs -- the donor bank, the
    # assignments, the schema overrides -- so without this a change to how
    # region 0 is built produces the same filename as before and the stale
    # image is reused. That is exactly what happened on 2026-10-16: fixing
    # region 0 +6 for banks 1-3 left the cached image untouched, same name,
    # different contents, and a render would have quietly used the old bytes.
    sig.update(b"layout=4|")
    if capture is None:
        # No donor at all: every preset slot stays erased, which the firmware
        # reads as "slot empty" and ignores (see mkflash.py). The image still
        # carries the bundle's own wavetable bank, and it boots -- which is
        # what the harness tests want, because `captures/` is not in this
        # repository and nothing about the emulator needs it.
        sig.update(b"no-capture|")
    else:
        st = os.stat(capture)
        sig.update(("%s|%d|%d|" % (os.path.abspath(capture), st.st_size,
                                   st.st_mtime_ns)).encode())
    for extra in (samples, wavetables):
        if extra:
            est = os.stat(extra)
            sig.update(("%s|%d|%d|" % (os.path.abspath(extra), est.st_size,
                                       est.st_mtime_ns)).encode())
    for slot in sorted(assignments):
        sig.update(b"%d:" % slot)
        sig.update(hashlib.sha1(assignments[slot]).digest())
    # part of the identity: the same payload under a different schema byte is a
    # different image, and caching them together would serve a stale render
    for slot in sorted(schemas or {}):
        sig.update(b"s%d=%d:" % (slot, schemas[slot]))
    path = os.path.join(work_dir, "flash-%s.img" % sig.hexdigest()[:16])
    if os.path.exists(path):
        return path

    cmd = [sys.executable, "-m", "mfemu.mkflash", "-o", path]
    if capture is not None:
        cmd += ["--presets-from", capture, "--max-presets", str(MAX_PRESETS)]
    # Always explicit, "" for none. mkflash defaults --samples and
    # --wavetables to their drop folders, and the signature above only knows
    # what was passed in -- so leaving a flag off when its value is None would
    # let mkflash put an export in the image that the cache key never saw,
    # and a later render with a different folder would reuse the wrong image.
    cmd += ["--samples", samples or "", "--wavetables", wavetables or ""]
    tmp = []
    for slot, payload in sorted(assignments.items()):
        fh = tempfile.NamedTemporaryFile(dir=work_dir, suffix=".bin", delete=False)
        fh.write(payload)
        fh.close()
        tmp.append(fh.name)
        # mkflash's command line takes the number on the instrument, like
        # every other one (cd23585), and converts it to an index itself.
        # `assignments` and `schemas` are keyed by INDEX, so the hand-off has
        # to add the one back. It did not: `--preset 367=FILE` landed in index
        # 365 -- panel 366 -- while the render selected 367 and played the
        # bank's own preset there. Every file placement since the panel change
        # rendered the wrong preset, silently: a placement at an empty slot
        # played silence, and one at a populated slot played its neighbour's
        # bank preset. Found 2026-10-20 when three different probe files
        # produced byte-identical renders.
        cmd += ["--preset", "%d=%s" % (slot + 1, fh.name)]
        if schemas and slot in schemas:
            cmd += ["--preset-schema", "%d=0x%02X" % (slot + 1, schemas[slot])]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True)
        if out.returncode != 0:
            raise RuntimeError("mkflash.py failed:\n%s%s" % (out.stdout, out.stderr))
        if not quiet:
            print("built flash image (%d slot overrides)" % len(assignments))
    finally:
        for t in tmp:
            os.unlink(t)
    return path


# TIM8's modelled clock. The platform declares 10 MHz; the RCC the firmware
# actually programs implies 90-180 MHz. Both are wrong, and the choice between
# them is not about faithfulness but about whether bandpass presets render at
# all: in BPF the firmware enables TIM8's compare interrupts and services a
# soft PWM every 10-20 ticks, which at 10 MHz is ~1 MHz of interrupts and about
# 4000x slower than real time. At 100 kHz it is ~10 kHz of interrupts and
# renders in 22 s.
#
# Every instruction still executes, in the same order, with the same values --
# only the interval between soft-PWM ticks changes, and the GPIO that tick
# drives is the analog filter's control line, which is outside the emulated
# domain and is modelled by filter_model.py instead.
#
# The control: the same LPF preset rendered at 10 MHz and at 100 kHz is
# **byte-identical** (sha1 6d57eea215b9233d both), so TIM8's rate has no
# influence on the audio path at all. Pass --timer8-hz 10000000 to restore the
# platform's value and watch BPF stop rendering.
TIMER8_HZ = 100000


def bootloader_lines(path):
    """-> the Renode lines that boot from the bootloader instead of the app.

    Shared with midilink.py, because getting this wrong is subtle in two ways
    that both read as the bootloader crashing:

      * the image goes in through the flash controller, NOT `sysbus
        LoadBinary`, which re-applies its binary on every machine reset -- and
        a firmware update ends in SYSRESETREQ, so the reset put the original
        factory data page back and the update's own write vanished.
      * no `cpu PC`. Renode takes the initial SP and PC from the vector table
        that VectorTableOffset names, as the hardware does. Pointing PC at the
        table's own address executes the vector WORDS as instructions.

    The copy at 0x00000000 is the boot alias the F446 provides: after a reset
    the core fetches from VTOR's reset value, which is 0. LoadBinary is right
    for that one -- it is read-only and restoring it is harmless.
    """
    # Absolute, because Renode runs with cwd=<its install dir> -- see
    # paths.resc. `--bootloader ./data/...` otherwise sent Renode looking
    # under /Applications/Renode.app/... and it exited before the socket.
    path = PATHS.resc(path)
    return ["flash_controller LoadImage @%s 0x%08X"
            % (path, PATHS.BOOTLOADER_BASE),
            "sysbus LoadBinary @%s 0x00000000" % path,
            "cpu VectorTableOffset 0x%08X" % PATHS.BOOTLOADER_BASE]


# --- the cutoff the firmware actually asks for ----------------------------
#
# The analog filter is outside the emulated domain, but its CONTROL is not:
# the firmware computes the cutoff -- knob plus every mod-matrix source routed
# to it -- and writes it out as a 16-bit value split across two 8-bit PWM
# channels, TIM3 CCR4 (high byte) and CCR3 (low byte), in FUN_08023368 via
# the per-channel output curve FUN_08023504 (channel 1). On the board an RC
# network smooths that PWM into the filter's control voltage.
#
# Identified 2026-10-20 by elimination, then confirmed: Sausage Bass at knob
# 45.4 writes 29746 (45.39% of 65535) and nothing else; the same preset at
# knob 80 writes 52428 (80.00%); with its cycling envelope routed to cutoff
# at 38.4 it alternates 45.4 <-> 83.5 every control tick, a mean of 64.4 --
# +24.4 semitones, against +18 to +33 measured on the instrument. The DAC
# channel and TIM14 did not move with cutoff at all.
#
# Watchpoints, not a hook: the store is in a leaf the hook-at-TB-boundary
# trap in METHOD.md applies to, and the TIM3 page is quiet -- a couple of
# thousand writes per emulated second.
CV_HI, CV_LO = 0x40000440, 0x4000043C
CV_T0_MARK = "MFCV-T0"
CPU_HZ = 180e6                  # ExecutedInstructions per emulated second


def cutoff_cv_watch_lines():
    """Renode lines that log every write of the cutoff CV, timestamped."""
    out = []
    for addr, tag in ((CV_HI, "H"), (CV_LO, "L")):
        out.append("sysbus AddWatchpointHook 0x%08X 4 Write \"self.Log(LogLevel.Error, "
                   "'MFCV %s %%d %%d' %% (int(value), int(self.GetCurrentCPU()"
                   ".ExecutedInstructions)))\"" % (addr, tag))
    return out


def cutoff_cv(log_lines, frames, rate):
    """-> the cutoff CV over a job's capture, one value per sample (0-65535).

    `log_lines` is the job's slice of Renode's output. The CV is a sample-
    and-hold of the last complete high/low pair; before the capture starts it
    holds whatever the firmware last wrote, which is how a static preset gets
    its single value. None if the job wrote no CV at all -- the caller falls
    back to the preset's stored cutoff.
    """
    import numpy as np
    import re
    t0 = None
    hi = None
    writes = []
    for i, line in enumerate(log_lines):
        if CV_T0_MARK in line:
            for nxt in log_lines[i + 1:i + 6]:
                # The Monitor prints the count in hex: 0x000000009B917080.
                m = re.search(r"^\s*(0x[0-9A-Fa-f]+|\d+)\s*$", nxt)
                if m:
                    t0 = int(m.group(1), 0)
                    break
            continue
        m = re.search(r"MFCV ([HL]) (\d+) (\d+)", line)
        if not m:
            continue
        tag, value, when = m.group(1), int(m.group(2)), int(m.group(3))
        if tag == "H":
            hi = value
        elif hi is not None:
            writes.append((when, (hi << 8) | (value & 0xFF)))
            hi = None
    if t0 is None or not writes:
        return None
    cv = np.empty(frames)
    before = [v for w, v in writes if w <= t0]
    current = before[-1] if before else writes[0][1]
    edges = [(int(round((w - t0) / CPU_HZ * rate)), v) for w, v in writes if w > t0]
    pos = 0
    for idx, value in edges:
        idx = min(max(idx, 0), frames)
        cv[pos:idx] = current
        pos = max(pos, idx)
        current = value
    cv[pos:] = current
    return cv


def build_script(jobs, image, events, boot, settle, tail, work_dir,
                 timer8_hz=TIMER8_HZ, hooks=(), firmware=None,
                 sysex_usb=False, bootloader=None, warm=None):
    """Generate the Renode script for a whole batch.

    One boot, then per job: program change, settle, clear the capture, feed
    the events at their own times, run out the tail, save.  Notes are released
    at the end of every job so a held note cannot bleed into the next preset.

    `warm` is a snapshot from `mfemu.warm`. Given one, the boot is replaced by
    a restore: the machine comes back already past the firmware's ~11.5
    emulated seconds of start-up. The image is still loaded afterwards, and
    the snapshot must have been warmed on that SAME image -- `mfemu.warm`
    keys on it, because start-up reads samples and wavetables from flash and a
    machine warmed without them renders audibly different audio. The audio is
    byte-identical to a cold boot when the key matches; see
    tests/test_warm_snapshot.py, which is the control for that claim.
    """
    if warm:
        import mfemu.warm as warmlib
        # No $fw, no boot.resc and no boot RunFor: the restored machine
        # already holds the firmware it was warmed with. The image goes on
        # AFTER the restore, which is sound because the firmware reads preset
        # pages from flash on demand rather than caching them at boot.
        lines = warmlib.restore_lines(warm) + [
            "quadspi LoadImage @%s" % PATHS.resc(image),
            "timer8 Frequency %d" % timer8_hz,
        ]
    else:
        # boot.resc takes its paths from these rather than hardcoding them, so
        # the project can live anywhere. See paths.py.
        lines = ["$mf = @%s" % _HERE,
                 # Not `or PATHS.MAIN_RAW` bare: that is None on a fresh
                 # clone and would be written into the .resc as the literal
                 # string "None", which Renode then fails to load with a
                 # message about a missing file called None.
                 "$fw = @%s" % PATHS.resc(_require_firmware(firmware)),
                 "\n".join(startup.resc_lines(_require_firmware(firmware))
                           + ["include @%s" % os.path.join(_HERE, "boot.resc")]),
                 "quadspi LoadImage @%s" % PATHS.resc(image),
                 "timer8 Frequency %d" % timer8_hz]
        if bootloader:
            # After the include, so this overrides boot.resc's entry point.
            # Only the cold path takes it: a warm snapshot already holds
            # whatever entry point it was warmed through, which is why the
            # bootloader is part of the snapshot's cache key.
            lines[3:3] = bootloader_lines(bootloader)

    # FUN_080454D4(obj, slot) is the preset page read.  It runs a handful of
    # times per program change, so hooking it is free, and it is the only way
    # to know the selection actually happened -- a program change that
    # silently fails renders whatever preset was already loaded, which is
    # indistinguishable from success in the audio alone.  Ask me how I know.
    #
    # Added after the restore as well as after a boot. Hooks are not carried
    # in a snapshot, and a warm run without this one reports no selections at
    # all, which reads exactly like the firmware ignoring every program
    # change.
    lines.append('cpu AddHook 0x080454D4 "self.Log(LogLevel.Error, '
                 '\'MFRENDER-LOADED %d\' '
                 '% int(self.GetRegister(1).RawValue))"')
    lines += cutoff_cv_watch_lines()
    # Tracing hooks, from --hook. Emitted before the boot RunFor so they are
    # armed for everything that follows.
    for spec in hooks:
        addr, _, label = spec.partition("=")
        addr = addr.strip(); label = (label or addr).strip()
        lines.append('cpu AddHook %s "self.Log(LogLevel.Error, '
                     '\'MFRENDER-HOOK %s\')"' % (addr, label))
    if not warm:
        lines.append('emulation RunFor "%.6f"' % boot)
    if sysex_usb:
        # STM32_OTGFS.Attach() is "plug the cable in: start enumeration. Call
        # after boot" -- nothing else calls it, because nothing used USB before.
        # Without it the device never reaches its configured state, so
        # DeliverPendingOut finds EPENA clear and every Send sits in pendingOut
        # undelivered. That is indistinguishable from the firmware ignoring the
        # message: the first attempt at this showed 0 dispatches and no error.
        # Debug logging makes the queue and the enumeration visible.
        lines += ["logLevel 0 usbfs",
                  "usbfs Attach",
                  'emulation RunFor "%.6f"' % USB_ENUM_SECONDS]
    for slot, label, base, raw_path in jobs:
        if not 0 <= slot <= MAX_SLOT:
            raise ValueError("slot %d is outside 0-%d" % (slot, MAX_SLOT))
        bank, program = divmod(slot, BANK_SIZE)
        # `base`, not `label`: base is the DEDUPLICATED name (used_names
        # appends -2, -3), and it is also the raw/wav filename. The marker
        # carried `label`, which two jobs in one batch can share, so the
        # load-verification below merged their results and could credit one
        # job with the other's load.
        lines += ['echo "MFRENDER-BEGIN %s"' % base,
                  # Bank Select MSB (CC#0), then the Program Change.
                  "usart1 WriteChar 0xB0",
                  "usart1 WriteChar 0x00",
                  "usart1 WriteChar 0x%02X" % bank,
                  "usart1 WriteChar 0xC0",
                  "usart1 WriteChar 0x%02X" % program,
                  'emulation RunFor "%.6f"' % settle,
                  "sai2 ClearCapture",
                  # When the capture starts, in the clock the CV writes are
                  # stamped with, so each write can be put on the audio's
                  # timeline. See cutoff_cv().
                  'echo "%s"' % CV_T0_MARK,
                  "cpu ExecutedInstructions"]
        now = 0.0
        if sysex_usb and any(m[0] == 0xF0 for _, m in events):
            # What state the USB device reached is the first thing to know when
            # a USB-driven run behaves oddly: a Send into an unconfigured
            # device is enqueued and never delivered, which looks exactly like
            # the firmware ignoring the message. `Dump` is the captured
            # device->host bytes, NOT the state -- these are the state.
            lines += ["usbfs HostState", "usbfs OutState", "usbfs Configured"]
        for when, msg in events:
            if when - now > 1e-6:
                lines.append('emulation RunFor "%.6f"' % (when - now))
                now = when
            if sysex_usb and msg[0] == 0xF0:
                # One Monitor call per message: SendBytes packetises it into
                # USB-MIDI 4-byte packets itself, because the code index number
                # depends on where the message ends.
                lines.append('usbfs Send "%s"'
                             % " ".join("%02X" % b for b in msg))
            else:
                lines += ["usart1 WriteChar 0x%02X" % b for b in msg]
        if tail > 0:
            lines.append('emulation RunFor "%.6f"' % tail)
        lines += ["sai2 SavePCM @%s" % PATHS.resc(raw_path),
                  'echo "MFRENDER-DONE %s"' % label]
        # All-notes-off by hand: the firmware need not implement CC 123, and a
        # note left sounding would carry into the next job's capture.
        for note in sorted({m[1] for _, m in events if m[0] & 0xF0 == 0x90}):
            lines += ["usart1 WriteChar 0x80", "usart1 WriteChar 0x%02X" % note,
                      "usart1 WriteChar 0x40"]
        lines.append('emulation RunFor "0.2"')
    path = os.path.join(work_dir, "mfrender.resc")
    open(path, "w").write("\n".join(lines) + "\n")
    return path


def _require_firmware(firmware):
    """-> a firmware path, or exit with the drop-folder message.

    `PATHS.MAIN_RAW` is None when `data/firmware/` is empty, and this is the
    one place that interpolates it into a Renode script rather than passing it
    to `image.resolve` (which has its own None handling).
    """
    path = firmware or PATHS.MAIN_RAW
    if path is None:
        PATHS.check(None)
    return path


def renode_command(renode_dir):
    """-> how to start Renode here, whichever way it was packaged.

    Up to 1.16 the macOS build was a mono application launched as
    `mono bin/Renode.exe`. From 1.17 it is a native `renode` binary. Both
    accept the same flags, so only the launch differs -- and hardcoding the
    older one meant a newer install failed with "no Renode at
    .../bin/Renode.exe", which reads like a missing install rather than a
    packaging change.

    Every invocation also carries `--config`, pointing at the file
    `paths.renode_config()` writes. That is not cosmetic: it switches on
    Renode's compiler cache, and without it no snapshot this project saves can
    be loaded by any later process. See that function for the full reason.
    """
    config = ["--config", PATHS.renode_config()]
    native = os.path.join(renode_dir, "renode")
    if os.path.exists(native):
        return [native] + config
    managed = os.path.join(renode_dir, "bin", "Renode.exe")
    if os.path.exists(managed):
        return ["mono", managed] + config
    raise RuntimeError(
        "no Renode in %s. Use --renode or set MF_RENODE." % renode_dir)


def run_renode(script, renode_dir, quiet=False, stall_timeout=0.0):
    """Run the script, echoing progress markers as they appear.

    `stall_timeout` kills the run if no job completes within that many
    seconds. A batch is one Renode process rendering every preset in turn, so
    without it a single candidate that wedges the firmware stalls the whole
    run -- which matters when the batch is a thousand generated presets and
    nobody is watching.
    """
    cmd = renode_command(renode_dir) + ["--disable-xwt", "--console", "--plain",
                                        script, "-e", "quit"]
    proc = subprocess.Popen(cmd, cwd=renode_dir, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    log = []
    progress = {"at": time.time(), "job": None, "stalled": False}
    boot_allowance = 300.0          # generous: the boot is ~85 s of wall clock

    def watchdog():
        while proc.poll() is None:
            time.sleep(5)
            # The boot runs before the first job begins and takes far longer
            # than a job does, so it gets its own allowance. Counting it
            # against the per-job timeout kills every batch whose timeout is
            # shorter than the boot -- "STALLED on None", which is the
            # watchdog eating the run it was meant to protect.
            limit = stall_timeout if progress["job"] else max(stall_timeout, boot_allowance)
            if stall_timeout and time.time() - progress["at"] > limit:
                progress["stalled"] = True
                print("  STALLED %s after %.0f s -- killing Renode"
                      % ("on " + progress["job"] if progress["job"]
                         else "during boot", limit), file=sys.stderr)
                proc.kill()
                return

    if stall_timeout:
        threading.Thread(target=watchdog, daemon=True).start()

    for line in proc.stdout:
        log.append(line)
        if "MFRENDER-BEGIN" in line:
            progress["at"] = time.time()
            progress["job"] = line.split("MFRENDER-BEGIN", 1)[1].strip()
            if not quiet:
                print("  rendering %s" % progress["job"], flush=True)
        elif "MFRENDER-DONE" in line:
            progress["at"] = time.time()
        elif "error executing" in line.lower():
            print("  renode: %s" % line.strip(), file=sys.stderr)
    proc.wait()
    if progress["stalled"]:
        log.append("MFRENDER-STALLED %s\n" % progress["job"])
    return "".join(log)


def preset_filter(payload):
    """-> (type, cutoff, resonance) from a preset's own fields, or None.

    The instrument's filter is AFTER the DAC, so these three values are the
    only part of it the firmware knows about -- the render cannot contain
    their effect, and `filter_model` is what applies it. Reading them here is
    what makes `--filter` render each preset through ITS filter rather than
    through one setting for a whole batch.

    `None` rather than a guess if the fields cannot be read: a preset that
    came from a file in an unexpected form should not silently get LPF 50.
    """
    try:
        import microfreak.preset.fields as PF
        import microfreak.preset.tlv as preset_tlv
        rows = PF.rows_from_payload(preset_tlv.encode_7to8(payload))
        cutoff = PF.raw_to_display(PF.get_field_raw(rows, PF.CONTROL["filter_cutoff"]))
        reso = PF.raw_to_display(PF.get_field_raw(rows,
                                                  PF.CONTROL["filter_resonance"]))
        field = PF.SWITCH["filter_type"]
        raw = PF.get_field_raw(rows, field)
        kind = min(field["values"], key=lambda v: abs(v["value"] - raw))["name"]
        return kind, cutoff, reso
    except Exception:
        return None


def apply_filter(wav_path, kind, cutoff, resonance, drive=0.0, cv=None):
    """Run a rendered .wav through the analog filter model, in place.

    -> (fc_hz, q, dc_before, dc_after).

    The DC offset needs no handling here, and an earlier version of this
    added a `--dc-block` flag that did nothing the model was not already
    doing. `filter_model.apply` is AC-coupled by design -- "like the output",
    its docstring says -- with a structural 5 Hz corner, because every analog
    output stage is and a hardware recording of this one sits at a DC of
    -0.00000. Measured here: the pre-filter render carries +0.053 of full
    scale and comes out at +0.001.

    That corner is the only place the model departs from a textbook
    second-order response, and only below the audible band: measured against
    the analytic magnitude at Q=0.707, it agrees to within 0.3% from 50 Hz
    up, is 0.894 at 10 Hz (exactly a 5 Hz single pole) and 0.196 at 1 Hz.
    """
    import numpy as np

    import mfemu.filter_model as FM

    with wave.open(wav_path, "rb") as w:
        fs, ch, n = w.getframerate(), w.getnchannels(), w.getnframes()
        data = np.frombuffer(w.readframes(n), "<i2")
    data = data.reshape(-1, ch).astype(np.float64) / 32768.0
    dc_before = float(data.mean())
    # The firmware's own cutoff CV when there is one -- knob plus modulation,
    # per sample -- and the stored knob value when the job wrote none.
    fc = FM.cv_to_fc(cv[:n], fs) if cv is not None else FM.cutoff_hz(cutoff)
    q = FM.resonance_q(resonance, kind)
    out = np.stack([FM.apply(data[:, c], kind, fc, fs, q, drive)
                    for c in range(ch)], axis=1)
    peak = float(np.abs(out).max())
    if peak > 1.0:
        out /= peak                       # resonance gain, not clipping
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(fs)
        w.writeframes((np.clip(out, -1, 1) * 32767).astype("<i2").tobytes())
    return fc, q, dc_before, float(out.mean())  # fc: Hz, or Hz per sample


def write_wav(raw_path, wav_path):
    pcm = open(raw_path, "rb").read()
    with wave.open(wav_path, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return len(pcm) // (2 * CHANNELS)


def _needs_library(spec):
    """True when resolving this --preset spec needs a preset export.

    `N=FILE` needs nothing: `presetlib.resolve` reads the file and the slot is
    given outright. Everything else does -- a slot number or a name has to be
    looked up, and a bare file path has to borrow a spare slot from the
    library because it has no slot of its own.
    """
    prefix, sep, path = spec.partition("=")
    return not (sep and prefix.isdigit() and os.path.exists(path))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", action="append", default=[], metavar="SPEC",
                    help="a name, a number (1-512), a .mfpz or payload file, "
                         "or N=FILE to put a file at preset N. Repeatable")
    ap.add_argument("--midi", metavar="FILE.mid",
                    help="a MIDI file to play. A bare name is looked up in "
                         "data/midi/ first")
    ap.add_argument("--notes", metavar="SPEC",
                    help='notes to play, e.g. "60,67" or "60:1.5,67:0.5" '
                         '(note:seconds)')
    ap.add_argument("--hold", action="store_true",
                    help="with --notes, hold every note to the end")
    ap.add_argument("--phrase", action="store_true",
                    help="play a standard test phrase: a held C3, then "
                         "C3 G3 D#4 A#3 (about 6 s)")
    ap.add_argument("--velocity", type=int, default=100,
                    help="note velocity (default: 100)")
    ap.add_argument("--channel", type=int,
                    help="with --midi, play only this channel (0-15)")
    ap.add_argument("--out", metavar="FILE.wav",
                    help="output file, for a single preset")
    ap.add_argument("--outdir", metavar="DIR",
                    help="output directory, for several presets")
    ap.add_argument("--tail", type=float, default=1.0,
                    help="seconds to keep recording after the last note "
                         "(default: 1)")
    ap.add_argument("--list", action="store_true",
                    help="list the presets in --source and stop")
    ap.add_argument("--grep", metavar="TEXT",
                    help="list only presets whose name contains TEXT")

    src = ap.add_argument_group("sources")
    src.add_argument("--source", default=PATHS.PRESET_FILE, metavar="FILE",
                     help="an MCC preset export (.mfprojz) or a decoded "
                          "capture (default: the one in data/presets/)")
    src.add_argument("--samples", metavar="FILE.mfsbz",
                     default=PATHS.SAMPLE_FILE,
                     help="an MCC sample export (default: the one in "
                          "data/samples/). Without one, the Sample and Grains "
                          "engines are silent")
    src.add_argument("--wavetables", metavar="FILE.mfwbz",
                     default=PATHS.WAVETABLE_FILE,
                     help="an MCC wavetable export (default: the one in "
                          "data/wavetables/, else the firmware's factory set)")
    src.add_argument("--firmware", metavar="FILE",
                     help="a .mff, headered .bin or bare payload (default: "
                          "the one in data/firmware/)")
    src.add_argument("--image", type=int, default=0, metavar="N",
                     help="which image inside a .mff (default: 0)")
    src.add_argument("--bootloader", metavar="BOOT.bin", nargs="?",
                     const=PATHS.BOOTLOADER, default=None,
                     help="boot through your own bootloader dump (default "
                          "file: data/bootloader/). See `mfreak fw "
                          "dump-bootloader`")

    out = ap.add_argument_group("output")
    out.add_argument("--no-filter", action="store_true",
                     help="skip the analog filter model and write the DAC "
                          "signal")
    out.add_argument("--save-cv", action="store_true",
                     help="also write FILE.cv.npy: the filter cutoff CV per "
                          "sample, 0-65535")
    out.add_argument("--keep-raw", action="store_true",
                     help="keep the raw capture beside each .wav")

    sx = ap.add_argument_group("sysex injection")
    sx.add_argument("--sysex", metavar="FILE",
                    help="raw SysEx messages (F0..F7) to send during the "
                         "render")
    sx.add_argument("--sysex-at", type=float, default=None, metavar="SECONDS",
                    help="when to send --sysex. For Speech, put it between "
                         "two notes")
    sx.add_argument("--sysex-spacing", type=float, default=0.02,
                    metavar="SECONDS",
                    help="gap between messages (default: 0.02)")
    sx.add_argument("--sysex-usb", action="store_true",
                    help="send --sysex over USB instead of MIDI DIN, as "
                         "mfreak does")

    dbg = ap.add_argument_group("debugging")
    dbg.add_argument("--warm", action=argparse.BooleanOptionalAction,
                     default=True,
                     help="start from a cached snapshot (default: on). Try "
                          "--no-warm if a render looks wrong")
    dbg.add_argument("--preset-schema", action="append", default=[],
                     metavar="N=BYTE",
                     help="override preset N's schema byte. A preset read off "
                          "a 5.0.0.2084 device needs 0x33")
    dbg.add_argument("--boot", type=float, default=BOOT_SECONDS,
                     help="emulated seconds to boot before rendering "
                          "(default: %.0f)" % BOOT_SECONDS)
    dbg.add_argument("--settle", type=float, default=SETTLE_SECONDS,
                     help="seconds between a program change and the first "
                          "note (default: %.1f)" % SETTLE_SECONDS)
    dbg.add_argument("--stall-timeout", type=float, default=180.0,
                     help="stop if no preset finishes in this many seconds; "
                          "0 disables (default: 180)")
    dbg.add_argument("--hook", action="append", default=[],
                     metavar="ADDR[=LABEL]",
                     help="log each time ADDR executes. Slow: avoid hot code")
    dbg.add_argument("--timer8-hz", type=int, default=TIMER8_HZ,
                     help="TIM8's modelled clock (default: %d)" % TIMER8_HZ)
    dbg.add_argument("--renode", default=DEFAULT_RENODE,
                     help="path to Renode")
    dbg.add_argument("--work-dir",
                     default=os.path.join(tempfile.gettempdir(), "mfrender"),
                     help="where temporary files go")
    dbg.add_argument("--dry-run", action="store_true",
                     help="print the Renode script and stop")
    args = ap.parse_args()
    for spec in args.preset or []:
        prefix, sep, path = spec.partition("=")
        if sep and prefix.isdigit():
            if not path.lower().endswith(".mfpz"):
                sys.exit("--preset %s: a preset file is an MCC .mfpz" % spec)
            if not os.path.exists(path):
                sys.exit("--preset %s: no such file" % spec)

    if not args.source:
        if args.list or args.grep:
            avail = PATHS.presets_available()
            if avail:
                sys.exit("--list and --grep need --source. %s holds %d preset "
                         "exports, so none is the default:\n%s\n\n"
                         "Name one, or move all but one into %s."
                         % (PATHS.PRESETS_DIR, len(avail),
                            "\n".join("  %s" % os.path.basename(b)
                                      for b in avail),
                            os.path.join(PATHS.PRESETS_DIR, "disabled")))
            sys.exit("--list and --grep need a preset export. Put one in %s,\n"
                     "or pass --source FILE. To get one:\n"
                     "  mfreak asset import --only presets --outdir %s\n"
                     "  mfreak preset backup mine.mfprojz"
                     % (PATHS.PRESETS_DIR, PATHS.PRESETS_DIR))
        need = [spec for spec in (args.preset or []) if _needs_library(spec)]
        avail = PATHS.presets_available()
        if not args.preset or need:
            sys.exit(
                "--source is required. To get a preset export:\n"
                "    mfreak asset import --only presets --outdir %s\n"
                "    mfreak preset backup mine.mfprojz\n"
                % PATHS.PRESETS_DIR
                + ("  Put one in %s and it becomes the default.\n"
                   % PATHS.PRESETS_DIR
                   if not avail else
                   "  %s holds %d preset exports, so none is the default. "
                   "Name one, or move\n  all but one into its disabled/ "
                   "folder:\n%s\n"
                   % (PATHS.PRESETS_DIR, len(avail),
                      "\n".join("    %s" % os.path.basename(b)
                                for b in avail)))
                + ("\n  These need a preset export: %s\n"
                   "  A preset supplied as a file in an explicit slot needs "
                   "no export at all:\n    mfemu render --preset 5=yours.mfpz "
                   "--notes 60 --out a.wav"
                   % ", ".join(repr(x) for x in need) if need else ""))
    lib = presetlib.library(args.source) if args.source else {}
    if args.list or (args.grep and not args.preset):
        for slot in sorted(lib):
            name = lib[slot][0]
            if args.grep and args.grep.lower() not in name.lower():
                continue
            print("%4d  %s" % (slot + 1, name))
        return 0

    if not args.preset:
        ap.error("give at least one --preset (or --list)")
    if sum(bool(v) for v in (args.midi, args.notes, args.phrase)) > 1:
        ap.error("--midi, --notes and --phrase are alternatives")
    if not args.midi and not args.notes and not args.phrase:
        args.notes = "60"
    if len(args.preset) > 1 and not args.outdir:
        ap.error("several presets need --outdir")
    if len(args.preset) == 1 and not args.out and not args.outdir:
        ap.error("give --out or --outdir")

    if args.phrase:
        events, _segments = midifile.standard_phrase(velocity=args.velocity)
    elif args.midi:
        args.midi = PATHS.find_midi(args.midi)
        events = midifile.read(args.midi, channel=args.channel)
    else:
        events = midifile.from_notes(args.notes, velocity=args.velocity,
                                     hold=args.hold)
    if not events:
        sys.exit("no MIDI events to play")
    if args.sysex:
        if args.sysex_at is None:
            ap.error("--sysex needs --sysex-at")
        blob = open(args.sysex, "rb").read()
        msgs, pos = [], 0
        while True:
            start = blob.find(b"\xF0", pos)
            if start < 0:
                break
            end = blob.find(b"\xF7", start)
            if end < 0:
                sys.exit("%s: unterminated SysEx at offset %d" % (args.sysex, start))
            msgs.append(blob[start:end + 1])
            pos = end + 1
        if not msgs:
            sys.exit("%s: no SysEx messages found" % args.sysex)
        # A SysEx message is just bytes to the event loop, and the
        # all-notes-off pass filters on 0x90, so it skips these.
        #
        # Space them out rather than dumping every message at one instant:
        # `usart1 WriteChar` delivers immediately, so a whole upload at a
        # single timestamp can outrun the firmware's UART receive buffer.
        for i, m in enumerate(msgs):
            events.append((args.sysex_at + i * args.sysex_spacing, m))
        events.sort(key=lambda e: e[0])
        span = (len(msgs) - 1) * args.sysex_spacing
        print("injecting %d SysEx messages (%d bytes) from t=%.3f s over %.3f s"
              % (len(msgs), sum(len(m) for m in msgs), args.sysex_at, span))
    length = max(t for t, _ in events) + args.tail

    # `--preset N=FILE` places an imported preset in a named slot instead of
    # letting it borrow one.  The survey wants its presets where the hardware
    # has them, so the emulator and the device can be compared slot for slot.
    specs = []
    for spec in args.preset:
        slot_prefix, sep, path = spec.partition("=")
        if sep and slot_prefix.isdigit() and os.path.exists(path):
            # N is the number on the instrument; everything below is an index.
            try:
                specs.append((path, panel.to_slot(slot_prefix)))
            except ValueError as exc:
                sys.exit("--preset %s: %s" % (spec, exc))
        else:
            specs.append((spec, None))
    # resolve() raises KeyError for a name it cannot find and ValueError for a
    # preset number outside 1-512. Both were reaching the terminal as a
    # traceback -- the KeyError since before the panel change, because
    # test_bad_input_is_refused only ever fed commands a missing FILE and no
    # case covered a bad --preset. Caught together.
    try:
        resolved = [presetlib.resolve(spec, lib) for spec, _ in specs]
    except (KeyError, ValueError) as exc:
        sys.exit("--preset: %s" % (exc.args[0] if exc.args else exc))

    # Presets already sitting in the base image are rendered where they are;
    # imported ones borrow a slot that this batch is not otherwise using, and
    # that slot's parameters are overwritten.  Borrowing keeps the slot's
    # region 0, so the firmware still sees a populated slot.
    native = {slot for _, _, slot in resolved if slot is not None}
    native |= {slot for _, slot in specs if slot is not None}
    # Every slot is selectable now (Bank Select MSB + Program Change), so a
    # preset renders where it already lives.  Borrowing is only needed for a
    # preset imported from a file, which has no slot of its own.
    spare = [s for s in sorted(lib, reverse=True) if s not in native]
    assignments, jobs = {}, []
    os.makedirs(args.work_dir, exist_ok=True)
    outdir = args.outdir or os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(outdir, exist_ok=True)

    used_names, settings = {}, {}
    placed_by = {}
    for (label, payload, slot), (spec, forced) in zip(resolved, specs):
        if forced is not None:
            slot = forced
            # Refuse a second --preset for the same slot instead of letting
            # the last one win. Both specs still become JOBS, so the dropped
            # one rendered the OTHER file's audio under its own name -- a
            # wrong result that looked like a successful render. The
            # --preset-schema path below already refuses its own version of
            # this; this path did not.
            if slot in placed_by and placed_by[slot] != spec:
                sys.exit("--preset %d given twice (%r and %r). Pick one, or "
                         "place the other in a free slot."
                         % (slot, placed_by[slot], spec))
            placed_by[slot] = spec
            assignments[slot] = payload
        elif slot is None:
            if not spare:
                sys.exit("no spare slot left to place %r" % spec)
            slot = spare.pop(0)
            assignments[slot] = payload
        base = _sanitise(label)
        used_names[base] = used_names.get(base, 0) + 1
        if used_names[base] > 1:
            base = "%s-%d" % (base, used_names[base])
        raw = os.path.join(args.work_dir, "%s.raw" % base)
        jobs.append((slot, label, base, raw))
        settings[base] = preset_filter(payload) if payload else None

    schemas = {}
    for spec in args.preset_schema:
        num, _, byte = spec.partition("=")
        if not byte:
            sys.exit("--preset-schema wants N=BYTE, got %r" % spec)
        try:
            schemas[panel.to_slot(num)] = int(byte, 0)
        except ValueError as exc:
            sys.exit("--preset-schema %s: %s" % (spec, exc))
    for slot in schemas:
        if slot not in assignments:
            sys.exit("--preset-schema %d given, but no preset is placed at "
                     "preset %d."
                     % (slot + 1, slot + 1))
    # `flash_image`, not `image`: this module imports `mfemu.image` under that
    # name, and a local called `image` shadowed it. The shadow was invisible
    # until someone passed --firmware, because the only use of the module here
    # sits behind `if args.firmware`, so `mfemu render --firmware X --source Y`
    # died with "'str' object has no attribute 'resolve'". Found 2026-10-16
    # while adding the warm-snapshot path.
    flash_image = build_image(assignments, args.source, args.work_dir,
                              quiet=args.dry_run, samples=args.samples,
                              wavetables=args.wavetables,
                              schemas=schemas or None)
    # Said every time, because the failure it heads off has no symptom but
    # silence: a Sample or Grains preset with no samples renders a flat-zero
    # WAV under a normal success line. That cost an evening on 2026-10-19,
    # with 'Kody' read as a broken emulator.
    if not args.dry_run:
        if args.samples:
            print("samples:    %s" % os.path.basename(args.samples))
        else:
            print(PATHS.drop_folder_warning("samples", args.samples) or
                  "samples:    none -- Sample and Grains presets will render "
                  "silent (see --samples)")
        print(PATHS.drop_folder_warning("wavetables", args.wavetables) or
              "wavetables: %s" % (os.path.basename(args.wavetables)
                                  if args.wavetables else
                                  "factory, from the firmware file"))
    # The generated script and the raw captures go in a directory of this run's
    # own. They used to share one path per work-dir, so two concurrent batches
    # silently overwrote each other's script mid-execution -- the symptom was
    # one run wedging with no error, which reads exactly like a preset that
    # hangs the firmware. The flash images stay shared: they are addressed by
    # content hash, so reuse is safe and saves rebuilding 16 MB.
    run_dir = os.path.join(args.work_dir, "run-%d" % os.getpid())
    os.makedirs(run_dir, exist_ok=True)
    jobs = [(slot, label, base, os.path.join(run_dir, os.path.basename(raw)))
            for slot, label, base, raw in jobs]
    # Resolve the DEFAULT too, not just an explicit --firmware. PATHS.MAIN_RAW
    # is whatever shape the person actually has -- paths.py searches for a bare
    # payload, then a headered .bin, then a .mff -- and its own comment says
    # "image.resolve() turns any of them into a bare payload ... so nothing
    # downstream has to care." This was the one path that did not call it: with
    # only the .mff unpacked, `$fw = @...mff` handed Renode a ZIP to LoadBinary,
    # which it does without complaint, so the CPU ran the zip's bytes. Worse on
    # the warm path, where those bytes are hashed into the snapshot key and the
    # poisoned snapshot is then reused by every later render.
    firmware = image.resolve(args.firmware or PATHS.MAIN_RAW, image=args.image)
    # A warm snapshot replaces the boot, which is the whole fixed cost of a
    # render: ~48 s of wall clock waiting out the firmware's own start-up,
    # against about a second to restore. Built on first use and then reused.
    # --no-warm forces the cold boot, which is what the control test needs and
    # what to reach for if a result ever looks wrong.
    warm = None
    if args.warm:
        import mfemu.warm as warmlib
        # flash_image is passed through: a snapshot warmed on a different
        # external flash restores into state the firmware never would have
        # reached, and the audio shows it.
        args_ = (firmware, args.bootloader, args.timer8_hz, args.renode,
                 warmlib.WARM_SECONDS, flash_image)
        if args.dry_run:
            # A dry run prints a script; it must not spend 50 s warming a
            # machine to do that. Show the path the script WOULD restore,
            # whether or not it is built yet.
            warm = (warmlib.ensure(*args_[:4], warm_seconds=args_[4],
                                   flash=args_[5], build_missing=False)
                    or warmlib.path(warmlib.key(*args_)))
        else:
            warm = warmlib.ensure(*args_[:4], warm_seconds=args_[4],
                                  flash=args_[5])
    script = build_script(jobs, flash_image, events, args.boot, args.settle,
                          args.tail, run_dir, timer8_hz=args.timer8_hz,
                          hooks=args.hook,
                          firmware=firmware,
                          bootloader=args.bootloader,
                          sysex_usb=args.sysex_usb,
                          warm=warm)
    if args.dry_run:
        print(open(script).read())
        return 0

    # 3.45 wall-seconds per emulated second, measured 2026-10-15 by timing
    # RunFor "2" against RunFor "20" so the shared boot cancels out. A warm
    # restore skips the boot term entirely and costs about a second.
    estimate = (1.0 if warm else args.boot * 3.6) \
        + len(jobs) * (args.settle + length) * 5.4
    print("%d preset(s), %.1f s of audio each; expect roughly %.0f s%s"
          % (len(jobs), length, estimate,
             "" if warm else "  (a warm snapshot would save ~%.0f s;"
                             " see `mfemu warm`)" % (args.boot * 3.6)),
          flush=True)
    log = run_renode(script, args.renode, stall_timeout=args.stall_timeout)

    # Check each job actually loaded its slot.  The markers interleave with
    # the hook's output, so walk the log once and attribute each load to the
    # job it followed.
    loaded = {}
    job_lines = {}
    current = None
    for line in log.splitlines():
        if current is not None:
            job_lines.setdefault(current, []).append(line)
        if "MFRENDER-BEGIN" in line:
            current = line.split("MFRENDER-BEGIN", 1)[1].strip()
        elif "MFRENDER-LOADED" in line and current is not None:
            loaded.setdefault(current, set()).add(
                int(line.split("MFRENDER-LOADED", 1)[1].strip()))

    failures = 0
    for slot, label, base, raw in jobs:
        # Keyed on `base` to match the marker; `label` is still what the
        # message shows, because that is the name the user typed.
        if slot not in loaded.get(base, set()):
            print("FAILED %s: the firmware never loaded slot %d (saw %s), "
                  "so the render would be the wrong preset."
                  % (label, slot, sorted(loaded.get(base, set())) or "nothing"),
                  file=sys.stderr)
            failures += 1
            continue
        if not os.path.exists(raw) or os.path.getsize(raw) == 0:
            print("FAILED %s (slot %d): no audio captured" % (label, slot), file=sys.stderr)
            failures += 1
            continue
        if args.out and len(jobs) == 1:
            wav = args.out
        else:
            wav = os.path.join(outdir, "%s.wav" % base)
        frames = write_wav(raw, wav)
        if not args.keep_raw:
            os.unlink(raw)
        note = ""
        got = settings.get(base)
        if args.save_cv:
            import numpy as np
            cv_trace = cutoff_cv(job_lines.get(base, []), frames, RATE)
            if cv_trace is not None:
                np.save(os.path.splitext(wav)[0] + ".cv.npy", cv_trace.astype(np.int32))
        if args.no_filter:
            note = "  (pre-filter: the signal as it leaves the DAC)"
        elif got is None:
            # No invented settings. There is nothing to override them with any
            # more -- the filter's position is the preset's business -- so a
            # preset whose fields cannot be read gets the pre-filter signal
            # and is told so, rather than a filter at some default the
            # instrument would never have been at.
            print("  warning: could not read %s's filter settings, so this "
                  "render is unfiltered." % label, file=sys.stderr)
            note = "  (pre-filter: its filter settings could not be read)"
        else:
            kind, cutoff, reso = got
            cv = cutoff_cv(job_lines.get(base, []), frames, RATE)
            fc, q, dc_in, dc_out = apply_filter(wav, kind, cutoff, reso, cv=cv)
            if cv is None:
                note = "  %s fc=%.0fHz Q=%.2f" % (kind, fc, q)
            else:
                lo_hz, hi_hz = float(fc.min()), float(fc.max())
                note = ("  %s fc=%.0fHz Q=%.2f" % (kind, lo_hz, q) if hi_hz - lo_hz < 1
                        else "  %s fc=%.0f-%.0fHz (modulated) Q=%.2f" % (kind, lo_hz, hi_hz, q))
            # From the model, not a copy of its numbers here: a
            # recalibration moves them and a duplicate would not follow.
            import mfemu.filter_model as _FM
            lo, hi = _FM.CUTOFF_FITTED_RANGE  # noqa: E501
            if not lo <= cutoff <= hi:
                note += " (knob %.0f is outside the fitted %g-%g, so fc is " \
                        "extrapolated)" % (cutoff, lo, hi)
            if abs(dc_in) > 0.01:
                note += "  DC %+.3f -> %+.3f (the model is AC-coupled, as " \
                        "the output is)" % (dc_in, dc_out)
        print("%-24s preset %-4d %7.2f s  %s%s"
              % (label, slot + 1, frames / RATE, wav, note))
    # Keep Renode's output when a render failed, and whenever --hook was used:
    # a hook whose output is thrown away on success is useless, which cost a
    # round of confused tracing on 2026-10-02.
    if failures or args.hook:
        logfile = os.path.join(args.work_dir, "mfrender.log")
        open(logfile, "w").write(log)
        if failures:
            print("%d render(s) failed; Renode output in %s" % (failures, logfile),
                  file=sys.stderr)
        else:
            print("Renode output (with hook hits) in %s" % logfile, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
