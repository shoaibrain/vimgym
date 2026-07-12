#!/usr/bin/env python3
"""Generate a deterministic mixed-provider benchmark corpus.

The default corpus is exactly 100 MiB of JSONL with 25,000 visible messages
across 500 sessions. Content is synthetic and contains no machine-specific path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


NAMESPACE = uuid.UUID("a0a80f64-b597-54ad-ae8d-d599864c6134")


@dataclass(frozen=True)
class Line:
    relative_path: Path
    record: dict
    is_message: bool


def _session_id(index: int) -> str:
    return str(uuid.uuid5(NAMESPACE, f"benchmark-session-{index:04d}"))


def _message_id(session_index: int, message_index: int) -> str:
    return str(uuid.uuid5(NAMESPACE, f"benchmark-message-{session_index:04d}-{message_index:05d}"))


def iter_lines(session_count: int, message_count: int) -> Iterator[Line]:
    base, remainder = divmod(message_count, session_count)
    for session_index in range(session_count):
        session_id = _session_id(session_index)
        messages = base + (1 if session_index < remainder else 0)
        if session_index % 2 == 0:
            relative = Path("claude/projects/-Users-example-benchmark") / f"{session_id}.jsonl"
            for message_index in range(messages):
                role = "user" if message_index % 2 == 0 else "assistant"
                yield Line(
                    relative,
                    {
                        "type": role,
                        "uuid": _message_id(session_index, message_index),
                        "sessionId": session_id,
                        "timestamp": f"2026-01-{(session_index % 28) + 1:02d}T00:{message_index % 60:02d}:00Z",
                        "cwd": "/Users/example/benchmark",
                        "gitBranch": "benchmark",
                        "message": {
                            "role": role,
                            "content": [{"type": "text", "text": ""}],
                        },
                    },
                    True,
                )
        else:
            relative = Path("codex/sessions/2026/01") / f"rollout-{session_id}.jsonl"
            yield Line(
                relative,
                {
                    "timestamp": "2026-01-01T00:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": session_id,
                        "cwd": "/Users/example/benchmark",
                        "originator": "Codex Benchmark",
                        "source": "benchmark",
                    },
                },
                False,
            )
            for message_index in range(messages):
                role = "user" if message_index % 2 == 0 else "assistant"
                yield Line(
                    relative,
                    {
                        "timestamp": f"2026-01-{(session_index % 28) + 1:02d}T00:{message_index % 60:02d}:00Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": role,
                            "id": _message_id(session_index, message_index),
                            "content": [
                                {
                                    "type": "input_text" if role == "user" else "output_text",
                                    "text": "",
                                }
                            ],
                        },
                    },
                    True,
                )


def _encode(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _set_filler(record: dict, filler: str) -> None:
    if record["type"] in {"user", "assistant"}:
        record["message"]["content"][0]["text"] = filler
    else:
        record["payload"]["content"][0]["text"] = filler


def generate(
    output: Path,
    *,
    session_count: int = 500,
    message_count: int = 25_000,
    target_bytes: int = 100 * 1024 * 1024,
    force: bool = False,
) -> dict:
    if session_count < 1 or message_count < session_count:
        raise ValueError("message_count must be at least session_count")
    if output.exists() and any(output.iterdir()):
        if not force:
            raise FileExistsError(f"output is not empty: {output}")
        shutil.rmtree(output)
    output.mkdir(parents=True, exist_ok=True)

    base_bytes = sum(len(_encode(line.record)) for line in iter_lines(session_count, message_count))
    filler_total = target_bytes - base_bytes
    if filler_total < 0:
        raise ValueError(f"target_bytes is too small; minimum is {base_bytes}")
    filler_base, filler_remainder = divmod(filler_total, message_count)

    handles: dict[Path, object] = {}
    message_index = 0
    try:
        for line in iter_lines(session_count, message_count):
            path = output / line.relative_path
            if path not in handles:
                path.parent.mkdir(parents=True, exist_ok=True)
                handles[path] = path.open("wb")
            if line.is_message:
                length = filler_base + (1 if message_index < filler_remainder else 0)
                phrase = "benchmark capture migration backup restore search "
                filler = (phrase * ((length // len(phrase)) + 1))[:length]
                _set_filler(line.record, filler)
                message_index += 1
            handles[path].write(_encode(line.record))  # type: ignore[union-attr]
    finally:
        for handle in handles.values():
            handle.close()  # type: ignore[union-attr]

    jsonl_files = sorted(output.rglob("*.jsonl"))
    actual_bytes = sum(path.stat().st_size for path in jsonl_files)
    if actual_bytes != target_bytes or message_index != message_count:
        raise RuntimeError(
            f"generator invariant failed: {actual_bytes} bytes/{message_index} messages"
        )
    digest = hashlib.sha256()
    for path in jsonl_files:
        digest.update(path.relative_to(output).as_posix().encode())
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    manifest = {
        "format": "vimgym-benchmark-corpus",
        "seed": str(NAMESPACE),
        "sessions": session_count,
        "messages": message_count,
        "jsonl_bytes": actual_bytes,
        "jsonl_files": len(jsonl_files),
        "sha256": digest.hexdigest(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    parser.add_argument("--sessions", type=int, default=500)
    parser.add_argument("--messages", type=int, default=25_000)
    parser.add_argument("--bytes", type=int, default=100 * 1024 * 1024, dest="target_bytes")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(
        json.dumps(
            generate(
                args.output,
                session_count=args.sessions,
                message_count=args.messages,
                target_bytes=args.target_bytes,
                force=args.force,
            ),
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
