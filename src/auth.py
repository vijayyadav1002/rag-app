"""
One shared website password, and a session per browser.

A single boolean cookie would log every browser out together and could
not be revoked while a copy of it still exists. Keeping sessions only in
process memory would drop the idle window on restart. Comparing the
password with == leaks timing, and storing it next to the token would
put the secret in a file we already have to keep on disk.

Sessions are random tokens. The file stores a hash of the token and a
stamp of the current password, and a logout deletes that one record.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Shell exports take precedence (override=False). Missing file is a no-op.
load_dotenv(Path(__file__).parent / ".env", override=False)

COOKIE_NAME = "northwind_session"
SESSION_IDLE_SECONDS = 10 * 24 * 60 * 60  # 864000
LOGIN_MAX_FAILURES = 5
LOGIN_COOLDOWN_SECONDS = 60
MAX_SESSIONS = 100
STAMP_PREFIX = b"ask-northwind-lock-v1"
SESSIONS_PATH = Path(__file__).parent / "sessions.json"

_UNREADABLE = "sessions.json could not be read; treating it as no sessions."
_UNWRITABLE = "session store could not be written."

_MISSING = "missing"
_LIVE = "live"
_DROP = "drop"


@dataclass(frozen=True)
class LoginResult:
    token: str | None
    throttled: bool
    store_error: bool
    evicted_keys: list[str]


@dataclass(frozen=True)
class TouchResult:
    state: str
    last_access: int | None = None


@dataclass(frozen=True)
class LogoutResult:
    state: str


@dataclass
class _Attempt:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class _Loaded:
    rows: dict[str, dict]
    malformed_keys: list[str]


_lock = threading.Lock()
_throttle: dict[str, _Attempt] = {}
_cached_password: str | None = None
_cached_stamp: str | None = None

_PUBLIC_GET = frozenset(
    {
        "/",
        "/library",
        "/app.css",
        "/manifest.webmanifest",
        "/sw.js",
        "/api/session",
    }
)
_PUBLIC_POST = frozenset({"/api/session", "/api/session/logout"})


def require_lock_password() -> str:
    password = (os.environ.get("LOCK_PASSWORD") or "").strip()
    if not password:
        raise SystemExit(
            "LOCK_PASSWORD is missing or empty. Set it in .env (see .env.example). "
            "The website does not start without a password."
        )
    return password


def passwords_match(supplied: str, expected: str) -> bool:
    left = hashlib.sha256(supplied.encode("utf-8")).digest()
    right = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(left, right)


def password_stamp(password: str) -> str:
    return hashlib.sha256(STAMP_PREFIX + password.encode("utf-8")).hexdigest()


def token_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_public(method: str, path: str) -> bool:
    if method == "GET":
        if path in _PUBLIC_GET:
            return True
        return path.startswith("/icons/") and ".." not in path
    if method == "POST":
        return path in _PUBLIC_POST
    return False


def reset_throttle() -> None:
    with _lock:
        _throttle.clear()


def reload_password() -> list[str]:
    global _cached_password, _cached_stamp
    password = require_lock_password()
    stamp = password_stamp(password)
    with _lock:
        _cached_password = password
        _cached_stamp = stamp
        now = int(time.time())
        loaded = _read_file()
        dropped = list(loaded.malformed_keys)
        kept: dict[str, dict] = {}
        for key, row in loaded.rows.items():
            if _is_live(row, now):
                kept[key] = row
            else:
                dropped.append(key)
        if dropped:
            _save({"sessions": kept})
        return dropped


def login(
    password: str,
    client_key: str,
    now: int | None = None,
    mono: float | None = None,
) -> LoginResult:
    """Use the cached password and stamp. Do not read LOCK_PASSWORD.

    Before the first reload_password(), return store_error and write nothing.
    Do not drop well-formed rows except MAX_SESSIONS eviction (oldest last_access).
    """
    if now is None:
        now = int(time.time())
    if mono is None:
        mono = time.monotonic()
    with _lock:
        if _throttled(client_key, mono):
            return LoginResult(None, True, False, [])
        if _cached_password is None or _cached_stamp is None:
            return LoginResult(None, False, True, [])
        if not passwords_match(password, _cached_password):
            _note_failure(client_key, mono)
            return LoginResult(None, False, False, [])
        _clear_client(client_key)
        return _insert_session(now)


def logout(token: str) -> LogoutResult:
    now = int(time.time())
    with _lock:
        loaded = _read_file()
        key = token_key(token)
        present = loaded.rows.get(key)
        live = present is not None and _is_live(present, now)
        kept = {
            row_key: row
            for row_key, row in loaded.rows.items()
            if row_key != key and _is_live(row, now)
        }
        changed = (
            bool(loaded.malformed_keys)
            or present is not None
            or len(kept) != len(loaded.rows)
        )
        if changed:
            try:
                _save({"sessions": kept})
            except OSError:
                if present is not None:
                    return LogoutResult("store_error")
                return LogoutResult("absent")
        if live:
            return LogoutResult("removed")
        return LogoutResult("absent")


def touch(token: str, now: int | None = None) -> TouchResult:
    if now is None:
        now = int(time.time())
    with _lock:
        # An empty cookie is dead even if a row exists. Do not slide it.
        if not token:
            return _decide(token_key(token), now, slide=False, force_dead=True)
        return _decide(token_key(token), now, slide=True)


def classify(token: str, now: int | None = None) -> TouchResult:
    """Hash the raw cookie once. A file key passed here misses the real row."""
    return classify_key(token_key(token), now)


def classify_key(key: str, now: int | None = None) -> TouchResult:
    """Look up a session-file key. Does not hash it, and does not slide."""
    if now is None:
        now = int(time.time())
    with _lock:
        return _decide(key, now, slide=False)


def _throttled(client_key: str, mono: float) -> bool:
    state = _throttle.get(client_key)
    if state is None:
        return False
    if mono < state.locked_until:
        return True
    if state.locked_until != 0 and mono >= state.locked_until:
        state.failures = 0
        state.locked_until = 0.0
    return False


def _note_failure(client_key: str, mono: float) -> None:
    state = _throttle.get(client_key)
    if state is None:
        state = _Attempt()
        _throttle[client_key] = state
    state.failures += 1
    if state.failures >= LOGIN_MAX_FAILURES:
        state.locked_until = mono + LOGIN_COOLDOWN_SECONDS


def _clear_client(client_key: str) -> None:
    state = _throttle.get(client_key)
    if state is not None:
        state.failures = 0
        state.locked_until = 0.0


def _insert_session(now: int) -> LoginResult:
    loaded = _read_file()
    rows = dict(loaded.rows)
    evicted: list[str] = []
    while len(rows) >= MAX_SESSIONS and rows:
        oldest = min(rows, key=lambda item: (rows[item]["last_access"], item))
        evicted.append(oldest)
        del rows[oldest]
    token = secrets.token_urlsafe(32)
    rows[token_key(token)] = {
        "stamp": _cached_stamp,
        "created_at": now,
        "last_access": now,
    }
    try:
        _save({"sessions": rows})
    except OSError:
        return LoginResult(None, False, True, [])
    return LoginResult(token, False, False, evicted)


def _decide(
    key: str, now: int, *, slide: bool, force_dead: bool = False
) -> TouchResult:
    loaded = _read_file()
    kept: dict[str, dict] = {}
    target = _MISSING
    target_row: dict | None = None
    changed = bool(loaded.malformed_keys)
    for row_key, row in loaded.rows.items():
        live = _is_live(row, now)
        if row_key == key:
            target_row = row
            if live and not force_dead:
                target = _LIVE
                if slide:
                    updated = dict(row)
                    updated["last_access"] = now
                    kept[row_key] = updated
                    changed = True
                else:
                    kept[row_key] = row
            else:
                target = _DROP
                changed = True
            continue
        if live:
            kept[row_key] = row
        else:
            changed = True
    if changed:
        try:
            _save({"sessions": kept})
        except OSError:
            if target == _MISSING:
                return TouchResult("dead", None)
            return TouchResult("store_error", None)
    if target == _LIVE and target_row is not None:
        access = now if slide else target_row["last_access"]
        return TouchResult("ok", access)
    return TouchResult("dead", None)


def _is_live(row: dict, now: int) -> bool:
    if not _stamp_matches(row.get("stamp")):
        return False
    return now - row["last_access"] < SESSION_IDLE_SECONDS


def _stamp_matches(stored: object) -> bool:
    expected = _cached_stamp
    if not isinstance(stored, str) or expected is None:
        return False
    try:
        return hmac.compare_digest(stored, expected)
    except (TypeError, ValueError):
        return False


def _parse_row(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    stamp = value.get("stamp")
    created = value.get("created_at")
    last = value.get("last_access")
    if not isinstance(stamp, str) or not _is_int(created) or not _is_int(last):
        return None
    return {"stamp": stamp, "created_at": created, "last_access": last}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _read_file() -> _Loaded:
    path = SESSIONS_PATH
    if not path.is_file():
        return _Loaded({}, [])
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print(_UNREADABLE, file=sys.stderr)
        return _Loaded({}, [])
    if not isinstance(data, dict):
        print(_UNREADABLE, file=sys.stderr)
        return _Loaded({}, [])
    raw = data.get("sessions", {})
    if not isinstance(raw, dict):
        return _Loaded({}, [])
    rows: dict[str, dict] = {}
    malformed: list[str] = []
    for key, value in raw.items():
        parsed = _parse_row(value)
        if parsed is None:
            malformed.append(key)
        else:
            rows[key] = parsed
    return _Loaded(rows, malformed)


def _save(payload: dict) -> None:
    path = SESSIONS_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError:
        print(_UNWRITABLE, file=sys.stderr)
        raise


if __name__ == "__main__":
    require_lock_password()
    print("LOCK_PASSWORD is set.")
