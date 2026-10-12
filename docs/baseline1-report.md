# Baseline 1 交付报告：完整读取所有邻居 slot

本文是 [baseline1-plan.md](baseline1-plan.md) 的实施结果记录，覆盖**交付概览、完成判据对照、论文阅读要点、代码改动、指标口径、实测数据、复现手册、局限与后续衔接**。

> 环境说明：本次开发与正确性验证全部在 **WSL2 + mmap** 下完成（WSL2 无 `/dev/infiniband`，无 IB LID）。因此**下面的延迟/吞吐数字不能作为正式性能结论**，仅用于验证测量链路与展示结构性浪费。正式 RDMA 实验请按 [erdma.md](erdma.md) 迁移到有网卡/eRDMA 的机器。
>
> **本次数据采集条件**：笔记本**已接电源**（`BAT1: Charging`，`AC1 online=1`），CPU 为 `i5-10210U`。WSL2 内核不暴露 `cpufreq`，频率由 Windows 主机电源计划决定。接电源后同一组计时数据的 `qps` 离散度从电池供电时的 ~30% 收敛到 <10%，说明电池状态确实引入了频率噪声；而**字节类指标在两种供电下逐位相同**（见 §6.1 注）。

---

## 1. 交付概览

| 产物 | 路径 | 说明 |
| --- | --- | --- |
| 具名策略 | `include/rdma_hnsw/read/read_strategy.h`、`src/read/read_strategy.cc` | `Baseline1FixedStrategy`，`name() == "baseline1_fixed"` |
| 指标扩展 | `include/rdma_hnsw/read/read_strategy.h` | 新增 `layer_reads/node_reads/node_bytes/degree_sum/capacity_sum` + `merge()` |
| 统计分账 | `src/runtime/query_engine.cc` | `read_node()` 改记 `node_reads/node_bytes`，邻居区口径自洽 |
| 正确性测试 | `tests/read_strategy_test.cc` | 7 类用例全覆盖 |
| 实验驱动 | `experiments/baseline1_main.cc` | 逐查询计时 + 指标快照 + CSV 导出 + 离线 recall |
| 构建注册 | `CMakeLists.txt` | `baseline1_experiment` 目标 + smoke 测试 |
| CLI 别名 | `include/rdma_hnsw/common/types.h`、`src/common/config.cc` | `--read-strategy baseline1` == `fixed` |
| 数据脚本 | `scripts/convert_sift.py` | `.fvecs` 子集切分 + 离线 ground truth |
| 扫描脚本 | `scripts/run_baseline1.sh` | `规模 × {静态, erase, upsert} × 更新比例` 全量扫描 |
| 分析脚本 | `scripts/analyze_baseline1.py` | degree 直方图、浪费率曲线、占比对照、Markdown 报告 |
| 依赖记录 | `scripts/requirements.txt` | 虚拟环境重建说明 |

**未修改** `QueryEngine::search()`；策略内无任何 ibverbs 调用（满足 D3）。

---

## 2. 完成判据对照（D1–D8）

| 编号 | 判据 | 状态 | 证据 |
| --- | --- | --- | --- |
| D1 | 具名策略类 + `name()` + 注释 | ✅ | `Baseline1FixedStrategy`，返回 `"baseline1_fixed"` |
| D2 | 可独立选择、互不影响 | ✅ | `--read-strategy fixed\|baseline1`；`set_read_strategy()` 注入点参数化 |
| D3 | 不改搜索主流程；策略内无 ibverbs | ✅ | `query_engine.cc` 仅改 `read_node()` 计数；`read_strategy.cc` 无网络调用 |
| D4 | 读取长度/degree/容量边界显式检查 | ✅ | `capacity==0`、短读、`degree>capacity` 截断、空 slot 不返回 |
| D5 | 小规模正确性测试 + 与真值一致 | ✅ | `ctest -R read_strategy_test` 全绿；与索引真值逐项比对 |
| D6 | 吞吐/分位延迟/字节/read_ops 可导出 | ✅ | `baseline1_experiment` 的 per-query CSV + summary CSV |
| D7 | 不同更新比例与规模下的浪费数据 | ✅ | §6 表格与图（规模 10K/100K、维度 32/128、`M` 8/16/32、erase/upsert、ratio 0.1/0.3/0.5） |
| D8 | 源码/命令/参数/数据/脚本可复现 | ✅ | 本文 §7 复现手册；`scripts/` 三个脚本 |

`ctest` 结果（6/6 通过）：

```text
cm_protocol_test  graph_store_test  read_strategy_test
hnsw_builder_test  runtime_api_test  baseline1_experiment_smoke
```

---

## 3. 论文阅读要点（对应 plan §3）

> 依据 [papers/论文概览.md](../papers/论文概览.md) 与 plan §3.1 的原文摘录。以下为**设计所需的关键判断**，不是全文综述。

### 3.1 SHINE（`papers/2.pdf`）为什么"一次读完整个邻居表"

SHINE §3.1 的节点布局是 `header + vector + 每层(数量前缀 + 邻居列表)`，节点被分配为**定长内存区域**，邻居"紧凑存放在定长内存区"。§3.2 的 Algorithm 1 直接 `RDMA_READ(nlist_rptr)` 整段读回邻居表，再做一次 `RDMA_READ(n_rptr)` 读邻居节点向量。

- **为什么这么做**：RDMA 单向 READ 的往返成本远高于带宽成本；先把 header 读回来再决定读多少，会引入一次**依赖往返**，且在高并发下难以掩盖。定长一次性读让每次访问变成"一次 READ、地址可预计算、无需等待"，可流水化。
- **定长假设在动态图下如何失效**：定长容量隐含"节点内容不变、地址稳定"。一旦有插入/删除，`degree` 会波动（见 §6 与 plan 陷阱 3），容量与真实 degree 脱钩，于是**每次读取都固定传输 `capacity` 个 slot**，而真实有用部分只有 `degree` 个。SHINE 之所以能忽略这一开销，是因为它的量化论断是"邻居列表只占约 1%"（见下）。

### 3.2 SHINE 的 1% 论断与本课题的口径换算

SHINE 原文："neighbor lists contribute only a small fraction … approximately `(M·8)/(M·d·4) = 2/d`；200 维时仅 **1%**"。

换到本项目口径（邻居 ID 为 4B，slot 数为 `capacity`）：

```text
邻居区域字节 / 总请求字节 ≈ (4 + cap·4) / (deg·(24 + 4·dim))
```

- `deg ≈ cap` 时可简化为 ≈ `1/(dim + 6)`；
- 实测（见 §6）`dim=128` 约 **0.92%**，`dim=32` 约 **3.45%**，与上式同量级。

**推论（已被实测支持）**：维度越低、degree 越低于 capacity，Baseline 1 的**绝对浪费字节占比越大**。所以主实验用 SIFT1M（128 维）保持常规设定，同时用 `dim=32` 的合成数据把效应放大以便验证测量链路。

### 3.3 HNSW（`papers/1.pdf`）为什么 degree 上界是 `Mmax0`，何时 degree 远小于容量

- `M` 是每层目标连接数，底层推荐上界 `Mmax0 = 2M`（本项目 `layer_capacity(0) = 2M`，`capacity(l>0) = M`）。
- degree 远小于容量的典型情形：**高层节点**（`level` 越高节点越少、边越稀疏，本实验数据 level 2–4 的容量利用率低至 0–44%）；**被剪枝后未回填**的节点；**删除造成的永久下降**（erase 会从其他节点邻居表移除该 id，见 plan 陷阱 3）。

### 3.4 FreshDiskANN（`papers/4.pdf`）与 RDMA 综述（`papers/7.pdf`）

- FreshDiskANN 的**惰性删除 + 批量整合**意味着 degree 分布会随时间持续漂移，正是 Baseline 1 固定容量读取持续产生浪费的根源；也说明"缓存邻居长度"（Baseline 3）必然面对失效问题。
- RDMA 综述指出**单向 READ 的额外往返代价高**：这正是 Baseline 2（header-first 两跳）要量化的权衡点，也是本文在 mmap 下**只验证测量链路、不宣称性能结论**的原因。

---

## 4. 代码改动清单

### 4.1 `include/rdma_hnsw/read/read_strategy.h`

`ReadMetrics` 只增字段（不重定义旧字段语义），并声明具名策略：

```cpp
struct ReadMetrics {
  // ... 既有字段不变 ...
  std::uint64_t layer_reads{};    // (node, level) 邻居读取次数
  std::uint64_t node_reads{};     // QueryEngine 读取 header+vector 的次数
  std::uint64_t node_bytes{};     // 节点区域请求字节
  std::uint64_t degree_sum{};     // Σ min(degree, capacity)
  std::uint64_t capacity_sum{};   // Σ capacity
  void merge(const ReadMetrics &o);
};

class Baseline1FixedStrategy final : public NeighborReadStrategy {
public:
  NeighborReadResult read(const RemoteNodeView &v, ReadContext &c) override;
  const char *name() const override { return "baseline1_fixed"; }
};
```

### 4.2 `src/read/read_strategy.cc`

一次读取 `4 + capacity*4` 字节，显式检查：`capacity==0` → 错误；`!r.ok || r.bytes != n` → 返回错误（不伪造邻居）；`degree>capacity` → 截断；只返回前 `degree` 个 slot（空 slot 的 0 不作邻居）。`merge()` 同步扩展新字段。

### 4.3 `src/runtime/query_engine.cc`

`read_node()` 的计数由 `read_ops/requested_bytes` 改为 `node_reads/node_bytes`，使 `requested_bytes` **只代表邻居区域**，从而恢复恒等式 `requested == useful + wasted`。**搜索主流程未改动。**

### 4.4 CMake / CLI

- 新增 `baseline1_experiment` 目标与 `baseline1_experiment_smoke` 测试（smoke 自动构建微型索引、跑少量查询、落 CSV）。
- `--read-strategy` 白名单新增 `baseline1`（映射到 `ReadPolicy::Fixed`），`print_usage` 同步说明。

---

## 5. 指标口径

```text
单次层读取（Baseline 1）：
  requested = 4 + 4·capacity
  useful    = 4 + 4·min(degree, capacity)
  wasted    = 4·(capacity - min(degree, capacity))
  read_ops  = 1

聚合：
  浪费率      = Σ wasted / Σ requested                 （仅邻居区域）
  容量利用率  = Σ min(degree,capacity) / Σ capacity
  平均度数    = Σ min(degree,capacity) / Σ layer_reads
  邻居区占比  = Σ requested / (Σ requested + Σ node_bytes)
  qps         = 由逐查询延迟导出（不含更新开销，便于跨策略比较）
  qps_end_to_end = 含更新开销的端到端吞吐
```

**分账要点**：`wasted_bytes` 是**相对邻居区域**的浪费；它对总流量的影响还需乘"邻居区占比"。两者混谈会严重高估 Baseline 1 的代价（本实验中邻居区占比仅约 1%）。

---

## 6. 实验结果（WSL2 + mmap）

### 6.1 汇总表

配置：`M=16, ef_construction=200, ef_search=64, k=10`，合成数据（随机高斯），每档 `queries=300, warmup=50, seed=42`，更新比例 0.1 / 0.3 / 0.5。`qps` 由逐查询延迟导出（不含更新开销，便于跨策略比较）。

| dataset | update_op | ratio | updates | qps | p50_us | p95_us | p99_us | waste_ratio | neighbor_share | cap_util | avg_deg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| dim128 / 10K | static | — | 0 | 563.9 | 1589 | 2739 | 3575 | 0.1603 | 0.0092 | 0.8343 | 25.05 |
| dim128 / 10K | erase | 0.1 | 30 | 533.3 | 1724 | 2635 | 3277 | 0.1628 | 0.0092 | 0.8318 | 24.97 |
| dim128 / 10K | erase | 0.3 | 100 | 491.9 | 1885 | 2908 | 4189 | 0.1666 | 0.0093 | 0.8279 | 24.86 |
| dim128 / 10K | erase | 0.5 | 150 | 467.7 | 1951 | 3139 | 5400 | 0.1692 | 0.0093 | 0.8252 | 24.78 |
| dim128 / 10K | upsert | 0.1 | 30 | 537.4 | 1692 | 2751 | 3603 | 0.1594 | 0.0092 | 0.8353 | 25.08 |
| dim128 / 10K | upsert | 0.3 | 100 | 523.7 | 1815 | 2583 | 2975 | 0.1575 | 0.0091 | 0.8373 | 25.14 |
| dim128 / 10K | upsert | 0.5 | 150 | 486.0 | 1947 | 3034 | 3543 | 0.1561 | 0.0091 | 0.8387 | 25.18 |
| dim128 / 100K | static | — | 0 | 459.2 | 2087 | 2801 | 3499 | 0.1116 | 0.0086 | 0.8847 | 26.29 |
| dim128 / 100K | erase | 0.1 | 30 | 359.5 | 2519 | 3768 | 9522 | 0.1120 | 0.0086 | 0.8843 | 26.27 |
| dim128 / 100K | erase | 0.3 | 100 | 362.9 | 2586 | 3791 | 4639 | 0.1123 | 0.0086 | 0.8839 | 26.26 |
| dim128 / 100K | erase | 0.5 | 150 | 343.4 | 2711 | 3933 | 4412 | 0.1126 | 0.0086 | 0.8836 | 26.25 |
| dim128 / 100K | upsert | 0.1 | 30 | 369.4 | 2577 | 3489 | 3933 | 0.1118 | 0.0086 | 0.8844 | 26.28 |
| dim128 / 100K | upsert | 0.3 | 100 | 334.1 | 2760 | 4191 | 5260 | 0.1116 | 0.0086 | 0.8847 | 26.29 |
| dim128 / 100K | upsert | 0.5 | 150 | 326.3 | 2799 | 4335 | 5086 | 0.1115 | 0.0086 | 0.8847 | 26.30 |
| dim32 / 10K | static | — | 0 | 848.6 | 1078 | 1759 | 2207 | 0.2197 | 0.0345 | 0.7729 | 23.17 |
| dim32 / 10K | erase | 0.1 | 30 | 594.8 | 1177 | 3443 | 10424 | 0.2218 | 0.0346 | 0.7708 | 23.10 |
| dim32 / 10K | erase | 0.3 | 100 | 695.1 | 1280 | 2241 | 3469 | 0.2247 | 0.0348 | 0.7678 | 23.01 |
| dim32 / 10K | erase | 0.5 | 150 | 664.1 | 1323 | 2511 | 3264 | 0.2270 | 0.0349 | 0.7654 | 22.94 |
| dim32 / 10K | upsert | 0.1 | 30 | 666.9 | 1279 | 2621 | 3775 | 0.2180 | 0.0345 | 0.7747 | 23.22 |
| dim32 / 10K | upsert | 0.3 | 100 | 748.8 | 1239 | 1946 | 2599 | 0.2135 | 0.0343 | 0.7794 | 23.36 |
| dim32 / 10K | upsert | 0.5 | 150 | 670.1 | 1336 | 2299 | 3179 | 0.2104 | 0.0341 | 0.7826 | 23.45 |

> **恒等式校验**：每行 `requested == useful + wasted`（驱动内断言，违反即抛错）。以 `dim128 / 10K / static` 为例：`requested = 2718136 = useful 2282296 + wasted 435840`。
>
> **确定性复核**：本轮（接电源）与上一轮（电池）在同一配置下的**字节类指标逐位相同**——例如 `dim128/10K/static` 两轮的 `requested / wasted / waste_ratio` 均为 `2718136 / 435840 / 0.160345`。发生变化的只有计时列（见开头环境说明）。这从经验上确认：**结构性结论由字节指标支撑，与供电/调频无关**。

### 6.2 结构性证据：degree 直方图（必须最先看的图）

`dim128 / 10000 节点 / M=16`：

```text
level  nodes    capacity  mean_deg  mean_util  full%
    0     10000        32     23.52      0.735   18.5   <- 底层 degree 尖峰在 16(=M) 与 32(=2M)
    1       646        16     14.09      0.881   43.8
    2        39        16      6.97      0.436    0.0
    3         3        16      2.00      0.125    0.0
    4         1        16      0.00      0.000    0.0
```

`dim128 / 100000 节点 / M=16`（规模放大后**高层反而更满**）：

```text
level  nodes    capacity  mean_deg  mean_util  full%
    0    100000        32     22.46      0.702   20.3
    1      6246        16     15.44      0.965   80.2
    2       407        16     13.78      0.861   37.1
    3        22        16     10.50      0.656   40.9
    4         2        16      1.00      0.062    0.0
```

关键结论：

1. **底层 degree 呈双峰**：`dim128/10K` 下 1020 个节点恰好停在 `M=16`，1850 个节点被回填到 `2M=32`；`M=8` 时为 8(2483) 与 16(2543)；`M=32` 时为 32(378) 与 64(472)。**"capacity=2M 但实际常常只用到 M"** 就是 Baseline 1 浪费的结构性来源。
2. **层级越高越稀疏**：`dim128/10K` 的 level 2 及以上容量利用率 ≤ 44%，level 4 为 0。这些层的整段固定读取几乎全是浪费。
3. **规模放大让高层变满**：100K 时 level 1 利用率升到 0.965（10K 为 0.881），聚合容量利用率从 0.834 升到 0.885，因此浪费率从 16.0% 降到 11.2%。
4. 因此**静态基线的浪费并非微小**：`dim128/10K` 为 16.0%，`dim32/10K` 为 22.0%，`M=32` 时达 23.2%。

### 6.3 陷阱 3 验证：erase 与 upsert 方向相反

以 `dim128 / 10K`（静态 `waste_ratio = 0.1603`）为基准，三种更新比例：

| 更新 | ratio 0.1 | ratio 0.3 | ratio 0.5 | 相对静态 | 含义 |
| --- | --- | --- | --- | --- | --- |
| erase | 0.1628 | 0.1666 | **0.1692** | **↑ 5.6%** | 从他人邻居表删除 id → 底层 degree 永久下降 → 浪费上升 |
| upsert | 0.1594 | 0.1575 | **0.1561** | **↓ 2.6%** | 目标节点邻居表按距离重填到 `cap` → degree 回到满 → 浪费下降 |

两种趋势都**随更新比例单调**，方向与 plan 陷阱 3 完全一致。同样的单调性在另外四组配置上全部复现：

| 配置 | 静态 | erase 0.5 | upsert 0.5 |
| --- | --- | --- | --- |
| dim128 / 10K / M16 | 0.1603 | 0.1692 (↑) | 0.1561 (↓) |
| dim128 / 100K / M16 | 0.1116 | 0.1126 (↑) | 0.1115 (↓) |
| dim32 / 10K / M16 | 0.2197 | 0.2270 (↑) | 0.2104 (↓) |
| dim128 / 10K / M8 | 0.1221 | 0.1306 (↑) | 0.1185 (↓) |
| dim128 / 10K / M32 | 0.2321 | 0.2398 (↑) | 0.2228 (↓) |

幅度不大是因为更新比例有限、且图本身已较饱和（底层利用率 0.70–0.87）。

### 6.4 维度放大效应与 SHINE 1% 论断对照

邻居区占总请求字节：

| dim | 实测 neighbor_share | 朴素模型 `1/(dim+6)` | 比值 |
| --- | --- | --- | --- |
| 32 | 0.03451 | 0.02632 | 1.31 |
| 128 | 0.00920 | 0.00746 | 1.23 |

> 表中实测值为该维度下**所有配置的平均**（`analyze_baseline1.py` 的口径），故 `dim=128` 略高于单条 `dim128 / 10K / static` 的 0.0092。

- **`dim=128` 时邻居区仅约 0.9%**，与 SHINE "邻居列表约 1%" 的论断**在同一量级**。
- 朴素模型偏低，因为 `deg ≈ capacity` 的假设不成立（底层利用率 0.70–0.87）；把真实 `deg` 代入 `(4+4·cap)/(deg·(24+4·dim))` 后误差降到 ~7%。
- **`dim=32` 时邻居区升至约 3.5%**，即"低维放大 Baseline 1 浪费"的推论成立。

### 6.5 `M` 扫描：容量放大与浪费的关系

固定 `dim=128 / 10000 节点`，仅改 `M`（`capacity(0)=2M`）：

| M | capacity(0) | static waste | erase 0.5 | upsert 0.5 | cap_util (static) | avg_degree (static) |
| --- | --- | --- | --- | --- | --- | --- |
| 8 | 16 | 0.1221 | 0.1306 | 0.1185 | 0.8694 | 12.39 |
| 16 | 32 | 0.1603 | 0.1692 | 0.1561 | 0.8343 | 25.05 |
| 32 | 64 | 0.2321 | 0.2398 | 0.2228 | 0.7642 | 46.92 |

结论：

1. **`M` 越大，浪费率越高**（12.2% → 16.0% → 23.2%），因为 `capacity=2M` 被放大，而真实 degree 追不上容量（容量利用率 0.869 → 0.834 → 0.764）。
2. erase/upsert 的方向性在三个 `M` 下**一致成立**（erase 抬高浪费、upsert 降低浪费），说明该结论与 `M` 无关。
3. `neighbor_share` 基本不随 `M` 变化（0.0090–0.0100），因为它由 `dim` 主导，印证 §6.4 的模型。

### 6.6 图表

`out/figures/`（由 `analyze_baseline1.py` 生成，`out/` 不纳入版本控制，可随时重生成）：

| 文件 | 内容 |
| --- | --- |
| `waste_vs_update_ratio.png` | 浪费率 vs 更新比例（5 组配置 × static/erase/upsert） |
| `utilization_vs_update_ratio.png` | 容量利用率 vs 更新比例 |
| `neighbor_share.png` | 邻居区字节占比（对照 1% 论断） |
| `degree_hist_<index>.png` | 每个索引一张、按层分子图的 degree 直方图（5 张） |
| `degree_hist_level0_compare.png` | 五个索引的 level-0 直方图叠加（`M` / `2M` 双峰对照） |

---

## 7. 复现手册（WSL Ubuntu）

```bash
# 0) 准备
cd ~/RDMA-HNSW-Lab            # 已复制到 Linux 文件系统，避开 /mnt/d 与 CJK 路径
python3 -m venv .venv && source .venv/bin/activate
pip install -r scripts/requirements.txt     # numpy + matplotlib

# 1) 构建（WSL 关闭 RDMA）
cmake -S . -B build -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure   # 6/6 通过

# 2) 单次 Baseline 1 运行
./build/hnsw_build --generate 10000 --dim 128 --M 16 --ef-construction 100 \
  --seed 42 --output data/index.bin
./build/baseline1_experiment --index data/index.bin --transport mmap \
  --queries-count 300 --ef-search 64 --k 10 --warmup 50 --seed 42 \
  --dataset synthetic --label n10000_static \
  --out out/baseline1_n10000_static.csv --summary-out out/baseline1_summary.csv

# 3) 全量扫描：规模 × {静态, erase, upsert} × 更新比例
SIZES=10000,100000 QUERIES=300 UPDATES=300 RATIOS=0.1,0.3,0.5 DIM=128 TAG=dim128 \
  scripts/run_baseline1.sh
# 追加低维（放大邻居区占比）与 M 扫描
SIZES=10000 QUERIES=300 UPDATES=300 RATIOS=0.1,0.3,0.5 DIM=32  TAG=dim32 RESET_SUMMARY=0 scripts/run_baseline1.sh
SIZES=10000 QUERIES=300 UPDATES=300 RATIOS=0.1,0.3,0.5 DIM=128 M=8  TAG=m8  RESET_SUMMARY=0 scripts/run_baseline1.sh
SIZES=10000 QUERIES=300 UPDATES=300 RATIOS=0.1,0.3,0.5 DIM=128 M=32 TAG=m32 RESET_SUMMARY=0 scripts/run_baseline1.sh

# 4) 分析：表格 + degree 直方图 + 图 + 报告
python3 scripts/analyze_baseline1.py --summary out/baseline1_summary.csv \
  --index data/index_n10000_d128_M16.bin \
  --index data/index_n100000_d128_M16.bin \
  --index data/index_n10000_d32_M16.bin \
  --index data/index_n10000_d128_M8.bin \
  --index data/index_n10000_d128_M32.bin \
  --out-dir out/figures --report out/baseline1_report.md

# 5) 真实数据（SIFT1M）
python3 scripts/convert_sift.py --base data/sift/sift_base.fvecs \
  --query data/sift/sift_query.fvecs --out-dir data/sift --sizes 10000,100000 --k 10
SIFT_DIR=data/sift TAG=sift SIZES=10000,100000 scripts/run_baseline1.sh
```

> 1M 规模**不要**传 `--base`（plan 陷阱 2），改用 `convert_sift.py` 生成的离线 ground truth（`--gt`）。已验证：`--base` 与 `--gt` 两条路径在同一数据上给出相同 recall（0.992）。

---

## 8. 风险与局限

| 项 | 状态 / 说明 |
| --- | --- |
| mmap 性能不可作结论 | 延迟/吞吐受本地文件系统与单线程影响，仅验证测量链路；正式数据需 RDMA 真机 |
| mmap 下 `reload()` 清零统计（陷阱 1） | 已在驱动中规避：**逐查询**前后快照求差，聚合为差值之和；更新前后不再依赖"循环结束读一次 `stats()`" |
| `requested_bytes` 口径混入节点字节（§2.2） | 已修正：`read_node()` 记入 `node_bytes`；测试断言 `requested == useful + wasted` |
| 1M 规模 `--base` 暴力搜索过慢（陷阱 2） | 已提供 `--gt` 离线 recall 路径 |
| mmap 更新为全量重写 | `StorageEngine::apply` 读+写整份索引，更新多时 wall time 偏高；这是原型的既有行为，本计划未改 |
| 未做插入（insert）扫描 | 插入会使全量重写代价随规模线性增长；erase/upsert 已覆盖陷阱 3 的两个方向 |
| **供电/调频影响计时** | 电池供电时 `qps` 离散度约 30%，接电源后收敛到 <10%；字节类指标不受影响。正式计时须接电源并把 Windows 电源计划设为"最佳性能"，且重复多次取中位数 |

---

## 9. 与后续周次的衔接

按 plan §11.3，本计划的产出已按"可被其他策略继承"的方式组织：

- **指标结构策略无关**：`ReadMetrics` 新增字段对所有策略通用，Baseline 2/3 可直接复用 `layer_reads/degree_sum/capacity_sum`；
- **注入点参数化**：实验驱动通过 `set_read_strategy()` 注入并在 `reload()` 后重新安装策略，后续策略只需改注入对象；
- **测试可扩展**：`tests/read_strategy_test.cc` 已是"策略 × 用例"结构，新策略登记名字即可纳入一致性对比；
- **CSV 口径稳定**：Baseline 2/3 可直接写入同一 `baseline1_summary.csv`，分析脚本无需改动。

**下一步建议**：实现 Baseline 2（header-first，量化"少传浪费 vs 多一次依赖往返"）与 Baseline 3（计算侧缓存 `(node, layer, generation) → degree`，复用 `ReadContext::length_cache` 与 `cache_*` 指标），然后设计分级前缀读取机制。
