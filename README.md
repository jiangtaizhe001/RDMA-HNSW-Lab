# RDMA-HNSW-Lab

RDMA-HNSW-Lab 是一个面向动态图索引研究的 HNSW 存算分离原型。项目提供 HNSW 图索引构建、索引文件格式、mmap 本地访问、RDMA 远程访问，以及可嵌入应用程序的存储节点和计算节点 API。

项目的目标是提供一个便于二次开发的系统底座。查询调度、读取策略、并发 workload、数据集转换和实验指标由使用者自行决定

科研轮转任务相关内容与要求见`docs/` 

## 项目组成

```text
include/rdma_hnsw/       公开 C++ 头文件
src/                     库实现和命令行程序
tests/                   单元测试
example/                 独立 API 使用示例
docs/                    实验说明
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
