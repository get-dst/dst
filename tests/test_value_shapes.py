"""Value-shape detection — textual columns hiding structure
get a shape + access hint; a shape-only column stays shape-only; enrichment
renders the hint.
Pinned by the failure class: a lens compares raw JSON to a plain string,
serves NULL, then asserts the data doesn't exist."""

from __future__ import annotations

import duckdb

from services.connectors.sampling import classify_value_shape, sample_table
from services.contracts.profile import ColumnSampleSpec, TableSampleSpec
from services.contracts.semantic_model import Entity, EntitySource, Field, SemanticModel
from services.lenses.profile_enrich import enrich_model


def test_classifier_shapes() -> None:
    shape = classify_value_shape("city", ['{"en": "Abakan", "ru": "Абакан"}', '{"en": "Moscow"}'])
    assert shape is not None and shape[0] == "json-object"
    assert "json_extract_string(city" in shape[1] and "en" in shape[1]

    shape = classify_value_shape("coordinates", ["(129.77,62.09)", "(114.03, 62.53)"])
    assert shape is not None and shape[0] == "point"
    assert "split_part" in shape[1]

    assert classify_value_shape("price", ["12.5", "7", "-3.25"])[0] == "numeric-text"  # type: ignore[index]
    assert classify_value_shape("day", ["2026-08-04", "2026-01-01 10:00:00"])[0] == "date-text"  # type: ignore[index]


def test_classifier_is_conservative() -> None:
    # Mixed shapes, plain words, or a single sample: never classify.
    assert classify_value_shape("c", ['{"a": 1}', "plain"]) is None
    assert classify_value_shape("c", ["hello", "world"]) is None
    assert classify_value_shape("c", ['{"a": 1}']) is None
    assert classify_value_shape("c", []) is None
    assert classify_value_shape("c", ["[1,2]", '{"a": 1}']) is None


def test_sampling_detects_json_column_and_enrichment_renders_it(tmp_path) -> None:
    db = tmp_path / "t.duckdb"
    con = duckdb.connect(str(db))
    con.execute("CREATE TABLE airports (code VARCHAR, city VARCHAR)")
    for i in range(30):  # above the low-cardinality threshold: forces the shape probe
        con.execute(
            "INSERT INTO airports VALUES (?, ?)",
            [f"A{i:03}", f'{{"en": "City{i}", "ru": "Град{i}"}}'],
        )
    con.close()

    ro = duckdb.connect(str(db), read_only=True)

    def run(sql: str):
        cur = ro.execute(sql)
        cols = [d[0] for d in cur.description or []]
        from services.contracts.warehouse import QueryResult

        return QueryResult(columns=cols, rows=[list(r) for r in cur.fetchall()])

    spec = TableSampleSpec(
        table="airports",
        columns=[
            ColumnSampleSpec(name="code", type="VARCHAR"),
            ColumnSampleSpec(name="city", type="VARCHAR"),
        ],
        row_count=30,
    )
    profile = sample_table(
        run, spec, connection="t", dialect="duckdb", max_rows=1000, max_distinct_for_enum=25
    )
    ro.close()
    assert profile is not None
    city = next(c for c in profile.columns if c.name == "city")
    assert city.value_shape == "json-object"
    assert city.access_hint is not None and "json_extract_string(city" in city.access_hint

    model = SemanticModel(
        lens="t",
        dialect="duckdb",
        entities=[
            Entity(
                name="airports",
                source=EntitySource(connection="t", table="airports"),
                fields=[Field(name="city", type="string")],
            )
        ],
    )
    enriched = enrich_model(model, [profile])
    desc = enriched.entities[0].fields[0].description or ""
    assert "json_extract_string(city" in desc
