"""
sss_auth_db.py — SSS (SO/STO Status)
Local SQLite store for SSS's own login accounts. Deliberately independent of
Sales_Mobile's auth_db.py/users.db — SSS is a standalone tool with its own
accounts, not a page bolted onto the shared sales hub. Same _connect()/
sqlite3.Row pattern as the rest of this codebase (auth_db.py, cms_db.py).
"""

import os
import sqlite3
from pathlib import Path
from werkzeug.security import generate_password_hash, check_password_hash

# DATA_DIR points at a persistent volume on Railway (set via env var); falls
# back to this file's own folder for local dev where nothing needs configuring.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent)))
DB_PATH = DATA_DIR / "sss_users.db"

# Nav tabs a user can be granted/denied from CMS. CMS itself isn't in this
# list — it's tied to role == 'admin' directly, not to per-user customization.
ALL_TABS = ["home", "search", "alerts", "settings"]


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_tabs(raw) -> list[str]:
    # NULL/never-customized => everyone gets every tab (today's default,
    # and the default for brand-new accounts). An explicit empty string is
    # a deliberate "no tabs" from CMS, not the same as "not set yet".
    if raw is None:
        return list(ALL_TABS)
    keys = [t for t in raw.split(",") if t]
    return [t for t in ALL_TABS if t in keys]


def _serialize_tabs(tabs: list[str]) -> str:
    return ",".join(t for t in ALL_TABS if t in tabs)


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                tabs TEXT
            )
            """
        )
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
        if "tabs" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN tabs TEXT")
        conn.commit()


def _with_parsed_tabs(row: dict) -> dict:
    row["tabs"] = _parse_tabs(row.get("tabs"))
    return row


def get_user(username: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        return _with_parsed_tabs(dict(row)) if row else None


def get_user_by_id(user_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _with_parsed_tabs(dict(row)) if row else None


def verify_login(username: str, password: str):
    user = get_user(username)
    if not user:
        return None
    # Passwords are hashed lowercase (see create_user/update_user) so login
    # is case-insensitive going forward. check_password_hash(..., password)
    # is also tried for accounts whose hash predates that change, so their
    # original exact-case password keeps working until it's reset.
    if check_password_hash(user["password_hash"], password.lower()) or \
       check_password_hash(user["password_hash"], password):
        return user
    return None


def create_user(username: str, password: str, role: str = "user", tabs: list[str] | None = None):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role, tabs) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password.lower()), role, _serialize_tabs(tabs) if tabs is not None else None),
        )
        conn.commit()


def list_users():
    with _connect() as conn:
        rows = conn.execute("SELECT id, username, role, tabs FROM users ORDER BY id").fetchall()
        return [_with_parsed_tabs(dict(r)) for r in rows]


def update_user(user_id: int, role: str | None = None, password: str | None = None, tabs: list[str] | None = None):
    fields, values = [], []
    if role is not None:
        fields.append("role = ?"); values.append(role)
    if password is not None:
        fields.append("password_hash = ?"); values.append(generate_password_hash(password.lower()))
    if tabs is not None:
        fields.append("tabs = ?"); values.append(_serialize_tabs(tabs))
    if not fields:
        return
    values.append(user_id)
    with _connect() as conn:
        conn.execute(f"UPDATE users SET {', '.join(fields)} WHERE id = ?", values)
        conn.commit()


def delete_user(user_id: int):
    with _connect() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
