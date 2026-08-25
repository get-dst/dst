"""The dst project file (dst.yaml) — the file-first workspace config.

A project directory is the OSS source of truth: dst.yaml holds providers,
pricing, and connection declarations; semantic/ holds the shared layer
(entities/*.yaml, definitions/*.md); lenses/<name>/ holds each lens's file
tree (lens.yaml, queries.yaml, definitions/*.md, certified/*.md,
certified_answers.yaml, evals/cases.yaml). Secrets NEVER live in the project —
providers use api_key_env, connections use secret_env; inline secrets are a
parse error, not a lint warning.
"""

from __future__ import annotations

from typing import Any

from pydantic import ConfigDict, Field, field_validator

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


class ProjectConfig(Authored):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    default_provider: str | None = None
    ai_pricing: dict[str, tuple[float, float]] = Field(default_factory=dict)
    connections: dict[str, ConnectionDecl] = Field(default_factory=dict)

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
