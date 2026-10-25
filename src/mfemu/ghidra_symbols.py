#!/usr/bin/env python3
"""Harvest this project's address tables out of the docs into a Ghidra symbol file.

Every address this project has named lives in a markdown table, and the Ghidra
project shows `FUN_0804ad1c`. So every lookup is a cross-reference by hand, and
knowledge that was expensive to get does not reach the tool where it would do
the most good.

    python -m mfemu.ghidra_symbols              # write ghidra/mf_symbols.json
    python -m mfemu.ghidra_symbols --print      # list it, write nothing
    python -m mfemu.ghidra_symbols --conf T     # only traced symbols

Then, in the MFC project:

    analyzeHeadless ~ MFC -process main.raw -noanalysis \\
        -scriptPath ghidra -postScript MFApplySymbols.java

The docs stay the source of truth: this reads them, never writes them. Re-run it
after adding a row and the project catches up.

## What it reads

`FIRMWARE.md`, `SPEECH.md` and `EMULATION.md` share one row shape:

    | `0x08045238` | T | `preset_page_write(obj, slot)` | issues erase + 16 page programs |
      address        conf  name                            notes

`PROTOCOL.md`'s handler table is a different shape and worth having, because it
names sixteen dispatcher handlers:

    | `0x58` | `0x0804AF0A` | confirmed live | sample audio-block select (write) |
      opcode   handler        status           role

## Confidence survives the trip

FIRMWARE.md is strict about `T`/`D`/`I` -- traced, disassembled, inferred --
and says "a wrong name here costs more than a missing one". That distinction is
the most valuable thing in the table, so it goes into every comment, with the
file and line it came from, and `--conf` can hold a listing to what was actually
observed running.
"""
import argparse
import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DOCS = os.path.join(REPO, "docs")      # where SOURCES actually live

# (file, what the rows mean). Order matters only for which provenance wins a
# duplicate: first file listed is kept, so the curated map beats the others.
SOURCES = [
    ("FIRMWARE.md", "the firmware map"),
    ("SPEECH.md", "the Speech engine"),
    # The docstring above has always named EMULATION.md as a source and this
    # list omitted it. It carries no rows in the harvest shape today, so this
    # changes nothing now -- which is the point of adding it before it does.
    ("EMULATION.md", "the emulation notes"),
]

CODE_LO, CODE_HI = 0x0800C000, 0x0807FA5F      # the image, per MFBlocks
SRAM_LO, SRAM_HI = 0x20000000, 0x2001FFFF

ROW = re.compile(r"^\|\s*`?(0x[0-9A-Fa-f]{8})`?\s*\|\s*([TDI])\s*\|(.*)$")
SYSEX_ROW = re.compile(
    r"^\|\s*`(0x[0-9A-Fa-f]{2})`\s*\|\s*`(0x[0-9A-Fa-f]{8})`\s*\|([^|]*)\|(.*?)\|?\s*$")


def _cells(rest):
    """Split a row's tail into (name, notes), tolerating pipes inside backticks."""
    out, cur, tick = [], [], False
    for ch in rest:
        if ch == "`":
            tick = not tick
        if ch == "|" and not tick:
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    cells = [c.strip() for c in out]
    name = cells[0] if cells else ""
    notes = cells[1] if len(cells) > 1 else ""
    return name.strip(" `"), notes.strip()


def is_prose(name):
    """Is this Name cell a description rather than an identifier?

    The tables are a symbol table but the Name column is written for humans, so
    some cells are sentences -- "mov r9,r2 so r9 is the formant caller's third
    argument", "writes coefficient 0 to [r0+0x18]". Those are worth having as
    comments and useless as symbols; left unfiltered they turn the listing into
    prose. Short descriptive names ("mix writer", "engine vtables") still make
    good labels, so the cut is at five words, plus anything with sentence
    punctuation.
    """
    # Drop an argument list first. `granular_render(engine, out)` is an
    # identifier with a signature attached, not prose, and the comma inside the
    # parentheses would otherwise condemn every multi-argument function here.
    bare = name.strip(" `").split("(")[0].strip()
    if len(bare.split()) > 4:
        return True
    return any(ch in bare for ch in ",;'")


def ident(name):
    """-> a Ghidra-safe symbol name, or None if there is nothing usable.

    Strikethrough rows (`~~stale~~`) are skipped: the docs use them for retracted
    findings, and importing those would put known-wrong names in the tool.
    """
    if not name or "~~" in name:
        return None
    n = name.split("(")[0]                      # drop an argument list
    n = n.replace("->", "to_")
    n = re.sub(r"[^0-9A-Za-z_]+", "_", n).strip("_")
    if not n or n[0].isdigit():
        return None
    return n


def harvest():
    syms, seen = [], {}
    missing = [f for f, _ in SOURCES if not os.path.exists(os.path.join(DOCS, f))]
    if missing:
        raise SystemExit("ghidra_symbols: no such document(s) under %s: %s"
                         % (DOCS, ", ".join(missing)))
    for fname, what in SOURCES:
        path = os.path.join(DOCS, fname)
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            m = ROW.match(line.rstrip("\n"))
            if not m:
                continue
            addr = int(m.group(1), 16)
            conf = m.group(2)
            name, notes = _cells(m.group(3))
            sym = ident(name)
            if sym is None:
                continue
            if addr in seen:
                seen[addr]["also"].append("%s:%d %s" % (fname, lineno, name))
                continue
            rec = {"addr": addr, "name": sym, "raw_name": name, "conf": conf,
                   "notes": notes, "where": "%s:%d" % (fname, lineno),
                   "what": what, "also": [],
                   "comment_only": is_prose(name)}
            rec["kind"] = ("code" if CODE_LO <= addr <= CODE_HI
                           else "data" if SRAM_LO <= addr <= SRAM_HI else "other")
            seen[addr] = rec
            syms.append(rec)

    # PROTOCOL.md's handler table: name handlers after the opcode they serve, which
    # is the fact you actually want in the listing.
    path = os.path.join(DOCS, "PROTOCOL.md")
    if os.path.exists(path):
        for lineno, line in enumerate(open(path, encoding="utf-8"), 1):
            m = SYSEX_ROW.match(line.rstrip("\n"))
            if not m:
                continue
            opcode, addr_s, status, role = m.groups()
            addr = int(addr_s, 16)
            if addr in seen:
                seen[addr]["also"].append("PROTOCOL.md:%d opcode %s" % (lineno, opcode))
                continue
            status, role = status.strip(), role.strip()
            rec = {"addr": addr,
                   "name": "sysex_handler_%s" % opcode[2:].upper(),
                   "raw_name": "SysEx opcode %s handler" % opcode,
                   # "confirmed live" is a traced observation; the rest are not.
                   "conf": "T" if "confirmed" in status.lower() else "I",
                   "notes": "%s -- %s" % (role, status) if role else status,
                   "where": "PROTOCOL.md:%d" % lineno,
                   "what": "the SysEx dispatcher", "also": [], "kind": "code",
                   "comment_only": False}
            seen[addr] = rec
            syms.append(rec)

    syms.sort(key=lambda r: r["addr"])
    return syms


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=os.path.join(REPO, "ghidra", "mf_symbols.json"))
    ap.add_argument("--conf", default="TDI", help="confidence marks to keep (default TDI)")
    ap.add_argument("--print", dest="show", action="store_true",
                    help="list what was harvested and write NOTHING "
                         "(a dry run: --out is not touched)")
    a = ap.parse_args()

    syms = [s for s in harvest() if s["conf"] in a.conf]
    if a.show:
        for s in syms:
            print("0x%08X  %-4s %-5s %-34s %s"
                  % (s["addr"], s["kind"], s["conf"], s["name"], s["where"]))
    by_kind, by_conf = {}, {}
    for s in syms:
        by_kind[s["kind"]] = by_kind.get(s["kind"], 0) + 1
        by_conf[s["conf"]] = by_conf.get(s["conf"], 0) + 1
    prose = sum(1 for s in syms if s.get("comment_only"))
    if a.show:
        print("%d symbols (--print: %s NOT written)" % (len(syms), a.out))
    elif not syms:
        # Overwriting a good file with [] is worse than refusing to write.
        raise SystemExit("ghidra_symbols: harvested 0 symbols; refusing to "
                         "overwrite %s. Check the table formats in docs/."
                         % a.out)
    else:
        json.dump(syms, open(a.out, "w"), indent=1)
        print("%d symbols -> %s" % (len(syms), a.out))
    print("  by kind: %s" % ", ".join("%s %d" % kv for kv in sorted(by_kind.items())))
    print("  by confidence: %s" % ", ".join("%s %d" % kv for kv in sorted(by_conf.items())))
    print("  %d are descriptions, not identifiers -> comment only, no symbol" % prose)


if __name__ == "__main__":
    main()
