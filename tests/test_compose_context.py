"""The composer's governing context — the silent-wrong-answer regression.

A served request makes two model calls. Generation receives the whole semantic
model; `compose()` — the call that writes the English a person reads — takes
`semantic_model` as a parameter, and if it puts none of it in the prompt the
composer contradicts the lens over correct SQL and a correct row. On a lens
whose `bond_type` definition states that `'-'` IS the single bond and that
hedging about missing data is wrong, the composed answer reads:

    No bond is recorded between atoms TR004_8 and TR004_20; the query returned a
    null or placeholder value ("-").
    confidence: verified · certified (certified)

— wrong, while wearing every label the product issues to say it is right.

The pin: a definition bound to a column the ANSWER projects reaches the compose
prompt, whole; one bound to a column the answer does not project stays out (the
scope is a scope, not "send the model everything"). The rows-only path pays
nothing either way — it never builds a composer (test_certified_api pins that).
"""

from __future__ import annotations

from services.contracts.protocols import CacheableBlock, GeneratedQuery, LLMResult, Message
from services.contracts.semantic_model import (
    Definition,
    Entity,
    EntitySource,
    Field,
    Metric,
    SemanticModel,
)
from services.contracts.warehouse import QueryResult
from services.runtime.answer import (
    AnswerComposer,
    compose_prompt,
    declared_currency,
    governing_definitions,
)

_TRAP = (
    'Never read `\'-\'` as "missing", "unknown" or "no bond type". It is a real, '
    "meaningful value: the single bond."
)

_MODEL = SemanticModel(
    lens="tox",
    dialect="duckdb",
    entities=[
        Entity(
            name="bond",
            source=EntitySource(connection="wh", table="main.bond"),
            fields=[Field(name="bond_id", type="string"), Field(name="bond_type", type="string")],
        )
    ],
    definitions=[
        Definition(
            term="bond_type",
            about="bond.bond_type",
            summary="Single bond = '-', double = '=', triple = '#'.",
            body=_TRAP,
        ),
        Definition(
            term="carcinogenic",
            about="molecule.label",
            body="Carcinogenic is label = '+'.",
            sql_expr="molecule.label = '+'",
        ),
        Definition(term="percentage", body="100.0 * matching / scope, in one pass."),
    ],
)

_SQL = "SELECT b.bond_type FROM main.bond AS b WHERE b.bond_id = 'TR004_8_20'"
_RESULT = QueryResult(columns=["bond_type"], rows=[["-"]])


class _CapturingLLM:
    """Records the exact blocks the composer sends — the only honest way to assert
    what the model saw, since the whole bug was a parameter that never became text."""

    def __init__(self) -> None:
        self.system = ""
        self.user = ""

    def complete(
        self,
        *,
        system: list[CacheableBlock],
        messages: list[Message],
        model: str,
        temperature: float,
        max_tokens: int,
    ) -> LLMResult:
        self.system = "\n".join(b.text for b in system)
        self.user = "\n".join(m.content for m in messages)
        return LLMResult(text="A single bond.", input_tokens=1, output_tokens=1)


def _compose(result: QueryResult = _RESULT, sql: str = _SQL) -> _CapturingLLM:
    llm = _CapturingLLM()
    AnswerComposer(llm).compose(
        question="What kind of bond joins the atoms TR004_8 and TR004_20?",
        generated=GeneratedQuery(sql=sql),
        result=result,
        semantic_model=_MODEL,
        prose_context=[],
    )
    return llm


def test_a_definition_governing_a_result_column_reaches_the_compose_prompt() -> None:
    """THE regression. The page that forbids the wrong sentence must be IN the
    prompt of the call that writes it — body and all, not a name and not a
    summary: the trap line sits at the bottom of the page."""
    llm = _compose()
    assert "bond_type" in llm.user
    assert _TRAP in llm.user, "the governing definition's BODY never reached the composer"
    assert "Single bond = '-'" in llm.user  # the summary rides along with it


def test_the_composer_is_told_the_definitions_govern_what_the_values_mean() -> None:
    """Shipping the page is half of it — the instruction that a stored code is a
    value and not a gap is what turns it into a sentence the model will not write."""
    llm = _compose()
    assert "never write a sentence a definition contradicts" in llm.system
    assert "never call" in llm.system and "not recorded" in llm.system


def test_a_definition_about_a_column_the_answer_does_not_project_stays_out() -> None:
    """The scope is a scope. Sending the whole model is 17 kB of SQL-authoring
    traps on every compose call, most of it about columns the prose cannot
    mention, so a page bound elsewhere is not shipped."""
    llm = _compose()
    assert "Carcinogenic is label = '+'." not in llm.user


def test_the_binding_is_the_about_column_not_the_term() -> None:
    picked = [d.term for d in governing_definitions(_MODEL, ["label"])]
    assert picked == ["carcinogenic"]  # about: molecule.label, though its term is not a column
    # An `about`-less page falls back to its term, which is how a computed column
    # ("percentage") gets its page.
    assert [d.term for d in governing_definitions(_MODEL, ["percentage"])] == ["percentage"]
    # Qualified and differently-cased result columns still bind.
    assert [d.term for d in governing_definitions(_MODEL, ["b.BOND_TYPE"])] == ["bond_type"]


def test_the_applied_definition_rides_along_whatever_the_columns_are() -> None:
    """`Definition applied: carcinogenic` named a page the composer could not
    read. If the generator says it applied one, the composer gets it."""
    _, user = compose_prompt(
        question="how many are carcinogenic?",
        generated=GeneratedQuery(sql="SELECT COUNT(*) AS n", definition_used="carcinogenic"),
        result=QueryResult(columns=["n"], rows=[[343]]),
        semantic_model=_MODEL,
    )
    assert "Carcinogenic is label = '+'." in user


def _model_with_note() -> SemanticModel:
    """`_MODEL` with a profile-enriched description on bond_type — the shape
    `profile_enrich` writes (authored text · stats suffixes)."""
    entity = _MODEL.entities[0]
    fields = [
        f.model_copy(update={"description": "bond code · ~26% null · range: 3..5"})
        if f.name == "bond_type"
        else f
        for f in entity.fields
    ]
    return _MODEL.model_copy(update={"entities": [entity.model_copy(update={"fields": fields})]})


def test_a_profile_note_on_a_projected_column_reaches_the_composer_with_the_rule() -> None:
    """Attribute, don't invent: profile facts the generator saw must not leak
    into prose unlabeled. The composer gets them EXPLICITLY, under
    a header that states the attribution rule, and the system prompt requires
    naming the source for any number taken from them."""
    _, user = compose_prompt(
        question="what kind of bond?",
        generated=GeneratedQuery(sql=_SQL),
        result=_RESULT,
        semantic_model=_model_with_note(),
    )
    assert "Data notes" in user and "per the table profile" in user
    assert "bond.bond_type: bond code · ~26% null · range: 3..5" in user
    system, _ = compose_prompt(
        question="q",
        generated=GeneratedQuery(sql=_SQL),
        result=_RESULT,
        semantic_model=_model_with_note(),
    )
    assert "must name its source" in system


def test_a_note_on_a_column_the_answer_does_not_project_stays_out() -> None:
    """Same scope rule as definitions: a note about a column the rows don't
    carry cannot change a word of the prose, so it is not shipped."""
    _, user = compose_prompt(
        question="how many bonds?",
        generated=GeneratedQuery(sql="SELECT COUNT(*) AS n FROM main.bond"),
        result=QueryResult(columns=["n"], rows=[[7]]),
        semantic_model=_model_with_note(),
    )
    assert "Data notes" not in user and "~26% null" not in user


def test_no_governing_definition_leaves_the_prompt_exactly_as_it_was() -> None:
    """A lens with nothing to say about these columns pays nothing — no header,
    no blank block, no change to the turn the composer has always received."""
    _, user = compose_prompt(
        question="how many rows?",
        generated=GeneratedQuery(sql="SELECT COUNT(*) AS n"),
        result=QueryResult(columns=["n"], rows=[[7]]),
        semantic_model=_MODEL,
    )
    assert "Definitions governing" not in user
    assert user.startswith("Question: how many rows?\nSQL: SELECT COUNT(*) AS n\n")


def test_the_row_budget_and_the_count_floor_survive_the_move() -> None:
    """`compose_prompt` was lifted out of `compose()`; the two things the old
    body promised — a slice is announced as a slice, and a capped count is a
    floor — are promises about prose, so they are pinned here too."""
    big = QueryResult(columns=["bond_type"], rows=[["-"]] * 5)
    _, user = compose_prompt(
        question="q",
        generated=GeneratedQuery(sql=_SQL),
        result=big,
        semantic_model=_MODEL,
        max_rows=2,
        row_count_exact=False,
    )
    assert "Result (5+ rows, showing the first 2)" in user
    assert user.endswith("bond_type\n-\n-\n\nWrite the answer.")  # two rows, not five


# ── currency: the author declares it, the product never guesses one ───────────
# Composing `The total revenue is $23841161.22` for an all-EUR company: nothing
# tells the composer a currency, so the model reaches for its own default, `$`.
# The fix is not to swap in `€` — it is to stop asserting a currency the layer
# was never given.

_EUR = SemanticModel(
    lens="sales",
    dialect="duckdb",
    entities=[
        Entity(
            name="orders",
            source=EntitySource(connection="wh", table="orders"),
            fields=[Field(name="amount", type="number")],
            metrics=[
                Metric(name="revenue", agg="sum", expr="orders.amount", format="currency",
                       currency="EUR"),
                Metric(name="order_count", agg="count"),
            ],
        )
    ],
)  # fmt: skip


def test_a_declared_currency_reaches_the_compose_prompt() -> None:
    """The author's judgment about their own money, honoured: a metric that
    declares `currency: EUR` makes the composer state EUR — so it can never
    write `$` over euro data."""
    assert declared_currency(_EUR, ["revenue"]) == "EUR"
    _, user = compose_prompt(
        question="what was total revenue?",
        generated=GeneratedQuery(sql="SELECT SUM(amount) AS revenue FROM orders"),
        result=QueryResult(columns=["revenue"], rows=[[23841161.22]]),
        semantic_model=_EUR,
    )
    assert "Monetary amounts are in EUR" in user


def test_no_currency_is_asserted_when_none_is_declared() -> None:
    """The default is a BARE number, not a guessed symbol. A projected metric
    with no declared currency puts no currency line in the prompt, and the system
    rule forbids the model inventing one."""
    assert declared_currency(_EUR, ["order_count"]) is None
    system, user = compose_prompt(
        question="how many orders?",
        generated=GeneratedQuery(sql="SELECT COUNT(*) AS order_count FROM orders"),
        result=QueryResult(columns=["order_count"], rows=[[89]]),
        semantic_model=_EUR,
    )
    assert "Monetary amounts are in" not in user
    assert "never" in system.lower() and "$" in system  # the no-guess rule is present


def test_mixed_declared_currencies_assert_nothing() -> None:
    """Two projected money metrics in different currencies: the product states
    neither — a bare number beats stamping one currency onto mixed money."""
    mixed = _EUR.model_copy(deep=True)
    mixed.entities[0].metrics.append(
        Metric(name="usd_revenue", agg="sum", expr="orders.amount", format="currency",
               currency="USD")
    )  # fmt: skip
    assert declared_currency(mixed, ["revenue", "usd_revenue"]) is None
