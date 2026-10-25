#!/usr/bin/env python3
"""`mfemu`'s own command surface: help text, usage lines, and bad input.

    uv run python tests/test_cli_surface.py

The fastest test here by a wide margin -- no Renode, no firmware, about half
a second -- and the only one that can run on a machine with nothing set up.
Each check was confirmed able to fail by breaking one thing at a time.

It exists because two commands had hand-rolled argument parsing that
accepted anything:

    mfemu check --frimware FILE

scanned argv for `--firmware`, did not find it, and checked the DEFAULT
firmware instead -- then printed READY. A green result about a file the user
never named is the worst kind of wrong answer, and no amount of reading
`check.py` makes it obvious. `paths` ignored unknown flags the same way.
Both now use argparse, and this holds every command to having a real parser
by checking that `--help` prints a usage line.

It also holds the three commands that used to answer a path typo with a
traceback -- `image`, `render --source`, `flash --presets-from` -- to the
one-line refusal `cli.main` now gives them.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

from mfemu.cli import ROUTES, SUMMARY              # noqa: E402

NOPE = "/nonexistent/definitely-not-here.raw"
# This test is the one that needs no data, so the preset export is OPTIONAL:
# the cases that need it are skipped and counted when it is absent, rather
# than making the whole file depend on it being there.
# `is None` explicitly rather than leaning on the except: paths.find_presets
# returns None for an empty OR an ambiguous presets folder, and
# os.path.exists(None) raises TypeError -- which the broad `except Exception`
# below would have swallowed into "no presets", hiding the real reason.
from mfemu.paths import PRESET_FILE as PRESETS           # noqa: E402
if PRESETS is not None and not os.path.exists(PRESETS):
    PRESETS = None
HELP_FLOOR = 150

fails = []


def chk(ok, label, detail=""):
    print("  %-4s %-50s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)


# `-c`, not `-m mfemu.cli`. On Python 3.14.6 and 3.15.0b4, argparse's
# `_prog_name()` returns f'{py} -m {modname}' whenever `__main__.__spec__`
# names a module, ignoring the `sys.argv[0]` this dispatcher sets to "mfemu" --
# so every command printed `usage: python3.14 -m mfemu.cli` and the usage-line
# check failed on all 9. 3.14.7 and 3.15.0rc2 added an `arg0 != modspec.origin`
# guard that restores the argv[0] branch, but requires-python is
# ">=3.11,<3.15", so 3.14.6 is supported and this must not depend on the patch
# release. `-c` leaves `__spec__` None on every version, which is also what the
# installed `mfemu` console script does. See the long note in
# microfreak-tools/tests/test_cli_surface.py.
_VIA_MAIN = "from mfemu.cli import main; raise SystemExit(main())"


def mfemu(args, timeout=300):
    return subprocess.run([sys.executable, "-c", _VIA_MAIN] + args,
                          capture_output=True, text=True, timeout=timeout,
                          cwd=ROOT,
                          env=dict(os.environ,
                                   PYTHONPATH=os.path.join(ROOT, "src")))


def names():
    return sorted(" ".join(k) if isinstance(k, tuple) else k for k in ROUTES)


def test_help():
    print("\n1. every command's --help")
    broken, thin, misnamed = [], [], []
    for name in names():
        r = mfemu(name.split() + ["--help"])
        out = r.stdout + r.stderr
        if r.returncode != 0 or "Traceback" in out:
            broken.append(name)
            continue
        if "usage:" not in out:
            # No usage line means no real parser, which is how `check` and
            # `paths` silently swallowed unknown flags.
            misnamed.append("%s (no usage line)" % name)
        elif not out.splitlines()[0].startswith("usage: mfemu %s" % name):
            misnamed.append("%s prints %r" % (name, out.splitlines()[0][:40]))
        if len(out.strip()) < HELP_FLOOR:
            thin.append("%s (%d chars)" % (name, len(out.strip())))
    chk(not broken, "every --help runs", ", ".join(broken) or "%d commands" % len(ROUTES))
    chk(not misnamed, "every --help prints a usage line naming the command",
        "; ".join(misnamed) or "%d commands" % len(ROUTES))
    chk(not thin, "no --help is under %d characters" % HELP_FLOOR,
        ", ".join(thin) or "all above the floor")


def test_summary():
    print("\n2. the top-level help")
    missing = [n for n in names() if n not in SUMMARY]
    chk(not missing, "`mfemu` with no arguments names every command",
        ", ".join(missing) or "%d commands" % len(ROUTES))


def test_unknown_flags_are_refused():
    print("\n3. unknown arguments")
    bad = []
    for name, args in (("check", ["--frimware", NOPE]),
                       ("paths", ["--nonsense"]),
                       ("image", ["--nonsense"])):
        r = mfemu([name] + args)
        if r.returncode == 0:
            bad.append("%s %s exited 0" % (name, " ".join(args)))
    chk(not bad, "a misspelled flag is refused, not ignored",
        "; ".join(bad) or "3 cases")


def test_bad_input_is_refused():
    print("\n4. missing files")
    cases = [("image", [NOPE]),
             # --preset 1, not 0: 0 is now refused on its own as "there is no
             # preset 0", which would make this case pass without the missing
             # --source file ever being reached.
             ("render", ["--source", NOPE, "--preset", "1"]),
             ("flash", ["--presets-from", NOPE]),
             ("check", ["--firmware", NOPE])]
    crashed = []
    for name, args in cases:
        r = mfemu([name] + args)
        if "Traceback (most recent call last)" in r.stdout + r.stderr:
            crashed.append("%s %s" % (name, " ".join(args)))
    chk(not crashed, "no command answers a path typo with a traceback",
        ", ".join(crashed) or "%d cases" % len(cases))

    # A bad --preset, which needs a readable preset export to reach resolve().
    # Neither of these was covered and both answered with a traceback: the
    # name since well before the panel change, the 0 because of it.
    if PRESETS is None:
        chk(True, "bad --preset values are refused",
            "skipped: no preset export in data/presets/")
    else:
        rude = []
        for spec in ("NoSuchPresetName", "0", "99999"):
            r = mfemu(["render", "--source", PRESETS, "--preset", spec,
                       "--notes", "60", "--out", os.devnull, "--dry-run"])
            if "Traceback (most recent call last)" in r.stdout + r.stderr:
                rude.append("--preset %s" % spec)
        chk(not rude, "bad --preset values are refused, not raised",
            ", ".join(rude) or "3 cases")


def main():
    print(__doc__.splitlines()[0])
    for t in (test_help, test_summary, test_unknown_flags_are_refused,
              test_bad_input_is_refused):
        t()
    print("\n%s" % ("FAIL - %d check(s) failed" % len(fails) if fails
                    else "PASS - mfemu's surface matches its documentation"))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
