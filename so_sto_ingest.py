"""
so_sto_ingest.py — SO / STO Status Tracker
Pure data logic, no Flask — reads the two dispatch report exports plus the
Item Master, normalizes them into so_sto_db's snapshot shape, and diffs each
sync against the previous snapshot to build the history log. Deliberately
free of any request/session dependency so a future scheduler (or an API
webhook, once the data source moves off manually-dropped Excel files) can
call sync_dispatch_files() directly with no rework — see the plan notes.

--- Data-quality notes, found by inspecting the real files (not assumptions) ---

1. "Invoice Status" (SO) / "invoice Status" (STO) do NOT mean what they sound
   like — their real values are Fresh/Authorized/Closed, i.e. the *order's*
   lifecycle vocabulary, not an invoiced/not-invoiced flag. We ignore that
   column entirely and derive invoice status from whether Invoice Number is
   populated.

2. Dispatch SO.xls has ~550 rows that are pure exact full-row duplicates (an
   export artifact) plus a further set of rows that share the same
   (Sale Order Number, Item Code) but differ in Ordered/Shipped/Invoiced
   Quantity (split packslips/invoices against the same line). We drop exact
   duplicates, then aggregate the remainder by (doc_no, item_code) — summing
   quantities, and taking the most-advanced packslip/invoice state for the
   status fields — since the tracker works at one-line-per-item-per-order
   granularity, not sub-line billing detail.
"""

import os
import datetime
from pathlib import Path
import pandas as pd

import so_sto_db

# Locally, the three source files already live in fixed folders next to the
# app (../Input and ../Item Master, relative to this file) — so the project
# can live anywhere on disk (or be renamed) without editing paths. On Railway
# (DATA_DIR set to a mounted persistent volume), there's no local Input folder
# to read from, so uploaded files land under DATA_DIR/uploads instead — see
# save_uploaded_file() / the /api/so-sto/upload route in app.py.
_DATA_DIR_ENV = os.environ.get("DATA_DIR")
if _DATA_DIR_ENV:
    INPUT_DIR = Path(_DATA_DIR_ENV) / "uploads"
    ITEM_MASTER_FILE = str(Path(_DATA_DIR_ENV) / "uploads" / "Item Master.csv")
else:
    # App/so_sto_ingest.py -> parent is App/, parent of that is the project root.
    PROJECT_ROOT = Path(__file__).resolve().parent.parent
    INPUT_DIR = PROJECT_ROOT / "Input"
    ITEM_MASTER_FILE = str(PROJECT_ROOT / "Item Master" / "Item Master.csv")
SO_FILE = str(INPUT_DIR / "Dispatch SO.xls")
STO_FILE = str(INPUT_DIR / "Dispach STO.xls")

UPLOAD_KIND_TO_PATH = {"so": SO_FILE, "sto": STO_FILE, "item_master": ITEM_MASTER_FILE}


def save_uploaded_file(kind: str, file_storage) -> str:
    """Saves an uploaded Werkzeug FileStorage to the path sync_dispatch_files()/
    load_item_master() read from. `kind` is 'so' | 'sto' | 'item_master'."""
    if kind not in UPLOAD_KIND_TO_PATH:
        raise ValueError(f"unknown upload kind: {kind}")
    dest = UPLOAD_KIND_TO_PATH[kind]
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    file_storage.save(dest)
    return dest

# Packslip state priority used when collapsing multiple sub-lines for the same
# (doc_no, item_code) down to one row — higher wins.
_PACKSLIP_RANK = {"Not Created": 0, "Pending": 1, "Reversed": 1, "Shipped": 2}


def _clean(v):
    if pd.isna(v):
        return None
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _clean_num(v):
    if pd.isna(v):
        return 0.0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clean_date(v):
    if pd.isna(v):
        return None
    if isinstance(v, str):
        return v.strip() or None
    if hasattr(v, "strftime"):
        return v.strftime("%d/%m/%Y")
    return str(v)


def _derive_packslip_status(packslip_no, shipped_date, raw_status):
    if not packslip_no:
        return "Not Created"
    if str(raw_status or "").strip().upper() == "REVERSED":
        return "Reversed"
    if not shipped_date:
        return "Pending"
    return "Shipped"


def _derive_invoice_status(invoice_no):
    return "Invoiced" if invoice_no else "Not Invoiced"


def _normalize_so(df: pd.DataFrame) -> list[dict]:
    df = df.drop_duplicates()
    rows = []
    for _, r in df.iterrows():
        packslip_no = _clean(r.get("Pack Slip Number"))
        shipped_date = _clean_date(r.get("Shipped Date"))
        invoice_no = _clean(r.get("Invoice Number"))
        city = _clean(r.get("City")) or ""
        state = _clean(r.get("State")) or ""
        rows.append({
            "doc_no": _clean(r.get("Sale Order Number")),
            "item_code": _clean(r.get("Item Code")),
            "doc_type": "SO",
            "doc_date": _clean_date(r.get("Sales Order Date")),
            "doc_status": _clean(r.get("Sales Order Status")) or "Fresh",
            "party": _clean(r.get("Customer Name")),
            "place": ", ".join(p for p in (city, state) if p),
            "person": _clean(r.get("Sales Person Name")),
            "item_desc": _clean(r.get("Item Description")),
            "uom": _clean(r.get("UOM")),
            "ordered_qty": _clean_num(r.get("Ordered Quantity")),
            "required_date": _clean_date(r.get("Required Date")),
            "packslip_no": packslip_no,
            "packslip_status": _derive_packslip_status(packslip_no, shipped_date, r.get("Pack Slip Status")),
            "shipped_date": shipped_date,
            "shipped_qty": _clean_num(r.get("Shipped Quantity")),
            "pending_qty": _clean_num(r.get("Pending Qty After Packslip")),
            "invoice_no": invoice_no,
            "invoice_status": _derive_invoice_status(invoice_no),
            "invoice_date": _clean_date(r.get("Invoice Date")),
            "invoice_value": _clean_num(r.get("Invoice Value")),
        })
    return rows


def _normalize_sto(df: pd.DataFrame) -> list[dict]:
    df = df.drop_duplicates()
    rows = []
    for _, r in df.iterrows():
        packslip_no = _clean(r.get("Pack Slip Number"))
        shipped_date = _clean_date(r.get("Shipped Date"))
        invoice_no = _clean(r.get("Invoice Number"))
        source_wh = _clean(r.get("Source WH")) or "?"
        dest_wh = _clean(r.get("Destination WH")) or "?"
        city = _clean(r.get("City")) or ""
        state = _clean(r.get("State")) or ""
        rows.append({
            "doc_no": _clean(r.get("Stock Transfer Order Number")),
            "item_code": _clean(r.get("Item Code")),
            "doc_type": "STO",
            "doc_date": _clean_date(r.get("Stock Transfer Order Date")),
            "doc_status": _clean(r.get("Stock Transfer Order Status")) or "Fresh",
            "party": f"{source_wh} \u2192 {dest_wh}",
            "place": ", ".join(p for p in (city, state) if p),
            "person": _clean(r.get("Ware House Supervisor")) or _clean(r.get("Sales Person name")),
            "item_desc": _clean(r.get("Item Description")),
            "uom": _clean(r.get("UOM")),
            "ordered_qty": _clean_num(r.get("Ordered Quantity")),
            "required_date": _clean_date(r.get("Required Date")),
            "packslip_no": packslip_no,
            "packslip_status": _derive_packslip_status(packslip_no, shipped_date, r.get("Pack Slip Status")),
            "shipped_date": shipped_date,
            "shipped_qty": _clean_num(r.get("Shipped Quantity")),
            "pending_qty": _clean_num(r.get("Pending Quantity After Packslip")),
            "invoice_no": invoice_no,
            "invoice_status": _derive_invoice_status(invoice_no),
            "invoice_date": _clean_date(r.get("Invoice Date")),
            "invoice_value": _clean_num(r.get("Invoice Value")),
        })
    return rows


def _collapse_lines(rows: list[dict]) -> list[dict]:
    """Collapses multiple sub-lines sharing (doc_no, item_code) into one row:
    quantities/value sum, status-ish fields take the most-advanced sub-line
    (see _PACKSLIP_RANK), packslip/invoice numbers from that same sub-line."""
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["doc_no"], r["item_code"]), []).append(r)

    out = []
    for (doc_no, item_code), lines in groups.items():
        if not doc_no or not item_code:
            continue
        best = max(lines, key=lambda r: (
            _PACKSLIP_RANK.get(r["packslip_status"], 0),
            r["invoice_status"] == "Invoiced",
            r["shipped_date"] or "",
        ))
        merged = dict(best)
        merged["ordered_qty"] = sum(r["ordered_qty"] for r in lines)
        merged["shipped_qty"] = sum(r["shipped_qty"] for r in lines)
        merged["pending_qty"] = sum(r["pending_qty"] for r in lines)
        merged["invoice_value"] = sum(r["invoice_value"] for r in lines)
        out.append(merged)
    return out


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def load_item_master():
    """Refreshes the item_master cache from the ~200k-row Item Master.csv."""
    df = pd.read_csv(ITEM_MASTER_FILE, encoding="utf-8-sig", dtype=str)
    rows = []
    for _, r in df.iterrows():
        code = _clean(r.get("Item Code"))
        if not code:
            continue
        rows.append({
            "item_code": code,
            "variant_code": _clean(r.get("Variant Code")),
            "item_desc": _clean(r.get("Item Variant Desc.")),
            "short_desc": _clean(r.get("Short Description")),
            "category": _clean(r.get("Item Category")),
            "uom": _clean(r.get("Stock UOM")),
            "status": _clean(r.get("Item Status")),
        })
    so_sto_db.replace_item_master(rows)
    return len(rows)


# Fields whose changes become a history event — the mockup's 3-stage pipeline.
_TRACKED_FIELDS = [
    ("doc_status", "Order Status"),
    ("packslip_status", "Packslip Status"),
    ("invoice_status", "Invoice Status"),
]


def _diff_and_log(old_by_key: dict, new_rows: list[dict], occurred_at: str) -> list[dict]:
    events = []
    is_first_sync = not old_by_key
    for row in new_rows:
        key = (row["doc_no"], row["item_code"])
        old = old_by_key.get(key)
        if old is None:
            # New line — only log it if this isn't the very first sync ever
            # (on the first sync everything is "new", which isn't a
            # meaningful event, just the starting baseline).
            if not is_first_sync:
                events.append({
                    "doc_no": row["doc_no"], "doc_type": row["doc_type"], "item_code": row["item_code"],
                    "field": "doc_status", "old_value": None, "new_value": row["doc_status"],
                    "event_text": f"New {row['doc_type']} line appeared \u2014 status {row['doc_status']}",
                    "occurred_at": occurred_at,
                })
            continue
        for field, label in _TRACKED_FIELDS:
            if old.get(field) != row.get(field):
                events.append({
                    "doc_no": row["doc_no"], "doc_type": row["doc_type"], "item_code": row["item_code"],
                    "field": field, "old_value": old.get(field), "new_value": row.get(field),
                    "event_text": f"{label}: {old.get(field) or '\u2014'} \u2192 {row.get(field)}",
                    "occurred_at": occurred_at,
                })
    return events


def sync_dispatch_files() -> dict:
    """The one function that does a full sync: read both Excel files, collapse
    sub-lines, diff against the current snapshot, write history events, then
    replace the snapshot. Safe to call repeatedly (idempotent if nothing
    changed — no events get written when nothing differs)."""
    occurred_at = _now()
    try:
        so_df = pd.read_excel(SO_FILE, engine="xlrd", header=1)
        sto_df = pd.read_excel(STO_FILE, engine="xlrd", header=1)
    except Exception as e:
        so_sto_db.record_sync(0, 0, 0, "error", str(e))
        raise

    so_rows = _collapse_lines(_normalize_so(so_df))
    sto_rows = _collapse_lines(_normalize_sto(sto_df))
    new_rows = so_rows + sto_rows
    for r in new_rows:
        r["synced_at"] = occurred_at

    old_by_key = {(r["doc_no"], r["item_code"]): r for r in so_sto_db.get_snapshot_rows()}
    events = _diff_and_log(old_by_key, new_rows, occurred_at)

    so_sto_db.upsert_snapshot(new_rows)
    so_sto_db.add_history_events(events)
    so_sto_db.record_sync(len(so_rows), len(sto_rows), len(events), "ok")

    return {
        "rows_so": len(so_rows), "rows_sto": len(sto_rows),
        "changes": len(events), "synced_at": occurred_at,
    }
