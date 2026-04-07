from pathlib import Path

from vimgym.pipeline.metadata import decode_project_name, extract_metadata
from vimgym.pipeline.parser import parse_session

DATA_DIR = Path(__file__).parent / "fixtures" / "sessions" / "-Users-example-edforge"


def test_decode_project_name_from_cwd():
    assert decode_project_name("-Users-shoaibrain-edforge", "/Users/shoaibrain/edforge") == "edforge"


def test_decode_project_name_with_dashes_in_cwd():
    assert decode_project_name("-Users-x-my-cool-api", "/Users/x/my-cool-api") == "my-cool-api"


def test_decode_project_name_no_cwd():
    assert decode_project_name("-Users-shoaibrain-edforge", None) == "edforge"


def test_extract_metadata_real_session():
    s = parse_session(DATA_DIR / "eaa3009a-c5ab-4015-a3e5-af26622652f9.jsonl")
    meta = extract_metadata(s)
    assert meta.project_name == "edforge"
    assert meta.duration_secs is not None
    assert meta.duration_secs > 0
    assert meta.message_count > 0
    assert meta.user_message_count > 0
    assert meta.asst_message_count > 0


def test_duration_computed_for_large_session():
    s = parse_session(DATA_DIR / "3438c55b-0df0-4bc0-811e-561afcf19350.jsonl")
    meta = extract_metadata(s)
    assert meta.duration_secs > 3600
