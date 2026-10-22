"""Every place dst asks a model for something declares what it is for.

A provider call's usage record carries the purpose its call site declared
(``usage.purpose``); a call made with none declared is recorded as "other",
and the per-purpose spend observe reports would hide it there. This walks
every ``complete``, ``decide`` and ``decide_many`` call in services/ and
fails on one that is not inside a ``with ...purpose(...)`` block, unless it
is listed below with the reason it inherits its caller's purpose.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_CALLS = {"complete", "decide", "decide_many"}

#: Call sites that inherit the purpose of whoever called them.
INHERITS = {
    "services/llm/retry.py::RetryingLLM.complete": "the retry wrapper around every client",
    "services/llm/vote_decider.py::VoteDecider.decide": "a vote is a call of the decision asked",
    "services/llm/openai_decisions.py::OpenAIDecisionsDecider.decide": "one decision as a batch",
    "services/router/decider.py::LlmDecider.decide_measured": (
        "a subclass's own decide(), a stub with no provider"
    ),
}


def _sites() -> dict[str, bool]:
    """``path::qualname`` -> whether every listed call in it is declared."""
    out: dict[str, bool] = {}
    for path in sorted((ROOT / "services").rglob("*.py")):
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))

        def visit(node: ast.AST, scope: list[str], declared: bool, rel: str = rel) -> None:
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    visit(child, [*scope, child.name], declared)
                    continue
                inner = declared
                if isinstance(child, ast.With):
                    inner = declared or any(
                        isinstance(item.context_expr, ast.Call)
                        and isinstance(f := item.context_expr.func, ast.Attribute | ast.Name)
                        and (f.attr if isinstance(f, ast.Attribute) else f.id) == "purpose"
                        for item in child.items
                    )
                if (
                    isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Attribute | ast.Name)
                    and (
                        child.func.attr if isinstance(child.func, ast.Attribute) else child.func.id
                    )
                    in _CALLS
                ):
                    key = f"{rel}::{'.'.join(scope)}"
                    out[key] = out.get(key, True) and inner
                visit(child, scope, inner)

        visit(tree, [], False)
    return out


def test_every_model_call_declares_its_purpose() -> None:
    undeclared = sorted(
        site for site, ok in _sites().items() if not ok and _outer(site) not in INHERITS
    )
    assert not undeclared, (
        "these call a model without declaring what for (wrap the call in "
        "`with usage.purpose(...)`, or list it in INHERITS with the reason): "
        + ", ".join(undeclared)
    )


def _outer(site: str) -> str:
    """The site's method or module function, for a call inside a closure."""
    path, _, name = site.partition("::")
    parts = name.split(".")
    keep = 2 if parts[0][:1].isupper() else 1
    return f"{path}::{'.'.join(parts[:keep])}"


def test_every_inherited_site_still_exists() -> None:
    present = {_outer(s) for s in _sites()}
    gone = sorted(set(INHERITS) - present)
    assert not gone, f"INHERITS lists call sites that no longer call a model: {gone}"
