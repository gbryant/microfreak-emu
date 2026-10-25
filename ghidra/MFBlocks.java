//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.mem.MemoryBlock;

public class MFBlocks extends GhidraScript {
    @Override
    public void run() throws Exception {
        println("image base: " + currentProgram.getImageBase());
        for (MemoryBlock b : currentProgram.getMemory().getBlocks()) {
            println(String.format("  %-16s %s .. %s  size=0x%X init=%s",
                b.getName(), b.getStart(), b.getEnd(), b.getSize(), b.isInitialized()));
        }
    }
}
