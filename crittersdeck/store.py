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
    info TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    tool TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    started REAL NOT NULL,
    updated REAL NOT NULL,
    PRIMARY KEY (source_id, session_id)
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created REAL NOT NULL,
    from_source TEXT NOT NULL DEFAULT '',   -- '' when written on the board
    from_session TEXT NOT NULL DEFAULT '',
    to_kind TEXT NOT NULL,                  -- session | source | all
    to_source TEXT NOT NULL DEFAULT '',
    to_session TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    ref TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS rooms (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created REAL NOT NULL,
    closed REAL                             -- NULL while open; closed rooms are read-only, then pruned
);
CREATE TABLE IF NOT EXISTS room_members (
    room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    joined REAL NOT NULL,
    PRIMARY KEY (room_id, source_id, session_id)
);
CREATE TABLE IF NOT EXISTS deliveries (
    message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    source_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    PRIMARY KEY (message_id, source_id, session_id)
);
"""

_SESSION_FIELDS = ("folder", "title", "renamed", "info", "status", "tool", "detail", "started", "updated")


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
        if "info" not in columns:  # databases from before session usage
            self.db.execute("ALTER TABLE sessions ADD COLUMN info TEXT NOT NULL DEFAULT '{}'")
        if "room_id" not in {r[1] for r in self.db.execute("PRAGMA table_info(messages)")}:  # before rooms
            self.db.execute("ALTER TABLE messages ADD COLUMN room_id TEXT NOT NULL DEFAULT ''")
        self.db.execute("CREATE INDEX IF NOT EXISTS messages_room ON messages(room_id)")

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
            self._drop_orphan_members()
        return cur.rowcount > 0

    # ---- sessions --------------------------------------------------------

    def session(self, source_id, session_id):
        row = self.db.execute(
            "SELECT * FROM sessions WHERE source_id = ? AND session_id = ?", (source_id, session_id)
        ).fetchone()
        return dict(row) if row else None

    def save_session(self, source_id, session_id, s):
        defaults = {"renamed": 0, "info": "{}"}
        values = [s.get(f, defaults.get(f, "")) for f in _SESSION_FIELDS]
        with self.db:
            self.db.execute(
                "INSERT INTO sessions (source_id, session_id, folder, title, renamed, info, status, tool, detail, started, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (source_id, session_id) DO UPDATE SET folder = excluded.folder, title = excluded.title,"
                " renamed = excluded.renamed, info = excluded.info,"
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
            self._drop_orphan_members()
        return cur.rowcount > 0

    # ---- messages between sessions ---------------------------------------------

    def add_message(self, m):
        with self.db:
            cur = self.db.execute(
                "INSERT INTO messages (created, from_source, from_session, to_kind, to_source, to_session, text, ref, room_id)"
                " VALUES (:created, :from_source, :from_session, :to_kind, :to_source, :to_session, :text, :ref, :room_id)",
                {"room_id": "", **m})
        return cur.lastrowid

    # A room message reaches the members that joined before it was posted, while the room is open.
    _FOR = ("((to_kind = 'session' AND to_source = :src AND to_session = :sid)"
            " OR (to_kind = 'source' AND to_source = :src) OR to_kind = 'all'"
            " OR (to_kind = 'room' AND EXISTS (SELECT 1 FROM room_members rm JOIN rooms r ON r.id = rm.room_id"
            " WHERE rm.room_id = messages.room_id AND rm.source_id = :src AND rm.session_id = :sid"
            " AND rm.joined <= messages.created AND r.closed IS NULL)))"
            " AND NOT (from_source = :src AND from_session = :sid)")

    def pending(self, source_id, session_id, since):
        """(room_id, from_source) of each message waiting for a session, without delivering them."""
        return [tuple(r) for r in self.db.execute(
            f"SELECT room_id, from_source FROM messages WHERE {self._FOR} AND created >= :since AND id NOT IN"
            " (SELECT message_id FROM deliveries WHERE source_id = :src AND session_id = :sid)",
            {"src": source_id, "sid": session_id, "since": since})]

    def inbox(self, source_id, session_id, since, limit=20, max_chars=8000):
        """Messages for a session not yet delivered to it, oldest first, up to `max_chars` of text
        (at least one); marks them delivered."""
        args = {"src": source_id, "sid": session_id, "since": since, "limit": limit}
        rows = [dict(r) for r in self.db.execute(
            f"SELECT * FROM messages WHERE {self._FOR} AND created >= :since AND id NOT IN"
            " (SELECT message_id FROM deliveries WHERE source_id = :src AND session_id = :sid)"
            " ORDER BY id LIMIT :limit", args)]
        total, kept = 0, []
        for r in rows:
            total += len(r["text"]) + len(r["ref"])
            if kept and total > max_chars:
                break
            kept.append(r)
        rows = kept
        with self.db:
            self.db.executemany("INSERT OR IGNORE INTO deliveries VALUES (?, ?, ?)",
                                [(r["id"], source_id, session_id) for r in rows])
        return rows

    def unread(self, since_by_session):
        """{(source, session): count} of messages waiting, for the board."""
        out = {}
        for (src, sid), since in since_by_session.items():
            n = self.db.execute(
                f"SELECT COUNT(*) FROM messages WHERE {self._FOR} AND created >= :since AND id NOT IN"
                " (SELECT message_id FROM deliveries WHERE source_id = :src AND session_id = :sid)",
                {"src": src, "sid": sid, "since": since}).fetchone()[0]
            if n:
                out[(src, sid)] = n
        return out

    def thread(self, source_id, session_id, limit=50):
        """Messages to or from a session (newest last), with whether each reached it."""
        args = {"src": source_id, "sid": session_id, "limit": limit}
        rows = self.db.execute(  # direct messages only: room messages are in the room
            f"SELECT messages.*, EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id = messages.id AND d.source_id = :src"
            f" AND d.session_id = :sid) AS delivered FROM messages WHERE (({self._FOR})"
            " OR (from_source = :src AND from_session = :sid)) AND to_kind != 'room' ORDER BY id DESC LIMIT :limit", args)
        return [dict(r) for r in rows][::-1]

    # ---- rooms ---------------------------------------------------------------------

    def add_room(self, name):
        room = {"id": secrets.token_hex(6), "name": name, "created": time.time(), "closed": None}
        with self.db:
            self.db.execute("INSERT INTO rooms (id, name, created) VALUES (:id, :name, :created)", room)
        return room

    def room(self, room_id):
        row = self.db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
        return dict(row) if row else None

    def rooms(self):
        """Every room (open ones, and closed ones until pruned), with members and message figures."""
        members, figures = {}, {}
        for m in self.db.execute("SELECT room_id, source_id, session_id, joined FROM room_members ORDER BY joined"):
            members.setdefault(m["room_id"], []).append({k: m[k] for k in ("source_id", "session_id", "joined")})
        for f in self.db.execute("SELECT room_id, COUNT(*) AS n, MAX(created) AS last FROM messages"
                                 " WHERE to_kind = 'room' GROUP BY room_id"):
            figures[f["room_id"]] = (f["n"], f["last"])
        delivered = dict(self.db.execute(
            "SELECT m.room_id, COUNT(*) FROM deliveries d JOIN messages m ON m.id = d.message_id"
            " WHERE m.to_kind = 'room' GROUP BY m.room_id").fetchall())
        return [{**dict(r), "members": members.get(r["id"], []), "messages": figures.get(r["id"], (0, None))[0],
                 "last": figures.get(r["id"], (0, None))[1], "delivered": delivered.get(r["id"], 0)}
                for r in self.db.execute("SELECT * FROM rooms ORDER BY created")]

    def drop_room(self, room_id):
        """Undo a room that could not be set up (members and messages go with it)."""
        with self.db:
            self.db.execute("DELETE FROM messages WHERE to_kind = 'room' AND room_id = ?", (room_id,))
            # and the welcome notices already sent for it (board messages naming the room's unique id)
            self.db.execute("DELETE FROM messages WHERE to_kind = 'session' AND from_source = '' AND instr(text, ?) > 0",
                            (f"(room:{room_id})",))
            self.db.execute("DELETE FROM rooms WHERE id = ?", (room_id,))

    def room_names(self):
        return dict(self.db.execute("SELECT id, name FROM rooms").fetchall())

    def rename_room(self, room_id, name):
        with self.db:
            return self.db.execute("UPDATE rooms SET name = ? WHERE id = ?", (name, room_id)).rowcount > 0

    def close_room(self, room_id):
        with self.db:
            return self.db.execute("UPDATE rooms SET closed = ? WHERE id = ? AND closed IS NULL",
                                   (time.time(), room_id)).rowcount > 0

    def add_member(self, room_id, source_id, session_id):
        """True when added, False when it was a member already."""
        with self.db:
            return self.db.execute("INSERT OR IGNORE INTO room_members VALUES (?, ?, ?, ?)",
                                   (room_id, source_id, session_id, time.time())).rowcount > 0

    def remove_member(self, room_id, source_id, session_id):
        with self.db:
            return self.db.execute("DELETE FROM room_members WHERE room_id = ? AND source_id = ? AND session_id = ?",
                                   (room_id, source_id, session_id)).rowcount > 0

    def messaged_since(self, source_id, session_id, since):
        """Whether any message for a session was posted since `since`."""
        return self.db.execute(f"SELECT EXISTS (SELECT 1 FROM messages WHERE {self._FOR} AND created >= :since)",
                               {"src": source_id, "sid": session_id, "since": since}).fetchone()[0] == 1

    def member_rooms(self, source_id, session_id):
        """Open rooms a session belongs to."""
        return [dict(r) for r in self.db.execute(
            "SELECT r.* FROM rooms r JOIN room_members m ON m.room_id = r.id WHERE m.source_id = ? AND m.session_id = ?"
            " AND r.closed IS NULL ORDER BY r.created", (source_id, session_id))]

    def room_messages(self, room_id, limit=50, before=None):
        """A room's messages, newest last, each with its recipients (the current members that had joined
        when it was posted, but not its sender) and how many of them it reached."""
        recipient = ("room_members rm WHERE rm.room_id = messages.room_id AND rm.joined <= messages.created"
                     " AND NOT (rm.source_id = messages.from_source AND rm.session_id = messages.from_session)")
        rows = self.db.execute(
            f"SELECT messages.*, (SELECT COUNT(*) FROM {recipient}) AS recipients,"
            f" (SELECT COUNT(*) FROM {recipient} AND EXISTS (SELECT 1 FROM deliveries d WHERE d.message_id = messages.id"
            "  AND d.source_id = rm.source_id AND d.session_id = rm.session_id)) AS delivered"
            " FROM messages WHERE to_kind = 'room' AND room_id = ? AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
            (room_id, before, before, limit))
        return [dict(r) for r in rows][::-1]

    def _drop_orphan_members(self):
        self.db.execute("DELETE FROM room_members WHERE NOT EXISTS (SELECT 1 FROM sessions s"
                        " WHERE s.source_id = room_members.source_id AND s.session_id = room_members.session_id)")

    def prune(self, ended_before):
        """Drop sessions that ended (or went silent) before the given time."""
        with self.db:
            self.db.execute(
                "DELETE FROM sessions WHERE (status = 'ended' AND updated < ?) OR updated < ?",
                (ended_before, ended_before - 6 * 86400),
            )
            self.db.execute("DELETE FROM messages WHERE created < ?", (ended_before - 6 * 86400,))
            self._drop_orphan_members()  # an ended session stays a (dimmed) member until it is pruned
            week_ago = time.time() - 7 * 86400
            self.db.execute("DELETE FROM messages WHERE to_kind = 'room' AND room_id IN"
                            " (SELECT id FROM rooms WHERE closed < ?)", (week_ago,))
            self.db.execute("DELETE FROM rooms WHERE closed < ?", (week_ago,))
