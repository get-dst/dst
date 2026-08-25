"""One hash naming the serving prompt-set.

Stamped into every persisted trace so an eval trend or a production regression
can be attributed to a prompt edit — before this, no trace recorded which
prompts produced it. The hash covers the serving path's system-prompt
constants; editing any of them changes the hash on the next deploy.
"""

from __future__ import annotations

import hashlib

from services.reviews import judge
from services.runtime import adversary, answer, assembly, generator, intent_generator

_CORPUS = "\n---\n".join(
    (
        generator._RULES,
        "".join(f"{k}:{v}" for k, v in sorted(generator._DIALECT_NOTES.items())),
        intent_generator._RULES,
        assembly._EQUIV_SYSTEM,
        judge._SYSTEM,
        adversary._SYSTEM,
        answer._SYSTEM,
    )
)

PROMPT_HASH = hashlib.sha256(_CORPUS.encode()).hexdigest()[:12]
