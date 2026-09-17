"""VoteDecider — a closed-set decision from a chat model, with an HONEST probability.

A chat model has no calibrated probability to give. Self-reported confidence
("answer and rate yourself 0-1") is not calibrated, not even monotone across
prompts — the fabricated-accuracy class. Token logprobs are real but exist only
on some wires and never after a reasoning model's hidden thinking. So the
probability here is the one thing a chat model CAN measure honestly: how often
it picks the same option when asked k times at temperature > 0. p = votes / k.
Coarse (k=5 resolves to 0.2), cheap on the fast tier, provider-agnostic, and a
claim about exactly this model on exactly this prompt — nothing more.

k = 1 is today's behaviour byte-for-byte: one call at temperature 0, no
probability (``probs=None``), the policy falls back to act-or-decline.

Starvation fails loud: a reply naming no option and no ``none`` is an outage
signal (a reasoning model out of output budget returns an empty string, not a
refusal), never a decline.
"""

from __future__ import annotations

import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

from services.contracts.protocols import (
    CacheableBlock,
    Decision,
    DecisionUsage,
    LLMProvider,
    Message,
    Option,
)

# Measured (scripts/decision_calibration.py, the routing calibration run of 2026-09-16:
# deepseek-v4-flash over the 52-question held-out routing set): k=5 had the
# lowest ECE (0.042, Brier 0.035) within 2pp of the best accuracy (96%); k=9
# was no better calibrated (ECE 0.053) at nearly double the cost. Re-run the
# script and update this line before changing it.
DEFAULT_K = 5
# The certified-equivalence slot was NOT in that run (it needs a live certified
# corpus — `dst test` grades it): k=1 keeps the legacy yes/no gate byte-for-byte
# until a run stamps it.
EQUIV_K = 1
_VOTE_TEMPERATURE = 0.7


class DeciderStarved(RuntimeError):
    """Every sampled reply was empty or unparseable — an outage, not a decline."""


def parse_choice(text: str, names: set[str]) -> str | None | bool:
    """An option name appearing in any form (customer_value / "customer value"
    / **bolded**), longest first so customer_value beats a stray "customer".
    Returns the name, None for an explicit ``none``, and False when the reply
    names nothing at all (starvation/garbage)."""
    low = text.strip().lower()
    for name in sorted(names, key=len, reverse=True):
        if name.lower() in low or name.lower().replace("_", " ") in low:
            return name
    if "none" in low:
        return None
    return False


class VoteDecider:
    def __init__(
        self,
        llm: LLMProvider,
        model: str,
        *,
        k: int = DEFAULT_K,
        temperature: float = _VOTE_TEMPERATURE,
        max_tokens: int = 512,
        concurrency: int | None = None,
    ) -> None:
        self._llm = llm
        self._model = model
        self._k = max(1, k)
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._concurrency = concurrency
        self.provider = f"vote:{model}:k={self._k}"

    def _prompt(
        self, question: str, context: str, options: list[Option], allow_none: bool
    ) -> tuple[str, str]:
        tail = " or the word none" if allow_none else ""
        system = (
            "You choose exactly ONE option from a closed list. Reply with ONLY the "
            f"option's name{tail} — no prose, no punctuation."
            + (
                " Reply none only when NO option fits; never stretch an unrelated option."
                if allow_none
                else ""
            )
        )
        lines = "\n".join(
            f"- {o.name}" + (f": {o.description}" if o.description else "") for o in options
        )
        user = (
            f"{context}\n\n" if context else ""
        ) + f"Options:\n{lines}\n\nQuestion: {question}\nAnswer (option name{tail}):"
        return system, user

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        if not options:
            if allow_none:
                return Decision(chosen=None, probs=None, provider=self.provider)
            raise ValueError("a decision needs at least one option")
        system, user = self._prompt(question, context, options, allow_none)
        names = {o.name for o in options}
        tokens: list[tuple[int, int]] = []
        started = time.perf_counter()

        def _one(_i: int) -> str | None | bool:
            out = self._llm.complete(
                system=[CacheableBlock(text=system)],
                messages=[Message(role="user", content=user)],
                model=self._model,
                temperature=0.0 if self._k == 1 else self._temperature,
                # deepseek reasons in output tokens; a tight cap truncates before
                # the visible answer lands (and silently declines everything).
                max_tokens=self._max_tokens,
            )
            tokens.append((int(out.input_tokens or 0), int(out.output_tokens or 0)))
            choice = parse_choice(out.text or "", names)
            if choice is None and not allow_none:
                return False  # "none" was not on offer — garbage, not a decline
            return choice

        def _usage() -> DecisionUsage:
            return DecisionUsage(
                calls=float(len(tokens)),
                input_tokens=float(sum(t[0] for t in tokens)),
                output_tokens=float(sum(t[1] for t in tokens)),
                latency_s=time.perf_counter() - started,
            )

        if self._k == 1:
            single = _one(0)
            if single is False:
                raise DeciderStarved(
                    "reply named no option and no 'none' — starvation, not a decline"
                )
            assert single is None or isinstance(single, str)
            return Decision(chosen=single, probs=None, provider=self.provider, usage=_usage())

        workers = self._concurrency or self._k
        with ThreadPoolExecutor(max_workers=min(workers, self._k)) as pool:
            replies = list(pool.map(_one, range(self._k)))
        valid = [r for r in replies if r is not False]
        if not valid:
            raise DeciderStarved(
                f"all {self._k} sampled replies named no option and no 'none' — "
                "starvation, not a decline"
            )
        votes = Counter("none" if r is None else str(r) for r in valid)
        # Every option (and none, when allowed) gets a probability, unvoted = 0.
        keys = [o.name for o in options] + (["none"] if allow_none else [])
        probs = {key: votes.get(key, 0) / len(valid) for key in keys}
        # Deterministic tie-break: option order — the same votes give the same choice.
        best = max(keys, key=lambda key: (probs[key], -keys.index(key)))
        return Decision(
            chosen=None if best == "none" else best,
            probs=probs,
            provider=self.provider,
            usage=_usage(),
        )
