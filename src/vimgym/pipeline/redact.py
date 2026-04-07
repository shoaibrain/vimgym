"""Redaction engine — strips secrets from text and JSONL."""
from __future__ import annotations

import json
import re
from pathlib import Path


class RedactionEngine:
    """Compiles patterns from a JSON rules file once, applies them on demand."""

    def __init__(self, rules_path: Path):
        self._patterns: list[tuple[str, re.Pattern[str], str]] = []
        rules_path = Path(rules_path)
        if not rules_path.exists():
            # Fall back to bundled defaults if vault rules file is missing.
            bundled = Path(__file__).resolve().parents[3] / "defaults" / "redaction-rules.json"
            if bundled.exists():
                rules_path = bundled
            else:
                return

        data = json.loads(rules_path.read_text())
        for rule in data.get("rules", []):
            try:
                compiled = re.compile(rule["pattern"])
            except re.error:
                continue
            self._patterns.append((rule["name"], compiled, rule["replacement"]))

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
