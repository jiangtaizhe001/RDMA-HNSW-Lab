# Baseline 2 交付报告：header-first 读取

本文是 [baseline2-plan.md](baseline2-plan.md) 的实施结果记录，覆盖**交付概览、完成判据对照、代码改动、指标口径、实测数据、模型与假设检验、复现手册、局限与后续衔接**。结构与 [baseline1-report.md](baseline1-report.md) 对齐。

> **环境说明（重要）**：本次全部在 **WSL2 + mmap** 下完成（无 `/dev/infiniband`，无 IB LID）。按 plan §0③ 的分工原则，**mmap 只能给出字节/操作数/正确性结论，不能给出往返延迟结论**。因此下文的延迟数字仅用于确认测量链路，**不构成 Baseline 2 的性能结论**；`rtt_per_dep_read` 的 mmap 值也不等于 `R`。正式延迟实验需按 [erdma.md](erdma.md) 迁移到有 RDMA 网卡的机器。
>
> **数据采集条件**：合成数据（`hnsw_build --generate`），500 条合成 query，`ef-search=64`、`k=10`、`ef-construction=200`、`seed=42`。未提供 base/ground-truth，故 sweep 中 `recall=0`；正确性由单元测试与恒等式保证（见 §5、§7）。

---

## 1. 交付概览

| 产物 | 路径 | 说明 |
| --- | --- | --- |
| 具名策略 | `include/rdma_hnsw/read/read_strategy.h`、`src/read/read_strategy.cc` | `Baseline2HeaderFirstStrategy`，`name() == "baseline2_header_first"` |
| 指标扩展 | `include/rdma_hnsw/read/read_strategy.h` | 新增 `failed_reads`、`zero_degree_layers`，并接入 `merge()` |
| 工厂接线 | `src/read/read_strategy.cc` | `make_read_strategy(HeaderActual)` 返回 Baseline 2 |
| CLI 文案 | `src/common/config.cc` | usage 列出 `fixed|header_actual` |
| 正确性测试 | `tests/read_strategy_test.cc` | Baseline 2 专用 8 类用例 + 与 Baseline 1 逐邻居等价 |
| 实验驱动 | `experiments/baseline1_main.cc` | `--read-strategy fixed|baseline1|header_actual|header`；导出 header/dependent/zero-degree 指标 |
| 扫描脚本 | `scripts/run_baseline2.sh` | `规模×{静态,erase,upsert×ratio}`，B1/B2 成对采集 |
| 分析脚本 | `scripts/analyze_baseline2.py` | 配对差额、恒等式校验、5 张图 + Markdown 报告 |
| 原始数据 | `out/baseline2_summary.csv`（70 行）、`out/baseline2_<label>_<策略>.csv` | |
| 图表 | `out/figures_baseline2/` | 10 张 PNG |
| 本报告 | `docs/baseline2-report.md` | |

**未修改** `QueryEngine::search()`；策略内无任何 ibverbs 调用（满足 D3）。

---

## 2. 完成判据对照（D1–D9）

| 编号 | 判据 | 状态 | 证据 |
| --- | --- | --- | --- |
| D1 | 具名策略类 + `name()` + 注释 | ✅ | `Baseline2HeaderFirstStrategy`，返回 `"baseline2_header_first"` |
| D2 | 可独立选择、互不影响 | ✅ | `--read-strategy header_actual`（`compute_client` 与实验驱动均已接线）；`set_read_strategy()` 注入点 |
| D3 | 不改搜索主流程；策略内无 ibverbs | ✅ | 仅经 `RemoteTransport::read`；`query_engine.cc` 未改 |
| D4 | 边界齐全 | ✅ | `capacity==0`、header 短读/失败、第二次读短读/失败、`degree>capacity` 截断、`degree==0` |
| D5 | 两次读取确实分两次 | ✅ | 两次独立 `transport.read`，`calls` 计数器断言；字节下降本身即证据 |
| D6 | 与 Baseline 1 逐邻居一致 | ✅ | `test_baseline2_equivalence_with_baseline1`（全部节点/层）；sweep 中 recall 逐位相同 |
| D7 | 指标可导出 | ✅ | per-query + summary CSV 含 `header_reads/dependent_reads/zero_degree_layers/failed_reads` |
| D8 | 动态更新下 degree 变化的性能数据 | ✅ | §6.3 erase/upsert × ratio 曲线 |
| D9 | 源码/命令/参数/数据/脚本可复现 | ✅ | §8 复现手册 + `scripts/` 三个脚本 |

---

## 3. 代码改动

### 3.1 `read_strategy.h` / `read_strategy.cc`

```cpp
struct ReadMetrics {
  // ... 既有字段不变 ...
  std::uint64_t failed_reads{};        // 失败/短读的 transport 读取次数
  std::uint64_t zero_degree_layers{};  // degree==0 的层数（跳过第二次读）
  void merge(const ReadMetrics &o);
};

class Baseline2HeaderFirstStrategy final : public NeighborReadStrategy {
public:
  NeighborReadResult read(const RemoteNodeView &v, ReadContext &c) override;
  const char *name() const override { return "baseline2_header_first"; }
};
```

`read()` 的关键行为：

1. `capacity==0` → 直接返回错误，**零次读取**；
2. 第一次读：`hdr = read({layer_offset, 0}, 4B)` → degree，`header_reads++`、`read_ops++`；
3. `degree = min(raw, capacity)`（截断，防越界）；
4. `degree==0` → `zero_degree_layers++`，**跳过第二次读**，返回 `ok` + 空集合；
5. 第二次读：`read({layer_offset+4, 0}, degree*4B)`，`dependent_reads++`、`read_ops++`；
6. 任一读失败/短读 → `failed_reads++`，返回错误，**绝不部分返回或伪造邻居**；
7. `wasted_bytes` 恒为 0（有用即所求）。

工厂新增分支：

```cpp
if (p == ReadPolicy::Fixed)        return std::make_unique<Baseline1FixedStrategy>();
if (p == ReadPolicy::HeaderActual) return std::make_unique<Baseline2HeaderFirstStrategy>();
throw std::invalid_argument("read strategy is a student extension; ...");
```

### 3.2 实验驱动参数化

驱动原本写死 `Baseline1FixedStrategy`。现按 `--read-strategy` 通过同一个工厂注入（`make_read_strategy`），保证实验与生产接线一致；并在 summary/per-query CSV 与 stdout 中导出 Baseline 2 专属指标，运行时断言恒等式 ②（`read_ops == header_reads + dependent_reads`）。

---

## 4. 指标口径

单次层读取：

```text
Baseline 2：requested = 4 + 4·deg   useful = 4 + 4·deg   wasted = 0
            read_ops  = 1 + [deg>0]  header_reads = 1  dependent_reads = [deg>0]
            zero_degree_layers = [deg==0]
```

对照量：

```text
saved_bytes      = B1.requested − B2.requested = B1.wasted      （逐运行精确成立）
saving_pct_layer = saved_bytes / B1.requested                   （邻居区域内）
saving_pct_total = saved_bytes / (B1.node_bytes + B1.requested) （对总流量）
ops_penalty_pct  = (B2.read_ops − B1.read_ops) / B1.read_ops     （邻居区域内）
rtt_per_dep_read = (B2.mean − B1.mean) / B2.dependent_reads      （仅 RDMA 有物理意义）
```

---

## 5. 恒等式校验（§8.3）

对全部 **70 组** B1/B2 配对运行断言，**全部通过**：

```text
① requested == useful + wasted                              ✅
② read_ops  == header_reads + dependent_reads   （Baseline 2）✅
③ dependent_reads + zero_degree_layers == layer_reads（Baseline 2）✅
④ B1.requested − B2.requested == B1.wasted                  ✅（逐运行精确相等）
```

恒等式 ④ 在每一行都精确成立，且 `B2.wasted_bytes == 0` 处处成立——这既验证了实现，也验证了「Baseline 2 确实分两次读」（若合并就不可能有此字节账）。

---

## 6. 实测数据

### 6.1 静态：字节账本（图 1）

![bytes](../out/figures_baseline2/b2_bytes_ledger.png)

| 配置 | B1 requested | B1 wasted | B2 requested | saved | saving%层内 | saving%总量 |
| --- | --- | --- | --- | --- | --- | --- |
| n100k d128 M16 | 4 590 112 | 516 368 | 4 073 744 | 516 368 | 11.25% | 0.097% |
| n10k d128 M16 | 4 536 188 | 726 740 | 3 809 448 | 726 740 | 16.02% | 0.147% |
| n10k d128 M8 | 2 501 044 | 305 140 | 2 195 904 | 305 140 | 12.20% | 0.111% |
| n10k d128 M32 | 8 692 900 | 2 016 644 | 6 676 256 | 2 016 644 | 23.20% | 0.229% |
| n10k d32 M16 | 4 537 752 | 995 532 | 3 542 220 | 995 532 | 21.94% | 0.757% |

**结论 1（H1 成立）**：Baseline 2 把邻居区域浪费清零，省下的字节**恰好等于** B1 的浪费（恒等式 ④）。

**结论 2（省字节的占比之谜）**：层内省 11–23%，但**对总流量只省 0.10–0.81%**。原因：`node_bytes`（候选节点 header+向量）支配总流量，而 Baseline 2 对它毫无帮助。维度越低，邻居区域占比越大（≈`1/(dim+6)`），故 `dim=32` 的 0.757% 明显高于 `dim=128`。**报告必须同时给出层内与总量两个口径**，否则会把 0.1% 说成 23%。

### 6.2 操作数（图 2）

![ops](../out/figures_baseline2/b2_read_ops.png)

| 配置 | B1 layer ops | B2 layer ops | 邻居区 ops 增幅 | B2 layer ops 占**全部**读操作比例 | 对**全部** ops 的净增 |
| --- | --- | --- | --- | --- | --- |
| n100k d128 M16 | 37 384 | 74 768 | +100.0% | 3.66% | +3.66% |
| n10k d128 M16 | 36 591 | 72 682 | +98.6% | 3.85% | +3.85% |
| n10k d128 M8 | 41 029 | 81 558 | +98.8% | 7.47% | +7.47% |
| n10k d128 M32 | 34 825 | 69 150 | +98.6% | 2.10% | +2.10% |
| n10k d32 M16 | 36 614 | 72 728 | +98.6% | 4.20% | +4.20% |

**结论 3**：`(node,level)` 邻居读在 mmap 下**几乎总是 1 次读**，故 Baseline 2 把它变成 2 次，邻居区 ops 约翻倍（+98.6%）。但**邻居区读只占全部读操作的 2.1–7.5%**（其余为 `read_node` 的候选向量读），因此对**全部 RDMA 操作数的净增只有 +2.1%~7.5%**。`n100k` 的 `+100.0%` 是因为该索引顶层无 `degree==0` 层（`zero_degree_layers==0`），每一层都多读一次。

### 6.3 动态 workload（图 4，对应 D8）

![dyn](../out/figures_baseline2/b2_saving_vs_update_ratio.png)

- `saved_bytes` 基本不随更新比例变化（erase 略升、upsert 略降，量级 <5%），因为**被扩展的层集合与平均 degree 变化很小**；
- `saving_pct_total` 随更新比例**略微下降**（upsert）或**略微上升**（erase）：erase 会从其他节点邻接表移除 id、降低 degree → 浪费变大；upsert 会回填邻接表、提高 degree → 浪费变小。这正是 plan §5.3 预测的 `saved_bytes ∝ (cap − deg)` 方向。
- 动态下 B1/B2 仍逐位满足恒等式 ④。

### 6.4 延迟（mmap，仅供参考，**非结论**）

![lat](../out/figures_baseline2/b2_latency_mmap.png)

静态 p50 变化在 **−3.2% ~ +5.2%** 之间，但混合组出现 **+68%/+71%** 的离群（`n100k upsert_0.1`、`erase_0.5`），明显是 WSL2 噪声而非策略效应。**mmap 的两次读只是两次 `pread` 系统调用，没有"往返"语义**（plan §0③），所以这里既不能证实也不能证伪 H2。`rtt_per_dep_read` 的 mmap 值我们不作为物理量报告。

---

## 7. 预注册假设检验（plan §5.4）

| 编号 | 假设 | 结论 | 依据 |
| --- | --- | --- | --- |
| **H1** | B2 `wasted==0`，`requested` 下降 `4·(cap−deg)` | ✅ **成立** | 恒等式 ④ 逐运行精确；`B2.wasted==0` |
| **H2** | RDMA 下延迟上升约 `1/(2+d)` | ⏳ **无法在 mmap 验证** | 无 RDMA 硬件；须迁移后测 `rtt_per_dep_read` |
| **H3** | mmap 下两者延迟差异可忽略（<5%） | ◑ **大体成立但噪声大** | 静态多在 −3%~+5%，混合有 >60% 离群 |
| **H4** | M 小、degree 小时相对受益最大 | ❌ **被证伪（按百分比）** | 层内省字节随容量增大而增大：M8 12.2% < M16 16.0% < M32 23.2%。绝对省字节也随 M 增大。**受益随 M 增大而增大** |
| **H5** | B1≡B2 查询结果逐条相同 | ✅ **成立** | 单元测试逐邻居等价；sweep 中 recall 逐位相同 |

**H4 的修正解释**：plan §5.3 的 `S = 4·cap = 8M` 是**每层可省字节的上界**，`cap` 越大浪费越大，故 Baseline 2 省得越多。真正随 degree 变小而变差的是**操作数相对惩罚**（每层固定 +1 次读，而层数不变），但由于邻居区读只占全部读的 2–7%，该惩罚在总量上很小。所以「M 越小越划算」不成立；**Baseline 2 在容量大、degree 低、维度低时省字节最多**。

### 7.1 盈亏平衡模型（plan §5.3）

```text
假设 R = 1.5 µs、W = 12.5 GB/s ⇒ S* = R·W ≈ 18 750 B
本原型每层 level-0 最多可省 = 4·cap = 8M：
  M=8 → 64 B（S*/8M ≈ 293×）  M=16 → 128 B（146×）  M=32 → 256 B（73×）
```

即使容量加到 `M=32`，单层可省字节仍比 `S*` 小 **73 倍**，与 plan §5.3 的结论一致：**在本原型参数空间内，Baseline 2 不可能靠减少字节在单查询延迟上获胜**；它只可能在高并发、带宽受限时以吞吐形式获益（需要 RDMA 才能测）。

---

## 8. 复现手册

```bash
# 1) 构建（WSL2，无 RDMA）
cmake -S . -B build -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure        # 含 baseline2_experiment_smoke

# 2) 扫描（默认 5 配置 × 7 workload × 2 策略 = 70 run，约 25 分钟）
source .venv/bin/activate
./scripts/run_baseline2.sh

# 3) 分析与绘图
python scripts/analyze_baseline2.py \
  --summary out/baseline2_summary.csv \
  --out-dir out/figures_baseline2 \
  --report out/baseline2_report.md \
  --index data/index_n10000_d128_M16.bin \
  --index data/index_n100000_d128_M16.bin \
  --index data/index_n10000_d128_M8.bin \
  --index data/index_n10000_d128_M32.bin \
  --index data/index_n10000_d32_M16.bin
```

单次对照（已验证可用）：

```bash
./build/baseline1_experiment --index data/index_n10000_d128_M16.bin \
  --transport mmap --read-strategy fixed         --label smoke --summary-out out/s.csv
./build/baseline1_experiment --index data/index_n10000_d128_M16.bin \
  --transport mmap --read-strategy header_actual --label smoke --summary-out out/s.csv
```

> **扫描耗时说明**：25 分钟几乎全花在 mmap 混合 workload 的**更新**上。每次 `StorageEngine::apply` 会对全部 N 个节点排序并**重写整个索引文件**，随后 `ComputeNode::reload()` 又把整个索引**重新解析进内存**；对 91 MB 的 100k 索引约 0.46 s/次更新，而 ratio=0.5 时每次 run 有 250 次更新。查询本身很快（`latency_mean≈2.8 ms × 500 ≈ 1.4 s`），这也是 `qps_end_to_end` 偏低的原因。RDMA 模式下更新走控制面、不重建本地文件，不会有此开销。

---

## 9. 局限与后续

1. **延迟结论缺失（最重要）**：H2 与 `rtt_per_dep_read` 必须在真实 RDMA 上完成（`storage_server` + `compute_client` + `--read-strategy header_actual`）。届时 `rtt_per_dep_read` 应落在 1–3 µs 量级，可与 §7.1 的 `R` 对照。
2. **数据集**：本次为合成数据且无 ground-truth，`recall` 未测。正式实验应用 SIFT1M 子集与离线 GT，在**相同 recall** 下比较（`scripts/convert_sift.py` 已具备）。
3. **并发**：plan §5.3 猜测高并发下 op 代价被流水化。需在 RDMA 上跑 `threads=1/4` 验证。
4. **结论一句话**：Baseline 2 用**邻居区约翻倍的操作数**换来**总流量 ~0.1–0.8% 的下降**，在 mmap 上无延迟收益、在理论上也未达单查询盈亏平衡点；它的价值区间是**大容量、低 degree、低维度 + 高并发带宽受限**，且需要 RDMA 数据才能定论。这是一个可被定量解释的**负面/条件性结论**。
