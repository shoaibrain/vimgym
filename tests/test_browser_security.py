from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn

playwright = pytest.importorskip("playwright.sync_api")

from vimgym.config import AppConfig, SourceConfig  # noqa: E402
from vimgym.db import init_db  # noqa: E402
from vimgym.ingestion import candidate_for_path, ingest_artifact  # noqa: E402
from vimgym.server import create_app  # noqa: E402


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_chromium_renders_stored_html_as_text_and_stays_offline(tmp_path: Path) -> None:
    root = tmp_path / "source"
    root.mkdir()
    external_id = "99999999-9999-4999-8999-999999999999"
    payload = '<img src=x onerror="window.pwned=1"> pwned-browser-marker'
    path = root / f"{external_id}.jsonl"
    path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "m1",
                "sessionId": external_id,
                "cwd": "/Users/example/browser",
                "gitBranch": "security",
                "message": {"role": "user", "content": [{"type": "text", "text": payload}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    source = SourceConfig("claude_code", "Claude", "claude_code", str(root))
    port = _free_port()
    cfg = AppConfig(vault_dir=tmp_path / "vault", sources=[source], server_port=port)
    init_db(cfg.db_path)
    ingest_artifact(candidate_for_path(source, path), cfg)

    server = uvicorn.Server(
        uvicorn.Config(
            create_app(cfg),
            host="127.0.0.1",
            port=port,
            log_level="critical",
            access_log=False,
        )
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started

    outbound: list[str] = []
    try:
        with playwright.sync_playwright() as runtime:
            browser = runtime.chromium.launch()
            page = browser.new_page()
            page.on(
                "request",
                lambda request: (
                    outbound.append(request.url)
                    if not request.url.startswith(f"http://127.0.0.1:{port}")
                    and not request.url.startswith(f"ws://127.0.0.1:{port}")
                    else None
                ),
            )
            started = time.perf_counter()
            response = page.goto(f"http://127.0.0.1:{port}", wait_until="domcontentloaded")
            assert response is not None and response.ok
            page.locator(".session-card").first.wait_for(state="visible")
            assert time.perf_counter() - started <= 1.5
            page.locator(".session-card").first.click()
            page.locator("#transcriptMessages").wait_for(state="visible")
            assert "pwned-browser-marker" in page.locator("#transcriptMessages").inner_text()
            assert page.locator('img[src="x"]').count() == 0
            assert page.evaluate("typeof window.pwned") == "undefined"

            page.keyboard.press("Control+k")
            search = page.locator("#commandInput")
            search.fill("pwned-browser-marker")
            page.locator(".command-result").first.wait_for(state="visible")
            assert page.locator('img[src="x"]').count() == 0
            page.keyboard.press("Escape")
            assert page.locator("#commandOverlay").is_hidden()
            assert not outbound
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
