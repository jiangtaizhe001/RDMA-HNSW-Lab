# RDMA-HNSW-Lab

RDMA-HNSW-Lab 是一个面向动态图索引研究的 HNSW 存算分离原型。项目提供 HNSW 图索引构建、索引文件格式、mmap 本地访问、RDMA 远程访问，以及可嵌入应用程序的存储节点和计算节点 API。

项目的目标是提供一个便于二次开发的系统底座。查询调度、读取策略、并发 workload、数据集转换和实验指标由使用者自行决定

科研轮转任务的研究问题、实验协议与实测结论全部在 `docs/` 中，按“计划 + 报告”成对组织，详见 [文档：计划与报告](#文档计划与报告)。

## 文档：计划与报告

`docs/` 下的实验文档按用途分工，每轮实验都是先有**计划**（协议、比较单元、指标口径、验收门禁），后有**报告**（实测数据、置信区间、结论边界）：

- `*-plan.md`：实验协议。固定数据、索引、查询顺序、预算、缓存与重复次数，并预先写明什么算达标、什么算未完成。
- `*-report.md`：对应的实测结果。数字来自 `out/` 下的逐查询原始数据，结论按“可下 / 不可下”分开写。
- `*-design.md`：机制设计（如分级前缀读取的桶、状态机与正确性门禁）。
- `*-progress-*.md` / `*-repair-*.md`：执行过程与故障记录，只追加不追溯改写旧结果。

| 主题 | 计划（协议） | 报告（实测） |
| --- | --- | --- |
| 课题背景与 baseline 定义 | [experiment.md](docs/experiment.md) | — |
| 系统分层与指标口径 | [help.md](docs/help.md) | — |
| Baseline 1：固定读取全部 slot | [baseline1-plan.md](docs/baseline1-plan.md) | [baseline1-report.md](docs/baseline1-report.md) |
| Baseline 2：header-first 依赖读取 | [baseline2-plan.md](docs/baseline2-plan.md) | [baseline2-report.md](docs/baseline2-report.md) |
| Baseline 3：长度缓存与三态读取 | [baseline3-plan.md](docs/baseline3-plan.md) | [baseline3-report.md](docs/baseline3-report.md) |
| 三个 baseline 的真实 IB 10K 计时 | — | [rdma-baseline-report.md](docs/rdma-baseline-report.md) |
| 分级前缀（Quantized Prefix）机制 | [prefix-read-design.md](docs/prefix-read-design.md) | [prefix-read-report.md](docs/prefix-read-report.md) |
| 100K RDMA 前缀粒度扫描 | [prefix-rdma-100k-plan.md](docs/prefix-rdma-100k-plan.md) | [prefix-rdma-100k-report.md](docs/prefix-rdma-100k-report.md) |
| G=32 十种索引规模的 efsearch 阈值 | [prefix-g32-efsearch-plan.md](docs/prefix-g32-efsearch-plan.md) | [prefix-g32-efsearch-report.md](docs/prefix-g32-efsearch-report.md) |
| **真实数据、1M RDMA、重复与并发** | **[real-data-rdma-improvement-plan.md](docs/real-data-rdma-improvement-plan.md)** | **[real-data-rdma-report.md](docs/real-data-rdma-report.md)** |

### 最新一轮：真实数据、1M RDMA 与重复实验

计划 [real-data-rdma-improvement-plan.md](docs/real-data-rdma-improvement-plan.md)
定义了四个研究问题（中间粒度是否有可重复折中、100K→1M 的 READ/字节/时延变化、动态更新收益来源、客户端扩展后的吞吐与公平性），并要求 SIFT1M/GIST1M × 100K/1M × recall 0.95/0.99 × 7 配置，每格 5 次独立重复、真实 RDMA、精确 float64 真值。

报告 [real-data-rdma-report.md](docs/real-data-rdma-report.md) 的结论摘要：

- **M=16 时 G=32 与 B1 完全等价**：READ/query 与请求字节逐项相等，退化为固定读取。
- **中间粒度没有稳定优势**。完整阶梯 G=1/2/4/8/16/32 上，浪费率单调上升 0 → 0.022 → 0.062 → 0.137 → 0.241 → 0.323，而 p50 只在 2367–2403 µs 间波动、均值比多在 ±1.2 % 内。
- **1M 上“少 READ、多字节”更快**：B1/G32 比 B3 快 2–3.6 %（配对区间不含 1），代价是字节 +1.3–1.5 倍；100K 上则无差别，说明层间权衡随规模反转。
- **B2 的依赖读取读次数翻倍**，在 SIFT 与 GIST 上稳定慢 1.3–3.6 %。
- **并发近线性到 8 客户端**：SIFT 100K 525→3353 QPS、1M 290→1669 QPS，Jain ≥0.997，8 客户端时服务端仍未饱和。
- **更新被整份索引重写支配**：单次更新 0.52 s（100K）/ 6.5 s（1M）；100 次更新后检查点质量不变（recall 0.9562 → 0.9560），所有读取策略结果一致。

报告同时登记了不可下的结论与未完成项：受延迟双峰污染的两格不参与策略比较、混合 workload 因更新成本改为固定 100 次更新（不构成比例敏感性实验）、仅单机 IB loopback 因而不能外推跨机 RTT。执行过程见 [real-data-rdma-progress-2026-11-16.md](docs/real-data-rdma-progress-2026-11-16.md)，故障与根因见 [real-data-rdma-repair-2026-11-12.md](docs/real-data-rdma-repair-2026-11-12.md)。

结果与数据的落盘位置：

```text
data/real/{sift,gist}/            子集向量、索引、精确 GT、查询划分、更新日志、检查点 GT
out/real_data_rdma/<campaign>/    每轮命令/日志/summary/per_query + analysis/（表格与置信区间）
out/real_data_rdma/summary_all.md 跨活动汇总（脚本生成，不手抄数字）
```

这两个目录已列入 `.gitignore`（体积与可再生产物），报告中的每个数字都可用 `scripts/` 下的脚本对它们复算。

复现入口：`scripts/run_real_data_pipeline.sh`（数据准备与静态矩阵）、`scripts/run_real_data_pipeline2.sh`（预算 top-up、混合/并发/检查点、构图种子与扩展扫描）；`scripts/analyze_real_rdma.py` 生成单个活动的 `analysis/`，`scripts/summarize_real_rdma_campaigns.py` 生成跨活动汇总。两个流水线都是标记驱动、可中断续跑的。

## 项目组成

```text
include/rdma_hnsw/       公开 C++ 头文件
src/                     库实现和命令行程序
tests/                   单元测试
example/                 独立 API 使用示例
docs/                    实验计划与报告（plan / report 成对）及部署说明
third_party/hnswlib/     vendored hnswlib 头文件
```

核心库按职责划分为 `graph`（构图和索引读写）、`transport`（mmap/RDMA 访问）、`read`（邻居读取策略接口）、`runtime`（存储和计算节点）以及 `common`（公共类型）。命令行程序包括 `hnsw_build`、`storage_server` 和 `compute_client`。

## 构建

```bash
cmake -S . -B build -DENABLE_RDMA=ON -DENABLE_RDMA_CM=ON -DBUILD_TESTING=ON
cmake --build build -j
ctest --test-dir build --output-on-failure
```

只使用 mmap 时可以设置 `-DENABLE_RDMA=OFF`。启用 RDMA 时，CMake 查找 `libibverbs`；启用 CM 时还需要 `librdmacm` 的头文件和库。确认输出包含 `librdmacm connection backend enabled` 后，才能使用 CM 后端。

`--rdma-connection ib`（默认）保留原有 InfiniBand LID 建连方式；`--rdma-connection cm` 使用 librdmacm 按 IP 建连，供 eRDMA 等环境使用。完整部署、环境检查、配置和验证步骤见 [eRDMA 使用说明](docs/erdma.md)。

## 构建索引

```bash
./build/hnsw_build --generate 10000 --dim 128 --M 16 \
  --ef-construction 100 --output data/index.bin
```

也可以从 fvecs 文件构建：

```bash
./build/hnsw_build --input data/base.fvecs --dim 128 \
  --M 16 --ef-construction 100 --output data/index.bin
```

索引文件包含 header、节点 offset 表和节点记录。节点记录包括向量、generation、删除标记以及每层的邻居 slot。

## 作为库使用

应用程序可以链接公开的 `rdma_hnsw` umbrella target：

```cmake
add_subdirectory(/path/to/RDMA-HNSW-lab rdma_hnsw)
target_link_libraries(my_program PRIVATE rdma_hnsw)
```

安装后也可以直接链接静态库。下面对应同时启用 IB 和 CM 的构建（实际库目录以安装输出为准）：

```bash
cmake --install build --prefix "$HOME/.local"
g++ -std=c++20 -I"$HOME/.local/include" app.cc \
  "$HOME/.local/lib/librdma_hnsw_runtime.a" \
  "$HOME/.local/lib/librdma_hnsw_rdma.a" \
  "$HOME/.local/lib/librdma_hnsw_core.a" -lrdmacm -libverbs -pthread -o app
```

RDMA 模式下 `ComputeNodeConfig::index_path` 可以为空，连接后自动获取远程索引元数据；mmap 模式需要提供本地索引路径。

## 独立部署

```bash
./build/storage_server --index /data/index.bin --transport rdma \
  --bind 0.0.0.0 --port 18515 --ib-device mlx5_0 --ib-port 1
```

```bash
./build/compute_client --transport rdma --host 10.0.0.8 \
  --port 18515 --ib-device mlx5_0 --ib-port 1 \
  --queries-count 1000 --ef-search 64 --k 10
```

上述命令默认使用 `ib` 后端，以 TCP 交换连接信息和更新请求。切换到 `cm` 时，两端均添加 `--rdma-connection cm`，使用 RDMA 网络上的实际 IP 地址，控制消息改走 SEND/RECV。两种后端均通过 RDMA READ 访问图数据；每个 session 使用独立 QP、CQ 和 mailbox，更新在存储侧串行处理。

mmap 是共享文件后端，适合单机开发和回归测试：

```bash
./build/compute_client --index data/index.bin --transport mmap --queries-count 1000
```

## API 和并发约定

`StorageNode`、`ComputeNode`、`GraphStore`、`QueryEngine`、`NeighborReadStrategy` 和 `RemoteTransport` 都有公开头文件。一个 `ComputeNode` 实例由一个线程使用；需要并发查询时，为每个线程创建独立 `ComputeNode`，RDMA 模式下每个实例对应独立连接和 QP。存储端负责串行化更新，并以完整索引版本提供查询数据。

`NeighborReadStrategy` 可以通过 `QueryEngine::set_read_strategy()` 注入，不需要修改 HNSW 遍历或 RDMA verbs 层。项目只内置固定 slot 读取策略。
