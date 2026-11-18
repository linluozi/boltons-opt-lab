"""Differential test: patched iterutils must be byte-identical to upstream.

Every sample is an independently generated document (fresh RNG state derived
from its index, never a loop that reuses one object), and samples are deduped
by sha256(repr(document)).  A sample only counts if its digest is unique.
"""
import collections
import hashlib
import importlib.util
import json
import math
import pickle
import random
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEED = 20261118


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "src" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BASE = load("iterutils_baseline", "iterutils_baseline.py")
PATCHED = load("iterutils_patched", "iterutils_patched.py")


class Box:
    def __init__(self, v):
        self.v = v

    def __eq__(self, other):
        return isinstance(other, Box) and self.v == other.v


class MyMap(collections.abc.Mapping):
    def __init__(self, data=None):
        self._d = dict(data or {})

    def __getitem__(self, k):
        return self._d[k]

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    def update(self, items):
        self._d.update(items)


class MySeq(collections.abc.Sequence):
    def __init__(self, data=None):
        self._d = list(data or [])

    def __getitem__(self, i):
        return self._d[i]

    def __len__(self):
        return len(self._d)


Point = collections.namedtuple("Point", "x y")

REAL = sorted((ROOT / "corpus" / "real").glob("*.json"))


def edge_docs():
    yield {}
    yield []
    yield {"": None}
    yield {"k": [None, float("nan"), float("inf"), -0.0, 2 ** 70]}
    yield {"ключ": {"日本語": ["值", b"bytes", "emoji \U0001F600"]}}
    yield [[[[[[[[[[0]]]]]]]]]]
    yield {"wide": {f"k{i}": i for i in range(200)}}
    yield {"deep": eval("{" + "'a':{" * 40 + "1" + "}" * 40 + "}")}  # noqa: S307 - literal only
    yield collections.OrderedDict([("b", 2), ("a", 1)])
    yield {"od": collections.OrderedDict([("x", [1, 2])]), "dd": collections.defaultdict(list, {"z": 1})}
    yield {"named": Point(1, "two"), "tuple": (1, [2, 3]), "set": {1, 2, 3}, "frozen": frozenset([4, 5])}
    yield {"map": MyMap({"a": 1, "b": [2]}), "seq": MySeq([1, {"x": 2}])}
    yield {"box": Box(3), "bytes": b"\x00\xff"}
    yield {"dup": (lambda a: {"a": a, "b": a})({"shared": [1]}), "alias": [1, 1, 1]}
    yield {"none": None, "true": True, "false": False, "neg": -1, "big": 10 ** 30}


def mutate(doc, rng, depth=0):
    """Return an independent mutated deep copy of doc."""
    if rng.random() < 0.18:
        return rng.choice([
            None, "", "\U0001F600", -1, 0, 3.5, float("nan"), b"\x01",
            [], {}, (), {1, 2}, {"": None},
            collections.OrderedDict([("u", 1)]),
        ])
    if isinstance(doc, dict):
        out = {}
        for k, v in doc.items():
            r = rng.random()
            if r < 0.12:
                continue
            if r < 0.20:
                out[str(k) + "\u00e9"] = mutate(v, rng, depth + 1)
            else:
                out[k] = mutate(v, rng, depth + 1)
        if rng.random() < 0.25:
            out["new_\u4e2d"] = mutate(doc, rng, depth + 1) if depth < 2 else 1
        return out
    if isinstance(doc, list):
        out = [mutate(v, rng, depth + 1) for v in doc if rng.random() > 0.1]
        if rng.random() < 0.2:
            out.append(mutate(doc, rng, depth + 1) if depth < 2 and doc else 1)
        return out
    if depth < 3 and rng.random() < 0.08:
        return mutate({"wrapped": doc}, rng, depth + 1)
    return doc


def corpus():
    docs = list(edge_docs())
    parents = []
    for path in REAL:
        with open(path, encoding="utf-8") as fh:
            parents.append(json.load(fh))
    # 12 boto3 docs + compounds: 260 independently seeded mutations each
    for pi, parent in enumerate(parents):
        for i in range(260):
            rng = random.Random(f"{SEED}:{pi}:{i}")  # fresh state per sample
            docs.append(mutate(parent, rng))
    return docs


def digest(obj):
    return hashlib.sha256(pickle.dumps(obj, protocol=4)).hexdigest()


def outcome(mod, doc):
    """Serialize the result, or the exact exception both implementations raise."""
    try:
        return "ok", digest(mod.remap(doc, visit=mod.default_visit))
    except Exception as exc:  # remap rejects scalar roots; both sides must agree
        return "err", type(exc).__name__, str(exc)


def unique_samples():
    seen, out = set(), []
    for doc in corpus():
        key = hashlib.sha256(repr(doc).encode()).hexdigest()
        if key in seen:
            continue
        seen.add(key)
        out.append((key, doc))
    return out


SAMPLES = unique_samples()


def test_sample_budget():
    assert len(SAMPLES) >= 1000, len(SAMPLES)


def test_byte_identical_on_unique_samples():
    mismatches = []
    for key, doc in SAMPLES:
        a = outcome(BASE, doc)
        b = outcome(PATCHED, doc)
        if a != b:
            mismatches.append((key, a, b))
    assert not mismatches, mismatches[:3]
    print(f"INDEPENDENT_UNIQUE_SAMPLES={len(SAMPLES)} mismatches={len(mismatches)}")


def test_redaction_transform_identical():
    def redact(path, key, value):
        return (key, "***") if key == "secret" else (key, value)

    docs = [{"secret": "a", "nested": [{"secret": "b"}, 1]}, {"x": {"secret": None}}, {}]
    for doc in docs:
        assert digest(BASE.remap(doc, visit=redact)) == digest(PATCHED.remap(doc, visit=redact))


def test_subclasses_fall_back():
    od = collections.OrderedDict([("b", 2), ("a", 1)])
    assert isinstance(PATCHED.remap(od), collections.OrderedDict)
    assert PATCHED.remap(od) == BASE.remap(od)
    class L(list):
        pass
    assert type(PATCHED.remap(L([1, {"x": 2}]))) is L
    assert PATCHED.remap(L([1, {"x": 2}])) == BASE.remap(L([1, {"x": 2}]))
    assert PATCHED.remap(Point(1, 2)) == BASE.remap(Point(1, 2))
    assert PATCHED.remap(MyMap({"a": 1})) == BASE.remap(MyMap({"a": 1}))
