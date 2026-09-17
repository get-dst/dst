"""TypesafeDecider — the typed-decision provider on the decision seam.

A typed-decision model (TypeSafe's Jev, "System One") answers closed-set
questions with a calibrated probability per option in tens of milliseconds.
dst's ``Decider`` protocol is that call's shape: question + context + options
→ chosen + probabilities. One decision is one ``choice`` question on
``POST /v1/systemone``:

    {"state": {"question": …}, "model": "jev-latest",
     "questions": {"d": {"type": "choice", "instructions": <context>,
                         "criteria": {<option>: <description or null>, …}}}}

and the answer's ``probabilities`` is the full distribution over the criteria
(sums to 1), so ``Decision.probs`` is exactly what the wire returned — nothing
is derived, sampled or self-reported. ``allow_none`` adds a ``none`` option
the docs recommend for inputs outside the set. Several questions may share
one call (``decide_many``); the resolver uses that where decisions are
independent.

Failures are loud: an HTTP error is a ``ProviderError`` (an outage a caller
surfaces), never a decision. 429/529 retry with backoff, as the API asks.

Configure with a provider of ``type: typesafe`` (``api_key_env``, optional
``base_url``); ``registry.resolve_decider()`` prefers it over the voting chat
decider whenever a key is present.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from services.contracts.errors import ProviderError
from services.contracts.protocols import Decision, DecisionUsage, Option

DEFAULT_BASE_URL = "https://api.typesafe.ai"
DEFAULT_MODEL = "jev-latest"
_NONE = "none"
_RETRY_STATUSES = {429, 529}


class TypesafeDecider:
    # A typed-decision provider decides over EVERY lens: the router hands it
    # the whole catalogue and skips the cosine shortlist and verbatim fast path
    # (measured equal on the held-out routing set, 2026-09-17 — so the simpler
    # path wins).
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
        self.provider = f"typesafe:{model}"

    # ── wire shape ───────────────────────────────────────────────────────────

    @staticmethod
    def question(context: str, options: list[Option], allow_none: bool) -> dict[str, Any]:
        criteria: dict[str, str | None] = {o.name: (o.description or None) for o in options}
        if allow_none:
            criteria[_NONE] = "none of the options fits"
        return {"type": "choice", "instructions": context, "criteria": criteria}

    def request(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> dict[str, Any]:
        """The wire request one decision carries."""
        return self.request_many(question, {"decision": (context, options, allow_none)})

    def request_many(
        self, question: str, questions: dict[str, tuple[str, list[Option], bool]]
    ) -> dict[str, Any]:
        return {
            "state": {"question": question},
            "model": self._model,
            "questions": {
                key: self.question(context, options, allow_none)
                for key, (context, options, allow_none) in questions.items()
            },
        }

    # ── the call ─────────────────────────────────────────────────────────────

    def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        last: str = ""
        client = self._http()
        for attempt in range(self._attempts):
            try:
                r = client.post("/v1/systemone", headers=headers, json=body)
            except httpx.HTTPError as exc:
                last = f"transport: {exc}"
                if attempt + 1 < self._attempts:
                    time.sleep(0.2 * (2**attempt))
                    continue
                raise ProviderError("typesafe", last) from exc
            if r.status_code in _RETRY_STATUSES and attempt + 1 < self._attempts:
                last = f"HTTP {r.status_code}"
                time.sleep(0.5 * (2**attempt))
                continue
            if r.status_code >= 400:
                detail = r.text[:300]
                raise ProviderError("typesafe", f"HTTP {r.status_code}: {detail}")
            data = r.json()
            if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
                raise ProviderError("typesafe", "malformed response: no answers map")
            return data
        raise ProviderError("typesafe", last or "no response")

    def _http(self) -> httpx.Client:
        """One connection pool per decider, shared across the lane's threads:
        a TLS handshake on every decision was pure overhead on a remote
        endpoint. Built on first use so a decider constructed but never
        asked opens nothing."""
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

    @staticmethod
    def _usage(data: dict[str, Any], latency_s: float, calls: float = 1.0) -> DecisionUsage:
        raw: dict[str, Any] = data["usage"] if isinstance(data.get("usage"), dict) else {}
        return DecisionUsage(
            calls=calls,
            input_tokens=float(raw.get("input_tokens") or 0),
            output_tokens=float(raw.get("output_tokens") or 0),
            latency_s=latency_s,
        )

    @staticmethod
    def _to_decision(answer: dict[str, Any], options: list[Option], provider: str) -> Decision:
        probs_raw = answer.get("probabilities")
        if not isinstance(probs_raw, dict):
            raise ProviderError("typesafe", "malformed answer: no probabilities")
        probs = {str(k): float(v) for k, v in probs_raw.items()}
        choice = answer.get("choice")
        if choice is None:
            choice = max(probs, key=lambda k: probs[k]) if probs else None
        chosen = None if choice == _NONE else str(choice)
        known = {o.name for o in options} | {_NONE}
        if chosen is not None and chosen not in known:
            raise ProviderError("typesafe", f"answer named an unknown option {chosen!r}")
        return Decision(chosen=chosen, probs=probs, provider=provider)

    def decide(
        self, *, question: str, context: str, options: list[Option], allow_none: bool
    ) -> Decision:
        if not options:
            if allow_none:
                return Decision(chosen=None, probs=None, provider=self.provider)
            raise ValueError("a decision needs at least one option")
        body = self.request(
            question=question, context=context, options=options, allow_none=allow_none
        )
        started = time.perf_counter()
        data = self._post(body)
        usage = self._usage(data, time.perf_counter() - started)
        answer = data["answers"].get("decision")
        if not isinstance(answer, dict):
            raise ProviderError("typesafe", "malformed response: missing the decision answer")
        d = self._to_decision(answer, options, self.provider)
        d.usage = usage
        return d

    def decide_many(
        self, question: str, questions: dict[str, tuple[str, list[Option], bool]]
    ) -> dict[str, Decision]:
        """Several independent decisions over one state in ONE call — the
        fan-out the API is built for (adding questions barely moves latency)."""
        if not questions:
            return {}
        started = time.perf_counter()
        data = self._post(self.request_many(question, questions))
        # One call, several decisions: each carries its share of the call.
        share = self._usage(data, time.perf_counter() - started).split(len(questions))
        out: dict[str, Decision] = {}
        for key, (_context, options, _allow_none) in questions.items():
            answer = data["answers"].get(key)
            if not isinstance(answer, dict):
                raise ProviderError("typesafe", f"malformed response: missing answer {key!r}")
            d = self._to_decision(answer, options, self.provider)
            d.usage = share
            out[key] = d
        return out
