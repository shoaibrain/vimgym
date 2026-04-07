from pathlib import Path

from vimgym.pipeline.parser import parse_session
from vimgym.pipeline.summary import heuristic_summary

DATA_DIR = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


def test_summary_length():
    for path in DATA_DIR.glob("*.jsonl"):
        s = parse_session(path)
        summary = heuristic_summary(s)
        assert len(summary) <= 280
        assert len(summary) > 0


def test_summary_contains_title():
    s = parse_session(DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl")
    summary = heuristic_summary(s)
    # The sanitized fixture preserves the "CloudFormation" keyword in the title.
    assert "CloudFormation" in summary
