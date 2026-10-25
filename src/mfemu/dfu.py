#!/usr/bin/env python3
"""Drive the MicroFreak's firmware update against the EMULATED instrument.

The host side of the protocol in docs/FIRMWARE_UPDATE.md, spoken over the
Renode USB model's `ControlRequest` instead of a real USB stack. It is the
emulated counterpart of `mfreak fw flash`, and the point of it is that an
update can be rehearsed -- including the parts that erase the application --
without a flash cycle on anything physical.

    from mfemu.dfu import EmulatedDfu
    dfu = EmulatedDfu(synth)              # synth is an EmulatedSynth
    dfu.reboot_to_dfu()                   # 0xF2, handled by the APPLICATION
    dfu.attach()                          # re-enumerate against the bootloader
    dfu.select_target(0)                  # 0xF4: this also ERASES the target
    dfu.download(payload)                 # 0x01 in 64-byte blocks
    dfu.finish()                          # 0xF3

Everything after `reboot_to_dfu` is answered by the BOOTLOADER, so none of it
works unless the machine was started with one -- see `midilink.EmulatedSynth`
and `paths.BOOTLOADER`.

Two facts from the capture that shape this:

  * `wTransferSize` is 0x0040, so a download is 64-byte blocks with the block
    number in `wValue`, and the run ends with a ZERO-LENGTH block -- DFU 1.1's
    end-of-transfer marker, which is why the device's block counts are one
    more than the arithmetic suggests.
  * the erase happens INSIDE `ARTURIA_SELECT_TARGET`, not before the first
    block: 3.8 s of device time in one control transfer. Here it is however
    long the emulated erase takes, which is no time at all.
  * **a DFU_GETSTATUS has to follow every block.** DFU 1.1 leaves the device
    in dfuDNLOAD_SYNC after a block and the status poll is what moves it to
    dfuDNLOAD_IDLE; the capture shows two polls per block. Skipping them does
    not merely go faster -- the next block is refused and the download stops
    after the first one. `status_every=0` exists for experiments and is not a
    shortcut.

**The first block is the HEADER, not image data.** Measured 2026-10-15: send
two distinct blocks and the SECOND one lands at the image base, with the first
nowhere in flash. That matches the capture's arithmetic -- 7,404 blocks for a
473,760-byte `.mff` member, which is the 64-byte container header plus the
473,695-byte payload, padded -- so the bootloader consumes block 0 as the
header it validates and programs from block 1 onward. `download()` therefore
takes the whole container, header included, exactly as `mfreak fw flash` sends
it.
"""
import re
import time

# bmRequestType
OUT_CLASS_IFACE = 0x21          # host-to-device, class, interface
IN_CLASS_IFACE = 0xA1           # device-to-host, class, interface

# bRequest
DFU_DNLOAD = 0x01
DFU_GETSTATUS = 0x03
DFU_GETSTATE = 0x05
ARTURIA_REBOOT_TO_DFU = 0xF2
ARTURIA_FINISH = 0xF3
ARTURIA_SELECT_TARGET = 0xF4

TRANSFER_SIZE = 0x40

# DFU 1.1 bState values, for legible failures.
STATES = {0: "appIDLE", 1: "appDETACH", 2: "dfuIDLE", 3: "dfuDNLOAD_SYNC",
          4: "dfuDNBUSY", 5: "dfuDNLOAD_IDLE", 6: "dfuMANIFEST_SYNC",
          7: "dfuMANIFEST", 8: "dfuMANIFEST_WAIT_RESET",
          9: "dfuUPLOAD_IDLE", 10: "dfuERROR"}


def _value(text):
    """-> the value a Monitor command printed.

    The transcript is the echoed command, then the answer, then the `(machine)`
    prompt, and reading the last line gives the prompt. This was a real bug:
    `usbfs HostState` came back as "(microfreak)" and failed a working
    enumeration.
    """
    for line in reversed([l.strip() for l in (text or "").splitlines()]):
        if not line or line.startswith("(") or "ControlRequest" in line:
            continue
        if line.startswith("usbfs ") or line.startswith("sysbus "):
            continue
        return line
    return None


class DfuError(RuntimeError):
    pass


class EmulatedDfu:
    def __init__(self, synth, timeout=30.0, verbose=True):
        self.synth = synth
        self.timeout = timeout
        self.verbose = verbose

    # -- plumbing ----------------------------------------------------------
    def _say(self, msg):
        if self.verbose:
            print("    dfu: %s" % msg)

    def _done_count(self, tries=6):
        """-> how many control transfers have completed, or None.

        Through `monitor_read`, which matches a bare numeric line and is the
        helper the rest of this repository uses, rather than hand-parsing the
        transcript. And retried: the Monitor drain gives up after a quiet
        period, so a busy machine sometimes returns the echo alone and the
        answer lands on the next read. Getting this wrong made every transfer
        look like a timeout while `ControlStatus` plainly said `ok 0xF4` -- and
        the transfers themselves had worked.
        """
        for _ in range(tries):
            v = self.synth.monitor_read("usbfs ControlDone")
            if v is not None:
                return v
            time.sleep(0.2)
        return None

    def control(self, request_type, request, value=0, index=0, length=0,
                data=b"", wait=True):
        """One control transfer. -> the device's data-stage reply, as bytes.

        Completion is counted rather than asked about: `ControlDone` before and
        after. Do NOT read success into it on its own -- a request the device
        stalls is counted too, measured 2026-10-15, so anything that matters is
        checked by its effect.
        """
        before = self._done_count()
        if before is None:
            raise DfuError("the USB model's ControlDone counter is unreadable; "
                           "cannot tell a completed transfer from a lost one")
        hexed = " ".join("%02X" % b for b in data)
        self.synth.monitor('usbfs ControlRequest 0x%02X 0x%02X %d %d %d "%s"'
                           % (request_type, request, value, index,
                              length or len(data), hexed))
        if not wait:
            return b""
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            now = self._done_count()
            if now is not None and now > before:
                break
            time.sleep(0.2)
        else:
            raise DfuError("control request 0x%02X never completed in %.0f s "
                           "(ControlStatus: %s)"
                           % (request, self.timeout,
                              _value(self.synth.monitor("usbfs ControlStatus"))))
        if request_type & 0x80:
            reply = _value(self.synth.monitor("usbfs LastControlReply")) or ""
            return bytes(int(b, 16) for b in reply.split() if re.fullmatch(
                r"[0-9A-Fa-f]{2}", b))
        return b""

    # -- the protocol ------------------------------------------------------
    def reboot_to_dfu(self):
        """0xF2. Answered by the running APPLICATION, which stamps the factory
        data page and resets into the bootloader. The USB model is reset with
        the machine, so `attach()` has to follow."""
        self._say("ARTURIA_REBOOT_TO_DFU")
        self.control(OUT_CLASS_IFACE, ARTURIA_REBOOT_TO_DFU, wait=False)

    def attach(self, settle=25.0):
        """Re-enumerate. The bootloader presents ONE interface (DFU alone)
        where the application presents three, so this is a different device as
        far as the host is concerned."""
        self._say("attaching to the bootloader's USB")
        state = self.synth.attach_usb(settle=settle)
        return _value(state)

    def get_state(self):
        reply = self.control(IN_CLASS_IFACE, DFU_GETSTATE, length=1)
        if not reply:
            raise DfuError("DFU_GETSTATE returned nothing")
        return reply[0]

    def get_status(self):
        """-> {'status', 'poll_timeout', 'state'} from the 6-byte reply."""
        reply = self.control(IN_CLASS_IFACE, DFU_GETSTATUS, length=6)
        if len(reply) < 6:
            raise DfuError("DFU_GETSTATUS returned %d bytes, need 6"
                           % len(reply))
        return {"status": reply[0],
                "poll_timeout": reply[1] | (reply[2] << 8) | (reply[3] << 16),
                "state": reply[4]}

    def select_target(self, image):
        """0xF4. **This erases the target's sectors**, inside the transfer."""
        self._say("ARTURIA_SELECT_TARGET %d  (the erase happens in here)" % image)
        self.control(OUT_CLASS_IFACE, ARTURIA_SELECT_TARGET, value=image)

    def download(self, payload, first_block=0, status_every=1):
        """0x01, in 64-byte blocks, ending with the zero-length block.

        `payload` is the whole thing the device expects, INCLUDING the 64-byte
        header as its first block -- see the module docstring. Image data
        starts at block 1 and lands at the target's base address.

        -> the number of blocks sent, the terminator included. `status_every`
        polls DFU_GETSTATUS between blocks, which the protocol REQUIRES: with
        0 the device refuses the second block. The capture polls twice per
        block; once is enough here.
        """
        blocks = [payload[i:i + TRANSFER_SIZE]
                  for i in range(0, len(payload), TRANSFER_SIZE)]
        self._say("DFU_DNLOAD %d block(s) of up to %d bytes, then the "
                  "zero-length terminator" % (len(blocks), TRANSFER_SIZE))
        n = first_block
        for block in blocks:
            self.control(OUT_CLASS_IFACE, DFU_DNLOAD, value=n, data=block)
            n += 1
            if status_every and (n - first_block) % status_every == 0:
                st = self.get_status()
                if st["state"] == 10:
                    raise DfuError("the device went to dfuERROR at block %d "
                                   "(status 0x%02X)" % (n - 1, st["status"]))
        self.control(OUT_CLASS_IFACE, DFU_DNLOAD, value=n)       # zero-length
        return n + 1 - first_block

    def finish(self):
        """0xF3. The bootloader validates and boots the application."""
        self._say("ARTURIA_FINISH")
        self.control(OUT_CLASS_IFACE, ARTURIA_FINISH, wait=False)

    def describe_state(self):
        try:
            s = self.get_state()
        except DfuError as exc:
            return "unreadable (%s)" % exc
        return "%s (%d)" % (STATES.get(s, "?"), s)


def main():
    """`mfemu dfu` -- run a firmware update against the emulated instrument."""
    import argparse
    import os

    import mfemu.mfrender as mfrender
    import mfemu.paths as PATHS
    from mfemu.midilink import EmulatedSynth

    ap = argparse.ArgumentParser(
        description="Run a firmware update against the emulated instrument.",
        epilog="Needs your own bootloader dump in data/bootloader/.\n"
               "`mfreak peek dump-bootloader` makes one.\n\n"
               "    mfemu dfu --image update.mff --finish",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--image", metavar="FILE", required=True,
                    help="what to send: a .mff, or a headered .bin from "
                         "one. A bare .raw is refused")
    ap.add_argument("--target", type=int, default=0, metavar="N",
                    help="which of the five DFU images (default: 0, the main "
                         "application)")
    ap.add_argument("--blocks", type=int, default=None, metavar="N",
                    help="stop after N blocks, to simulate an interrupted "
                         "update (default: send it all)")
    ap.add_argument("--firmware", metavar="FILE", default=None,
                    help="the application to boot first (default: the one "
                         "in data/firmware/)")
    # nargs/const to match boot, serve, render and warm. This command always
    # uses the bootloader, so a bare --bootloader is the default it already
    # takes -- but the four other commands accept the flag bare, and throwing
    # an argparse error on the one place the habit comes from is its own bug.
    ap.add_argument("--bootloader", metavar="BOOT.bin", nargs="?",
                    const=PATHS.BOOTLOADER, default=None,
                    help="bootloader dump (default: the one in "
                         "data/bootloader/)")
    ap.add_argument("--finish", action="store_true",
                    help="finish the update so the bootloader validates "
                         "and boots it. Without this it stays interrupted")
    ap.add_argument("--port", type=int, default=3710,
                    help="MIDI socket port (default: 3710)")
    ap.add_argument("--quiet", action="store_true",
                    help="print less")
    args = ap.parse_args()

    import mfemu.image as image_file
    bootloader = args.bootloader or PATHS.BOOTLOADER
    PATHS.check(args.firmware or PATHS.MAIN_RAW, bootloader, args.image)
    firmware = image_file.resolve(args.firmware or PATHS.MAIN_RAW)
    # The headered member, not the stripped payload: block 0 IS the header.
    payload = image_file.for_download(args.image, image=args.target)
    if args.blocks is not None:
        payload = payload[:args.blocks * TRANSFER_SIZE]

    work = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-dfu-cli")
    os.makedirs(work, exist_ok=True)
    flash = mfrender.build_image({}, None, work, quiet=True)

    print("downloading %s (%d bytes, %d block(s) + terminator) to image %d"
          % (os.path.basename(args.image), len(payload),
             (len(payload) + TRANSFER_SIZE - 1) // TRANSFER_SIZE, args.target))
    print("booting %s through %s"
          % (os.path.basename(firmware), os.path.basename(bootloader)))

    with EmulatedSynth(firmware=firmware, flash=flash, bootloader=bootloader,
                       renode_dir=PATHS.RENODE, port=args.port) as synth:
        deadline = time.time() + 420
        while time.time() < deadline:
            if "MILESTONE main()" in open(synth.log_path, errors="replace").read():
                break
            time.sleep(3.0)
        else:
            print("the application never reached main(); nothing to ask for DFU")
            return 1
        synth.boot()
        time.sleep(5.0)
        synth.attach_usb()

        dfu = EmulatedDfu(synth, verbose=not args.quiet)
        dfu.reboot_to_dfu()
        time.sleep(12.0)
        dfu.attach(settle=30.0)
        state = dfu.describe_state()
        print("    dfu: state %s" % state)
        if not state.startswith("dfuIDLE"):
            print("the bootloader is not in dfuIDLE; stopping")
            return 1
        dfu.select_target(args.target)
        sent = dfu.download(payload)
        print("    dfu: %d blocks sent" % sent)
        if args.finish:
            dfu.finish()
            time.sleep(10.0)
            # `pc or 0` printed 0x00000000 labelled "bootloader" when the
            # Monitor simply returned nothing -- a confident wrong answer on
            # a healthy boot. boot.py guards this and says why; this path
            # reintroduced it. Retry once, then say "unread", as boot.py does.
            pc = synth.monitor_read("cpu PC")
            if pc is None:
                pc = synth.monitor_read("cpu PC")
            if pc is None:
                print("    dfu: after FINISH, PC unread (Renode returned "
                      "no value; the boot did not fail)")
            else:
                print("    dfu: after FINISH, PC=0x%08X (%s)"
                      % (pc, "application" if pc >= 0x0800C000 else "bootloader"))
        base = 0x0800C000
        # Same reason: an unread word is not a word that reads as zero, and
        # at the image base 0x00000000 would look like an erased image.
        words = [synth.monitor_read("sysbus ReadDoubleWord 0x%08X" % a)
                 for a in (base, base + 4)]
        print("\nimage base 0x%08X now reads %s"
              % (base, " ".join("unread" if w is None else "0x%08X" % w
                                for w in words)))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
