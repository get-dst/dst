"""`dst init` scaffold pins: shape per flag combo, and ASCII-clean output.

Generated projects land in brand-new (untrusted) editor workspaces where
non-ASCII lights up unicode-highlight warnings on every line that has it —
the scaffold must read clean there.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pytest

from services.cli.init import run_init


def _init(tmp_path: Path, **over: object) -> Path:
    root = tmp_path / "proj"
    ns = argparse.Namespace(dir=str(root), name=None, warehouse=None, example=None, yes=True)
    for k, v in over.items():
        setattr(ns, k, v)
    assert run_init(ns) == 0
    return root


def _non_ascii(root: Path) -> list[str]:
    hits = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts or p.suffix == ".duckdb":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            bad = {c for c in line if ord(c) > 127}
            if bad:
                hits.append(f"{p.relative_to(root)}:{i} {sorted(bad)}")
    return hits


def test_bare_init_creates_named_folder(tmp_path: Path, monkeypatch: object) -> None:
    """No dir argument -> the project name becomes the folder; the cwd is never
    scaffolded into implicitly (a bare `dst init` once littered a $HOME)."""
    monkeypatch.chdir(tmp_path)  # type: ignore[attr-defined]
    ns = argparse.Namespace(dir=None, name="acme", warehouse=None, example=None, yes=True)
    assert run_init(ns) == 0
    assert (tmp_path / "acme" / "dst.yaml").exists()
    assert not (tmp_path / "dst.yaml").exists()
    # a second run refuses instead of scaffolding into the existing folder
    assert run_init(ns) == 1


def test_bigquery_init_asks_for_credentials_path(tmp_path: Path, monkeypatch: object) -> None:
    """Interactive bigquery init asks where the SA JSON lives and records it as an
    @path env ref; resolve_env_ref dereferences it to the file's contents."""
    sa = tmp_path / "sa.json"
    sa.write_text('{"type": "service_account"}', encoding="utf-8")
    # name, warehouse, example, instance name, creds
    answers = iter(["proj", "bigquery", "n", "", str(sa)])
    monkeypatch.setattr("builtins.input", lambda _: next(answers))  # type: ignore[attr-defined]
    root = tmp_path / "proj"
    ns = argparse.Namespace(dir=str(root), name=None, warehouse=None, example=None, yes=False)
    assert run_init(ns) == 0
    env_text = (root / ".env").read_text(encoding="utf-8")
    assert f"DST_API_KEY_BIGQUERY=@{sa}" in env_text

    from services.config import EnvRefError, resolve_env_ref

    monkeypatch.chdir(root)  # type: ignore[attr-defined]
    assert resolve_env_ref("DST_API_KEY_BIGQUERY") == '{"type": "service_account"}'
    # A dangling @path is a stated intention that failed — it raises naming the
    # env var and path, never a silent None indistinguishable from unset
    # — a silent None is indistinguishable from a var that was never set.
    (root / ".env").write_text("X=@/nowhere/nope.json\n", encoding="utf-8")
    import pytest

    with pytest.raises(EnvRefError, match="X.*nope.json"):
        resolve_env_ref("X")


def test_non_tty_init_names_the_fix_instead_of_tracebacking(
    tmp_path: Path, monkeypatch: object, capsys: object
) -> None:
    """Prompts firing on a non-tty stdin (the first command a scripting agent
    runs) must exit 1 with the flag to pass — never a raw EOFError traceback."""

    def _eof(_: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)  # type: ignore[attr-defined]
    ns = argparse.Namespace(
        dir=str(tmp_path / "proj"), name=None, warehouse=None, example=None, yes=False
    )
    with pytest.raises(SystemExit) as exc:
        run_init(ns)
    assert exc.value.code == 1
    err = capsys.readouterr().err  # type: ignore[attr-defined]
    assert "--yes" in err and "not interactive" in err


def test_instance_name_rides_both_rails(tmp_path: Path) -> None:
    """--instance-name watson must land on BOTH rails of the alias: the server-side
    env (DST_INSTANCE_NAME, what MCP presents itself by) and the scaffolded client
    registration command (`claude mcp add watson`) — a name that reaches one tier
    doesn't exist. Default is bare `dst`, and the two rails always agree."""
    named = _init(tmp_path, instance_name="watson")
    assert "DST_INSTANCE_NAME=watson" in (named / ".env").read_text(encoding="utf-8")
    assert "claude mcp add watson " in (named / "README.md").read_text(encoding="utf-8")

    plain = tmp_path / "plain"
    ns = argparse.Namespace(dir=str(plain), name=None, warehouse=None, example=None, yes=True)
    assert run_init(ns) == 0
    assert "DST_INSTANCE_NAME=dst" in (plain / ".env").read_text(encoding="utf-8")
    assert "claude mcp add dst " in (plain / "README.md").read_text(encoding="utf-8")


def test_scaffold_documents_a_postgres_connection_example(tmp_path: Path) -> None:
    """The reference comments list connection fields but no concrete warehouse
    example — the commented postgres block is the copy-paste target,
    and it pins `database:` (the known `dbname:` regression class)."""
    text = (_init(tmp_path) / "dst.yaml").read_text(encoding="utf-8")
    line = next(li for li in text.splitlines() if "database: analytics" in li)
    assert line.lstrip().startswith("#")  # commented out: changes no behavior
    assert "#   type: postgres" in text
    assert "never `dbname`" in text  # the trap, named in negative form


def test_scaffold_documents_the_field_type_enum(tmp_path: Path) -> None:
    """`fields[].type` is a closed enum whose every natural source (introspect,
    dbt, the warehouse) hands back an INVALID value — an author writes
    `type: BIGINT` and apply rejects the whole push. The scaffold documents it."""
    doc = (_init(tmp_path) / "semantic" / "README.md").read_text(encoding="utf-8")
    assert "BIGINT/INT64 -> integer" in doc  # the prose mapping
    # and the schema-derived reference block carries the enum on the item shape,
    # so a new type can never silently miss the docs
    line = next(li for li in doc.splitlines() if li.lstrip().startswith("# fields:"))
    assert "type: <required> (string | number | integer" in line


def test_scaffold_is_ascii_clean(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert _non_ascii(root) == []


def test_scaffold_never_contradicts_the_ports_it_was_given(tmp_path: Path) -> None:
    """A scaffold that names a port `init` was told NOT to use is worse than one
    with no examples: it sends the reader to a server that is not there. A
    hard-coded `:8000` next to an `--api-port` flag leaves callers hunting for
    the server."""
    root = _init(tmp_path, api_port=9123, db_port=9124)
    lied: list[str] = []
    for p in sorted(root.rglob("*")):
        if not p.is_file() or ".git" in p.parts or p.suffix == ".duckdb":
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "8000" in line:
                lied.append(f"{p.relative_to(root)}:{i} {line.strip()}")
    assert lied == []
    assert "DST_URL=http://localhost:9123" in (root / ".env").read_text(encoding="utf-8")
    assert '"9124:5432"' in (root / "docker-compose.yml").read_text(encoding="utf-8")
    for doc in ("README.md", "AGENTS.md"):
        text = (root / doc).read_text(encoding="utf-8")
        assert ":9123" in text, doc
        # And the other half of the same finding: agents started a SECOND server
        # while DST_URL already pointed at a live one.
        assert "DST_URL" in text, doc


def test_default_includes_example_lens(tmp_path: Path) -> None:
    root = _init(tmp_path)
    assert (root / "lenses/customer_value/lens.yaml").exists()
    assert (root / "lenses/customer_value/queries.yaml").exists()
    assert not (root / "lenses/customer_value/semantic_model.yaml").exists()
    # evals/cases.yaml ships shape-teaching comments and NOTHING live — nothing
    # else teaches the case surface, and guessing the keys costs attempts.
    import yaml

    cases = (root / "lenses/customer_value/evals/cases.yaml").read_text(encoding="utf-8")
    assert cases.startswith("# Eval cases")
    # The header teaches ONLY the behavioral shape and points value
    # cases at certification — certifying IS writing the regression test
    # (value cases are not scored anywhere).
    assert "expect: clarify" in cases and "status: approved" in cases
    assert "certified answers ARE the regression suite" in cases
    assert "certified_answers.yaml" in cases and "dst evals migrate" in cases
    assert yaml.safe_load(cases) is None  # comment-only: appending an entry stays valid YAML
    assert (root / "fixtures/jaffle_shop.duckdb").exists()
    assert "jaffle:" in (root / "dst.yaml").read_text(encoding="utf-8")
    # dst dev needs a Postgres story on a machine with none running.
    assert "pgvector" in (root / "docker-compose.yml").read_text(encoding="utf-8")
    # the shared layer ships with the scaffold, demo assets nested under
    # examples/ so they never mix with the user's real layer
    assert (root / "semantic/entities/examples/orders.yaml").exists()
    value_page = (root / "semantic/definitions/examples/value.md").read_text(encoding="utf-8")
    assert "status: ambiguous" in value_page
    assert "Reference: entity file" in (root / "semantic/README.md").read_text(encoding="utf-8")
    # the authoring loop ships as a scaffolded Claude Code skill
    skill = (root / ".claude/skills/dst-semantic/SKILL.md").read_text(encoding="utf-8")
    assert "dst introspect" in skill and "select.definitions" in skill
    # so does the BI-import-to-certified-answers loop
    certify = (root / ".claude/skills/dst-certify/SKILL.md").read_text(encoding="utf-8")
    assert "certified_answers.yaml" in certify
    # and the context-authoring rules
    context = (root / ".claude/skills/dst-context/SKILL.md").read_text(encoding="utf-8")
    assert "Decide, don't describe" in context
    assert "status: ambiguous" in context and "negative form" in context
    assert "verified_by" in certify and "source" in certify
    # and the history-bootstrap loop
    history = (root / ".claude/skills/dst-history-bootstrap/SKILL.md").read_text(encoding="utf-8")
    assert "normalized_literals" in history and "query_parameterized_hash" in history
    assert "status: ambiguous" in history and "run_count is popularity" in history
    assert "never" in history and "history:<shape_hash>" in history
    assert "--probe-certified" in certify and "VERBATIM" in certify
    # Certifying is also writing the regression test
    assert "dst test" in certify
    # pointer lines ride along in AGENTS.md and the README
    agents_md = (root / "AGENTS.md").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "dst-certify" in agents_md
    assert "dst-certify" in readme
    assert "dst-history-bootstrap" in agents_md
    assert "dst-history-bootstrap" in readme
    # the /v1 query body shape is spelled out in BOTH ambient docs — without it
    # a caller guesses {"question": ...} and gets a 422
    for doc in (agents_md, readme):
        assert "/v1/lenses/<lens>/query" in doc and '{"q": "..."}' in doc


def test_scaffold_list_files_survive_an_append(tmp_path: Path) -> None:
    """A scaffolded `[]` placeholder plus an APPENDED entry — the natural
    motion — is malformed YAML. The files ship comment-only: the loader reads
    them as present-but-empty (files win from day one), and appending an entry
    at the bottom just works."""
    import yaml

    from services.project.loader import load_lens_source

    lens = _init(tmp_path) / "lenses/customer_value"

    def _lens_files() -> dict[str, str]:
        return {
            p.relative_to(lens).as_posix(): p.read_text(encoding="utf-8")
            for p in lens.rglob("*")
            if p.is_file()
        }

    for rel in ("certified_answers.yaml", "evals/cases.yaml"):
        scaffold = (lens / rel).read_text(encoding="utf-8")
        assert all(li.startswith("#") or not li.strip() for li in scaffold.splitlines()), rel
        assert yaml.safe_load(scaffold) is None
    source = load_lens_source(_lens_files())
    assert source.certified_answers == []  # present-but-empty, never absent (None)
    assert source.eval_cases == []

    for rel, entry in (
        ("certified_answers.yaml", "- question: How many customers?\n  sql: SELECT 1\n"),
        ("evals/cases.yaml", "- question: what is value?\n  expect: clarify\n  term: value\n"),
    ):
        path = lens / rel
        path.write_text(path.read_text(encoding="utf-8") + entry, encoding="utf-8")
    source = load_lens_source(_lens_files())
    assert [a["question"] for a in source.certified_answers or []] == ["How many customers?"]
    assert [c["expect"] for c in source.eval_cases] == ["clarify"]


def test_scaffold_is_an_appliable_project(tmp_path: Path) -> None:
    """init output == a project apply would accept: parse + load + compile, pure."""
    from services.project.compile import compile_lens_model, dialect_for
    from services.project.loader import load_lens_source, split_semantic
    from services.semantic.files import parse_semantic_files

    root = _init(tmp_path)
    files = {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in root.rglob("*")
        if p.is_file() and p.suffix in {".yaml", ".yml", ".md"} and ".git" not in p.parts
    }
    entities, definitions = parse_semantic_files(split_semantic(files))
    assert set(entities) == {"customers", "orders"}
    assert definitions["value"].status == "ambiguous"
    lens_files = {
        p.removeprefix("lenses/customer_value/"): c
        for p, c in files.items()
        if p.startswith("lenses/customer_value/")
    }
    source = load_lens_source(lens_files)
    model, warnings = compile_lens_model(
        config=source.config,
        shared_entities=entities,
        shared_definitions=definitions,
        local_definitions=source.local_definitions,
        use_when=source.use_when,
        sample_queries=source.sample_queries,
        dialect=dialect_for("duckdb"),
    )
    assert warnings == []
    assert {e.name for e in model.entities} == {"customers", "orders"}
    assert {d.term for d in model.definitions} == {"lifetime_value", "repeat_customer", "value"}


def test_no_example_still_documents_the_surface(tmp_path: Path) -> None:
    root = _init(tmp_path, example=False, warehouse="none")
    assert not (root / "lenses/customer_value").exists()
    assert not (root / "fixtures").exists()
    assert "jaffle" not in (root / "dst.yaml").read_text(encoding="utf-8")
    ref = (root / "lenses/REFERENCE.md").read_text(encoding="utf-8")
    assert "access" in ref and "metric" in ref  # lens.yaml + definition frontmatter
    # the eval-case surface is documented too: the behavioral shape + the
    # pointer sending value cases to certification (they are not scored)
    assert "expect: clarify | refuse" in ref and "status: approved" in ref
    assert "ARE the regression suite" in ref
    assert "dst evals migrate" in ref
    assert _non_ascii(root) == []
    # The join condition is documented as a QUOTED key: copying the reference
    # used to reproduce the YAML-boolean bug (`on:` loads as True), and a
    # reference an author can paste is the whole point of generating one.
    doc = (root / "semantic/README.md").read_text(encoding="utf-8")
    assert '# "on": <required>' in doc  # the join-fields reference block
    assert '"on": <required>' in doc.split("joins:")[1]  # and the inline item shape
    assert "\non: " not in doc and "{right: <required>, on:" not in doc
    assert "a YAML boolean" in doc and "condition:" in doc
    # COUNT(*) is authorable: the metric reference says expr is optional for it
    assert "COUNT(*)" in doc


@pytest.mark.parametrize("warehouse", ["demo", "none", "postgres", "bigquery", "snowflake"])
@pytest.mark.parametrize("example", [True, False])
def test_every_init_variant_scaffolds_a_valid_dst_yaml(
    tmp_path: Path, warehouse: str, example: bool
) -> None:
    """`init --warehouse none --no-example` — the path a user with their own
    warehouse takes — wrote `connections:` with nothing under it but comments.
    YAML reads that as null and the project schema rejects it, so `dst plan`
    failed on command one: `dst.yaml: invalid — connections: Input should be
    a valid dictionary`. Every flag combo is checked, because the trap is a
    key whose only content is comments, not this one flag."""
    from services.project.schema import parse_project_yaml

    root = _init(tmp_path / warehouse / str(example), warehouse=warehouse, example=example)
    parse_project_yaml((root / "dst.yaml").read_text(encoding="utf-8"))


def test_the_reference_says_a_lens_needs_connections(tmp_path: Path) -> None:
    """Authoring a lens from scratch hits `lens 'x' names no connection`.
    `connections` defaults to empty, so without a description the generated
    reference — the ONLY lens documentation a --no-example project gets —
    renders a bare `connections: []` teaching nothing."""
    ref = (_init(tmp_path, example=False, warehouse="none") / "lenses/REFERENCE.md").read_text(
        encoding="utf-8"
    )
    line = next(li for li in ref.splitlines() if li.lstrip().startswith("# connections:"))
    assert "REQUIRED" in line
    assert "dst.yaml connection" in line  # what goes in it
    assert "names no connection" in line  # the error it prevents, verbatim


def test_no_required_field_reaches_the_scaffold_undescribed(tmp_path: Path) -> None:
    """One field missing its description means the reference is only as good as
    whoever last touched that field. Pinned on the RENDERED text, so a model added
    later is covered without listing it here: a `<required>` with nothing after it
    is a field the author must guess. Were five (lens name/display_name, entity
    name/source, metric name); display_name now DEFAULTS to name (no longer
    required, so no longer `<required>`), leaving four — `source` the worst, since a
    required NESTED model had no default to read a shape off and printed no keys."""
    root = _init(tmp_path, example=False, warehouse="none")
    bare = re.compile(r"^\s*#\s+\S+:\s+<required>\s*$")
    undescribed = [
        f"{p.relative_to(root)}:{i} {line.strip()}"
        for p in sorted(root.rglob("*"))
        if p.is_file() and ".git" not in p.parts and p.suffix != ".duckdb"
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if bare.match(line)
    ]
    assert undescribed == []
    doc = (root / "semantic/README.md").read_text(encoding="utf-8")
    assert "# source:\n  # connection: <required>" in doc  # the shape, not `<required>`


def test_example_rides_along_with_a_real_warehouse(tmp_path: Path) -> None:
    root = _init(tmp_path, warehouse="snowflake")
    text = (root / "dst.yaml").read_text(encoding="utf-8")
    assert "snowflake:" in text and "jaffle:" in text  # both connections declared
    assert (root / "lenses/customer_value/lens.yaml").exists()


def test_env_file_is_owner_only(tmp_path: Path) -> None:
    """The scaffolded .env holds the admin token, Fernet key and provider keys;
    a shared dev box is the normal early habitat, so init must never leave it
    group/other-readable."""
    root = _init(tmp_path)
    assert (root / ".env").stat().st_mode & 0o777 == 0o600


def test_warehouse_duckdb_scaffolds_own_file(tmp_path: Path) -> None:
    """`--warehouse duckdb` is the "my warehouse IS a duckdb file" path — the one
    warehouse dst bundles as its demo — and without it every project has to
    hand-edit dst.yaml to point at real data."""
    root = _init(tmp_path, warehouse="duckdb", duckdb_path="warehouse/acme.duckdb", example=False)
    y = (root / "dst.yaml").read_text(encoding="utf-8")
    assert "type: duckdb" in y
    assert "warehouse/acme.duckdb" in y
    assert "serve cwd" in y  # the relative-path resolution rule rides the file


def test_agents_md_indexes_every_scaffolded_skill(tmp_path: Path) -> None:
    """AGENTS.md is the scaffold's own index; a skill written to disk but absent
    there doesn't exist for the driver agent."""
    root = _init(tmp_path)
    agents = (root / "AGENTS.md").read_text(encoding="utf-8")
    skills = sorted(p.name for p in (root / ".claude" / "skills").iterdir())
    assert skills, "scaffold should ship skills"
    for skill in skills:
        assert f".claude/skills/{skill}/" in agents, f"{skill} missing from AGENTS.md"


# ── --skills-only: the scaffold is refreshable ───────────────────────────────
# AGENTS.md and the skills are a snapshot taken at init, so a project scaffolded
# before a skill improved keeps the old copy forever and nothing says so:
# improving the scaffold reaches new projects only.


def _refresh(root: Path) -> int:
    ns = argparse.Namespace(dir=str(root), name=None, skills_only=True, yes=True)
    return run_init(ns)


def test_a_current_project_refreshes_to_a_no_op(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    assert _refresh(root) == 0
    assert "Already current" in capsys.readouterr().out


def test_a_stale_skill_is_rewritten_and_named(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    skill = root / ".claude" / "skills" / "dst-context" / "SKILL.md"
    fresh = skill.read_text(encoding="utf-8")
    skill.write_text("an older release's copy\n", encoding="utf-8")
    assert _refresh(root) == 0
    assert skill.read_text(encoding="utf-8") == fresh
    out = capsys.readouterr().out
    assert ".claude/skills/dst-context/SKILL.md" in out
    assert "Refreshed 1 file(s)" in out


def test_a_deleted_skill_comes_back(tmp_path: Path, capsys) -> None:
    root = _init(tmp_path)
    (root / ".claude" / "skills" / "dst-flywheel" / "SKILL.md").unlink()
    assert _refresh(root) == 0
    assert (root / ".claude" / "skills" / "dst-flywheel" / "SKILL.md").exists()
    assert "(new)" in capsys.readouterr().out


def test_refresh_touches_nothing_else(tmp_path: Path) -> None:
    """The one init mode that runs inside a live project: it must not go near
    dst.yaml, .env, or the authored layer."""
    root = _init(tmp_path)
    before = {
        p: p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and ".claude" not in p.parts and p.name != "AGENTS.md"
    }
    assert _refresh(root) == 0
    assert {p: p.read_bytes() for p in before} == before


def test_refresh_refuses_outside_a_project(tmp_path: Path, capsys) -> None:
    assert _refresh(tmp_path / "empty") == 1
    assert "no dst.yaml" in capsys.readouterr().out


def test_every_scaffolded_agent_file_is_refreshable(tmp_path: Path) -> None:
    """The scaffold and the refresh write ONE list — a file written by init but
    absent from `agent_files` would be permanently un-upgradable, which is the
    whole defect."""
    from services.cli.init import agent_files

    root = _init(tmp_path)
    refreshable = {p for p, _c in agent_files(root, "acme", 8000)}
    on_disk = {p for p in (root / ".claude" / "skills").rglob("SKILL.md")} | {root / "AGENTS.md"}
    assert on_disk <= refreshable


# ── the staleness signal ─────────────────────────────────────────────────────
# The refresh only helps a project that knows it needs one, so `plan` and
# `doctor` read the same check.


def test_a_current_project_is_not_flagged(tmp_path: Path) -> None:
    from services.cli.init import stale_agent_note

    assert stale_agent_note(_init(tmp_path)) is None


def test_a_drifted_skill_is_named_with_the_refresh_command(tmp_path: Path) -> None:
    from services.cli.init import stale_agent_note

    root = _init(tmp_path)
    (root / ".claude" / "skills" / "dst-certify" / "SKILL.md").write_text("older copy\n")
    note = stale_agent_note(root)
    assert note is not None
    assert ".claude/skills/dst-certify/SKILL.md" in note
    assert "--skills-only" in note


def test_a_deleted_skill_is_not_nagged_about(tmp_path: Path) -> None:
    """Only files that EXIST are compared: deleting a skill is a choice, and a
    refresh prompt for a file nobody wants is noise on every plan forever."""
    from services.cli.init import stale_agent_note

    root = _init(tmp_path)
    (root / ".claude" / "skills" / "dst-flywheel" / "SKILL.md").unlink()
    assert stale_agent_note(root) is None


def test_a_project_without_a_scaffold_is_silent(tmp_path: Path) -> None:
    from services.cli.init import stale_agent_note

    assert stale_agent_note(tmp_path / "nothing-here") is None


def test_an_ambient_dst_url_does_not_fake_a_stale_scaffold(tmp_path: Path, monkeypatch) -> None:
    """AGENTS.md is rendered from the PROJECT's port. Resolving DST_URL the way a
    credential resolves it — process env first — reported a current scaffold as
    stale for exactly the operators who target a server explicitly
    (`DST_URL=… dst plan`, every CI job). Caught in a real plan run, not here."""
    from services.cli.init import stale_agent_note

    root = _init(tmp_path)
    monkeypatch.setenv("DST_URL", "http://127.0.0.1:8077")
    assert stale_agent_note(root) is None
