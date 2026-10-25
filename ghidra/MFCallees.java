// Transitive callees of each address given, with their address ranges.
//
// Attributing a resource (an allocation, a store) to a caller needs the call
// graph: a size subtracted inside LPCSpeechSynthWordBank::Init belongs to
// SpeechEngine::Init, and sorting sites by address gets that wrong.
// Args: DEPTH ADDR [ADDR...]
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import java.util.*;

public class MFCallees extends GhidraScript {
    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        int depth = Integer.parseInt(args[0]);
        for (int ai = 1; ai < args.length; ai++) {
            Address at = toAddr(Long.parseLong(args[ai].replace("0x", ""), 16));
            Function root = getFunctionContaining(at);
            if (root == null) {
                try { disassemble(at); root = createFunction(at, null); } catch (Exception e) { }
            }
            println("==== " + args[ai] + " root=" + (root == null ? "?" : root.getName()));
            if (root == null) continue;
            Set<Function> seen = new HashSet<>();
            Deque<Object[]> q = new ArrayDeque<>();
            q.add(new Object[]{root, 0});
            while (!q.isEmpty()) {
                Object[] cur = q.poll();
                Function f = (Function) cur[0];
                int dd = (Integer) cur[1];
                if (f == null || !seen.add(f)) continue;
                println(String.format("   %s %s %s %s", f.getName(),
                        f.getEntryPoint(), f.getBody().getMinAddress(),
                        f.getBody().getMaxAddress()));
                if (dd >= depth) continue;
                for (Function c : f.getCalledFunctions(monitor))
                    q.add(new Object[]{c, dd + 1});
            }
        }
    }
}
