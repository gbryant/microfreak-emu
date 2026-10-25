#!/usr/bin/env python3
"""Where this project's data lives, in one place.

Everything used to default to `~/Downloads`: the factory banks, the firmware
bundle, and the render directories. That made the repository unmovable -- paths
were hardcoded across nine modules and in `boot.resc` -- and it scattered the
project across a folder full of unrelated downloads. It now all lives under
`data/` beside the code, and each subfolder is a DROP FOLDER:

    data/
      firmware/     Arturia's update. The .mff as downloaded, a headered .bin,
                    a bare payload, or the whole unpacked bundle folder -- the
                    main application is found inside whichever you have.
      bootloader/   optional. A dump of your own instrument's low 48 KB.
      presets/      optional. MCC preset exports (.mfprojz), for presets to
                    render.
      samples/      optional. An MCC sample export (.mfsbz), loaded into
                    every image so the Sample and Grains engines can play.
      wavetables/   optional. An MCC wavetable export (.mfwbz). Without one
                    the factory wavetables come from the firmware file.
      midi/         optional. MIDI files for `render --midi`, which looks
                    here first, so a bare filename is enough.

The three are named as MIDI Control Center names them. Each uses the export
inside it only when there is exactly ONE. A subfolder is never read, so
`presets/disabled/` and the like are where to park the ones not in use.

**Nothing here needs an environment variable.** Put the file in the folder and
it is used; pass `--firmware`/`--bootloader`/`--source` to override for one
command. There used to be MF_DATA, MF_FIRMWARE, MF_FIRMWARE_IMAGE, MF_BANKS
and MF_BOOTLOADER as well, which is five ways to say one thing and a first run
that began with `export`. Dragging a download into a folder is the whole setup
now. If the data lives on another volume, make `data` a symlink to it --
`.gitignore` covers a symlink as well as a directory, which it did not until
the trailing slash came off.

`data/` is not tracked by git -- it is ~9 GB of firmware, renders and audio --
so a fresh clone has the code but not the data. **`git clean -xdf` would delete
all of it.** Back it up rather than relying on the repository.

Renode and Ghidra are NOT here. They are third-party, architecture-specific
applications that belong wherever they were installed, so where they are is a
fact about the MACHINE rather than about this project -- which is why they
keep their environment variables. Both are searched for in the usual places
-- /Applications first, then ~/Downloads -- and `MF_RENODE` / `MF_GHIDRA`
override the search. `renode/render.sh` reads MF_RENODE from the shell, where
a config file would need a parser.
"""
import glob
import os

# src/mfemu/paths.py -> src/mfemu -> src -> the repository root.
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# One blessed location, no environment variable. `data/` sits beside the code
# and each subfolder is a DROP FOLDER: put a file in, it gets used. There used
# to be MF_DATA, MF_FIRMWARE, MF_FIRMWARE_IMAGE, MF_BANKS and MF_BOOTLOADER on
# top of this, which meant five ways to say the same thing and a first run that
# began with `export`. A casual user should be able to drag their Arturia
# download into a folder and press go.
#
#   data/firmware/     the Arturia update. A .mff as downloaded, or the
#                      unpacked bundle -- the MAIN APPLICATION is found inside
#                      either (see find_main_image).
#   data/bootloader/   optional. A dump of your own instrument's low 48 KB,
#                      needed only to emulate a firmware update.
#   data/presets/      optional. MCC preset exports, for presets to render.
#   data/samples/      optional. An MCC sample export, for the Sample and
#                      Grains engines.
#   data/wavetables/   optional. An MCC wavetable export. Without one the
#                      factory wavetables come out of the firmware file.
#
# A per-invocation `--firmware`/`--bootloader`/`--source`/`--samples`/
# `--wavetables` still overrides, so the layering is two deep: explicit flag,
# then the drop folder. Renode and
# Ghidra keep MF_RENODE/MF_GHIDRA because they are third-party installs whose
# location is a property of the MACHINE, not of this project, and render.sh
# reads MF_RENODE from the shell where a config file would need a parser.
DATA = os.path.join(REPO, "data")
FIRMWARE_DIR = os.path.join(DATA, "firmware")
BOOTLOADER_DIR = os.path.join(DATA, "bootloader")
# Named the way MIDI Control Center names them: its MicroFreak templates are
# `Presets`, `Samples` and `Wavetables`. This folder was `data/banks/` until
# 2026-10-19, which read as if samples and wavetables did not belong in it.
PRESETS_DIR = os.path.join(DATA, "presets")
SAMPLES_DIR = os.path.join(DATA, "samples")
WAVETABLES_DIR = os.path.join(DATA, "wavetables")
# Not a drop folder in the one-file sense: it holds any number of .mid files,
# and `render --midi NAME` looks here before anywhere else. See find_midi().
MIDI_DIR = os.path.join(DATA, "midi")
MIDI_SUFFIXES = (".mid", ".midi")

# Each of the three is a drop folder like firmware/: put ONE export in it and
# it becomes the default for its flag. Several, and there is no default --
# choosing between somebody's exports is not ours to guess, and the flag is
# one word. Subfolders are never read, so `presets/disabled/` is where the
# ones not in use are parked, by hand.
#
# What used to be here was the research corpus by name: PRESET_BANK pinned to
# `factory_6_preset_bank.mfprojz` with a comment forbidding anyone to repoint
# it (experiment 1's baseline was measured against that exact file),
# SAMPLE_BANK, WAVETABLE_BANK, a four-entry FACTORY_BANKS spanning every
# firmware generation, and factory_presets() to walk the union of 448
# payloads. Zero of them were used by anything in src/. They belong to the
# research repository, which is where they went -- a released emulator should
# not ship a constant naming a file only one person has, nor a research
# constraint about which measurements it would invalidate. PRESET_FILE,
# SAMPLE_FILE and WAVETABLE_FILE below are not those constants come back:
# each is resolved from whatever the user put in the folder.
PRESET_SUFFIXES = (".mfprojz", ".mfpz")
SAMPLE_SUFFIXES = (".mfsbz",)
WAVETABLE_SUFFIXES = (".mfwbz",)


def exports_in(dirpath, suffixes):
    """-> every export in the folder with one of `suffixes`, sorted.

    Files only, top level only: a subfolder such as `disabled/` is how a user
    parks the exports they are not using, so it is never descended into.
    """
    if not os.path.isdir(dirpath):
        return []
    return [os.path.join(dirpath, n) for n in sorted(os.listdir(dirpath))
            if n.lower().endswith(suffixes)
            and os.path.isfile(os.path.join(dirpath, n))]


def _only(found):
    return found[0] if len(found) == 1 else None


def presets_available():
    """-> every preset export in `data/presets/`."""
    return exports_in(PRESETS_DIR, PRESET_SUFFIXES)


def samples_available():
    """-> every sample export in `data/samples/`."""
    return exports_in(SAMPLES_DIR, SAMPLE_SUFFIXES)


def wavetables_available():
    """-> every wavetable export in `data/wavetables/`."""
    return exports_in(WAVETABLES_DIR, WAVETABLE_SUFFIXES)


def find_presets():
    """-> the one preset export in `data/presets/`, or None.

    None for an empty folder AND for an ambiguous one. The caller already has
    a good message for "no presets" -- it names `mfreak preset backup` and MCC
    both -- so this does not raise; it just declines to pick.
    """
    return _only(presets_available())


def find_samples():
    """-> the one sample export in `data/samples/`, or None.

    The same rule as the presets. Several is worth saying out loud, though,
    because the cost of no samples is not an error -- 221 of the 384 presets
    in MCC's Factory6.0 use the Sample or a Grains engine, and every one of
    them renders silent, under a success line, without samples. See
    drop_folder_warning().
    """
    return _only(samples_available())


def find_wavetables():
    """-> the one wavetable export in `data/wavetables/`, or None.

    None is fine here: mkflash then places the factory wavetables from image
    4 of the firmware file. Measured 2026-10-19 that this changes nothing for
    the factory set -- MCC's installed `Factory` wavetables and image 4 hold
    the same 16 tables, audio and names, and flash images built either way
    are byte-identical. The folder is for your own.
    """
    return _only(wavetables_available())


def drop_folder_warning(kind, chosen):
    """-> a note for an ambiguous samples/ or wavetables/ folder, or None.

    `kind` is "samples" or "wavetables"; `chosen` is what the command ended
    up with, flag or default. Only the ambiguous folder gets a note: an empty
    one is a legitimate setup, and an explicit flag needs no commentary.
    """
    if chosen:
        return None
    folder, avail, cost = {
        "samples": (SAMPLES_DIR, samples_available(),
                    "none was loaded and the Sample and\nGrains engines will "
                    "render silent"),
        "wavetables": (WAVETABLES_DIR, wavetables_available(),
                       "the factory wavetables from the\nfirmware file are "
                       "used instead"),
    }[kind]
    if len(avail) < 2:
        return None
    return ("note: %s holds %d %s exports, so %s. Pass --%s, or move all but "
            "one into\n%s:\n%s"
            % (folder, len(avail), kind[:-1], cost, kind,
               os.path.join(folder, "disabled"),
               "\n".join("  %s" % os.path.basename(b) for b in avail)))


def midi_available():
    """-> every MIDI file in `data/midi/`."""
    return exports_in(MIDI_DIR, MIDI_SUFFIXES)


def find_midi(name):
    """-> the file `render --midi NAME` means, or raise SystemExit naming why.

    `data/midi/` is searched FIRST, then `name` as given -- relative to the
    shell's directory, `~` expanded. Each place is tried with the name as
    typed and then with `.mid` added, so `--midi ode_to_joy` works too.

    Folder first is deliberate: the point is that a reference tune is one
    word on the command line from anywhere. The cost is that a same-named
    file in the current directory is shadowed; pass `./name.mid` to mean it.
    A name containing a directory separator skips the folder, so that
    spelling is always unambiguous.
    """
    expanded = os.path.expanduser(name)
    places = []
    if os.sep not in name and "/" not in name:
        places.append(os.path.join(MIDI_DIR, name))
    places.append(expanded)
    for base in places:
        for cand in (base,) + tuple(base + s for s in MIDI_SUFFIXES
                                    if not base.lower().endswith(MIDI_SUFFIXES)):
            if os.path.isfile(cand):
                return os.path.abspath(cand)
    avail = midi_available()
    raise SystemExit(
        "--midi %s: no such file in %s or the current directory.%s"
        % (name, MIDI_DIR,
           "\nIn %s:\n%s" % (MIDI_DIR, "\n".join(
               "  %s" % os.path.basename(m) for m in avail)) if avail else
           "\nDrop .mid files in %s and name them by filename alone."
           % MIDI_DIR))


PRESET_FILE = find_presets()
SAMPLE_FILE = find_samples()
WAVETABLE_FILE = find_wavetables()


def _first_existing(candidates, env):
    """The first of these that exists, or the first as a last resort.

    Third-party tools land wherever the person installing them chose --
    /Applications from a drag-and-drop, ~/Downloads from an unzip in place,
    /opt/homebrew from a cask. Searching beats hardcoding one and being wrong
    on the next machine, which is exactly what happened with Renode: it moved
    to /Applications and paths.py went on looking in ~/Downloads.
    """
    if os.environ.get(env):
        return os.environ[env]
    for c in candidates:
        c = os.path.expanduser(c)
        if os.path.exists(c):
            return c
    return os.path.expanduser(candidates[0])


# Where the application image is found, and how. `data/firmware/` is a drop
# folder: put the Arturia download in it, in whatever shape you have, and this
# finds the main application inside.
#
#   update.mff                 the package as downloaded -- image 0 is main
#   nanowave_main__*.bin       one image with Arturia's 64-byte header
#   payloads/main.raw          a bare payload, if someone stripped headers
#
# The hard part is that the bundle holds FIVE images and a `payloads/` tree,
# and a dump of the bootloader may be sitting nearby. Matching on filename
# would be guessing, so the test is architectural: the main application is the
# only image whose RESET VECTOR lands in the application window. Measured
# across the 5.0.0.2084 bundle:
#
#   payloads/main.raw          473695 B   reset 0x0805F849   <- in  [APP]
#   bootloader_5_0_0_2084.bin   49152 B   reset 0x080014F5       below
#   payloads/iochip_kbd.raw     26863 B   reset 0x080000D1       below
#   payloads/iochip_matrix.raw  29679 B   reset 0x080000D1       below
#   payloads/iochip_oled.raw    81647 B   reset 0x080000D5       below
#
# so one rule rejects the co-processors, the bootloader, the wavetable bank,
# info.json, the stray .py scripts and .DS_Store, with no list of names to keep
# up to date. The co-processors are separate chips; their vectors are nowhere
# near 0x0800C000, which is where this harness loads the app.
APP_BASE, APP_END = 0x0800C000, 0x08080000
_HEADER = 0x40                   # Arturia's header, ahead of the vector table


def _reset_vector_in_app_window(data):
    """Is this the main application image? -> bool.

    Checked at offset 0 and, for a headered image, at 0x40 -- the two places
    the vector table can begin. `image.looks_headered` is the same test used
    to strip it.
    """
    import struct
    headered = len(data) > _HEADER + 1 and not any(data[2:_HEADER])
    for off in (0, _HEADER) if headered else (0,):
        if len(data) >= off + 8:
            _sp, reset = struct.unpack_from("<II", data, off)
            if APP_BASE <= reset < APP_END:
                return True
    return False


def find_main_image(dirpath=None):
    """-> the one main-application image under `dirpath`, or None.

    Walks the drop folder. A ZIP is taken on trust as a container, because
    `image.resolve` reads its info.json to pick image 0. Everything else is
    judged by its reset vector.

    The bundle ships the same application TWICE -- `nanowave_main__*.bin` and,
    if headers were stripped, `payloads/main.raw` -- and both legitimately
    match. That is not an ambiguity to complain about: they reduce to the same
    bare payload, byte for byte (verified: both give sha256 54be2e0b...), so
    duplicates are collapsed by comparing the payload rather than the file.
    Only genuinely DIFFERENT images are reported as a conflict, because
    choosing between two firmware versions is not ours to guess.
    """
    import zipfile
    if dirpath is None:
        dirpath = FIRMWARE_DIR
    if not os.path.isdir(dirpath):
        return None
    hits = []
    for root, _dirs, names in os.walk(dirpath):
        for n in sorted(names):
            path = os.path.join(root, n)
            try:
                if zipfile.is_zipfile(path):
                    hits.append(path)
                    continue
                with open(path, "rb") as fh:
                    head = fh.read(_HEADER + 8)
                if _reset_vector_in_app_window(head):
                    hits.append(path)
            except OSError:
                continue          # unreadable is not a candidate
    if len(hits) <= 1:
        return hits[0] if hits else None
    # More than one. Collapse the shapes that carry the same payload, and when
    # they do, hand back the CHEAPEST shape rather than whichever os.walk
    # reached first.
    #
    # The order matters beyond tidiness. A bare payload needs no unpacking and
    # no checksum verification, and `MAIN_RAW` is read as bare bytes directly
    # by test_encoder_roundtrip, which patches the image and writes it back --
    # given the headered .bin it produced a file whose header checksum no
    # longer matched its body, and image.resolve refused it:
    # "main-fixed.raw: checksum mismatch (computed E9, header D2, trailer D2)".
    # Walk order decided which shape it got, so the suite's result depended on
    # a directory listing. Ranked explicitly now.
    import mfemu.image as _image

    def shape_rank(path):
        import zipfile
        if zipfile.is_zipfile(path):
            return 2                      # container: unpack to use
        with open(path, "rb") as fh:
            head = fh.read(_HEADER + 8)
        headered = len(head) > _HEADER + 1 and not any(head[2:_HEADER])
        return 1 if headered else 0       # 0 = bare payload, usable as-is

    byhash = {}
    for h in sorted(hits, key=lambda p: (shape_rank(p), p)):
        try:
            resolved = _image.resolve(h, quiet=True)
            with open(resolved, "rb") as fh:
                import hashlib
                key = hashlib.sha256(fh.read()).hexdigest()
        except SystemExit:
            continue              # not really an image after all
        byhash.setdefault(key, h)
    if not byhash:
        return None
    if len(byhash) == 1:
        return next(iter(byhash.values()))
    raise SystemExit(
        "%s holds %d different firmware images:\n%s\n\n"
        "Leave one in the folder, or name the one you want with --firmware."
        % (dirpath, len(byhash),
           "\n".join("  %s" % os.path.relpath(v, dirpath)
                      for v in sorted(byhash.values()))))


MAIN_RAW = find_main_image()

# The 48 KB bootloader, read off a device. NOT in the firmware bundle and not
# in this repository: Arturia ship the application only, and the bootloader is
# never updated, so the only source is a dump of your own instrument -- see
# `mfreak peek dump-bootloader` in microfreak-tools, and docs/BOOTLOADER.md.
#
# Nothing needs it to boot the application: the harness loads the app at
# 0x0800C000 and sets the vector table there, which is what the bootloader
# itself would have done. It is needed to emulate the FIRMWARE UPDATE, because
# everything after the app's reset-into-DFU runs in the bootloader.
BOOTLOADER_BASE = 0x08000000
BOOTLOADER_SIZE = 48 << 10


def find_bootloader(dirpath=None):
    """-> the one bootloader dump in `data/bootloader/`, or None.

    Its own folder, NOT under `data/firmware/`, which is where it used to
    live. Two reasons, and the first is a real hazard: `find_main_image` walks
    the firmware folder, so a 48 KB bootloader sitting inside it is a file the
    scanner has to be careful to reject -- and a wrong answer there means
    booting the bootloader as though it were the application. Separate folders
    make that impossible rather than merely handled. The second is that it is
    not part of Arturia's download at all: it comes off your own instrument,
    so it is a different KIND of thing from the update package.

    Size is the filter: the bootloader occupies sectors 0-2, 48 KB exactly.
    Anything else in the folder is reported rather than silently ignored,
    because a near-miss here is usually a truncated dump.
    """
    if dirpath is None:
        dirpath = BOOTLOADER_DIR
    if not os.path.isdir(dirpath):
        return None
    exact, wrong = [], []
    for n in sorted(os.listdir(dirpath)):
        path = os.path.join(dirpath, n)
        if not os.path.isfile(path):
            continue
        size = os.path.getsize(path)
        (exact if size == BOOTLOADER_SIZE else wrong).append((n, size))
    if len(exact) == 1:
        return os.path.join(dirpath, exact[0][0])
    if len(exact) > 1:
        raise SystemExit(
            "%s holds %d bootloader-sized files:\n%s\n\n"
            "Leave one, or name it with --bootloader."
            % (dirpath, len(exact),
               "\n".join("  %s" % n for n, _ in exact)))
    if wrong:
        raise SystemExit(
            "%s holds no bootloader. One is exactly %d bytes; found:\n%s\n\n"
            "A short file is usually an interrupted dump. Run "
            "`mfreak peek dump-bootloader` again."
            % (dirpath, BOOTLOADER_SIZE,
               "\n".join("  %-40s %d bytes" % (n, sz) for n, sz in wrong)))
    return None


BOOTLOADER = find_bootloader()
RENODE_DIR = os.path.join(REPO, "renode")

# Where warm snapshots and the generated Renode config live. Not in the repo:
# a snapshot is 25 MB, it is derived from Arturia's firmware, and it is
# specific to one machine's Renode build. MFEMU_CACHE overrides it.
CACHE = os.environ.get("MFEMU_CACHE") or os.path.expanduser("~/.cache/mfemu")


def renode_config():
    """-> a Renode config file path with the compiler cache switched ON.

    This exists for one reason, and it is the difference between warm
    snapshots working and not working. Renode compiles each `include *.cs`
    into a dynamic assembly named after a fresh GUID, and records that name in
    any snapshot it saves:

        STM32_QUADSPI, b41f99a7-8e12-4283-a1d3-47f5b331da7a.dll

    A second Renode process mints different GUIDs, so loading that snapshot
    sends the deserializer looking for an assembly that does not exist. It
    reports "This snapshot is incompatible or the emulation's state is
    corrupted" while printing two IDENTICAL version strings, which is a
    thoroughly misleading way to say "I cannot find your types".

    With `compiler-cache-enabled = True` the compiled assembly is cached and
    reused, so the GUID is stable across processes and the snapshot loads.
    Measured 2026-10-15: two separate processes both produced
    73860662-80ea-45f7-9178-6223ee8e315a.dll, and a snapshot saved by one
    loaded in the other.

    We write our own config rather than editing the user's: Renode stores its
    settings in a per-user file it also writes back to, and silently flipping a
    global setting of someone else's tool is not ours to do.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "renode.config")
    if not os.path.exists(path):
        with open(path, "w") as fh:
            fh.write("[general]\ncompiler-cache-enabled = True\n")
    return path

# There is no captures path here any more. The protocol recordings were how
# this project bootstrapped -- MCC's own traffic, decoded -- and nothing needs
# them now: a flash image builds from the firmware bundle alone, and the
# formats they established are in docs/FORMATS.md and docs/PROTOCOL.md with
# the reasoning that got there. Presets come from a preset export; see
# PRESET_FILE above.

# Third-party, architecture-specific, deliberately outside the project.
RENODE = _first_existing([
    "/Applications/Renode.app/Contents/MacOS",
    "~/Downloads/Renode.app/Contents/MacOS",
    "/opt/homebrew/opt/renode/libexec",
], "MF_RENODE")

# Ghidra is only needed to read firmware, never to render. It ships as a
# directory rather than an app bundle, so the version number is part of the
# path; the glob keeps this working across upgrades.
# Homebrew's `ghidra` formula first: it is the tidiest install and brings its
# own openjdk@21. Then the hand-unzipped layouts, newest version first.
# /Applications/Ghidra.app is only a click-through wrapper around ghidraRun --
# the actual tree lives in the Homebrew prefix, which is what analyzeHeadless
# needs, so the bundle is deliberately not searched here.
GHIDRA = _first_existing(
    ["/opt/homebrew/opt/ghidra/libexec", "/usr/local/opt/ghidra/libexec"] +
    sorted(glob.glob("/Applications/ghidra_*_PUBLIC"), reverse=True) +
    sorted(glob.glob(os.path.expanduser("~/Downloads/ghidra_*_PUBLIC")), reverse=True),
    "MF_GHIDRA")
ANALYZE_HEADLESS = os.path.join(GHIDRA, "support", "analyzeHeadless")
GHIDRA_SCRIPTS = os.path.join(REPO, "ghidra")

# Homebrew's `ghidraRun` shim sets JAVA_HOME before launching the GUI, but
# `support/analyzeHeadless` is invoked directly and gets no such help -- on a
# machine with no system Java it fails with "Unable to locate a Java Runtime",
# which looks like a broken Ghidra rather than a missing variable.
JAVA_HOME = _first_existing([
    "/opt/homebrew/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
    "/usr/local/opt/openjdk@21/libexec/openjdk.jdk/Contents/Home",
    "/Library/Java/JavaVirtualMachines/temurin-21.jdk/Contents/Home",
], "JAVA_HOME")


def ghidra_env():
    """-> an environment for subprocess that lets analyzeHeadless find Java."""
    env = dict(os.environ)
    if os.path.exists(JAVA_HOME):
        env.setdefault("JAVA_HOME", JAVA_HOME)
    return env


def generation(n):
    """-> data/genN, the directory for one generation's candidates and clips."""
    return os.path.join(DATA, "gen%d" % n)


def _empty_folder_hint():
    """-> what to say when a drop folder is empty, naming the folder."""
    return ("Nothing was found in the drop folders. Put the files in and no\n"
            "flag or variable is needed:\n\n"
            "  %s\n      Arturia's update -- the .mff as downloaded is fine\n"
            "  %s\n      optional: a dump of your own instrument's low 48 KB\n"
            "  %s\n      optional: MCC preset exports, for presets to render\n"
            "  %s\n      optional: an MCC sample export, for the Sample and\n"
            "      Grains engines (`mfreak asset import --only samples`)\n"
            "  %s\n      optional: an MCC wavetable export; the factory\n"
            "      wavetables come from the firmware file without one\n\n"
            "`mfemu paths` shows what resolved and what did not. If the data\n"
            "lives elsewhere, make `data` a symlink to it."
            % (FIRMWARE_DIR, BOOTLOADER_DIR, PRESETS_DIR, SAMPLES_DIR,
               WAVETABLES_DIR))


def resc(path):
    """-> `path` made absolute, for interpolation into a Renode script.

    Renode is launched with `cwd=<its own install directory>`, so a RELATIVE
    path written into a .resc is resolved against that, not against the user's
    shell. The symptom is not a sensible error:

        $ mfemu boot --bootloader ./data/bootloader/bootloader_5_0_0_2084.bin
        RuntimeError: Renode exited before opening the socket.

    and only Renode's own log, in TMPDIR, says why --

        Could not find a part of the path '/Applications/Renode.app/Contents/
        MacOS/data/bootloader/bootloader_5_0_0_2084.bin'

    -- which is the install directory with the user's relative path glued on.
    `--bootloader` with no argument worked, because that takes paths.BOOTLOADER
    and everything in here is already absolute; it was only a path the user
    typed that broke, which is the one shape the test suite never used.

    expanduser as well as abspath: `~/Downloads/x.bin` in quotes reaches us
    with the tilde intact, and Renode does not expand it either.
    """
    return os.path.abspath(os.path.expanduser(path))


def check(*paths):
    """Fail early and legibly rather than deep inside a render.

    A missing firmware bundle used to surface as a Renode error about a binary
    it could not load, several minutes into a batch.

    `None` is accepted and means "the drop folder is empty", which is a
    different message from "this path does not exist": there is no path to
    print, so printing one would be inventing it. Callers pass
    `PATHS.MAIN_RAW` straight in, and it is None on a fresh clone -- this used
    to reach `os.path.exists(None)` and raise TypeError, which is a traceback
    in front of the newcomer this whole layout is for.
    """
    if any(p is None for p in paths):
        raise SystemExit(_empty_folder_hint())
    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        raise SystemExit(
            "missing project data:\n" + "\n".join("  " + p for p in missing) +
            "\n\n" + _empty_folder_hint())


def main():
    """List every location this project uses, and what is missing."""
    import argparse
    ap = argparse.ArgumentParser(
        description=main.__doc__.strip(),
        epilog="Put files in the data/ folders to use them. MF_RENODE and "
               "MF_GHIDRA\noverride where Renode and Ghidra are found.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    # Takes nothing, but still parses: the hand-rolled version accepted and
    # silently ignored any flag you gave it.
    ap.parse_args()
    missing = 0
    # Grouped by whether you have to do anything about it, because a flat
    # list of fourteen paths gave a newcomer no way to tell the one required
    # thing from a research leftover.
    groups = [
        ("required", ("MAIN_RAW", "RENODE")),
        ("optional -- drop a file in to enable",
         ("BOOTLOADER", "PRESETS_DIR", "PRESET_FILE", "SAMPLES_DIR",
          "SAMPLE_FILE", "WAVETABLES_DIR", "WAVETABLE_FILE", "MIDI_DIR")),
        ("folders this project owns",
         ("REPO", "DATA", "FIRMWARE_DIR", "BOOTLOADER_DIR", "RENODE_DIR")),
        ("firmware reading, not needed to render",
         ("GHIDRA", "ANALYZE_HEADLESS", "JAVA_HOME")),
    ]
    for label, names in groups:
        print("\n%s" % label)
        for name in names:
            value = globals()[name]
            if value is None:
                print("  %-16s %-56s %s" % (name, "(nothing found)", "MISSING"))
                missing += label == "required"
                continue
            there = os.path.exists(value)
            missing += not there and label == "required"
            print("  %-16s %-56s %s" % (name, value, "ok" if there else "MISSING"))
    if MAIN_RAW is None:
        print("\nNo firmware. Put Arturia's .mff, as downloaded, in %s."
              % FIRMWARE_DIR)
    if missing:
        print("\n%d required path missing." % missing)
    return 0


if __name__ == "__main__":
    main()
