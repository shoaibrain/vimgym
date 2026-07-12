"""Redaction engine — strips secrets from text and JSONL."""

from __future__ import annotations

import json
import hashlib
import re
from importlib.resources import files
from pathlib import Path
from typing import Any


class RedactionPolicyError(RuntimeError):
    """Raised when the credential-scrubbing policy cannot be used safely."""


def _load_bundled_defaults() -> dict[str, Any]:
    """Read the redaction rules bundled inside the installed package.

    Uses importlib.resources so this works correctly when vimgym is installed
    from a wheel (pip/pipx/Homebrew), not just in editable repo layout.
    """
    try:
        return json.loads(
            (files("vimgym.defaults") / "redaction-rules.json").read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError, json.JSONDecodeError) as exc:
        raise RedactionPolicyError("bundled redaction policy is unavailable") from exc


class RedactionEngine:
    """Compiles patterns from a JSON rules file once, applies them on demand."""

    def __init__(self, rules_path: Path):
        self._patterns: list[tuple[str, re.Pattern[str], str]] = []
        rules_path = Path(rules_path)

        if rules_path.exists():
            try:
                data = json.loads(rules_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                # A configured policy is authoritative. Falling back silently would
                # persist data under a policy the owner did not select.
                raise RedactionPolicyError(f"invalid redaction policy: {rules_path}") from exc
        else:
            data = _load_bundled_defaults()

        rules = data.get("rules") if isinstance(data, dict) else None
        if not isinstance(rules, list) or not rules:
            raise RedactionPolicyError("redaction policy must contain at least one rule")

        normalized_rules: list[dict[str, str]] = []
        for index, rule in enumerate(rules):
            if not isinstance(rule, dict):
                raise RedactionPolicyError(f"redaction rule {index} is not an object")
            name_value = rule.get("name")
            pattern_value = rule.get("pattern")
            replacement_value = rule.get("replacement")
            if (
                not isinstance(name_value, str)
                or not name_value.strip()
                or not isinstance(pattern_value, str)
                or not pattern_value
                or not isinstance(replacement_value, str)
            ):
                raise RedactionPolicyError(f"invalid redaction rule {index}")
            try:
                compiled = re.compile(pattern_value)
                compiled.sub(replacement_value, "redaction-policy-probe")
            except re.error as exc:
                raise RedactionPolicyError(f"invalid redaction rule {index}") from exc
            if compiled.search("") is not None:
                raise RedactionPolicyError(f"redaction rule {index} may not match an empty string")
            name = name_value
            replacement = replacement_value
            self._patterns.append((name, compiled, replacement))
            normalized_rules.append(
                {"name": name, "pattern": pattern_value, "replacement": replacement}
            )

        canonical = json.dumps(
            {"rules": normalized_rules}, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        self._policy_hash = hashlib.sha256(canonical).hexdigest()

    @property
    def rule_count(self) -> int:
        return len(self._patterns)

    @property
    def policy_hash(self) -> str:
        """Stable hash used to invalidate artifacts when policy changes."""
        return self._policy_hash

    def redact_text(self, text: str) -> str:
        if not text:
            return text
        for _, pattern, replacement in self._patterns:
            text = pattern.sub(replacement, text)
        return text

    def redact_value(self, value: Any) -> Any:
        """Recursively scrub every string in JSON-like provider content.

        The method deliberately returns fresh containers. Callers can therefore
        treat the provider object as untrusted input and the returned object as
        the only value eligible for persistence, indexing, diagnostics, or egress.
        """
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {
                self.redact_text(str(key)): self.redact_value(item) for key, item in value.items()
            }
        if isinstance(value, list):
            return [self.redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_value(item) for item in value)
        return value

    def redact_session_raw(self, raw_jsonl: str) -> str:
        """Apply redaction to a legacy in-memory JSONL string.

        Canonical v2 never persists provider-native JSONL. This helper remains
        for v0.1 compatibility tests; malformed lines still pass through
        ``redact_text`` because they may contain secrets.
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
