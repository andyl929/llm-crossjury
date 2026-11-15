"""Aggregate JSONL records into per-dataset metrics and a Markdown report.

Only aggregate numbers, model names, ids and hashes appear in the output; never
question or answer text.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from crossjury_eval.scoring import FlagStats, accuracy, flag_stats

Record = Mapping[str, Any]
FlagRule = Callable[[Mapping[str, Any]], bool]

HEADLINE_FLAG = "disputed_or_possibly_wrong"


def _ratio_below(threshold: float) -> FlagRule:
    def rule(s: Mapping[str, Any]) -> bool:
        ratio = s.get("agreement_ratio")
        return ratio is not None and float(ratio) < threshold

    return rule


FLAG_RULES: dict[str, FlagRule] = {
    HEADLINE_FLAG: lambda s: bool(s.get("n_disputed")) or bool(s.get("n_possibly_wrong")),
    "chair_disputed": lambda s: bool(s.get("n_disputed")),
    "review_possibly_wrong": lambda s: bool(s.get("n_possibly_wrong")),
    "agreement_disputed_groups": lambda s: bool(s.get("agreement_disputed_groups")),
    "agreement_ratio_below_0.8": _ratio_below(0.8),
}
"""Question-level flag definitions over the synthesis record. The headline flag is what
the UI shows under the final answer: any disputed point or any possibly-wrong item."""


def latest_records(records: Iterable[Record]) -> dict[str, Record]:
    """One record per question: the last ``ok`` one, else the last one."""
    chosen: dict[str, Record] = {}
    for record in records:
        key = f"{record.get('dataset')}:{record.get('item_id')}"
        current = chosen.get(key)
        if current is None or record.get("status") == "ok" or current.get("status") != "ok":
            chosen[key] = record
    return chosen


def _member_name(member: Mapping[str, Any]) -> str:
    return f"{member['provider']}/{member['model']} ({member['backend']})"


def _flag_dict(stats: FlagStats) -> dict[str, Any]:
    return {
        "total": stats.total,
        "flagged": stats.flagged,
        "coverage": stats.coverage,
        "flagged_error_rate": stats.flagged_error_rate,
        "unflagged_error_rate": stats.unflagged_error_rate,
        "error_recall": stats.error_recall,
        "lift": stats.lift,
        "flagged_errors": stats.flagged_errors,
        "unflagged_errors": stats.unflagged_errors,
    }


def _wall_stats(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"mean": None, "median": None, "max": None, "sum": 0.0}
    return {
        "mean": round(statistics.fmean(values), 1),
        "median": round(statistics.median(values), 1),
        "max": round(max(values), 1),
        "sum": round(sum(values), 1),
    }


def summarize_dataset(
    dataset: str, records: Sequence[Record], attempts: Sequence[Record]
) -> dict[str, Any]:
    """Metrics of one dataset.

    Args:
        dataset: Dataset name.
        records: Latest record per question of this dataset.
        attempts: Every record of this dataset (for cost and failure counts).
    """
    scored = [r for r in records if r.get("status") == "ok"]
    members: dict[str, list[bool]] = {}
    member_failures: dict[str, int] = {}
    for record in scored:
        for member in record.get("members", []):
            name = _member_name(member)
            members.setdefault(name, []).append(bool(member.get("correct")))
            if not member.get("ok"):
                member_failures[name] = member_failures.get(name, 0) + 1
    member_acc = {name: accuracy(values) for name, values in members.items()}
    best_name = max(member_acc, key=lambda n: member_acc[n] or 0.0) if member_acc else None
    synth_correct = [bool(r["synthesis"].get("correct")) for r in scored]
    majority_correct = [bool(r.get("majority", {}).get("correct")) for r in scored]
    synth_ok = [r for r in scored if r["synthesis"].get("ok")]

    flags = {
        name: _flag_dict(
            flag_stats(
                [(rule(r["synthesis"]), bool(r["synthesis"].get("correct"))) for r in synth_ok]
            )
        )
        for name, rule in FLAG_RULES.items()
    }

    best_correct = (
        [
            bool(next(m for m in r["members"] if _member_name(m) == best_name).get("correct"))
            for r in scored
        ]
        if best_name
        else []
    )
    head_to_head = {
        "synthesis_right_majority_wrong": sum(
            1 for s, m in zip(synth_correct, majority_correct, strict=True) if s and not m
        ),
        "synthesis_wrong_majority_right": sum(
            1 for s, m in zip(synth_correct, majority_correct, strict=True) if m and not s
        ),
        "synthesis_right_best_wrong": sum(
            1 for s, b in zip(synth_correct, best_correct, strict=True) if s and not b
        ),
        "synthesis_wrong_best_right": sum(
            1 for s, b in zip(synth_correct, best_correct, strict=True) if b and not s
        ),
    }

    grades: dict[str, dict[str, int]] = {}
    if dataset == "simpleqa":
        for record in scored:
            for member in record.get("members", []):
                bucket = grades.setdefault(_member_name(member), {})
                label = str(member.get("answer") or "no_answer")
                bucket[label] = bucket.get(label, 0) + 1
            bucket = grades.setdefault("crossjury synthesis", {})
            label = str(record["synthesis"].get("answer") or "no_answer")
            bucket[label] = bucket.get(label, 0) + 1

    costs = [Decimal(str(r.get("cost_usd") or 0)) for r in scored]
    walls = [float(r.get("wall_s") or 0.0) for r in scored]
    return {
        "dataset": dataset,
        "questions": len(records),
        "scored": len(scored),
        "unscored": {
            status: sum(1 for r in records if r.get("status") == status)
            for status in ("error", "budget_stopped")
        },
        "members": {
            name: {
                "accuracy": member_acc[name],
                "n": len(members[name]),
                "failed_calls": member_failures.get(name, 0),
            }
            for name in members
        },
        "best_single": {
            "member": best_name,
            "accuracy": member_acc.get(best_name) if best_name else None,
        },
        "majority_vote": {"accuracy": accuracy(majority_correct)},
        "synthesis": {
            "accuracy": accuracy(synth_correct),
            "failed": len(scored) - len(synth_ok),
            "accuracy_when_ok": accuracy([bool(r["synthesis"].get("correct")) for r in synth_ok]),
        },
        "head_to_head": head_to_head,
        "flags": flags,
        "simpleqa_grades": grades,
        "cost": {
            "priced_usd_all_attempts": str(
                sum((Decimal(str(r.get("cost_usd") or 0)) for r in attempts), Decimal(0))
            ),
            "priced_usd_per_scored_question": (
                str((sum(costs, Decimal(0)) / len(costs)).quantize(Decimal("0.0001")))
                if costs
                else None
            ),
            "cli_calls": sum(int(r.get("cli_calls") or 0) for r in attempts),
            "api_calls": sum(int(r.get("api_calls") or 0) for r in attempts),
            "attempts": len(attempts),
        },
        "wall_s": _wall_stats(walls),
    }


def summarize(
    records: Sequence[Record], *, project_n: int | None = None, concurrency: int = 2
) -> dict[str, Any]:
    """Per-dataset metrics, totals and (optionally) a projection to ``project_n``
    questions per dataset at ``concurrency`` questions at once."""
    latest = latest_records(records)
    datasets = sorted({str(r.get("dataset")) for r in latest.values()})
    per: dict[str, Any] = {}
    for dataset in datasets:
        per[dataset] = summarize_dataset(
            dataset,
            [r for r in latest.values() if r.get("dataset") == dataset],
            [r for r in records if r.get("dataset") == dataset],
        )
    summary: dict[str, Any] = {
        "datasets": per,
        "total_priced_usd": str(
            sum((Decimal(str(r.get("cost_usd") or 0)) for r in records), Decimal(0))
        ),
        "total_cli_calls": sum(int(r.get("cli_calls") or 0) for r in records),
    }
    if project_n is not None:
        projection: dict[str, Any] = {}
        total_cost = Decimal(0)
        total_hours = 0.0
        for dataset, data in per.items():
            per_q = data["cost"]["priced_usd_per_scored_question"]
            mean_wall = data["wall_s"]["mean"]
            cost = Decimal(per_q) * project_n if per_q is not None else None
            hours = (
                mean_wall * project_n / max(1, concurrency) / 3600
                if mean_wall is not None
                else None
            )
            projection[dataset] = {
                "usd": None if cost is None else str(cost.quantize(Decimal("0.01"))),
                "hours": None if hours is None else round(hours, 2),
            }
            total_cost += cost or Decimal(0)
            total_hours += hours or 0.0
        summary["projection"] = {
            "n_per_dataset": project_n,
            "concurrency": concurrency,
            "by_dataset": projection,
            "usd": str(total_cost.quantize(Decimal("0.01"))),
            "hours": round(total_hours, 2),
        }
    return summary


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def to_markdown(summary: Mapping[str, Any]) -> str:
    """Render :func:`summarize` output as Markdown tables."""
    lines: list[str] = ["# Eval results", ""]
    for dataset, data in summary["datasets"].items():
        lines += [
            f"## {dataset}",
            "",
            f"Questions: {data['questions']} (scored {data['scored']}, "
            f"errors {data['unscored']['error']}, budget-stopped "
            f"{data['unscored']['budget_stopped']})",
            "",
            "| System | Accuracy | n |",
            "|---|---:|---:|",
        ]
        for name, member in data["members"].items():
            failed = f" ({member['failed_calls']} failed calls)" if member["failed_calls"] else ""
            lines.append(f"| {name}{failed} | {_pct(member['accuracy'])} | {member['n']} |")
        best = data["best_single"]
        lines += [
            f"| Best single member: {best['member']} | {_pct(best['accuracy'])} | "
            f"{data['scored']} |",
            f"| Majority vote of members | {_pct(data['majority_vote']['accuracy'])} | "
            f"{data['scored']} |",
            f"| **crossjury synthesis** ({data['synthesis']['failed']} failed) | "
            f"**{_pct(data['synthesis']['accuracy'])}** | {data['scored']} |",
            "",
            "Flag quality (questions with a synthesis; error = synthesis answer not correct):",
            "",
            "| Flag rule | Coverage | Error rate flagged | Error rate unflagged | "
            "Errors caught | n |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for name, flag in data["flags"].items():
            label = f"**{name}**" if name == HEADLINE_FLAG else name
            lines.append(
                f"| {label} | {_pct(flag['coverage'])} ({flag['flagged']}) | "
                f"{_pct(flag['flagged_error_rate'])} | {_pct(flag['unflagged_error_rate'])} | "
                f"{_pct(flag['error_recall'])} | {flag['total']} |"
            )
        h2h = data["head_to_head"]
        lines += [
            "",
            f"Synthesis right where majority wrong: {h2h['synthesis_right_majority_wrong']}; "
            f"synthesis wrong where majority right: {h2h['synthesis_wrong_majority_right']}; "
            f"synthesis right where best member wrong: {h2h['synthesis_right_best_wrong']}; "
            f"synthesis wrong where best member right: {h2h['synthesis_wrong_best_right']}.",
            "",
        ]
        if data["simpleqa_grades"]:
            lines += ["| System | Grades |", "|---|---|"]
            for name, counts in data["simpleqa_grades"].items():
                text = ", ".join(f"{k}: {v}" for k, v in sorted(counts.items()))
                lines.append(f"| {name} | {text} |")
            lines.append("")
        cost, wall = data["cost"], data["wall_s"]
        lines += [
            f"Cost: USD {cost['priced_usd_all_attempts']} priced (API) over "
            f"{cost['attempts']} attempts, {cost['priced_usd_per_scored_question'] or 'n/a'} "
            f"per scored question; {cost['api_calls']} API calls, {cost['cli_calls']} CLI "
            "(subscription) calls.",
            f"Wall time per question: mean {wall['mean']} s, median {wall['median']} s, "
            f"max {wall['max']} s.",
            "",
        ]
    lines.append(
        f"Total priced spend: USD {summary['total_priced_usd']}; CLI calls: "
        f"{summary['total_cli_calls']}."
    )
    projection = summary.get("projection")
    if projection:
        lines += [
            "",
            f"Projection for {projection['n_per_dataset']} questions per dataset at "
            f"concurrency {projection['concurrency']}: USD {projection['usd']}, "
            f"about {projection['hours']} h.",
        ]
    return "\n".join(lines) + "\n"
