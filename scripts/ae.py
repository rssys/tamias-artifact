#!/usr/bin/env python3
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional


REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "configs" / "experiments.json"
IMAGES_PATH = REPO_ROOT / "configs" / "images.json"
JOB_NAME_LIMIT = 50
WORKDIR_HASH_LENGTH = 12
NYX_SOCKET_PATH_SAFETY_LIMIT = 90
NYX_SOCKET_PATH_HARD_LIMIT = 107


def command_env() -> Dict[str, str]:
    env = dict(os.environ)
    kubeconfig = Path.home() / ".kube" / "config"
    if "KUBECONFIG" not in env and kubeconfig.is_file():
        env["KUBECONFIG"] = str(kubeconfig)
    return env


def load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sanitize(raw: str, limit: int = 50) -> str:
    text = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")
    return (text or "run")[:limit].strip("-") or "run"


def workdir_name(run_name: str, run_ts: str) -> str:
    # Nyx/QEMU creates a UNIX socket under this path; keep it short.
    digest = hashlib.sha1(f"{run_name}_{run_ts}".encode("utf-8")).hexdigest()[:WORKDIR_HASH_LENGTH]
    return f"w-{digest}"


def tamias_nyx_socket_path(root: Path, run_name: str, run_ts: str, node: str, global_id: int) -> str:
    instance_id = f"{global_id:03d}"
    node_short = sanitize(node, 12)
    return str(root / "workdir" / workdir_name(run_name, run_ts) / f"{instance_id}_{node_short}" / "workdir" / "nyx" / "interface_0")


def check_tamias_nyx_socket_path(root: Path, run_name: str, run_ts: str, node: str, global_id: int) -> None:
    path = tamias_nyx_socket_path(root, run_name, run_ts, node, global_id)
    path_len = len(path.encode("utf-8"))
    if path_len > NYX_SOCKET_PATH_SAFETY_LIMIT:
        raise SystemExit(
            "Nyx/QEMU socket path is too long for this launcher "
            f"({path_len} bytes > {NYX_SOCKET_PATH_SAFETY_LIMIT} byte safety limit; "
            f"QEMU hard limit is {NYX_SOCKET_PATH_HARD_LIMIT} bytes): {path}\n"
            "Use a shorter --root, for example /data/tamias-artifact, or shorter machine labels."
        )


def image_ref(images: Dict[str, Any], image_key: str, use_digest: bool = True) -> str:
    item = images["images"][image_key]
    repo = item["repository"]
    if use_digest and item.get("digest"):
        return f"{repo}@{item['digest']}"
    return f"{repo}:{item.get('tag', 'latest')}"


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def resolve_output_owner(args: argparse.Namespace) -> Optional[str]:
    if args.output_owner and args.output_owner_current_user:
        raise SystemExit("use only one of --output-owner or --output-owner-current-user")
    if args.output_owner_current_user:
        return f"{os.getuid()}:{os.getgid()}"
    if args.output_owner is None:
        return None
    if not re.fullmatch(r"[0-9]+:[0-9]+", args.output_owner):
        raise SystemExit("--output-owner must be a numeric UID:GID pair")
    return args.output_owner


def indent(text: str, spaces: int) -> str:
    prefix = " " * spaces
    return "\n".join(prefix + line if line else prefix for line in text.splitlines())


def distribute_trials(nodes: List[str], total: int, concurrent_trials_per_node: int) -> List[Dict[str, Any]]:
    if not nodes:
        raise ValueError("nodes cannot be empty")
    if total <= 0:
        raise ValueError("trials must be positive")
    if concurrent_trials_per_node <= 0:
        raise ValueError("concurrent_trials_per_node must be positive")
    out = []
    offset = 0
    base = total // len(nodes)
    extra = total % len(nodes)
    for i, node in enumerate(nodes):
        n = base + (1 if i < extra else 0)
        if n <= 0:
            continue
        out.append({
            "node": node,
            "count": n,
            "parallelism": min(n, concurrent_trials_per_node),
            "offset": offset,
            "batch": 0,
        })
        offset += n
    return out


def active_deadline_seconds(duration_minutes: int, grace_minutes: int) -> int:
    return int(duration_minutes) * 60 + int(grace_minutes) * 60


def tamias_job_yaml(
    *,
    job_name: str,
    namespace: str,
    node: str,
    image: str,
    root: str,
    run_name: str,
    run_ts: str,
    offset: int,
    completions: int,
    parallelism: int,
    extra_args: str,
    shm_size: str,
    debug_sleep: int,
    duration_minutes: int,
    cpu: Optional[str],
    deadline_grace_minutes: int,
    output_owner: Optional[str],
) -> str:
    owner = output_owner or ""
    run_dir_name = workdir_name(run_name, run_ts)
    node_dir_name = sanitize(node, 12)
    resources = ""
    if cpu:
        resources = f"""        resources:
          requests:
            cpu: "{cpu}"
          limits:
            cpu: "{cpu}"
"""
    script = f"""set -euo pipefail
IDX="${{JOB_COMPLETION_INDEX}}"
GLOBAL_ID=$(( {offset} + IDX ))
INSTANCE_ID="$(printf "%03d" "${{GLOBAL_ID}}")"
NODE_SHORT="{node_dir_name}"
RUN_DIR="{root}/workdir/{run_dir_name}"
BASE="${{RUN_DIR}}/${{INSTANCE_ID}}_${{NODE_SHORT}}"
OUTPUT_OWNER="{owner}"
chown_output() {{
  if [ -n "${{OUTPUT_OWNER}}" ]; then
    echo "chown-output owner=${{OUTPUT_OWNER}} path=${{BASE}}"
    chown "${{OUTPUT_OWNER}}" "${{RUN_DIR}}" || echo "WARN: failed to chown ${{RUN_DIR}} to ${{OUTPUT_OWNER}}" >&2
    chown -R "${{OUTPUT_OWNER}}" "${{BASE}}" || echo "WARN: failed to chown ${{BASE}} to ${{OUTPUT_OWNER}}" >&2
  fi
}}
trap 'rc=$?; chown_output || true; exit "${{rc}}"' EXIT
mkdir -p "${{BASE}}"
LOG_FILE="${{BASE}}/run.log"
exec > >(tee -a "${{LOG_FILE}}") 2>&1
echo "tamias-run name={run_name} ts={run_ts} node=${{NODE_ID}} index=${{GLOBAL_ID}} duration_minutes={duration_minutes}"
set +e
if command -v timeout >/dev/null 2>&1; then
  timeout -k 60s "{duration_minutes}m" /usr/local/bin/run.sh -workdir "${{BASE}}/workdir" {extra_args}
else
  echo "WARN: timeout command missing; relying on Kubernetes activeDeadlineSeconds" >&2
  /usr/local/bin/run.sh -workdir "${{BASE}}/workdir" {extra_args}
fi
rc=$?
set -e
if [ "${{rc}}" = "124" ]; then
  echo "duration budget reached after {duration_minutes} minutes"
  rc=0
fi
if [ "${{rc}}" != "0" ] && [ "{debug_sleep}" != "0" ]; then
  echo "run.sh exited with ${{rc}}; sleeping for debug" >&2
  sleep "{debug_sleep}"
fi
exit "${{rc}}"
"""
    return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: tamias-ae
    tamiasEvalName: {sanitize(run_name)}
    tamiasRunTs: {sanitize(run_ts)}
spec:
  completions: {completions}
  parallelism: {parallelism}
  completionMode: Indexed
  backoffLimit: 0
  activeDeadlineSeconds: {active_deadline_seconds(duration_minutes, deadline_grace_minutes)}
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app.kubernetes.io/name: tamias-ae
        tamiasEvalName: {sanitize(run_name)}
        tamiasRunTs: {sanitize(run_ts)}
    spec:
      restartPolicy: Never
      nodeSelector:
        short: {node}
        kvm: "true"
      containers:
      - name: fuzzer
        image: {image}
        imagePullPolicy: IfNotPresent
        securityContext:
          privileged: true
{resources}        env:
        - name: NODE_ID
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        - name: TAMIAS_DURATION_MINUTES
          value: "{duration_minutes}"
        command: ["/bin/bash", "-lc"]
        args:
        - |
{indent(script, 10)}
        volumeMounts:
        - name: dev-kvm
          mountPath: /dev/kvm
        - name: root
          mountPath: {root}
        - name: dshm
          mountPath: /dev/shm
      volumes:
      - name: dev-kvm
        hostPath:
          path: /dev/kvm
          type: CharDevice
      - name: root
        hostPath:
          path: {root}
          type: DirectoryOrCreate
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: {shm_size}
"""


def distfuzz_job_yaml(
    *,
    job_name: str,
    namespace: str,
    node: str,
    image: str,
    root: str,
    run_name: str,
    run_ts: str,
    offset: int,
    completions: int,
    parallelism: int,
    distfuzz_path: str,
    shm_size: str,
    debug_sleep: int,
    duration_minutes: int,
    cpu: str,
    memory: str,
    deadline_grace_minutes: int,
    output_owner: Optional[str],
) -> str:
    owner = output_owner or ""
    run_dir_name = workdir_name(run_name, run_ts)
    node_dir_name = sanitize(node, 12)
    script = f"""set -euo pipefail
IDX="${{JOB_COMPLETION_INDEX}}"
GLOBAL_ID=$(( {offset} + IDX ))
INSTANCE_ID="$(printf "%03d" "${{GLOBAL_ID}}")"
NODE_SHORT="{node_dir_name}"
RUN_DIR="{root}/workdir/{run_dir_name}"
BASE="${{RUN_DIR}}/${{INSTANCE_ID}}_${{NODE_SHORT}}"
ARTIFACT_DIR="${{BASE}}/artifacts"
SRC_CURVE="/home/zyh/DistFuzz/{distfuzz_path}/bin/output/fuzzer1/plot-curve"
OUTPUT_OWNER="{owner}"
mkdir -p "${{ARTIFACT_DIR}}"
LOG_FILE="${{BASE}}/run.log"
mirror_curve() {{
  if [ -f "${{SRC_CURVE}}" ]; then
    install -m 0644 "${{SRC_CURVE}}" "${{ARTIFACT_DIR}}/plot-curve"
  fi
}}
chown_output() {{
  if [ -n "${{OUTPUT_OWNER}}" ]; then
    echo "chown-output owner=${{OUTPUT_OWNER}} path=${{BASE}}"
    chown "${{OUTPUT_OWNER}}" "${{RUN_DIR}}" || echo "WARN: failed to chown ${{RUN_DIR}} to ${{OUTPUT_OWNER}}" >&2
    chown -R "${{OUTPUT_OWNER}}" "${{BASE}}" || echo "WARN: failed to chown ${{BASE}} to ${{OUTPUT_OWNER}}" >&2
  fi
}}
cleanup() {{
  mirror_curve || true
  if [ -n "${{MIRROR_PID:-}}" ]; then
    kill "${{MIRROR_PID}}" 2>/dev/null || true
    wait "${{MIRROR_PID}}" 2>/dev/null || true
  fi
  chown_output || true
}}
trap cleanup EXIT INT TERM
(
  while true; do
    mirror_curve || true
    sleep 10
  done
) &
MIRROR_PID="$!"
exec > >(tee -a "${{LOG_FILE}}") 2>&1
echo "distfuzz-run name={run_name} ts={run_ts} node=${{NODE_ID}} index=${{GLOBAL_ID}} duration_minutes={duration_minutes}"
cd /home/zyh/DistFuzz/{distfuzz_path}/bin
set +e
if command -v timeout >/dev/null 2>&1; then
  timeout -k 60s "{duration_minutes}m" ./fuzz.sh
else
  echo "WARN: timeout command missing; relying on Kubernetes activeDeadlineSeconds" >&2
  ./fuzz.sh
fi
rc=$?
set -e
if [ "${{rc}}" = "124" ]; then
  echo "duration budget reached after {duration_minutes} minutes"
  rc=0
fi
if [ "${{rc}}" != "0" ] && [ "{debug_sleep}" != "0" ]; then
  echo "fuzz.sh exited with ${{rc}}; sleeping for debug" >&2
  sleep "{debug_sleep}"
fi
exit "${{rc}}"
"""
    return f"""apiVersion: batch/v1
kind: Job
metadata:
  name: {job_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: tamias-ae
    tamiasEvalName: {sanitize(run_name)}
    tamiasRunTs: {sanitize(run_ts)}
spec:
  completions: {completions}
  parallelism: {parallelism}
  completionMode: Indexed
  backoffLimit: 0
  activeDeadlineSeconds: {active_deadline_seconds(duration_minutes, deadline_grace_minutes)}
  ttlSecondsAfterFinished: 86400
  template:
    metadata:
      labels:
        app.kubernetes.io/name: tamias-ae
        tamiasEvalName: {sanitize(run_name)}
        tamiasRunTs: {sanitize(run_ts)}
    spec:
      restartPolicy: Never
      nodeSelector:
        short: {node}
      containers:
      - name: fuzzer
        image: {image}
        imagePullPolicy: IfNotPresent
        securityContext:
          privileged: true
          runAsUser: 0
          allowPrivilegeEscalation: true
          seccompProfile:
            type: Unconfined
          capabilities:
            add: ["NET_ADMIN", "SYS_PTRACE", "SYS_NICE", "IPC_LOCK"]
        resources:
          requests:
            cpu: "{cpu}"
            memory: "{memory}"
          limits:
            cpu: "{cpu}"
            memory: "{memory}"
        env:
        - name: NODE_ID
          valueFrom:
            fieldRef:
              fieldPath: spec.nodeName
        - name: TAMIAS_DURATION_MINUTES
          value: "{duration_minutes}"
        command: ["/bin/bash", "-lc"]
        args:
        - |
{indent(script, 10)}
        volumeMounts:
        - name: root
          mountPath: {root}
        - name: dev-net-tun
          mountPath: /dev/net/tun
        - name: dshm
          mountPath: /dev/shm
      volumes:
      - name: root
        hostPath:
          path: {root}
          type: DirectoryOrCreate
      - name: dev-net-tun
        hostPath:
          path: /dev/net/tun
          type: CharDevice
      - name: dshm
        emptyDir:
          medium: Memory
          sizeLimit: {shm_size}
"""


def ablation_specs(config: Dict[str, Any], target: str, scheduler: str, mode: str) -> List[Dict[str, Any]]:
    exp = config["experiments"]["ablation"]
    target_cfg = exp["targets"][target]
    mode_names = list(config["ablation_modes"].keys()) if mode == "all" else [mode]
    pctcp = scheduler == "pct"
    specs = []
    for mode_name in mode_names:
        mode_cfg = config["ablation_modes"][mode_name]
        label_key = "pct_label" if pctcp else "random_label"
        mode_label = mode_cfg.get(label_key, f"{scheduler}_{mode_name}")
        specs.append({
            "runner": "tamias",
            "run_name": f"{target}_{mode_label}",
            "image": target_cfg["image"],
            "env": target_cfg["env"],
            "strategy": mode_cfg["strategy"],
            "pctcp": pctcp,
            "msg_mode": mode_cfg["msg_mode"],
            "mut_opt": mode_cfg["mut_opt"],
            "concurrent_trials_per_node": target_cfg.get(
                "concurrent_trials_per_node",
                config["defaults"]["concurrent_trials_per_node"],
            ),
            "target": target,
            "system": "tamias",
            "scheduler": scheduler,
            "mode": mode_name,
        })
    return specs


def distfuzz_specs(config: Dict[str, Any], target: str, system: str) -> List[Dict[str, Any]]:
    target_cfg = config["experiments"]["distfuzz_comparison"]["targets"][target]
    spec = dict(target_cfg["systems"][system])
    spec.update({"target": target, "system": system, "scheduler": "pct", "mode": "comparison"})
    return [spec]


def extra_args_for_tamias(spec: Dict[str, Any], root: Path) -> str:
    env_path = root / spec["env"]
    pieces = [
        "-env_config", str(env_path),
        "-fuzz_strategy", spec["strategy"],
        f"-pctcp={'true' if spec['pctcp'] else 'false'}",
        "-msg_mode", spec.get("msg_mode", "none"),
    ]
    if spec.get("mut_opt"):
        pieces.extend(["-mut_opt", spec["mut_opt"]])
    return " ".join(sh_quote(x) for x in pieces)


def render_launch(args: argparse.Namespace) -> Dict[str, Any]:
    config = load_json(CONFIG_PATH)
    images = load_json(IMAGES_PATH)
    defaults = config["defaults"]
    nodes = [n.strip() for n in args.nodes.split(",") if n.strip()]
    run_ts = args.run_ts or dt.datetime.utcnow().strftime("%Y%m%d_%H%M")
    root = Path(args.root or defaults["root"]).resolve()
    namespace = args.namespace or defaults["namespace"]
    duration = int(args.duration_minutes or defaults["duration_minutes"])
    deadline_grace = int(args.deadline_grace_minutes if args.deadline_grace_minutes is not None else defaults["deadline_grace_minutes"])
    trials = int(args.trials or defaults["trials"])
    shm_size = args.shm_size or defaults["shm_size"]
    debug_sleep = int(args.debug_sleep_seconds if args.debug_sleep_seconds is not None else defaults["debug_sleep_seconds"])
    output_owner = resolve_output_owner(args)

    if args.experiment == "ablation":
        if not args.scheduler:
            raise SystemExit("--scheduler is required for ablation")
        if not args.mode:
            raise SystemExit("--mode is required for ablation; use --mode all to run rand/mut_inp/mut_all")
        specs = ablation_specs(config, args.target, args.scheduler, args.mode)
    elif args.experiment == "distfuzz_comparison":
        specs = distfuzz_specs(config, args.target, args.system)
    else:
        raise SystemExit(f"unknown experiment: {args.experiment}")

    yaml_docs = []
    jobs = []
    for spec in specs:
        concurrent_trials_per_node = int(
            args.concurrent_trials_per_node
            or spec.get("concurrent_trials_per_node")
            or defaults["concurrent_trials_per_node"]
        )
        allocations = distribute_trials(nodes, trials, concurrent_trials_per_node)
        for alloc in allocations:
            if spec["runner"] == "tamias":
                check_tamias_nyx_socket_path(
                    root=root,
                    run_name=spec["run_name"],
                    run_ts=run_ts,
                    node=alloc["node"],
                    global_id=alloc["offset"] + alloc["count"] - 1,
                )
            job_name = sanitize(f"tamias-{spec['run_name']}-{alloc['node']}-{run_ts}-{alloc['batch']}", JOB_NAME_LIMIT)
            img = image_ref(images, spec["image"], use_digest=not args.use_tags)
            tamias_cpu = None
            if args.experiment == "distfuzz_comparison":
                tamias_cpu = spec.get("cpu") or defaults.get("tamias_distfuzz_cpu")
            if spec["runner"] == "tamias":
                yaml_doc = tamias_job_yaml(
                    job_name=job_name,
                    namespace=namespace,
                    node=alloc["node"],
                    image=img,
                    root=str(root),
                    run_name=spec["run_name"],
                    run_ts=run_ts,
                    offset=alloc["offset"],
                    completions=alloc["count"],
                    parallelism=alloc["parallelism"],
                    extra_args=extra_args_for_tamias(spec, root),
                    shm_size=shm_size,
                    debug_sleep=debug_sleep,
                    duration_minutes=duration,
                    cpu=tamias_cpu,
                    deadline_grace_minutes=deadline_grace,
                    output_owner=output_owner,
                )
            else:
                yaml_doc = distfuzz_job_yaml(
                    job_name=job_name,
                    namespace=namespace,
                    node=alloc["node"],
                    image=img,
                    root=str(root),
                    run_name=spec["run_name"],
                    run_ts=run_ts,
                    offset=alloc["offset"],
                    completions=alloc["count"],
                    parallelism=alloc["parallelism"],
                    distfuzz_path=spec["distfuzz_path"],
                    shm_size=shm_size,
                    debug_sleep=debug_sleep,
                    duration_minutes=duration,
                    cpu=args.distfuzz_cpu or defaults["distfuzz_cpu"],
                    memory=args.distfuzz_memory or defaults["distfuzz_memory"],
                    deadline_grace_minutes=deadline_grace,
                    output_owner=output_owner,
                )
            yaml_docs.append(yaml_doc)
            jobs.append({
                "job_name": job_name,
                "node": alloc["node"],
                "count": alloc["count"],
                "parallelism": alloc["parallelism"],
                "offset": alloc["offset"],
                "runner": spec["runner"],
                "run_name": spec["run_name"],
                "run_ts": run_ts,
                "target": spec["target"],
                "system": spec["system"],
                "workdir": str(root / "workdir" / workdir_name(spec["run_name"], run_ts)),
                "output_owner": output_owner,
            })

    manifest = {
        "run_id": sanitize(f"{args.experiment}-{args.target}-{run_ts}", 80),
        "created_utc": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "experiment": args.experiment,
        "target": args.target,
        "scheduler": args.scheduler,
        "mode": args.mode,
        "system": args.system,
        "namespace": namespace,
        "root": str(root),
        "run_ts": run_ts,
        "trials": trials,
        "duration_minutes": duration,
        "deadline_grace_minutes": deadline_grace,
        "output_owner": output_owner,
        "nodes": nodes,
        "jobs": jobs,
    }
    manifest_dir = REPO_ROOT / "runs"
    manifest_dir.mkdir(exist_ok=True)
    manifest_path = manifest_dir / f"{manifest['run_id']}.json"
    manifest["manifest_path"] = str(manifest_path)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    yaml_text = "---\n".join(yaml_docs)
    if args.render_only:
        print(yaml_text)
    else:
        env = command_env()
        subprocess.run(["kubectl", "get", "ns", namespace], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, env=env)
        subprocess.run(["kubectl", "create", "ns", namespace], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, env=env)
        subprocess.run(["kubectl", "apply", "-f", "-"], input=yaml_text, text=True, check=True, env=env)
    print(f"manifest: {manifest_path}", file=sys.stderr)
    return manifest


def cmd_list(_args: argparse.Namespace) -> int:
    config = load_json(CONFIG_PATH)
    images = load_json(IMAGES_PATH)
    print("Experiments:")
    for name, exp in config["experiments"].items():
        targets = ", ".join(sorted(exp["targets"]))
        print(f"  {name}: {targets}")
    print("\nImages:")
    for key, item in sorted(images["images"].items()):
        print(f"  {key}: {item['repository']}@{item['digest']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    config = load_json(CONFIG_PATH)
    if args.experiment == "ablation":
        obj = config["experiments"]["ablation"]["targets"][args.target]
    else:
        obj = config["experiments"]["distfuzz_comparison"]["targets"][args.target]
    print(json.dumps(obj, indent=2))
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    render_launch(args)
    return 0


def remote_collect_one(
    *,
    node: str,
    runner: str,
    workdir: str,
    run_name: str,
    out_local: Path,
    ssh_user: Optional[str],
) -> None:
    collect_script = REPO_ROOT / "scripts" / "collect_results.py"
    remote = f"{ssh_user + '@' if ssh_user else ''}{node}"
    remote_script = "/tmp/tamias_collect_results.py"
    remote_out = f"/tmp/{out_local.name}"
    subprocess.run(["scp", "-o", "StrictHostKeyChecking=no", str(collect_script), f"{remote}:{remote_script}"], check=True)
    subprocess.run([
        "ssh", "-o", "StrictHostKeyChecking=no", remote,
        "python3", remote_script, runner, workdir, "--run-name", run_name, "--out", remote_out
    ], check=True)
    out_local.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["scp", "-o", "StrictHostKeyChecking=no", f"{remote}:{remote_out}", str(out_local)], check=True)


def local_collect_one(*, runner: str, workdir: str, run_name: str, out_local: Path) -> None:
    out_local.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable,
        str(REPO_ROOT / "scripts" / "collect_results.py"),
        runner,
        workdir,
        "--run-name",
        run_name,
        "--out",
        str(out_local),
    ], check=True)


def cmd_collect(args: argparse.Namespace) -> int:
    manifest = load_json(Path(args.manifest))
    out_root = Path(args.out_dir or (REPO_ROOT / "results" / manifest["run_id"] / "raw"))
    seen = set()
    failures = 0
    for job in manifest["jobs"]:
        key = (job["node"], job["runner"], job["workdir"], job["run_name"])
        if key in seen:
            continue
        seen.add(key)
        out_local = out_root / job["target"] / f"{job['node']}_{job['run_name']}_{job['run_ts']}.json"
        try:
            if args.local:
                local_collect_one(runner=job["runner"], workdir=job["workdir"], run_name=job["run_name"], out_local=out_local)
            else:
                remote_collect_one(
                    node=job["node"],
                    runner=job["runner"],
                    workdir=job["workdir"],
                    run_name=job["run_name"],
                    out_local=out_local,
                    ssh_user=args.ssh_user,
                )
        except subprocess.CalledProcessError as exc:
            failures += 1
            print(f"collect failed for {job['node']} {job['run_name']}: {exc}", file=sys.stderr)
    print(out_root)
    return 1 if failures else 0


def cmd_analyze(args: argparse.Namespace) -> int:
    out_dir = Path(args.out_dir or (Path(args.input_dir).parent / "analysis"))
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "analyze_results.py"),
        "--input-dir",
        str(args.input_dir),
        "--out-dir",
        str(out_dir),
        "--bin-minutes",
        str(args.bin_minutes),
    ]
    if args.max_hours is not None:
        cmd.extend(["--max-hours", str(args.max_hours)])
    if args.include_unmapped_bugs:
        cmd.append("--include-unmapped-bugs")
    subprocess.run(cmd, check=True)
    return 0


def add_common_launch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--experiment", choices=["ablation", "distfuzz_comparison"], required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--nodes", required=True, help="Comma-separated machine labels/hostnames, e.g. node1,node2")
    parser.add_argument("--scheduler", choices=["random", "pct"], default=None)
    parser.add_argument("--mode", choices=["rand", "mut_inp", "mut_all", "all"], default=None)
    parser.add_argument("--system", choices=["tamias", "distfuzz"], default="tamias")
    parser.add_argument("--trials", type=int, default=None)
    parser.add_argument("--duration-minutes", type=int, default=None)
    parser.add_argument("--deadline-grace-minutes", type=int, default=None,
                        help="Extra Kubernetes activeDeadlineSeconds grace for image pulls/startup; the in-container timeout still enforces fuzzing duration.")
    parser.add_argument("--concurrent-trials-per-node", type=int, default=None,
                        help="Maximum concurrent trials per machine for this config.")
    parser.add_argument("--root", default=None)
    parser.add_argument("--namespace", default=None)
    parser.add_argument("--run-ts", default=None)
    parser.add_argument("--shm-size", default=None)
    parser.add_argument("--debug-sleep-seconds", type=int, default=None)
    parser.add_argument("--distfuzz-cpu", default=None)
    parser.add_argument("--distfuzz-memory", default=None)
    parser.add_argument("--output-owner", default=None,
                        help="After a pod exits, chown its generated hostPath output to numeric UID:GID.")
    parser.add_argument("--output-owner-current-user", action="store_true",
                        help="Equivalent to --output-owner $(id -u):$(id -g) on the control node.")
    parser.add_argument("--use-tags", action="store_true", help="Use repository:tag instead of repository@digest.")
    parser.add_argument("--render-only", action="store_true", help="Print Kubernetes YAML and write manifest without applying.")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Tamias SOSP artifact evaluation helper.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show")
    p_show.add_argument("--experiment", choices=["ablation", "distfuzz_comparison"], required=True)
    p_show.add_argument("--target", required=True)
    p_show.set_defaults(func=cmd_show)

    p_launch = sub.add_parser("launch")
    add_common_launch_args(p_launch)
    p_launch.set_defaults(func=cmd_launch)

    p_collect = sub.add_parser("collect")
    p_collect.add_argument("--manifest", required=True)
    p_collect.add_argument("--out-dir", default=None)
    p_collect.add_argument("--local", action="store_true", help="Collect from local filesystem instead of SSHing to nodes.")
    p_collect.add_argument("--ssh-user", default=None)
    p_collect.set_defaults(func=cmd_collect)

    p_analyze = sub.add_parser("analyze")
    p_analyze.add_argument("--input-dir", required=True)
    p_analyze.add_argument("--out-dir", default=None)
    p_analyze.add_argument("--max-hours", type=float, default=None)
    p_analyze.add_argument("--bin-minutes", type=float, default=5.0)
    p_analyze.add_argument("--include-unmapped-bugs", action="store_true")
    p_analyze.set_defaults(func=cmd_analyze)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
