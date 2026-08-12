"""Parse a candidate CSV and load it into a group.

Two layouts are recognised with no configuration:

**RR2 top-K** (lensfinder ensemble inference)
    ``rank,ensemble_score,object_id,tile_id,local_index,ra,dec,point_like_prob``
    ``cutoutname`` is built as ``TILE{tile_id}_{object_id}``.

**master_table** (the AgileLens catalogue, ``;``-delimited)
    ``index;cutoutname;objname;ra;dec;VIS;Y;H;J;grade;new;rawscore;notes``

Anything else works as long as one column holds the cutout name; the web
upload form lets you point at it by name. Columns that are not mapped to a
first-class field are preserved in the candidate's ``extra`` JSON and shown in
the grading sidebar.

Also usable from a terminal::

    python -m lensinspect.ingest list.csv --group "RR2 top 500"
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
from typing import Any, Iterable, Optional

from .db import connect, init_db, slugify, utcnow

# Column names tried, in order, when one is not named explicitly.
CUTOUT_ALIASES = ("cutoutname", "cutout", "cutout_name", "filename", "name")
OBJNAME_ALIASES = ("objname", "object_name", "obj", "id", "object_id")
# RIGHT_ASCENSION / DECLINATION are what the lensfinder inference CSVs use.
RA_ALIASES = ("ra", "ra_deg", "right_ascension")
DEC_ALIASES = ("dec", "dec_deg", "declination")
SCORE_ALIASES = (
    "ensemble_score",
    "ens_score",
    "rawscore",
    "score",
    "prob",
    "probability",
)
# "cand_rank" first: in the q1_clean_ranked_nonlens exports it runs 1..N over
# the candidates themselves, while "rank" is the object's position among all
# 5M scored objects. Both give the same order; the former reads better.
# Deliberately not "index": in master_table.csv that column is a row id, not a
# ranking. Lists with no rank column fall back to ordering by score.
RANK_ALIASES = ("cand_rank", "rank")


def _pick(fieldnames: Iterable[str], aliases: Iterable[str]) -> Optional[str]:
    """First alias present in the header, matched case-insensitively."""
    lookup = {name.strip().lower(): name.strip() for name in fieldnames if name}
    for alias in aliases:
        if alias.lower() in lookup:
            return lookup[alias.lower()]
    return None


def sniff_delimiter(head: str) -> str:
    # A semicolon-delimited master_table has no commas in its header at all, so
    # a simple count is more robust here than csv.Sniffer.
    return ";" if head.count(";") > head.count(",") else ","


def _to_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def read_candidates(
    text: str,
    delimiter: Optional[str] = None,
    cutout_col: Optional[str] = None,
    objname_col: Optional[str] = None,
    ra_col: Optional[str] = None,
    dec_col: Optional[str] = None,
    score_col: Optional[str] = None,
    rank_col: Optional[str] = None,
    tile_col: str = "tile_id",
    object_col: str = "object_id",
    limit: Optional[int] = None,
) -> list[dict[str, Any]]:
    """Parse CSV ``text`` into candidate dicts ready for insertion."""
    first_line = text.split("\n", 1)[0]
    delim = delimiter or sniff_delimiter(first_line)

    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    if not reader.fieldnames:
        raise ValueError("Could not read a header row from the file.")

    fields = [f.strip() for f in reader.fieldnames if f is not None]

    cutout = cutout_col or _pick(fields, CUTOUT_ALIASES)
    tile = _pick(fields, (tile_col,))
    obj_id = _pick(fields, (object_col,))

    if cutout is None and not (tile and obj_id):
        raise ValueError(
            "No cutout-name column found. Looked for "
            f"{', '.join(CUTOUT_ALIASES)}, and for '{tile_col}' + '{object_col}' "
            f"to build one. The header was: {', '.join(fields)}. "
            "Name the column explicitly to load this file."
        )

    objname = objname_col or _pick(fields, OBJNAME_ALIASES)
    ra_name = ra_col or _pick(fields, RA_ALIASES)
    dec_name = dec_col or _pick(fields, DEC_ALIASES)
    score_name = score_col or _pick(fields, SCORE_ALIASES)
    rank_name = rank_col or _pick(fields, RANK_ALIASES)

    # Everything not mapped above is kept verbatim in `extra`.
    mapped = {cutout, objname, ra_name, dec_name, score_name, rank_name}
    extra_fields = [f for f in fields if f and f not in mapped]

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in reader:
        row = {
            (k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
            for k, v in raw.items()
        }

        if cutout:
            name = str(row.get(cutout, "") or "").strip()
        else:
            tile_val, obj_val = row.get(tile, ""), row.get(obj_id, "")
            name = f"TILE{tile_val}_{obj_val}" if tile_val and obj_val else ""

        if not name:
            continue
        name = name[:-4] if name.lower().endswith(".png") else name
        if name in seen:
            continue  # de-duplicate within the file
        seen.add(name)

        rows.append(
            {
                "cutoutname": name,
                "objname": str(row.get(objname, "") or "").strip() if objname else None,
                "ra": _to_float(row.get(ra_name)) if ra_name else None,
                "dec": _to_float(row.get(dec_name)) if dec_name else None,
                "score": _to_float(row.get(score_name)) if score_name else None,
                "rank": _to_int(row.get(rank_name)) if rank_name else None,
                "extra": {
                    f: row.get(f) for f in extra_fields if row.get(f) not in (None, "")
                },
            }
        )

        if limit is not None and len(rows) >= limit:
            break

    return rows


def insert_candidates(
    conn: Any, group_id: int, rows: list[dict[str, Any]]
) -> dict[str, int]:
    """Insert ``rows`` into ``group_id``, skipping names already in that group.

    Existing rows are never mutated, so a repeat load is a no-op for objects
    that are already there and votes cast against them stay attached.
    """
    now = utcnow()
    stats = {"read": len(rows), "inserted": 0, "skipped_existing": 0}

    with conn:
        for row in rows:
            cur = conn.execute(
                """
                INSERT INTO candidates
                    (group_id, cutoutname, objname, ra, dec, score, rank, extra, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (group_id, cutoutname) DO NOTHING
                """,
                (
                    group_id,
                    row["cutoutname"],
                    row["objname"] or None,
                    row["ra"],
                    row["dec"],
                    row["score"],
                    row["rank"],
                    json.dumps(row["extra"]) if row["extra"] else None,
                    now,
                ),
            )
            if cur.rowcount:
                stats["inserted"] += 1
            else:
                stats["skipped_existing"] += 1

    return stats


def ensure_group(
    conn: Any,
    name: str,
    description: Optional[str] = None,
    image_dir: Optional[str] = None,
) -> int:
    """Return the id of the group called ``name``, creating it if needed."""
    slug = slugify(name)
    row = conn.execute("SELECT id FROM groups WHERE slug = ?", (slug,)).fetchone()
    if row is not None:
        return row["id"]
    with conn:
        cur = conn.execute(
            "INSERT INTO groups (slug, name, description, image_dir, is_open, created_at)"
            " VALUES (?, ?, ?, ?, 1, ?)",
            (slug, name.strip(), description, image_dir, utcnow()),
        )
    return cur.lastrowid


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

DEFAULT_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "inspector.db"
)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Load a candidate list into an inspection group."
    )
    parser.add_argument("csv_path", help="CSV file to load")
    parser.add_argument(
        "--group",
        default=None,
        help="Group name (default: the CSV's basename). Created if it does not exist.",
    )
    parser.add_argument("--db", default=os.environ.get("INSPECTOR_DB", DEFAULT_DB_PATH))
    parser.add_argument("--image-dir", default=None, help="Cutout directory for the group")
    parser.add_argument("--description", default=None)
    parser.add_argument("--delimiter", default=None)
    parser.add_argument("--limit", type=int, default=None, help="Load at most N rows")
    parser.add_argument("--cutout-col", default=None)
    parser.add_argument("--objname-col", default=None)
    parser.add_argument("--ra-col", default=None)
    parser.add_argument("--dec-col", default=None)
    parser.add_argument("--score-col", default=None)
    parser.add_argument("--rank-col", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not os.path.isfile(args.csv_path):
        parser.error(f"no such file: {args.csv_path}")

    group_name = args.group or os.path.splitext(os.path.basename(args.csv_path))[0]

    with open(args.csv_path, encoding="utf-8-sig") as handle:
        text = handle.read()

    rows = read_candidates(
        text,
        delimiter=args.delimiter,
        cutout_col=args.cutout_col,
        objname_col=args.objname_col,
        ra_col=args.ra_col,
        dec_col=args.dec_col,
        score_col=args.score_col,
        rank_col=args.rank_col,
        limit=args.limit,
    )

    print(f"Parsed {len(rows)} candidates from {args.csv_path} (group={group_name!r})")
    if rows:
        print(f"  first: {rows[0]['cutoutname']}  ra={rows[0]['ra']} dec={rows[0]['dec']}")
        print(f"  last:  {rows[-1]['cutoutname']}")
        if rows[0]["extra"]:
            print(f"  extra columns kept: {sorted(rows[0]['extra'])}")

    if args.dry_run:
        print("Dry run -- nothing written.")
        return 0

    init_db(args.db)
    conn = connect(args.db)
    group_id = ensure_group(
        conn, group_name, description=args.description, image_dir=args.image_dir
    )
    stats = insert_candidates(conn, group_id, rows)
    conn.close()

    print(
        f"Wrote to {args.db}: {stats['inserted']} inserted, "
        f"{stats['skipped_existing']} already in the group"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
