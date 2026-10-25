// Find instructions that store to a given structure offset, e.g. [rN, #0x134].
// Reversing an object means asking "who writes this field", and with no symbol
// table the only handle is the immediate offset itself.
//
// usage: -postScript MFStores.java 0x134 0x138 0x12C
//
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.listing.Instruction;
import ghidra.program.model.listing.InstructionIterator;
import ghidra.program.model.listing.Function;
import ghidra.program.model.scalar.Scalar;
import java.util.HashSet;
import java.util.Set;

public class MFStores extends GhidraScript {
    @Override
    public void run() throws Exception {
        Set<Long> want = new HashSet<>();
        for (String a : getScriptArgs())
            want.add(Long.parseLong(a.replace("0x", "").replace("0X", ""), 16));

        InstructionIterator it = currentProgram.getListing().getInstructions(true);
        int n = 0;
        while (it.hasNext()) {
            Instruction ins = it.next();
            String m = ins.getMnemonicString().toLowerCase();
            boolean store = m.startsWith("str") || m.startsWith("vstr");
            boolean load  = m.startsWith("ldr") || m.startsWith("vldr");
            if (!store && !load) continue;
            for (int op = 0; op < ins.getNumOperands(); op++) {
                for (Object o : ins.getOpObjects(op)) {
                    if (!(o instanceof Scalar)) continue;
                    long v = ((Scalar) o).getUnsignedValue();
                    if (!want.contains(v)) continue;
                    Function f = getFunctionContaining(ins.getAddress());
                    println(String.format("  %s  %-6s 0x%03X  %-28s %s",
                        ins.getAddress(), store ? "STORE" : "load", v,
                        ins.toString(),
                        f == null ? "<no function>" : f.getName()));
                    n++;
                }
            }
        }
        println("  " + n + " reference(s)");
    }
}
