"""The cube lane: a question (or a caller's own Cube query) to a served answer.

    candidate → pre-checks → Cube compiles the SQL (``/v1/sql``) → dst runs
    it through the lens's own connection, under ``sql_guard`` → post-checks
    → response, receipt, trace

Cube is the compiler, not the executor: the warehouse credential stays in one
home, the guard sees every statement, and the SQL that served is the SQL on
the receipt. A check that must look at data (the readings of an ambiguous
term, the figure without an unasked filter, the rows past a limit) runs its
query the same way.

The lane writes no prose: the answer text is the lane's note and every
disclosure, deterministic, so the same ask reads the same.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from services.contracts.cube_query import Binding, Candidate, CubeQuery, absolute_range
from services.contracts.failure_modes import FailureMode
from services.contracts.lens_config import LensConfig
from services.contracts.protocols import Connector
from services.contracts.response import (
    AnswerStatus,
    ClarificationRequest,
    DataPayload,
    QueryResponse,
    TruncationInfo,
)
from services.contracts.semantic_model import SemanticModel
from services.contracts.trace import TraceLog
from services.contracts.verification import VerificationReport
from services.cube.client import CubeClient, CubeError
from services.cube.context import CubeContext
from services.cube.lens_view import CubeLensView
from services.runtime import receipt as receipts
from services.runtime import sql_guard
from services.runtime.external_access import ExternalAccessRefused
from services.verify import post
from services.verify.base import Ask, Check, Rows
from services.verify.pre import PRE_ORDER, QUESTION_ONLY

log = logging.getLogger("dst.cube.lane")

# Modes whose disclosure caps a served answer at ``partial``: the figure is
# right for what is loaded, and what is loaded is short of the ask.
_CAPS = {FailureMode.data_gap, FailureMode.stale, FailureMode.unverifiable}


class LaneRefusal(RuntimeError):
    """A statement the guard would not run, or a Cube call that failed with
    a mode: ends the ask with that mode."""

    def __init__(self, mode: FailureMode | None, detail: str) -> None:
        self.mode, self.detail = mode, detail
        super().__init__(detail)


def inline_params(sql: str, params: list[Any], dialect: str = "duckdb") -> str:
    """Cube's compiled SQL with its parameters written in as literals
    (``?`` in order, or ``$n``), outside quoted strings and identifiers, so
    the guard and the connector see the one statement that runs.

    BigQuery quotes differently: ``"`` opens a string, not an identifier,
    identifiers are in backticks, a quote inside a string is escaped with a
    backslash (``'it''s'`` is two literals there, a syntax error) and a
    string may be triple-quoted. The scan and the literals follow the
    dialect, so a value with a quote in it is one literal on every warehouse."""
    bq = dialect == "bigquery"
    quotes = ("'", '"', "`") if bq else ("'", '"')
    out: list[str] = []
    i, n, k = 0, len(sql), 0
    while i < n:
        ch = sql[i]
        if ch in quotes:
            j = _quoted_end(sql, i, bq)
            out.append(sql[i:j])
            i = j
            continue
        if ch == "?" and k < len(params):
            out.append(_literal(params[k], bq))
            k += 1
        elif ch == "$" and i + 1 < n and sql[i + 1].isdigit():
            j = i + 1
            while j < n and sql[j].isdigit():
                j += 1
            idx = int(sql[i + 1 : j]) - 1
            if 0 <= idx < len(params):
                out.append(_literal(params[idx], bq))
                i = j
                continue
            out.append(ch)
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def _quoted_end(sql: str, i: int, bq: bool) -> int:
    """The index just past the quoted span opening at ``sql[i]`` (or the end
    of the statement, if it never closes)."""
    q, n = sql[i], len(sql)
    if bq and q != "`" and sql.startswith(q * 3, i):
        close = sql.find(q * 3, i + 3)
        while close != -1 and _escaped(sql, close, i + 3):
            close = sql.find(q * 3, close + 1)
        return n if close == -1 else close + 3
    j = i + 1
    while j < n:
        if bq and sql[j] == "\\":
            j += 2
            continue
        if sql[j] == q:
            if not bq and j + 1 < n and sql[j + 1] == q:  # '' inside a standard string
                j += 2
                continue
            return j + 1
        j += 1
    return n


def _escaped(sql: str, j: int, start: int) -> bool:
    backslashes = 0
    while j - 1 - backslashes >= start and sql[j - 1 - backslashes] == "\\":
        backslashes += 1
    return backslashes % 2 == 1


def _literal(value: Any, bq: bool = False) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if bq:
        return "'" + str(value).replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"
    return "'" + str(value).replace("'", "''") + "'"


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, date):
        return value.isoformat()
    return value


def _range_label(r: Any) -> str:
    return " - ".join(str(x)[:10] for x in r) if isinstance(r, list) else str(r)


def absolute(q: CubeQuery, today: date) -> CubeQuery:
    """Every window as absolute ISO days against the lens clock: Cube would
    otherwise read a relative phrase against its own clock."""
    q = q.model_copy(deep=True)
    for td in q.timeDimensions:
        if td.dateRange is not None:
            td.dateRange = absolute_range(td.dateRange, today)
        if td.compareDateRange is not None:
            td.compareDateRange = [absolute_range(r, today) for r in td.compareDateRange]
    return q


def _relative_left(q: CubeQuery) -> str | None:
    for td in q.timeDimensions:
        for r in [td.dateRange, *(td.compareDateRange or [])]:
            if isinstance(r, str):
                return r
    return None


def _sha(obj: Any) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


@dataclass
class Served:
    query: CubeQuery
    rows: Rows


@dataclass
class CubeLane:
    lens: str
    config: LensConfig
    view: CubeLensView
    client: CubeClient
    ctx: CubeContext
    connector: Connector
    dialect: str
    today: date
    org_id: str = ""
    caller: str = ""
    groups: list[str] = field(default_factory=list)
    request_id: str = ""
    executed: list[str] = field(default_factory=list)
    # The lens's certified question → Cube query pairs: the frontier lane's
    # exemplars, in its cached system block.
    exemplars: list[tuple[str, CubeQuery]] = field(default_factory=list)
    # Set when the lane serves a certified pair's Cube query: the answer and
    # its receipt are certified under this id.
    cert_id: str | None = None

    # ── executing a Cube query ────────────────────────────────────────────

    def run(self, q: CubeQuery) -> Rows:
        """One Cube query's rows, keyed by member: Cube compiles, dst guards
        and executes. A comparison runs one statement per window, each row
        tagged ``compareDateRange``."""
        cmp_td = next((t for t in q.timeDimensions if t.compareDateRange), None)
        if cmp_td is not None:
            rows: Rows = []
            for r in cmp_td.compareDateRange or []:
                one = q.model_copy(deep=True)
                for t in one.timeDimensions:
                    if t.dimension == cmp_td.dimension and t.compareDateRange:
                        t.compareDateRange, t.dateRange = None, r
                rows += [{**row, "compareDateRange": _range_label(r)} for row in self.run(one)]
            return rows
        try:
            compiled = self.client.sql(q, self.ctx)
        except CubeError as exc:
            raise LaneRefusal(exc.mode, exc.detail) from exc
        statement = inline_params(compiled.sql, compiled.params, self.dialect)
        model = SemanticModel.model_validate({"lens": self.lens, "dialect": self.dialect})
        guard = sql_guard.check(statement, model, trust_tables=True)
        if not guard.ok:
            raise LaneRefusal(guard.mode, f"the compiled SQL was not run: {guard.reason}")
        sql = guard.sql or statement
        self.executed.append(sql)
        try:
            result = self.connector.execute(sql, read_only=True)
        except ExternalAccessRefused as exc:
            raise LaneRefusal(FailureMode.external_access, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — the warehouse's own error, any connector
            # Cube's SQL failing on the warehouse is a fault, named and loud.
            raise LaneRefusal(None, f"the warehouse did not run Cube's SQL: {exc}") from exc
        alias = (compiled.raw.get("sql") or {}).get("aliasNameToMember") or {}
        return [
            {str(alias.get(c, c)): _plain(v) for c, v in zip(result.columns, row, strict=False)}
            for row in result.rows
        ]

    # ── one ask ───────────────────────────────────────────────────────────

    def ask(
        self,
        question: str,
        *,
        ledger: list[Binding] | None,
        queries: list[CubeQuery],
    ) -> Ask:
        queries = [absolute(q, self.today) for q in queries]
        return Ask(
            question=question,
            lens=self.view,
            config=self.config,
            groups=list(self.groups),
            ledger=ledger,
            asked=queries,
            run=self.run,
            today=self.today,
        )

    def question_refusal(self, question: str) -> Check | None:
        """The refusals that need no query: run before a lane spends a
        model call on a question the lens refuses whatever the query."""
        a = Ask(question=question, lens=self.view, config=self.config, groups=self.groups)
        for check in QUESTION_ONLY:
            c = check(a, [])
            if c.stops:
                return c
        return None

    def serve(
        self,
        question: str,
        candidates: list[Candidate],
        *,
        ledger: list[Binding] | None,
        lane: str,
        note: str = "",
        structured: bool = False,
        untested: str | None = None,
    ) -> tuple[QueryResponse, TraceLog]:
        """The candidates (one decision of one to three queries) through every
        check, executed and served. ``structured``: the caller wrote the
        query itself (no question words to account for, no ledger).
        ``untested``: why the question-word checks a structured ask skips
        could not run, recorded as a skip on the receipt instead of left out."""
        started = time.monotonic()
        ask = self.ask(question, ledger=ledger, queries=[c.query for c in candidates])
        checks: list[Check] = []
        qs = list(ask.asked)
        left = next((r for r in map(_relative_left, qs) if r), None)
        if left is not None:
            stop = Check(
                "period",
                FailureMode.period_unreadable,
                "clarify",
                f"the window '{left}' does not read as dates against {self.today.isoformat()}; "
                "which dates do you mean?",
            )
            return self._non_answer(question, stop, [stop], qs, ledger, lane, started)
        try:
            for check in PRE_ORDER:
                if structured and check.__name__ in ("uncovered_word", "unasked_filter"):
                    # A caller's own query names its members: no question
                    # words to account for, no scope the caller did not ask.
                    if untested:
                        name = "words" if check.__name__ == "uncovered_word" else "unasked_scope"
                        checks.append(Check(name, None, "skip", f"UNTESTED: {untested}"))
                    continue
                c = check(ask, qs)
                checks.append(c)
                if c.stops:
                    return self._non_answer(question, c, checks, qs, ledger, lane, started)
                if c.queries is not None:
                    qs = c.queries
            part = post.partial_period(ask, qs)
            checks.append(part)
            if part.stops:
                return self._non_answer(question, part, checks, qs, ledger, lane, started)
            served: list[Served] = []
            refusals: list[Check] = []
            for q in qs:
                fresh = post.data_gap(ask, q)
                checks.append(fresh)
                if fresh.stops:
                    refusals.append(fresh)
                    continue
                for q2, pin, coarse in post.snapshot_split(self.view, q):
                    if pin:
                        checks.append(post.snapshot_pin(ask, q))
                    rows = self.run(q2)
                    if pin:
                        rows = post.snapshot_reduce(q2, rows, pin, coarse)
                    empty = post.empty_result(ask, q2, rows)
                    checks.append(empty)
                    if empty.stops:
                        refusals.append(empty)
                        continue
                    nb = post.null_bucket(ask, q2, rows)
                    checks.append(nb)
                    if nb.queries:
                        q2, rows = nb.queries[0], nb.rows or []
                    if not pin:
                        tie = post.tie_at_limit(ask, q2, rows)
                        checks.append(tie)
                        if tie.rows is not None:
                            rows = tie.rows
                            # The rows past the limit may hold the empty
                            # bucket the first page did not: never ranked.
                            again = post.null_bucket(ask, q2, rows)
                            if again.queries:
                                checks.append(again)
                                q2, rows = again.queries[0], again.rows or []
                                tie = post.tie_at_limit(ask, q2, rows)
                                checks.append(tie)
                                if tie.rows is not None:
                                    rows = tie.rows
                    gap = post.empty_window(ask, q2, rows)
                    checks.append(gap)
                    served.append(Served(q2, post.compare_pivot(q2, rows)))
        except LaneRefusal as exc:
            stop = Check("execute", exc.mode, "refuse", exc.detail)
            checks.append(stop)
            return self._non_answer(question, stop, checks, qs, ledger, lane, started)
        if not served:
            first = refusals[0]
            stop = Check(
                first.name,
                first.mode,
                "refuse",
                "; ".join(dict.fromkeys(r.detail for r in refusals)),
            )
            return self._non_answer(question, stop, checks, qs, ledger, lane, started)
        # A gap on some of the ask's queries, not all: what is loaded serves,
        # and what is not is said beside it, never dropped.
        checks = [
            Check(c.name, c.mode, "disclose", c.detail) if any(c is r for r in refusals) else c
            for c in checks
        ]
        return self._answer(question, served, checks, ledger, lane, note, started)

    # ── responses ─────────────────────────────────────────────────────────

    def non_answer(
        self,
        question: str,
        stop: Check,
        *,
        queries: list[CubeQuery] | None = None,
        ledger: list[Binding] | None = None,
        lane: str,
    ) -> tuple[QueryResponse, TraceLog]:
        """A refusal or a clarification decided outside the checks (the
        lane said it cannot, or asked), in the same shape."""
        return self._non_answer(
            question, stop, [stop], queries or [], ledger, lane, time.monotonic()
        )

    def _receipt_checks(self, checks: list[Check]) -> list[str]:
        return [f"{c.name}:{c.outcome}" for c in checks]

    def _trace(
        self,
        question: str,
        status: str,
        qs: list[CubeQuery],
        ledger: list[Binding] | None,
        lane: str,
        started: float,
        **kw: Any,
    ) -> TraceLog:
        return TraceLog(
            request_id=self.request_id,
            org_id=self.org_id,
            lens=self.lens,
            caller=self.caller,
            question=question,
            status=status,  # type: ignore[arg-type]
            sql=";\n".join(self.executed) or None,
            cube_query=[q.wire() for q in qs],
            ledger=ledger,
            serving=f"cube:{lane}",
            latency_ms={"cube_lane": round((time.monotonic() - started) * 1000, 1)},
            **kw,
        )

    def _non_answer(
        self,
        question: str,
        stop: Check,
        checks: list[Check],
        qs: list[CubeQuery],
        ledger: list[Binding] | None,
        lane: str,
        started: float,
    ) -> tuple[QueryResponse, TraceLog]:
        # A stop with no mode is a fault (Cube or the guard failed in a way no
        # mode names): it answers ``error`` and fails loud, never a refusal.
        status: AnswerStatus = (
            "error"
            if stop.mode is None
            else "clarification"
            if stop.outcome == "clarify"
            else "refused"
        )
        clar = (
            ClarificationRequest(
                term=stop.name, question=stop.detail, kind="unresolved_slot", mode=stop.mode
            )
            if status == "clarification"
            else None
        )
        report = VerificationReport(grade="unverified", checks=[c.verification() for c in checks])
        resp = QueryResponse(
            lens=self.lens,
            status=status,
            answer=stop.detail,
            clarification=clar,
            verification=report,
            mode=stop.mode,
            request_id=self.request_id,
        )
        trace = self._trace(
            question,
            status,
            qs,
            ledger,
            lane,
            started,
            answer=stop.detail,
            clarification=clar,
            verification=report,
            error=stop.detail if status == "error" else None,
            mode=stop.mode,
        )
        return resp, trace

    def _answer(
        self,
        question: str,
        served: list[Served],
        checks: list[Check],
        ledger: list[Binding] | None,
        lane: str,
        note: str,
        started: float,
    ) -> tuple[QueryResponse, TraceLog]:
        columns, rows = _table(served)
        caveats = self.view.caveats([m for s in served for m in _read(s.query)])
        if caveats:
            # The author's caveats on what was read: a fact dst cannot check
            # the caller relays, so it leads the answer and is signed.
            checks = [*checks, Check("caveats", None, "disclose", "; ".join(caveats))]
        cap = next((c for c in checks if c.outcome == "disclose" and c.mode in _CAPS), None)
        report = VerificationReport(
            grade="partial" if cap else "verified",
            checks=[c.verification() for c in checks],
            partial_reason=f"{cap.name}: {cap.detail}" if cap else None,
        )
        disclosures = [c.detail for c in checks if c.outcome == "disclose" and c.name != "caveats"]
        disclosures += [c.detail for c in checks if c.outcome == "pass" and c.detail]
        limit = self.config.model.max_rows_to_return
        shown = rows[:limit] if limit else rows
        text = " ".join(
            x
            for x in [
                *(f"CAVEAT {c}" for c in caveats),
                note.strip().rstrip(".") + "." if note.strip() else "",
                f"{len(rows)} row{'s' if len(rows) != 1 else ''}.",
                *(f"NOTE {d}" for d in dict.fromkeys(disclosures)),
            ]
            if x
        )
        qs = [s.query for s in served]
        # Rows of an unordered ask come back in whatever order the warehouse
        # chose; their identity is the set, so the hash reads them sorted.
        ordered = any(q.order for q in qs)
        hashed = rows if ordered else sorted(rows, key=lambda r: json.dumps(r, default=str))
        result_sha = _sha({"columns": columns, "rows": hashed})
        query_sha = (
            qs[0].sha256(self.today)
            if len(qs) == 1
            else _sha([q.canonical(self.today) for q in qs])
        )
        sql = ";\n".join(self.executed)
        rc = receipts.build(
            request_id=self.request_id,
            lens=self.lens,
            certification="certified" if self.cert_id else "none",
            cert_id=self.cert_id,
            confidence=report.grade,
            sql=sql,
            data_as_of=self.view.probe.clock_end,
            mode=cap.mode if cap else None,
            query_sha256=query_sha,
            result_sha256=result_sha,
            model_version=self.view.model_version,
            checks=self._receipt_checks(checks),
            caveats=caveats,
            disclosures=list(dict.fromkeys(disclosures)),
        )
        resp = QueryResponse(
            lens=self.lens,
            status="ok",
            answer=text,
            data=DataPayload(
                columns=columns,
                rows=shown,
                row_count=len(rows),
                truncated=len(shown) < len(rows),
            ),
            sql=sql,
            confidence=report.grade,
            verification=report,
            data_as_of=self.view.probe.clock_end,
            truncated=TruncationInfo(returned=len(shown), total=len(rows))
            if len(shown) < len(rows)
            else None,
            receipt=rc,
            certification="certified" if self.cert_id else "none",
            caveats=caveats,
            mode=cap.mode if cap else None,
            request_id=self.request_id,
        )
        trace = self._trace(
            question,
            "ok",
            qs,
            ledger,
            lane,
            started,
            answer=text,
            row_count=len(rows),
            confidence=report.grade,
            verification=report,
            mode=cap.mode if cap else None,
        )
        return resp, trace


def _read(q: CubeQuery) -> list[str]:
    """Every member a query reads: measures, dimensions, segments, time
    dimensions and filter members (nested groups too)."""
    out = [*q.measures, *q.dimensions, *q.segments, *(t.dimension for t in q.timeDimensions)]
    stack = list(q.filters)
    while stack:
        f = stack.pop(0)
        if f.member:
            out.append(f.member)
        stack += [*(f.and_ or []), *(f.or_ or [])]
    return out


def _table(served: list[Served]) -> tuple[list[str], list[list[object]]]:
    """One table for the response: a single query's rows as they are; several
    queries' rows stacked under a leading ``result`` column (1, 2, …)."""

    def cols_of(s: Served) -> list[str]:
        cols: list[str] = []
        for r in s.rows:
            cols += [c for c in r if c not in cols]
        return cols

    if len(served) == 1:
        cols = cols_of(served[0])
        return cols, [[r.get(c) for c in cols] for r in served[0].rows]
    cols = ["result"]
    for s in served:
        cols += [c for c in cols_of(s) if c not in cols]
    rows: list[list[object]] = []
    for i, s in enumerate(served, 1):
        rows += [[i, *(r.get(c) for c in cols[1:])] for r in s.rows]
    return cols, rows
