from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


SCRIPT = Path(__file__).parent / "_fixture_tools" / "generate_benchmark.py"
SPEC = importlib.util.spec_from_file_location("vimgym_benchmark_generator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_benchmark_generator_is_exact_and_deterministic(tmp_path: Path) -> None:
    first = MODULE.generate(
        tmp_path / "first", session_count=10, message_count=100, target_bytes=200_000
    )
    second = MODULE.generate(
        tmp_path / "second", session_count=10, message_count=100, target_bytes=200_000
    )
    assert first == second
    assert first["sessions"] == 10
    assert first["messages"] == 100
    assert first["jsonl_bytes"] == 200_000
    assert first["jsonl_files"] == 10
