// Clear a mis-aligned region and re-disassemble from a known-good entry.
// Ghidra's auto-analysis can start mid-instruction and carry the misalignment
// forward, producing a function whose decompilation is nonsense (the telltale
// is unaff_rN for callee-saved registers -- the prologue was never decoded).
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.address.AddressSet;
import ghidra.program.model.listing.Function;

public class MFRedisasm extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] a = getScriptArgs();
        Address start = toAddr(Long.parseLong(a[0].replace("0x", ""), 16));
        Address end   = toAddr(Long.parseLong(a[1].replace("0x", ""), 16));
        println("clearing " + start + " .. " + end);
        clearListing(new AddressSet(start, end));
        Function f = getFunctionContaining(start);
        if (f != null) { println("removing stale function " + f.getName()); removeFunction(f); }
        disassemble(start);
        f = createFunction(start, null);
        println(f == null ? "createFunction FAILED" : "created " + f.getName() + " body=" + f.getBody().getNumAddresses() + " bytes");
    }
}
