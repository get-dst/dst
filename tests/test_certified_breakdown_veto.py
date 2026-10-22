"""A certified answer serves exact or equivalent only at the grain the question
asks. "Delivery target and parcels delivered per driver in August 2026" embeds a hair
from the certified "Delivery target and parcels delivered in August 2026", whose SQL
returns one total: served exact, the per-driver breakdown was dropped in silence.
Pure: search and org_session are monkeypatched, as in the window veto's tests."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

from services.certify import store as certify_store
from services.certify.lexical import breakdown_mismatch
from services.runtime import assembly
from services.runtime.assembly import certified_lookup

_Q = "Delivery target and parcels delivered in August 2026"
# A GROUP BY inside the subquery dedups the daily target; the answer is one row.
_TOTAL_SQL = (
    "SELECT SUM(monthly_target) AS delivery_target, SUM(parcels_delivered) AS parcels_delivered "
    "FROM (SELECT driver_name, MAX(monthly_target) AS monthly_target, SUM(parcels_delivered) AS "
    "parcels_delivered FROM driver_daily GROUP BY driver_name) t"
)
_PER_DRIVER_SQL = (
    "SELECT driver_name, SUM(parcels_delivered) AS parcels_delivered FROM driver_daily GROUP BY 1"
)


@dataclass
class _Answer:
    id: str = "ca_1"
    question: str = _Q
    sql: str = _TOTAL_SQL
    slots: dict[str, str] | None = None
    resolution: dict[str, object] | None = None
    terms: list[str] | None = None


@dataclass
class _Hit:
    answer: _Answer = field(default_factory=_Answer)
    score: float = 0.995


def _wire(monkeypatch, hits: list[_Hit]) -> None:  # noqa: ANN001
    @contextlib.contextmanager
    def _null_session(_org):  # noqa: ANN001
        yield None

    monkeypatch.setattr(assembly, "org_session", _null_session)
    monkeypatch.setattr(certify_store, "search", lambda session, lens, qvec, k=5: hits)


def test_the_breakdown_words_against_the_outer_group_by() -> None:
    for asked in (
        "Delivery target and parcels delivered per driver in August 2026",
        "delivery target and parcels delivered by driver in August 2026",
        "delivery target and parcels delivered for each driver in August 2026",
        "delivery target and parcels delivered for every driver in August 2026",
    ):
        assert breakdown_mismatch(asked, _Q, _TOTAL_SQL), asked
    assert breakdown_mismatch(_Q, _Q, _TOTAL_SQL) is None
    assert breakdown_mismatch(f"{_Q}, by the end of the month", _Q, _TOTAL_SQL) is None
    # grouped by what the question names, positionally or by name: no mismatch
    assert (
        breakdown_mismatch(
            "Parcels delivered per driver", "Parcels delivered per driver", _PER_DRIVER_SQL
        )
        is None
    )
    by_month = "SELECT DATE_TRUNC('month', d) AS m, SUM(revenue) AS revenue FROM t GROUP BY 1"
    assert breakdown_mismatch("Revenue by month in 2026", "Revenue by month", by_month) is None
    # a ranking's "by <measure>" is the approved question's own grain
    ranked = (
        "SELECT resort, SUM(x) AS lift_pass_sales FROM t GROUP BY resort ORDER BY 2 DESC LIMIT 5"
    )
    top5 = "Top 5 resorts by lift-pass sales this month"
    ranking = "Which 5 resorts rank highest by lift-pass sales this month?"
    assert breakdown_mismatch(ranking, top5, ranked) is None
    # ...and the other way round: a grouped answer for a question asking none
    assert breakdown_mismatch(
        "Parcels delivered in August", "Parcels delivered per driver", _PER_DRIVER_SQL
    )


def test_a_breakdown_the_certified_sql_lacks_drops_to_assisted(monkeypatch) -> None:  # noqa: ANN001
    _wire(monkeypatch, [_Hit()])
    top, exemplars, mode, match, _ = certified_lookup(
        "deliveries",
        [0.1],
        "org-1",
        "Delivery target and parcels delivered per driver in August 2026",
    )
    assert top is None and match is None and mode == "assisted"
    assert exemplars and exemplars[0].sql == _TOTAL_SQL


def test_the_certified_question_itself_still_serves_exact(monkeypatch) -> None:  # noqa: ANN001
    _wire(monkeypatch, [_Hit()])
    top, _, mode, match, _ = certified_lookup("deliveries", [0.1], "org-1", _Q)
    assert top is not None and mode == "certified" and match == "exact"
