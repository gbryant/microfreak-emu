# Filter calibration rig

Measures the analog filter the emulator cannot have, by comparing the
emulator's pre-filter render of a preset against a recording of the same
preset played on the device.

**Render with `--no-filter`.** Since 2026-10-15 `mfemu render` applies this
model by default, and feeding its output back in here would fit the filter
against audio that has already been through it. Everything below assumes the
pre-filter signal, which is what that flag gives.

**Nobody needs this to use the emulator.** The curves it produced are already
in `src/mfemu/filter_model.py` and `tests/test_filter_model.py` pins them. This
directory is here so those numbers can be re-derived, disputed or extended --
and because the traps below are about the instrument, not about the rig.

Running it needs the hardware, an audio interface, and the optional MIDI
dependency: `uv sync --extra hardware`. The recordings and the fitted sweep
(`src36/*.wav`, `cutoff_sweep.pkl`) are **not** in this repository; they are
gigabytes of audio of one person's instrument.

    capture.py   record from the USB codec while driving the device over MIDI
    point.py     one filter setting, one self-contained recording
    als.py       fit cutoff per setting against the emulator source

A second, hands-free path added 2026-10-17, driven by preset writes rather
than CC. It needs nothing set on the instrument and nobody at the panel:

    sweep_presets.py   write a preset per cutoff value, record each
    als_presets.py     the same ALS fit, keyed on the knob value 0-100

    uv run --extra hardware python sweep_presets.py            # the sweep
    uv run --extra hardware python sweep_presets.py 60 60 60   # repeatability
    uv run python sweep_presets.py --from-wavs                 # re-analyse only
    uv run python als_presets.py EMU_NOFILTER.wav              # fit

Writing a preset stores an absolute value, so the knob-catch mode below cannot
affect it. `--from-wavs` re-derives the pickle from recordings already on
disk, so changing the analysis never needs the instrument again.

Method: `hw(f) = a(f) + emu(f) + lp_db(f; fc, Q)` in dB, where `a(f)` is
everything fixed in the analog path and is shared by every setting, so it is
solved for rather than assumed. Alternating least squares. Only magnitudes are
used, so no time alignment is needed.

## Three traps, all of which produced convincing wrong answers first

1. **The codec presents the synth differentially.** The two channels are
   anti-phase (r = -0.998). Averaging them cancels the signal to -58 dBFS;
   their difference recovers it at -28.5. Use `point.mono()`.
2. **Measure the pitch, never assume it.** The device is 0.3 cents off
   nominal and the emulator 1.2 cents. By the 96th harmonic that is several
   FFT bins, and a fixed-frequency bin grid walks off the harmonics and
   reports a band limit that is not in the signal. This happened twice.
3. **Repeat a point before believing the curve.** The first full sweep fitted
   beautifully -- residual 1.55 dB^2, a clean exponential, damping linear in
   the knob -- and then the same CC values re-measured 20 minutes later came
   back 0.65-0.86x. The fit was not the problem; the reference moved.

## Four more traps, from the preset-driven sweep (2026-10-17)

5. **A designed preset is not a stationary source.** `16 V.Analog` has four
   mod-matrix cells routed to cutoff and a mid-note amplitude dip; the SAME
   setting re-measured gave rms 0.0085, 0.0238 and 0.0649 -- a **7.6x
   spread**. Built from `tests/fixtures/init-patch.mfpz` instead, which has no
   mod-matrix cell routed anywhere, the same setting repeats to **0.4%**.
   Establish repeatability before believing any curve; trap 3 above says the
   same thing and it still caught us.

6. **A program change to the already-selected slot does not reload the
   voice.** `preset select` documents the short-circuit. Overwriting the
   scratch slot and re-sending its own program change measures the FIRST
   preset every time, silently. Select another slot first.

7. **The first note after a reload can be wrong.** `speech/cli.py` says this
   about a power cycle; it is true of a preset reload too. Measuring the
   first note produced a non-monotonic amplitude curve with knob 30 louder
   than knob 100. Play a priming note and measure the LAST burst.

8. **Do not refine f0 by maximising a harmonic sum.** This was tried as an
   improvement on autocorrelation and it is biased: it moved knob 100 from
   65.536 Hz to 66.811 Hz, +33 cents. Locating each harmonic's own peak and
   dividing by its index showed 65.536 agreeing to 0.49% while 66.811
   scattered 2.20%. The comb has spurious alignments and the sum finds them.
   Autocorrelation, seeded once from the widest setting and rejected when it
   disagrees by more than 2%, is what works -- it is immune to the one real
   failure (at low cutoff it can lock to the wrong period outright: knob 20
   reported 49.841 Hz for a note whose f0 is 65.4).

## What the source engine has to be

The filter can only be measured where the source has energy, and `als.py`
tracks harmonics, so the source needs a dense harmonic series that survives
the filter. Measured at knob 100, harmonics passing 10x the LOCAL noise
between harmonics:

| engine | f0 read | harmonics usable |
|---|---|---|
| **SawX** | 65.42 | **20** |
| Superwave | 65.37 | 16 |
| Basic Waves | 65.36 | 13 |
| Harmonic | 65.56 | 1 |
| V. Analog | 42.61 | 0 |

`Harmonic` has the cleanest pitch (implied-f0 scatter 0.43% against SawX's
0.8%) and is useless here: its spectrum is sparse, so the bins between its
partials are not noise and the test rejects almost everything. `Basic Waves`
-- the Init patch's own engine -- puts 90% of its energy below 335 Hz and can
say nothing about a corner above about 500 Hz. `Noise` is stationary and
tempting, but it is not white (**-3.2 dB/octave** measured) and has no
harmonics to track at all. Use SawX.

## What the preset-driven fit established, and what it did not (2026-10-17)

A 17-point sweep, SawX source, fitted against `mfemu render --no-filter` of
the same preset. **No constant in `filter_model.py` was changed on the
strength of it**, for the reasons below.

**The model's slope is corroborated.** Fitted 0.1160 log2-per-knob against the
model's 0.1037, a ratio of **1.118** -- so octaves-per-knob is right to within
about 15%. Widening the Q grid down to 0.15 and the fc grid up to 80 kHz moved
it only to 1.141, so this is stable.

**The absolute fc is not identified by this data.** That same widening moved
the fitted fc at knob 60 from 1.93x the model to 3.63x, and the top two
settings pinned to whatever the grid ceiling was. A parameter that slides with
the search bounds has not been measured. The cause is visible: the harmonics
reach about 9 kHz (140 x 65 Hz) while the fitted corner sits above that, so
the data sees only the filter's gentle onset and never its corner. Q landing
on the lowest value offered is the same symptom.

**Below knob 60 nothing could be fitted** -- fewer than five harmonics survive
10x the local noise. The harmonics that carry the information are the ones the
filter is removing, so closing the filter destroys the measurement. That is
the method's floor.

An earlier pass using spectral rolloff rather than the ALS fit suggested the
slope was 0.70x the model's. That was an artifact of the source: the Noise
engine falls at -3.2 dB/octave, which compresses a rolloff statistic relative
to the true corner. Cancelling the source through the ALS fit removed it, and
the two results are not in conflict -- the first one was measuring the source.

To finish this properly the usable band has to reach past the corner: a louder
source, a higher note (more Hz per harmonic), or a longer analysis window.
**The region that matters most is still unmeasured** -- 38 of the 64 presets in
the factory bank sit below knob 44, which is below where this fit can see.

## The higher note: slope confirmed, offset still not (2026-10-17)

The C2 sweep's harmonics stopped at 9.2 kHz -- `als.py`'s NMAX=140 times
65.4 Hz -- which is *below* where the fit was putting the corner, so the fit
never saw the corner at all. Re-run at C4 (note 60, 261.6 Hz), where the same
harmonic count reaches past 15 kHz:

| | C2 (note 36) | C4 (note 60) |
|---|---|---|
| rms dynamic range over the sweep | 10x | **130x** |
| fitted log2 slope / model | 1.118 | **0.999** |
| mean sq residual | 3.22 dB^2 | 12.27 dB^2 |
| points fitted, of 17 | 8 | 8 |

**The model's exponential coefficient is now corroborated twice, the second
time almost exactly** -- 0.103547 fitted against 0.103701 in
`filter_model.py`. Two independent notes, the source cancelled both times.
The 130x rms range is the reason to trust C4 more: at C2 the filter had
stopped doing anything measurable by knob 50.

**The absolute cutoff is still not identified.** Fitted/model stays between
1.6 and 3.7, the top settings still pin to the fc grid ceiling, and the shared
Q still lands on the lowest value offered (0.4) -- below `filter_model`'s own
`Q_MIN` of 0.5 and well below the 0.616 its `resonance_q(0)` would use for the
resonance-0 presets these recordings were made with. A boundary solution means
the data does not constrain that parameter.

**Below knob 30 nothing fits, and the higher note made that worse** -- knob 15
to 25 have two or three usable harmonics where C2 had slightly more. Fewer,
wider-spaced harmonics is the wrong trade when the filter has already removed
everything above its corner. If the low end is the goal, the lever is
signal-to-noise -- a louder source, a longer window -- not pitch.

### A lead that does NOT overturn the topology

A Q wanted below the model's floor, together with a cutoff pushed high, is the
signature of fitting a 2-pole to something steeper. Tested directly, refitting
the same data as two cascaded sections:

| | residual | slope / model | fc / model |
|---|---|---|---|
| 12 dB/oct (1 section) | 12.27 dB^2 | 0.999 | 1.6-2.8 |
| 24 dB/oct (2 sections) | **5.74 dB^2** | 0.738 | 2.1-3.7 |

Halving the residual looks like evidence and is not conclusive: the steeper
fit makes BOTH the slope and the offset worse, and Q pins to the grid floor
either way, so neither fit is well posed. `filter_model.py`'s header records
the 12 dB/octave SEM topology as **owner-supplied**, and that is also what the
instrument is publicly documented to have. Nothing here is strong enough to
contradict it -- recorded so the next person does not have to re-run it, and
so that it is not mistaken for a finding. `ALS_SECTIONS=2` reproduces the
right-hand column.

## The low band cannot be measured on this rig -- CLOSED (2026-10-17)

The obvious way to measure a corner at 50-150 Hz is a low note, so the
harmonics bracket it from below. It does not work, and the reason is the
capture path rather than the choice of note, so there is no point trying
another one.

At note 24 (C1, nominal 32.70 Hz) with the filter **wide open**, energy by
band, where a sawtooth's fundamental should dominate everything:

| band | energy, dB relative to 20-40 Hz |
|---|---|
| 20-40 Hz (the fundamental) | 0.0 |
| **40-60 Hz** | **+14.6** |
| 60-90 Hz | +9.7 |
| 130-200 Hz | +8.1 |

The fundamental is the WEAKEST band measured. Something between the
oscillator and the WAV file is attenuating it by at least 15 dB, and the
symptom shows up first as pitch: note 36 measures 65.42 Hz (correct) while
note 24 measures 46.9 to 50.2 Hz instead of 32.70 -- on `SawX` and on
`Basic Waves` alike, so it is not an engine. With the fundamental suppressed,
autocorrelation locks to an upper partial.

Consequence: a filter corner below about 50 Hz has no harmonic structure
beneath it to measure, so knob settings below roughly 30 -- which the model
puts at 148 Hz and below -- are **out of reach of this rig**, whatever note is
played. That is 41% of the factory bank's presets, and it is a limit of the
measurement chain, not of the method.

What would lift it: a DC-coupled or instrument-level input instead of the USB
codec's line input, measured against a known low-frequency reference so the
rolloff can be characterised rather than guessed. Until then, the low band
rests on the model's exponential holding -- which the slope results above do
support, twice, but which has not been checked where it matters most.

## The cutoff LOCATED, not fitted -- resonance peak (2026-10-17)

`find_peak.py`. Turning the resonance up puts a magnitude peak at the cutoff,
so the cutoff can be read off the spectrum instead of fitted. Each point is a
ratio of two recordings at the same cutoff, resonance 90 over resonance 0, so
the source, the output stage, the codec and the lowpass's own baseline all
cancel. Nothing is assumed about order, Q or the analog path -- the three
things the ALS fit had to solve simultaneously and could not separate.

Noise source, +21 dB peak at every point, measured with 400 log bands
(+-0.7% quantisation):

| knob | peak Hz | model fc | peak/model |
|---|---|---|---|
| 30 | 144 | 148 | 1.00 |
| 40 | 309 | 303 | 1.08 |
| 50 | 661 | 622 | 1.07 |
| 60 | 1413 | 1277 | 1.08 |
| 70 | 3024 | 2620 | 1.11 |
| 80 | 6469 | 5375 | 1.15 |

**The model's absolute mapping is confirmed.** Mean peak/model 1.083 -- the
model reads 8.3% low, which is **1.38 semitones against its own documented
`ACCURACY_SEMITONES` of 1.43**. It is performing inside the accuracy it
claims. The slope is right to 3% (0.10698 measured against 0.103701), and both
figures are stable from 60 bands to 400, so neither is a quantisation
artifact.

**This also settles the ALS fit's disagreement.** That fit put fc at 1.9x to
3.6x the model and its answer moved with the search grid; this one lands at
1.08x and does not move with the analysis resolution. The fit was
under-determined and its absolute number should be disregarded -- the slope
it agreed on (0.999 at C4) is the part worth keeping. Do not revive the
24 dB/oct lead on the strength of the fit either; nothing here supports it.

The one correction not applied: a resonant second-order lowpass peaks slightly
BELOW fc, by 0.97% at the Q the model uses for resonance 90 (5.09). Applying
it would make the measured cutoffs 1% higher and the bias slightly larger. It
is an order of magnitude below the bias being measured and needs the Q this
method exists to avoid assuming, so it is noted rather than applied.

Optional refinement, NOT applied: adding log2(1.083) = +0.1150 to
`CUTOFF_LOG2_AT_ZERO`, taking it from 4.0961 to 4.2111, would null the 8.3%
and bring the residual spread to about 0.3 semitones. That changes every
render and the constants `tests/test_filter_model.py` pins, so it is a
decision rather than a measurement.

## SOLVED: it was the Utility knob-catch mode

**Set knob catch to `Jump` before measuring anything.** The default here was
`Scaled`, in which an incoming CC is not an absolute value but a relative
nudge whose effect depends on the accumulated state. Measured: one target CC
approached four times gave 1671, 1327, 2046 and 1719 Hz, drifting upward.
That is why a whole sweep could fit beautifully -- it was one monotonic pass,
so the ordering held -- and then reproduce at 0.65-0.86x twenty minutes later,
and why three presets written with known stored cutoffs all played at whatever
the last CC had left.

In `Jump` the same setting reproduces to 3.3%, opposite approach directions
agree within fit noise, and a preset's stored cutoff takes effect on load.

This is worth knowing beyond calibration: **any CC-driven search loop on this
device is meaningless unless the knob-catch mode is Jump.**

## CC scale is not quite the stored-field scale

At the same nominal percentage, a CC-driven cutoff sits about 2.6 semitones
above one set through the preset's `filter_cutoff` field -- consistently, on
all five points whose corner is inside the measurement band. So the final
calibration takes its SLOPE from the CC sweep, which is precise (10 points,
0.63 semitone residual), and its ANCHOR from the stored-field points, which
are the route a generated preset actually takes. Accuracy 1.43 semitones RMS.

Resonance was measured through CC only and may carry the same offset.

## Mode coverage: cutoff transfers, Q does not

Easy to assume, wrong to. Tested by writing presets, since filter type has no
CC assignment.

**Cutoff, verified in all three modes.** BPF and HPF at knob 60/70/80, each
fitted with its own tap's magnitude response, follow the same `fc(knob)` curve
to **1.20 semitones RMS** -- tighter than the LPF calibration's own 1.43,
because those taps pass the high harmonics and the fits use 76-118 of them
against the lowpass's 9-32. This is what the SVF topology predicts: one pair
of integrators, three outputs.

**Resonance, measured per mode.** Eight stored-field points each in LPF and
BPF, same cutoff, fitted with each tap's own response. Damping is linear in
the knob in both:

    LPF   R = 1/Q = -0.015845 * knob + 1.6227    5.8% RMS, R zero at knob 102.4
    BPF   R = 1/Q = -0.014195 * knob + 1.4334    7.2% RMS, R zero at knob 101.0

Both reach zero damping just past the knob's own maximum, which is the shape
of a filter that gets very resonant without tipping over -- and it does not
tip over, measured directly.

    knob    Q(LPF)   Q(BPF)   difference
       0      0.63     0.66        +6%
      40      1.00     1.24       +24%
      70      1.83     2.22       +22%
      90      5.34     5.66        +6%

So BPF runs about 20% hotter than LPF through the middle and converges at both
ends. HPF was not measured and borrows the LPF curve.

**Measure Q at the bandpass tap.** Those fits used 107-139 harmonics against
the lowpass's 39-84, because the BP output passes everything the LP output
throws away. A broad Q ~ 1 lowpass resonance is nearly shapeless and fits
badly; the same setting at the BP tap is a clear peak.

### Retracted: "Q is out by 13-41% between modes"

An earlier BPF spot-check at three points gave 1.71 / 2.28 / 13.79 against the
curve's 1.01 / 1.98 / 9.90, and this file said to treat Q as +-40% and
mode-untransferable. That was mostly an under-conditioned fit over a narrow
cutoff grid. Re-measured properly the same BPF settings give 1.24 / 2.22 /
~23, the LPF curve reproduces the original CC-derived one to about 10%, and
the real mode difference is the ~20% above. The lesson is not new: three
points fitted loosely will happily disagree with a curve that is right.

## HPF resonance: checked, not pinned

Eight stored-field points in HPF at the same cutoff give Q within about 19%
RMS of the LPF curve over knob 0-90, and the top point (97) would not fit at
all. That is inside the measurement's own noise rather than a confirmed match:
the highpass tap throws away the low harmonics where a sawtooth keeps its
energy, so those fits had 38-77 harmonics against the bandpass's 107-139.

HPF borrows the LPF curve, now as a checked approximation. To pin it properly
would need a brighter source than a 65 Hz saw -- something with real energy
above the corner.

## Linearity: no measurable saturation

`drive` stays 0.0 in the model, and that is now a measurement.

Finding a lever took two attempts. `Gen/PrstVol` changes the hardware level by
8.5 dB but is **identical in the emulator's render to within 0.006 dB**, so it
is applied in the analog domain, after the DAC, and cannot drive the filter
harder relative to its input. MIDI velocity does nothing on this preset
(+-0.1 dB). The envelope's **sustain level** is the right lever: it is in the
firmware, and the hardware level tracked the stored value to within 0.4 dB
over a 14 dB range, which is what a clean digital gain looks like.

Driving the filter over that range at BPF resonance 90 -- where the internal
level peaks -- gives:

    sustain  20%   +0.0 dB    Q 5.69    fc 2893 Hz    119 harmonics
    sustain  45%   +7.0 dB    Q 5.87    fc 2874 Hz     84
    sustain 100%  +14.0 dB    Q 5.78    fc 2893 Hz    132

A 3% spread with no trend. Saturation would show as Q falling at the top. Over
the range this instrument produces at these settings, the filter is linear, so
an integrator tanh would be decoration. If one is ever added, these are the
numbers it has to beat.

## The modulated cutoff (2026-10-20)

Everything above calibrates the filter at a FIXED cutoff. A preset that routes
anything to cutoff -- an envelope, the cycling envelope, an LFO, key tracking
-- was rendered at its stored knob value for the whole note, and that was the
largest error left in a render: on Sausage Bass (factory 367) the instrument's
filter sits about two octaves above its knob, and the render sounded muddy
next to the hardware because it was.

The fix does not model the modulation. It reads it: the firmware computes the
cutoff it wants and writes it as a 16-bit PWM on TIM3 (CCR4 high byte, CCR3
low byte; 65535 = knob 100), and `mfemu render` now captures every write and
drives the model with it per sample (`mfrender.cutoff_cv`,
`filter_model.cv_to_fc`). `mfemu render --save-cv` writes the trace beside the
`.wav`, which is what a comparison against a recording needs. See
`docs/FIRMWARE.md`, "The analog outputs", for how the channel was found.

The one new constant is the RC that smooths that PWM on the board,
`CV_SMOOTH_TAU`. Method, all on the instrument through the USB codec, each
preset written to slot 367, recorded through CoreAudio (ffmpeg's avfoundation
input dropped 1.6 s out of a 13 s take -- see trap 9) and differenced to mono:

| preset | what it tests | before (static knob) | with the CV |
|---|---|---|---|
| Sausage Bass, cycling env routing removed | the static model at knob 45.4 | 5.1 dB band spread | 5.1 dB (the CV is one constant value: identical) |
| Sausage Bass | a ~1 kHz square CV, knob 45.4 / 83.5 | 36.3 dB band spread | 4.5 dB at tau >= 1 ms; 6.8 dB at tau 0 |
| Buzzsaw (319) | an envelope sweep, knob 35.9 -> 88.7, resonance 34.9 | 9.7 dB mean residual | 2.4 dB, any tau 0.3-30 ms |
| Buzzsaw switched to BPF | the same sweep through the bandpass tap | 10.2 dB | 1.1 dB at tau 2 ms |
| Buzzsaw switched to HPF | the same sweep through the highpass tap | 10.3 dB | 1.1 dB at tau 2 ms |
| Init + SawX, LFO 32.12 Hz to cutoff +-25 | how much of a 32 Hz swing survives the RC | -- | depth ratio 0.97 at 0.3 ms, 0.94 at 3 ms, 0.84 at 8 ms, 0.61 at 20 ms |

"Band spread" is max minus min of the median per-harmonic level ratio
(instrument over render) in octave bands from 100 Hz to 12.8 kHz, notes C2, C3
and C4, one gain removed. The first row is that method's floor. So tau is
between ~1 ms (below that the raw square's sidebands appear) and ~3 ms (above
that a 32 Hz LFO loses depth the instrument keeps). It is set to 2 ms.

On Ode to Joy through Sausage Bass, the long-term spectrum of the render
against the instrument, 250 Hz-16 kHz: 14.3 dB mean difference with the
static model, 1.6 dB with the CV.

9. **Do not record through ffmpeg's avfoundation input.** Asked for 18 s it
   delivered 15.7, with nine splices where audio was dropped -- 1.6 s missing
   out of the middle of one take, heard as glitches. CoreAudio through
   `sounddevice` delivered 19.1 s with no overflow on the same interface.
   Opening by index is not safe either: in one probe on this machine `:0`
   and `:1` both opened the webcam microphone (16 kHz mono), though an
   earlier take through `:1` had the codec's anti-phase channels. Open the
   codec by name, `:USB Audio CODEC`, if ffmpeg is used at all.
   `capture.py` records through `microfreak.audio` (sounddevice), opens the
   codec by name, and warns when a take drops audio. The 2026-09 and
   2026-10-17 takes were recorded through avfoundation and may carry the same
   dropouts. Their analysis
   used averaged harmonic magnitudes, which a splice disturbs far less than it
   disturbs a listener, but it has not been checked; the recordings are not in
   this repository to check against.
