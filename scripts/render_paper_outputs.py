#!/usr/bin/env python3
import csv
import json
import math
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from lib.result_common import (
    aggregate_mode_stats,
    final_coverage_at,
    merge_bug_label_counts,
    merge_mode_jsons,
    merge_mode_trial_counts,
)


ModePairs = Dict[str, List[Tuple[str, List[Tuple[float, int]]]]]


TARGET_DISPLAY = {
    "redis": "Redis",
    "mongo": "MongoDB",
    "mysql": "MySQL",
    "etcd": "ETCD",
    "nuraft": "Nuraft",
    "braft": "Braft",
    "redis_raft": "Redis Raft",
}


FIGURE_BY_TARGET = {
    "redis": "figure8",
    "etcd": "figure8",
    "mongo": "figure8",
    "mysql": "figure9",
    "nuraft": "figure9",
    "braft": "figure9",
}


PANEL_BY_TARGET_SCHEDULER = {
    ("redis", "random"): "a",
    ("redis", "pct"): "b",
    ("etcd", "random"): "c",
    ("etcd", "pct"): "d",
    ("mongo", "random"): "e",
    ("mongo", "pct"): "f",
    ("mysql", "random"): "a",
    ("mysql", "pct"): "b",
    ("nuraft", "random"): "c",
    ("nuraft", "pct"): "d",
    ("braft", "random"): "e",
    ("braft", "pct"): "f",
}


PAPER_Y_RANGES = {
    "redis": (2000.0, 6000.0, 1.0, 3),
    "etcd": (9000.0, 15000.0, 1.0, 3),
    "mongo": (60000.0, 120000.0, 10.0, 3),
    "mysql": (16000.0, 24000.0, 1.0, 3),
    "nuraft": (2000.0, 4100.0, 1.0, 3),
    "braft": (0.0, 2100.0, 1.0, 3),
}


DISTFUZZ_Y_RANGES = {
    "etcd": (0.0, 10000.0, 5.0, 3),
    "nuraft": (0.0, 4000.0, 1.0, 3),
    "redis_raft": (0.0, 2000.0, 1.0, 3),
    "braft": (0.0, 2000.0, 1.0, 3),
}


TABLE4_BUGS_BY_TARGET = {
    "redis": ["Redis-1", "Redis-2"],
    "mongo": ["Mongo-1"],
    "mysql": ["MySQL-3", "MySQL-4"],
    "etcd": ["ETCD-1"],
    "nuraft": ["Nuraft-1"],
    "braft": [],
}


@dataclass(frozen=True)
class LineSpec:
    key: str
    label: str
    color: str
    marker: str


@dataclass(frozen=True)
class PaperContext:
    analysis_dir: Path
    input_dir: Path
    json_files: List[Path]
    max_hours: float
    bin_minutes: float


def compute_hour_ticks(max_hours: float) -> List[float]:
    if max_hours <= 0:
        return []
    step = max_hours / 4.0
    out = [0.0]
    for i in range(1, 5):
        t = step * i
        out.append(float(int(round(t))) if abs(t - round(t)) < 1e-9 else t)
    return out


def format_tick(t: float) -> str:
    if abs(t - round(t)) < 1e-9:
        return str(int(round(t)))
    return f"{t:.2f}".rstrip("0").rstrip(".")


def load_context(
    analysis_dir: Path,
    *,
    max_hours: Optional[float] = None,
    bin_minutes: Optional[float] = None,
) -> PaperContext:
    metadata_path = analysis_dir / "analysis_metadata.json"
    if not metadata_path.is_file():
        raise FileNotFoundError(f"missing analysis metadata: {metadata_path}")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    input_dir = Path(metadata["input_dir"])
    raw_jsons = metadata.get("json_files")
    if raw_jsons:
        json_files = [Path(p) for p in raw_jsons]
    else:
        json_files = sorted(p for p in input_dir.rglob("*.json") if p.is_file())
    if not json_files:
        raise ValueError(f"no collected JSON files found for {analysis_dir}")

    resolved_max_hours = max_hours if max_hours is not None else metadata.get("max_hours")
    if resolved_max_hours is None:
        resolved_max_hours = 24.0
    resolved_bin_minutes = bin_minutes if bin_minutes is not None else metadata.get("bin_minutes", 5.0)

    return PaperContext(
        analysis_dir=analysis_dir,
        input_dir=input_dir,
        json_files=json_files,
        max_hours=float(resolved_max_hours),
        bin_minutes=float(resolved_bin_minutes),
    )


def load_merged(ctx: PaperContext) -> ModePairs:
    return merge_mode_jsons(ctx.json_files)


def resolve_mode(merged: ModePairs, label: str, candidates: Sequence[str]) -> str:
    matches = [candidate for candidate in candidates if candidate in merged]
    if not matches:
        raise ValueError(f"missing mode for {label}; tried: {', '.join(candidates)}")
    nonempty = [key for key in matches if merged.get(key)]
    if not nonempty:
        raise ValueError(f"mode for {label} exists but has zero collected trials: {', '.join(matches)}")
    if len(nonempty) > 1:
        raise ValueError(f"ambiguous mode for {label}: {', '.join(nonempty)}")
    return nonempty[0]


def ablation_specs(target: str, scheduler: str, merged: ModePairs) -> List[LineSpec]:
    prefix = "R" if scheduler == "random" else "P"
    old_base = {
        f"{prefix}-ISM": ["mut_all", f"{target}_mut_all"],
        f"{prefix}-IM": ["mut_inp", f"{target}_mut_inp"],
        prefix: ["rand", f"{target}_rand"],
    }
    candidates = {
        f"{prefix}-ISM": [f"{target}_{prefix}-ISM", f"{target}_{scheduler}_mut_all"] + old_base[f"{prefix}-ISM"],
        f"{prefix}-IM": [f"{target}_{prefix}-IM", f"{target}_{scheduler}_mut_inp"] + old_base[f"{prefix}-IM"],
        prefix: [f"{target}_{prefix}", f"{target}_{scheduler}_rand"] + old_base[prefix],
    }
    return [
        LineSpec(resolve_mode(merged, f"{target} {prefix}-ISM", candidates[f"{prefix}-ISM"]), f"{prefix}-ISM", "C2", "o"),
        LineSpec(resolve_mode(merged, f"{target} {prefix}-IM", candidates[f"{prefix}-IM"]), f"{prefix}-IM", "C0", "^"),
        LineSpec(resolve_mode(merged, f"{target} {prefix}", candidates[prefix]), prefix, "C1", "s"),
    ]


def distfuzz_specs(target: str, merged: ModePairs) -> List[LineSpec]:
    candidates = {
        "etcd": {
            "Distfuzz": ["distfuzz_etcd", "etcd_distfuzz"],
            "Tamias": ["distfuzz_etcd_mut_all", "etcd_mut_all", "etcd_P-ISM"],
        },
        "nuraft": {
            "Distfuzz": ["distfuzz_nuraft", "nuraft_distfuzz"],
            "Tamias": ["nuraft_mut_all", "distfuzz_nuraft_mut_all", "nuraft_P-ISM"],
        },
        "redis_raft": {
            "Distfuzz": ["distfuzz_redis_raft", "redis_raft_distfuzz"],
            "Tamias": ["redis_raft_mut_all", "distfuzz_redis_raft_mut_all", "redis_raft_P-ISM"],
        },
        "braft": {
            "Distfuzz": ["distfuzz_braft", "braft_distfuzz"],
            "Tamias": ["braft_mut_all", "distfuzz_braft_mut_all", "braft_P-ISM"],
        },
    }
    if target not in candidates:
        raise ValueError(f"unsupported DistFuzz target: {target}")
    return [
        LineSpec(resolve_mode(merged, f"{target} Distfuzz", candidates[target]["Distfuzz"]), "Distfuzz", "C2", "o"),
        LineSpec(resolve_mode(merged, f"{target} Tamias", candidates[target]["Tamias"]), "Tamias", "C0", "^"),
    ]


def marker_positions(x: Sequence[float], desired_count: int) -> Any:
    if desired_count <= 0:
        return None
    if len(x) <= desired_count:
        return 1
    step = max(1, len(x) // desired_count)
    return list(range(0, len(x), step))


def all_values_for_specs(
    merged: ModePairs,
    specs: Sequence[LineSpec],
    *,
    max_hours: float,
    bin_minutes: float,
) -> List[float]:
    vals: List[float] = []
    for spec in specs:
        x, mean_y, min_y, max_y = aggregate_mode_stats(
            merged[spec.key],
            bin_minutes=bin_minutes,
            max_hours=max_hours,
        )
        if x:
            vals.extend(mean_y)
            vals.extend(min_y)
            vals.extend(max_y)
    return [v for v in vals if math.isfinite(v)]


def expanded_y_range(
    target: str,
    values: Sequence[float],
    *,
    distfuzz: bool = False,
) -> Tuple[float, float, float, int, bool]:
    ranges = DISTFUZZ_Y_RANGES if distfuzz else PAPER_Y_RANGES
    ymin, ymax, step_k, ticks_count = ranges.get(target, (0.0, max(values or [1.0]), 1.0, 3))
    if not values:
        return ymin, ymax, step_k, ticks_count, False
    data_max = max(values)
    expanded = False
    if data_max > ymax:
        expanded = True
        pad = max((data_max - ymin) * 0.08, 100.0)
        ymax = max(ymax, data_max + pad)
        ymax = math.ceil(ymax / 1000.0) * 1000.0 if ymax > 1000.0 else math.ceil(ymax / 100.0) * 100.0
    return ymin, ymax, step_k, ticks_count, expanded


def set_k_axis(ax: Any, ymin: float, ymax: float, step_k: float, ticks_count: int) -> None:
    from matplotlib.ticker import FuncFormatter

    ax.set_ylim(ymin, ymax)
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{int(round(v / 1000.0))}K"))
    if step_k <= 0:
        step_k = 1.0
    kmin = math.ceil((ymin / 1000.0) / step_k) * step_k
    kmax = math.floor((ymax / 1000.0) / step_k) * step_k
    if kmin > kmax:
        return
    total = int(round((kmax - kmin) / step_k))
    if ticks_count <= 1 or total <= 0:
        ks = [kmin]
    else:
        idxs = [int(round(i * total / (ticks_count - 1))) for i in range(ticks_count)]
        ks = [kmin + i * step_k for i in idxs]
    ax.set_yticks([k * 1000.0 for k in ks])


def plot_single_axis(
    ax: Any,
    merged: ModePairs,
    specs: Sequence[LineSpec],
    *,
    max_hours: float,
    bin_minutes: float,
    target: str,
    distfuzz: bool = False,
    marker_count: int = 20,
    marker_size: float = 10.0,
) -> bool:
    plotted = False
    for spec in specs:
        x, mean_y, min_y, max_y = aggregate_mode_stats(
            merged[spec.key],
            bin_minutes=bin_minutes,
            max_hours=max_hours,
        )
        if not x:
            raise ValueError(f"mode has no plottable coverage data: {spec.key}")
        plotted = True
        ax.plot(
            x,
            mean_y,
            label=spec.label,
            color=spec.color,
            marker=spec.marker,
            linestyle="-",
            markersize=marker_size,
            markerfacecolor="white",
            markeredgecolor=spec.color,
            markeredgewidth=1.0,
            markevery=marker_positions(x, marker_count),
        )
        ax.fill_between(x, min_y, max_y, color=spec.color, alpha=0.18)

    ax.grid(True, linestyle="--", linewidth=0.6, alpha=0.5)
    ax.set_xlim(-(max_hours / 24.0), max_hours)
    ticks = compute_hour_ticks(max_hours)
    ax.set_xticks(ticks)
    ax.set_xticklabels([format_tick(t) for t in ticks])
    ax.set_ylabel("Coverage (k)")
    values = all_values_for_specs(merged, specs, max_hours=max_hours, bin_minutes=bin_minutes)
    ymin, ymax, step_k, ticks_count, expanded = expanded_y_range(target, values, distfuzz=distfuzz)
    set_k_axis(ax, ymin, ymax, step_k, ticks_count)
    ax.legend(loc="lower right", frameon=True, fontsize="medium")
    return plotted and expanded


def render_ablation_figure(
    ctx: PaperContext,
    merged: ModePairs,
    *,
    target: str,
    scheduler: str,
    out_path: Path,
) -> Dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    specs = ablation_specs(target, scheduler, merged)
    plt.rcParams.update({"font.size": 18})
    fig, ax = plt.subplots()
    axis_expanded = plot_single_axis(
        ax,
        merged,
        specs,
        max_hours=ctx.max_hours,
        bin_minutes=ctx.bin_minutes,
        target=target,
        distfuzz=False,
    )
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200)
    plt.close(fig)
    return {"figure": str(out_path), "modes": [spec.key for spec in specs], "axis_expanded": axis_expanded}


def render_distfuzz_target_figure(
    ctx: PaperContext,
    merged: ModePairs,
    *,
    target: str,
    out_path: Path,
) -> Dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    specs = distfuzz_specs(target, merged)
    plt.rcParams.update({"font.size": 18})
    fig, ax = plt.subplots()
    axis_expanded = plot_single_axis(
        ax,
        merged,
        specs,
        max_hours=ctx.max_hours,
        bin_minutes=ctx.bin_minutes,
        target=target,
        distfuzz=True,
    )
    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200)
    plt.close(fig)
    return {"figure": str(out_path), "modes": [spec.key for spec in specs], "axis_expanded": axis_expanded}


def render_distfuzz_figure7(
    ctx: PaperContext,
    merged: ModePairs,
    *,
    out_path: Path,
) -> Dict[str, Any]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    targets = ["etcd", "nuraft", "redis_raft", "braft"]
    subtitles = ["(a) etcd", "(b) NuRaft", "(c) Redis Raft", "(d) Braft"]
    plt.rcParams.update({"font.size": 11})
    fig, axes = plt.subplots(2, 2, figsize=(6.6, 4.8))
    expanded: Dict[str, bool] = {}
    modes: Dict[str, List[str]] = {}
    for ax, target, subtitle in zip(axes.flat, targets, subtitles):
        specs = distfuzz_specs(target, merged)
        expanded[target] = plot_single_axis(
            ax,
            merged,
            specs,
            max_hours=ctx.max_hours,
            bin_minutes=ctx.bin_minutes,
            target=target,
            distfuzz=True,
            marker_count=6,
            marker_size=5.5,
        )
        modes[target] = [spec.key for spec in specs]
        ax.text(0.5, -0.28, subtitle, transform=ax.transAxes, ha="center", va="top")
        ax.legend(loc="lower right", frameon=True, fontsize=8)
    fig.tight_layout(h_pad=1.6, w_pad=1.5)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=200)
    plt.close(fig)
    return {"figure": str(out_path), "modes": modes, "axis_expanded": expanded}


def final_values(instances: Sequence[Tuple[str, List[Tuple[float, int]]]], end_hours: float) -> List[int]:
    vals: List[int] = []
    for _inst, pairs in instances:
        value = final_coverage_at(pairs, end_hours)
        if value is not None:
            vals.append(int(value))
    return vals


def coverage_cell(vals: Sequence[int]) -> str:
    if not vals:
        return ""
    mean = statistics.mean(vals)
    stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
    return f"{mean:.0f} +- {stdev:.0f}"


def table_label(label: str) -> str:
    if label.endswith("-ISM"):
        return f"{label} (Tamias)"
    return label


def coverage_rows(
    merged: ModePairs,
    specs: Sequence[LineSpec],
    *,
    target: str,
    scheduler: Optional[str],
    end_hours: float,
) -> List[Dict[str, Any]]:
    rows = []
    for spec in specs:
        vals = final_values(merged[spec.key], end_hours)
        rows.append({
            "target": target,
            "scheduler": scheduler or "",
            "mode": spec.key,
            "paper_label": spec.label,
            "runs": len(vals),
            "mean": statistics.mean(vals) if vals else "",
            "stdev": statistics.stdev(vals) if len(vals) > 1 else (0.0 if vals else ""),
            "min": min(vals) if vals else "",
            "max": max(vals) if vals else "",
            "paper_cell": coverage_cell(vals),
        })
    return rows


def write_csv(path: Path, rows: Sequence[Dict[str, Any]], fieldnames: Sequence[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_table3(
    out_prefix: Path,
    *,
    target: str,
    scheduler: str,
    rows: Sequence[Dict[str, Any]],
) -> Dict[str, str]:
    labels = [table_label(row["paper_label"]) for row in rows]
    cells = [row["paper_cell"] for row in rows]
    display = TARGET_DISPLAY.get(target, target)
    md = out_prefix.with_suffix(".md")
    tex = out_prefix.with_suffix(".tex")

    md_lines = [
        f"# Table 3 Slice: {display} ({'Random Walk' if scheduler == 'random' else 'PCT'})",
        "",
        "| Target | " + " | ".join(labels) + " |",
        "| --- | " + " | ".join(["---:"] * len(labels)) + " |",
        "| " + display + " | " + " | ".join(cells) + " |",
    ]
    md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    header = "Target & " + " & ".join(labels) + r" \\"
    row = display + " & " + " & ".join(cell.replace("+-", r"$\pm$") for cell in cells) + r" \\"
    tex.write_text(
        "\n".join([
            r"\begin{tabular}{l" + "r" * len(labels) + "}",
            r"\toprule",
            header,
            r"\midrule",
            row,
            r"\bottomrule",
            r"\end{tabular}",
            "",
        ]),
        encoding="utf-8",
    )
    return {"markdown": str(md), "latex": str(tex)}


def write_table4(
    out_prefix: Path,
    *,
    target: str,
    scheduler: str,
    specs: Sequence[LineSpec],
    ctx: PaperContext,
) -> Dict[str, str]:
    bug_counts = merge_bug_label_counts(ctx.json_files)
    total_runs = merge_mode_trial_counts(ctx.json_files)
    labels = [spec.label for spec in specs]
    display_labels = [table_label(label) for label in labels]
    bug_labels = list(TABLE4_BUGS_BY_TARGET.get(target, []))
    observed = {
        bug
        for spec in specs
        for bug in bug_counts.get(spec.key, {})
    }
    for bug in sorted(observed):
        if bug not in bug_labels:
            bug_labels.append(bug)

    rows: List[Dict[str, Any]] = []
    for bug in bug_labels:
        row: Dict[str, Any] = {"bug_label": bug}
        for spec in specs:
            row[spec.label] = int(bug_counts.get(spec.key, {}).get(bug, {}).get("found_runs", 0))
            row[f"{spec.label}_total_runs"] = int(total_runs.get(spec.key, len(merge_mode_jsons(ctx.json_files).get(spec.key, []))))
        rows.append(row)

    csv_path = out_prefix.with_suffix(".csv")
    md = out_prefix.with_suffix(".md")
    tex = out_prefix.with_suffix(".tex")
    fieldnames = ["bug_label"] + labels + [f"{label}_total_runs" for label in labels]
    write_csv(csv_path, rows, fieldnames)

    display = TARGET_DISPLAY.get(target, target)
    title = f"Table 4 Slice: {display} ({'Random Walk' if scheduler == 'random' else 'PCT'})"
    md_lines = [
        f"# {title}",
        "",
        "| Bug | " + " | ".join(display_labels) + " |",
        "| --- | " + " | ".join(["---:"] * len(display_labels)) + " |",
    ]
    if rows:
        for row in rows:
            md_lines.append("| " + row["bug_label"] + " | " + " | ".join(str(row[label]) for label in labels) + " |")
    else:
        md_lines.append("| n/a | " + " | ".join("0" for _ in display_labels) + " |")
    md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    tex_lines = [
        r"\begin{tabular}{l" + "r" * len(labels) + "}",
        r"\toprule",
        "Bug & " + " & ".join(display_labels) + r" \\",
        r"\midrule",
    ]
    if rows:
        for row in rows:
            tex_lines.append(row["bug_label"] + " & " + " & ".join(str(row[label]) for label in labels) + r" \\")
    else:
        tex_lines.append("n/a & " + " & ".join("0" for _ in labels) + r" \\")
    tex_lines.extend([r"\bottomrule", r"\end{tabular}", ""])
    tex.write_text("\n".join(tex_lines), encoding="utf-8")
    return {"csv": str(csv_path), "markdown": str(md), "latex": str(tex)}


def render_ablation_outputs(
    ctx: PaperContext,
    out_dir: Path,
    *,
    target: str,
    scheduler: str,
) -> Dict[str, Any]:
    merged = load_merged(ctx)
    specs = ablation_specs(target, scheduler, merged)
    figure_base = FIGURE_BY_TARGET.get(target, "figure")
    panel = PANEL_BY_TARGET_SCHEDULER.get((target, scheduler), "")
    scheduler_name = "random_walk" if scheduler == "random" else "pct"
    figure_name = f"{figure_base}{panel}_{target}_{scheduler_name}_coverage.pdf"

    figure = render_ablation_figure(
        ctx,
        merged,
        target=target,
        scheduler=scheduler,
        out_path=out_dir / figure_name,
    )
    table_specs = list(reversed(specs))
    rows = coverage_rows(merged, table_specs, target=target, scheduler=scheduler, end_hours=ctx.max_hours)
    table3_csv = out_dir / f"table3_{target}_{scheduler_name}_coverage.csv"
    write_csv(table3_csv, rows, ["target", "scheduler", "mode", "paper_label", "runs", "mean", "stdev", "min", "max", "paper_cell"])
    table3 = write_table3(out_dir / f"table3_{target}_{scheduler_name}_coverage", target=target, scheduler=scheduler, rows=rows)
    table4 = write_table4(out_dir / f"table4_{target}_{scheduler_name}_bug_counts", target=target, scheduler=scheduler, specs=table_specs, ctx=ctx)
    return {
        "kind": "ablation_study",
        "target": target,
        "scheduler": scheduler,
        "figure": figure,
        "table3": {"csv": str(table3_csv), **table3},
        "table4": table4,
    }


def distfuzz_target_order(target: Optional[str]) -> List[str]:
    if target:
        return [target]
    return ["etcd", "nuraft", "redis_raft", "braft"]


def write_distfuzz_table(
    out_prefix: Path,
    *,
    rows_by_target: Dict[str, List[Dict[str, Any]]],
) -> Dict[str, str]:
    csv_path = out_prefix.with_suffix(".csv")
    md = out_prefix.with_suffix(".md")
    tex = out_prefix.with_suffix(".tex")
    flat_rows: List[Dict[str, Any]] = []
    md_rows: List[Tuple[str, str, str, str]] = []
    for target, rows in rows_by_target.items():
        by_label = {row["paper_label"]: row for row in rows}
        dist = by_label.get("Distfuzz", {})
        tamias = by_label.get("Tamias", {})
        dist_mean = float(dist["mean"]) if dist.get("mean") != "" else None
        tamias_mean = float(tamias["mean"]) if tamias.get("mean") != "" else None
        improvement = ""
        if dist_mean and tamias_mean is not None:
            improvement = f"{((tamias_mean - dist_mean) / dist_mean) * 100.0:.1f}"
        flat_rows.append({
            "target": target,
            "distfuzz": dist.get("paper_cell", ""),
            "tamias": tamias.get("paper_cell", ""),
            "tamias_improvement_percent": improvement,
            "distfuzz_runs": dist.get("runs", ""),
            "tamias_runs": tamias.get("runs", ""),
        })
        md_rows.append((
            TARGET_DISPLAY.get(target, target),
            str(dist.get("paper_cell", "")),
            str(tamias.get("paper_cell", "")),
            improvement,
        ))

    write_csv(csv_path, flat_rows, ["target", "distfuzz", "tamias", "tamias_improvement_percent", "distfuzz_runs", "tamias_runs"])
    md_lines = [
        "# Figure 7 Coverage Summary",
        "",
        "| Target | Distfuzz | Tamias | Tamias Improvement (%) |",
        "| --- | ---: | ---: | ---: |",
    ]
    for target_name, dist, tamias, improvement in md_rows:
        md_lines.append(f"| {target_name} | {dist} | {tamias} | {improvement} |")
    md.write_text("\n".join(md_lines) + "\n", encoding="utf-8")

    tex_lines = [
        r"\begin{tabular}{lrrr}",
        r"\toprule",
        r"Target & Distfuzz & Tamias & Improvement (\%) \\",
        r"\midrule",
    ]
    for target_name, dist, tamias, improvement in md_rows:
        tex_lines.append(
            target_name
            + " & "
            + dist.replace("+-", r"$\pm$")
            + " & "
            + tamias.replace("+-", r"$\pm$")
            + " & "
            + improvement
            + r" \\"
        )
    tex_lines.extend([r"\bottomrule", r"\end{tabular}", ""])
    tex.write_text("\n".join(tex_lines), encoding="utf-8")
    return {"csv": str(csv_path), "markdown": str(md), "latex": str(tex)}


def render_distfuzz_outputs(
    ctx: PaperContext,
    out_dir: Path,
    *,
    target: Optional[str] = None,
) -> Dict[str, Any]:
    merged = load_merged(ctx)
    figures: Dict[str, Any] = {}
    if target:
        figure = render_distfuzz_target_figure(
            ctx,
            merged,
            target=target,
            out_path=out_dir / f"figure7_{target}_distfuzz_comparison.pdf",
        )
        figures[target] = figure
    else:
        figures["figure7"] = render_distfuzz_figure7(
            ctx,
            merged,
            out_path=out_dir / "figure7_distfuzz_comparison.pdf",
        )

    rows_by_target: Dict[str, List[Dict[str, Any]]] = {}
    for t in distfuzz_target_order(target):
        specs = distfuzz_specs(t, merged)
        rows_by_target[t] = coverage_rows(merged, specs, target=t, scheduler=None, end_hours=ctx.max_hours)
    table = write_distfuzz_table(out_dir / "figure7_coverage_summary", rows_by_target=rows_by_target)
    return {
        "kind": "distfuzz_comparison",
        "target": target or "all",
        "figures": figures,
        "coverage_summary": table,
    }
