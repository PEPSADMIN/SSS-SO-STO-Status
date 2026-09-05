"""
app.py — SSS (SO / STO Status), standalone
A single-purpose Flask app: log in, check SO/STO Order/Packslip/Invoice
status, track items via the Alerts watchlist. Nothing else — deliberately
independent of the Sales_Mobile hub (own login, own accounts, own process),
so it doesn't touch Sales Dashboard/Orders/Retailer Map/Announcements or
whoever currently relies on those.

Data source: ../Input/Dispatch SO.xls / Dispach STO.xls (relative to the
project root), plus the Item Master CSV for the item watchlist — see
so_sto_ingest.py. Kept fresh by a background thread (_background_sync_loop,
admin-configurable interval, default 20 min) so nobody needs to click Sync
Now; that button still exists for admins who want to force an immediate
refresh (e.g. right after manually dropping a new file onto the D: drive).

Run: python app.py  (dev server, plain HTTP — the Cloudflare Tunnel in front
of this handles public HTTPS, so this app doesn't need its own certs).
"""

import os
import secrets
from pathlib import Path

from flask import Flask, render_template, jsonify, request, redirect, url_for, send_from_directory
from flask_cors import CORS

import sss_auth_db as auth_db
import so_sto_db
import so_sto_ingest
from sss_auth import login_required, admin_required, login_user, logout_user, current_user

APP_DIR = Path(__file__).resolve().parent
PWA_DIR = APP_DIR / "pwa"

app = Flask(__name__)

# On Railway (or anywhere with SECRET_KEY set in the environment), use that —
# it's stable across redeploys, unlike a file on an ephemeral container
# filesystem. Locally, fall back to a generated file so nothing needs to be
# configured to just run `python app.py`.
if os.environ.get("SECRET_KEY"):
    app.secret_key = os.environ["SECRET_KEY"]
else:
    SECRET_KEY_FILE = APP_DIR / "sss_secret.key"
    if SECRET_KEY_FILE.exists():
        app.secret_key = SECRET_KEY_FILE.read_text().strip()
    else:
        key = secrets.token_hex(32)
        SECRET_KEY_FILE.write_text(key)
        app.secret_key = key

CORS(app, supports_credentials=True)


# ── PWA (installable on iOS Safari & Android Chrome) ──
@app.route("/manifest.webmanifest")
def manifest():
    return send_from_directory(PWA_DIR, "manifest.webmanifest", mimetype="application/manifest+json")


@app.route("/sw.js")
def service_worker():
    res = send_from_directory(PWA_DIR, "sw.js", mimetype="application/javascript")
    # Allow the SW to control the whole origin even though the file lives at /sw.js.
    res.headers["Service-Worker-Allowed"] = "/"
    return res

auth_db.init_db()
so_sto_db.init_db()
if so_sto_db.item_master_count() == 0:
    try:
        so_sto_ingest.load_item_master()
    except Exception as e:
        print(f"[SSS] Item Master load failed at startup: {e}")


# ── Auth ──────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = auth_db.verify_login(username, password)
        if user:
            login_user(user)
            next_url = request.args.get("next") or url_for("so_sto_page")
            return redirect(next_url)
        error = "Invalid username or password."
    return render_template("sss_login.html", error=error)


@app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("login"))


# ── Main page ─────────────────────────────────────────
def _render_so_sto():
    user = current_user()
    assert user is not None  # guaranteed by @login_required on every caller
    return render_template(
        "so_sto.html",
        user=user,
        can_manage_production=so_sto_db.is_production_manager(user["username"]),
    )


@app.route("/")
@login_required
def home():
    return _render_so_sto()


@app.route("/so-sto")
@login_required
def so_sto_page():
    return _render_so_sto()


# ── Admin / CMS ───────────────────────────────────────
@app.route("/admin")
@admin_required
def admin_page():
    return render_template(
        "sss_admin.html",
        user=current_user(),
        users=auth_db.list_users(),
        production_managers=so_sto_db.list_production_managers(),
        all_tabs=auth_db.ALL_TABS,
    )


@app.route("/api/admin/users_list")
@admin_required
def admin_users_list():
    return jsonify({"users": auth_db.list_users()})


def _tabs_from_body(body: dict) -> list[str] | None:
    if "tabs" not in body:
        return None
    return [t for t in (body.get("tabs") or []) if t in auth_db.ALL_TABS]


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def admin_add_user():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role_in = body.get("role")
    role = role_in if role_in in ("admin", "user") else "user"
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    if auth_db.get_user(username):
        return jsonify({"error": "that username already exists"}), 400
    auth_db.create_user(username, password, role=role, tabs=_tabs_from_body(body))
    return jsonify({"ok": True}), 201


@app.route("/api/admin/users/<int:user_id>", methods=["PATCH"])
@admin_required
def admin_update_user(user_id):
    body = request.get_json(silent=True) or {}
    role = body.get("role") if body.get("role") in ("admin", "user") else None
    password = body.get("password") or None
    auth_db.update_user(user_id, role=role, password=password, tabs=_tabs_from_body(body))
    return jsonify({"ok": True})


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id):
    auth_db.delete_user(user_id)
    return "", 204


# ── SO / STO API ──────────────────────────────────────
# Friendlier labels for the raw order-lifecycle status, requested in place of
# the ERP's own Fresh/Authorized/Closed/Deleted vocabulary.
_ORDER_STATUS_LABELS = {
    "Fresh": "Not Processed",
    "Deleted": "Cancelled",
    "Short Closed": "Cancelled",
    "Authorized": "Ready to Process",
    "Closed": "Production Ready",
}


def _order_status_label(raw: str) -> str:
    return _ORDER_STATUS_LABELS.get(raw, raw)


def _dispatch_label(order_status_label: str, invoice_status: str, packslip_status: str) -> str:
    """Replaces the plain Invoiced/Not Invoiced wording with the dispatch
    pipeline's own vocabulary: once packed it's "Ready for Dispatch", and
    once invoiced (which is when the gate pass gets raised) it's "Dispatched".
    A cancelled order overrides all of that — packslip/invoice fields are
    stale leftovers on a dead order, not a real dispatch state."""
    if order_status_label == "Cancelled":
        return "Order Cancelled"
    if invoice_status == "Invoiced":
        return "Dispatched"
    if packslip_status == "Shipped":
        return "Ready for Dispatch"
    return invoice_status


def _record_shape(r: dict) -> dict:
    summary = so_sto_db.doc_summary(r["items"])
    order_status = _order_status_label(summary["orderStatus"])
    dispatch_status = _dispatch_label(order_status, summary["invoice"]["status"], summary["packslip"]["status"])
    return {
        "no": r["doc_no"], "type": r["doc_type"], "date": r["doc_date"], "party": r["party"],
        "orderStatus": order_status,
        "orderedQty": summary["orderedQty"],
        "packslip": {"status": summary["packslip"]["status"], "shippedQty": summary["packslip"]["shippedQty"]},
        "invoice": {"status": dispatch_status},
        # The big end-state badge favors the order's own status over the
        # generic "Not Invoiced" — nothing dispatch-wise has happened yet,
        # so where the order stands in processing is the more useful thing
        # to highlight. The filter and the Invoice pill still use the plain
        # dispatch_status above, so "Not Invoiced" stays filterable/accurate.
        "endStatus": order_status if dispatch_status == "Not Invoiced" else dispatch_status,
    }


def _fmt_when(iso: str) -> str:
    try:
        from datetime import datetime as _dt
        return _dt.fromisoformat(iso).strftime("%d %b, %H:%M")
    except Exception:
        return iso


@app.route("/api/so-sto/sync", methods=["POST"])
@login_required
def so_sto_sync():
    try:
        return jsonify(so_sto_ingest.sync_dispatch_files())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/so-sto/upload", methods=["POST"])
@admin_required
def so_sto_upload():
    """Admin-only upload for the source files (Dispatch SO/STO xls, Item
    Master csv) — the cloud-hosted replacement for manually dropping these
    onto the D: drive. Saves the file, then runs the matching sync so the
    upload takes effect immediately."""
    kind = request.form.get("kind", "")
    f = request.files.get("file")
    if kind not in ("so", "sto", "item_master"):
        return jsonify({"error": "kind must be 'so', 'sto', or 'item_master'"}), 400
    if not f or not f.filename:
        return jsonify({"error": "no file uploaded"}), 400
    try:
        so_sto_ingest.save_uploaded_file(kind, f)
        if kind == "item_master":
            result = so_sto_ingest.load_item_master()
        else:
            result = so_sto_ingest.sync_dispatch_files()
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/so-sto/sync-interval", methods=["GET", "POST"])
@admin_required
def so_sto_sync_interval():
    """Admin-configurable background auto-sync cadence — the whole point is
    that nobody, including the admin, should need to remember to hit
    Sync Now; see _background_sync_loop() below."""
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        minutes_raw = body.get("minutes")
        if minutes_raw is None:
            return jsonify({"error": "minutes must be a number"}), 400
        try:
            minutes = int(minutes_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "minutes must be a number"}), 400
        if minutes < so_sto_db.MIN_SYNC_INTERVAL_MINUTES:
            return jsonify({"error": f"minimum is {so_sto_db.MIN_SYNC_INTERVAL_MINUTES} minutes"}), 400
        saved = so_sto_db.set_sync_interval_minutes(minutes)
        return jsonify({"minutes": saved})
    return jsonify({
        "minutes": so_sto_db.get_sync_interval_minutes(),
        "min_minutes": so_sto_db.MIN_SYNC_INTERVAL_MINUTES,
    })


@app.route("/api/so-sto/last-sync")
@login_required
def so_sto_last_sync():
    return jsonify(so_sto_db.last_sync() or {})


def _csv_response(rows: list[dict], fieldnames: list[str], filename: str):
    import csv, io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    for r in rows:
        w.writerow(r)
    out = buf.getvalue().encode("utf-8-sig")  # BOM so Excel reads UTF-8 cleanly
    # response_class is typed as `type[Response]`, but pyright resolves its
    # constructor overload oddly here (flags a correct call either way) —
    # verified against the real werkzeug Response.__init__ signature at runtime.
    return app.response_class(
        response=out,  # pyright: ignore[reportCallIssue]
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/so-sto/export/watchlist")
@login_required
def so_sto_export_watchlist():
    """CSV of the signed-in user's watchlist and every live SO/STO match."""
    user = current_user()
    assert user is not None  # guaranteed by @login_required
    rows = []
    for w in so_sto_db.list_watchlist(user["username"]):
        matches = so_sto_db.matches_for_item_code(w["item_code"])
        if not matches:
            rows.append({
                "item_code": w["item_code"], "item_desc": w["item_desc"], "category": w["item_cat"],
                "qty": w["qty"], "priority": w["priority"], "status": w["status"],
                "match_doc": "", "match_type": "", "order_status": "",
                "packslip_status": "", "invoice_status": "", "invoice_no": "",
            })
        for m in matches:
            rows.append({
                "item_code": w["item_code"], "item_desc": w["item_desc"], "category": w["item_cat"],
                "qty": w["qty"], "priority": w["priority"], "status": w["status"],
                "match_doc": m["doc_no"], "match_type": m["doc_type"], "order_status": m["doc_status"],
                "packslip_status": m["packslip_status"], "invoice_status": m["invoice_status"],
                "invoice_no": m["invoice_no"],
            })
    return _csv_response(
        rows,
        ["item_code", "item_desc", "category", "qty", "priority", "status",
         "match_doc", "match_type", "order_status", "packslip_status", "invoice_status", "invoice_no"],
        "sss_watchlist.csv",
    )


@app.route("/api/so-sto/export/search")
@login_required
def so_sto_export_search():
    """CSV of the current Search results (same query the UI uses)."""
    term = request.args.get("q", "").strip()
    doc_type = request.args.get("type", "ALL")
    docs = so_sto_db.search_docs(term, doc_type)
    rows = []
    for d in docs:
        summary = so_sto_db.doc_summary(d["items"])
        for it in d["items"]:
            rows.append({
                "doc_no": d["doc_no"], "doc_type": d["doc_type"], "doc_date": d["doc_date"],
                "party": d["party"], "place": d["place"], "person": d["person"],
                "item_code": it["item_code"], "item_desc": it["item_desc"],
                "ordered_qty": it["ordered_qty"],
                "order_status": summary["orderStatus"],
                "packslip_status": summary["packslip"]["status"],
                "packslip_no": it["packslip_no"],
                "invoice_status": summary["invoice"]["status"],
                "invoice_no": it["invoice_no"],
            })
    return _csv_response(
        rows,
        ["doc_no", "doc_type", "doc_date", "party", "place", "person",
         "item_code", "item_desc", "ordered_qty", "order_status",
         "packslip_status", "packslip_no", "invoice_status", "invoice_no"],
        "sss_search_export.csv",
    )


@app.route("/api/so-sto/summary")
@login_required
def so_sto_summary():
    start = request.args.get("start", "0000-01-01")
    end = request.args.get("end", "9999-12-31")
    return jsonify(so_sto_db.summary_for_range(start, end))


@app.route("/api/so-sto/search")
@login_required
def so_sto_search():
    term = request.args.get("q", "").strip()
    doc_type = request.args.get("type", "ALL")
    docs = so_sto_db.search_docs(term, doc_type)
    records = [_record_shape(d) for d in docs]
    statuses = {s for s in request.args.get("status", "").split(",") if s}
    if statuses:
        records = [r for r in records if r["invoice"]["status"] in statuses]
    order_statuses = {s for s in request.args.get("orderStatus", "").split(",") if s}
    if order_statuses:
        records = [r for r in records if r["orderStatus"] in order_statuses]
    return jsonify({"records": records})


@app.route("/api/so-sto/doc/<path:doc_no>")
@login_required
def so_sto_doc(doc_no):
    d = so_sto_db.get_doc(doc_no)
    if not d:
        return jsonify({"error": "not found"}), 404
    summary = so_sto_db.doc_summary(d["items"])
    history = [
        {"when": _fmt_when(h["occurred_at"]), "what": h["event_text"]}
        for h in so_sto_db.history_for_doc(doc_no)
    ]
    order_status = _order_status_label(summary["orderStatus"])
    invoice = dict(summary["invoice"])
    invoice["status"] = _dispatch_label(order_status, invoice["status"], summary["packslip"]["status"])
    return jsonify({
        "no": d["doc_no"], "type": d["doc_type"], "date": d["doc_date"], "required": d["required_date"],
        "party": d["party"], "partyRole": "Route" if d["doc_type"] == "STO" else "Customer",
        "place": d["place"], "person": d["person"],
        "orderStatus": order_status, "orderedQty": summary["orderedQty"],
        "packslip": summary["packslip"], "invoice": invoice,
        "items": [{"code": it["item_code"], "name": it["item_desc"], "qty": it["ordered_qty"]} for it in d["items"]],
        "history": history,
    })


@app.route("/api/so-sto/items")
@login_required
def so_sto_items():
    term = request.args.get("q", "").strip()
    rows = so_sto_db.search_items(term, limit=30)
    return jsonify({"items": [
        {"code": r["item_code"], "desc": r["item_desc"], "cat": r["category"], "uom": r["uom"]} for r in rows
    ]})


@app.route("/api/so-sto/watchlist", methods=["GET", "POST"])
@login_required
def so_sto_watchlist():
    user = current_user()
    assert user is not None  # guaranteed by @login_required
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        code = (body.get("code") or "").strip()
        if not code:
            return jsonify({"error": "item code is required"}), 400
        watch_id = so_sto_db.add_watch(
            user["username"], code, body.get("desc"), body.get("cat"),
            body.get("qty"), body.get("priority") or "Normal",
        )
        return jsonify({"id": watch_id}), 201

    out = []
    for w in so_sto_db.list_watchlist(user["username"]):
        matches = so_sto_db.matches_for_item_code(w["item_code"])
        out.append({
            "id": w["id"], "code": w["item_code"], "desc": w["item_desc"], "cat": w["item_cat"],
            "qty": w["qty"], "priority": w["priority"], "status": w["status"],
            "matches": [{
                "no": m["doc_no"], "type": m["doc_type"], "orderStatus": m["doc_status"],
                "packslipStatus": m["packslip_status"], "invoiceStatus": m["invoice_status"],
                "invoiceNo": m["invoice_no"],
            } for m in matches],
        })
    return jsonify({"watchlist": out})


@app.route("/api/so-sto/watchlist/<int:watch_id>", methods=["DELETE"])
@login_required
def so_sto_watchlist_delete(watch_id):
    user = current_user()
    assert user is not None  # guaranteed by @login_required
    ok = so_sto_db.remove_watch(watch_id, user["username"])
    return ("", 204) if ok else (jsonify({"error": "not found"}), 404)


def _require_production_manager(user):
    return so_sto_db.is_production_manager(user["username"])


@app.route("/api/so-sto/watchlist/<int:watch_id>/produce", methods=["POST"])
@login_required
def so_sto_watchlist_produce(watch_id):
    user = current_user()
    assert user is not None  # guaranteed by @login_required
    if not _require_production_manager(user):
        return jsonify({"error": "production manager only"}), 403
    ok = so_sto_db.set_watch_status(watch_id, "Produced", by_user=user["username"])
    return jsonify({"ok": True}) if ok else (jsonify({"error": "not found"}), 404)


@app.route("/api/so-sto/watchlist/<int:watch_id>/reopen", methods=["POST"])
@login_required
def so_sto_watchlist_reopen(watch_id):
    user = current_user()
    if not _require_production_manager(user):
        return jsonify({"error": "production manager only"}), 403
    ok = so_sto_db.set_watch_status(watch_id, "Open")
    return jsonify({"ok": True}) if ok else (jsonify({"error": "not found"}), 404)


@app.route("/api/so-sto/production-managers", methods=["GET", "POST"])
@admin_required
def so_sto_production_managers():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        username = (body.get("username") or "").strip()
        if not username:
            return jsonify({"error": "username is required"}), 400
        granter = current_user()
        assert granter is not None  # guaranteed by @admin_required
        so_sto_db.grant_production_manager(username, granter["username"])
        return jsonify({"ok": True}), 201
    return jsonify({"production_managers": so_sto_db.list_production_managers()})


@app.route("/api/so-sto/production-managers/<username>", methods=["DELETE"])
@admin_required
def so_sto_production_managers_revoke(username):
    so_sto_db.revoke_production_manager(username)
    return "", 204


def _background_sync_loop():
    """Runs for the lifetime of the process so nobody — including the admin —
    has to remember to click Sync Now. Interval is re-read from app_settings
    on every cycle, so a change made via /api/so-sto/sync-interval takes
    effect starting with the next sleep rather than requiring a restart."""
    import time
    while True:
        try:
            so_sto_ingest.sync_dispatch_files()
        except Exception as e:
            print(f"[SSS] background sync failed: {e}", flush=True)
        interval_minutes = so_sto_db.get_sync_interval_minutes()
        time.sleep(interval_minutes * 60)


def _lan_ip():
    # No real network traffic — just asks the OS which local interface
    # it would use to reach an outside address, to find the LAN IP other
    # devices on the same network should hit instead of localhost.
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


if __name__ == "__main__":
    # Railway (and most hosts) assign the port via $PORT — 5050 is just the
    # local-dev default when nothing sets it.
    PORT = int(os.environ.get("PORT", 5050))
    lan_ip = _lan_ip()
    print("\n" + "-" * 60)
    print("  SSS — SO / STO Status")
    print(f"  Open (this PC) : http://localhost:{PORT}")
    if lan_ip:
        print(f"  Open (network) : http://{lan_ip}:{PORT}")
    print("-" * 60 + "\n")
    # stdout is block-buffered (not line-buffered) once redirected to a file,
    # as NSSM does for this service's log — without an explicit flush, this
    # banner can sit unflushed in memory and vanish if the process is later
    # killed by a service restart, rather than exiting cleanly.
    import sys
    sys.stdout.flush()

    import threading
    threading.Thread(target=_background_sync_loop, daemon=True).start()

    import signal, sys

    def _graceful_exit(signum, frame):
        sys.exit(0)

    # NSSM/service stop delivers SIGTERM (or Ctrl-C); exit cleanly so no
    # orphaned python process is left holding the port between restarts.
    try:
        signal.signal(signal.SIGTERM, _graceful_exit)
    except (ValueError, OSError, AttributeError):
        pass

    from waitress import serve
    try:
        serve(app, host="0.0.0.0", port=PORT)
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.flush()
