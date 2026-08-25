"""LLM description pass (F4): write column/table descriptions into stored profiles.

The lens wizard used to author these synchronously and throw the result away. Instead
the background profiling chain (catalog → sampling → here) fills in any column the
warehouse, dbt, or sampling pass left undocumented, persisting
``description_source="llm"`` so the generator prompts with real meaning at query time
(``profile_enrich.enrich_model`` folds stored profiles into the semantic model).

Gaps only — a warehouse/dbt/sampled description is never overwritten.
Idempotent: a re-run only touches columns that are still undocumented, so it's
cheap to fire on every refresh. No LLM configured, or ``DST_LLM_DESCRIPTIONS=false``
(the operator's off switch for this egress) ⇒ a no-op that returns the stored
profiles unchanged.
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from services.config import settings
from services.contracts.profile import ColumnProfile, TableProfile
from services.lenses import profile_store
from services.llm.assist import assist_llm, complete_json

log = logging.getLogger("dst")

_DESCRIBE_SYSTEM = (
    "You document a warehouse table for analytics. Given one table's name and its "
    "columns (name, type, and any sampled example values), write a SHORT one-line "
    "plain-English description for EVERY column listed — say what the value means, "
    "do not restate the column name.\n"
    "Output ONLY a JSON object (no prose, no code fences):\n"
    '{"table": "<one-line table description>", '
    '"columns": [{"name": "<exact column name>", "description": "<one line>"}]}\n'
    "Use ONLY the given column names. One line each. No prose outside the JSON."
)


def run_description_pass(
    session: Session,
    connection_name: str,
    tables: list[str] | None = None,
) -> list[TableProfile]:
    """Fill missing descriptions on ``connection_name``'s stored profiles, upsert, return them.

    Run after the catalog/sampling passes. ``tables`` narrows the scope (a lens's
    tables); ``None`` enriches the whole connection.
    """
    stored = profile_store.list_profiles(session, connection_name)
    if tables is not None:
        wanted = set(tables)
        stored = [s for s in stored if s.profile.table in wanted]
    profiles = [s.profile for s in stored]
    if not settings.llm_descriptions:
        # Checked before any provider is even resolved: off means zero LLM calls.
        log.info("LLM description pass skipped: DST_LLM_DESCRIPTIONS is off")
        return profiles
    llm_model = assist_llm()
    if llm_model is None:
        return profiles
    llm, model = llm_model.llm, llm_model.name
    out: list[TableProfile] = []
    for profile in profiles:
        gaps = [c for c in profile.columns if _needs_description(c)]
        if not gaps and profile.description:
            out.append(profile)
            continue
        merged = _describe_one(llm, model, profile, {c.name for c in gaps})
        if merged is not profile:
            profile_store.upsert_profile(session, merged)
        out.append(merged)
    return out


def _needs_description(column: ColumnProfile) -> bool:
    """An undocumented column — the only kind worth spending a completion on."""
    return column.description is None


def _col_line(column: ColumnProfile) -> str:
    line = f"- {column.name} ({column.type})"
    if column.description:
        line += f" — already described: {column.description}"
    elif column.top_values:
        line += f" — example values: {', '.join(column.top_values[:8])}"
    return line


def _describe_one(
    llm: object, model: str, profile: TableProfile, gap_names: set[str]
) -> TableProfile:
    """Ask the model to describe the table; write descriptions for gap columns only.

    Returns the same profile object (identity-equal) when nothing changed, so the
    caller can skip the upsert.
    """
    catalog = "\n".join(_col_line(c) for c in profile.columns)
    user = f"Table: {profile.table}\nColumns:\n{catalog}"
    try:
        data = complete_json(llm, model, _DESCRIBE_SYSTEM, user, max_tokens=1200)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — bad/empty JSON or provider error: leave the profile as-is
        return profile
    if not isinstance(data, dict):
        return profile

    descs = {
        str(item["name"]).strip(): str(item.get("description") or "").strip()
        for item in (data.get("columns") or [])
        if isinstance(item, dict) and str(item.get("name") or "").strip()
    }
    columns: list[ColumnProfile] = []
    changed = False
    for column in profile.columns:
        desc = descs.get(column.name, "")
        if column.name in gap_names and desc:
            columns.append(
                column.model_copy(update={"description": desc, "description_source": "llm"})
            )
            changed = True
        else:
            columns.append(column)

    table_desc = profile.description
    suggested = str(data.get("table") or "").strip()
    if not table_desc and suggested:
        table_desc = suggested
        changed = True

    if not changed:
        return profile
    return profile.model_copy(update={"columns": columns, "description": table_desc})
