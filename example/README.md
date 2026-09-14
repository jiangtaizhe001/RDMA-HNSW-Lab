# API 使用示例

本目录展示如何编写自己的应用程序并链接 RDMA-HNSW-Lab 库。示例直接使用公开头文件和 `rdma_hnsw` target，不调用项目自带的 `compute_client`。

## 构建

```bash
cd /home/xjs/lesson/RDMA-HNSW-lab
cmake -S . -B build -DENABLE_RDMA=ON -DENABLE_RDMA_CM=ON
cmake --build build -j
cmake -S example -B example/build -DENABLE_RDMA=ON -DENABLE_RDMA_CM=ON
cmake --build example/build -j
```

`example/CMakeLists.txt` 通过 `add_subdirectory` 引入主工程，并链接公开的 `rdma_hnsw` umbrella target。源码中的最小 mmap API 使用示例见 [mmap_client.cc](mmap_client.cc)，RDMA API 使用示例见 [rdma_client.cc](rdma_client.cc)。

## mmap 示例

```bash
./example/run_build.sh
./example/build/mmap_client
```

`mmap_client` 创建 mmap `ComputeNode`，执行查询和 upsert，并为多个查询线程分别创建独立 `ComputeNode`。mmap 更新通过原子快照文件发布，查询不会读到半写文件。

## RDMA 示例

在存储节点准备索引，编辑 [storage-rdma.env](configs/storage-rdma.env)，然后启动：

```bash
./example/run_build.sh
./example/run_storage.sh
```

在计算节点编辑 [compute-rdma.env](configs/compute-rdma.env)，确认存储节点地址、RDMA 设备和端口后执行：

```bash
cmake -S example -B example/build -DENABLE_RDMA=ON
cmake --build example/build -j
./example/run_compute.sh
```

`run_compute.sh` 实际运行本目录自行编写并链接库的 `example/build/rdma_client`。该程序不需要本地索引文件，连接存储节点后自动获取远程索引维度和 offset 表，并创建多个独立 RDMA 连接执行查询和更新。

也可以直接运行：

```bash
./example/build/rdma_client 10.0.0.8 18515 mlx5_0 1
```

参数依次为存储节点地址、服务端口、RDMA 设备名、设备端口号、连接后端（可选 `ib`/`cm`，默认 `ib`）和 CM 超时时间（可选，默认 10000 ms）。

## eRDMA / librdmacm 示例

两端编译时启用 `ENABLE_RDMA=ON` 和 `ENABLE_RDMA_CM=ON`。存储实例准备好索引后，修改 [storage-erdma.env](configs/storage-erdma.env) 中的本机 ERI IP、设备和索引路径；计算实例修改 [compute-erdma.env](configs/compute-erdma.env) 中的存储实例 ERI IP 和本机设备。以下命令从项目根目录执行：

```bash
# 存储实例
./example/run_storage.sh example/configs/storage-erdma.env

# 计算实例
./example/run_compute.sh example/configs/compute-erdma.env
```

在 `example/` 目录执行时，分别使用 `./run_storage.sh configs/storage-erdma.env` 和 `./run_compute.sh configs/compute-erdma.env`。不传配置文件参数仍使用原来的 IB 模式。

CM 模式仍运行本目录的 `rdma_client`，通过链接库创建 `ComputeNode`，不需要计算节点本地索引。示例执行 20 次查询、一次 upsert，以及 4 个查询线程和 1 个更新线程并发；并发阶段更新线程执行 5 次 upsert。每个线程独立拥有 `ComputeNode`，错误会导致非零退出码。示例会修改索引，应使用测试数据。

存储端按会话提供不可变快照：其他客户端的更新需要 `reload()` 或重连才对当前读客户端可见。环境检查、最小自定义客户端、并发和缓存语义、集成测试见 [eRDMA 使用说明](../docs/erdma.md)。

## 配置文件

- [build.env](configs/build.env)：构图参数。
- [storage-rdma.env](configs/storage-rdma.env)：存储节点 RDMA 参数。
- [compute-rdma.env](configs/compute-rdma.env)：计算节点 RDMA 参数。
- [storage-erdma.env](configs/storage-erdma.env)：存储节点 CM/eRDMA 参数。
- [compute-erdma.env](configs/compute-erdma.env)：计算节点 CM/eRDMA 参数。

配置文件中的每个字段都带有注释，可以复制后修改。
