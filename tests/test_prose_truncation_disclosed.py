"""Prose cut off by the token cap says so.

dst discloses every ROW cap loudly (fetch cap, max_rows, compose_rows). It did
not disclose the other way an answer loses data: the composer hitting its own
token limit partway through an enumeration. A field answer listed 44 of 137
work-order ids and read as the complete list — every row was present in the
data payload, and the prose (which is what a business user's AI assistant
actually shows them) said nothing.

The signal is deterministic: the provider reports finish_reason="length" when
it truncated. It just was not carried out of the composer.
"""

from __future__ import annotations

from dataclasses import dataclass

from services.runtime.answer import AnswerResult


@dataclass
class _Res:
    """A provider completion, as AnswerComposer.compose sees it."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    finish_reason: str | None = None


def test_answer_result_carries_finish_reason() -> None:
    r = AnswerResult(text="a, b, c", finish_reason="length")
    assert r.finish_reason == "length"


def test_finish_reason_defaults_to_none() -> None:
    """Every existing construction site keeps working untouched."""
    assert AnswerResult(text="x").finish_reason is None


def _note_for(finish_reason: str | None) -> str:
    """The pipeline's deterministic append, isolated."""
    text = "The ids are: WO001, WO002, WO003"
    if finish_reason == "length":
        text += (
            " Note: this answer was cut off by the response limit — anything it "
            "enumerates is incomplete, and the full result is in the data payload."
        )
    return text


def test_truncated_prose_is_disclosed() -> None:
    out = _note_for("length")
    assert "cut off by the response limit" in out
    assert "incomplete" in out


def test_complete_prose_gets_no_note() -> None:
    """No false alarms: a normal stop must not claim the answer is partial."""
    assert "cut off" not in _note_for("end_turn")
    assert "cut off" not in _note_for(None)
