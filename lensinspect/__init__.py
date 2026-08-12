"""Standalone lens inspection app.

A small Flask app whose only job is letting a group of people grade cutouts:

* people sign in with a UCI NetID, no password
* candidates are organised into **groups**, each worked through independently
* anything you are unsure about is **skipped** and comes back in a review pass
* when you are done you **submit**, and the organiser exports the results

Run it with ``python run_inspector.py`` or ``flask --app lensinspect run``.

Configuration, all from the environment:

=============================  ===========================================
``INSPECTOR_DB``               SQLite file (default ``data/inspector.db``)
``CUTOUT_DIR``                 fallback cutout directory for groups that
                               do not set their own
``INSPECTOR_UPLOAD_DIR``       where uploaded cutout archives are unpacked
``ADMIN_NETIDS``               comma-separated NetIDs that get admin rights;
                               if unset, the first person to sign in does
``SECRET_KEY``                 session signing key -- set it, or every
                               restart signs everybody out
``INSPECTOR_MAX_UPLOAD_MB``    upload size ceiling (default 512)
=============================  ===========================================
"""

from __future__ import annotations

import os
import secrets
from datetime import timedelta
from typing import Any, Optional

from flask import Flask, render_template

from . import admin, aggregate, auth, db, ingest, views

__all__ = ["create_app", "admin", "aggregate", "auth", "db", "ingest", "views"]

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _admin_netids() -> set[str]:
    raw = os.environ.get("ADMIN_NETIDS", "")
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def create_app(config: Optional[dict[str, Any]] = None) -> Flask:
    app = Flask(__name__)

    app.config["DB_PATH"] = os.environ.get(
        "INSPECTOR_DB", os.path.join(BASE_DIR, "data", "inspector.db")
    )
    app.config["CUTOUT_DIR"] = os.path.abspath(
        os.environ.get("CUTOUT_DIR", os.path.join(BASE_DIR, "cutouts"))
    )
    app.config["UPLOAD_IMAGE_DIR"] = os.path.abspath(
        os.environ.get(
            "INSPECTOR_UPLOAD_DIR", os.path.join(BASE_DIR, "data", "group_images")
        )
    )
    app.config["ADMIN_NETIDS"] = _admin_netids()
    app.config["MAX_CONTENT_LENGTH"] = (
        int(os.environ.get("INSPECTOR_MAX_UPLOAD_MB", "512")) * 1024 * 1024
    )

    # Fine for local use; on a deployment this must be set or every restart
    # signs everybody out. run_inspector.py warns when it is missing.
    app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=60)
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

    if config:
        app.config.update(config)

    os.makedirs(app.config["UPLOAD_IMAGE_DIR"], exist_ok=True)

    db.init_app(app)
    app.register_blueprint(auth.bp)
    app.register_blueprint(views.bp)
    app.register_blueprint(admin.bp)

    @app.context_processor
    def _inject_inspector() -> dict[str, Any]:
        return {"inspector": auth.current_inspector()}

    @app.errorhandler(404)
    def _not_found(_exc: Any):
        return render_template("error.html", code=404,
                               message="That page does not exist."), 404

    @app.errorhandler(413)
    def _too_large(_exc: Any):
        limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
        return render_template(
            "error.html",
            code=413,
            message=f"That upload is larger than the {limit} MB limit. Split it "
                    "up, or raise INSPECTOR_MAX_UPLOAD_MB and restart.",
        ), 413

    return app
