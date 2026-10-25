"""Does your Renode setup actually boot the stock MicroFreak firmware?

The first test that runs the machine. Everything it asserts is about the
HARNESS, not about any firmware modification: it boots Arturia's unmodified
5.0.0.2084 payload on a flash image built from the firmware bundle alone --
no captures, no presets, no patch -- and checks that the firmware gets where
it is documented to get.

    MILESTONES   SystemInit -> __libc_init_array -> main(), in that order.
                 boot.resc installs the hooks; the addresses are from
                 docs/EMULATION.md, where main() being 0x0802479C is itself a
                 correction of an earlier wrong answer.
    ALIVE        the firmware's own millisecond counter at 0x2001E1D8 keeps
                 advancing. Reaching main() once says it started; a counter
                 that moves says it is still running rather than wedged in a
                 retry loop, which is what a half-modelled peripheral looks
                 like. The I2C codec is covered for free here: the firmware
                 waits forever for an ack from it during boot, so a machine
                 that reaches main() has a working codec model.
    BASELINE     the instruction count at each milestone, reported by the hook
                 itself rather than polled afterwards -- reading
                 `cpu ExecutedInstructions` after noticing the log line answers
                 "how far has it got by now", which is a much larger number.
                 Printed, not asserted; tests/test_determinism.py is what holds
                 these to account.

    NEGATIVE CONTROL  the same boot with a ZEROED image must reach no
                      milestone at all. Without it, "we saw MILESTONE main()"
                      could be a hook that fires on anything, a stale log, or
                      a log file being read from a previous run -- and this
                      harness has produced each of those.

    uv run python tests/test_boot.py
"""
import os
import re
import sys
import time

import mfemu.mfrender as mfrender
import mfemu.paths as PATHS
from mfemu.midilink import EmulatedSynth

MILESTONES = ["MILESTONE SystemInit", "MILESTONE __libc_init_array",
              "MILESTONE main()"]
# The firmware's own tick counter. NOT tenths of a millisecond, which is what
# this said and what midilink.BOOT_SECONDS was derived from until 2026-10-16:
# by the time main() runs the firmware has reprogrammed SysTick to a reload of
# 1,600, so it advances at 180 MHz / 1600 = 112,500/s -- measured 112,538
# against Renode's virtual time. Nothing here converts it; the values are
# printed raw, so the stale unit misled a reader rather than a calculation.
UPTIME = 0x2001E1D8
APP_BASE, APP_END = 0x0800C000, 0x08080000
GOOD_DEADLINE = 420.0          # a cold boot to main() is ~1-2 min of wall clock
CONTROL_DEADLINE = 90.0        # nothing will ever happen; just long enough

WORK = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-boot")
os.makedirs(WORK, exist_ok=True)


def seen(log_path):
    """-> [(milestone, instructions_at_it)] in the order they appear.

    The hooks report `MILESTONE <name> insns=<n>`, where n is the count AT the
    hook. That is not what `cpu ExecutedInstructions` says afterwards: the
    machine free-runs while this test polls the log, so by the time a poll
    notices main() the CPU is tens of millions of instructions past it. An
    earlier version of this test printed the polled number and labelled it
    "instructions at main()", which was wrong by a factor of fifteen.
    """
    if not log_path or not os.path.exists(log_path):
        return []
    text = open(log_path, errors="replace").read()
    hits = []
    for m in MILESTONES:
        found = re.search(re.escape(m) + r"(?: insns=(\d+))?", text)
        if found:
            hits.append((found.start(), m,
                         int(found.group(1)) if found.group(1) else None))
    return [(m, n) for _, m, n in sorted(hits)]


def read_retry(synth, command, tries=3):
    """Read a Monitor value, retrying an empty reply.

    One empty reply is not a measurement. This test failed 2 runs in 6 on
    2026-10-17, every failure being the single `cpu PC` read coming back with
    no value while the milestones showed the machine running -- and in one of
    them the PC turned up as the answer to the NEXT read, which is the shape
    of the stale-prompt race `midilink.monitor()` documents. That race is a
    lead and not a diagnosis: two known-answer controls (11 reads of flash
    constants, then 24 PC reads interleaved with 24 constant reads in one
    boot) produced 0 wrong answers and did not reproduce it.

    So retry, and let the caller distinguish "unread" from a bad value --
    reporting an unread PC as 0x00000000 is what made this look like a boot
    failure rather than a read failure.
    """
    for _ in range(tries):
        value = synth.monitor_read(command)
        if value is not None:
            return value
    return None


def run(firmware, flash, label, port, deadline, want_main):
    print("\n=== %s ===" % label)
    print("    firmware %s (%d bytes)"
          % (os.path.basename(firmware), os.path.getsize(firmware)))
    got, counts, pc, uptimes = [], {}, None, []
    with EmulatedSynth(firmware=firmware, flash=flash,
                       renode_dir=PATHS.RENODE, port=port) as synth:
        end = time.time() + deadline
        while time.time() < end:
            got = seen(synth.log_path)
            if MILESTONES[-1] in [m for m, _ in got]:
                break
            time.sleep(2.0)
        counts = dict(got)
        for m in MILESTONES:
            n = counts.get(m)
            print("    %-28s %s%s" % (m, "yes" if m in counts else "no",
                                      "   at %s instructions" % f"{n:,}" if n else ""))
        if MILESTONES[-1] in counts:
            pc = read_retry(synth, "cpu PC")
            for _ in range(3):
                uptimes.append(read_retry(
                    synth, "sysbus ReadDoubleWord 0x%08X" % UPTIME))
                time.sleep(3.0)
            print("    PC (while running)           %s"
                  % ("UNREAD -- the Monitor returned no value in 3 tries"
                     if pc is None else "0x%08X" % pc))
            print("    uptime counter               %s" % uptimes)
    return [m for m, _ in got], counts, pc, uptimes


def main():
    PATHS.check(PATHS.MAIN_RAW)
    flash = mfrender.build_image({}, None, WORK, quiet=True)
    print("flash image: %s (%d MB, bundle only -- every preset slot erased)"
          % (os.path.basename(flash), os.path.getsize(flash) >> 20))

    zeroed = os.path.join(WORK, "zeroed.img")
    if not os.path.exists(zeroed):
        with open(zeroed, "wb") as fh:
            fh.write(b"\0" * os.path.getsize(PATHS.MAIN_RAW))

    good, counts, pc, uptimes = run(PATHS.MAIN_RAW, flash,
                                    "THE HARNESS  stock 5.0.0.2084", 3940,
                                    GOOD_DEADLINE, True)
    ctl, _, _, _ = run(zeroed, flash, "NEGATIVE CONTROL  a zeroed image", 3950,
                       CONTROL_DEADLINE, False)

    ordered = good == MILESTONES
    alive = (len([u for u in uptimes if u is not None]) == len(uptimes)
             and len(uptimes) >= 2
             and all(b > a for a, b in zip(uptimes, uptimes[1:])))
    pc_read = pc is not None
    in_app = pc_read and APP_BASE <= pc < APP_END
    control_silent = not ctl

    print("\n" + "=" * 68)
    print("MILESTONES  all three, in order             : %s  %s"
          % (ordered, [m.split()[-1] for m in good]))
    print("ALIVE       uptime counter advancing        : %s  %s" % (alive, uptimes))
    print("            PC inside the app image         : %s%s"
          % (in_app, "" if pc_read else
             "  (PC UNREAD -- this says nothing about where the CPU is)"))
    print("CONTROL     zeroed image reached nothing    : %s  %s"
          % (control_silent, [m.split()[-1] for m in ctl] or "silent"))
    main_insns = counts.get(MILESTONES[-1])
    if main_insns:
        print("\nBASELINE    %s instructions to reach main(), measured AT the hook"
              % f"{main_insns:,}")
        print("            Not asserted: a legitimate platform change moves it.")
        print("            tests/test_determinism.py is what holds it to account,")
        print("            by requiring two boots to agree exactly.")

    if not control_silent:
        print("\nVOID - a zeroed image reached a milestone, so the milestone check")
        print("       is not measuring the firmware. Fix that before trusting a pass.")
        return 2
    if ordered and alive and in_app:
        print("\nPASS - your harness boots Arturia's unmodified 5.0.0.2084 to main()")
        print("       and keeps running it, and the control proves the check can fail.")
        return 0
    if ordered and alive and not pc_read:
        print("\nFAIL - but note WHAT failed: the milestones and the uptime counter")
        print("       both say the machine booted and is running. The only check")
        print("       that did not pass is reading `cpu PC`, which returned no")
        print("       value in 3 tries. That is a harness/Monitor failure, not a")
        print("       boot failure -- see read_retry() above. Re-run before")
        print("       chasing a firmware or platform cause.")
        return 1
    print("\nFAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
