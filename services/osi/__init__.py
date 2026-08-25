"""Apache Ossie / OSI interchange — read and write the vendor-neutral semantic format.

OSI (`open-semantic-interchange.org`, Apache-2.0, core spec 0.2.0.dev0) standardizes
exactly dst's object graph — semantic model / datasets / fields / relationships /
metrics — with 60+ signatories including Snowflake, Databricks, dbt Labs, Cube, Omni
and Tableau. Two reasons this seam earns its place:

  * IMPORT answers "why must I author this twice?" — a warehouse that already has a
    semantic model gets a dst layer without retyping it.
  * EXPORT answers "what happens when I leave?" — and, more interestingly, the spec
    carries an explicit ``ai_context`` slot on the model, every dataset and every
    field. That slot is approximately what dst sells: `ai_instructions`,
    `use_when`, `grain`, `use_cases`, `common_questions`. Being the layer that fills
    it best is a better position than being one more layer that defines metrics.

Deliberately a ONE-SHOT translation in both directions, like `dst import dbt`:
dst owns its `semantic/` files after import, and nothing is live-synced. Anything
that does not survive the round trip is reported as an explicit skip — a silent drop
in a governance layer is the failure this whole module exists to avoid.
"""

from __future__ import annotations

from services.osi.emit import to_osi
from services.osi.load import OsiImport, from_osi

__all__ = ["OsiImport", "from_osi", "to_osi"]
