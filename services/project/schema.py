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

from typing import Any, Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from services.config import ProviderConfig
from services.contracts.authoring import Authored, parse_authored
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


class ProjectConfig(Authored):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    default_provider: str | None = None
    ai_pricing: dict[str, tuple[float, float]] = Field(default_factory=dict)
    connections: dict[str, ConnectionDecl] = Field(default_factory=dict)
    demo: DemoConfig | None = None

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
