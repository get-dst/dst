"""Generate a lens's "use this when…" routing examples — the natural-language asks a
caller would make that this lens answers.

Seeded by the assist LLM at lens creation from what the lens already declares (purpose,
entities, metrics, definitions). They are curated by the data team and fed to the router
as scoring anchors (services/router/profiles.py): the signal that disambiguates routing
is HOW people phrase a question, which the specific metric/definition anchors miss. The
provider is injected, so this stays offline/mock-testable; bad/empty output returns [].
"""

from __future__ import annotations

from services.contracts.protocols import LLMProvider
from services.contracts.semantic_model import SemanticModel
from services.llm.assist import complete_json

_SYSTEM = (
    "You write routing examples for a governed data lens: the natural-language questions a "
    "business user would ask that THIS lens answers, in the user's own voice (how they really "
    "ask — not formal metric names). From the lens's purpose, entities, metrics, and "
    "definitions, output 5-7 SHORT, DISTINCT questions that include BOTH:\n"
    "- specific asks: one per key metric the lens governs; AND\n"
    "- 1-2 UMBRELLA asks for the lens as a whole — how someone requests everything it covers "
    "by its purpose or use case (e.g. for a board-reporting lens, 'pull the latest numbers for "
    "the board meeting' or 'give me the board summary').\n"
    "Do NOT invent subjects the lens does not cover.\n"
    'Output ONLY a JSON object (no prose, no code fences): {"use_when": ["<question>", ...]}'
)

_MAX = 7


def _lens_brief(sm: SemanticModel, display_name: str, description: str) -> str:
    lines = [f"Lens: {display_name} — {description}".strip(" —")]
    if sm.definitions:
        lines.append("Definitions: " + "; ".join(f"{d.term}: {d.body}" for d in sm.definitions))
    metrics = list(dict.fromkeys(m.name for e in sm.entities for m in e.metrics))
    if metrics:
        lines.append("Metrics: " + ", ".join(metrics))
    if sm.entities:
        lines.append("Tables: " + ", ".join(e.name for e in sm.entities))
    return "\n".join(lines)


def generate_use_when(
    llm: LLMProvider, model: str, sm: SemanticModel, *, display_name: str, description: str
) -> list[str]:
    """4-6 'use this when…' example asks for the lens, deduped. Best-effort: returns []
    on a provider error or unusable output, never raises."""
    try:
        data = complete_json(
            llm, model, _SYSTEM, _lens_brief(sm, display_name, description), max_tokens=500
        )
    except Exception:  # noqa: BLE001 — bad/empty JSON or provider error: no examples
        return []
    items = data.get("use_when") or []
    out = [str(q).strip() for q in items if isinstance(q, str) and str(q).strip()]
    return list(dict.fromkeys(out))[:_MAX]
