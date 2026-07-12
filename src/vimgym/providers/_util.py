"""Small, provider-private helpers for safe artifact discovery and JSONL reads."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .base import ArtifactCandidate, SourceSpec


READ_CHUNK_SIZE = 64 * 1024


def walk_files(root: Path) -> Iterator[Path]:
    """Walk regular files deterministically without following directory links."""

    if not root.is_dir():
        return
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if not (Path(directory) / name).is_symlink())
        for filename in sorted(filenames):
            path = Path(directory) / filename
            try:
                if path.is_file() and not path.is_symlink():
                    yield path
            except OSError:
                continue


def candidate(source: SourceSpec, path: Path, artifact_type: str) -> ArtifactCandidate | None:
    try:
        stat = path.stat()
        relative = path.relative_to(source.root).as_posix()
    except (OSError, ValueError):
        return None
    if artifact_type not in {"session_jsonl", "tool_result"}:
        raise ValueError(f"unsupported artifact type: {artifact_type}")
    return ArtifactCandidate(
        source=source,
        path=path,
        relative_path=relative,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        artifact_type=artifact_type,  # type: ignore[arg-type]
    )


def iter_complete_jsonl(
    path: Path,
) -> Iterator[tuple[int, bytes, dict[str, Any] | None, str | None]]:
    """Yield complete JSONL records, preserving malformed-line diagnostics.

    The final line is deliberately omitted when it has no newline terminator;
    providers may still be appending to it.  The caller detects this separately
    with ``scan_file`` and records a partial-artifact diagnostic.
    """

    with path.open("rb") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            if not raw_line.endswith(b"\n"):
                continue
            stripped = raw_line.strip()
            if not stripped:
                yield line_number, raw_line, None, None
                continue
            try:
                value = json.loads(stripped.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                yield line_number, raw_line, None, str(exc)
                continue
            if not isinstance(value, dict):
                yield line_number, raw_line, None, "top-level JSON value is not an object"
                continue
            yield line_number, raw_line, value, None


def scan_file(path: Path) -> tuple[str, int, int, bool]:
    """Hash a file with constant memory and report complete line/byte counts."""

    digest = hashlib.sha256()
    complete_lines = 0
    total_bytes = 0
    last_complete_byte = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(READ_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            complete_lines += chunk.count(b"\n")
            newline = chunk.rfind(b"\n")
            if newline >= 0:
                last_complete_byte = total_bytes + newline + 1
            total_bytes += len(chunk)
    partial = total_bytes > last_complete_byte
    return digest.hexdigest(), complete_lines, last_complete_byte, partial


def first_json_objects(path: Path, limit: int = 256) -> Iterator[dict[str, Any]]:
    """Yield at most ``limit`` valid, complete JSON objects from an artifact."""

    seen = 0
    for _, _, value, _ in iter_complete_jsonl(path):
        if value is None:
            continue
        yield value
        seen += 1
        if seen >= limit:
            break


def strings(value: Any) -> Iterator[str]:
    """Yield nested string leaves in stable container order."""

    if isinstance(value, str):
        if value:
            yield value
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)
    elif isinstance(value, dict):
        for key in sorted(value):
            yield from strings(value[key])


def first_text(value: Any) -> str | None:
    for text in strings(value):
        clean = " ".join(text.split())
        if clean:
            return clean
    return None


def preview(text: str | None, limit: int = 160) -> str | None:
    if text is None:
        return None
    clean = " ".join(text.split())
    if not clean:
        return None
    return clean[:limit]


def strip_opaque(value: Any) -> Any:
    """Remove binary/encrypted payloads while retaining visible metadata."""

    if isinstance(value, list):
        return [strip_opaque(item) for item in value]
    if not isinstance(value, dict):
        return value
    result: dict[str, Any] = {}
    for key, item in value.items():
        lowered = key.lower()
        if lowered in {"encrypted_content", "data", "bytes", "base64"} and isinstance(item, str):
            result[key] = {"omitted": True, "original_size": len(item)}
        else:
            result[key] = strip_opaque(item)
    return result


def nested_value(value: Any, key: str) -> Any:
    """Find the first mapping value for ``key`` in a nested provider object."""

    if isinstance(value, dict):
        if key in value:
            return value[key]
        for child in value.values():
            found = nested_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = nested_value(child, key)
            if found is not None:
                return found
    return None
