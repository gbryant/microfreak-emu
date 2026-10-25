#!/usr/bin/env python3
"""Boot the emulator and keep it running so mfreak can talk to it.

    mfemu serve                          stock firmware, empty flash
    mfemu serve --source mybank.mfprojz  with presets loaded
    mfemu serve --preset 7=speech        a test preset at 7
    mfemu serve --bootloader             boot through the real bootloader

Then, in another shell:
    MFREAK_EMULATOR=1 mfreak preset read 7

Startup takes about 7 seconds. A served machine answers SysEx and MIDI but
does not produce audio; use `mfemu render` for that. Ctrl-C stops it.
"""
import argparse
import json
import os
import signal
import sys
import time

import mfemu.image as image
import mfemu.mfrender as mfrender
import mfemu.paths as PATHS
from mfemu.midilink import EmulatedSynth
import mfemu.panel as panel


def engine_named(name, spec):
    """-> the engine's name as the preset fields spell it, matched without case."""
    from microfreak.preset.fields import ENGINE_VALUES
    for known in ENGINE_VALUES:
        if known.lower() == name.lower():
            return known
    sys.exit("--preset %s: no engine called %r. Engines: %s"
             % (spec, name, ", ".join(ENGINE_VALUES)))


def main():
    ap = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog="\n".join(__doc__.splitlines()[1:]), formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--firmware", metavar="FILE", default=None,
                    help="a .mff package, a headered .bin, or a bare payload")
    ap.add_argument("--image", type=int, default=0, metavar="N",
                    help="which image inside a .mff (default: 0)")
    ap.add_argument("--bootloader", metavar="BOOT.bin", nargs="?",
                    const=PATHS.BOOTLOADER, default=None,
                    help="boot through the bootloader (default file: "
                         "data/bootloader/)")
    ap.add_argument("--source", metavar="FILE", default=None,
                    help="an MCC preset export (.mfprojz) to load")
    ap.add_argument("--samples", metavar="FILE.mfsbz",
                    default=PATHS.SAMPLE_FILE,
                    help="an MCC sample export (default: the one in "
                         "data/samples/)")
    ap.add_argument("--wavetables", metavar="FILE.mfwbz",
                    default=PATHS.WAVETABLE_FILE,
                    help="an MCC wavetable export (default: the one in "
                         "data/wavetables/, else the firmware's own)")
    ap.add_argument("--preset", action="append", default=[], metavar="N=ENGINE",
                    help="put an Init-based test preset at N on ENGINE, "
                         "e.g. 8=Speech. Repeatable")
    ap.add_argument("--port", type=int, default=3456,
                    help="MIDI socket port (default: 3456; the Monitor "
                         "uses the next one)")
    ap.add_argument("--session", default=EmulatedSynth.SESSION,
                    help="where to write the session file")
    ap.add_argument("--no-usb", action="store_true",
                    help="skip USB setup. Testing only: SysEx replies "
                         "will not arrive")
    ap.add_argument("--deadline", type=float, default=420.0, metavar="SECONDS",
                    help="give up if boot takes longer (default: 420)")
    args = ap.parse_args()

    firmware = image.resolve(args.firmware or PATHS.MAIN_RAW, image=args.image)
    PATHS.check(firmware)
    work = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-serve")
    os.makedirs(work, exist_ok=True)

    assignments = {}
    if args.preset:
        import mfemu.vehicle as vehicle
        for spec in args.preset:
            num, _, engine = spec.partition("=")
            if not engine:
                sys.exit("--preset wants N=ENGINE, got %r" % spec)
            try:
                n = panel.to_slot(num)      # panel number in, index out
            except ValueError as exc:
                sys.exit("--preset %s: %s" % (spec, exc))
            engine = engine_named(engine, spec)
            wave = 100.0 if engine == "Speech" else None
            assignments[n] = vehicle.page(n, engine=engine, wave=wave,
                                          name=engine[:14])
            print("preset %d: a built %s vehicle" % (n + 1, engine))
    flash = mfrender.build_image(assignments, args.source, work, quiet=True,
                                 samples=args.samples,
                                 wavetables=args.wavetables)
    for kind, chosen in (("samples", args.samples),
                         ("wavetables", args.wavetables)):
        warning = PATHS.drop_folder_warning(kind, chosen)
        if warning:
            print(warning, file=sys.stderr)
        elif chosen:
            print("%s: %s" % (kind, os.path.basename(chosen)))

    print("booting %s%s" % (os.path.basename(firmware),
                            " through the bootloader" if args.bootloader else ""))
    synth = EmulatedSynth(firmware=firmware, flash=flash,
                          bootloader=args.bootloader,
                          renode_dir=PATHS.RENODE, port=args.port)
    synth.start()
    try:
        end = time.time() + args.deadline
        while time.time() < end:
            if "MILESTONE main()" in open(synth.log_path, errors="replace").read():
                break
            time.sleep(2.0)
        else:
            print("it never reached main(); see %s" % synth.log_path, file=sys.stderr)
            return 1
        synth.boot()                      # open the MIDI receive gate
        if not args.no_usb:
            synth.attach_usb()
            synth.send_over_usb = True
        path = synth.write_session(args.session)
        # Let go of the channels: Renode's socket terminal and Monitor each
        # take one client, so holding them would mean nobody else can talk to
        # the machine this command exists to share.
        synth.release()

        print("\nemulator is up.")
        print("  MIDI socket   127.0.0.1:%d" % synth.port)
        print("  Monitor       127.0.0.1:%d" % synth.monitor_port)
        print("  USB capture   %s" % synth.usb_path)
        print("  session       %s" % path)
        print("  Renode log    %s" % synth.log_path)
        print("\nPoint the tools at it:")
        # The preset actually placed, not a hardcoded 7. This printed the same
        # number whatever --preset said, so the suggested command read a
        # different preset from the one just built.
        example = (min(assignments) + 1) if assignments else 1
        print("    MFREAK_EMULATOR=1 mfreak preset read %d" % example)
        print("\nOr from Python:")
        print("    from mfemu.midilink import EmulatedSynth")
        print("    synth = EmulatedSynth.attach()")
        print("\nCtrl-C to stop.")

        stop = {"now": False}
        signal.signal(signal.SIGINT, lambda *a: stop.__setitem__("now", True))
        signal.signal(signal.SIGTERM, lambda *a: stop.__setitem__("now", True))
        while not stop["now"]:
            time.sleep(0.5)
            if synth.proc.poll() is not None:
                print("\nRenode exited on its own; see %s" % synth.log_path,
                      file=sys.stderr)
                return 1
        print("\nstopping")
    finally:
        synth.close()
        try:
            os.unlink(args.session)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
