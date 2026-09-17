"""The certified matcher must not serve across time windows.

"this quarter" and "last quarter" are one token apart and embed near-identical,
so a certified this-quarter answer will serve last-quarter and this-year asks at
`exact` — verbatim SQL, certified/verified badge, a wrong number under a named
approver. A deterministic temporal-term comparison vetoes the serve bands on
disagreement; windowless paraphrases still serve, and the vetoed pair still
folds as an exemplar so generation answers the asked window. Pure: search and
org_session are monkeypatched — no DB, no embedder."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from services.certify import store as certify_store
from services.runtime import assembly
from services.runtime.assembly import certified_lookup, temporal_mismatch, temporal_terms

# ── the extraction ───────────────────────────────────────────────────────────


def test_temporal_terms_normalize() -> None:
    assert temporal_terms("How many leads this quarter?") == {"this quarter"}
    assert temporal_terms("orders in the current quarter") == {"this quarter"}
    assert temporal_terms("churn over the previous month") == {"last month"}
    assert temporal_terms("revenue for the last 30 days") == {"last 30 day"}
    assert temporal_terms("bookings YTD") == {"this year"}
    assert temporal_terms("net revenue in Q1 2026") == {"q1", "2026"}
    assert temporal_terms("signups in June 2026") == {"june", "2026"}
    assert temporal_terms("how many orders today?") == {"this day"}


def test_a_month_name_alone_is_not_a_window() -> None:
    # "may" is a modal verb; month names count only with a year attached.
    assert temporal_terms("how many deals may close?") == frozenset()
    assert temporal_terms("what happened in May 2026?") == {"may", "2026"}


def test_mismatch_requires_windows_on_both_sides() -> None:
    assert temporal_mismatch("leads last quarter?", "leads this quarter?")
    assert temporal_mismatch("leads this year?", "leads this quarter?")
    assert not temporal_mismatch("how many leads?", "leads this quarter?")
    assert not temporal_mismatch("leads this quarter?", "SQLs generated this quarter?")


# ── the serve veto ───────────────────────────────────────────────────────────

_THIS_QUARTER_Q = "How many leads, SQLs and SQOs did we generate this quarter?"
_THIS_QUARTER_SQL = (
    "SELECT SUM(leads) FROM funnel WHERE full_date >= DATE_TRUNC(CURRENT_DATE, QUARTER)"
)


@dataclass
class _Answer:
    id: str = "ca_1"
    question: str = _THIS_QUARTER_Q
    sql: str = _THIS_QUARTER_SQL
    slots: dict[str, str] | None = None


@dataclass
class _Hit:
    answer: _Answer = field(default_factory=_Answer)
    score: float = 0.995


def _wire(monkeypatch, hits: list[_Hit]) -> None:
    @contextlib.contextmanager
    def _null_session(_org):  # noqa: ANN001
        yield None

    monkeypatch.setattr(assembly, "org_session", _null_session)
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: hits)


def test_window_mismatch_blocks_an_exact_certified_serve(monkeypatch) -> None:
    _wire(monkeypatch, [_Hit()])
    top, exemplars, mode, match, bound = certified_lookup(
        "funnel", [0.1], "org-1", "How many leads, SQLs and SQOs did we generate last quarter?"
    )
    assert top is None and match is None
    # …and the certified pair still teaches generation as an exemplar.
    assert mode == "assisted"
    assert exemplars and exemplars[0].sql == _THIS_QUARTER_SQL


def test_grain_mismatch_blocks_too(monkeypatch) -> None:
    _wire(monkeypatch, [_Hit()])
    top, _, mode, match, _ = certified_lookup(
        "funnel", [0.1], "org-1", "How many leads, SQLs and SQOs did we generate this year?"
    )
    assert top is None and match is None and mode == "assisted"


def test_same_window_paraphrase_still_serves_exact(monkeypatch) -> None:
    # REGRESSION: certification must keep working for the question it was
    # written for, including harmless paraphrase.
    _wire(monkeypatch, [_Hit()])
    top, _, mode, match, _ = certified_lookup(
        "funnel", [0.1], "org-1", "How many leads, SQLs, and SQOs have we generated this quarter?"
    )
    assert top is not None and mode == "certified" and match == "exact"


def test_windowless_ask_still_serves(monkeypatch) -> None:
    # The veto fires only when BOTH sides state a window — an unqualified ask
    # matching a windowed certified answer is the certified question's meaning.
    _wire(monkeypatch, [_Hit()])
    top, _, mode, match, _ = certified_lookup(
        "funnel", [0.1], "org-1", "How many leads, SQLs and SQOs did we generate?"
    )
    assert top is not None and mode == "certified" and match == "exact"


def test_a_month_span_with_a_year_is_one_window() -> None:
    from datetime import date

    from services.runtime.timewindow import unplaced_months, window_ranges

    terms = temporal_terms("Group revenue by entity for January through August 2026")
    assert terms == frozenset({"span:january-august", "2026"})
    assert window_ranges(terms, date(2026, 9, 17)) == [(date(2026, 1, 1), date(2026, 8, 31))]
    assert unplaced_months("January through August 2026", terms) == []
    for wording in ("March to June 2026", "March–June 2026", "March - June 2026"):
        assert window_ranges(temporal_terms(wording), date(2026, 9, 17)) == [
            (date(2026, 3, 1), date(2026, 6, 30))
        ]
    # A backwards span resolves to nothing rather than to a guess.
    assert window_ranges(temporal_terms("August through January 2026"), date(2026, 9, 17)) == []


def test_a_stated_month_no_term_carries_is_unplaced() -> None:
    from services.runtime.timewindow import unplaced_months

    q = "revenue for January and August 2026"
    terms = temporal_terms(q)
    assert "august" in terms and "january" not in terms
    assert unplaced_months(q, terms) == ["january"]
    assert unplaced_months("revenue in January", frozenset()) == ["january"]
    assert unplaced_months("orders that may ship this month", temporal_terms("this month")) == []


def test_month_abbreviations_count_beside_a_year_or_in_a_span_never_bare() -> None:
    from datetime import date

    from services.runtime.timewindow import unplaced_months, window_ranges

    today = date(2026, 9, 17)
    assert temporal_terms("revenue Aug 2026") == frozenset({"august", "2026"})
    assert temporal_terms("revenue Sept. 2026") == frozenset({"september", "2026"})
    assert window_ranges(temporal_terms("revenue Jan–Aug 2026"), today) == [
        (date(2026, 1, 1), date(2026, 8, 31))
    ]
    assert window_ranges(temporal_terms("Jan-Mar 2026 bookings"), today) == [
        (date(2026, 1, 1), date(2026, 3, 31))
    ]
    # Bare abbreviations are words: not a window, not an unplaced month.
    assert temporal_terms("mar the dec figures") == frozenset()
    assert unplaced_months("mar the dec figures", frozenset()) == []


def test_a_fiscal_period_token_is_not_a_calendar_quarter() -> None:
    """ "FY25-Q1" is a stored period value (a dictionary the filter round
    offers), not Q1 of some year: the window parser leaves it alone, so a
    question about it never clarifies on a window the entity does not have."""
    assert temporal_terms("total spiff for period FY25-Q1") == frozenset()
    assert temporal_terms("payout row count for FY26-Q3") == frozenset()
    assert temporal_terms("revenue in Q1 2026") == frozenset({"q1", "2026"})
