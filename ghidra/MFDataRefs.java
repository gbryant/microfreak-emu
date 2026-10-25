// Report every reference to a data address, with the function each comes from.
// Data addresses need a different lookup from code: getFunctionContaining is
// meaningless for a RAM global, so this reports the referrers instead.
//
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

public class MFDataRefs extends GhidraScript {
    @Override
    public void run() throws Exception {
        for (String arg : getScriptArgs()) {
            Address at = toAddr(Long.parseLong(arg.replace("0x", ""), 16));
            println("");
            println("==== references to " + arg);
            ReferenceIterator it =
                currentProgram.getReferenceManager().getReferencesTo(at);
            int n = 0;
            while (it.hasNext() && n < 40) {
                Reference r = it.next();
                Function f = getFunctionContaining(r.getFromAddress());
                println("   " + r.getFromAddress() + "  " + r.getReferenceType()
                        + "  in " + (f == null ? "<no function>"
                                               : f.getName() + " @ " + f.getEntryPoint()));
                n++;
            }
            if (n == 0) println("   none found");
        }
    }
}
