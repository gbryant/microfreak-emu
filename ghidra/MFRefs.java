// Report the function containing each address given, and every reference to
// that function -- the reverse edges the emulator cannot supply.
//
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import ghidra.program.model.symbol.ReferenceIterator;

public class MFRefs extends GhidraScript {
    @Override
    public void run() throws Exception {
        for (String arg : getScriptArgs()) {
            Address at = toAddr(Long.parseLong(arg.replace("0x", ""), 16));
            Function f = getFunctionContaining(at);
            println("");
            println("==== " + arg + " is in "
                    + (f == null ? "<no function>"
                                 : f.getName() + " @ " + f.getEntryPoint()));
            if (f == null) continue;
            ReferenceIterator it = currentProgram.getReferenceManager()
                    .getReferencesTo(f.getEntryPoint());
            int n = 0;
            while (it.hasNext() && n < 20) {
                Reference r = it.next();
                Function from = getFunctionContaining(r.getFromAddress());
                println("   referenced from " + r.getFromAddress()
                        + " (" + r.getReferenceType() + ") in "
                        + (from == null ? "<none>" : from.getName()));
                n++;
            }
            if (n == 0) println("   no references -- reached indirectly only");
        }
    }
}
