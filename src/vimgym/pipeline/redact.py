"""Redaction engine — strips secrets from text and JSONL."""
from __future__ import annotations

import json
import re
from importlib.resources import files
from pathlib import Path


def _load_bundled_defaults() -> dict:
    """Read the redaction rules bundled inside the installed package.

    Uses importlib.resources so this works correctly when vimgym is installed
    from a wheel (pip/pipx/Homebrew), not just in editable repo layout.
    """
    try:
        return json.loads(
            (files("vimgym.defaults") / "redaction-rules.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError):
        return {"rules": []}


class RedactionEngine:
    """Compiles patterns from a JSON rules file once, applies them on demand."""

    def __init__(self, rules_path: Path):
        self._patterns: list[tuple[str, re.Pattern[str], str]] = []
        rules_path = Path(rules_path)

        if rules_path.exists():
            try:
                data = json.loads(rules_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = _load_bundled_defaults()
        else:
            data = _load_bundled_defaults()

        for rule in data.get("rules", []):
            try:
                compiled = re.compile(rule["pattern"])
            except (re.error, KeyError):
                continue
            self._patterns.append((rule["name"], compiled, rule["replacement"]))

    @property
    def rule_count(self) -> int:
        return len(self._patterns)

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        for _, pattern, replacement in self._patterns:
            text = pattern.sub(replacement, text)
        return text

    def redact_session_raw(self, raw_jsonl: str) -> str:
        """Apply redaction line-by-line to a JSONL string.

        Lines that don't parse are still passed through redact_text — they're
        likely partial writes and may still contain secrets.
        """
        if not raw_jsonl:
            return raw_jsonl
        out_lines: list[str] = []
        for line in raw_jsonl.splitlines():
            if not line.strip():
                out_lines.append(line)
                continue
            try:
                json.loads(line)  # validate; we don't use the parsed value
            except json.JSONDecodeError:
                out_lines.append(self.redact_text(line))
                continue
            out_lines.append(self.redact_text(line))
        return "\n".join(out_lines)
