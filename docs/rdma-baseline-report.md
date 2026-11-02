# RDMA 环境下三个 Baseline 的 10K 索引计时报告

本文记录在**真实 InfiniBand 环境**下重新运行 Baseline 1/2/3 的结果。实验索引固定为 **10000 个节点、128 维、M=16**，重点测量查询与更新耗时，并同时记录邻居区字节、RDMA READ 操作数和 Baseline 3 的缓存行为。

> 与之前三份 `baseline*-report.md` 的区别：那些报告的数据来自 WSL2 + mmap，本报告的数据来自 `mlx5_0` InfiniBand 设备上的 RDMA READ/WRITE 路径。两者不能直接比较绝对延迟。

---

## 1. 结论摘要

1. **Baseline 2 在 RDMA 上确实变慢**：静态查询 p50 从 B1 的 3849 µs 升到 3987 µs，逐查询配对中位数 **+139 µs（+3.6%）**。它把邻居区读取操作数提高 98.6%，总 RDMA READ 操作数提高 3.8%。
2. **Baseline 2 省下的字节对总流量几乎不可见**：邻居区字节下降 16.0%，但邻居区只占总读流量的 **0.92%**，总流量只下降约 **0.15%**。延迟变化与总操作数变化（+3.8%）同量级，而不是与字节数变化同量级。
3. **Baseline 3 保住了 B2 的字节收益，同时把操作数降回接近 B1**：静态下邻居区操作数比 B2 少 **46.5%**，只比 B1 多 **6.3%**；总 RDMA READ 操作数比 B1 多 **0.25%**。静态 p50 与 B1 的配对中位数只差 **+13 µs（约 +0.3%）**，落在重复实验的噪声范围内。
4. **B3 静态缓存命中率 94.9%**；混合 workload 下稳定在约 **93%**。`upsert` 会造成 hint 低估和 fallback（0.1/0.3/0.5 比例下分别为 507/1439/2036 次），`erase` 只造成少量过度读取浪费（22.7 B/query），不产生 fallback。
5. **更新延迟由存储端整份索引重写支配**：p50 约 **48–64 ms**，三个策略几乎相同。更新比例从 0.1 升到 0.5 时，查询 p50 从约 4.3 ms 升到约 5.8–6.0 ms，主要来自单机环境下更新与查询争用 CPU/文件缓存，而不是读取策略本身。
6. **4 客户端静态并发**下，三策略聚合吞吐约 **859–879 QPS**，相对单客户端约 3.4 倍；B2 略低，B3 与 B1 基本持平。

---

## 2. 实验环境

| 项 | 值 |
| --- | --- |
| 主机 | 单机（存储服务与计算客户端同机） |
| CPU | Intel Xeon Gold 6240 @ 2.60 GHz，`nproc` 可见 72 个逻辑核 |
| 内存 | 181 GB（`MemTotal`） |
| RDMA 设备 | `mlx5_0`，MT4115，firmware 12.16.1006 |
| 链路 | InfiniBand，PORT_ACTIVE，100 Gb/s（4X EDR），active MTU 4096 |
| LID | 0x19（十进制 25） |
| 内核 | Linux 5.4.0-144-generic |
| 编译器 | conda-forge GCC 14.3.0（`x86_64-conda-linux-gnu-g++`），C++20 |
| 构建 | `ENABLE_RDMA=ON`、`ENABLE_RDMA_CM=OFF` |
| 传输后端 | IB LID 后端；控制面 TCP 走 `127.0.0.1:18515` |
| Python | conda 环境 `.venv`，Python 3.14.9，NumPy 2.5.4，Matplotlib 3.11.2 |

**重要限制：这是单机回环实验。** 存储进程和计算进程在同一台机器上，QP 通过同一块 HCA 的 loopback LID 建连。它走的是真实 verbs/RDMA READ 数据路径，但不包含两台机器之间的网络往返，也无法避免更新线程与查询线程争用同一台机器的 CPU 和页缓存。绝对延迟不应直接外推到两机 eRDMA/IB 部署。

### 2.1 构建说明

conda 的 GCC 使用自带 sysroot，默认找不到系统 `/usr/include/infiniband`。本次把系统头文件链接进 conda include 目录后再构建：

```text
ln -sfn /usr/include/infiniband $CONDA_PREFIX/include/infiniband
cmake -S . -B build \
  -DCMAKE_CXX_COMPILER="$(which x86_64-conda-linux-gnu-g++)" \
  -DENABLE_RDMA=ON -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON \
  -DIBVERBS_INCLUDE_DIR="$CONDA_PREFIX/include" \
  -DIBVERBS_LIBRARY=/usr/lib/x86_64-linux-gnu/libibverbs.so
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure
```

`ctest` 结果：**9/9 通过**，其中包括 B1/B2/B3 邻居读取等价的单元测试、以及三个策略的实验驱动 smoke test。

`ENABLE_RDMA_CM=OFF` 的原因：本次 conda 工具链下 CMake 未找到 `librdmacm` 开发文件。实验使用 IB 后端，不需要 RDMA_CM。

---

## 3. 数据集与索引

由于本轮选择“沿用 `hnsw_build --generate`，不计算 recall”，数据与查询均由项目自带的均匀分布生成器产生：

| 项 | 值 |
| --- | --- |
| 节点数 | 10000 |
| 维度 | 128 |
| 距离 | L2 |
| `M` | 16 |
| `ef_construction` | 200 |
| `ef_search` | 64 |
| `k` | 10 |
| 随机种子 | 42 |
| 索引文件 | `data/index_n10000_d128_M16.bin` |
| 文件大小 | 9.1 MB |
| SHA-256 | `4d8c9de4960d8ed854d193b6ef12bec1cb710ed500a27bbb5e16f268e7e362e1` |

构建命令：

```text
./build/hnsw_build --generate 10000 --dim 128 --M 16 \
  --ef-construction 200 --seed 42 \
  --output data/index_n10000_d128_M16.bin
```

**未计算 recall/ground truth**。因此本报告只能比较“相同搜索参数、相同查询生成器”下的时间与操作数，不能证明三策略在结果质量上等价。正确性由 `ctest` 中的策略等价测试覆盖；三个策略共享同一 `QueryEngine::search()` 主流程，仅邻居读取实现不同。

索引的静态 degree 特征（来自实验输出）：

| 指标 | 值 |
| --- | --- |
| level-0 capacity | 32 |
| 聚合容量利用率 | 0.8345 |
| 平均 degree | 25.03 |
| B1 邻居区浪费率 | 16.0% |

---

## 4. 实验方法

### 4.1 策略

| 标签 | 类 | 行为 |
| --- | --- | --- |
| B1 fixed | `Baseline1FixedStrategy` | 一次读取 `degree + capacity` 个 slot |
| B2 header-first | `Baseline2HeaderFirstStrategy` | 先读 4B degree，再读 `degree` 个邻居 |
| B3 cached-length | `Baseline3CachedLengthStrategy` | 缓存 `(node, level) → degree`，命中时一次读取预测长度；低估时 fallback |

B3 缓存容量固定为 **10000** 条，覆盖 10K 索引约 1.07 万个 `(node, level)` 键。

### 4.2 Workload

| 阶段 | 配置 | 重复 | 说明 |
| --- | --- | --- | --- |
| 静态 | 1000 查询 + 200 warmup | 3 | 单客户端，同一 server 连续跑三策略 |
| 4 客户端 | 每客户端 500 查询 + 200 warmup | 3 | 4 个独立 `ComputeNode` 进程并发连接同一 server |
| 混合 | 500 查询 + 300 更新向量 | 3 | `erase`/`upsert` × 更新比例 0.1/0.3/0.5，roundrobin 更新 |

**重复与顺序**：每个重复内按轮转顺序运行三个策略，降低顺序偏差。混合阶段为每次运行复制一份全新索引并重启存储服务，保证三策略看到完全相同的初始索引和更新序列。

**统计口径**：延迟统计剔除 `query_idx < 200` 的预热样本；报告跨 3 次重复的中位数。配对分析按相同 `query_idx` 对 B1/B2/B3 的逐查询延迟做差，再汇总。所有 99 行 summary 都通过字节/操作数恒等式校验：

```text
requested == useful + wasted
B2: read_ops == header_reads + dependent_reads
B3: 8 条缓存/fallback 恒等式
RDMA: transport_read_ops == read_ops + node_reads
```

**规模**：共执行 54,000 条计入统计的查询（静态 9,000 + 并发 18,000 + 混合 27,000），以及 8,406 次更新。整轮扫描墙钟时间约 **11.9 分钟**。

---

## 5. 静态查询结果

### 5.1 主表

| strategy | QPS | mean µs | p50 µs | p95 µs | p99 µs | 邻居 B/query | 邻居 ops/query | RDMA ops/query | runs |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| B1 fixed | 259.2 | 3857.8 | 3849.1 | 4165.7 | 4279.4 | 9077 | 73.2 | 1901.7 | 3 |
| B2 header-first | 250.4 | 3993.1 | 3986.9 | 4310.8 | 4406.9 | 7625 | 145.5 | 1974.0 | 3 |
| B3 cached-length | 256.7 | 3895.6 | 3901.9 | 4199.4 | 4268.0 | 7625 | 77.9 | 1906.4 | 3 |

逐次重复的范围（p50）：B1 `3840.7–3912.9 µs`，B2 `3980.2–3999.3 µs`，B3 `3852.8–3919.2 µs`。B2 的 p50 在三次重复中都高于 B1；B1 与 B3 的范围重叠。

![静态延迟](../out/figures_rdma/static_latency.png)

![静态 QPS](../out/figures_rdma/static_qps.png)

![静态延迟 CDF](../out/figures_rdma/static_latency_cdf.png)

### 5.2 配对逐查询差

| pair | 每次重复的均值差中位数 | 最小 | 最大 | 合并均值 | 95% CI |
| --- | --- | --- | --- | --- | --- |
| B2 − B1 | **+139.4 µs** | +88.0 | +144.3 | +123.9 µs | ±10.6 µs |
| B3 − B1 | **+13.2 µs** | +9.9 | +37.8 | +20.3 µs | ±7.4 µs |
| B3 − B2 | **−106.5 µs** | −129.5 | −74.9 | −103.6 µs | ±8.8 µs |

配对差的置信区间很小，说明差异不是查询难度或重复间漂移造成的。B2 比 B1 慢约 **0.14 ms/query**；B3 与 B1 的差异只有约 **0.01–0.02 ms/query**。

### 5.3 相对 B1 的变化

| strategy | p50 | QPS | 邻居字节 | 邻居操作数 |
| --- | --- | --- | --- | --- |
| B1 fixed | +0.0% | +0.0% | +0.0% | +0.0% |
| B2 header-first | +3.6% | −3.4% | **−16.0%** | **+98.6%** |
| B3 cached-length | +1.4% | −1.0% | **−16.0%** | **+6.3%** |

> 表中相对值来自跨重复的聚合中位数；配对分析给出的 B3−B1 更小（+0.3%）。两种口径都表明 B3 与 B1 基本持平。

![邻居区字节与操作数](../out/figures_rdma/static_ops_bytes.png)

### 5.4 解读

- **字节节省被总流量稀释**：B1 每查询邻居区约 9.1 KB，节点区约 979 KB；邻居区只占总读流量的 0.92%。B2 把邻居区浪费清零，只省下约 **1.45 KB/query**，对总流量仅约 **0.15%**。
- **延迟随总操作数变化**：B2 的邻居操作数从 73.2 升到 145.5（+98.6%），总 RDMA READ 操作数从 1901.7 升到 1974.0（+3.8%），p50 上升 3.6%。在本实验的读多、单次数据量小（邻居区约 100 B）的形态下，**每次 RDMA READ 的固定成本比传输字节数更重要**。
- **B3 的作用是“B2 的字节 + B1 的操作数”**：B3 命中时一次读取 `degree + hint`，避免了 B2 的第二次依赖 READ；静态命中率 94.9%，只有 5.1% 的层需要冷启动两跳。它的邻居操作数比 B2 少 46.5%，只比 B1 多 6.3%，总操作数只多 0.25%。
- **B3 仍有约 6.3% 的额外邻居操作**来自约 5.1% 的冷启动 miss；在本 10K 索引和 1000 查询下，这项开销没有转化为可测量的 p50 差异。

---

## 6. 4 客户端静态并发

| strategy | 1 客户端 QPS | 4 客户端聚合 QPS（各客户端之和） | 组墙钟 QPS（含建连/启动） |
| --- | --- | --- | --- |
| B1 fixed | 259.2 | **879.4** | 585.5 |
| B2 header-first | 250.4 | **858.9** | 570.3 |
| B3 cached-length | 256.7 | **878.7** | 586.0 |

![并发吞吐](../out/figures_rdma/concurrency_qps.png)

- 4 客户端下 B1/B2/B3 分别达到 **3.39× / 3.43× / 3.42×** 的单客户端吞吐，说明服务端和 HCA 仍能吸收并发。
- 每个客户端在 4 并发下的单进程 QPS 约 209–229，低于单独运行的 250–259，属于共享 server、CQ 和内存带宽后的正常争用。
- “组墙钟 QPS”约 570–586，低于“各客户端 QPS 之和”，因为短查询组（每客户端 500 条）包含进程启动、连接建立和 server 会话初始化开销；稳态吞吐应看各客户端之和。
- 三策略的扩展趋势基本一致；B2 的聚合吞吐略低（−2.3%），B3 与 B1 基本持平（−0.1%）。

---

## 7. 混合 erase/upsert workload

### 7.1 查询与更新延迟

| op | ratio | strategy | QPS | query p50 µs | query p95 µs | update p50 µs | B3 hit % | B3 fallbacks |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| erase | 0.1 | B1 fixed | 215.4 | 4343.2 | 6213.8 | 62978 | – | – |
| erase | 0.1 | B2 header-first | 211.6 | 4421.4 | 6308.3 | 62327 | – | – |
| erase | 0.1 | B3 cached-length | 215.1 | 4332.2 | 6195.6 | 62797 | 93.1 | 0 |
| erase | 0.3 | B1 fixed | 176.2 | 5714.4 | 6265.3 | 50854 | – | – |
| erase | 0.3 | B2 header-first | 172.0 | 5891.4 | 6394.7 | 51240 | – | – |
| erase | 0.3 | B3 cached-length | 175.7 | 5762.4 | 6280.5 | 50973 | 93.0 | 0 |
| erase | 0.5 | B1 fixed | 170.8 | 5853.0 | 6285.4 | 48704 | – | – |
| erase | 0.5 | B2 header-first | 168.0 | 5946.9 | 6412.6 | 48400 | – | – |
| erase | 0.5 | B3 cached-length | 169.0 | 5927.1 | 6315.3 | 48599 | 93.0 | 0 |
| upsert | 0.1 | B1 fixed | 216.0 | 4306.4 | 6225.7 | 64063 | – | – |
| upsert | 0.1 | B2 header-first | 212.3 | 4382.9 | 6365.7 | 64059 | – | – |
| upsert | 0.1 | B3 cached-length | 214.8 | 4326.2 | 6301.6 | 63843 | 93.1 | 507 |
| upsert | 0.3 | B1 fixed | 173.4 | 5833.7 | 6331.0 | 53159 | – | – |
| upsert | 0.3 | B2 header-first | 171.1 | 5946.4 | 6463.9 | 53325 | – | – |
| upsert | 0.3 | B3 cached-length | 172.9 | 5877.8 | 6404.4 | 53092 | 93.1 | 1439 |
| upsert | 0.5 | B1 fixed | 167.7 | 5961.4 | 6477.0 | 50164 | – | – |
| upsert | 0.5 | B2 header-first | 164.2 | 6081.6 | 6597.3 | 50584 | – | – |
| upsert | 0.5 | B3 cached-length | 166.5 | 5996.7 | 6494.4 | 50074 | 93.0 | 2036 |

![混合查询延迟](../out/figures_rdma/mixed_query_latency.png)

![混合更新延迟](../out/figures_rdma/mixed_update_latency.png)

### 7.2 观察

1. **更新延迟由存储端重写整份索引支配**。三个策略的 update p50 几乎完全重合（48–64 ms），说明查询侧的邻居读取策略不改变更新成本。当前原型每次 `StorageEngine::apply()` 都会对全部节点排序、重写整份 9.1 MB 索引文件，server 随后又把整份文件读回注册内存；这项开销远大于一次 RDMA 往返。
2. **更新比例越高，查询 p50 越高**：从 0.1 的约 4.3 ms 升到 0.5 的约 5.8–6.0 ms。原因是本实验为单机回环，更新线程在存储端持续重写文件并占用 CPU/页缓存，与查询线程竞争；这不是读取策略差异，也不能直接代表两机部署下的表现。
3. **B2 仍是混合 workload 中最慢的查询策略**：同比例下 p50 比 B1 高约 1.5–2.4%；B3 与 B1 的差在 0.1–1.3% 内，基本重合。
4. **B3 命中率对更新比例不敏感**：erase/upsert 在三个比例下都约 93.0–93.1%。原因是缓存容量 10000 足够覆盖 10K 索引，命中率主要由容量而非更新次数决定。
5. **erase 与 upsert 的失效方向相反**：
   - `erase`：从其他节点邻居表删除 id，degree 只下降，缓存 hint 只会“偏大”。实测 **fallback = 0**，但过度读取率从 2.9% 升到 11.3%，平均浪费 22.7 B/query。
   - `upsert`：目标节点邻居表被重填到 capacity，degree 上升，缓存 hint 会“偏小”。实测 fallback 从 507 次升到 2036 次，fallback 总字节 63,564 B；没有过度读取浪费。
6. **端到端 QPS 被更新主导**：ratio=0.5 时，500 条查询 + 250 次更新约需 16.5 s，端到端约 31–32 QPS；查询本身仍有 164–167 QPS。

![B3 缓存命中率](../out/figures_rdma/mixed_b3_cache.png)

---

## 8. 讨论

### 8.1 为什么 B2 省了 16% 邻居字节却慢了 3.6%

本实验的每个节点记录包含 24 B header + 128×4 B 向量 = 536 B，每次查询平均读取约 1827 个节点记录；邻居区每次只有约 73 次读取、9 KB 数据。于是：

```text
邻居区字节占比 = 9077 / (9077 + 979438) ≈ 0.92%
总流量节省     = 0.16 × 0.92% ≈ 0.15%
```

B2 把邻居区读取拆成两次依赖 READ，邻居区操作数 +98.6%，总操作数 +3.8%。在本实验中，**总 RDMA READ 操作数与时延近似同比例变化**：B2 操作数 +3.8%，p50 +3.6%。这说明瓶颈是每次 READ 的固定成本（verbs 提交、完成等待、内存访问），而不是邻居区的几百字节带宽。

### 8.2 B3 的收益边界

B3 命中时只发一次 READ，把 B2 的“字节收益”保留下来，同时把邻居区操作数从 145.5 降到 77.9。它的总操作数只比 B1 多 0.25%，因此静态 p50 与 B1 基本一致。B3 的代价是：

- 冷启动 miss（约 5.1% 的层）需要先读 4B degree 再读邻居，等价于 B2 的两跳；
- `upsert` 导致 hint 低估时产生 fallback；
- `erase` 导致 hint 偏大时产生少量过度读取。

在 10K 索引、容量 10000 的缓存下，这些代价都较小；若索引规模增大到 100K/1M，缓存容量与命中率的关系需要重新实验。

### 8.3 与 mmap 报告的关系

之前的 mmap 报告在 WSL2 上测得静态 p50 约 1.9 ms，而本报告在 RDMA 回环上约 3.85 ms。差异主要来自：

- RDMA 路径每次 READ 都要提交 WR、等待 CQE；mmap 的 `pread` 命中页缓存时更快；
- 本机是 Xeon + 100 Gb IB，但仍是单机回环，存储与计算共享 CPU/内存；
- mmap 报告使用的是另一套硬件和数据，不能逐项对比。

因此本报告的价值不在于“RDMA 比 mmap 慢/快”，而在于：**在真实 RDMA 操作模型下，B2 的额外依赖往返可以被稳定测量，而 B3 能把操作数降回 B1 水平**。这与 mmap 下“三者差异很小”的结论不同，说明 RDMA 的每次操作固定成本是策略选择的关键因素。

---

## 9. 局限

1. **单机 IB 回环**：没有跨机网络 RTT，也无法隔离更新线程与查询线程的 CPU/页缓存争用。两机 eRDMA/IB 部署下，RDMA READ 的固定成本更高，B2 的额外往返可能更不利；但具体数值需重新测量。
2. **未计算 recall**：按本轮选择，查询为 `hnsw_build --generate` 的合成数据，没有 ground truth。报告中的“正确性”仅指 `ctest` 的策略等价测试和共享搜索主流程；不能据此声称三策略在真实数据集上召回相同。
3. **合成均匀数据**：不是 SIFT/GIST，degree 分布和查询难度与真实数据不同；低维/低 degree 场景下邻居区占比可能更高，B2 的字节收益会更明显。
4. **更新路径是教学原型**：每次更新重写整份索引文件并重新读入 MR，update p50 约 50 ms，不能代表生产级增量更新。更新延迟的比较只能说明“三个策略的查询侧差异不影响更新成本”。
5. **并发模型**：4 客户端是 4 个独立进程/连接，不是进程内多线程；没有测 2 客户端、8 客户端或读写混合并发。
6. **B3 参数**：只测了 cache capacity=10000，没有做容量敏感性、热点更新、不同粒度前缀策略的对比。
7. **CM 后端未启用**：librdmacm 开发文件在当前 conda 工具链下未找到，实验只覆盖 IB LID 后端。
8. **计时噪声**：每档 3 次重复，报告使用中位数和配对差；CPU 频率、虚拟化与页缓存仍可能带来数个百分点波动。B3 与 B1 的差异处于噪声边缘，应理解为“无可测量的实质差异”，而不是“B3 严格等于 B1”。

---

## 10. 复现

### 10.1 环境

```text
source ~/.local/miniconda3/etc/profile.d/conda.sh
conda activate .venv
conda install -n .venv -y -c conda-forge 'gxx_linux-64=14' numpy matplotlib
ln -sfn /usr/include/infiniband "$CONDA_PREFIX/include/infiniband"
```

### 10.2 构建与索引

```text
cmake -S . -B build \
  -DCMAKE_CXX_COMPILER="$(which x86_64-conda-linux-gnu-g++)" \
  -DENABLE_RDMA=ON -DENABLE_RDMA_CM=OFF -DBUILD_TESTING=ON \
  -DIBVERBS_INCLUDE_DIR="$CONDA_PREFIX/include" \
  -DIBVERBS_LIBRARY=/usr/lib/x86_64-linux-gnu/libibverbs.so
cmake --build build -j"$(nproc)"
ctest --test-dir build --output-on-failure

./build/hnsw_build --generate 10000 --dim 128 --M 16 \
  --ef-construction 200 --seed 42 \
  --output data/index_n10000_d128_M16.bin
```

### 10.3 扫描

```text
./scripts/run_rdma_baselines.sh
```

默认参数（可在脚本头部用环境变量覆盖）：

```text
IB_DEVICE=mlx5_0 IB_PORT=1
RDMA_HOST=127.0.0.1 RDMA_BIND=127.0.0.1 PORT=18515
REPS=3 STATIC_QUERIES=1000 MIXED_QUERIES=500 UPDATES=300
RATIOS=0.1,0.3,0.5 CLIENTS=4 CONC_QUERIES=500
STRATEGIES="baseline1 header_actual cached_length"
CACHE_CAPACITY=10000 EFS=64 K=10 EFC=200 SEED=42 WARMUP=200
```

脚本会为混合阶段每次运行复制索引到 `tmp/` 并启动独立 server；所有 server 日志在 `out/rdma/logs/`，逐查询 CSV 在 `out/rdma/per_query/`，逐更新延迟在 `out/rdma/updates/`。

### 10.4 分析与绘图

```text
python scripts/analyze_rdma_baselines.py \
  --root out/rdma \
  --out-dir out/figures_rdma \
  --tables out/rdma_report_tables.md \
  --warmup 200
```

分析脚本会先校验 99 行 summary 的恒等式；任一违反都会以非零退出码终止。

### 10.5 产物

| 产物 | 路径 |
| --- | --- |
| 静态 summary | `out/rdma/static_summary.csv`（9 行） |
| 混合 summary | `out/rdma/mixed_summary.csv`（54 行） |
| 并发 summary | `out/rdma/concurrency/summary_c1..c4.csv`（共 36 行） |
| 逐查询原始数据 | `out/rdma/per_query/` |
| 逐更新延迟 | `out/rdma/updates/` |
| server 日志 | `out/rdma/logs/` |
| 图表 | `out/figures_rdma/` |
| 自动生成表格 | `out/rdma_report_tables.md` |
| 扫描脚本 | `scripts/run_rdma_baselines.sh` |
| 分析脚本 | `scripts/analyze_rdma_baselines.py` |
| 驱动改动 | `experiments/baseline1_main.cc` |

---

## 11. 最终结论

在 10K/128 维/M=16 的单机 IB 回环环境中，三个 Baseline 的计时表现可以概括为：

- **Baseline 1**：邻居区固定读取，p50 3849 µs，QPS 259.2，总 RDMA READ 操作数 1901.7/query。它是延迟最低的基准，但浪费 16.0% 的邻居区字节。
- **Baseline 2**：消除了邻居区浪费，但每层多一次依赖 READ，邻居操作数 +98.6%、总操作数 +3.8%，p50 +139 µs（+3.6%），QPS −3.4%。在本场景中“省字节”不足以抵消“多一次 RDMA 操作”。
- **Baseline 3**：用计算侧长度缓存把邻居操作数降回接近 B1（+6.3%，比 B2 少 46.5%），同时保留 B2 的 16% 邻居字节收益；静态 p50 与 B1 的配对差仅 +13 µs，属于噪声范围。代价是缓存 miss、upsert fallback 和 erase 过度读取。

因此，在本实验条件下：**B2 是负收益；B3 是“以少量缓存复杂度换取 B2 的字节收益而不增加可测量延迟”的可行折中**。若目标是大幅降低总传输量，仅优化邻居区不够——节点向量读取占了总流量的 99% 以上，后续工作应优先考虑节点/向量读取的批量、缓存或压缩，而不是继续压缩邻居 slot。
