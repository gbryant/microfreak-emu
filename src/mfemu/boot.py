#!/usr/bin/env python3
"""Boot the firmware and report what happened.

    mfemu boot                          the stock application
    mfemu boot --bootloader             through the bootloader, as the instrument does
    mfemu boot --firmware patched.raw   your own image
    mfemu boot --hold 60                keep it running and keep reporting

It prints each boot milestone with its instruction count, the firmware's
uptime counter, and the program counter. A wedged boot stops reaching
milestones.
"""
import argparse
import os
import re
import sys
import time

import mfemu.image as image
import mfemu.mfrender as mfrender
import mfemu.paths as PATHS
from mfemu.midilink import EmulatedSynth

MILESTONES = ["MILESTONE SystemInit", "MILESTONE __libc_init_array",
              "MILESTONE main()"]
APP_BASE, APP_END = 0x0800C000, 0x08080000


def where_is(pc):
    """-> what region a sampled PC is in, in words.

    The test was `"application" if pc >= APP_BASE else "bootloader"`, an
    unbounded comparison that called ANY high value the application. A real
    run printed

        PC    0xFFFFFFE9  (application)

    which is not an address: 0xFFFFFFxx is an EXC_RETURN magic value, meaning
    the core was sampled part-way through returning from an exception. On a
    machine that is running and servicing interrupts that is an ordinary thing
    to catch, and reporting it as a flash address makes it look like a wild
    jump. Bounded against APP_END now, and the EXC_RETURN case is named.
    """
    if pc >= 0xFFFFFF00:
        return "mid-exception-return, EXC_RETURN -- the machine is running"
    if APP_BASE <= pc < APP_END:
        return "application"
    if pc < APP_BASE:
        return "bootloader"
    return "outside both the bootloader and the application"


def read_hex(synth, expr):
    """-> a Monitor read as "0x%08X", or "unread".

    One helper rather than a guard at each call, because the guard kept
    landing on only some of them: the PC read below got one, the uptime read
    four lines under it printed the literal string "None", and so did the
    uptime read inside --hold. See midilink.monitor() on the stale-prompt
    race. Retries once, which is what makes "unread" mean "it really did not
    answer" rather than "we asked at a bad moment".

    The PC read keeps its own guard rather than using this, because it needs
    the NUMBER -- it compares against APP_BASE to say application or
    bootloader -- and this returns a formatted string.
    """
    v = synth.monitor_read(expr)
    if v is None:
        v = synth.monitor_read(expr)
    return "unread" if v is None else "0x%08X" % v


def milestones(log_path):
    """-> [(name, instructions_at_it)] in the order they appear."""
    if not log_path or not os.path.exists(log_path):
        return []
    text = open(log_path, errors="replace").read()
    out = []
    for name in MILESTONES:
        m = re.search(re.escape(name) + r"(?: insns=(\d+))?", text)
        if m:
            out.append((m.start(), name,
                        int(m.group(1)) if m.group(1) else None))
    return [(n, c) for _, n, c in sorted(out)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="\n".join(__doc__.splitlines()[1:]))
    ap.add_argument("--firmware", metavar="FILE", default=None,
                    help="a .mff, headered .bin or bare payload (default: "
                         "the one in data/firmware/)")
    ap.add_argument("--image", type=int, default=0, metavar="N",
                    help="which image inside a .mff (default: 0, the "
                         "application)")
    ap.add_argument("--bootloader", metavar="BOOT.bin", nargs="?",
                    const=PATHS.BOOTLOADER, default=None,
                    help="boot through the bootloader (default file: "
                         "data/bootloader/)")
    ap.add_argument("--flash", metavar="FLASH.img", default=None,
                    help="external flash image (default: empty presets)")
    ap.add_argument("--port", type=int, default=3700,
                    help="MIDI socket port (default: 3700)")
    ap.add_argument("--deadline", type=float, default=420.0,
                    help="seconds to wait for main() (default: 420)")
    ap.add_argument("--hold", type=float, default=0.0, metavar="SECONDS",
                    help="keep running this long after boot, reporting "
                         "uptime and PC")
    args = ap.parse_args()

    firmware = image.resolve(args.firmware or PATHS.MAIN_RAW,
                             image=args.image)
    PATHS.check(firmware)
    if args.bootloader:
        PATHS.check(args.bootloader)

    work = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-boot")
    os.makedirs(work, exist_ok=True)
    flash = args.flash or mfrender.build_image({}, None, work, quiet=True)

    print("flash      %s" % os.path.basename(flash))
    if args.bootloader:
        print("bootloader %s -- booting from 0x%08X, through its gates"
              % (os.path.basename(args.bootloader), PATHS.BOOTLOADER_BASE))
    else:
        print("bootloader none -- entering the application directly at 0x%08X"
              % APP_BASE)
    print()

    with EmulatedSynth(firmware=firmware, flash=flash,
                       bootloader=args.bootloader,
                       renode_dir=PATHS.RENODE, port=args.port) as synth:
        seen, end = [], time.time() + args.deadline
        while time.time() < end:
            seen = milestones(synth.log_path)
            if MILESTONES[-1] in [n for n, _ in seen]:
                break
            time.sleep(2.0)
        counts = dict(seen)
        for name in MILESTONES:
            n = counts.get(name)
            print("  %-28s %s%s"
                  % (name, "yes" if name in counts else "NO",
                     ("   at %s instructions" % f"{n:,}")
                     if n is not None else ""))
        if MILESTONES[-1] not in counts:
            print("\nIt did not reach main() in %.0f s." % args.deadline)
            print("Renode's log is at %s" % synth.log_path)
            print("Run `mfemu check`. A wrong firmware image stops here.")
            return 1
        synth.boot()
        # A Monitor read can come back with no value (see midilink.monitor()'s
        # note on the stale-prompt race). That is "unread", NOT zero: printing
        # it as 0x00000000 labelled "bootloader" is a confident wrong answer on
        # a perfectly healthy application boot. Retry once, then say so.
        pc = synth.monitor_read("cpu PC")
        if pc is None:
            pc = synth.monitor_read("cpu PC")
        if pc is None:
            print("\n  PC                           unread  "
                  "(Renode returned no value)")
        else:
            print("\n  PC                           0x%08X  (%s)"
                  % (pc, where_is(pc)))
        print("  emulated time                %.2f s" % (synth.virtual_time() or 0.0))
        if args.hold:
            print("\nholding for %.0f s" % args.hold)
            stop = time.time() + args.hold
            while time.time() < stop:
                time.sleep(min(5.0, max(0.1, stop - time.time())))
                print("  t+%-4.0fs emulated=%-8.2f PC=%s"
                      % (args.hold - (stop - time.time()),
                         synth.virtual_time() or 0.0,
                         read_hex(synth, "cpu PC")))
    print("\nIt booted and kept running. Renode's log: %s" % synth.log_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
