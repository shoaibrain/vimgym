"""Contentless block-level FTS helpers.

The redacted canonical block is the sole content copy. FTS stores only its term
index and uses the message-block rowid as the stable in-database mapping.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any


def flatten_strings(value: Any) -> str:
    parts: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict):
            for key, child in item.items():
                parts.append(str(key))
                visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)

    visit(value)
    return "\n".join(part for part in parts if part)


def searchable_text(text: str | None, data_json: str | None) -> str:
    try:
        data = json.loads(data_json or "{}")
    except json.JSONDecodeError:
        data = data_json or ""
    return "\n".join(part for part in (text or "", flatten_strings(data)) if part)


def insert_block_fts(
    conn: sqlite3.Connection,
    block_rowid: int,
    text: str | None,
    data_json: str | None,
    *,
    visible: bool,
) -> None:
    value = searchable_text(text, data_json) if visible else ""
    if value:
        conn.execute(
            "INSERT INTO message_fts(rowid,searchable_text) VALUES(?,?)",
            (block_rowid, value),
        )


def delete_block_fts(
    conn: sqlite3.Connection,
    block_rowid: int,
    text: str | None,
    data_json: str | None,
    *,
    visible: bool,
) -> None:
    value = searchable_text(text, data_json) if visible else ""
    if value:
        conn.execute(
            """
            INSERT INTO message_fts(message_fts,rowid,searchable_text)
            VALUES('delete',?,?)
            """,
            (block_rowid, value),
        )


def delete_fts_for_blocks(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[Any, ...],
) -> None:
    rows = conn.execute(
        "SELECT rowid,text_content,data_json,visibility FROM message_blocks WHERE " + where_sql,
        params,
    ).fetchall()
    for row in rows:
        delete_block_fts(
            conn,
            int(row["rowid"]),
            row["text_content"],
            row["data_json"],
            visible=row["visibility"] == "visible",
        )
