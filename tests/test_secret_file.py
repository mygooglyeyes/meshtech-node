"""channel.secret_file - the key out of config.json (Brett, 2026-09-27).

Contract: resolve_channel_secret returns the secret TEXT for
derive_channel_keys. Priority: secret_file first line > secret_hex >
hashtag rule. A named file that fails at BUILD time is a loud
RuntimeError - never a quiet fall-back to the hashtag rule (the
honesty rule; a wrong key would just silently decode nothing).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from meshtech_node import config as cfgmod  # noqa: E402
from meshtech_node.client import (  # noqa: E402
    resolve_channel_secret,
    scope_secret,
)
from meshtech_node.node import _build  # noqa: E402

SECRET = "ab" * 32  # 32 bytes of hex text


def _write_config(tmp_path, body: dict) -> str:
    path = tmp_path / "config.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    return str(path)


def _keyfile(tmp_path, text: str) -> str:
    path = tmp_path / "channel.key"
    path.write_text(text, encoding="utf-8")
    return str(path)


def _settings(tmp_path, channel_body: dict):
    path = _write_config(tmp_path, {"channel": channel_body})
    return cfgmod.load(path)


# ------------------------------------------------------- resolution ----

def test_file_first_line_wins(tmp_path):
    s = _settings(tmp_path, {"name": "#meshtech",
                             "secret_file": _keyfile(tmp_path, SECRET + "\n")})
    assert resolve_channel_secret(s.channel) == SECRET


def test_hex_still_works(tmp_path):
    s = _settings(tmp_path, {"name": "#meshtech", "secret_hex": SECRET})
    assert resolve_channel_secret(s.channel) == SECRET


def test_hashtag_rule_when_both_empty(tmp_path):
    s = _settings(tmp_path, {"name": "#meshtech"})
    assert resolve_channel_secret(s.channel) == \
        scope_secret("#meshtech").hex()


# ---------------------------------------------------- fail closed ------

def test_file_deleted_after_load_is_loud(tmp_path):
    """Config load validates the file, but it can vanish between load
    and build - the build-time refusal is the second line of defense,
    and it must be LOUD (never a quiet hashtag fall-back)."""
    path = _keyfile(tmp_path, SECRET + "\n")
    s = _settings(tmp_path, {"name": "#meshtech", "secret_file": path})
    os.remove(path)
    with pytest.raises(RuntimeError) as exc:
        resolve_channel_secret(s.channel)
    assert "refusing to start" in str(exc.value)


def test_file_corrupted_after_load_is_loud(tmp_path):
    path = _keyfile(tmp_path, SECRET + "\n")
    s = _settings(tmp_path, {"name": "#meshtech", "secret_file": path})
    path2 = tmp_path / "channel.key"
    path2.write_text("not hex any more\n", encoding="utf-8")
    with pytest.raises(RuntimeError):
        resolve_channel_secret(s.channel)


# ------------------------------------------------------- wiring -------

def test_build_uses_file_secret(tmp_path):
    """The whole point: a node built with ONLY a secret_file carries the
    same channel keys as one built with the same secret as hex - the
    on-air keys must be identical either way."""
    secret_hex = scope_secret("#meshtech").hex()
    s_file = _settings(tmp_path, {"name": "#meshtech",
                                  "secret_file":
                                  _keyfile(tmp_path, secret_hex + "\n")})
    _, sender_file, _, _ = _build(s_file, bench_no_radio=True)
    assert sender_file.channel.aes_key == bytes.fromhex(secret_hex)[:16]
    assert sender_file.channel.aes_key != b"\x00" * 16
