"""The dst project file (dst.yaml) — the file-first workspace config.

A project directory is the OSS source of truth: dst.yaml holds providers,
pricing, connection declarations, and a public demo page's content;
semantic/ holds the shared layer (entities/*.yaml, definitions/*.md);
lenses/<name>/ holds each lens's file tree (lens.yaml, queries.yaml,
definitions/*.md, certified/*.md, certified_answers.yaml, evals/cases.yaml).
Secrets NEVER live in the project — providers use api_key_env, connections use
secret_env; inline secrets are a parse error, not a lint warning.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from services.config import ProviderConfig, check_ai_pricing
from services.contracts.authoring import Authored, parse_authored
from services.contracts.semantic_model import StrList
from services.project.loader import parse_yaml


class ConnectionDecl(Authored):
    """A declared warehouse/context connection — config only, secret by env ref."""

    model_config = ConfigDict(extra="forbid")

    type: str = Field(
        description="connection type: duckdb | postgres | mysql | bigquery | snowflake"
    )
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="non-secret connector settings (host, project, path, …); "
        "`schema:` scopes introspection to one schema — omitted, it spans every "
        "non-system schema",
    )
    secret_env: str | None = Field(
        default=None,
        description="process-env var holding the credential (convention: "
        "DST_API_KEY_<NAME>); a secret value itself can never appear here",
    )


_TURN_KINDS = ("user", "tool", "assistant", "receipt")


class DemoTurn(Authored):
    """One turn of the demo page's example exchange: exactly one of the four keys."""

    model_config = ConfigDict(coerce_numbers_to_str=True)

    user: str | None = Field(default=None, description="what the person asks their AI")
    tool: str | None = Field(
        default=None, description="the tool-call line, e.g. `asked <instance> — <question>`"
    )
    assistant: str | None = Field(default=None, description="the AI's reply")
    receipt: dict[str, str] | None = Field(
        default=None, description="receipt lines under a reply, `label: value`, in order"
    )

    @model_validator(mode="after")
    def _one_kind(self) -> DemoTurn:
        given = [k for k in _TURN_KINDS if getattr(self, k) is not None]
        if len(given) != 1:
            raise ValueError(
                "each turn is exactly one of "
                + ", ".join(_TURN_KINDS)
                + (f" — this one has {', '.join(given)}" if given else " — this one is empty")
            )
        value = getattr(self, given[0])
        if isinstance(value, dict):
            empty = not value or not all(str(k).strip() and v.strip() for k, v in value.items())
        else:
            empty = not value.strip()
        if empty:
            raise ValueError(f"an empty `{given[0]}` turn")
        return self


class DemoConfig(Authored):
    """What a public demo's page (``/demo``) shows besides what dst derives itself.

    Read only by a deployment in demo mode whose DST_DEMO_ORG_ID is the org this
    project applies to; every key is optional and an absent one renders nothing."""

    tagline: str | None = Field(
        default=None,
        description="the one sentence under the instance name, in the visitor's words: "
        "what they can ask about, from which AI",
    )
    example: list[DemoTurn] = Field(
        default_factory=list,
        description="an example conversation, folded under the connect block: a list of turns",
    )
    privacy_url: str | None = Field(
        default=None, description="the operator's privacy notice, linked in the fine print"
    )
    log_days: int | None = Field(
        default=None,
        ge=1,
        description="how long questions stay in the log, as the operator prunes it "
        "(`dst prune-log --keep-days`); stated in the fine print",
    )
    audience: Literal["engineer", "consumer"] = Field(
        default="engineer",
        description="who the demo serves: `engineer` answers carry their SQL, rows, "
        "citations and checks; `consumer` answers to demo callers carry the prose, "
        "its scope and freshness, and a refusal's reason, the rest stays in the log",
    )

    @field_validator("privacy_url")
    @classmethod
    def _http_url(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("https://", "http://")):
            raise ValueError(f"privacy_url must be an http(s) URL, not {v!r}")
        return v


_DURATION = re.compile(r"^(\d+)([dhm])?$")
_UNIT_SECONDS = {"d": 86_400, "h": 3_600, "m": 60}


def parse_duration(text: str) -> timedelta:
    """``7d`` / ``48h`` / ``30m`` / ``0`` → timedelta. Strict: a bare number other
    than zero needs a unit, so ``max_age: 7`` cannot silently mean seven of
    anything."""
    match = _DURATION.match(str(text).strip())
    if match is None:
        raise ValueError(f"not a duration: {text!r} (use <n>d, <n>h, <n>m, or 0)")
    n, unit = int(match.group(1)), match.group(2)
    if unit is None and n != 0:
        raise ValueError(f"duration {text!r} needs a unit: {n}d, {n}h or {n}m")
    return timedelta(seconds=n * _UNIT_SECONDS.get(unit or "d", 86_400))


class DriftTolerance(Authored):
    """How much a column's measured distribution may move before `dst drift`
    calls it a change. A column's comment or type changing is never within
    tolerance; these bound the deltas the probe measures."""

    model_config = ConfigDict(extra="forbid")

    time_shift: str = Field(
        default="1h",
        description="how far a time column's median offset from the entity's "
        "default_time_field may move (`1h`, `30m`, `1d`; `0` reports any move)",
    )
    numeric_shift: float = Field(
        default=0.2,
        ge=0,
        description="how far a numeric column's mean may move, relative to the baseline "
        "(0.2 = twenty percent; 0 reports any move)",
    )

    @field_validator("time_shift", mode="before")
    @classmethod
    def _valid_duration(cls, v: object) -> str:
        text = str(v).strip()
        parse_duration(text)
        return text

    @property
    def time_shift_seconds(self) -> float:
        return parse_duration(self.time_shift).total_seconds()


class CubeProbeConfig(Authored):
    """What `dst probe --cube` and `dst monitor` may spend on Cube per run."""

    model_config = ConfigDict(extra="forbid")

    max_queries: int = Field(
        default=500,
        ge=1,
        description="the most /v1/load queries one run makes; what the budget leaves "
        "unread is listed in the artifact and said, never dropped silently",
    )
    max_values: int = Field(
        default=500,
        ge=1,
        description="the most values read for one dimension's dictionary; a dimension "
        "holding more is recorded incomplete",
    )


class ProbeConfig(Authored):
    """How `dst probe` samples and how old its artifact may get before serving
    says so. The user owns the probe clock (a scheduled job, a deploy step);
    these two knobs are how dst notices."""

    model_config = ConfigDict(extra="forbid")

    max_age: str = Field(
        default="7d",
        description="how old the newest profile may be before every surface says so "
        "(`7d`, `48h`; `0` turns the age rails off)",
    )
    recent_partitions: int = Field(
        default=7,
        ge=0,
        description="how many latest partitions (or days of the entity's default_time_field) "
        "the sampling pass reads on a refresh; 0 reads whole tables",
    )
    cube: CubeProbeConfig = Field(
        default_factory=lambda: CubeProbeConfig(),
        description="the query budget of the probe and monitor that read through Cube",
    )
    drift_tolerance: DriftTolerance = Field(
        default_factory=lambda: DriftTolerance(),
        description="how far a column's measured distribution may move between the drift "
        "baseline and the newest probe before `dst drift` reports it as breaking",
    )

    @field_validator("max_age", mode="before")
    @classmethod
    def _valid_duration(cls, v: object) -> str:
        text = str(v).strip()  # YAML reads a bare `0` as an int
        parse_duration(text)
        return text

    @property
    def max_age_delta(self) -> timedelta | None:
        """The age limit, or None when the rails are off (``max_age: 0``)."""
        delta = parse_duration(self.max_age)
        return delta if delta else None


class LineageDecl(Authored):
    """Where the transformation tool's model DAG lives, for `table/X+`
    selection: one of a dbt target directory or a SQLMesh project directory,
    relative to the project root. Read by the client; the server stores nothing."""

    model_config = ConfigDict(extra="forbid")

    dbt: str | None = Field(
        default=None,
        description="a dbt target directory holding manifest.json (`dbt parse` writes it), "
        "or the dbt project directory whose target/ holds it",
    )
    sqlmesh: str | None = Field(
        default=None, description="a SQLMesh project directory (its models/ are read)"
    )

    @model_validator(mode="after")
    def _exactly_one(self) -> LineageDecl:
        if (self.dbt is None) == (self.sqlmesh is None):
            raise ValueError("lineage names exactly one tool: `dbt: <dir>` or `sqlmesh: <dir>`")
        return self

    @property
    def tool(self) -> tuple[str, str]:
        """(tool, directory)."""
        return ("dbt", self.dbt) if self.dbt is not None else ("sqlmesh", str(self.sqlmesh))


class CubeDecl(Authored):
    """The Cube this project's governed asks compile through. Config only; the
    secret is named by env ref, like a connection's."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(description="Cube's REST base, e.g. http://localhost:4000")
    api_secret_env: str = Field(
        default="CUBEJS_API_SECRET",
        description="process-env var holding CUBEJS_API_SECRET, the key dst signs "
        "each request's security-context token with",
    )
    meta_ttl: int = Field(
        default=300,
        ge=0,
        description="seconds a fetched /v1/meta is reused per caller before Cube is asked again",
    )
    jwt_clock: Literal["wall", "process"] = Field(
        default="wall",
        description="the clock each request's token is dated by: 'wall' (real time, even "
        "when dst's own clock is pinned to an asked-on day, so Cube never finds the token "
        "expired) or 'process' (the clock Python reports)",
    )
    plan_context: CubePlanContext = Field(
        default_factory=lambda: CubePlanContext(),
        description="who plan and apply read /v1/meta as: the groups whose view of the "
        "model is the one checked and stamped (a member an access_policy withholds from "
        "them is not in it)",
    )


class CubePlanContext(Authored):
    model_config = ConfigDict(extra="forbid")

    groups: StrList = Field(
        default_factory=list,
        description="caller groups plan reads the meta as; empty reads it as a caller in "
        "every group the overlay's hide entries except",
    )


class ProjectConfig(Authored):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    default_provider: str | None = None
    ai_pricing: dict[str, tuple[float, ...]] = Field(default_factory=dict)
    connections: dict[str, ConnectionDecl] = Field(default_factory=dict)
    demo: DemoConfig | None = None
    probe: ProbeConfig = Field(default_factory=ProbeConfig)
    lineage: LineageDecl | None = None
    cube: CubeDecl | None = None
    groups: StrList = Field(
        default_factory=list,
        description="the caller groups a `hide` entry may except, e.g. [support, "
        "sales]; a name not listed here fails plan and apply. Declaring a group "
        "grants nothing: membership is set on the caller (or asserted by the "
        "identity provider), never by a request",
    )

    @field_validator("groups")
    @classmethod
    def _group_names(cls, v: list[str]) -> list[str]:
        cleaned = [g.strip() for g in v]
        if any(not g for g in cleaned):
            raise ValueError("groups: every entry needs a name")
        return cleaned

    @field_validator("ai_pricing")
    @classmethod
    def _pricing_shape(cls, v: dict[str, tuple[float, ...]]) -> dict[str, tuple[float, ...]]:
        return check_ai_pricing(v)

    @field_validator("providers")
    @classmethod
    def _no_inline_secrets(cls, v: dict[str, ProviderConfig]) -> dict[str, ProviderConfig]:
        for name, p in v.items():
            if p.api_key:
                raise ValueError(
                    f"provider '{name}' inlines api_key — dst.yaml is committed to a "
                    "repo; use api_key_env"
                )
        return v


def parse_project_yaml(text: str) -> ProjectConfig:
    data = parse_yaml(text, "dst.yaml") or {}
    if not isinstance(data, dict):
        raise ValueError("dst.yaml must be a mapping")
    return parse_authored(ProjectConfig, data, "dst.yaml")
