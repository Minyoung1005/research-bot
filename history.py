import sqlite3
import os
import uuid
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "history.db")


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = _connect()
    with conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id              TEXT PRIMARY KEY,
                channel_id      TEXT NOT NULL DEFAULT '',
                thread_ts       TEXT NOT NULL DEFAULT '',
                machine         TEXT NOT NULL DEFAULT '',
                model           TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                parent_session_id TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_channel_thread
                ON sessions(channel_id, thread_ts);

            CREATE TABLE IF NOT EXISTS messages (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id  TEXT NOT NULL REFERENCES sessions(id),
                role        TEXT NOT NULL,
                content     TEXT NOT NULL DEFAULT '',
                model       TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_messages_session
                ON messages(session_id, created_at);

            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
                USING fts5(
                    content,
                    content=messages,
                    content_rowid=id
                );

            CREATE TRIGGER IF NOT EXISTS messages_ai
                AFTER INSERT ON messages BEGIN
                    INSERT INTO messages_fts(rowid, content)
                        VALUES (new.id, new.content);
                END;

            CREATE TRIGGER IF NOT EXISTS messages_ad
                AFTER DELETE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                END;

            CREATE TRIGGER IF NOT EXISTS messages_au
                AFTER UPDATE ON messages BEGIN
                    INSERT INTO messages_fts(messages_fts, rowid, content)
                        VALUES ('delete', old.id, old.content);
                    INSERT INTO messages_fts(rowid, content)
                        VALUES (new.id, new.content);
                END;
        """)
    conn.close()


def _ensure_session(conn, session_id, channel_id='', thread_ts='', machine='', model=''):
    row = conn.execute("SELECT id FROM sessions WHERE id=?", (session_id,)).fetchone()
    if row is None:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO sessions(id, channel_id, thread_ts, machine, model, created_at) VALUES (?,?,?,?,?,?)",
            (session_id, channel_id, thread_ts, machine, model, now)
        )


def add_message(session_id, role, content, model='', channel_id='', thread_ts='', machine='') -> None:
    conn = _connect()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        _ensure_session(conn, session_id, channel_id, thread_ts, machine, model)
        conn.execute(
            "INSERT INTO messages(session_id, role, content, model, created_at) VALUES (?,?,?,?,?)",
            (session_id, role, content, model, now)
        )
    conn.close()


def get_history(channel_id, thread_ts, limit=12) -> list:
    conn = _connect()
    rows = conn.execute("""
        SELECT m.role, m.content, m.model
        FROM messages m
        JOIN sessions s ON m.session_id = s.id
        WHERE s.channel_id = ? AND s.thread_ts = ?
        ORDER BY m.created_at DESC
        LIMIT ?
    """, (channel_id, thread_ts, limit)).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"], "model": r["model"]} for r in reversed(rows)]


def search(query, channel_id=None, limit=5) -> list:
    conn = _connect()
    if channel_id:
        rows = conn.execute("""
            SELECT s.id AS session_id,
                   snippet(messages_fts, 0, '[', ']', '...', 20) AS snippet,
                   s.thread_ts, s.channel_id, m.created_at
            FROM messages_fts
            JOIN messages m ON messages_fts.rowid = m.id
            JOIN sessions s ON m.session_id = s.id
            WHERE messages_fts MATCH ? AND s.channel_id = ?
            ORDER BY rank
            LIMIT ?
        """, (query, channel_id, limit)).fetchall()
    else:
        rows = conn.execute("""
            SELECT s.id AS session_id,
                   snippet(messages_fts, 0, '[', ']', '...', 20) AS snippet,
                   s.thread_ts, s.channel_id, m.created_at
            FROM messages_fts
            JOIN messages m ON messages_fts.rowid = m.id
            JOIN sessions s ON m.session_id = s.id
            WHERE messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def compress_session(session_id, summary_content, new_session_id) -> None:
    conn = _connect()
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        old = conn.execute(
            "SELECT channel_id, thread_ts, machine, model FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if old is None:
            raise ValueError(f"Session {session_id!r} not found")
        conn.execute(
            """INSERT INTO sessions(id, channel_id, thread_ts, machine, model, created_at, parent_session_id)
               VALUES (?,?,?,?,?,?,?)""",
            (new_session_id, old["channel_id"], old["thread_ts"], old["machine"], old["model"], now, session_id)
        )
        conn.execute(
            "INSERT INTO messages(session_id, role, content, model, created_at) VALUES (?,?,?,?,?)",
            (new_session_id, "assistant", summary_content, "", now)
        )
    conn.close()


def get_recent_sessions(channel_id, limit=20) -> list:
    conn = _connect()
    rows = conn.execute("""
        SELECT id, channel_id, thread_ts, machine, model, created_at, parent_session_id
        FROM sessions
        WHERE channel_id = ?
        ORDER BY created_at DESC
        LIMIT ?
    """, (channel_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
