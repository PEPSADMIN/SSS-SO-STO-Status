"""
sss_auth.py — SSS (SO/STO Status)
Minimal session-based login, same shape as Sales_Mobile's auth.py but backed
by sss_auth_db.py's own accounts — SSS's login is independent on purpose.
"""

from functools import wraps
from flask import session, redirect, url_for, request, jsonify

import sss_auth_db as auth_db


def login_user(user: dict):
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["role"] = user["role"]


def logout_user():
    session.clear()


def current_user():
    user_id = session.get("user_id")
    if not user_id:
        return None
    return auth_db.get_user_by_id(user_id)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "login required"}), 401
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "login required"}), 401
            return redirect(url_for("login", next=request.path))
        user = current_user()
        if not user or user["role"] != "admin":
            if request.path.startswith("/api/"):
                return jsonify({"error": "admin only"}), 403
            return redirect(url_for("so_sto_page"))
        return view(*args, **kwargs)
    return wrapped
