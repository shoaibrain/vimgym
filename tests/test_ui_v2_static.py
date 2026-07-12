"""Static safety and accessibility contracts for the v0.2 vault browser."""

import re
from html.parser import HTMLParser
from pathlib import Path


UI_DIR = Path(__file__).parents[1] / "src" / "vimgym" / "ui"


class _HTMLAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.external_urls: list[str] = []
        self.ids: set[str] = set()
        self.buttons_without_type: list[dict[str, str | None]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.add(str(values["id"]))
        for key in ("href", "src"):
            value = values.get(key) or ""
            if value.startswith(("http://", "https://")):
                self.external_urls.append(value)
        if tag == "button" and values.get("type") is None:
            self.buttons_without_type.append(values)


def test_index_is_offline_and_uses_semantic_controls() -> None:
    parser = _HTMLAudit()
    parser.feed((UI_DIR / "index.html").read_text())

    assert parser.external_urls == []
    assert parser.buttons_without_type == []
    assert {
        "providerFilter",
        "kindFilter",
        "lifecycleFilter",
        "sessionDetail",
        "commandInput",
        "announcer",
    }.issubset(parser.ids)


def test_javascript_has_no_markup_injection_sink() -> None:
    javascript = (UI_DIR / "app.js").read_text()

    assert "innerHTML" not in javascript
    assert "type !== 'session_archived'" not in javascript
    assert "outerHTML" not in javascript
    assert "insertAdjacentHTML" not in javascript
    assert "document.write" not in javascript
    assert "snippet_parts" in javascript
    assert "textContent" in javascript


def test_transcript_mount_and_full_block_fetch_are_bounded() -> None:
    javascript = (UI_DIR / "app.js").read_text()

    assert "`${count} uses`" in javascript
    assert "MAX_MESSAGE_PAGES = 3" in javascript
    assert "MESSAGE_PAGE_SIZE = 100" in javascript
    assert "MAX_MOUNTED_MESSAGES" in javascript
    assert "startsWith('/api/message-blocks/')" in javascript
    assert re.search(
        r"State\.messages\s*=\s*State\.messages\.slice\(0, MAX_MOUNTED_MESSAGES\)", javascript
    )


def test_styles_use_local_font_stack_and_visible_focus() -> None:
    stylesheet = (UI_DIR / "style.css").read_text()

    assert "fonts.googleapis.com" not in stylesheet
    assert "ui-monospace" in stylesheet
    assert ":focus-visible" in stylesheet
    assert "min-height: 44px" in stylesheet
    assert "prefers-reduced-motion" in stylesheet
