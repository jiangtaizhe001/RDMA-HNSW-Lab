# API 使用示例

本目录展示如何编写自己的应用程序并链接 RDMA-HNSW-Lab 库。示例直接使用公开头文件和 `rdma_hnsw` target，不调用项目自带的 `compute_client`。

## 构建

```bash
cd /home/xjs/lesson/RDMA-HNSW-lab
cmake -S . -B build -DENABLE_RDMA=ON
cmake --build build -j
cmake -S example -B example/build -DENABLE_RDMA=ON
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

参数依次为存储节点地址、TCP 控制端口、RDMA 设备名和 RDMA 端口号。

## 配置文件

- [build.env](configs/build.env)：构图参数。
- [storage-rdma.env](configs/storage-rdma.env)：存储节点 RDMA 参数。
- [compute-rdma.env](configs/compute-rdma.env)：计算节点 RDMA 参数。

配置文件中的每个字段都带有注释，可以复制后修改。