"""Reference-library availability for the crypto tests.

The node treats pymc_core (vendored inside the read-only
openhop_core/src clone) as its crypto/parse reference. The crypto
tests need it; the pure tests in test_packets.py run without it.
"""
import os
import sys

# The node's own package (src layout).
_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# The read-only reference library (crypto/parse proof target).
_REF = "C:/projects/openhop_core/src" if os.name == "nt" \
    else "/c/projects/openhop_core/src"
if os.path.isdir(os.path.join(_REF, "pymc_core")):
    sys.path.insert(0, _REF)
