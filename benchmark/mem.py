#!/usr/bin/env python3
"""Print peak RSS (KB) for one remap implementation: `python benchmark/mem.py base|opt`."""
import importlib.util, json, resource, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
name = sys.argv[1]
path = {"base": ROOT / "src/iterutils_baseline.py", "opt": ROOT / "src/iterutils_patched.py"}[name]
spec = importlib.util.spec_from_file_location("m", path)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
doc = json.load(open(ROOT / "corpus/real/compounds.json", encoding="utf-8"))
for _ in range(12):
    mod.remap(doc, visit=mod.default_visit)
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
