// Print Ghidra's own instructions at the given addresses, to compare against
// an independent disassembly. A Thumb/ARM mode mismatch shows up immediately.
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Instruction;

public class MFDisasm extends GhidraScript {
    @Override
    public void run() throws Exception {
        for (String arg : getScriptArgs()) {
            Address at = toAddr(Long.parseLong(arg.replace("0x", ""), 16));
            println("======== " + arg + " ========");
            Instruction ins = getInstructionAt(at);
            if (ins == null) { println("  no instruction at " + at); continue; }
            for (int i = 0; i < 8 && ins != null; i++) {
                println(String.format("  %s  %s", ins.getAddress(), ins.toString()));
                ins = ins.getNext();
            }
        }
    }
}
