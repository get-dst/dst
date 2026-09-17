"""Decision-calibration experiment: which k makes the voting decider honest?

The threshold policy (services/runtime/decision_policy.py) ships PLACEHOLDER
defaults until this runs. Arms: the voting decider at k ∈ {1, 5, 9} over the
router's held-out labelled set (scripts/router_experiment.py — paraphrases
plus uncovered noise, recorded bge-small embeddings for the shortlist so the
only live call is the decider). Per arm: accuracy, Brier, ECE, the coverage
curve. The pick rule, stated before the run: the lowest ECE among arms within
2 pp accuracy of the best sets DEFAULT_K, and the coverage curve at that k
sets the lens policy's act_p (the lowest threshold whose error-among-acted is
at or below the k=1 error rate — the regime must not act worse than today).

A logprobs arm is deliberately NOT here: it needs a wire change on the
openai-compatible provider and it never exists after a reasoning model's
hidden thinking; it becomes a follow-up only if the vote arms are not good
enough. The certified-equivalence slot needs a live certified corpus and is
graded by `dst test` instead.

Run:  uv run python scripts/decision_calibration.py [--k 1,5,9] [--out DIR]
Needs a fast-tier provider key (DST_PROVIDERS). Results are internal — never
in public docs (small n).
"""

from __future__ import annotations

import argparse
import gzip
import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.evals import calibrate  # noqa: E402
from services.evals.calibration import format_report, report  # noqa: E402
from services.llm import registry  # noqa: E402
from services.router import Router  # noqa: E402
from services.router.decider import LlmDecider  # noqa: E402

_FIXTURE = ROOT / "tests" / "fixtures" / "router_experiment_bge.json.gz"


def _experiment():  # noqa: ANN202 — module type
    spec = importlib.util.spec_from_file_location(
        "router_experiment", ROOT / "scripts" / "router_experiment.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("router_experiment", mod)
    spec.loader.exec_module(mod)
    return mod


class FixtureEmbedder:
    dim = 384

    def __init__(self) -> None:
        self._vecs: dict[str, list[float]] = json.loads(gzip.decompress(_FIXTURE.read_bytes()))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vecs[t] for t in texts]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", default="1,5,9")
    ap.add_argument(
        "--typesafe",
        action="store_true",
        help="add the typed-decision provider as an arm (TYPESAFE_KEY in the env)",
    )
    ap.add_argument("--out", default=None, help="directory for the JSON results")
    args = ap.parse_args()
    ks = [int(k) for k in args.k.split(",")]

    fast = registry.resolve(registry.tier("fast"))
    if fast is None:
        print("no fast-tier model resolves — set DST_PROVIDERS with a key", file=sys.stderr)
        return 1
    exp = _experiment()
    profiles = list(exp.PROFILES)
    by_lens = {p.lens: p for p in profiles}
    router = Router(profiles, FixtureEmbedder())
    # Gold: the lens, or "none" for uncovered noise — the decider's `none` option.
    labeled = [(q, gold if gold is not None else None) for q, gold in exp.LABELED]

    results: dict[str, object] = {
        "model": fast.ref,
        "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "n_questions": len(labeled),
        "arms": {},
    }
    all_records = []
    arms: list[tuple[str, LlmDecider, bool]] = [
        (f"k={k}", LlmDecider(fast.llm, fast.name, k=k), False) for k in ks
    ]
    if args.typesafe:
        import os

        from services.llm.typesafe import TypesafeDecider

        key = os.environ.get("TYPESAFE_KEY", "")
        if not key:
            print("--typesafe needs TYPESAFE_KEY in the env", file=sys.stderr)
            return 1
        typed = LlmDecider(None, "", decider=TypesafeDecider(key))
        # Two arms: the cosine shortlist in front of the typed decider (today's
        # router), and the typed decider over every lens with no cosine at all.
        arms.append(("typesafe", typed, False))
        arms.append(("typesafe-all-lenses", typed, True))
    for label, decider, all_lenses in arms:
        started = time.monotonic()
        recs = calibrate.lens_decisions(
            router,
            by_lens,
            decider,  # type: ignore[arg-type]
            labeled,
            concurrency=8,
            all_lenses=all_lenses,
        )
        elapsed = time.monotonic() - started
        all_records += recs
        arm = report(recs)
        results["arms"][label] = {"elapsed_s": round(elapsed, 1), "report": arm}  # type: ignore[index]
        print(f"\n== {label} ({len(recs)} decider decisions, {elapsed:.0f}s) ==")
        print(format_report(arm))

    rep = report(all_records)
    # The pick rule, applied mechanically and printed with the evidence.
    measured = {d: e for d, e in rep.items() if e.get("status") == "measured"}
    baseline = next((e for d, e in rep.items() if ":k=1 [" in d), None)
    pick: dict[str, object] = {"rule": "lowest ECE within 2pp accuracy of the best"}
    if measured:
        best_acc = max(e["accuracy"] for e in measured.values())
        eligible = {d: e for d, e in measured.items() if e["accuracy"] >= best_acc - 0.02}
        chosen = min(eligible, key=lambda d: eligible[d]["ece"])
        pick["decider"] = chosen
        pick["k"] = int(chosen.rsplit("k=", 1)[1].split(" ")[0]) if "k=" in chosen else None
        base_err = 1 - baseline["accuracy"] if baseline else None
        act_p = next(
            (
                c["threshold"]
                for c in eligible[chosen]["curve"]
                if base_err is None or c["error_among_acted"] <= base_err
            ),
            None,
        )
        pick["act_p"] = act_p
        pick["k1_error_rate"] = base_err
    else:
        pick["decider"] = None
        pick["reason"] = "no arm reached the measured threshold (n too small or unmeasured)"
    results["pick"] = pick
    print("\n== pick ==")
    print(json.dumps(pick, indent=2))

    out_dir = Path(args.out) if args.out else None
    if out_dir is not None:
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = out_dir / f"decision-calibration-{stamp}.json"
        path.write_text(json.dumps(results, indent=2))
        print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
