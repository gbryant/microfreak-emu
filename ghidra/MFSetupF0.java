// Lay out an STM32F0 co-processor image: RAM and peripheral blocks, plus the
// 45 vector-table entry points.  The F0 map differs from the F4's in more than
// bases -- GPIO lives at 0x48000000 and RCC at 0x40021000.
//
// Run as a headless preScript on a raw binary imported at 0x08000000.
//
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.mem.Memory;

public class MFSetupF0 extends GhidraScript {

    private static final Object[][] BLOCKS = {
        {"SRAM",   0x20000000L, 0x00008000L},
        {"SYSMEM", 0x1FFF0000L, 0x00010000L},
        {"APB",    0x40000000L, 0x00030000L},
        {"AHB2",   0x48000000L, 0x00002000L},
        {"PPB",    0xE0000000L, 0x00100000L},
    };

    private static final long BASE = 0x08000000L;
    private static final int VECTORS = 45;      // 16 system + 29 IRQs

    @Override
    public void run() throws Exception {
        Memory mem = currentProgram.getMemory();
        for (Object[] b : BLOCKS) {
            Address at = toAddr((Long) b[1]);
            if (mem.getBlock(at) != null) continue;
            mem.createUninitializedBlock((String) b[0], at, (Long) b[2], false);
            println("created " + b[0] + " at " + at);
        }
        int marked = 0;
        for (int i = 1; i < VECTORS; i++) {
            long v = getInt(toAddr(BASE + i * 4)) & 0xFFFFFFFFL;
            if ((v & 1) == 0 || v < BASE || v > BASE + 0x80000L) continue;
            try {
                Address h = toAddr(v & ~1L);
                disassemble(h);
                if (getFunctionAt(h) == null) createFunction(h, null);
                marked++;
            } catch (Exception e) { }
        }
        println("marked " + marked + " vector handlers");
    }
}
