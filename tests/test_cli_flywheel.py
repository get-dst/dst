"""The flywheel's first two steps have a CLI.

`dst reviews` lists and `dst patches approve` rules, but FILING a correction
and DRAFTING its patch are otherwise REST-only, forcing callers to hand-roll
raw HTTP to get from step 3 to step 4 of the loop. `dst correct` and
`dst patches draft` close it, and draft PRINTS the drafted target and body
because the loop requires reading the draft before approving. httpx is
monkeypatched (the test_cli_governance pattern) — no server, no DB.
"""

from __future__ import annotations

import io
import json

import httpx
import pytest


def _run_cli(monkeypatch, argv: list[str]) -> int:
    import sys

    from services.cli.main import main

    monkeypatch.setattr(sys, "argv", ["dst", *argv])
    return main()


def _stdin(monkeypatch, text: str, *, tty: bool = False) -> None:
    import sys

    class _In(io.StringIO):
        def isatty(self) -> bool:
            return tty

    monkeypatch.setattr(sys, "stdin", _In(text))


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch, tmp_path):
    """No developer .env leaks in — every test passes --token explicitly."""
    monkeypatch.delenv("DST_URL", raising=False)
    monkeypatch.delenv("DST_ADMIN_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)


def _ticket(**over: object) -> dict[str, object]:
    t: dict[str, object] = {
        "ticket_id": "rev_1",
        "request_id": "req_9",
        "lens": "customer_value",
        "caller": "admin",
        "state": "needs_human",
        "origin": "human",
        "correction": None,
        "url": "http://localhost:8000/reviews/rev_1",
    }
    t.update(over)
    return t


def _candidate(**over: object) -> dict[str, object]:
    c: dict[str, object] = {
        "id": "pc_1",
        "ticket_id": "rev_1",
        "lens": "customer_value",
        "kind": "definition",
        "target": "repeat_customer",
        "owner": "lens-owner",
        "diff_before": "A customer with more than one paid order.",
        "diff_after": "A customer with more than one paid order in the last 12 months.",
        "status": "candidate",
        "rejection_note": None,
    }
    c.update(over)
    return c


# ── dst correct: flywheel step 3 ─────────────────────────────────────────

_NOTE = (
    "Repeat customers must be windowed to the trailing 12 months. The lifetime "
    "reading double-counts churned accounts that ordered twice in 2019, which is "
    "what made this answer 3x too high."
)


def _capture_post(
    monkeypatch, status: int = 201, payload: object | None = None
) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"], seen["body"], seen["timeout"] = url, json, timeout
        seen["headers"] = headers
        return httpx.Response(
            status,
            json=payload if payload is not None else _ticket(),
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", fake_post)
    return seen


def test_correct_files_the_delta_and_names_the_next_step(monkeypatch, capsys) -> None:
    seen = _capture_post(monkeypatch)
    argv = [
        "correct",
        "req_9",
        "--kind",
        "definition",
        "--target",
        "repeat_customer",
        "--note",
        _NOTE,
        "--token",
        "dstadm_t",
    ]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["url"] == "http://localhost:8000/v1/reviews"
    assert seen["body"] == {
        "request_id": "req_9",
        "correction": {"kind": "definition", "note": _NOTE, "target": "repeat_customer"},
    }
    assert seen["headers"] == {"Authorization": "Bearer dstadm_t"}
    out = capsys.readouterr().out
    assert "rev_1: correction filed (definition → repeat_customer) on req_9" in out
    assert "state=needs_human" in out
    # The whole point of the verb: hand the agent step 4 instead of a REST recipe.
    assert "next: dst patches draft rev_1" in out


def test_correct_reads_the_note_from_a_file(monkeypatch, capsys, tmp_path) -> None:
    """Corrections are paragraphs, several sentences long — not a shell flag's
    shape."""
    seen = _capture_post(monkeypatch)
    note_file = tmp_path / "note.md"
    note_file.write_text(f"{_NOTE}\n", encoding="utf-8")
    argv = [
        "correct",
        "req_9",
        "--kind",
        "definition",
        "--target",
        "repeat_customer",
        "--note-file",
        str(note_file),
        "--token",
        "dstadm_t",
    ]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["body"]["correction"]["note"] == _NOTE  # trailing newline stripped


@pytest.mark.parametrize("argv_tail", [["--note-file", "-"], []])
def test_correct_reads_the_note_from_stdin(monkeypatch, argv_tail) -> None:
    """`--note-file -` and a bare pipe both work — an agent writing the ruling
    into a heredoc must not have to shell-quote a paragraph."""
    seen = _capture_post(monkeypatch)
    _stdin(monkeypatch, _NOTE + "\n")
    argv = [
        "correct",
        "req_9",
        "--kind",
        "scope",
        "--target",
        "orders",
        "--token",
        "dstadm_t",
        *argv_tail,
    ]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["body"]["correction"]["note"] == _NOTE


def test_correct_passes_the_corrected_sql_and_answer_through(monkeypatch) -> None:
    seen = _capture_post(monkeypatch)
    argv = [
        "correct",
        "req_9",
        "--kind",
        "number",
        "--target",
        "repeat_customer",
        "--note",
        "wrong count",
        "--corrected-sql",
        "select count(*) from customers where orders > 1",
        "--corrected-answer",
        "412 customers",
        "--token",
        "dstadm_t",
    ]
    assert _run_cli(monkeypatch, argv) == 0
    correction = seen["body"]["correction"]
    assert correction["corrected_sql"] == "select count(*) from customers where orders > 1"
    assert correction["corrected_answer"] == "412 customers"


def test_correct_omits_the_optional_fields_when_unset(monkeypatch) -> None:
    """A None corrected_sql must not be sent as an explicit null — the drafter
    routes on its presence."""
    seen = _capture_post(monkeypatch)
    argv = ["correct", "req_9", "--kind", "other", "--target", "t", "--note", "n", "--token", "k"]
    assert _run_cli(monkeypatch, argv) == 0
    assert set(seen["body"]["correction"]) == {"kind", "note", "target"}


def test_correct_requires_an_explicit_target(monkeypatch, capsys) -> None:
    """Without a target the drafter places by vocabulary similarity, which
    mistargets. The verb refuses before any HTTP."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("argparse must refuse first"))
    argv = ["correct", "req_9", "--kind", "definition", "--note", "x", "--token", "dstadm_t"]
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, argv)
    assert exc.value.code == 2
    assert "--target" in capsys.readouterr().err


def test_correct_without_a_note_says_how_to_pass_one(monkeypatch, capsys) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no note, no request"))
    _stdin(monkeypatch, "", tty=True)  # interactive terminal: nothing to read
    argv = ["correct", "req_9", "--kind", "other", "--target", "t", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert "--note" in err and "--note-file" in err


def test_correct_with_an_empty_note_refuses(monkeypatch, capsys) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("empty note, no request"))
    _stdin(monkeypatch, "   \n")
    argv = ["correct", "req_9", "--kind", "other", "--target", "t", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 1
    assert "note is empty" in capsys.readouterr().err


def test_correct_with_a_missing_note_file_names_it(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no note, no request"))
    argv = [
        "correct",
        "req_9",
        "--kind",
        "other",
        "--target",
        "t",
        "--note-file",
        str(tmp_path / "absent.md"),
        "--token",
        "dstadm_t",
    ]
    assert _run_cli(monkeypatch, argv) == 1
    assert "no note file" in capsys.readouterr().err


def test_correct_surfaces_the_server_error(monkeypatch, capsys) -> None:
    """An unknown request_id 404s — the CLI must say so, not traceback (the
    hand-rolled scripts printed httpx stack dumps)."""
    _capture_post(
        monkeypatch, status=404, payload={"detail": "no traced request 'req_x' to review"}
    )
    argv = ["correct", "req_x", "--kind", "other", "--target", "t", "--note", "n", "--token", "k"]
    assert _run_cli(monkeypatch, argv) == 1
    err = capsys.readouterr().err
    assert "no traced request 'req_x' to review" in err
    assert "Traceback" not in err


def test_correct_json_is_the_agent_surface(monkeypatch, capsys) -> None:
    _capture_post(monkeypatch)
    argv = [
        "correct",
        "req_9",
        "--kind",
        "other",
        "--target",
        "t",
        "--note",
        "n",
        "--json",
        "--token",
        "k",
    ]
    assert _run_cli(monkeypatch, argv) == 0
    assert json.loads(capsys.readouterr().out) == _ticket()


# ── dst patches draft: flywheel step 4 ───────────────────────────────────


def test_patches_draft_prints_the_target_and_the_body(monkeypatch, capsys) -> None:
    """The loop requires INSPECTING the draft before approving: drafted bodies
    can silently drop rulings outside the correction's scope, and a mistargeted
    draft is only visible here."""
    seen = _capture_post(monkeypatch, payload=_candidate())
    argv = ["patches", "draft", "rev_1", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["url"] == "http://localhost:8000/mgmt/reviews/rev_1/draft-patch"
    assert seen["timeout"] == 180  # the drafter is an LLM call, not a 60s read
    out = capsys.readouterr().out
    assert "pc_1" in out and "candidate" in out and "definition" in out
    assert "repeat_customer" in out
    # both sides: what it is now, and what the draft would make it
    assert "A customer with more than one paid order." in out
    assert "A customer with more than one paid order in the last 12 months." in out
    assert "dst patches approve pc_1" in out
    assert "dst patches reject pc_1" in out


def test_patches_draft_of_a_new_term_has_no_before_side(monkeypatch, capsys) -> None:
    _capture_post(monkeypatch, payload=_candidate(diff_before=None, target="active_account"))
    assert _run_cli(monkeypatch, ["patches", "draft", "rev_1", "--token", "dstadm_t"]) == 0
    out = capsys.readouterr().out
    assert "--- current" not in out
    assert "--- drafted definition 'active_account' ---" in out


def test_patches_draft_json_is_the_agent_surface(monkeypatch, capsys) -> None:
    _capture_post(monkeypatch, payload=_candidate())
    argv = ["patches", "draft", "rev_1", "--json", "--token", "dstadm_t"]
    assert _run_cli(monkeypatch, argv) == 0
    assert json.loads(capsys.readouterr().out) == _candidate()


def test_patches_draft_without_an_id_names_the_ticket(monkeypatch, capsys) -> None:
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no ticket, no request"))
    assert _run_cli(monkeypatch, ["patches", "draft", "--token", "dstadm_t"]) == 1
    assert "`dst patches draft` needs a review ticket id" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (400, "ticket has no correction to draft a patch from"),
        (404, "review 'rev_x' not found"),
        (422, "no patch route applies to this correction"),
    ],
)
def test_patches_draft_surfaces_the_server_error(monkeypatch, capsys, status, detail) -> None:
    _capture_post(monkeypatch, status=status, payload={"detail": detail})
    assert _run_cli(monkeypatch, ["patches", "draft", "rev_x", "--token", "dstadm_t"]) == 1
    err = capsys.readouterr().err
    assert detail in err
    assert "Traceback" not in err


def test_patches_approve_still_needs_a_patch_id(monkeypatch, capsys) -> None:
    """The shared positional now carries two identities — approve/reject must
    keep naming the one they take."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no id, no request"))
    assert _run_cli(monkeypatch, ["patches", "approve", "--token", "dstadm_t"]) == 1
    assert "`dst patches approve` needs a patch id" in capsys.readouterr().err


# ── the caller's door: filing and reading a correction with only a dst_ key ──
#
# The person who actually SEES a wrong answer usually holds a caller key and no
# admin token. `POST /v1/reviews` accepts caller keys, but without a CLI path
# that reaches it with one, `dst correct` refuses them — and every wrong answer
# they find reaches the data team as a message instead of through the product.


def _capture_get(monkeypatch, payload: object, status: int = 200) -> dict[str, object]:
    seen: dict[str, object] = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        seen["url"], seen["headers"], seen["params"] = url, headers, params
        return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    return seen


def test_correct_files_with_only_a_caller_key(monkeypatch, capsys) -> None:
    """The business user's exact posture: a dst_ key and nothing else."""
    seen = _capture_post(monkeypatch, payload=_ticket(caller="peder-holm"))
    argv = [
        *("correct", "req_9", "--kind", "definition", "--target", "active_customer"),
        *("--note", _NOTE, "--key", "dst_peder"),
    ]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["url"] == "http://localhost:8000/v1/reviews"
    assert seen["headers"] == {"Authorization": "Bearer dst_peder"}
    out = capsys.readouterr().out
    assert "rev_1: correction filed (definition → active_customer) on req_9" in out
    # The next step must be one HE can take. `patches draft` is an admin verb:
    # printing it here walks him straight back into the door that was shut.
    assert "dst patches draft" not in out
    assert "dst reviews --key" in out and "rev_1" in out


def test_correct_takes_the_caller_key_from_the_environment(monkeypatch, capsys) -> None:
    """No flags at all: a home directory holding one .env with one DST_API_KEY
    is the whole of the business user's configuration."""
    monkeypatch.setenv("DST_API_KEY", "dst_env")
    seen = _capture_post(monkeypatch)
    argv = ["correct", "req_9", "--kind", "number", "--target", "revenue", "--note", _NOTE]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["headers"] == {"Authorization": "Bearer dst_env"}
    assert "dst reviews --key" in capsys.readouterr().out


def test_correct_prefers_the_admin_token_over_an_ambient_caller_key(monkeypatch) -> None:
    """An analyst holds BOTH an admin token and her own caller key. Silently
    demoting her to her own requests breaks the cross-party correction loop —
    she triages tickets raised on other people's answers. Admin wins unless
    --key says otherwise."""
    monkeypatch.setenv("DST_API_KEY", "dst_marta")
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_marta")
    seen = _capture_post(monkeypatch)
    argv = ["correct", "req_9", "--kind", "number", "--target", "revenue", "--note", _NOTE]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["headers"] == {"Authorization": "Bearer dstadm_marta"}


def test_explicit_key_beats_the_admin_token(monkeypatch) -> None:
    """Precedence: asking for the caller door gets it."""
    monkeypatch.setenv("DST_ADMIN_TOKEN", "dstadm_marta")
    seen = _capture_post(monkeypatch)
    argv = [
        *("correct", "req_9", "--kind", "number", "--target", "revenue"),
        *("--note", _NOTE, "--key", "dst_marta"),
    ]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["headers"] == {"Authorization": "Bearer dst_marta"}


def test_correct_dir_env_supplies_the_caller_key(monkeypatch, tmp_path) -> None:
    """--dir resolution, same rule as every other --dir verb: the named project's
    .env, never the shell cwd's."""
    project = tmp_path / "peder"
    project.mkdir()
    (project / ".env").write_text("DST_API_KEY=dst_fromenvfile\n", encoding="utf-8")
    seen = _capture_post(monkeypatch)
    argv = [
        *("correct", "req_9", "--kind", "number", "--target", "revenue"),
        *("--note", _NOTE, "--dir", str(project)),
    ]
    assert _run_cli(monkeypatch, argv) == 0
    assert seen["headers"] == {"Authorization": "Bearer dst_fromenvfile"}


def test_correct_refusal_names_the_caller_key_door(monkeypatch, capsys) -> None:
    """A refusal with no credential must name the door, not dead-end."""
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no credential, no request"))
    argv = ["correct", "req_9", "--kind", "number", "--target", "revenue", "--note", _NOTE]
    with pytest.raises(SystemExit):
        _run_cli(monkeypatch, argv)
    assert "caller key with --key (dst_…)" in capsys.readouterr().err


def test_reviews_with_a_caller_key_reads_the_caller_scoped_list(monkeypatch, capsys) -> None:
    """The other half of the loop: the reporter learns the outcome."""
    seen = _capture_get(monkeypatch, [_ticket(caller="peder-holm", state="approved")])
    assert _run_cli(monkeypatch, ["reviews", "--key", "dst_peder"]) == 0
    assert seen["url"] == "http://localhost:8000/v1/reviews"
    assert seen["headers"] == {"Authorization": "Bearer dst_peder"}
    assert "rev_1" in capsys.readouterr().out


def test_reviews_with_an_admin_token_still_reads_the_whole_queue(monkeypatch) -> None:
    seen = _capture_get(monkeypatch, [_ticket()])
    assert _run_cli(monkeypatch, ["reviews", "--token", "dstadm_t"]) == 0
    assert seen["url"] == "http://localhost:8000/mgmt/reviews"


def test_reviews_empty_list_speaks_the_callers_language(monkeypatch, capsys) -> None:
    _capture_get(monkeypatch, [])
    assert _run_cli(monkeypatch, ["reviews", "--key", "dst_peder"]) == 0
    assert "you have filed no corrections" in capsys.readouterr().out


def test_reviews_watch_refuses_a_caller_key_by_naming_the_read_path(monkeypatch, capsys) -> None:
    """--watch polls the team queue (admin-only). The refusal has to point at the
    door the caller DOES have, not repeat 'no admin token'."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: pytest.fail("no poll on the caller door"))
    assert _run_cli(monkeypatch, ["reviews", "--watch", "--key", "dst_peder"]) == 1
    assert "dst reviews --key" in capsys.readouterr().err
