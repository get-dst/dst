"""Warning spam must not drown the one warning that matters.

Without collapsing, every apply emits one identically-shaped
`definition 'X' has no sql_expr` line PER prose-only definition — a dozen or
more on a real lens — with the `eval_gate: warn configured but … gate SKIPPED`
line somewhere in the middle. Readers learn to skip the whole warnings block,
which is the worst possible outcome for a governance surface.

Two fixes, pinned here: same-class repeats collapse to one counted line that
still names every term (``collapse_warnings``), and degradations — a skipped
gate, an unembedded corpus, an unprobed store — are prefixed ``DEGRADED:`` and
sorted last by ``order_warnings``, which every lens row passes through.
Pure: no server, no DB.
"""

from __future__ import annotations

from services.api.mgmt_project import _lens_row
from services.contracts.semantic_model import Definition
from services.lenses.demo import jaffle_customer_value_bundle
from services.project.apply import DEGRADED, LensApplyResult, _degraded, order_warnings
from services.validate.report import Issue, collapse_warnings, validate_bundle


def _prose(term: str) -> Definition:
    return Definition(term=term, body=f"{term}, in prose, with no enforceable expression")


# ── collapsing repeats ───────────────────────────────────────────────────────


def test_prose_only_definitions_collapse_to_one_counted_line() -> None:
    """The reported shape: nine prose definitions, nine identical lines. One
    line now, naming the count and every term."""
    bundle = jaffle_customer_value_bundle()
    terms = [f"term_{i}" for i in range(9)]
    bundle.semantic_model.definitions = [_prose(t) for t in terms]
    report = validate_bundle(bundle, [], [])
    warnings = collapse_warnings([i for i in report.issues if i.severity == "warning"])

    prose_lines = [w for w in warnings if "`sql:`" in w]
    assert len(prose_lines) == 1
    line = prose_lines[0]
    assert line.startswith("9 definitions have no `sql:` expression in their frontmatter")
    for term in terms:  # nothing dropped — every term is still named
        assert term in line
    # and the class explanation survives the collapse
    assert "can't be enforced or verified in generated SQL" in line


def test_a_single_member_class_keeps_its_own_message() -> None:
    """One prose definition is not spam — the specific wording is better than a
    counted line, so collapsing starts at two."""
    bundle = jaffle_customer_value_bundle()
    bundle.semantic_model.definitions = [_prose("solo")]
    report = validate_bundle(bundle, [], [])
    warnings = collapse_warnings([i for i in report.issues if i.severity == "warning"])
    # The warning names the key authors TYPE (`sql:` in the frontmatter), not
    # only the internal field name.
    assert any(w.startswith("definition 'solo' has no `sql:` expression") for w in warnings)
    assert not any(w.startswith("1 definitions") for w in warnings)


def test_collapse_keeps_the_detail_that_varies_beyond_the_term() -> None:
    """about_dangling names WHICH member is missing — collapsing must not eat
    it, so the subject carries the detail."""
    issues = [
        Issue(
            severity="warning",
            code="definition_about_dangling",
            message="definition 'a' is about 'orders.ghost', which is not in this "
            "lens's compiled model",
            subject="a (about 'orders.ghost')",
        ),
        Issue(
            severity="warning",
            code="definition_about_dangling",
            message="definition 'b' is about 'customers.gone', which is not in this "
            "lens's compiled model",
            subject="b (about 'customers.gone')",
        ),
    ]
    (line,) = collapse_warnings(issues)
    assert line.startswith("2 definitions are about a member that is not in this lens")
    assert "a (about 'orders.ghost')" in line and "b (about 'customers.gone')" in line


def test_distinct_classes_never_merge_and_uncollapsible_codes_print_whole() -> None:
    issues = [
        Issue(severity="warning", code="entity_no_fields", message="entity 'x' …", subject="x"),
        Issue(severity="warning", code="entity_no_fields", message="entity 'y' …", subject="y"),
        Issue(severity="warning", code="no_callers", message="no callers on the allow-list"),
        Issue(severity="warning", code="reference_rewritten", message="rewrote a.b → t.a.b"),
        Issue(severity="warning", code="reference_rewritten", message="rewrote c.d → t.c.d"),
    ]
    out = collapse_warnings(issues)
    # entities collapse; no_callers stands alone; reference_rewritten has no
    # subject, so both lines survive verbatim rather than losing their content
    assert out == [
        "2 entities have no modeled fields: x, y",
        "no callers on the allow-list",
        "rewrote a.b → t.a.b",
        "rewrote c.d → t.c.d",
    ]


def test_collapsed_class_holds_its_first_appearance_position() -> None:
    issues = [
        Issue(severity="warning", code="no_callers", message="first"),
        Issue(severity="warning", code="entity_no_fields", message="e1", subject="a"),
        Issue(severity="warning", code="unknown_code", message="middle"),
        Issue(severity="warning", code="entity_no_fields", message="e2", subject="b"),
    ]
    assert collapse_warnings(issues) == [
        "first",
        "2 entities have no modeled fields: a, b",
        "middle",
    ]


def test_collapse_of_nothing_is_nothing() -> None:
    assert collapse_warnings([]) == []


# ── degradations land last, and visibly ──────────────────────────────────────


def test_degraded_warnings_sort_last_and_stay_ordered_within_their_group() -> None:
    warnings = [
        _degraded("eval_gate: warn configured but no approved eval cases — gate SKIPPED"),
        "3 definitions have no sql_expr (prose-only): a, b, c",
        _degraded("4 certified answer(s) stored unembedded — no embedding provider configured"),
        "no callers on the allow-list",
    ]
    assert order_warnings(warnings) == [
        "3 definitions have no sql_expr (prose-only): a, b, c",
        "no callers on the allow-list",
        _degraded("eval_gate: warn configured but no approved eval cases — gate SKIPPED"),
        _degraded("4 certified answer(s) stored unembedded — no embedding provider configured"),
    ]


def test_the_lens_row_is_where_ordering_happens() -> None:
    """Every apply row — apply AND recompile — goes through _lens_row, so the
    skipped-gate line is the last thing an operator reads on any of them."""
    gate = _degraded("eval_gate: warn configured but no approved eval cases — gate SKIPPED")
    result = LensApplyResult(
        lens="customer_value",
        action="updated",
        version=7,
        warnings=[gate, "9 definitions have no sql_expr (prose-only): a, b, c"],
    )
    row = _lens_row(result)
    assert row["warnings"][-1] == gate
    assert row["warnings"][-1].startswith(DEGRADED)  # greppable, not just last
    assert len(row["warnings"]) == 2  # ordering never drops a line


def test_ordering_is_a_no_op_when_nothing_degraded() -> None:
    plain = ["one", "two", "three"]
    assert order_warnings(plain) == plain


# ── the gate footer ──────────────────────────────────────────────────────────


def test_apply_footer_summarizes_gate_outcomes(capsys) -> None:
    """Whether the safety net ran must be legible without grepping 40 identical
    skip warnings — one footer line, skips broken out by reason."""
    from services.cli.main import _summarize_apply

    _summarize_apply(
        [
            {"lens": "a", "action": "updated", "version": 2, "gate": "passed"},
            {"lens": "b", "action": "updated", "version": 3, "gate": "skipped (empty suite)"},
            {"lens": "c", "action": "updated", "version": 4, "gate": "skipped (provider error)"},
            {"lens": "d", "action": "unchanged"},  # no gate ran — not counted
        ]
    )
    out = capsys.readouterr().out
    assert "eval gates: 1 passed, 2 skipped (1 empty suite, 1 provider error)" in out


def test_apply_footer_stays_silent_when_no_lens_was_gated(capsys) -> None:
    from services.cli.main import _summarize_apply

    _summarize_apply([{"lens": "a", "action": "unchanged"}])
    assert "eval gates" not in capsys.readouterr().out


def test_identical_warnings_across_rows_print_once_counted(capsys) -> None:
    """The same sentence repeated across rows buries the lines that matter —
    3+ identical warnings collapse to one counted line; distinct warnings and
    2x repeats stay verbatim, and the counts line stays honest."""
    from services.cli.main import _summarize_apply

    dup = "no callers on the allow-list (admin-only until callers are added)"
    _summarize_apply(
        [
            {"lens": "a", "action": "updated", "version": 1, "warnings": [dup, "only-on-a"]},
            {"lens": "b", "action": "updated", "version": 1, "warnings": [dup]},
            {"lens": "c", "action": "updated", "version": 1, "warnings": [dup]},
        ]
    )
    out = capsys.readouterr().out
    assert out.count(dup) == 1
    assert "identical on 3 rows" in out
    assert "only-on-a" in out
    assert "4 warning(s)" in out  # the count is the true total, not the deduped one


def test_quiet_apply_keeps_only_what_needs_a_human(capsys) -> None:
    """A multi-lens apply emits one warning line per lens, which buries the
    signal. --quiet: no per-lens ok lines, each DISTINCT warning once naming
    who it fired on, rejects/errors/deletions/footer stay."""
    from services.cli.main import _summarize_apply

    dup = "cross-claim: two lenses answer this"
    _summarize_apply(
        [
            {"lens": "a", "action": "published", "version": 1, "warnings": [dup], "gate": "passed"},
            {"lens": "b", "action": "unchanged", "warnings": [dup]},
            {"lens": "c", "action": "rejected", "errors": ["validation failed"]},
            {"lens": "d", "action": "updated", "applied": ["certified answers: deleted 1"]},
        ],
        quiet=True,
    )
    out = capsys.readouterr().out
    assert "lens a" not in out.split("warning:")[0]  # no per-lens ok line
    assert out.count(dup) == 1  # distinct warning exactly once
    assert "(a, b)" in out  # naming who it fired on
    assert "lens c: rejected" in out and "validation failed" in out
    assert "deleted 1" in out  # deletions survive --quiet
    assert "2 warning(s)" in out  # the counts line stays the true total
    assert "eval gates: 1 passed" in out  # the footer stays


def test_quiet_apply_caps_the_lens_list_per_warning(capsys) -> None:
    from services.cli.main import _summarize_apply

    note = "same everywhere"
    _summarize_apply(
        [
            {"lens": f"l{i}", "action": "published", "version": 1, "warnings": [note]}
            for i in range(6)
        ],
        quiet=True,
    )
    out = capsys.readouterr().out
    assert out.count(note) == 1
    assert "+3 more" in out
