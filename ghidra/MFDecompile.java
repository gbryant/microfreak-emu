// Decompile the functions named on the command line and print the C.
//
//@category MicroFreak
import ghidra.app.decompiler.DecompInterface;
import ghidra.app.decompiler.DecompileResults;
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;

public class MFDecompile extends GhidraScript {

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        DecompInterface ifc = new DecompInterface();
        ifc.openProgram(currentProgram);
        for (String arg : args) {
            Address at = toAddr(Long.parseLong(arg.replace("0x", ""), 16));
            Function f = getFunctionContaining(at);
            if (f == null) {
                try {
                    disassemble(at);
                    f = createFunction(at, null);
                } catch (Exception e) { }
            }
            println("");
            println("======== " + arg + " ========");
            if (f == null) {
                println("no function at or containing " + at);
                continue;
            }
            DecompileResults res = ifc.decompileFunction(f, 120, monitor);
            if (res == null || res.getDecompiledFunction() == null) {
                println("decompilation failed: "
                        + (res == null ? "null" : res.getErrorMessage()));
                continue;
            }
            println(res.getDecompiledFunction().getC());
        }
        ifc.dispose();
    }
}
