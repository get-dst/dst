"""Strictness for the files a human AUTHORS: an unknown key is a named error.

The most expensive recurring failure in this project has one generator — a
misspelled authored key validates cleanly, applies successfully, lands in
compiled.yaml, and does nothing. ``dimensions:`` cost four agents their work
that way; ``use_when:`` written as a folded string was list()-ed into ~300
single-character router anchors and served for two applies, apply reporting
success both times. A typo must never be indistinguishable from a feature.

WHY NOT ``extra="forbid"`` ON THE CONTRACT CLASSES. LensConfig and SemanticModel
are not only the authoring schema — they are the STORAGE schema. Every lens
bundle round-trips out of Postgres through them (``LensBundle.model_validate``
against ``lens.draft_json`` / ``lens.published_json`` / ``lens_version.
bundle_json``), and every shared asset re-validates out of
``semantic_asset.body``. ``lens_version.bundle_json`` is immutable history: the
precedent is migration 0022, which had to text-replace ``canon_dir`` ->
``certified_dir`` across all three columns. Under a class-level forbid that same
drift is not a silently-ignored key, it is a hard 500 on every read of every
historical version row — and every future rename would have to keep rewriting
history forever. Storage must stay tolerant.

So strictness belongs to the SEAM, not the class. The authoring parsers pass an
``AuthoringScope`` as pydantic validation context, which propagates into every
nested model; storage and wire paths call ``model_validate`` exactly as before
and are unaffected.

The second job of the same pass is honesty about keys that are REAL but INERT —
retired blocks like ``context:`` (ingestion only ever happens through the POST
body; the block existed only because the skeleton once emitted it). Those cannot
be rejected: projects in the wild author them. They are declared with
``inert(...)`` on the field itself, and this pass collects one note per file so
apply can say so out loud.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from functools import cache
from typing import Any, get_args

from pydantic import AliasChoices, BaseModel, ValidationError, ValidationInfo, model_validator
from pydantic.fields import FieldInfo

_CONTEXT_KEY = "dst.authoring"


def inert(reason: str) -> dict[str, Any]:
    """Mark a field as parsed-but-never-read, next to the field it describes.

    ``Field(..., json_schema_extra=inert("answer_mode sets this now"))``. The
    reason is what the author is told at apply, so write it as the thing to do
    instead — not as an apology.
    """
    return {"dst_inert": reason}


def inert_reason(f: FieldInfo) -> str | None:
    extra = f.json_schema_extra
    if isinstance(extra, dict):
        reason = extra.get("dst_inert")
        if isinstance(reason, str):
            return reason
    return None


@cache
def authored_keys(cls: type[BaseModel]) -> dict[str, str]:
    """Every key this model accepts in a file -> the field name it fills.

    Aliases count: ``Join.on`` also authors as ``condition`` and
    ``CertifiedDefinition.metric`` also authors as ``term`` — rejecting either
    would be exactly the false positive this pass exists to avoid.
    """
    out: dict[str, str] = {}
    by_name = bool(cls.model_config.get("populate_by_name"))
    for name, f in cls.model_fields.items():
        alias = f.validation_alias if f.validation_alias is not None else f.alias
        if alias is None:
            out[name] = name
            continue
        if by_name:
            out[name] = name
        if isinstance(alias, str):
            out[alias] = name
        elif isinstance(alias, AliasChoices):
            for choice in alias.choices:
                if isinstance(choice, str):
                    out[choice] = name
    return out


def suggest(key: str, valid: list[str]) -> str:
    """``did you mean`` for one unknown key. With a strict schema a typo's fix
    is mechanically derivable, which is the whole point of having one."""
    close = difflib.get_close_matches(key, valid, n=1, cutoff=0.6)
    if not close:
        # Singular/plural slips on short names (`dimension:` for `dimensions:`)
        # sit under the ratio cutoff; prefix containment catches them.
        close = [v for v in valid if v.startswith(key) or key.startswith(v)][:1]
    return f" — did you mean `{close[0]}`?" if close else ""


def unknown_keys_message(unknown: list[str], valid: list[str]) -> str:
    named = "; ".join(f"unknown key `{k}`{suggest(k, valid)}" for k in unknown)
    return f"{named} (keys here: {', '.join(valid)})"


@dataclass
class AuthoringScope:
    """One authored file being parsed: its path, and what the parse found.

    ``notes`` collects the inert-key findings (real keys that do nothing);
    unknown keys raise instead.
    """

    path: str
    notes: list[str] = field(default_factory=list)

    def context(self) -> dict[str, Any]:
        return {_CONTEXT_KEY: self}

    def note(self, key: str, reason: str) -> None:
        line = f"{self.path}: `{key}` is parsed but not read — {reason}"
        if line not in self.notes:  # one file authoring the key twice says it once
            self.notes.append(line)


class Authored(BaseModel):
    """Base for every model parsed from a file a human writes.

    Extra keys stay TOLERATED on the class (see the module docstring: these are
    also the storage schema). The check runs only when an ``AuthoringScope``
    rides the validation context, which only the authoring parsers pass.
    """

    @model_validator(mode="before")
    @classmethod
    def _authored_keys(cls, data: Any, info: ValidationInfo) -> Any:
        scope = (info.context or {}).get(_CONTEXT_KEY) if info.context else None
        if not isinstance(scope, AuthoringScope) or not isinstance(data, dict):
            return data
        keys = authored_keys(cls)
        # Subclass ``mode="before"`` validators run FIRST (pydantic walks the MRO
        # from the bottom), so a join's `on:`-as-YAML-boolean is already repaired
        # to the string key by the time this sees it.
        unknown = [str(k) for k in data if str(k) not in keys]
        if unknown:
            raise ValueError(unknown_keys_message(unknown, sorted(keys)))
        for k in data:
            reason = inert_reason(cls.model_fields[keys[str(k)]])
            if reason is not None:
                scope.note(str(k), reason)
        return data


def _inner_model(annotation: Any) -> type[BaseModel] | None:
    """The BaseModel a field annotation ultimately wraps — through Annotated,
    ``list[Model]``, ``dict[str, Model]``, ``Model | None`` — so a nested
    ``missing`` error can be walked to the field that is actually absent. None
    when the annotation carries no model."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return annotation
    for arg in get_args(annotation):
        inner = _inner_model(arg)
        if inner is not None:
            return inner
    return None


def _field_at(model: type[BaseModel], loc: tuple[Any, ...]) -> FieldInfo | None:
    """The FieldInfo a pydantic error ``loc`` points at, descending nested models
    and skipping list/dict indices. None when the path can't be resolved — the
    caller falls back to the plain message, because an enriched error must never
    be a WORSE one than the one it replaces."""
    cur: type[BaseModel] | None = model
    fi: FieldInfo | None = None
    for part in loc:
        if cur is None:
            return None
        if isinstance(part, int):
            continue  # a list index — stay on the same item model
        key = str(part)
        fi = cur.model_fields.get(key) or cur.model_fields.get(authored_keys(cur).get(key, ""))
        if fi is None:
            return None
        cur = _inner_model(fi.annotation)
    return fi


def _missing_message(model: type[BaseModel] | None, loc: tuple[Any, ...]) -> str:
    """``Field required`` upgraded to say the key is REQUIRED and what it is for —
    the actionability the unknown-key ``did you mean`` errors already carry, on the
    other half of the same seam. The field's own description supplies the example;
    a field with none still gets 'required, no default', which beats the bare
    pydantic string that names neither."""
    fi = _field_at(model, loc) if model is not None else None
    desc = (fi.description or "").strip() if fi else ""
    if desc:
        return f"required — {desc}"
    return "required (no default — this key must be set)"


def _render(exc: ValidationError, path: str, model: type[BaseModel] | None = None) -> str:
    lines: list[str] = []
    for err in exc.errors():
        at = ".".join(str(part) for part in err.get("loc", ()))
        if err.get("type") == "missing":
            msg = _missing_message(model, tuple(err.get("loc", ())))
        else:
            msg = str(err.get("msg", "")).removeprefix("Value error, ")
        lines.append(f"{path}: {at}: {msg}" if at else f"{path}: {msg}")
    return "\n".join(lines)


def parse_authored[M: BaseModel](
    model: type[M], data: Any, path: str, *, notes: list[str] | None = None
) -> M:
    """Validate *data* as an AUTHORED file: an unknown key is a ValueError
    naming the file, the key path, and the nearest valid key.

    ``notes``, when given, collects the inert-key findings for the caller to
    surface as apply warnings.
    """
    scope = AuthoringScope(path)
    try:
        parsed = model.model_validate(data, context=scope.context())
    except ValidationError as exc:
        raise ValueError(_render(exc, path, model)) from exc
    if notes is not None:
        notes.extend(scope.notes)
    return parsed


def collapse_notes(notes: list[str]) -> list[str]:
    """Fold ``{path}: {message}`` notes that share a message into one counted
    line naming every path.

    Same doctrine as validate's collapse_warnings: one
    identically-shaped line per file — 20 definition pages all authoring
    ``owner:`` — is how a warnings block becomes something people learn to
    skip, which buries the one warning that matters. Nothing is dropped.
    """
    paths: dict[str, list[str]] = {}
    order: list[str] = []
    for note in notes:
        path, sep, message = note.partition(": ")
        if not sep:
            path, message = "", note
        if message not in paths:
            order.append(message)
        paths.setdefault(message, []).append(path)
    out: list[str] = []
    for message in order:
        found = paths[message]
        if len(found) == 1:
            out.append(f"{found[0]}: {message}" if found[0] else message)
        else:
            out.append(f"{len(found)} files — {message}: {', '.join(found)}")
    return out


def check_entry_keys(
    entry: dict[str, Any], allowed: frozenset[str], path: str, what: str
) -> str | None:
    """The same unknown-key rule for a hand-validated mapping — the list files
    (certified_answers.yaml, evals/cases.yaml) whose entries stay plain dicts.
    Returns the error message, or None."""
    unknown = [str(k) for k in entry if str(k) not in allowed]
    if not unknown:
        return None
    return f"{path}: {what}: {unknown_keys_message(unknown, sorted(allowed))}"
