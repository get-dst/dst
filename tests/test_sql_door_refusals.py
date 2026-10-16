"""The `sql` door honours the lens's declared refusals and hides.

The governed door refused a per-patient listing and a birth date; the same
lens's `sql` door served both, because the allow-list was the only thing it
checked. One `not_computable`, every door: a statement naming a column the
entry names, or listing rows per entity key where the entry is row-level, is
refused with the sentence `query` answers with — and an entity's `hide` is
absent from the allow-list the guard enforces.
"""

from __future__ import annotations

import pytest

from services.contracts.lens_config import NotComputable
from services.contracts.semantic_model import SemanticModel
from services.contracts.shared_semantic import SharedEntity
from services.runtime import sql_guard
from services.runtime.pipeline import deterministic_refusal_not_computable, not_computable_refusal

REASON = (
    "identifiers (name, personal id, email, birth date) never leave the records team and "
    "a list of identifiable people is not analytics output"
)


def _model(*, row_level: bool = True, keys: list[str] | None = None) -> SemanticModel:
    entry = NotComputable(
        measure="patient_identifiers",
        aliases=["email", "personal id", "birth date", "date of birth", "which patients"],
        reason=REASON,
        row_level=row_level,
        keys=keys or [],
    )
    return SemanticModel.model_validate(
        {
            "lens": "px",
            "dialect": "duckdb",
            "entities": [
                {
                    "name": "patients",
                    "source": {"connection": "wh", "table": "staging.stg_crm_patients"},
                    "primary_key": ["patient_id"],
                    "fields": [
                        {"name": "patient_id", "type": "string"},
                        {"name": "birth_date", "type": "date"},
                        {"name": "city", "type": "string"},
                    ],
                },
                {
                    "name": "appointments",
                    "source": {"connection": "wh", "table": "staging.stg_ehr_appointments"},
                    "primary_key": ["appointment_id"],
                    "fields": [
                        {"name": "appointment_id", "type": "string"},
                        {"name": "patient_id", "type": "string"},
                        {"name": "appointment_date", "type": "date"},
                        {"name": "status", "type": "string"},
                    ],
                },
                {
                    "name": "fct_no_shows",
                    "source": {"connection": "wh", "table": "marts.fct_no_shows"},
                    "exposed": True,
                    "fields": [
                        {"name": "patient_id", "type": "string"},
                        {"name": "no_shows", "type": "number"},
                    ],
                },
            ],
            "not_computable": [entry.model_dump(mode="json")],
        }
    )


# ── the statements the governed door refused and this door served ─────────────

LIVE_SHAPES = [
    "select patient_id, birth_date, city from staging.stg_crm_patients limit 3",
    # the full history of one patient, joined to the patient view
    "select a.appointment_date, a.status, p.birth_date, p.city "
    "from staging.stg_ehr_appointments a join staging.stg_crm_patients p "
    "on a.patient_id = p.patient_id where a.patient_id = 'P0001' order by 1",
    # top-10 patients by no-shows from the exposed mart
    "select patient_id, no_shows from marts.fct_no_shows order by no_shows desc limit 10",
    # a filter on the named column is a lookup
    "select city from staging.stg_crm_patients where birth_date = date '2003-02-10'",
]


@pytest.mark.parametrize("sql", LIVE_SHAPES)
def test_a_statement_the_entry_covers_is_refused_with_the_query_wording(sql: str) -> None:
    model = _model()
    hit = sql_guard.not_computable_hit(sql, model)
    assert hit is not None and hit["measure"] == "patient_identifiers"
    # the sentence is the one `query` refuses a question with
    assert not_computable_refusal(hit) == deterministic_refusal_not_computable(
        "show me the birth date of a patient", model
    )
    assert REASON in not_computable_refusal(hit)


@pytest.mark.parametrize(
    "sql",
    [
        "select count(distinct patient_id) as patients from marts.fct_no_shows",
        "select city, count(*) as n from staging.stg_crm_patients group by city",
        "select status, count(*) as n from staging.stg_ehr_appointments group by status",
        "select appointment_date, count(*) from staging.stg_ehr_appointments group by 1",
    ],
)
def test_aggregates_over_the_key_still_serve(sql: str) -> None:
    """A count of patients is analytics; a list of them is the measure."""
    assert sql_guard.not_computable_hit(sql, _model()) is None


def test_row_level_is_opt_in_and_keys_can_be_named() -> None:
    listing = "select patient_id, no_shows from marts.fct_no_shows order by 2 desc limit 10"
    assert sql_guard.not_computable_hit(listing, _model(row_level=False)) is None
    # the declared keys replace the entity primary keys
    assert sql_guard.not_computable_hit(listing, _model(keys=["appointment_id"])) is None
    assert sql_guard.not_computable_hit(listing, _model(keys=["patient_id"])) is not None


def test_a_lens_declaring_nothing_hits_nothing() -> None:
    model = _model().model_copy(update={"not_computable": []})
    assert sql_guard.not_computable_hit(LIVE_SHAPES[0], model) is None


# ── hide on an entity: absent from the allow-list on every door ───────────────


def _hidden_model() -> SemanticModel:
    patients = SharedEntity.model_validate(
        {
            "name": "patients",
            "source": {"connection": "wh", "table": "staging.stg_crm_patients"},
            "primary_key": ["patient_id"],
            "hide": ["birth_date", "patients.email"],
            "fields": [
                {"name": "patient_id", "type": "string"},
                {"name": "birth_date", "type": "date"},
                {"name": "email", "type": "string"},
                {"name": "city", "type": "string"},
            ],
        }
    )
    return SemanticModel(lens="px", dialect="duckdb", entities=[patients])


def test_hidden_columns_are_out_of_scope_however_named() -> None:
    model = _hidden_model()
    assert model.allowed_columns()["staging.stg_crm_patients"] == {"patient_id", "city"}
    for sql in (
        "select birth_date from staging.stg_crm_patients",
        "select p.email from staging.stg_crm_patients p",
        "select city from patients where birth_date > date '2000-01-01'",
    ):
        result = sql_guard.check(sql, model)
        assert not result.ok and "out of lens scope" in (result.reason or ""), sql
    assert sql_guard.check("select city from patients", model).ok
