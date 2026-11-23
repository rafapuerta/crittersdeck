"""SQLite persistence for sources (accounts or machines) and their sessions."""

import hashlib
import secrets
import sqlite3
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    session_id TEXT NOT NULL,
    folder TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    renamed INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    tool TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    started REAL NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (source_id, session_id)
);
"""

_SESSION_FIELDS = ("folder", "title", "renamed", "status", "tool", "detail", "started", "updated")


def hash_token(token):
    # Tokens are 256-bit random values, so a plain digest is enough (no password stretching).
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(_SCHEMA)
        columns = {r[1] for r in self.db.execute("PRAGMA table_info(sessions)")}
        if "title" not in columns:  # databases from before session titles
            self.db.execute("ALTER TABLE sessions ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        if "renamed" not in columns:  # databases from before titles from Claude
            self.db.execute("ALTER TABLE sessions ADD COLUMN renamed INTEGER NOT NULL DEFAULT 0")

    # ---- sources ---------------------------------------------------------

    def add_source(self, name, color):
        """Create a source; returns (source, token). The token is shown only now."""
        token = "cd_" + secrets.token_urlsafe(32)
        source = {"id": secrets.token_hex(6), "name": name, "color": color, "created": time.time()}
        with self.db:
            self.db.execute(
                "INSERT INTO sources (id, name, color, token_hash, created) VALUES (?, ?, ?, ?, ?)",
                (source["id"], name, color, hash_token(token), source["created"]),
            )
        return source, token

    def sources(self):
        rows = self.db.execute("SELECT id, name, color, created FROM sources ORDER BY created")
        return [dict(r) for r in rows]

    def source_by_token(self, token):
        row = self.db.execute(
            "SELECT id, name, color, created FROM sources WHERE token_hash = ?", (hash_token(token),)
        ).fetchone()
        return dict(row) if row else None

    def delete_source(self, source_id):
        with self.db:
            cur = self.db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        return cur.rowcount > 0

    # ---- sessions --------------------------------------------------------

    def session(self, source_id, session_id):
        row = self.db.execute(
            "SELECT * FROM sessions WHERE source_id = ? AND session_id = ?", (source_id, session_id)
        ).fetchone()
        return dict(row) if row else None

    def save_session(self, source_id, session_id, s):
        values = [s.get(f, 0 if f == "renamed" else "") for f in _SESSION_FIELDS]
        with self.db:
            self.db.execute(
                "INSERT INTO sessions (source_id, session_id, folder, title, renamed, status, tool, detail, started, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_id, session_id) DO UPDATE SET folder = excluded.folder, title = excluded.title,"
                " renamed = excluded.renamed,"
                " status = excluded.status, tool = excluded.tool, detail = excluded.detail,"
                " updated = excluded.updated",
                [source_id, session_id, *values],
            )

    def sessions(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM sessions ORDER BY started")]

    def count_sessions(self, source_id):
        return self.db.execute("SELECT COUNT(*) FROM sessions WHERE source_id = ?", (source_id,)).fetchone()[0]

    def evict_oldest(self, source_id):
        """Make room for a new session: drop the one that has been quiet the longest. Returns its id."""
        row = self.db.execute(
            "SELECT session_id FROM sessions WHERE source_id = ? ORDER BY updated LIMIT 1", (source_id,)
        ).fetchone()
        if row:
            self.forget_session(source_id, row[0])
        return row[0] if row else None

    def forget_session(self, source_id, session_id):
        with self.db:
            cur = self.db.execute(
                "DELETE FROM sessions WHERE source_id = ? AND session_id = ?", (source_id, session_id)
            )
        return cur.rowcount > 0

    def prune(self, ended_before):
        """Drop sessions that ended (or went silent) before the given time."""
        with self.db:
            self.db.execute(
                "DELETE FROM sessions WHERE (status = 'ended' AND updated < ?) OR updated < ?",
                (ended_before, ended_before - 6 * 86400),
            )
