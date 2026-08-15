"""Progress tracking and consensus statistics, all scoped to one group.

Conventions
-----------
* Skips are never counted as votes. A skip means "I could not decide", and in
  this app it is a temporary state: the object comes back to the same person
  in the review pass. Whatever is still skipped at the end is reported as
  ``n_unsure`` rather than being silently dropped.
* ``mean_grade`` averages A=3, B=2, C=1, X=0.
* ``agreement`` is the fraction of votes on the modal grade, so a unanimous
  object scores 1.0 and a three-way split scores 1/3.
* ``conflict`` marks objects where two people are two or more grade steps
  apart (A vs C, B vs X) -- the worklist for a tiebreak pass.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any, Iterator, Optional

from .db import GRADE_VALUES, GRADES, LENS_GRADES, SKIP, row_extra

#: Two grade steps apart counts as a genuine disagreement.
CONFLICT_SPREAD = 2


# --------------------------------------------------------------------------
# Per-person progress
# --------------------------------------------------------------------------


def my_progress(
    db: sqlite3.Connection, group_id: int, inspector_id: int
) -> dict[str, Any]:
    """How far one inspector has got through one group."""
    total = db.execute(
        "SELECT COUNT(*) AS n FROM candidates WHERE group_id = ?", (group_id,)
    ).fetchone()["n"]

    rows = db.execute(
        "SELECT v.grade, COUNT(*) AS n FROM votes v"
        " JOIN candidates c ON c.id = v.candidate_id"
        " WHERE c.group_id = ? AND v.inspector_id = ?"
        " GROUP BY v.grade",
        (group_id, inspector_id),
    ).fetchall()

    counts = {row["grade"]: row["n"] for row in rows}
    graded = sum(n for grade, n in counts.items() if grade in GRADE_VALUES)
    skipped = counts.get(SKIP, 0)
    touched = graded + skipped

    # Objects retired by other people are gone from this person's queue, so
    # counting them as "not looked at yet" would leave the number stuck above zero
    # with nothing left to grade.
    cap = db.execute(
        "SELECT max_votes FROM groups WHERE id = ?", (group_id,)
    ).fetchone()["max_votes"] or 0
    retired_unseen = 0
    if cap > 0:
        retired_unseen = db.execute(
            """
            SELECT COUNT(*) AS n FROM candidates c
             WHERE c.group_id = ?
               AND NOT EXISTS (SELECT 1 FROM votes v
                                WHERE v.candidate_id = c.id AND v.inspector_id = ?)
               AND (SELECT COUNT(*) FROM votes vc
                     WHERE vc.candidate_id = c.id AND vc.grade != ?) >= ?
            """,
            (group_id, inspector_id, SKIP, cap),
        ).fetchone()["n"]

    submitted = db.execute(
        "SELECT submitted_at FROM submissions WHERE group_id = ? AND inspector_id = ?",
        (group_id, inspector_id),
    ).fetchone()

    return {
        "total": total,
        "graded": graded,
        "skipped": skipped,
        "unseen": max(0, total - touched - retired_unseen),
        "retired": retired_unseen,
        "counts": {g: counts.get(g, 0) for g in GRADES},
        "percent": round(100.0 * graded / total, 1) if total else 0.0,
        "finished": total > 0 and graded >= total,
        "submitted_at": submitted["submitted_at"] if submitted else None,
    }


def group_overview(db: sqlite3.Connection) -> list[dict[str, Any]]:
    """One row per group for the landing page: size, participants, submissions."""
    rows = db.execute(
        """
        SELECT g.*,
               (SELECT COUNT(*) FROM candidates c WHERE c.group_id = g.id) AS n_candidates,
               (SELECT COUNT(*) FROM submissions s WHERE s.group_id = g.id) AS n_submitted,
               (SELECT COUNT(DISTINCT v.inspector_id) FROM votes v
                  JOIN candidates c2 ON c2.id = v.candidate_id
                 WHERE c2.group_id = g.id) AS n_inspectors
        FROM groups g
        -- Practice first: it is where a newcomer should land.
        ORDER BY g.is_example DESC, g.is_sandbox DESC, g.is_open DESC, g.created_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def group_inspectors(db: sqlite3.Connection, group_id: int) -> list[dict[str, Any]]:
    """Per-person status within a group, for the admin and progress views."""
    rows = db.execute(
        """
        SELECT i.id, i.netid, i.display_name, i.is_admin, v.grade, COUNT(*) AS n,
               MAX(v.updated_at) AS last_at
        FROM votes v
        JOIN candidates c ON c.id = v.candidate_id
        JOIN inspectors i ON i.id = v.inspector_id
        WHERE c.group_id = ?
        GROUP BY i.id, v.grade
        """,
        (group_id,),
    ).fetchall()

    submitted = {
        row["inspector_id"]: row["submitted_at"]
        for row in db.execute(
            "SELECT inspector_id, submitted_at FROM submissions WHERE group_id = ?",
            (group_id,),
        ).fetchall()
    }

    by_inspector: dict[int, dict[str, Any]] = {}
    for row in rows:
        entry = by_inspector.setdefault(
            row["id"],
            {
                "netid": row["netid"],
                "display_name": row["display_name"] or row["netid"],
                "is_admin": bool(row["is_admin"]),
                "graded": 0,
                "skipped": 0,
                "submitted_at": submitted.get(row["id"]),
                "last_at": None,
                **{f"votes_{g}": 0 for g in GRADES},
            },
        )
        if row["last_at"] and (entry["last_at"] or "") < row["last_at"]:
            entry["last_at"] = row["last_at"]
        if row["grade"] == SKIP:
            entry["skipped"] += row["n"]
        elif row["grade"] in GRADE_VALUES:
            entry[f"votes_{row['grade']}"] += row["n"]
            entry["graded"] += row["n"]

    return sorted(by_inspector.values(), key=lambda e: -e["graded"])


def group_summary(db: sqlite3.Connection, group_id: int) -> dict[str, Any]:
    """Coverage of a group across everyone who has worked on it."""
    total = db.execute(
        "SELECT COUNT(*) AS n FROM candidates WHERE group_id = ?", (group_id,)
    ).fetchone()["n"]

    per_candidate = db.execute(
        "SELECT v.candidate_id, COUNT(*) AS n FROM votes v"
        " JOIN candidates c ON c.id = v.candidate_id"
        " WHERE c.group_id = ? AND v.grade != ?"
        " GROUP BY v.candidate_id",
        (group_id, SKIP),
    ).fetchall()

    counts = [row["n"] for row in per_candidate]
    graded_votes = sum(counts)
    people = db.execute(
        "SELECT COUNT(DISTINCT v.inspector_id) AS n FROM votes v"
        " JOIN candidates c ON c.id = v.candidate_id WHERE c.group_id = ?",
        (group_id,),
    ).fetchone()["n"]

    return {
        "total_candidates": total,
        "with_votes": len(counts),
        "untouched": total - len(counts),
        "graded_votes": graded_votes,
        "n_inspectors": people,
        "n_submitted": db.execute(
            "SELECT COUNT(*) AS n FROM submissions WHERE group_id = ?", (group_id,)
        ).fetchone()["n"],
        "max_votes": max(counts) if counts else 0,
        "percent_covered": round(100.0 * len(counts) / total, 1) if total else 0.0,
    }


# --------------------------------------------------------------------------
# Consensus
# --------------------------------------------------------------------------


def _consensus_from_grades(grades: list[str]) -> dict[str, Any]:
    """Reduce a list of letter grades to consensus fields."""
    counts = Counter(g for g in grades if g in GRADE_VALUES)
    n = sum(counts.values())
    if n == 0:
        return {
            "n_votes": 0,
            **{f"votes_{g}": 0 for g in GRADES},
            "majority_grade": "",
            "mean_grade": None,
            "agreement": None,
            "spread": None,
            "conflict": False,
            "tied": False,
            "lens_fraction": None,
            "consensus_is_lens": None,
        }

    top_count = max(counts.values())
    modal = [g for g in GRADES if counts.get(g, 0) == top_count]
    # On a tie take the more optimistic grade: an object half the group calls a
    # lens is worth keeping in view for a tiebreak pass.
    majority = modal[0]

    values = [GRADE_VALUES[g] for g in grades if g in GRADE_VALUES]
    n_lens = sum(counts.get(g, 0) for g in LENS_GRADES)

    return {
        "n_votes": n,
        **{f"votes_{g}": counts.get(g, 0) for g in GRADES},
        "majority_grade": majority,
        "mean_grade": round(sum(values) / n, 3),
        "agreement": round(top_count / n, 3),
        "spread": max(values) - min(values),
        "conflict": (max(values) - min(values)) >= CONFLICT_SPREAD,
        "tied": len(modal) > 1,
        "lens_fraction": round(n_lens / n, 3),
        "consensus_is_lens": n_lens / n >= 0.5,
    }


def consensus_rows(
    db: sqlite3.Connection,
    group_id: int,
    only_conflicts: bool = False,
    min_votes: int = 0,
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Per-candidate consensus for one group, ordered by rank then score."""
    candidates = db.execute(
        """
        SELECT c.id, c.cutoutname, c.objname, c.ra, c.dec, c.score, c.rank, c.extra
        FROM candidates c
        WHERE c.group_id = ?
        ORDER BY c.rank IS NULL, c.rank ASC, c.score DESC, c.id ASC
        """,
        (group_id,),
    ).fetchall()

    vote_rows = db.execute(
        """
        SELECT v.candidate_id, v.grade, v.flagged, v.note, i.netid
        FROM votes v
        JOIN candidates c ON c.id = v.candidate_id
        JOIN inspectors i ON i.id = v.inspector_id
        WHERE c.group_id = ?
        ORDER BY v.candidate_id, i.netid
        """,
        (group_id,),
    ).fetchall()

    by_candidate: dict[int, list[sqlite3.Row]] = {}
    for row in vote_rows:
        by_candidate.setdefault(row["candidate_id"], []).append(row)

    out: list[dict[str, Any]] = []
    for cand in candidates:
        votes = by_candidate.get(cand["id"], [])
        stats = _consensus_from_grades([v["grade"] for v in votes])

        if stats["n_votes"] < min_votes:
            continue
        if only_conflicts and not stats["conflict"]:
            continue

        notes = "; ".join(
            f"{v['netid']}: {v['note'].strip()}"
            for v in votes
            if v["note"] and v["note"].strip()
        )
        graders = ",".join(
            f"{v['netid']}={v['grade']}" for v in votes if v["grade"] != SKIP
        )
        unsure = ",".join(v["netid"] for v in votes if v["grade"] == SKIP)

        out.append(
            {
                "cutoutname": cand["cutoutname"],
                "objname": cand["objname"] or "",
                "ra": cand["ra"],
                "dec": cand["dec"],
                "score": cand["score"],
                "rank": cand["rank"],
                **stats,
                "n_unsure": sum(1 for v in votes if v["grade"] == SKIP),
                "unsure_by": unsure,
                "n_flagged": sum(1 for v in votes if v["flagged"]),
                "graders": graders,
                "notes": notes,
                "extra": row_extra(cand),
            }
        )
        if limit is not None and len(out) >= limit:
            break

    return out


CONSENSUS_COLUMNS: tuple[str, ...] = (
    "cutoutname",
    "objname",
    "ra",
    "dec",
    "score",
    "rank",
    "n_votes",
    "votes_A",
    "votes_B",
    "votes_C",
    "votes_X",
    "n_unsure",
    "unsure_by",
    "majority_grade",
    "mean_grade",
    "agreement",
    "spread",
    "conflict",
    "tied",
    "lens_fraction",
    "consensus_is_lens",
    "n_flagged",
    "graders",
    "notes",
)


def consensus_records(rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Flatten consensus rows to plain CSV-ready dicts."""
    for row in rows:
        record = {col: row.get(col, "") for col in CONSENSUS_COLUMNS}
        for key, value in record.items():
            if isinstance(value, bool):
                record[key] = "true" if value else "false"
            elif value is None:
                record[key] = ""
        yield record


VOTE_COLUMNS: tuple[str, ...] = (
    "group_name",
    "cutoutname",
    "objname",
    "ra",
    "dec",
    "score",
    "rank",
    "netid",
    "grade",
    "grade_is_skip",
    "flagged",
    "note",
    "submitted",
    "created_at",
    "updated_at",
)


def vote_records(
    db: sqlite3.Connection, group_id: int, netid: Optional[str] = None
) -> Iterator[dict[str, Any]]:
    """Every individual vote in a group, one row each -- the raw export.

    ``netid`` narrows it to one person, which is how the per-inspector download
    works: one file per grader, handed over once they have submitted.
    """
    rows = db.execute(
        """
        SELECT g.name AS group_name, c.cutoutname, c.objname, c.ra, c.dec,
               c.score, c.rank, i.netid, v.grade, v.flagged, v.note,
               v.created_at, v.updated_at,
               (SELECT 1 FROM submissions s
                 WHERE s.group_id = g.id AND s.inspector_id = i.id) AS submitted
        FROM votes v
        JOIN candidates c  ON c.id = v.candidate_id
        JOIN groups g      ON g.id = c.group_id
        JOIN inspectors i  ON i.id = v.inspector_id
        WHERE c.group_id = ? AND (? IS NULL OR i.netid = ?)
        ORDER BY c.rank IS NULL, c.rank ASC, c.score DESC, c.id ASC, i.netid
        """,
        (group_id, netid, netid),
    ).fetchall()

    for row in rows:
        record = dict(row)
        record["grade_is_skip"] = "true" if record["grade"] == SKIP else "false"
        record["flagged"] = "true" if record["flagged"] else "false"
        record["submitted"] = "true" if record["submitted"] else "false"
        record["note"] = record["note"] or ""
        record["objname"] = record["objname"] or ""
        yield {col: record.get(col, "") for col in VOTE_COLUMNS}
