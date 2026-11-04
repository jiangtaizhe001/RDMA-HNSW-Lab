# 100K 索引分级前缀与三个 Baseline 的真实 RDMA 报告

状态：**已按 [100K 计划](prefix-rdma-100k-plan.md)第 9 节的缩减协议执行完成；原计划的全量 5 次/5000 查询与完整混合矩阵未执行**。搜索流程保持原型不变，未修复 `ef_search` 扩展上限问题。

## 1. 结论摘要

1. **G=32 的邻居 READ 最少**：缓存 100000 的稳态窗口为 **75.04 ops/query**，B3 为 93.73、B1 为 75.04。G=32 相对 B3 少 18.69 次 READ/query（−19.9%），但多 **893.8 B/query** 过读。
2. **G=1–8 没有 READ 收益**：与 B3 同为 93.73 ops/query，只增加 0–218.7 B/query 过读；G=16 只少 2.82 次 READ，却多 412.4 B。
3. **G=32 的端到端 p50 与 B1/B3 没有可辨别差异**：B1 4338.9 µs、G=32 4360.7 µs、B3 4379.6 µs；B2 为 4460.6 µs，比 B1 慢约 2.8%。
4. **缓存容量达到 100000 后不再改善命中**：B3 在 1000/10000/100000/200000 容量下稳态命中率为 14.3%/50.9%/75.1%/75.1%；100000 与 200000 的 `cache_evictions` 均为 0，剩余 miss 是窗口内首次访问，不是 LRU 淘汰。
5. **混合 workload 方向相同**：upsert 0.5 下 B3 为 129.04 ops/query，G=32 为 74.51，G=16 为 122.17；缓存低估补读仅 0–0.03 次/query，G 的收益主要来自消除冷启动补读，而不是吸收桶内增长。
6. **四客户端静态聚合吞吐 775–796 QPS**：B1 最高（796.5），B2 最低（774.7），G=32 为 787.2，与单客户端结论一致但没有数量级差异。
7. **recall@10 全部为 0.0447**：九种配置完全一致。该值来自原型的“最多扩展 `ef_search` 个节点”语义，不是标准 HNSW 候选列表语义；本轮按要求**不修复搜索流程**，因此 recall 只用于策略一致性检查，不能作为标准 HNSW 质量结论。

## 2. 环境与输入

| 项 | 值 |
| --- | --- |
| 主机 / 拓扑 | 单机，存储与计算同机，IB HCA loopback |
| RDMA 设备 | `mlx5_0`，MT4115，fw 12.16.1006，IB `PORT_ACTIVE`，active MTU 4096，LID 25 |
| 构建 | `ENABLE_RDMA=ON`，`ENABLE_RDMA_CM=OFF`，IB LID 后端 |
| 索引 | `data/index_n100000_d128_M16.bin`，100000 节点，128 维，M=16，ef_construction=200，seed=42 |
| 索引 SHA-256 | `ccd95ac2dab47170a6eb19c849ae922fc75493b8a5b19bb8267e6614f5ecdd0f` |
| 查询 | `query_timing_1000.fvecs`（seed=43），`query_quality.fvecs`（seed=45），`query_mixed_100.fvecs`，`query_conc_1000.fvecs` |
| 更新 | `updates.fvecs`（seed=44，250 条） |
| GT | `gt_quality_k10.u32`，精确 L2 top-10，1000 条质量查询 |
| 参数 | `ef_search=64`，`k=10`，L2 |
| 输入哈希 | `data/prefix100k/SHA256SUMS` |

构建、索引和输入均由 [计划第 9 节](prefix-rdma-100k-plan.md)的命令生成。100K 索引构建耗时约 63 s，文件 91 MB。

## 3. 正确性与质量门禁

- `check_prefix_index` 遍历 **100000 节点、106677 个有效层**，比较 B1/B2/B3 与 G=1/2/4/8/16/32 的冷/热邻居序列：**mismatches=0**。
- `ctest`：11/11 通过。
- 质量运行 9 个配置、1000 查询、缓存 10000：`failed_reads=0`，recall@10 全部为 **0.0447**。
- recall 值低的原因已在独立排查中定位：`QueryEngine::search()` 用 `expanded < ef_search` 限制总扩展节点数，100K 时最多评估 64 个节点；`docs/experiment.md` 要求“不得修改 HNSW 搜索主流程”，用户决定不修复。因此本报告只要求四种策略在**相同搜索路径和相同 recall** 下比较，不把 0.0447 解释为标准 HNSW 召回率。
- 静态与混合 summary 共 180 行（90 静态 + 54 容量 + 36 混合）通过字节/操作恒等式校验；分析脚本输出 `identity validation OK`。

## 4. 静态主实验

缓存 100000、后 500 查询稳态窗口，5 次重复中位数：

| 策略 | G | p50 µs | ops/query | 查找命中 | 冷启动补读/query | 缓存低估补读/query | 浪费 B/query |
| --- | --- | --- | --- | --- | --- | --- | --- |
| B1 fixed | - | 4338.9 | 75.04 | - | - | - | 893.8 |
| B2 header-first | - | 4460.6 | 150.07 | - | - | - | 0.0 |
| B3 cached-length | - | 4379.6 | 93.73 | 75.1% | - | - | 0.0 |
| QP | 1 | 4363.5 | 93.73 | 75.1% | 18.69 | 0.00 | 0.0 |
| QP | 2 | 4383.7 | 93.73 | 75.1% | 18.69 | 0.00 | 31.7 |
| QP | 4 | 4383.8 | 93.73 | 75.1% | 18.69 | 0.00 | 94.8 |
| QP | 8 | 4387.6 | 93.73 | 75.1% | 18.69 | 0.00 | 218.7 |
| QP | 16 | 4377.3 | 90.91 | 75.1% | 15.87 | 0.00 | 412.4 |
| QP | 32 | 4360.7 | 75.04 | 75.1% | 0.00 | 0.00 | 893.8 |

观察：

- G=1–8 的 ops/query 与 B3 完全相同。静态查询不改变 degree，缓存低估为 0，分级只增加取整过读。
- G=16 把冷启动补读从 18.69 降到 15.87，ops 只降 2.82；原因是高层容量 M=16 被一次覆盖。
- G=32 覆盖 L0 容量 32 和所有高层容量 16，冷启动补读为 0，ops 降到 B1 水平；代价是 893.8 B/query 过读，与 B1 的固定读取浪费相同量级。
- p50 差异：B2 比 B1 高 121.7 µs（+2.8%）；G=32 比 B1 高 21.8 µs（+0.5%），B3 比 B1 高 40.7 µs（+0.9%）。这些都在单机 loopback 的重复波动范围内，不能宣布 G=32 更快。

## 5. 缓存容量敏感性

稳态窗口 ops/query（缓存容量 1000 / 10000 / 100000 / 200000）：

| 配置 | 1000 | 10000 | 100000 | 200000 |
| --- | --- | --- | --- | --- |
| B3 | 139.34 | 111.86 | 93.73 | 93.73 |
| QP G=4 | 139.32 | 111.86 | 93.73 | 93.73 |
| QP G=16 | 131.19 | 107.01 | 90.91 | 90.91 |
| QP G=32 | 75.04 | 75.04 | 75.04 | 75.04 |

B3 命中率分别为 14.3%、50.9%、75.1%、75.1%。100000 与 200000 的 `cache_evictions` 中位数均为 0，说明：

- 容量 1000/10000 时大量 miss 来自真实淘汰；
- 容量 ≥100000 后，剩余 24.9% miss 是窗口内**首次访问**的新 `(node,level)`，增加容量不会消除；
- G=32 不受命中率影响，因为每次都是读满容量；这正是它的 READ 下限来源。

## 6. 混合 erase/upsert

缓存 10000，后 80 查询稳态窗口，1 次运行。ratio 0.5（50 次更新）：

| op | 策略 | G | p50 µs | ops/query | 查找命中 | 冷启动补读/query | 缓存低估补读/query | 浪费 B/query |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| erase | B1 | - | 6532.2 | 74.51 | - | - | - | 901.9 |
| erase | B3 | - | 6710.1 | 128.86 | 27.1% | - | 0.00 | 1.0 |
| erase | QP | 4 | 6847.5 | 128.82 | 27.1% | 54.31 | 0.00 | 50.2 |
| erase | QP | 16 | 6781.4 | 122.00 | 27.1% | 47.49 | 0.00 | 173.5 |
| erase | QP | 32 | 6581.4 | 74.51 | 27.1% | 0.00 | 0.00 | 901.9 |
| upsert | B1 | - | 6680.8 | 74.51 | - | - | - | 889.9 |
| upsert | B3 | - | 6852.3 | 129.04 | 26.9% | - | 0.00 | 0.0 |
| upsert | QP | 4 | 6770.3 | 128.99 | 26.9% | 54.46 | 0.01 | 46.2 |
| upsert | QP | 16 | 6740.4 | 122.17 | 26.9% | 47.65 | 0.01 | 169.5 |
| upsert | QP | 32 | 6714.8 | 74.51 | 26.9% | 0.00 | 0.00 | 889.9 |

ratio 0.1（10 次更新）方向相同：B3 129.03 ops/query，G=4 128.99，G=16 122.16，G=32 74.53。实际更新率为 0.100/0.500 updates/query，对应 0.091/0.333 updates/(updates+queries)。

观察：

- 100K 下 upsert/erase 的 update p50 中位数约 **532/519 ms**，由存储端整份索引重写与重新注册支配。这是本轮缩减混合矩阵的直接原因。
- 混合命中率仅约 27%，因为每轮从空缓存开始且更新后部分访问是新节点；G=4 的 54 次冷启动补读几乎与 B3 的 miss 数相同。
- 缓存低估补读在 G=1/4/16 下只有 0–0.01 次/query（G=1 在 0.5 档约 0.03），没有出现设计期望的“桶内小幅增长”收益；G 的收益仍来自冷启动，而非量化吸收。
- G=32 的 ops 与 B1 相同，过读约 890–902 B/query；p50 与 B1 相差约 35–80 µs，低于 B2 的劣化，但没有稳定优于 B3。

## 7. 四客户端静态并发

每客户端 1000 查询，4 个独立 `ComputeNode`，1 次运行：

| 配置 | 四客户端 QPS 之和 | 组墙钟 QPS（含建连） |
| --- | --- | --- |
| B1 | 796.49 | 733.27 |
| B2 | 774.69 | 714.03 |
| B3 | 781.45 | 721.76 |
| QP G=1 | 783.39 | 723.33 |
| QP G=2 | 785.50 | 725.29 |
| QP G=4 | 786.28 | 724.90 |
| QP G=8 | 778.56 | 714.54 |
| QP G=16 | 780.37 | 720.07 |
| QP G=32 | 787.23 | 727.67 |

四客户端相对单客户端约 3.4–3.5×；B2 略低，B3 与 B1 基本持平，G=32 与 B3/B1 的差异小于 1%。组墙钟 QPS 包含连接与启动开销，不能与稳态 QPS 混用。

## 8. 账本与代价模型

180 行 summary 通过校验：

```text
requested = useful + wasted
QP: prefix_reads = layer_reads
QP: hits + misses = layer_reads
QP: read_ops = prefix_reads + fallbacks
QP: dependent_reads = fallbacks
QP: fallbacks = cached_fallbacks + cold_fallbacks
QP: wasted = rounding + drift + cold_prefix
B3: read_ops = hint + header + dependent
B3: hits + misses = layer_reads
B3: fallbacks = cache_invalidations
```

代价模型检查：

| 策略 | 预测 ops/query | 实测 ops/query | 最大绝对误差 |
| --- | --- | --- | --- |
| B3 | 108.769 | 108.769 | 0.000 |
| QP | 103.076 | 103.076 | 0.000 |

误差为零只说明计数口径自洽，不是独立预测验证；本轮没有做轨迹回放。

## 9. 哪个 G 效率最高

按不同目标分开回答：

1. **READ 次数最少**：**G=32（即 L0 容量 2M）**。静态缓存 100000 下 75.04 ops/query，B3 为 93.73（−19.9%）；混合 0.5 下 74.51，B3 为 129.04（−42.3%）。
2. **字节最少且不增加 READ**：**G=1 或直接用 B3**。G=1–8 与 B3 操作数相同，G=1 过读为 0；G=2/4/8 只增加 31.7/94.8/218.7 B/query。
3. **折中**：G=16 只少 2.82 次静态 READ（缓存 100000），却多 412.4 B/query；在缓存 1000 下少 8.15 次 READ、多 140.9 B，是容量受限时唯一略有意义的中点。
4. **端到端延迟**：本轮没有可辨别的 G 最优值。B2 明确最慢；B1、B3、G=32 的 p50 差异在 0.5–0.9% 内，G=4/G=16 也没有稳定优势。要判断“少一次 READ 是否值得多 894 B”，需要两机 RDMA 或更高重复次数。

因此，本实验支持：**若目标是最小化依赖 READ，G 取层容量（G=32/2M）；若目标是字节或端到端效率，G=32 只是退回 B1，中间 G 没有优于 B3 的证据。** G=4 默认值在 100K RDMA 上同样不成立。

## 10. 局限与偏差

- 单机 IB HCA loopback，没有跨机网络 RTT；绝对 p50 不能外推到两机部署。
- 合成均匀 128 维数据，不是 SIFT/GIST；degree 分布与真实数据不同。
- recall@10=0.0447 是原型 `ef_search` 扩展上限语义的结果，且按要求未修复；只用于策略一致性，不代表标准 HNSW 质量。
- 执行缩减：静态 1000 查询（原计划 5000）、混合仅 ratio 0.1/0.5 且 1 次重复（原计划 0.1/0.3/0.5、3 次）、并发仅 1 次重复；完整矩阵未完成。
- 混合只测 roundrobin，未测 hot/random/insert；未做逐层轨迹回放。
- 100000/200000 缓存 `evictions=0` 已确认，但没有单独记录“首次访问 miss vs 重复 miss”计数器；该归因由 `evictions=0` 与容量不敏感推断。
- 并发 groups.csv 的旧行未包含 G 键；报告中的分配置 QPS 由逐客户端 summary 按 label 重新汇总。

## 11. 产物与复现

```text
out/rdma_100k_prefix/
  static/summary, static/per_query, static/logs
  capacity/..., quality/..., mixed/..., concurrency/...
  static_summary.csv, capacity_summary.csv, mixed_summary.csv, quality_summary.csv
  mixed/updates/
out/rdma_100k_prefix_tables.md
```

复现命令见 [计划第 9 节](prefix-rdma-100k-plan.md)。本轮实际使用：

```text
PHASES=smoke scripts/run_rdma_prefix_100k.sh
PHASES=quality scripts/run_rdma_prefix_100k.sh
PHASES=static REP_FROM=1 REP_TO=5 scripts/run_rdma_prefix_100k.sh
PHASES=capacity REP_FROM=1 REP_TO=3 scripts/run_rdma_prefix_100k.sh
PHASES=mixed MIX_RATIOS=0.1 scripts/run_rdma_prefix_100k.sh
PHASES=mixed MIX_RATIOS=0.5 MIX_OPS=erase scripts/run_rdma_prefix_100k.sh
PHASES=mixed MIX_RATIOS=0.5 MIX_OPS=upsert scripts/run_rdma_prefix_100k.sh
PHASES=concurrency scripts/run_rdma_prefix_100k.sh
conda run -n .venv python scripts/analyze_prefix_sweep.py \
  --root out/rdma_100k_prefix --cold-split 500 --mixed-cold-split 20 \
  --tables out/rdma_100k_prefix_tables.md
```
