# Ghidra, hands-on, on this firmware

The project's knowledge has always lived in markdown tables while Ghidra showed
`FUN_0804ad1c`. `MFApplySymbols.java` closes that gap: it pushes every named
address from the docs into the program, so the listing and the decompiler read
in this project's own vocabulary.

## Get the annotated project

```sh
uv run python -m mfemu.ghidra_symbols   # docs/ -> ghidra/mf_symbols.json
                                        # (--print to see it without writing)

JAVA_HOME=/opt/homebrew/opt/openjdk@21 \
PATH=/opt/homebrew/opt/openjdk@21/bin:$PATH \
/opt/homebrew/opt/ghidra/libexec/support/analyzeHeadless ~ MFC \
    -process main.raw -noanalysis \
    -scriptPath ghidra -postScript MFApplySymbols.java
```

Add `-readOnly` to see the report without touching the project. There is no
`java` on PATH here, which is why `JAVA_HOME` is spelled out — the error Ghidra
gives otherwise ("Unable to locate a Java Runtime") does not say which JDK it
wants.

Then open `~/MFC.gpr` in the Ghidra GUI and the names are there. As of the first
run: **75 functions renamed, 56 labels, 12 described-only, 0 failures.**

Re-run it after adding a row to the docs. The docs stay the source of truth —
the script reads them and never writes them, so there is one place to correct a
mistake.

## In the GUI

Ghidra is started here by double-clicking `/Applications/Ghidra` (12.1.4), then
opening `~/MFC.gpr`.

**Opening the `.gpr` is not enough to see anything.** That gives the *project
browser*, a file tree. The program is `main.raw` inside it — **double-click
that** and the CodeBrowser opens with the names in place.

Quick confirmation: press `G` (Go To), enter `0804acc8`, and you should land on
`sysex_op_dispatch`, with a plate comment naming its confidence and the
`file:line` it came from. Others worth a look: `08044290` `qspi_write`,
`0802f2dc` `sai2_blockB_dma_init`, `08016e30` `sample_engine_render`.

To browse the lot: **Window -> Symbol Tree**, then the *Functions* folder.

### Running the scripts from the GUI

Better than depending on headless, and the point of being hands-on:

1. **Window -> Script Manager**
2. **Script Directories** (the bundle icon, top right of that window)
3. Add this repository's `ghidra/` directory
4. Filter for the script, select it, **Run** (the green arrow)
5. Output goes to the Console — **then `Ctrl-S` to save**

**The GUI does not auto-save; headless does.** `analyzeHeadless` reports
`Save succeeded` and the change is on disk. A script run from the Script Manager
leaves the program dirty until you save it, so a GUI run that is not saved looks
exactly like a script that did nothing.

After adding a row to the docs: regenerate in the terminal with
`uv run python -m mfemu.ghidra_symbols`, then Run in the GUI and save.

### Headless and the GUI cannot both hold the project

Whichever opens it first takes the lock; the other gets

    ERROR Abort due to Headless analyzer error:
    ghidra.framework.store.LockException: Unable to lock project! ~/MFC

Harmless, just a refusal — but note that a wrapper which filters script output
will swallow it and the run will look like a script that printed nothing. Close
the CodeBrowser *and* the project window before running anything headless.

## Read the report, not just the names

Three mismatch classes are called out, and each means something:

| | what it tells you |
|---|---|
| `MID-FUNCTION` (34) | the docs name an *instruction*, not an entry point — deliberate here. Gets a label and a comment; the containing function is reported |
| `NOT-A-FUNCTION` (7) | no function starts there and the bytes would not make one. All seven are in-flash **data**: the `tbh` jump table, the engine vtables, label tables, an LCG multiplier |
| `COLLISION` (1) | two rows reduced to the same identifier; the second gets `_2` |

A `NOT-A-FUNCTION` on something the docs call code would be worth chasing —
either the address is wrong or the code is reached only indirectly, which
`METHOD.md` records as a real false negative from `MFRefs`.

## Confidence is the most valuable column

`FIRMWARE.md` is strict about it, and says why: *"a wrong name here costs
more than a missing one."*

| | |
|---|---|
| `T` | **Traced** — observed executing, arguments or effects read at runtime |
| `D` | **Disassembled** — read the code, the name follows from what it does |
| `I` | **Inferred** — from a call site, literal pool or vtable slot; **not confirmed** |

Every plate comment carries the mark, the expansion, the note, and the
`file:line` it came from. So when the decompiler shows you `HAL_PCD_EP_Receive`,
the comment tells you that name is *inferred* and where to go argue with it.
`--conf T` builds a listing of only what has actually been seen running.

Current split: **72 traced, 63 disassembled, 8 inferred.**

## Coming from pre-Ghidra RE

Rough translation, since the concepts carry over but the names don't:

| elsewhere | Ghidra |
|---|---|
| segments | **memory blocks** (`MFBlocks.java` prints them) |
| make code / make data | `D` / `Ctrl-D`, or `disassemble()` in a script |
| cross-references | **references**; `Ctrl-Shift-F` for refs *to* |
| rename | `L` |
| set function signature | `Ctrl-F6` |
| comment | `;` (EOL), `Ctrl-;` (plate) |
| structure definition | **Data Type Manager**, bottom-left |
| decompiler | the right-hand pane, always on |
| scripting | Java or Python in the Script Manager, or `analyzeHeadless -postScript` |

The habit worth forming early is that **the decompiler and the listing disagree
usefully**. The decompiler is a model; when it produces nonsense the listing
tells you why.

## The traps this image sets

Each of these has cost this project at least a session.

**Check the project holds the binary you think.** A stale `MFC` cost most of
2026-09-10 — `~/MF.gpr` had a `ram` block of 386,895 bytes against `main.raw`'s
473,695, so every address decoded *different bytes*, and five separate
"findings" were all artefacts. Run `MFBlocks.java` first; `ram` must be
`0x73A5F`.

**`unaff_rN` on a callee-saved register means a wrong function boundary** — the
prologue was never decoded — *or* the wrong binary. Check the binary first;
it is one command and the cheaper mistake to rule out. `MFRedisasm.java` fixes
genuine misalignment.

**It is Thumb-2.** A function entered in the wrong mode decodes as plausible
garbage. `MFDisasm.java` exists to compare Ghidra's decode against an
independent one.

**Script output is prefixed on the first line only.** `println` emits
`INFO  <Script>.java> …` on line one and nothing on the rest, so a filter that
greps *for* that prefix silently eats the body of every multi-line decompile.
Filter the noise out instead.

**A scan for quiet-looking bytes measures nothing.** Two "unused region"
findings were retracted — a 190-byte zero run that was the zero-crossing of an
int16 LUT, and "5,348 bytes of slack" that was seven live objects. Prove a
region dead by references, not by looking at it.

## The scripts

| script | what it answers |
|---|---|
| `MFBlocks` | what is mapped, and is this the right binary |
| `MFSetup` / `MFSetupF0` | lay out blocks + entry points on a fresh import (F4 main / F0 co-processor) |
| `MFApplySymbols` | push the docs' names into the program |
| `MFFuncs` | what functions live in this range, and which reference a constant |
| `MFDisasm` / `MFRange` | instructions at a point / across a window |
| `MFDecompile` | the C for a function |
| `MFRefs` | who calls this, and what contains this address |
| `MFDataRefs` | who references this RAM global (the right tool for data) |
| `MFCallees` | transitive callees, for attributing an allocation or a store |
| `MFStores` | who writes `[rN, #0xNN]` — the handle on a field with no symbols |
| `MFRedisasm` | clear and re-disassemble a misaligned region |

## The loop

1. Find something in Ghidra.
2. Add a row to `FIRMWARE.md` with an honest `T`/`D`/`I`.
3. Re-run `ghidra_symbols.py` and `MFApplySymbols.java`.

That way a session's work lands in both the prose and the tool, and the next
person gets it for free. Step 2 is the one that is easy to skip and expensive to
have skipped.

## Something real to practise on

`captures/validated-payloads/README.md` has an open, reproducible bug: two SysEx
messages, 62 bytes, freeze the instrument, and the same bytes run clean in
Renode. The handler is `vocab_handler` (built by `speech_sysex.py`, living at
`0x0807B264` in v12); the dispatcher is `sysex_op_dispatch` at `0x0804ACC8`; the
inner loop does a 7-to-8 unpack and walks a destination pointer. Neither message
2's destination (`0x032F`) nor its data is fatal alone — only together.

Nobody knows why yet. Six hypotheses have died.
