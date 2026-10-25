// Apply this project's accumulated names to the MFC program, from the docs.
//
// `mfemu.ghidra_symbols` harvests every named address out of docs/FIRMWARE.md,
// docs/SPEECH.md and docs/PROTOCOL.md into mf_symbols.json. This puts them
// into the listing, so the decompiler shows `preset_page_write` instead of
// `FUN_08045238` and every name carries where it came from.
//
//   analyzeHeadless ~ MFC -process main.raw -noanalysis \
//       -scriptPath ghidra -postScript MFApplySymbols.java
//
// Add `-readOnly` to see the report without changing the project.
//
// Names are set as USER_DEFINED, so Ghidra shows them as deliberate rather than
// analysed, and they survive re-analysis. Re-running is safe: setting a name to
// what it already is costs nothing.
//
// The report matters as much as the renaming. Every address the docs name is
// checked against what the program actually has, and three kinds of mismatch
// are called out:
//
//   NOT-A-FUNCTION  the docs call it code, but no function starts there and the
//                   bytes have not been disassembled. Either the address is
//                   wrong or that code is only reached indirectly.
//   MID-FUNCTION    the address is inside a function that starts elsewhere.
//                   Often deliberate here -- the docs name specific
//                   instructions -- so these get a label and a comment instead
//                   of a rename, and the containing function is reported.
//   COLLISION       two docs rows reduced to the same identifier.
//
// METHOD.md's warning applies to all of it: "before believing any decompile,
// print the memory blocks and check the initialised block's size against the
// file on disk". A stale project will make this script report nonsense
// confidently. Run MFBlocks.java first.
//
//@category MicroFreak
import ghidra.app.script.GhidraScript;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.SourceType;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

public class MFApplySymbols extends GhidraScript {

    /** One harvested row. */
    private static class Sym {
        long addr; String name, rawName, conf, notes, where, what, kind;
        boolean commentOnly;
        List<String> also = new ArrayList<>();
    }

    @Override
    public void run() throws Exception {
        String[] args = getScriptArgs();
        File json = new File(args.length > 0 ? args[0]
                : new File(getSourceFile().getParentFile().getFile(false),
                           "mf_symbols.json").getPath());
        if (!json.exists()) {
            println("no " + json + " -- run `python -m mfemu.ghidra_symbols` first");
            return;
        }
        List<Sym> syms = parse(json);
        println("loaded " + syms.size() + " symbols from " + json.getName());

        int renamed = 0, labelled = 0, midFunc = 0, notFunc = 0, collided = 0, failed = 0;
        int described = 0;
        Set<String> used = new HashSet<>();
        Map<String, Integer> dupes = new HashMap<>();
        List<String> midReport = new ArrayList<>();
        List<String> notReport = new ArrayList<>();

        for (Sym s : syms) {
            Address at = toAddr(s.addr);
            String name = s.name;
            if (!used.add(name)) {
                int n = dupes.merge(name, 2, Integer::sum);
                name = name + "_" + n;
                used.add(name);
                collided++;
                println(String.format("COLLISION  %-30s at %s -> %s",
                                      s.name, at, name));
            }
            String comment = comment(s);
            if (s.commentOnly) {
                // The docs' Name cell is a sentence here, not an identifier.
                // Worth reading at the address, useless as a symbol.
                setPlateComment(at, comment);
                described++;
                used.remove(name);
                continue;
            }
            try {
                if ("code".equals(s.kind)) {
                    Function f = getFunctionAt(at);
                    if (f == null) {
                        Function containing = getFunctionContaining(at);
                        if (containing != null) {
                            // The docs name instructions, not just entry points.
                            // A label keeps the name findable without pretending
                            // a function starts here.
                            createLabel(at, name, true, SourceType.USER_DEFINED);
                            setPlateComment(at, comment);
                            labelled++; midFunc++;
                            midReport.add(String.format("  %s  %-30s inside %s @ %s",
                                    at, name, containing.getName(),
                                    containing.getEntryPoint()));
                            continue;
                        }
                        // Nothing here yet. Try to make it code; if the bytes
                        // are not a plausible function this will fail, and that
                        // failure is information.
                        try {
                            disassemble(at);
                            f = createFunction(at, name);
                        } catch (Exception e) {
                            f = null;
                        }
                        if (f == null) {
                            createLabel(at, name, true, SourceType.USER_DEFINED);
                            setPlateComment(at, comment);
                            labelled++; notFunc++;
                            notReport.add(String.format("  %s  %-30s %s",
                                    at, name, s.where));
                            continue;
                        }
                    }
                    f.setName(name, SourceType.USER_DEFINED);
                    setPlateComment(at, comment);
                    renamed++;
                } else {
                    // SRAM and anything else: a label plus the note. The blocks
                    // MFSetup.java creates make these resolve in decompiled
                    // output instead of showing as bare constants.
                    createLabel(at, name, true, SourceType.USER_DEFINED);
                    setPlateComment(at, comment);
                    labelled++;
                }
            } catch (Exception e) {
                failed++;
                println(String.format("FAILED     %s %-30s %s", at, name, e));
            }
        }

        println("");
        println("renamed functions : " + renamed);
        println("labels created    : " + labelled);
        println("described only    : " + described + "  (prose in the Name cell)");
        println("  of those, mid-function : " + midFunc);
        println("  of those, no function  : " + notFunc);
        println("name collisions   : " + collided);
        println("failures          : " + failed);

        if (!midReport.isEmpty()) {
            println("");
            println("MID-FUNCTION (named instruction inside a larger function):");
            for (String r : midReport) println(r);
        }
        if (!notReport.isEmpty()) {
            println("");
            println("NOT-A-FUNCTION (no function, and the bytes would not make one):");
            println("  Check these: either the address is wrong, or the code is");
            println("  reached only indirectly and Ghidra never found it.");
            for (String r : notReport) println(r);
        }
    }

    /** The plate comment: what it is, how well it is known, and where that came from. */
    private String comment(Sym s) {
        StringBuilder b = new StringBuilder();
        b.append(s.rawName).append("\n");
        b.append("confidence: ").append(s.conf).append(" (")
         .append("T".equals(s.conf) ? "traced -- observed executing"
                 : "D".equals(s.conf) ? "disassembled -- name follows from the code"
                 : "inferred -- from a call site, literal or vtable; NOT confirmed")
         .append(")\n");
        if (!s.notes.isEmpty()) {
            b.append(s.notes).append("\n");
        }
        b.append("source: ").append(s.where);
        if (!s.what.isEmpty()) b.append("  (").append(s.what).append(")");
        for (String a : s.also) {
            b.append("\nalso: ").append(a);
        }
        return b.toString();
    }

    // A deliberately small JSON reader. Pulling in a library for a file this
    // script generates itself would be the wrong trade; the shape is fixed by
    // ghidra_symbols.py and a malformed field throws rather than guessing.
    private List<Sym> parse(File f) throws Exception {
        StringBuilder sb = new StringBuilder();
        try (BufferedReader r = new BufferedReader(new FileReader(f))) {
            String line;
            while ((line = r.readLine()) != null) sb.append(line).append('\n');
        }
        String text = sb.toString();
        List<Sym> out = new ArrayList<>();
        int i = 0;
        while ((i = text.indexOf('{', i)) >= 0) {
            int end = text.indexOf('}', i);
            if (end < 0) break;
            String obj = text.substring(i, end + 1);
            Sym s = new Sym();
            s.addr = Long.parseLong(num(obj, "addr"));
            s.name = str(obj, "name");
            s.rawName = str(obj, "raw_name");
            s.conf = str(obj, "conf");
            s.notes = str(obj, "notes");
            s.where = str(obj, "where");
            s.what = str(obj, "what");
            s.kind = str(obj, "kind");
            s.commentOnly = flag(obj, "comment_only");
            for (String a : arr(obj, "also")) s.also.add(a);
            out.add(s);
            i = end + 1;
        }
        return out;
    }

    private String num(String obj, String key) {
        int k = obj.indexOf("\"" + key + "\"");
        int c = obj.indexOf(':', k) + 1;
        int e = c;
        while (e < obj.length() && "-0123456789".indexOf(obj.charAt(e)) < 0) e++;
        int st = e;
        while (e < obj.length() && "-0123456789".indexOf(obj.charAt(e)) >= 0) e++;
        return obj.substring(st, e);
    }

    /** A JSON boolean. Absent reads as false, which is the safe default here:
     *  a missing flag means "treat it as a real symbol", and a wrong name is
     *  visible in the listing, whereas a silently skipped one is not. */
    private boolean flag(String obj, String key) {
        int k = obj.indexOf("\"" + key + "\"");
        if (k < 0) return false;
        int c = obj.indexOf(':', k);
        if (c < 0) return false;
        return obj.regionMatches(true, obj.indexOf('e', c) - 3, "true", 0, 4)
                || obj.substring(c + 1, Math.min(c + 8, obj.length())).trim()
                      .startsWith("true");
    }

    private String str(String obj, String key) {
        int k = obj.indexOf("\"" + key + "\"");
        if (k < 0) return "";
        int q = obj.indexOf('"', obj.indexOf(':', k) + 1);
        if (q < 0) return "";
        StringBuilder b = new StringBuilder();
        for (int i = q + 1; i < obj.length(); i++) {
            char ch = obj.charAt(i);
            if (ch == '\\') { b.append(obj.charAt(++i)); continue; }
            if (ch == '"') break;
            b.append(ch);
        }
        return b.toString();
    }

    private List<String> arr(String obj, String key) {
        List<String> out = new ArrayList<>();
        int k = obj.indexOf("\"" + key + "\"");
        if (k < 0) return out;
        int lb = obj.indexOf('[', k), rb = obj.indexOf(']', lb);
        if (lb < 0 || rb < 0) return out;
        String body = obj.substring(lb + 1, rb);
        int i = 0;
        while ((i = body.indexOf('"', i)) >= 0) {
            StringBuilder b = new StringBuilder();
            int j = i + 1;
            for (; j < body.length(); j++) {
                char ch = body.charAt(j);
                if (ch == '\\') { b.append(body.charAt(++j)); continue; }
                if (ch == '"') break;
                b.append(ch);
            }
            out.add(b.toString());
            i = j + 1;
        }
        return out;
    }
}
