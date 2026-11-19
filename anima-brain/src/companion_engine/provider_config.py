"""Explicit, per-OS-user provider settings. No keys enter MCP/tool output."""

import hashlib
import json
import os
import stat
import sys
import tempfile
from pathlib import Path
from uuid import uuid4


def settings_path() -> Path:
    return (
        Path(os.environ.get("COMPANION_SETTINGS_FILE", "~/.config/companion-engine/providers.json"))
        .expanduser()
        .absolute()
    )


def _read_settings_file() -> dict:
    path = settings_path()
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return {}
    except OSError:
        raise ValueError("Cannot safely open provider settings file") from None
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        mode = os.fstat(stream.fileno())
        if not stat.S_ISREG(mode.st_mode) or stat.S_IMODE(mode.st_mode) & 0o077:
            raise ValueError("Provider settings must be a private regular file (mode 0600)")
        if mode.st_size > 65536:
            raise ValueError("Provider settings file is too large")
        try:
            values = json.load(stream)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid provider settings file") from None
    if not isinstance(values, dict):
        raise ValueError("Invalid provider settings file")
    return values


SERVICE = "companion-engine.providers"


def _keychain():
    try:
        from keyring.backends.macOS import Keyring

        return Keyring()
    except Exception:
        raise ValueError("macOS Keychain is unavailable; no keys were saved") from None


def secret_store():
    mode = os.environ.get(
        "COMPANION_SECRET_STORE", "keychain" if sys.platform == "darwin" else "file"
    )
    if mode not in ("keychain", "file"):
        raise ValueError("COMPANION_SECRET_STORE must be keychain or file")
    return mode


def storage_status():
    raw = _read_settings_file()
    return {
        "saved_secrets": "macos_keychain"
        if raw.get("_secret_store")
        else "private_file"
        if any(k.endswith("key") and v for k, v in raw.items())
        else "none",
        "save_destination": secret_store(),
        "configuration_location": str(settings_path()),
    }


def load_provider_settings() -> dict:
    values = _read_settings_file()
    ref = values.pop("_secret_store", None)
    if ref:
        try:
            if ref["backend"] != "macos-keychain":
                raise ValueError()
            stored = _keychain().get_password(SERVICE, ref["account"])
            keys = json.loads(stored) if stored else None
            if not isinstance(keys, dict) or any(
                not k.endswith("key") or not isinstance(v, str) for k, v in keys.items()
            ):
                raise ValueError()
            values.update(keys)
        except Exception:
            raise ValueError(
                "Cannot read provider keys from macOS Keychain; "
                "unlock Keychain or reconfigure locally"
            ) from None
    return values


def save_provider_settings(settings) -> None:
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Provider settings file cannot be a symlink")
    previous = _read_settings_file().get("_secret_store")
    values = {
        name: value.get_secret_value() if hasattr(value, "get_secret_value") else value
        for name, value in settings.__dict__.items()
    }
    account = None
    if secret_store() == "keychain":
        keys = {k: v for k, v in values.items() if k.endswith("key")}
        values = {k: v for k, v in values.items() if not k.endswith("key")}
        account = hashlib.sha256(str(path).encode()).hexdigest()[:16] + "-" + str(uuid4())
        try:
            _keychain().set_password(SERVICE, account, json.dumps(keys))
        except Exception:
            raise ValueError("Keychain save failed; existing settings unchanged") from None
        values["_secret_store"] = {"backend": "macos-keychain", "account": account}
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=".providers-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        if account:
            try:
                _keychain().delete_password(SERVICE, account)
            except Exception:
                pass
        raise
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)

    if previous and previous.get("account") != account:
        try:
            _keychain().delete_password(SERVICE, previous["account"])
        except Exception:
            pass  # Orphaned vault entry is safer than undoing a successful atomic save.
