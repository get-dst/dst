"""Serving-side context state: the last embedding/serving error, surfaced on /ready.

The prose-chunk store that used to live here was removed with prose-context
ingestion (2026-08-25): curated context is the semantic model, the governed
definitions, and the certified-definition pages — all file-authored. What
remains is the error surface the serving path writes and /ready reads.
"""

from __future__ import annotations

LAST_SERVING_ERROR: dict[str, str] = {}
