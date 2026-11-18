# boltons-opt-lab

**boltons** is a stdlib-only Python utility library; this patch adds exact-type fast paths to `boltons.iterutils.default_enter`/`default_exit`, cutting the container-dispatch tax that `remap()` pays on every plain dict/list node.

Upstream: https://github.com/mahmoud/boltons

Baseline commit: `d31d821d56c8f7756af9c30a864077f2e5855321` (2026-10-25). Patch: 14 insertions / 2 deletions in one file, `patch.diff`.

## 结果（中位数，真实文档）

| 规模 | 文档 | 基线中位数 | 优化后中位数 | 加速比 |
|---|---|---|---|---|
| small (identity) | boto3-dynamodb.json, 3,849 B | 0.2527 ms | 0.1750 ms | **1.4438x** |
| medium (identity) | boto3-ec2.json, 76,922 B | 4.5247 ms | 3.1898 ms | **1.4185x** |
| large (identity) | compounds.json, 869,246 B | 16.2598 ms | 12.3510 ms | **1.3165x** |
| small (redact) | same | 0.3206 ms | 0.1769 ms | 1.8128x |
| medium (redact) | same | 4.8746 ms | 3.2908 ms | 1.4813x |
| large (redact) | same | 18.1124 ms | 13.0356 ms | 1.3895x |

三个规模 identity 加速比的中位数 = **1.4185x**；G4 最大规模 = 1.3165x。

**真实语料（非构造输入）**：6 份 boto3 自带 AWS resource-model JSON（共 167868 B）+ deepdiff 的 869 KB `compounds.json`，整份文档 remap，identity **1.4101x**（9.589→6.800 ms/doc-set），redact **1.4738x**（10.288→6.981 ms/doc-set）。

p25/p75 与原始样本在 `benchmark/raw/`（64 个文件）；large-redact 的 rel-stdev 为 18.46%（>10%），噪声来源是 869 KB 文档反复分配旧代对象触发的 GC 与 page fault，已保留全部 5 个原始样本，未做剔除。

## 差分测试

`tests/test_diff.py`：**1,472 个去重后的独立样本**，0 差异。去重方式是对每个样本的 `repr(doc)` 取 sha256，只保留首次出现的 digest；每个样本用独立种子 `f"{SEED}:{parent}:{i}"` 生成，不是在循环里复用同一个对象。比对的是 `pickle.dumps(result, protocol=4)` 的 sha256（逐字节），以及两侧抛出的异常类型/消息。样本覆盖空容器、单元素、极深/极宽、非 ASCII 键、None/NaN/inf、bytes、tuple/set/namedtuple、`OrderedDict`/`defaultdict`/`UserDict`-style Mapping、list/dict 子类、别名与重复引用。

## 质量闸门（每条可复现）

| 闸门 | 结论 | 复现命令 |
|---|---|---|
| G1 上游测试 | **通过**：621 passed, 22 subtests, 0 failed（基线 621 passed / 3.20s，打补丁后 621 passed / 2.25s） | `git clone https://github.com/mahmoud/boltons.git /tmp/b && cd /tmp/b && git apply --check /path/patch.diff && git apply /path/patch.diff && python -m pytest tests -q` |
| G2 差分一致 | **通过**：1,472 独立唯一样本，0 mismatch | `python -m pytest tests/test_diff.py -q -s` |
| G3 中位加速 | **通过**：1.4185x ≥1.15x | `python benchmark/bench.py --reps 5 --warmup 3` |
| G3b 真实语料 | **通过**：1.4101x（identity）/ 1.4738x（redact），均 ≥1.15x | 同上，`real_corpus` 字段 |
| G4 最大规模 | **通过**：1.3165x ≥0.95x | 同上，large 行 |
| G5 内存 | **通过**：峰值 RSS 30,772 KB vs 30,772 KB（1.000x） | `/usr/bin/time -v python /tmp/memrun.py src/iterutils_patched.py`（脚本见下） |

G5 复现脚本（对 869 KB 文档 remap 12 次）：
```python
import importlib.util, json, resource
s = importlib.util.spec_from_file_location("m", "src/iterutils_baseline.py" if __import__("sys").argv[1]=="base" else "src/iterutils_patched.py")
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
doc = json.load(open("corpus/real/compounds.json"))
for _ in range(12): m.remap(doc, visit=m.default_visit)
print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
```

## 复现（3 条命令）

```bash
python3 -m venv .venv && .venv/bin/pip install pytest && .venv/bin/python -m pytest tests -q -s
.venv/bin/python benchmark/bench.py --reps 5 --warmup 3
git clone https://github.com/mahmoud/boltons.git /tmp/b && cd /tmp/b && git apply ../patch.diff && python -m pytest tests -q
```

## 环境

CPU: AMD EPYC 7K62 48-Core Processor（4 vCPU）· 内存 3,723 MB · Python 3.12.3（`/usr/bin/python3.12`）· 种子 20261118 · 计时 `time.perf_counter()` · 3 warmup + 5 timed reps。

本机只有 Python 3.12，无 3.10/3.11，无法做三解释器交叉；未编造多版本数据。

## 本轮创新

**B 档**：首次把 `boltons.iterutils.remap` 的耗时归因为“容器协议分派税”而不是用户 `visit` 回调——在 869 KB 真实 JSON 上，26.5% 的 cProfile 采样花在 `default_enter` 的 ABC `isinstance` 与 `ItemsView` 构造（`isinstance` 自身 21.2%，其中一半走 `ABCMeta.__instancecheck__`）；exact-type 快路径在**保留子类/自定义 Mapping 回退语义**的前提下，把真实 AWS 文档 remap 提速 1.41x，并用 1,472 个独立样本（含 dict/list 子类、namedtuple、自定义 Mapping/Sequence）证明 0 字节差异。台账没有 boltons 条目，也没有把“remap 的分派税”单独测量过。

## 诚实边界

- identity 负载是纯遍历（`default_visit`），用于隔离分派成本；redact 负载是更接近真实使用的“按 key 脱敏”变换，两者都给出。
- boltons 上游不带 JSON fixture；真实语料因此取自 boto3 自带的 AWS resource model（Apache-2.0）与 deepdiff 的 MIT fixture，来源与 sha256 在 `benchmark/baseline.json` 的 `scales.*.doc_sha256` 与 `corpus/real/` 中列明。
- 本补丁只加速 plain `dict`/`list` 节点；`OrderedDict`、`defaultdict`、自定义 Mapping 走原路径，速度不变（差分测试覆盖）。

## License

上游 BSD-3-Clause 原文见 `LICENSE`。`src/iterutils_baseline.py` 是 commit `d31d821` 的原文副本，`src/iterutils_patched.py` 是应用 `patch.diff` 后的副本；版权归 Mahmoud Hashemi 与 boltons contributors，本次仅新增 14 行快路径。
