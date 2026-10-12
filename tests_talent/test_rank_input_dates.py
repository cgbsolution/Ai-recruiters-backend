"""Role dates reach the ranking for every parser's date format (found by the
local end-to-end run: only "YYYY-MM" was read, so regex-parsed profiles — the
whole Resume Database — were ranked without dates, and Claude flagged "work
history has no dates" on every match)."""
import pytest

from app.talent.redaction import _ym, build_rank_input


@pytest.mark.parametrize("raw, expected", [
    ("2021-01", "2021-01"), ("2021", "2021-01"), ("Jan 2021", "2021-01"), ("January 2021", "2021-01"),
    ("Sept 2019", "2019-09"), ("Jan'21", "2021-01"), ("Jun'98", "1998-06"), ("01/2021", "2021-01"),
    ("Present", None), ("not a date", None), ("", None),
])
def test_date_forms(raw, expected):
    assert _ym(raw) == expected


def test_regex_parsed_roles_carry_start_and_end():
    parsed = {"work_experience": [
        {"title": "Lead Data Engineer", "company": "Acme", "start_date": "Jan 2021", "end_date": "Present"},
        {"title": "Engineer", "company": "Infosys", "start_date": "Jun 2019", "end_date": "Dec 2020"},
    ]}
    payload, fields, _ = build_rank_input(parsed)
    assert payload["work_history"][0]["start"] == "2021-01" and payload["work_history"][0]["end"] == "present"
    assert payload["work_history"][1]["start"] == "2019-06" and payload["work_history"][1]["end"] == "2020-12"
    assert {"work_history[].start", "work_history[].end"} <= set(fields)
