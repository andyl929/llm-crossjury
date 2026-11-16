"""M1.7 eval: GPQA Diamond and SimpleQA through crossjury (MANUAL; real calls).

NOT collected by pytest. With ``--live`` it makes real calls: subscription CLI calls
(unknown cost, counted against your plans' usage limits) and billed API calls (checked
against ``--max-usd``). Without ``--live`` it only downloads the data and prints the
estimate.

Results are appended to ``<out-dir>/results.jsonl`` (one line per question) and a rerun
skips finished questions. Turn them into tables with ``scripts/eval/report.py``.

Usage::

    uv run python scripts/eval/run.py                       # estimate only
    uv run python scripts/eval/run.py --live --yes --n 3    # pilot
    uv run python scripts/eval/run.py --live --yes          # full run (N=50 each)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from crossjury_eval import data
from crossjury_eval.data import EvalItem
from crossjury_eval.runner import (
    DEFAULT_CHAIR,
    DEFAULT_GRADER,
    DEFAULT_MEMBERS,
    EvalConfig,
    EvalLedger,
    ModelSpec,
    ResultWriter,
    estimate_question,
    make_grader,
    pending_items,
    prior_spend,
    question_key,
    read_records,
    run_items,
    with_eval_ledger,
)
from llm_crossjury.core.compare_service import CompareDeps, build_default_deps, key_is_set
from llm_crossjury.core.cost.ledger import SqliteLedger
from llm_crossjury.core.keys import PROVIDER_KEY_ENVS, load_all_keys
from llm_crossjury.core.providers.base import Backend
from llm_crossjury.core.storage import resolve_data_paths


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--live", action="store_true", help="Required for any model call.")
    parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt.")
    parser.add_argument("--datasets", default="gpqa,simpleqa", help="Comma list (gpqa,simpleqa).")
    parser.add_argument("--n", type=int, default=50, help="Questions per dataset (default 50).")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument(
        "--member",
        action="append",
        metavar="PROVIDER:BACKEND:MODEL",
        help="Member (repeatable). Default: " + ", ".join(str(m) for m in DEFAULT_MEMBERS),
    )
    parser.add_argument("--chair", default=str(DEFAULT_CHAIR), metavar="PROVIDER:BACKEND:MODEL")
    parser.add_argument("--grader", default=str(DEFAULT_GRADER), metavar="PROVIDER:BACKEND:MODEL")
    parser.add_argument(
        "--review",
        choices=("always", "auto", "never"),
        default="always",
        help="Peer review: always, never, or auto (skip when the agreement ratio is at "
        "least the product's skip threshold).",
    )
    parser.add_argument("--member-max-output", type=int, default=16_000)
    parser.add_argument("--max-usd", type=Decimal, default=Decimal(8), help="Hard cap (API spend).")
    parser.add_argument("--concurrency", type=int, default=2, help="Questions at once.")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--keys-dir",
        type=Path,
        default=None,
        help="Extra directory to load .env / .<provider>.key files from (as build_default_deps "
        "does for the working directory; existing variables win).",
    )
    parser.add_argument(
        "--retry-failures",
        action="store_true",
        help="Also rerun scored questions where a member or the synthesis failed.",
    )
    return parser.parse_args(argv)


def _inside_git_tree(path: Path) -> bool:
    return any((parent / ".git").exists() for parent in (path, *path.parents))


def _out_dir(args: argparse.Namespace) -> Path:
    if args.out_dir is not None:
        out: Path = args.out_dir.resolve()
    else:
        out = resolve_data_paths().root / "evals" / f"m17-n{args.n}-seed{args.seed}"
    if _inside_git_tree(out):
        raise SystemExit(f"refusing an output directory inside a git working tree: {out}")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _config(args: argparse.Namespace) -> EvalConfig:
    members = tuple(ModelSpec.parse(m) for m in args.member) if args.member else DEFAULT_MEMBERS
    return EvalConfig(
        members=members,
        chair=ModelSpec.parse(args.chair),
        grader=ModelSpec.parse(args.grader),
        seed=args.seed,
        review=args.review,
        member_max_output_tokens=args.member_max_output,
        max_usd=args.max_usd,
        concurrency=args.concurrency,
    )


async def _cli_versions(cfg: EvalConfig, deps: CompareDeps) -> dict[str, dict[str, Any]]:
    providers = {
        spec.provider
        for spec in (*cfg.members, cfg.chair, cfg.grader)
        if spec.backend is Backend.CLI
    }
    report: dict[str, dict[str, Any]] = {}
    for provider in sorted(providers):
        status = await deps.providers[(provider, Backend.CLI)].check()
        report[provider] = {
            "available": status.available,
            "version": status.version,
            "detail": status.detail,
        }
    return report


def _progress(total: int) -> Any:
    done = 0

    def show(record: dict[str, Any]) -> None:
        nonlocal done
        done += 1
        synthesis = record.get("synthesis") or {}
        members = record.get("members") or []
        ok_members = sum(1 for m in members if m.get("ok"))
        print(
            f"[{done}/{total}] {record['dataset']}:{record['item_id']} status={record['status']} "
            f"members_ok={ok_members}/{len(members)} synthesis_ok={synthesis.get('ok')} "
            f"correct={synthesis.get('correct')} wall={record.get('wall_s')}s "
            f"cost=USD {record.get('cost_usd')} cli_calls={record.get('cli_calls')}"
            + (f" error={record['error']}" if record.get("error") else ""),
            flush=True,
        )

    return show


class _AbortError(Exception):
    """Stop before any model call, with a process exit code."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _datasets(args: argparse.Namespace) -> list[data.Dataset]:
    chosen: list[data.Dataset] = []
    for name in (d.strip() for d in args.datasets.split(",")):
        match = next((d for d in data.DATASETS if d == name), None)
        if match is None:
            raise _AbortError(f"unknown dataset {name!r}", 2)
        chosen.append(match)
    return chosen


def _check_keys(cfg: EvalConfig, deps: CompareDeps, live: bool) -> None:
    for spec in (*cfg.members, cfg.chair, cfg.grader):
        if spec.backend is Backend.API:
            state = "set" if key_is_set(deps.key_envs[spec.provider]) else "not set"
            print(f"{spec.provider} API key: {state}")
            if state == "not set" and live:
                raise _AbortError(f"the {spec.provider} api model needs its key", 2)


async def _estimate(
    pending: list[EvalItem], cfg: EvalConfig, deps: CompareDeps
) -> tuple[Decimal, Decimal]:
    low = high = Decimal(0)
    for item in pending:
        item_low, item_high = await estimate_question(item, cfg, deps)
        low += item_low
        high += item_high
    return low, high


async def _preflight(cfg: EvalConfig, deps: CompareDeps, confirmed: bool) -> dict[str, Any]:
    versions = await _cli_versions(cfg, deps)
    for provider, status in versions.items():
        print(f"{provider} cli: {status['detail']}")
    unavailable = [p for p, s in versions.items() if not s["available"]]
    if unavailable:
        raise _AbortError(f"CLI not available: {', '.join(unavailable)}", 2)
    if not confirmed:
        answer = input("Run now? Subscription CLI calls count against your plan limits [y/N] ")
        if answer.strip().lower() not in {"y", "yes"}:
            raise _AbortError("not confirmed", 1)
    return versions


async def _main(args: argparse.Namespace) -> int:
    cfg = _config(args)
    datasets = _datasets(args)
    out = _out_dir(args)
    results_path = out / "results.jsonl"
    if args.keys_dir is not None:
        load_all_keys(args.keys_dir.resolve(), PROVIDER_KEY_ENVS)
    deps = build_default_deps(Path.cwd())
    _check_keys(cfg, deps, args.live)

    items = [item for name in datasets for item in data.load_items(name, n=args.n, seed=args.seed)]
    records = read_records(results_path)
    prior = prior_spend(records)
    pending = pending_items(items, records, retry_failures=args.retry_failures)
    ledger = EvalLedger(SqliteLedger(out / "ledger.sqlite3"), max_usd=cfg.max_usd, prior=prior)
    deps = with_eval_ledger(deps, ledger, cfg.max_usd)
    low, high = await _estimate(pending, cfg, deps)
    finished = len(items) - len(pending)
    print(f"output dir: {out}")
    print(f"questions: {len(items)} sampled, {finished} finished, {len(pending)} to run")
    print(f"members: {', '.join(str(m) for m in cfg.members)}")
    print(f"chair: {cfg.chair}; grader: {cfg.grader}; review: {cfg.review}")
    print(
        f"priced (API) estimate for the remaining questions: USD {low:.2f} - {high:.2f} "
        f"(already spent: USD {prior:.4f}; cap: USD {cfg.max_usd})"
    )
    if prior + high > cfg.max_usd:
        raise _AbortError(
            "the high estimate plus the spend so far exceeds --max-usd. Lower --n or "
            "--member-max-output, or raise --max-usd.",
            3,
        )
    if not args.live or not pending:
        print("estimate only (pass --live to run)." if not args.live else "nothing to run.")
        return 0

    versions = await _preflight(cfg, deps, args.yes)
    manifest = {
        "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "config": cfg.manifest(),
        "datasets": datasets,
        "n_per_dataset": args.n,
        "sources": {"gpqa": data.GPQA_ZIP_URL, "simpleqa": data.SIMPLEQA_URL},
        "cli_versions": versions,
        "estimate_usd": {"low": str(low), "high": str(high)},
        "questions": [{"key": question_key(i), "qhash": i.qhash} for i in items],
    }
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (out / f"manifest-{stamp}.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    grade = make_grader(deps, cfg.grader) if "simpleqa" in datasets else None
    results = await run_items(
        pending,
        cfg,
        deps,
        ledger,
        ResultWriter(results_path),
        grade=grade,
        progress=_progress(len(pending)),
    )
    stopped = any(r.get("status") == "budget_stopped" for r in results)
    print(
        f"done: {len(results)} question(s) this session; priced spend this session "
        f"USD {ledger.session_spent:.4f}, total USD {ledger.total_spent:.4f}"
        + ("; STOPPED by the budget cap" if stopped else "")
    )
    print(f'report: uv run python scripts/eval/report.py --out-dir "{out}"')
    return 4 if stopped else 0


def main(argv: list[str] | None = None) -> int:
    """Entry point."""
    try:
        return asyncio.run(_main(_parse_args(argv)))
    except _AbortError as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
