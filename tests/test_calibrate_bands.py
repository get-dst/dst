"""`dst calibrate-bands` fits the certified serve bands for the configured embedder.

The defect: serve bands are measured per embedding model, and only the local
default had its own; any other model ran on the defaults, and a model that
spreads similarities differently stopped serving paraphrases of approved
questions with nothing failing. The bands are now fitted from the project's
certified questions and a labelled pairs file the user writes, stored per
model, and read by every serve on that model. Too few pairs is a refusal that
says how many more are needed.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from services.certify import bands
from services.certify.bands import LabelledPair
from services.config import settings
from services.runtime import assembly
from tests.test_query_api import needs_db

MODEL = "example/embed-large-v2"
CORPUS = {
    "parcels shipped last week": "SELECT count(*) FROM parcels WHERE shipped",
    "parcels returned last week": "SELECT count(*) FROM parcels WHERE returned",
}


class _Angles:
    """Each asked text sits at a chosen similarity to the certified question
    it is paired with; every certified question is the same unit vector."""

    def __init__(self, sims: dict[str, float]) -> None:
        self.sims = sims
        self.dim = 2

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            s = self.sims.get(t, 1.0)
            out.append([s, math.sqrt(max(0.0, 1 - s * s))])
        return out


def _pairs(n_yes: int, n_no: int) -> tuple[list[LabelledPair], dict[str, float]]:
    sims: dict[str, float] = {}
    pairs: list[LabelledPair] = []
    for i in range(n_yes):
        asked = f"how many parcels went out last week, take {i}"
        sims[asked] = 0.80 + 0.15 * i / max(1, n_yes - 1)  # 0.80 .. 0.95
        pairs.append(LabelledPair(asked, "parcels shipped last week", True))
    for i in range(n_no):
        asked = f"parcels sent back last week, take {i}"
        sims[asked] = 0.70 + 0.18 * i / max(1, n_no - 1)  # 0.70 .. 0.88
        pairs.append(LabelledPair(asked, "parcels shipped last week", False))
    return pairs, sims


def test_too_few_pairs_is_a_refusal_that_says_how_many_more() -> None:
    pairs, sims = _pairs(7, 12)
    got = bands.fit(MODEL, CORPUS, pairs, _Angles(sims).embed)
    assert got.bands is None and got.refused is not None
    assert "13 more that should serve (have 7)" in got.refused
    assert "8 more that should not (have 12)" in got.refused


def test_the_fit_serves_no_negative_outright_and_keeps_every_paraphrase() -> None:
    pairs, sims = _pairs(25, 25)
    got = bands.fit(MODEL, CORPUS, pairs, _Angles(sims).embed)
    b = got.bands
    assert b is not None, got.refused
    assert max(got.not_serves) < b.exact  # a false serve is the one thing ruled out
    assert b.assist <= min(got.serves) and b.assist <= b.equiv <= b.exact
    assert sum(s >= b.equiv for s in got.serves) >= 0.95 * len(got.serves)
    assert b.exact == pytest.approx(0.885, abs=1e-9)
    text_out = "\n".join(got.lines())
    assert f"embedder {MODEL}: 25 pair(s) that should serve, 25 that should not" in text_out
    assert "none of the 25 that should not serve" in text_out


def test_a_pair_a_serve_time_veto_decides_is_left_out() -> None:
    pairs, sims = _pairs(20, 20)
    other_period = LabelledPair("parcels shipped last month", "parcels shipped last week", False)
    sims[other_period.asked] = 0.99  # the period veto rejects it before any band
    got = bands.fit(MODEL, CORPUS, [*pairs, other_period], _Angles(sims).embed)
    assert got.vetoed == 1 and got.bands is not None and got.bands.exact < 0.99


def test_a_pair_naming_a_question_the_corpus_does_not_hold_is_an_error() -> None:
    pairs, sims = _pairs(20, 20)
    typo = LabelledPair("parcels out", "parcel shiped last week", True)
    with pytest.raises(ValueError, match="parcel shiped last week"):
        bands.fit(MODEL, CORPUS, [*pairs, typo], _Angles(sims).embed)


def test_the_pairs_file_is_checked_entry_by_entry() -> None:
    with pytest.raises(ValueError, match="pair 2: `serves` must be true or false"):
        bands.parse_pairs(
            {
                "pairs": [
                    {"asked": "a", "certified": "b", "serves": True},
                    {"asked": "a", "certified": "b", "serves": "yes"},
                ]
            }
        )


# ── the command, end to end, and the serve reading the fit ─────────────────


def _project(root: Path, pairs: list[LabelledPair]) -> Path:
    import yaml

    lens = root / "lenses" / "parcels"
    lens.mkdir(parents=True)
    (lens / "certified_answers.yaml").write_text(
        yaml.safe_dump([{"question": q, "sql": s} for q, s in CORPUS.items()]),
        encoding="utf-8",
    )
    (root / "pairs.yaml").write_text(
        yaml.safe_dump(
            {
                "pairs": [
                    {"asked": p.asked, "certified": p.certified, "serves": p.serves} for p in pairs
                ]
            }
        ),
        encoding="utf-8",
    )
    return root


def _cli(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> int:
    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def _embedder(monkeypatch: pytest.MonkeyPatch, sims: dict[str, float]) -> None:
    monkeypatch.setattr("services.llm.registry.resolve_embedder", lambda: _Angles(sims))
    monkeypatch.setattr("services.llm.registry.embedding_model_name", lambda: MODEL)


def _forget() -> None:
    bands.reset_cache()
    with create_engine(settings.database_admin_url).begin() as c:
        c.execute(text("DELETE FROM serve_band WHERE model = :m"), {"m": MODEL})


def test_the_command_refuses_too_few_pairs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pairs, sims = _pairs(3, 3)
    root = _project(tmp_path, pairs)
    _embedder(monkeypatch, sims)
    code = _cli(
        monkeypatch, ["calibrate-bands", "--dir", str(root), "--pairs", str(root / "pairs.yaml")]
    )
    assert code == 1
    assert "17 more that should serve (have 3)" in capsys.readouterr().err


@needs_db
def test_a_stored_fit_is_what_the_serve_reads_for_that_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pairs, sims = _pairs(25, 25)
    root = _project(tmp_path, pairs)
    _embedder(monkeypatch, sims)
    _forget()
    try:
        assert assembly.certified_bands().exact == 0.95  # the defaults, uncalibrated
        assert assembly.uncalibrated_bands_note() is not None
        argv = ["calibrate-bands", "--dir", str(root), "--pairs", str(root / "pairs.yaml")]
        assert _cli(monkeypatch, [*argv, "--dry-run"]) == 0
        assert "dry run: nothing stored" in capsys.readouterr().out
        bands.reset_cache()
        assert assembly.certified_bands().exact == 0.95
        assert _cli(monkeypatch, argv) == 0
        assert f"stored for {MODEL}" in capsys.readouterr().out
        got = assembly.certified_bands()
        assert got.exact == pytest.approx(0.885, abs=1e-9) and got.assist <= got.equiv
        assert assembly.uncalibrated_bands_note() is None
    finally:
        _forget()
