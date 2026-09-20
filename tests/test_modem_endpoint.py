"""_modem_endpoint - the single-source-of-truth port derivation.

The 5052/5055 mismatch (caught on hilltop 2026-09-20): the embedded
server binds modem.conf's port while the node dialed the plugin-era
companion default - radio up, node knocking on the wrong door. The
endpoint the node dials must come from the SAME file the server binds.
"""
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.node import _modem_endpoint  # noqa: E402


class _S:
    """Minimal settings stand-in."""

    def __init__(self, modem_conf="", companion_host="127.0.0.1",
                 companion_port=5052):
        self.modem_conf = modem_conf
        self.companion_host = companion_host
        self.companion_port = companion_port


def test_embedded_mode_dials_modem_conf_port(tmp_path):
    conf = tmp_path / "modem.conf"
    conf.write_text("host = 127.0.0.1\nport = 5055\n", encoding="utf-8")
    assert _modem_endpoint(_S(modem_conf=str(conf))) == ("127.0.0.1", 5055)


def test_embedded_mode_dials_modem_conf_host(tmp_path):
    conf = tmp_path / "modem.conf"
    conf.write_text("host = 127.0.0.2\nport = 5060\n", encoding="utf-8")
    assert _modem_endpoint(_S(modem_conf=str(conf))) == ("127.0.0.2", 5060)


def test_fallback_without_modem_conf():
    # bench / standalone-cleanmodem layouts keep the companion endpoint.
    assert _modem_endpoint(_S()) == ("127.0.0.1", 5052)


def test_missing_modem_conf_matches_embedded_server_defaults():
    # A missing file makes cleanmodem's loader return defaults - and
    # InProcessRadio.start() would bind those SAME defaults. The node
    # must dial what the server actually binds, so defaults here are
    # correct, not a fallback (consistency is the invariant).
    from cleanmodem.config import build_config, load_config  # noqa: PLC0415

    host, port = _modem_endpoint(_S(modem_conf="/no/such/modem.conf"))
    cfg = build_config(load_config("/no/such/modem.conf"))
    assert (host, port) == (cfg.host, cfg.port)


def test_fallback_when_modem_conf_unparseable(tmp_path, monkeypatch):
    # A hard parse error (malformed value) degrades honestly to the
    # companion endpoint rather than crashing the shell - the radio
    # itself would then refuse to start loudly at InProcessRadio.start.
    conf = tmp_path / "modem.conf"
    conf.write_text("port = not-a-number\n", encoding="utf-8")

    import meshtech_node.node as node_mod

    def _boom(*_a, **_k):
        raise RuntimeError("simulated parse failure")

    monkeypatch.setattr(node_mod, "_load_modem_config", _boom)
    assert _modem_endpoint(_S(modem_conf=str(conf))) == ("127.0.0.1", 5052)


def test_real_template_port_is_what_the_node_dials():
    # The shipped template: whatever port it binds, the node dials.
    import tempfile

    from cleanmodem.config import build_config, load_config  # noqa: PLC0415
    repo = os.path.join(os.path.dirname(__file__), "..")
    template = os.path.join(repo, "deploy", "modem.conf")
    if not os.path.isfile(template):
        textwrap.dedent("skip")  # pragma: no cover
    cfg = build_config(load_config(template))
    host, port = _modem_endpoint(_S(modem_conf=template))
    assert (host, port) == (cfg.host, cfg.port)
