#!/usr/bin/env python
"""Start the standalone Lens Inspector.

    python run_inspector.py

Reads its settings from the environment -- see lensinspect/__init__.py for the
full list. The port defaults to 8000 rather than 5000 because macOS binds 5000
to its AirPlay Receiver, which answers with a confusing 403.
"""

from __future__ import annotations

import os
import sys

from lensinspect import create_app
from lensinspect.db import connect


def _load_dotenv() -> None:
    """Read .env before the app reads its config.

    start_inspector.sh already sources .env, but starting the server by hand --
    which is easy to do while debugging -- then silently skipped it and generated
    a throwaway SECRET_KEY, signing every grader out and keeping them that way
    until someone noticed. Doing it here means no start path can regress.
    Anything already exported wins, so the shell can still override the file.
    """
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

app = create_app()


def main() -> int:
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "0.0.0.0")

    conn = connect(app.config["DB_PATH"])
    n_groups = conn.execute("SELECT COUNT(*) AS n FROM groups").fetchone()["n"]
    n_candidates = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
    conn.close()

    print(f"Lens Inspector")
    print(f"  database    {app.config['DB_PATH']}")
    print(f"  cutouts     {app.config['CUTOUT_DIR']}"
          + ("" if os.path.isdir(app.config["CUTOUT_DIR"]) else "  (missing)"))
    print(f"  uploads     {app.config['UPLOAD_IMAGE_DIR']}")
    print(f"  loaded      {n_groups} group(s), {n_candidates} candidate(s)")

    admins = app.config["ADMIN_NETIDS"]
    if admins:
        print(f"  admins      {', '.join(sorted(admins))}")
    else:
        print("  admins      not set -- the FIRST person to sign in becomes admin.")
        print("              Set ADMIN_NETIDS=yournetid to claim it deliberately.")

    if not os.environ.get("SECRET_KEY"):
        print("  WARNING     SECRET_KEY is unset, so a random one was generated.")
        print("              Everyone is signed out on every restart. Set it with:")
        print("              export SECRET_KEY=\"$(python -c 'import secrets;"
              " print(secrets.token_hex(32))')\"")

    print(f"\n  http://localhost:{port}\n")

    app.run(host=host, port=port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
