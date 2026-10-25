"""Does a firmware DOWNLOAD actually land in flash?

The second half of the firmware update, and the half that writes. Needs a dump
of your own bootloader in `data/bootloader/` and skips without one, because
everything here is answered by the bootloader and Arturia ship the application
only.

The sequence is the one in docs/FIRMWARE_UPDATE.md, driven over real USB
control transfers by `mfemu.dfu`:

    0xF2  REBOOT_TO_DFU     the application stamps the data page and resets
          re-enumerate      the bootloader presents DFU alone
    0x05  DFU_GETSTATE      must be dfuIDLE before anything else
    0xF4  SELECT_TARGET 0   **this erases the whole application region**
    0x01  DFU_DNLOAD        64-byte blocks, each followed by DFU_GETSTATUS
    0x01  DFU_DNLOAD (0)    the zero-length end-of-transfer marker

Three things are asserted, and the erase is what makes the third one mean
something:

    ERASED     the application's base word must go from the image's stack
               pointer to 0xFFFFFFFF. SELECT_TARGET does that, inside the
               control transfer, and it is observable proof the bootloader
               acted on a message we sent.
    WRITTEN    the bytes of block 1 must be readable at the application's
               base address. They cannot be left over from anything: the
               region was erased a moment earlier, in this same run.
    NOT BEYOND the word after the last block we sent must still read
               0xFFFFFFFF. Without this, "the pattern is there" could be
               satisfied by a model that wrote the whole payload everywhere,
               or by reading the wrong place.

The first block is the HEADER, not image data -- the bootloader consumes it
and programs from block 1 onward. This test sends a recognisable pattern as
block 1 and checks for that, which is also what established the convention.

A run leaves the emulated instrument with its application erased and only a
few blocks written, which is exactly what an interrupted update looks like.
That is fine -- it is a 16 MB file and a firmware image on disk -- but it is
why this is not part of the boot suite.

    uv run python tests/test_dfu_download.py   # with data/bootloader/ filled
"""
import os
import sys
import time

import mfemu.mfrender as mfrender
import mfemu.paths as PATHS
from mfemu.dfu import EmulatedDfu, DfuError
from mfemu.midilink import EmulatedSynth

APP_BASE = 0x0800C000
BLOCK = 0x40
BOOT_DEADLINE = 420.0
WORK = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-dfudl")
os.makedirs(WORK, exist_ok=True)

# Block 0 is the header the bootloader eats; block 1 is what we check for.
# A pattern no erased flash and no real firmware would produce by accident.
HEADER = bytes(0xA5 for _ in range(BLOCK))
IMAGE = bytes((0x40 + i) & 0xFF for i in range(BLOCK))


def main():
    # `is None` first: paths.find_bootloader returns None when
    # data/bootloader/ is empty, and os.path.exists(None) is a TypeError --
    # a traceback where this test's whole point is to skip politely.
    if PATHS.BOOTLOADER is None or not os.path.exists(PATHS.BOOTLOADER):
        print("SKIP - no bootloader image; see tests/test_dfu_handoff.py for how")
        print("       to read one off your own instrument. Drop it in:")
        print("       %s" % PATHS.BOOTLOADER_DIR)
        return 0

    PATHS.check(PATHS.MAIN_RAW, PATHS.BOOTLOADER)
    flash = mfrender.build_image({}, None, WORK, quiet=True)
    print("booting through the real bootloader")

    with EmulatedSynth(firmware=PATHS.MAIN_RAW, flash=flash,
                       bootloader=PATHS.BOOTLOADER,
                       renode_dir=PATHS.RENODE, port=3978) as synth:
        end = time.time() + BOOT_DEADLINE
        reached = False
        while time.time() < end:
            if "MILESTONE main()" in open(synth.log_path, errors="replace").read():
                reached = True
                break
            time.sleep(3.0)
        if not reached:
            print("FAIL - never reached the application's main()")
            return 1
        synth.boot()
        time.sleep(5.0)
        synth.attach_usb()

        def word(addr):
            v = synth.monitor_read("sysbus ReadDoubleWord 0x%08X" % addr)
            if v is None:            # the stale-prompt race; retry once
                v = synth.monitor_read("sysbus ReadDoubleWord 0x%08X" % addr)
            return v

        def hexs(v):
            """An unread Monitor value is "unread", never 0x00000000.

            `(v or 0)` was the whole bug a146bb7 fixed in dfu.py and
            test_dfu_handoff.py, and it was still here: a read that never
            came back printed as a confident zero, which in a flash test
            reads as "erased wrongly" rather than "not measured".
            """
            return "unread" if v is None else "0x%08X" % v

        dfu = EmulatedDfu(synth)
        print("\n1. into DFU")
        app_before = word(APP_BASE)
        print("   application base holds %s" % hexs(app_before))
        dfu.reboot_to_dfu()
        time.sleep(12.0)
        dfu.attach(settle=30.0)
        state = dfu.describe_state()
        print("   DFU state: %s" % state)
        if not state.startswith("dfuIDLE"):
            print("\nFAIL - the bootloader is not in dfuIDLE; nothing below can run")
            return 1

        print("\n2. SELECT_TARGET 0, which erases the application")
        dfu.select_target(0)
        time.sleep(5.0)
        app_erased = word(APP_BASE)
        # `app_before is not None` explicitly. It used to be `app_before !=
        # 0xFFFFFFFF`, and None satisfies that, so a run whose BEFORE read
        # never came back still reported the erase as ok -- a false pass, not
        # just a bad display.
        erased = (app_before is not None and app_erased is not None
                  and app_erased == 0xFFFFFFFF and app_before != 0xFFFFFFFF)
        print("   %-4s 0x%08X: %s -> %s"
              % ("ok" if erased else "FAIL", APP_BASE, hexs(app_before),
                 hexs(app_erased)))

        print("\n3. download the header block and one image block")
        try:
            sent = dfu.download(HEADER + IMAGE)
        except DfuError as exc:
            print("   FAIL %s" % exc)
            return 1
        print("   sent %d blocks (2 of data, 1 zero-length terminator)" % sent)
        time.sleep(5.0)

        want = [int.from_bytes(IMAGE[i:i + 4], "little") for i in (0, 4, 0x3C)]
        got = [word(APP_BASE), word(APP_BASE + 4), word(APP_BASE + 0x3C)]
        written = got == want
        for off, w, g in zip((0, 4, 0x3C), want, got):
            print("   %-4s 0x%08X = %s  (want 0x%08X)"
                  % ("ok" if w == g else "FAIL", APP_BASE + off, hexs(g), w))

        beyond = word(APP_BASE + BLOCK)
        untouched = beyond is not None and beyond == 0xFFFFFFFF
        print("   %-4s 0x%08X = %s  (want 0xFFFFFFFF: we sent nothing there)"
              % ("ok" if untouched else "FAIL", APP_BASE + BLOCK, hexs(beyond)))

    print("\n" + "=" * 68)
    if erased and written and untouched:
        print("PASS - a firmware download lands in flash. The bootloader erased the")
        print("       application region on SELECT_TARGET and programmed the block")
        print("       we sent at the image base, through a flash controller that")
        print("       implements FLASH_CR.PG -- and the word past the last block is")
        print("       still erased, so this is the data we wrote and not a readback")
        print("       of something else.")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
