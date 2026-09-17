"""An ambiguous term's reading as a typed decision.

The deterministic rail (`ambiguity.resolve`) pins a reading when the question
literally names one of the declared meanings or an authored audience phrase.
Everything else used to clarify — including a question that names the
reading in words the lexicon does not carry ("gross invoiced revenue" against
a mapping labelled "net invoiced revenue"). Under typed serving that
residue is one measured decision over the definition's own possible_mappings:
act pins the reading exactly as a question-named one is pinned (disclosed,
on the ledger with its probability); anything below the bar clarifies as
before. The option set is the declaration — never a prose heuristic.
"""

from __future__ import annotations

from services.contracts.protocols import Decider, Option
from services.contracts.resolution import DecisionRecord
from services.contracts.semantic_model import Definition
from services.runtime import ambiguity, decision_policy

SLOT = "reading"


def decide_reading(
    decider: Decider,
    question: str,
    d: Definition,
    policies: dict[str, decision_policy.Policy] | None = None,
) -> tuple[ambiguity.Resolution | None, DecisionRecord]:
    """Which declared reading of ``d`` the question means, under the
    ``reading`` policy. Returns the pinnable resolution on act (None
    otherwise) and the record for the ledger either way."""
    options = [Option(ambiguity.mapping_label(m), m) for m in d.possible_mappings]
    context = (
        f"The question uses the governed term '{d.term}', which has more than one "
        "declared meaning. Which declared reading does the question mean? "
        "Pick none if the wording does not settle it."
    )
    dec = decider.decide(question=question, context=context, options=options, allow_none=True)
    verdict = decision_policy.verdict(dec, SLOT, policies)
    record = DecisionRecord(
        slot=SLOT,
        chosen=dec.chosen,
        p=dec.p,
        runner_up=dec.runner_up,
        margin=dec.margin,
        verdict=verdict,
        provider=dec.provider,
    )
    if verdict != "act" or dec.chosen is None:
        return None, record
    mapping = next(
        (m for m in d.possible_mappings if ambiguity.mapping_label(m) == dec.chosen), None
    )
    if mapping is None:  # a name outside the option set is a provider fault, not a reading
        return None, record.model_copy(update={"verdict": "clarify"})
    # The probability stays on the ledger record: a figure in the prose that
    # the result cannot ground reads as an invented number to the grounding
    # check, and the retry it triggers drops the disclosure with it.
    return (
        ambiguity.Resolution(
            term=d.term,
            mapping=mapping,
            via="typed decision over the declared readings",
            audience=None,
            others=[ambiguity.mapping_label(m) for m in d.possible_mappings if m != mapping],
        ),
        record,
    )
