"""Schema-derived commented YAML templates (sqlmesh-style scaffolding).

Every scaffold the CLI writes shows the FULL config surface — each field
commented out with its default and description — rendered from the Pydantic
models so a new field can never silently miss the docs. The one source of
truth is Field(description=...) on the models themselves.
"""

from __future__ import annotations

from typing import Any, Literal, get_args, get_origin

import yaml
from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from services.contracts.authoring import inert_reason


def _fmt(v: Any) -> str:
    if v is PydanticUndefined:
        return "<required>"
    if isinstance(v, BaseModel):
        return ""
    dumped: str = yaml.safe_dump(v, default_flow_style=True, allow_unicode=True)
    return dumped.strip().split("\n")[0]  # scalars only; drop the ... document marker


def _yaml_key(name: str) -> str:
    """The field name as it must be TYPED in YAML — quoted when YAML would not
    load it back as itself.

    `on:` is the live case: YAML 1.1 resolves the bare key to the boolean True,
    so a reference that prints it unquoted teaches a file that fails to validate.
    Asked of the real resolver rather than a hand-kept list of bool-ish words.
    """
    return name if next(iter(yaml.safe_load(f"{name}: x"))) == name else f'"{name}"'


def _choice_word(value: object) -> str:
    """A Literal member as it must be TYPED in YAML — the `_yaml_key` rule for
    values: bare `off` loads as False, so a reference that prints it unquoted
    teaches a file that fails to validate. Asked of the real resolver, never a
    hand-kept bool-ish list. (The loader also coerces the boolean back — belt
    and braces, because people copy from comments.)"""
    if isinstance(value, str) and yaml.safe_load(f"k: {value}")["k"] != value:
        return f"'{value}'"
    return str(value)


def _choices(annotation: Any) -> str:
    if get_origin(annotation) is Literal:
        return " | ".join(_choice_word(a) for a in get_args(annotation))
    for arg in get_args(annotation):
        if get_origin(arg) is Literal:
            return " | ".join(_choice_word(a) for a in get_args(arg))
    return ""


def _item_shape(annotation: Any) -> str:
    """For list[SomeModel] fields, the item shape inline — so a scaffold reader
    can author entries without leaving the file (e.g. access.allow).

    Item fields whose type is an enum carry their choices: `fields[].type` is a
    closed enum whose every warehouse-shaped guess (BIGINT, VARCHAR) is invalid,
    so a reference that omits the choices teaches a file apply rejects."""
    if get_origin(annotation) is not list:
        return ""
    args = get_args(annotation)
    if not (args and isinstance(args[0], type) and issubclass(args[0], BaseModel)):
        return ""
    inner = ", ".join(
        f"{_yaml_key(n)}: {_fmt(f.get_default(call_default_factory=True))}"
        + (f" ({choices})" if (choices := _choices(f.annotation)) else "")
        for n, f in args[0].model_fields.items()
    )
    return f"each item: {{{inner}}}"


def commented_block(model: type[BaseModel], *, indent: str = "", _depth: int = 0) -> str:
    """Render every field of *model* as commented YAML: name, default, description.

    Nested BaseModel defaults recurse one level deeper, so e.g. a LensConfig
    template shows the full ModelConfig surface under `model:`.

    A REQUIRED nested model has no default instance to read the shape off, so it
    would render as a bare `# source: <required>` — the field an entity author
    most needs taught least. Recurse on the annotation when there is no default;
    the only field in the whole scaffold this reaches is `entity.source`, and it
    gains its two keys.
    """
    lines: list[str] = []
    for name, f in model.model_fields.items():
        if f.exclude:
            continue  # a retired tombstone — dumped nowhere, taught nowhere
        default = f.get_default(call_default_factory=True)
        desc = f.description or ""
        # A reference block that teaches an inert key as a live knob IS the bug:
        # the scaffold is where authors copy keys from.
        if (reason := inert_reason(f)) is not None:
            desc = f"IGNORED - {reason}"
        choices = _choices(f.annotation)
        if choices and choices not in desc:
            desc = f"({choices}) {desc}".strip()
        shape = _item_shape(f.annotation)
        if shape:
            desc = f"{desc} — {shape}".strip(" —")
        suffix = f"  # {desc}" if desc else ""
        key = _yaml_key(name)
        nested: type[BaseModel] | None = None
        if isinstance(default, BaseModel):
            nested = type(default)
        elif isinstance(f.annotation, type) and issubclass(f.annotation, BaseModel):
            nested = f.annotation
        if nested is not None and _depth < 2:
            lines.append(f"{indent}# {key}:{suffix}")
            lines.append(commented_block(nested, indent=indent + "  ", _depth=_depth + 1))
        else:
            lines.append(f"{indent}# {key}: {_fmt(default)}{suffix}")
    return "\n".join(lines)


# Scaffolds land in brand-new (untrusted) editor workspaces where non-ASCII
# trips unicode-highlight warnings on every line — transliterate at this seam
# so model descriptions can't leak typography into generated files.
_ASCII = str.maketrans({"—": "-", "–": "-", "→": "->", "…": "...", "·": "*", "’": "'", "‘": "'"})


def reference_section(title: str, model: type[BaseModel], *, indent: str = "") -> str:
    """A titled, fully-commented reference block to append to a scaffold file."""
    bar = "-" * max(4, 70 - len(title))
    block = f"\n{indent}# -- {title} {bar}\n{commented_block(model, indent=indent)}\n"
    return block.translate(_ASCII)
