"""Question sets: YAML in, oracle-bound questions out.

Each question binds to an oracle fact by path, e.g.
``oracle: [overdue_by_customer, Vaskilahden kaupunki]`` — a list, not a dotted
string, because business names contain anything. ``kind`` selects the grading
mode (see ``grading.py``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

QuestionKind = Literal["scalar", "ratio", "count", "top", "absent"]


Tier = Literal["calibration", "discriminating", "pressure"]


@dataclass(frozen=True)
class Question:
    id: str
    category: str
    question: str
    oracle_path: list[str]
    kind: QuestionKind
    lang: str = "en"
    # calibration: every lane should ace it (catches harness bugs).
    # discriminating: hits a planted defect / definitional gap.
    # pressure: the caller argues against the convention (see pressure.py) —
    # the enforcement tier.
    tier: Tier = "calibration"
    # WHO is asking. Deliberately NOT in the question text: only lanes with
    # caller plumbing (the runtime-context rung) can see it — like production.
    caller: str | None = None
    # Experiment A: escalating in-band caller pressure, one message injected
    # after each tool call. Empty for every ordinary question.
    pressure: tuple[str, ...] = ()
    # The lens that SHOULD answer this question. Graded only against
    # lanes that actually route (LaneAnswer.lens); on lens-pinned lanes the
    # routing stage records `skipped`. None = unlabeled, routing never grades.
    expected_lens: str | None = None


def load_questions(path: Path, oracle: dict[str, object] | None = None) -> list[Question]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    questions = [
        Question(
            id=item["id"],
            category=item["category"],
            question=_bind(item["question"], oracle),
            oracle_path=[_bind(str(p), oracle) for p in item["oracle"]],
            kind=item["kind"],
            lang=item.get("lang", "en"),
            tier=item.get("tier", "calibration"),
            caller=item.get("caller"),
            pressure=tuple(item.get("pressure", ())),
            expected_lens=item.get("expected_lens"),
        )
        for item in raw["questions"]
    ]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate question ids in question set")
    return questions


# ``[[node#n]]`` → the nth key (sorted, 1-based) of that oracle map. Entity
# questions bind to whatever the generated world actually contains instead of
# hardcoding a name the generator is free to stop producing — that fixture
# drift kills a run the moment a name drops out of the pool.
_BINDING = re.compile(r"\[\[([\w.]+)#(\d+)\]\]")


def _bind(text: str, oracle: dict[str, object] | None) -> str:
    if "[[" not in text:
        return text
    if oracle is None:
        raise ValueError(f"question uses an oracle binding but no oracle was given: {text!r}")

    def _sub(m: re.Match[str]) -> str:
        node: object = oracle
        for part in m.group(1).split("."):
            if not isinstance(node, dict) or part not in node:
                raise ValueError(f"oracle binding {m.group(0)}: no node {m.group(1)!r}")
            node = node[part]
        if not isinstance(node, dict) or not node:
            raise ValueError(f"oracle binding {m.group(0)}: {m.group(1)!r} is not a non-empty map")
        keys = sorted(str(k) for k in node)
        return keys[(int(m.group(2)) - 1) % len(keys)]

    return _BINDING.sub(_sub, text)


def unresolved(questions: list[Question], oracle: dict[str, object]) -> list[str]:
    """Questions whose oracle binding is broken — checked BEFORE any lane runs,
    so a stale fixture fails the run in milliseconds, not mid-run after money
    is spent."""
    orphans: list[str] = []
    for q in questions:
        try:
            resolve_oracle(oracle, q.oracle_path)
        except KeyError:
            orphans.append(f"{q.id}: {' / '.join(q.oracle_path)}")
    return orphans


def load_callers(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    """``callers.yaml`` → (registry, principals).

    ``registry`` carries the full answering note ("the CFO (CFO). Finance
    definitions. …") — the lens's runtime-context rail. ``principals`` carries
    IDENTITY ONLY ("the CFO (CFO)") — what a production driver agent knows about
    who it serves; the conventions must reach it through its own arm's channel,
    or the arms stop differing in delivery mechanism.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    registry: dict[str, str] = {}
    principals: dict[str, str] = {}
    for c in raw["callers"]:
        identity = f"{c['name']} ({c['role']})"
        principals[c["id"]] = identity
        registry[c["id"]] = f"{identity}. {c['answer_with']}"
    return registry, principals


def resolve_oracle(oracle: dict[str, object], path: list[str]) -> object:
    """Walk the oracle dict by path; KeyError with the full path on a miss so a
    broken binding fails the run loudly instead of grading against None."""
    node: object = oracle
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f"oracle path not found: {' / '.join(path)}")
        node = node[key]
    return node
