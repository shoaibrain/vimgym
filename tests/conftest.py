"""Shared pytest fixtures. All tests run against real session files in data/."""
from __future__ import annotations

from pathlib import Path

import pytest

from vimgym.pipeline.parser import ParsedSession, parse_session

DATA_DIR = Path(__file__).parent.parent / "data" / "-Users-shoaibrain-edforge"


@pytest.fixture(scope="session")
def data_dir() -> Path:
    assert DATA_DIR.exists(), f"data/ dir missing: {DATA_DIR}"
    return DATA_DIR


@pytest.fixture(scope="session")
def simple_session_path(data_dir: Path) -> Path:
    return data_dir / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl"


@pytest.fixture(scope="session")
def agents_session_path(data_dir: Path) -> Path:
    return data_dir / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl"


@pytest.fixture(scope="session")
def minimal_session_path(data_dir: Path) -> Path:
    return data_dir / "1fb8b1b8-6cb3-4e34-8446-fa60ba5df626.jsonl"


@pytest.fixture(scope="session")
def all_session_paths(data_dir: Path) -> list[Path]:
    paths = sorted(data_dir.glob("*.jsonl"))
    assert len(paths) >= 5, f"Expected ≥5 session files, found {len(paths)}"
    return paths


@pytest.fixture(scope="session")
def parsed_simple(simple_session_path: Path) -> ParsedSession:
    return parse_session(simple_session_path)


@pytest.fixture(scope="session")
def parsed_agents(agents_session_path: Path) -> ParsedSession:
    return parse_session(agents_session_path)
