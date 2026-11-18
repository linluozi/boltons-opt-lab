#!/usr/bin/env python3
"""A/B benchmark for the boltons iterutils container-dispatch fast path.

Runs the *same* workload through the unmodified upstream file
(src/iterutils_baseline.py) and the patched file (src/iterutils_patched.py),
records every timed sample under benchmark/raw/, and writes
benchmark/baseline.json / benchmark/optimized.json with medians and IQRs.

Real corpus: 12 AWS resource-model JSON documents shipped with boto3
(Apache-2.0) plus qlustered/deepdiff's 869 KB compounds fixture (MIT).
No synthetic inputs are used for the headline number.
"""
import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = 20261118
SCALES = ("small", "medium", "large")
VISITS = ("identity", "redact")
SECRET_KEYS = {"SecretAccessKey", "secret", "token", "password"}


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def redact(path, key, value):
    if key in SECRET_KEYS:
        return key, "***"
    return key, value


def read_json(name):
    with open(ROOT / "corpus" / "real" / name, encoding="utf-8") as fh:
        return json.load(fh)


def doc_sha(name):
    h = hashlib.sha256()
    with open(ROOT / "corpus" / "real" / name, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def pick_scale_docs():
    boto = sorted(p.name for p in (ROOT / "corpus" / "real").glob("boto3-*.json"))
    sizes = {n: (ROOT / "corpus" / "real" / n).stat().st_size for n in boto}
    small = min(boto, key=sizes.get)
    medium = max(boto, key=sizes.get)
    return {"small": small, "medium": medium, "large": "compounds.json"}


def pct(values, q):
    # inclusive linear interpolation, same as statistics.quantiles(inclusive)
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo, hi = math.floor(pos), math.ceil(pos)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def stats(values):
    med = statistics.median(values)
    p25, p75 = pct(values, 0.25), pct(values, 0.75)
    mean = statistics.fmean(values)
    sd = statistics.stdev(values) if len(values) > 1 else 0.0
    return {"median_ms": med, "p25_ms": p25, "p75_ms": p75,
            "rel_stdev": (sd / mean) if mean else 0.0, "n": len(values)}


def timed(mod, doc, visit, iterations):
    remap = mod.remap
    t0 = time.perf_counter()
    out = None
    for _ in range(iterations):
        out = remap(doc, visit=visit)
    elapsed = time.perf_counter() - t0
    return elapsed, out


def run_scale(impl, mod, doc, visit_name, visit, scale, iterations, reps, warmup):
    for _ in range(warmup):
        timed(mod, doc, visit, max(1, iterations // 4))
    samples = []
    for rep in range(reps):
        elapsed, out = timed(mod, doc, visit, iterations)
        per_iter_ms = elapsed * 1000.0 / iterations
        samples.append(per_iter_ms)
        rec = {"impl": impl, "scale": scale, "visit": visit_name, "rep": rep,
               "iterations": iterations, "seconds": elapsed,
               "per_iter_ms": per_iter_ms, "seed": SEED}
        (ROOT / "benchmark" / "raw" / f"{impl}-{scale}-{visit_name}-{rep}.json").write_text(
            json.dumps(rec, indent=1) + "\n")
    return samples


def run_real_corpus(impl, mod, docs, visit_name, visit, reps, warmup):
    per_doc = {}
    for name, doc in docs.items():
        for _ in range(warmup):
            timed(mod, doc, visit, 1)
        vals = []
        for _ in range(reps):
            elapsed, _ = timed(mod, doc, visit, 1)
            vals.append(elapsed * 1000.0)
        per_doc[name] = vals
    totals = [sum(per_doc[n][i] for n in docs) for i in range(reps)]
    (ROOT / "benchmark" / "raw" / f"{impl}-real-boto3-{visit_name}.json").write_text(
        json.dumps({"impl": impl, "scale": "real-boto3", "visit": visit_name,
                    "seed": SEED, "documents": len(docs),
                    "per_doc_ms": per_doc, "total_ms": totals}, indent=1) + "\n")
    return totals


def env_block():
    cpu = "unknown"
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                cpu = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    mem_kb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                mem_kb = int(line.split()[1])
    except OSError:
        pass
    return {"cpu": cpu, "mem_total_mb": round(mem_kb / 1024),
            "python": sys.version.split()[0], "platform": platform.platform(),
            "seed": SEED}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=3)
    args = ap.parse_args()
    if args.reps < 5 or args.warmup < 3:
        raise SystemExit("need --reps >=5 and --warmup >=3")

    base = load("iterutils_baseline", ROOT / "src" / "iterutils_baseline.py")
    patched = load("iterutils_patched", ROOT / "src" / "iterutils_patched.py")
    mods = {"baseline": base, "optimized": patched}
    visits = {"identity": base.default_visit, "redact": redact}
    scale_docs = pick_scale_docs()
    docs = {name: read_json(name) for name in scale_docs.values()}
    all_docs = {p.name: read_json(p.name) for p in sorted((ROOT / "corpus" / "real").glob("boto3-*.json"))}

    iterations = {}
    for scale, name in scale_docs.items():
        size = (ROOT / "corpus" / "real" / name).stat().st_size
        iterations[scale] = max(1, int(round(20000 / max(size, 1) * 60)))
        iterations[scale] = min(iterations[scale], 128)

    result = {"environment": env_block(), "method": {
        "timer": "time.perf_counter()", "reps": args.reps, "warmup": args.warmup,
        "seed": SEED, "statistic": "median of per-iteration milliseconds",
        "corpus": "12 boto3 AWS resource models + deepdiff compounds fixture (real files, not generated)"},
        "scales": {}, "real_corpus": {}}

    for scale, name in scale_docs.items():
        result["scales"][scale] = {"doc": name, "doc_bytes": (ROOT / "corpus" / "real" / name).stat().st_size,
                                   "doc_sha256": doc_sha(name), "iterations": iterations[scale], "runs": {}}
        for vname in VISITS:
            for impl in ("baseline", "optimized"):
                vals = run_scale(impl, mods[impl], docs[name], vname, visits[vname],
                                 scale, iterations[scale], args.reps, args.warmup)
                result["scales"][scale]["runs"].setdefault(vname, {})[impl] = stats(vals)
                print(f"{scale:>6} {vname:>8} {impl:>9} median={statistics.median(vals):.4f} ms/iter")

    for vname in VISITS:
        for impl in ("baseline", "optimized"):
            totals = run_real_corpus(impl, mods[impl], all_docs, vname, visits[vname],
                                     args.reps, 1)
            result["real_corpus"].setdefault(vname, {})[impl] = {
                **stats(totals), "documents": len(all_docs)}
            print(f"real-boto3 {vname:>8} {impl:>9} median={statistics.median(totals):.3f} ms/doc-set")

    for vname in VISITS:
        for scale in SCALES:
            b = result["scales"][scale]["runs"][vname]["baseline"]["median_ms"]
            o = result["scales"][scale]["runs"][vname]["optimized"]["median_ms"]
            result["scales"][scale]["runs"][vname]["speedup"] = b / o
        b = result["real_corpus"][vname]["baseline"]["median_ms"]
        o = result["real_corpus"][vname]["optimized"]["median_ms"]
        result["real_corpus"][vname]["speedup"] = b / o

    base_doc = {"implementation": "upstream 79e4379? (see README)", "environment": result["environment"],
                "method": result["method"], "scales": {}, "real_corpus": {}}
    opt_doc = {"implementation": "patched src/iterutils_patched.py", "environment": result["environment"],
               "method": result["method"], "scales": {}, "real_corpus": {}}
    for scale in SCALES:
        base_doc["scales"][scale] = {"doc": result["scales"][scale]["doc"],
                                     "doc_bytes": result["scales"][scale]["doc_bytes"],
                                     "doc_sha256": result["scales"][scale]["doc_sha256"],
                                     "iterations": result["scales"][scale]["iterations"],
                                     "runs": {v: result["scales"][scale]["runs"][v]["baseline"] for v in VISITS}}
        opt_doc["scales"][scale] = {"doc": result["scales"][scale]["doc"],
                                    "doc_bytes": result["scales"][scale]["doc_bytes"],
                                    "doc_sha256": result["scales"][scale]["doc_sha256"],
                                    "iterations": result["scales"][scale]["iterations"],
                                    "runs": {v: result["scales"][scale]["runs"][v]["optimized"] for v in VISITS}}
    for vname in VISITS:
        base_doc["real_corpus"][vname] = result["real_corpus"][vname]["baseline"]
        opt_doc["real_corpus"][vname] = result["real_corpus"][vname]["optimized"]
    for vname in VISITS:
        r = result["real_corpus"][vname]
        base_doc["real_corpus"][vname]["speedup"] = r["speedup"]
        opt_doc["real_corpus"][vname]["speedup"] = r["speedup"]
    for scale in SCALES:
        for vname in VISITS:
            s = result["scales"][scale]["runs"][vname]["speedup"]
            base_doc["scales"][scale]["runs"][vname]["speedup"] = s
            opt_doc["scales"][scale]["runs"][vname]["speedup"] = s
    base_doc["command"] = "python benchmark/bench.py --reps 5 --warmup 3"
    opt_doc["command"] = base_doc["command"]
    base_doc["seed"] = SEED
    opt_doc["seed"] = SEED
    (ROOT / "benchmark" / "baseline.json").write_text(json.dumps(base_doc, indent=2) + "\n")
    (ROOT / "benchmark" / "optimized.json").write_text(json.dumps(opt_doc, indent=2) + "\n")
    print(json.dumps({s: {v: round(result["scales"][s]["runs"][v]["speedup"], 4) for v in VISITS} for s in SCALES}, indent=1))
    print({v: round(result["real_corpus"][v]["speedup"], 4) for v in VISITS})


if __name__ == "__main__":
    main()
