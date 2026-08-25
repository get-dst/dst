"""The canonical lens config — DB-backed, with full JSON/YAML serialization.

The management API does CRUD + export/import against this; the wizard UI and any
GitOps/agent automation share it.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from services.contracts.authoring import Authored, inert
from services.contracts.semantic_model import StrList
from services.contracts.shared_semantic import SelectSpec


def _yaml11_off(v: object) -> object:
    """YAML 1.1 parses bare ``off`` as False, so ``answer_contract: off`` never
    reached the validator as a string and the error named the exact literal the
    user wrote. Coerce the boolean back to its spelling for
    string enums; the trap lands hardest on someone uncommenting the reference
    block, which prints the values unquoted."""
    if v is False:
        return "off"
    if v is True:
        return "on"
    return v


class ModelConfig(Authored):
    # A provider-registry entry name (BYOK: any configured provider, not a fixed
    # vendor list). Validated against the registry at publish time, not parse time —
    # a lens file must load even where its provider isn't configured.
    provider: str | None = Field(
        default=None,
        description="provider-registry entry name serving this lens's model "
        "(any configured provider; validated at publish, not parse). Unset = "
        "resolve the model name against every configured provider",
    )
    model: str | None = Field(
        default=None,
        description="the model answering for this lens; the cheap first pass uses "
        "the provider's fast sibling and escalates here. UNSET IS THE DEFAULT AND "
        "USUALLY RIGHT: the lens then follows this install's own smart tier, so it "
        "serves wherever it is deployed. Naming a model here pins it, and publish "
        "refuses the lens if no configured provider serves it",
    )
    # NOT inert any more, and deliberately not marked so: a lens asking for
    # temperature 0.0 used to be a silent no-op. Implementing beats warning;
    # the honest-inventory verdict for this one field is superseded.
    temperature: float | None = Field(
        default=None,
        description="explicit sampling temperature for generation; unset (the default) "
        "follows answer_mode (strict 0.0 / balanced 0.2 / exploratory 0.5)",
    )
    # Two caps, deliberately separate (cold-start blocker 4): the payload cap is
    # what the CALLER gets back, the compose cap is only the composer's prompt
    # budget. One number for both made a 271-row deliverable return 200 rows so
    # that the prose stayed cheap — a row-per-entity answer is not a summary.
    max_rows_to_return: int = Field(
        default=1000,
        ge=1,
        description="row cap on the returned data payload (above it the response "
        "carries data.truncated + data.row_count); bounded by the 5000-row fetch cap",
    )
    max_rows_to_compose: int = Field(
        default=200,
        ge=1,
        description="row cap fed to answer composition (the LLM's prompt budget) — "
        "the prose says so when it summarizes only part of the result",
    )
    # Execution-guided self-repair budget: how many times a guard rejection or engine
    # error is fed back to the generator (the retry escalates to the smart model).
    # One retry catches the mistakes the engine names verbatim (a self-referencing
    # CTE written without WITH RECURSIVE, an unknown column); it does not catch a
    # generator that keeps making the same one. Default stays 1; a lens that would
    # rather pay latency than lose the answer raises it.
    max_repairs: int = Field(
        default=1,
        ge=0,
        le=3,
        description="repair attempts after a guard rejection or execution error "
        "(0 = never retry; each attempt costs one more generation round-trip)",
    )
    inline_judge: bool = Field(
        default=False,
        exclude=True,
        json_schema_extra=inert(
            "retired — `answer_mode: strict` is the one knob and switches the "
            "inline judge on; delete this key"
        ),
    )
    adversarial_review: bool = Field(
        default=False,
        exclude=True,
        json_schema_extra=inert(
            "retired — `answer_mode: strict` is the one knob and switches the "
            "adversarial reviewer on; delete this key"
        ),
    )
    # Certified-definition directory for this lens. When set, the per-question
    # certified (definitions + SQL exemplars) is injected as a context source: the
    # single biggest accuracy lever a lens has on a warehouse whose schema is too
    # big to hand the model whole. None = no certified.
    certified_dir: str | None = Field(
        default=None,
        description="server-local directory of certified-definition pages injected "
        "as context — the biggest accuracy lever on a large warehouse",
    )
    # How loosely the lens is allowed to answer — the one knob a lens owner sets instead
    # of raw temperature (steering prose lives in `instructions`). "strict" answers only
    # when grounded and declines otherwise; "exploratory" is more willing to attempt and
    # surface partial answers; "balanced" is the default. Drives generation temperature
    # and how readily the verification grade downgrades / the lens declines.
    answer_mode: Literal["strict", "balanced", "exploratory"] = Field(
        default="balanced",
        description="how loosely the lens answers: strict declines when ungrounded; "
        "exploratory attempts partial answers; drives temperature + verification",
    )
    # A whole class of wrong answers is a CORRECT value in the WRONG grid — year
    # groupings rendered as dates, working columns left in the final SELECT — and
    # per-project convention prose is a weak defense against it. The answer
    # contract (services/runtime/assembly.py) pins the output grid in every
    # generation prompt as a product default; prompt-side only, never a guard.
    answer_contract: Literal["strict", "off"] = Field(
        default="strict",
        description="the output-grid contract in every generation prompt: project "
        "exactly the asked quantities at the asked grain, full precision — "
        "'off' removes the block",
    )

    _coerce_off = field_validator("answer_contract", mode="before")(_yaml11_off)

    def generation_temperature(self) -> float:
        """An explicitly set `temperature` wins; otherwise the answer mode's.

        This used to return the mode's temperature unconditionally, which made
        `temperature: 0.0` in a lens file a silent no-op: a lens pinned at 0.0 kept
        generating at the mode's 0.2 with nothing saying so, and the same question
        asked twice could answer once and decline once. `answer_mode: strict` was not
        the workaround: it also switches on the inline judge and the adversarial
        reviewer (services/api/query.py), so there was no way to pin sampling alone.
        A knob the schema documents and the dashboard renders must not be quietly
        ignored.

        `balanced` generates at 0.0 too: SQL generation is a computation, not prose —
        sampled generation lets one question return two different totals and two
        response SHAPES across runs, and `dst test` diffs regenerated SQL, so it
        makes the eval gate itself flaky. Determinism is the default; `exploratory`
        (or an explicit `temperature`) is the stated opt-out.
        """
        if self.temperature is not None:
            return self.temperature
        return {"strict": 0.0, "balanced": 0.0, "exploratory": 0.5}[self.answer_mode]

    def model_ref(self) -> str:
        """The provider-registry ref for this lens's model.

        Three shapes, in order: no model at all → "" , which the registry reads
        as "this install's own tier" (``registry.default_ref``) — contracts stays
        a leaf package and resolution stays in the one module that owns it; a
        bare model name (or the historical "anthropic" provider, which pre-BYOK
        bundles carry) → resolved against the configured providers' catalogs; an
        explicit provider → its registry entry, pinned.
        """
        if self.model is None:
            return ""
        if self.provider is None or self.provider == "anthropic":
            return self.model
        return f"{self.provider}/{self.model}"


class AccessRule(Authored):
    # Match a specific caller identity by name, OR any caller in a group/role.
    caller: str | None = Field(default=None, description="grant one caller by name")
    group: str | None = Field(
        default=None, description="or a group — 'everyone' = any valid key in the org"
    )
    # An allow entry is the whole grant: matched callers reach every column the
    # lens exposes. Narrowing WHAT a caller sees is done by giving them a lens
    # whose entities expose fewer columns, not by a per-column control here.


class AccessConfig(Authored):
    allow: list[AccessRule] = Field(
        default_factory=list,
        description="deny-by-default: a caller queries this lens only with a matching entry",
    )


class LoggingConfig(Authored):
    log_samples: bool = Field(
        default=False,
        description="store the first rows of each answer on its request_log row "
        "(sample) — off by default, because the sample is the answer's real "
        "values and the log outlives the request",
    )


class RateLimitConfig(Authored):
    per_caller_rpm: int = 60


class NotComputable(Authored):
    """A measure this lens must REFUSE, not approximate: when the
    warehouse or the selection genuinely lacks a measure, the model otherwise
    finds the nearest available column and answers confidently about the wrong
    thing — and no prose can prevent it (advisory, measured non-deterministic).
    This is the enforced form: a deterministic refusal keyed to the measure's
    name and aliases, with an optional pointer to where the measure DOES live."""

    measure: str = Field(
        description="the measure this lens cannot compute, as users say it, e.g. 'lifetime value'"
    )
    aliases: StrList = Field(
        default_factory=list,
        description="other phrasings that mean this measure ('LTV', 'customer "
        "lifetime value') — the refusal triggers on any of them",
    )
    route_to: str = Field(
        default="",
        description="optional: the lens that CAN compute it — the refusal names it "
        "so the caller's next move is one hop",
    )
    reason: str = Field(
        default="",
        description="optional: why it cannot be computed here (rides the refusal)",
    )


class LensConfig(Authored):
    name: str = Field(
        description="slug id for this lens, unique in the org and immutable after "
        "create — it is what `dst query <lens>` and the API path take. "
        "Conventionally the directory name under lenses/, e.g. sales"
    )
    # Defaults to `name` (the batch-3 paper cut: an author who typed the lens name
    # got `display_name: Field required` for a label they never chose). Set it only
    # when the human-facing label differs from the slug, e.g. name: sales_ops ->
    # display_name: "Sales Operations". The `mode="after"` fill runs on both plan
    # sides through _canonical_lens_yaml, so an omitted display_name never phantom-diffs.
    display_name: str = Field(
        default="",
        description="human label shown in the dashboard and listings; "
        "defaults to `name` when omitted",
    )
    description: str = ""
    # A denied caller's next move is "who do I ask?", and a 403 that names only the
    # caller and the lens leaves them to find the admin by asking around. Who owns
    # a lens is a declared fact the caller cannot infer from the refusal.
    owner: str = Field(
        default="",
        description="who runs this lens (caller name or contact) — appended to "
        "403 denials so a refused caller knows who grants access. Empty = unstated",
    )
    # The lens's business clock. "Yesterday", "this month", day boundaries and
    # date_trunc all depend on WHOSE day it is — a warehouse holding UTC events
    # under an Oslo sales team answers "how many signups yesterday" differently
    # depending on the anchor, by up to the timezone offset's worth of traffic.
    # The same undeclared clock is what serves a "sharp drop" that is really a
    # complete month compared against a partial one; declaring the clock is the
    # first half of fixing that class. Empty = undeclared (generation says nothing).
    timezone: str = Field(
        default="",
        description="IANA zone for this lens's business days, e.g. Europe/Oslo. "
        "Steers how generation reads 'today'/'yesterday'/month boundaries and is "
        "stated to the caller. Empty = undeclared",
    )
    # The lens's freshness contract — the second declared clock fact. The scope's
    # measured last-update (data_as_of) already rides every answer; what nobody
    # downstream can infer is how old is TOO old for this domain (a finance close
    # tolerates a week, an ops board a day). Declaring it turns "quietly served
    # month-old data" into a deterministic serve-time check: data_as_of older
    # than this many days fails `freshness`, caps the grade at partial, and the
    # answer says so out loud. None = undeclared (the check reports skip).
    stale_after_days: int | None = Field(
        default=None,
        description="days after the scope's measured last-update at which answers "
        "flag stale (freshness check fails, grade caps at partial). "
        "Omitted = no freshness contract declared",
    )
    connections: StrList = Field(
        default_factory=list,
        description="REQUIRED: the dst.yaml connection name(s) this lens reads. "
        "Empty is the default and answers nothing — plan refuses the lens with "
        "'names no connection'. Usually exactly one, e.g. [warehouse]",
    )
    # Retired 2026-08-18: server-side skill packs are gone — steering prose lives in
    # `instructions`, terms in semantic/definitions/, question→SQL pairs in the
    # certified store. Same tombstone contract as `context` below: old files parse,
    # no render emits the key, it dies by attrition.
    skills: StrList = Field(
        default_factory=list,
        exclude=True,
        json_schema_extra=inert(
            "skill packs are retired — delete this key; put steering prose in "
            "`instructions`, shared terms in semantic/definitions/, and "
            "question→SQL pairs in certified answers"
        ),
    )
    select: SelectSpec = Field(
        default_factory=SelectSpec,
        description="what this lens pulls from the shared semantic layer (semantic/)",
    )
    not_computable: list[NotComputable] = Field(
        default_factory=list,
        description="measures this lens must REFUSE rather than approximate — "
        "deterministic (a refusal, enforced in code, holds across phrasings that "
        "name the measure or an alias); each entry may route_to the lens that "
        "can compute it",
    )
    # Retired 2026-08-09: context ingestion was never configured here — every
    # ingest is a POST to /mgmt/lenses/<lens>/context/* whose body carries the
    # repo/paths/scopes. The block only ever existed because the skeleton emitted
    # it. Tombstone: old files still parse (apply warns instead of rejecting real
    # trees), `exclude=True` keeps it out of every render, so it dies by attrition.
    context: dict[str, Any] | None = Field(
        default=None,
        exclude=True,
        json_schema_extra=inert(
            "the context block is retired — delete it; context ingestion runs "
            "through POST /mgmt/lenses/<lens>/context/* and is never configured "
            "in lens.yaml"
        ),
    )
    model: ModelConfig = Field(default_factory=ModelConfig)
    instructions: str | None = Field(
        default=None,
        description="free-text steering for this lens's answers. Unvalidated: never "
        "restate a definition's logic here — a restated rule keeps steering answers "
        "after the governed definition changes, silently shadowing it. Name the "
        "term; let its definition carry the logic. NOTE: on a lens with metrics, "
        "generation's first pass is the metric-layer prompt, which does not render "
        "this — it steers only once that pass fails and generation escalates to raw "
        "SQL (measured: the first pass cannot act on it anyway). A ruling you need "
        "on EVERY answer belongs on the dimension, metric or definition it is about; "
        '`dst lens prompt <lens> "<question>"` shows exactly where it lands.',
    )
    access: AccessConfig = Field(default_factory=AccessConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)
    # Eval regression gate at publish: "off" = no gate; "warn" =
    # run + surface a regression but publish anyway; "block" = refuse publish on a
    # score regression or certified divergence. Blocking by default, the way a
    # dbt test blocks by default — safe on a fresh lens because an empty corpus
    # or an unservable model degrades to a LOUD skip, never a brick;
    # the cost of the default only starts once something is certified, which is
    # exactly when the guarantee must fire.
    eval_gate: Literal["off", "warn", "block"] = "block"
    # Auto-flag low-confidence served answers into the review queue, tagged
    # origin=ai — the queue watches the lens, not just its callers. Refusals,
    # clarifications, and certified answers are never flagged.
    auto_review: Literal["off", "unverified", "partial"] = Field(
        default="off",
        description="auto-flag served answers into the review queue by confidence — "
        "'unverified' flags only unverified answers, 'partial' flags partial AND "
        "unverified. Tickets land tagged origin=ai.",
    )

    _coerce_off = field_validator("eval_gate", "auto_review", mode="before")(_yaml11_off)
    # Product decision 3 (2026-08-03): an answer that would COMPOSE a metric the
    # shared layer defines but this lens's selection dropped — the
    # conversion rate rebuilt as a GROUP BY'd flag plus division in prose —
    # REFUSES by default, naming the metric and the path (select it in lens.yaml
    # or certify the answer). Refusing beats a low-confidence answer; this knob
    # opts one lens back into serving those compositions at confidence:
    # unverified, exactly as before the guard.
    serve_ungoverned_shapes: bool = Field(
        default=False,
        description="serve answers that compose a dropped shared-layer metric's "
        "shape from raw columns (at confidence: unverified) instead of refusing "
        "with the path",
    )

    @model_validator(mode="after")
    def _default_display_name(self) -> LensConfig:
        """An omitted (or blank) display_name IS the lens name — the slug is
        already a fine label, and requiring a second one was a documented paper
        cut. Idempotent: a filled value round-trips unchanged, so re-validating a
        stored bundle never rewrites it."""
        if not self.display_name:
            self.display_name = self.name
        return self
