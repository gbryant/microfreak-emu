#!/usr/bin/env python3
"""Does the firmware decode OUR bitstream to the same frames as Arturia's?

The test SPEECH.md specifies: encode, boot, read `frames_` back out of
the running firmware, compare -- with our decoder nowhere in the comparison.

How the expectation is obtained, which is the whole point: run 0 boots the
FACTORY image and reads what the firmware decodes bank_4 into.  Runs A and B
boot images whose bank_4 has been replaced by our own re-encode of those same
frames.  Our bytes are not Arturia's (9 bytes shorter, different `repeat`
choices), so if the firmware lands on byte-identical frames and boundaries,
our encoder agrees with the firmware.  The reference comes from the firmware
itself, so self-consistency through our decoder cannot produce a false pass.

    run 0  factory bank_4         must match bank_4's truth  (readback works)
    run A  our re-encode          must match bank_4's truth  (the result sought)
    run B  bank_3's bitstream     must match BANK_3's truth  (the control)

Run B is the negative control, and it is checked in both directions: it has to
stop matching bank_4 AND start matching bank_3.  Merely differing would also be
satisfied by a blind readback returning noise.

It used to be our re-encode with 0-padding, on the belief that padding was the
old failure's cause.  Padding is cosmetic -- the firmware flushes to the byte
boundary at end-of-word and never reads those bits -- and 75937e6 withdrew that
claim in the same commit that made 0-padding a must-fail run here, so this test
could not return PASS from the day it was written.
"""
import os, sys, time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import mfemu.paths as PATHS
import mfemu.image as _image


def _bare_firmware():
    """-> a path to the BARE payload, whatever shape data/firmware/ holds.

    This file reads the firmware as raw bytes and patches it, so it needs the
    payload without Arturia's 64-byte header. `PATHS.MAIN_RAW` is whatever
    shape was dropped in -- a .mff, a headered .bin or a bare .raw -- and
    paths.py prefers the bare one when several carry the same image, but a
    folder holding only the .mff as downloaded is both legal and the most
    likely case for a new user. resolve() normalises all three.
    """
    if PATHS.MAIN_RAW is None:
        return None
    return _image.resolve(PATHS.MAIN_RAW, quiet=True)


# Resolved once. None when the drop folder is empty, which main() checks
# for and skips on before anything opens it.
FIRMWARE_BARE = _bare_firmware()
import microfreak.preset.fields as PF
import microfreak.preset.tlv as preset_tlv
import mfemu.presetlib as presetlib
import mfemu.mfrender as mfrender
import mfemu.vehicle as vehicle
import microfreak.lpc as S
from microfreak.firmware import patching as F
import mfreak_speech.sysex as X
from mfemu.midilink import EmulatedSynth

BANK = 4
SLOT = 0
NOTE = 60
FRAMES_TO_CHECK = 120            # frames read back per run (14 bytes each)
WORK = os.path.join("/tmp", "enc-roundtrip")

# The vehicle is built from tests/fixtures/init-patch.mfpz, the instrument's own
# blank patch with the engine switched. See mfemu/vehicle.py for why it is a
# template rather than something synthesised: 64 of a blank patch's 110
# parameters are non-zero, and zeroing them loads fine and renders silence.


def encode_with_padding(bit):
    """Re-encode bank_4's own frames with the tail padded by `bit`."""
    def flush(self):
        while len(self.bits) % 8:
            self.bits.append(bit)
    saved = S._Writer.flush_to_byte
    S._Writer.flush_to_byte = flush
    try:
        img = bytearray(open(FIRMWARE_BARE, "rb").read())
        ptr, size = F.word_banks(img)[BANK]
        orig = bank_bytes(img, ptr, size)
        frames, bounds = S.decode_bank(orig)
        words = S.split_words(frames, bounds)
        return orig, S.encode_bank(words), words
    finally:
        S._Writer.flush_to_byte = saved


CONTROL_BANK = 3


def foreign_bank(src=CONTROL_BANK):
    """Another bank's bitstream, to be written where bank_4 lives.

    The negative control.  It puts bytes under bank_4's descriptor that the
    firmware must decode differently, and the two banks disagree on both
    numbers the readback already reports:

        bank_3  2524 bytes  26 words  604 frames
        bank_4  4802 bytes  22 words  926 frames

    Nothing of ours is in the measurement -- these are Arturia's own bytes at
    another address -- so it separates "does the readback follow the bytes we
    wrote" from "is our encoder right".  That separation is what the old
    0-padding run was reaching for and could not provide.
    """
    img = bytearray(open(FIRMWARE_BARE, "rb").read())
    ptr, size = F.word_banks(img)[src]
    blob = bank_bytes(img, ptr, size)
    frames, bounds = S.decode_bank(blob)
    return blob, S.split_words(frames, bounds)


def bank_bytes(img, ptr, size):
    return bytes(img[ptr - F.BASE:ptr - F.BASE + size])


def truth(words):
    """(num_words, cumulative frame boundaries) -- derived from the input."""
    bounds = [0]
    for w in words:
        bounds.append(bounds[-1] + len(w))
    return len(words), bounds


def matches(r, words):
    """Does a readback agree with the truth derived from `words`?"""
    nwords, bounds = truth(words)
    return (r[1] == nwords and r[0] == bounds[-1]
            and r[2][:len(bounds)] == bounds)


def build_image(bitstream, tag):
    """bank_4's bitstream swapped IN PLACE, descriptor size updated.

    In place, so the other four banks stay Arturia's bytes at Arturia's
    addresses.  This test is about the bitstream only, and our stream is 9
    bytes shorter than Arturia's, so it drops straight in.  Bytes past the declared
    size are never read.
    """
    img = bytearray(open(FIRMWARE_BARE, "rb").read())
    descs = F.word_banks(img)
    ptr, size = descs[BANK]
    assert len(bitstream) <= size, (len(bitstream), size)
    off = ptr - 0x0800C000                      # load base -> image offset
    img[off:off + len(bitstream)] = bitstream
    descs[BANK] = (ptr, len(bitstream))
    F.set_word_banks(img, descs)
    # prove the swap: read it straight back out through the descriptor
    back = bank_bytes(img, *F.word_banks(img)[BANK])
    assert back == bitstream, "descriptor/bytes disagree after the swap"
    path = os.path.join(WORK, "main-%s.raw" % tag)
    open(path, "wb").write(bytes(img))
    return path


# ---------------------------------------------------------------- the readback
def addrs():
    bank0 = X.BANK_BASE
    return {"count": bank0 + 0x0C, "num_words": bank0 + 0x10,
            "bounds": bank0 + 0x14, "frames": X.FRAMES_BASE}


def rd(synth, a):
    v = synth.monitor_read("sysbus ReadDoubleWord 0x%08X" % a)
    return None if v is None else (v - (1 << 32) if v >= (1 << 31) else v)


def read_state(synth, nframes):
    A = addrs()
    count = rd(synth, A["count"])
    nwords = rd(synth, A["num_words"])
    bounds = [rd(synth, A["bounds"] + 4 * i) for i in range(32)]
    nbytes = nframes * S.FRAME_BYTES
    blob = bytearray()
    for off in range(0, nbytes, 4):
        # NOT `(v or 0)`. This blob is compared byte for byte against what the
        # encoder produced, so substituting zero for an unread word either
        # invents a mismatch or -- where the expected bytes are zero, which in
        # a frame buffer is common -- invents a MATCH. `rd` above already
        # returns None for exactly this reason; this loop went around it.
        # Retry once, then refuse to compare rather than fabricate.
        a = A["frames"] + off
        v = synth.monitor_read("sysbus ReadDoubleWord 0x%08X" % a)
        if v is None:
            v = synth.monitor_read("sysbus ReadDoubleWord 0x%08X" % a)
        if v is None:
            raise RuntimeError(
                "the Monitor returned no value for 0x%08X after a retry, so "
                "the frame buffer cannot be compared. This is the stale-prompt "
                "race, not a firmware result -- rerun." % a)
        blob += v.to_bytes(4, "little")
    return count, nwords, bounds, bytes(blob[:nbytes])


def run(firmware, flash, label, port, nframes):
    print("\n=== %s ===" % label)
    print("    firmware: %s" % os.path.basename(firmware))
    with EmulatedSynth(firmware=firmware, flash=flash,
                       renode_dir=PATHS.RENODE, port=port) as synth:
        synth.boot()
        synth.send([0xB0, 0x00, 0x00])          # Bank Select MSB
        synth.send([0xC0, SLOT])                # Program Change -> our preset
        time.sleep(6)
        synth.send([0x90, NOTE, 100])           # a note forces the lazy Load
        time.sleep(10)
        synth.send([0x80, NOTE, 0])
        time.sleep(2)
        count, nwords, bounds, blob = read_state(synth, nframes)
    print("    frame count %s   num_words %s" % (count, nwords))
    print("    boundaries[0:8] %s" % bounds[:8])
    print("    frames_[0:14]   %s" % blob[:14].hex(" "))
    return count, nwords, bounds, blob


def verdict(label, ok):
    print("%-47s : %s" % (label, ok))


def main():
    os.makedirs(WORK, exist_ok=True)
    if PATHS.MAIN_RAW is None:
        print("SKIP - no firmware. Drop Arturia's update in %s"
              % PATHS.FIRMWARE_DIR)
        return 0
    # Skip rather than fail if the one committed fixture has been removed: it
    # is factory data, and someone may reasonably delete it. Everything else
    # in this repository is text.
    if not os.path.exists(vehicle.INIT_PATCH):
        print("SKIP - %s is not present." % vehicle.INIT_PATCH)
        print("       It is the instrument's Init patch, and this test builds its")
        print("       vehicle from it. Regenerate it on any MicroFreak with")
        print("       `mfreak preset read <slot> --out %s`" % vehicle.INIT_PATCH)
        print("       after pressing Init and Save to that slot -- see NOTICE.")
        return 0

    # The vehicle is built, not supplied: the instrument's own Init patch with
    # the engine switched to Speech and the Wave knob at 100, which is bank 4.
    # Five bytes differ from the template -- the same five a player changes by
    # turning the Type knob on a fresh patch.
    page = vehicle.page(SLOT, engine="Speech", wave=100.0, name="SpeechTest")
    print("vehicle: the Init patch, engine -> Speech, Wave 100 (bank 4)")
    # A whole 4096-byte page, so mkflash takes it region 0 and all and the
    # image needs no donor: no capture, no bank, nothing but the firmware.
    flash = mfrender.build_image({SLOT: page}, None, WORK)
    print("flash image: %s\n" % flash)

    orig, fixed, words = encode_with_padding(1)
    ctl_blob, ctl_words = foreign_bank()
    exp_words, exp_bounds = truth(words)
    ctl_nwords, ctl_bounds = truth(ctl_words)
    print("bank_%d: factory %d bytes, ours %d   control bank_%d: %d bytes"
          % (BANK, len(orig), len(fixed), CONTROL_BANK, len(ctl_blob)))
    print("bank_%d truth:  %d words, %d frames, bounds[:6]=%s"
          % (BANK, exp_words, exp_bounds[-1], exp_bounds[:6]))
    print("bank_%d truth:  %d words, %d frames, bounds[:6]=%s   <- what RUN B must become"
          % (CONTROL_BANK, ctl_nwords, ctl_bounds[-1], ctl_bounds[:6]))
    print("our stream differs from Arturia's: %s" % (fixed != orig))

    jobs = [(FIRMWARE_BARE, "RUN 0  factory bank_4 (reference + harness proof)", 3800),
            (build_image(fixed, "fixed"), "RUN A  our re-encode of bank_4", 3810),
            (build_image(ctl_blob, "ctl"),
             "RUN B  bank_%d's bitstream under bank_%d's descriptor (control)"
             % (CONTROL_BANK, BANK), 3820)]
    res = [run(fw, flash, lbl, port, FRAMES_TO_CHECK) for fw, lbl, port in jobs]
    r0, rA, rB = res

    print("\n" + "=" * 68)
    # harness proof: the factory run must agree with the input-derived truth
    h_ok = matches(r0, words)
    verdict("HARNESS  factory run matches bank_%d's truth" % BANK, h_ok)
    if not h_ok:
        print("  expected num_words %d count %d bounds %s"
              % (exp_words, exp_bounds[-1], exp_bounds[:6]))
        print("  VOID - the readback or the preset is wrong, nothing below means anything")
        return 2

    # the control: it must stop looking like bank_4 AND start looking like bank_3.
    # "differs" alone would also be satisfied by a blind readback returning noise.
    ctl_moved = rB[:3] != r0[:3] or rB[3] != r0[3]
    ctl_is_b3 = matches(rB, ctl_words)
    verdict("CONTROL  bank_%d run stopped matching bank_%d"
            % (CONTROL_BANK, BANK), ctl_moved)
    verdict("CONTROL  bank_%d run matches bank_%d's own truth"
            % (CONTROL_BANK, CONTROL_BANK), ctl_is_b3)
    if not (ctl_moved and ctl_is_b3):
        print("  got num_words %s count %s bounds %s" % (rB[1], rB[0], rB[2][:6]))
        print("  VOID - the control did not land. Either the readback does not")
        print("         follow the bytes written, or the bank swap never took,")
        print("         so RUN A proves nothing either.")
        return 2

    a_ok = rA[:3] == r0[:3] and rA[3] == r0[3]
    verdict("RUN A    our re-encode decodes identically", a_ok)
    if not a_ok:
        print("  run A differs: count %s vs %s | num_words %s vs %s | bounds %s | frames %s"
              % (rA[0], r0[0], rA[1], r0[1],
                 "same" if rA[2] == r0[2] else "DIFFER",
                 "same" if rA[3] == r0[3] else "DIFFER"))
    print()
    if a_ok:
        print("PASS - the encoder agrees with the firmware, and the control proves")
        print("       the readback tracks the bytes written rather than ignoring them.")
        return 0
    print("FAIL - the control is sound, so this is a real encoder disagreement.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
