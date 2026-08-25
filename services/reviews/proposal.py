"""An approved patch as a PROPOSED FILE CHANGE — the ruling never writes authored truth.

Files author, the UI governs. A definition lives in
``semantic/definitions/<slug>.md`` (shared) or ``lenses/<lens>/definitions/<slug>.md``
(lens-local); a lens's ``instructions`` live in its ``lens.yaml``. Approving a patch
that targets one of those used to mutate the DB bundle — and the next ``dst apply``
recompiled the lens from the SAME unchanged files and silently reverted the human's
ruling. So approval proposes the file instead: its path, the whole new content, and a
unified diff against what the lens carries today. Landing it stays the file loop —
write, commit, ``dst apply``.

Pure (no DB, no filesystem), like ``patch.py``: the API layer loads the bundle and
returns whatever this produces.
"""

from __future__ import annotations

import difflib

from pydantic import BaseModel

from services.contracts.semantic_model import Definition
from services.lenses.repo import render_lens_repo
from services.lenses.store import LensBundle
from services.reviews.patch import PatchCandidate
from services.semantic.files import DEFINITION_DIR, definition_to_page, slug


class ProposedFile(BaseModel):
    """The file change an approved ruling asks for. Not live until it is committed
    and applied — ``content`` is the WHOLE new file, ``diff`` is it against the
    version the server currently compiles from."""

    path: str
    content: str
    diff: str


def _diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _definition_path(lens: str, term: str, *, shared: bool) -> str:
    """Where the term is authored: the shared layer when the lens compiled it from
    there, else the lens's own tree. A term the lens doesn't carry yet defaults to
    lens-local — a local page needs no ``select:`` edit to take effect, while adding
    one to the org's shared namespace is a decision only its owner makes, which is
    why ``propose_definition(shared=True)`` exists: the decision is theirs to make,
    not one the drafter should be able to take on its own."""
    if shared:
        return f"{DEFINITION_DIR}/{slug(term)}.md"
    return f"lenses/{lens}/definitions/{slug(term)}.md"


def propose_definition(
    candidate: PatchCandidate, bundle: LensBundle, *, shared: bool | None = None
) -> ProposedFile:
    """The definition page carrying the approved body.

    Rendered from the lens's COMPILED definition, so a term whose compile tailored it
    (an ambiguous term auto-resolved for this lens, services/project/compile.py) can
    propose a page that differs from the authored one in more than the body — which is
    exactly why this is a diff the human reads before landing, not a write.

    ``shared`` overrides where it lands: ``None`` infers from what the lens compiled
    (today's behavior), ``True`` proposes into the org's shared layer, ``False``
    forces the lens's own tree. There was NO way to land a shared definition for a
    term the lens hadn't already compiled from there, while the context skill's
    blast-radius rules assume shared definitions are the normal case — so every
    ruling on a new cross-cutting term was silently confined to one lens.
    """
    existing = next(
        (d for d in bundle.semantic_model.definitions if d.term == candidate.target), None
    )
    target = existing or Definition(term=candidate.target, body="")
    if shared is None:
        shared = existing is not None and existing.source == "shared"
    path = _definition_path(candidate.lens, candidate.target, shared=shared)
    before = definition_to_page(existing) if existing is not None else ""
    content = definition_to_page(target.model_copy(update={"body": candidate.diff_after}))
    return ProposedFile(path=path, content=content, diff=_diff(path, before, content))


def propose_instruction(candidate: PatchCandidate, bundle: LensBundle) -> ProposedFile | None:
    """``lenses/<lens>/lens.yaml`` with the drafted instruction appended to
    ``instructions:`` — or None when the lens already carries that exact sentence
    (nothing to author).

    The content is the canonical lens.yaml render — the same one ``dst export``
    writes and ``dst plan`` compares against, so landing it can't phantom-diff.
    """
    instruction = (candidate.diff_after or "").strip()
    current = bundle.config.instructions or ""
    if not instruction or instruction in current:
        return None
    path = f"lenses/{candidate.lens}/lens.yaml"
    before = render_lens_repo(bundle)["lens.yaml"]
    amended = bundle.model_copy(deep=True)
    amended.config.instructions = f"{current.rstrip()}\n{instruction}" if current else instruction
    content = render_lens_repo(amended)["lens.yaml"]
    return ProposedFile(path=path, content=content, diff=_diff(path, before, content))
