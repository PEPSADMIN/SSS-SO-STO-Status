"""
so_sto_db.py — SO / STO Status Tracker
Local SQLite store for the tracker's own data: the current-state snapshot
ingested from Dispatch SO.xls / Dispach STO.xls, the history log derived by
diffing successive syncs, a cached copy of the Item Master for fast partial
search, each user's item watchlist, and the "who can mark a request Produced"
permission list. Kept in its own DB file (so_sto.db), same convention as
auth_db.py owning users.db and cms_db.py owning cms.db — this is tracker
data, not login/territory data, so it doesn't belong in users.db.
"""

import os
import sqlite3
import datetime
from pathlib import Path

# DATA_DIR points at a persistent volume on Railway (set via env var); falls
# back to this file's own folder for local dev where nothing needs configuring.
DATA_DIR = Path(os.environ.get("DATA_DIR", str(Path(__file__).resolve().parent)))
DB_PATH = DATA_DIR / "so_sto.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS so_sto_snapshot (
                doc_no TEXT NOT NULL,
                item_code TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                doc_date TEXT,
                doc_status TEXT,
                party TEXT,
                place TEXT,
                person TEXT,
                item_desc TEXT,
                uom TEXT,
                ordered_qty REAL,
                required_date TEXT,
                packslip_no TEXT,
                packslip_status TEXT,
                shipped_date TEXT,
                shipped_qty REAL,
                pending_qty REAL,
                invoice_no TEXT,
                invoice_status TEXT,
                invoice_date TEXT,
                invoice_value REAL,
                synced_at TEXT,
                PRIMARY KEY (doc_no, item_code)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS so_sto_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_no TEXT NOT NULL,
                doc_type TEXT NOT NULL,
                item_code TEXT,
                field TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                event_text TEXT NOT NULL,
                occurred_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_doc ON so_sto_history(doc_no)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_occurred ON so_sto_history(occurred_at)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_master (
                item_code TEXT PRIMARY KEY,
                variant_code TEXT,
                item_desc TEXT,
                short_desc TEXT,
                category TEXT,
                uom TEXT,
                status TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_item_code_lower ON item_master(item_code COLLATE NOCASE)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_item_desc_lower ON item_master(item_desc COLLATE NOCASE)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS item_watchlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL,
                item_code TEXT NOT NULL,
                item_desc TEXT,
                item_cat TEXT,
                qty REAL,
                priority TEXT NOT NULL DEFAULT 'Normal',
                status TEXT NOT NULL DEFAULT 'Open',
                produced_by TEXT,
                produced_at TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watchlist_user ON item_watchlist(username)")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS production_managers (
                username TEXT PRIMARY KEY,
                granted_by TEXT,
                granted_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                synced_at TEXT NOT NULL,
                rows_so INTEGER,
                rows_sto INTEGER,
                changes INTEGER,
                status TEXT,
                message TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
            """
        )
        conn.commit()


# ---------------- app settings (background auto-sync interval) ----------------

DEFAULT_SYNC_INTERVAL_MINUTES = 20
MIN_SYNC_INTERVAL_MINUTES = 5


def get_sync_interval_minutes() -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = 'sync_interval_minutes'"
        ).fetchone()
    if row and row["value"]:
        try:
            return max(MIN_SYNC_INTERVAL_MINUTES, int(row["value"]))
        except ValueError:
            pass
    return DEFAULT_SYNC_INTERVAL_MINUTES


def set_sync_interval_minutes(minutes: int) -> int:
    minutes = max(MIN_SYNC_INTERVAL_MINUTES, int(minutes))
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value) VALUES ('sync_interval_minutes', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(minutes),),
        )
        conn.commit()
    return minutes


# ---------------- snapshot ----------------

def get_snapshot_rows():
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM so_sto_snapshot").fetchall()
        return [dict(r) for r in rows]


def upsert_snapshot(rows: list[dict]):
    """Updates/inserts each (doc_no, item_code) row from the latest export.
    Deliberately NOT a full replace: an order that stops appearing in a later
    export (the source report may only cover recent/active orders) stays in
    the snapshot at its last-known state rather than disappearing — so
    historical/closed orders remain searchable and viewable indefinitely.
    Diffing against the *previous* contents happens in so_sto_ingest.py
    before this is called."""
    with _connect() as conn:
        conn.executemany(
            """
            INSERT OR REPLACE INTO so_sto_snapshot (
                doc_no, item_code, doc_type, doc_date, doc_status, party, place, person,
                item_desc, uom, ordered_qty, required_date, packslip_no, packslip_status,
                shipped_date, shipped_qty, pending_qty, invoice_no, invoice_status,
                invoice_date, invoice_value, synced_at
            ) VALUES (
                :doc_no, :item_code, :doc_type, :doc_date, :doc_status, :party, :place, :person,
                :item_desc, :uom, :ordered_qty, :required_date, :packslip_no, :packslip_status,
                :shipped_date, :shipped_qty, :pending_qty, :invoice_no, :invoice_status,
                :invoice_date, :invoice_value, :synced_at
            )
            """,
            rows,
        )
        conn.commit()


def search_docs(term: str = "", doc_type: str = "ALL"):
    """Groups snapshot lines by doc_no for the Search tab's result list."""
    with _connect() as conn:
        sql = "SELECT * FROM so_sto_snapshot WHERE 1=1"
        params: list = []
        if doc_type in ("SO", "STO"):
            sql += " AND doc_type = ?"
            params.append(doc_type)
        if term:
            like = f"%{term}%"
            sql += (" AND (doc_no LIKE ? OR party LIKE ? OR invoice_no LIKE ?)")
            params.extend([like, like, like])
        sql += " ORDER BY doc_date DESC, doc_no"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]

    docs: dict[str, dict] = {}
    for r in rows:
        d = docs.setdefault(r["doc_no"], {
            "doc_no": r["doc_no"], "doc_type": r["doc_type"], "doc_date": r["doc_date"],
            "doc_status": r["doc_status"], "party": r["party"], "place": r["place"],
            "person": r["person"], "required_date": r["required_date"], "items": [],
        })
        d["items"].append(r)
    return list(docs.values())


# Packslip state priority for picking the "representative" line when a doc
# has multiple items with differing packslip/invoice status — same rule
# so_sto_ingest.py uses when collapsing sub-lines within one item.
_PACKSLIP_RANK = {"Not Created": 0, "Pending": 1, "Reversed": 1, "Shipped": 2}


def doc_summary(items: list[dict]) -> dict:
    """Aggregates a doc's item lines into one Order/Packslip/Invoice summary,
    matching the mockup's single-object-per-order shape."""
    best = max(items, key=lambda r: (
        _PACKSLIP_RANK.get(r["packslip_status"], 0),
        r["invoice_status"] == "Invoiced",
    ))
    return {
        "orderStatus": items[0]["doc_status"],
        "orderedQty": sum(r["ordered_qty"] or 0 for r in items),
        "packslip": {
            "no": best["packslip_no"] or "—",
            "status": best["packslip_status"],
            "shippedDate": best["shipped_date"] or "—",
            "shippedQty": sum(r["shipped_qty"] or 0 for r in items),
        },
        "invoice": {
            "no": best["invoice_no"] or "—",
            "status": best["invoice_status"],
            "date": best["invoice_date"] or "—",
            "value": sum(r["invoice_value"] or 0 for r in items) or None,
        },
    }


def get_doc(doc_no: str):
    with _connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM so_sto_snapshot WHERE doc_no = ?", (doc_no,)
        ).fetchall()]
    if not rows:
        return None
    head = rows[0]
    return {
        "doc_no": head["doc_no"], "doc_type": head["doc_type"], "doc_date": head["doc_date"],
        "doc_status": head["doc_status"], "party": head["party"], "place": head["place"],
        "person": head["person"], "required_date": head["required_date"], "items": rows,
    }


# ---------------- history ----------------

def add_history_events(events: list[dict]):
    if not events:
        return
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO so_sto_history (doc_no, doc_type, item_code, field, old_value, new_value,
                                         event_text, occurred_at)
            VALUES (:doc_no, :doc_type, :item_code, :field, :old_value, :new_value,
                    :event_text, :occurred_at)
            """,
            events,
        )
        conn.commit()


def history_for_doc(doc_no: str):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM so_sto_history WHERE doc_no = ? ORDER BY id", (doc_no,)
        ).fetchall()
        return [dict(r) for r in rows]


def history_in_range(start: str, end: str):
    """start/end are 'YYYY-MM-DD' date-only bounds on occurred_at."""
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM so_sto_history
            WHERE substr(occurred_at, 1, 10) BETWEEN ? AND ?
            ORDER BY occurred_at
            """,
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]


def summary_for_range(start: str, end: str) -> dict:
    """Shapes history events in [start, end] into the Home tab's counts +
    document list — see the mockup's DAY_ACTIVITY / renderDaySummary()."""
    events = history_in_range(start, end)
    authorized = sum(1 for e in events if e["field"] == "doc_status" and e["new_value"] == "Authorized")
    dispatched = sum(1 for e in events if e["field"] == "packslip_status" and e["new_value"] == "Shipped")
    invoiced = sum(1 for e in events if e["field"] == "invoice_status" and e["new_value"] == "Invoiced")
    docs = [
        {"no": e["doc_no"], "type": e["doc_type"], "text": e["event_text"], "dateKey": e["occurred_at"][:10]}
        for e in events
    ]
    return {"authorized": authorized, "dispatched": dispatched, "invoiced": invoiced, "docs": docs}


# ---------------- item master ----------------

def replace_item_master(rows: list[dict]):
    """INSERT OR REPLACE, not plain INSERT — Item Master.csv has a handful of
    repeated Item Code values (same base code, different Variant Code); the
    tracker only needs one searchable row per code, so last-one-wins."""
    with _connect() as conn:
        conn.execute("DELETE FROM item_master")
        conn.executemany(
            """
            INSERT OR REPLACE INTO item_master
                (item_code, variant_code, item_desc, short_desc, category, uom, status)
            VALUES (:item_code, :variant_code, :item_desc, :short_desc, :category, :uom, :status)
            """,
            rows,
        )
        conn.commit()


def item_master_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM item_master").fetchone()[0]


def search_items(term: str, limit: int = 30):
    like = f"%{term}%"
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM item_master
            WHERE item_code LIKE ? COLLATE NOCASE OR item_desc LIKE ? COLLATE NOCASE
            ORDER BY item_code LIMIT ?
            """,
            (like, like, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------- watchlist ----------------

def list_watchlist(username: str):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM item_watchlist WHERE username = ? ORDER BY id", (username,)
        ).fetchall()
        return [dict(r) for r in rows]


def add_watch(username: str, item_code: str, item_desc: str, item_cat: str,
              qty: float | None, priority: str) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO item_watchlist (username, item_code, item_desc, item_cat, qty, priority,
                                         status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'Open', ?)
            """,
            (username, item_code, item_desc, item_cat, qty, priority, _now()),
        )
        conn.commit()
        return cur.lastrowid


def remove_watch(watch_id: int, username: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM item_watchlist WHERE id = ? AND username = ?", (watch_id, username)
        )
        conn.commit()
        return cur.rowcount > 0


def get_watch(watch_id: int):
    with _connect() as conn:
        row = conn.execute("SELECT * FROM item_watchlist WHERE id = ?", (watch_id,)).fetchone()
        return dict(row) if row else None


def set_watch_status(watch_id: int, status: str, by_user: str | None = None) -> bool:
    with _connect() as conn:
        if status == "Produced":
            cur = conn.execute(
                "UPDATE item_watchlist SET status = ?, produced_by = ?, produced_at = ? WHERE id = ?",
                (status, by_user, _now(), watch_id),
            )
        else:
            cur = conn.execute(
                "UPDATE item_watchlist SET status = ?, produced_by = NULL, produced_at = NULL WHERE id = ?",
                (status, watch_id),
            )
        conn.commit()
        return cur.rowcount > 0


def matches_for_item_code(item_code: str):
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM so_sto_snapshot WHERE item_code = ? ORDER BY doc_date DESC", (item_code,)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------- production managers ----------------

def list_production_managers():
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM production_managers ORDER BY username"
        ).fetchall()
        return [dict(r) for r in rows]


def is_production_manager(username: str | None) -> bool:
    if not username:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM production_managers WHERE username = ?", (username,)
        ).fetchone()
        return row is not None


def grant_production_manager(username: str, granted_by: str):
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO production_managers (username, granted_by, granted_at) VALUES (?, ?, ?)",
            (username, granted_by, _now()),
        )
        conn.commit()


def revoke_production_manager(username: str):
    with _connect() as conn:
        conn.execute("DELETE FROM production_managers WHERE username = ?", (username,))
        conn.commit()


# ---------------- sync log ----------------

def record_sync(rows_so: int, rows_sto: int, changes: int, status: str, message: str = ""):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sync_log (synced_at, rows_so, rows_sto, changes, status, message) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), rows_so, rows_sto, changes, status, message),
        )
        conn.commit()


def last_sync():
    with _connect() as conn:
        row = conn.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None
