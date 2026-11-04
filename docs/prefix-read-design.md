# 分级前缀读取机制设计

本文依据 [experiment.md](experiment.md)、[rdma-baseline-report.md](rdma-baseline-report.md)、三个 Baseline 的计划与报告，以及现有策略代码设计。状态：**最小机制已实现并通过正确性门禁；已完成首轮 10K mmap 与 100K RDMA（缩减协议）扫描，结果见 [10K 报告](prefix-read-report.md) 和 [100K 报告](prefix-rdma-100k-report.md)**。不改变索引格式、HNSW 搜索流程或 RDMA Verbs 接口。

## 1. 实验依据与研究问题

### 1.1 证据的使用顺序

性能依据优先使用 [RDMA 统计表](../out/rdma_report_tables.md) 及其原始 [静态 CSV](../out/rdma/static_summary.csv)、[混合 CSV](../out/rdma/mixed_summary.csv)。[RDMA smoke 表](../out/rdma_smoke_tables.md) 仅说明测量链路可运行，不用于稳定性能结论。mmap 报告用于结构性计数、容量敏感性和测试设计，不与 RDMA 绝对延迟横比。

当前工作区没有 mmap 报告提到的全部历史原始输出，不能声称已重新核验那些实验。RDMA 报告正文与自动统计表的部分混合负载 p50/QPS 不一致，例如 erase/0.1/B1 的 p50 分别为 4343.2 和 4333.0 微秒；不能混合引用两种统计口径。

| 观察 | 数据 | 对设计的约束 |
| --- | --- | --- |
| B2 减少字节但增加依赖读取 | 静态邻居字节约 -16%，邻居 ops 约 +98.6%，p50 约 +3.6% | 首要优化对象是依赖 READ，不是只看字节 |
| 邻居区只占总读字节约 0.92% | B1 约 9.1 KB 邻居、979 KB 节点数据/query | 新机制不应承诺显著端到端加速 |
| B3 已接近 B1 的操作数 | 静态总 READ 比 B1 多约 0.25% | 静态大缓存下进一步优化空间很小 |
| 冷启动与缓存容量确实影响操作数 | RDMA 正式静态 B3 查找命中率 94.9%；短 smoke 仍有较多冷启动读；mmap 容量扫描存在明显淘汰 | 冷、热缓存分开报告；扫描容量必须覆盖工作集以下 |
| upsert 的补读很短 | 每个重复的 ratio=0.5：2036 次、11208 B，约 5.50 B/次 | 用少量前缀余量减少短尾补读是值得验证的假设 |
| erase 主要造成高估 | B3 erase fallback 为 0，仍有过读 | 不能只升级 hint 而永不回落 |

混合 CSV 中 upsert/0.1、0.3、0.5 的 fallback 分别为 507、1439、2036 次，每个重复一致；对应字节为 2452、7528、11208 B。报告中的 63564 B 是这九次运行的总和，不是单次 ratio=0.5 的补读量。这里的频率和字节分布不是新机制的收益预测：缺少逐次旧 degree、新 degree 和访问顺序，不能据此计算任何 G 的确切收益。

### 1.2 可证伪的核心假设

**把上一次真实 degree 向上量化为 slot 前缀，可以吸收量化桶内的小幅增长，从而减少 B3 的低估补读；代价是可度量的过读。**

这不是提高缓存查找命中率的机制。同容量、同 LRU、同访问序列下，B3 与新机制的查找命中率应相同。需要提高的是“首次 READ 足够率”。若补读没有减少，或节约的依赖 READ 不足以抵消过读和 CPU 成本，该配置即不成立。

特别注意：当前 upsert 会把目标邻接表填到 capacity。若旧 degree 为 25、capacity=32，则 G=4 的宽度是 28，仍然需要补读；不能从平均补读约 5.5 B 推断 G=4 会消除大部分 fallback。

## 2. 基础机制：QuantizedPrefixStrategy

建议类名 `QuantizedPrefixStrategy`，`name()` 返回 `quantized_prefix`，由已有 `ReadPolicy::Prefix` 和 `--read-strategy prefix` 独立选择。基础版固定全局 G，**不加入自适应、预取、共享线程缓存或额外升级余量**，以便解释 G 的独立作用。

### 2.1 参数与缓存

| 项 | 规则 |
| --- | --- |
| 粒度 G | 正整数，初始候选默认 4；不是已证实最优值 |
| 层容量 C | 使用 `RemoteNodeView::capacity`；L0 为 2M，高层为 M |
| 最大级数 | `Lmax = C / G + (C % G != 0)` |
| 冷启动 | 无条目时初始级数 1，宽度 `h=min(G,C)` |
| 已缓存级数 l | 合法范围 `[0,Lmax]`；0 表示上一次真实 degree 为 0 |
| 读取宽度 | `h=min(l*G,C)`；实现采用检查乘法或饱和运算防溢出 |
| 缓存键 | `(NodeId,level)`，沿用 `length_cache_key()` 的 64 位编码；不缓存远程地址 |
| 缓存值 | `level_hint`、`last_degree`、LRU 位置；last_degree 供指标归因，不参与一致性判断 |
| 更新规则 | 完整成功后令 `l=d/G+(d%G!=0)`；允许下降，d=0 写入 l=0 |
| 淘汰 | 与 B3 相同的严格 LRU，每次完整成功后 touch/put |
| 容量 0 | 明确定义为禁用缓存，每次都走一级前缀冷启动，不隐式改用默认容量 |
| 生命周期 | 驱动持有独立 `shared_ptr<PrefixCache>`，跨更新导致的 reload 和策略重注入存活 |

级数与精确 degree 不混存在 B3 的 `LengthCacheEntry::degree` 中，避免“同一字段两种单位”。G 在一轮运行中固定；修改 G 必须清空前缀缓存。普通更新不按 generation 清空；切换到无关索引或改变 ID 命名空间、布局契约时应新建缓存。

保留 last_degree 意味着基础实现**不宣称缓存压缩收益**。LRU、哈希节点、分配器与指针开销可能远大于 hint 本身；需要实测 bytes/entry。若未来删除 last_degree、压缩级数，则作为单独消融，不与基础算法混在一起。

### 2.2 读取协议

设层起始地址为 a，布局是 `uint32 degree + NodeId slots[C]`，当前两种字段均为 4B。

1. 本地查缓存；有条目使用缓存级数，无条目使用初始级数 1。禁止预读 generation 或 degree 来判断缓存有效性。
2. 第一次 READ：地址 a，长度 `4+4h`。一次取得 degree 和 h 个 slot；必须验证传输成功且长度完整。
3. 解析真实 d 并校验容量。正常输入要求 `0<=d<=C`；与现有 Baseline 对齐的兼容模式可用 `min(raw_degree,C)`，但必须额外计数畸形 degree，不能把截断解释成损坏数据的正确恢复。
4. 若 `d<=h`，只返回前 d 个邻居；其余 slot 计入过读。
5. 若 `d>h`，第二次 READ 从 `a+4+4h` 开始，长度 `4(d-h)`；将首次的 h 个邻居与尾段按原顺序拼成 d 个邻居。不逐级重试，不重读已取得前缀。
6. 在全部字节和邻居合法性检查通过后，更新级数与 LRU。任一失败不返回部分邻居，不更新 hint，不留下新条目。

| 状态 | 缓存查找 | 数据条件 | READ 数 | 计数分类 |
| --- | --- | --- | --- | --- |
| 冷启动足够 | miss | d<=min(G,C) | 1 | miss、cold-prefix success |
| 冷启动不足 | miss | d>min(G,C) | 2 | miss、cold fallback，不算缓存失效 |
| 热缓存足够 | hit | d<=h | 1 | hit、single-read success |
| 热缓存低估 | hit | d>h | 2 | hit、cached underestimate/fallback |

每个正常层访问恰好一次首读、至多一次依赖补读。冷启动补读和热缓存补读均属于实际 fallback，但必须分列；旧 B3 的 `cold_start_reads` 是 header-first 的第二次读，不能不加说明直接改成新语义。

### 2.3 示例与边界

G=4、C=32，上次 d=21，缓存 l=6、h=24。新 d=23 时一次读 100 B，返回 23 个邻居，浪费 4 B；新 d=25 时首读 100 B，尾读 4 B，返回全部 25 个邻居，缓存提升到 l=7。

若 C=18、G=8，最大级数为 3，最大前缀宽度为 18 而非 24。G>C 时宽度直接饱和到 C。C=0 是无效层，零次读并报错；G=0 在配置阶段拒绝。空层冷启动会过读 min(G,C) 个 slot，但写入 l=0 后后续只读 4B；若随后增长，再由尾读恢复。

## 3. 正确性与并发边界

### 3.1 长度与结果完整性

在**两段读取属于同一稳定版本邻接表**的前提下：d<=h 时前 d 个 slot 都已取得；d>h 时首段覆盖 `[0,h)`，尾段覆盖 `[h,d)`，不重不漏。缓存完全错误也仅影响长度和操作数，不影响返回邻居的完整性。

所有地址加法、字节乘法必须检查溢出和远端区域边界。不能只验证 degree：输出 NodeId 还应在当前 catalog 中可定位；非法 ID 报错，不静默丢弃而伪称完整。现有 `RemoteNodeView` 不含 catalog，策略层不能凭它验证全部 ID，需通过 ReadContext 增加只读 catalog/验证器，或在查询层已有消费边界明确检查；只检验数值范围不等于稀疏 ID 存在性检查。

传输失败或短读直接返回 error，不在算法内部无限重试。生产级恢复如重连、刷新 catalog、重做整次查询应在调用侧有界执行，并单独记录恢复代价，不混入正常两读上限。

### 3.2 degree 校验不能替代快照协议

禁止 generation 预读，是指不能用远端版本检查替代缓存预测。它**不意味着**“degree 检查已经解决并发一致性”：两次 READ 之间发生邻居重排，即使 degree 没变，也可能拼出跨版本结果；单次大 READ 对整张表也不天然具有事务原子性。

现有 IB LID 服务会将更新后整份文件读回会话注册缓冲区；当前混合驱动是同一线程先完成 query 再 update，故可用于串行交错更新测试，不能据此证明并发原地更新安全。CM 后端具有会话 snapshot 机制，但也必须保证查询期间不由同一会话的另一线程切换 descriptor/MR，并明确旧快照何时释放及跨会话可见性。

基础验收采用：每个 ComputeNode/策略实例只在一个线程中使用；同一会话的 query/update 串行，或者由存储层提供整个查询期间固定的不可变快照。多客户端静态吞吐可独立测量，**不等于多线程共享缓存或并发动态更新实验**。

真正并发更新的验收门槛是先具备快照固定及安全回收，或另行设计有界版本重试协议；需要额外 READ 的版本校验必须计入成本，并对所有策略采用相同一致性等级。此项是传输/存储协议前置工作，不由前缀缓存自行解决。

## 4. 精确账本与成本模型

### 4.1 成功层访问的字节恒等式

设首读宽度 h、真实度数 d，尾部补读，不重读前缀：

```text
first_bytes    = 4 + 4h
fallback_bytes = 4 max(d-h,0)
requested      = 4 + 4 max(d,h)
useful         = 4 + 4d
wasted         = 4 max(h-d,0)
ops            = 1 + [d>h]
requested      = useful + wasted
```

这里 useful 沿用项目口径，包含 degree 的 4B；另设 `useful_neighbor_bytes=4d` 可避免将 header 误称为邻居 payload。

**过读不总是小于 G 个 slot。** 热缓存且真实 d 没变时，纯取整余量最多 G-1；动态删除后 h-d 可以远大于 G-1，最大接近 C。成功读后立即回落级数，限制这种历史高估的持续时间，但不能消除本次浪费。

若缓存 last_degree=s、h 为其向上取整宽度，可把本次过读 slot 分为：

```text
drift_waste_slots    = max(s-d,0)
rounding_waste_slots = max(h-max(s,d),0)
total_waste_slots    = drift_waste_slots + rounding_waste_slots
```

该分解在合法 `s<=h` 下精确成立。冷启动没有历史 s，单列 `cold_prefix_waste_slots=max(h-d,0)`，不冒称历史取整开销。重新约束缓存值后仍不能满足 s<=h 时，应报缓存状态异常。

### 4.2 与 B3 的比较

令 p 是缓存查找命中比例；`q3=P(hit 且 B3 低估)`、`qG=P(hit 且分级低估)` 均以所有层访问为分母，**不是条件概率**；`z=P(miss 且 d=0)`；`rG=P(d>min(G,C) | miss)`。

```text
E[ops_B3] = 2 - p - z + q3
E[ops_G]  = 1 + (1-p) rG + qG
saved_ops = (1-p)(1-rG) - z + q3-qG
```

在同一访问轨迹、同 LRU、相同成功更新规则下，热缓存 hG>=hB3，故 qG<=q3。静态非零且 miss degree 通常大于 G 时，rG 接近 1、两者无低估，操作数基本相同，但分级多读字节；这正是必须接受的负结果。

近似时间模型为 `T = ops * alpha + requested / B_eff + CPU_cache_decode`。alpha 是实测 READ 固定成本，不直接把 B2-B1 延迟差视为网络 RTT。选择 G 的条件是节约操作的时间超过增加字节及 CPU 的代价；端到端节点区、控制面与更新时间需另外统计。

G=1 只在**热缓存宽度**上等价于精确 degree；冷启动为一个 slot，与 B3 的 header-first 并非完全相同。G>=该层 C 时，所有非零历史 degree 和冷启动都读取全容量，但曾缓存空层后增长仍可能补读，因此不能无条件宣称等价于 B1。

## 5. 指标契约与实现落点

### 5.1 指标

保留现有 `ReadMetrics` 字段含义。新策略追加独立字段，显式区分 cached/cold fallback；不要套用 B3 的 `cache_misses==header_reads` 等恒等式。

| 类别 | 必需指标 |
| --- | --- |
| 请求/结果 | layer attempts、completed layers、read attempts、successful reads、failed reads |
| 缓存 | lookup hits/misses、evictions、level updates、level up/down，按层细分 |
| 前缀 | first reads、first slots sum、observed degree sum、同一批样本的 h-d 分布 |
| 补读 | cached underestimates、cold fallbacks、两类 fallback bytes、dependent reads |
| 浪费 | rounding/drift/cold prefix waste bytes、总 wasted bytes |
| 系统 | node reads/bytes、transport READ/WRITE ops/bytes、控制面与 catalog refresh 开销 |
| 性能/质量 | query-only QPS、wall-time QPS、mean/p50/p95/p99、update latency、recall、失败率 |

成功且无恢复的统计区间应满足：

```text
cache_hits + cache_misses = completed_layers
prefix_reads = completed_layers
fallbacks = cached_fallbacks + cold_fallbacks
read_ops = prefix_reads + fallbacks
dependent_reads = fallbacks
requested_bytes = useful_bytes + wasted_bytes
wasted_bytes = rounding_waste_bytes + drift_waste_bytes + cold_prefix_waste_bytes
```

查找命中率为 `hits/(hits+misses)`；首次读足够率为 `1-fallbacks/completed_layers`；缓存低估条件率为 `cached_fallbacks/hits`；全局低估率为 `cached_fallbacks/completed_layers`。分母为零输出 NA，不能输出伪造的 100% 命中。

失败路径另记所有提交 READ 的请求长度、成功接收字节和失败阶段；完整结果账本只在 completed 层上结算。失败实验不要求 `requested=useful+wasted`，也不得丢弃失败操作而声称网络成本不变。现有策略失败计数口径并不完全统一，比较主表先要求所有策略 failed_reads=0，故障测试另表报告。

### 5.2 最小实施范围

| 文件 | 后续改动 |
| --- | --- |
| [read_strategy.h](../include/rdma_hnsw/read/read_strategy.h) | 新类、独立 PrefixCache 与追加指标；不复用旧 NodeId 单键缓存 |
| [read_strategy.cc](../src/read/read_strategy.cc) | 量化、首读/补读、成功提交缓存、merge、Prefix 工厂分支 |
| [baseline1_main.cc](../experiments/baseline1_main.cc) | prefix 白名单、标签、共享缓存注入、差量/汇总/CSV/恒等式 |
| [query_engine.cc](../src/runtime/query_engine.cc) | 只核实参数和结果消费边界，不改 search 算法隐藏策略差异 |
| [read_strategy_cached_test.cc](../tests/read_strategy_cached_test.cc) | 复用现有 FakeTransport 思路；按本仓测试组织决定同文件扩展或独立 prefix 测试 |
| [CMakeLists.txt](../CMakeLists.txt) | 注册有针对性的 prefix 正确性与 smoke 测试 |
| [run_rdma_baselines.sh](../scripts/run_rdma_baselines.sh)、[analyze_rdma_baselines.py](../scripts/analyze_rdma_baselines.py) | 已扩展四策略轮转配对、`schema_version` 校验、prefix 恒等式/代价模型检查和粒度汇总；尚无 RDMA 实测 |

`prefix_granularity` 传递链路已贯通配置、`QueryEngine` 与实验驱动；工厂已实现 `ReadPolicy::Prefix`，驱动可输出实际策略名、G、缓存限制和禁用状态，并在所有入口拒绝 G=0。代码可运行不等于研究假设已被验证：性能测量仍按第 6 节执行。

## 6. 实验与验收

### 6.1 先做最便宜的判别实验

先通过 FakeTransport/小图枚举验证机制，再在现有 10K 数据上做 G 扫描。建议采集只用于离线分析的 `(node,level,C,last_degree,h,d,hit,read_lengths)` 轨迹，计时实验关闭详细 trace。离线回放可预测各 G 的 READ/字节计数，但不能预测真实 RDMA 时延。

首轮 10K mmap G 扫描已按本节流程完成，结果见 [prefix-read-report.md](prefix-read-report.md)；100K RDMA 缩减协议也已完成，见 [prefix-rdma-100k-report.md](prefix-rdma-100k-report.md)。轨迹采集与离线回放仍未实现。

需有一个可直接证伪的正向例子：G=4，缓存精确 d=21 后增长到 23，B3 应补读、分级不补读；再有 25->32 的反例，G=4 仍补读。静态大缓存下允许观察到分级与 B3 操作数相同、字节更高，不能只保留有利 workload。

### 6.2 正确性门禁

覆盖全部节点/层的 B1/B2/B3/prefix 邻居序列一致，以及冷/热两种缓存；边界包括 d=0、G-1、G、G+1、C，C 非 G 整数倍、G>C、G=1、G=0、C=0、l=0 后增长。检查跨层隔离、LRU 淘汰、禁用缓存、跨 reload 保留与 G 改变清空。

对首读与尾读分别注入失败和短读，验证不返回部分结果、不更新缓存、重试后正确；测试同 degree 但邻居内容变化仍读取真实数据、generation 变但 degree 不变不预读、generation 不变但 degree 增大必补读；注入异常 degree、非法 ID、地址/长度溢出。

并发测试必须先满足第 3.2 节的一致性前置条件；故意在两段读取间更换邻居的测试应证明底层快照协议有效，不能仅比较 d。

### 6.3 统一性能配置

四策略使用相同数据、查询、索引参数、更新 ID/向量/顺序、初始快照和预热。每轮恢复独立索引，轮转策略顺序并做多轮独立重复；置信区间以重复运行为单位，不能把相关的同一运行内查询全部当作独立重复。

| 维度 | 最低配置 |
| --- | --- |
| G | 1、2、4、8、16；可加 G=C 观察饱和端点 |
| 缓存 | 10K 开发集先用 100、1000、10000 条；另做相同实际内存预算比较 |
| 更新 | 0%、10%、30%、50%，分别测 erase、upsert，并补 insert 正确性 |
| 分布 | random、hot；roundrobin 保留作与旧报告复现对照 |
| 并发 | 1、4 个独立客户端先做静态；动态并发需快照协议先验收 |
| 规模 | 10K 开发、100K、1M；主数据 SIFT，扩展数据遵循课题要求 |
| 质量 | 固定查询、暴力/标准 GT；动态 GT 必须随可见数据及删除状态更新 |

现有 `update_ratio` 实际通过 `floor(1/ratio)` 查询间隔调度，0.3 在 500 查询中变成 167 次更新，是约 0.334 updates/query，并非更新占全部操作 30%。新实验必须输出 `updates/queries` 和 `updates/(updates+queries)`，若目标是严格操作占比，应使用预生成操作序列；四策略回放相同序列，不悄悄与旧结果混用。

当前驱动先独立 warmup，再测完整查询集；分析脚本又去掉 `query_idx<200` 的测量样本，而 summary 的计数仍可能覆盖完整测量集。新结果必须在同一统计窗口计算延迟、ops、字节和缓存比率，分别输出冷启动窗口与稳态窗口，避免跨窗口解释收益。

现有合成数据 `recall=0` 表示未提供真值，不是召回率实测为零。必须保留结果序列等价验证，并对主性能实验提供有效 recall；仅通过计数恒等式不证明搜索质量。

### 6.4 成功与失败判据

实现成功：独立可选，读取结果完整，失败不污染缓存，无 generation 预读，正常层访问至多两次 READ，所有门禁通过。

研究假设成功：至少一个有代表性的动态配置相对 B3 显著减少依赖 READ，同时报告新增浪费、CPU/缓存内存和端到端质量；性能是否显著改善单独判断。若仅减少邻居 READ 而总 QPS 无显著变化，也应如实报告。

默认 G=4 只是可复现起点。根据独立调参 workload 的吞吐、p99 与字节预算选 G，再用保留 workload 验证，不能在同一测试集上挑最好数字。自适应粒度、升降级滞后和热点专属余量仅在基础版完成后分别消融。

## 7. 设计结论

本机制定位为 **B3 的量化余量版本，加一级前缀冷启动**：用受控但并非始终小于 G 的过读，交换更少的低估依赖 READ。它不比 B1 更少于一次首读，不天然提高缓存查找命中率，也不解决索引发布和跨版本拼接问题。

当前 RDMA 结果支持“优化短尾补读值得实验”，不支持“分级必然优于 B3/B1”。最有信息量的下一步是实现固定 G 的最小机制，先验证逐次计数预测，再开展相同 recall、明确快照语义的 RDMA 对比。

首轮 10K mmap 证据（[报告](prefix-read-report.md)第 3–6 节）进一步收紧了结论：静态查询下 G<16 与 B3 操作数相同、G=32 接近 B1 行为；upsert 0.1/0.3/0.5 的 B3 低估只有 0.02–0.03 次/query，分级最多把它降到 0，节省被 14–16 次/query 的冷启动补读和 38–712 B/query 过读淹没。因此固定 G=4 在本数据上不成立；是否继续取决于目标 workload 是否存在“精确 degree 频繁小幅低估”的场景，并需在 RDMA 与更大规模数据上复核。

100K 真实 RDMA（单机 loopback，缩减协议，[报告](prefix-rdma-100k-report.md)第 4–9 节）给出同方向结果：G=1–8 与 B3 的静态 ops/query 相同（93.73，缓存 100000），G=16 只少 2.82 次，G=32 降到 75.04（与 B1 相同）但多 893.8 B/query；混合 0.5 下 B3 为 129.04，G=16 为 122.17，G=32 为 74.51。G=32 是 READ 下限端点，但相对 B1 没有读取模式上的优势；中间 G 没有优于 B3 的证据。