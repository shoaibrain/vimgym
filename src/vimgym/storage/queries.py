"""Provider-neutral read queries, filters, safe snippets, and keyset cursors."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


class AmbiguousIDError(Exception):
    """Raised when an internal or legacy identifier prefix is ambiguous."""

    def __init__(self, prefix: str, matches: list[str]):
        super().__init__(f"prefix '{prefix}' matched {len(matches)} sessions")
        self.prefix = prefix
        self.matches = matches


class InvalidCursorError(ValueError):
    """Raised for malformed or query-mismatched opaque cursors."""


@dataclass
class SearchResult:
    id: str
    session_uuid: str | None
    external_session_id: str
    provider: str
    kind: str
    lifecycle: str
    project_name: str
    ai_title: str | None
    title: str | None
    started_at: str
    duration_secs: int | None
    git_branch: str | None
    snippet: str
    snippet_parts: list[dict[str, Any]]
    rank: float
    message_id: str
    block_id: str
    parent_session_id: str | None = None
    root_session_id: str | None = None


@dataclass
class StatsRow:
    total_sessions: int
    total_messages: int
    degraded_sessions: int
    degraded_artifacts: int
    total_duration_secs: int
    total_input_tokens: int
    total_output_tokens: int
    db_size_bytes: int
    sessions_this_week: int
    top_projects: list[dict]
    top_tools: list[dict]


def _parse_since(since: str | None) -> str | None:
    if not since:
        return None
    value = since.strip()
    if value.endswith("d") and value[:-1].isdigit():
        dt = datetime.now(timezone.utc) - timedelta(days=int(value[:-1]))
        return dt.isoformat()
    return value


def _escape_fts_query(query: str) -> str:
    if not query:
        return ""
    # The index uses ``detail=none`` to avoid retaining a second copy of block
    # content. FTS5 phrase queries are unavailable in that mode, so tokenize
    # punctuation (including branch-name hyphens) before quoting each literal
    # term. Adjacent terms retain FTS5's implicit AND semantics without exposing
    # operators from untrusted query input.
    tokens = re.findall(r"\w+", query, flags=re.UNICODE)
    return " ".join('"' + token.replace('"', '""') + '"' for token in tokens)


def _query_fingerprint(kind: str, values: dict[str, Any]) -> str:
    payload = json.dumps([kind, values], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, *, fingerprint: str) -> dict[str, Any]:
    if len(cursor) > 4096:
        raise InvalidCursorError("invalid cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidCursorError("invalid cursor") from exc
    if not isinstance(payload, dict) or payload.get("f") != fingerprint:
        raise InvalidCursorError("cursor does not match this query")
    return payload


def _filter_sql(
    *,
    provider: str | None = None,
    source_id: str | None = None,
    kind: str | None = None,
    workspace_id: str | None = None,
    project: str | None = None,
    branch: str | None = None,
    lifecycle: str | None = None,
    since: str | None = None,
    until: str | None = None,
    alias: str = "s",
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for column, value in (
        ("provider", provider),
        ("source_id", source_id),
        ("kind", kind),
        ("workspace_id", workspace_id),
        ("project_name", project),
        ("git_branch", branch),
        ("lifecycle", lifecycle),
    ):
        if value:
            clauses.append(f"{alias}.{column}=?")
            params.append(value)
    if since:
        clauses.append(f"{alias}.started_at>=?")
        params.append(_parse_since(since))
    if until:
        clauses.append(f"{alias}.started_at<=?")
        params.append(until)
    return (" AND ".join(clauses), params)


def list_sessions_page(
    conn: sqlite3.Connection,
    *,
    provider: str | None = None,
    source_id: str | None = None,
    kind: str | None = None,
    workspace_id: str | None = None,
    project: str | None = None,
    branch: str | None = None,
    lifecycle: str | None = None,
    since: str | None = None,
    until: str | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> tuple[list[sqlite3.Row], str | None]:
    limit = max(1, min(int(limit), 100))
    values = {
        "provider": provider,
        "source_id": source_id,
        "kind": kind,
        "workspace_id": workspace_id,
        "project": project,
        "branch": branch,
        "lifecycle": lifecycle,
        "since": since,
        "until": until,
    }
    fingerprint = _query_fingerprint("sessions", values)
    filters, params = _filter_sql(
        provider=provider,
        source_id=source_id,
        kind=kind,
        workspace_id=workspace_id,
        project=project,
        branch=branch,
        lifecycle=lifecycle,
        since=since,
        until=until,
    )
    sql = "SELECT s.* FROM sessions s WHERE 1=1"
    if filters:
        sql += " AND " + filters
    if cursor:
        decoded = decode_cursor(cursor, fingerprint=fingerprint)
        if not isinstance(decoded.get("t"), str) or not isinstance(decoded.get("i"), str):
            raise InvalidCursorError("invalid session cursor payload")
        sql += " AND (s.started_at < ? OR (s.started_at = ? AND s.id < ?))"
        params.extend([decoded["t"], decoded["t"], decoded["i"]])
    sql += " ORDER BY s.started_at DESC, s.id DESC LIMIT ?"
    params.append(limit + 1)
    rows = conn.execute(sql, params).fetchall()
    more = len(rows) > limit
    items = rows[:limit]
    next_cursor = None
    if more and items:
        last = items[-1]
        next_cursor = encode_cursor({"f": fingerprint, "t": last["started_at"], "i": last["id"]})
    return items, next_cursor


def list_sessions(
    conn: sqlite3.Connection,
    project: str | None = None,
    branch: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """Legacy offset wrapper retained for direct v0.1 callers."""
    filters, params = _filter_sql(project=project, branch=branch, since=since, until=until)
    sql = "SELECT s.* FROM sessions s WHERE 1=1"
    if filters:
        sql += " AND " + filters
    sql += " ORDER BY s.started_at DESC, s.id DESC LIMIT ? OFFSET ?"
    params.extend([min(max(limit, 1), 500), max(offset, 0)])
    return conn.execute(sql, params).fetchall()


def count_sessions(
    conn: sqlite3.Connection,
    project: str | None = None,
    branch: str | None = None,
    since: str | None = None,
    until: str | None = None,
    *,
    provider: str | None = None,
    source_id: str | None = None,
    kind: str | None = None,
    workspace_id: str | None = None,
    lifecycle: str | None = None,
) -> int:
    filters, params = _filter_sql(
        provider=provider,
        source_id=source_id,
        kind=kind,
        workspace_id=workspace_id,
        project=project,
        branch=branch,
        lifecycle=lifecycle,
        since=since,
        until=until,
    )
    sql = "SELECT COUNT(*) n FROM sessions s WHERE 1=1"
    if filters:
        sql += " AND " + filters
    row = conn.execute(sql, params).fetchone()
    return int(row["n"]) if row else 0


def get_session(conn: sqlite3.Connection, identifier: str) -> sqlite3.Row | None:
    """Resolve internal ID first, then unique legacy/provider identifier prefix."""
    if not identifier:
        return None
    exact = conn.execute("SELECT * FROM sessions WHERE id=?", (identifier,)).fetchone()
    if exact is not None:
        return exact
    escaped = identifier.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    rows = conn.execute(
        """
        SELECT * FROM sessions
        WHERE id LIKE ? ESCAPE '!' OR external_session_id LIKE ? ESCAPE '!'
           OR session_uuid LIKE ? ESCAPE '!'
        ORDER BY id LIMIT 11
        """,
        (escaped + "%", escaped + "%", escaped + "%"),
    ).fetchall()
    unique = {row["id"]: row for row in rows}
    if not unique:
        return None
    if len(unique) > 1:
        raise AmbiguousIDError(
            identifier,
            [str(row["external_session_id"] or row["id"]) for row in unique.values()],
        )
    return next(iter(unique.values()))


def _block_payload(row: sqlite3.Row, *, preview_bytes: int = 8192) -> dict[str, Any]:
    text = row["text_content"]
    try:
        data = json.loads(row["data_json"] or "{}")
    except json.JSONDecodeError:
        data = row["data_json"]
    encoded = json.dumps(
        {"text": text, "data": data}, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    truncated = len(encoded) > 65536
    if truncated:
        preview_source = text or json.dumps(data, ensure_ascii=False)
        text = preview_source.encode("utf-8")[:preview_bytes].decode("utf-8", errors="replace")
        data = None
    return {
        "id": row["id"],
        "kind": row["kind"],
        "visibility": row["visibility"],
        "text": text,
        "data": data,
        "name": row["name"],
        "call_id": row["call_id"],
        "mime_type": row["mime_type"],
        "is_error": bool(row["is_error"]),
        "truncated": truncated,
        "content_url": f"/api/message-blocks/{row['id']}" if truncated else None,
        "original_bytes": int(row["original_bytes"] or len(encoded)),
    }


def get_session_messages_page(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 100,
) -> tuple[list[dict[str, Any]], int | None]:
    limit = max(1, min(int(limit), 200))
    messages = conn.execute(
        """
        SELECT * FROM messages WHERE session_id=? AND sequence>?
        ORDER BY sequence, item_index LIMIT ?
        """,
        (session_id, max(0, after_sequence), limit + 1),
    ).fetchall()
    more = len(messages) > limit
    page = messages[:limit]
    result: list[dict[str, Any]] = []
    response_budget = 1_800_000
    used_bytes = 0
    budget_limited = False
    for message in page:
        blocks = conn.execute(
            "SELECT * FROM message_blocks WHERE message_id=? ORDER BY block_index",
            (message["id"],),
        ).fetchall()
        payload = {
            "id": message["id"],
            "external_message_id": message["external_message_id"],
            "sequence": message["sequence"],
            "role": message["role"],
            "model": message["model"],
            "turn_id": message["turn_id"],
            "timestamp": message["timestamp"],
            "parent_message_id": message["parent_message_id"],
            "blocks": [_block_payload(block) for block in blocks],
        }
        payload_size = len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        if used_bytes + payload_size > response_budget and result:
            budget_limited = True
            break
        if payload_size > response_budget:
            # A single pathological message is still navigable: each block is
            # reduced to a bounded preview and links to the explicit full-block
            # endpoint, which is the only unbounded response surface.
            for block in payload["blocks"]:
                serialized = block.get("text") or json.dumps(block.get("data"), ensure_ascii=False)
                block["text"] = str(serialized)[:8192]
                block["data"] = None
                block["truncated"] = True
                block["content_url"] = f"/api/message-blocks/{block['id']}"
            payload_size = len(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            )
            while payload_size > response_budget and len(payload["blocks"]) > 1:
                payload["blocks"].pop()
                payload_size = len(
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                )
        result.append(payload)
        used_bytes += payload_size
    has_more = more or budget_limited or len(result) < len(page)
    next_sequence = int(result[-1]["sequence"]) if has_more and result else None
    return result, next_sequence


def get_session_messages(conn: sqlite3.Connection, session_identifier: str) -> list[sqlite3.Row]:
    """Legacy raw-row accessor used only by old export/tests."""
    session = get_session(conn, session_identifier)
    internal = session["id"] if session is not None else session_identifier
    return conn.execute(
        "SELECT * FROM messages WHERE session_id=? ORDER BY sequence, item_index",
        (internal,),
    ).fetchall()


def get_message_block(conn: sqlite3.Connection, block_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM message_blocks WHERE id=?", (block_id,)).fetchone()


def _snippet_parts(text: str, query: str, *, max_chars: int = 500) -> list[dict[str, Any]]:
    tokens = [token for token in query.split() if token]
    if not tokens:
        return [{"text": text[:max_chars], "matched": False}]
    pattern = re.compile("(" + "|".join(re.escape(token) for token in tokens) + ")", re.I)
    first = pattern.search(text)
    start = max(0, (first.start() if first else 0) - 160)
    end = min(len(text), start + max_chars)
    excerpt = text[start:end]
    parts: list[dict[str, Any]] = []
    if start:
        parts.append({"text": "…", "matched": False})
    cursor = 0
    for match in pattern.finditer(excerpt):
        if match.start() > cursor:
            parts.append({"text": excerpt[cursor : match.start()], "matched": False})
        parts.append({"text": match.group(0), "matched": True})
        cursor = match.end()
    if cursor < len(excerpt):
        parts.append({"text": excerpt[cursor:], "matched": False})
    if end < len(text):
        parts.append({"text": "…", "matched": False})
    return parts


def search_sessions_page(
    conn: sqlite3.Connection,
    query: str,
    *,
    provider: str | None = None,
    source_id: str | None = None,
    kind: str | None = None,
    workspace_id: str | None = None,
    project: str | None = None,
    branch: str | None = None,
    lifecycle: str | None = None,
    since: str | None = None,
    until: str | None = None,
    tool: str | None = None,
    cursor: str | None = None,
    limit: int = 20,
) -> tuple[list[SearchResult], str | None]:
    fts_query = _escape_fts_query(query)
    if not fts_query:
        return [], None
    limit = max(1, min(int(limit), 100))
    values = {
        "q": query,
        "provider": provider,
        "source_id": source_id,
        "kind": kind,
        "workspace_id": workspace_id,
        "project": project,
        "branch": branch,
        "lifecycle": lifecycle,
        "since": since,
        "until": until,
        "tool": tool,
    }
    fingerprint = _query_fingerprint("search", values)
    filters, filter_params = _filter_sql(
        provider=provider,
        source_id=source_id,
        kind=kind,
        workspace_id=workspace_id,
        project=project,
        branch=branch,
        lifecycle=lifecycle,
        since=since,
        until=until,
    )
    sql = """
        SELECT * FROM (
            SELECT s.*, b.id _block_id, b.message_id _message_id,
                   (substr(COALESCE(b.text_content,''),1,32768) || char(10) ||
                    substr(COALESCE(b.data_json,''),1,32768)) _searchable_text,
                   bm25(message_fts) _rank
            FROM message_fts f
            JOIN message_blocks b ON b.rowid=f.rowid
            JOIN sessions s ON s.id=b.session_id
            WHERE message_fts MATCH ?
    """
    params: list[Any] = [fts_query]
    if filters:
        sql += " AND " + filters
        params.extend(filter_params)
    if tool:
        sql += " AND EXISTS (SELECT 1 FROM session_tools st WHERE st.session_id=s.id AND st.tool_name=?)"
        params.append(tool)
    sql += ") ranked WHERE 1=1"
    if cursor:
        decoded = decode_cursor(cursor, fingerprint=fingerprint)
        rank = decoded.get("r")
        if (
            not isinstance(rank, (int, float))
            or isinstance(rank, bool)
            or not math.isfinite(float(rank))
            or not isinstance(decoded.get("m"), str)
            or not isinstance(decoded.get("b"), str)
        ):
            raise InvalidCursorError("invalid search cursor payload")
        sql += " AND (_rank>? OR (_rank=? AND (_message_id>? OR (_message_id=? AND _block_id>?))))"
        params.extend([decoded["r"], decoded["r"], decoded["m"], decoded["m"], decoded["b"]])
    sql += " ORDER BY _rank, _message_id, _block_id LIMIT ?"
    params.append(limit + 1)
    rows = conn.execute(sql, params).fetchall()
    more = len(rows) > limit
    page = rows[:limit]
    results: list[SearchResult] = []
    for row in page:
        parts = _snippet_parts(str(row["_searchable_text"] or ""), query)
        results.append(
            SearchResult(
                id=row["id"],
                session_uuid=row["session_uuid"],
                external_session_id=row["external_session_id"],
                provider=row["provider"],
                kind=row["kind"],
                lifecycle=row["lifecycle"],
                project_name=row["project_name"],
                ai_title=row["ai_title"],
                title=row["title"],
                started_at=row["started_at"],
                duration_secs=row["duration_secs"],
                git_branch=row["git_branch"],
                snippet="".join(str(part["text"]) for part in parts),
                snippet_parts=parts,
                rank=float(row["_rank"] or 0.0),
                message_id=row["_message_id"],
                block_id=row["_block_id"],
                parent_session_id=row["parent_session_id"],
                root_session_id=row["root_session_id"],
            )
        )
    next_cursor = None
    if more and page:
        last = page[-1]
        next_cursor = encode_cursor(
            {"f": fingerprint, "r": last["_rank"], "m": last["_message_id"], "b": last["_block_id"]}
        )
    return results, next_cursor


def search_sessions(
    conn: sqlite3.Connection,
    query: str,
    project: str | None = None,
    branch: str | None = None,
    since: str | None = None,
    until: str | None = None,
    tool: str | None = None,
    limit: int = 20,
    *,
    provider: str | None = None,
    kind: str | None = None,
    lifecycle: str | None = None,
) -> list[SearchResult]:
    results, _ = search_sessions_page(
        conn,
        query,
        project=project,
        branch=branch,
        since=since,
        until=until,
        tool=tool,
        limit=limit,
        provider=provider,
        kind=kind,
        lifecycle=lifecycle,
    )
    return results


def get_stats(conn: sqlite3.Connection) -> StatsRow:
    totals = conn.execute(
        """
        SELECT COUNT(*) total_sessions,
               (SELECT COUNT(*) FROM messages) total_messages,
               SUM(CASE WHEN health='degraded' THEN 1 ELSE 0 END) degraded_sessions,
               (SELECT COUNT(*) FROM source_artifacts
                WHERE status IN ('degraded','failed')) degraded_artifacts,
               COALESCE(SUM(duration_secs),0) total_duration_secs,
               COALESCE(SUM(input_tokens),0) total_input_tokens,
               COALESCE(SUM(output_tokens),0) total_output_tokens
        FROM sessions
        """
    ).fetchone()
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    week = conn.execute(
        "SELECT COUNT(*) n FROM sessions WHERE started_at>=?", (week_ago,)
    ).fetchone()
    top_projects = [
        dict(row)
        for row in conn.execute(
            "SELECT project_name,session_count,last_active FROM projects ORDER BY session_count DESC LIMIT 10"
        )
    ]
    top_tools = [
        {"tool": row["tool_name"], "count": int(row["n"])}
        for row in conn.execute(
            "SELECT tool_name, SUM(use_count) n FROM session_tools GROUP BY tool_name ORDER BY n DESC LIMIT 10"
        )
    ]
    db_size = 0
    try:
        from pathlib import Path

        for row in conn.execute("PRAGMA database_list"):
            if row["file"]:
                db_size = Path(row["file"]).stat().st_size
                break
    except OSError:
        pass
    return StatsRow(
        total_sessions=int(totals["total_sessions"]),
        total_messages=int(totals["total_messages"]),
        degraded_sessions=int(totals["degraded_sessions"] or 0),
        degraded_artifacts=int(totals["degraded_artifacts"]),
        total_duration_secs=int(totals["total_duration_secs"]),
        total_input_tokens=int(totals["total_input_tokens"]),
        total_output_tokens=int(totals["total_output_tokens"]),
        db_size_bytes=db_size,
        sessions_this_week=int(week["n"]),
        top_projects=top_projects,
        top_tools=top_tools,
    )


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects ORDER BY session_count DESC").fetchall()


def get_timeline(conn: sqlite3.Connection, since_days: int = 365) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
    rows = conn.execute(
        """
        SELECT substr(started_at,1,10) day, COUNT(*) n FROM sessions
        WHERE started_at>=? GROUP BY day ORDER BY day
        """,
        (since,),
    ).fetchall()
    return [{"date": row["day"], "count": int(row["n"])} for row in rows]
