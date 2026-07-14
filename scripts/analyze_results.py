#!/usr/bin/env python3
import argparse
import csv
import json
from pathlib import Path
from typing import Optional

from lib.result_common import aggregate_mode_stats, coverage_summary, merge_bug_label_counts, merge_mode_jsons, merge_mode_stable_jsons, merge_mode_trial_counts, merge_unmapped_bug_markers


def write_coverage_csv(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["mode", "runs", "mean", "stdev", "min", "max"])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def write_bug_csv(path: Path, bug_counts, total_runs_by_mode) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["mode", "bug_label", "markers", "found_runs", "total_runs"])
        writer.writeheader()
        for mode in sorted(bug_counts):
            for bug_label, item in sorted(bug_counts[mode].items()):
                writer.writerow({
                    "mode": mode,
                    "bug_label": bug_label,
                    "markers": "; ".join(item["markers"]),
                    "found_runs": item["found_runs"],
                    "total_runs": total_runs_by_mode.get(mode, 0),
                })


def write_bug_markdown(path: Path, bug_counts, total_runs_by_mode) -> None:
    lines = [
        "| Mode | Bug | Runs Triggered | Markers |",
        "| --- | --- | ---: | --- |",
    ]
    for mode in sorted(bug_counts):
        for bug, item in sorted(bug_counts[mode].items()):
            total = total_runs_by_mode.get(mode, 0)
            safe_marker = "; ".join(item["markers"]).replace("|", "\\|")
            lines.append(f"| {mode} | {bug} | {item['found_runs']}/{total} | `{safe_marker}` |")
    if len(lines) == 2:
        lines.append("| n/a | n/a | 0/0 | No bug markers found. |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_unmapped_bug_csv(path: Path, unmapped) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["mode", "marker", "found_runs"])
        writer.writeheader()
        for mode in sorted(unmapped):
            for marker, count in sorted(unmapped[mode].items()):
                writer.writerow({"mode": mode, "marker": marker, "found_runs": count})


def maybe_plot(path: Path, merged, max_hours: Optional[float], bin_minutes: float) -> None:
    if max_hours is None:
        return
    try:
        import matplotlib.pyplot as plt
    except Exception as exc:
        print(f"matplotlib unavailable; skipping plot: {exc}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    for mode in sorted(merged):
        x, mean_y, min_y, max_y = aggregate_mode_stats(
            merged[mode],
            bin_minutes=bin_minutes,
            max_hours=max_hours,
        )
        if not x:
            continue
        ax.plot(x, mean_y, marker="o", markersize=3, linewidth=1.5, label=mode)
        ax.fill_between(x, min_y, max_y, alpha=0.15)
    ax.set_xlabel("Time (hours)")
    ax.set_ylabel("Coverage")
    ax.grid(True, linestyle="--", linewidth=0.5, alpha=0.4)
    ax.legend(frameon=False, fontsize="small")
    fig.tight_layout()
    fig.savefig(path, dpi=200, bbox_inches="tight")
    print(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Produce coverage and bug-finding summaries from collected JSONs.")
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--max-hours", type=float, default=None,
                        help="Coverage horizon. Omit to use each run's last recorded point for tables.")
    parser.add_argument("--bin-minutes", type=float, default=5.0)
    parser.add_argument("--include-unmapped-bugs", action="store_true",
                        help="Include bug markers that are not mapped to paper Table 4 bug labels.")
    args = parser.parse_args()

    jsons = sorted(p for p in args.input_dir.rglob("*.json") if p.is_file())
    if not jsons:
        raise SystemExit(f"No JSON files found under {args.input_dir}")

    merged = merge_mode_jsons(jsons)
    stable_merged = merge_mode_stable_jsons(jsons)
    cov_rows = coverage_summary(merged, args.max_hours)
    stable_cov_rows = coverage_summary(stable_merged, args.max_hours)
    total_runs_by_mode = merge_mode_trial_counts(jsons)
    for row in cov_rows:
        total_runs_by_mode.setdefault(row["mode"], int(row["runs"]))
    bug_counts = merge_bug_label_counts(jsons, include_unmapped=args.include_unmapped_bugs)
    unmapped = merge_unmapped_bug_markers(jsons)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    write_coverage_csv(args.out_dir / "coverage_summary.csv", cov_rows)
    if stable_cov_rows:
        write_coverage_csv(args.out_dir / "stable_coverage_summary.csv", stable_cov_rows)
    write_bug_csv(args.out_dir / "bug_counts.csv", bug_counts, total_runs_by_mode)
    write_bug_markdown(args.out_dir / "table4_bug_counts.md", bug_counts, total_runs_by_mode)
    if unmapped:
        write_unmapped_bug_csv(args.out_dir / "unmapped_bug_markers.csv", unmapped)
    maybe_plot(args.out_dir / "coverage.pdf", merged, args.max_hours, args.bin_minutes)

    metadata = {
        "input_dir": str(args.input_dir),
        "json_files": [str(p) for p in jsons],
        "max_hours": args.max_hours,
        "bin_minutes": args.bin_minutes,
    }
    (args.out_dir / "analysis_metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    print(args.out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
