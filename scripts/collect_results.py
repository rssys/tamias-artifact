#!/usr/bin/env python3
import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


COVERAGE_RE = re.compile(r"^\d+_(\d+)\+(\d+)")
DISTFUZZ_CC_RE = re.compile(r"cc:(\d+)\(")

BUG_TYPE_MARKERS: List[str] = [
    "virtual double ha_ndbcluster::read_time(uint, uint, ha_rows)",
    "Ndb_metadata::compare(thd, thd_ndb->ndb, dbname, ndbtab, new_table_def)' failed",
    "get_metadata(Ndb *, const",
    "index_stat_list == nullptr",
    "m_used_cnt == 0' failed",
    "heap-use-after-free",
    "failed to open snapshot backend",
    "bad server role for applying a snapshot, exit for debugging",
    "broke Reliable",
    "broke Resumable",
    "fatal error: invalid pointer found on stack",
    "SIGSEGV: segmentation violation",
    "fatal error: found bad pointer in Go heap",
    "clusterUpdateSlotsConfigWith",
    "myself->numslots",
]


def parse_created_at(raw: str) -> float:
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)([+-]\d{2}:\d{2})$", text)
    if match:
        head, frac, tz = match.groups()
        text = f"{head}.{frac[:6]}{tz}"
    return datetime.fromisoformat(text).timestamp()


def corpus_edge_pairs(corpus_dir: Path) -> List[Tuple[float, List[int]]]:
    rows = []
    if not corpus_dir.is_dir():
        return rows
    for entry in corpus_dir.iterdir():
        entry_json = entry / "entry.json"
        if not entry.is_dir() or not entry_json.is_file():
            continue
        try:
            obj = json.loads(entry_json.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        try:
            created = parse_created_at(str(obj.get("created_at", "")))
        except Exception:
            created = entry.stat().st_mtime
        new_cov = []
        if isinstance(obj.get("new_stable_cov"), list):
            for item in obj["new_stable_cov"]:
                try:
                    new_cov.append(int(item))
                except Exception:
                    pass
        if new_cov:
            rows.append((created, new_cov))
    if not rows:
        return []
    rows.sort(key=lambda x: x[0])
    base = rows[0][0]
    return [(ts - base, cov) for ts, cov in rows]


def tamias_coverage_pairs(corpus_dir: Path) -> List[Tuple[float, int]]:
    pairs = []
    if not corpus_dir.is_dir():
        return pairs
    for entry in corpus_dir.iterdir():
        if not entry.is_dir():
            continue
        match = COVERAGE_RE.match(entry.name)
        if not match:
            continue
        cov = int(match.group(1)) + int(match.group(2))
        pairs.append((entry.stat().st_mtime, cov))
    if not pairs:
        return []
    pairs.sort(key=lambda x: x[0])
    base = pairs[0][0]
    return [(ts - base, cov) for ts, cov in pairs]


def read_bug_timestamp(bug_dir: Path) -> float:
    ts_path = bug_dir / "timestamp"
    try:
        raw = ts_path.read_text(encoding="utf-8", errors="replace").strip()
        if raw:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        pass
    return bug_dir.stat().st_mtime


def marker_for_bug_dir(bug_dir: Path) -> Optional[str]:
    for root, _dirs, files in os.walk(bug_dir):
        for name in files:
            path = Path(root) / name
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            for marker in BUG_TYPE_MARKERS:
                if marker in content:
                    return marker
    return None


def collect_bugs(instance_dir: Path) -> Dict[str, Any]:
    bugs_root = instance_dir / "workdir" / "bugs"
    earliest: Dict[str, Dict[str, Any]] = {}
    unmatched = []
    if not bugs_root.is_dir():
        return {"earliest_by_type": earliest, "unmatched_bug_paths": unmatched}
    for bug_dir in sorted(p for p in bugs_root.iterdir() if p.is_dir()):
        ts = read_bug_timestamp(bug_dir)
        marker = marker_for_bug_dir(bug_dir)
        if marker is None:
            unmatched.append(str(bug_dir))
            continue
        current = earliest.get(marker)
        if current is None or ts < float(current["timestamp_unix"]):
            earliest[marker] = {
                "bug_path": str(bug_dir),
                "timestamp_unix": ts,
                "timestamp": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            }
    return {"earliest_by_type": earliest, "unmatched_bug_paths": unmatched}


def collect_tamias(run_dir: Path, run_name: str) -> Dict[str, Any]:
    entries = []
    bugs_by_instance = {}
    if run_dir.is_dir():
        for inst_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            corpus_dir = inst_dir / "workdir" / "corpus"
            pairs = tamias_coverage_pairs(corpus_dir)
            edge_pairs = corpus_edge_pairs(corpus_dir)
            if pairs or edge_pairs:
                entries.append([inst_dir.name, pairs, edge_pairs])
            bugs_by_instance[inst_dir.name] = collect_bugs(inst_dir)
    return {"earliest_bugs_by_instance": bugs_by_instance, run_name: entries}


def parse_distfuzz_plot_curve(path: Path) -> List[List[float]]:
    points: List[List[float]] = [[0.0, 0]]
    base_us = None
    last_cc = 0
    if not path.is_file():
        return points
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line:
                continue
            parts = line.split()
            try:
                ts_us = int(parts[0])
            except Exception:
                continue
            match = DISTFUZZ_CC_RE.search(line)
            if not match:
                continue
            cc = int(match.group(1))
            if base_us is None:
                base_us = ts_us
            rel_s = (ts_us - base_us) / 1000000.0
            if cc != last_cc:
                points.append([rel_s, cc])
                last_cc = cc
    return points


def collect_distfuzz(run_dir: Path, run_name: str) -> Dict[str, Any]:
    entries = []
    bugs_by_instance = {}
    if run_dir.is_dir():
        for inst_dir in sorted(p for p in run_dir.iterdir() if p.is_dir()):
            plot_curve = inst_dir / "artifacts" / "plot-curve"
            if plot_curve.is_file():
                entries.append([inst_dir.name, parse_distfuzz_plot_curve(plot_curve)])
            bugs_by_instance[inst_dir.name] = {"earliest_by_type": {}, "unmatched_bug_paths": []}
    return {"earliest_bugs_by_instance": bugs_by_instance, run_name: entries}


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect Tamias or DistFuzz run output into artifact JSON.")
    parser.add_argument("runner", choices=["tamias", "distfuzz"])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--run-name", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.runner == "tamias":
        result = collect_tamias(args.run_dir, args.run_name)
    else:
        result = collect_distfuzz(args.run_dir, args.run_name)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
