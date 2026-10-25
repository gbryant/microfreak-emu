// Lay out the MicroFreak main image for analysis: RAM and peripheral blocks so
// globals resolve, plus entry points so the analyser has somewhere to start.
//
// Run as a headless preScript on a raw binary imported at 0x0800C000.
//
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.mem.Memory;

public class MFSetup extends GhidraScript {

    // name, base, size.  Uninitialised: the firmware writes these, we only
    // need addresses to resolve so decompiled globals are not raw constants.
    private static final Object[][] BLOCKS = {
        {"SRAM",    0x20000000L, 0x00020000L},
        {"CCM",     0x10000000L, 0x00010000L},
        {"SYSMEM",  0x1FFF0000L, 0x00010000L},
        {"PERIPH",  0x40000000L, 0x00030000L},
        {"USB",     0x50000000L, 0x00040000L},
        {"QUADSPI", 0xA0000000L, 0x00002000L},
        {"PPB",     0xE0000000L, 0x00100000L},
    };

    @Override
    public void run() throws Exception {
        Memory mem = currentProgram.getMemory();
        for (Object[] b : BLOCKS) {
            String name = (String) b[0];
            Address at = toAddr((Long) b[1]);
            if (mem.getBlock(at) != null) {
                println("block at " + at + " already present, skipping " + name);
                continue;
            }
            mem.createUninitializedBlock(name, at, (Long) b[2], false);
            println("created " + name + " at " + at);
        }

        // The vector table is 114 entries at the load address.  Every odd word
        // is a Thumb handler; marking them gives the analyser real entry
        // points instead of leaving most of the image undiscovered.
        //
        // Hardcoded rather than getImageBase(): headless BinaryLoader reports
        // an image base of 0 here even though the block loads at 0x0800C000,
        // so deriving it that way reads the wrong address.
        Address base = toAddr(0x0800C000L);
        int marked = 0;
        for (int i = 1; i < 114; i++) {
            long v = getInt(base.add(i * 4)) & 0xFFFFFFFFL;
            if ((v & 1) == 0 || v < 0x0800C000L || v > 0x08080000L) continue;
            Address h = toAddr(v & ~1L);
            try {
                disassemble(h);
                if (getFunctionAt(h) == null) createFunction(h, null);
                marked++;
            } catch (Exception e) {
                // a bogus vector entry is not worth aborting the run for
            }
        }
        println("marked " + marked + " vector handlers as entry points");

        // Addresses this investigation cares about, so they are functions even
        // if nothing statically calls them (several are virtual-dispatch only).
        long[] interesting = {
            0x080387A4L, 0x0802F3BCL, 0x08031DF4L, 0x08015110L,
            0x0802F13CL, 0x0801F100L, 0x08031DF8L, 0x08015110L,
            0x0802479CL, 0x08031E8CL, 0x0801F6BCL,
        };
        for (long a : interesting) {
            Address at = toAddr(a);
            try {
                disassemble(at);
                if (getFunctionAt(at) == null) createFunction(at, null);
            } catch (Exception e) {
                println("could not make a function at " + at + ": " + e);
            }
        }
        println("setup done");
    }
}
