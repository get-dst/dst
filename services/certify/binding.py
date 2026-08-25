"""Slot machinery for parameterized certified answers.

A template is a certified answer whose SQL carries ``{slot}`` placeholders and
whose ``slots`` declare each one's type. Everything here is deterministic:
validators decide whether a raw value inhabits a slot type, and the renderer
turns validated values into dialect-correct literals via sqlglot — never
string-splicing. The LLM bind gate (assembly) merely *proposes* values; nothing
it says reaches SQL except through ``validate_binding`` + ``render_sql``.

Slot types v1: ``date_range``, ``date``, ``enum``, ``number``.
Enum values are declared inline (column-referenced enums are v2). The
``date_range`` grammar is deliberately tiny and canonical — ``YYYY``,
``YYYY-Qn``, ``YYYY-MM``, or ``YYYY-MM-DD/YYYY-MM-DD`` — and ranges are
half-open: ``{slot.start}`` inclusive, ``{slot.end}`` exclusive. The bind gate
prompt teaches this grammar; the validator enforces it.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

from sqlglot import exp

SLOT_TYPES = ("date_range", "date", "enum", "number")

# {name} or {name.start} / {name.end} — nothing else is a placeholder.
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)(?:\.(start|end))?\}")

_QUARTER = re.compile(r"^(\d{4})-[qQ]([1-4])$")
_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_YEAR = re.compile(r"^(\d{4})$")
_EXPLICIT = re.compile(r"^(\d{4}-\d{2}-\d{2})/(\d{4}-\d{2}-\d{2})$")
_NUMBER = re.compile(r"^-?\d+(\.\d+)?$")


@dataclass(frozen=True)
class SlotSpec:
    type: str  # one of SLOT_TYPES
    values: list[str] | None = None  # enum canonical values (inline, v1)


def parse_slots(raw: object) -> tuple[dict[str, SlotSpec], list[str]]:
    """Decode the yaml/jsonb ``slots`` mapping. Returns (specs, errors)."""
    errors: list[str] = []
    specs: dict[str, SlotSpec] = {}
    if not isinstance(raw, dict) or not raw:
        return {}, ["slots must be a non-empty mapping of name -> {type: ...}"]
    for name, spec in raw.items():
        if not re.fullmatch(r"[a-z_][a-z0-9_]*", str(name)):
            errors.append(f"slot name '{name}' must be lower_snake_case")
            continue
        body = spec if isinstance(spec, dict) else {}
        stype = str(body.get("type", ""))
        if stype not in SLOT_TYPES:
            errors.append(f"slot '{name}': unknown type {stype!r} (one of {', '.join(SLOT_TYPES)})")
            continue
        values = body.get("values")
        if stype == "enum":
            if "column" in body:
                errors.append(
                    f"slot '{name}': column-referenced enums are not supported yet — "
                    "declare inline values"
                )
                continue
            if not isinstance(values, list) or not values or not all(values):
                errors.append(f"slot '{name}': enum slots declare non-empty inline values")
                continue
            values = [str(v) for v in values]
        elif values is not None:
            errors.append(f"slot '{name}': only enum slots take values")
            continue
        specs[str(name)] = SlotSpec(type=stype, values=values)
    return specs, errors


def placeholders_in(text: str) -> set[str]:
    return {m.group(1) for m in _PLACEHOLDER.finditer(text)}


def validate_template(
    question: str,
    sql: str,
    slots_raw: object,
    sample_bindings: object,
) -> list[str]:
    """Certify-time gate: every error names the problem; [] means valid."""
    specs, errors = parse_slots(slots_raw)
    if errors:
        return errors
    used = placeholders_in(sql)
    undeclared = sorted((used | placeholders_in(question)) - set(specs))
    if undeclared:
        errors.append(f"undeclared placeholder(s): {', '.join(undeclared)}")
    unused = sorted(set(specs) - used)
    if unused:
        errors.append(f"declared slot(s) never used in the SQL: {', '.join(unused)}")
    for m in _PLACEHOLDER.finditer(sql):
        name, part = m.group(1), m.group(2)
        spec = specs.get(name)
        if spec is None:
            continue  # already reported as undeclared
        if spec.type == "date_range" and part is None:
            errors.append(
                f"slot '{name}' is a date_range — the SQL must use "
                f"{{{name}.start}} / {{{name}.end}}, never bare {{{name}}}"
            )
        if spec.type != "date_range" and part is not None:
            errors.append(f"slot '{name}' is {spec.type} — {{{name}.{part}}} is range syntax")
    if not isinstance(sample_bindings, list) or not sample_bindings:
        errors.append(
            "sample_bindings must be a non-empty list — an untestable "
            "certification is not a certification"
        )
        return errors
    for i, binding in enumerate(sample_bindings):
        if not isinstance(binding, dict):
            errors.append(f"sample_bindings[{i}] must be a mapping of slot -> value")
            continue
        _canon, bind_errors = validate_binding(specs, binding)
        errors += [f"sample_bindings[{i}]: {e}" for e in bind_errors]
    return errors


def _date_range(raw: str) -> tuple[datetime.date, datetime.date] | None:
    """Canonical range grammar -> (start_inclusive, end_exclusive), or None."""
    if m := _QUARTER.match(raw):
        year, quarter = int(m.group(1)), int(m.group(2))
        start = datetime.date(year, 3 * (quarter - 1) + 1, 1)
        return start, _add_months(start, 3)
    if m := _MONTH.match(raw):
        year, month = int(m.group(1)), int(m.group(2))
        if not 1 <= month <= 12:
            return None
        start = datetime.date(year, month, 1)
        return start, _add_months(start, 1)
    if m := _YEAR.match(raw):
        year = int(m.group(1))
        return datetime.date(year, 1, 1), datetime.date(year + 1, 1, 1)
    if m := _EXPLICIT.match(raw):
        try:
            start = datetime.date.fromisoformat(m.group(1))
            end = datetime.date.fromisoformat(m.group(2))
        except ValueError:
            return None
        return (start, end) if start < end else None
    return None


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month0 = d.month - 1 + months
    return datetime.date(d.year + month0 // 12, month0 % 12 + 1, 1)


def validate_binding(
    specs: dict[str, SlotSpec], binding: dict[str, object]
) -> tuple[dict[str, str], list[str]]:
    """Validate raw values against the slots. Returns (canonical, errors) —
    canonical values are what render_sql accepts (enum spelling normalized)."""
    errors: list[str] = []
    canonical: dict[str, str] = {}
    missing = sorted(set(specs) - {str(k) for k in binding})
    if missing:
        errors.append(f"missing value(s) for slot(s): {', '.join(missing)}")
    for name, value in binding.items():
        spec = specs.get(str(name))
        if spec is None:
            errors.append(f"'{name}' is not a declared slot")
            continue
        raw = str(value).strip()
        if spec.type == "date_range":
            if _date_range(raw) is None:
                errors.append(
                    f"slot '{name}': {raw!r} is not a canonical range "
                    "(YYYY, YYYY-Qn, YYYY-MM, or YYYY-MM-DD/YYYY-MM-DD)"
                )
                continue
        elif spec.type == "date":
            try:
                datetime.date.fromisoformat(raw)
            except ValueError:
                errors.append(f"slot '{name}': {raw!r} is not a YYYY-MM-DD date")
                continue
        elif spec.type == "number":
            if not _NUMBER.match(raw):
                errors.append(f"slot '{name}': {raw!r} is not a number")
                continue
        elif spec.type == "enum":
            assert spec.values is not None  # parse_slots guarantees it
            match = next((v for v in spec.values if v.lower() == raw.lower()), None)
            if match is None:
                errors.append(f"slot '{name}': {raw!r} is not one of {', '.join(spec.values)}")
                continue
            raw = match  # canonical spelling
        canonical[str(name)] = raw
    return canonical, errors


def _literal(spec: SlotSpec, canonical: str, part: str | None, dialect: str) -> str:
    if spec.type == "date_range":
        start, end = _date_range(canonical)  # type: ignore[misc]  # validated upstream
        day = start if part == "start" else end
        return exp.Literal.string(day.isoformat()).sql(dialect=dialect)
    if spec.type == "number":
        return exp.Literal.number(canonical).sql(dialect=dialect)
    # date + enum render as string literals — sqlglot owns quoting/escaping.
    return exp.Literal.string(canonical).sql(dialect=dialect)


def render_sql(sql: str, specs: dict[str, SlotSpec], binding: dict[str, str], dialect: str) -> str:
    """Validated canonical binding -> concrete SQL. Raises ValueError on any
    mismatch — callers validate first for friendly errors; this re-checks so a
    bad value can never slip through as spliced text."""
    canonical, errors = validate_binding(specs, dict(binding))
    if errors:
        raise ValueError("; ".join(errors))

    def _sub(m: re.Match[str]) -> str:
        name, part = m.group(1), m.group(2)
        spec = specs.get(name)
        if spec is None:
            raise ValueError(f"undeclared placeholder '{name}'")
        return _literal(spec, canonical[name], part, dialect)

    return _PLACEHOLDER.sub(_sub, sql)


def render_question(question: str, binding: dict[str, object]) -> str:
    """Placeholder question -> concrete question text (raw values, no quoting)."""

    def _sub(m: re.Match[str]) -> str:
        return str(binding.get(m.group(1), m.group(0)))

    return _PLACEHOLDER.sub(_sub, question)
