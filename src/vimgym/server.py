"""Loopback-only FastAPI server, provider-neutral API, WebSocket, and UI."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue as _queue
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from vimgym import __version__
from vimgym.config import AppConfig, save_config, validate_loopback_host
from vimgym.db import SCHEMA_VERSION, get_connection
from vimgym.events import event_queue
from vimgym.storage.export import iter_session_canonical_jsonl, iter_session_markdown
from vimgym.storage.queries import (
    AmbiguousIDError,
    InvalidCursorError,
    count_sessions,
    get_message_block,
    get_session,
    get_session_messages_page,
    get_stats,
    get_timeline,
    list_projects,
    list_sessions_page,
    search_sessions_page,
)

logger = logging.getLogger(__name__)
_LOOPBACK_NAMES = {"127.0.0.1", "localhost", "::1"}


def _host_name(authority: str) -> str:
    value = authority.strip().lower()
    if value.startswith("[") and "]" in value:
        return value[1 : value.index("]")]
    if value.count(":") == 1:
        return value.rsplit(":", 1)[0]
    return value


def _allowed_authority(authority: str | None) -> bool:
    # ``testserver`` is Starlette's in-process ASGI authority; no socket can
    # bind through this exception. Runtime Uvicorn remains loopback-only.
    return bool(authority) and _host_name(authority or "") in (_LOOPBACK_NAMES | {"testserver"})


def _allowed_origin(origin: str | None, port: int) -> bool:
    if not origin:
        return True
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and (parsed.hostname or "").lower() in _LOOPBACK_NAMES
        and (parsed.port or (443 if parsed.scheme == "https" else 80)) == port
    )


def _security_headers(port: int) -> dict[str, str]:
    connect = f"'self' ws://127.0.0.1:{port} ws://localhost:{port} ws://[::1]:{port}"
    return {
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; "
            f"connect-src {connect}; object-src 'none'; base-uri 'none'; "
            "frame-ancestors 'none'; form-action 'self'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Cross-Origin-Resource-Policy": "same-origin",
        "X-Frame-Options": "DENY",
    }


def _safe_export_stem(value: Any) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "session")).strip("-.")
    return stem[:80] or "session"


class WSManager:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self.connections.add(ws)

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            self.connections.discard(ws)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        serialized = json.dumps(payload, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self.connections):
            try:
                await ws.send_text(serialized)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.connections.discard(ws)


def _row_to_dict(row: Any, *, derived_limit: int = 200) -> dict[str, Any]:
    data = dict(row)
    for key in ("tools_used", "files_modified", "diagnostics_json", "config_json"):
        if isinstance(data.get(key), str):
            try:
                data[key.removesuffix("_json")] = json.loads(data[key])
            except json.JSONDecodeError:
                data[key.removesuffix("_json")] = [] if key != "config_json" else {}
            if key.endswith("_json"):
                data.pop(key, None)
    for key in ("tools_used", "files_modified"):
        values = data.get(key)
        if isinstance(values, list):
            data[f"{key}_truncated"] = len(values) > derived_limit
            data[key] = [str(value)[:1024] for value in values[:derived_limit]]
    if data.get("provider") != "claude_code":
        data.pop("session_uuid", None)
    return data


def _resolve_session(conn: Any, identifier: str) -> Any:
    try:
        row = get_session(conn, identifier)
    except AmbiguousIDError as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "ambiguous_id", "matches": exc.matches},
        ) from exc
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


def create_app(config: AppConfig) -> FastAPI:
    validate_loopback_host(config.server_host)
    ws_manager = WSManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state._broadcaster_stop = False

        async def pump() -> None:
            loop = asyncio.get_running_loop()

            def next_event() -> dict[str, Any] | None:
                try:
                    return event_queue.get(timeout=0.25)
                except _queue.Empty:
                    return None

            while not app.state._broadcaster_stop:
                event = await loop.run_in_executor(None, next_event)
                if event is None:
                    continue
                if event.get("type") == "shutdown":
                    break
                await ws_manager.broadcast(event)

        app.state._broadcaster = asyncio.create_task(pump())
        try:
            yield
        finally:
            app.state._broadcaster_stop = True
            task = getattr(app.state, "_broadcaster", None)
            if task:
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()

    app = FastAPI(title="vimgym", version=__version__, lifespan=lifespan)
    app.state.config = config
    app.state.ws_manager = ws_manager
    import time as _time

    app.state._boot_monotonic = _time.monotonic()

    @app.middleware("http")
    async def localhost_security(request: Request, call_next):
        if not _allowed_authority(request.headers.get("host")):
            return JSONResponse(
                {"detail": "untrusted Host header"},
                status_code=400,
                headers={**_security_headers(config.server_port), "Cache-Control": "no-store"},
            )
        if not _allowed_origin(request.headers.get("origin"), config.server_port):
            return JSONResponse(
                {"detail": "untrusted Origin header"},
                status_code=403,
                headers={**_security_headers(config.server_port), "Cache-Control": "no-store"},
            )
        response = await call_next(request)
        for key, value in _security_headers(config.server_port).items():
            response.headers[key] = value
        if request.url.path.startswith("/api/") or request.url.path == "/health":
            response.headers["Cache-Control"] = "no-store"
        return response

    def conn():
        return get_connection(config.db_path)

    @app.get("/health")
    def health() -> dict[str, Any]:
        c = conn()
        import time as _time

        return {
            "status": "ok",
            "version": __version__,
            "schema_version": SCHEMA_VERSION,
            "pid": os.getpid(),
            "sessions": int(c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]),
            "uptime_secs": int(_time.monotonic() - app.state._boot_monotonic),
        }

    @app.get("/api/sessions")
    def api_sessions(
        provider: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        workspace: str | None = None,
        project: str | None = None,
        branch: str | None = None,
        lifecycle: str | None = None,
        since: str | None = None,
        until: str | None = None,
        cursor: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            rows, next_cursor = list_sessions_page(
                conn(),
                provider=provider,
                source_id=source,
                kind=kind,
                workspace_id=workspace,
                project=project,
                branch=branch,
                lifecycle=lifecycle,
                since=since,
                until=until,
                cursor=cursor,
                limit=limit,
            )
        except InvalidCursorError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        items = []
        for row in rows:
            item = _row_to_dict(row, derived_limit=4)
            item.pop("diagnostics", None)
            items.append(item)
        total = count_sessions(
            conn(),
            project,
            branch,
            since,
            until,
            provider=provider,
            source_id=source,
            kind=kind,
            workspace_id=workspace,
            lifecycle=lifecycle,
        )
        return {
            "items": items,
            "next_cursor": next_cursor,
            # Transitional aliases retained throughout v0.2.
            "sessions": items,
            "total": total,
            "has_more": next_cursor is not None,
        }

    @app.get("/api/sessions/{identifier}")
    def api_session_detail(identifier: str) -> dict[str, Any]:
        return _row_to_dict(_resolve_session(conn(), identifier))

    @app.get("/api/sessions/{identifier}/messages")
    def api_session_messages(
        identifier: str,
        after_sequence: int = Query(0, ge=0),
        cursor: int | None = Query(None, ge=0),
        limit: int = Query(100, ge=1, le=200),
    ) -> dict[str, Any]:
        session = _resolve_session(conn(), identifier)
        items, next_sequence = get_session_messages_page(
            conn(),
            session["id"],
            after_sequence=cursor if cursor is not None else after_sequence,
            limit=limit,
        )
        return {
            "items": items,
            "messages": items,
            "next_cursor": next_sequence,
            "next_sequence": next_sequence,
        }

    @app.get("/api/message-blocks/{block_id}")
    def api_message_block(block_id: str) -> dict[str, Any]:
        block = get_message_block(conn(), block_id)
        if block is None:
            raise HTTPException(404, detail="message block not found")
        data: Any = block["data_json"]
        if data:
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                pass
        return {
            "id": block["id"],
            "kind": block["kind"],
            "visibility": block["visibility"],
            "text": block["text_content"],
            "data": data,
            "name": block["name"],
            "call_id": block["call_id"],
            "mime_type": block["mime_type"],
            "is_error": bool(block["is_error"]),
        }

    @app.get("/api/search")
    def api_search(
        q: str = Query(..., min_length=1),
        provider: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        workspace: str | None = None,
        project: str | None = None,
        branch: str | None = None,
        lifecycle: str | None = None,
        since: str | None = None,
        until: str | None = None,
        tool: str | None = None,
        cursor: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            rows, next_cursor = search_sessions_page(
                conn(),
                q,
                provider=provider,
                source_id=source,
                kind=kind,
                workspace_id=workspace,
                project=project,
                branch=branch,
                lifecycle=lifecycle,
                since=since,
                until=until,
                tool=tool,
                cursor=cursor,
                limit=limit,
            )
        except InvalidCursorError as exc:
            raise HTTPException(400, detail=str(exc)) from exc
        items = []
        for row in rows:
            item = dict(row.__dict__)
            if item.get("provider") != "claude_code":
                item.pop("session_uuid", None)
            items.append(item)
        return {
            "query": q,
            "items": items,
            "results": items,
            "total": len(items),
            "next_cursor": next_cursor,
        }

    @app.get("/api/sources")
    @app.get("/api/config/sources")
    def api_sources() -> dict[str, Any]:
        db_sources = {row["id"]: dict(row) for row in conn().execute("SELECT * FROM sources")}
        items = []
        for source in config.sources:
            stored = db_sources.get(source.id, {})
            items.append(
                {
                    "id": source.id,
                    "name": source.name,
                    "type": source.type,
                    "provider": source.type,
                    "path": source.path,
                    "enabled": source.enabled and source.supported,
                    "exists": source.exists(),
                    "auto_detected": source.auto_detected,
                    "parser_available": source.supported,
                    "health": stored.get("health", "unknown"),
                    "diagnostic_count": stored.get("diagnostic_count", 0),
                    "last_error": stored.get("last_error"),
                    "last_indexed_at": stored.get("last_indexed_at"),
                }
            )
        return {"items": items, "sources": items}

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        return {
            "vault_dir": str(config.vault_dir),
            "server_host": config.server_host,
            "server_port": config.server_port,
            "log_level": config.log_level,
            "auto_open_browser": config.auto_open_browser,
            "debounce_secs": config.debounce_secs,
            "schema_version": SCHEMA_VERSION,
        }

    @app.patch("/api/config/sources/{source_id}")
    def api_update_source(source_id: str, body: dict[str, Any]) -> dict[str, Any]:
        for source in config.sources:
            if source.id == source_id:
                if "enabled" in body:
                    requested = bool(body["enabled"])
                    if requested and not source.supported:
                        raise HTTPException(400, detail=f"unsupported source type: {source.type}")
                    source.enabled = requested
                save_config(config)
                return {
                    "id": source.id,
                    "enabled": source.enabled,
                    "note": "takes effect on next vg start",
                }
        raise HTTPException(404, detail=f"source not found: {source_id}")

    @app.get("/api/projects")
    def api_projects() -> list[dict[str, Any]]:
        return [dict(row) for row in list_projects(conn())]

    @app.get("/api/stats/timeline")
    def api_stats_timeline(since: str = "365d") -> dict[str, Any]:
        days = int(since[:-1]) if since.endswith("d") and since[:-1].isdigit() else 365
        return {"days": get_timeline(conn(), days)}

    @app.get("/api/stats")
    def api_stats() -> dict[str, Any]:
        stats = get_stats(conn())
        return stats.__dict__

    @app.get("/api/sessions/{identifier}/export")
    def api_export(identifier: str, format: str = "markdown") -> StreamingResponse:
        c = conn()
        session = _resolve_session(c, identifier)
        if format == "markdown":
            iterator = iter_session_markdown(c, session)
            media_type = "text/markdown; charset=utf-8"
            suffix = "md"
        elif format == "canonical-jsonl":
            iterator = iter_session_canonical_jsonl(c, session)
            media_type = "application/x-ndjson; charset=utf-8"
            suffix = "jsonl"
        else:
            raise HTTPException(400, detail="format must be markdown or canonical-jsonl")
        stem = _safe_export_stem(session["slug"] or session["external_session_id"][:8])
        date = str(session["started_at"] or "")[:10]
        filename = f"{stem}-{date}.{suffix}".replace("--", "-")
        return StreamingResponse(
            iterator,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        if not _allowed_authority(ws.headers.get("host")) or not _allowed_origin(
            ws.headers.get("origin"), config.server_port
        ):
            await ws.close(code=1008, reason="untrusted Host or Origin")
            return
        await ws_manager.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await ws_manager.disconnect(ws)

    ui_dir = Path(__file__).resolve().parent / "ui"
    if ui_dir.exists() and any(ui_dir.iterdir()):
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    else:

        @app.get("/")
        def root_placeholder() -> Response:
            return Response("vimgym UI is not installed", media_type="text/plain")

    return app
