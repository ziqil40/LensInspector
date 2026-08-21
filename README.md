# Lens Inspector

A small self-hosted web app for grading strong gravitational lens candidates by
eye. People sign in with a username and no password, work through a list of
cutouts one image at a time, park anything they are unsure about, and come back
to it. The organiser exports the pooled result as CSV.

It was built to check a machine-learning shortlist by eye: the model ranks a few
million objects, and a handful of people grade the top few thousand. It has no
dependency on any particular survey or model — it serves PNG cutouts you point
it at, and records what people say about them.

Grader instructions live in the app itself at **`/guide`**, along with an
interactive practice group, so you do not have to write any.

![grades: A sure lens, B probable, C possible, X not a lens](lensinspect/static/img/guide/nonlens-spiral.png)

---

## Quick start

```bash
git clone https://github.com/ziqil40/LensInspector.git
cd LensInspector

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env
# edit .env: set SECRET_KEY to a random string and ADMIN_NETIDS to your username
.venv/bin/python -c 'import secrets; print(secrets.token_hex(32))'

.venv/bin/python run_inspector.py      # http://localhost:8000
```

Then load a candidate list (see [Adding your own group](#adding-your-own-group))
and send people the address.

Port 8000 is the default deliberately: macOS binds 5000 to its AirPlay Receiver,
which answers with a confusing `403`. Override with `PORT=…`.

---

## Adding your own group

A **group** is one list of candidates. Groups are independent — each has its own
objects, its own image directory and its own results — and people pick one from
the landing page.

Groups are **not creatable from the web UI**, on purpose: loading a candidate
list is an operation for whoever runs the deployment.

```bash
.venv/bin/python -m lensinspect.ingest my_candidates.csv \
    --group "My survey, top 2000" \
    --image-dir /path/to/cutout/pngs \
    --description "Ensemble run of March 2026"
```

`--dry-run` checks the parse first. Re-running on the same file is a no-op for
objects already in the group, so existing grades are never disturbed. The same
object may appear in two groups and collects votes independently.

### The CSV

One column has to hold the cutout name. Two layouts need no configuration:

**A ranked inference export**, where the cutout name is built as
`TILE{TILE_ID}_{OBJECT_ID}`:

```
cand_rank,rank,pct,ens_score,OBJECT_ID,RIGHT_ASCENSION,DECLINATION,TILE_ID,CUTOUT_INDEX,…
```

**A `;`-delimited master table**, where `cutoutname` is used directly:

```
index;cutoutname;objname;ra;dec;VIS;Y;H;J;grade;new;rawscore;notes
```

Anything else works too — name the column with `--cutout-col`, plus
`--ra-col` / `--dec-col` / `--score-col` / `--rank-col` as needed. **Unmapped
columns are kept** and shown in the sidebar while grading, so photometry and
diagnostics stay visible.

Object order is `cand_rank`/`rank` when present, otherwise descending score.
Everyone sees the same order unless the group has `shuffle_order` set — see
[Order, and spreading the work](#order-and-spreading-the-work).

### The images

One PNG per candidate, named `<cutoutname>.png`, in the group's `--image-dir`.
A missing image is not fatal: the object still appears, with a message telling
the grader to mark it unsure.

If your cutouts live in per-tile `.npy` stacks, unpack them to PNGs first — any
script that writes `<cutoutname>.png` will do.

### A practice group

Set `is_example = 1` on a group and it becomes a tutorial: after each answer the
grader is shown how an expert graded it, so they can calibrate. Nothing in it
counts.

**It is also a gate.** While a practice group exists, non-admin graders cannot
open any real group until they have graded every object in it — enforced on the
page, the queue API and the vote API, not just hidden in the UI. Skips do not
count, or someone could park all eight without ever committing to an answer.
Admins are exempt, and a deployment with no practice group gates nothing.

Completion is stamped on the grader (`inspectors.practice_done_at`) rather than
recomputed from their current votes, because a practice group is usually also a
sandbox: "Start over" wipes the votes, and inferring the gate from them would
punish someone for revising. The stamp is written once and never cleared, and
graders who finished before the column existed are backfilled from their votes
on first start.

The teaching text lives in each candidate's `extra` JSON:

| Key | Meaning |
|---|---|
| `_answer` | the expert's grade (`A`/`B`/`C`/`X`) |
| `_hint` | a nudge shown before they answer |

Underscore-prefixed keys are hidden from the metadata sidebar.

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `SECRET_KEY` | random per start | Session signing key. **Set this**, or every restart signs everybody out. `.env` is read automatically at startup, so this holds however the server is started |
| `ADMIN_NETIDS` | *(empty)* | Comma-separated usernames allowed on the results pages. If empty, the first person to sign in becomes admin |
| `INSPECTOR_DB` | `./data/inspector.db` | SQLite file |
| `CUTOUT_DIR` | `./cutouts` | Fallback image directory for groups that do not set their own |
| `PORT` / `HOST` | `8000` / `0.0.0.0` | Where to listen |

`.env.example` lists these in copyable form. `.env` itself is gitignored.

---

## Grading

| Key | Meaning |
|---|---|
| `1` / `a` | **A** — a sure lens |
| `2` / `b` | **B** — a probable lens |
| `3` / `c` | **C** — a possible lens |
| `4` / `x` | **X** — not a lens |
| `s` | **Not sure** — park it and come back |
| `←` | Back, to change the previous answer |

The full definitions shown to graders live in `GRADE_DESCRIPTIONS`
(`lensinspect/db.py`) and are rendered into the guide from there, so there is one
copy of the wording.

A/B is the positive class, matching the `letter_grade ∈ {A, B}` convention.

Grading advances immediately and saves in the background; a failed save raises a
red toast naming the object and rolls the answer back, so a problem is always
visible. The next few images are preloaded. Zoom, brightness, contrast and
pixelated are display-only and never recorded. Cutouts are drawn at 3× natural
size, since a 96 px stamp at 1:1 is unreadable.

### Order, and spreading the work

By default every grader is shown every object in the same rank order. That is
fine when people finish, but if they stop early they all stop in the same place:
the top of the list gets every vote and the tail gets none.

Two per-group settings on the admin page change that:

**Give each grader their own random order** (`shuffle_order`) hands each person a
different, stable permutation of the group, seeded from (group, grader). Partial
effort then spreads over the whole list. With 10 graders on a 2,000-object group:

| Each grader completes | Same order | Own random order |
|---|---|---|
| 25% | 500 objects covered (25%) | **1,889 covered (94%)**, ~2.5 votes each |
| 50% | 1,000 covered (50%) | **1,997 covered (100%)**, ~5 votes each |
| 100% | all 2,000 × 10 | all 2,000 × 10 |

The order is a hash of (group, grader, object) rather than `random()`, so it is
stable across requests and restarts — otherwise "carry on where you left off"
would hand people a freshly shuffled list every time.

**Retire an object after N grades** (`max_votes`, 0 = never) drops an object from
everyone's queue once N people have graded it — nobody is offered it again, and
it stops counting toward anyone's "not looked at yet". Skips do not count: an
object three people were unsure about is not settled, so it stays in circulation.

Note the cap only bites when there are *more* graders than N. With 10 people and
N=10, nothing retires early, since each person votes at most once per object.

Redundancy still comes from several people grading the same object, and the
agreement between them is the result.

### One row per cutout, or one row per system

A candidate list is usually preselected against stars and spurious detections but
not deduplicated by position. A deblended pair a fraction of an arcsecond apart
therefore survives as two catalogue entries — and since a 96 px stamp covers
about 10", both cutouts show the same scene, framed a few pixels differently. The
grader sees the same picture twice; the sky has one object.

The results page offers both exports:

| Download | Row per | Use for |
|---|---|---|
| **One row per cutout** | catalogue entry | auditing what each person was actually shown |
| **One row per system** | object on the sky | the published catalogue |

The merged export clusters entries within `SYSTEM_RADIUS_ARCSEC` (3", single
linkage), keeps the highest-ranked entry as the representative, and pools the
votes. A grader who saw both framings counts **once**, contributing their most
lens-like answer — if the scene read as a lens either way, that is what they saw.
Where their two answers differed, `framing_disagreement` records it rather than
averaging it away: how much the framing swayed people is a measurement, not
noise. `n_entries`, `members` and `max_separation_arcsec` keep the merge
auditable.

**Graders see their own progress; retirement is hidden from them.** A grader's
bar counts the cutouts *they* have graded — *"100 of 1,842 graded"* — so it moves
only when they grade something. Retirement shows up as a *shrinking denominator*
rather than as progress they did not earn: an object finished by other people
before it reached them drops out of their total, so 1,842 becomes 1,790 and their
100 is unchanged. They are never shown a retirement count.

**The organiser sees the real figures.** Admin → the group has a **Retirement**
card and a `retired at N grades` tile: how many objects have reached the cap and
how many are still in circulation.

### Adding cutouts while people are grading

Appending to a group mid-run is safe and needs no restart — upload a CSV under
**Add more candidates** on the group's admin page, or re-run `lensinspect.ingest`
against the same group. Objects already present are left alone, so no grade is
disturbed.

On a shuffled group the new objects **interleave** into each grader's existing
order rather than landing at the end, because the sort key is a hash of the
object rather than its position. Someone halfway through will start seeing new
cutouts straight away, mixed in with what they had left, and the relative order
of everything they had not yet reached is unchanged.

The only visible side effect is that the group got bigger: a grader who was at
100% will drop back below it, which is correct — there is genuinely more to do.

### Nothing to submit

There is no submit step and no lock. A grade is recorded the moment the key is
pressed, so a part-finished group is still real data — someone who gets through
300 of 2,000 objects has contributed 300 usable grades. People can revise an
earlier answer at any time.

A skip is the one thing that is *not* an answer. It parks the object, and the
**still unsure** count on the grader's summary page is a link back to exactly
those objects, usable as often as they like. Whatever is still skipped is
reported as `n_unsure` rather than counted as a grade.

---

## Results

**Admin → Groups** lists every group with coverage and how many people have
worked on it. **Admin → a group** shows per-person progress and a **Download
CSV** button for each grader. **Admin → People** lists everyone who has signed
in, and lets you delete an account — useful when a typo creates one that is
never used.

Any signed-in user gets **My progress**, showing their own work only. They never
see anyone else's answers; that would bias the independent second opinion the
whole exercise depends on.

### Exports

**Per grader** — one row per object for one person: their grade, timestamps, and
whether it was a skip. This is the download on the group page.

**`consensus.csv`** — one row per object, pooled across everyone. No button, but
the route is live at `/admin/groups/<slug>/consensus.csv`:

| Field | Meaning |
|---|---|
| `n_votes` | grades cast (skips excluded) |
| `votes_A` … `votes_X` | count per grade |
| `n_unsure`, `unsure_by` | how many left it unsure, and who |
| `majority_grade` | modal grade; ties resolve to the more optimistic one and set `tied` |
| `mean_grade` | mean of A=3, B=2, C=1, X=0 |
| `agreement` | fraction of votes on the modal grade — 1.0 is unanimous |
| `spread` | distance between the harshest and most generous grade |
| `conflict` | `spread >= 2` (A vs C, B vs X) — the tiebreak worklist |
| `lens_fraction` | fraction of votes that were A or B |
| `consensus_is_lens` | `lens_fraction >= 0.5` |
| `graders` | `alice=A,bob=B` — who said what |

**`votes.csv`** — every individual vote in the group, unaggregated. Keep this if
you want to redo the aggregation yourself.

---

## Sign-in and who can see what

There is no password. Someone types a username and they are in; the browser
stays signed in for 60 days. A username that has never been used asks for
confirmation first, so a typo does not silently create a second account.

The username **attributes** grades, it does not authenticate anyone — nothing
stops a person typing someone else's.

> **This is the deliberate trade for zero friction, and it means the app is only
> safe on a private network.** Anyone who can reach the port can sign in as
> anyone, including an admin. Run it behind a VPN, an SSH tunnel, a mesh network
> like Tailscale, or SSO at a reverse proxy. Do not expose it to the internet
> without adding real authentication first.

Groups can also be **hidden**: set `is_hidden = 1` and `owner_netid` and only
that person can see the group, its pages, its images and its exports — useful
for a scratch group you are testing the interface with.

---

## Layout

```
lensinspect/
  __init__.py    create_app() -- config and blueprint wiring
  db.py          schema, connections, grade vocabulary, migrations
  auth.py        sign-in, login_required / admin_required, group visibility
  views.py       group picker, queue, voting, the unsure pile, progress
  admin.py       results, per-grader exports, people, group settings
  aggregate.py   progress and consensus statistics
  ingest.py      CSV parsing, plus the command-line loader
  templates/     base, login, groups, inspect, summary, progress, guide, admin/*
  static/css/app.css
  static/img/guide/   example cutouts shown in the guide
run_inspector.py
GUIDE.md         editable source for the in-app guide text
data/            SQLite database + cutout images (gitignored)
```

Five tables: `inspectors`, `groups`, `candidates`, `votes`, `submissions`. A
candidate name is unique *within* a group, not globally. Columns added after the
first release are applied by `_migrate()` in `db.py` at startup, so upgrading in
place does not need a manual migration.

---

## Deploying

SQLite in WAL mode handles a research group's concurrency fine, but the file has
to survive restarts. On an ephemeral host attach a persistent disk and point
`INSPECTOR_DB` at it, or the database is wiped on every redeploy. Back it up by
copying the file.

```bash
gunicorn 'lensinspect:create_app()' --workers 2 --bind 0.0.0.0:$PORT
```

Multiple workers share the one SQLite file. Set `SECRET_KEY` in the environment
so sessions survive restarts and are consistent across workers.

`start_inspector.sh` is an example of keeping it up with cron: a `@reboot` entry
plus a every-10-minutes watchdog that is a no-op when the app is already
listening.

---

## Licence

MIT — see [LICENSE](LICENSE).
