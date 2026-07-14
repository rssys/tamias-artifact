#!/usr/bin/env python3
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


ModePairs = Dict[str, List[Tuple[str, List[Tuple[float, int]]]]]


BUG_LABELS: Dict[str, str] = {
    "heap-use-after-free": "Mongo-1",
    "bad server role for applying a snapshot, exit for debugging": "Nuraft-1",
    "broke Reliable": "ETCD-1",
    "broke Resumable": "ETCD-1",
    "failed to open snapshot backend": "ETCD-2",
    "Ndb_metadata::compare(thd, thd_ndb->ndb, dbname, ndbtab, new_table_def)' failed": "MySQL-3",
    "virtual double ha_ndbcluster::read_time(uint, uint, ha_rows)": "MySQL-4",
    "index_stat_list == nullptr": "MySQL-2",
    "clusterUpdateSlotsConfigWith": "Redis-2",
    "myself->numslots": "Redis-1",
}

BugLabelCounts = Dict[str, Dict[str, Dict[str, Any]]]


def load_mode_json(path: Path) -> ModePairs:
    obj = json.loads(path.read_text(encoding="utf-8"))
    out: ModePairs = {}
    if not isinstance(obj, dict):
        return out
    for mode, instances in obj.items():
        if not isinstance(mode, str) or not isinstance(instances, list):
            continue
        parsed = []
        for item in instances:
            if not isinstance(item, list) or len(item) < 2:
                continue
            inst = str(item[0])
            pairs = []
            for pair in item[1]:
                if isinstance(pair, list) and len(pair) == 2:
                    pairs.append((float(pair[0]), int(pair[1])))
            pairs.sort(key=lambda x: x[0])
            parsed.append((inst, pairs))
        out[mode] = parsed
    return out


def stable_pairs_from_edge_rows(edge_rows: Any) -> List[Tuple[float, int]]:
    if not isinstance(edge_rows, list):
        return []
    events: List[Tuple[float, List[int]]] = []
    for item in edge_rows:
        if not isinstance(item, list) or len(item) != 2:
            continue
        try:
            t = float(item[0])
        except Exception:
            continue
        edges = []
        if isinstance(item[1], list):
            for edge in item[1]:
                try:
                    edges.append(int(edge))
                except Exception:
                    pass
        if edges:
            events.append((t, edges))
    events.sort(key=lambda x: x[0])
    seen: Set[int] = set()
    pairs: List[Tuple[float, int]] = []
    for t, edges in events:
        seen.update(edges)
        pairs.append((t, len(seen)))
    return pairs


def load_mode_stable_json(path: Path) -> ModePairs:
    obj = json.loads(path.read_text(encoding="utf-8"))
    out: ModePairs = {}
    if not isinstance(obj, dict):
        return out
    for mode, instances in obj.items():
        if not isinstance(mode, str) or not isinstance(instances, list):
            continue
        parsed = []
        for item in instances:
            if not isinstance(item, list) or len(item) < 3:
                continue
            inst = str(item[0])
            pairs = stable_pairs_from_edge_rows(item[2])
            if pairs:
                parsed.append((inst, pairs))
        if parsed:
            out[mode] = parsed
    return out


def merge_mode_jsons(paths: Iterable[Path]) -> ModePairs:
    merged: Dict[str, List[Tuple[str, List[Tuple[float, int]]]]] = defaultdict(list)
    for path in paths:
        for mode, instances in load_mode_json(path).items():
            merged[mode].extend(instances)
    return dict(merged)


def merge_mode_stable_jsons(paths: Iterable[Path]) -> ModePairs:
    merged: Dict[str, List[Tuple[str, List[Tuple[float, int]]]]] = defaultdict(list)
    for path in paths:
        for mode, instances in load_mode_stable_json(path).items():
            merged[mode].extend(instances)
    return dict(merged)


def load_mode_trial_counts(path: Path) -> Dict[str, int]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        return {}
    modes = [k for k, v in obj.items() if isinstance(k, str) and isinstance(v, list)]
    bugs_by_instance = obj.get("earliest_bugs_by_instance", {})
    if isinstance(bugs_by_instance, dict):
        count = len(bugs_by_instance)
        return {mode: count for mode in modes}
    return {mode: len(obj.get(mode, [])) for mode in modes}


def merge_mode_trial_counts(paths: Iterable[Path]) -> Dict[str, int]:
    merged: Dict[str, int] = defaultdict(int)
    for path in paths:
        for mode, count in load_mode_trial_counts(path).items():
            merged[mode] += int(count)
    return dict(merged)


def final_coverage_at(pairs: List[Tuple[float, int]], end_hours: Optional[float]) -> Optional[int]:
    if not pairs:
        return None
    if end_hours is None:
        return int(pairs[-1][1])
    end_sec = float(end_hours) * 3600.0
    last = None
    for t, cov in pairs:
        if t <= end_sec:
            last = int(cov)
        else:
            break
    return last


def coverage_summary(merged: ModePairs, end_hours: Optional[float]) -> List[Dict[str, Any]]:
    rows = []
    for mode in sorted(merged):
        vals = []
        for _inst, pairs in merged[mode]:
            v = final_coverage_at(pairs, end_hours)
            if v is not None:
                vals.append(v)
        if vals:
            stdev = statistics.stdev(vals) if len(vals) > 1 else 0.0
            rows.append({
                "mode": mode,
                "runs": len(vals),
                "mean": statistics.mean(vals),
                "stdev": stdev,
                "min": min(vals),
                "max": max(vals)
            })
        else:
            rows.append({"mode": mode, "runs": 0, "mean": "", "stdev": "", "min": "", "max": ""})
    return rows


def load_bug_counts(path: Path) -> Dict[str, Dict[str, int]]:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        return {}
    modes = [k for k, v in obj.items() if isinstance(k, str) and isinstance(v, list)]
    bugs_by_instance = obj.get("earliest_bugs_by_instance", {})
    if not isinstance(bugs_by_instance, dict):
        return {}
    counts: Dict[str, int] = defaultdict(int)
    for info in bugs_by_instance.values():
        if not isinstance(info, dict):
            continue
        typed = info.get("earliest_by_type", {})
        if not isinstance(typed, dict):
            continue
        for marker in typed.keys():
            if isinstance(marker, str) and marker:
                counts[marker] += 1
    return {mode: dict(counts) for mode in modes}


def merge_bug_counts(paths: Iterable[Path]) -> Dict[str, Dict[str, int]]:
    merged: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in paths:
        for mode, counts in load_bug_counts(path).items():
            for marker, count in counts.items():
                merged[mode][marker] += int(count)
    return {mode: dict(counts) for mode, counts in merged.items()}


def load_bug_label_counts(path: Path, *, include_unmapped: bool = False) -> BugLabelCounts:
    obj = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(obj, dict):
        return {}
    modes = [k for k, v in obj.items() if isinstance(k, str) and isinstance(v, list)]
    bugs_by_instance = obj.get("earliest_bugs_by_instance", {})
    if not isinstance(bugs_by_instance, dict):
        return {}

    counts: Dict[str, Dict[str, Any]] = {}
    for info in bugs_by_instance.values():
        if not isinstance(info, dict):
            continue
        typed = info.get("earliest_by_type", {})
        if not isinstance(typed, dict):
            continue
        seen: Dict[str, Set[str]] = defaultdict(set)
        for marker in typed.keys():
            if not isinstance(marker, str) or not marker:
                continue
            label = BUG_LABELS.get(marker)
            if label is None:
                if not include_unmapped:
                    continue
                label = marker
            seen[label].add(marker)
        for label, markers in seen.items():
            item = counts.setdefault(label, {"found_runs": 0, "markers": set()})
            item["found_runs"] += 1
            item["markers"].update(markers)

    normalized = {
        label: {
            "found_runs": int(item["found_runs"]),
            "markers": sorted(item["markers"]),
        }
        for label, item in counts.items()
    }
    return {mode: dict(normalized) for mode in modes}


def merge_bug_label_counts(paths: Iterable[Path], *, include_unmapped: bool = False) -> BugLabelCounts:
    merged: Dict[str, Dict[str, Any]] = defaultdict(lambda: defaultdict(lambda: {"found_runs": 0, "markers": set()}))
    for path in paths:
        for mode, labels in load_bug_label_counts(path, include_unmapped=include_unmapped).items():
            for label, item in labels.items():
                target = merged[mode][label]
                target["found_runs"] += int(item["found_runs"])
                target["markers"].update(item["markers"])
    return {
        mode: {
            label: {
                "found_runs": int(item["found_runs"]),
                "markers": sorted(item["markers"]),
            }
            for label, item in labels.items()
        }
        for mode, labels in merged.items()
    }


def merge_unmapped_bug_markers(paths: Iterable[Path]) -> Dict[str, Dict[str, int]]:
    merged: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for path in paths:
        for mode, counts in load_bug_counts(path).items():
            for marker, count in counts.items():
                if marker not in BUG_LABELS:
                    merged[mode][marker] += int(count)
    return {mode: dict(counts) for mode, counts in merged.items()}


def instance_to_binned_series(
    pairs: List[Tuple[float, int]],
    *,
    bin_minutes: float,
    max_hours: float
) -> Dict[int, int]:
    if not pairs:
        return {}
    bin_sec = float(bin_minutes) * 60.0
    max_sec = float(max_hours) * 3600.0
    if bin_sec <= 0 or max_sec <= 0:
        return {}
    pairs = [(t, c) for t, c in pairs if t <= max_sec]
    if not pairs:
        return {}
    max_bin = max(0, int(math.ceil(max_sec / bin_sec)) - 1)
    out: Dict[int, int] = {}
    i = 0
    cur = int(pairs[0][1])
    for b in range(max_bin + 1):
        end_t = (b + 1) * bin_sec
        while i < len(pairs) and pairs[i][0] <= end_t:
            cur = int(pairs[i][1])
            i += 1
        out[b] = cur
    return out


def aggregate_mode_stats(
    instances: List[Tuple[str, List[Tuple[float, int]]]],
    *,
    bin_minutes: float,
    max_hours: float
) -> Tuple[List[float], List[float], List[float], List[float]]:
    series = [instance_to_binned_series(pairs, bin_minutes=bin_minutes, max_hours=max_hours)
              for _inst, pairs in instances]
    max_bin = max((max(s.keys()) for s in series if s), default=-1)
    if max_bin < 0:
        return [], [], [], []
    x_hours = [0.0]
    mean_y = [0.0]
    min_y = [0.0]
    max_y = [0.0]
    for b in range(max_bin + 1):
        vals = [s[b] for s in series if b in s]
        if not vals:
            continue
        x_hours.append(((b + 1) * float(bin_minutes) * 60.0) / 3600.0)
        mean_y.append(float(statistics.mean(vals)))
        min_y.append(float(min(vals)))
        max_y.append(float(max(vals)))
    return x_hours, mean_y, min_y, max_y


def safe_name(raw: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw.strip()).strip("-")
    return name or "run"
