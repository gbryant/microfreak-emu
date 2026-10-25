#!/usr/bin/env python3
"""Manage the cached snapshots that renders start from.

    mfemu warm                     build one for the default firmware
    mfemu warm --list              list cached snapshots and their sizes
    mfemu warm --clear             delete them

`mfemu render` builds a snapshot on first use, so you rarely need this.
A cold boot takes about a minute; restoring a snapshot takes about a second,
with identical audio.

A snapshot only matches the exact firmware, bootloader setting, timer rate,
flash image and peripheral models it was built with. Change any of these and
the next render builds a new one.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

import mfemu.paths as PATHS
import mfemu.startup as startup

# Mirrors mfrender.BOOT_SECONDS. A snapshot taken earlier than the firmware
# finishes starting up is worse than no snapshot: it restores instantly into a
# machine that is not ready to make a sound.
WARM_SECONDS = 14.0

# The models whose compiled identity the snapshot depends on, plus the
# platform and boot script that place them. Their BYTES are hashed into the
# key, so any edit to one of these -- a comment included -- invalidates every
# existing snapshot and costs one rebuild per flash image. That is the right
# trade: a stale snapshot of a changed model restores a machine that no longer
# matches the source.
SOURCES = ["STM32F4_Flash.cs", "STM32F4DMA.cs", "STM32_QUADSPI.cs",
           "MicroFreakCodec.cs", "STM32_SAI.cs", "STM32_OTGFS.cs",
           "microfreak.repl", "boot.resc"]


def _renode_version(renode_dir):
    """The Renode build string, which a snapshot is also tied to."""
    import mfemu.mfrender as mfrender
    try:
        out = subprocess.run(mfrender.renode_command(renode_dir) + ["--version"],
                             cwd=renode_dir, capture_output=True, text=True,
                             timeout=60)
        return (out.stdout or out.stderr).strip().splitlines()[0]
    except Exception:
        return "unknown"


def key(firmware=None, bootloader=None, timer8_hz=None, renode_dir=None,
        warm_seconds=WARM_SECONDS, flash=None):
    """-> a hex digest naming the snapshot for exactly this configuration."""
    import mfemu.mfrender as mfrender
    import mfemu.image as image
    # resolve(), not the raw default: PATHS.MAIN_RAW may be a .mff or a
    # headered .bin, and hashing the container instead of the payload keys the
    # snapshot on the wrapping rather than the firmware. Two people with the
    # same firmware in different shapes would get different keys, and a .mff
    # would key on bytes the CPU must never run. A .raw passes through
    # untouched, so this is free on the common path.
    firmware = image.resolve(firmware or PATHS.MAIN_RAW, quiet=True)
    renode_dir = renode_dir or PATHS.RENODE
    timer8_hz = mfrender.TIMER8_HZ if timer8_hz is None else timer8_hz
    h = hashlib.sha256()
    h.update(b"mfemu-warm-2\n")
    with open(firmware, "rb") as fh:
        h.update(hashlib.sha256(fh.read()).digest())
    h.update(b"bootloader=")
    if bootloader:
        with open(bootloader, "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
    h.update(("\ntimer8=%d\nwarm=%.6f\n" % (timer8_hz, warm_seconds)).encode())
    # The external flash is part of the key, and it took a measurement to
    # learn that it has to be. See the module docstring: a machine warmed on
    # empty flash restores into state the firmware never would have reached,
    # because start-up reads samples and wavetables out of that flash.
    h.update(b"flash=")
    if flash:
        with open(flash, "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
    for name in SOURCES:
        with open(os.path.join(PATHS.RENODE_DIR, name), "rb") as fh:
            h.update(hashlib.sha256(fh.read()).digest())
    h.update(_renode_version(renode_dir).encode())
    return h.hexdigest()[:16]


def path(k):
    return os.path.join(PATHS.CACHE, "warm-%s.snap" % k)


def restore_lines(snapshot):
    """The script preamble that replaces a boot: include, Load, re-select.

    The includes must come first and must be the same sources the snapshot was
    saved with -- that is what the compiler cache makes reusable. `mach set`
    is not optional: after a Load the Monitor has no current machine, so the
    very next `cpu` or `quadspi` command fails with "No such emulation
    element" and the script aborts half-built.
    """
    lines = ["$mf = @%s" % PATHS.RENODE_DIR]
    lines += ["include @%s" % os.path.join(PATHS.RENODE_DIR, name)
              for name in SOURCES if name.endswith(".cs")]
    lines += ["Load @%s" % PATHS.resc(snapshot), 'mach set "microfreak"']
    return lines


def build(firmware=None, bootloader=None, timer8_hz=None, renode_dir=None,
          warm_seconds=WARM_SECONDS, quiet=False, flash=None):
    """Boot to `warm_seconds` of emulated time and save. -> the snapshot path.

    `flash` is the external-flash image to warm up WITH, and leaving it out is
    not equivalent to passing one -- see the module docstring.
    """
    import mfemu.mfrender as mfrender
    import mfemu.image as image
    # Same as key(): the machine must be warmed from the payload, not from a
    # .mff. key() resolves too, so the two agree whichever way this is reached.
    firmware = image.resolve(firmware or PATHS.MAIN_RAW, quiet=True)
    renode_dir = renode_dir or PATHS.RENODE
    timer8_hz = mfrender.TIMER8_HZ if timer8_hz is None else timer8_hz
    k = key(firmware, bootloader, timer8_hz, renode_dir, warm_seconds, flash)
    out = path(k)
    os.makedirs(PATHS.CACHE, exist_ok=True)

    lines = ["$mf = @%s" % PATHS.RENODE_DIR,
             "$fw = @%s" % PATHS.resc(firmware),
             "\n".join(startup.resc_lines(firmware)
                       + ["include @%s" % os.path.join(PATHS.RENODE_DIR, "boot.resc")])]
    if bootloader:
        lines[3:3] = mfrender.bootloader_lines(bootloader)
    if flash:
        lines.append("quadspi LoadImage @%s" % PATHS.resc(flash))
    lines += ["timer8 Frequency %d" % timer8_hz,
              'emulation RunFor "%.6f"' % warm_seconds,
              # Written to a temporary name and moved into place, so an
              # interrupted save cannot leave a half-file that later runs
              # would try to load and reject as corrupt.
              "Save @%s.part" % PATHS.resc(out),
              'echo "MFEMU-WARM-SAVED"',
              "quit"]
    script = os.path.join(PATHS.CACHE, "warm-%s.resc" % k)
    with open(script, "w") as fh:
        fh.write("\n".join(lines) + "\n")

    if not quiet:
        print("warming %s to %.1f emulated seconds -- about %.0f s, once"
              % (os.path.basename(firmware), warm_seconds, warm_seconds * 3.6),
              file=sys.stderr)
    started = time.time()
    proc = subprocess.run(
        mfrender.renode_command(renode_dir)
        + ["--disable-xwt", "--console", "--plain", script],
        cwd=renode_dir, capture_output=True, text=True, timeout=1800)
    part = out + ".part"
    if "MFEMU-WARM-SAVED" not in proc.stdout or not os.path.exists(part):
        tail = "\n".join((proc.stdout or proc.stderr).splitlines()[-12:])
        raise RuntimeError("the warm-up did not save a snapshot.\n%s" % tail)
    os.replace(part, out)
    # A sidecar saying what this snapshot is OF. The key is a hash, so without
    # this a cache of 25 MB files is unreadable -- there is no way to tell
    # which firmware or bank a snapshot belongs to, or whether it is safe to
    # delete. `--list` reads these.
    with open(out + ".json", "w") as fh:
        json.dump({"firmware": firmware, "bootloader": bootloader,
                   "flash": flash, "timer8_hz": timer8_hz,
                   "warm_seconds": warm_seconds,
                   "renode": _renode_version(renode_dir),
                   "built": time.strftime("%Y-%m-%dT%H:%M:%S")}, fh, indent=2)
    if not quiet:
        print("warm snapshot: %s  (%.1f MB, %.0f s)"
              % (out, os.path.getsize(out) / 1e6, time.time() - started),
              file=sys.stderr)
    return out


def ensure(firmware=None, bootloader=None, timer8_hz=None, renode_dir=None,
           warm_seconds=WARM_SECONDS, quiet=False, build_missing=True,
           flash=None):
    """-> the snapshot path, building it if absent. None if absent and not built."""
    k = key(firmware, bootloader, timer8_hz, renode_dir, warm_seconds, flash)
    out = path(k)
    if os.path.exists(out):
        return out
    if not build_missing:
        return None
    return build(firmware, bootloader, timer8_hz, renode_dir, warm_seconds,
                 quiet, flash)


def cached():
    """-> [(path, bytes, mtime)] for every snapshot in the cache."""
    if not os.path.isdir(PATHS.CACHE):
        return []
    out = []
    for name in sorted(os.listdir(PATHS.CACHE)):
        if name.startswith("warm-") and name.endswith(".snap"):
            full = os.path.join(PATHS.CACHE, name)
            st = os.stat(full)
            out.append((full, st.st_size, st.st_mtime))
    return out


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", metavar="FILE", default=None,
                    help="a .mff package, a headered .bin, or a bare payload")
    ap.add_argument("--image", type=int, default=0, metavar="N",
                    help="which image inside a .mff (default: 0)")
    ap.add_argument("--bootloader", metavar="BOOT.bin", nargs="?",
                    const=PATHS.BOOTLOADER, default=None,
                    help="warm up through the bootloader")
    # The last key component without a flag. Default None, not TIMER8_HZ, so
    # key() resolves it the same way render does and every snapshot already on
    # disk keeps its key.
    ap.add_argument("--timer8-hz", type=int, default=None, metavar="HZ",
                    help="TIM8 rate to warm up with; must match the "
                         "render's --timer8-hz")
    ap.add_argument("--seconds", type=float, default=WARM_SECONDS,
                    metavar="S", help="emulated seconds to warm up for "
                                      "(default: %.1f)" % WARM_SECONDS)
    ap.add_argument("--rebuild", action="store_true",
                    help="rebuild even if a matching snapshot is cached")
    ap.add_argument("--list", action="store_true", help="list cached snapshots")
    ap.add_argument("--clear", action="store_true", help="delete all cached snapshots")
    ap.add_argument("--flash", metavar="FLASH.bin", default=None,
                    help="external-flash image to warm up with. A snapshot "
                         "built without one does not match a real render")
    ap.add_argument("--renode", default=PATHS.RENODE, help="path to Renode")
    args = ap.parse_args()

    if args.list:
        rows = cached()
        if not rows:
            print("no warm snapshots in %s" % PATHS.CACHE)
            return 0
        total = 0
        for full, size, mtime in rows:
            total += size
            print("%s  %6.1f MB  %s" % (os.path.basename(full), size / 1e6,
                                        time.strftime("%Y-%m-%d %H:%M",
                                                      time.localtime(mtime))))
            try:
                with open(full + ".json") as fh:
                    meta = json.load(fh)
            except (IOError, OSError, ValueError):
                print("    (no description file)")
                continue
            print("    firmware  %s" % os.path.basename(meta.get("firmware") or "-"))
            print("    flash     %s" % os.path.basename(meta.get("flash") or
                                                        "none (see `mfemu warm --help`)"))
            if meta.get("bootloader"):
                print("    through the bootloader")
        print("%d snapshot(s), %.1f MB, in %s" % (len(rows), total / 1e6,
                                                  PATHS.CACHE))
        return 0

    if args.clear:
        rows = cached()
        for full, _, _ in rows:
            os.remove(full)
            if os.path.exists(full + ".json"):
                os.remove(full + ".json")
        print("removed %d snapshot(s) from %s" % (len(rows), PATHS.CACHE))
        return 0

    import mfemu.image as image
    firmware = image.resolve(args.firmware or PATHS.MAIN_RAW, args.image)
    # args.flash and args.timer8_hz, not None. `mfemu warm` had no --flash at
    # all and passed None here and to build(), so every snapshot it made was
    # keyed on "no flash" -- while `mfemu render --warm` always keys on the
    # flash image it is about to render with. The keys could never match, so
    # pre-warming did nothing at all, silently. build()'s own docstring
    # already said "leaving it out is not equivalent to passing one".
    #
    # timer8_hz was the same defect left standing: key() hashes it, render can
    # set it, and this command passed a hardcoded None. It bit less hard only
    # because None resolves to the default both sides use, so the keys agreed
    # until someone passed --timer8-hz to render. Found by sweeping the key's
    # six components against this parser's flags -- five had one, this did not.
    k = key(firmware, args.bootloader, args.timer8_hz, args.renode,
            args.seconds, args.flash)
    out = path(k)
    if os.path.exists(out) and not args.rebuild:
        print("already cached: %s  (%.1f MB). --rebuild makes a new one."
              % (out, os.path.getsize(out) / 1e6))
        return 0
    build(firmware, args.bootloader, args.timer8_hz, args.renode,
          args.seconds, flash=args.flash)
    return 0


if __name__ == "__main__":
    sys.exit(main())
