"""Read-side queries: list, search, get, stats."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class AmbiguousIDError(Exception):
    """Raised when a UUID prefix matches multiple sessions."""

    def __init__(self, prefix: str, matches: list[str]):
        super().__init__(f"prefix '{prefix}' matched {len(matches)} sessions")
        self.prefix = prefix
        self.matches = matches


@dataclass
class SearchResult:
    session_uuid: str
    project_name: str
    ai_title: str | None
    started_at: str
    duration_secs: int | None
    git_branch: str | None
    snippet: str
    rank: float


@dataclass
class StatsRow:
    total_sessions: int
    total_duration_secs: int
    total_input_tokens: int
    total_output_tokens: int
    db_size_bytes: int
    sessions_this_week: int
    top_projects: list[dict]
    top_tools: list[dict]


def _parse_since(since: str | None) -> str | None:
    """Accept ISO date or 'Nd' format. Returns ISO8601 timestamp."""
    if not since:
        return None
    s = since.strip()
    if s.endswith("d") and s[:-1].isdigit():
        days = int(s[:-1])
        dt = datetime.now(timezone.utc) - timedelta(days=days)
        return dt.isoformat()
    return s  # caller is responsible for ISO format


def _escape_fts_query(query: str) -> str:
    """Quote bare words for FTS5 to handle hyphens, slashes, etc.

    Multi-word query is treated as AND across tokens. Wrap each token in
    double quotes; FTS5 ignores punctuation inside quoted phrases.
    """
    if not query:
        return ""
    tokens = [t for t in query.split() if t]
    quoted = ['"' + t.replace('"', '""') + '"' for t in tokens]
    return " ".join(quoted)


def search_sessions(
    conn: sqlite3.Connection,
    query: str,
    project: str | None = None,
    branch: str | None = None,
    since: str | None = None,
    until: str | None = None,
    tool: str | None = None,
    limit: int = 20,
) -> list[SearchResult]:
    fts_query = _escape_fts_query(query)
    if not fts_query:
        return []

    sql = """
        SELECT
            s.session_uuid, s.project_name, s.ai_title, s.started_at,
            s.duration_secs, s.git_branch,
            snippet(sessions_fts, 5, '<mark>', '</mark>', '...', 15) AS snippet,
            rank AS rank
        FROM sessions_fts
        JOIN sessions s ON s.session_uuid = sessions_fts.session_uuid
        WHERE sessions_fts MATCH ?
    """
    params: list = [fts_query]

    if project:
        sql += " AND s.project_name = ?"
        params.append(project)
    if branch:
        sql += " AND s.git_branch = ?"
        params.append(branch)
    if since:
        sql += " AND s.started_at >= ?"
        params.append(_parse_since(since))
    if until:
        sql += " AND s.started_at <= ?"
        params.append(until)
    if tool:
        sql += " AND s.tools_used LIKE ?"
        params.append(f"%{tool}%")

    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    return [
        SearchResult(
            session_uuid=r["session_uuid"],
            project_name=r["project_name"],
            ai_title=r["ai_title"],
            started_at=r["started_at"],
            duration_secs=r["duration_secs"],
            git_branch=r["git_branch"],
            snippet=r["snippet"] or "",
            rank=r["rank"] if r["rank"] is not None else 0.0,
        )
        for r in rows
    ]


def list_sessions(
    conn: sqlite3.Connection,
    project: str | None = None,
    branch: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM sessions WHERE 1=1"
    params: list = []
    if project:
        sql += " AND project_name = ?"
        params.append(project)
    if branch:
        sql += " AND git_branch = ?"
        params.append(branch)
    if since:
        sql += " AND started_at >= ?"
        params.append(_parse_since(since))
    if until:
        sql += " AND started_at <= ?"
        params.append(until)
    sql += " ORDER BY started_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    return conn.execute(sql, params).fetchall()


def count_sessions(
    conn: sqlite3.Connection,
    project: str | None = None,
    branch: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    sql = "SELECT COUNT(*) AS n FROM sessions WHERE 1=1"
    params: list = []
    if project:
        sql += " AND project_name = ?"
        params.append(project)
    if branch:
        sql += " AND git_branch = ?"
        params.append(branch)
    if since:
        sql += " AND started_at >= ?"
        params.append(_parse_since(since))
    if until:
        sql += " AND started_at <= ?"
        params.append(until)
    row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def get_session(conn: sqlite3.Connection, uuid_prefix: str) -> sqlite3.Row | None:
    """Return the unique session whose uuid starts with uuid_prefix.

    Raises AmbiguousIDError if more than one matches. Returns None if zero.
    """
    if not uuid_prefix:
        return None
    rows = conn.execute(
        "SELECT * FROM sessions WHERE session_uuid LIKE ? LIMIT 10",
        (uuid_prefix + "%",),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise AmbiguousIDError(uuid_prefix, [r["session_uuid"] for r in rows])
    return rows[0]


def get_session_messages(conn: sqlite3.Connection, session_uuid: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM messages WHERE session_uuid = ? ORDER BY timestamp ASC",
        (session_uuid,),
    ).fetchall()


def get_stats(conn: sqlite3.Connection) -> StatsRow:
    totals = conn.execute(
        """
        SELECT
            COUNT(*) AS total_sessions,
            COALESCE(SUM(duration_secs), 0) AS total_duration_secs,
            COALESCE(SUM(input_tokens), 0)  AS total_input_tokens,
            COALESCE(SUM(output_tokens), 0) AS total_output_tokens
        FROM sessions
        """
    ).fetchone()

    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week = conn.execute(
        "SELECT COUNT(*) AS n FROM sessions WHERE started_at >= ?", (week_ago,)
    ).fetchone()

    top_projects = [
        dict(r)
        for r in conn.execute(
            """
            SELECT project_name, session_count, last_active
            FROM projects ORDER BY session_count DESC LIMIT 10
            """
        ).fetchall()
    ]

    # Tools: aggregate by parsing JSON arrays in sessions.tools_used.
    tool_counts: dict[str, int] = {}
    for r in conn.execute("SELECT tools_used FROM sessions"):
        try:
            import json
            for t in json.loads(r["tools_used"] or "[]"):
                tool_counts[t] = tool_counts.get(t, 0) + 1
        except Exception:
            continue
    top_tools = [
        {"tool": name, "count": cnt}
        for name, cnt in sorted(tool_counts.items(), key=lambda kv: -kv[1])[:10]
    ]

    db_size = 0
    try:
        from pathlib import Path

        for row in conn.execute("PRAGMA database_list"):
            p = row["file"]
            if p:
                db_size = Path(p).stat().st_size
                break
    except Exception:
        pass

    return StatsRow(
        total_sessions=int(totals["total_sessions"]),
        total_duration_secs=int(totals["total_duration_secs"]),
        total_input_tokens=int(totals["total_input_tokens"]),
        total_output_tokens=int(totals["total_output_tokens"]),
        db_size_bytes=db_size,
        sessions_this_week=int(week["n"]) if week else 0,
        top_projects=top_projects,
        top_tools=top_tools,
    )


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM projects ORDER BY session_count DESC"
    ).fetchall()


def get_timeline(conn: sqlite3.Connection, since_days: int = 365) -> list[dict]:
    """Return per-day session counts going back since_days days.

    Output: [{"date": "YYYY-MM-DD", "count": N}, ...] in date ASC order.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    rows = conn.execute(
        """
        SELECT substr(started_at, 1, 10) AS day, COUNT(*) AS n
        FROM sessions
        WHERE started_at >= ?
        GROUP BY day
        ORDER BY day ASC
        """,
        (since,),
    ).fetchall()
    return [{"date": r["day"], "count": int(r["n"])} for r in rows]
