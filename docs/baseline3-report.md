# Baseline 3 交付报告：计算侧缓存邻居长度

本文是 [baseline3-plan.md](baseline3-plan.md) 的实施结果记录，覆盖**交付概览、完成判据对照、代码改动、指标口径、实测数据、`p − q` 模型校验、假设检验、动态更新、局限声明与复现手册**。结构与 [baseline1-report.md](baseline1-report.md)、[baseline2-report.md](baseline2-report.md) 对齐。

> **环境说明（重要）**：本次全部在 **WSL2 + mmap** 下完成（无 `/dev/infiniband`，无 IB LID）。按 plan §10.3 与 baseline2-plan §0③ 的分工，**mmap 只能给出字节 / 操作数 / 正确性结论，不能给出往返延迟结论**。下文的 p50/p95/p99 只用于确认测量链路通畅，**不构成 Baseline 3 的性能结论**；`rtt_per_saved_op_us` 的 mmap 值也不等于 RDMA 的往返代价 `R`。
>
> **数据采集条件**：合成数据（`hnsw_build --generate`），`n=10000`、`dim=128`、`M=16`、`ef-construction=200`；查询 300 条、`ef-search=64`、`k=10`、`warmup=50`、`seed=42`；更新 300 条、`{erase, upsert} × ratio{0.1,0.3,0.5} × {roundrobin, hot}`；缓存容量 `{100, 1000, 10000, 100000}`；另有一组 `hot` 热点规模 `K=10` 的补充实验（§7.2）。未提供 base/ground-truth，故 sweep 中 `recall=0`；正确性由 18 个单元用例与 8 条恒等式保证（§4、§9）。

---

## 1. 交付概览

| 产物 | 路径 | 说明 |
| --- | --- | --- |
| 具名策略 | `include/rdma_hnsw/read/read_strategy.h`、`src/read/read_strategy.cc` | `Baseline3CachedLengthStrategy`，`name() == "baseline3_cached_length"` |
| 缓存类型 | 同上 | `LengthCache{entries, lru}` + `length_cache_key(id, level)`，`shared_ptr` 持有 |
| 指标扩展 | `include/rdma_hnsw/read/read_strategy.h` | 9 个标量 + 2 个按层 vector，`merge()` 同步扩展 |
| 工厂接线 | `src/read/read_strategy.cc` | `make_read_strategy(CachedLength)` 返回 Baseline 3 |
| CLI 文案 | `src/common/config.cc` | usage 列出 `cached_length|cached`、`--cache-capacity`、`--prefix-granularity` |
| 正确性测试 | `tests/read_strategy_cached_test.cc` | plan §8 的 18 个用例 |
| CMake | `CMakeLists.txt` | 新增 `read_strategy_cached_test`、`baseline3_experiment_smoke`（共 9 个测试） |
| 实验驱动 | `experiments/baseline1_main.cc` | `cached_length` 白名单、`--cache-capacity` 灌入 `cfg`、外部 shared cache、`--update-distribution`/`--update-hot-size`、B3 列与恒等式断言 |
| 扫描脚本 | `scripts/run_baseline3.sh` | 3 策略 × 配置 × workload × 容量 × 分布；索引副本放 `tmp/` |
| 分析脚本 | `scripts/analyze_baseline3.py` | 6 张图 + 恒等式门禁（违反时非零退出） |
| 原始数据 | `out/baseline3_summary.csv`（84 行）、`out/baseline3_<label>_<策略>[_c<cap>].csv` | |
| 图表 | `out/figures_baseline3/` | 7 张 PNG |
| 本报告 | `docs/baseline3-report.md` | |

**未修改** `QueryEngine::search()`；策略内无任何 ibverbs 调用（满足 D3）。

---

## 2. 完成判据对照（D1–D11）

| 编号 | 判据 | 状态 | 证据 |
| --- | --- | --- | --- |
| D1 | 具名策略类 + `name()` + 注释 | ✅ | `Baseline3CachedLengthStrategy` → `"baseline3_cached_length"` |
| D2 | 可独立选择 | ✅ | `--read-strategy cached_length\|cached`（`compute_client` 与驱动均已接线）；`ReadPolicy::CachedLength` |
| D3 | 不改搜索主流程；策略内无 ibverbs | ✅ | 仅经 `RemoteTransport::read` |
| D4 | 边界齐全 | ✅ | `capacity==0`、header 短读/失败、冷启动读失败、fallback 短读、`degree>capacity`、`degree==0`、条目 degree>cap（读取时二次 clamp） |
| D5 | 命中时只要一次 READ | ✅ | 用例 2/4/5/18 断言 `t.calls` 与 `read_ops`；无预读、无合并 |
| D6 | 与 B1/B2 逐邻居一致；冷/热一致 | ✅ | 用例 1（全部节点×层）、用例 2；sweep 中策略无关列逐位相同 |
| D7 | 指标可导出 | ✅ | 见 §3.2；8 条恒等式在驱动与分析脚本中双处校验 |
| D8 | 动态场景：`erase`/`upsert` × 比例 × **容量** × **分布** | ✅ | §7；含 `hot K=10` 放大实验 |
| D9 | 失败不污染缓存；缓存跨 `update()/reload()` 存活 | ✅ | 用例 12/13/17；驱动用外部 `shared_ptr<LengthCache>` |
| D10 | 未用 generation 预判命中 | ✅ | 用例 16（generation 变、degree 不变仍命中；degree 变、generation 不变必 fallback） |
| D11 | 源码/命令/参数/数据/脚本可复现 | ✅ | §10 |

---

## 3. 代码改动

### 3.1 缓存类型与键

```cpp
using LruList = std::list<std::uint64_t>;
struct LengthCacheEntry {
  Generation generation{};      // 仅统计，禁止参与命中判定（DR-5）
  std::uint32_t degree{};       // 上一次校验过的真实 degree（写入时已 clamp）
  LruList::iterator lru_pos{};
};
struct LengthCache {            // map 与 LRU 链表放在同一个共享对象里：
  std::unordered_map<std::uint64_t, LengthCacheEntry> entries;
  LruList lru;                  //   entry 持有 list 迭代器，二者必须同寿命
};
inline std::uint64_t length_cache_key(NodeId id, std::uint32_t level) {
  return (static_cast<std::uint64_t>(level) << 32) | id;
}
```

键含 `level` 是硬性要求：`layer_capacity(0) = 2M`、`layer_capacity(l>0) = M`，单键必然跨层污染（plan §0④）。缓存以 `std::shared_ptr<LengthCache>` 持有，由实验程序注入，因此 `ComputeNode::update() → reload()` 重建 `QueryEngine`（并清空 `ReadContext::length_cache`）之后仍存活（plan §0②、用例 17）。

### 3.2 三态读取

| 状态 | 触发 | READ | `read_ops` |
| --- | --- | --- | --- |
| A miss | 无该 key | ① 4B degree ② `4d` 邻居 | `1 + [d>0]` |
| B hit `d ≤ h` | 预测足够 | **仅一次** `4 + 4h` | 1 |
| C hit `d > h` | 低估 | ① `4+4h` ② `4(d−h)` | 2 |

新增字段：`hint_reads`、`cold_start_reads`（miss 的第二次读，**不是** fallback）、`fallback_bytes`、`cache_evictions`、`hint_overestimates`、`cache_updates`、`hint_slots_sum`、`over_read_slots_sum`、`cache_hits_by_level[]`、`cache_misses_by_level[]`。`cache_hits/misses/invalidations/fallbacks` 直接复用既有字段。

`put()` 用严格 LRU（`splice` 到队首 + 从队尾淘汰）并返回淘汰条数，由调用方累加进 `cache_evictions`；`limit == 0` 时禁用缓存（不插入、不淘汰）。

### 3.3 实验驱动

- `strategy_label()` 改为 `switch`，新增 `baseline3_cached_length`（否则 B3 行会被标成 B1，配对错乱）。
- `cfg.cache_capacity` / `cfg.prefix_granularity` **真正灌入**（此前一个都没传，容量扫描会是一条直线）。
- `inject()` 对 B3 注入 `Baseline3CachedLengthStrategy(o.cache_capacity, length_cache)`，`length_cache` 由驱动持有。
- `--update-distribution roundrobin|random|hot` + `--update-hot-size K`：三种分布全部由 `--seed` 决定，三种策略看到完全相同的更新序列。
- summary CSV 追加 24 列（含紧凑的按层命中 `0:12;1:3`），per-query CSV 追加 10 列；B3 使用独立 summary 文件（DR-13）。

---

## 4. 指标口径与恒等式

单层账本（`requested == useful + wasted` 恒成立）：

```text
A. miss      : requested = 4 + 4d    useful = 4 + 4d    wasted = 0
B. hit d≤h   : requested = 4 + 4h    useful = 4 + 4d    wasted = 4(h−d)
C. hit d>h   : requested = 4 + 4d    useful = 4 + 4d    wasted = 0（fallback 补读）
```

八条恒等式（plan §7.4）在驱动里逐运行断言，在 `analyze_baseline3.py` 里逐行校验，在 `tests/read_strategy_cached_test.cc` 的 `check_baseline3()` 里逐用例断言：

```text
① requested == useful + wasted
② read_ops  == hint_reads + header_reads + dependent_reads
③ cache_hits == hint_reads
④ cache_hits + cache_misses == layer_reads
⑤ cache_misses == header_reads
⑥ fallbacks == cache_invalidations
⑦ dependent_reads == fallbacks + cold_start_reads
⑧ wasted_bytes == 4 * over_read_slots_sum
```

> ①、④、⑦ 以 `failed_reads == 0` 为前提：失败的读会计入 `requested`/`cache_misses`，但不计入 `useful`/`layer_reads`。驱动在 B3 分支先断言 `failed_reads == 0`，再用 ②–⑧ 兜底。**本次 84 次运行全部通过**（`all Baseline 3 identities hold`，分析脚本退出码 0）。

`total_read_ops = read_ops + node_reads`。summary 中旧行没有 `node_reads` 列，分析脚本按 `node_bytes / (24 + 4·dim)` 精确还原（每次节点读固定 `sizeof(NodeDiskHeader)=24` 字节头 + 向量），新行直接读列。

---

## 5. 静态工作量：三策略对照

10K / dim128 / M16 / ef=64 / k=10 / 300 query（`n10000_d128_M16_static`）：

| 策略 | 邻居区 requested | 邻居区 wasted | 邻居 read_ops | node_reads | **总 read_ops** | p50 (µs) | p95 (µs) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| B1 fixed | 2 723 168 B | 481 800 B（17.69%） | 21 976 | 537 390 | 559 366 | 1904 | 3051 |
| B2 header_actual | 2 241 368 B | 0 | 43 652 | 537 390 | 581 042 | 2071 | 3424 |
| **B3 cached_length（cap 10000）** | **2 241 368 B** | **0** | **26 482** | 537 390 | **563 872** | 2050 | 3586 |

- **B3 的字节账与 B2 完全相同**（`requested` 逐字节相等），因为命中时请求的正是 `4 + 4·hint`，而 `hint` 在静态图上就是真实 degree（`wasted = 0`）；
- 相对 B2 的邻居操作数收益 **39.3%**（`26 482` vs `43 652`），相对 B1 只多 **20.5%**（`26 482` vs `21 976`）；
- 相对 B1 的邻居区字节收益 **17.69%**，但邻居区只占全部读流量的 **0.77%**，因此**总字节收益仅 0.17%**、**总读操作收益仅 2.96%**（`563 872` vs `581 042`）。这两组口径必须同时报（plan §7.3）；
- 命中率恒为 `1 − miss`，静态下 `q = 0`（预测从未被证伪），`cache_updates = 0`。

### 5.1 缓存容量扫描（H2）

| `cache_capacity` | 命中率 | 未命中 | 淘汰 | 邻居 read_ops | 相对 B2 |
| --- | --- | --- | --- | --- | --- |
| 100 | 8.25% | 20 164 | 20 164 | 42 140 | −3.5% |
| 1 000 | 27.47% | 15 940 | 15 940 | 37 916 | −13.1% |
| 10 000 | 79.50% | 4 506 | 0 | 26 482 | −39.3% |
| 100 000 | 79.50% | 4 506 | 0 | 26 482 | −39.3% |

10K 索引的 `(node, level)` 键约 1.07 万，容量 10 000 已接近全覆盖，命中率**在该点饱和**、此后不再上升；100 / 1 000 时 `cache_evictions == cache_misses`，说明容量是唯一约束（每次插入都要挤掉一条）。这直接印证 plan §0⑤：**用默认容量 100 000 扫 10K 索引会得到一条直线**。

### 5.2 按层命中率（H1）

| 容量 | L0 | L1 | L2 | L3 | L4 |
| --- | --- | --- | --- | --- | --- |
| 100 | 3.06% | 18.41% | 73.61% | 94.04% | 100% |
| 1 000 | 23.19% | 36.87% | 80.39% | 96.55% | 100% |
| 10 000 | 78.02% | 84.46% | 96.61% | 99.69% | 100% |

命中率**随层数单调上升**，即使容量只有 100 条，L4 也 100% 命中、L2 已达 73.6%。原因是高层节点被每条查询反复经过（入口点 → L2 → L1 → L0），而 base 层的节点几乎只被访问一次 —— 这正是 SHINE 所说的 "numerous unique graph nodes"。**结论：只报全局命中率会低估该机制的适用面，必须按层报告。**

### 5.3 hint 与真实 degree

静态下 `avg_hint` 与 `avg_degree` 的差异随容量收敛：cap 100 时 `10.78` vs `24.50`（只对少量热点节点有预测，且预测的是它们的历史度数），cap 10 000 时 `24.80` vs `24.50`。差异分布见 `out/figures_baseline3/b3_hint_vs_degree.png`（左：每次运行的平均 hint vs 平均 degree；右：逐查询的 `mean hint − mean degree` CDF）。

> 注意 `avg_hint > avg_degree` 是**采样偏差**而非系统性高估：`hint` 只在命中时记入 `hint_slots_sum`，而 `degree_sum` 覆盖全部成功读取。真实的过宽量由 `wasted_bytes = 4 × over_read_slots_sum` 直接给出，静态下为 0。

---

## 6. `p − q` 模型校验

模型（plan §4.2）：`E[ops] = 2 − p + q`，故相对 Baseline 2 的操作数收益 `≈ (p − q) / 2`。

| 配置 | 实测 `p` | 实测 `q` | 实测 op 收益 | 模型 `(p−q)/2` | 偏差 |
| --- | --- | --- | --- | --- | --- |
| static cap 100 | 0.0825 | 0 | 3.46% | 4.12% | −16.1% |
| static cap 1 000 | 0.2747 | 0 | 13.14% | 13.73% | −4.3% |
| static cap 10 000 | 0.7950 | 0 | 39.33% | 39.75% | −1.1% |
| upsert 0.3 hot10 cap 1 000 | 0.2748 | 0.0138 | 12.96% | 13.05% | −0.7% |
| erase 0.3 hot10 cap 1 000 | 0.2751 | 0 | 13.16% | 13.75% | −4.3% |

高命中率下模型与实测相差 **1% 以内**；低命中率时偏差变大，原因是模型把分母当成固定 2，而实际分母是 `1 + [d>0] ≈ 1.986`（300 / 21 976 个层的 `degree == 0`，只读 4 字节），并且命中/未命中的层并不均匀分布。**模型可用，但结论必须写成 `p − q` 的函数**（plan §0①）。

三个可以直接引用的定量结论：

1. 只要 `p > q`，B3 的邻居区操作数就优于 B2；反之（全部命中都低估）二者都退化成 2 次。
2. B3 在操作数上**永远不优于 B1**（B1 恒 1 次），它的价值是"B2 的字节 + 接近 B1 的操作数"。
3. 相对 B2 的**总**读操作收益被邻居区占比放缩：`(p−q)/2 × neighbor_share ≈ 39% × 7.7% ≈ 3%`。

---

## 7. 动态更新

### 7.1 容量 × 更新比例 × 分布

`erase` 与 `upsert` 各 3 个比例 × 2 种分布 × 4 个容量，共 48 次 B3 运行，全部满足 8 条恒等式。要点：

- 命中率**几乎只由容量决定**（cap 1 000 时 `erase 0.1/0.3/0.5` 为 27.5%–28.4%，`hot` 与 `roundrobin` 差异 < 0.8 个百分点）；
- `erase` 的 `q` 始终为 0（degree 只会下降 → 只可能"预测过宽"），`upsert` 在非热点分布下 `q ≈ 0.1%`（degree 上升但被改写的节点很少被再次读到）；
- 因此常规分布下"更新比例"对 B3 的**邻居区**代价几乎没有影响，B3 的命中率退化只发生在**容量不足**时。

### 7.2 热点更新：把 degree 漂移放大（H3 的决定性证据）

`hot K=10`（10 个热点 id 轮流被改写）、`ratio=0.3`、`cap=1000`：

| 更新 | `p` | `q` | 过宽率 | fallbacks | `wasted` | p50 |
| --- | --- | --- | --- | --- | --- | --- |
| `erase` | 27.51% | 0 | 0.0496% | **0** | 12 B | 2433 µs |
| `upsert` | 27.48% | **1.375%** | 0 | **83** | 0 B | 2097 µs |

**非对称性成立且方向与 plan 陷阱 4 一致**：

- `erase` 从其他节点的邻接表中删 id → 那些节点 degree 下降 → 命中时 `h > d` → **只产生"预测过宽"浪费，永不 fallback**；
- `upsert` 把目标节点的邻接表重填到 `cap`（2M）→ degree 上升 → 命中时 `h < d` → **fallback 上升，不产生浪费**。

热点分布把 `upsert` 的 `q` 从 0.1% 放大到 1.4%（14 倍），fallback 从 2 次放大到 83 次；而 `erase` 的 fallback 始终为 0。图：`b3_overestimate_waste.png`、`b3_fallbacks_vs_update_ratio.png`、`b3_hit_and_q_vs_update_ratio.png`。

---

## 8. 假设检验汇总

| 编号 | 结论 | 证据 |
| --- | --- | --- |
| H1 命中率随层升高 | ✅ 成立（cap 10 000：78.0% → 84.5% → 96.6% → 99.7% → 100%） | §5.2 |
| H2 命中率随容量先快后平 | ✅ 成立，10K 索引在 10 000 条饱和 | §5.1 |
| H3 erase→过宽、upsert→fallback | ✅ 成立（热点实验：12 B vs 83 fallback） | §7.2 |
| H4 热点更新压低命中率 | ❌ 未观察到：容量充足时热点与轮转命中率差异 < 0.2% | §7.1 |
| H5 八条恒等式 | ✅ 84 次运行全通过 | §4 |
| H6 三策略结果逐位相同 | ✅ 策略无关列（`useful_bytes`/`node_bytes`/`layer_reads`/`degree_sum`/`capacity_sum`）逐位相同；用例 1 覆盖全部节点×层 | §5、§9 |
| H7 mmap 上延迟优势远小于 op 收益 | ✅ B3 p50 2050 µs vs B2 2071 µs（−1.0%），而邻居 op −39% | §5 |
| H8 RDMA 上 `rtt_per_saved_op` ∈ [1,3] µs | ⏳ **未验证**：本机无 RDMA 硬件，需按 §10.3 在 eRDMA 机器上复测 | — |

H4 被证伪的解读：在"容量不足"区间，miss 由淘汰而非偏斜决定；只有当工作集远小于容量、热点又反复改写同一批 key 时，偏斜才会体现为命中率下降。当前 10K 索引 / 300 query 的规模不满足这个条件，**不能据此否定 H4**，只能说本组参数下不成立。

---

## 9. 正确性测试

`tests/read_strategy_cached_test.cc`（18 个用例，全部 `assert`）：

1. 与 B1 逐邻居等价（真索引，全部节点 × 层，每层读两次覆盖 miss/hit）；
2. 冷/热两次读结果一致；
3. 状态 A（miss，`d>0`）：`ops=2`、`header_reads=1`、`cold_start_reads=1`、`fallbacks=0`、`wasted=0`；
4. 状态 B 过宽（hint 5 / degree 2）：`ops` 只 +1、`wasted=12 B`、`over_read_slots_sum=3`、`cache_updates=1`；
5. 状态 B 精确：`ops` 只 +1、`wasted=0`、`cache_updates=0`；
6. 状态 C 低估（1 → 4）：`ops` +2、`fallbacks=1`、`fallback_bytes=12 B`、回写 degree=4；
7. 缓存 hint 0 而层非空 → 走状态 C，不越界；
8. 层隔离：同一节点 L0(cap 8) / L1(cap 4) 两个 key 独立；
9. 畸形 `degree > capacity`：miss 与 fallback 两条路径都截断到 cap，第二次读长度 `(cap−hint)·4`；
10. `degree == 0`：状态 A 只读 4 B；状态 B（缓存 0）只读 4 B 且 `wasted=0`；
11. `capacity == 0`：错误、`t.calls == 0`、不写缓存；
12. 冷启动第二次读失败 → 不写缓存，再读仍是 miss；
13. fallback 短读 → 报错且**保留旧 hint**，下一次仍会重试 fallback；
14. header 短读 / 硬失败 → 错误、`read_ops=0`、不写缓存；
15. 严格 LRU：容量 2 时 A、B、C 顺序读 → 淘汰 A；再读 A 未命中、读 C 命中；
16. **generation 不得用于判定**：gen 变而 degree 不变 → 仍命中；degree 变而 gen 不变 → 必 fallback；
17. 同一 `shared_ptr<LengthCache>` 的新实例第一次读即命中（模拟 `reload()`）；
18. 多层节点：L0 用 2M、L>0 用 M 的偏移与长度都正确，且命中时只需一次读。

`ctest`：9/9 通过（含 `baseline3_experiment_smoke`，`--cache-capacity 1000` 强制真实淘汰）。

---

## 10. 复现手册

```bash
# 构建与测试
cmake -S . -B build -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure

# CLI 单跑（cache_capacity 走 --cache-capacity）
./build/compute_client --index data/index_n10000_d128_M16.bin --transport mmap \
  --read-strategy cached_length --cache-capacity 1000 \
  --queries-count 300 --ef-search 64 --k 10 --seed 42

# 实验驱动（外部 shared cache 跨 reload 存活）
./build/baseline1_experiment --index data/index_n10000_d128_M16.bin --transport mmap \
  --read-strategy cached_length --cache-capacity 10000 \
  --queries-count 300 --ef-search 64 --k 10 --warmup 50 --seed 42 \
  --out out/b3_static.csv --summary-out out/baseline3_summary.csv

# 全量扫描（本报告数据；索引副本在 tmp/，数据在 data/，结果在 out/）
CONFIGS="10000:128:16" QUERIES=300 UPDATES=300 RATIOS=0.1,0.3,0.5 \
CACHE_CAPACITIES=100,1000,10000,100000 DISTS="roundrobin hot" \
./scripts/run_baseline3.sh

# 热点补充实验（hot K=10，三个策略各跑一次；label 加到同一 summary）
HOT_SIZE=10 CONFIGS="10000:128:16" QUERIES=300 UPDATES=300 RATIOS=0.3 \
CACHE_CAPACITIES=1000 DISTS="hot" RESET_SUMMARY=0 ./scripts/run_baseline3.sh

# 分析与绘图（恒等式违反时退出码为 1）
source .venv/bin/activate
python scripts/analyze_baseline3.py \
  --summary out/baseline3_summary.csv --per-query-dir out \
  --out-dir out/figures_baseline3 --report out/baseline3_report.md \
  --index data/index_n10000_d128_M16.bin
```

### 10.1 RDMA（H8 的唯一来源）

```bash
./build/storage_server --index data/index_n10000_d128_M16.bin --transport rdma \
  --rdma-connection cm --bind <存储IPv4> --port 18515 --ib-device erdma_0 --ib-port 1

./build/compute_client --transport rdma --rdma-connection cm \
  --host <存储IPv4> --port 18515 --ib-device erdma_0 --ib-port 1 \
  --read-strategy {fixed|header_actual|cached_length} --cache-capacity 10000 \
  --queries-count 1000 --ef-search 64 --k 10
```

RDMA 模式下 `upsert/erase` 不会 `reload()`，驱动的 shared cache 自然延续；跨策略比较统一用 `ReadMetrics`，不用 `TransportStats`。

---

## 11. 局限与后续衔接

**mmap 局限（必须声明）**：

1. `pread` 没有"往返"语义，B3 省掉的那一次依赖读在 mmap 上只省一次系统调用 → 延迟差异（−1.0%）远小于操作数差异（−39%）。**延迟结论只能来自 RDMA**（H8 待补）。
2. mmap 下每次更新都会 `apply()` 重写整份索引并 `reload()` 重新解析（实测 10K 约 40–60 ms/次，100K 约 0.46 s/次），因此更新比例高的配置里 `qps_end_to_end` 被更新主导，**不能用它比较策略**；本报告只比较 per-query 延迟与操作数。
3. 本报告的 `q` 是**保守下界**：驱动在每个查询后立即测量，热点节点被改写后若在剩余查询里没被再次读到，fallback 就不会发生。

**未完成项**：并发度（plan DR-14，`QueryEngine::context_`/`stats_` 非线程安全）、RDMA 实测（H8）、1M 规模（缓存按 `N·1.07` 线性增长，需外推验证）。

**与分级前缀（B4）的衔接**：`LengthCache` / `length_cache_key()` 可直接复用，只需把存的 `degree` 换成缓存的级数 `ceil(degree/G)`；`hint_slots_sum` / `over_read_slots_sum` / `hint_overestimates` 正是"向上取整浪费"的口径；`fallbacks` / `fallback_bytes` / `cache_invalidations` 对应前缀机制的失效路径；`--prefix-granularity` 与 `ReadContext::prefix_granularity` 已全链路就绪。
