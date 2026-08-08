"""
app.py — SSS (SO / STO Status), standalone
A single-purpose Flask app: log in, check SO/STO Order/Packslip/Invoice
status, track items via the Alerts watchlist. Nothing else — deliberately
independent of the Sales_Mobile hub (own login, own accounts, own process),
so it doesn't touch Sales Dashboard/Orders/Retailer Map/Announcements or
whoever currently relies on those.

Data source: D:\\Hari JR. DATA\\Development\\SO\\Input\\Dispatch SO.xls /
Dispach STO.xls (manually refreshed, read fresh on "Sync Now" — see
so_sto_ingest.py), plus the Item Master CSV for the item watchlist.

Run: python app.py  (dev server, plain HTTP — the Cloudflare Tunnel in front
of this handles public HTTPS, so this app doesn't need its own certs).
"""

import os
import secrets
from pathlib import Path

from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_cors import CORS

import sss_auth_db as auth_db
import so_sto_db
import so_sto_ingest
from sss_auth import login_required, admin_required, login_user, logout_user, current_user

APP_DIR = Path(__file__).resolve().parent

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
    return render_template(
        "so_sto.html",
        user=user,
        can_manage_production=so_sto_db.is_production_manager(user["username"]),
        is_developer=so_sto_db.is_developer(user["username"]),
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
        developers=so_sto_db.list_developers(),
    )


@app.route("/api/admin/users_list")
@admin_required
def admin_users_list():
    return jsonify({"users": auth_db.list_users()})


@app.route("/api/admin/users", methods=["POST"])
@admin_required
def admin_add_user():
    body = request.get_json(silent=True) or {}
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    role = body.get("role") if body.get("role") in ("admin", "user") else "user"
    if not username or not password:
        return jsonify({"error": "username and password are required"}), 400
    if auth_db.get_user(username):
        return jsonify({"error": "that username already exists"}), 400
    auth_db.create_user(username, password, role=role)
    return jsonify({"ok": True}), 201


@app.route("/api/admin/users/<int:user_id>", methods=["DELETE"])
@admin_required
def admin_delete_user(user_id):
    auth_db.delete_user(user_id)
    return "", 204


# ── SO / STO API ──────────────────────────────────────
def _record_shape(r: dict) -> dict:
    summary = so_sto_db.doc_summary(r["items"])
    return {
        "no": r["doc_no"], "type": r["doc_type"], "date": r["doc_date"], "party": r["party"],
        "orderStatus": summary["orderStatus"],
        "packslip": {"status": summary["packslip"]["status"]},
        "invoice": {"status": summary["invoice"]["status"]},
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


@app.route("/api/so-sto/last-sync")
@login_required
def so_sto_last_sync():
    return jsonify(so_sto_db.last_sync() or {})


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
    return jsonify({"records": [_record_shape(d) for d in docs]})


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
    return jsonify({
        "no": d["doc_no"], "type": d["doc_type"], "date": d["doc_date"], "required": d["required_date"],
        "party": d["party"], "partyRole": "Route" if d["doc_type"] == "STO" else "Customer",
        "place": d["place"], "person": d["person"],
        "orderStatus": summary["orderStatus"], "orderedQty": summary["orderedQty"],
        "packslip": summary["packslip"], "invoice": summary["invoice"],
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
    ok = so_sto_db.remove_watch(watch_id, user["username"])
    return ("", 204) if ok else (jsonify({"error": "not found"}), 404)


def _require_production_manager(user):
    return so_sto_db.is_production_manager(user["username"])


@app.route("/api/so-sto/watchlist/<int:watch_id>/produce", methods=["POST"])
@login_required
def so_sto_watchlist_produce(watch_id):
    user = current_user()
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
        so_sto_db.grant_production_manager(username, current_user()["username"])
        return jsonify({"ok": True}), 201
    return jsonify({"production_managers": so_sto_db.list_production_managers()})


@app.route("/api/so-sto/production-managers/<username>", methods=["DELETE"])
@admin_required
def so_sto_production_managers_revoke(username):
    so_sto_db.revoke_production_manager(username)
    return "", 204


@app.route("/api/so-sto/developers", methods=["GET", "POST"])
@admin_required
def so_sto_developers():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        username = (body.get("username") or "").strip()
        if not username:
            return jsonify({"error": "username is required"}), 400
        so_sto_db.grant_developer(username, current_user()["username"])
        return jsonify({"ok": True}), 201
    return jsonify({"developers": so_sto_db.list_developers()})


@app.route("/api/so-sto/developers/<username>", methods=["DELETE"])
@admin_required
def so_sto_developers_revoke(username):
    so_sto_db.revoke_developer(username)
    return "", 204


if __name__ == "__main__":
    # Railway (and most hosts) assign the port via $PORT — 5009 is just the
    # local-dev default when nothing sets it.
    PORT = int(os.environ.get("PORT", 5009))
    print("\n" + "-" * 60)
    print("  SSS — SO / STO Status")
    print(f"  Open : http://localhost:{PORT}")
    print("-" * 60 + "\n")

    from waitress import serve
    serve(app, host="0.0.0.0", port=PORT)
