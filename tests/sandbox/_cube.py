"""The sandbox repros replayed through the cube lane.

Each case is the question, the Cube queries and word ledger a frontier lane
answers it with, and what Cube said about them: the meta of the cubes they
read and the SQL Cube compiled for every query the lane ran (its own, and the
ones its checks ran: a reading's counterpart, the figure without an unasked
filter, the rows past a limit). The lane runs that SQL through its own DuckDB
connector on the test's world, so the same rows that graded the typed lane
grade this one, under the same rule.

Recorded once against the sandbox's pinned Cube with
``DST_TEST_SANDBOX_CUBE_URL=http://localhost:4000 DST_TEST_SANDBOX_CUBE_SECRET=…``;
without them every Cube answer replays from ``cube/<case>.json`` and a query
the recording never saw fails loud. The probe facts (where each time
dimension's data ends in the world, the dictionaries the meta lists) are
recorded with it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
import jwt
import yaml

from services.contracts.cube_meta import MetaDoc
from services.contracts.cube_probe import CubeProbe
from services.contracts.cube_query import Binding, Candidate, CubeQuery
from services.contracts.lens_config import LensConfig
from services.contracts.lens_model import Overlay, parse_overlay
from services.contracts.response import QueryResponse
from services.cube.client import CubeClient
from services.cube.context import CubeContext
from services.cube.lens_view import CubeLensView
from services.serve.cube_lane import CubeLane
from tests.sandbox._world import CATALOGUE, TODAY, World

CASES = Path(__file__).parent / "cube"
CLOCK_END = "2026-08-31"  # the last loaded day before the asked-on date


def _renamed(payload: Any) -> Any:
    """The recording in the tests' own names: the sandbox Cube's model names
    its companies differently from these worlds, and
    ``DST_TEST_SANDBOX_CUBE_RENAME=<from>=<to>[,…]`` maps them while recording,
    so the literals Cube compiles match the rows the worlds hold."""
    pairs = [
        p.split("=", 1)
        for p in os.environ.get("DST_TEST_SANDBOX_CUBE_RENAME", "").split(",")
        if "=" in p
    ]
    if not pairs:
        return payload
    text = json.dumps(payload, ensure_ascii=False)
    for old, new in pairs:
        text = text.replace(old, new).replace(old.lower(), new.lower())
    return json.loads(text)


def _key(query: dict[str, Any]) -> str:
    return json.dumps(query, sort_keys=True)


def _contract_shape(meta: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[str]]]:
    """The sandbox converter writes a dimension's ``dictionary`` as its value
    list and ``value_aliases`` as alias/value pairs; the closed schema reads a
    flag and value → aliases. The values become the probe's dictionaries."""
    dictionaries: dict[str, list[str]] = {}
    for c in meta.get("cubes") or []:
        for group in ("measures", "dimensions", "segments"):
            for x in c.get(group) or []:
                dst = (x.get("meta") or {}).get("dst")
                if not isinstance(dst, dict):
                    continue
                if isinstance(dst.get("dictionary"), list):
                    dictionaries[x["name"]] = [str(v) for v in dst["dictionary"]]
                    dst["dictionary"] = True
                if isinstance(dst.get("value_aliases"), list):
                    out: dict[str, list[str]] = {}
                    for a in dst["value_aliases"]:
                        out.setdefault(str(a["value"]), []).append(str(a["alias"]))
                    dst["value_aliases"] = out
    return meta, dictionaries


class _Recorder:
    """The Cube transport for one case: replays the recording, or (recording)
    proxies to the live Cube and keeps every answer."""

    def __init__(self, case: dict[str, Any], live: str | None, secret: str) -> None:
        self.case, self.live, self.secret = case, live, secret
        self.case.setdefault("compiled", {})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/meta"):
            return httpx.Response(200, json=self.case["meta"])
        query = json.loads(request.content)["query"]
        key = _key(query)
        if self.live:
            token = jwt.encode({"exp": int(time.time()) + 600}, self.secret, algorithm="HS256")
            r = httpx.post(
                f"{self.live}/cubejs-api/v1/sql",
                json={"query": query},
                headers={"Authorization": token},
                timeout=60,
            )
            body = r.json()
            sql = body.get("sql") or {}
            self.case["compiled"][key] = _renamed(
                {
                    "status": r.status_code,
                    "body": {"sql": {k: sql[k] for k in ("sql", "aliasNameToMember") if k in sql}}
                    if r.status_code == 200
                    else body,
                }
            )
        hit = self.case["compiled"].get(key)
        if hit is None:
            return httpx.Response(400, json={"error": f"not recorded: {key}"})
        return httpx.Response(hit["status"], json=hit["body"])


def _cubes_of(queries: list[dict[str, Any]], overlay: Overlay) -> set[str]:
    """The cubes the queries read, and every cube a check may reach from
    them: the other readings of an ambiguous term."""
    names: set[str] = set()
    for q in queries:
        cq = CubeQuery.model_validate(q)
        names |= {m.split(".")[0] for m in cq.members()}
    for term in [t for t in overlay.ambiguous if t.readings]:
        members = [m for r in term.readings for m in r.members]
        if any(m.split(".")[0] in names for m in members):
            names |= {m.split(".")[0] for m in members}
    return names


def _live_meta(live: str, secret: str, cubes: set[str]) -> dict[str, Any]:
    token = jwt.encode({"exp": int(time.time()) + 600}, secret, algorithm="HS256")
    meta = httpx.get(
        f"{live}/cubejs-api/v1/meta", headers={"Authorization": token}, timeout=60
    ).json()
    keep = [c for c in meta["cubes"] if c["name"] in cubes]
    for c in keep:
        c.pop("connectedComponent", None)
    return _renamed({"cubes": keep})


def _probe(lane: CubeLane, cubes: set[str]) -> dict[str, Any]:
    """Where each used cube's time dimensions end in this world, read the way
    the probe reads them: through Cube, one ordered dimension query each."""
    out: dict[str, Any] = {}
    for cube in sorted(cubes):
        dims: dict[str, Any] = {}
        for td in lane.view.time_dimensions(cube):
            try:
                rows = lane.run(CubeQuery(dimensions=[td], order=[(td, "desc")], limit=1))
            except Exception:  # noqa: BLE001 — this world holds no table for that cube
                continue
            last = next((str(r.get(td))[:10] for r in rows if r.get(td)), None)
            dims[td.split(".", 1)[1]] = {
                "kind": "event",
                "data_end": min(last, CLOCK_END) if last else None,
                "max": last,
                "future_rows": 1 if last and last > CLOCK_END else 0,
            }
        out[cube] = {"time_dimensions": dims}
    return out


def _lens_config(lens: str) -> LensConfig:
    """The catalogue lens's refusals (not_computable, not_for): the
    question-level checks read them whatever the serving core."""
    raw = yaml.safe_load((CATALOGUE / f"lenses/{lens}/lens.yaml").read_text())
    return LensConfig(
        name=lens,
        not_computable=raw.get("not_computable") or [],
        not_for=raw.get("not_for") or [],
        timezone=raw.get("timezone") or "",
    )


def ask_cube(
    case: str,
    question: str,
    queries: list[dict[str, Any]],
    ledger: list[tuple[str, str, str]],
    world: World,
    *,
    lens: str,
    note: str = "",
) -> QueryResponse:
    """The question through the cube lane on the test's world, replaying (or
    recording) what Cube compiled."""
    path = CASES / f"{case}.json"
    live = os.environ.get("DST_TEST_SANDBOX_CUBE_URL")
    secret = os.environ.get("DST_TEST_SANDBOX_CUBE_SECRET", "")
    recorded: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    data: dict[str, Any] = {"question": question, "queries": queries}
    overlay = parse_overlay((CASES / "overlay.yaml").read_text(), "cube/overlay.yaml")
    if live:
        data["meta"] = _live_meta(live, secret, _cubes_of(queries, overlay))
        data["compiled"] = {}
    else:
        assert recorded, f"{path.name} is not recorded: run with DST_TEST_SANDBOX_CUBE_URL set"
        assert recorded["queries"] == queries, f"{path.name} records other queries"
        data = recorded
    meta, dictionaries = _contract_shape(json.loads(json.dumps(data["meta"])))
    transport = _Recorder(data, live, secret)
    probe = {"clock_end": CLOCK_END, "dictionaries": dictionaries, "cubes": data.get("probe", {})}
    lane = CubeLane(
        lens=lens,
        config=_lens_config(lens),
        view=CubeLensView(
            doc=MetaDoc.parse(meta), overlay=overlay, probe=CubeProbe.model_validate(probe)
        ),
        client=CubeClient(
            "http://cube.sandbox", secret or "replay", transport=httpx.MockTransport(transport)
        ),
        ctx=CubeContext.operator(lens=lens),
        connector=world.connector(),
        dialect="duckdb",
        today=TODAY,
        org_id="org-sandbox",
        caller="analyst",
        request_id="req_sandbox",
    )
    if live:
        data["probe"] = _probe(lane, _cubes_of(queries, overlay))
        lane.view = CubeLensView(
            doc=lane.view.doc,
            overlay=overlay,
            probe=CubeProbe.model_validate({**probe, "cubes": data["probe"]}),
        )
        lane.executed.clear()
    # A word the lane mapped to nothing is not a binding (the frontier lane
    # drops it the same way): the word check reads it as uncovered.
    led = [
        Binding(word=w, kind=k, target=t, source="recorded")  # type: ignore[arg-type]
        for w, k, t in ledger
        if k != "none"
    ]
    candidates = [
        Candidate(query=CubeQuery.model_validate(q), ledger=led, lane="frontier") for q in queries
    ]
    resp, _trace = lane.serve(question, candidates, ledger=led, lane="frontier", note=note)
    if os.environ.get("DST_TEST_SANDBOX_CUBE_SHOW"):
        print(f"\n[{case}] {resp.status} {resp.mode}: {resp.answer[:600]}")
        if resp.data is not None:
            print("  ", resp.data.columns, resp.data.rows[:8])
    if live:
        CASES.mkdir(exist_ok=True)
        path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    return resp


def cell(resp: QueryResponse, column: str, row: int = 0) -> Any:
    assert resp.data is not None, resp.answer
    return resp.data.rows[row][resp.data.columns.index(column)]


def column(resp: QueryResponse, name: str) -> list[Any]:
    assert resp.data is not None, resp.answer
    i = resp.data.columns.index(name)
    return [r[i] for r in resp.data.rows]
