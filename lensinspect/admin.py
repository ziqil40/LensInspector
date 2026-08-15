"""Organiser pages: watch progress, manage groups, export results.

Groups are deliberately not creatable from the web UI. Loading a candidate
list is an operation for whoever runs the deployment, so it lives in the
``lensinspect.ingest`` command line instead, which needs server access::

    python -m lensinspect.ingest list.csv --group "Round 2" --image-dir /path/to/pngs
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any, Optional

from flask import (
    Blueprint,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import aggregate, ingest
from .auth import admin_required, can_see_group, current_inspector
from .db import GRADES, get_db, group_by_slug

bp = Blueprint("admin", __name__, url_prefix="/admin")


def _group_or_404(slug: str) -> sqlite3.Row:
    group = group_by_slug(get_db(), slug)
    if group is None:
        abort(404)
    # Being an admin is not enough for a hidden group: it belongs to one person,
    # and that includes its detail page, its settings and its CSV exports.
    if not can_see_group(group, current_inspector()):
        abort(404)
    return group


# --------------------------------------------------------------------------
# Group management
# --------------------------------------------------------------------------


@bp.route("/")
@admin_required
def dashboard():
    db = get_db()
    inspector = current_inspector()
    groups = [g for g in aggregate.group_overview(db) if can_see_group(g, inspector)]
    for group in groups:
        group["summary"] = aggregate.group_summary(db, group["id"])
    return render_template("admin/dashboard.html", groups=groups, inspector=inspector)


@bp.route("/groups/<slug>/add", methods=("POST",))
@admin_required
def add_candidates(slug: str):
    """Append more candidates to an existing group from another CSV."""
    db = get_db()
    group = _group_or_404(slug)
    upload = request.files.get("candidates")

    if upload is None or not upload.filename:
        flash("Choose a CSV file to add.", "error")
        return redirect(url_for("admin.group_detail", slug=slug))

    try:
        text = upload.read().decode("utf-8-sig", errors="replace")
        rows = ingest.read_candidates(text)
    except (ValueError, UnicodeDecodeError) as exc:
        flash(f"Could not read that CSV: {exc}", "error")
        return redirect(url_for("admin.group_detail", slug=slug))

    stats = ingest.insert_candidates(db, group["id"], rows)
    flash(
        f"Added {stats['inserted']} new candidates "
        f"({stats['skipped_existing']} were already in this group).",
        "success",
    )
    return redirect(url_for("admin.group_detail", slug=slug))


@bp.route("/groups/<slug>/settings", methods=("POST",))
@admin_required
def group_settings(slug: str):
    db = get_db()
    group = _group_or_404(slug)

    name = request.form.get("name", "").strip() or group["name"]
    description = request.form.get("description", "").strip() or None
    image_dir = request.form.get("image_dir", "").strip() or None
    is_open = 1 if request.form.get("is_open") else 0
    shuffle_order = 1 if request.form.get("shuffle_order") else 0
    try:
        max_votes = max(0, int(request.form.get("max_votes", "0") or 0))
    except ValueError:
        max_votes = group["max_votes"] or 0

    with db:
        db.execute(
            "UPDATE groups SET name = ?, description = ?, image_dir = ?, is_open = ?,"
            " shuffle_order = ?, max_votes = ? WHERE id = ?",
            (name, description, image_dir, is_open, shuffle_order, max_votes,
             group["id"]),
        )
    flash("Group updated.", "success")
    return redirect(url_for("admin.group_detail", slug=slug))


@bp.route("/groups/<slug>/delete", methods=("POST",))
@admin_required
def delete_group(slug: str):
    db = get_db()
    group = _group_or_404(slug)

    # Deleting cascades into candidates, votes and submissions, so make the
    # organiser type the name out rather than lose a round to a stray click.
    if request.form.get("confirm", "").strip() != group["name"]:
        flash(
            "Group not deleted - type the group's name exactly to confirm.", "error"
        )
        return redirect(url_for("admin.group_detail", slug=slug))

    with db:
        db.execute("DELETE FROM groups WHERE id = ?", (group["id"],))
    flash(f"Deleted '{group['name']}' and every grade recorded against it.", "success")
    return redirect(url_for("admin.dashboard"))


# --------------------------------------------------------------------------
# People
# --------------------------------------------------------------------------


@bp.route("/people")
@admin_required
def people():
    """Everyone who has ever signed in, with how much they have actually done."""
    db = get_db()
    rows = db.execute(
        """
        SELECT i.id, i.netid, i.display_name, i.is_admin, i.created_at, i.last_seen_at,
               (SELECT COUNT(*) FROM votes v WHERE v.inspector_id = i.id) AS n_votes,
               (SELECT COUNT(*) FROM submissions s WHERE s.inspector_id = i.id) AS n_submitted
        FROM inspectors i
        ORDER BY n_votes DESC, i.netid
        """
    ).fetchall()
    return render_template(
        "admin/people.html", people=rows, inspector=current_inspector()
    )


@bp.route("/people/<netid>/delete", methods=("POST",))
@admin_required
def delete_inspector(netid: str):
    """Remove an account -- typically one created by a typo and never used."""
    db = get_db()
    me = current_inspector()
    row = db.execute("SELECT * FROM inspectors WHERE netid = ?", (netid,)).fetchone()
    if row is None:
        abort(404)

    # Deleting yourself would drop the only admin and lock the results away.
    if row["id"] == me["id"]:
        flash("You cannot delete your own account.", "error")
        return redirect(url_for("admin.people"))

    n_votes = db.execute(
        "SELECT COUNT(*) AS n FROM votes WHERE inspector_id = ?", (row["id"],)
    ).fetchone()["n"]

    # Votes and submissions cascade, so an account with real work behind it takes
    # its grades with it. Typing the NetID is the guard for that; an account that
    # never graded anything is a typo and goes without ceremony.
    if n_votes and request.form.get("confirm", "").strip() != row["netid"]:
        flash(
            f"'{row['netid']}' has {n_votes} grade{'' if n_votes == 1 else 's'}. "
            "Type the NetID exactly to confirm deleting the account and its grades.",
            "error",
        )
        return redirect(url_for("admin.people"))

    with db:
        db.execute("DELETE FROM inspectors WHERE id = ?", (row["id"],))
    flash(
        f"Deleted '{row['netid']}'"
        + (f" and {n_votes} grade{'' if n_votes == 1 else 's'}." if n_votes else "."),
        "success",
    )
    return redirect(url_for("admin.people"))


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------


@bp.route("/groups/<slug>")
@admin_required
def group_detail(slug: str):
    db = get_db()
    group = _group_or_404(slug)

    # The per-object consensus table is not rendered here any more -- it lives in
    # the CSV download, which has every row rather than the first 500. Building it
    # just to throw it away costs a scan of every vote in the group.
    return render_template(
        "admin/group.html",
        group=group,
        inspector=current_inspector(),
        summary=aggregate.group_summary(db, group["id"]),
        people=aggregate.group_inspectors(db, group["id"]),
        grades=GRADES,
    )


def _csv_response(fieldnames: Any, records: Any, filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(fieldnames))
    writer.writeheader()
    for record in records:
        writer.writerow(record)
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/groups/<slug>/consensus.csv")
@admin_required
def export_consensus(slug: str):
    """One row per object: the combined verdict across everyone."""
    db = get_db()
    group = _group_or_404(slug)
    only_conflicts = request.args.get("conflicts", "") == "1"
    try:
        min_votes = int(request.args.get("min_votes", 1))
    except ValueError:
        min_votes = 1

    rows = aggregate.consensus_rows(
        db, group["id"], only_conflicts=only_conflicts, min_votes=min_votes
    )
    name = f"{group['slug']}_consensus" + ("_conflicts" if only_conflicts else "")
    return _csv_response(
        aggregate.CONSENSUS_COLUMNS, aggregate.consensus_records(rows), f"{name}.csv"
    )


@bp.route("/groups/<slug>/votes.csv")
@admin_required
def export_votes(slug: str):
    """One row per person per object: the raw, unaggregated grades."""
    db = get_db()
    group = _group_or_404(slug)
    return _csv_response(
        aggregate.VOTE_COLUMNS,
        aggregate.vote_records(db, group["id"]),
        f"{group['slug']}_votes.csv",
    )


@bp.route("/groups/<slug>/votes/<netid>.csv")
@admin_required
def export_votes_for(slug: str, netid: str):
    """One person's grades for this group, as their own file.

    Always available: grades count from the moment they are cast, so there is no
    "finished" state to wait for. The file is a snapshot of where they are now.
    """
    db = get_db()
    group = _group_or_404(slug)

    inspector = db.execute(
        "SELECT id, netid FROM inspectors WHERE netid = ?", (netid,)
    ).fetchone()
    if inspector is None:
        abort(404)


    return _csv_response(
        aggregate.VOTE_COLUMNS,
        aggregate.vote_records(db, group["id"], netid=inspector["netid"]),
        f"{group['slug']}_{inspector['netid']}.csv",
    )
