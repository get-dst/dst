"""The certified rail after the three-stack comparison on Kuura.

Five defects, each pinned on the live shape that found it. A certified answer
read the revenue mart over a month a definition flags and served the
duplicated figure `verified` — definitions now ride the certified rail: a
period rule the approved SQL reads past fails `definition_period`, is named on
every surface and caps the grade. Certify-time prose composed over the 5-row
probe served "the five clinics" over fourteen rows — the prose is composed over
the whole result and a stored row count tells a result that grew. A NO_ANSWER
below an explanatory paragraph graded `verified` — the marker anywhere is a
decline. A generated de-duplication re-added May's single rows under `partial`
with a generic reason — a UNION ALL over one table is counted against the
table's declared keys. And a certified basis cited every definition declared
on the table — a metric's page rides only when the SQL computes it.
"""

from __future__ import annotations

import pytest

from services.certify.generate import _value_summary
from services.contracts.fakes import FakeConnector, ScriptedLLM
from services.contracts.response import CertifiedProvenance
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.warehouse import QueryResult
from services.project.apply import _certified_definition_warnings
from services.runtime import verification
from services.runtime.answer import AnswerComposer, decline_reason
from services.runtime.generator import FixedSQLGenerator
from services.runtime.pipeline import certified_definitions, run_query

_PROVENANCE = CertifiedProvenance(
    cert_id="c1", certified_by="apply", certified_at="2026-10-17T10:00:00+00:00"
)

# The cmp-dst finance lens, as far as these rails read it.
_REVENUE_DAILY = Entity(
    name="revenue_daily",
    source=EntitySource(connection="warehouse", table="marts.fct_revenue_daily"),
    fields=[
        Field(name="clinic_code", type="string"),
        Field(name="day", type="date"),
        Field(name="revenue_eur", type="number"),
    ],
    metrics=[Metric(name="revenue_net_eur", agg="sum", expr="revenue_daily.revenue_eur")],
    default_time_field="day",
)
_INVOICE_LINES = Entity(
    name="invoice_lines",
    source=EntitySource(connection="warehouse", table="marts.fct_invoice_lines_monthly"),
    primary_key=["invoice_id", "line_no"],
    fields=[
        Field(name="invoice_id", type="string"),
        Field(name="line_no", type="integer"),
        Field(name="partition_month", type="date"),
        Field(name="payer", type="string"),
        Field(name="status", type="string"),
        Field(name="net_amount", type="number"),
    ],
    metrics=[
        Metric(
            name="invoice_net_eur",
            agg="sum",
            expr="invoice_lines.net_amount",
            filters=["invoice_lines.status <> 'credited'"],
        )
    ],
)
_REVENUE_DEF = Definition(
    term="revenue_net_eur",
    summary="Revenue = net invoiced amount ex VAT",
    body="Revenue is recognised on the invoice date.",
    about="revenue_daily.revenue_net_eur",
)
_MAY_RULE = Definition(
    term="may_2026_billing_duplicates",
    summary="May 2026 in the revenue mart holds the billing export twice",
    body="Read May from invoice_lines de-duplicated on invoice_id + line_no.",
    about="revenue_daily",
    period="2026-05",
    sql_expr="revenue_daily.day NOT BETWEEN DATE '2026-05-01' AND DATE '2026-05-31'",
)
_Q13_SQL = (
    "SELECT d.clinic_name, ROUND(SUM(r.revenue_eur), 2) AS net_revenue_eur "
    "FROM marts.fct_revenue_daily AS r JOIN marts.dim_clinic AS d "
    "ON r.clinic_code IN (d.clinic_code, d.legacy_clinic_code) "
    "WHERE r.day BETWEEN '2026-01-01' AND '2026-10-16' GROUP BY 1 ORDER BY 2 DESC"
)
_Q13 = "Net revenue per clinic, year to date 2026, with clinic names"
_CLINICS = [
    ["Kuura Kamppi", 3386398.81],
    ["Kuura Tallinn Kesklinn", 2766210.4],
    ["Kuura Tampere", 2575516.16],
    ["Kuura Pasila", 2353867.36],
    ["Kuura Tapiola", 2131507.53],
    ["Kuura Turku", 1933379.83],
    ["Kuura Tikkurila", 1692741.83],
    ["Kuura Oulu", 1689923.53],
    ["Kuura Herttoniemi", 1687865.35],
    ["Kuura Tallinn Mustamäe", 1489897.4],
    ["Kuura Jyväskylä", 1293097.21],
    ["Kuura Tartu", 1292833.43],
    ["Kuura Lahti", 1055232.15],
    ["Kuura Pärnu", 840586.47],
]
_FIVE_PROSE = (
    "Kuura Kamppi leads, followed by Kuura Tallinn Kesklinn, with Kuura Tapiola "
    "recording the lowest net revenue among the five clinics."
)


def _finance(definitions: list[Definition]) -> SemanticModel:
    return SemanticModel(
        lens="finance",
        dialect="duckdb",
        entities=[_REVENUE_DAILY, _INVOICE_LINES],
        definitions=definitions,
    )


def _serve_q13(model: SemanticModel, **overrides: object):
    args: dict[str, object] = dict(
        question=_Q13,
        lens_name="finance",
        org_id="org-1",
        caller="markus",
        semantic_model=model,
        connector=FakeConnector(
            result=QueryResult(columns=["clinic_name", "net_revenue_eur"], rows=_CLINICS)
        ),
        generator=FixedSQLGenerator(_Q13_SQL),
        composer=None,
        certification="certified",
        certified_match="exact",
        certified_provenance=_PROVENANCE,
        certified_question=_Q13,
    )
    return run_query(**{**args, **overrides})  # type: ignore[arg-type]


# ─── 1. definitions ride the certified rail ──────────────────────────────────


def test_period_rule_read_past_is_named() -> None:
    model = _finance([_REVENUE_DEF, _MAY_RULE])
    ignored = verification.ignored_definitions(_Q13_SQL, model)
    assert [d.term for d, _ in ignored] == ["may_2026_billing_duplicates"]
    assert "over 2026-05-01 to 2026-05-31" in ignored[0][1]


def test_period_rule_is_silent_outside_its_period_and_when_applied() -> None:
    model = _finance([_REVENUE_DEF, _MAY_RULE])
    september = (
        "select round(sum(revenue_eur), 2) as net_revenue_eur from marts.fct_revenue_daily "
        "where day between '2026-09-01' and '2026-09-30'"
    )
    assert verification.ignored_definitions(september, model) == []
    applied = (
        "select round(sum(revenue_eur), 2) from marts.fct_revenue_daily as revenue_daily "
        "where revenue_daily.day between '2026-01-01' and '2026-10-16' "
        "and revenue_daily.day not between date '2026-05-01' and date '2026-05-31'"
    )
    assert verification.ignored_definitions(applied, model) == []
    other_table = "select sum(net_amount) from marts.fct_invoice_lines_monthly"
    assert verification.ignored_definitions(other_table, model) == []


def test_rule_without_a_period_governs_the_questions_that_name_it() -> None:
    """A term's predicate governs the questions asking for the term, as on
    the generated rail — never every certified read of the table (that would
    demote every customer count for a `repeat_customer` rule)."""
    rule = _MAY_RULE.model_copy(update={"period": None, "aliases": ["duplicated export"]})
    unbounded = "select sum(revenue_eur) from marts.fct_revenue_daily"
    model = _finance([rule])
    assert verification.ignored_definitions(unbounded, model, "net revenue per clinic") == []
    named = verification.ignored_definitions(
        unbounded, model, "revenue net of the duplicated export"
    )
    assert len(named) == 1 and "for a question naming the term" in named[0][1]
    # A period rule needs no naming: it governs every read of its dates.
    assert len(verification.ignored_definitions(unbounded, _finance([_MAY_RULE]))) == 1


def test_served_window_reads_dates_then_years() -> None:
    from datetime import date

    assert verification.served_window(_Q13_SQL) == (date(2026, 1, 1), date(2026, 10, 16))
    assert verification.served_window("where extract(year from day) = 2025") == (
        date(2025, 1, 1),
        date(2025, 12, 31),
    )
    assert verification.served_window("select 1 from t") is None
    assert verification.period_dates("2026-05") == (date(2026, 5, 1), date(2026, 5, 31))
    assert verification.period_dates("2026") == (date(2026, 1, 1), date(2026, 12, 31))
    assert verification.period_dates("2026-05-04/2026-05-10") == (
        date(2026, 5, 4),
        date(2026, 5, 10),
    )


def test_period_shape_is_an_authoring_error() -> None:
    with pytest.raises(ValueError, match="period"):
        Definition(term="x", body="b", period="May 2026")


def test_certified_serve_over_a_flagged_period_is_not_verified() -> None:
    """The live shape (req_3eeb4a4d13c64af9): Q13 YTD per clinic served the
    mart's May overstatement `verified · certified`."""
    res = _serve_q13(_finance([_REVENUE_DEF, _MAY_RULE]), verified_value={"rows": _CLINICS[:5]})
    assert res.trace.status == "ok"
    report = res.response.verification
    assert report is not None and report.grade == "partial"
    check = report.check("definition_period")
    assert check is not None and check.status == "fail"
    assert "may_2026_billing_duplicates" in (check.reason or "")
    assert any(
        line.startswith("DEFINITION NOT APPLIED: `may_2026_billing_duplicates`")
        for line in res.response.degraded
    )
    assert "Definition NOT applied: `may_2026_billing_duplicates`" in (
        res.response.trust_summary or ""
    )
    assert "DEFINITION NOT APPLIED: `may_2026_billing_duplicates`" in res.response.answer
    # The basis still names the meaning the SQL computed, one line per term.
    assert (res.response.trust_summary or "").count("Computed as `revenue_net_eur`") == 1


def test_certified_serve_outside_the_period_stays_verified() -> None:
    september = (
        "select round(sum(revenue_eur), 2) as net_revenue_eur from marts.fct_revenue_daily "
        "where day between '2026-09-01' and '2026-09-30'"
    )
    res = _serve_q13(
        _finance([_REVENUE_DEF, _MAY_RULE]),
        question="Net revenue last month (September 2026)",
        certified_question="Net revenue last month (September 2026)",
        generator=FixedSQLGenerator(september),
        connector=FakeConnector(result=QueryResult(columns=["net_revenue_eur"], rows=[[1.0]])),
        verified_value={"value": 1.0},
    )
    report = res.response.verification
    assert report is not None and report.grade == "verified"
    assert report.check("definition_period") is not None
    assert report.check("definition_period").status == "pass"  # type: ignore[union-attr]
    assert not any("DEFINITION NOT APPLIED" in line for line in res.response.degraded)


def test_plan_and_apply_warn_certified_ignores_definition() -> None:
    answers = [
        {"question": _Q13, "sql": _Q13_SQL, "status": "active"},
        {
            "question": "Net revenue last month (September 2026)",
            "sql": "select sum(revenue_eur) from marts.fct_revenue_daily "
            "where day between '2026-09-01' and '2026-09-30'",
        },
        {"question": "retired one", "sql": _Q13_SQL, "status": "retired"},
    ]
    warnings = _certified_definition_warnings(answers, _finance([_REVENUE_DEF, _MAY_RULE]))
    assert len(warnings) == 1
    assert warnings[0].startswith(f"certified_ignores_definition: certified answer '{_Q13}'")
    assert "definition 'may_2026_billing_duplicates', period 2026-05" in warnings[0]


# ─── 2. prose over the whole result ──────────────────────────────────────────


def test_value_summary_records_the_count_only_for_a_whole_result() -> None:
    rows = [[f"c{i}", float(i)] for i in range(14)]
    whole = _value_summary(["clinic", "eur"], rows, exact_count=True)
    assert whole["row_count"] == 14 and len(whole["rows"]) == 5
    assert "row_count" not in _value_summary(["clinic", "eur"], rows)
    assert _value_summary(["n"], [[3]], exact_count=True) == {"value": 3}


def test_a_result_that_grew_has_moved() -> None:
    stored = {"columns": ["clinic", "eur"], "rows": _CLINICS[:5], "row_count": 14}
    live = QueryResult(columns=["clinic", "eur"], rows=_CLINICS)
    assert verification.restatement(stored, live) is None
    grown = QueryResult(columns=["clinic", "eur"], rows=[*_CLINICS, ["Kuura Vantaa", 1.0]])
    assert verification.restatement(stored, grown) == ("14 rows", "15 rows")


def test_sample_prose_never_serves_over_the_whole_result() -> None:
    """The live shape (req_3eeb4a4d13c64af9): prose composed over the 5-row
    probe said "the five clinics" over fourteen rows, `verified`."""
    legacy = {"columns": ["clinic_name", "net_revenue_eur"], "rows": _CLINICS[:5]}
    res = _serve_q13(
        _finance([_REVENUE_DEF]),
        verified_value=legacy,
        verified_prose=_FIVE_PROSE,
        composer=AnswerComposer(ScriptedLLM(["Kuura Kamppi leads the fourteen clinics."])),
    )
    assert "the five" not in res.response.answer
    assert "fourteen clinics" in res.response.answer
    assert any(
        line.startswith("SAMPLED: ") and "5-row sample" in line for line in res.response.degraded
    )
    report = res.response.verification
    assert report is not None and report.check("restated").status == "pass"  # type: ignore[union-attr]

    rows_only = _serve_q13(
        _finance([_REVENUE_DEF]), verified_value=legacy, verified_prose=_FIVE_PROSE
    )
    assert "the five" not in rows_only.response.answer
    assert rows_only.response.answer.startswith("Structured answer: 14 result rows")


def test_prose_over_the_whole_result_serves_verbatim() -> None:
    whole = {"columns": ["clinic_name", "net_revenue_eur"], "rows": _CLINICS[:5], "row_count": 14}
    prose = "Kuura Kamppi leads the fourteen clinics; Kuura Pärnu is lowest."
    res = _serve_q13(
        _finance([_REVENUE_DEF]),
        verified_value=whole,
        verified_prose=prose,
        composer=AnswerComposer(ScriptedLLM(["never called"])),
    )
    assert res.response.answer.startswith(prose)
    assert not any(line.startswith("SAMPLED") for line in res.response.degraded)


# ─── 3. the decline marker anywhere ──────────────────────────────────────────


def test_decline_marker_anywhere_in_the_prose() -> None:
    """The live shape (req_13e1d744cfc44ecc): the composer explained the
    mismatch, then declined on the next paragraph."""
    live = (
        "The SQL filters for completed or billed appointments in 2026 but does not "
        "restrict to each patient's **first-ever** visit, so it does not answer the question.\n\n"
        "NO_ANSWER: The query counts all patients with a completed/billed appointment in "
        "2026, not patients whose first-ever visit occurred in 2026."
    )
    reason = decline_reason(live)
    assert reason is not None and reason.startswith("The query counts all patients")
    assert decline_reason("**NO_ANSWER:** the rows list names") == "the rows list names"
    assert decline_reason("Prose first.\nNO_ANSWER — nothing here") == "nothing here"
    assert decline_reason("NO_ANSWER") == "the result does not answer the question"
    assert decline_reason("There are 100 customers; no answer beyond that is needed.") is None


# ─── 4. row conservation ─────────────────────────────────────────────────────

# The live shape (req_95ce136715de478d): `deduped` keeps May's single rows,
# `deduped_may` re-adds every May row, `combined` unions both.
_Q19_SQL = (
    "WITH deduped AS (SELECT invoice_lines.payer, invoice_lines.net_amount "
    "FROM marts.fct_invoice_lines_monthly AS invoice_lines "
    "WHERE invoice_lines.status <> 'credited' "
    "AND invoice_lines.partition_month BETWEEN '2026-01-01' AND '2026-10-01' "
    "AND NOT (invoice_lines.partition_month = '2026-05-01' AND (invoice_lines.invoice_id, "
    "invoice_lines.line_no) IN (SELECT invoice_lines2.invoice_id, invoice_lines2.line_no "
    "FROM marts.fct_invoice_lines_monthly AS invoice_lines2 "
    "WHERE invoice_lines2.partition_month = '2026-05-01' "
    "GROUP BY invoice_lines2.invoice_id, invoice_lines2.line_no HAVING COUNT(*) > 1))), "
    "deduped_may AS (SELECT invoice_lines.payer, invoice_lines.net_amount FROM "
    "(SELECT DISTINCT ON (invoice_lines.invoice_id, invoice_lines.line_no) invoice_lines.payer, "
    "invoice_lines.net_amount, invoice_lines.invoice_id, invoice_lines.line_no "
    "FROM marts.fct_invoice_lines_monthly AS invoice_lines "
    "WHERE invoice_lines.status <> 'credited' AND invoice_lines.partition_month = '2026-05-01') "
    "AS invoice_lines), "
    "combined AS (SELECT payer, net_amount FROM deduped UNION ALL "
    "SELECT payer, net_amount FROM deduped_may) "
    "SELECT combined.payer, SUM(combined.net_amount) AS revenue_net_eur FROM combined "
    "GROUP BY combined.payer ORDER BY revenue_net_eur DESC"
)


class _CountingConnector(FakeConnector):
    """The payer rows for the served SQL; the two conservation counts for the
    plan's count statements."""

    def __init__(self, produced: int, distinct: int) -> None:
        super().__init__(
            result=QueryResult(
                columns=["payer", "revenue_net_eur"], rows=[["patient", 15184813.95]]
            )
        )
        self.produced, self.distinct = produced, distinct
        self.counts: list[str] = []

    def execute(self, sql: str, *, read_only: bool = True, row_limit: int | None = None):
        if sql.startswith("SELECT COUNT(*) FROM (SELECT DISTINCT"):
            self.counts.append(sql)
            return QueryResult(columns=["n"], rows=[[self.distinct]])
        if sql.startswith("WITH") and "SELECT COUNT(*) FROM combined" in sql:
            self.counts.append(sql)
            return QueryResult(columns=["n"], rows=[[self.produced]])
        return super().execute(sql, read_only=read_only, row_limit=row_limit)


def test_conservation_plan_reads_the_union_over_one_table() -> None:
    plan = verification.row_conservation_plan(_Q19_SQL, _finance([]))
    assert isinstance(plan, verification.ConservationPlan)
    assert plan.cte == "combined" and plan.entity == "invoice_lines"
    assert plan.keys == ("invoice_id", "line_no")
    assert plan.produced_sql.startswith("WITH deduped AS")
    assert plan.produced_sql.endswith("SELECT COUNT(*) FROM combined")
    assert plan.distinct_sql.startswith(
        "SELECT COUNT(*) FROM (SELECT DISTINCT invoice_id, line_no "
        "FROM marts.fct_invoice_lines_monthly WHERE ("
    )
    assert "(status <> 'credited' AND partition_month = '2026-05-01')" in plan.distinct_sql


def test_conservation_skips_without_a_declared_key_and_ignores_tagged_unions() -> None:
    keyless = _INVOICE_LINES.model_copy(update={"primary_key": []})
    model = SemanticModel(lens="finance", dialect="duckdb", entities=[_REVENUE_DAILY, keyless])
    plan = verification.row_conservation_plan(_Q19_SQL, model)
    assert isinstance(plan, verification.VerificationCheck)
    assert plan.status == "skip" and "declares no primary_key" in (plan.reason or "")
    unpivot = (
        "WITH series AS (SELECT day, 'medical' AS kind, revenue_eur AS eur "
        "FROM marts.fct_revenue_daily UNION ALL SELECT day, 'retail' AS kind, vat_amount_eur "
        "FROM marts.fct_revenue_daily) SELECT kind, SUM(eur) FROM series GROUP BY kind"
    )
    assert verification.row_conservation_plan(unpivot, _finance([])) is None
    assert verification.row_conservation_plan(_Q13_SQL, _finance([])) is None


def test_double_counting_union_grades_partial_naming_the_counts() -> None:
    connector = _CountingConnector(produced=305_420, distinct=283_984)
    res = run_query(
        question="Revenue by payer for 2026 year to date.",
        lens_name="finance",
        org_id="org-1",
        caller="markus",
        semantic_model=_finance([]),
        connector=connector,
        generator=FixedSQLGenerator(_Q19_SQL),
        composer=None,
    )
    assert res.trace.status == "ok" and len(connector.counts) == 2
    report = res.response.verification
    assert report is not None and report.grade == "partial"
    assert report.partial_reason is not None
    assert report.partial_reason.startswith("row_conservation: `combined` yields 305,420 rows")
    assert "keys number 283,984" in report.partial_reason

    kept = _CountingConnector(produced=283_984, distinct=283_984)
    res = run_query(
        question="Revenue by payer for 2026 year to date.",
        lens_name="finance",
        org_id="org-1",
        caller="markus",
        semantic_model=_finance([]),
        connector=kept,
        generator=FixedSQLGenerator(_Q19_SQL),
        composer=None,
    )
    check = res.response.verification.check("row_conservation")  # type: ignore[union-attr]
    assert check is not None and check.status == "pass"


# ─── 5. the certified basis names what the SQL engages ───────────────────────

_APPOINTMENTS = Entity(
    name="appointments",
    source=EntitySource(connection="warehouse", table="marts.fct_appointments"),
    primary_key=["appointment_id"],
    fields=[
        Field(name="appointment_id", type="string"),
        Field(name="patient_id", type="string"),
        Field(name="status", type="string"),
        Field(name="appointment_date", type="date"),
        Field(name="lead_days", type="integer"),
    ],
    metrics=[
        Metric(name="bookings", agg="count", expr="appointments.appointment_id"),
        Metric(
            name="visits",
            agg="sum",
            expr="CASE WHEN appointments.status IN ('completed', 'billed') THEN 1 ELSE 0 END",
        ),
        Metric(
            name="no_shows",
            agg="sum",
            expr="CASE WHEN appointments.status = 'no_show' THEN 1 ELSE 0 END",
        ),
        Metric(
            name="attendance_base",
            agg="sum",
            expr="CASE WHEN appointments.status IN ('completed', 'billed', 'no_show') "
            "THEN 1 ELSE 0 END",
        ),
        Metric(
            name="no_show_rate", type="ratio", numerator="no_shows", denominator="attendance_base"
        ),
    ],
)
_PATIENT_DEFS = [
    Definition(
        term="bookings", summary="any appointment record", body="b", about="appointments.bookings"
    ),
    Definition(
        term="visits", summary="status completed or billed", body="v", about="appointments.visits"
    ),
    Definition(
        term="no_show_rate",
        summary="no-shows / (visits + no-shows)",
        body="n",
        about="appointments.no_show_rate",
    ),
]
_PATIENT_MODEL = SemanticModel(
    lens="patient_experience", dialect="duckdb", entities=[_APPOINTMENTS], definitions=_PATIENT_DEFS
)


def test_median_over_appointments_cites_only_the_metric_it_computes() -> None:
    """The live shape (req_6400e3c4648e46a1): a median lead time cited the
    no-show-rate page — a ratio reads no column of its own, so reading the
    table "read" it. COUNT(*) over the keyed table is `bookings`; nothing
    here computes a no-show rate."""
    sql = (
        "SELECT MEDIAN(lead_days) AS median_lead_days, COUNT(*) AS appointments "
        "FROM marts.fct_appointments WHERE appointment_date >= '2026-01-01' "
        "AND appointment_date < '2027-01-01'"
    )
    cited = certified_definitions(sql, ["median_lead_days", "appointments"], _PATIENT_MODEL)
    assert [d.term for d in cited] == ["bookings"]


def test_first_visit_template_cites_visits_not_the_ratio() -> None:
    sql = (
        "SELECT d.country AS clinic_country, COUNT(*) AS new_patients FROM (SELECT patient_id, "
        "MIN(appointment_date) AS first_visit FROM marts.fct_appointments "
        "WHERE status IN ('completed', 'billed') GROUP BY 1) AS f "
        "JOIN marts.dim_clinic AS d ON TRUE WHERE f.first_visit >= '2026-01-01' GROUP BY 1"
    )
    cited = certified_definitions(sql, ["clinic_country", "new_patients"], _PATIENT_MODEL)
    assert [d.term for d in cited] == ["visits"]


def test_computed_ratio_cites_its_page() -> None:
    sql = (
        "SELECT SUM(CASE WHEN appointments.status = 'no_show' THEN 1 ELSE 0 END) / "
        "SUM(CASE WHEN appointments.status IN ('completed', 'billed', 'no_show') "
        "THEN 1 ELSE 0 END) AS no_show_rate FROM marts.fct_appointments AS appointments"
    )
    cited = certified_definitions(sql, ["no_show_rate"], _PATIENT_MODEL)
    assert "no_show_rate" in [d.term for d in cited]


def test_certified_basis_is_one_line_per_engaged_term() -> None:
    res = _serve_q13(_finance([_REVENUE_DEF, _MAY_RULE]), verified_value={"rows": _CLINICS[:5]})
    basis = res.response.trust_summary or ""
    assert basis.count("Computed as `") == 1
    assert "Computed as `revenue_net_eur`" in basis
