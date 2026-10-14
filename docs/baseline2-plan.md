# Baseline 2 实施计划：header-first 读取

本文是 [experiment.md](experiment.md) 中「Baseline 2」的落地计划。结构与 [baseline1-plan.md](baseline1-plan.md) 对齐，可以并行阅读：**Baseline 1 的测量底座（`ReadMetrics` 分账、实验驱动、CSV 口径）是本计划的前置依赖**，本计划只写 Baseline 2 特有的部分。

相关文档：[experiment.md](experiment.md)（课题要求）、[help.md](help.md)（§6 是 Baseline 2 的伪代码与统计建议）、[erdma.md](erdma.md)（RDMA 部署）、[baseline1-plan.md](baseline1-plan.md)（公共底座）。

---

## 0. 核心判断（三条，先看这里）

**① 这是三个 Baseline 里唯一「代价大于收益」很可能是真实结论的一个。** [experiment.md](experiment.md#L99) 明确说它要分析的是"减少无效传输和增加额外往返之间的权衡"。本计划 §5 给出量化模型，预测在这次原型的参数下**额外的往返数量级上远贵于省下的字节**。这不是坏消息——一个能被量化解释的负面结论，比一个含糊的正面结论更有分量。

**② 它的读取逻辑必须**真的**分两次，不能合并。** [help.md](help.md#L189) 原文：

> 第二次读取依赖第一次读取的结果，RDMA 下会产生额外的操作完成等待；**不要把两次读取合并**，否则就失去了 Baseline2 的实验含义。

**③ 它的结论只能在 RDMA 上成立。** mmap 的 `pread` 没有"往返"语义，两次 `pread` 只是多一次系统调用。所以分工是：**mmap 验证正确性与字节/操作数口径，RDMA 给出延迟判定**。任何把 mmap 延迟当作 Baseline 2 往返代价的做法都是错的。

---

## 1. 目标与验收标准

### 1.1 定义

> 第一次读取节点 header 或 degree 信息，获得实际邻居长度后，再读取后续邻居数据。
> —— [experiment.md](experiment.md#L87-L89)

对应本原型的落点（[help.md](help.md#L162-L191)）：`RemoteNodeView::layer_offset` 指向该层的 `degree` 字段，邻居 slot 紧跟在后面。

```text
第一次：transport.read({view.layer_offset, 0}, 4 字节)              → degree
第二次：transport.read({view.layer_offset + 4, 0}, degree × 4 字节)  → 邻居
```

### 1.2 完成判据（DoD）

| 编号 | 判据 | 来源 |
| --- | --- | --- |
| D1 | 具名策略类 `Baseline2HeaderFirstStrategy`，`name()` 返回 `"baseline2_header_first"`，含注释 | [§六.5](experiment.md#L173-L174) |
| D2 | 通过 `--read-strategy header_actual` 或 `set_read_strategy()` **独立选择** | [§六.5](experiment.md#L137-L138) |
| D3 | 未修改 `QueryEngine::search()`；策略内无 ibverbs | [§六.5](experiment.md#L175-L176) |
| D4 | 边界齐全：header 短读/失败、degree 截断、第二次读失败、`capacity == 0`、`degree == 0` | [§六.5](experiment.md#L177) |
| D5 | **两次读取确实是两次**，未做合并或预读 | [help.md](help.md#L189) |
| D6 | 正确性测试：**与 Baseline 1 逐邻居一致**（同一 `RemoteNodeView` 返回相同集合） | [§六.5](experiment.md#L179) |
| D7 | 指标可导出：额外读取操作次数、依赖读取次数、延迟、传输字节数变化 | [§五.3](experiment.md#L91-L97) |
| D8 | 动态更新场景下 degree 变化对性能的影响有数据 | [§五.3](experiment.md#L97) |
| D9 | 保存源码、构建命令、运行参数、原始数据与统计脚本 | [§六.5](experiment.md#L180) |

### 1.3 关键优势：CLI 和枚举**都已经就绪**

我核对了代码，Baseline 2 的接入成本比 Baseline 1 更低：

| 组件 | 状态 |
| --- | --- |
| `ReadPolicy::HeaderActual` 枚举值 | ✅ 已存在（[types.h](include/rdma_hnsw/common/types.h#L24)） |
| `to_string(HeaderActual)` → `"header_actual"` | ✅ 已存在（[types.h](include/rdma_hnsw/common/types.h#L30-L31)） |
| `"header_actual"` / `"header"` → `HeaderActual` | ✅ 已存在（[types.h](include/rdma_hnsw/common/types.h#L40-L41)） |
| `--read-strategy` 白名单 | ✅ 已包含 `header_actual` 和 `header`（[config.cc](src/common/config.cc#L70-L73)） |
| `ReadMetrics::header_reads` / `dependent_reads` | ✅ 字段已存在并被 `merge()` 处理（[read_strategy.h](include/rdma_hnsw/read/read_strategy.h#L14-L15)），**但全代码库无人自增** —— 就是给 Baseline 2 留的 |
| 工厂 `make_read_strategy` | ❌ 只认 `Fixed`，其余抛异常（[read_strategy.cc](src/read/read_strategy.cc#L63-L69)） |
| `print_usage` 文案 | ⚠️ 只写了 `fixed`，应补 `header_actual`（[config.cc](src/common/config.cc#L148-L149)） |

**所以接入只需要：写类 + 在工厂加一个分支 + 改一行 usage 文案。**不需要动 `types.h`、`config.cc` 的白名单、`CMakeLists.txt` 的源文件列表。

---

## 2. 与 Baseline 1 的差异一览

| 维度 | Baseline 1（fixed） | Baseline 2（header-first） |
| --- | --- | --- |
| 每层读取次数 | **1** | **2**（`degree == 0` 时 1） |
| 读取长度 | `4 + 4·capacity` | `4` + `4·degree` |
| `requested_bytes` | `4 + 4·cap` | `4 + 4·deg`（**减少 `4·(cap−deg)`**） |
| `wasted_bytes` | `4·(cap − deg)` | **恒为 0** |
| `read_ops` | 每层 1 | 每层 1 或 2 |
| `header_reads` | 0（degree 搭在大读里） | 每层 1 |
| `dependent_reads` | **0**（无依赖） | 每层 1（`degree > 0` 时） |
| 数据依赖 | 无 | **第二次读的地址和长度都依赖第一次的结果** → 不可流水线 |
| 返回的邻居集合 | 真值前 degree 个 | **必须完全相同**（D6） |
| mmap 上可测的 | 全部 | 字节、op 数、正确性（**测不出往返代价**） |
| RDMA 上的风险 | 传输浪费 | **延迟上升**（op 数增加） |

用 M=16、dim=128、`degree=20` 的具体数字（cap(0)=32）：

```text
Baseline 1：requested = 4 + 128 = 132   useful = 84   wasted = 48（浪费率 36.4%）
Baseline 2：requested = 4 +  80 =  84   useful = 84   wasted =  0（浪费率 0%）
                                                    Δbytes = −48（−36.4% 的层读取）
                                                     Δops   = +1
```

---

## 3. 现状盘点

### 3.1 已有

| 能力 | 位置 |
| --- | --- |
| 层地址（指向 degree 字段） | `node_layer_offset()`（[graph_store.h](include/rdma_hnsw/graph/graph_store.h#L75-L83)） |
| 层容量 | `layer_capacity()`（[graph_store.h](include/rdma_hnsw/graph/graph_store.h#L46-L48)） |
| 策略接口与 `ReadContext::transport` | [read_strategy.h](include/rdma_hnsw/read/read_strategy.h#L38-L53) |
| 指标字段 `header_reads` / `dependent_reads` | [read_strategy.h](include/rdma_hnsw/read/read_strategy.h#L14-L15) |
| `decode()` 辅助函数（可复用） | [read_strategy.cc](src/read/read_strategy.cc#L9-L16) |
| CLI/枚举接线 | 见 §1.3 |
| 公共测量底座 | [baseline1-plan.md](baseline1-plan.md) §T2 / §T4 |

### 3.2 缺失

| 缺口 | 影响 |
| --- | --- |
| 策略类本身 | D1 / D4 / D5 |
| 工厂分支 | D2 |
| `header_reads` / `dependent_reads` 从未自增 | D7 |
| `degree == 0` 的读取语义未定义 | read_ops 口径会与 Baseline 1 不可比 |
| 没有"与 Baseline 1 逐邻居一致"的测试 | D6 |
| 没有依赖读取的延迟测量手段 | D7 的"额外往返造成的延迟变化" |
| `print_usage` 未列出 `header_actual` | 可复现性 |

### 3.3 必须先统一的共享语义（Baseline 1 / 2 / 3 一致）

这三条不一致会让横向对比失去意义，必须在写代码前定死：

| 情形 | 统一语义 | 理由 |
| --- | --- | --- |
| `capacity == 0` | **返回 `{ {}, false, "..." }`**，不发起任何读取 | 与 [baseline1-plan.md](baseline1-plan.md) §T1 保持一致。注意现行 `Fixed` 在这个分支上会读 4 字节并返回 `ok` + 空集合 —— 这是刻意的行为收紧，且因 `M >= 2`（[config.cc](src/common/config.cc#L127) 强制）而实际不可达 |
| `degree > capacity` | **截断到 `capacity`**，随后按截断值读取 | 远程数据不可信（[help.md](help.md#L191)） |
| 短读 / 传输失败 | **返回错误**，绝不部分返回、绝不伪造邻居 | [help.md](help.md#L95-L96) |
| `degree == 0` | Baseline 1 读满 cap（有浪费）；Baseline 2 **跳过第二次读**（`read_ops = 1`, `dependent_reads = 0`） | 让"额外操作次数"可解释；需在报告中说明 |

---

## 4. 论文阅读任务

| 优先级 | 文档 | 必读部分 | 要回答的问题 |
| --- | --- | --- | --- |
| **P0** | `papers/2.pdf`（SHINE） | §3.2 Query Processing 的 Algorithm 1；"What to Cache?" 段落 | SHINE 第 6 行**一次读完整个邻居表**（`neighborlist ← RDMA_READ(nlist_rptr)`）——**它为什么不做 header-first？** Baseline 2 就是它的反事实 |
| **P0** | `papers/7.pdf`（RDMA 存储综述） | 通信模式、单向 vs 双向、批处理、资源管理；设计指南部分 | 一次 RDMA 操作的**往返代价**由什么决定？什么情况下"多一次 op"比"多传字节"更贵？ |
| P1 | `papers/6.pdf`（FaSST） | 对"大量使用单向 RDMA"的批评、消息率与可扩展性、Doorbell batching | 为什么**操作数/消息率**而不是带宽会先成为瓶颈？（支撑 §5 的 H2） |
| P1 | `papers/3.pdf`（DiskANN） | BeamSearch 批取邻域、C 跳缓存 | **替代方案**：不追求"精确长度"，而是把多个邻居区域的读取**批量合并**来摊薄往返 —— 这是 Baseline 2 与本课题前缀机制的共同参照 |
| P2 | `papers/5.pdf`（FaRM） | RDMA 延迟与消息率实测数据（31µs、1.67 亿 KV/s 等） | 给 §5 的 RTT 量级取一个真实锚点 |
| P2 | `papers/1.pdf`（HNSW） | 参数段（`M`、`Mmax0 = 2M`、`efConstruction`） | degree 的上界决定 Baseline 2 最多能省多少字节 |
| P2 | `papers/4.pdf`（FreshDiskANN） | 惰性删除、动态 degree 变化 | 动态场景下"能省的字节"如何随时间变化 |

### 4.1 带着读的两个反直觉点

**反直觉点一：SHINE 的算法里没有 header-first。** 它的第 6 行一次 `RDMA_READ` 就把整段邻居表取回，之后才解析出表头里的条目数。也就是说 **SHINE 选择了 Baseline 1 的形态**。读到这一段时你必须能回答：在 SHINE 的设定下（200 维、图节点定长、`M` 中等），header-first 多出来的那次往返值不值？——读它的 "What to Cache?" 那段（`2/d`、200 维时邻居表只占 1%）你就有答案了。

**反直觉点二：`papers/3.pdf` 走的是第三条路。** DiskANN 不试图精确知道长度，而是**把多个邻域的读取打包**（BeamSearch 批取），让一次往返覆盖多个对象。这解释了为什么"减少往返"有两种解法：**少读**（Baseline 2）与**批量读**（DiskANN / 进阶 6.4），而它们可以叠加。

---

## 5. 成本模型：先把账算清楚，再写代码

这一节是本计划的核心。Baseline 2 的结论必须能用一个公式解释，否则数据只是数字。

### 5.1 单次层读取的精确账

```text
Baseline 1：ops = 1                bytes = 4 + 4·cap
Baseline 2：ops = 1 + [deg > 0]    bytes = 4 + 4·deg
Δbytes = −4·(cap − deg)            Δops = +[deg > 0]
```

### 5.2 每次扩展节点的账（dim=128, M=16, cap(0)=32, 有效邻居数 d）

```text
Baseline 1：ops = 2 + d      bytes = 536 + 132 + 536·d
Baseline 2：ops = 3 + d      bytes = 536 +  84 + 536·d

d = 30 → B1: 32 ops / 16 748 B      B2: 33 ops / 16 700 B
         Δops = +3.1%              Δbytes = −0.29%
d =  4 → B1:  6 ops /  3 812 B      B2:  7 ops /  3 700 B
         Δops = +16.7%             Δbytes = −2.9%
```

**两个结论直接跳出来**：

1. **省下的字节只占 0.3%~3%**，因为它只作用在「邻居区域」这一小块上（§[baseline1-plan](baseline1-plan.md) §6.3：邻居区域占比 ≈ `1/(dim+6)` ≈ 0.75%）。而 `node_bytes`（候选节点向量读取）支配了总流量，Baseline 2 对它一点帮助都没有。
2. **增加的操作次数是每层 +1，相对增幅是 `1/(2+d)`**。`d` 越小，操作数增幅越大——而这恰好也是省字节最划算的区间。这是本 Baseline 唯一可能翻盘的地方。

### 5.3 往返 vs 字节的盈亏平衡

一次 RDMA READ 的往返时间记 `R`，有效带宽记 `W`。省掉 `S` 字节、多付一次往返：

```text
单查询延迟意义上翻盘的条件：   S / W  >  R      ⟺   S > R · W
```

代入真实量级（`R ≈ 1.5 µs`、100 GbE 即 `W ≈ 12.5 GB/s`）：

```text
盈亏平衡点 S* ≈ 1.5e-6 × 12.5e9 ≈ 18.75 KB
本原型的最大可省字节（level 0）= 4·cap = 8·M 字节
M = 16 → 128 B        ← 比 S* 小约 150 倍
需要 M ≈ 2 344（cap ≈ 4 700 个 slot）才可能靠延迟翻盘
```

**所以在本原型的参数空间内，Baseline 2 不可能在单查询延迟上赢。** 它只可能在**高并发、带宽受限**的场景下以吞吐形式获益（此时 op 数被多个 QP 流水化，不再串行主导延迟）。

### 5.4 预注册假设（先写下来，再用实验证伪）

| 编号 | 假设 | 判据 | 若被证伪说明什么 |
| --- | --- | --- | --- |
| **H1** | Baseline 2 的邻居区域 `wasted_bytes == 0`，`requested` 下降 `4·(cap−deg)` | 逐层账本自检 | 测量或实现有 bug（这是**必须成立**的自检项） |
| **H2** | RDMA 下 Baseline 2 延迟上升，幅度约 `1/(2+d)` | 与 Baseline 1 同配置对比延迟 | 说明后端对 READ 做了流水化（需查 transport 实现） |
| **H3** | mmap 下两者延迟差异可忽略（< 5%） | 同配置 mmap 对比 | 说明 mmap 也能反映 op 代价，结论可以下移 |
| **H4** | Baseline 2 在 M 小、degree 小时相对受益最大 | `M ∈ {8,16,32}` 扫描 | 说明模型漏了别的因素 |
| **H5** | Baseline 1 与 Baseline 2 的**查询结果逐条完全相同** | 同查询集结果比对 | 实现有 bug（偏移或长度算错） |

### 5.5 一个应该写进报告的设计洞察

**degree 存在"层区域"里而不是 `NodeDiskHeader` 里，是 Baseline 2 必须多付一次往返的根因。**

而 `QueryEngine::search()` **反正已经读了 24 字节的 `NodeDiskHeader`**（[query_engine.cc](src/runtime/query_engine.cc#L47-L64)）。如果 degree 放在那个 header 里，header-first 的额外往返就是 **0**——"先读 header 再读邻居"会退化成"读 header 时顺带知道长度"。

这一条直接指向分级前缀机制的设计选择：**把长度信息放在已经被读到的那块区域里**（或计算侧缓存），而不是靠第二次专程读取去取。这个观察也解释了为什么 SHINE 的布局（header + 向量 + 定长邻居表）能让它安然选择"一次读完"。

---

## 6. 设计决策（DR）

| 编号 | 决策 | 理由 |
| --- | --- | --- |
| DR-1 | 使用已存在的 `ReadPolicy::HeaderActual`，不新增枚举值 | §1.3：枚举、字符串映射、CLI 白名单全部已就绪 |
| DR-2 | 类名 `Baseline2HeaderFirstStrategy`，`name()` = `"baseline2_header_first"` | 满足 D1；与 `--read-strategy header_actual` 的映射关系写进注释 |
| DR-3 | 两次读取都必须显式检查 `ok` 与长度；失败即返回错误 | D4；`Fixed` 忽略 `r.ok` 的缺陷不能复制 |
| DR-4 | `degree == 0` 时**跳过**第二次读取，`dependent_reads` 不增加 | 定义清晰的 op 口径（§3.3）；否则 B2 的 `read_ops` 会被空读虚增 |
| DR-5 | `header_reads` 只统计策略内**专程**的 degree 读取；Baseline 1 的 `header_reads = 0` | 让"额外读取操作次数"可跨策略相减 |
| DR-6 | 不引入任何预读、不合并两次读、不缓存 | D5；否则失去实验含义 |
| DR-7 | 依赖读取的延迟测量放在**实验程序的 transport 装饰器**里，不进核心库 | 满足 D3；也让 Baseline 3 复用 |
| DR-8 | 复用 Baseline 1 的 `ReadMetrics` 分账与实验驱动，仅参数化策略注入点 | 保证两策略使用完全相同的统计与 workload |

---

## 7. 任务分解（WBS）

总计约 **4.5–5 个工作日**，对应 [experiment.md](experiment.md#L308-L310) 第 3 周的后半段（Baseline 1 完成之后）。

### T1 — 策略实现与工厂接线（P0，0.5 天）

**改动文件**：`include/rdma_hnsw/read/read_strategy.h`、`src/read/read_strategy.cc`、`src/common/config.cc`（仅 usage 文案）

```cpp
// include/rdma_hnsw/read/read_strategy.h
// Baseline 2：header-first。先读取该层 4 字节 degree 字段，再按实际 degree
// 读取邻居。两次读取存在数据依赖，无法流水化 —— 这正是本策略的实验对象。
// 对应 --read-strategy header_actual。
class Baseline2HeaderFirstStrategy final : public NeighborReadStrategy {
public:
  NeighborReadResult read(const RemoteNodeView &view, ReadContext &ctx) override;
  const char *name() const override { return "baseline2_header_first"; }
};
```

```cpp
// src/read/read_strategy.cc
NeighborReadResult Baseline2HeaderFirstStrategy::read(const RemoteNodeView &v,
                                                      ReadContext &c) {
  if (v.capacity == 0)
    return {{}, false, "baseline2_header_first: layer capacity is zero"};

  // ── 第一次读取：只取 degree 字段 ──
  std::array<byte, sizeof(std::uint32_t)> hdr{};
  const auto hr = c.transport.read({v.layer_offset, 0}, hdr);
  if (!hr.ok || hr.bytes != hdr.size()) {
    if (c.metrics) c.metrics->failed_reads++;
    return {{}, false, "baseline2_header_first: header read failed: " + hr.error};
  }
  if (c.metrics) {
    c.metrics->read_ops++;
    c.metrics->header_reads++;                       // 专程的 degree 读取
    c.metrics->requested_bytes += hdr.size();
  }
  std::uint32_t raw{};
  std::memcpy(&raw, hdr.data(), sizeof(raw));
  const std::uint32_t degree = std::min<std::uint32_t>(raw, v.capacity);

  // degree == 0：没有邻居可读，不发起第二次读取（见 DR-4）
  if (degree == 0) {
    if (c.metrics) {
      c.metrics->zero_degree_layers++;               // 新增字段
      c.metrics->useful_bytes += hdr.size();
      c.metrics->layer_reads++;
      c.metrics->capacity_sum += v.capacity;
    }
    return {{}, true, {}};
  }

  // ── 第二次读取：长度与地址都依赖第一次的结果 ──
  const std::size_t n = degree * sizeof(NodeId);
  std::vector<byte> b(n);
  const auto nr = c.transport.read({v.layer_offset + sizeof(std::uint32_t), 0}, b);
  if (!nr.ok || nr.bytes != n) {
    if (c.metrics) c.metrics->failed_reads++;
    return {{}, false, "baseline2_header_first: neighbor read failed: " + nr.error};
  }
  if (c.metrics) {
    c.metrics->read_ops++;
    c.metrics->dependent_reads++;                    // 依赖前一次读取的后续读取
    c.metrics->requested_bytes += n;
    c.metrics->useful_bytes += hdr.size() + n;       // wasted 恒为 0
    c.metrics->layer_reads++;
    c.metrics->capacity_sum += v.capacity;
    c.metrics->degree_sum += degree;
  }
  return decode(b, degree);
}
```

工厂补一个分支（[read_strategy.cc](src/read/read_strategy.cc#L63-L69)）：

```cpp
if (p == ReadPolicy::HeaderActual)
  return std::make_unique<Baseline2HeaderFirstStrategy>();
if (p != ReadPolicy::Fixed)
  throw std::invalid_argument("read strategy is a student extension; ...");
return std::make_unique<Baseline1FixedStrategy>();
```

**验收**：`--read-strategy header_actual --help` 无异常；`ctest` 全绿；usage 里出现 `header_actual`。

### T2 — 指标补全（P0，0.5 天）

**改动文件**：`include/rdma_hnsw/read/read_strategy.h`、`src/read/read_strategy.cc`（`merge()`）

在 [baseline1-plan.md](baseline1-plan.md) §T2 引入的字段（`layer_reads`、`node_reads`、`node_bytes`、`degree_sum`、`capacity_sum`、`failed_reads`）之上，再补一个：

```cpp
std::uint64_t zero_degree_layers{};   // degree == 0 的层数（解释 read_ops 为何 < 2×layer_reads）
```

**验收**：两个恒等式成立

```text
邻居区域内：requested_bytes == useful_bytes + wasted_bytes
Baseline 2：read_ops == header_reads + dependent_reads
            dependent_reads + zero_degree_layers == layer_reads
```

第二条是 Baseline 2 特有的、非常有用的自检——它能把"offset 算错"和"degree 读错"当场暴露出来。

### T3 — 正确性测试（P0，0.5 天）

**改动文件**：`tests/read_strategy_test.cc`（或新增 `tests/read_strategy_equivalence_test.cc`）

| 用例 | 期望 |
| --- | --- |
| **与 Baseline 1 等价（核心）** | 同一 `RemoteNodeView`，两策略返回的 `neighbors` **完全相同** |
| `degree < capacity` | `requested == 4 + 4·deg`，`wasted == 0`，`read_ops == 2` |
| `degree == capacity` | `read_ops == 2`，`wasted == 0` |
| **`degree == 0`** | `read_ops == 1`，`dependent_reads == 0`，`zero_degree_layers == 1`，返回 `ok` + 空集合 |
| `degree > capacity`（畸形） | 截断到 capacity；第二次读长度为 `capacity·4`；不越界 |
| header 短读 / 失败 | 返回错误；`failed_reads == 1`；**不发起第二次读取** |
| 第二次读短读 / 失败 | 返回错误；**不部分返回**、不伪造邻居 |
| `capacity == 0` | 返回错误，零次读取 |
| 多层（`max_level = 1`） | level 0（cap=2M）与 level 1（cap=M）偏移都正确 |
| 空 slot 不当邻居 | 返回长度恰为 degree |

等价性测试的写法（复用 [baseline1-plan.md](baseline1-plan.md) §4 的真值手法）：

```cpp
auto catal = rdma_hnsw::GraphStore::load_catalog(path);
const auto off = rdma_hnsw::node_layer_offset(catal, id, level);
const rdma_hnsw::RemoteNodeView v{id, gen, level, off, catal.layer_capacity(level)};

rdma_hnsw::ReadMetrics m1, m2;
rdma_hnsw::ReadContext c1{*t, &m1, 4, 100, {}}, c2{*t, &m2, 4, 100, {}};
rdma_hnsw::Baseline1FixedStrategy       s1;
rdma_hnsw::Baseline2HeaderFirstStrategy s2;

const auto r1 = s1.read(v, c1);
const auto r2 = s2.read(v, c2);
assert(r1.ok && r2.ok);
assert(r1.neighbors == r2.neighbors);                       // D6
assert(m2.wasted_bytes == 0);
// 恒等式④：B1 的浪费字节 == B1 与 B2 的请求字节之差（逐层精确成立）
assert(m1.requested_bytes - m2.requested_bytes == m1.wasted_bytes);
assert(m2.read_ops == m2.header_reads + m2.dependent_reads);
```

### T4 — 实验驱动扩展（P0，1 天）

**前置**：Baseline 1 的实验驱动已完成（[baseline1-plan.md](baseline1-plan.md) §T4）。

在驱动上做三件事：

1. **策略参数化**：`--read-strategy fixed|header_actual`，通过 `ComputeNodeConfig::read_policy` 传入，**不硬编码**；
2. **同一二进制内跑两个策略**（推荐）：顺序执行两轮，共用同一份查询集与同一份索引副本，最后打印对照表。这样 H5（结果逐条相同）可以在运行时直接断言：

```cpp
// 同一查询，两策略结果必须逐条相同
const auto ids_b1 = run_with(ReadPolicy::Fixed,        idx_path, q);
const auto ids_b2 = run_with(ReadPolicy::HeaderActual, idx_path, q);
if (ids_b1 != ids_b2) { /* 立即报错并打印该查询，便于定位 */ }
```

3. **依赖读取的延迟追踪**（DR-7，见 T6）。

**验收**：一条命令产出包含两个策略的对照 CSV。

### T5 — CMake 与文案（P0，0.25 天）

```cmake
add_executable(baseline_compare experiments/baseline_compare.cc)   # B1 vs B2
target_link_libraries(baseline_compare PRIVATE rdma_hnsw_runtime)
if(BUILD_TESTING)
  add_executable(read_strategy_equivalence_test tests/read_strategy_equivalence_test.cc)
  target_link_libraries(read_strategy_equivalence_test PRIVATE rdma_hnsw_core)
  add_test(NAME read_strategy_equivalence_test COMMAND read_strategy_equivalence_test)
  target_compile_options(read_strategy_equivalence_test PRIVATE
    $<$<CXX_COMPILER_ID:GNU,Clang>:-UNDEBUG>)
endif()
```

并把 [config.cc](src/common/config.cc#L148-L149) 的 usage 文案改成：

```text
  --read-strategy fixed|header_actual   built-in strategies; others via the API
```

### T6 — 依赖读取的延迟测量（P1，0.5 天）

"额外往返造成的延迟变化"是 §五.3 明确要求的一项，但核心库不提供。做法是**实验程序里的 transport 装饰器**（不进核心库，满足 D3）：

```cpp
class TracingTransport final : public rdma_hnsw::RemoteTransport {
public:
  // 记录每次 read 的 (offset, len, 耗时)，并维护一个"依赖深度"标记：
  // 策略内部连续两次 read 且第二次的地址依赖第一次的结果 → 归为同一条依赖链。
  // 简化做法：由实验程序在策略返回后统一汇总，把 header_reads 与 dependent_reads
  // 的耗时分别累加（本策略里二者一一对应，故可直接配对）。
};
```

更简单可靠的替代（**推荐先用这个**）：不做装饰器，直接用

```text
Δ延迟 = 延迟(Baseline2) − 延迟(Baseline1)
单位化：Δ延迟 / layer_reads      →  每次"额外往返"的平均代价
```

因为 Baseline 2 相对 Baseline 1 的操作数增量恰好等于 `dependent_reads`，所以 `Δ延迟 / dependent_reads` 就是一次依赖往返的实测代价——这个数在 RDMA 上应当落在 `R ≈ 1~3 µs` 量级，**与 §5.3 的模型直接对照**。

### T7 — 实验矩阵与图表（P0，1 天）

见 §10。必出四张图：

1. 邻居区域 `requested/useful/wasted`：B1 vs B2 堆叠柱状图（验证 H1）；
2. `read_ops` 与 `dependent_reads`：B1 vs B2（额外操作次数）；
3. `Δ延迟` 与 `Δ延迟/dependent_reads`：mmap vs RDMA 并排（验证 H2/H3）；
4. `saved_bytes` 随 `update_ratio` 变化（erase 曲线，验证 H4 与 §五.3 的动态要求）。

### T8 — WSL 与 RDMA 验证（P0，0.5 天）

见 §11。**核心要求：字节结论在 mmap 上出，延迟结论必须在 RDMA 上出。**

### T9 — 报告（P0，0.5 天）

Baseline 2 小节应包含：定义 → 实现要点 → 成本模型（§5）→ 实测 → 模型与实测的偏差解释 → 适用/不适用条件。

---

## 8. 指标口径与公式

### 8.1 单次层读取

```text
Baseline 2：
  requested       = 4 + 4·deg            （两次读之和）
  useful          = 4 + 4·deg
  wasted          = 0                    ← 恒等式，不需要算
  read_ops        = 1 + [deg > 0]
  header_reads    = 1
  dependent_reads = [deg > 0]
  zero_degree_layers = [deg == 0]
```

### 8.2 与 Baseline 1 的对照量

```text
saved_bytes      = B1.wasted = Σ 4·(cap − min(deg, cap))
extra_ops        = B2.dependent_reads
bytes_saving_pct = saved_bytes / B1.requested          （邻居区域内）
bytes_saving_tot = saved_bytes / (node_bytes + B1.requested)   ← 对总流量的真实影响
ops_penalty_pct  = extra_ops / B1.read_ops
latency_penalty  = (B2.latency − B1.latency) / B1.latency
rtt_per_dep_read = (B2.latency − B1.latency) / B2.dependent_reads    ← 与 R 对照
```

`bytes_saving_tot` 必须报——否则会把 0.3% 说成 36%。参看 [baseline1-plan.md](baseline1-plan.md) §6.3。

### 8.3 恒等式（写进测试）

```text
① requested == useful + wasted
② read_ops == header_reads + dependent_reads                    （Baseline 2）
③ dependent_reads + zero_degree_layers == layer_reads           （Baseline 2）
④ B1.wasted_bytes == B2_saved_bytes
   ⟺ B1.requested − B2.requested == 4·(cap − deg) 逐层成立
```

### 8.4 必须同时报告的 recall

B1 与 B2 **应当**返回完全相同的邻居集合，因此 recall 必须逐位相同。若不同，先修 bug 再谈性能（H5）。这也是 [experiment.md](experiment.md#L165-L167) "在相同 recall 下比较"的最省事形态：本 Baseline 天然满足。

---

## 9. 测试方案

见 §T3 的 10 个用例。原则：

1. **等价性优先**：B1 ≡ B2 是最强的正确性断言，比逐字段核对更有效；
2. **单层先测**：手工构造 `RemoteNodeView` + 真实索引文件，不启动 `ComputeNode`；
3. **畸形输入必测**：`degree > capacity`、`capacity == 0`、短读——这些是 §六.5 明示的检查项；
4. **记账恒等式当断言**：把 §8.3 的四条写成 `assert`，让口径错误在 CI 里就暴露，而不是在写报告时才发现。

---

## 10. 实验设计

### 10.1 固定主配置（与 Baseline 1 完全相同，才能横向比较）

```text
数据集 : SIFT1M（10K / 100K / 1M），128 维，L2
HNSW   : M = 16, ef-construction = 200
查询   : ef-search = 64, k = 10
对比   : Baseline1FixedStrategy vs Baseline2HeaderFirstStrategy
传输   : mmap（字节/正确性）→ RDMA（延迟/操作数）
```

### 10.2 矩阵

| 因子 | 取值 | 目的 |
| --- | --- | --- |
| 策略 | B1 / B2 | 主对照 |
| 索引规模 | 10K / 100K / 1M | degree 分布随规模变化 |
| `M` | 8 / 16 / 32 | 验证 `S = 4·cap = 8M` 的模型（H4） |
| 维度 | 32 / 128 | 验证 `bytes_saving_tot ≈ 1/(dim+6)` 的占比模型 |
| 更新操作 | 静态 / erase / upsert | `saved_bytes ∝ (cap − deg)` 的动态变化 |
| 更新比例 | 0% / 10% / 30% / 50% | §五.3 的"degree 变化对性能的影响" |
| 并发度 | 1 / 4（RDMA） | 检验 §5.3 的"高并发下 op 代价被流水化"猜想 |

### 10.3 每组必须记录的字段

```text
strategy, dataset, dim, node_count, M, ef_construction, ef_search, k,
threads, transport, update_op, update_ratio, queries,
qps, latency_mean_us, latency_p50_us, latency_p95_us, latency_p99_us,
neighbor_requested_bytes, neighbor_useful_bytes, neighbor_wasted_bytes,
saved_bytes_vs_b1, bytes_saving_pct_of_layer, bytes_saving_pct_of_total,
read_ops, header_reads, dependent_reads, zero_degree_layers, layer_reads,
node_bytes, total_requested_bytes,
ops_penalty_pct, rtt_per_dep_read_us,
avg_degree, capacity_utilization, recall
```

### 10.4 两个必须有结论的对照

**(a) 模型 vs 实测的往返代价。** `rtt_per_dep_read` 应落在 `R` 的合理范围（§5.3 取 1.5 µs 只是量级估算，RDMA 实测常见 1–3 µs）。偏差超过 2 倍就要解释：是不是发生了流水化？是不是 page cache/CPU 成了瓶颈？

**(b) 省字节最划算的区间在哪。** 扫 `M` 与 `degree`：`Δbytes` 随 `cap − deg` 线性增长，而 `Δops` 固定为每层 +1 → **存在一个 `d` 的下界，低于它 B2 反而更差**（因为 op 增幅 `1/(2+d)` 变大）。把这个交叉点算出来并画出来，就是本 Baseline 最有价值的结论。

---

## 11. 构建与运行（与 Baseline 1 的差异）

完整步骤见 [baseline1-plan.md](baseline1-plan.md) §7 与 [erdma.md](erdma.md)。Baseline 2 只需替换策略名。

### 11.1 WSL（只能验字节与正确性）

```bash
cmake -S . -B build -DENABLE_RDMA=OFF -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure

mkdir -p data out
./build/hnsw_build --generate 10000 --dim 128 --M 16 \
  --ef-construction 100 --seed 42 --output data/index128.bin

# Baseline 1
./build/compute_client --index data/index128.bin --transport mmap \
  --read-strategy fixed --queries-count 1000 --ef-search 64 --k 10 --seed 42

# Baseline 2（T1 完成后可用的名字；"header" 是等价别名）
./build/compute_client --index data/index128.bin --transport mmap \
  --read-strategy header_actual --queries-count 1000 --ef-search 64 --k 10 --seed 42
```

> ⚠️ **T1 完成之前**，`--read-strategy header_actual` 能通过 CLI 校验但会在工厂处抛出
> `read strategy is a student extension; implement and inject it before selecting header_actual`（[read_strategy.cc](src/read/read_strategy.cc#L63-L69)）。这正是现状，不是环境问题。

### 11.2 RDMA（延迟结论的唯一来源）

```bash
# 存储实例
./build/storage_server --index data/index128.bin --transport rdma \
  --rdma-connection cm --bind <存储IPv4> --port 18515 --ib-device erdma_0 --ib-port 1

# 计算实例：两个策略各跑一遍，参数完全一致
./build/compute_client --transport rdma --rdma-connection cm \
  --host <存储IPv4> --port 18515 --ib-device erdma_0 --ib-port 1 \
  --read-strategy fixed         --queries-count 1000 --ef-search 64 --k 10

./build/compute_client --transport rdma --rdma-connection cm \
  --host <存储IPv4> --port 18515 --ib-device erdma_0 --ib-port 1 \
  --read-strategy header_actual --queries-count 1000 --ef-search 64 --k 10
```

注意：`TransportStats` 的 READ/WRITE 计数**不含**控制面 SEND/RECV（[erdma.md](erdma.md#L162)），所以跨策略比较时必须用策略内的 `ReadMetrics::read_ops`，不要用 `TransportStats`。另外单次读 > 1 MiB 会拆成多个 WR；Baseline 2 的两次读都很小，各占 1 个 WR。

### 11.3 对比驱动

```bash
./build/baseline_compare --index data/index128.bin --transport mmap \
  --queries-count 1000 --ef-search 64 --k 10 \
  --out out/b1_vs_b2_dim128.csv
```

---

## 12. 风险登记

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 在 mmap 上得出"往返代价"结论 | 结论错误、误导 | §0③ 的分工原则；mmap 只报字节/op/正确性（H3 用来确认这一点） |
| 为了提高性能把两次读合并 | 违反 D5，实验失去含义 | 代码评审重点核查该文件；H1 的字节数下降本身就是"确实分两次"的证据 |
| `read_ops` 口径不含 `degree == 0` 的例外 | B1/B2 的 op 对比被污染 | DR-4 + `zero_degree_layers` 字段 + §8.3 恒等式②③ 断言 |
| B2 与 B1 结果不一致 | 正确性 bug 被当成性能差异 | H5：同一查询集逐条比对，先过这一关再看性能 |
| 只报 `bytes_saving_pct_of_layer` | 把 0.3% 说成 36% | 必须同时报 `bytes_saving_pct_of_total` |
| 用 `TransportStats::read_ops` 做跨策略对比 | 口径不一致（缺 SEND/RECV、大读拆分） | 统一用 `ReadMetrics::read_ops` |
| 动态 workload 下忘记 `reload()` 清零统计 | 数据只覆盖最后一段 | 复用 [baseline1-plan](baseline1-plan.md) §T4 的累加器 |
| 结论"B2 更差"被当成失败 | 报告缺亮点 | §5 的模型把它变成可解释的定量结论：给出交叉点与适用条件 |

---

## 13. 附录

### 13.1 关键代码位置

| 用途 | 位置 |
| --- | --- |
| Baseline 2 要实现的接口 | [read_strategy.h](include/rdma_hnsw/read/read_strategy.h#L48-L55) |
| 层偏移（指向 degree 字段） | [graph_store.h](include/rdma_hnsw/graph/graph_store.h#L75-L83) |
| 层容量 | [graph_store.h](include/rdma_hnsw/graph/graph_store.h#L46-L48) |
| `decode()` 可复用 | [read_strategy.cc](src/read/read_strategy.cc#L9-L16) |
| 工厂（要加分支） | [read_strategy.cc](src/read/read_strategy.cc#L63-L69) |
| `header_reads` / `dependent_reads` | [read_strategy.h](include/rdma_hnsw/read/read_strategy.h#L14-L15) |
| `merge()`（要加字段） | [read_strategy.cc](src/read/read_strategy.cc#L50-L62) |
| CLI 白名单（**不用改**） | [config.cc](src/common/config.cc#L68-L75) |
| usage 文案（要改） | [config.cc](src/common/config.cc#L140-L156) |
| 查询主流程（禁止修改） | [query_engine.cc](src/runtime/query_engine.cc#L66-L123) |
| 节点读取（已在读 24 字节 header） | [query_engine.cc](src/runtime/query_engine.cc#L47-L64) |

### 13.2 三个策略的共享语义表（跨计划一致）

| 情形 | B1 fixed | B2 header_actual | B3 cached_length |
| --- | --- | --- | --- |
| `capacity == 0` | 错误，0 次读 | 错误，0 次读 | 错误，0 次读 |
| `degree > capacity` | 截断到 cap | 截断到 cap | 截断到 cap，并按 fallback 处理 |
| `degree == 0` | 读满 cap（有浪费） | 只读 4 字节 | 只读预测范围 |
| 短读/失败 | 错误 | 错误 | 错误 |
| `wasted_bytes` | `4·(cap−deg)` | **0** | `4·(hint−deg)` 的向上取整部分 |
| `dependent_reads` | 0 | 每层 1 | 每层 1（fallback 时更多） |

### 13.3 完成检查清单

- [ ] `Baseline2HeaderFirstStrategy` 具名、有 `name()` 与注释（D1）
- [ ] `--read-strategy header_actual` 可用；usage 文案已更新（D2）
- [ ] `QueryEngine::search()` 未被修改；策略内无 ibverbs（D3）
- [ ] header 短读、degree 截断、第二次读失败、`capacity==0`、`degree==0` 全覆盖（D4）
- [ ] 确认是两次独立读取，无合并/预读（D5）
- [ ] 与 Baseline 1 逐邻居一致的测试通过（D6）
- [ ] `header_reads` / `dependent_reads` / `zero_degree_layers` 已在 T2 中激活
- [ ] §8.3 四条恒等式写成断言并通过（D7）
- [ ] `baseline_compare` 一条命令产出 B1/B2 对照 CSV（D7）
- [ ] erase/upsert 下 `saved_bytes` 随更新比例变化的曲线（D8）
- [ ] RDMA 上测出 `rtt_per_dep_read`，与 §5.3 的 `R` 对照（D7）
- [ ] 报告同时给出 `bytes_saving_pct_of_layer` 与 `bytes_saving_pct_of_total`
- [ ] 报告给出"省字节最划算的 `d` 下界"与适用/不适用条件
- [ ] 构建命令、运行参数、原始 CSV、统计脚本齐备（D9）
