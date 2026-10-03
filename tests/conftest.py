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

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    """Tests never touch the real data dir: data_dir() (the origin
    key file) lands in a PER-TEST tmp dir, so a run can never mint a
    key into the repo's data/ or inherit yesterday's."""
    monkeypatch.setenv("OPENHOP_PLUGIN_DATA", str(tmp_path))

# The read-only reference library (crypto/parse proof target).
_REF = "C:/projects/openhop_core/src" if os.name == "nt" \
    else "/c/projects/openhop_core/src"
if os.path.isdir(os.path.join(_REF, "pymc_core")):
    sys.path.insert(0, _REF)
