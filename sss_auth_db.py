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


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user'
            )
            """
        )
        conn.commit()


def get_user(username: str):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def verify_login(username: str, password: str):
    user = get_user(username)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


def create_user(username: str, password: str, role: str = "user"):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), role),
        )
        conn.commit()


def list_users():
    with _connect() as conn:
        rows = conn.execute("SELECT id, username, role FROM users ORDER BY id").fetchall()
        return [dict(r) for r in rows]


def update_user(user_id: int, role: str | None = None, password: str | None = None):
    fields, values = [], []
    if role is not None:
        fields.append("role = ?"); values.append(role)
    if password is not None:
        fields.append("password_hash = ?"); values.append(generate_password_hash(password))
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
