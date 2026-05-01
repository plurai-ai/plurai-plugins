"""Tests for the API-key auth module."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from pluto_judge import auth
from pluto_judge.errors import CorruptCredentialsError, MissingApiKeyError


@pytest.fixture
def creds_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the credentials file into a tmp dir and clear PLUTO_API_KEY."""
    path = tmp_path / "pluto" / "credentials.json"
    monkeypatch.setenv("PLUTO_CREDENTIALS_PATH", str(path))
    monkeypatch.delenv("PLUTO_API_KEY", raising=False)
    return path


def test_load_api_key_returns_none_when_unset(creds_path: Path) -> None:
    assert not creds_path.exists()
    key, source = auth.load_api_key()
    assert key is None
    assert source == "none"


def test_save_and_load_round_trip(creds_path: Path) -> None:
    auth.save_api_key("ak_test_xyz")
    key, source = auth.load_api_key()
    assert key == "ak_test_xyz"
    assert source == "file"


def test_save_creates_file_with_0600_perms(creds_path: Path) -> None:
    auth.save_api_key("ak_test_xyz")
    mode = stat.S_IMODE(creds_path.stat().st_mode)
    assert mode == 0o600


def test_save_sets_dir_to_0700(creds_path: Path) -> None:
    auth.save_api_key("ak_test_xyz")
    mode = stat.S_IMODE(creds_path.parent.stat().st_mode)
    assert mode == 0o700


def test_env_var_overrides_file(creds_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    auth.save_api_key("ak_from_file")
    monkeypatch.setenv("PLUTO_API_KEY", "ak_from_env")
    key, source = auth.load_api_key()
    assert key == "ak_from_env"
    assert source == "env"


def test_blank_env_var_falls_through_to_file(
    creds_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth.save_api_key("ak_from_file")
    monkeypatch.setenv("PLUTO_API_KEY", "   ")
    key, source = auth.load_api_key()
    assert key == "ak_from_file"
    assert source == "file"


def test_save_api_key_rejects_empty(creds_path: Path) -> None:
    with pytest.raises(ValueError):
        auth.save_api_key("")
    with pytest.raises(ValueError):
        auth.save_api_key("   ")


def test_save_api_key_strips_whitespace(creds_path: Path) -> None:
    auth.save_api_key("  ak_padded  ")
    assert json.loads(creds_path.read_text())["api_key"] == "ak_padded"


def test_save_is_atomic_via_tmp_rename(creds_path: Path) -> None:
    """A second save replaces the first cleanly with no .tmp leftover."""
    auth.save_api_key("ak_one")
    auth.save_api_key("ak_two")
    key, _ = auth.load_api_key()
    assert key == "ak_two"
    leftovers = list(creds_path.parent.glob("*.tmp"))
    assert leftovers == []


def test_delete_api_key(creds_path: Path) -> None:
    auth.save_api_key("ak_test_xyz")
    assert auth.delete_api_key() is True
    assert not creds_path.exists()
    # Idempotent: calling again returns False rather than raising.
    assert auth.delete_api_key() is False


def test_pluto_headers_raises_when_missing(creds_path: Path) -> None:
    with pytest.raises(MissingApiKeyError) as exc:
        auth.pluto_headers()
    assert "Run /pluto-judge:login" in str(exc.value)


def test_agent_headers_raises_when_missing(creds_path: Path) -> None:
    with pytest.raises(MissingApiKeyError):
        auth.agent_headers()


def test_pluto_and_agent_headers_match_after_save(creds_path: Path) -> None:
    auth.save_api_key("ak_test_xyz")
    assert auth.pluto_headers() == {"Authorization": "Bearer ak_test_xyz"}
    assert auth.agent_headers() == {"Authorization": "Bearer ak_test_xyz"}


def test_load_api_key_raises_on_corrupt_json(creds_path: Path) -> None:
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text("not-json")
    with pytest.raises(CorruptCredentialsError) as exc:
        auth.load_api_key()
    assert str(creds_path) in str(exc.value)
    assert "Run /pluto-judge:login" in str(exc.value)


def test_load_api_key_raises_on_non_string_value(creds_path: Path) -> None:
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(json.dumps({"api_key": 123}))
    with pytest.raises(CorruptCredentialsError):
        auth.load_api_key()


def test_load_api_key_raises_on_non_object_root(creds_path: Path) -> None:
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(json.dumps(["not", "a", "dict"]))
    with pytest.raises(CorruptCredentialsError):
        auth.load_api_key()


def test_load_api_key_treats_missing_field_as_logged_out(creds_path: Path) -> None:
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(json.dumps({"other_field": "hi"}))
    key, source = auth.load_api_key()
    assert key is None
    assert source == "none"


def test_load_api_key_treats_empty_string_as_logged_out(creds_path: Path) -> None:
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(json.dumps({"api_key": "   "}))
    key, source = auth.load_api_key()
    assert key is None
    assert source == "none"


def test_corrupt_file_does_not_block_env_var(
    creds_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var is checked before the file, so a broken file shouldn't matter."""
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text("garbage")
    monkeypatch.setenv("PLUTO_API_KEY", "ak_env")
    key, source = auth.load_api_key()
    assert key == "ak_env"
    assert source == "env"


def test_cli_login_logout_status(creds_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert auth.main(["status"]) == 1
    assert "No API key configured." in capsys.readouterr().out

    assert auth.main(["login", "--key", "ak_cli"]) == 0
    assert "Saved API key" in capsys.readouterr().out

    assert auth.main(["status"]) == 0
    assert "source: file" in capsys.readouterr().out

    assert auth.main(["logout"]) == 0
    assert "Removed" in capsys.readouterr().out

    assert auth.main(["logout"]) == 0
    assert "No saved API key." in capsys.readouterr().out


def test_cli_login_requires_key_flag(creds_path: Path) -> None:
    with pytest.raises(SystemExit):
        auth.main(["login"])


def test_cli_requires_subcommand(creds_path: Path) -> None:
    with pytest.raises(SystemExit):
        auth.main([])


def test_cli_status_reports_env_source(
    creds_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PLUTO_API_KEY", "ak_env")
    assert auth.main(["status"]) == 0
    assert "source: env" in capsys.readouterr().out


def test_cli_status_reports_corrupt_file(
    creds_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text("not-json")
    assert auth.main(["status"]) == 1
    err = capsys.readouterr().err
    assert "unreadable" in err
    assert str(creds_path) in err
