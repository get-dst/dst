"""Caller-simulation arms: the agent loop lives OUTSIDE the lens.

Open-ended task reasoning belongs to the caller; dst is the governed data
plane the loop iterates against. Three arms, one loop, one budget:

- ``AgenticBaselineLane``  — tool = raw read-only SQL, uninformed. A control,
  never a headline: see below.
- ``AgenticMemoLane``      — the same raw SQL tool **plus MEMO**, a 4 KB
  CONVENTIONS.md carrying every convention the lens encodes, written by the same
  author (see ``conventions.py``). Three variants: static MEMO, MEMO+ (the agent
  may append after a correction — what agent memory does today), and MEMO×M (M
  independent sessions, each maintaining its own copy).
- ``AgenticDstLane``   — the governed lens: ``describe`` (free — zero LLM
  calls, zero warehouse hits) then ``ask`` (the full pipeline).

**Accuracy against the uninformed control is not a result.** Every fact a
question needs is either derivable from the warehouse — a probing agent gets it
free — or it is not, in which case it must be transferred as text, and every
text channel transfers it equally: memo, skill, agent instructions file, MCP
resource, lens YAML. So an accuracy headline vs. an uninformed baseline measures
the *presence of a file*, and one markdown file refutes it. (``lanes.py`` itself
injects the caller's identity as a prose ContextChunk — that mechanism is a
string in a prompt and ties against MEMO given the same string.) MEMO is the
control that makes any arm interpretable; a published evaluation found a 4 KB
markdown file buying +17–23pp across three frontier models (arXiv 2604.25149).

What differentiates the arms is enforcement and determinism, not accuracy:
``pressure.py`` (does the arm hold its conventions when the caller pushes?),
``drift.py`` (how many distinct answers does one question get across sessions,
callers and consumer stacks?), and the runner's Silent-Wrong Rate.

Every fairness rule below is a fix for a way this harness could be rigged, and
each one cuts in a direction someone would rather it did not:

1. **Budget is cost, not turns.** ``Budget`` caps tool calls, wall-clock seconds
   AND tokens together. A 3.5 ms SQL tool and an 80 s lens ask are not
   comparable per-turn; per-second and per-token they are. Run several budget
   levels and report all of them — "equal budget" is a curve, not a number.
2. **Protocol failures never consume budget** and are reported as a first-class
   metric. A ```json fence is not task difficulty. Nudges are unlimited (bounded
   only by the token/wall budget they burn, plus a liveness cap).
3. **The agent designates its answer** — ``{"done": true, "answer_from": N}``,
   default the last observation. No arm is ordered to restate the question
   verbatim; that order guaranteed agent-dst ≈ one-shot-dst plus N
   wasted calls, and the loop could not help it.
4. **Assistant turns are not truncated.** An agent that cannot see the 1,800-char
   SQL it just ran is debugging blind.
5. **The shape contract is one sentence, identical in every arm.**
6. ``describe`` does not consume the *call* budget, because the SQL arms get
   their schema and their conventions file free in the system prompt. It does
   consume tokens and wall-clock, and its use is reported (``free_calls``).
7. **Every arm knows whose behalf it asks on** — the opening turn carries the
   principal's identity (name + role), never their answering conventions. A
   production driver agent knows its principal; a loop that hides it makes
   user-context questions structurally unpassable for every arm — the driver
   pins the default definition onto a caller whose lens says otherwise.

Protocol is provider-agnostic JSON (one LLM call per step), extracted
permissively — fenced, bare, or embedded in prose:
    {"sql": "<query>"}   |   {"ask": "<question>"}   |   {"describe": true}
    {"done": true}  /  {"done": true, "answer_from": 2}
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from threading import Lock, RLock

from services.contracts.protocols import CacheableBlock, LLMProvider, Message
from services.observability.cost import ai_cost_usd

from .conventions import MEMO_BYTES, memo_file
from .lanes import DstLane, LaneAnswer
from .questions import Question
from .warehouse import Warehouse, coerce

_OBS_ROWS = 20
_OBS_CHARS = 2000
_OBS_SQL_CHARS = 2000
_LOG_CHARS = 2000
# Not a budget — a liveness cap. Nudges are free of the call budget and bounded
# by the tokens they burn, but a model that will never comply must still stop.
_MAX_CONSECUTIVE_PROTOCOL_FAILURES = 4
_MAX_FREE_CALLS = 3

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

# The SAME sentence in every arm. Grading needs a shape contract; a shape
# contract given to one arm and not the other is a thumb on the scale.
_SHAPE_CONTRACT = (
    "The result you designate is your answer and is graded exactly as it "
    "stands: it must contain what the question asks and nothing else — right "
    "rows, right columns, no exploration dumps, no extra columns. Exploration "
    "steps may be as broad as you like; only the designated one is graded."
)

_PROTOCOL = (
    'Reply with ONLY a JSON object. When the question is answered: {"done": true} '
    '(your last result is the answer) or {"done": true, "answer_from": N} to '
    "designate the result of step N instead."
)


@dataclass(frozen=True)
class Budget:
    """Equal budget must mean equal COST, so all three caps bind at once.

    Turns were the old budget and they were meaningless across arms: six turns
    of a 3.5 ms SQL tool is a different purchase from six turns of an 80 s
    governed ask. Wall-clock and tokens are what the buyer actually spends.
    """

    tool_calls: int = 8
    seconds: float = 600.0
    tokens: int = 200_000

    @property
    def label(self) -> str:
        return f"{self.tool_calls}c-{self.seconds:.0f}s-{self.tokens // 1000}kt"

    @classmethod
    def parse(cls, spec: str) -> Budget:
        """``calls[:seconds[:tokens]]`` — e.g. ``4:120`` or ``12:600:400000``."""
        parts = [p.strip() for p in spec.split(":") if p.strip()]
        if not parts:
            raise ValueError(f"empty budget spec: {spec!r}")
        base = cls()
        return cls(
            tool_calls=int(parts[0]),
            seconds=float(parts[1]) if len(parts) > 1 else base.seconds,
            tokens=int(parts[2]) if len(parts) > 2 else base.tokens,
        )


def _json_objects(text: str) -> list[str]:
    """Every balanced ``{...}`` span in the text, string-literal aware."""
    out: list[str] = []
    depth, start, in_str, esc = 0, -1, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0:
                out.append(text[start : i + 1])
    return out


def extract_step(text: str, verbs: set[str]) -> dict[str, object] | None:
    """Permissive protocol extraction: a fenced block, a bare object, or an
    object embedded in prose. The LAST object carrying a known verb wins — a
    model that narrates its plan and then acts is acting in the last object.

    Strictness here graded the *formatter*, not the analyst: under the old
    parse a ```json fence was a protocol failure, and two of them graded the
    run ``declined``, which then got read as task difficulty.
    """
    chunks = [m.group(1) for m in _JSON_FENCE.finditer(text)] + [text]
    fallback: dict[str, object] | None = None
    for chunk in chunks:
        for blob in _json_objects(chunk):
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            if verbs & set(obj):
                fallback = obj  # keep scanning: the last verb-carrying object wins
            elif fallback is None:
                fallback = obj
    return fallback


def _table(columns: list[str], rows: list[list[object]]) -> str:
    head = [columns] + [[str(c)[:60] for c in r] for r in rows[:_OBS_ROWS]]
    text = "\n".join(" | ".join(map(str, r)) for r in head)
    more = f"\n… ({len(rows)} rows total)" if len(rows) > _OBS_ROWS else ""
    return (text + more)[:_OBS_CHARS]


def _obs(answer: LaneAnswer) -> str:
    """What the caller sees back. Carries the SQL that ran, the certification
    and the confidence — an agent that cannot see whether it got a *certified*
    answer cannot reason about whether to trust it, which is half the product."""
    head: list[str] = []
    if answer.sql:
        head.append(f"sql: {answer.sql[:_OBS_SQL_CHARS]}")
    if answer.certification:
        head.append(f"certification: {answer.certification}")
    if answer.confidence:
        head.append(f"confidence: {answer.confidence}")
    if answer.error and not answer.rows:
        head.append(f"ERROR: {answer.error[:400]}")
    else:
        head.append(_table(answer.columns, answer.rows))
    return "\n".join(head)


class _AgentLoop:
    """One loop, every arm. Tools are named callables; ``free`` names the ones
    that cost no LLM call and no warehouse hit."""

    def __init__(
        self,
        llm: LLMProvider,
        *,
        model: str,
        budget: Budget,
        system: str,
        tool_names: list[str],
        free_names: list[str] | None = None,
        principals: dict[str, str] | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._budget = budget
        self._system = system
        self._tool_names = tool_names
        self._free_names = free_names or []
        self._verbs = set(tool_names) | set(self._free_names) | {"done"}
        # Caller id → identity ("the CFO (CFO)") — IDENTITY ONLY, never the
        # caller's answering conventions: a production driver agent knows WHO it
        # serves; what the org's words mean must arrive through each arm's own
        # channel (MEMO file, lens runtime context), or the arms stop differing
        # in delivery mechanism. Without this line the driver pins the default
        # definition onto whoever asks, and no arm can pass a user-context
        # question.
        self._principals = principals or {}

    def _nudge(self) -> str:
        return (
            "That reply was not a JSON protocol step, so nothing ran (this costs "
            "you no tool calls). " + self._protocol_line()
        )

    def _protocol_line(self) -> str:
        verbs = [f'{{"{v}": "…"}}' for v in self._tool_names]
        verbs += [f'{{"{v}": true}}' for v in self._free_names]
        return "Next: " + " or ".join(verbs) + ". " + _PROTOCOL

    def run(
        self,
        question: Question,
        tools: dict[str, Callable[[str], LaneAnswer]],
        free: dict[str, Callable[[str], str]] | None = None,
        *,
        system: str | None = None,
    ) -> LaneAnswer:
        """Callables are bound per run, never on the loop: the runner fans
        questions out over threads, so per-question state on a shared lane would
        cross-contaminate. ``system`` overrides the prompt for this run — a
        mutable memo has moved since the lane was built."""
        free = free or {}
        system = system or self._system
        started = time.perf_counter()
        opening = question.question
        who = self._principals.get(question.caller or "")
        if who:
            opening = f"Asking on behalf of: {who}.\n\n{question.question}"
        messages: list[Message] = [Message(role="user", content=opening)]
        observations: list[LaneAnswer] = []
        calls = free_calls = protocol_failures = tokens = 0
        consecutive_bad = 0
        spent = 0.0
        log: list[str] = []
        designated: int | None = None
        stop = ""
        while True:
            stop = self._exhausted(started, calls, tokens)
            if stop:
                log.append(f"BUDGET: {stop}")
                break
            result = self._llm.complete(
                system=[CacheableBlock(text=system, ttl="1h")],
                messages=messages,
                model=self._model,
                temperature=0.0,
                # Reasoning-mode headroom (same bug as the lanes' 1024 cap):
                # thinking bills against max_tokens before the JSON lands.
                max_tokens=8192,
            )
            tokens += result.input_tokens + result.output_tokens
            spent += ai_cost_usd(self._model, result.input_tokens, result.output_tokens) or 0.0
            log.append(f"MODEL: {result.text[:_LOG_CHARS]}")

            step = extract_step(result.text, self._verbs)
            if step is None or not (self._verbs & set(step)):
                protocol_failures += 1
                consecutive_bad += 1
                if consecutive_bad > _MAX_CONSECUTIVE_PROTOCOL_FAILURES:
                    stop = f"{protocol_failures} unparseable replies — gave up"
                    log.append(f"BUDGET: {stop}")
                    break
                # Untruncated: an agent must be able to see what it just said.
                messages.append(Message(role="assistant", content=result.text))
                messages.append(Message(role="user", content=self._nudge()))
                continue
            consecutive_bad = 0

            if step.get("done"):
                designated = _as_index(step.get("answer_from"))
                break

            free_name = next((n for n in free if step.get(n)), None)
            if free_name is not None:
                messages.append(Message(role="assistant", content=result.text))
                if free_calls >= _MAX_FREE_CALLS:
                    # A free tool called forever would spin forever: count it
                    # against the protocol, which is what the liveness cap
                    # bounds. (Tokens bound it too, but 100k iterations of a
                    # cheap model is not a bound anyone wants to discover.)
                    protocol_failures += 1
                    consecutive_bad += 1
                    if consecutive_bad > _MAX_CONSECUTIVE_PROTOCOL_FAILURES:
                        stop = f"{free_name} called past its cap — gave up"
                        log.append(f"BUDGET: {stop}")
                        break
                    messages.append(
                        Message(role="user", content=f"Already called {free_name}. Use a tool.")
                    )
                    continue
                free_calls += 1
                text = free[free_name](str(step[free_name]))
                log.append(f"FREE TOOL {free_name}: {len(text)} chars")
                messages.append(
                    Message(
                        role="user",
                        content=f"{free_name} says:\n{text}\n\n"
                        f"{self._left(started, calls, tokens)}\n{self._protocol_line()}",
                    )
                )
                continue

            name = next((n for n in tools if step.get(n)), None)
            if name is None:
                protocol_failures += 1
                messages.append(Message(role="assistant", content=result.text))
                messages.append(Message(role="user", content=self._nudge()))
                continue

            calls += 1
            answer = tools[name](str(step[name]))
            observations.append(answer)
            spent += answer.ai_cost_usd
            tokens += answer.tokens
            obs = _obs(answer)
            log.append(f"TOOL {name} [step {calls}]: {obs[:_LOG_CHARS]}")
            messages.append(Message(role="assistant", content=result.text))
            # Experiment A: the caller pushes back, in-band, escalating. Later
            # text wins in a prompt, so a memo agent tends to comply; a governed
            # definition is out-of-band from the caller's message.
            push = question.pressure[calls - 1] if calls <= len(question.pressure) else ""
            messages.append(
                Message(
                    role="user",
                    content=f"Result of step {calls}:\n{obs}\n\n"
                    + (f"{push}\n\n" if push else "")
                    + f"{self._left(started, calls, tokens)}\n{self._protocol_line()}",
                )
            )

        ms = round((time.perf_counter() - started) * 1000, 1)
        chosen = _designated(observations, designated)
        base = dict(
            ai_cost_usd=spent,
            latency_ms=ms,
            tokens=tokens,
            trace_id=uuid.uuid4().hex,
            calls=calls,
            free_calls=free_calls,
            protocol_failures=protocol_failures,
            transcript="\n\n".join(log) or None,
        )
        if chosen is None:
            return LaneAnswer(
                columns=[],
                rows=[],
                sql=None,
                error=f"agent produced no answer ({stop or 'no tool calls'})",
                **base,  # type: ignore[arg-type]
            )
        return LaneAnswer(
            columns=chosen.columns,
            rows=chosen.rows,
            sql=chosen.sql,
            error=chosen.error,
            confidence=chosen.confidence,
            certification=chosen.certification,
            answer=chosen.answer,
            grounding=chosen.grounding,
            grounding_reason=chosen.grounding_reason,
            lens=chosen.lens,
            **base,  # type: ignore[arg-type]
        )

    def _exhausted(self, started: float, calls: int, tokens: int) -> str:
        b = self._budget
        if calls >= b.tool_calls:
            return f"{calls}/{b.tool_calls} tool calls spent"
        if time.perf_counter() - started >= b.seconds:
            return f"{b.seconds:.0f}s wall-clock spent"
        if tokens >= b.tokens:
            return f"{tokens}/{b.tokens} tokens spent"
        return ""

    def _left(self, started: float, calls: int, tokens: int) -> str:
        b = self._budget
        return (
            f"Budget left: {b.tool_calls - calls} tool calls, "
            f"{max(0.0, b.seconds - (time.perf_counter() - started)):.0f}s, "
            f"{max(0, b.tokens - tokens)} tokens."
        )


def _as_index(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _designated(observations: list[LaneAnswer], designated: int | None) -> LaneAnswer | None:
    """The step the agent named, else its last one. 1-based, out-of-range falls
    back to last — a bad index is a protocol slip, not a reason to grade nothing."""
    if not observations:
        return None
    if designated is not None and 1 <= designated <= len(observations):
        return observations[designated - 1]
    return observations[-1]


def _sql_system(
    schema: str, memo: str | None, *, remember: bool = False, dialect: str = "DuckDB"
) -> str:
    return (
        "You answer business questions by querying a warehouse step by step.\n"
        f"Schema (schema.table, columns, types):\n{schema}\n\n"
        + (
            "CONVENTIONS.md — the data team's file for this warehouse. It is "
            "authoritative about what things MEAN; the schema above is "
            f"authoritative about what EXISTS.\n\n{memo}\n\n"
            if memo
            else ""
        )
        + (
            'You may append to CONVENTIONS.md with {"remember": "<one convention>"} '
            "— free, no tool call — when you learn something the file should "
            "have said.\n"
            if remember
            else ""
        )
        + f"{_SHAPE_CONTRACT}\n"
        f'Each turn reply with ONLY JSON: {{"sql": "<one read-only {dialect} SELECT>"}} '
        f"to run a query. {_PROTOCOL}"
    )


class AgenticBaselineLane:
    """The same agent, raw warehouse in hand and no context layer: the control."""

    def __init__(
        self,
        llm: LLMProvider,
        warehouse: Path | Warehouse,
        schema: str,
        *,
        model: str,
        budget: Budget | None = None,
        principals: dict[str, str] | None = None,
        name: str = "agent-baseline",
    ) -> None:
        self.name = name
        wh = coerce(warehouse)
        self._sql = wh.execute
        self._loop = _AgentLoop(
            llm,
            model=model,
            budget=budget or Budget(),
            system=_sql_system(schema, None, dialect=wh.dialect_word),
            tool_names=["sql"],
            principals=principals,
        )

    def answer(self, question: Question) -> LaneAnswer:
        return self._loop.run(question, {"sql": self._sql})


class Memo:
    """One session's copy of CONVENTIONS.md.

    ``mutable`` is the MEMO+ variant: the agent may append to its own file after
    a correction, which is what agent memory does today. Sessions each hold
    their own ``Memo``, which is how MEMO×M diverges — and that divergence is
    the thing ``drift.py`` counts.
    """

    def __init__(self, text: str, *, mutable: bool = False, cap: int = MEMO_BYTES) -> None:
        self.mutable = mutable
        self._cap = cap
        self._text = text[:cap]
        self._notes: list[str] = []
        # Reentrant: append() measures the rendered file, which re-enters read().
        self._lock = RLock()

    def read(self) -> str:
        with self._lock:
            if not self._notes:
                return self._text
            return (
                self._text
                + "\n\n## Learned in earlier sessions\n"
                + "\n".join(f"- {n}" for n in self._notes)
            )

    def append(self, note: str) -> str:
        """Append a learned convention. The cap is real: a memo is a file
        someone maintains, not an unbounded log."""
        if not self.mutable:
            return "This file is read-only."
        with self._lock:
            if len(self.read()) + len(note) + 4 > self._cap:
                return "The memo is full — nothing appended (a file has a size)."
            self._notes.append(note.strip().replace("\n", " ")[:400])
            return f"Appended. The memo now carries {len(self._notes)} learned note(s)."

    def notes(self) -> list[str]:
        with self._lock:
            return list(self._notes)


class AgenticMemoLane:
    """Raw SQL + MEMO: "just put the conventions in a markdown file" as a
    measurable arm.

    Same tool and same budget as the uninformed control; the difference is a
    4 KB prose file rendered from the lens itself, so this arm and the lens arm
    hold the same conventions in different form. MEMO is the *control that makes
    the comparison interpretable* — an arm measured only against an uninformed
    baseline is measuring the presence of a file.

    ``memos`` of length M is the MEMO×M variant: M independent sessions, each
    maintaining its own copy, assigned round-robin across question runs.
    """

    def __init__(
        self,
        llm: LLMProvider,
        warehouse: Path | Warehouse,
        schema: str,
        memos: list[Memo],
        *,
        model: str,
        budget: Budget | None = None,
        principals: dict[str, str] | None = None,
        name: str = "agent-memo",
    ) -> None:
        if not memos:
            raise ValueError("a memo arm needs at least one Memo")
        self.name = name
        self.memos = memos
        wh = coerce(warehouse)
        self._sql = wh.execute
        self._dialect = wh.dialect_word
        self._next = 0
        self._lock = Lock()
        self._mutable = memos[0].mutable
        self._schema = schema
        self._loop = _AgentLoop(
            llm,
            model=model,
            budget=budget or Budget(),
            system=_sql_system(
                schema, memos[0].read(), remember=self._mutable, dialect=self._dialect
            ),
            tool_names=["sql"],
            free_names=["remember"] if self._mutable else [],
            principals=principals,
        )

    def _session(self) -> Memo:
        with self._lock:
            memo = self.memos[self._next % len(self.memos)]
            self._next += 1
            return memo

    def answer(self, question: Question) -> LaneAnswer:
        memo = self._session()
        # Rebuilt per run: a mutable memo has moved since the lane was built.
        system = _sql_system(
            self._schema, memo.read(), remember=self._mutable, dialect=self._dialect
        )
        free: dict[str, Callable[[str], str]] = {"remember": memo.append} if self._mutable else {}
        return self._loop.run(question, {"sql": self._sql}, free, system=system)


class AgenticDstLane:
    """The same agent, same budget — its tools are a governed lens.

    ``describe`` is free of the call budget (zero LLM calls, zero warehouse
    hits) because the SQL arms get their schema and conventions free in the
    prompt; charging dst a call for the same class of information would be
    the unfairness pointing the other way.
    """

    def __init__(
        self,
        inner: DstLane,
        llm: LLMProvider,
        *,
        model: str,
        budget: Budget | None = None,
        describe_text: str | None = None,
        principals: dict[str, str] | None = None,
        name: str = "agent-dst",
    ) -> None:
        self.name = name
        self._inner = inner
        # Certified SQL bodies are omitted: this arm cannot run SQL, so they
        # would only burn its tokens. See conventions.py on that asymmetry.
        self._describe = describe_text or memo_file(inner, certified_sql=False)
        self._loop = _AgentLoop(
            llm,
            model=model,
            budget=budget or Budget(),
            system=(
                "You answer business questions by asking a governed data lens. The "
                "lens knows the warehouse, the company's definitions, and refuses "
                "what it cannot know — phrase asks as full business questions, not "
                "SQL. Its answers carry a certification and a confidence label; a "
                "certified answer was approved by a human and is the trust ceiling.\n"
                f"{_SHAPE_CONTRACT}\n"
                'Each turn reply with ONLY JSON: {"describe": true} to read what the '
                "lens knows — its definitions, standing orders and certified "
                "questions — which costs you no tool calls, or "
                '{"ask": "<one question for the lens>"} to spend one. '
                f"{_PROTOCOL}"
            ),
            tool_names=["ask"],
            free_names=["describe"],
            principals=principals,
        )

    def answer(self, question: Question) -> LaneAnswer:
        def ask(text: str) -> LaneAnswer:
            # The caller identity is the ASKER's, not the sub-question's —
            # runtime context follows the principal through the loop, as in
            # production. Bound per run: the runner fans questions out over
            # threads, so nothing about the asker may live on the lane.
            return self._inner.answer(
                Question(
                    id=question.id,
                    category=question.category,
                    question=text,
                    oracle_path=question.oracle_path,
                    kind=question.kind,
                    tier=question.tier,
                    caller=question.caller,
                )
            )

        return self._loop.run(question, {"ask": ask}, {"describe": lambda _: self._describe})
