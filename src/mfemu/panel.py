"""Panel numbers in, slot indices out -- the emulator's half of the rule.

The instrument has presets 1-512. The wire counts from 0, because MIDI program
change does. Everything a person types at `mfemu` is a panel number;
everything inside is a 0-based slot.

This mirrors `microfreak.panel` in microfreak-tools, deliberately and with the
same vocabulary, because a number that changed meaning when you switched
repositories would be its own trap: `mfreak preset read 180` and
`mfemu render --preset 180` have to be the same preset. See that module for
the longer note on why the convention inverted on 2026-10-19.

`presetlib.library()` already keyed its dict 0-based, converting from the
1-based slots an MCC bank carries, so the inside of this package needed no
change -- only the specs the user writes on the command line.
"""

PRESETS = 512


def to_slot(value, what="preset"):
    """A 1-based panel number -> the 0-based slot index.

    Raises ValueError, which every caller here already turns into a
    `sys.exit`. Takes an int or a string of digits.
    """
    n = int(value)
    if n == 0:
        raise ValueError(
            "there is no %s 0. They are numbered from 1." % what)
    if not 1 <= n <= PRESETS:
        raise ValueError("%s must be 1-%d, the number the instrument shows; "
                         "got %d" % (what, PRESETS, n))
    return n - 1
