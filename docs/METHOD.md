# How to investigate this thing without wasting days

Hard-won during the Speech-engine hunt (2026-09-08/09), which took far longer
than it should have. Every item below cost at least one wasted run, and
several cost a wrong conclusion that had to be retracted from the docs.

Read this before starting a new emulator or firmware investigation.

---

## Tools that lie to you

**`cpu AddHook` silently does nothing on an address that is not a translation
block boundary.** No error, no warning -- the hook simply never fires, which
looks exactly like "that code never ran". Burned on `0x0805B3D2`,
`0x0802FE80`, `0x08033D10` and `0x08030A32`, each time producing a confident
and wrong "this function is not reached".

*Rule: before concluding a hook proves absence, prove the hook fires at all --
put it on a nearby call or branch target you know executes.*

**Watchpoints do not have this problem**, and a watchpoint hook can read
`cpu.PC` **and `cpu.LR`**. That is how `osc_set_gate`'s caller was found after
the direct hook refused to fire. Prefer a watchpoint on the data over a hook
on the code whenever there is a choice.

**Callee-saved registers survive into the callee.** `r4` was needed to address
a struct, and no hook would fire where it was live -- but `r4` is callee-saved,
so a watchpoint inside a function it called could read it:
`cpu.GetRegister(4)`. Worth remembering when a value seems unreachable.

**A harvester that finds nothing exits 0.** `mfemu.ghidra_symbols` looked for
its source documents at the repository root while they live in `docs/`, so
every one was missed behind an `os.path.exists()` guard and it reported
success having harvested nothing. Worse, `--print` wrote the output file
anyway, so a "show me what you'd do" run truncated the committed
`ghidra/mf_symbols.json` from 52,692 bytes to `[]`. Fixed 2026-10-17: a
missing source is now an error, `--print` writes nothing, and harvesting zero
symbols refuses to overwrite.

*Rule: an empty result and a successful run must not look the same. If a tool
can find nothing, make finding nothing fail.*

**A Monitor read can return no value, and `or 0` turns that into a fact.**
`monitor_read()` returns `None` when no bare value comes back. `boot.py`
printed `pc or 0`, so an unread PC appeared as `PC 0x00000000 (bootloader)` --
on a healthy application boot. `tests/test_boot.py` did the same and failed.
Measured 2026-10-17: that test failed 2 runs in 6, every failure the single
`cpu PC` read, and in one of them the value arrived as the answer to the NEXT
read. **The mechanism is not established.** Two known-answer controls (11
reads of flash constants; 24 PC reads interleaved with 24 constant reads in
one boot) produced 0 wrong answers and did not reproduce it, and the
cold-compiler-cache hypothesis was refuted by the cache's own timestamps. The
stale-prompt race `midilink.monitor()` documents is the right shape but
remains a lead. Both call sites now retry and report "unread" distinctly.

*Rule: `x or 0` on a value that can legitimately be absent manufactures a
measurement. Distinguish "could not read" from "read zero" at every point
where you print one.*

**Renode suppresses repeated log lines.** A message that stops appearing has
not necessarily stopped happening. Two separate "the drain never runs"
conclusions came from this. Count events by writing to memory or by using
distinct message text, not by trusting the log's tail.

**VFP registers are not reachable from a hook.** `GetRegisters()` does not
exist. Read floats from memory, or from the stack after a `vpush` -- e.g.
`vpush {d8-d15}` puts `s16` at `[sp]`, `s19` at `[sp+12]`, `s20` at `[sp+16]`.

**Never give Renode an unread stdout pipe.** It fills at 64 KB and blocks the
writer, so the emulated machine simply stops mid-transfer. The symptom is a
reply that never arrives. `midilink.py` writes the log to a file for this
reason.

**Verify the Ghidra project holds the binary you are asking about.** A stale
project cost most of a session on 2026-09-10. `~/MF.gpr` had a `ram` block of
386,895 bytes; `main.raw` is 473,695. Every address queried decoded *different
bytes at the same address*, so:

  * decompiles came out as nonsense and I blamed the decompiler;
  * clearing and re-disassembling changed nothing (the bytes were never the
    ones I thought);
  * `MFRefs` reported "no direct references" for a function that plainly has
    a caller;
  * a `DAT_` literal dereferenced to `0x68006023`, which is not a pointer.

Each failure was read as a fact about the firmware. None of them were.

*Rule: before believing any decompile, print the memory blocks and check the
initialised block's size against the file on disk. `unaff_rN` on callee-saved
registers in decompiler output means a wrong function boundary OR a wrong
binary -- check the binary first, it is one command
(`MFBlocks.java`) and it is the cheaper mistake to rule out.*

**`analyzeHeadless` needs `JAVA_HOME` set.** There is no `java` on the default
PATH here, and Ghidra's own error ("Unable to locate a Java Runtime ... no TTY
detected") does not say which JDK it wants. `openjdk@21` is installed:

    JAVA_HOME=/opt/homebrew/opt/openjdk@21 \
    PATH=/opt/homebrew/opt/openjdk@21/bin:$PATH \
    /opt/homebrew/opt/ghidra/libexec/support/analyzeHeadless ~ MFC \
        -process main.raw -noanalysis -scriptPath ../ghidra \
        -postScript MFBlocks.java

Also: a script's `println` output is prefixed `<Script>.java> ` on the **first**
line only, so filtering on that prefix silently eats the body of anything
multi-line -- which is every decompile. Filter the noise out instead of
filtering the wanted lines in.

The correct project is **`MFC`**, imported with the invocation in
EMULATION.md. `MFBlocks.java`, `MFDisasm.java` and `MFRedisasm.java` in
`../ghidra/` exist to make this check quick: blocks, Ghidra's own
disassembly (compare it against an independent one), and a clear/re-disassemble
for genuinely mis-aligned regions.

## Measurements that lie to you

**Check whether the requirement is real before hunting for the resource.** Two
sessions went into finding somewhere to put 4 KB of RAM for a relocated speech
descriptor table. The firmware already decodes the vocabulary into a 14 KB RAM
buffer (`frames_`, `0x20004880`), so overwriting that needed no new memory and
no boot-seeding stub at all. The search was well executed and answered the wrong
question. *Rule: when looking for somewhere to put something, first state what it
is for and check whether the firmware already holds it in a usable form.*


**Validate an immediate decoder against the disassembler before trusting any
constant it produces.** A hand-written `ThumbExpandImm` was wrong twice in one
session and each version produced a confident wrong number: rotating by `rot-8`
instead of `rot` turned `mov.w r3,#0x4000` into `0x400000`, and gating the
special cases on `imm12<11:9>` instead of `imm12<11:10>` turned `0xFFFFFFFF`
into `0xFE000001`. The first made a 16 KB buffer pool look like a 4 MB one and
nearly replaced a correct reading with an emulator run; the second corrupted a
reported `RCC_AHB1ENR` mask. Both were caught by comparing against instructions
Ghidra had already printed, which costs one command.


**A scan for quiet-looking bytes measures nothing about whether they are used.**
This has now cost two findings and been retracted twice -- the 190-byte zero runs
at `0x08061C16` (the zero-crossing region of an int16 LUT) and the "5,348 bytes
of unreferenced slack" at `0x0807B367` (seven objects reached by real literals,
one of them read by live code at `0x0805ECCA`). Both were declared free because
they *looked* empty.

*Rule: freeness is a claim about references, so measure references. Build the set
of words that a PC-relative `LDR` actually reads, and check membership.*

**And do not bucket every 4-byte-aligned word to do it.** The image is dense code,
so most aligned words are instruction pairs: `D8 F8 00 10` is `ldr.w r1,[r8]`,
not the address `0x1000F8D8`. Decoding the loads instead of the words cut 1,454
apparent SRAM "pointers" to 213 real ones, and it is the difference between a
region looking empty and looking owned.

**Absence of a write during one boot is not absence of an owner.** `0x2000C000`
was adopted because a watchpoint caught only the `.bss` clear writing it. It is
`.bss` -- `_sbss`..`_ebss` is `0x200002F8`..`0x2001F230` and `.data` + `.bss` +
stack come to exactly 128 KB, so every byte of SRAM is allocated and there is no
free region to find. A variable belonging to an untested feature looks identical
to free space under that test. **The linker's own symbols settle ownership in one
read; a watchpoint cannot.**

**Gaps between address literals are not free space either.** The two largest gaps
in this image's SRAM literal map are 48,487 and 28,217 bytes, and both are large
buffers reached through a base pointer.


**Renders depend on batch history -- but deterministically.** REFINED
2026-09-10; the earlier wording here was "do not reproduce", which is wrong
and sent a whole investigation after a non-existent state leak.

The emulator reproduces *exactly*. What varies is the **position in the
batch**: the same preset file rendered at positions 2, 3 and 4 of one boot
gave RMS 823 / 521 / 639 and three distinct hashes. Fix the prefix and the
output is byte-identical -- position 2 of two different batches with the same
position-1 render hashed the same, and a position-1 render was byte-identical
across three separate boots.

The mechanism is **free-running modulators**, not a leak. Kill them (cycling
envelope in `Env` rather than `Run`/`Loop`, no LFO routed) and positions
2/3/4 agree to 0.05% of RMS -- numerically different, inaudibly so. Leave them
on and the phase at note-on depends on elapsed firmware time, which is what
batch position changes. That is what the hardware does too.

Consequences: batching is valid, and one boot per preset is *not*
required. But a preset built on free-running modulation is a **distribution**
of sounds, not one sound -- judge it from several positions, never one. And
never A/B two arms at different positions: an early comparison in this
session had every design at position 1 and every baseline at position 2,
which made the whole table meaningless.

**A silent slot proves nothing on its own.** `Aft-WavPad` wants aftertouch and
`Brd But Arp` wants the arpeggiator; both are silent untouched. Three arms of
the preset-write investigation were confounded by writing into a slot that was
already silent.

**Byte-identical output between two arms means neither did anything.** Applied
ruthlessly: it caught a poke that never propagated, a "fix" to `mfrender` that
changed nothing, and a preset write that had gone to an unreachable slot.

**Check what the instrument is actually reading.** A "constant 1" in the audio
buffer was the float bit pattern `0x00000001` -- a denormal, 1.4e-45 -- seen
through a 16-bit read. It was reported as an integer sample and sent a whole
line of enquiry the wrong way.

**Object addresses move between runs.** Voices are allocated per note, so a
pointer captured in one run addresses a different object in the next. Poking a
remembered address "sticks" while the live object is untouched. Force the value
at the *copy or use site* instead, where the address is whatever the code is
actually using.

**Two renders at the same batch position are byte-identical, which can make a
comparison read as zero difference.** A second pass in a *fresh* Renode boot
puts position 0 at the same elapsed firmware time at note-on as position 0 of
the first pass. Same elapsed time means same modulator phase, so the hold
window is byte-identical and any measure of "how much did this preset differ
from itself" reads exactly 0 -- not because the preset is stable but because
nothing moved between the two renders.

Measured over 100 presets in 8 chunks: of 8 clips reading exactly zero, 6 sat
at position 0 of their chunk, and no clip at position 0 read non-zero, while a
position-6 clip in the same chunk read 0.3955. It is a bias rather than noise,
and it scales with the number of chunks.

The fix is to rotate the second pass by one so nothing occupies the same
position twice -- a rotation rather than a reversal, because a reversal leaves
the middle item fixed on odd counts. Afterwards every former zero read
non-zero: 0.0930, 0.1276, 0.1538, 0.2015, 0.2092, 0.2802, with the rotation as
the single isolated change.

Two corollaries. An exact 0.0 should be read as missing data rather than as
stability. And a silent preset compared against itself is silence versus
silence, which is a legitimate zero that says nothing about any modulator.

**The render protocol silences 9.6% of Arturia's own presets.** Measured
2026-10-02 by rendering all 448 factory presets: **43 came out silent**, across
ten engines -- `Hit Grains` 30 of 145 (21%), then `Speech` 2 of 29, `Superwave`
2 of 24, `Harmonic` 2 of 19, `Cloud Grains` 2 of 51, and one each in
`Waveshaper`, `Modal`, `Chords`, `Wavetable`, `Scan Grains`. Twelve engines had
none.

These are presets Arturia shipped and presumably tested, so this is a
measurement of **our protocol's blind spot against known-good material**, which
is better evidence than anything a batch of generated presets can give -- a
generated preset that renders silent might simply be bad. The spread across ten engines
rules out a single engine being broken.

**Partly retracted the next day: the likely cause is grammar, not the protocol.**
Four hypotheses about the protocol were tested against the 405 sounding presets
as a control and **all four failed to discriminate**: pressure routed anywhere
(72% of silents against 71% of sounding), `amp_mod` on (95% vs 91%), a long
attack (7% vs 10%), cycling-envelope amount near zero (2% vs 4%). The protocol
sends only note-on and note-off at velocity 100 -- no aftertouch at all, so the
`Xpr` source never receives anything -- and that still does not explain the
silences.

What does discriminate is **one macro**. `Hit Grains`'s `Param1` is **`Start`**,
the grain's start position in the sample:

    Hit Grains    silent Start median 81.8 (n=30)    sounding 33.3 (n=115)    AUC 0.904

A threshold at `Start > 65` catches 24 of the 30 silents while losing 7 of the
115 sounding. It generalises: `Cloud Grains` also has a `Start` macro and shows
AUC 0.704, and in a 100-preset generated batch four of its six silents were
sample-playing engines with `Start` between 62 and 92. The other two are not -- `KarplusStrong`'s
`Param1` is `Bow` and `V. Analog`'s is `Detune`, so their high `Param1` means
nothing.

The mechanism this suggests is a **dead region in the grammar**, the same shape
as a highpass at cutoff 78: in a sample-playing engine a high `Start` begins
playback near the end of the sample with little left to sound. That would make
these bad *presets* rather than a bad protocol.

### Settled on hardware, 2026-10-02

16 `Hit Grains` factory presets were written to panels 85-100 under neutral names
`P01`-`P16` -- 8 with `Start` = 100 that render silent against 8 with `Start` = 0
that render fine -- order randomised, the key held back so the arm could not be
inferred from the panel. The owner reported which made sound:

                        silent on hardware   sounds on hardware
      arm A (Start=100)        6 of 8              2 of 8
      arm B (Start=0)          0 of 8              8 of 8

    Fisher exact, two-tailed: p = 0.0070

So the 43 factory silents are a property of the presets rather than of our
render protocol, and the protocol framing in this entry is **withdrawn**.

**The causal reading of `Start` is withdrawn too -- retracted 2026-10-02 by a
within-preset sweep.** This arm test is a *between-preset* comparison: the
`Start` = 100 group is silent more often, which is real at p = 0.0070 and says
nothing about cause. Manipulating `Start` on a single preset says everything,
and it says `Start` does nothing on hardware:

    172-HitGrains (loud on hardware)    Start 0 -> 100:  rms 0.0213 to 0.0230, FLAT
    216-HitGrains (silent on hardware)  Start 0, 50, 100: silent at all three

A loud preset stays loud across the whole range and a silent one stays silent,
so **`Start` is a marker of something else on hardware, not the cause.** What
the `Start` = 100 group has in common that actually silences it is unknown.

This is the project's own rule arriving the hard way: the between-group p-value
was correct and the conclusion drawn from it was not.

**This is also the first time the screen's `silent` verdict has been checked
against the real instrument, and it agrees 14 of 16 (87.5%)** -- 8 of 8 on the
sounding arm, 6 of 8 on the silent arm, on a case chosen to be maximally hard.
That is a validation of the front of the pipeline that nothing before it had.

**No grammar change is being made, deliberately.** It is tempting to cap `Start`
for sample-playing engines, and it would be wrong:

- Such presets are discarded as silent before anyone listens, so the cost is
  render time rather than listening time -- 4 of 100 in a measured batch.
- Hardware says **2 of 8** high-`Start` presets do sound. A cap would discard
  valid presets to save 4% of render time.
- It would be exactly the kind of anticipation the owner ruled out when opening
  the sampler: judge by ear, do not pre-trim the space.

### The emulator and the hardware disagree on 2 of 8 -- measured, 2026-10-02

The synth's output is wired into a USB codec, so the hardware side can now be
captured rather than asked about. `filter_cal/capture.py` drives MIDI while
ffmpeg records. Levels, with the noise floor at 0.000104:

    panel 85   silent in both      0.000115    at the floor      agrees
    panel 86   sounds in both      0.002482    24x floor         agrees
    panel 95   renders silent      0.030649    300x floor        DISAGREES
    panel 99   renders silent      0.029024    284x floor        DISAGREES

**These are not marginal.** Panels 95 and 99 are 12x LOUDER on the instrument
than the control that both agree sounds, among the loudest presets on the
device -- and the emulator renders them as **digital zero**, peak 0.000000, not
a quiet signal under the active floor. A hard failure, not a calibration gap.

### CLOSED, deliberately: the panel 95 edge case (2026-10-02)

**Do not reopen this to chase the remaining question.** The owner's call, and the
right one: it is an edge case affecting a handful of sample-engine presets and
it was pulling effort away from the work that mattered.

What came out of it, all of it kept:

- **The emulator is behaving correctly, not buggily.** `grain_set_start`
  computes `position = sample_count * raw / 32768`, so `Start` = 100 leaves about
  35 samples of a long sample -- too few for a grain -- and the engine emits
  digital zero by design. See FIRMWARE.md.
- **The practical consequence is mild.** Sample-engine presets with a high
  `Start` are genuinely near-silent, so discarding them as silent is *correct*
  rather than a loss. A measured batch lost 4 of 100 that way, and those four were
  asking for a grain at the end of a sample.
- **Four hypotheses were falsified** and are recorded so nobody re-runs them:
  the sample bank (present, complete, correctly hashed), mix-writer selection
  (both presets take the same one), `Start` as a hardware cause (flat
  within-preset), and an inherited sample index (panel 95 is history-independent).
- **The hardware is the unexplained side**, which is the opposite of where this
  started. `Start` = 100 should silence the instrument by its own arithmetic and
  does not. Three untested explanations are listed in FIRMWARE.md if it ever
  matters.
- **Tooling that outlived the question**: `mfrender.py --hook`,
  `MFStores.java`, `capture.mono()`, and a working hardware-capture loop through
  the USB codec.

### It is not a peripheral or flash-access problem -- proved 2026-10-02

The emulator at `Start` = 100 is **reading the sample's zero padding**, and that
is demonstrated rather than argued. `mkflash.py` rounds each sample record's
allocated size up to a 4096-byte page and fills the remainder with `0x00`, which
reads as digital silence. Replacing that padding with a loud 1 kHz square in the
flash image, changing nothing else:

    zero padding     rms 0.000000   peak 0.000000   dominant      0 Hz
    1 kHz padding    rms 0.005158   peak 0.023376   dominant    834 Hz

Where there was exact digital zero there is now an audible tone from the region
we filled. **So the flash path works perfectly** -- Renode's QUADSPI model
returns precisely what we put in the image -- and the question was never how we
handle the peripheral. It is where the grain is pointed.

**Setting the length honestly does not fix it.** Rewriting every record's
`+0x04` from the page-rounded allocation to the exact header-plus-audio length,
positions unchanged, leaves `Start` = 100 at rms 0.000000. On reflection that is
expected: `Start` = 100 puts the grain at the *end* of the audio either way, so
there is nothing after it to play. The cached length is a real fidelity detail
but it is not the lever.

So the divergence is in **how the grain start position is derived and bounded**.
The instrument plays `Start` = 100 at full level -- flat 0.0213 to 0.0230 across
the whole range -- so it does not place the grain past the audio. Whether it
wraps, clamps, or scales `Start` differently is **not yet known**, and
`engine+0x128`, computed as `(rec[0x04] >> 1) * (engine[300] >> 4) >> 11`, is
the obvious place to look next.

**One measurement here is not usable and should not be quoted.** An attempt to
ask whether `Start` changes the hardware's *content* gave similarities of 0.856
to 1.000 against `Start` = 0 -- but the only available reproducibility reference
scored **+0.480**, because the two captures used different stimuli (the full
phrase against a single note). With the control worse than the effect, that
comparison says nothing. It needs two captures at one setting with one stimulus.

### The trailing 24 preset bytes are inert -- closed

`build_page()` truncates the decoded block from 4088 bytes to 4064, which looked
like a possible loss. It is not. The page layout is region 0 at 32 bytes, region
1 at 1980 and region 2 at 2084, totalling exactly 4096, so the payload fills
4064 bytes of page and the rest is not page content. Checked across all 448
factory presets: bytes 4064-4088 are **`00` x 24 in every one**. The truncation
is correct.

### The sample bank is NOT the cause -- checked 2026-10-02

The obvious suspect was that our flash image lacks the samples the device has,
especially since `mkflash.py` documents a failure mode where a zero content hash
at record `+0x08` makes the granular engines render silence "while running
perfectly happily". Inspected directly, the image we render with holds:

    52 sample records chained from 0x281000
    zero hashes:        0 of 52
    duplicate hashes:   0
    `Piano Pop` = 0x66a2, exactly the value mkflash records MCC sending

So the bank is present, complete and correctly hashed, and the documented
zero-hash silence mechanism is **not** in play. "We do not have the factory
samples" is ruled out.

The emulator is also demonstrably reading real sample content: fingerprinted
against all 52 bank samples, its output at `Start` = 50 matches `Braam`
(slot 10) at **+0.860**. It is playing a sample, not noise.

**What does not reconcile is the two sides' audio.** The hardware capture at the
same `Start` matches *no* bank sample (best **-0.094** across all 52) and is
anti-correlated with the emulator's own output at every `Start` tested, from
-0.06 to -0.67, with the analog filter applied to the emulator side to make the
comparison fair (it moved the numbers by less than 0.015). Even at `Start` = 50,
where both are comfortably audible -- emulator rms 0.0194, hardware 0.0248 --
they do not look like the same sound.

**Treat that last paragraph as suggestive, not settled.** The fingerprint is 28
mean-centred log bands over a hand-aligned window across two different sample
rates, which is fine for "same or not" at a coarse level and not evidence about
mechanism. `172`'s filter is also an extreme setting -- HPF at cutoff 0.0 with
resonance 100.0 -- so tilt differences between a modelled filter and the real
analog one are expected.

What it does say is that the remaining divergence is in what the two sides *do*
with a sample both of them have, not in whether they have it.

### SOLVED on the emulator side: `Start` >= 90 renders digital zero

The same sweep, rendered rather than captured, isolates the emulator bug exactly.
One preset, one parameter, everything else held:

    Start      emulator rms     hardware rms     ratio
      0          0.007359          0.02242         3.0
     50          0.017916          0.02129         1.2
     70          0.002703          0.02296         8.5
     80          0.000448          0.02256        50.4
     85          0.000006          0.02244      4032.1
     90          0.000000          0.02275         inf
     95          0.000000          0.02283         inf
    100          0.000000          0.02218         inf

**The emulator decays monotonically to digital zero as `Start` rises past ~50
and reaches exact zero at 90; the hardware is flat.** That is the whole of the
panel 95 divergence, bounded to one parameter and one threshold.

The shape is the signature of a sample read running off the end of the buffer
and returning zeros, while the instrument wraps or clamps the start offset into
a valid range -- hardware being *flat* rather than merely louder is what says
it wraps. So the next place to look is the grain start offset in the sample
read, not the mix: either the emulator's sample data is shorter than the
device's, or the offset is not being wrapped modulo the sample length.

**This is NOT the live question in FIRMWARE.md**: the two mix
writers, `0x08030AA2` ("the arm silent presets take") against `0x0803261C`
("the arm audible presets take"), found with silent Speech presets and never
explained. These two presets are a far better handle on it than Speech was,
because hardware ground truth says they are loud, the emulator says exactly
zero, and there are six matched `Start` = 100 presets that are silent on both
sides to compare against. Next step is to see which mix writer each takes --
with the standing caution about not hooking the main loop for a full run.

No mechanism found in the preset data itself at n=2: `Density` is marginally
lower in both (48.7 against 50-66 for the silent six) but `170-HitGrains-v30` at
50.6 is silent, so that is not a rule, and the envelopes and cycling-envelope
settings do not separate them either.

### Averaging a stereo hardware capture reads as silence on this rig

The synth reaches the codec **anti-phase**: `corr(L, R)` is -0.999 to -1.000
whenever there is signal, the signature of a mono TS jack into a differential or
stereo input, tip to L and ring to -R. So `x.mean(axis=1)` cancels it. A capture
whose channels each read rms 0.0029 came out at 0.000074 that way -- a factor of
39, indistinguishable from the 0.000104 noise floor -- and three presets were
nearly written off as silent on the hardware because of it. The positive control
is what caught it: a preset known to sound read at the floor, which no amount of
staring at the quiet ones would have revealed.

`microfreak.audio.mono()` takes the difference when the channels are
anti-phase and the mean when they are not, so it works either way without
being told the wiring. `filter_cal/capture.py` and `mfreak render` use it.

**A raw capture has no filter in it.** The analog filter's control line is
outside the emulated domain, so the firmware's audio comes out of SAI
unfiltered and the filter is modelled afterwards in `filter_model.py`.
`filter_cutoff`, `filter_resonance` and `filter_type` therefore have no effect
whatever on a raw capture.

This is easy to walk into. A probe on 2026-10-01 compared two presets
differing only in `env->cutoff` depth, 20 against 100, and measured a spectral
distance of 0.0067 against a batch-position noise floor of 0.0042 -- i.e.
nothing. The conclusion "modulating cutoff does nothing" would have been
entirely an artifact of judging an unfiltered render.

**Since 2026-10-15 `mfemu render` applies the filter itself**, at the preset's
own resonance and -- since 2026-10-20 -- at the firmware's own cutoff CV,
because that is how the instrument works; `--no-filter`
opts out. So a `.wav` from `mfemu render` is filtered unless you asked for it
not to be. The trap now belongs to anything that reads the SAI capture directly
-- `sai2 SavePCM`, a `--keep-raw` file -- and to any measurement built on those
rather than on the rendered audio.

### A cache key doing its job looks like a cache key that is broken (2026-10-17)

Three warm snapshots appeared for what looked like identical inputs --
same firmware, same flash image, same command line -- and the first two of
three consecutive `mfemu render` runs each paid a full 67 s boot instead of a
16 s restore. That reads as an unstable cache key, which would be a real bug
worth reporting.

It was not. `warm.key()` hashes **the bytes of the eight model sources**, and
comment-only edits were being made to four of them (`STM32_QUADSPI.cs`,
`STM32F4_Flash.cs`, `boot.resc`, `microfreak.repl`) between the runs. The key
was correct; the inputs really had changed. Checking what had actually been
edited, rather than reporting the symptom, is the whole difference.

*Rule: before reporting non-determinism, establish that the inputs were
identical -- including the ones you changed yourself.*

Practical consequence worth knowing: **editing a comment in any file listed in
`warm.SOURCES` invalidates every existing snapshot** and costs one rebuild per
flash image. That is the correct trade (a stale snapshot of a changed model is
worse), but it means a docs pass over `renode/` has a measurable price.

## Reasoning that lies to you

**Do not build on a label you have not verified.** A preset was used as a
"working Speech reference" for three conclusions; its engine came from this
repo's own interpolated table and was never confirmed. All three had to be
withdrawn. The owner's correction -- "Raverz is V. Analog" -- exposed a real
decoder bug affecting 337 of 398 presets.

**Ask the owner about hardware behaviour. Do not infer it.** Twice in one
session an assumption about the device was asserted, propagated into a
conclusion, and then falsified by one sentence from someone with the hardware
in front of them:

- "Raverz must be Speech" -- it is V. Analog.
- "on hardware the first key press after power-on speaks" -- it does not; the
  hardware is silent on the first press after a cold boot, exactly like the
  emulator. What had been written up as the last remaining defect was correct
  behaviour, faithfully reproduced.

*Rule: a claim about what the device does belongs in the "ask" column. It
costs the owner one line and can save a day.*

**A correlation across one capture is not a decoder.** Grouping presets by a
TLV tag byte produced a perfect 1:1 table against another field, and the
"engine" it implied was wrong -- the byte was a type tag, and the real engine
lives elsewhere as a 15-bit value (see FORMATS.md).

**A decoder that only agrees with itself has not been tested.** The engine
decoder was rewritten twice on internal evidence -- first a tag-byte
correlation, then a count-scaled table that "explained" 337 mis-decodes --
and both were wrong. What settled it took one message: five presets written
to the device, the engine read off the **panel**, corrected by the owner and
read back. Nine (value -> engine) pairs from the panel pinned the formula
uniquely; no other floor/round/ceil form over divisors 18..25 fits them.

*Rule: a field whose meaning is asserted by this repo's own tables is
unverified no matter how many presets it "explains". Get ground truth from
the panel, then fit.*

**Confirmation count is not coverage.** The corrected engine formula was
written into the docs on the strength of *nine* hardware-confirmed points --
all of which sat at index 16 or below, where the wrong formula and the right
one agree exactly. The first write that could distinguish them (index 17)
came back as the wrong engine. Nine points proved nothing about the range
they did not span.

*Rule: before trusting a fitted model, ask which observations would have to
differ if it were wrong -- and go get one of those. Probe the ends of a range,
not the middle, and probe the case the model was not fitted on.*

**Work backwards from the sink.** Guessing forwards from the engine burned
hours. Reading the DMA buffer address out of the peripheral registers,
watchpointing it and reading the writing PC found the true audio path in one
run. Do this first for any "no output" problem.

**Test the tool before declaring a gap in it.** "`mfrender.py` needs priming"
was inferred from hand-written scripts that behaved differently. Stock
`mfrender.py` already rendered the preset correctly; the priming was
implemented, measured, found to be byte-identical at one setting and to break
the render at another, and reverted.

## The shape of a good investigation here

1. **Reproduce deterministically** -- one-shot script, fixed timings.
2. **Bracket the failure** -- find one place the signal is good and one where
   it is not, then bisect between them. "Alive at the formant caller, dead at
   the mix writer" was worth more than any hypothesis.
3. **Work backwards from the sink**, not forwards from the suspect.
4. **Watchpoint the data**, and read `PC`/`LR` from the hook.
5. **Prove the negative** -- when something "never happens", show the probe
   would have fired if it had.
6. **Force the value** -- if a field is suspected, force it at its use site and
   listen. A proven positive test turns argument into measurement.
7. **Ask the owner** anything about the hardware.
8. **Write down the retraction**, not just the finding. Several conclusions in
   `FIRMWARE.md` and `../renode/NOTES.md` are recorded as withdrawn, with
   what was measured and why it was wrong. That is what stopped the same wrong
   turns being taken twice.

## What this bought, in the end

The Speech engine renders `Snit` as an understandable "eight", confirmed by
the owner against hardware, from nothing but the firmware image and a preset
read over SysEx -- and the emulator reproduces the hardware's own quirk of
staying silent on the first key press after a cold boot. The engine was never
broken. The test procedure was.

## Render throughput on the Mac Studio (2026-09-24)

Measured with `.raw` and wall timings, never from stdout -- Renode's output is
block buffered. Boot and per-render cost were separated by running the same
job with 4 and then 8 presets and taking the marginal difference.

    boot          28 s   (old machine ~85 s)
    per render  12.8 s   (old machine ~52 s)

Concurrency, 8 presets per job, 18 cores (6 performance + 12 efficiency), 64 GB:

    jobs   wall  renders  renders/s  vs solo  s/render  result
       1   130s        8      0.062    1.00x      16.2  complete
       4   162s       32      0.198    3.21x       5.1  complete
       8   193s       64      0.332    5.39x       3.0  complete
      10   295s       79      0.268    4.35x       3.7  1 render LOST
      12   489s       95      0.194    3.16x       5.1  1 render LOST

**Eight jobs is the setting.** Throughput peaks there and then falls: twelve
jobs is slower in absolute terms than four.

**Past eight, renders are dropped, not just delayed.** At ten and twelve a
render failed with *"the firmware never loaded slot N (saw nothing)"* -- the
emulated firmware misses its program change under contention. `mfrender`
catches this and refuses to write the file, so the cost is a lost render
rather than a mislabelled one, but it means over-parallelising is a
correctness problem and not only a performance one.

**Memory is no longer the constraint.** It sat at 92% free through every run,
including twelve jobs. The old machine's "budget ~1.2 GB free per instance,
thrashed at four" does not apply here; the limit is now CPU contention, and
eight jobs at roughly two threads each is about 16 of 18 cores.

End to end this is **6.2x faster**: 18.6 s per render effective on the old
machine at its best setting, against 3.0 s here. A 365-render job took 2 h
20 min there and about 18 minutes here.

The `boot 28 s` above is pre-warm-snapshot and no longer what a render pays;
see "Warm snapshots" in EMULATION.md, which takes the fixed cost to about a
second. The per-render figure still holds, and so does the eight-job ceiling
-- that is CPU contention, which no snapshot changes.
