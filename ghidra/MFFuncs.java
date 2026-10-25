// List functions in an address range, with size and whether they reference a
// given constant. Navigation helper: Ghidra's auto-named FUN_* addresses are
// the only handle on this image, and "what is next to this function" is a
// question that comes up constantly.
//
// usage: -postScript MFFuncs.java 0x0804e000 0x0804f200
//
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.listing.FunctionIterator;

public class MFFuncs extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        Address lo = toAddr(Long.parseLong(a[0].replace("0x", ""), 16));
        Address hi = toAddr(Long.parseLong(a[1].replace("0x", ""), 16));
        FunctionIterator it = currentProgram.getFunctionManager()
                .getFunctions(lo, true);
        while (it.hasNext()) {
            Function f = it.next();
            if (f.getEntryPoint().compareTo(hi) > 0) break;
            println(String.format("  %s  %-22s size=%d",
                f.getEntryPoint(), f.getName(), f.getBody().getNumAddresses()));
        }
    }
}
