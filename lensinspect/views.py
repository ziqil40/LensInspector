"""Everything an inspector sees: group picker, grading, the unsure pile, progress.

There is no submit step. A grade is recorded the moment the key is pressed, so a
part-finished group is still 1,000 usable grades rather than nothing -- waiting for
someone to reach the end before their work counts throws away real data. Nothing is
ever locked; people may revise an answer whenever they like.
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Optional

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from . import aggregate
from .auth import can_see_group, current_inspector, login_required
from .db import (
    ALL_GRADES,
    GRADE_DESCRIPTIONS,
    GRADE_LABELS,
    GRADES,
    SKIP,
    get_db,
    group_by_slug,
    row_extra,
    utcnow,
)

bp = Blueprint("main", __name__)

#: The review pass hands the whole outstanding skip list to the browser in one
#: response. Anything beyond this is picked up on the next fetch.
REVIEW_BATCH = 200


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _group_or_404(slug: str) -> sqlite3.Row:
    group = group_by_slug(get_db(), slug)
    if group is None:
        abort(404)
    # Every group page resolves through here -- the queue, voting, images, summary,
    # submit and restart included -- so leaving a hidden group off the landing page
    # is not on its own enough: the URL would still work.
    if not can_see_group(group, current_inspector()):
        abort(404)
    return group


def _image_dir(group: sqlite3.Row) -> str:
    """Where this group's cutouts live, falling back to the global default."""
    configured = (group["image_dir"] or "").strip()
    if configured:
        return os.path.abspath(configured)
    return current_app.config["CUTOUT_DIR"]


def _has_image(group: sqlite3.Row, cutoutname: str) -> bool:
    return os.path.isfile(os.path.join(_image_dir(group), f"{cutoutname}.png"))


def _payload(group: sqlite3.Row, row: sqlite3.Row) -> dict[str, Any]:
    item = {
        "cutoutname": row["cutoutname"],
        "objname": row["objname"] or "",
        "ra": row["ra"],
        "dec": row["dec"],
        "score": row["score"],
        "rank": row["rank"],
        "has_image": _has_image(group, row["cutoutname"]),
        "extra": row_extra(row),
        "my_vote": None,
    }
    keys = row.keys()
    if "my_grade" in keys and row["my_grade"] is not None:
        item["my_vote"] = {
            "grade": row["my_grade"],
            "flagged": bool(row["my_flagged"]) if "my_flagged" in keys else False,
            "note": (row["my_note"] if "my_note" in keys else None) or "",
        }
    return item


# --------------------------------------------------------------------------
# Landing
# --------------------------------------------------------------------------


@bp.route("/")
@login_required
def group_list():
    db = get_db()
    inspector = current_inspector()
    groups = [g for g in aggregate.group_overview(db) if can_see_group(g, inspector)]
    for group in groups:
        group["mine"] = aggregate.my_progress(db, group["id"], inspector["id"])
    return render_template("groups.html", groups=groups, inspector=inspector)


@bp.route("/guide")
def guide():
    return render_template("guide.html", grade_labels=GRADE_LABELS,
                           grade_descriptions=GRADE_DESCRIPTIONS, grades=GRADES)


@bp.route("/progress")
@login_required
def progress_page():
    """Your own progress, for anyone signed in.

    Deliberately *only* progress: counts of what you have done and how far the
    group has got overall. No grades, nobody else's answers, and no exports --
    seeing how other people voted would bias the independent second opinion the
    whole exercise depends on.
    """
    db = get_db()
    inspector = current_inspector()
    groups = [g for g in aggregate.group_overview(db) if can_see_group(g, inspector)]
    rows = []
    for group in groups:
        mine = aggregate.my_progress(db, group["id"], inspector["id"])
        rows.append({"group": group, "mine": mine})
    return render_template("progress.html", rows=rows, inspector=inspector)


# There is deliberately no "how to reach this site" page here. Connection
# instructions cannot work from inside the app: someone who is not yet on
# Tailscale cannot load any page to read how to get on Tailscale. They live in
# Lens_Inspector_Access.docx (built by make_access_doc.py) and are emailed with
# the Tailscale invite instead.


# --------------------------------------------------------------------------
# Grading
# --------------------------------------------------------------------------


@bp.route("/g/<slug>/")
@login_required
def inspect_page(slug: str):
    db = get_db()
    group = _group_or_404(slug)
    inspector = current_inspector()

    phase = request.args.get("phase", "main")
    if phase not in ("main", "review"):
        phase = "main"

    progress = aggregate.my_progress(db, group["id"], inspector["id"])
    # An example group is only a *tutorial* if its objects carry known answers.
    # Without them showVerdict() no-ops and the run behaves like a normal group,
    # so the coaching intro would be promising feedback that never arrives.
    has_answers = bool(
        db.execute(
            "SELECT 1 FROM candidates WHERE group_id = ?"
            " AND extra LIKE '%\"_answer\"%' LIMIT 1",
            (group["id"],),
        ).fetchone()
    )
    return render_template(
        "inspect.html",
        group=group,
        inspector=inspector,
        phase=phase,
        grades=GRADES,
        grade_labels=GRADE_LABELS,
        progress=progress,
        locked=not group["is_open"],
        has_answers=has_answers,
    )


@bp.route("/g/<slug>/api/queue")
@login_required
def api_queue(slug: str):
    """The next objects for this person.

    ``phase=main``   objects they have not looked at yet, in list order.
    ``phase=review`` objects they marked unsure, so nothing is left undecided.
    """
    db = get_db()
    group = _group_or_404(slug)
    inspector = current_inspector()

    phase = request.args.get("phase", "main")
    try:
        limit = max(1, min(100, int(request.args.get("n", 25))))
    except ValueError:
        limit = 25

    if phase == "review":
        rows = db.execute(
            """
            SELECT c.*, v.grade AS my_grade, v.flagged AS my_flagged, v.note AS my_note
            FROM candidates c
            JOIN votes v ON v.candidate_id = c.id AND v.inspector_id = ?
            WHERE c.group_id = ? AND v.grade = ?
            ORDER BY c.rank IS NULL, c.rank ASC, c.score DESC, c.id ASC
            LIMIT ?
            """,
            (inspector["id"], group["id"], SKIP, REVIEW_BATCH),
        ).fetchall()
    else:
        rows = db.execute(
            """
            SELECT c.*, NULL AS my_grade, NULL AS my_flagged, NULL AS my_note
            FROM candidates c
            WHERE c.group_id = ?
              AND NOT EXISTS (
                    SELECT 1 FROM votes v
                     WHERE v.candidate_id = c.id AND v.inspector_id = ?
              )
            ORDER BY c.rank IS NULL, c.rank ASC, c.score DESC, c.id ASC
            LIMIT ?
            """,
            (group["id"], inspector["id"], limit),
        ).fetchall()

    return jsonify(
        {
            "candidates": [_payload(group, row) for row in rows],
            "progress": aggregate.my_progress(db, group["id"], inspector["id"]),
        }
    )


@bp.route("/g/<slug>/api/vote", methods=("POST",))
@login_required
def api_vote(slug: str):
    db = get_db()
    group = _group_or_404(slug)
    inspector = current_inspector()

    if not group["is_open"]:
        return jsonify({"error": "This group has been closed by the organiser."}), 403

    payload = request.get_json(silent=True) or {}
    cutoutname = str(payload.get("cutoutname", "")).strip()
    grade = str(payload.get("grade", "")).strip().upper()
    note = str(payload.get("note", "")).strip() or None
    flagged = 1 if payload.get("flagged") else 0

    if grade not in ALL_GRADES:
        return jsonify({"error": f"Unknown grade {grade!r}."}), 400

    candidate = db.execute(
        "SELECT id FROM candidates WHERE group_id = ? AND cutoutname = ?",
        (group["id"], cutoutname),
    ).fetchone()
    if candidate is None:
        return jsonify({"error": f"Unknown object {cutoutname!r}."}), 404

    now = utcnow()
    with db:
        db.execute(
            """
            INSERT INTO votes
                (candidate_id, inspector_id, grade, flagged, note, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (candidate_id, inspector_id) DO UPDATE SET
                grade      = excluded.grade,
                flagged    = excluded.flagged,
                note       = excluded.note,
                updated_at = excluded.updated_at
            """,
            (candidate["id"], inspector["id"], grade, flagged, note, now, now),
        )

    return jsonify(
        {"ok": True, "progress": aggregate.my_progress(db, group["id"], inspector["id"])}
    )


# --------------------------------------------------------------------------
# Summary and submission
# --------------------------------------------------------------------------


@bp.route("/g/<slug>/summary")
@login_required
def summary_page(slug: str):
    db = get_db()
    group = _group_or_404(slug)
    inspector = current_inspector()
    progress = aggregate.my_progress(db, group["id"], inspector["id"])
    return render_template(
        "summary.html",
        group=group,
        inspector=inspector,
        progress=progress,
        locked=not group["is_open"],
        grades=GRADES,
        grade_labels=GRADE_LABELS,
    )


@bp.route("/g/<slug>/restart", methods=("POST",))
@login_required
def restart(slug: str):
    """Clear this person's answers for a practice group so they can run it again.

    Only example groups may be reset: the practice set is a teaching tool people
    are meant to repeat until the examples stop surprising them, whereas wiping a
    real group would destroy grading effort with one click. Real groups are
    reopened by an admin instead, which keeps the votes.
    """
    db = get_db()
    group = _group_or_404(slug)
    inspector = current_inspector()

    if not (group["is_example"] or group["is_sandbox"]):
        flash("Only the practice and trial groups can be restarted.", "error")
        return redirect(url_for("main.summary_page", slug=slug))

    with db:
        db.execute(
            "DELETE FROM votes WHERE inspector_id = ? AND candidate_id IN"
            " (SELECT id FROM candidates WHERE group_id = ?)",
            (inspector["id"], group["id"]),
        )
        db.execute(
            "DELETE FROM submissions WHERE group_id = ? AND inspector_id = ?",
            (group["id"], inspector["id"]),
        )

    flash("Practice reset - it is ready to run through again.", "success")
    return redirect(url_for("main.inspect_page", slug=slug))


# --------------------------------------------------------------------------
# Images
# --------------------------------------------------------------------------


@bp.route("/g/<slug>/img/<path:filename>")
@login_required
def serve_image(slug: str, filename: str):
    group = _group_or_404(slug)
    directory = _image_dir(group)
    if not os.path.isdir(directory):
        abort(404)
    # send_from_directory rejects paths that try to escape the directory.
    return send_from_directory(directory, filename, max_age=3600)
