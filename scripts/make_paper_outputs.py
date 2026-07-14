#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
from typing import Any, Dict

from render_paper_outputs import (
    load_context,
    render_ablation_outputs,
    render_distfuzz_outputs,
)


def write_manifest(out_dir: Path, payload: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    lines = [
        f"# {payload['kind']} Paper Outputs",
        "",
        f"Source analysis directory: `{payload['analysis_dir']}`",
        f"Coverage horizon: `{payload['max_hours']}` hours",
        f"Bin size: `{payload['bin_minutes']}` minutes",
        "",
    ]
    if payload["kind"] == "ablation_study":
        lines.extend([
            f"Target: `{payload['target']}`",
            f"Scheduler: `{payload['scheduler']}`",
            "",
            "Generated files:",
            "",
            f"- `{Path(payload['figure']['figure']).name}`",
            f"- `{Path(payload['table3']['csv']).name}`",
            f"- `{Path(payload['table3']['markdown']).name}`",
            f"- `{Path(payload['table3']['latex']).name}`",
            f"- `{Path(payload['table4']['csv']).name}`",
            f"- `{Path(payload['table4']['markdown']).name}`",
            f"- `{Path(payload['table4']['latex']).name}`",
        ])
    else:
        lines.extend([
            f"Target: `{payload['target']}`",
            "",
            "Generated files:",
            "",
        ])
        for item in payload["figures"].values():
            if isinstance(item, dict) and "figure" in item:
                lines.append(f"- `{Path(item['figure']).name}`")
        lines.extend([
            f"- `{Path(payload['coverage_summary']['csv']).name}`",
            f"- `{Path(payload['coverage_summary']['markdown']).name}`",
            f"- `{Path(payload['coverage_summary']['latex']).name}`",
        ])
    (out_dir / "README.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Render paper-style Tamias AE figures and tables from collected analysis metadata."
    )
    parser.add_argument("--kind", choices=["ablation_study", "distfuzz_comparison"], required=True)
    parser.add_argument("--analysis-dir", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--target", choices=["redis", "mongo", "mysql", "etcd", "nuraft", "braft", "redis_raft"],
                        help="Required for ablation. Optional for DistFuzz to render a single target instead of full Figure 7.")
    parser.add_argument("--scheduler", choices=["random", "pct"],
                        help="Required for ablation; selects the R* or P* paper figure/table slice.")
    parser.add_argument("--max-hours", type=float, default=None,
                        help="Override the analysis horizon from analysis_metadata.json.")
    parser.add_argument("--bin-minutes", type=float, default=None,
                        help="Override the bin size from analysis_metadata.json.")
    args = parser.parse_args()

    try:
        ctx = load_context(args.analysis_dir, max_hours=args.max_hours, bin_minutes=args.bin_minutes)

        if args.kind == "ablation_study":
            if args.target is None:
                parser.error("--target is required for --kind ablation_study")
            if args.scheduler is None:
                parser.error("--scheduler is required for --kind ablation_study")
            if args.target == "redis_raft":
                parser.error("redis_raft is only valid for --kind distfuzz_comparison")
            scheduler_name = "random_walk" if args.scheduler == "random" else "pct"
            out_dir = args.out_dir or (args.analysis_dir / "paper_outputs" / f"{args.target}_{scheduler_name}")
            payload = render_ablation_outputs(ctx, out_dir, target=args.target, scheduler=args.scheduler)
        else:
            if args.scheduler is not None:
                parser.error("--scheduler is only valid for --kind ablation_study")
            if args.target in {"redis", "mongo", "mysql"}:
                parser.error("--kind distfuzz_comparison supports targets: etcd, nuraft, redis_raft, braft")
            out_dir = args.out_dir or (args.analysis_dir / "paper_outputs" / (f"{args.target}_distfuzz" if args.target else "figure7"))
            payload = render_distfuzz_outputs(ctx, out_dir, target=args.target)
    except (FileNotFoundError, KeyError, ValueError) as exc:
        parser.exit(2, f"error: {exc}\n")

    payload.update({
        "analysis_dir": str(args.analysis_dir),
        "input_dir": str(ctx.input_dir),
        "json_files": [str(p) for p in ctx.json_files],
        "max_hours": ctx.max_hours,
        "bin_minutes": ctx.bin_minutes,
    })
    write_manifest(out_dir, payload)
    print(out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
