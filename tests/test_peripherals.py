"""Do the peripheral models Renode does not ship actually work?

Five of the six are exercised here: the codec, QUADSPI, SAI2, DMA2 and
OTG_FS. The sixth, `renode/STM32F4_Flash.cs`, cannot be shown to work by
booting -- a boot never programs flash -- so it has tests of its own:
`test_dfu_handoff.py` and `test_dfu_download.py`.

`renode/` carries this project's own models for QUADSPI, SAI2, OTG_FS, DMA2 and
the I2C codec, because Renode's STM32F4 platform has none of them. They are the
generically useful part of this repository, and they are also the part most
likely to be broken by a Renode upgrade or a bad merge -- in a way that looks
like a firmware mystery rather than a harness fault.

One boot of unmodified 5.0.0.2084 on a bundle-only flash image, then one
assertion per model, each with something that makes a null result legible:

  CODEC    free, and first because everything else depends on it. The firmware
           writes 96 registers to an I2C slave at 0x18 during boot and waits
           forever for an ack. Reaching main() at all means the codec model
           answered -- so this is asserted by the boot test and restated here.

  QUADSPI  the model counts reads per 4096-byte page. The firmware must read
           SOME pages (it scans the sample region at boot and reads the
           wavetable bank), and must NOT read all 4096 of them -- "non-empty"
           alone would pass against a model that logged every page whether or
           not anyone asked.

  SAI2     the capture buffer must go from empty to non-empty, and empty has
           to be PROVEN rather than assumed: the machine is paused, the buffer
           cleared, and the count read back as zero before it is started again.
           Clearing it on a free-running machine does not work -- 0.8 s of
           audio accumulates inside a single Monitor round trip.

  DMA2     not directly observable, and not claimed to be. SAI2's buffer fills
           from DMA2 stream 4 and QUADSPI's reads are requested on stream 7, so
           those two assertions are what stands behind it. Stated as an
           inference, which is what it is.

  OTG_FS   `usbfs Attach` must walk the host state machine to Configured. This
           is the model that stood in as a plain memory region for a while, so
           a regression here is a real possibility rather than a hypothetical.

    uv run python tests/test_peripherals.py
"""
import os
import sys
import time

import mfemu.mfrender as mfrender
import mfemu.paths as PATHS
from mfemu.midilink import EmulatedSynth

UPTIME = 0x2001E1D8
PAGE = 4096
TOTAL_PAGES = (16 << 20) // PAGE
BOOT_DEADLINE = 420.0
SAI_WINDOW = 20.0              # wall clock; the machine is slower than real time
MIN_SAMPLES = 1000             # anything above noise: the claim is "it fills"
WORK = os.path.join(os.environ.get("TMPDIR", "/tmp"), "mfemu-periph")
os.makedirs(WORK, exist_ok=True)

fails, voids = [], []


def chk(ok, label, detail=""):
    print("  %-4s %-40s %s" % ("ok" if ok else "FAIL", label, detail))
    if not ok:
        fails.append(label)
    return ok


def void(label, detail):
    print("  %-4s %-40s %s" % ("VOID", label, detail))
    voids.append(label)


def _answer(text):
    """-> the value a Monitor command printed, not its echo or the prompt.

    The transcript is `<command>` / `<value>` / `(machine)`, and which line
    holds the value depends on what else was draining at the time, so take the
    last line that is neither the prompt nor a repeat of a command.
    """
    for line in reversed([l.strip() for l in (text or "").splitlines()]):
        if line and not line.startswith("(") and not line.startswith("usbfs"):
            return line
    return None


def read_trace(path):
    """-> [(page_addr, count)] from `quadspi SaveReadTrace`."""
    out = []
    if not os.path.exists(path):
        return out
    for line in open(path):
        parts = line.split()
        if len(parts) == 2:
            try:
                out.append((int(parts[0], 16), int(parts[1])))
            except ValueError:
                pass
    return out


def main():
    PATHS.check(PATHS.MAIN_RAW)
    flash = mfrender.build_image({}, None, WORK, quiet=True)
    trace_path = os.path.join(WORK, "qspi-reads.txt")
    pcm_path = os.path.join(WORK, "sai-capture.pcm")
    for stale in (trace_path, pcm_path):
        if os.path.exists(stale):
            os.unlink(stale)

    print("stock 5.0.0.2084 on a bundle-only image (every preset slot erased)\n")
    with EmulatedSynth(firmware=PATHS.MAIN_RAW, flash=flash,
                       renode_dir=PATHS.RENODE, port=3960) as synth:
        # Before the firmware has set anything up. Enabling the trace here
        # means the very first accesses of the boot may be missed, which is
        # stated rather than hidden: the assertions below are about pages read
        # later, during the sample scan.
        synth.monitor("quadspi TraceReads true")
        synth.monitor("quadspi ClearReadTrace")

        reached = False
        end = time.time() + BOOT_DEADLINE
        while time.time() < end:
            if "MILESTONE main()" in open(synth.log_path, errors="replace").read():
                reached = True
                break
            time.sleep(2.0)
        print("1. the I2C codec (implicit: boot blocks on its ack forever)")
        chk(reached, "firmware reached main()",
            "" if reached else "never got there in %.0f s" % BOOT_DEADLINE)
        if not reached:
            print("\nNOT RUN: nothing below means anything without a booted machine.")
            return 1
        # Let the firmware settle into its main loop and run audio.
        synth.boot()
        time.sleep(10.0)

        print("\n2. QUADSPI")
        synth.monitor("quadspi SaveReadTrace @%s" % trace_path)
        time.sleep(1.0)
        trace = read_trace(trace_path)
        pages = len(trace)
        reads = sum(c for _, c in trace)
        chk(pages > 0, "the firmware read flash",
            "%d pages, %d reads" % (pages, reads))
        chk(0 < pages < TOTAL_PAGES, "and not every page (so this is selective)",
            "%d of %d pages" % (pages, TOTAL_PAGES))
        for addr, count in sorted(trace, key=lambda t: -t[1])[:5]:
            print("       0x%06X  %d reads" % (addr, count))

        print("\n3. SAI2")
        # The baseline has to be taken with the machine STOPPED. Clearing the
        # buffer while it free-runs and reading back immediately gave 77,504
        # samples -- 0.8 s of audio accumulated inside one Monitor round trip --
        # so "it started empty" was unprovable that way and the test correctly
        # refused to pass. Paused, the count is zero because nothing can run.
        synth.monitor("pause")
        synth.monitor("sai2 ClearCapture")
        zeroed = synth.monitor_read("sai2 CapturedSamples")
        synth.monitor("start")
        if zeroed is None:
            void("capture counter readable", "CapturedSamples did not answer")
        elif zeroed != 0:
            void("buffer empties when cleared",
                 "%d samples after ClearCapture on a paused machine" % zeroed)
        else:
            time.sleep(SAI_WINDOW)
            after = synth.monitor_read("sai2 CapturedSamples")
            chk(after is not None and after > MIN_SAMPLES,
                "fills from a verified-empty buffer",
                "0 -> %s samples in %.0f s of wall clock"
                % (f"{after:,}" if after is not None else "?", SAI_WINDOW))
            synth.monitor("sai2 SavePCM @%s" % pcm_path)
            time.sleep(2.0)
            size = os.path.getsize(pcm_path) if os.path.exists(pcm_path) else 0
            chk(size > 0, "and the PCM saves to disk", "%d bytes" % size)

        print("\n4. DMA2 (inferred, not measured directly)")
        print("       SAI2's buffer fills from stream 4 and QUADSPI's reads are")
        print("       requested on stream 7; both moved above, so DMA2 is moving.")

        print("\n5. OTG_FS")
        # `usbfs HostState` is a string and `usbfs Configured` a bool. The
        # Monitor echoes the command and then its prompt, so the answer is
        # neither the first line nor the last -- reading the last line gave
        # "(microfreak)", the prompt, and failed a working enumeration.
        synth.attach_usb()
        state = _answer(synth.monitor("usbfs HostState"))
        configured = _answer(synth.monitor("usbfs Configured"))
        chk(configured == "True", "enumeration reached Configured",
            "HostState=%s Configured=%s" % (state or "?", configured or "?"))

    print("\n" + "=" * 68)
    if fails:
        print("FAIL: %s" % ", ".join(fails))
        return 1
    if voids:
        print("INCONCLUSIVE: a control was void -- %s" % ", ".join(voids))
        print("The checks that did run passed, but do not count this as a pass.")
        return 3
    print("PASS - these five peripheral models carry the stock firmware's boot.")
    print("       The sixth, STM32F4_Flash.cs, is covered by the DFU tests.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
