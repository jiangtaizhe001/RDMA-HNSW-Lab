# 100K 索引下分级前缀与三个 Baseline 的真实 RDMA 实验计划

状态：**已按第 9 节缩减协议执行完成；结果见 [prefix-rdma-100k-report.md](prefix-rdma-100k-report.md)**。原计划的全量 5 次/5000 查询与完整混合矩阵未执行。

## 1. 目标与依据

固定节点数 **100000**、维度 **128**、**M=16**，在真实 verbs/RDMA READ 数据路径上比较 B1、B2、B3 与不同 G 的分级前缀策略（QP）。回答三个独立问题：

1. 哪个 G 的邻居 READ 次数最少，减少来自冷启动还是缓存低估？
2. 哪个 G 的 query-only QPS、p50/p95/p99 最好，是否具有可重复的收益？
3. 节约 READ 所付出的字节、CPU 和缓存内存成本是否值得，是否优于直接使用 B1/B3？

依据为 [10K RDMA Baseline 报告](rdma-baseline-report.md)、[10K mmap 前缀报告](prefix-read-report.md) 和 [机制设计](prefix-read-design.md)。前者用于确定真实 RDMA 方法与基线，后者只提供计数和测试线索，不能提供 RDMA 时延结论。

需要避免沿用旧报告中的过强推断：

- 10K 上 G=32 的 READ 最少，不等于 100K 上端到端效率最高。
- 缓存 10000 与 100000 的有限轨迹计数相同，不能证明 miss 来自淘汰；必须分别记录首次访问、重复访问 miss 和 eviction。
- QP 的 cold fallback 与 B3 的 header-first 第二读是可比较的依赖开销；不能把 QP 的冷启动补读全部解释为相对 B3 的新增操作。
- 不能将同一次运行内的相关查询当作独立重复，用很窄的逐查询置信区间宣称显著收益。

## 2. 环境与数据固定

| 项 | 计划配置 |
| --- | --- |
| 传输 | `--transport rdma --rdma-connection ib`，禁止 mmap 自动替代 |
| 初始环境 | 沿用 `mlx5_0`、IB port 1、TCP 控制面端口 18515；执行前重新核验 |
| 拓扑 | 当前机器若只能同机连接，明确标为单机 HCA loopback；两机结果另表，不能混合 |
| 节点数 / 维度 / M | 100000 / 128 / 16 |
| 索引参数 | L2、`ef_construction=200`、seed=42 |
| 查询参数 | `ef_search=64`、`k=10` |
| 主数据 | 与前轮一致的合成均匀数据，固定生成器、版本和种子；不冒称 SIFT |
| 文件 | `data/index_n100000_d128_M16.bin`，只读原始快照，记录 SHA-256 |
| Python / 编译 | 通过 conda `.venv` 调用，记录 Python、NumPy、Matplotlib、编译器和 CMake 版本 |
| 构建 | `ENABLE_RDMA=ON`，IB 后端；不为本实验切换 CM 或改变一致性模型 |

先核验设备、PORT_ACTIVE、LID、链路速率、MTU、可注册内存限制、可用内存、CPU/NUMA 与进程绑定方式。保存完整启动参数、Git commit、构建选项和服务日志。不预设上次的硬件状态仍有效。

索引大小预计约为 10K 的十倍，即约 90--100 MB，实际大小、层数、节点记录布局和注册内存量以新索引为准。不能将节点数扩大十倍直接解释为查询耗时或访问节点数扩大十倍。

保存固定 base、query 与 update 输入及其哈希。计时查询与更新不得因策略或 G 重新生成不同序列。原始索引在每阶段前后校验哈希；混合实验必须使用副本，禁止直接更新主文件。

### 2.1 正确性与 recall

执行前用同一 100K 索引验证全节点、全有效层的邻居序列一致，覆盖 B1/B2/B3 和六个 G 的冷/热路径。实际 RDMA smoke 再验证确定的查询结果序列，错误必须使实验失败，不能只依赖成功 READ 计数。

固定独立的 1000 条质量验证查询，离线计算 100K base 的精确 top-10，排除 GT 计算时间。静态阶段四类策略结果序列和 recall@10 应一致。若复现生成器无法导出同一 base，应先补输入导出，不得以 `recall=0` 代表质量通过。

混合阶段在相同更新检查点用当前可见数据计算 GT，erase 排除删除节点，upsert 使用最新向量；分别记录结果序列与 recall。只验证检查点时，明确质量覆盖范围，不声称逐查询动态 recall。

## 3. 理论模型与可证伪预期

### 3.1 单层精确成本

L0 容量 C=32，高层 C=16。degree 与 NodeId 均为 4 B。QP 热缓存宽度为 `h=min(G*ceil(s/G),C)`，s 是上次真实 degree；冷启动为 `h=min(G,C)`。s=0 时热缓存 h=0。

```text
B1: ops = 1; requested = 4 + 4C
B2: ops = 1 + [d>0]; requested = 4 + 4d
B3 hit: h = s; ops = 1 + [d>s]
B3 miss: ops = 1 + [d>0]; requested = 4 + 4d
QP: ops = 1 + [d>h]
QP requested = 4 + 4max(d,h)
QP useful = 4 + 4d
QP wasted = 4max(h-d,0)
```

在 d=25 的 L0 上，假设静态热缓存 s=25：

| 策略 / G | 冷启动 READ / B | 热缓存 READ / B |
| --- | --- | --- |
| B1 | 1 / 132 | 1 / 132 |
| B2 | 2 / 104 | 2 / 104 |
| B3 | 2 / 104 | 1 / 104 |
| QP G=1 | 2 / 104 | 1 / 104 |
| QP G=2 | 2 / 104 | 1 / 108 |
| QP G=4 | 2 / 104 | 1 / 116 |
| QP G=8 | 2 / 104 | 1 / 132 |
| QP G=16 | 2 / 104 | 1 / 132 |
| QP G=32 | 1 / 132 | 1 / 132 |

该表只是给定 degree 的算例，不是 100K 的均值预测。高层 G>=16 已饱和；G=32 覆盖所有层容量。静态访问已缓存空层时 QP 仍只读 4 B，不能无条件宣称 G=32 与 B1 完全等价。动态空层增长仍可能触发补读。

### 3.2 平均操作数与时间模型

令 p 为查找命中率，q3/qG 为“hit 且低估”占全部层访问的比例，z 为“miss 且 d=0”占全部层访问的比例，rG 为 miss 条件下 `d>min(G,C)` 的概率：

```text
E[ops_B3] = 2 - p - z + q3
E[ops_QP] = 1 + (1-p)rG + qG
saved_ops = (1-p)(1-rG) - z + q3 - qG
T_neighbor ≈ alpha * ops + requested / B_eff + CPU_cache_decode
```

以 B3 为对照，若每查询减少 S 次 READ、增加 D 字节和 CPU 时间 delta_cpu，收益条件为 `alpha*S > D/B_eff + delta_cpu`。端到端还包括节点/向量读取、查询计算、控制面及更新，不能仅由邻居区模型宣布加速。

在同一设备上用独立微基准扫描 4/8/20/36/68/100/116/132/536 B 的串行 READ，估计固定成本 alpha 与小消息有效带宽；另测大块传输以区分带宽端点。记录每档至少 5 个批次，不用 B2-B1 的整次查询差反推纯 RTT。

非计时运行采集逐层 `(query_idx,node,level,C,last_degree,h,d,hit,read_lengths)` 轨迹，使用同一访问序列与严格 LRU 离线回放不同 G。先预测 READ/字节，再与独立运行比较；只用本次 fallback 反推本次 ops 属于恒等式，不属于模型预测验证。

### 3.3 预注册假设

- H1：静态且工作集被缓存覆盖时，中间 G 基本不降低热缓存 READ，却增加过读。
- H2：缓存受限时，大 G 主要减少 miss 后的依赖 READ；G=32 是 READ 次数下限的对照端点，而非预定的延迟赢家。
- H3：动态小幅 degree 增长时 qG 可能低于 q3；若 upsert 主要直接填满容量，G=4 可能仍无实用收益。
- H4：扩大 N 不改变 C；变化应通过实际度数、访问工作集、miss 与节点区流量解释，不预设 G 最优值随 N 增大。

## 4. 策略与实验矩阵

共 **9 个配置**：B1 fixed、B2 header-first、B3 cached-length，以及 QP G=1/2/4/8/16/32。每轮 G 固定，每次运行新建缓存，禁止不同 G 共用旧缓存。所有策略使用相同数据、查询、初始快照和搜索参数。

| 阶段 | 配置 | 独立重复 | 运行数 |
| --- | --- | --- | --- |
| A：RDMA smoke | 9 配置；100 查询；缓存 10000 | 1 | 9 |
| B：静态主实验 | 9 配置 x 缓存 10000/100000；5000 查询；warmup=0 | 5 | 90 |
| C：容量敏感性 | 9 配置 x 缓存 1000/200000；5000 查询；warmup=0 | 3 | 54 |
| D：串行混合 | 9 配置 x erase/upsert x ratio 0.1/0.3/0.5；缓存 10000；500 查询 | 3 | 162 |
| E：静态并发 | 9 配置；4 个独立客户端；每客户端 1000 查询；缓存 10000 | 3 | 27 组 / 108 客户端运行 |

阶段按 A、B、C、D、E 执行；主结论先来自 B，不用并发结果替代单客户端 G 对比。B/C 共 144 次静态运行。D 先做一轮代表性耗时试跑，估计整份索引重写的实际成本；不得因为耗时而静默减少配置或重复，需先修订计划。

混合为同会话 query/update 串行交错，不是并发原地更新实验。roundrobin 为复现主分布；hot/random 与 insert 只列为后续扩展，不计入上述计划完成条件。4 客户端只测静态，不涉及共享缓存。

每轮配置顺序使用固定随机种子生成的平衡轮转，缓存大小与阶段也记录顺序；不要总把 G=32 排在最后。每个混合配置重启存储服务并复制原始索引。静态可复用同一只读服务，但客户端与缓存必须独立。

### 4.1 更新次数与比例

沿用驱动 `floor(1/ratio)` 查询间隔语义，500 查询预计更新次数为 50/167/250；更新输入至少 250 条，不能由 `--updates-count` 提前截断。执行前用实际调度验证这些次数。

| 配置 ratio | updates/query | updates/(updates+queries) |
| --- | --- | --- |
| 0.1 | 50/500 = 0.100 | 50/550 = 0.091 |
| 0.3 | 167/500 = 0.334 | 167/667 = 0.250 |
| 0.5 | 250/500 = 0.500 | 250/750 = 0.333 |

这些不是全部操作中 10%/30%/50% 的严格占比。所有配置回放同一调度，报告原始次数与两种比例；严格操作占比属于另一个实验协议，不能混用。

## 5. 统计窗口与效率定义

### 5.1 冷启动、后段与预热

正式 B/C 均 `--warmup 0`，独立空缓存开始；前 1000 查询为冷启动观察窗，后 4000 为后段观察窗。**后段不自动称为稳态**：按连续 500 查询检查 miss、ops、字节和延迟是否仍变化，未稳定时报告轨迹和“未达稳态”。

D 使用前 100 / 后 400 查询分窗，并保留整轮摘要。E 前 200 / 后 800 分窗。若需预热后重复查询的单独测量，另列协议，不与空缓存不同查询序列混在一张表。

每个窗口的延迟、QPS、字节、READ、命中和补读都从同一批逐查询行计算。禁止只丢弃前段延迟，却继续用全轮计数解释后段收益。warmup=0 不代表页缓存、HCA 或服务注册缓冲区也处于硬件冷态。

### 5.2 必需输出

- 性能：query-only QPS、mean/p50/p95/p99、全轮 wall-time QPS、更新 mean/p50/p95/p99、失败率。
- 成本：邻居 READ/字节、节点 READ/字节、传输 READ/WRITE ops/字节、catalog refresh 和控制面时间；计时范围明确标注。
- 缓存：hits/misses/evictions、首次访问 miss 与重复访问 miss、级数升降；缓存 peak entries、RSS 与增量内存，不能只比较条目数就宣称相同内存。
- 前缀：首次足够率、cold/cached fallback 次数与字节、rounding/drift/cold waste，按层细分。
- 质量：结果序列检查、recall@10 和 GT 来源；分母为零的比率输出 NA。

以完整一次运行作为统计单位，给出逐轮结果、中位数和范围。配对 delta 使用相同输入/重复编号的运行级差值；以运行对为单位 bootstrap 给出 95% CI，明确 3--5 次重复的区间不稳定，不做逐查询伪重复。5 次无法区分时报告“无可辨别差异”，不宣布显著领先。

“效率最高”分开给出 READ 最少、流量最少和端到端最快配置。主推荐以保留查询集上的 QPS、p99、recall 和字节预算共同判断，展示 Pareto 前沿。调参集与保留集预先固定，不能在同一数据上挑最小 p50 后称为普适最优 G。

## 6. 实施前需要补齐的工具

复用 [RDMA 扫描脚本](../scripts/run_rdma_baselines.sh)、[分析脚本](../scripts/analyze_rdma_baselines.py) 和 [实验驱动](../experiments/baseline1_main.cc)，不重新实现策略。

| 准备项 | 完成条件 |
| --- | --- |
| N/G/cache 参数化 | 标签不再写死 n10000；输出键包含 N、G、缓存、workload、repeat/client |
| 防覆盖与恢复 | 单次 run 独立文件；不在每个 G 开始时删除全局 summary；已有结果仅经完整性校验后跳过 |
| 输入与质量 | 固定 base/query/update 文件与哈希，精确 GT 和结果序列核验可用 |
| 同窗口汇总 | 延迟与成本按相同 query_idx 聚合；聚合键含 G/cache，禁止混合多个 G 求中位数 |
| 运行级统计 | 替换旧逐查询 pooled CI；B3 fallback 与 QP cached fallback 统一解释 |
| 轨迹与 miss 归因 | 非计时采样/回放可用，首次与重复 miss 分开；详细 trace 在计时中关闭 |
| RDMA 校验 | 失败、短读、更新失败为 0；验证 verbs ops 与逻辑读取，明确控制面未计入部分 |
| 耗时试跑 | 单次静态/erase/upsert 耗时、MR/RSS、磁盘空间与总预算已记录 |

当前脚本虽有 prefix 支持，但未完成上述 100K 多 G 协议；不能通过简单外层循环 G 就认为扫描可靠。构建和 CTest 经 VS Code CMake Tools 执行；Python 分析统一由 conda `.venv` 环境调用。

## 7. 验收与停止条件

1. 确认实际节点数 100000、128 维、M=16；保存不可变索引哈希与环境清单。
2. 构建、已有单元测试、新索引全层一致性、RDMA smoke 和 GT 验证通过。
3. A--E 按矩阵完成，或明确列出阻塞阶段，不将部分实验标为全完成。
4. 成功层满足 `requested=useful+wasted`；QP 满足 `ops=layers+cold_fallbacks+cached_fallbacks` 等账本，正常每层至多两读。
5. 输出节点区与邻居区成本、冷/后段窗口和实际更新率；无失败样本被静默丢弃。
6. 模型回放 READ/字节预测与独立实际计数对照，区分模型误差与统计恒等式。
7. 真实 RDMA ops 非零，服务端/客户端日志证明 IB 会话；设备不可用则停止并报告阻塞，不能降级 mmap 后声称完成。
8. 索引哈希变化、结果不一致、短读、更新失败、服务退出或统计行缺失时停止对应批次，修复后由原始快照重跑。

并发动态更新、共享线程缓存、改 HNSW 搜索流程、自适应 G 和节点向量优化均不属于本轮范围。

## 8. 产物与报告结构

原始产物放在 `out/rdma_100k_prefix/`，包括 manifest、独立 run summary、per_query、updates、日志、输入哈希、顺序表、GT 检查、非计时轨迹和微基准。继续沿用仓库忽略原始数据的约定，报告写入 `docs/prefix-rdma-100k-report.md`，本计划阶段不创建空报告。

报告至少包含：

1. 真实设备与拓扑、构建版本、索引 SHA-256、实际 degree/工作集分布。
2. 静态九配置主表及每个 G 相对 B1/B2/B3 的 QPS、p99、READ、字节变化。
3. 缓存容量敏感性、冷启动/后段曲线与首次/淘汰 miss 归因。
4. erase/upsert 三档更新率下的质量、查询/更新时延、低估补读与漂移浪费。
5. 四客户端聚合 QPS 与含启动/建连的组墙钟 QPS，二者分列。
6. alpha/带宽微基准、轨迹预测误差、CPU/内存成本与负结果。
7. READ 最小 G、字节最小配置、端到端推荐及不确定性；不预先指定 G=32 获胜。

相对 10K RDMA 的数值只作注明协议差异的旁证。若要定量归因于规模，必须用本轮修正后的统计协议补跑 10K 配对控制组，不能直接拿历史表作严格缩放比较。

## 9. 执行修订（2026-11-04，实测后）

100K 试跑测得：B1 静态 5000 查询约 22 s；upsert 的 update p50 约 **541 ms**（250 次更新约 135 s/run）。按原 D 阶段 162 次运行，仅更新就需约 6 小时，超出本轮预算。因此本轮执行以下**明示缩减**的协议，原矩阵保留为后续完整版：

| 阶段 | 本轮实际 | 相对原计划的变化 |
| --- | --- | --- |
| A smoke | 9 配置，100 查询，1 次 | 不变 |
| 质量检查 | 9 配置，1000 质量查询 + 预计算 GT，1 次 | 新增执行项 |
| B 静态主实验 | 9 配置 x 缓存 10000/100000 x **5 次** x **1000 查询** | 查询数 5000 -> 1000 |
| C 容量敏感性 | 9 配置 x 缓存 1000/200000 x 3 次 x 1000 查询 | 查询数 5000 -> 1000 |
| D 串行混合 | 9 配置 x erase/upsert x ratio **0.1/0.5** x **1 次** x 100 查询（10/50 次更新） | 去掉 0.3、重复 3 -> 1、查询 500 -> 100 |
| E 静态并发 | 9 配置 x 4 客户端 x 1000 查询 x 1 次 | 重复 3 -> 1 |

缩减后的 B/C 静态运行约 144 次、每次约 4.5 s，D 约 36 次、0.5 档每次约 27 s。冷启动/后段分窗在 B/C 中使用前 500/后 500 查询，在 D 中使用前 20/后 80。所有缩减在结果报告中逐项列出，不能把本轮描述为完整 5 次/5000 查询协议。

复现命令：

```text
cmake -S . -B build -DENABLE_RDMA=ON -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON
cmake --build build -j"$(nproc)"
./build/hnsw_build --generate 100000 --dim 128 --M 16 \
  --ef-construction 200 --seed 42 \
  --output data/index_n100000_d128_M16.bin
./build/dump_vectors --count 100000 --dim 128 --seed 42 \
  --output data/prefix100k/base.fvecs
./build/dump_vectors --count 5000 --dim 128 --seed 43 \
  --output data/prefix100k/query_timing.fvecs
./build/dump_vectors --count 1000 --dim 128 --seed 45 \
  --output data/prefix100k/query_quality.fvecs
./build/dump_vectors --count 250 --dim 128 --seed 44 \
  --output data/prefix100k/updates.fvecs
conda run -n .venv python scripts/compute_gt.py \
  --base data/prefix100k/base.fvecs \
  --queries data/prefix100k/query_quality.fvecs \
  --k 10 --output data/prefix100k/gt_quality_k10.u32
./build/check_prefix_index --index data/index_n100000_d128_M16.bin \
  --gs 1,2,4,8,16,32
scripts/run_rdma_prefix_100k.sh
conda run -n .venv python scripts/analyze_prefix_sweep.py \
  --root out/rdma_100k_prefix --cold-split 500 --mixed-cold-split 20 \
  --tables out/rdma_100k_prefix_tables.md
```
