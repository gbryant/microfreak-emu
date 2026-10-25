#!/usr/bin/env python3
"""Show what is inside a firmware file, or extract its payload.

Arturia ship a `.mff`, custom-firmware projects publish `.mff`, and
`mfreak fw patch` writes `.mff`. Until now the harness wanted the bare payload
-- the inner image with its 64-byte header stripped -- which meant everyone
unpacked a container by hand before they could boot anything. That is a bad
first step for a stranger and a worse one for testing somebody else's build.

Three shapes are accepted, told apart by looking rather than by extension:

    update.mff      a container: a Stored ZIP of five images plus info.json,
                    which maps image_num -> file_name. Image 0 is the main
                    application; the others are the two IO co-processors, the
                    OLED and the factory wavetable bank.
    image.bin       one image WITH Arturia's header: 64 bytes, then the
                    payload, then one byte repeating the header's XOR-8. Both
                    copies of that checksum are verified.
    main.raw        a bare payload, which is what the harness used to demand.

The discriminator for a header is the one `unpackage_bytes` already relies on:
bytes 2 through 0x3F are zero in a header and are the vector table in a bare
payload, so they are never both.

Extraction is cached by content under TMPDIR, so re-running a render does not
re-unpack, and two different containers cannot collide.

The reading and the checksum verification are microfreak-tools' -- `mff.read`
and `image.looks_packaged` -- rather than a second implementation
here. That is the point of depending on the tool.
"""
import hashlib
import json
import os
import sys
import zipfile

MAIN_IMAGE = 0
HEADER = 0x40
CACHE = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-images")

# What each image_num is, for a legible listing. From info.json across the
# 5.0.0.2084 bundle; the numbering is Arturia's, not ours.
ROLES = {0: "main application", 1: "OLED co-processor",
         2: "matrix co-processor", 3: "keyboard co-processor",
         4: "factory wavetable bank"}


def looks_headered(data):
    """Does this carry Arturia's 64-byte header? -> bool.

    Bytes 2..0x3F are zero padding in a header and the vector table in a bare
    payload, which cannot be all zero: the first two words are the initial
    stack pointer and the reset vector.
    """
    return len(data) > HEADER + 1 and not any(data[2:HEADER])


def members(path):
    """-> {image_num: (file_name, headered_bytes)} plus the container's info."""
    import microfreak.firmware.mff as mff
    items, _comment = mff.read(path)
    by_name = {i.filename: data for i, data in items}
    raw_info = by_name.get("info.json")
    if raw_info is None:
        raise SystemExit("%s is not a firmware container (no info.json)."
                         % path)
    info = json.loads(raw_info)
    out = {}
    for img in info.get("images", []):
        name = img["file_name"]
        if name in by_name:
            out[int(img["image_num"])] = (name, by_name[name])
    return out, info


def describe(path):
    """Print what is in a container, or what a bare image is. -> 0."""
    if not zipfile.is_zipfile(path):
        data = open(path, "rb").read()
        kind = ("one image with Arturia's header" if looks_headered(data)
                else "a bare payload, no header")
        print("%s\n  %s, %d bytes" % (path, kind, len(data)))
        if looks_headered(data):
            print("  image_num %d, checksum 0x%02X" % (data[0], data[1]))
        return 0
    imgs, info = members(path)
    print("%s\n  version %s, dated %s, %d image(s)"
          % (path, info.get("version_number", "?"), info.get("date", "?"),
             len(imgs)))
    for num in sorted(imgs):
        name, data = imgs[num]
        print("  image %d  %-24s %8d bytes  %s"
              % (num, ROLES.get(num, "?"), len(data), name))
    return 0


def payload_of(data, label):
    """-> the bare payload, header and checksum verified if there is one."""
    if not looks_headered(data):
        return bytes(data)
    from microfreak.firmware.image import looks_packaged
    if looks_packaged(data) is None:
        sys.exit("%s: the checksum does not match" % label)
    return bytes(data[HEADER:-1])


def for_download(path, image=MAIN_IMAGE):
    """-> the bytes a DFU download should SEND, header included.

    The opposite of `resolve`, and the difference matters: the bootloader
    expects the container's inner image exactly as shipped, because the first
    64-byte DNLOAD block IS Arturia's header -- that is why a 473,695-byte
    payload is downloaded as 473,760 bytes. Stripping the header here would
    send image data where the bootloader looks for the header and one block
    short at the end.
    """
    if zipfile.is_zipfile(path):
        imgs, _info = members(path)
        if image not in imgs:
            raise SystemExit(
                "%s has no image %d. It holds %s."
                % (path, image, ", ".join("%d (%s)" % (n, ROLES.get(n, "?"))
                                          for n in sorted(imgs))))
        return imgs[image][1]
    data = open(path, "rb").read()
    if not looks_headered(data):
        raise SystemExit(
            "%s is a bare payload with no Arturia header. Pass the .mff, or "
            "the .bin from inside it." % path)
    return data


WAVETABLE_IMAGE = 4


def find_wavetable_bank(dirpath):
    """-> the factory wavetable bank's bytes from `dirpath`, or None.

    The bank is image 4 of Arturia's bundle --
    `userwavetable_factory_v1_3__20211015.bin`, 327,744 bytes -- and it is
    flash CONTENT, not code: header `magic 0x7981`, then its own destination
    address 0x00F5F000, then the payload. mkflash drops it straight in.

    Two shapes, because `data/firmware/` is a drop folder. mkflash used to
    scan only for a loose `userwavetable*.bin`, which exists exactly when
    someone has unpacked the bundle by hand -- so after the drop folder
    landed, a folder holding just the .mff as downloaded built a flash image
    reporting

        (erased image -- no wavetable bank found)

    and the Wavetable engine had nothing to play, silently. The bank was in
    the .mff the whole time. A container is read through `members()` now,
    which is the same path `resolve()` takes for image 0.
    """
    loose, container = None, None
    if not os.path.isdir(dirpath):
        return None
    for name in sorted(os.listdir(dirpath)):
        path = os.path.join(dirpath, name)
        if not os.path.isfile(path):
            continue
        if name.startswith("userwavetable") and name.endswith(".bin"):
            loose = path
        elif container is None and zipfile.is_zipfile(path):
            container = path
    if loose:
        return open(loose, "rb").read()
    if container:
        imgs, _info = members(container)
        if WAVETABLE_IMAGE in imgs:
            return imgs[WAVETABLE_IMAGE][1]
    return None


def resolve(path, image=MAIN_IMAGE, quiet=False):
    """-> a path to the bare payload, extracting and caching if needed.

    A `.raw` is returned untouched. A container or a headered `.bin` is
    unpacked into TMPDIR under a name derived from its content hash, so the
    same input always gives the same file and a different input never reuses
    it.
    """
    if path is None:
        # paths.find_main_image found nothing, so the drop folder is empty.
        # There is no file to name in the message -- the folder is the answer.
        import mfemu.paths as _paths
        raise SystemExit(
            "no firmware. Put Arturia's 5.0.0.2084 .mff, as downloaded, in\n"
            "    %s\n"
            "or pass --firmware FILE."
            % _paths.FIRMWARE_DIR)
    if not os.path.exists(path):
        raise SystemExit(
            "no such firmware image: %s" % path)

    if zipfile.is_zipfile(path):
        imgs, info = members(path)
        if image not in imgs:
            raise SystemExit(
                "%s has no image %d. It holds %s. Pick one with --image N."
                % (path, image,
                   ", ".join("%d (%s)" % (n, ROLES.get(n, "?"))
                             for n in sorted(imgs))))
        name, data = imgs[image]
        label = "%s!%s" % (os.path.basename(path), name)
        version = info.get("version_number", "?")
    else:
        data = open(path, "rb").read()
        if not looks_headered(data):
            if not quiet:
                print("firmware: %s (a bare payload, %d bytes)"
                      % (os.path.basename(path), len(data)))
            return path
        name, label, version = os.path.basename(path), os.path.basename(path), "?"

    body = payload_of(data, label)
    digest = hashlib.sha256(body).hexdigest()
    os.makedirs(CACHE, exist_ok=True)
    out = os.path.join(CACHE, "%s-%s.raw" % (
        os.path.splitext(os.path.basename(path))[0].replace(os.sep, "_")[:40],
        digest[:12]))
    if not os.path.exists(out):
        # .part + os.replace, the same pattern warm.build() uses. The name
        # carries the content hash, so two concurrent extractions write
        # identical bytes -- the race is not corruption but a READER: once the
        # file exists, the next process's existence check passes while the
        # first is still writing, and it loads a truncated firmware. replace()
        # is atomic on the same filesystem, so the path never exists partly.
        part = "%s.part-%d" % (out, os.getpid())
        with open(part, "wb") as fh:
            fh.write(body)
        os.replace(part, out)
    if not quiet:
        print("firmware: %s" % label)
        print("          image %d (%s), version %s, %d bytes of payload"
              % (image, ROLES.get(image, "?"), version, len(body)))
        print("          sha256 %s%s" % (digest[:32] + "...", _supported(digest)))
    return out


def _supported(digest):
    """-> a note on whether this is the build the project's addresses target."""
    try:
        from microfreak.firmware.patching import KNOWN_PAYLOAD_SHA256
    except Exception:
        return ""
    if digest == KNOWN_PAYLOAD_SHA256:
        return "  (the supported 5.0.0.2084 build)"
    return "  (NOT the supported build -- see `mfemu check`)"


def main():
    """`mfemu image` -- say what is in a firmware file."""
    import argparse
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="    mfemu image update.mff             list its images\n"
               "    mfemu image update.mff --extract   write image 0's payload",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", help="a .mff container, a headered .bin, or a .raw")
    ap.add_argument("--image", type=int, default=MAIN_IMAGE, metavar="N",
                    help="which image to extract (default: 0, the application)")
    ap.add_argument("--extract", action="store_true",
                    help="extract the payload and print where it landed")
    args = ap.parse_args()
    if not args.extract:
        return describe(args.file)
    print(resolve(args.file, image=args.image))
    return 0


if __name__ == "__main__":
    sys.exit(main())
