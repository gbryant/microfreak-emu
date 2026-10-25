#!/usr/bin/env python3
"""mfemu -- run the MicroFreak firmware under Renode.

Each command is a module with its own `main()` and argument parsing. This
file only picks which one runs.
"""
import importlib
import sys

__version__ = "0.0.0"

ROUTES = {
    ("check",):   "mfemu.check",
    ("paths",):   "mfemu.paths",
    ("boot",):    "mfemu.boot",
    ("render",):  "mfemu.mfrender",
    ("flash",):   "mfemu.mkflash",
    ("image",):   "mfemu.image",
    ("dfu",):     "mfemu.dfu",
    ("serve",):   "mfemu.serve",
    ("panel",):   "mfemu.frontpanel",
    ("warm",):    "mfemu.warm",
}

SUMMARY = """usage: mfemu <command> [options]

  Setup:
    check                  is this machine set up? (about a second)
    paths                  where everything resolves to, and what is missing
    image FILE             list the images inside a firmware file

  Run:
    render                 render presets to .wav, from a MIDI file or notes
    boot                   boot the firmware and report what happened
    serve                  boot and keep running, for mfreak to talk to
    panel                  press buttons and turn knobs on a running serve
    flash                  build a backing image for the external SPI flash
    dfu                    run a firmware update against the emulator
    warm                   manage the cached snapshots renders start from

  Render:
    mfemu render --source BANK.mfprojz --list
    mfemu render --source BANK.mfprojz --preset 72 --notes 60,67 --out a.wav
    mfemu render --source BANK.mfprojz --preset Buzzsaw --midi p.mid --out b.wav

  The first render boots for about a minute. Later ones skip that.

  Drive the emulator with mfreak:
    mfemu serve --preset 8=Speech                  (leave this running)
    MFREAK_EMULATOR=1 mfreak preset read 8         (in another shell)

  You supply the firmware. A .mff works as downloaded, with nothing to
  extract. Renode is a separate install: renode.io or `brew install renode`.

`mfemu <command> --help` shows that command's options.
"""


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(SUMMARY, end="")
        return 0
    if argv[0] in ("-V", "--version"):
        print("mfemu %s" % __version__)
        return 0

    key = (argv[0],)
    if key not in ROUTES:
        print("mfemu: unknown command %r\nknown commands: %s\n\n"
              "Try `mfemu --help`."
              % (argv[0], ", ".join(sorted(k[0] for k in ROUTES))),
              file=sys.stderr)
        return 2

    module = importlib.import_module(ROUTES[key])
    saved = sys.argv
    sys.argv = ["mfemu " + argv[0]] + argv[1:]
    try:
        return module.main() or 0
    except (FileNotFoundError, IsADirectoryError, PermissionError) as exc:
        # A missing path is a typo, not a bug: print it without a traceback.
        where = getattr(exc, "filename", None)
        print("%s%s" % ("%s: " % where if where else "",
                        exc.strerror or exc), file=sys.stderr)
        return 1
    finally:
        sys.argv = saved


if __name__ == "__main__":
    sys.exit(main())
