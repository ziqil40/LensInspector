"""Password-free sign-in.

An inspector types their UCI NetID and is in. There is no password, no email
confirmation and no signup code -- the deployment is expected to sit behind a
link shared with the group, and the NetID exists to attribute grades, not to
authenticate anyone. Treat the results as trusted-group data, not as evidence
of who graded what.

Admins are named up front in ``ADMIN_NETIDS``. If that is left empty the first
person to sign in becomes the admin, so a fresh deployment is never locked out
of its own results pages.
"""

from __future__ import annotations

import functools
import sqlite3
from typing import Any, Callable, Optional

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .db import NETID_RE, get_db, normalise_netid, utcnow

bp = Blueprint("auth", __name__)


def current_inspector() -> Optional[sqlite3.Row]:
    """The signed-in inspector for this request, or ``None``."""
    if "inspector" not in g:
        inspector_id = session.get("inspector_id")
        if inspector_id is None:
            g.inspector = None
        else:
            g.inspector = get_db().execute(
                "SELECT * FROM inspectors WHERE id = ?", (inspector_id,)
            ).fetchone()
            if g.inspector is None:
                # Row was deleted out from under a live session.
                session.clear()
    return g.inspector


def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Send anonymous visitors to the sign-in page."""

    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if current_inspector() is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Restrict a view to admins."""

    @functools.wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        inspector = current_inspector()
        if inspector is None:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if not inspector["is_admin"]:
            return render_template("forbidden.html"), 403
        return view(*args, **kwargs)

    return wrapped


def can_see_group(group: sqlite3.Row, inspector: Optional[sqlite3.Row]) -> bool:
    """Whether this person may see this group at all.

    Only hidden groups restrict anything, and a hidden group belongs to exactly
    one person -- ``owner_netid``. Admin does not grant access: these are scratch
    groups for trying the interface out, and the other admins have no reason to
    see them or their grades. Both blueprints funnel through this, so the normal
    pages, the JSON APIs, the admin dashboard and the CSV exports agree.
    """
    if not group["is_hidden"]:
        return True
    if inspector is None:
        return False
    owner = (group["owner_netid"] or "").strip()
    return bool(owner) and inspector["netid"] == owner


def _should_be_admin(db: sqlite3.Connection, netid: str) -> bool:
    """Whether ``netid`` gets the admin flag when its row is first created."""
    configured = current_app.config.get("ADMIN_NETIDS") or set()
    if configured:
        return netid in configured
    # No admins configured: bootstrap the very first person so the results
    # pages are reachable at all.
    return db.execute("SELECT COUNT(*) AS n FROM inspectors").fetchone()["n"] == 0


@bp.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        typed = request.form.get("netid", "")
        netid = normalise_netid(typed)

        # Errors render against the field itself, and the box keeps what was
        # typed. Flashing instead cleared the input and put the reason in a bar
        # above the card, so a rejected sign-in looked exactly like a fresh page
        # -- graders read that as "the button does nothing" and retried for
        # minutes without ever seeing why.
        error = None
        if not netid:
            error = "Please enter your UCI NetID."
        elif " " in netid:
            error = (
                "That looks like your full name. Your NetID is the short "
                "username you sign in to UCI with \u2014 the part before "
                "@uci.edu, with no spaces, e.g. 'jsmith2'."
            )
        elif not NETID_RE.match(netid):
            error = (
                "That does not look like a UCI NetID. It should be 2-16 letters "
                "and digits starting with a letter, e.g. 'jsmith2'."
            )

        if error is not None:
            return render_template("login.html", error=error, typed=typed.strip())

        db = get_db()
        row = db.execute(
            "SELECT * FROM inspectors WHERE netid = ?", (netid,)
        ).fetchone()

        # A NetID nobody has used before is far more often a typo than a new
        # grader -- and a typo silently creates a second account whose grades
        # are stranded under a name nobody recognises. Confirm once first.
        if row is None and request.form.get("confirm_new") != "yes":
            return render_template(
                "login.html", confirm_netid=netid,
                next=request.args.get("next", ""),
            )

        with db:
            if row is None:
                is_admin = 1 if _should_be_admin(db, netid) else 0
                cur = db.execute(
                    "INSERT INTO inspectors"
                    " (netid, is_admin, created_at, last_seen_at)"
                    " VALUES (?, ?, ?, ?)",
                    (netid, is_admin, utcnow(), utcnow()),
                )
                inspector_id = cur.lastrowid
            else:
                inspector_id = row["id"]
                # Promote if they were added to ADMIN_NETIDS after first use.
                promote = 1 if netid in (current_app.config.get("ADMIN_NETIDS") or set()) else row["is_admin"]
                db.execute(
                    "UPDATE inspectors SET last_seen_at = ?, is_admin = ?"
                    " WHERE id = ?",
                    (utcnow(), promote, inspector_id),
                )

        session.clear()
        session["inspector_id"] = inspector_id
        session.permanent = True

        target = request.args.get("next", "")
        # Only accept same-site relative paths as a redirect target.
        if not target.startswith("/") or target.startswith("//"):
            target = url_for("main.group_list")
        return redirect(target)

    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    flash("Signed out.", "success")
    return redirect(url_for("auth.login"))
