#!/usr/bin/env python3
import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
AE = REPO_ROOT / "scripts" / "ae.py"


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_name(raw: str) -> str:
    out = []
    for ch in raw.lower():
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("-")
    return "".join(out).strip("-") or "run"


def csv(values: Iterable[str]) -> str:
    return ",".join(values)


def selected_jobs(workload: Dict[str, Any], raw_jobs: Optional[str]) -> List[Dict[str, Any]]:
    jobs = list(workload.get("jobs", []))
    if not raw_jobs:
        return jobs
    keep = {x.strip() for x in raw_jobs.split(",") if x.strip()}
    selected = [job for job in jobs if job.get("id") in keep]
    missing = keep - {job.get("id") for job in selected}
    if missing:
        raise ValueError(f"unknown job id(s): {', '.join(sorted(missing))}")
    return selected


def resolve_nodes(workload: Dict[str, Any], job: Dict[str, Any]) -> List[str]:
    if "nodes" not in job:
        raise ValueError(f"{job['id']}: must specify nodes")
    nodes = list(job["nodes"])
    if not nodes:
        raise ValueError(f"{job['id']}: node list cannot be empty")
    return nodes


def job_value(workload: Dict[str, Any], job: Dict[str, Any], key: str, override: Any = None) -> Any:
    if override is not None:
        return override
    if key in job:
        return job[key]
    return workload.get("defaults", {}).get(key)


def launch_command(
    workload: Dict[str, Any],
    job: Dict[str, Any],
    *,
    nodes: List[str],
    run_ts: str,
    job_suffix: str,
    duration_minutes: Optional[int],
    deadline_grace_minutes: Optional[int],
    trials: Optional[int],
    concurrent_trials_per_node: Optional[int],
    output_owner: Optional[str],
    output_owner_current_user: bool,
    render_only: bool,
    use_tags: bool,
) -> List[str]:
    duration = job_value(workload, job, "duration_minutes", duration_minutes)
    deadline_grace = job_value(workload, job, "deadline_grace_minutes", deadline_grace_minutes)
    trial_count = job_value(workload, job, "trials", trials)
    concurrent_trials = job_value(workload, job, "concurrent_trials_per_node", concurrent_trials_per_node)
    job_run_ts = f"{run_ts}-{safe_name(job_suffix)}"
    cmd = [
        sys.executable,
        str(AE),
        "launch",
        "--experiment",
        job["experiment"],
        "--target",
        job["target"],
        "--nodes",
        csv(nodes),
        "--trials",
        str(trial_count),
        "--duration-minutes",
        str(duration),
        "--deadline-grace-minutes",
        str(deadline_grace),
        "--concurrent-trials-per-node",
        str(concurrent_trials),
        "--run-ts",
        job_run_ts,
    ]
    if job["experiment"] == "ablation":
        cmd.extend(["--scheduler", job["scheduler"], "--mode", job["mode"]])
    if job["experiment"] == "distfuzz_comparison":
        cmd.extend(["--system", job["system"]])
    if render_only:
        cmd.append("--render-only")
    if use_tags:
        cmd.append("--use-tags")
    if output_owner:
        cmd.extend(["--output-owner", output_owner])
    if output_owner_current_user:
        cmd.append("--output-owner-current-user")
    return cmd


def print_plan(rows: List[Dict[str, Any]]) -> None:
    print("id\texperiment\ttarget\tsystem/scheduler\tnodes\ttrials\tconcurrent_trials_per_node\tduration_min")
    for row in rows:
        mode = row.get("system") or f"{row.get('scheduler')}/{row.get('mode')}"
        print(
            f"{row['id']}\t{row['experiment']}\t{row['target']}\t{mode}\t"
            f"{csv(row['nodes'])}\t{row['trials']}\t{row['concurrent_trials_per_node']}\t{row['duration_minutes']}"
        )


def build_plan(args: argparse.Namespace) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    workload = load_json(Path(args.config))
    if workload.get("name") == "distfuzz_comparison" and not args.jobs:
        raise ValueError("distfuzz_comparison requires --jobs for one target pair, e.g. --jobs etcd_tamias,etcd_distfuzz")
    jobs = selected_jobs(workload, args.jobs)
    rows = []
    for job in jobs:
        rows.append({
            "id": job["id"],
            "experiment": job["experiment"],
            "target": job["target"],
            "system": job.get("system"),
            "scheduler": job.get("scheduler"),
            "mode": job.get("mode"),
            "nodes": resolve_nodes(workload, job),
            "trials": job_value(workload, job, "trials", args.trials),
            "concurrent_trials_per_node": job_value(
                workload,
                job,
                "concurrent_trials_per_node",
                args.concurrent_trials_per_node,
            ),
            "duration_minutes": job_value(workload, job, "duration_minutes", args.duration_minutes),
            "deadline_grace_minutes": job_value(workload, job, "deadline_grace_minutes", args.deadline_grace_minutes),
            "job": job,
        })
    if workload.get("name") == "distfuzz_comparison":
        targets = {row["target"] for row in rows}
        systems = {row["system"] for row in rows}
        if len(rows) != 2 or len(targets) != 1 or systems != {"tamias", "distfuzz"}:
            raise ValueError("distfuzz_comparison expects exactly one target pair, e.g. --jobs etcd_tamias,etcd_distfuzz")
    owner_by_node: Dict[str, str] = {}
    for row in rows:
        for node in row["nodes"]:
            owner = owner_by_node.get(node)
            if owner is not None:
                raise ValueError(f"node {node} is assigned to both {owner} and {row['id']}")
            owner_by_node[node] = row["id"]
    return workload, rows


def cmd_plan(args: argparse.Namespace) -> int:
    _workload, rows = build_plan(args)
    print_plan(rows)
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    if args.output_owner and args.output_owner_current_user:
        raise ValueError("use only one of --output-owner or --output-owner-current-user")
    workload, rows = build_plan(args)
    run_ts = args.run_ts or dt.datetime.utcnow().strftime("%Y%m%d_%H%M")
    summary = {
        "workload": workload["name"],
        "description": workload.get("description", ""),
        "created_utc": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "run_ts": run_ts,
        "render_only": bool(args.render_only),
        "output_owner": args.output_owner,
        "output_owner_current_user": bool(args.output_owner_current_user),
        "jobs": [],
    }
    print_plan(rows)
    for index, row in enumerate(rows, start=1):
        cmd = launch_command(
            workload,
            row["job"],
            nodes=row["nodes"],
            run_ts=run_ts,
            job_suffix=f"{index:02d}",
            duration_minutes=args.duration_minutes,
            deadline_grace_minutes=args.deadline_grace_minutes,
            trials=args.trials,
            concurrent_trials_per_node=args.concurrent_trials_per_node,
            output_owner=args.output_owner,
            output_owner_current_user=args.output_owner_current_user,
            render_only=args.render_only,
            use_tags=args.use_tags,
        )
        print("RUN", " ".join(cmd))
        proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if proc.stdout:
            print(proc.stdout, end="")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)
        if proc.returncode:
            return proc.returncode
        manifest_path = None
        for line in proc.stderr.splitlines():
            if line.startswith("manifest:"):
                manifest_path = line.split(":", 1)[1].strip()
        item = {k: row[k] for k in ("id", "experiment", "target", "system", "scheduler", "mode", "nodes", "trials", "concurrent_trials_per_node", "duration_minutes", "deadline_grace_minutes")}
        item["job_run_ts"] = f"{run_ts}-{index:02d}"
        item["manifest"] = manifest_path
        summary["jobs"].append(item)

    out_dir = REPO_ROOT / "runs"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"workload-{safe_name(workload['name'])}-{run_ts}.json"
    summary["manifest_path"] = str(out_path)
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"workload manifest: {out_path}", file=sys.stderr)
    return 0


def cmd_collect(args: argparse.Namespace) -> int:
    summary = load_json(Path(args.manifest))
    out_root = Path(args.out_dir or (REPO_ROOT / "results" / f"{summary['workload']}-{summary['run_ts']}" / "raw"))
    failures = 0
    for job in summary["jobs"]:
        job_out = out_root / job["id"]
        cmd = [
            sys.executable,
            str(AE),
            "collect",
            "--manifest",
            job["manifest"],
            "--out-dir",
            str(job_out),
        ]
        if args.local:
            cmd.append("--local")
        if args.ssh_user:
            cmd.extend(["--ssh-user", args.ssh_user])
        print("RUN", " ".join(cmd))
        proc = subprocess.run(cmd, cwd=REPO_ROOT)
        if proc.returncode:
            failures += 1
    print(out_root)
    return 1 if failures else 0


def cmd_analyze(args: argparse.Namespace) -> int:
    summary = load_json(Path(args.manifest))
    input_dir = Path(args.input_dir or (REPO_ROOT / "results" / f"{summary['workload']}-{summary['run_ts']}" / "raw"))
    out_dir = Path(args.out_dir or input_dir.parent / "analysis")
    cmd = [
        sys.executable,
        str(AE),
        "analyze",
        "--input-dir",
        str(input_dir),
        "--out-dir",
        str(out_dir),
        "--bin-minutes",
        str(args.bin_minutes),
    ]
    if args.max_hours is not None:
        cmd.extend(["--max-hours", str(args.max_hours)])
    if args.include_unmapped_bugs:
        cmd.append("--include-unmapped-bugs")
    print("RUN", " ".join(cmd))
    return subprocess.run(cmd, cwd=REPO_ROOT).returncode


def add_workload_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", required=True, help="Path to workload JSON, e.g. configs/workloads/distfuzz_comparison.json")
    parser.add_argument("--jobs", default=None, help="Comma-separated job ids to include.")
    parser.add_argument("--trials", type=int, default=None)
    parser.add_argument("--duration-minutes", type=int, default=None)
    parser.add_argument("--deadline-grace-minutes", type=int, default=None)
    parser.add_argument("--concurrent-trials-per-node", type=int, default=None,
                        help="Override concurrent trials per machine for each selected config.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch, collect, and analyze structured Tamias AE workload matrices.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan")
    add_workload_args(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_launch = sub.add_parser("launch")
    add_workload_args(p_launch)
    p_launch.add_argument("--run-ts", default=None)
    p_launch.add_argument("--output-owner", default=None,
                          help="After each pod exits, chown its generated hostPath output to numeric UID:GID.")
    p_launch.add_argument("--output-owner-current-user", action="store_true",
                          help="Equivalent to --output-owner $(id -u):$(id -g) on the control node.")
    p_launch.add_argument("--render-only", action="store_true")
    p_launch.add_argument("--use-tags", action="store_true")
    p_launch.set_defaults(func=cmd_launch)

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--manifest", required=True)
    p_collect.add_argument("--out-dir", default=None)
    p_collect.add_argument("--local", action="store_true")
    p_collect.add_argument("--ssh-user", default=None)
    p_collect.set_defaults(func=cmd_collect)

    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("--manifest", required=True)
    p_analyze.add_argument("--input-dir", default=None)
    p_analyze.add_argument("--out-dir", default=None)
    p_analyze.add_argument("--max-hours", type=float, default=None)
    p_analyze.add_argument("--bin-minutes", type=float, default=5.0)
    p_analyze.add_argument("--include-unmapped-bugs", action="store_true")
    p_analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args()
    try:
        return args.func(args)
    except ValueError as exc:
        parser.exit(2, f"error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
