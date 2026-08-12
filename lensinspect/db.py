"""SQLite storage for the standalone lens inspector.

Five tables:

``inspectors``   one row per person, identified by UCI NetID (no password)
``groups``       one row per candidate list people can work through
``candidates``   one row per object, belonging to exactly one group
``votes``        one row per (candidate, inspector) -- a grade or a skip
``submissions``  one row per (group, inspector) -- "I have finished this group"

A candidate name is unique *within* a group, not globally, so the same object
may legitimately appear in two different rounds and collect independent votes.

Timestamps are ISO-8601 UTC strings rather than sqlite3's implicit datetime
adapters, which are deprecated in Python 3.12+.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any, Optional

from flask import current_app, g

# --------------------------------------------------------------------------
# Grading vocabulary
# --------------------------------------------------------------------------

#: Grades that count as a real judgement, best first.
GRADES: tuple[str, ...] = ("A", "B", "C", "X")

#: Recorded when someone is unsure. A skip is *not* a judgement: it parks the
#: object so the same person is asked again in the review pass at the end.
SKIP = "S"

ALL_GRADES: tuple[str, ...] = GRADES + (SKIP,)

#: Numeric mapping used for consensus averaging and conflict detection.
GRADE_VALUES: dict[str, int] = {"A": 3, "B": 2, "C": 1, "X": 0}

#: Short name for a grade -- what fits on a button.
GRADE_LABELS: dict[str, str] = {
    "A": "A sure lens",
    "B": "A probable lens",
    "C": "A possible lens",
    "X": "Not a lens",
    SKIP: "Not sure - decide later",
}

#: The full definition of each grade, as written by the project lead. Shown in
#: the guide. Kept verbatim -- these are the criteria graders are held to, so
#: they are not paraphrased to fit a layout.
GRADE_DESCRIPTIONS: dict[str, str] = {
    "A": "A sure lens - shows clear lensing features and no additional "
         "information is needed.",
    "B": "A probable lens - it shows lensing features but additional "
         "information is required to verify it as a definite lens.",
    "C": "A possible lens - it shows lensing features, but they can be "
         "explained without resorting to gravitational lensing.",
    "X": "Not a lens: Definitively not a lens.",
}

#: Grades treated as a positive lens identification (the A+B convention).
LENS_GRADES: frozenset[str] = frozenset({"A", "B"})


SCHEMA = """
CREATE TABLE IF NOT EXISTS inspectors (
    id           INTEGER PRIMARY KEY,
    netid        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    display_name TEXT,
    is_admin     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT    NOT NULL,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    id          INTEGER PRIMARY KEY,
    slug        TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    name        TEXT    NOT NULL,
    description TEXT,
    image_dir   TEXT,
    is_open     INTEGER NOT NULL DEFAULT 1,
    -- A practice group: every object carries a known answer and a coaching
    -- note, and the grading page walks the inspector through them.
    is_example  INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    id         INTEGER PRIMARY KEY,
    group_id   INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    cutoutname TEXT    NOT NULL,
    objname    TEXT,
    ra         REAL,
    dec        REAL,
    score      REAL,
    rank       INTEGER,
    extra      TEXT,
    created_at TEXT    NOT NULL,
    UNIQUE (group_id, cutoutname)
);

CREATE INDEX IF NOT EXISTS idx_candidates_group ON candidates(group_id);
CREATE INDEX IF NOT EXISTS idx_candidates_order ON candidates(group_id, rank, score);

CREATE TABLE IF NOT EXISTS votes (
    id           INTEGER PRIMARY KEY,
    candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
    inspector_id INTEGER NOT NULL REFERENCES inspectors(id) ON DELETE CASCADE,
    grade        TEXT    NOT NULL,
    flagged      INTEGER NOT NULL DEFAULT 0,
    note         TEXT,
    created_at   TEXT    NOT NULL,
    updated_at   TEXT    NOT NULL,
    UNIQUE (candidate_id, inspector_id)
);

CREATE INDEX IF NOT EXISTS idx_votes_candidate ON votes(candidate_id);
CREATE INDEX IF NOT EXISTS idx_votes_inspector ON votes(inspector_id);

CREATE TABLE IF NOT EXISTS submissions (
    id           INTEGER PRIMARY KEY,
    group_id     INTEGER NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
    inspector_id INTEGER NOT NULL REFERENCES inspectors(id) ON DELETE CASCADE,
    submitted_at TEXT    NOT NULL,
    UNIQUE (group_id, inspector_id)
);
"""


def utcnow() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with the settings this app relies on."""
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers proceed while another inspector's vote is being written,
    # which is what makes concurrent grading usable on one SQLite file.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


#: Columns added after the first release. ``CREATE TABLE IF NOT EXISTS`` will
#: not add them to a database that already exists, so apply them by hand.
_ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("groups", "is_example", "INTEGER NOT NULL DEFAULT 0"),
    # Separate from is_example on purpose. is_example means "a tutorial": the run
    # pauses on a verdict after each object and ends with a summary instead of a
    # review pass. is_sandbox means only "throwaway, so let people wipe it and go
    # again" -- it behaves exactly like a real group, second pass included. Folding
    # the two together silently disables the review pass on a group whose whole
    # purpose may be to demonstrate it.
    ("groups", "is_sandbox", "INTEGER NOT NULL DEFAULT 0"),
    # Visibility for scratch groups the organiser keeps around to try the
    # interface out: they would only confuse a grader, and grades cast in them are
    # not real results. A hidden group is visible to owner_netid alone -- not to
    # other admins, who have no reason to see someone's scratch work.
    ("groups", "is_hidden", "INTEGER NOT NULL DEFAULT 0"),
    ("groups", "owner_netid", "TEXT"),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, spec in _ADDED_COLUMNS:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")


def init_db(db_path: str) -> None:
    """Create tables and indexes if they do not already exist."""
    conn = connect(db_path)
    with conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
    conn.close()


def get_db() -> sqlite3.Connection:
    """Connection for the current request, opened lazily and cached on ``g``."""
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(exc: Optional[BaseException] = None) -> None:
    """Teardown hook -- closes the request's connection if one was opened."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app: Any) -> None:
    """Register the teardown hook and ensure the schema exists."""
    app.teardown_appcontext(close_db)
    init_db(app.config["DB_PATH"])


# --------------------------------------------------------------------------
# Helpers shared across modules
# --------------------------------------------------------------------------

#: UCI NetIDs are short lowercase alphanumeric handles, e.g. "jsmith2".
NETID_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")


def normalise_netid(raw: str) -> str:
    """Lowercase and trim a NetID, tolerating a pasted full email address."""
    value = (raw or "").strip().lower()
    if "@" in value:
        value = value.split("@", 1)[0]
    return value


def slugify(raw: str) -> str:
    """URL-safe slug for a group name."""
    value = re.sub(r"[^a-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    return value[:60] or "group"


def row_extra(row: sqlite3.Row) -> dict[str, Any]:
    """Decode a candidate's ``extra`` JSON column into a dict."""
    raw = row["extra"] if "extra" in row.keys() else None
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def group_by_slug(db: sqlite3.Connection, slug: str) -> Optional[sqlite3.Row]:
    return db.execute("SELECT * FROM groups WHERE slug = ?", (slug,)).fetchone()
