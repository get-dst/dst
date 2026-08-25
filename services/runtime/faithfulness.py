"""Result-grounded faithfulness check — does the answer follow from the rows?

A deterministic guard against the dominant failure mode: an answer that states numbers
not present in the executed result. Every numeric claim in the prose must match a value
in the result set (allowing thousands separators, magnitude abbreviations, and light
rounding). Magnitude scaling is applied so prose like "€6.66M" grounds the raw value
6661765.89 — without it, every abbreviated figure was falsely flagged unverified, the
exact rounding artifact this check is supposed to look past. Percent-scaling is
allowed ONLY for claims explicitly written with '%', and only against result
cells in [0, 1] — "51.9%" is the correct rendering of the cell 0.5192…, and
flagging it turns a large share of unverified grades into false positives.
A PLAIN number never scales: treating a bare "42" as grounded by 0.42 made any
rate column ground almost any small integer, and that hole stays closed.

Four numbers in good prose are NOT cell values and must not be flagged as invented:
the result's row count ("17 reps" = 17 rows), a rate stated in an applied definition
("10% commission rate" when the definition says default 10%), a number echoed from the
user's question ("more than 3 orders" answering "avg CLTV of customers with more than
3 orders" — with an empty result, that echo was the dominant false positive), and
derived comparisons ("41% less", "15× less"). The first three are grounded; the
comparisons are recognised as derived and skipped. A bare rate with no backing
definition still fails ("42% of users churned"), preserving the guard. Used as
the numeric_grounding check, not a gate.

`reconcile` is the other half, and the newer one: tolerating a rounding is right for
GRADING an answer and wrong for SHIPPING one, because a tolerated rounding is a second
value for the same question. It rewrites exactly what the check tolerates, so the prose
quotes the rows instead of restating them.
"""

from __future__ import annotations

import decimal
import re
from collections.abc import Iterator

import sqlglot
from sqlglot import exp

from services.contracts.verification import CheckStatus
from services.contracts.warehouse import QueryResult

# A number, optionally followed by a magnitude: glued single-letter abbreviations
# ("6.66M", "2.5bn") or a spaced full word ("6.66 million"). Single letters must be
# glued so a list like "3, B" is never read as 3 billion.
# Two guards against tokens that only look numeric:
# - a digit directly after a LETTER is part of a token, not a numeric claim —
#   "Q3", "H1", "W33", "FY26", "v2" claimed bare 3s and 26s that nothing grounds;
# - "-" binds as a minus only when NOT preceded by a digit — "rated 3-5" claimed
#   MINUS five, and "2026-08-15" in prose claimed -8 and -15.
_NUM = re.compile(
    # Alphanumeric lookbehind, not just letters: "W33" blocks at the first 3 but
    # the engine restarts at the second — a digit before the match start always
    # means we're mid-token.
    # Comma only BETWEEN digits: "August 15," used to capture "15," — the
    # trailing comma defeated every surface-form guard (bare-year, day-of-month)
    # for any number at a clause boundary.
    # − is the Unicode minus — "−12" must not silently become +12.
    # Scientific notation rides the same token: the composer writes 9-figure
    # values as 1.65786e+08, and without the exponent the scan reads TWO
    # claims — 1.65786, then the 8 after the '+' — so every correct large
    # answer is withheld as ungrounded. float() parses the captured form
    # natively.
    r"(?<![A-Za-z0-9])((?:(?<!\d)[-−])?\d(?:[\d,]*\d)?(?:\.\d+)?(?:[eE][-+]?\d+)?)"
    r"(?:(bn|k|m|b)\b|\s+(thousand|million|billion)\b)?",
    re.IGNORECASE,
)
# Ordinal suffix: "the 3rd largest" is a position label, not a quantity claim.
_ORDINAL = re.compile(r"^(st|nd|rd|th)\b", re.IGNORECASE)
# A day-of-month riding a month name ("as of August 15" / "15 August") is a date,
# not a quantity: freshness statements are constant composer prose and every one
# of them would otherwise flag its own day number. Requires direct adjacency, so
# "August: 15 orders" still claims its 15.
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun[e]?|jul[y]?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_MONTH_BEFORE = re.compile(rf"{_MONTH}\.?\s+$", re.IGNORECASE)
_MONTH_AFTER = re.compile(rf"^\s+(?:of\s+)?{_MONTH}\b", re.IGNORECASE)
# "1 in 3 customers" is a proportion idiom, derived from the data, not two
# claims. Narrow on purpose: single-digit numerator, ≤2-digit denominator —
# "sold 45 in 12 markets" keeps claiming its 45.
_RATIO_AFTER = re.compile(r"^\s+in\s+\d{1,2}\b(?!\d)", re.IGNORECASE)
_RATIO_BEFORE = re.compile(r"\b[1-9]\s+in\s+$", re.IGNORECASE)

# Descriptor numbers: a number that DESCRIBES the window, grain, or ranking is
# not a claim about the data — "the last 30 days", "a 7-day rolling average",
# "the top 5 regions" are all normal prose, and treating the 30/7/5 as claimed
# figures withholds correct answers whole (a correct account count suppressed
# for mentioning its own 30-day window). Conservative on
# purpose: a duration noun right after the number, or a ranking word right
# before it — nothing else.
_DURATION_AFTER = re.compile(
    r"^(?:(?:[-‑]|\s)?(?:calendar\s+|business\s+|rolling\s+|trailing\s+)?"
    r"(?:day|week|month|quarter|year|hour|minute)s?"
    r"|(?:d|wk|mo|yr))\b",  # glued short units: "(30d)", "12mo" — column-name echoes
    re.IGNORECASE,
)
_RANK_BEFORE = re.compile(r"\b(?:top|first|last|bottom|latest|past|previous)\s+$", re.IGNORECASE)
# Basis points: "25 bps" states 0.0025.
_BPS = re.compile(r"^\s?bps?\b", re.IGNORECASE)
_SCALE = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}
# A comparison cue right after a "N%" marks it derived ("41% less than …", "3%
# MoM") — not a data claim, so it is skipped rather than required to match a value.
_COMPARE = re.compile(
    r"^\s*(less|more|fewer|higher|lower|than|below|above|vs\.?|versus|compared|against"
    r"|from|since|yoy|mom|qoq|year-over-year|month-over-month|quarter-over-quarter)\b",
    re.IGNORECASE,
)
# A change verb right BEFORE a "N%" ("down 12% vs June", "grew by 8%") marks the
# claim as derived arithmetic over the periods shown. Soft, not blind: the claim
# still grounds when a matching rate cell exists; when it cannot ground it is
# SKIPPED (derived), never failed — flagging honest month-over-month prose is a
# false-positive class of its own. A cue-less rate ("margin is
# 73%") still hard-fails without backing.
_CHANGE_CUE = re.compile(
    r"\b(up|down|grew|fell|rose|dropped|declined|decreased|increased|gained|lost|shrank)"
    r"\s+(?:by\s+)?$",
    re.IGNORECASE,
)
# A year as prose actually writes one: four bare digits, 18xx-20xx. "2,025" is a
# count, "2.026" is a decimal, "2026.5" is neither — none of them may ride the
# clock. Used against the claim's raw surface form, never its parsed float.
_BARE_YEAR = re.compile(r"(?:1[89]|20)\d{2}")


def _numbers(text: str) -> list[float]:
    out: list[float] = []
    for m in _NUM.finditer(text):
        try:
            value = float(m.group(1).replace(",", "").replace("\u2212", "-"))
        except ValueError:
            continue
        suffix = m.group(2) or m.group(3)
        if suffix:
            value *= _SCALE[suffix.lower()]
        out.append(value)
    return out


def _scan(text: str) -> Iterator[tuple[re.Match[str], float, str]]:
    """Every numeric claim, with its span, its value, and how it's used:
    'plain' (a figure that must match a value), 'percent' (a rate — must match a value
    or an applied definition's stated rate), or 'derived' (a multiple like '15×' or a
    comparison like '41% less' — not a data claim, so skipped).

    The span is what `reconcile` rewrites, so classification is shared with the check
    that grades the result: one scanner, one notion of what counts as a claim."""
    for m in _NUM.finditer(text):
        try:
            value = float(m.group(1).replace(",", "").replace("\u2212", "-"))
        except ValueError:
            continue
        suffix = m.group(2) or m.group(3)
        if suffix:
            value *= _SCALE[suffix.lower()]
        tail = text[m.end() : m.end() + 24]
        head = tail[:1]
        start = m.start()
        if start >= 2 and text[start - 1] in "-–−" and text[start - 2].isdigit():
            # The continuation of a hyphenated compound — the "5" of "rated 3-5",
            # the "08"/"15" of "2026-08-15" — is part of a range/date token, not
            # a standalone data claim (and must never be "reconciled" either).
            kind = "derived"
        elif _ORDINAL.match(tail):
            kind = "derived"  # a position label ("3rd largest"), not a quantity
        elif (
            value == int(value)
            and 1 <= value <= 31
            and "," not in m.group(1)
            and "." not in m.group(1)
            and (_MONTH_BEFORE.search(text[:start]) or _MONTH_AFTER.match(tail))
        ):
            kind = "derived"  # a day-of-month next to its month name — a date
        elif (
            _RATIO_AFTER.match(tail) and re.fullmatch(r"[1-9]", m.group(1)) is not None
        ) or _RATIO_BEFORE.search(text[:start]):
            kind = "derived"  # the "1 in 3" proportion idiom (single-digit numerator)
        elif (
            "," not in m.group(1)
            and "." not in m.group(1)
            and (_DURATION_AFTER.match(tail) or _RANK_BEFORE.search(text[:start]))
        ):
            # "last 30 days", "7-day rolling", "top 5 regions" — a descriptor
            # of the window/grain/rank, not a data claim. The
            # separator guard keeps real figures out: "1,234 days of data"
            # still claims 1234.
            kind = "derived"
        elif _BPS.match(tail):
            kind = "bps"  # "25 bps" states the fraction 0.0025
        elif head == "×" or (head in ("x", "X") and tail[1:2] in ("", " ", ".", ",", ";", ")")):
            kind = "derived"  # a multiple ("15×")
        elif tail.lstrip()[:1] == "%":
            after_pct = tail.lstrip()[1:]
            kind = "derived" if _COMPARE.match(after_pct) else "percent"
        else:
            kind = "plain"
        yield m, value, kind


def _claims(text: str) -> list[tuple[float, str]]:
    return [(value, kind) for _, value, kind in _scan(text)]


# A numeric spelling with 3+ decimal digits — as prose, that's float residue
# quoted verbatim, not a figure a human wrote (see reconcile's exact-claim carve-out).
_RESIDUE = re.compile(r"-?\d+\.\d{3,}")


def _matches(claim: float, value: float) -> bool:
    # exact-ish, or rounded to a displayable precision — never percent-rescaled.
    tol = max(0.01, abs(value) * 0.01)
    if abs(claim - value) <= tol:
        return True
    return any(abs(claim - round(value, nd)) <= 0.001 for nd in (0, 1, 2))


def _numeric_cell(cell: object) -> float | None:
    """The cell as a float, or None for non-numerics. ONE predicate for the
    whole module: warehouse drivers return `decimal.Decimal` for exact-numeric
    columns (BigQuery NUMERIC — i.e. the money columns), and scattered
    `int | float` isinstance checks silently exclude them. An answer over a
    NUMERIC money column then has NOTHING to ground against: exact claims fail,
    reconcile cannot rewrite, and the prose is withheld — precisely on the
    figures a CFO asks for."""
    if isinstance(cell, bool):
        return None
    if isinstance(cell, int | float | decimal.Decimal):
        return float(cell)
    return None


def _values(result: QueryResult) -> list[float]:
    out: list[float] = []
    for row in result.rows:
        for cell in row:
            v = _numeric_cell(cell)
            if v is not None:
                out.append(v)
            elif isinstance(cell, str):
                out.extend(_numbers(cell))
    return out


def sql_literal_numbers(sql: str, dialect: str | None = None) -> list[float]:
    """Numbers stated as literals in the served SQL — the query's own provenance.

    "Win rate this year" answered over `WHERE closed_year = 2026` honestly says
    "deals closed in 2026" — and 2026 is in no result cell, so it graded unverified.
    That year echo is a large share of all grounding flags. A literal the SQL
    itself carries is a declared input the caller can read in the trace, never an
    invention. Number literals count directly; string literals contribute ONLY
    their year-like tokens (`>= '2026-01-01'` grounds "2026" — but never the 01s,
    and a timestamp's :30 must not ground an invented "30"; both leak without
    that narrowing). Identifiers do NOT contribute (q4_revenue must
    not ground a "4"), which is why this parses with sqlglot instead of regexing
    the text — and a query it cannot parse contributes nothing (conservative:
    the check stays as strict as before)."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect) if dialect else sqlglot.parse_one(sql)
    except Exception:
        return []
    out: list[float] = []
    for lit in tree.find_all(exp.Literal):
        if lit.is_number:
            try:
                value = float(lit.name)
            except ValueError:
                continue
            parent = lit.parent
            out.append(-value if isinstance(parent, exp.Neg) else value)
        elif lit.find_ancestor(exp.Interval) is not None:
            # INTERVAL '30 days' is a duration the query declares — "the last
            # 30 days" in prose must ground against it (a year-only literal
            # filter over-corrects here and drops the duration).
            out.extend(_numbers(lit.name))
        else:
            out.extend(float(y) for y in re.findall(r"\b(?:1[89]|20)\d{2}\b", lit.name))
    return out


# Bound on the rows column totals are accumulated over. The pipeline already caps
# what it hands this module (FETCH_CAP), so the bound is belt-and-braces for other
# callers — totals stay a cheap linear pass, never a surprise on a huge result.
_TOTAL_ROW_CAP = 5000


def _column_totals(result: QueryResult) -> list[float]:
    """Per-column sums and running (cumulative) totals of the numeric cells.

    Business prose legitimately totals its own rows: summing five
    channel rows into a grand total is a figure in no cell, honest arithmetic over
    the result itself — and the check used to brand it invented. The running totals,
    not just the final sum, ground because the composer may be shown only the
    result's head ("showing the first 200"): an honest total of the rows it was
    shown is the cumulative total after row N, never the full-column sum.

    Only sums over ≥2 cells count. A one-row "total" is the cell itself, already
    groundable — and letting it in would make every single-row aggregate its own
    total, which teaches ``reconcile``'s total guard to skip every cell claim on
    exactly the results it rewrites most (the determinism invariant).
    Bools are excluded as everywhere else; string cells contribute nothing.
    """
    totals: list[float] = []
    running: dict[int, tuple[float, int]] = {}
    for row in result.rows[:_TOTAL_ROW_CAP]:
        for i, cell in enumerate(row):
            v = _numeric_cell(cell)
            if v is None:
                continue
            acc, seen = running.get(i, (0.0, 0))
            acc, seen = acc + v, seen + 1
            running[i] = (acc, seen)
            if seen >= 2:
                totals.append(acc)
    return totals


def _rendered(cell: int | float | decimal.Decimal) -> str:
    """The spelling a rewritten claim gets: the cell's value, readable as prose.

    `str(cell)` put binary-float residue into certified finance answers —
    `$88759841.48000014` for a SUM over FLOAT64, at `verified · certified`.
    Deterministic presentation instead: thousands
    separators on integral values, and 2-decimal rounding once a non-integral
    value reaches separator scale (sub-cent residue at ≥1000 is far inside
    `_matches`' tolerance, so the reconcile invariant — a rewritten claim is
    one the check tolerates — holds). Small non-integral values keep their
    full precision: 0.10634 is data, not residue. Decimal cells (exact
    numerics) render from their own digits, never through a float repr that
    can go exponential."""
    if isinstance(cell, int):
        return f"{cell:,}"
    if isinstance(cell, decimal.Decimal):
        if cell == cell.to_integral_value():
            return f"{int(cell):,}"
        if abs(cell) >= 1000:
            return f"{cell:,.2f}"
        return format(cell, "f")
    if cell == int(cell) and abs(cell) < 1e15:
        return f"{int(cell):,}"
    if abs(cell) >= 1000:
        return f"{cell:,.2f}"
    return str(cell)


def _cells(result: QueryResult) -> list[tuple[float, str]]:
    """Numeric result cells as (value, the cell's presentation rendering).

    The rendering comes from the stored cell, never from the float — so an integer
    count stays `4` instead of becoming `4.0`. String cells are deliberately excluded:
    a number parsed out of prose in a cell has no canonical rendering to quote.
    """
    out: list[tuple[float, str]] = []
    for row in result.rows:
        for cell in row:
            if not isinstance(cell, int | float | decimal.Decimal) or isinstance(cell, bool):
                continue
            out.append((float(cell), _rendered(cell)))
            if cell < 0:
                # The magnitude spelling of a negative cell:
                # a rounded "614,447" for the cell -614446.91 reconciles to
                # the magnitude's own presentation rendering.
                magnitude = -cell
                out.append((float(magnitude), _rendered(magnitude)))
    return out


def reconcile(
    answer_text: str,
    result: QueryResult,
    *,
    definition_text: str = "",
    question_text: str = "",
    notes_text: str = "",
    sql_text: str = "",
    dialect: str | None = None,
    clock_years: tuple[int, ...] = (),
) -> str:
    """Rewrite every rounded restatement of a result value back to the value itself.

    The composer is a model. Told to quote the rows verbatim it will still sometimes
    write "38.81%" for a row holding 38.813651137594796, and the response then carries
    two answers to one question — the dominant way one value ends up published two ways.
    A prompt instruction cannot close it (the model can always paraphrase a number), so
    this deterministic pass runs after the model and cannot be talked out of: prose
    becomes a RENDERING of the data rather than a second computation.

    The tolerance is `_matches` — the same predicate `numeric_check` uses to bless a
    rounding — so the set of claims the check would call grounded-by-rounding is exactly
    the set rewritten here. After this runs, no numeric claim can be both tolerated by
    the check and different from the value it was tolerated against. That equivalence is
    the invariant, and it is why the rewrite is not merely a prettier round: widening
    one without the other would reopen the gap.

    Untouched, by construction: a claim that already equals a groundable value exactly
    (so `1,234` for the cell `1234` keeps its separators — same value, no churn), the
    result's row count, a rate stated in the applied definition, a number echoed from
    the question, and derived comparisons.
    """
    cells = _cells(result)
    if not cells:
        return answer_text
    # Exact hits are grounded as they stand — checked against every source, so a row
    # count or a definition's rate is never "corrected" into a cell it merely resembles.
    exact = {value for value, _ in cells}
    exact.add(float(len(result.rows)))
    exact.update(_numbers(definition_text))
    exact.update(_numbers(question_text))
    # A declared data-note figure (profile null rate, range) is a fact the composer
    # was HANDED and told to attribute — never "corrected" into a cell it resembles;
    # same for a literal the served SQL states (the WHERE clause's year).
    exact.update(_numbers(notes_text))
    if sql_text:
        exact.update(sql_literal_numbers(sql_text, dialect))
    # …and the clock's years: over CURRENT_DATE-relative SQL there is no literal,
    # and the 1% match slack is ±20 years — without this, an honest "in 2026" gets
    # "corrected" into a year CELL like 2024 — the rewrite's overreach case.
    exact.update(float(y) for y in clock_years)
    totals = _column_totals(result)
    cell_values = {v for v, _ in cells}
    parts: list[str] = []
    last = 0
    for match, value, kind in _scan(answer_text):
        if kind == "derived":
            continue
        if value in exact:
            # Exact claims stand as written (no churn) — UNLESS the spelling is
            # a verbatim quote of a cell's float residue (3+ decimal digits):
            # the composer is TOLD to quote rows verbatim, so raw
            # `88759841.48000014` arrives exact and would otherwise stand at
            # `verified · certified`. That one case rewrites
            # to the cell's presentation rendering; everything else — a
            # question echo, a definition rate, an author's own separators —
            # stays untouched.
            residue = _RESIDUE.fullmatch(match.group(1).replace(",", ""))
            if not (residue and value in cell_values):
                continue
        if any(_matches(value, t) for t in totals):
            # A claim that renders a COLUMN TOTAL is grounded arithmetic over the
            # rows, not a restatement of any one cell — and when one row carries
            # nearly all of the total, the sum lands inside that cell's 1%
            # tolerance, so "correcting" it would corrupt a right figure into a
            # wrong one. The cost is a rounded cell restatement that collides
            # with a total staying unrewritten: conservative, never corrupting.
            continue
        # Closest matching cell wins, ties by row-major order — deterministic, and it
        # keeps a claim from being rewritten to a further-away value it also tolerates.
        candidates = [(abs(value - v), rendered) for v, rendered in cells if _matches(value, v)]
        if not candidates and kind == "percent":
            # A '%'-marked claim tolerated as the rendering of a fraction cell is
            # rewritten to the DETERMINISTIC one-decimal rendering of that cell —
            # so "51.92%" and "51.9%" across runs collapse to one spelling, keeping
            # the invariant that a tolerated claim and its value agree.
            # Percent space, same width as the check's tolerance — fraction-space
            # slack (±0.01 absolute) silently meant ±1 full percentage point.
            candidates = [
                (abs(value - v * 100), f"{v * 100:.1f}")
                for v, _ in cells
                if 0 <= v <= 1 and _matches(value, v * 100)
            ]
        if not candidates:
            continue
        parts.append(answer_text[last : match.start()])
        parts.append(min(candidates, key=lambda c: c[0])[1])
        last = match.end()
    parts.append(answer_text[last:])
    return "".join(parts)


def numeric_check(
    answer_text: str,
    result: QueryResult,
    *,
    definition_text: str = "",
    question_text: str = "",
    notes_text: str = "",
    sql_text: str = "",
    dialect: str | None = None,
    clock_years: tuple[int, ...] = (),
    truncated: bool = False,
) -> tuple[CheckStatus, str | None]:
    """The numeric_grounding check: every numeric claim must match a groundable value.

    Groundable = the result's cell values, each column's sum and running cumulative
    totals (``_column_totals`` — business prose legitimately totals its own rows;
    per-column only, so cross-column arithmetic still fails), its row count
    ("17 reps" = 17 rows), any
    number stated in the applied definition's body (so "10% commission rate" grounds when
    the definition says default 10%), any number echoed from the user's question (so
    "no customers with more than 3 orders" grounds against the ask, even on an empty
    result — _numbers normalises "3"/"3.0"/thousands separators on both sides), and any
    number in the data notes handed to the composer (declared table-profile facts about
    the projected columns — "~26% null" is a declared fact, not an invention; the
    composer's prompt requires attributing it), and any literal the served SQL itself
    states (`sql_literal_numbers` — the year in the WHERE clause is provenance, not
    invention), plus the lens clock's current/previous YEAR (``clock_years``: "this
    year" resolved against CURRENT_DATE leaves no literal anywhere, yet "in 2026" is
    the honest scope statement). Clock years match EXACTLY and only claims whose
    surface form is a bare four-digit year — the tolerant matcher's 1% slack is
    ±20 years, and "2,025 users" parses to 2025.0 — both are leaks that exact,
    bare-year matching closes. A '%'-marked claim additionally grounds against a cell
    in [0, 1], compared in PERCENT space so the tolerance is the same width as a
    direct match ("51.9%" is the rendering of 0.5192…; "42%" is not a rendering of
    0.41); a plain number never scales. Multiples and "N% less/more" comparisons
    are derived, not data claims, and are skipped. A bare rate with no backing
    still fails. No numeric claims at all is reported as skip (`no_numeric_claims`).

    ``truncated`` = the caller's result window is SHORT of the query's full result
    (the pipeline's row cap bit). The rows a soft-skipped derivation would rest on
    may never have been fetched, so the change-cue skip is withdrawn: an
    un-groundable claim over a truncated window FAILS. The class: monthly
    totals invented past the 200-row cap, the model filling the gap the
    cap left."""
    cell_values = _values(result)
    values = [
        *cell_values,
        # Column sums and running totals: business prose legitimately totals its
        # own rows (e.g. the sum of five channel rows,
        # false-flagged as invented). Per-column only: cross-column arithmetic
        # stays ungroundable, so the guard keeps its teeth.
        *_column_totals(result),
        float(len(result.rows)),
        *_numbers(definition_text),
        *_numbers(question_text),
        *_numbers(notes_text),
        *(sql_literal_numbers(sql_text, dialect) if sql_text else []),
    ]
    # Sign-insensitive magnitudes: churn is stored as a negative
    # delta and spoken as a magnitude — "ARR churned was $614,447" against a
    # cell of -614446.91 is the natural phrasing, and "-$614,447" even PARSES
    # positive (the $ splits the minus from the digits). A negative groundable
    # value also grounds its absolute value; the magnitude check is untouched,
    # so a wrong magnitude still fails.
    values = values + [abs(v) for v in values if v < 0]
    clock_set = {float(y) for y in clock_years}
    grounded_any = False
    seen_claims = False
    for m, value, kind in _scan(answer_text):
        seen_claims = True
        if kind == "derived":
            continue  # a multiple or comparison — not a claim about a result value
        # Accounting-style negatives: "(1,234)" states -1234.
        neg_paren = (
            m.start() > 0
            and answer_text[m.start() - 1] == "("
            and answer_text[m.end() : m.end() + 1] == ")"
        )
        grounded = (
            any(_matches(value, v) for v in values)
            or (neg_paren and any(_matches(-value, v) for v in values))
            # bps space, not fraction space — the 0.01 absolute tolerance floor
            # swallows every bps-scale difference (same bug shape as percents).
            or (kind == "bps" and any(_matches(value, v * 1e4) for v in cell_values))
            or (
                # Fraction cells in [-1, 1], compared sign-blind: "declined 12%"
                # is how prose renders the cell -0.12 — the sign lives in the verb.
                # (reconcile stays sign-strict: grading tolerates the idiom,
                # rewriting must never flip a sign.)
                kind == "percent"
                and any(abs(v) <= 1 and _matches(abs(value), abs(v * 100)) for v in cell_values)
            )
            or (
                kind == "plain"
                and value in clock_set
                and _BARE_YEAR.fullmatch(m.group(1)) is not None
            )
        )
        if not grounded:
            if not truncated and kind == "percent" and _CHANGE_CUE.search(answer_text[: m.start()]):
                # "down 12% vs June" over two period cells: derived arithmetic,
                # not an invented figure. Verifying the derivation deterministically
                # would be the stricter treatment; short of that,
                # a change-cued rate that can't ground skips instead of failing —
                # EXCEPT over a truncated window, where the periods behind the
                # derivation may never have been fetched.
                continue
            # Quote the claim AS WRITTEN. A `{value:g}` rendering turns every
            # large claim into scientific notation in the failure reason
            # ("2.06201e+07"), which reads as if the composer had written sci
            # notation — the message must never misattribute a spelling to the
            # prose.
            claim = m.group(1)
            if truncated:
                return "fail", (
                    f"the answer states {claim}, which is not groundable from the "
                    "truncated result window"
                )
            return "fail", f"the answer states {claim}, which is not in the result"
        grounded_any = True
    if not seen_claims:
        return "skip", "no_numeric_claims"
    if not grounded_any:
        return "skip", "only_derived_claims"
    return "pass", None


def is_grounded(
    answer_text: str, result: QueryResult, *, definition_text: str = "", question_text: str = ""
) -> bool:
    """True unless a numeric claim is contradicted by the result (skip counts as ok)."""
    status, _ = numeric_check(
        answer_text, result, definition_text=definition_text, question_text=question_text
    )
    return status != "fail"
