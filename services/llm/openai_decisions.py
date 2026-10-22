"""OpenAIDecisionsDecider — OpenAI's Decisions API on the decision seam.

The Decisions API (``POST /v1/decisions``, model ``gpt-6-luna``) answers typed
questions about an input. One dst decision is one ``choice`` question:

    {"model": "gpt-6-luna", "input": <question>,
     "questions": [{"type": "choice", "name": "q0", "instructions": <context>,
                    "choices": [{"value": <option>, "description": …}, …]}]}

and the answer comes back by name with the chosen value and a probability per
value. ``Decision.probs`` is that list as returned, keyed by value: nothing is
derived, renormalised or recalibrated, and the policy reads it the way it reads
any typed-decision provider's (act on the choice unless it is ``none``; a
measured bar only discloses). ``allow_none`` adds a ``none`` choice for inputs
outside the set. Several decisions share one call (``decide_many``); the wire
names are positional (``q0``, ``q1``, …), so a caller's keys never have to fit
the API's naming rules.

A ``refusal`` answer is a decision of none with ``refused`` set and nothing
measured: the slot declines, it is never a silent pick. An HTTP error is a
``ProviderError`` (an outage a caller surfaces), never a decision; 429 and 5xx
retry with backoff first.

Usage is read from the response's ``usage`` (``input_tokens``,
``output_tokens``) and priced as a Decisions API call in
``services/observability/cost.py``: input only, output is not billed (over
chat completions the same model's output is).

Configure with a provider of ``type: openai-decisions`` (``api_key_env``,
default ``OPENAI_API_KEY``; optional ``base_url``; the model is the first of
``models``, default ``gpt-6-luna``).
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from services.contracts.errors import ProviderError
from services.contracts.protocols import Decision, DecisionUsage, Option, spend_on
from services.observability import cost
from services.observability import usage as ai_usage

log = logging.getLogger("dst")

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_KEY_ENV = "OPENAI_API_KEY"
_PROVIDER = "openai-decisions"
_NONE = "none"


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


def _record(provider: str, usage: DecisionUsage) -> None:
    """One billed call, for the request's usage records."""
    ai_usage.record(
        provider,
        cost.decision_model(provider),
        round(usage.input_tokens),
        round(usage.output_tokens),
    )


class OpenAIDecisionsDecider:
    # A typed-decision provider: the router decides over every lens with no
    # cosine shortlist, and DST_TYPED_SERVING=auto serves typed on it.
    typed_decisions = True

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = 30.0,
        attempts: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self._model = model
        self._timeout = timeout
        self._attempts = max(1, attempts)
        self._transport = transport
        self._warned_usage = False
        self.provider = f"{_PROVIDER}:{model}"

    # ── wire shape ───────────────────────────────────────────────────────────

    @staticmethod
    def question(
        name: str, context: str, options: list[Option], allow_none: bool
    ) -> dict[str, Any]:
        choices: list[dict[str, str]] = [
            {"value": o.name, "description": o.description} if o.description else {"value": o.name}
            for o in options
        ]
        if allow_none and all(o.name != _NONE for o in options):
            choices.append({"value": _NONE, "description": "none of the options fits"})
        return {"type": "choice", "name": name, "instructions": context, "choices": choices}

    def request_many(
        self, question: str, questions: dict[str, tuple[str, list[Option], bool]]
    ) -> dict[str, Any]:
        return {
            "model": self._model,
            "input": question,
            "questions": [
                self.question(f"q{i}", context, options, allow_none)
                for i, (context, options, allow_none) in enumerate(questions.values())
            ],
        }

    # ── the call ─────────────────────────────────────────────────────────────

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last = ""
        client = self._http()
        for attempt in range(self._attempts):
            try:
                r = client.post("/decisions", headers=headers, json=body)
            except httpx.HTTPError as exc:
                last = (
                    "timed out"
                    if isinstance(exc, httpx.TimeoutException)
                    else f"unreachable ({type(exc).__name__})"
                )
                if attempt + 1 < self._attempts:
                    time.sleep(0.2 * (2**attempt))
                    continue
                raise ProviderError(_PROVIDER, last) from exc
            if _retryable(r.status_code) and attempt + 1 < self._attempts:
                last = f"HTTP {r.status_code}"
                time.sleep(0.5 * (2**attempt))
                continue
            if r.status_code >= 400:
                raise ProviderError(_PROVIDER, r.text[:300], status=r.status_code)
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data.get("answers"), list):
                raise ProviderError(_PROVIDER, "malformed response: no answers list")
            return data
        raise ProviderError(_PROVIDER, last or "no response")

    def _http(self) -> httpx.Client:
        """One connection pool per decider, built on first use."""
        client = getattr(self, "_client", None)
        if client is None:
            client = httpx.Client(
                base_url=self._base_url,
                timeout=self._timeout,
                transport=self._transport,
                limits=httpx.Limits(max_connections=128, max_keepalive_connections=64),
            )
            self._client = client
        return client

    def _usage(self, data: dict[str, Any], latency_s: float) -> DecisionUsage:
        raw = data.get("usage")
        if not isinstance(raw, dict) and not self._warned_usage:
            # Without tokens the call is counted at $0: said once, not hidden.
            self._warned_usage = True
            log.warning(
                "%s: the response carried no usage; its decisions count 0 tokens", _PROVIDER
            )
        raw = raw if isinstance(raw, dict) else {}
        return DecisionUsage(
            calls=1.0,
            input_tokens=float(raw.get("input_tokens") or 0),
            output_tokens=float(raw.get("output_tokens") or 0),
            latency_s=latency_s,
        )

    def _to_decision(self, answer: dict[str, Any], options: list[Option]) -> Decision:
        kind = answer.get("type")
        if kind == "refusal":
            return Decision(chosen=None, probs=None, provider=self.provider, refused=True)
        if kind != "choice":
            raise ProviderError(_PROVIDER, f"malformed answer: type {kind!r} for a choice question")
        raw = answer.get("probabilities")
        if not isinstance(raw, list):
            raise ProviderError(_PROVIDER, "malformed answer: no probabilities")
        try:
            probs = {str(p["value"]): float(p["probability"]) for p in raw}
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError(_PROVIDER, "malformed answer: unreadable probabilities") from exc
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise ProviderError(_PROVIDER, "malformed answer: no choice")
        known = {o.name for o in options} | {_NONE}
        if choice not in known:
            raise ProviderError(_PROVIDER, f"answer named an unknown option {choice!r}")
        return Decision(
            chosen=None if choice == _NONE else choice, probs=probs, provider=self.provider
        )

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        if not options:
            if allow_none:
                return Decision(chosen=None, probs=None, provider=self.provider)
            raise ValueError("a decision needs at least one option")
        return self.decide_many(question, {"decision": (context, options, allow_none)})["decision"]

    def decide_many(
        self, question: str, questions: dict[str, tuple[str, list[Option], bool]]
    ) -> dict[str, Decision]:
        """Several independent decisions about one question in ONE call."""
        if not questions:
            return {}
        started = time.perf_counter()
        data = self._post(self.request_many(question, questions))
        # One call, several decisions: each carries its share of the call.
        usage = self._usage(data, time.perf_counter() - started)
        _record(self.provider, usage)
        shares = usage.split(len(questions))
        by_name = {a.get("name"): a for a in data["answers"] if isinstance(a, dict)}
        out: dict[str, Decision] = {}
        try:
            for i, (key, (_context, options, _allow_none)) in enumerate(questions.items()):
                share = shares[i]
                answer = by_name.get(f"q{i}")
                if answer is None:
                    raise ProviderError(_PROVIDER, f"malformed response: missing answer {key!r}")
                d = self._to_decision(answer, options)
                d.usage = share
                out[key] = d
        except ProviderError as exc:
            # The call was billed although its reply is unusable.
            spend_on(exc, self.provider, usage)
            raise
        return out
