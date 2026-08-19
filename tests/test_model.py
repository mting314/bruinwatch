"""Golden tests for deterministic ``model`` construction.

The expected values are lifted verbatim from live Schedule of Classes responses
(see ``tests/fixtures/course_titles_comsci_p1.html``), which embed the models the
registrar generates for itself. If ``build_model`` ever drifts from what the site
emits, these fail and every scrape silently returns nothing -- so they matter
more than their size suggests.
"""

from __future__ import annotations

import json

import pytest

from bruinwatch.registrar.model import (
    build_model,
    catalog_number,
    course_path,
    is_interesting_course,
    split_course_number,
)


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("32", ("", "32", "")),
        ("151AH", ("", "151", "AH")),
        ("M151B", ("M", "151", "B")),
        ("CM122", ("CM", "122", "")),
        ("A", ("", "", "A")),
        ("1", ("", "1", "")),
    ],
)
def test_split_course_number(number, expected):
    assert split_course_number(number) == expected


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("32", "0032    "),
        ("1", "0001    "),
        ("151AH", "0151AH  "),
        ("M151B", "0151B M "),
        ("A", "0000A   "),
    ],
)
def test_catalog_number_is_fixed_width(number, expected):
    assert catalog_number(number) == expected
    assert len(catalog_number(number)) == 8


@pytest.mark.parametrize(
    ("subject", "number", "expected"),
    [
        ("COM SCI", "32", "COMSCI0032"),
        ("COM SCI", "1", "COMSCI0001"),
        ("C&S BIO", "M120", "CSBIO0120M"),
        ("AERO ST", "A", "AEROST0000A"),
    ],
)
def test_course_path_strips_punctuation(subject, number, expected):
    assert course_path(subject, number) == expected


@pytest.mark.parametrize(
    ("subject", "number", "term", "expected_token"),
    [
        # Captured from the live registrar bootstrap for Fall 2026.
        ("COM SCI", "32", "26F", "MDAzMiAgICBDT01TQ0kwMDMy"),
        ("COM SCI", "1", "26F", "MDAwMSAgICBDT01TQ0kwMDAx"),
        ("COM SCI", "30", "26F", "MDAzMCAgICBDT01TQ0kwMDMw"),
    ],
)
def test_build_model_matches_registrar_output(subject, number, term, expected_token):
    model = json.loads(build_model(subject, number, term))
    assert model["Token"] == expected_token
    assert model["Term"] == term
    assert model["SubjectAreaCode"] == f"{subject:<7}"
    assert model["IsRoot"] is True
    assert model["ClassNumber"] == "%"
    assert model["SequenceNumber"] is None
    assert model["MultiListedClassFlag"] == "n"


def test_build_model_key_order_matches_registrar():
    # The endpoint is picky: the blob has to look like its own output.
    assert list(json.loads(build_model("COM SCI", "32", "26F"))) == [
        "Term",
        "SubjectAreaCode",
        "CatalogNumber",
        "IsRoot",
        "SessionGroup",
        "ClassNumber",
        "SequenceNumber",
        "Path",
        "MultiListedClassFlag",
        "Token",
    ]


def test_build_model_round_trips_against_fixture(fixture_text):
    """Every root model the registrar emitted must be one we can reproduce."""
    import re

    markup = fixture_text("course_titles_comsci_p1.html")
    found = 0
    for match in re.finditer(r'AddToCourseData\("[^"]*",(\{.*?\})\)', markup):
        theirs = json.loads(match.group(1))
        if not theirs["IsRoot"]:
            continue
        number = theirs["CatalogNumber"].strip()
        # Reconstruct the human catalog number from their fixed-width form.
        digits = number[:4].lstrip("0") or "0"
        suffix = theirs["CatalogNumber"][4:6].strip()
        prefix = theirs["CatalogNumber"][6:8].strip()
        ours = json.loads(build_model("COM SCI", f"{prefix}{digits}{suffix}", theirs["Term"]))
        assert ours == theirs, f"drift for {theirs['Path']}"
        found += 1
    assert found > 5, "fixture should contain several root course models"


@pytest.mark.parametrize(
    ("subject", "number", "expected"),
    [
        ("COM SCI", "32", True),
        ("COM SCI", "375", False),  # teaching apprentice practicum
        ("COM SCI", "596", False),  # directed individual study
        ("LAW", "500", True),  # professional schools are all 300/500s
        ("MED", "301", True),
    ],
)
def test_is_interesting_course(subject, number, expected):
    assert is_interesting_course(subject, number) is expected
