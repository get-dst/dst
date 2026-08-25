"""Thinking headroom for reasoning-mode models (the `LLMProvider` seam).

A reasoning-mode model spends its thinking against the SAME `max_tokens`
budget as the answer, and it spends it FIRST. A cap hand-tuned for a
non-reasoning model therefore returns an EMPTY completion on one:
`GroundedSQLGenerator` at max_tokens=1024 produces 0 characters of SQL on
deepseek-v4-flash where 8192 produces valid SQL. Every caller then degrades
silently through its own `or fallback` (the definition drafter pasted the
user's raw correction note in as a definition body).

The fix lives here, once, instead of in every call site's magic number: call
sites keep expressing how much ANSWER they want and the provider adds thinking
headroom on top when the model needs it.

**A cap is a CEILING, not a spend.** You are billed for tokens produced, not
tokens allowed, so raising it costs nothing when the output is short. That is
what lets the detection below lean permissive: a false positive buys an unused
ceiling, a false negative buys an empty answer.

Detection is config-first, heuristic-second:

- ``ProviderConfig.reasoning`` (true/false) is authoritative when set. It is
  the install's own knowledge of its own models, so nothing here has to rot —
  a new reasoning model is one line of config, not a release.
- Unset (the default) falls back to the name markers below: the models known
  to reason, plus the naming conventions vendors actually use. A floor, not a
  registry. Set ``reasoning: true`` on the provider entry when your model
  reasons and its name does not say so.

Non-reasoning models are untouched: nothing matches, no headroom is added, and
the request is byte-identical to what it was before this module existed.
"""

from __future__ import annotations

import logging

log = logging.getLogger("dst")

# Extra output budget a reasoning model gets on top of the requested cap.
#
# Reasoning tokens are drawn from the SAME output cap as the answer, and the
# thinking runs FIRST. Size the headroom below what the thinking wants and the
# cap is spent before a single content token is emitted: the provider returns
# finish_reason="length" with an empty string — a silent failure on exactly the
# multi-step questions reasoning was turned on for. A hard SQL question can want
# tens of thousands of tokens of thinking alone; 32768 leaves the answer its full
# requested budget on top of that.
#
# A cap is a CEILING, not a spend, so this costs nothing when output is short —
# but it is ADDITIVE, so it must stay clear of the provider's max-output limit.
# Largest ask in this codebase is 8192 → 40,960 on the wire, under both current
# ceilings (Anthropic 128k, 64k on Haiku 4.5). Revisit if a call site asks for
# substantially more.
THINKING_HEADROOM = 32768

_REASONING_MARKERS = (
    # DeepSeek's reasoning line (deepseek-v4-flash/pro).
    "deepseek-v4",
    "deepseek-reasoner",
    # R1 and its distills, whoever ships them.
    "-r1",
    # Anthropic models whose extended thinking is ON when the request omits the
    # `thinking` parameter — which is exactly what AnthropicProvider sends. The
    # 4.x family does NOT think unless asked, so it stays untouched.
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-fable-5",
    "claude-mythos-5",
    # Vendor-neutral naming conventions.
    "reasoner",
    "reasoning",
    "thinking",
)


def is_reasoning(model: str, configured: bool | None) -> bool:
    """Does *model* bill thinking against `max_tokens`? Config wins over names."""
    if configured is not None:
        return configured
    name = model.lower()
    return any(marker in name for marker in _REASONING_MARKERS)


def with_headroom(max_tokens: int, model: str, configured: bool | None) -> int:
    """The cap to actually put on the wire for *model*.

    Additive rather than a floor: the caller's number stays meaningful as "how
    much ANSWER I want", and the thinking budget is stacked on top of it.
    """
    if not is_reasoning(model, configured):
        return max_tokens
    return max_tokens + THINKING_HEADROOM


def completion_text(
    text: str | None,
    *,
    provider: str,
    model: str,
    max_tokens: int,
    finish_reason: str | None = None,
    thinking_chars: int = 0,
) -> str:
    """The completion's text — with an EMPTY reply made loud, and DIAGNOSED.

    An empty completion is never what a caller asked for, but nearly every call
    site absorbs one through its own `or fallback` and reports success. We log
    rather than raise: the fallbacks are legitimate degradation paths, and
    turning a soft degrade into a mid-request 502 is a bigger behaviour change
    than this bug warrants. The log line is what makes the degrade findable —
    it names the model and the cap that produced nothing. Pass the EFFECTIVE cap
    (headroom included): it is the number that was actually on the wire, so an
    operator reading the log is not told 400 when we really sent 8592.

    The old line advised "set `reasoning: true`, or raise the cap" for every
    empty reply, which is useless when the flag is already on — it sent an
    operator to a knob that was not the problem — a reasoning model can come
    back empty at an already-headroomed 16384. `finish_reason` and
    the size of the thinking block are what separate the two causes, so say
    which one happened instead of listing both.
    """
    if text:
        return text
    if finish_reason == "length":
        log.warning(
            "empty completion from %s (model=%s): the cap of %s tokens was fully "
            "consumed before any answer — %s chars of thinking, none of it content. "
            "Raise the caller's max_tokens or THINKING_HEADROOM; `reasoning` is "
            "already %s for this model. Callers will fall back silently.",
            provider,
            model,
            max_tokens,
            thinking_chars,
            "on" if thinking_chars else "off",
        )
    else:
        log.warning(
            "empty completion from %s (model=%s, max_tokens=%s, finish_reason=%s) — the "
            "model stopped without producing content and did NOT hit the cap, so this is "
            "not a budget problem. Callers will fall back silently.",
            provider,
            model,
            max_tokens,
            finish_reason,
        )
    return ""
