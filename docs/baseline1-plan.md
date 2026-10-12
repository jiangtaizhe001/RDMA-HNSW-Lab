# Baseline 1 实施计划：完整读取所有邻居 slot

本文是 [experiment.md](experiment.md) 中「Baseline 1」的落地计划，覆盖**目标定义、现状盘点、代码改动清单、指标口径、测试方案、实验设计、WSL 运行手册、排期与验收**。阅读顺序建议：先看 §1–§3 建立判断，再按 §5 的任务表施工。

相关文档：[experiment.md](experiment.md)（课题要求）、[help.md](help.md)（原型导读与三个 Baseline 的伪代码）、[erdma.md](erdma.md)（RDMA/eRDMA 部署与测试）。

---

## 1. 目标与验收标准

### 1.1 Baseline 1 的定义

> 对每个访问到的节点和层，读取该层固定容量的全部邻居 slot，**不根据实际 degree 缩短读取长度**。
> —— [experiment.md](experiment.md#L73-L86)

对应原型的磁盘布局（详见 [help.md](help.md#L48-L77)）：

```text
NodeDiskHeader(24B) + vector[dim] + for level = 0..max_level { uint32 degree; NodeId slots[cap(level)] }

// NodeDiskHeader 布局（无 #pragma pack，按 System V ABI 对齐）：
//   id(4) + padding(4) + generation(8) + level(4) + deleted(1) + pad[3] = 24 字节
// 注意：不是 16 字节。generation 是 uint64 强制 8 字节对齐，id 之后有 4 字节填充。

cap(0)    = 2 * M      // graph_store.h :: IndexCatalog::layer_capacity()
cap(l>0)  = M
```

Baseline 1 的单次层读取：

```text
requested_bytes = sizeof(uint32) + capacity * sizeof(NodeId)   // 一次 transport.read()，无依赖往返
useful_bytes    = sizeof(uint32) + min(degree, capacity) * sizeof(NodeId)
wasted_bytes    = (capacity - min(degree, capacity)) * sizeof(NodeId)
read_ops        = 1                                            // 不产生第二跳
```

### 1.2 本计划的完成判据（Definition of Done）

| 编号 | 判据 | 来源 |
| --- | --- | --- |
| D1 | 存在具名策略类 `Baseline1FixedStrategy`，有清晰类名、`name()` 返回值与注释 | [experiment.md](experiment.md#L173-L174) |
| D2 | 该策略可通过配置/API **独立选择**，与其他策略互不影响 | [experiment.md](experiment.md#L137-L138) |
| D3 | 未修改 HNSW 搜索主流程；策略内不出现 ibverbs 调用 | [experiment.md](experiment.md#L175-L176) |
| D4 | 读取长度、degree、邻居 ID、容量边界全部有显式检查 | [experiment.md](experiment.md#L177) |
| D5 | 至少一个小规模正确性测试，验证结果与索引真值一致、与其他策略一致 | [experiment.md](experiment.md#L179) |
| D6 | 可导出：查询吞吐、平均/p50/p95/p99 延迟、每次查询读取字节、有效字节、无效字节、read_ops | [experiment.md](experiment.md#L148-L161) |
| D7 | 产出不同**更新比例**与不同**索引规模**下的传输浪费数据 | [experiment.md](experiment.md#L77-L85) |
| D8 | 保存策略源码、构建命令、运行参数、原始数据与统计脚本，可复现 | [experiment.md](experiment.md#L180) |

### 1.3 本计划的核心判断（重要）

**Baseline 1 的读取逻辑在原型中已经存在。** [help.md](help.md#L142-L160) 明确写道：

> 原型内置的 `ReadPolicy::Fixed` 已经实现了该模式，可以直接作为参考和 Baseline1。

对应实现是 [src/read/read_strategy.cc](../src/read/read_strategy.cc#L30-L48) 中的 `Fixed` 类：一次读取 `4 + capacity * 4` 字节，解析 degree 并截断到 capacity，只返回前 `degree` 个 slot。

因此本计划的**工作重心不是"重新实现固定读取"，而是三件事**：

1. **规范化**：把 `Fixed` 提升为具名、可独立选择、边界检查完整的 Baseline 1 策略（D1–D4）；
2. **补测量**：原型缺少每查询字节数、尾延迟、degree 分布、邻居区域与节点区域的分账统计（D6）；
3. **补验证与复现**：正确性测试 + 实1验驱动 + 数据集/脚本 + 动态 workload 数据（D5、D7、D8）。

> **反模式警示**：不要为了"让 Baseline 1 有工作量"而去改 `QueryEngine::search()` 的遍历逻辑。该流程是四个策略共享的比较基准，改它会直接违反 D3。

---

## 2. 现状盘点

### 2.1 已经具备（直接复用）

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| Baseline 1 读取实现 | [src/read/read_strategy.cc](../src/read/read_strategy.cc#L30-L48) | `Fixed`：一次读满 `capacity` 个 slot |
| 读取辅助与计数 | [src/read/read_strategy.cc](../src/read/read_strategy.cc#L17-L29) | `read_bytes()`：累加 `read_ops` / `requested_bytes` |
| 策略接口与指标结构 | [include/rdma_hnsw/read/read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L9-L53) | `ReadMetrics` / `RemoteNodeView` / `ReadContext` / `NeighborReadStrategy` |
| 策略工厂 | [src/read/read_strategy.cc](../src/read/read_strategy.cc#L63-L69) | `make_read_strategy(ReadPolicy)` |
| 层地址计算 | [include/rdma_hnsw/graph/graph_store.h](../include/rdma_hnsw/graph/graph_store.h#L46-L48) | `layer_capacity(level)`、`node_layer_offset()` |
| mmap 传输后端 | [src/transport/mmap_transport.cc](../src/transport/mmap_transport.cc) | `pread`，用于 WSL 单机开发 |
| 索引构建 | [src/hnsw_build.cc](../src/hnsw_build.cc)、[src/graph/hnsw_builder.cc](../src/graph/hnsw_builder.cc) | 合成数据或 fvecs 输入 |
| 命令行客户端 | [src/compute_client.cc](../src/compute_client.cc) | 静态查询、mixed workload |
| 已有最小断言测试 | [tests/read_strategy_test.cc](../tests/read_strategy_test.cc#L22-L28) | `M=4, degree=2 → wasted = 6*4` |
| 本地 demo 脚本 | [scripts/run_local_demo.sh](../scripts/run_local_demo.sh)、[scripts/run_mixed.sh](../scripts/run_mixed.sh) | 端到端跑通 |

### 2.2 缺失（本计划要补）

| 缺口 | 证据 | 影响 |
| --- | --- | --- |
| 策略类名/`name()` 不满足交付要求 | `Fixed` 位于匿名 namespace，`name()` 返回 `"fixed"` | D1 |
| 无边界的显式检查 | `read_bytes()` 忽略 `r.ok`，只看 `b.size()` | D4 |
| **指标口径不一致** | [src/runtime/query_engine.cc](../src/runtime/query_engine.cc#L47-L64) 的 `read_node()` 往同一个 `stats_.reads` 加 `read_ops`/`requested_bytes`，但**不加** `useful_bytes`/`wasted_bytes` | D6：`requested - useful` 不是浪费 |
| 无每查询字节数与尾延迟 | `QueryStats` 只有 `elapsed_ms` 总量，`stats_.reads` 是整批累加 | D6 |
| 无 degree 分布/按层分解统计 | `ReadMetrics` 无 `layer_reads`/`degree_sum`/`capacity_sum` | D7 分析无力 |
| 无独立实验驱动与 CSV 导出 | `compute_client` 只打印 `completed_queries`/`elapsed_ms` | D6、D8 |
| 正确性测试覆盖不足 | 现有测试只查一个 `wasted_bytes` | D5 |
| 未注册实验目标 | [CMakeLists.txt](../CMakeLists.txt#L117-L133) 无 experiments target | D8 |
| 无数据集转换/子集切分脚本 | `scripts/` 只有 demo 与 RDMA 测试脚本 | D7、D8 |

### 2.3 必须提前知道的三个陷阱

**陷阱 1：每次更新都会清零统计（mmap 模式）**

`ComputeNode::update()` 在 mmap 下走 `local_storage_->apply()` 然后 `reload()`（[src/runtime/compute_node.cc](../src/runtime/compute_node.cc#L108-L123)）；而 `reload()` 会重建 `transport_` 与 `engine_`（[src/runtime/compute_node.cc](../src/runtime/compute_node.cc#L61-L68)），于是 `QueryStats` / `ReadMetrics` 归零、`ReadContext::length_cache` 清空。

而 [src/compute_client.cc](../src/compute_client.cc#L71) 在 mixed 循环**结束后**才读一次 `stats()`，因此它给出的是"最后一次更新之后"的统计，不是全程统计。
→ **自研驱动必须在每次 `update()` 之前把 `stats()` 快照 merge 进外部累加器。**

**陷阱 2：`--base` 会触发客户端暴力搜索**

`QueryEngine::run()` 对每条查询在 `base` 上做全量精确搜索来算 recall（[src/runtime/query_engine.cc](../src/runtime/query_engine.cc#L136-L149)）。10K/100K 规模可用；**1M 规模下每查询 1M×dim 次运算会支配总运行时间**。1M 规模改为离线算 recall（保存结果 ID + 预生成 ground truth）。

**陷阱 3：erase 与 upsert 对 degree 的影响方向相反**

[src/runtime/storage_engine.cc](../src/runtime/storage_engine.cc#L26-L72)：

- `Erase`：从**其他所有节点**的 `neighbors[0]` 中删除该 id → base 层 degree 永久下降 → 浪费上升；
- `Upsert` / `Insert`：目标节点 `neighbors[0].clear()` 后按距离重填到 `cap`（2M）→ degree 回到满 → 浪费下降，且 `generation` 自增。

→ 动态 workload 必须区分 `erase` 与 `upsert`，否则"浪费随更新比例上升"的结论会被抵消。

---

## 3. 论文阅读任务（设计前必须完成）

| 优先级 | 文档 | 必读部分 | 要回答的问题 |
| --- | --- | --- | --- |
| **P0** | `papers/2.pdf`（SHINE） | §3.1 Index Layout；§3.2 Query Processing 的 Algorithm 1；"What to Cache?" 段落 | 为什么 SOTA 设计选择"一次 RDMA READ 读完整个邻居表"？它的定长节点假设在动态图下会怎样失效？ |
| **P0** | `papers/1.pdf`（HNSW） | Alg.1/2（搜索与插入两阶段）、Alg.3/4（邻居选择）、Alg.5（K-ANNS 搜索）；参数段落（`M`、`Mmax`、`Mmax0=2M`、`mL=1/ln M`、`efConstruction`、`ef`） | degree 的上界为何是 `Mmax0`？哪些情况使实际 degree 远小于容量？ |
| P1 | `papers/4.pdf`（FreshDiskANN） | 惰性删除、插入/删除规则、召回稳定性 | 动态增删如何持续改变 degree 分布？ |
| P1 | `papers/7.pdf`（RDMA 存储综述） | 单向 vs 双向、连接 vs 数据报、MR 注册、批处理设计指南 | 为什么"多一次 header 往返"在 RDMA 上昂贵？ |
| P2 | `papers/3.pdf`（DiskANN） | BeamSearch 批取邻域、C 跳缓存 | 另一种读取粒度长什么样，与 Baseline 1 如何对照？ |

### 3.1 必读原文摘录与对应判断

SHINE §3.1：

> "we use a **header** that stores the node id, the maximum level… For each level, we store a **neighbor list prefixed by the number of list items**."
> "…we allocate a **fixed-size node** on some MN… its outgoing neighbors are represented by remote pointers **compactly stored in a fixed-size memory region**."

→ 本项目索引格式（degree 前置 + 定长 slot）的来源；**容量由 M 决定、与真实 degree 脱钩**，这正是 Baseline 1 浪费的结构性原因。

SHINE §3.2 Algorithm 1：

```text
5  nlist_rptr  ← compute remote address from p and l
6  neighborlist ← RDMA_READ(nlist_rptr)     // 整段邻居表一次读完
12 neighbor     ← RDMA_READ(n_rptr)         // 再读邻居节点（向量）
```

→ Baseline 1 不是"假想敌"，而是 SOTA 的默认形态；本课题要量化它在新条件下的代价。

SHINE "What to Cache?"：

> "since neighbor lists contribute only a small fraction of RDMA reads, **we do not cache them**. For a node's neighbor list of size M·8B, we must read up to M vectors, each of size d·4B… the ratio … is approximately (M·8)/(M·d·4) = **2/d**. For 200-dimensional vectors, reading neighbor lists accounts only for **1%**."

→ 这是**需要验证或反驳的核心论断**。换成本项目口径（邻居 ID 为 4B，slot 数为 `capacity`）：

```text
邻居区域字节 / 总请求字节 ≈ (4 + cap·4) / (deg·(24 + 4·dim))   ，deg ≈ cap
                          ≈ 1 / (dim + 6)

dim = 128 → 约 0.75%
dim = 32  → 约 2.6%
```

**推论（必须实测）**：维度越低、degree 越低于 capacity，Baseline 1 的浪费才越显著。这直接决定实验设计：
- 主数据集选 SIFT1M（128 维，与常规设定一致）；
- 开发阶段用 `dim = 32` 的合成数据把效应放大，便于验证测量链路；
- **动态 workload 是必需的**，因为静态 hnswlib 图在 base 层通常填满，浪费主要来自稀疏节点与高层。

---

## 4. 设计决策（DR）

| 编号 | 决策 | 理由 |
| --- | --- | --- |
| DR-1 | 保留 `ReadPolicy::Fixed` 枚举语义，仅将实现类具名化为 `Baseline1FixedStrategy` | 避免波及 `to_string` / `read_policy_from_string` / 工厂三处；向后兼容现有测试与脚本 |
| DR-2 | 统计字段**只增不改**，`merge()` 同步扩展 | 避免破坏已有断言与其他策略 |
| DR-3 | 节点区域（header+vector）与邻居区域字节**分账统计** | 修正 §2.2 的口径缺陷，使 `requested = useful + wasted` 在邻居区域内自洽 |
| DR-4 | 每查询计时与 CSV 导出放在**独立实验程序**，不侵入核心库 | 满足 D3（不改搜索主流程），也让 Baseline 2/3 复用同一 harness |
| DR-5 | CLI 别名 `baseline1` 映射到 `ReadPolicy::Fixed`（可选，P1） | 满足 D2 的"可独立选择"，同时零风险 |
| DR-6 | 1M 规模实验使用离线 recall | 规避陷阱 2 |
| DR-7 | 实验程序内部维护跨更新的统计累加器 | 规避陷阱 1 |

---

## 5. 任务分解（WBS）

工作量以"半天"为单位估算；总计约 **3.5–4 个工作日**，对应 [experiment.md](experiment.md#L308-L310) 第 3 周前半段。

### T1 — 策略规范化与边界检查（P0，0.5 天）

**改动文件**：`include/rdma_hnsw/read/read_strategy.h`、`src/read/read_strategy.cc`

- 在头文件声明具名类 `Baseline1FixedStrategy`，`name()` 返回 `"baseline1_fixed"`；工厂 `ReadPolicy::Fixed` 改为 `std::make_unique<Baseline1FixedStrategy>()`。
- 补检查：`capacity == 0` → 返回错误；`!r.ok || r.bytes != n` → 返回 `{ {}, false, "short fixed neighbor read: " + r.error }`；`degree` 超过 `capacity` 时截断。
- 保持"只返回前 `degree` 个 slot"的语义（空 slot 里的 0 不得当作邻居）。

**骨架**：

```cpp
// src/read/read_strategy.cc
NeighborReadResult Baseline1FixedStrategy::read(const RemoteNodeView &v,
                                                ReadContext &c) {
  if (v.capacity == 0) return {{}, false, "layer capacity is zero"};
  const std::size_t n = sizeof(std::uint32_t) + v.capacity * sizeof(NodeId);
  std::vector<byte> b(n);
  const auto r = c.transport.read({v.layer_offset, 0}, b);
  if (c.metrics) { c.metrics->read_ops++; c.metrics->requested_bytes += n;
                   c.metrics->layer_reads++; c.metrics->capacity_sum += v.capacity; }
  if (!r.ok || r.bytes != n) return {{}, false, "short fixed neighbor read: " + r.error};
  std::uint32_t degree{};
  std::memcpy(&degree, b.data(), sizeof(degree));
  const auto effective = std::min<std::uint32_t>(degree, v.capacity);
  if (c.metrics) {
    c.metrics->useful_bytes += sizeof(degree) + effective * sizeof(NodeId);
    c.metrics->wasted_bytes += (v.capacity - effective) * sizeof(NodeId);
    c.metrics->degree_sum   += effective;
  }
  return decode(std::span<const byte>(b).subspan(sizeof(degree)), effective);
}
```

**验收**：`ctest -R read_strategy_test` 通过；`--read-strategy fixed` 行为不变。

### T2 — 指标结构扩展（P0，0.5 天）

**改动文件**：`include/rdma_hnsw/read/read_strategy.h`（`ReadMetrics`）、`src/read/read_strategy.cc`（`merge()`）

新增字段（示例）：

```cpp
struct ReadMetrics {
  // ...既有字段保持不变...
  std::uint64_t layer_reads{};     // 层读取次数（Baseline1 = 每 (node,level) 一次）
  std::uint64_t node_reads{};      // QueryEngine 读取 header+vector 的次数
  std::uint64_t node_bytes{};      // 上述读取的字节数
  std::uint64_t degree_sum{};      // Σ min(degree, capacity)，用于算平均度数
  std::uint64_t capacity_sum{};    // Σ capacity，用于算容量利用率
  std::vector<std::uint32_t> degree_hist;  // 可选：degree 直方图
  void merge(const ReadMetrics &o);
};
```

**同时修改** [src/runtime/query_engine.cc](../src/runtime/query_engine.cc#L47-L64) 的 `read_node()`：把 `read_ops`/`requested_bytes` 的累加改为记入 `node_reads` / `node_bytes`，使 `requested_bytes` 只代表**邻居区域**请求。

> ⚠️ 这项改动会让 `tests/read_strategy_test.cc` 的既有断言仍然成立（该测试直接调用策略，不经 `read_node`），但会影响任何依赖 `requested_bytes` 包含节点字节的代码。**改动后必须全量跑 `ctest`。**

**验收**：新增断言 `requested_bytes == useful_bytes + wasted_bytes`（邻居区域内恒等式）通过。

### T3 — 正确性测试（P0，0.5 天）

**改动文件**：`tests/read_strategy_test.cc`（扩展现有测试）

覆盖用例：

| 用例 | 构造方式 | 期望 |
| --- | --- | --- |
| degree < capacity | `M=4, degree=2`（已有） | `wasted == 6*4`，返回值为索引真值前 2 个 |
| degree == capacity | `M=2, degree=4`（level 0 cap=4） | `wasted == 0` |
| degree > capacity（畸形） | 手工调用 `GraphStore::write` 后覆盖字节，或直接构造 `RemoteNodeView` 指向自造 buffer | 截断到 capacity，无越界读 |
| 多层节点 | `max_level = 2`，校验 `node_layer_offset` 在 level 0（2M）与 level>0（M） | 各层邻居正确 |
| 空 slot 不当邻居 | degree 之后填 0 | 返回值长度恰为 degree，且不含额外 id |
| capacity == 0 | 构造 `RemoteNodeView{capacity = 0}` | 返回 `ok == false`，不崩溃 |
| 短读 | 用截断长度的伪 transport | 返回错误而非伪造邻居 |

**验收**：`ctest --output-on-failure` 全绿；测试文件保留 `assert`（CMake 已用 `-UNDEBUG` 保证 Release 下生效，见 [CMakeLists.txt](../CMakeLists.txt#L129-L133)）。

### T4 — 实验驱动与 CSV 导出（P0，1 天）

**新增文件**：`experiments/baseline1_main.cc`（新目录）

职责：

1. 解析参数：`--index`、`--transport mmap|rdma`、`--queries`、`--base`、`--ef-search`、`--k`、`--queries-count`、`--updates-count`、`--update-op`、`--update-ratio`、`--out CSV`、`--warmup`；
2. 构造 `ComputeNode`，注入 `set_read_strategy(std::make_unique<Baseline1FixedStrategy>())`；
3. 静态路径：逐条 `node.query(q)`，用 `steady_clock` 计时，前后快照 `stats().reads` 求差；
4. mixed 路径：**在每次 `update()` 之前**把 `stats()` merge 进外部累加器并清账（规避陷阱 1），同时记录更新延迟；
5. 输出：CSV 原始样本 + stdout 汇总（qps、p50/p95/p99、requested/useful/wasted、read_ops、recall）；
6. 统计 `degree_sum/capacity_sum` 得到容量利用率与浪费率。

**关键片段**：

```cpp
for (const auto &q : queries) {
  ReadMetrics before = node.query_engine().stats().reads;
  auto t0 = std::chrono::steady_clock::now();
  auto ids = node.query(q);
  double us = std::chrono::duration<double, std::micro>(
      std::chrono::steady_clock::now() - t0).count();
  ReadMetrics after = node.query_engine().stats().reads;
  ReadMetrics d = diff(after, before);
  samples.push_back({us, d.requested_bytes, d.useful_bytes, d.wasted_bytes, d.read_ops});
  if (need_update) {           // 更新前先结算，避免 reload() 清零
    accumulator.merge(node.query_engine().stats().reads);
    node.update(req);
  }
}
```

**验收**：一条命令产出 CSV；同一配置两次运行结果一致（固定 `--seed`）。

### T5 — CMake 注册（P0，0.25 天）

**改动文件**：`CMakeLists.txt`

```cmake
add_executable(baseline1_experiment experiments/baseline1_main.cc)
target_link_libraries(baseline1_experiment PRIVATE rdma_hnsw_runtime)
target_compile_options(baseline1_experiment PRIVATE -Wall -Wextra -Wpedantic)
if(BUILD_TESTING)
  add_test(NAME baseline1_experiment_smoke
           COMMAND baseline1_experiment --smoke --index ${CMAKE_BINARY_DIR}/smoke.bin)
endif()
```

**验收**：`cmake --build build -j` 无警告；`ctest` 包含新测试。

### T6 — CLI 别名（P1，0.25 天）

**改动文件**：`include/rdma_hnsw/common/types.h`、`src/common/config.cc`

- `read_policy_from_string`：`"baseline1"` → `ReadPolicy::Fixed`；
- `--read-strategy` 白名单加入 `"baseline1"`；`print_usage` 同步更新。

**验收**：`--read-strategy baseline1` 与 `--read-strategy fixed` 输出一致。

### T7 — 数据集与脚本（P1，0.5 天）

**新增文件**：`scripts/convert_sift.py`、`scripts/run_baseline1.sh`

- `convert_sift.py`：SIFT1M `.fvecs` 子集切分（10K / 100K / 1M），生成离线 ground truth（规避陷阱 2）；
- `run_baseline1.sh`：遍历 `{10K,100K,1M} × {静态, erase 10/30/50%, upsert 10/30/50%}`，落盘 CSV。

**验收**：一条命令复现全部 Baseline 1 数据。

### T8 — 分析与图表（P1，0.5 天）

产出（[experiment.md](experiment.md#L231) 动机实验要求）：

1. **degree 直方图**（按层分解，degree vs capacity）——所有结论的前提；
2. 浪费率 vs 更新比例（erase / upsert 两条曲线）；
3. 浪费率 vs 索引规模（10K/100K/1M）；
4. 浪费率 vs `M` / vs `dim`（验证 §3.1 的 `≈ 1/(dim+6)` 模型）；
5. 邻居区域字节占总请求字节的比例（对照 SHINE 的 1% 论断）。

> **必须先看第 1 张图**：如果静态基线的 base 层几乎总是 `degree == capacity`，则 Baseline 1 的浪费在静态场景下很小，结论必须落在动态场景与低维/高层节点上。这本身就是有价值且诚实的动机实验结论。

### T9 — WSL 验证与文档（P0，0.5 天）

执行 §7 运行手册全流程，并把命令、参数、结果写入交付报告。

---

## 6. 指标口径与统计公式

### 6.1 单次层读取

```text
requested = sizeof(uint32) + capacity * sizeof(NodeId)          = 4 + 4·cap
useful    = sizeof(uint32) + min(degree, cap) * sizeof(NodeId)  = 4 + 4·min(degree, cap)
wasted    = (capacity - min(degree, cap)) * sizeof(NodeId)      = 4·(cap - min(degree, cap))
read_ops  = 1
```

### 6.2 聚合口径

```text
浪费率        = Σ wasted / Σ requested                      （仅邻居区域）
容量利用率    = Σ min(degree, cap) / Σ capacity
平均度数      = Σ min(degree, cap) / Σ layer_reads
可回收上界    = 1 - 容量利用率                              （Baseline 2/3 的理论收益上界）
```

### 6.3 每次扩展节点的读取放大（用于解释总字节构成）

```text
对每个被扩展节点（层集合 L）：
  自身 header+vector 读        : 1 次，(24 + 4·dim) 字节
  每层邻居区域读               : |L| 次，Σ(4 + 4·cap(l)) 字节        ← Baseline 1 的浪费只在这里
  候选邻居 header+vector 读    : Σ_{l∈L} deg(l) 次，(24 + 4·dim) 字节   ← 支配总字节
总请求字节 ≈ 节点读 + 邻居读
邻居区域占比 ≈ 1 / (dim + 6)      （deg ≈ cap 时）
```

**报告必须区分"浪费字节"与"总请求字节"**：浪费率是相对邻居区域的；它对总流量的影响还要乘以上面的占比。两者混谈是常见错误。

### 6.4 Recall

`QueryEngine::run()` 用 `base` 暴力求精确 top-k，逐命中累加 `1/k` → recall@k（[src/runtime/query_engine.cc](../src/runtime/query_engine.cc#L136-L155)）。所有策略必须在**相同 recall** 下比较（[experiment.md](experiment.md#L237-L247)）。

---

## 7. WSL Ubuntu 运行手册

> 前提：WSL2 无 RDMA 硬件（无 `/dev/infiniband`、无 IB LID），因此 Baseline 1 的开发与正确性测试全部走 **mmap**。正式 RDMA 实验放到有网卡的机器或 eRDMA 实例，见 [erdma.md](erdma.md)。

### 7.1 准备

```powershell
wsl -d Ubuntu
```

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake git python3
# 路径含中文且跨 /mnt 慢，复制到 Linux 文件系统再构建
cp -r "/mnt/d/JTZ/whk03/科研轮转/RDMA-HNSW-Lab" ~/rdma-hnsw-lab
cd ~/rdma-hnsw-lab
```

### 7.2 构建（WSL 关闭 RDMA，避免依赖/stub 干扰）

```bash
cmake -S . -B build -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure
```

预期：`graph_store_test`、`read_strategy_test`、`hnsw_builder_test`、`runtime_api_test`、`cm_protocol_test` 通过。

### 7.3 构建索引

```bash
mkdir -p data out
# 低维合成数据：放大邻居读取占比，便于验证测量链路
./build/hnsw_build --generate 10000 --dim 32 --M 16 \
  --ef-construction 100 --seed 42 --output data/index32.bin
# 与主实验同维度的合成数据
./build/hnsw_build --generate 10000 --dim 128 --M 16 \
  --ef-construction 100 --seed 42 --output data/index128.bin
```

### 7.4 静态查询（Baseline 1）

```bash
./build/compute_client --index data/index128.bin --transport mmap \
  --read-strategy fixed --queries-count 1000 --ef-search 64 --k 10 \
  --seed 42 --verbose
```

端到端 demo（自动构建 + 静态 + 带更新两轮）：

```bash
./scripts/run_local_demo.sh
```

### 7.5 动态 workload（动机实验关键）

```bash
# erase：base 层 degree 永久下降 → 浪费上升
./build/compute_client --index data/index128.bin --transport mmap \
  --workload mixed --update-op erase --update-ratio 0.3 \
  --queries-count 3000 --ef-search 64 --k 10 --read-strategy fixed

# upsert：目标节点 base 邻居表被重填满 → 浪费下降
./build/compute_client --index data/index128.bin --transport mmap \
  --workload mixed --update-op upsert --update-ratio 0.3 \
  --queries-count 3000 --read-strategy fixed
```

### 7.6 真实数据集（SIFT1M）

SIFT1M 的 `.fvecs` 已经是"int32 维度 + float32 分量"，与 [src/common/data.cc](../src/common/data.cc#L7-L31) 的 reader 一致，base/query 可直接使用；只需子集切分与 ground truth 生成。

```bash
./build/hnsw_build --input data/sift_base_10k.fvecs --dim 128 \
  --M 16 --ef-construction 200 --output data/sift10k.bin
./build/compute_client --index data/sift10k.bin --transport mmap \
  --queries data/sift_query.fvecs --base data/sift_base_10k.fvecs \
  --read-strategy fixed --queries-count 1000 --ef-search 64 --k 10
```

> 1M 规模**不要**传 `--base`（见陷阱 2），改用 T7 的离线 recall。

### 7.7 自研驱动

```bash
./build/baseline1_experiment --index data/index128.bin --transport mmap \
  --queries-count 1000 --ef-search 64 --k 10 --warmup 100 \
  --out out/baseline1_dim128_static.csv
```

---

## 8. 实验与参数矩阵

### 8.1 固定主配置（横向比较用）

```text
数据集   : SIFT1M（子集 10K / 100K / 1M），128 维，L2
HNSW     : M = 16, ef-construction = 200
查询     : ef-search = 64, k = 10
策略     : Baseline1FixedStrategy
传输     : mmap（开发/正确性）→ RDMA（正式性能）
```

### 8.2 Baseline 1 动机实验矩阵

| 维度 | 取值 | 目的 |
| --- | --- | --- |
| 索引规模 | 10K, 100K, 1M | 图规模对 degree 分布与浪费的影响 |
| 更新类型 | 静态, erase, upsert, erase+upsert 混合 | 验证陷阱 3 的非对称性 |
| 更新比例 | 0%, 10%, 30%, 50% | 浪费 vs 更新比例曲线 |
| `M` | 8, 16, 32 | 容量放大与浪费的关系 |
| 维度 | 32, 128 | 验证 `≈1/(dim+6)` 占比模型 |

### 8.3 每组记录

```text
strategy, dataset, dim, node_count, M, ef_construction, ef_search, k,
threads, update_op, update_ratio, queries, updates,
qps, latency_p50_us, latency_p95_us, latency_p99_us,
requested_bytes, useful_bytes, wasted_bytes, waste_ratio,
node_bytes, neighbor_share, read_ops, layer_reads,
capacity_utilization, recall
```

---

## 9. 排期

| 天 | 任务 | 产出 |
| --- | --- | --- |
| D1 上午 | §3 论文阅读（2.pdf + 1.pdf 重点部分） | 阅读笔记：定长节点假设、1% 论断、`≈1/(dim+6)` 模型 |
| D1 下午 | T1 + T2 | 具名策略 + 指标分账；`ctest` 绿 |
| D2 上午 | T3 | 正确性测试全覆盖 |
| D2 下午 | T4 + T5 | 实验驱动 + CMake + CSV |
| D3 上午 | T6 + T7 + WSL 全流程（T9） | CLI 别名、数据脚本、WSL 可复现 |
| D3 下午 | 合成数据测量链路验证（dim=32 放大效应） | 首批 CSV，确认测量正确 |
| D4 上午 | T8 第一批：degree 直方图 + erase/upsert 曲线 | 动机实验图 1–3 |
| D4 下午 | T8 第二批：规模/M/dim 扫描 + 报告 | 动机实验图 4–5、Baseline 1 小节初稿 |

---

## 10. 风险登记

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 静态图 base 层 degree 普遍等于 capacity → 浪费极小 | 动机实验缺乏张力 | 先出 degree 直方图；结论转向动态场景、高层、低维、大 M；同时报告"邻居区域占比 ≈1/(dim+6)"说明绝对值边界 |
| mmap 下 `reload()` 清零统计（陷阱 1） | 数据完全错误 | T4 中更新前结算并 merge；报告中说明快照/缓存生命周期 |
| `requested_bytes` 口径混入节点字节（§2.2） | 浪费率被系统性低估 | T2 分账；测试中断言 `requested == useful + wasted` |
| 1M 规模 `--base` 暴力搜索过慢（陷阱 2） | 实验无法完成 | T7 离线 recall |
| `/mnt/d` + CJK 路径 | 计时噪声、工具报错 | 复制到 `~/rdma-hnsw-lab` |
| WSL2 无 RDMA | 无法出正式性能结论 | 开发用 mmap；正式实验迁移到真机/eRDMA（[erdma.md](erdma.md)） |
| 改动 `ReadMetrics` 破坏既有测试 | 回归 | 只增字段；改后全量 `ctest` |
| 误改 `QueryEngine::search()` | 违反 D3，比较失去意义 | 代码评审时重点核查该文件 diff |

---

## 11. 附录

### 11.1 关键代码位置速查

| 用途 | 位置 |
| --- | --- |
| Baseline 1 实现 | [src/read/read_strategy.cc](../src/read/read_strategy.cc#L30-L48) |
| 指标结构 | [include/rdma_hnsw/read/read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L9-L22) |
| 策略接口 | [include/rdma_hnsw/read/read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L48-L55) |
| 层容量与层偏移 | [include/rdma_hnsw/graph/graph_store.h](../include/rdma_hnsw/graph/graph_store.h#L46-L48) / [graph_store.h](../include/rdma_hnsw/graph/graph_store.h#L75-L83) |
| 索引写入（定长 slot 填充） | [src/graph/graph_store.cc](../src/graph/graph_store.cc#L145-L164) |
| 索引读取（全 slot 解析） | [src/graph/graph_store.cc](../src/graph/graph_store.cc#L94-L104) |
| 查询主流程（禁止修改） | [src/runtime/query_engine.cc](../src/runtime/query_engine.cc#L66-L123) |
| 节点读取统计点 | [src/runtime/query_engine.cc](../src/runtime/query_engine.cc#L47-L64) |
| 策略调用点 | [src/runtime/query_engine.cc](../src/runtime/query_engine.cc#L99-L105) |
| `reload()` / `update()` | [src/runtime/compute_node.cc](../src/runtime/compute_node.cc#L61-L68) / [compute_node.cc](../src/runtime/compute_node.cc#L108-L123) |
| 更新对 degree 的影响 | [src/runtime/storage_engine.cc](../src/runtime/storage_engine.cc#L21-L73) |
| CLI 选项解析 | [src/common/config.cc](../src/common/config.cc#L68-L113) |
| 测试注册 | [CMakeLists.txt](../CMakeLists.txt#L106-L133) |

### 11.2 命令速查

```bash
# 构建与测试
cmake -S . -B build -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure

# 索引
./build/hnsw_build --generate 10000 --dim 128 --M 16 --ef-construction 100 --output data/index.bin
./build/hnsw_build --input data/sift_base_10k.fvecs --dim 128 --M 16 --ef-construction 200 --output data/sift10k.bin

# Baseline 1
./build/compute_client --index data/index.bin --transport mmap --read-strategy fixed \
  --queries-count 1000 --ef-search 64 --k 10 --verbose
./build/baseline1_experiment --index data/index.bin --transport mmap \
  --queries-count 1000 --ef-search 64 --k 10 --out out/baseline1.csv
```

### 11.3 与后续周次的衔接

T2 的指标扩展、T4 的实验驱动与 CSV 口径、T3 的测试骨架将被 Baseline 2/3 与分级前缀机制**直接复用**。因此本计划的产出应按"可被其他策略继承"的方式组织：

- 指标结构保持"策略无关"，新增字段对所有策略通用；
- 实验程序的策略注入点参数化（`--read-strategy` 或 `set_read_strategy`），不硬编码 Baseline 1；
- 正确性测试组织成"策略 × 用例"矩阵，后续策略只需注册名字即可纳入一致性对比。

---

## 12. 完成检查清单

> 状态：**已全部完成**。实施结果、实测数据与复现命令见 [baseline1-report.md](baseline1-report.md)。

- [x] `Baseline1FixedStrategy` 具名、有 `name()` 与注释（D1）
- [x] 可独立选择：`--read-strategy fixed|baseline1`（D2）
- [x] `QueryEngine::search()` 未被修改；策略内无 ibverbs 调用（D3）
- [x] capacity / degree / 长度 / 短读 / 截断检查齐全（D4）
- [x] `tests/read_strategy_test.cc` 覆盖 §T3 全部用例，`ctest` 全绿（D5）
- [x] 指标分账完成，断言 `requested == useful + wasted`（D6）
- [x] `experiments/baseline1_main.cc` + CMake + smoke test（D6）
- [x] 每查询延迟分位数、每查询字节数、read_ops 可导出（D6）
- [x] degree 直方图、erase/upsert 曲线、规模/M/dim 扫描（D7）
- [x] 构建命令、运行参数、原始 CSV、统计脚本齐备（D8）
- [x] WSL 全流程可从零复现（§7）
- [x] 报告含"邻居区域占比 ≈1/(dim+6)"与 SHINE 1% 论断的对照讨论
