"""Is this machine set up to run the harness? -> mfemu.check

The implementation lives in the package rather than here, so `mfemu check`
and this test cannot drift apart -- and so the thing a newcomer runs first is
one import away from the thing the CLI runs.
"""
import sys

from mfemu.check import main

if __name__ == "__main__":
    sys.exit(main())
