# Tamias Artifact

This repo contains the artifact for the SOSP 2026 paper "Tamias:
Feedback-Guided Systematic Concurrency Exploration for Transparent Distributed
System Fuzzing".

## Structure

This repository contains scripts for running the Tamias fuzzer images,
collecting results, and producing the evaluation figures and tables. The
default configuration uses the pre-built images listed in `configs/images.json`.
To build the images yourself, see https://github.com/tamias-dev/tamias. For
artifact evaluation, we recommend using the pre-built images because building
all target images takes a long time.

- `configs/images.json`: Docker image names and digests.
- `configs/experiments.json`: targets, images, and fuzzer options.
- `configs/workloads/*.json`: machine assignment for each experiment config.
- `configs/env/*.json`: target runtime configs.
- `bugs/README.md`: reported bugs and evidence bundles.
- `scripts/setup/*`: setup helpers.
- `scripts/run_workload.py`: launch, collect, and analyze a workload file.
- `scripts/make_paper_outputs.py`: make paper-style figures and tables.

Names used below:

- `trial`: one independent fuzzing run.
- `workers`: fuzzing workers inside one trial. This is `num_executors` in
  `configs/env/*.json`.
- `machine`: a k3s machine such as `node1`.
- `config`: one experiment setting, such as `redis + random + R-ISM`.
- `concurrent_trials_per_node`: how many trials of one config may run at the
  same time on one machine.

## Hardware

Use Ubuntu 22.04 or a similar Linux image, KVM support, and a k3s cluster. More
cores or hardware threads help because the experiments run multiple trials in
parallel. Tamias runs privileged containers because the fuzzers use KVM.

We recommend CloudLab `c6320` machines. The paper-scale runs are large: one
ablation target has six configs, and each config uses 10 trials for 24 hours.
The workload files pack trials onto machines to reduce wall-clock time.

## Setup

Run setup in this order. On CloudLab, we assume every cluster machine has a
large data disk mounted at `/data`, and this repository is checked out at
`/data/tamias-artifact` on the control machine. Keep this path short because
Nyx/QEMU uses UNIX sockets under the run directory; the launcher checks this
before starting Tamias jobs.

⚠️ Warning: the setup commands format `DEVICE` if it has no filesystem. Use them
only on the empty CloudLab data disk, not on a disk with data you want to keep.

```bash
DEVICE=/dev/sdb

# Control machine: mount /data, make it persistent, and make it writable.
sudo mkdir -p /data
if ! mountpoint -q /data; then
  FSTYPE="$(sudo blkid -o value -s TYPE "$DEVICE" 2>/dev/null || true)"
  if [ -z "$FSTYPE" ]; then
    echo "WARNING: formatting $DEVICE. This destroys data on that device."
    sudo mkfs.ext4 -F "$DEVICE"
  elif [ "$FSTYPE" != "ext4" ]; then
    echo "$DEVICE has filesystem type $FSTYPE; expected ext4 or empty device"
    exit 1
  fi
  sudo mount "$DEVICE" /data
  UUID="$(sudo blkid -o value -s UUID "$DEVICE")"
  if ! grep -q "UUID=${UUID}[[:space:]]/data[[:space:]]" /etc/fstab; then
    echo "UUID=${UUID} /data ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
  fi
fi
sudo chown -R "$USER:$(id -gn)" /data

git clone https://github.com/rssys/tamias-artifact.git /data/tamias-artifact
cd /data/tamias-artifact

# CloudLab: mount the large data disk at /data on every cluster machine.
# This also chowns /data to the current user. Change --device if needed.
./scripts/setup/setup_cloudlab_data_disk.sh \
  --nodes <control>,<worker1>,<worker2> \
  --device "$DEVICE"

# Tools used by the scripts and commands below.
sudo apt-get update
sudo apt-get install -y python3-venv rsync jq
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# k3s server on the control machine.
./scripts/setup/install_k3s_server.sh
export KUBECONFIG="${HOME}/.kube/config"
```

Copy scripts and configs to each worker:

```bash
./scripts/setup/sync_workers.sh \
  --root /data/tamias-artifact \
  --nodes <worker1>,<worker2>
```

Join each worker using the server URL and token printed by
`install_k3s_server.sh`:

```bash
sudo /data/tamias-artifact/scripts/setup/join_k3s_worker.sh \
  --server https://<control-ip>:6443 \
  --token <token>
```

Then finish the cluster setup from the control machine:

```bash
# Label every worker used in workload files.
./scripts/setup/label_nodes.sh \
  node1=<k8s-node-name-1> \
  node2=<k8s-node-name-2> \
  node3=<k8s-node-name-3> \
  node4=<k8s-node-name-4> \
  node5=<k8s-node-name-5> \
  node6=<k8s-node-name-6> \
  node7=<k8s-node-name-7> \
  node8=<k8s-node-name-8> \
  node9=<k8s-node-name-9>

# Enable the KVM option needed by QEMU-Nyx.
./scripts/setup/enable_kvm_vmware_backdoor.sh \
  --nodes node1,node2,node3,node4,node5,node6,node7,node8,node9

# Check setup.
kubectl get nodes -L short,kvm
kubectl get pods -A
./scripts/setup/preflight.sh /data/tamias-artifact
```

Expected output:

- `kubectl get nodes -L short,kvm`: all worker machines show `Ready`; the
  `short` and `kvm` columns are present.
- `kubectl get pods -A`: k3s system pods are `Running` or `Completed`; no pod is
  in `CrashLoopBackOff`.
- `preflight.sh`: ends with `Preflight passed.`

## Kick the Tires Check

This checks that the cluster, images, KVM, collection, and analysis work. It
does not try to reproduce paper numbers. It runs one short ETCD Tamias config.

If you are in a new shell, activate the Python environment first:

```bash
. .venv/bin/activate
```

Check `configs/workloads/ablation_etcd.json` and edit the `nodes` fields if
your cluster uses different machine labels. See
[Run One Target-Scheduler Ablation](#run-one-target-scheduler-ablation) for how
workload fields map to machines and trials. Then run:

```bash
TARGET=etcd
SCHEDULER=random
JOBS="${TARGET}_${SCHEDULER}_mut_all"
RUN_TS="${TARGET}_${SCHEDULER}_kick_$(date -u +%Y%m%d_%H%M)"
MANIFEST="runs/workload-ablation_${TARGET}-${RUN_TS}.json"
echo "MANIFEST=${MANIFEST}"

./scripts/run_workload.py plan \
  --config "configs/workloads/ablation_${TARGET}.json" \
  --jobs "$JOBS" \
  --duration-minutes 10 \
  --trials 1

./scripts/run_workload.py launch \
  --config "configs/workloads/ablation_${TARGET}.json" \
  --jobs "$JOBS" \
  --run-ts "$RUN_TS" \
  --duration-minutes 10 \
  --trials 1 \
  --output-owner-current-user

./scripts/run_workload.py collect \
  --manifest "$MANIFEST"

./scripts/run_workload.py analyze \
  --manifest "$MANIFEST" \
  --max-hours 0.167 \
  --bin-minutes 1
```

Expected files include `coverage_summary.csv`, `stable_coverage_summary.csv`,
`bug_counts.csv`, `table4_bug_counts.md`, `coverage.pdf`, and
`analysis_metadata.json`.

## Experiments

These experiments are fuzzing runs. Absolute numbers are not deterministic
across runs because of machine differences and inherent fuzzing randomness. We
recommend comparing trends across trials.

Analysis also writes `stable_coverage_summary.csv` for Tamias runs as a small
feedback-determinism diagnostic. Paper-style figures and tables use
`coverage_summary.csv`.

Each run below prints `MANIFEST` before launch. Save that path for collect,
analyze, preview, and stop commands in a new shell.

### Evaluation: Bug Finding

See `bugs/README.md` for the reported bugs and evidence bundles. Table 4 bug
counts are produced from each run's collected output.

### Evaluation: Ablation Study

This evaluation compares mutation options for each `<target, scheduler>`.
It produces Figures 8-9, Table 3, and Table 4 slices.

Targets: `redis`, `mongo`, `mysql`, `etcd`, `nuraft`, `braft`.

Schedulers: `random`, `pct`.

#### Run One Target-Scheduler Ablation

An ablation config is `<target, scheduler, mut>`, for example
`<redis, random, mut_all>`. A target-scheduler ablation runs the three mut
configs for that target and scheduler: `rand` (R/P), `mut_inp` (R-IM/P-IM), and
`mut_all` (R-ISM/P-ISM).

Before each run, check the workload file directly and edit it if needed. Example
for one config:

```jsonc
{
  "id": "redis_random_mut_all",          // <target, scheduler, mut>
  "experiment": "ablation",
  "target": "redis",
  "scheduler": "random",
  "mode": "mut_all",
  "nodes": ["node5", "node6"],           // 10 trials / 5 per machine = 2 machines
  "trials": 10,                          // total trials for this config
  "concurrent_trials_per_node": 5        // 11 workers/trial * 5 = 55 threads
}
```

`configs/env/default.json` sets `num_executors` (`workers`) to 11. If a machine
has 55 hardware threads, use 5 concurrent trials because `11 * 5 = 55`. With 10
trials for one `<target, scheduler, mut>` config and 5 trials per machine, list
two machines in `nodes`. One machine should run trials for only one config.

```bash
# Pick one target: redis, mongo, mysql, etcd, nuraft, or braft.
TARGET=redis

# Pick one scheduler: random or pct.
SCHEDULER=random

# This target-scheduler run includes three mut configs.
JOBS="${TARGET}_${SCHEDULER}_rand,${TARGET}_${SCHEDULER}_mut_inp,${TARGET}_${SCHEDULER}_mut_all"

# Build a run timestamp and manifest path.
RUN_TS="${TARGET}_${SCHEDULER}_ablation_$(date -u +%Y%m%d_%H%M)"
MANIFEST="runs/workload-ablation_${TARGET}-${RUN_TS}.json"
echo "MANIFEST=${MANIFEST}"
echo "Save this path if you collect, analyze, or stop the run in a new shell later."

# Use 30 and 0.5 for a short check; use 1440 and 24 for paper-scale.
# Minutes to run the fuzzers.
RUN_MINUTES=1440
# Hours of collected data to analyze from the beginning of the run.
ANALYZE_HOURS=24

./scripts/run_workload.py plan \
  --config "configs/workloads/ablation_${TARGET}.json" \
  --jobs "$JOBS"

./scripts/run_workload.py launch \
  --config "configs/workloads/ablation_${TARGET}.json" \
  --jobs "$JOBS" \
  --run-ts "$RUN_TS" \
  --duration-minutes "$RUN_MINUTES" \
  --output-owner-current-user

./scripts/run_workload.py collect \
  --manifest "$MANIFEST"

./scripts/run_workload.py analyze \
  --manifest "$MANIFEST" \
  --max-hours "$ANALYZE_HOURS" \
  --bin-minutes 5  # Plot coverage using 5-minute buckets.
```

Make paper-style output for one target and one scheduler:

```bash
./scripts/make_paper_outputs.py \
  --kind ablation_study \
  --analysis-dir "results/ablation_${TARGET}-${RUN_TS}/analysis" \
  --target "$TARGET" \
  --scheduler "$SCHEDULER"
```

Random-walk output goes under `paper_outputs/<target>_random_walk/`. PCT output
goes under `paper_outputs/<target>_pct/`.

The table columns match the paper: `R`, `R-IM`, `R-ISM (Tamias)` or `P`,
`P-IM`, `P-ISM (Tamias)`.

#### During a Run

Check status:

```bash
kubectl get jobs -n tamias-eval -o wide
kubectl get pods -n tamias-eval -o wide --sort-by=.metadata.creationTimestamp
```

Make a temporary plot without stopping the run:

```bash
PREVIEW_DIR="results/ablation_${TARGET}-${RUN_TS}/preview"

./scripts/run_workload.py collect \
  --manifest "$MANIFEST" \
  --out-dir "$PREVIEW_DIR/raw"

./scripts/run_workload.py analyze \
  --manifest "$MANIFEST" \
  --input-dir "$PREVIEW_DIR/raw" \
  --out-dir "$PREVIEW_DIR/analysis" \
  --max-hours "$ANALYZE_HOURS" \
  --bin-minutes 5  # Plot coverage using 5-minute buckets.
```

The temporary plot is `$PREVIEW_DIR/analysis/coverage.pdf`.

Stop this run:

```bash
for m in $(jq -r '.jobs[].manifest' "$MANIFEST"); do
  kubectl delete job -n tamias-eval --ignore-not-found $(jq -r '.jobs[].job_name' "$m")
done
```

#### Run Multiple Target-Scheduler Experiments

Run the same block in separate shells with different `TARGET`, `SCHEDULER`, and
`RUN_TS` values. Before launching, run `plan` for each one. Simultaneous runs
should not share machines in their workload files.

### Evaluation: Comparison with Prior Work

This evaluation compares Tamias with DistFuzz for each target. It produces
Figure 7.

Targets: `etcd`, `nuraft`, `redis_raft`, `braft`.

Systems: `tamias`, `distfuzz`.

#### Run One Target Comparison

A comparison config is `<target, system>`, for example `<braft, tamias>`. A
target comparison runs the two system configs for that target: `tamias` and
`distfuzz`.

Before each run, check `configs/workloads/distfuzz_comparison.json` directly
and edit it if needed. Example for one config:

```jsonc
{
  "id": "braft_tamias",                 // <target, system>
  "experiment": "distfuzz_comparison",
  "target": "braft",
  "system": "tamias",
  "nodes": ["node7"],                   // one machine for this config
  "trials": 10,                         // total trials for this config
  "concurrent_trials_per_node": 5       // 5 CPUs/workers per trial
}
```

Each comparison trial uses 5 CPUs/workers. With 10 trials for one
`<target, system>` config and 5 trials per machine, one machine runs the trials
in two batches. One machine should run trials for only one config.

```bash
# Pick one target: etcd, nuraft, redis_raft, or braft.
TARGET=braft
JOBS="${TARGET}_tamias,${TARGET}_distfuzz"

# Build a run timestamp and manifest path.
RUN_TS="${TARGET}_distfuzz_$(date -u +%Y%m%d_%H%M)"
MANIFEST="runs/workload-distfuzz_comparison-${RUN_TS}.json"
echo "MANIFEST=${MANIFEST}"
echo "Save this path if you collect, analyze, or stop the run in a new shell later."

# Use 30 and 0.5 for a short check; use 1440 and 24 for paper-scale.
# Minutes to run the fuzzers.
RUN_MINUTES=1440
# Hours of collected data to analyze from the beginning of the run.
ANALYZE_HOURS=24

# Check the machines, trials, and concurrent trials before launching.
./scripts/run_workload.py plan \
  --config configs/workloads/distfuzz_comparison.json \
  --jobs "$JOBS"

# Launch the Tamias and DistFuzz jobs for this target.
./scripts/run_workload.py launch \
  --config configs/workloads/distfuzz_comparison.json \
  --jobs "$JOBS" \
  --run-ts "$RUN_TS" \
  --duration-minutes "$RUN_MINUTES" \
  --output-owner-current-user

# Copy results from worker machines.
./scripts/run_workload.py collect \
  --manifest "$MANIFEST"

# Produce coverage and bug summary files.
./scripts/run_workload.py analyze \
  --manifest "$MANIFEST" \
  --max-hours "$ANALYZE_HOURS" \
  --bin-minutes 5  # Plot coverage using 5-minute buckets.

# Produce the paper-style Figure 7 output for this target.
./scripts/make_paper_outputs.py \
  --kind distfuzz_comparison \
  --analysis-dir "results/distfuzz_comparison-${RUN_TS}/analysis" \
  --target "$TARGET"
```

Expected files for one target are under
`results/distfuzz_comparison-${RUN_TS}/analysis/paper_outputs/${TARGET}_distfuzz/`.

Tamias jobs use target-specific comparison images and env files from
`configs/experiments.json`. DistFuzz jobs use `congyuliu/tamias_distfuzz_eval`.
Pinned image digests are in `configs/images.json`.

#### During a Run

Check status:

```bash
kubectl get jobs -n tamias-eval -o wide
kubectl get pods -n tamias-eval -o wide --sort-by=.metadata.creationTimestamp
```

Make a temporary plot without stopping the run:

```bash
PREVIEW_DIR="results/distfuzz_comparison-${RUN_TS}/preview"

./scripts/run_workload.py collect \
  --manifest "$MANIFEST" \
  --out-dir "$PREVIEW_DIR/raw"

./scripts/run_workload.py analyze \
  --manifest "$MANIFEST" \
  --input-dir "$PREVIEW_DIR/raw" \
  --out-dir "$PREVIEW_DIR/analysis" \
  --max-hours "$ANALYZE_HOURS" \
  --bin-minutes 5  # Plot coverage using 5-minute buckets.
```

The temporary plot is `$PREVIEW_DIR/analysis/coverage.pdf`.

Stop this run:

```bash
for m in $(jq -r '.jobs[].manifest' "$MANIFEST"); do
  kubectl delete job -n tamias-eval --ignore-not-found $(jq -r '.jobs[].job_name' "$m")
done
```

#### Run Multiple Target Comparisons

Run the same block in separate shells with different `TARGET` and `RUN_TS`
values. Before launching, run `plan` for each one. Simultaneous runs should not
share machines in `configs/workloads/distfuzz_comparison.json`.

## Cleanup

Remove old generated output before long runs:

```bash
NODES="node1 node2"
for n in $NODES; do
  ssh "$n" 'find /data/tamias-artifact/workdir -mindepth 1 -maxdepth 1 -exec rm -rf -- {} +'
done
```

## License

This artifact is licensed under the Apache License 2.0. See `LICENSE`.
