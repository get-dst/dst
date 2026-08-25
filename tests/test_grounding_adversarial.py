"""Adversarial battery for the grounding sources.

The question each test answers: do the sources (SQL literals, clock years,
percent scaling, profile notes) close their classes, or just the observed cases —
and do they open leaks in the other direction? Each test is one specific way the
fix could break, in either direction:

  - LEAK: an invented number wrongly grounds through a new source.
  - OVERREACH: reconcile "corrects" an honest number into a value it resembles.
  - REGRESSION: the old strictness that was right stays right.

Tests marked "documented tradeoff" pin accepted behavior that should change
loudly rather than drift silently.
"""

from __future__ import annotations

from services.contracts.warehouse import QueryResult
from services.runtime.faithfulness import numeric_check, reconcile, sql_literal_numbers

# ── Class A: SQL-literal leaks ───────────────────────────────────────────────


def test_limit_literal_grounds_top_n_prose() -> None:
    # Intended: "top 10" with LIMIT 10 is the query's own shape, not invention.
    result = QueryResult(columns=["name", "rev"], rows=[["A", 5.0], ["B", 3.0]])
    sql = "SELECT name, rev FROM t ORDER BY rev DESC LIMIT 10"
    assert (
        numeric_check("Top 10 products by revenue: A leads with 5.", result, sql_text=sql)[0]
        == "pass"
    )


def test_limit_collision_is_a_documented_tradeoff() -> None:
    # LEAK, accepted: an invented count that happens to equal the LIMIT grounds.
    # Inherent to membership testing — the fix would be provenance-tagged
    # composition, not a smarter regex.
    result = QueryResult(columns=["name"], rows=[["A"], ["B"]])
    sql = "SELECT name FROM t LIMIT 10"
    assert numeric_check("10 customers matched.", result, sql_text=sql)[0] == "pass"


def test_date_string_literal_grounds_the_year_not_its_fragments() -> None:
    # '2026-01-01' must ground "2026" — but NOT hand out 1s and 01s that let
    # "only 1 region" ground against a date boundary.
    lits = sql_literal_numbers("SELECT x FROM t WHERE d >= '2026-01-01'", "duckdb")
    assert 2026.0 in lits
    assert 1.0 not in lits and -1.0 not in lits


def test_time_string_digits_do_not_leak() -> None:
    # A timestamp literal must not ground invented "30" or "12" claims.
    lits = sql_literal_numbers("SELECT x FROM t WHERE ts > '2026-08-15 12:30:00'", "duckdb")
    assert 2026.0 in lits
    assert 30.0 not in lits and 12.0 not in lits and 0.0 not in lits


def test_identifier_digits_still_never_ground() -> None:
    result = QueryResult(columns=["q4_revenue"], rows=[[9.0]])
    assert (
        numeric_check("Revenue was 4.", result, sql_text="SELECT q4_revenue FROM finance")[0]
        == "fail"
    )


def test_unparseable_sql_contributes_nothing() -> None:
    assert sql_literal_numbers("SELEC broken (((", "duckdb") == []


# ── Class B: clock-year collisions ───────────────────────────────────────────


def test_thousands_separated_count_never_grounds_as_a_year() -> None:
    # LEAK candidate: "2,025 users" parses to the float 2025.0 — the clock must
    # not bless it. A year is written bare ("2025"), never with a separator.
    result = QueryResult(columns=["users"], rows=[[1980]])
    assert numeric_check("We gained 2,025 users.", result, clock_years=(2026, 2025))[0] == "fail"


def test_bare_year_grounds_but_decimals_and_magnitudes_do_not() -> None:
    result = QueryResult(columns=["orders"], rows=[[14]])
    ok = "Monthly orders in 2026 averaged 14."
    assert numeric_check(ok, result, clock_years=(2026, 2025))[0] == "pass"
    # "2.026k" is 2026.0 numerically — not a year statement.
    assert numeric_check("We shipped 2.026k units.", result, clock_years=(2026, 2025))[0] == "fail"


def test_clock_year_does_not_widen_into_nearby_numbers() -> None:
    # The clock grounds ITS years exactly — 2020 is not "close enough" to 2026.
    result = QueryResult(columns=["orders"], rows=[[14]])
    assert numeric_check("Orders in 2020 were 14.", result, clock_years=(2026, 2025))[0] == "fail"


# ── Class C: percent tolerance width ─────────────────────────────────────────


def test_percent_claim_one_point_off_fails() -> None:
    # The scaled comparison must be as tight as the direct one: "42%" is NOT a
    # rendering of 0.41 (±1 full point was the fraction-space tolerance floor).
    result = QueryResult(columns=["rate"], rows=[[0.41]])
    assert numeric_check("42% of users churned.", result)[0] == "fail"
    assert numeric_check("41% of users churned.", result)[0] == "pass"


def test_percent_rendering_precision_still_tolerated() -> None:
    result = QueryResult(columns=["rate"], rows=[[0.5192307692307693]])
    assert numeric_check("Win rate was 51.9%.", result)[0] == "pass"
    assert numeric_check("Win rate was 51.92%.", result)[0] == "pass"
    assert numeric_check("Win rate was 53%.", result)[0] == "fail"


def test_tiny_rates_render_and_tolerate_correctly() -> None:
    result = QueryResult(columns=["rate"], rows=[[0.005]])
    assert numeric_check("The fee is 0.5%.", result)[0] == "pass"
    assert numeric_check("The fee is 1.5%.", result)[0] == "fail"


# ── Class D: reconcile must not "correct" honest numbers ─────────────────────


def test_reconcile_never_rewrites_the_clock_year_into_a_year_cell() -> None:
    # OVERREACH candidate: prose "in 2026" over a result that carries a year
    # column (2024) and CURRENT_DATE-relative SQL (no literal). The 1% match
    # tolerance makes 2024 "match" 2026 — without clock protection the honest
    # scope statement gets rewritten into a wrong year.
    result = QueryResult(columns=["year", "orders"], rows=[[2024, 120]])
    out = reconcile(
        "Orders rose in 2026.",
        result,
        sql_text="SELECT year, orders FROM t WHERE d >= CURRENT_DATE - INTERVAL 365 DAY",
        dialect="duckdb",
        clock_years=(2026, 2025),
    )
    assert "2026" in out and "2024" not in out


def test_reconcile_percent_rewrite_is_deterministic_one_decimal() -> None:
    # "51.92%" and "51.9%" across runs collapse to one spelling of the cell.
    result = QueryResult(columns=["rate"], rows=[[0.5192307692307693]])
    assert reconcile("Win rate was 51.92%.", result) == "Win rate was 51.9%."
    assert reconcile("Win rate was 51.9%.", result) == "Win rate was 51.9%."


def test_reconcile_still_rewrites_a_rounded_cell_restatement() -> None:
    # The original invariant survives the new sources: a rounded restatement of
    # a plain cell still snaps back to the cell's own value — in presentation
    # rendering (separators at ≥1000).
    result = QueryResult(columns=["clv"], rows=[[1234.56]])
    assert reconcile("Average CLV is 1234.6.", result) == "Average CLV is 1,234.56."


# ── float residue in certified finance prose ─────────────────────────────────


def test_reconcile_cleans_verbatim_float_residue() -> None:
    # SUM over FLOAT64 reaches prose as $88759841.48000014 when the composer
    # quotes the cell verbatim (it is TOLD to) — exact, so the old exact-claim
    # skip leaves it standing at `verified · certified`. Residue spellings of a
    # cell rewrite to the presentation rendering; the $ sits outside the
    # numeric span and survives.
    result = QueryResult(columns=["current_arr"], rows=[[88759841.48000014]])
    out = reconcile("Current ARR is $88759841.48000014.", result)
    assert out == "Current ARR is $88,759,841.48."


def test_reconcile_keeps_small_precise_values_verbatim() -> None:
    # Sub-1000 precision is data, not residue — no churn.
    result = QueryResult(columns=["rate"], rows=[[0.10634]])
    assert reconcile("The rate is 0.10634.", result) == "The rate is 0.10634."


def test_reconcile_never_touches_a_question_echo_with_residue() -> None:
    # A noisy figure echoed FROM the question is not a cell restatement.
    result = QueryResult(columns=["n"], rows=[[42]])
    out = reconcile(
        "For the 12.3456 threshold you asked about, the count is 42.",
        result,
        question_text="how many exceed 12.3456?",
    )
    assert "12.3456" in out


def test_rewritten_int_restatements_gain_separators() -> None:
    result = QueryResult(columns=["accounts"], rows=[[2852]])
    assert reconcile("About 2,850 accounts.", result) == "About 2,852 accounts."


# ── sign-insensitive magnitudes ──────────────────────────────────────────────


def test_absolute_value_framing_of_a_negative_result_grounds() -> None:
    # Churn is stored as a negative delta and spoken as a magnitude; "-$614,447"
    # even PARSES positive (the $ splits the minus from the digits).
    from services.runtime.faithfulness import is_grounded

    r = QueryResult(columns=["churned"], rows=[[-614446.9099999999]])
    assert is_grounded("ARR churned was $614,447.", r)
    assert is_grounded("ARR churned was 614,446.91.", r)
    assert is_grounded("ARR churned was -614446.91.", r)  # the exact form keeps working


def test_negative_result_still_rejects_a_wrong_magnitude() -> None:
    # REGRESSION: sign-insensitivity must not become magnitude-insensitivity.
    from services.runtime.faithfulness import is_grounded

    r = QueryResult(columns=["churned"], rows=[[-614446.91]])
    assert not is_grounded("ARR churned was $6,144,470.", r)


def test_magnitude_restatement_reconciles_to_the_magnitudes_rendering() -> None:
    r = QueryResult(columns=["churned"], rows=[[-614446.9099999999]])
    out = reconcile("ARR churned was 614,000 last month.", r)
    assert "614,446.91" in out


# ── Round 2, Class F: the claim SCANNER itself ───────────────────────────────
# Round 1 attacked the grounding sources; this round attacks what counts as a
# claim in the first place: "3-5" claims MINUS five, "Q3" and "3rd" claim bare
# 3s, ISO dates claim -8/-15.


def test_hyphen_ranges_claim_the_start_not_minus_the_end() -> None:
    result = QueryResult(columns=["rating"], rows=[[3], [4], [5]])
    # "3-5" must not manufacture a -5 that nothing can ever ground.
    assert numeric_check("Customers rated us 3-5 overall.", result)[0] == "pass"


def test_iso_dates_in_prose_claim_only_the_year() -> None:
    result = QueryResult(columns=["rev"], rows=[[963451]])
    assert (
        numeric_check("As of 2026-08-15, revenue is 963451.", result, clock_years=(2026, 2025))[0]
        == "pass"
    )


def test_quarter_week_and_version_labels_are_not_claims() -> None:
    result = QueryResult(columns=["rev"], rows=[[100]])
    assert numeric_check("Revenue held at 100 in Q3.", result)[0] == "pass"
    assert numeric_check("Week W33 closed at 100.", result)[0] == "pass"
    assert numeric_check("FY26 target: 100.", result)[0] == "pass"


def test_ordinals_are_labels_not_quantities() -> None:
    result = QueryResult(columns=["region", "rev"], rows=[["North", 100]])
    assert numeric_check("The 3rd largest region, North, did 100.", result)[0] == "pass"


def test_letter_guard_does_not_eat_magnitudes_or_currency() -> None:
    # The letter-prefix guard must not weaken what already worked.
    result = QueryResult(columns=["rev"], rows=[[6661765.89]])
    assert numeric_check("Total revenue is €6.66M.", result)[0] == "pass"
    assert numeric_check("Total revenue is €9.9M.", result)[0] == "fail"


def test_true_negatives_still_claim_and_still_flag() -> None:
    # The digit-lookbehind must not stop a real minus from binding.
    result = QueryResult(columns=["delta"], rows=[[-12.0]])
    assert numeric_check("The delta was -12.", result)[0] == "pass"
    assert numeric_check("The delta was -15.", result)[0] == "fail"


# ── Round 2, Class G: negative rates and intervals ───────────────────────────


def test_declined_percent_grounds_a_negative_fraction_cell() -> None:
    # "declined 12%" is how prose renders -0.12 — the sign lives in the verb.
    result = QueryResult(columns=["delta"], rows=[[-0.12]])
    assert numeric_check("Churn declined 12% this period.", result)[0] == "pass"
    # ROUND-3 FLIP (deliberate, loud): an ungroundable change-cued rate is now a
    # SKIP (derived arithmetic awaiting a derivation verifier), no longer a hard
    # fail — because the same shape over two period cells ("down 12% vs June")
    # was false-flagging the most common honest sentence in analytics prose.
    assert numeric_check("Churn declined 5% this period.", result)[0] == "skip"


def test_reconcile_never_flips_a_sign_when_rewriting_percents() -> None:
    # Grading tolerates the declined-12% idiom; the rewriter must not "fix"
    # the prose into "-12.0%" — sign-strict, so it leaves the claim alone.
    result = QueryResult(columns=["delta"], rows=[[-0.1234]])
    out = reconcile("Churn declined 12.3% this period.", result)
    assert "-12" not in out


def test_interval_strings_ground_their_durations() -> None:
    # INTERVAL '30 days' is a declared duration — "the last 30 days" grounds;
    # a year-only string filter over-corrects this to nothing.
    sql = "SELECT count(*) FROM t WHERE d > CURRENT_DATE - INTERVAL '30 days'"
    result = QueryResult(columns=["n"], rows=[[57]])
    assert numeric_check("57 tickets in the last 30 days.", result, sql_text=sql)[0] == "pass"


def test_timestamp_strings_still_do_not_leak_after_interval_carveout() -> None:
    # The interval carve-out must not reopen the timestamp-fragment hole.
    lits = sql_literal_numbers("SELECT x FROM t WHERE ts > '2026-08-15 12:30:00'", "duckdb")
    assert 30.0 not in lits and 12.0 not in lits


# ── Round 3, Class H: comparative phrasing ───────────────────────────────────
# "down 12% vs June" derives its 12 from two period cells — nothing can ground
# it, and hard-failing it rejects the most common analyst sentence shape — in
# every cue position.


def test_change_cued_percent_over_period_cells_skips_not_fails() -> None:
    months = QueryResult(columns=["month", "rev"], rows=[["Jun", 100.0], ["Jul", 88.0]])
    assert numeric_check("Revenue is down 12% vs June.", months)[0] == "skip"
    assert numeric_check("Sessions rose 3% MoM.", months)[0] == "skip"


def test_change_cued_percent_still_grounds_when_a_rate_cell_exists() -> None:
    # Soft, not blind: with a matching rate cell the claim grounds and the
    # sentence's other figures are still checked.
    result = QueryResult(columns=["delta", "rev"], rows=[[-0.12, 88.0]])
    assert numeric_check("Revenue fell 12%, closing at 88.", result)[0] == "pass"
    assert numeric_check("Revenue fell 12%, closing at 91.", result)[0] == "fail"


def test_grew_by_with_grounded_total_still_passes_overall() -> None:
    months = QueryResult(columns=["month", "rev"], rows=[["Jun", 100.0], ["Jul", 88.0]])
    assert numeric_check("Revenue grew by 8% year-over-year, reaching 88.", months)[0] == "pass"


def test_cueless_rates_still_hard_fail() -> None:
    # The guard survives the softening: a bare rate with no cue and no backing
    # is still an invention, not a comparison.
    assert numeric_check("Our margin is 73%.", QueryResult(columns=["x"], rows=[[5]]))[0] == "fail"


# ── Round 3, Class I: tolerance edges (documented) ───────────────────────────


def test_one_percent_relative_tolerance_is_a_documented_tradeoff() -> None:
    # LEAK, accepted: 1,000,000 grounds against 990,100 (1% relative slack, the
    # same tolerance that lets "€6.66M" ground 6,661,765.89). Tightening it
    # means re-litigating magnitude abbreviations — pinned so it changes loudly.
    result = QueryResult(columns=["rev"], rows=[[990100.0]])
    assert numeric_check("Revenue reached 1,000,000.", result)[0] == "pass"


def test_question_echo_id_collision_is_a_documented_tradeoff() -> None:
    # LEAK, accepted: "account 42" in the question grounds an invented "42
    # regions" in the answer. Echo grounding predates the sweep and kills a
    # worse false-positive class (empty-result echoes); membership can't tell
    # the two apart.
    result = QueryResult(columns=["r"], rows=[["x"]])
    assert (
        numeric_check("42 regions grew.", result, question_text="revenue for account 42")[0]
        == "pass"
    )


def test_word_multipliers_make_no_claims() -> None:
    # "doubled"/"halved" carry no numeral — nothing to flag, nothing to rewrite.
    result = QueryResult(columns=["rev"], rows=[[200.0]])
    assert numeric_check("Revenue doubled since spring, reaching 200.", result)[0] == "pass"


# ── Round 4, Class J: dates, proportions, finance shapes ─────────────────────


def test_trailing_comma_no_longer_defeats_surface_guards() -> None:
    # "in 2026," captures as the token "2026," — the comma
    # broke the bare-year check for any mid-sentence year. Same for "August 15,".
    result = QueryResult(columns=["orders"], rows=[[14]])
    assert (
        numeric_check("In 2026, orders averaged 14.", result, clock_years=(2026, 2025))[0] == "pass"
    )


def test_prose_dates_do_not_claim_their_day_number() -> None:
    # Freshness statements are constant composer prose — they must not flag.
    r = QueryResult(columns=["rev"], rows=[[963451.0]])
    assert (
        numeric_check("As of August 15, revenue is 963451.", r, clock_years=(2026, 2025))[0]
        == "pass"
    )
    assert (
        numeric_check("As of 15 August, revenue is 963451.", r, clock_years=(2026, 2025))[0]
        == "pass"
    )


def test_month_adjacency_is_required_a_colon_still_claims() -> None:
    # "August: 15 orders" is a quantity, not a date — the guard needs adjacency.
    assert (
        numeric_check("August: 15 orders shipped.", QueryResult(columns=["n"], rows=[[15]]))[0]
        == "pass"
    )
    assert (
        numeric_check("August: 15 orders shipped.", QueryResult(columns=["n"], rows=[[9]]))[0]
        == "fail"
    )


def test_one_in_three_is_a_proportion_not_two_claims() -> None:
    r = QueryResult(columns=["rev"], rows=[[963451.0]])
    assert numeric_check("1 in 3 customers reordered; revenue is 963451.", r)[0] == "pass"
    # …but "sold 45 in 12 markets" keeps claiming its 45 (narrow numerator).
    assert (
        numeric_check("We sold 45 in 12 markets.", QueryResult(columns=["n"], rows=[[45], [12]]))[0]
        == "pass"
    )
    assert (
        numeric_check("We sold 45 in 12 markets.", QueryResult(columns=["n"], rows=[[9], [12]]))[0]
        == "fail"
    )


def test_accounting_negatives_ground_their_negative_cell() -> None:
    result = QueryResult(columns=["net"], rows=[[-1234.0]])
    assert numeric_check("Net was (1,234) this period.", result)[0] == "pass"
    assert numeric_check("Net was (999) this period.", result)[0] == "fail"


def test_basis_points_state_their_fraction() -> None:
    assert (
        numeric_check("Spread widened 25 bps.", QueryResult(columns=["s"], rows=[[0.0025]]))[0]
        == "pass"
    )
    assert (
        numeric_check("Spread widened 25 bps.", QueryResult(columns=["s"], rows=[[0.005]]))[0]
        == "fail"
    )


# ── Round 4, Class K: exotic surface forms ───────────────────────────────────


def test_unicode_minus_keeps_its_sign() -> None:
    result = QueryResult(columns=["delta"], rows=[[-12.0]])
    assert numeric_check("The delta was −12.", result)[0] == "pass"
    assert numeric_check("The delta was −15.", result)[0] == "fail"


def test_en_dash_ranges_are_compounds_too() -> None:
    # "3–5" (en dash) must behave exactly like "3-5": start claims, end doesn't.
    result = QueryResult(columns=["rating"], rows=[[3]])
    assert numeric_check("Customers rated us 3–5 overall.", result)[0] == "pass"


# ── Class E: notes cross-contamination (documented) ─────────────────────────


def test_note_numbers_ground_plain_claims_too_documented_tradeoff() -> None:
    # LEAK, accepted: a note's "range: 3..5" also grounds an unrelated plain "3".
    # Membership can't tell a cited caveat from a coincidence; the composer's
    # attribution rule plus the judge carry this case. Pin it so a future
    # tightening flips a test, not silently.
    result = QueryResult(columns=["avg"], rows=[[4.2]])
    notes = "- tickets.satisfaction: 1-5 rating · ~26% null · range: 3..5"
    assert (
        numeric_check("3 regions improved, averaging 4.2.", result, notes_text=notes)[0] == "pass"
    )
