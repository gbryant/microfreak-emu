// Print Ghidra's instructions across an address RANGE, not just a fixed count.
//
// MFDisasm.java prints eight instructions from a point, which is the wrong
// shape for "read this window and see what the code does with the literal
// pool next to it".  Args: START END (hex, inclusive start, exclusive end).
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Instruction;

public class MFRange extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        long lo = Long.parseLong(args[0].replace("0x", ""), 16);
        long hi = Long.parseLong(args[1].replace("0x", ""), 16);
        Address at = toAddr(lo);
        Instruction ins = getInstructionAt(at);
        if (ins == null) ins = getInstructionAfter(at);
        while (ins != null && ins.getAddress().getOffset() < hi) {
            StringBuilder b = new StringBuilder();
            for (byte x : ins.getBytes()) b.append(String.format("%02x", x));
            println(String.format("  %s  %-12s %s",
                    ins.getAddress(), b.toString(), ins.toString()));
            ins = ins.getNext();
        }
    }
}
