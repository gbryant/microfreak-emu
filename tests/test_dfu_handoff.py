"""Does the firmware update's handshake run under emulation?

Needs a dump of your own instrument's bootloader in `data/bootloader/`, see the
README -- and SKIPS cleanly without one, because Arturia ship the application
only and this repository will never contain the bootloader.

With it, the machine boots the way the instrument does: from 0x08000000,
through the bootloader's three gates, into the application. Then the host
sends ARTURIA_REBOOT_TO_DFU as a real USB control transfer, which is what MIDI
Control Center does at t=17.23 in the capture in docs/FIRMWARE_UPDATE.md.

    GATES      reaching the application's main() means all three passed: the
               forced-DFU pin read low, both validity records were found
               intact in the factory data page, and gate 3 handed over. Before
               2026-10-15 the harness always entered the application directly,
               so none of them had ever executed.

    0xF2       bmRT 0x21, bRequest 0xF2, wLength 0 -- accepted by the running
               application's own class driver, not the bootloader's.

    CONTROL    bRequest 0xF0 -- outside the set the application accepts, so
               USBD_CtlError stalls it -- must leave the firmware alone. Sent
               FIRST, because 0xF2 ends in a reset.

The discriminator is the FIRMWARE'S behaviour, not the host's bookkeeping, and
that is a correction. The first version of this test asked the host whether
the transfer had completed, and the host said yes to 0xF0 as well: its
completion signal cannot tell accepted from refused, and checking the
endpoint's STALL bit did not help either. So this watches what only the 0xF2
path does -- call `mark_dfu_request` at 0x08045B58 -- through a hook whose log
line survives the reset that follows. A host that accepted everything would
now fail the control, which is the whole point of having one.

    END STATE  the device must come back up in the BOOTLOADER, not the
               application. That is the whole chain's only unfakeable
               outcome, and section 1 of the same run is its control.

WHAT THIS DOES NOT TEST: the download that follows -- that is
tests/test_dfu_download.py, which erases and writes for real. Nor the content
of the data page, which reads byte-identical afterwards because the bootloader
re-stamps it on the way into DFU. See docs/FIRMWARE_UPDATE.md.

    uv run python tests/test_dfu_handoff.py    # with data/bootloader/ filled
"""
import os
import sys
import time

import mfemu.mfrender as mfrender
import mfemu.paths as PATHS
from mfemu.midilink import EmulatedSynth

STALL_REQUEST = 0xF0           # outside the app's accepted set
BOOT_DEADLINE = 420.0
WORK = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-dfu")
os.makedirs(WORK, exist_ok=True)


MARK_DFU_REQUEST = 0x08045B58   # docs/FIRMWARE_UPDATE.md


def hook_count(log_text):
    return log_text.count("HOOK mark_dfu_request")


def main():
    if PATHS.BOOTLOADER is None or not os.path.exists(PATHS.BOOTLOADER):
        print("SKIP - no bootloader image.")
        print("""
  The firmware update happens mostly in the bootloader, and Arturia's update
  bundle contains the application only. The bootloader can come from nowhere
  but an instrument you own:

      mfreak fw patch peek stock.mff --out peek.mff
      mfreak fw flash peek.mff
      mfreak peek dump-bootloader --out bootloader.bin
      mfreak fw flash stock.mff                        # put stock back
      cp bootloader.bin data/bootloader/

  paths.py looked in: %s""" % PATHS.BOOTLOADER)
        return 0

    PATHS.check(PATHS.MAIN_RAW, PATHS.BOOTLOADER)
    flash = mfrender.build_image({}, None, WORK, quiet=True)
    print("bootloader %s (%d bytes), booting from 0x%08X"
          % (os.path.basename(PATHS.BOOTLOADER), os.path.getsize(PATHS.BOOTLOADER),
             PATHS.BOOTLOADER_BASE))

    with EmulatedSynth(firmware=PATHS.MAIN_RAW, flash=flash,
                       bootloader=PATHS.BOOTLOADER,
                       renode_dir=PATHS.RENODE, port=3992) as synth:
        reached = False
        end = time.time() + BOOT_DEADLINE
        while time.time() < end:
            if "MILESTONE main()" in open(synth.log_path, errors="replace").read():
                reached = True
                break
            time.sleep(3.0)
        print("\n1. the bootloader's gates")
        print("   %-4s application reached main(), so all three passed"
              % ("ok" if reached else "FAIL"))
        if not reached:
            print("\nFAIL - the bootloader did not hand over. Nothing below can run.")
            return 1

        synth.boot()
        time.sleep(5.0)
        synth.attach_usb()
        synth.monitor('cpu AddHook 0x%08X '
                      '"self.Log(LogLevel.Error, \'HOOK mark_dfu_request\')"'
                      % MARK_DFU_REQUEST)

        print("\n2. CONTROL: 0x%02X, which the application stalls" % STALL_REQUEST)
        synth.monitor('usbfs ControlRequest 0x21 0x%02X 0 0 0 ""' % STALL_REQUEST)
        time.sleep(30.0)
        log = open(synth.log_path, errors="replace").read()
        before = hook_count(log)
        stalled = before == 0
        print("   %-4s the firmware did not start a DFU handoff  (hook fired %d"
              " times)" % ("ok" if stalled else "FAIL", before))

        print("\n3. ARTURIA_REBOOT_TO_DFU over USB")
        synth.monitor('usbfs ControlRequest 0x21 0xF2 0 0 0 ""')
        accepted = False
        for _ in range(10):
            time.sleep(3.0)
            log = open(synth.log_path, errors="replace").read()
            if hook_count(log) > before:
                accepted = True
                break
        print("   %-4s mark_dfu_request ran  (hook fired %d times)"
              % ("ok" if accepted else "FAIL", hook_count(log)))
        # The hook's log line is the evidence, not `usbfs ControlDone`: this
        # request ends in a SYSRESETREQ and the model's Reset() zeroes that
        # counter, while the log survives the machine reset.

        print("\n4. where the machine ends up")
        # The outcome that matters, and the only one that cannot be faked by a
        # write going nowhere: the device must come back up in the BOOTLOADER
        # rather than the application. Gate 2 only fails if image 0's validity
        # marker was invalidated, so ending in the bootloader proves the whole
        # chain -- control transfer, request flag, page rewrite, reset.
        #
        # The page itself is NOT checked, and that is deliberate. It reads
        # byte-identical afterwards: all 128 words. The bootloader evidently
        # re-stamps it on the way into DFU -- "erased sector 2" is logged
        # twice, once by the application and once after the reset -- so a
        # before/after diff of the page says nothing either way. Where the
        # core is running does.
        time.sleep(10.0)
        pc = synth.monitor_read("cpu PC")
        if pc is None:                      # stale-prompt race, retry once
            pc = synth.monitor_read("cpu PC")
        in_bootloader = pc is not None and pc < 0x0800C000
        # An unread PC is not a PC of zero. This printed "0x00000000 is in
        # the application" and failed, which blames the firmware for a
        # Monitor race -- the exact confusion boot.py documents.
        if pc is None:
            print("   FAIL PC unread (the Monitor returned no value twice; "
                  "this says nothing about where the CPU is)")
        else:
            print("   %-4s PC 0x%08X is in the %s"
                  % ("ok" if in_bootloader else "FAIL", pc,
                     "bootloader" if in_bootloader else "application"))
        print("        (section 1 had it in the application, which is this")
        print("         check's control: the same run, before the request)")

    print("\n" + "=" * 68)
    if reached and accepted and stalled and in_bootloader:
        print("PASS - the firmware update's first phase runs end to end. The real")
        print("       bootloader boots the application; a genuine USB control")
        print("       transfer of Arturia's 0xF2 makes the application stamp the")
        print("       data page and reset; and the device comes back up in the")
        print("       BOOTLOADER, which only happens when gate 2 finds image 0's")
        print("       validity marker cleared. A refused request does none of it.")
        print("\n       The download that follows is tests/test_dfu_download.py:")
        print("       SELECT_TARGET's erase and DFU_DNLOAD both land in flash.")
        return 0
    if accepted and not stalled:
        print("VOID - a request the application refuses also started a DFU")
        print("       handoff, so 0xF2 doing so proves nothing.")
        return 2
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
