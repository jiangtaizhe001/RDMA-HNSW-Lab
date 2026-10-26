# Baseline 3 实施计划：计算侧缓存邻居长度

本文是 [experiment.md](experiment.md) 中「Baseline 3」的落地计划。结构与 [baseline1-plan.md](baseline1-plan.md)、[baseline2-plan.md](baseline2-plan.md) 对齐，可以并行阅读：**Baseline 1 的测量底座（`ReadMetrics` 分账、实验驱动、CSV 口径）与 Baseline 2 的两跳读取语义、`cache_*` 预留字段是本计划的前置依赖**，本计划只写 Baseline 3 特有的部分。

相关文档：[experiment.md](experiment.md)（课题要求）、[help.md](help.md)（§7 是 Baseline 3 的伪代码、§10 是字段含义）、[erdma.md](erdma.md)（RDMA 部署）、[baseline1-plan.md](baseline1-plan.md)（公共测量底座）、[baseline2-plan.md](baseline2-plan.md)（共享语义与恒等式）。

> **状态**：已实施（WSL2 + mmap）。实施结果、实测数据与假设检验见 [baseline3-report.md](baseline3-report.md)。

---

## 0. 核心判断（五条，先看这里）

**① 它是三个 Baseline 里唯一"会随 workload 自己变好或变坏"的策略。** Baseline 1 的浪费、Baseline 2 的往返都由**图结构**单方面决定；Baseline 3 的代价由**命中率 `p` 与低估率 `q`** 决定，而 `p`、`q` 由「同一 `(node, level)` 是否被反复访问」「缓存容量是否覆盖工作集」「degree 是否被更新改写」共同决定。因此本计划的结论必须是 `p − q` 的函数，而不是一个命中率数字。

**② 缓存必须活得比 `QueryEngine` 长，否则动态实验全是假的。** mmap 下 [`ComputeNode::update()`](../src/runtime/compute_node.cc#L108-L123) 会调 [`reload()`](../src/runtime/compute_node.cc#L61-L68)，销毁并重建 `engine_`、`transport_`、`context_`；实验驱动的 `inject()` 又会调 [`set_read_strategy()`](../src/runtime/query_engine.cc#L34-L40)，把 `context_.length_cache` 再清一次。**每次更新都会让缓存清零。** 解法：缓存数据放在实验程序持有的 `std::shared_ptr<LengthCache>` 里，每次 reload 后用同一个 shared cache 重建策略（§7 T5）。

**③ 禁止用 `generation` 预判命中。** [experiment.md §五](experiment.md#L63) 与 [§六.5](experiment.md#L180) 两处点名：Baseline 3 与分级机制"不在读取前检查 generation，而是在本次读取中检查真实 degree，发现预测不足后 fallback"。可以**存** generation 供统计，但命中判定只能看本次读回的 degree。这条必须在报告里写成设计约束，并用测试钉死（§9 用例 12/13）。

**④ 缓存键必须含 `level`。** [`ReadContext::length_cache`](../include/rdma_hnsw/read/read_strategy.h#L64) 是 `unordered_map<NodeId, pair<Generation, uint32_t>>` 单键。同节点 `layer_capacity(0) = 2M`、`layer_capacity(l>0) = M`（[graph_store.h](../include/rdma_hnsw/graph/graph_store.h#L46-L48)），degree 上界不同，单键必然跨层污染。用 `key = (uint64(level) << 32) | id` 自建缓存，**不要复用** `ReadContext` 那张表。

**⑤ 默认缓存容量会让容量扫描变成一条直线。** `cache_capacity` 默认 100000（[read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L63)、[compute_node.h](../include/rdma_hnsw/runtime/compute_node.h#L23)），而 10K 索引的全部 `(node, level)` 键只有约 **1.07 万**个（见 [baseline1-report.md](baseline1-report.md#L190-L197) 的层分布）。不显式传小值，永远不会发生淘汰，experiment.md §9 要求的"缓存容量 2~3 个取值"就测不出任何东西。

---

## 1. 目标与验收标准

### 1.1 Baseline 3 的定义

> 在计算节点按"节点 ID + 层"缓存上一次观察到的邻居长度。缓存命中时，按缓存长度请求 `degree` 以及相应数量的邻居 slot；返回的真实 degree 不超过预测长度时，直接截取有效邻居并把多读的 slot 计入浪费。若真实 degree 超过预测长度，说明缓存预测不足，执行第二次 RDMA READ 读取剩余邻居（或完整的实际范围），合并结果并更新缓存。缓存未命中时可以先读包含 degree 的最小 header，再读取实际长度；这部分应单独计入 Baseline3 的 miss/fallback 开销。
> —— [experiment.md](experiment.md#L101-L113)

对应本原型的落点（[help.md §7](help.md#L193-L234)）：`RemoteNodeView::layer_offset` 指向该层的 `degree` 字段，邻居 slot 紧跟在后面；命中时的**一次**读取必须**同时包含** degree 字段与预测宽度内的前缀。

### 1.2 三态读取与单层账本（写代码前必须定死）

设该层 `cap = view.capacity`，本次读到并截断后的真实度数 `d = min(raw_degree, cap)`，缓存 hint 为 `h`（写入时已 clamp 到该层 `cap`，读取时再 clamp 一次，`h ∈ [0, cap]`）。

| 状态 | 触发条件 | 发起的 READ | `read_ops` |
| --- | --- | --- | --- |
| **A. miss（冷启动）** | 缓存无该 key | ① `[layer_offset, 4B)` → degree；若 `d > 0` ② `[layer_offset+4, 4d)` | `1 + [d>0]` |
| **B. hit 且 `d ≤ h`** | 有缓存且预测足够 | **仅一次** `[layer_offset, 4 + 4h)`（degree 搭在里面） | **1** |
| **C. hit 且 `d > h`（低估/失效）** | 预测不足 | ① `[layer_offset, 4 + 4h)` ② `[layer_offset+4+4h, 4(d−h))` | 2 |

单层字节账本（三种状态都必须满足 `requested == useful + wasted`）：

```text
A. miss      : requested = 4 + 4d            useful = 4 + 4d            wasted = 0
B. hit       : requested = 4 + 4h            useful = 4 + 4d            wasted = 4(h − d)
C. fallback  : requested = (4+4h) + 4(d−h) = 4 + 4d
               useful    = 4 + 4h + 4(d−h)  = 4 + 4d                    wasted = 0
```

与两个已实现 Baseline 的对照：

```text
B1 : ops = 1                bytes = 4 + 4·cap      wasted = 4(cap − d)
B2 : ops = 1 + [d>0]        bytes = 4 + 4·d        wasted = 0
B3 : ops = (1−p)(1+[d>0]) + (p−q)·1 + q·2 = 2 − p + q     （d > 0 时）
```

**注意状态 A 是刻意设计成与 Baseline 2 完全同形的**：experiment.md 明确要求"缓存未命中时可以先读包含 degree 的最小 header，再读取实际长度；这部分应单独计入 Baseline3 的 miss/fallback 开销"，因此 miss 的两次读**不算** fallback，用独立的 `cold_start_reads` 计数（见 §8）。

### 1.3 完成判据（Definition of Done）

| 编号 | 判据 | 来源 |
| --- | --- | --- |
| D1 | 存在具名策略类 `Baseline3CachedLengthStrategy`，含清晰类名、`name()` 返回值与注释 | [§六.5](experiment.md#L173-L174) |
| D2 | 可通过 `--read-strategy cached_length\|cached` 与 `set_read_strategy()` **独立选择** | [§六.1](experiment.md#L137-L140) |
| D3 | 未修改 `QueryEngine::search()`；策略内无任何 ibverbs 调用 | [§六.5](experiment.md#L175-L178) |
| D4 | 边界齐全：`capacity==0`、header 短读/失败、fallback 短读/失败、`degree>capacity` 截断、`degree==0`、缓存条目 `degree>capacity` | [§六.5](experiment.md#L177-L179) |
| D5 | 命中时**确实只要一次** READ；未做预读、未把状态 C 的两段合并 | [help.md](help.md#L189) 的同类要求 |
| D6 | 与 Baseline 1 / Baseline 2 **逐邻居一致**；冷缓存与热缓存两种状态下结果一致 | [§六.5](experiment.md#L179) |
| D7 | 指标可导出：命中率、未命中、失效、淘汰、fallback 次数与字节、预测过宽浪费、平均 hint vs 平均 degree | [§五.4](experiment.md#L105-L111)、[§六.3](experiment.md#L148-L163) |
| D8 | 动态场景数据：`erase`/`upsert` × 更新比例 × **缓存容量** × **更新分布** | [§五.4](experiment.md#L108-L111)、[§六.3](experiment.md#L162) |
| D9 | **缓存正确性语义**：失败/短读**不污染缓存**；缓存跨 `update()`/`reload()` 存活 | [§六.5](experiment.md#L180) |
| D10 | **未使用 generation 预判命中**，且有专门测试证明 | [§五](experiment.md#L63)、[§六.5](experiment.md#L180) |
| D11 | 源码、构建命令、运行参数、原始数据与统计脚本齐备，可复现 | [§六.5](experiment.md#L180) |

### 1.4 三个 Baseline 的分工（写报告时不要混）

| | 被谁支配 | 失败模式 | 本计划要量化的量 |
| --- | --- | --- | --- |
| B1 fixed | 图结构与 `M` | 传输浪费 `4(cap−d)` | 浪费率（B1 已交付） |
| B2 header_actual | 图结构与往返代价 `R` | 每层固定多一次依赖读 | `saved_bytes` 与 op 惩罚（B2 已交付） |
| **B3 cached_length** | **访问局部性 + 缓存容量 + degree 漂移** | **miss 退化成 B2；低估比 B2 还贵一次 op** | **`p − q`、按层命中率、失效/淘汰/fallback 字节** |

---

## 2. 现状盘点

### 2.1 已经具备（直接复用）

| 能力 | 位置 | 说明 |
| --- | --- | --- |
| 策略接口、`ReadContext`、`ReadMetrics` | [read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L19-L72) | B3 实现同一接口 |
| 层地址（指向 degree 字段）与层容量 | [graph_store.h](../include/rdma_hnsw/graph/graph_store.h#L75-L83)、[graph_store.h](../include/rdma_hnsw/graph/graph_store.h#L46-L48) | `node_layer_offset()` / `layer_capacity()` |
| `decode()` 辅助函数 | [read_strategy.cc](../src/read/read_strategy.cc#L13-L20) | fallback 合并时可复用/扩展 |
| Baseline 2 的两跳读取与边界检查写法 | [read_strategy.cc](../src/read/read_strategy.cc#L69-L134) | 状态 A 直接照它的形状写 |
| 指标分账与 `merge()` | [read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L19-L41)、[read_strategy.cc](../src/read/read_strategy.cc#L136-L155) | 只增字段（baseline1-plan DR-2） |
| **预留给 B3 的 5 个字段** | [read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L26-L30) | `cache_hits` / `cache_misses` / `cache_invalidations` / `prefix_reads` / `fallbacks` —— **全代码库无人自增**，就是给 B3/B4 的 |
| `cache_capacity` / `prefix_granularity` 全链路 | [config.cc](../src/common/config.cc#L98-L101) → [config.h](../include/rdma_hnsw/common/config.h#L34-L35) → [compute_client.cc](../src/compute_client.cc#L28) → [compute_node.h](../include/rdma_hnsw/runtime/compute_node.h#L22-L23) → [query_engine.cc](../src/runtime/query_engine.cc#L26-L32) → `ReadContext` | CLI 侧**不需要新接口**，只差工厂分支 |
| CLI 名称映射 | [types.h](../include/rdma_hnsw/common/types.h#L39-L49)、[config.cc](../src/common/config.cc#L68-L76) | `cached_length` / `cached` → `ReadPolicy::CachedLength` **已就绪** |
| 实验驱动（逐查询快照、跨更新累加、分位延迟、CSV、离线 GT） | [baseline1_main.cc](../experiments/baseline1_main.cc) | B3 在同一二进制内参数化注入 |
| 扫描/分析脚本骨架（独立索引副本、成对采集、恒等式校验） | [run_baseline2.sh](../scripts/run_baseline2.sh)、[analyze_baseline2.py](../scripts/analyze_baseline2.py) | 扩成 3 策略 × 容量 × 分布 |
| `FakeTransport`（畸形 degree / 短读 / 指定第 N 次调用失败 / 调用计数） | [read_strategy_test.cc](../tests/read_strategy_test.cc#L22-L54) | B3 全部边界用例 |

### 2.2 缺失（本计划要补）

| 缺口 | 证据 | 影响 |
| --- | --- | --- |
| 策略类本身 | 工厂在 `HeaderActual` 之外一律抛 `"student extension"`：[read_strategy.cc](../src/read/read_strategy.cc#L157-L165) | D1 / D2 |
| **缓存生命周期** | [compute_node.cc](../src/runtime/compute_node.cc#L61-L68) 的 `reload()` + [query_engine.cc](../src/runtime/query_engine.cc#L34-L40) 的 `set_read_strategy()` 都会清缓存 | D8 / D9：动态命中率不可信 |
| 驱动策略白名单 | 只认 `fixed\|baseline1\|header_actual\|header`：[baseline1_main.cc](../experiments/baseline1_main.cc#L198-L204) | D2 |
| **驱动未把 `cache_capacity` 灌进 `cfg`** | [baseline1_main.cc](../experiments/baseline1_main.cc#L370-L379) 只设了 index/transport/host/port/policy/distance/ef_search/k | **容量扫描全部无效** |
| `strategy_label()` 会错标 B3 | `HeaderActual ? "baseline2_header_first" : "baseline1_fixed"`：[baseline1_main.cc](../experiments/baseline1_main.cc#L81-L84) | 分析脚本配对错乱 |
| `cache_*` 字段从未自增 | grep 结果：只在 [merge()](../src/read/read_strategy.cc#L143-L147) 出现 | D7 |
| 缺 fallback/淘汰/预测过宽的字段 | `fallback_bytes` / `cache_evictions` / `over_read_slots_sum` 等不存在 | D7 |
| 无 `--update-distribution` | 更新 id 是顺序轮转 `ui % count`：[baseline1_main.cc](../experiments/baseline1_main.cc#L466-L483) | experiment.md §9 要求"随机更新 + 热点更新"各一种 |
| 无并发度 | [`Config::threads`](../include/rdma_hnsw/common/config.h#L33) 能解析但无人使用；`QueryEngine::context_`/`stats_` 非线程安全 | §9 要求"至少两种并发度" |
| summary CSV 表头只在文件新建时写 | [baseline1_main.cc](../experiments/baseline1_main.cc#L549-L556) | 新列混写会错位 |
| 无 B3 脚本与报告 | `scripts/` 只有 baseline1/2 的四个脚本 | D8 / D11 |

### 2.3 必须提前知道的四个陷阱

**陷阱 1：每次更新都会清零缓存（mmap）。** 链路是 `ComputeNode::update()` → `local_storage_->apply()` → `reload()` → 新的 `QueryEngine` = 新的策略对象 + 新的 `context_`；驱动的 `inject()` 还会再清一次 `context_.length_cache`。**若不处理，mmap 下"命中率 vs 更新比例"整组实验只反映"距上次更新以来的窗口"。**

> 反之，**RDMA 模式下 upsert/erase 不会 reload**（[compute_node.cc](../src/runtime/compute_node.cc#L115-L122) 只做 `refresh_remote_catalog`），所以这是 mmap 后端的固有假象，必须在报告里说明，且不要把 mmap 的动态命中率当作最终结论。

**陷阱 2：`generation` 看起来"正好可用"，但它是禁用的。** [storage_engine.cc](../src/runtime/storage_engine.cc#L21-L73) 里 erase 会 `++other.generation`、upsert/insert 也会自增，因此 `view.generation != cached.generation` 与"degree 变了"高度相关 —— 这正是诱惑所在。但一旦用它预判，命中率就变成 generation 传播的函数，而在真实 RDMA 部署里 generation 需要广播/校验才可靠（多一次往返），机制就退化了。**只能存，不能用。**

**陷阱 3：`ReadContext::length_cache` 是单键且会被清空。** 见 §0 ④ 与陷阱 1。B3 一律使用自建的 `LengthCache`。

**陷阱 4：`erase` 与 `upsert` 对缓存的影响方向相反。** 沿用 [baseline1-plan 陷阱 3](baseline1-plan.md#L113-L120) 的机制（[storage_engine.cc](../src/runtime/storage_engine.cc#L26-L72)）：

- `Erase`：从**其他所有节点**的 `neighbors[0]` 里删掉该 id → 那些节点 degree 永久下降 → 命中时 `h > d` → **产生预测过宽浪费，但不触发 fallback**；
- `Upsert`/`Insert`：目标节点 `neighbors[0]` 被清空后重填到 `cap`（2M），并可能改写其他节点的邻接表 → degree 上升 → `h < d` → **fallback 上升**。

→ 动态实验必须把 `erase` 与 `upsert` 分开报，否则 `p`、`q` 的效应相互抵消。

### 2.4 三策略共享语义表（在 [baseline2-plan §13.2](baseline2-plan.md#L655-L664) 基础上补全 B3 列）

| 情形 | B1 fixed | B2 header_actual | **B3 cached_length** |
| --- | --- | --- | --- |
| `capacity == 0` | 错误，0 次读 | 错误，0 次读 | **错误，0 次读，不写缓存** |
| `degree > capacity`（畸形） | 截断到 cap | 截断到 cap | **截断到 cap；若截断后 `> h` 则按 fallback 处理** |
| 缓存条目 `degree > capacity`（跨层污染/布局变化） | — | — | **读取时再次 clamp 到 cap（防御性）** |
| `degree == 0` | 读满 cap（有浪费） | 只读 4 字节，跳过第二次读 | **状态 A 只读 4B；状态 B（缓存 0）只读 4B；都写回缓存 0** |
| 短读 / 传输失败 | 错误 | 错误 | **错误，且不修改缓存条目** |
| `wasted_bytes` | `4(cap−d)` | `0` | **`4(h−d)`（仅状态 B）** |
| `header_reads` | 0 | 每层 1 | **仅状态 A（miss）= 1；命中 = 0** |
| `dependent_reads` | 0 | 每层 1（`d>0`） | **状态 A（`d>0`）1 次 + 状态 C 1 次** |
| `fallbacks` | 0 | 0 | **状态 C 每层 1 次** |

---

## 3. 论文阅读任务（设计前必须完成）

| 优先级 | 文档 | 必读部分 | 要回答的问题 |
| --- | --- | --- | --- |
| **P0** | `papers/2.pdf`（SHINE） | **§5 Node Caching** 整节：`What to Cache?` → `Cache Implementation` → **§5.1 Cache Replacement** → **§5.2 Cache Admission（+Discussion）** | SHINE 缓存的是**节点向量**，并明确写"neighbor lists contribute only a small fraction … `(M·8)/(M·d·4) = 2/d`，200 维时仅 **1%**，**we do not cache them**"。那么"缓存邻居长度"（每 `(node,level)` 仅 4B）的正当性在哪？→ **B3 省的不是带宽，而是一次依赖往返**（B2 的痛点）。这条论证必须自己算清并写进报告 |
| **P0** | `papers/2.pdf` | **§3.1 Index Layout**（header=id/max level/lock bit；每层"邻居列表前置数量前缀"；节点定长 `8 + d·4 + 4 + 2M·8 + l·(4 + M·8)` 字节） | 本项目 `uint32 degree + slots[cap]` 的来源；为什么"预测宽度的上界 = capacity，而 capacity 与真实 degree 脱钩" |
| **P0** | `papers/2.pdf` | **§3.2 Query Processing + Algorithm 1**（第 6 行 `neighborlist ← RDMA_READ(nlist_rptr)` 一次读完；第 11–14 行 `cache_lookup` → miss 则 READ → `if admit to cache then cache_insert`） | ① 第 6 行说明 **SHINE 不需要 header-first**：长度前缀搭在大读里 → B3 是它的反事实；② 第 11–14 行就是"**在读取路径里做缓存查询 + 准入**"的模板，与 B3 的 hit/miss 分支同构，但**缓存对象不同** |
| **P0** | `papers/2.pdf` | **§3.4 Index Updates**（lock bit、邻居列表的插入式修改） | degree 为何持续漂移 → **为什么不能依赖 generation 预判** |
| **P1** | `papers/2.pdf` | **§6.1 Cache Segmentation Penalty** | "缓存被切分/容量不足时相对理想缓存的损失"的量化范式，B3 的容量敏感性可用同一套语言表述 |
| **P1** | `papers/2.pdf` | **§8.4 Sensitivity Analysis**（`Cache Size Ratio`、`Skewness`）；§8.5 `Cache Compression` | **B3 的实验设计直接对标**：缓存容量 × 查询/更新偏斜。§8.5 提示"条目越小越可压缩"，正好支持"缓存 4B 长度而非整张邻居表" |
| **P1** | `papers/2.pdf` | §2.3 | one-sided RDMA 访问延迟 "∼2 µs" → 给 B3 的往返代价 `R` 取论文锚点（不要再用 baseline2-plan 里估的 1.5 µs） |
| **P1** | `papers/4.pdf`（FreshDiskANN） | 惰性删除、插入/删除规则、`StreamingMerge`、召回稳定性 | degree 分布以什么速率漂移 → 缓存失效从哪来、以什么频率来；为什么"更新比例"是自变量 |
| **P1** | `papers/3.pdf`（DiskANN） | **C 跳缓存**、BeamSearch 批取邻域 | C-hop cache 是"在计算侧缓存邻居 ID 以避免远程往返"的最接近对照（与 B3 同族但缓存对象不同）；批取说明"减少往返"的第三条路，可与 B3 叠加（进阶 6.4） |
| **P2** | `papers/1.pdf`（HNSW） | Alg.1/2/5；参数段（`M`、`Mmax0 = 2M`、`mL = 1/ln M`、`efConstruction`） | degree 的理论上界就是 capacity → hint 的合法范围、预测过宽的概率空间 |
| **P2** | `papers/7.pdf`（RDMA 综述） | 通信模式、单向 vs 双向、缓存与资源管理设计指南 | 为什么"多一次往返"贵；缓存一致性/资源管理的设计约束 |

### 3.1 三个必须带着读的判断

**判断一：SHINE 的 "we do not cache them" 不是对 B3 的反驳，而是对 B3 的约束。** SHINE 拒绝缓存的是**整张邻居表**（因为它相对向量只有 1%）；B3 缓存的是**每个 `(node,level)` 4 字节的长度元数据**，比向量小 2~3 个数量级，且收益是"把 B2 的专程 header 往返变成 0"。**报告必须把"缓存对象"写清楚，并把收益口径写在"往返/操作数"上，而不是"字节"上**，否则会被 SHINE 的 1% 论断直接反驳。

**判断二：`degree` 存在层区域而不是 `NodeDiskHeader`，是 B2 必须多付一次往返的根因（[baseline2-plan §5.5](baseline2-plan.md#L211-L217)），也是 B3 的存在理由。** 而 `QueryEngine::read_node()` 反正已经读了 24 字节 `NodeDiskHeader`。B3 选择"在计算侧缓存"而不是"把 degree 搬进节点 header"，是因为改布局会改变索引格式、波及所有策略的比较基准 —— 这个取舍要明确写出。

**判断三：B3 与 B1 在 op 数上不可能三角。** B1 只要 1 次 op（代价是浪费字节），B3 命中也要 1 次 op（且字节接近 B2）。所以 B3 的对手**不是 B1 而是 B2**：B3 相对 B2 的 op 收益恰为 `p − q`；相对 B1，B3 只有在…… 见 §5 的定量结论。

---

## 4. 成本模型：先把 `p` 和 `q` 算清楚，再写代码

这一节是本计划的核心。Baseline 3 的结论必须能用一个公式解释，否则数据只是数字。

### 4.1 单层精确账

```text
A. miss      : ops = 1 + [d>0]   bytes = 4 + 4d   waste = 0
B. hit (d≤h) : ops = 1           bytes = 4 + 4h   waste = 4(h−d)
C. hit (d>h) : ops = 2           bytes = 4 + 4d   waste = 0
```

### 4.2 每层期望代价（设 `d > 0`）

记 `p` = 命中率（该 `(node,level)` 读前已有缓存），`q` = **命中中低估的比例**（`q ≤ p`），`h̄` = 命中时的平均 hint：

```text
E[ops]_B3 = (1−p)·2 + (p−q)·1 + q·2 = 2 − p + q
E[bytes]_B3 ≈ (1−p)(4+4d) + (p−q)(4+4h̄) + q(4+4d)
            = 4 + 4d + (p−q)·4(h̄ − d)
```

**两个结论直接跳出来：**

1. **相对 Baseline 2 的 op 收益 = `p − q`。** 只要"命中且无需 fallback"的比例高于 0，B3 就赢 B2；若每次命中都低估（`q = p`），二者相等（都退化成 2 次 op）。
2. **相对 Baseline 1，B3 在 op 上永远不赢**（B1 恒定 1 次），它的价值只在字节：`4(cap−d) − (p−q)·4(h̄−d)`。理想情况 `h̄ = d`（精确缓存）时，B3 拿到 **B2 的字节 + 优于 B2 的 op**。

### 4.3 定位与适用区间

```text
B3 = 「B1 级的 op 数（当 p→1, q→0）+ B2 级的字节数」
两个失效模式：miss（完全退化成 B2）；低估（比 B2 还贵一次 op）
B3 的适用区间 ⊇ { 访问局部性高（高层节点、热点查询）× 缓存容量覆盖工作集 × degree 漂移慢 }
```

### 4.4 预注册假设（先写下来，再用实验证伪）

| 编号 | 假设 | 判据 | 若被证伪说明什么 |
| --- | --- | --- | --- |
| **H1** | 命中率随层升高而升高（`level ≥ 1` 远高于 `level 0`：高层节点被反复访问，base 层每次查询几乎都是新节点，即 SHINE 的 "numerous unique graph nodes"） | 按层命中率 | 访问模式与本原型不同，必须**按层分别报告**而不是只报全局 |
| **H2** | 命中率随 `cache_capacity` 先快后平，容量覆盖 `(node,level)` 工作集后不再上升 | 容量扫描 | 键数/工作集统计口径有问题 |
| **H3** | `erase` 使 `h > d` → **预测过宽浪费上升、fallback 不升**；`upsert`/`insert` 使 `h < d` → **fallback 与失效上升** | erase/upsert 两条曲线 | 更新语义接线错了（方向应与 baseline1-plan 陷阱 3 一致） |
| **H4** | 热点更新把命中率压得最低（最热的 key 被反复改写）；顺序轮转影响最小 | `--update-distribution roundrobin\|random\|hot` | 热点集合未生效 |
| **H5** | 记账恒等式 §8.4 全部成立 | 逐运行断言 | 实现或口径有 bug（**必须成立**的自检项） |
| **H6** | B1 / B2 / B3 查询结果**逐条完全相同**，recall 逐位相同 | 同查询集结果比对 | 实现有 bug（offset/长度算错） |
| **H7** | mmap 上 B3 的延迟优势远小于 op 收益（`pread` 没有"往返"语义） | 同配置 mmap 对比 | 说明 mmap 也能反映 op 代价，结论可以下移（对应 [baseline2-plan H3](baseline2-plan.md#L207)） |
| **H8** | RDMA 上 B3 的 `Δ延迟/(B2.ops − B3.ops)` 落在 1~3 µs（对照 SHINE §2.3 的 ~2 µs） | RDMA 实测 | 说明发生了流水化，或 CPU/page cache 成了瓶颈 |

### 4.5 一个应该写进报告的设计洞察

**B3 的缓存条目只有 4 字节有效载荷，因此它的容量应按"条数"而不是"字节"设计，并且它几乎不占计算节点内存。** 以 1M 节点、`M=16` 的索引估算：`(node,level)` 键总数约为 `N · (1 + 1/M + 1/M² + …) ≈ 1.07N`；若每条目 24 字节（map + LRU 链表开销），1M 索引约 **26 MB**，只相当于约 820 个 128 维向量的空间。**这是"缓存长度"相对"缓存向量/邻居表"的核心优势，也是回应 SHINE 1% 论断的正确方式。**

---

## 5. 设计决策（DR）

| 编号 | 决策 | 理由 |
| --- | --- | --- |
| DR-1 | 使用已存在的 `ReadPolicy::CachedLength`，不新增枚举值 | 枚举、`to_string`、`read_policy_from_string`、CLI 白名单全部已就绪（§2.1） |
| DR-2 | 类名 `Baseline3CachedLengthStrategy`，`name()` = `"baseline3_cached_length"` | 满足 D1；与 `--read-strategy cached_length` 的映射写进注释 |
| DR-3 | **自建 `LengthCache`，键 = `(level << 32) \| id`；不复用 `ReadContext::length_cache`** | §0 ④ 层隔离；且 context 表会被 `set_read_strategy()` 清空 |
| DR-4 | 缓存以 `std::shared_ptr<LengthCache>` 持有，可由实验程序注入外部实例 | §0 ② 缓存跨 `reload()` 存活；单元测试可用私有实例独立运行 |
| DR-5 | **禁止**用 `generation` 做命中判定；仅在写入时记录、供统计 | [experiment.md §五/§六.5](experiment.md#L63) |
| DR-6 | 状态 A 的形状**与 Baseline 2 完全一致**，且用独立字段 `cold_start_reads` 计数，不计入 `fallbacks` | experiment.md 要求 miss 开销"单独计入"；让 fallback 与 miss 可分别归因 |
| DR-7 | 命中时**一次**读取同时取 degree 字段与 hint 前缀；**不预读、不合并** | D5；否则失去实验含义 |
| DR-8 | 读取失败/短读时**不修改缓存条目**（含 fallback 失败） | D9；避免一次瞬时故障把 hint 污染成"不可用但不报错" |
| DR-9 | 淘汰使用严格 LRU（`std::list` + 迭代器），不用"扫描最小时间戳" | 小容量下淘汰频繁，O(n) 扫描会污染计时 |
| DR-10 | 缺省容量为 0 时取 `ReadContext::cache_capacity`；工厂**不改签名** | 无需触碰 `QueryEngine` 的构造接线 |
| DR-11 | 策略相关的新字段**只增不改**，`merge()` 同步扩展（含按层 vector 的 resize） | 不破坏 B1/B2 的既有断言与脚本 |
| DR-12 | 实验驱动的改造放在 [baseline1_main.cc](../experiments/baseline1_main.cc) 内，**不新建二进制、不改名** | B1/B2 的脚本与报告都引用 `build/baseline1_experiment`，改名会破坏既有复现 |
| DR-13 | B3 使用**独立的 summary CSV 文件** | 表头只在新建时写（[baseline1_main.cc](../experiments/baseline1_main.cc#L549-L556)），混写会错位 |
| DR-14 | 并发度与热点更新列为 **P1**：先交付单线程 + 顺序/随机/热点三种更新分布；多线程作为进阶 6.3 | experiment.md §9 要求两种并发度，但 `QueryEngine` 的 `context_`/`stats_` 非线程安全，需要独立基础设施 |

---

## 6. 任务分解（WBS）

工作量以"半天"为单位估算；总计约 **6~7 个工作日**，对应 [experiment.md](experiment.md#L314-L316) 第 4 周（"实现计算侧邻居长度缓存，处理动态更新和缓存失效"）。这是三个 Baseline 中实现难度最高的一个，主要是缓存语义与生命周期，而不是读取逻辑。

### T1 — 缓存类型与策略骨架（P0，0.5 天）

**改动文件**：`include/rdma_hnsw/read/read_strategy.h`

```cpp
// ---- Baseline 3 (docs/baseline3-plan.md) ----------------------------------
// 缓存键必须含层：level 0 的容量是 2M，level>0 是 M，NodeId 单键会跨层污染。
using LruList = std::list<std::uint64_t>;
struct LengthCacheEntry {
  Generation generation{};      // 仅统计/调试用，禁止参与命中判定（DR-5）
  std::uint32_t degree{};       // 上一次成功读取并校验过的真实 degree（写入时已 clamp）
  LruList::iterator lru_pos{};  // 在 LRU 链表中的位置（队首 = 最近使用）
};
using LengthCache = std::unordered_map<std::uint64_t, LengthCacheEntry>;
using SharedLengthCache = std::shared_ptr<LengthCache>;

inline std::uint64_t length_cache_key(NodeId id, std::uint32_t level) {
  return (static_cast<std::uint64_t>(level) << 32) | id;
}

// Baseline 3：计算侧缓存邻居长度。命中时用缓存宽度发起一次包含 degree 字段的
// 读取（一次往返即可完成）；真实 degree 超过预测宽度时 fallback 第二次读取并
// 合并。缓存正确性由"本次读回的真实 degree"保证，**不使用 generation 预判**。
// 对应 --read-strategy cached_length|cached / ReadPolicy::CachedLength。
class Baseline3CachedLengthStrategy final : public NeighborReadStrategy {
public:
  // capacity == 0 → 使用 ReadContext::cache_capacity；
  // cache 非空 → 由调用方持有，可在 update()->reload() 之后继续复用（DR-4）。
  explicit Baseline3CachedLengthStrategy(std::size_t capacity = 0,
                                         SharedLengthCache cache = {});
  NeighborReadResult read(const RemoteNodeView &v, ReadContext &c) override;
  const char *name() const override { return "baseline3_cached_length"; }
  std::size_t cache_size() const;   // 供实验统计
private:
  std::size_t capacity_{};
  SharedLengthCache cache_;
  void touch(std::uint64_t key);
  bool put(std::uint64_t key, Generation gen, std::uint32_t degree,
           std::size_t limit);      // 写回 + LRU 淘汰；淘汰时 cache_evictions++
};
```

**验收**：头文件独立编译通过；`std::list` 已 include。

### T2 — 三态读取实现（P0，1 天）

**改动文件**：`src/read/read_strategy.cc`

骨架（记账点全部按 §1.2 与 §8.1 展开，务必逐项对齐）：

```cpp
NeighborReadResult Baseline3CachedLengthStrategy::read(const RemoteNodeView &v,
                                                       ReadContext &c) {
  auto *m = c.metrics;
  const std::size_t cap = v.capacity;
  if (cap == 0)
    return {{}, false, "baseline3_cached_length: layer capacity is zero"};

  const std::size_t limit = capacity_ ? capacity_ : c.cache_capacity;
  const std::uint64_t key = length_cache_key(v.id, v.level);
  auto it = cache_->find(key);

  // ===== 状态 A：miss（冷启动），形状与 Baseline 2 一致（DR-6） =====
  if (it == cache_->end()) {
    std::array<byte, sizeof(std::uint32_t)> hdr{};
    const auto hr = c.transport.read({v.layer_offset, 0}, hdr);
    if (!hr.ok || hr.bytes != hdr.size()) {
      if (m) m->failed_reads++;
      return {{}, false, "baseline3_cached_length: header read failed: " +
                             (hr.error.empty() ? std::string("short read") : hr.error)};
    }
    if (m) {
      m->read_ops++; m->header_reads++; m->cache_misses++;
      m->requested_bytes += hdr.size();
      if (m->cache_misses_by_level.size() <= v.level)
        m->cache_misses_by_level.resize(v.level + 1, 0);
      m->cache_misses_by_level[v.level]++;
    }
    std::uint32_t raw{};
    std::memcpy(&raw, hdr.data(), sizeof(raw));
    const auto d = std::min<std::uint32_t>(raw, cap);   // 远程数据不可信

    if (d == 0) {
      if (m) {
        m->zero_degree_layers++; m->useful_bytes += hdr.size();
        m->layer_reads++; m->capacity_sum += cap;
      }
      put(key, v.generation, 0, limit);      // 缓存 0 是合法命中
      return {{}, true, {}};
    }
    const std::size_t n = d * sizeof(NodeId);
    std::vector<byte> b(n);
    const auto nr = c.transport.read({v.layer_offset + sizeof(std::uint32_t), 0}, b);
    if (!nr.ok || nr.bytes != n) {           // 失败不写缓存（DR-8）
      if (m) m->failed_reads++;
      return {{}, false, "baseline3_cached_length: cold-start read failed"};
    }
    if (m) {
      m->read_ops++; m->dependent_reads++; m->cold_start_reads++;
      m->requested_bytes += n; m->useful_bytes += hdr.size() + n;
      m->layer_reads++; m->capacity_sum += cap; m->degree_sum += d;
    }
    put(key, v.generation, d, limit);
    return decode(b, d);
  }

  // ===== 命中：用缓存 hint 发起一次包含 degree 字段的读取（DR-7） =====
  const auto h = std::min<std::uint32_t>(it->second.degree, cap);  // 防御性 clamp
  const std::size_t first = sizeof(std::uint32_t) + h * sizeof(NodeId);
  std::vector<byte> b(first);
  const auto r = c.transport.read({v.layer_offset, 0}, b);
  if (!r.ok || r.bytes != first) {
    if (m) m->failed_reads++;
    return {{}, false, "baseline3_cached_length: hint read failed"};
  }
  touch(key);
  if (m) {
    m->read_ops++; m->hint_reads++; m->cache_hits++;
    m->requested_bytes += first; m->hint_slots_sum += h;
    m->layer_reads++; m->capacity_sum += cap;
    if (m->cache_hits_by_level.size() <= v.level)
      m->cache_hits_by_level.resize(v.level + 1, 0);
    m->cache_hits_by_level[v.level]++;
  }
  std::uint32_t raw{};
  std::memcpy(&raw, b.data(), sizeof(raw));
  const auto d = std::min<std::uint32_t>(raw, cap);

  if (d <= h) {                              // ===== 状态 B：预测足够 =====
    if (m) {
      m->useful_bytes += sizeof(std::uint32_t) + d * sizeof(NodeId);
      m->wasted_bytes += static_cast<std::uint64_t>(h - d) * sizeof(NodeId);
      m->over_read_slots_sum += h - d;
      if (d < h) m->hint_overestimates++;
      m->degree_sum += d; m->cache_updates++;
    }
    it->second.degree = d; it->second.generation = v.generation;   // 回写（不回退 LRU）
    return decode(std::vector<byte>(b.begin() + sizeof(std::uint32_t), b.end()), d);
  }

  // ===== 状态 C：低估（d > h）→ fallback（DR-6 / DR-8） =====
  const std::size_t need = (d - h) * sizeof(NodeId);
  std::vector<byte> rest(need);
  const auto fr = c.transport.read(
      {v.layer_offset + sizeof(std::uint32_t) + h * sizeof(NodeId), 0}, rest);
  if (!fr.ok || fr.bytes != need) {
    if (m) m->failed_reads++;
    return {{}, false, "baseline3_cached_length: fallback read failed"};
  }
  if (m) {
    m->read_ops++; m->dependent_reads++; m->fallbacks++; m->fallback_bytes += need;
    m->cache_invalidations++; m->cache_updates++;
    m->requested_bytes += need;
    m->useful_bytes += sizeof(std::uint32_t) + d * sizeof(NodeId);
    m->degree_sum += d;
  }
  it->second.degree = d; it->second.generation = v.generation;
  // 合并：b[4 .. 4+4h) 是前 h 个邻居，rest 是紧随其后的 (d−h) 个；总长恰为 d
  std::vector<NodeId> out(d);
  if (h)
    std::memcpy(out.data(), b.data() + sizeof(std::uint32_t), h * sizeof(NodeId));
  if (need)
    std::memcpy(out.data() + h, rest.data(), need);
  return {std::move(out), true, {}};
}
```

**验收**：`baseline3_experiment_smoke`（T8）能跑；单层手工用例的字节/op 数与 §1.2 完全一致。

### T3 — 指标扩展与 `merge()`（P0，0.5 天）

**改动文件**：`include/rdma_hnsw/read/read_strategy.h`、`src/read/read_strategy.cc`

在 [ReadMetrics](../include/rdma_hnsw/read/read_strategy.h#L19-L41) 上**只增字段**（DR-11）：

```cpp
// ---- Baseline 3 (docs/baseline3-plan.md T3) ----
std::uint64_t hint_reads{};            // 宽度来自缓存 hint 的读取（状态 B/C 的首读）
std::uint64_t cold_start_reads{};      // miss 路径里真实 degree>0 的第二次读（≠ fallback）
std::uint64_t fallback_bytes{};        // fallback 读取的字节
std::uint64_t cache_evictions{};       // 因容量不足被淘汰的条目
std::uint64_t hint_overestimates{};    // h > d（预测过宽，产生浪费）
std::uint64_t cache_updates{};         // 用真实 degree 回写缓存的次数
std::uint64_t hint_slots_sum{};        // Σ h（命中时），用于算平均 hint
std::uint64_t over_read_slots_sum{};   // Σ (h − d)，用于校验 wasted == 4×该值
std::vector<std::uint64_t> cache_hits_by_level;    // 按层命中率（H1 必需）
std::vector<std::uint64_t> cache_misses_by_level;
```

`cache_hits` / `cache_misses` / `cache_invalidations` / `fallbacks` **直接复用已有字段**（[read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L26-L30)），语义按 [help.md §10](help.md#L313-L336) 定义：`cache_invalidations` = **预测被真实 degree 证伪的次数（`h < d`，必须 fallback）**。

`merge()`（[read_strategy.cc](../src/read/read_strategy.cc#L136-L155)）必须处理 vector：

```cpp
auto grow = [](std::vector<std::uint64_t> &dst, const std::vector<std::uint64_t> &src) {
  if (dst.size() < src.size()) dst.resize(src.size(), 0);
  for (std::size_t i = 0; i < src.size(); ++i) dst[i] += src[i];
};
```

**验收**：§8.4 的 8 条恒等式全部通过。

### T4 — 工厂接线与 CLI 文案（P0，0.25 天）

**改动文件**：`src/read/read_strategy.cc`、`src/common/config.cc`

```cpp
if (p == ReadPolicy::Fixed)        return std::make_unique<Baseline1FixedStrategy>();
if (p == ReadPolicy::HeaderActual) return std::make_unique<Baseline2HeaderFirstStrategy>();
if (p == ReadPolicy::CachedLength) return std::make_unique<Baseline3CachedLengthStrategy>();
throw std::invalid_argument("read strategy is a student extension; ...");
```

并把 [print_usage](../src/common/config.cc#L141-L159) 的文案补齐（白名单已支持，只是文案落伍；`--cache-capacity` / `--prefix-granularity` 目前完全没在 usage 里出现）：

```text
  --read-strategy fixed|baseline1|header_actual|header|cached_length|cached
  --cache-capacity N                    length-cache entries (default 100000)
  --prefix-granularity N                prefix granularity G (default 4)
```

**验收**：`compute_client --read-strategy cached_length` 不再抛异常；`--help` 出现新名字与两个选项。

### T5 — 实验驱动改造（P0，1 天）★ 本计划风险最集中的一步

**改动文件**：`experiments/baseline1_main.cc`

1. **`strategy_label()` 改成 switch**（[L81-L84](../experiments/baseline1_main.cc#L81-L84)）：加 `"baseline3_cached_length"`，否则 B3 行被标成 `baseline1_fixed`，配对错乱。
2. **`Options` 增字段**（[L49-L78](../experiments/baseline1_main.cc#L49-L78)）：`cache_capacity`、`prefix_granularity`、`update_distribution`（可选 `threads`、`cold_start`）。
3. **parse 白名单**（[L198-L204](../experiments/baseline1_main.cc#L198-L204)）：`cached_length|cached` → `ReadPolicy::CachedLength`；新增 `--cache-capacity N`、`--update-distribution roundrobin|random|hot[:K]`。
4. **把容量真正灌进 `cfg`**（[L370-L379](../experiments/baseline1_main.cc#L370-L379) 目前一个都没传）：

```cpp
cfg.cache_capacity     = o.cache_capacity;      // ← 不补这行，容量扫描全无效
cfg.prefix_granularity = o.prefix_granularity;  // ← B4 也要用
```

5. **`inject()` 改成 policy-aware + 外部 shared cache**（[L384-L387](../experiments/baseline1_main.cc#L384-L387)）——**这是让动态实验成立的关键**：

```cpp
// 缓存数据由驱动持有，因此 ComputeNode::update() -> reload() 之后依然存活（§0 ②）。
SharedLengthCache b3_cache = std::make_shared<LengthCache>();
auto inject = [&] {
  if (o.policy == ReadPolicy::CachedLength)
    node.query_engine().set_read_strategy(
        std::make_unique<Baseline3CachedLengthStrategy>(o.cache_capacity, b3_cache));
  else
    node.query_engine().set_read_strategy(rh::make_read_strategy(o.policy));
};
inject();
```

6. **指标搬运**：`diff_metrics`（[L264-L280](../experiments/baseline1_main.cc#L264-L280)）、`Totals`（[L299-L303](../experiments/baseline1_main.cc#L299-L303)）、`add()`（[L305-L319](../experiments/baseline1_main.cc#L305-L319)）加全部新字段（vector 逐元素相减/相加）。
7. **CSV 列**：per-query（[L529-L546](../experiments/baseline1_main.cc#L529-L546)）与 summary（[L549-L579](../experiments/baseline1_main.cc#L549-L579)）加 `hint_reads, cold_start_reads, cache_hits, cache_misses, cache_invalidations, cache_evictions, cache_updates, hit_rate, hint_overestimates, fallbacks, fallback_bytes, hint_slots_sum, over_read_slots_sum, avg_hint, avg_degree`。**B3 写入独立的 summary 文件**（DR-13）。
8. **断言区**（[L608-L615](../experiments/baseline1_main.cc#L608-L615)）：`CachedLength` 分支断言 §8.4 的 8 条恒等式。

**验收**：同一条命令产出的 CSV 里 `cache_hits + cache_misses == layer_reads`、`fallbacks == cache_invalidations`；同一配置两次运行（固定 `--seed`）结果一致；**混合 workload 下 `cache_hits > 0` 且随更新次数不断增长**（证明缓存跨 reload 存活）。

### T6 — 更新分布（P1，0.5 天）

**改动文件**：`experiments/baseline1_main.cc`

当前更新 id 是顺序轮转 `ui % count`（[L466-L483](../experiments/baseline1_main.cc#L466-L483)），既不是随机也不是热点。改为：

```cpp
// 三种分布，全部由 seed 决定 → 三种策略看到完全相同的更新序列（可比性前提）
enum class UpdateDist { RoundRobin, Random, Hot };
// hot：从 [0, count) 里按 seed 取 K = max(1, count/100) 个 "热点 id"，
//      更新在这 K 个 id 之间轮转（模拟热点节点的重复 upsert/erase）
```

`--update-distribution` 与 `--update-hot-size K` 记入 summary CSV，分析脚本据此分组。

**验收**：三种分布下 `updates` 次数相同、id 序列确定；热点分布下 B3 的命中率显著低于轮转分布（H4）。

### T7 — 正确性测试（P0，1 天）

**改动文件**：新增 `tests/read_strategy_cached_test.cc`（或扩展 [read_strategy_test.cc](../tests/read_strategy_test.cc)；建议新增，B3 用例多且需要 `shared_ptr` 场景）

复用 [FakeTransport](../tests/read_strategy_test.cc#L22-L54) 与真值索引手法（[baseline2-plan §T3](baseline2-plan.md#L343-L380)）。用例清单见 §9。

**验收**：`ctest --output-on-failure` 全绿；新文件把 §8.4 的 8 条恒等式写成 `assert`。

### T8 — CMake 注册与 smoke（P0，0.25 天）

**改动文件**：`CMakeLists.txt`

```cmake
if(BUILD_TESTING)
  add_executable(read_strategy_cached_test tests/read_strategy_cached_test.cc)
  target_link_libraries(read_strategy_cached_test PRIVATE rdma_hnsw_core)
  add_test(NAME read_strategy_cached_test COMMAND read_strategy_cached_test)
  # Baseline 3 smoke：同一驱动，cached_length 策略 + 小容量
  add_test(NAME baseline3_experiment_smoke
           COMMAND baseline1_experiment --smoke --read-strategy cached_length
                   --cache-capacity 1000
                   --index ${CMAKE_BINARY_DIR}/baseline3_smoke.bin
                   --out ${CMAKE_BINARY_DIR}/baseline3_smoke.csv
                   --summary-out ${CMAKE_BINARY_DIR}/baseline3_smoke_summary.csv)
endif()
# 并把新测试加入 -UNDEBUG 的 foreach（见 CMakeLists.txt 的既有列表）
```

**验收**：`cmake --build build -j` 无警告；`ctest` 含两个新项且通过。

### T9 — 扫描脚本与数据集（P0，0.5 天）

**新增文件**：`scripts/run_baseline3.sh`

照 [run_baseline2.sh](../scripts/run_baseline2.sh) 改，要点：

- 三策略 `baseline1 header_actual cached_length` × 配置 × `{static, erase, upsert}` × `RATIOS=0,0.1,0.3,0.5` × **`CACHE_CAPACITIES`（见 §10.2）** × `DISTS=roundrobin hot`；
- **每个 mixed run 使用独立索引副本**（[run_baseline2.sh](../scripts/run_baseline2.sh#L90-L96) 已示范 `cp -f idx work` → 跑 → `rm`），保证三策略看到完全相同的更新流；
- 落盘到**独立的** `out/baseline3_summary.csv`（DR-13）；
- 环境变量：`CONFIGS / RATIOS / CACHE_CAPACITIES / DISTS / QUERIES / UPDATES / WARMUP / EFS / K / EFC / SEED / OUT_DIR / RESET_SUMMARY`。

数据集沿用 [convert_sift.py](../scripts/convert_sift.py)（SIFT1M 子集 + 离线 ground truth）。

**验收**：一条命令复现全部 B3 数据；SIFT 10K 子集上三策略位相同 recall。

### T10 — 分析与图表（P0，1 天）

**新增文件**：`scripts/analyze_baseline3.py`（复用 [analyze_baseline2.py](../scripts/analyze_baseline2.py) 的配对框架与 [analyze_baseline1.degree_histogram](../scripts/analyze_baseline1.py)）

必出图表（≥6 张）：

1. **命中率（全局 / 分层）vs 缓存容量**（H2；分层是 H1 的验收）；
2. **命中率、低估率 `q` vs 更新比例**（`erase` 与 `upsert` 两条曲线，H3）；
3. **fallback 次数与 fallback 字节 vs 更新比例**；
4. **hint 与真实 degree 的差异分布**（散点或 CDF；正面回答 [experiment.md §9](experiment.md#L253-L272) 的"hinted degree 与真实 degree 的差异"）；
5. **预测过宽浪费字节**（`over_read_slots_sum`）与 **`erase`/`upsert` 的非对称性**；
6. **延迟 p50/p95/p99 与 `read_ops + node_reads`（总 RDMA READ 操作数）三策略对照**；
7. 恒等式校验，**违反时非零退出**（照 [analyze_baseline2.py](../scripts/analyze_baseline2.py#L436)）。

> 读 summary 时所有新列用 `r.get(key)` 并容忍空串 —— [analyze_baseline2.py 的 read_summary](../scripts/analyze_baseline2.py#L72-L81) 是 `int(float(r[key]))` 硬转，遇到 B1/B2 写的、没有新列的行会崩。

### T11 — 运行验证与报告（P0，0.5 天）

执行 §11 运行手册全流程，产出 [baseline3-report.md](baseline3-report.md)：定义 → 实现要点 → `p/q` 成本模型 → 实测（按层命中率、容量、更新比例、更新分布）→ 与 B1/B2 的定位对照 → 模型偏差解释 → 适用/不适用条件 → **mmap 局限声明**。

---

## 7. 指标口径与公式

### 7.1 单次层读取

```text
A. miss      : requested = 4 + 4d   useful = 4 + 4d   wasted = 0
               read_ops = 1 + [d>0]  header_reads = 1  hint_reads = 0
               dependent_reads = [d>0]  cold_start_reads = [d>0]  fallbacks = 0
B. hit (d≤h) : requested = 4 + 4h   useful = 4 + 4d   wasted = 4(h−d)
               read_ops = 1  header_reads = 0  hint_reads = 1
               dependent_reads = 0  fallbacks = 0  over_read_slots_sum += (h−d)
C. hit (d>h) : requested = 4 + 4d   useful = 4 + 4d   wasted = 0
               read_ops = 2  header_reads = 0  hint_reads = 1
               dependent_reads = 1  fallbacks = 1  cache_invalidations++
               fallback_bytes += 4(d−h)
```

### 7.2 聚合口径

```text
命中率          = cache_hits / (cache_hits + cache_misses) = hint_reads / layer_reads
未命中率        = cache_misses / layer_reads
失效（低估）率 q = cache_invalidations / cache_hits
预测过宽率       = hint_overestimates / cache_hits
缓存修正率       = cache_updates / cache_hits
淘汰率           = cache_evictions / cache_updates
平均 hint / 平均 degree = (hint_slots_sum / hint_reads) vs (degree_sum / layer_reads)
预测过宽浪费     = 4 × over_read_slots_sum           （等价于 wasted_bytes，可交叉校验）
fallback 字节占比 = fallback_bytes / requested_bytes
按层命中率       = cache_hits_by_level[l] / (cache_hits_by_level[l] + cache_misses_by_level[l])
总 RDMA READ 操作数 = read_ops + node_reads          ← 必须报，邻居区只占 2~8%
```

### 7.3 与 Baseline 1 / 2 的对照量

```text
op 收益(vs B2)     = (B2.read_ops − B3.read_ops) / B2.read_ops      ≈ (p − q) / 2
字节收益(vs B1)    = (B1.requested − B3.requested) / B1.requested
字节收益(vs B2)    = (B2.requested − B3.requested) / B2.requested   ≈ −(p−q)(h̄−d)/d
总流量口径收益     = saved / (node_bytes + requested)               ← 必须同时报（见 baseline1-plan §6.3）
rtt_per_saved_op   = (B2.mean − B3.mean) / (B2.read_ops − B3.read_ops)   ← 仅 RDMA 有物理意义
```

### 7.4 恒等式（写进测试与驱动断言）

```text
① requested_bytes == useful_bytes + wasted_bytes
② read_ops == hint_reads + header_reads + dependent_reads
③ cache_hits == hint_reads
④ cache_hits + cache_misses == layer_reads
⑤ cache_misses == header_reads
⑥ fallbacks == cache_invalidations
⑦ dependent_reads == fallbacks + cold_start_reads
⑧ wasted_bytes == sizeof(NodeId) * over_read_slots_sum
```

> 恒等式 ⑦ 的存在理由：miss 路径的第二次读**不是** fallback（DR-6），必须能被区分开，否则"fallback 次数"会虚增、与 B2 的 `dependent_reads` 也不可比。
> 恒等式 ⑤ 能把"offset 算错"和"miss/hit 判定错"当场暴露；④ 能把"某状态漏记 layer_reads"暴露。

### 7.5 必须同时报告的 recall

B1/B2/B3 的邻居集合应**逐位相同**，因此 recall 必须逐位相同（H6）。若不同，先修 bug 再谈性能 —— 这是 [experiment.md](experiment.md#L165-L169) "在相同 recall 下比较"最省事的形态。

---

## 8. 测试方案

原则：**等价性优先**（B3 ≡ B1 强于逐字段核对）；**单层先测**（手工构造 `RemoteNodeView` + 真索引文件，不启动 `ComputeNode`）；**畸形输入与生命周期必测**；**把 §7.4 的八条恒等式当断言**，让口径错误在 CI 里暴露。

| # | 用例 | 构造方式 | 期望 |
| --- | --- | --- | --- |
| 1 | **与 B1 逐邻居等价（核心）** | 真索引，遍历全部节点 × 层 | `r1.neighbors == r3.neighbors` |
| 2 | **同一策略冷/热两次读结果一致** | 先读一次（miss），再读一次（hit） | 两次 `neighbors` 相同 |
| 3 | 状态 A（miss，`d>0`） | `M=4, d=2` | `ops==2`、`header_reads==1`、`hint_reads==0`、`cache_misses==1`、`cold_start_reads==1`、`fallbacks==0`、`wasted==0`、`requested==4+8` |
| 4 | 状态 B（hit，`d<h`） | 先读一次 `d=2`，再手工把缓存改成 5 后重读 | `ops==1`、`hint_reads==1`、`header_reads==0`、`wasted==4(5−2)`、`over_read_slots_sum==3`、`hint_overestimates==1` |
| 5 | 状态 B（hit，`d==h`） | 连续两次读同一层 | 第二次 `ops==1`、`wasted==0`、`hint_overestimates==0` |
| 6 | 状态 C（hit，`d>h`） | 先读一次 `d=1` 建缓存，再用真索引换成 `d=4` 的层重读 | 结果与 B1 一致、`ops==2`、`dependent_reads==1`、`fallbacks==1`、`cache_invalidations==1`、`fallback_bytes==4(4−1)`、`requested==4+16` |
| 7 | 缓存 hint 为 0 而 `d>0` | 缓存 0 后读到手写 degree=3 | 走状态 C，`ops==2`，不越界 |
| 8 | **层隔离** | 同一 node 的 level 0（cap=2M）与 level 1（cap=M）分别读 | 两个 key 独立；level 1 不会被 level 0 的 hint 越界（读取时 clamp 生效） |
| 9 | `degree > capacity`（畸形） | `FakeTransport` + `fake_degree=100`、`cap=8` | 截断到 8；状态 C 第二次读长度 `(8−h)·4`；无越界 |
| 10 | `degree == 0` | 状态 A 与状态 B 各一次 | A：`ops==1`、`zero_degree_layers==1`、返回 ok+空集；B（缓存 0）：`ops==1`、`hint_reads==1`、`wasted==0` |
| 11 | `capacity == 0` | `RemoteNodeView{capacity=0}` | 返回错误、`read_ops==0`、`t.calls==0`、**不写缓存** |
| 12 | **失败不污染缓存** | `fail_on_call=2`（状态 A 的第二次读失败） | 返回错误、`failed_reads==1`；随后用同一缓存再读该层 → 仍是 **miss**（说明没有写入脏值） |
| 13 | fallback 失败 | 状态 C 的第二次读 `truncate` | 返回错误、不部分返回、不伪造邻居；条目保持旧值（再读仍走状态 C 或 miss） |
| 14 | header 短读/失败 | `truncate=2` / `fail=true` | 返回错误、`t.calls==1`、`read_ops==0` |
| 15 | **淘汰（LRU）** | `cache_capacity=2`，顺序读 key A、B、C | `cache_evictions>=1`；再读 A → miss；再读 C → hit（LRU 顺序可断言） |
| 16 | **generation 不得用于判定** | ①`generation` 变、`degree` 不变 ②`degree` 变、`generation` 不变 | ①仍命中（`read_ops==1`，无 fallback）②必须 fallback。**这两条直接编码 [experiment.md](experiment.md#L63) 的约束** |
| 17 | **缓存跨注入存活** | 同一个 `shared_ptr<LengthCache>` 构造两个策略实例 | 第二个实例第一次读即 `cache_hits==1`（对应 T5 的 reload 场景） |
| 18 | 多层节点偏移 | `max_level=2` | level 0（2M）与 level>0（M）的 `layer_offset` 与读取长度都正确 |

---

## 9. 实验设计

### 9.1 固定主配置（与 B1/B2 完全相同，才能横向比较）

```text
数据集 : SIFT1M（10K / 100K / 1M 子集），128 维，L2
HNSW   : M = 16, ef-construction = 200
查询   : ef-search = 64, k = 10, 固定查询集 + 离线 ground truth
对比   : Baseline1FixedStrategy / Baseline2HeaderFirstStrategy / Baseline3CachedLengthStrategy
传输   : mmap（字节 / 操作数 / 正确性）→ RDMA（延迟与操作数结论）
```

### 9.2 矩阵

| 因子 | 取值 | 目的 |
| --- | --- | --- |
| 策略 | B1 / B2 / B3 | 主对照 |
| 索引规模 | 10K / 100K / 1M | 工作集大小 vs 缓存容量（H2） |
| `M` | 8 / 16 / 32 | 容量与 degree 分布对 hint 的影响 |
| 维度 | 32 / 128 | 邻居区占比（≈`1/(dim+6)`）对"字节收益"的稀释 |
| 更新操作 | 静态 / erase / upsert | H3 的非对称性 |
| 更新比例 | 0% / 10% / 30% / 50% | [§五.4](experiment.md#L108-L111) 的动态要求 |
| **缓存容量（条数）** | 100 / 1000 / 10000 / 100000（10K 索引键数≈1.07万，见 §0 ⑤） | [§六.3](experiment.md#L162) |
| **更新分布** | roundrobin / random / hot(1% 热点) | [§九](experiment.md#L259) 要求"随机 + 热点"各一种 |
| 并发度 | 1（必做）/ 4（P1，见 DR-14） | [§九](experiment.md#L258) |

### 9.3 每组必须记录的字段（在 B1/B2 字段基础上追加）

```text
strategy, dataset, dim, node_count, M, ef_construction, ef_search, k,
transport, update_op, update_ratio, update_distribution, cache_capacity,
queries, updates,
qps, qps_end_to_end, latency_mean_us, latency_p50_us, latency_p95_us, latency_p99_us,
neighbor_requested_bytes, neighbor_useful_bytes, neighbor_wasted_bytes,
node_bytes, total_requested_bytes, neighbor_share,
read_ops, node_reads, total_read_ops,
hint_reads, header_reads, dependent_reads, cold_start_reads,
cache_hits, cache_misses, cache_invalidations, cache_evictions, cache_updates,
hit_rate, miss_rate, underestimate_rate_q, overestimate_rate,
fallbacks, fallback_bytes, hint_slots_sum, over_read_slots_sum,
avg_hint, avg_degree, capacity_utilization,
op_gain_vs_b2, byte_gain_vs_b1, byte_gain_vs_b2,
rtt_per_saved_op_us,          # 仅 RDMA
recall
```

### 9.4 三个必须有结论的对照

**(a) `p − q` 模型 vs 实测。** 用实测的 `hit_rate` 与 `underestimate_rate_q` 预测 `op_gain_vs_b2 ≈ (p−q)/2`，与实测对比；偏差超过 2 倍要解释（`d==0` 层的权重、miss 的 `[d>0]` 项、fallback 的分布不均）。

**(b) 缓存容量的拐点在哪。** 扫 `cache_capacity`，找出命中率饱和点，并换算成"键数 / 工作集比例"；如果 1M 规模在 100000 条时仍未饱和，说明容量需求随 `N` 线性增长，要给出外推。

**(c) `erase` 与 `upsert` 的非对称性。** 分别给出"预测过宽浪费字节"与"fallback 次数"随更新比例的两条曲线，验证 H3，并给出**哪种更新对一个"只缓存长度"的机制更致命**的结论。

---

## 10. 构建与运行

### 10.1 WSL（只能验字节 / 操作数 / 正确性）

```bash
cmake -S . -B build -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure     # 含 read_strategy_cached_test、baseline3_experiment_smoke

mkdir -p data out
./build/hnsw_build --generate 10000 --dim 128 --M 16 \
  --ef-construction 200 --seed 42 --output data/index_n10000_d128_M16.bin

# CLI 路径（cache_capacity 走 --cache-capacity；注意 mmap 下每次更新缓存会清零）
./build/compute_client --index data/index_n10000_d128_M16.bin --transport mmap \
  --read-strategy cached_length --cache-capacity 1000 \
  --queries-count 1000 --ef-search 64 --k 10 --seed 42

# 实验驱动（推荐路径：外部 shared cache 跨 reload 存活）
./build/baseline1_experiment --index data/index_n10000_d128_M16.bin --transport mmap \
  --read-strategy cached_length --cache-capacity 1000 \
  --queries-count 1000 --ef-search 64 --k 10 --warmup 100 --seed 42 \
  --out out/b3_dim128_static.csv --summary-out out/baseline3_summary.csv
```

### 10.2 缓存容量的取法（重要）

10K 索引的全部 `(node,level)` 键约 **1.07 万**（`level 0` 10 000 + `level 1` 646 + `level 2` 39 + …，见 [baseline1-report.md](baseline1-report.md#L190-L197)）。因此：

```text
10K  索引 → cache_capacity ∈ {100, 1000, 10000, 100000}
100K 索引 → cache_capacity ∈ {1000, 10000, 100000, 1000000}
1M   索引 → cache_capacity ∈ {10000, 100000, 1000000, 10000000}
```

**绝不能用默认 100000 直接跑 10K/100K 的容量扫描**：那样永远不淘汰，三条曲线会完全重合（§0 ⑤）。

### 10.3 RDMA（延迟结论的唯一来源）

```bash
# 存储实例
./build/storage_server --index data/index_n10000_d128_M16.bin --transport rdma \
  --rdma-connection cm --bind <存储IPv4> --port 18515 --ib-device erdma_0 --ib-port 1

# 计算实例：三策略各跑一遍，参数完全一致
./build/compute_client --transport rdma --rdma-connection cm \
  --host <存储IPv4> --port 18515 --ib-device erdma_0 --ib-port 1 \
  --read-strategy cached_length --cache-capacity 10000 \
  --queries-count 1000 --ef-search 64 --k 10
```

注意：

- `TransportStats` 的 READ/WRITE 计数**不含**控制面 SEND/RECV（[erdma.md](erdma.md#L162)），跨策略比较统一用 `ReadMetrics`；
- RDMA 模式下 upsert/erase **不会** `reload()`（[compute_node.cc](../src/runtime/compute_node.cc#L115-L122)），因此驱动的 shared cache 会自然延续 —— 但要确认 `refresh_remote_catalog` 不会让 `ReadContext` 失效；
- 单次读 > 1 MiB 会拆成多个 WR；B3 的读都很小，各占 1 个 WR；
- `rtt_per_saved_op` 应与 SHINE §2.3 的 ~2 µs 对照（H8）。

### 10.4 扫描与对比

```bash
# 全量扫描（三策略 × 配置 × workload × 容量 × 更新分布）
./scripts/run_baseline3.sh

# 分析与绘图
source .venv/bin/activate
python scripts/analyze_baseline3.py \
  --summary out/baseline3_summary.csv \
  --out-dir out/figures_baseline3 \
  --report out/baseline3_report.md \
  --index data/index_n10000_d128_M16.bin \
  --index data/index_n100000_d128_M16.bin
```

> **扫描耗时提醒**：沿用 [baseline2-report.md](baseline2-report.md#L232) 的实测——mmap 混合 workload 的耗时几乎全在更新上（每次 `apply` 会重写整个索引文件，随后 `reload()` 重新解析；100k 索引约 0.46 s/次更新）。因此 B3 的扫描矩阵**先把容量与更新分布限定在小规模（10K）上做完**，再放到 100K/1M。

---

## 11. 排期

| 天 | 任务 | 产出 |
| --- | --- | --- |
| D1 上午 | §3 论文阅读（**2.pdf §5 整节** + §3.1/3.2/3.4 + §8.4） | 阅读笔记：What-to-Cache 三方比较、`p/q` 模型、`R≈2µs` 锚点 |
| D1 下午 | T1 + T2 | 缓存类型 + 三态读取可编译、单层手工用例通过 |
| D2 上午 | T3 + T4 | 指标扩展 + 工厂接线；`--read-strategy cached_length` 可用 |
| D2 下午 | T5（前 4 项） | 驱动接线：白名单、`--cache-capacity` 灌入、shared cache、label |
| D3 上午 | T5（后 4 项） | CSV 列 + 恒等式断言；smoke 跑通 |
| D3 下午 | T7 + T8 | 全部 18 个用例 + CMake；`ctest` 全绿 |
| D4 上午 | T6 | `--update-distribution roundrobin\|random\|hot` |
| D4 下午 | T9 + 10K 规模小批采集 | `run_baseline3.sh` 可用；首批 CSV（含容量扫描） |
| D5 上午 | T10 第一批 | 命中率（分层/容量）、`q` vs 更新比例、fallback 曲线 |
| D5 下午 | T10 第二批 + 100K 采集 | hint vs degree 差异、预测过宽浪费、三策略延迟对照 |
| D6 上午 | RDMA 验证（§10.3） | `rtt_per_saved_op` 与 H8 对照 |
| D6 下午 | T11 | [baseline3-report.md](baseline3-report.md) 初稿 + 复现手册 |

---

## 12. 风险登记

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| **mmap 下 `reload()` 清零缓存** | 动态命中率与失效数据完全失真 | T5 的 shared `LengthCache`；报告中说明这是 mmap 后端的固有假象（RDMA 不 reload） |
| 用 generation 预判命中 | 违反 [§六.5](experiment.md#L180)，机制含义退化 | 代码评审重点核查该文件；§8 用例 16 编码该约束 |
| 复用 `ReadContext::length_cache` 单键 | 跨层污染，浪费虚高、偶发 fallback | DR-3 自建 `(id,level)` 键缓存；用例 8 |
| **驱动没把 `cache_capacity` 灌进 `cfg`** | 容量扫描完全是直线，白跑一天 | T5 第 4 项写成显式清单项；smoke 里带 `--cache-capacity 1000` |
| `strategy_label()` 错标 B3 | 分析脚本配对错乱、数据不可用 | T5 第 1 项；比较脚本时校验 `strategy` 取值 |
| 默认容量 100000 在 10K/100K 下不淘汰 | 容量敏感性实验无效应 | §10.2 明确按规模给容量 |
| 新列混写进 B1/B2 的 summary | CSV 列错位 / 分析脚本崩溃 | DR-13 独立 summary 文件；分析脚本用 `r.get()` |
| 失败/短读时误写缓存 | 一次瞬时故障把 hint 永久污染 | DR-8；用例 12/13 |
| 把 miss 的第二次读算成 fallback | `fallbacks` 虚增，与 B2 不可比 | DR-6 独立 `cold_start_reads` + 恒等式 ⑦ |
| 在 mmap 上得出"往返代价"结论 | 结论错误 | 沿用 [baseline2-plan §0③](baseline2-plan.md#L17) 的分工：mmap 只报字节/op/正确性（H7），延迟结论必须 RDMA |
| 只报 `saving_pct_layer` | 把 0.3% 说成 23% | 必须同时报 `total_requested_bytes` 口径（§7.3） |
| 并发度要求（§9）无法按时交付 | 缺一组实验 | DR-14：列为 P1，报告中说明"单线程口径 + 每线程独立 `ComputeNode`"的替代方案 |
| 用 `TransportStats::read_ops` 做跨策略对比 | 口径不一致（缺 SEND/RECV、大读拆分） | 统一用 `ReadMetrics::read_ops` / `node_reads` |
| 误改 `QueryEngine::search()` | 违反 D3，比较失去意义 | 评审时核查 [query_engine.cc](../src/runtime/query_engine.cc#L69-L126) 的 diff |
| 结论"命中率很低"被当成失败 | 报告缺亮点 | 命中率低本身就是有价值的动机结论：配合 §4.3 的定位与 SHINE 的 "numerous unique graph nodes"，可解释为**该机制只对上层/热点有效**，并自然引出分级前缀（B4）与批量预取（进阶 6.4） |

---

## 13. 附录

### 13.1 关键代码位置速查

| 用途 | 位置 |
| --- | --- |
| B3 要实现的接口 | [read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L67-L72) |
| 预留给 B3 的 5 个字段 | [read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L26-L30) |
| `ReadContext`（transport / metrics / 容量） | [read_strategy.h](../include/rdma_hnsw/read/read_strategy.h#L57-L65) |
| `merge()`（要加字段与 vector 处理） | [read_strategy.cc](../src/read/read_strategy.cc#L136-L155) |
| 工厂（要加分支） | [read_strategy.cc](../src/read/read_strategy.cc#L157-L165) |
| Baseline 2 实现（状态 A 的形状来源） | [read_strategy.cc](../src/read/read_strategy.cc#L69-L134) |
| `decode()` 可复用 | [read_strategy.cc](../src/read/read_strategy.cc#L13-L20) |
| 层偏移（指向 degree 字段）/ 层容量 | [graph_store.h](../include/rdma_hnsw/graph/graph_store.h#L75-L83)、[graph_store.h](../include/rdma_hnsw/graph/graph_store.h#L46-L48) |
| CLI 白名单（**不用改**） | [config.cc](../src/common/config.cc#L68-L76) |
| CLI 名称映射（**不用改**） | [types.h](../include/rdma_hnsw/common/types.h#L39-L49) |
| usage 文案（要改） | [config.cc](../src/common/config.cc#L141-L159) |
| 实验驱动：`strategy_label` / `cfg` / `inject` | [baseline1_main.cc](../experiments/baseline1_main.cc#L81-L84)、[#L370-L379](../experiments/baseline1_main.cc#L370-L379)、[#L384-L387](../experiments/baseline1_main.cc#L384-L387) |
| 实验驱动：更新 id 选择（要加分布） | [baseline1_main.cc](../experiments/baseline1_main.cc#L466-L483) |
| 实验驱动：CSV 与断言 | [baseline1_main.cc](../experiments/baseline1_main.cc#L529-L546)、[#L549-L579](../experiments/baseline1_main.cc#L549-L579)、[#L608-L615](../experiments/baseline1_main.cc#L608-L615) |
| 缓存被清空的两处根因 | [compute_node.cc](../src/runtime/compute_node.cc#L61-L68)、[query_engine.cc](../src/runtime/query_engine.cc#L34-L40) |
| 更新对 degree 的影响 | [storage_engine.cc](../src/runtime/storage_engine.cc#L21-L73) |
| 查询主流程（**禁止修改**） | [query_engine.cc](../src/runtime/query_engine.cc#L69-L126) |
| 测试骨架与 `FakeTransport` | [read_strategy_test.cc](../tests/read_strategy_test.cc#L22-L54) |
| 测试注册与 smoke | [CMakeLists.txt](../CMakeLists.txt#L111-L150) |

### 13.2 命令速查

```bash
# 构建与测试
cmake -S . -B build -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure

# 索引
./build/hnsw_build --generate 10000 --dim 128 --M 16 --ef-construction 200 \
  --seed 42 --output data/index_n10000_d128_M16.bin

# Baseline 3（CLI）
./build/compute_client --index data/index_n10000_d128_M16.bin --transport mmap \
  --read-strategy cached_length --cache-capacity 1000 \
  --queries-count 1000 --ef-search 64 --k 10 --seed 42

# Baseline 3（实验驱动 + 容量扫描）
./build/baseline1_experiment --index data/index_n10000_d128_M16.bin --transport mmap \
  --read-strategy cached_length --cache-capacity 1000 \
  --queries-count 1000 --ef-search 64 --k 10 --warmup 100 --seed 42 \
  --out out/b3.csv --summary-out out/baseline3_summary.csv

# 全量扫描与分析
./scripts/run_baseline3.sh
python scripts/analyze_baseline3.py --summary out/baseline3_summary.csv \
  --out-dir out/figures_baseline3 --report out/baseline3_report.md
```

### 13.3 与后续分级前缀机制（进阶/第 5 周）的衔接

B3 的所有基础设施都被分级前缀机制**直接复用**，因此本计划的产出应按"可被继承"的方式组织：

- `LengthCache` / `LengthCacheEntry` / `length_cache_key()` 直接复用，只需把存的 `degree` 换成"缓存的级数 `l`"（前缀机制缓存 `ceil(degree/G)`）；
- `hint_slots_sum` / `over_read_slots_sum` / `hint_overestimates` 正是前缀机制"向上取整浪费"的直接口径；
- `fallbacks` / `fallback_bytes` / `cache_invalidations` 对应前缀机制的"hint 失效/低估 fallback"；
- `--prefix-granularity` 与 `ReadContext::prefix_granularity` 已经全链路就绪（§2.1），B4 只需读它；
- 驱动的 shared-cache 注入、`--update-distribution`、按层命中率、恒等式断言框架全部可原样使用。

### 13.4 完成检查清单

> 状态：**已实施**，逐项对照见 [baseline3-report.md §2](baseline3-report.md)。

- [ ] `Baseline3CachedLengthStrategy` 具名、有 `name()` 与注释（D1）
- [ ] `LengthCache` 按 `(node, level)` 键；未复用 `ReadContext::length_cache`（DR-3）
- [ ] 缓存以 `shared_ptr` 持有，可由实验程序注入并在 `reload()` 后存活（DR-4 / D9）
- [ ] `--read-strategy cached_length|cached` 可用；usage 文案已更新（D2）
- [ ] `QueryEngine::search()` 未被修改；策略内无 ibverbs（D3）
- [ ] `capacity==0` / header 短读 / fallback 短读 / `degree>capacity` / `degree==0` / 条目 degree>cap 全覆盖（D4）
- [ ] 命中时确实只要一次 READ，无预读、无合并（D5）
- [ ] 与 B1/B2 逐邻居一致；冷/热缓存结果一致（D6）
- [ ] **未使用 generation 预判命中**，用例 16 通过（D10）
- [ ] 失败/短读不污染缓存，用例 12/13 通过（D9）
- [ ] `hint_reads / cold_start_reads / fallback_bytes / cache_evictions / hint_overestimates / cache_updates / hint_slots_sum / over_read_slots_sum / 按层命中率` 已激活（D7）
- [ ] §7.4 八条恒等式写成断言并通过（D7）
- [ ] 驱动：`strategy_label` 已改 switch、`cache_capacity` 已灌入 `cfg`、shared cache 注入（T5）
- [ ] 驱动：`--update-distribution roundrobin|random|hot` 可用（D8）
- [ ] 独立 summary CSV；分析脚本用 `r.get()` 容错（DR-13）
- [ ] `read_strategy_cached_test` 18 个用例 + `baseline3_experiment_smoke` 通过（T7/T8）
- [ ] 容量扫描覆盖 §10.2 的取值，且**确实发生过淘汰**（`cache_evictions > 0`）（D8）
- [ ] erase / upsert × ratio × 容量 × 更新分布 的数据与图表齐备（D8）
- [ ] RDMA 上测出 `rtt_per_saved_op`，与 `R ≈ 2 µs` 对照（H8）
- [ ] 报告同时给出全局与**按层**命中率、`p − q` 模型与实测的对照、以及 mmap 局限声明
- [ ] 构建命令、运行参数、原始 CSV、统计脚本齐备（D11）
