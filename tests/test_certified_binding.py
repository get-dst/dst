"""Slot validators + renderer. Deterministic by construction:
the LLM proposes values, but nothing reaches SQL except through
validate_binding + render_sql — injection lands as a typed literal or fails."""

from __future__ import annotations

import pytest

from services.certify.binding import (
    SlotSpec,
    parse_slots,
    render_question,
    render_sql,
    validate_binding,
    validate_template,
)

_SLOTS = {
    "period": {"type": "date_range"},
    "segment": {"type": "enum", "values": ["Enterprise", "SMB"]},
}
_SQL = (
    "SELECT SUM(o.amount_eur) FROM orders AS o WHERE o.segment = {segment} "
    "AND o.closed_at >= {period.start} AND o.closed_at < {period.end}"
)


# ── parse + template validation ──────────────────────────────────────────────


def test_parse_slots_accepts_v1_types_and_rejects_the_rest() -> None:
    specs, errors = parse_slots(_SLOTS)
    assert not errors and specs["period"].type == "date_range"
    _, errors = parse_slots({"x": {"type": "freeform"}})
    assert errors and "unknown type" in errors[0]
    _, errors = parse_slots({"x": {"type": "enum"}})
    assert errors and "inline values" in errors[0]
    _, errors = parse_slots({"x": {"type": "enum", "column": "orders.segment"}})
    assert errors and "column-referenced" in errors[0]
    _, errors = parse_slots({"BadName": {"type": "date"}})
    assert errors and "lower_snake_case" in errors[0]


def test_template_validation_names_every_problem() -> None:
    ok = validate_template(
        "revenue for {segment} in {period}", _SQL, _SLOTS, [{"period": "2026-Q2", "segment": "SMB"}]
    )
    assert ok == []
    errs = validate_template("q {mystery}", _SQL, _SLOTS, [{"period": "2026", "segment": "SMB"}])
    assert any("undeclared placeholder" in e and "mystery" in e for e in errs)
    errs = validate_template("q", "SELECT 1", _SLOTS, [{"period": "2026", "segment": "SMB"}])
    assert any("never used in the SQL" in e for e in errs)
    errs = validate_template("q", _SQL.replace("{period.start}", "{period}"), _SLOTS, [])
    assert any("range syntax" in e or "never bare" in e for e in errs)
    errs = validate_template("q", _SQL, _SLOTS, [])
    assert any("sample_bindings" in e for e in errs)
    errs = validate_template("q", _SQL, _SLOTS, [{"period": "soon", "segment": "SMB"}])
    assert any("sample_bindings[0]" in e and "canonical range" in e for e in errs)


# ── value validation ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("2026", True),
        ("2026-Q3", True),
        ("2026-q1", True),
        ("2026-07", True),
        ("2026-01-01/2026-03-01", True),
        ("2026-13", False),  # not a month
        ("2026-03-01/2026-01-01", False),  # end before start
        ("Q3 2026", False),  # not canonical — the gate must emit the grammar
        ("last quarter", False),
    ],
)
def test_date_range_grammar(value: str, ok: bool) -> None:
    specs, _ = parse_slots({"p": {"type": "date_range"}})
    _, errors = validate_binding(specs, {"p": value})
    assert (errors == []) is ok


def test_enum_canonicalizes_case_and_rejects_strangers() -> None:
    specs, _ = parse_slots(_SLOTS)
    canonical, errors = validate_binding(specs, {"period": "2026-Q2", "segment": "smb"})
    assert not errors and canonical["segment"] == "SMB"
    _, errors = validate_binding(specs, {"period": "2026-Q2", "segment": "Wholesale"})
    assert errors and "not one of" in errors[0]


def test_missing_and_undeclared_slots_are_named() -> None:
    specs, _ = parse_slots(_SLOTS)
    _, errors = validate_binding(specs, {"period": "2026"})
    assert any("missing value" in e and "segment" in e for e in errors)
    _, errors = validate_binding(specs, {"period": "2026", "segment": "SMB", "extra": "x"})
    assert any("not a declared slot" in e for e in errors)


# ── rendering ────────────────────────────────────────────────────────────────


def test_render_produces_typed_literals_with_half_open_ranges() -> None:
    specs, _ = parse_slots(_SLOTS)
    sql = render_sql(_SQL, specs, {"period": "2026-Q2", "segment": "SMB"}, "postgres")
    assert "o.segment = 'SMB'" in sql
    assert "o.closed_at >= '2026-04-01'" in sql
    assert "o.closed_at < '2026-07-01'" in sql  # end EXCLUSIVE
    assert "{" not in sql  # nothing unsubstituted


def test_render_number_and_date() -> None:
    specs, _ = parse_slots({"cap": {"type": "number"}, "asof": {"type": "date"}})
    sql = render_sql(
        "SELECT * FROM t WHERE n > {cap} AND d = {asof}",
        specs,
        {"cap": "42.5", "asof": "2026-08-01"},
        "postgres",
    )
    assert "n > 42.5" in sql and "d = '2026-08-01'" in sql


def test_injection_lands_as_escaped_literal_or_fails() -> None:
    # An enum value can only be a declared canonical value — anything else fails.
    specs, _ = parse_slots(_SLOTS)
    with pytest.raises(ValueError):
        render_sql(_SQL, specs, {"period": "2026", "segment": "SMB'; DROP TABLE o;--"}, "postgres")
    # Even a declared value containing a quote renders escaped, never spliced.
    quoted, _ = parse_slots({"s": {"type": "enum", "values": ["O'Brien & Co"]}})
    sql = render_sql("SELECT * FROM t WHERE c = {s}", quoted, {"s": "O'Brien & Co"}, "postgres")
    assert "'O''Brien & Co'" in sql


def test_render_question_uses_raw_values() -> None:
    binding = {"segment": "SMB", "period": "2026-Q2"}
    q = render_question("revenue for {segment} in {period}", binding)
    assert q == "revenue for SMB in 2026-Q2"


def test_slotspec_is_frozen_metadata() -> None:
    with pytest.raises(AttributeError):
        SlotSpec(type="date").type = "enum"  # type: ignore[misc]
