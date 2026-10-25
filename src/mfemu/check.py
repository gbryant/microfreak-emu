"""Check that this machine can run the emulator. Takes about a second.

    mfemu check

It checks five things and names the fix for any that fail:

  DATA        the firmware payload is where paths.py looks for it
  IDENTITY    it is firmware 5.0.0.2084, the only build the addresses fit
  RENODE      Renode is installed and runs, and which version it is
  ASSETS      the machine description and peripheral models are present
  PYTHON      numpy and microfreak-tools import

Preset, sample and wavetable exports are not checked. Booting does not need
them, and `mfemu render` says so when one is missing.
"""
import argparse
import hashlib
import os
import subprocess
import sys

import mfemu.image as image
import mfemu.paths as PATHS

# Renode assets that are this project's own work: without them there is no
# machine. Renode ships none of these.
ASSETS = ["microfreak.repl", "boot.resc", "otg_fs_reset.resc",
          "STM32_QUADSPI.cs", "STM32_SAI.cs", "STM32_OTGFS.cs",
          "STM32F4DMA.cs", "STM32F4_Flash.cs", "MicroFreakCodec.cs"]

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-46s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)
    return ok


def main():
    # argparse, not a hand-rolled scan of argv. The scan accepted anything:
    # `mfemu check --frimware FILE` ignored the typo and checked the DEFAULT
    # firmware, then printed ok -- a green result about a file the user did
    # not name. `--firmware` with no value did the same. Both are now
    # refused with exit 2.
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", metavar="FILE",
                    help="check this payload instead of the default. It "
                         "may be a build other than 5.0.0.2084")
    args = ap.parse_args()
    # An explicitly named firmware is allowed to be something other than
    # the supported build -- being able to check somebody else's is the
    # point of accepting one. The default must not be.
    given = args.firmware
    print("1. the firmware")
    source = given or PATHS.MAIN_RAW
    # `source` is None when nothing is in the drop folder, which is the state
    # a fresh clone is in -- and this is the command people run first, so it
    # must REPORT that rather than raise. os.path.exists(None) is a TypeError.
    have = source is not None and os.path.exists(source)
    chk(have, "exists", source if source is not None
        else "nothing in %s" % PATHS.FIRMWARE_DIR)
    if have and given:
        print()
        image.describe(source)
    if not have:
        print("""
       Not found. Download the 5.0.0.2084 update from Arturia and put the
       .mff, as downloaded, in

         %s

       `mfemu check --firmware FILE` checks a file somewhere else.""" % PATHS.FIRMWARE_DIR)
        return 1

    # Reported, never required. The bootloader is optional -- nothing needs
    # it to boot the application -- but SILENCE about it was the problem: a
    # user who had put a dump in data/bootloader/ had no way to learn from
    # `check` whether it had been picked up, and `--bootloader` with no
    # argument is the one invocation that depends on it. Saying "not present,
    # and that is fine" costs two lines and answers the question.
    print("\n2. the bootloader (optional)")
    if PATHS.BOOTLOADER is None:
        print("  --   none in %s" % PATHS.BOOTLOADER_DIR)
        print("       Only `mfemu dfu` needs it. Read it off your instrument "
              "with\n"
              "       `mfreak peek dump-bootloader`.")
    else:
        size = os.path.getsize(PATHS.BOOTLOADER)
        chk(size == PATHS.BOOTLOADER_SIZE,
            "found, %d bytes" % size,
            os.path.basename(PATHS.BOOTLOADER))
        if size != PATHS.BOOTLOADER_SIZE:
            print("       A MicroFreak bootloader is exactly %d bytes "
                  "(sectors 0-2).\n"
                  "       A short file is usually a dump that stopped early."
                  % PATHS.BOOTLOADER_SIZE)
        else:
            print("       Used by `--bootloader` with no argument.")

    print("\n3. is it 5.0.0.2084?")
    try:
        from microfreak.firmware.patching import KNOWN_PAYLOAD_SHA256, KNOWN_PAYLOAD_SIZE
    except Exception as exc:
        chk(False, "microfreak-tools importable", repr(exc))
        print("\n       `uv sync` installs it from ../microfreak-tools.")
        return 1
    # Of the PAYLOAD, so a container or a headered .bin is unpacked first.
    custom_firmware = False
    blob = open(image.resolve(source, quiet=True), "rb").read()
    got = hashlib.sha256(blob).hexdigest()
    # Informational on the `given` path, exactly as the hash is below. A
    # named --firmware "is allowed to be a build other than 5.0.0.2084" --
    # this function's own promise -- and a custom build is under no obligation
    # to be 473,695 bytes. The hash was relaxed for that and the size was not,
    # so any custom build of a different size ended NOT READY. The READY line
    # saying "custom firmware (size ok, ...)" was the tell: it assumed a size
    # match that nothing guaranteed.
    if given and len(blob) != KNOWN_PAYLOAD_SIZE:
        print("  note %-46s %d bytes (supported build is %d)"
              % ("not the supported build's size", len(blob),
                 KNOWN_PAYLOAD_SIZE))
        size_ok = True
    else:
        size_ok = chk(len(blob) == KNOWN_PAYLOAD_SIZE,
                      "size is %d bytes" % KNOWN_PAYLOAD_SIZE,
                      "%d" % len(blob))
    if got != KNOWN_PAYLOAD_SHA256 and given:
        # Named on purpose, so a mismatch is information, not a failure.
        print("  note %-46s %s" % ("not the supported build", got[:32] + "..."))
        for line in (
                "",
                "       Expected for custom firmware. It will boot, but the addresses",
                "       in docs/ and the boot hooks are for 5.0.0.2084 and may not fit."):
            print(line)
        sha_ok = True
        custom_firmware = True
    else:
        sha_ok = chk(got == KNOWN_PAYLOAD_SHA256,
                     "sha256 matches the supported build", got[:32] + "...")
    if not (size_ok and sha_ok):
        print("""
       This is not 5.0.0.2084. It may boot, but the emulated hardware and
       the addresses in docs/ are for that build. Expected
         %s""" % KNOWN_PAYLOAD_SHA256)

    print("\n4. Renode")
    found = os.path.isdir(PATHS.RENODE)
    chk(found, "installation directory", PATHS.RENODE)
    version = ""
    if found:
        import mfemu.mfrender as mfrender
        try:
            out = subprocess.run(mfrender.renode_command(PATHS.RENODE) + ["--version"],
                                 cwd=PATHS.RENODE, capture_output=True, text=True,
                                 timeout=120)
            version = (out.stdout + out.stderr).strip().splitlines()
            version = version[0] if version else ""
            chk(bool(version), "runs and reports a version", version)
        except Exception as exc:
            chk(False, "runs and reports a version", repr(exc))
    else:
        print("""
       Install Renode from renode.io or with `brew install renode`.
       MF_RENODE points at an installation elsewhere.""")

    print("\n5. the machine description and the peripheral models")
    for name in ASSETS:
        p = os.path.join(PATHS.RENODE_DIR, name)
        chk(os.path.exists(p) and os.path.getsize(p) > 0, name,
            "%d bytes" % os.path.getsize(p) if os.path.exists(p) else "missing")

    print("\n6. Python side")
    for mod in ("numpy", "microfreak.preset.tlv", "mfemu.mkflash", "mfemu.midilink"):
        try:
            __import__(mod)
            chk(True, mod)
        except Exception as exc:
            chk(False, mod, repr(exc))

    print()
    if fails:
        print("NOT READY: %s" % ", ".join(fails))
        return 1
    # Not unconditionally "verified by hash": on the custom-firmware path the
    # hash did NOT match, and saying it did contradicted the note printed a
    # few lines earlier in the same run.
    print("READY - %s, %s"
          % (version or "Renode found",
             "custom firmware (hash is NOT 5.0.0.2084 -- addresses in "
             "docs/ may not describe it)" if custom_firmware
             else "firmware 5.0.0.2084 verified by hash"))
    # Carry `--firmware` forward. Whoever had to name the image here has
    # nothing in the drop folder, so a bare `mfemu boot` is the one command
    # guaranteed to fail for them -- and it was the command this line told
    # them to run.
    print("Next: `mfemu boot%s` actually boots it (about 7 s)."
          % (" --firmware %s" % given if given else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
