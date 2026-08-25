"""The benchmark process: run dst — whole, stripped, or absent —
against the synthetic proving ground and grade every answer against the oracle.

Pieces:
- ``world``     — CSV world (from the company generator) → DuckDB artifact
- ``questions`` — YAML question set, each bound to an oracle key
- ``lanes``     — how an answer is produced: ``baseline`` (no dst) or
                  ``dst`` (the real runtime pipeline), with feature stripping
- ``grading``   — candidate answers vs oracle facts (type-aware, cent tolerance)
- ``runner``    — lanes × questions → report (JSON + markdown)

Run: ``python -m services.benchmark --help`` or ``make benchmark``.
"""
