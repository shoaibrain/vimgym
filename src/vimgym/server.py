"""FastAPI server: REST API + WebSocket + static UI."""
from __future__ import annotations

import asyncio
import json
import logging
import queue as _queue
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from vimgym import __version__
from vimgym.config import AppConfig, save_config
from vimgym.db import get_connection
from vimgym.events import event_queue
from vimgym.storage.export import render_session_markdown
from vimgym.storage.queries import (
    AmbiguousIDError,
    count_sessions,
    get_session,
    get_session_messages,
    get_stats,
    get_timeline,
    list_projects,
    list_sessions,
    search_sessions,
)

logger = logging.getLogger(__name__)


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
        text = json.dumps(payload)
        dead: list[WebSocket] = []
        for ws in list(self.connections):
            try:
                await ws.send_text(text)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self.connections.discard(ws)


def _row_to_dict(row) -> dict[str, Any]:
    d = dict(row)
    for key in ("tools_used", "files_modified"):
        if key in d and isinstance(d[key], str):
            try:
                d[key] = json.loads(d[key])
            except Exception:
                pass
    return d


def create_app(config: AppConfig) -> FastAPI:
    ws_manager = WSManager()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ── startup ──
        app.state._broadcaster_stop = False

        async def pump() -> None:
            loop = asyncio.get_running_loop()

            def _next() -> dict | None:
                # Short-timeout poll: queue.get with no timeout would block the
                # executor thread forever and deadlock shutdown (asyncio cannot
                # cancel a thread blocked in C). Found in Sprint 3.
                try:
                    return event_queue.get(timeout=0.25)
                except _queue.Empty:
                    return None

            while not app.state._broadcaster_stop:
                event = await loop.run_in_executor(None, _next)
                if event is None:
                    continue
                if event.get("type") == "shutdown":
                    break
                try:
                    await ws_manager.broadcast(event)
                except Exception:
                    logger.exception("broadcast failed")

        app.state._broadcaster = asyncio.create_task(pump())

        try:
            yield
        finally:
            # ── shutdown ──
            app.state._broadcaster_stop = True
            task = getattr(app.state, "_broadcaster", None)
            if task:
                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()

    app = FastAPI(title="vimgym", version=__version__, lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            f"http://localhost:{config.server_port}",
            f"http://127.0.0.1:{config.server_port}",
        ],
        allow_methods=["GET", "POST", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    app.state.config = config
    app.state.ws_manager = ws_manager
    import time as _t
    app.state._boot_monotonic = _t.monotonic()

    def conn():
        return get_connection(config.db_path)

    # ---------------- routes ----------------

    @app.get("/health")
    def health() -> dict[str, Any]:
        import time as _t
        c = conn()
        n = c.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return {
            "status": "ok",
            "version": __version__,
            "sessions": int(n),
            "uptime_secs": int(_t.monotonic() - app.state._boot_monotonic),
        }

    @app.get("/api/sessions")
    def api_sessions(
        project: str | None = None,
        branch: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict[str, Any]:
        c = conn()
        rows = list_sessions(c, project, branch, since, until, limit, offset)
        total = count_sessions(c, project, branch, since, until)
        return {
            "sessions": [_row_to_dict(r) for r in rows],
            "total": total,
            "has_more": (offset + len(rows)) < total,
        }

    @app.get("/api/sessions/{uuid_prefix}")
    def api_session_detail(uuid_prefix: str) -> dict[str, Any]:
        c = conn()
        try:
            row = get_session(c, uuid_prefix)
        except AmbiguousIDError as e:
            raise HTTPException(
                status_code=409,
                detail={"error": "ambiguous_id", "matches": e.matches},
            )
        if row is None:
            raise HTTPException(status_code=404, detail="session not found")

        msgs = get_session_messages(c, row["session_uuid"])
        result = _row_to_dict(row)
        result["messages"] = [
            {
                **{k: m[k] for k in m.keys() if k != "content_json"},
                "content": json.loads(m["content_json"] or "[]"),
            }
            for m in msgs
        ]
        return result

    @app.get("/api/sessions/{uuid_prefix}/raw")
    def api_session_raw(uuid_prefix: str) -> PlainTextResponse:
        c = conn()
        try:
            row = get_session(c, uuid_prefix)
        except AmbiguousIDError as e:
            raise HTTPException(409, detail={"error": "ambiguous_id", "matches": e.matches})
        if row is None:
            raise HTTPException(404, "session not found")
        raw = c.execute(
            "SELECT raw_jsonl FROM sessions_raw WHERE session_uuid = ?",
            (row["session_uuid"],),
        ).fetchone()
        return PlainTextResponse(raw["raw_jsonl"] if raw else "")

    @app.get("/api/search")
    def api_search(
        q: str = Query(..., min_length=1),
        project: str | None = None,
        branch: str | None = None,
        since: str | None = None,
        until: str | None = None,
        tool: str | None = None,
        limit: int = Query(20, ge=1, le=100),
    ) -> dict[str, Any]:
        c = conn()
        results = search_sessions(c, q, project, branch, since, until, tool, limit)
        return {
            "query": q,
            "total": len(results),
            "results": [
                {
                    "session_uuid": r.session_uuid,
                    "project_name": r.project_name,
                    "ai_title": r.ai_title,
                    "started_at": r.started_at,
                    "duration_secs": r.duration_secs,
                    "git_branch": r.git_branch,
                    "snippet": r.snippet,
                    "rank": r.rank,
                }
                for r in results
            ],
        }

    @app.get("/api/projects")
    def api_projects() -> list[dict[str, Any]]:
        return [dict(r) for r in list_projects(conn())]

    # ── Config / sources ────────────────────────────────────────────

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        return {
            "vault_dir": str(config.vault_dir),
            "server_host": config.server_host,
            "server_port": config.server_port,
            "log_level": config.log_level,
            "auto_open_browser": config.auto_open_browser,
            "debounce_secs": config.debounce_secs,
            "schema_version": 1,
        }

    @app.get("/api/config/sources")
    def api_sources() -> dict[str, Any]:
        return {
            "sources": [
                {
                    "id": s.id,
                    "name": s.name,
                    "type": s.type,
                    "path": s.path,
                    "enabled": s.enabled,
                    "exists": s.exists(),
                    "auto_detected": s.auto_detected,
                    "parser_available": s.type == "claude_code",
                }
                for s in config.sources
            ]
        }

    @app.patch("/api/config/sources/{source_id}")
    def api_update_source(source_id: str, body: dict[str, Any]) -> dict[str, Any]:
        for s in config.sources:
            if s.id == source_id:
                if "enabled" in body:
                    s.enabled = bool(body["enabled"])
                save_config(config)
                return {
                    "id": s.id,
                    "enabled": s.enabled,
                    "note": "takes effect on next vg start",
                }
        raise HTTPException(404, f"source not found: {source_id}")

    @app.get("/api/stats/timeline")
    def api_stats_timeline(since: str = "365d") -> dict[str, Any]:
        days = 365
        s = since.strip()
        if s.endswith("d") and s[:-1].isdigit():
            days = int(s[:-1])
        return {"days": get_timeline(conn(), days)}

    @app.get("/api/sessions/{uuid_prefix}/export")
    def api_export(uuid_prefix: str, format: str = "markdown") -> Response:
        c = conn()
        try:
            row = get_session(c, uuid_prefix)
        except AmbiguousIDError as e:
            raise HTTPException(409, detail={"error": "ambiguous_id", "matches": e.matches})
        if row is None:
            raise HTTPException(404, "session not found")
        if format != "markdown":
            raise HTTPException(400, "only markdown format is supported")

        msgs = get_session_messages(c, row["session_uuid"])
        md = render_session_markdown(row, msgs)

        slug = row["slug"] or row["session_uuid"][:8]
        date = (row["started_at"] or "")[:10]
        filename = f"{slug}-{date}.md".replace("--", "-")
        return Response(
            content=md,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/api/stats")
    def api_stats() -> dict[str, Any]:
        s = get_stats(conn())
        return {
            "total_sessions": s.total_sessions,
            "total_duration_secs": s.total_duration_secs,
            "total_input_tokens": s.total_input_tokens,
            "total_output_tokens": s.total_output_tokens,
            "db_size_bytes": s.db_size_bytes,
            "sessions_this_week": s.sessions_this_week,
            "top_projects": s.top_projects,
            "top_tools": s.top_tools,
        }

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws_manager.connect(ws)
        try:
            while True:
                # Server is push-only; just hold the connection.
                await ws.receive_text()
        except WebSocketDisconnect:
            pass
        finally:
            await ws_manager.disconnect(ws)

    # Static UI: only mount if directory exists (Sprint 4 will populate it).
    ui_dir = Path(__file__).resolve().parent / "ui"
    if ui_dir.exists() and any(ui_dir.iterdir()):
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
    else:
        @app.get("/")
        def _root_placeholder() -> dict[str, str]:
            return {
                "vimgym": __version__,
                "ui": "not yet built (Sprint 4)",
                "api": "/api/sessions, /api/search, /api/stats, /api/projects",
            }

    return app
