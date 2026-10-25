# Related projects

Other people's public MicroFreak work, and how it relates to this one.

**This project's findings were derived independently.** Everything in
`FIRMWARE.md`, `FORMATS.md` and `PROTOCOL.md` was read out of
the firmware, off the wire or off the instrument here, and the projects below
were found in October 2026 — after the work they overlap. Some of them predate
this project; none of them informed it.

That is worth stating precisely, because it changes what the overlaps mean.
Where an independent effort reached the same answer, that is **corroboration**:
two people arriving at `0x08054A68` separately is better evidence than either
on its own. Where one of them has something this project does not, it is listed
below as a lead and marked unverified, because that is exactly what it is until
someone checks it.

Credit where it is due, and if you maintain one of these and something here is
wrong or out of date, please open an issue.

## Existing reverse-engineering projects

- **[francoisgeorgy/microfreak-reverse](https://github.com/francoisgeorgy/microfreak-reverse)**
  — sysex format notes.
- **[francoisgeorgy/microfreak-reader](https://github.com/francoisgeorgy/microfreak-reader)**
  — a working preset reader; forks
  ([astrodidge](https://github.com/astrodidge/community-microfreak-reader),
  [colinmreed](https://github.com/colinmreed/community-microfreak-reader))
  updated for the 512 slots of firmware v5. Hosted build at
  [studiocode.dev/microfreak](https://studiocode.dev/microfreak/).

### Where we independently agree

Same frame, same transport, same chunking — `20 16` = more chunks follow,
`20 17` = final chunk. Independent agreement with sections 3 and 10.

### What they have that we don't

**A value-decoding formula.** 15-bit signed split across MSB / LSB / a high bit
of LSB / a sign bit, with `display = raw * 100 / 32768` mapping to a 0.0–100.0
range. [PRESET_FORMAT.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/PRESET_FORMAT.md) lists "how to read a value's engineering meaning from
its raw bytes" as unsolved — this is a direct lead, **unverified by us**.

**An explanation for the conditional schema.** Their reader warns that *"some
factory presets use a format different from the user preset, especially with
the MOD Matrix"*, and it detects and flags presets whose format it cannot fully
parse.

This is very likely our `EPanel` anomaly ([PRESET_FORMAT.md](https://github.com/gbryant/microfreak-tools/blob/main/docs/PRESET_FORMAT.md)): 38 of 399
presets carrying an extra field, with no single earlier field predicting it.
Our 399 are all *factory* presets, so we were sampling exactly the population
where this variance is known to live — and looking for a trigger *inside* the
parameter table, when the real discriminator may be which format variant the
preset is stored in at all. Their README mentions workarounds for converting
unsupported factory presets into a readable form; worth reading before more
partition-testing.

### A discrepancy worth noting, not glossing

Their docs give the preset-select request as `01 19`; we verified `03 19` live
on hardware (section 11, and `mfquery`). Could be a firmware difference, a
typo, or two working variants. Ours is hardware-verified on firmware `05 00 00
24`; theirs is not necessarily wrong for other versions.
