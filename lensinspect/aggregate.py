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

import math
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

    unseen = max(0, total - touched - retired_unseen)

    # What this person can actually be asked for: the whole group minus anything
    # that retired without their vote. Shrinking the denominator rather than
    # crediting them for other people's work keeps the bar an honest measure of
    # their own grading, while never asking for more than the queue can offer.
    available = max(0, total - retired_unseen)
    left = unseen + skipped              # == available - graded

    return {
        "total": total,                  # the group's real size, for the admin views
        "available": available,          # what this person will ever be offered
        "graded": graded,
        "skipped": skipped,
        "unseen": unseen,
        "retired": retired_unseen,
        "left": left,
        "counts": {g: counts.get(g, 0) for g in GRADES},
        "percent": round(100.0 * graded / available, 1) if available else 100.0,
        "finished": available > 0 and left == 0,
        "submitted_at": submitted["submitted_at"] if submitted else None,
    }


def group_progress(db: sqlite3.Connection, group_id: int) -> dict[str, Any]:
    """How far the *group as a whole* has got -- the same numbers for everybody.

    An object is "done" when it has been graded by ``max_votes`` people and has
    retired out of everyone's queue. Without a cap there is no finishing line, so
    ``capped`` is False and callers should not show a completion bar.

    Skips are excluded, matching the retirement rule: an object several people
    were unsure about is not settled.
    """
    row = db.execute(
        "SELECT max_votes, (SELECT COUNT(*) FROM candidates c WHERE c.group_id = g.id)"
        " AS total FROM groups g WHERE g.id = ?",
        (group_id,),
    ).fetchone()
    total = row["total"] if row else 0
    cap = (row["max_votes"] if row else 0) or 0

    if cap <= 0 or total == 0:
        return {"total": total, "cap": cap, "capped": False,
                "done": 0, "left": total, "percent": 0.0}

    done = db.execute(
        """
        SELECT COUNT(*) AS n FROM candidates c
         WHERE c.group_id = ?
           AND (SELECT COUNT(*) FROM votes v
                 WHERE v.candidate_id = c.id AND v.grade != ?) >= ?
        """,
        (group_id, SKIP, cap),
    ).fetchone()["n"]

    return {
        "total": total,
        "cap": cap,
        "capped": True,
        "done": done,
        "left": total - done,
        "percent": round(100.0 * done / total, 1),
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


# --------------------------------------------------------------------------
# Collapsing co-located entries into one system
# --------------------------------------------------------------------------

#: Two entries this close on the sky are the same scene to a grader. The stamps
#: are 96 px at ~0.1"/px, so a pair 3" apart is a ~30 px shift of a ~10" field.
SYSTEM_RADIUS_ARCSEC = 3.0

SYSTEM_COLUMNS: tuple[str, ...] = (
    "system_id",
    "n_entries",
    "members",
    "max_separation_arcsec",
    "framing_disagreement",
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
    "n_flagged",
    "graders",
    "notes",
)


def _separation_arcsec(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
    """Small-angle separation. Good to well under a mas at these scales."""
    cos_dec = math.cos(math.radians((dec1 + dec2) / 2.0))
    return math.hypot((ra1 - ra2) * cos_dec * 3600.0, (dec1 - dec2) * 3600.0)


def _cluster_by_position(
    rows: list[sqlite3.Row], radius_arcsec: float
) -> list[list[int]]:
    """Group row indices into systems by sky position, single-linkage.

    Buckets on a grid one radius across and only compares within the 3x3
    neighbourhood, so this stays linear instead of comparing all 2,000 x 2,000
    pairs. Rows without coordinates are each their own system -- there is nothing
    to match them on, and silently merging them would be worse than not merging.
    """
    step = radius_arcsec / 3600.0
    parent = list(range(len(rows)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    buckets: dict[tuple[int, int], list[int]] = {}
    for i, row in enumerate(rows):
        if row["ra"] is None or row["dec"] is None:
            continue
        cos_dec = max(math.cos(math.radians(row["dec"])), 1e-6)
        key = (int(row["ra"] * cos_dec / step), int(row["dec"] / step))
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for j in buckets.get((key[0] + dx, key[1] + dy), ()):
                    sep = _separation_arcsec(
                        row["ra"], row["dec"], rows[j]["ra"], rows[j]["dec"]
                    )
                    if sep <= radius_arcsec:
                        a, b = find(i), find(j)
                        if a != b:
                            parent[a] = b
        buckets.setdefault(key, []).append(i)

    groups: dict[int, list[int]] = {}
    for i in range(len(rows)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def system_rows(
    db: sqlite3.Connection,
    group_id: int,
    radius_arcsec: float = SYSTEM_RADIUS_ARCSEC,
    only_conflicts: bool = False,
    min_votes: int = 0,
) -> list[dict[str, Any]]:
    """Consensus with co-located catalogue entries collapsed into one system.

    The source catalogue is preselected against stars and spurious detections but
    is not deduplicated by position, so a deblended pair a fraction of an arcsecond
    apart survives as two entries whose cutouts show the same scene framed slightly
    differently. Graded separately they are two rows; on the sky they are one
    object, and one row per object is what a catalogue wants.

    A grader who saw both framings still counts once, contributing their most
    lens-like answer: if the same scene read as a lens under either framing, that
    is what they saw. Where their two answers differed, ``framing_disagreement``
    records it -- that is a measurement of how much the framing swayed people,
    which is worth keeping rather than averaging away.
    """
    candidates = db.execute(
        """
        SELECT c.id, c.cutoutname, c.objname, c.ra, c.dec, c.score, c.rank
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
    for members in _cluster_by_position(candidates, radius_arcsec):
        # Representative: the entry the model ranked highest, so the row keeps the
        # name and rank the shortlist was built on.
        members = sorted(
            members,
            key=lambda i: (
                candidates[i]["rank"] is None,
                candidates[i]["rank"] if candidates[i]["rank"] is not None else 0,
                -(candidates[i]["score"] or 0.0),
            ),
        )
        head = candidates[members[0]]

        per_grader: dict[str, list[sqlite3.Row]] = {}
        for i in members:
            for vote in by_candidate.get(candidates[i]["id"], []):
                per_grader.setdefault(vote["netid"], []).append(vote)

        grades: list[str] = []
        graded_by: list[str] = []
        unsure: list[str] = []
        disagreed: list[str] = []
        n_flagged = 0
        notes: list[str] = []
        for netid in sorted(per_grader):
            votes = per_grader[netid]
            real = [v["grade"] for v in votes if v["grade"] != SKIP]
            if len({v["grade"] for v in votes}) > 1:
                disagreed.append(
                    f"{netid}=" + "/".join(v["grade"] for v in votes)
                )
            n_flagged += sum(1 for v in votes if v["flagged"])
            notes.extend(
                f"{netid}: {v['note'].strip()}"
                for v in votes
                if v["note"] and v["note"].strip()
            )
            if real:
                grades.append(max(real, key=lambda g: GRADE_VALUES[g]))
                graded_by.append(netid)
            else:
                unsure.append(netid)

        stats = _consensus_from_grades(grades)
        if stats["n_votes"] < min_votes:
            continue
        if only_conflicts and not stats["conflict"]:
            continue

        seps = [
            _separation_arcsec(
                head["ra"], head["dec"], candidates[i]["ra"], candidates[i]["dec"]
            )
            for i in members[1:]
            if head["ra"] is not None and candidates[i]["ra"] is not None
        ]

        out.append(
            {
                "system_id": head["cutoutname"],
                "n_entries": len(members),
                "members": " ".join(candidates[i]["cutoutname"] for i in members),
                "max_separation_arcsec": round(max(seps), 3) if seps else 0.0,
                "framing_disagreement": ",".join(disagreed),
                "cutoutname": head["cutoutname"],
                "objname": head["objname"] or "",
                "ra": head["ra"],
                "dec": head["dec"],
                "score": head["score"],
                "rank": head["rank"],
                **stats,
                "n_unsure": len(unsure),
                "unsure_by": ",".join(unsure),
                "n_flagged": n_flagged,
                "graders": ",".join(
                    f"{netid}={grade}" for netid, grade in zip(graded_by, grades)
                ),
                "notes": "; ".join(notes),
            }
        )

    out.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, -(r["score"] or 0.0)))
    return out


def system_records(rows: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Flatten system rows to plain CSV-ready dicts."""
    for row in rows:
        record = {col: row.get(col, "") for col in SYSTEM_COLUMNS}
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


# --------------------------------------------------------------------------
# Practice gate
# --------------------------------------------------------------------------


def _practice_group(db: sqlite3.Connection) -> Optional[sqlite3.Row]:
    """The group that gates the real ones: the first practice group, or None."""
    return db.execute(
        "SELECT id, slug, name FROM groups WHERE is_example = 1 ORDER BY id LIMIT 1"
    ).fetchone()


def mark_practice_done(
    db: sqlite3.Connection, inspector_id: int, when: str
) -> None:
    """Stamp this person as having completed the practice, if they just have.

    Called after every practice vote. ``AND practice_done_at IS NULL`` keeps the
    first completion as the recorded one, and means this is safe to call on each
    vote of a repeat run.
    """
    group = _practice_group(db)
    if group is None:
        return
    total = db.execute(
        "SELECT COUNT(*) AS n FROM candidates WHERE group_id = ?", (group["id"],)
    ).fetchone()["n"]
    if not total:
        return
    graded = db.execute(
        "SELECT COUNT(*) AS n FROM votes v JOIN candidates c ON c.id = v.candidate_id"
        " WHERE c.group_id = ? AND v.inspector_id = ? AND v.grade != ?",
        (group["id"], inspector_id, SKIP),
    ).fetchone()["n"]
    if graded >= total:
        db.execute(
            "UPDATE inspectors SET practice_done_at = ?"
            " WHERE id = ? AND practice_done_at IS NULL",
            (when, inspector_id),
        )


def practice_status(db: sqlite3.Connection, inspector_id: int) -> dict[str, Any]:
    """Whether this person has worked through the practice group.

    Real groups are gated on this: the practice set is where someone learns what
    an arc looks like and calibrates against the expert answers, and grades cast
    before that are the ones most likely to be wrong. It is eight objects.

    A skip does not count -- parking all eight would otherwise "finish" it without
    the person ever committing to an answer or seeing whether they agreed.

    Returns ``required=False`` when no practice group exists, so a deployment
    without one is not locked out of its own data.
    """
    row = db.execute(
        "SELECT id, slug, name,"
        " (SELECT COUNT(*) FROM candidates c WHERE c.group_id = g.id) AS total"
        " FROM groups g WHERE g.is_example = 1 ORDER BY g.id LIMIT 1"
    ).fetchone()
    if row is None or row["total"] == 0:
        return {"required": False, "done": True, "graded": 0, "total": 0,
                "slug": None, "name": None}

    graded = db.execute(
        "SELECT COUNT(*) AS n FROM votes v JOIN candidates c ON c.id = v.candidate_id"
        " WHERE c.group_id = ? AND v.inspector_id = ? AND v.grade != ?",
        (row["id"], inspector_id, SKIP),
    ).fetchone()["n"]

    # Completion is a fact about the person, not about their current vote count:
    # once stamped it stays stamped, so pressing "Start over" on the practice --
    # which is encouraged -- never takes the real groups away again.
    stamped = db.execute(
        "SELECT practice_done_at FROM inspectors WHERE id = ?", (inspector_id,)
    ).fetchone()["practice_done_at"]

    return {
        "required": True,
        "done": bool(stamped) or graded >= row["total"],
        "done_at": stamped,
        "graded": graded,
        "total": row["total"],
        "slug": row["slug"],
        "name": row["name"],
    }
