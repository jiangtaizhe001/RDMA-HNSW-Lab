# 使用 librdmacm 在 eRDMA 实例上运行

本文介绍连接后端、部署和 API 使用。科研轮转的任务与实验要求见 [experiment.md](experiment.md)。

## 1. 选择连接后端

`--transport rdma` 下有两种连接方式，两端必须选择同一种：

| 参数 | 建连与控制消息 | 图数据访问 | 使用条件 |
| --- | --- | --- | --- |
| `--rdma-connection ib`（默认） | TCP 交换 QP/LID 信息，手动设置 QP | ibverbs RDMA READ/WRITE | 原有 InfiniBand 环境，需要有效 LID |
| `--rdma-connection cm` | librdmacm 按 IP 建连；控制消息走 QP 的 SEND/RECV | ibverbs RDMA READ/WRITE | eRDMA，或已配置可用 IP 路径的 RDMA_CM 环境 |

`cm` 后端不依赖 LID，不手动设置 INIT/RTR/RTS，也不额外建立普通 TCP 控制 socket。它使用 `RDMA_PS_TCP`、RC QP 和标准 verbs。`mmap` 仍用于没有 RDMA 的单机开发。

阿里云的 eRDMA Standard 模式要求通过 RDMA_CM 建连；本后端按此接口实现，无需为了运行本项目切换到 Compat 模式。驱动、ERI 和运行模式的要求以[阿里云 eRDMA 使用说明](https://www.alibabacloud.com/help/en/ecs/user-guide/erdma-usage/)为准。当前未在实际 eRDMA 实例上完成验收，部署后应先执行本文的连通性检查和集成测试。

## 2. 准备实例与编译

在两台实例上准备相互可达的 ERI 网络，安装云厂商要求的 eRDMA 内核驱动和用户态 provider。安装 `librdmacm` 开发包本身不会安装或配置完整的 eRDMA 环境。

Ubuntu/Debian 的常规编译依赖示例（已安装云厂商 RDMA 软件栈时，使用与其匹配的开发包）：

```bash
sudo apt-get install build-essential cmake libibverbs-dev librdmacm-dev
```

两端分别检查：

```bash
ibv_devices
ibv_devinfo
ip -brief address
ip route get 10.0.0.8  # 在计算实例上替换为存储实例 ERI 对应的 IP
ulimit -l
```

设备名以 `ibv_devices` 为准，配置中的 `erdma_0` 只是示例。CM 依据 IP 路由选卡；填写 `--ib-device erdma_0` 仅校验路由选中的设备，不会强制流量改走该设备。请使用 ERI 对应网卡的实际 IPv4 地址；管理网可 ping 通不等于 RDMA_CM 可连通。当前后端只解析 IPv4。

先用 rdma-core 自带的 `rping` 验证同一对地址。示例中的地址和端口应按实际网络修改：

```bash
# 存储实例
rping -s -a 10.0.0.8 -p 18516

# 计算实例，另一个终端/实例运行
rping -c -a 10.0.0.8 -p 18516 -C 10 -v
```

`rping` 所在的软件包名称随发行版而异。若它也不能连接，先检查 ERI、路由、驱动和云网络访问规则。不要将 `127.0.0.1` 当作跨实例地址；同机 CM loopback 是否可用取决于设备、provider 和网络配置。

在项目根目录编译：

```bash
cmake -S . -B build -DENABLE_RDMA=ON -DENABLE_RDMA_CM=ON -DBUILD_TESTING=ON
cmake --build build -j
cmake -S example -B example/build -DENABLE_RDMA=ON -DENABLE_RDMA_CM=ON
cmake --build example/build -j
```

确认 CMake 输出包含 `librdmacm connection backend enabled`。如果缺少依赖，mmap/已有 IB 后端仍可构建，但选择 `cm` 会明确报错，不会自动退回 mmap。独立示例有自己的 CMake 构建目录，也必须开启这两个选项。

## 3. 独立存储节点和自行链接库的客户端

以下命令均从项目根目录执行。只需在存储实例构建索引；计算实例不需要索引文件。

```bash
# 存储实例：此处用小规模随机数据验证功能
./build/hnsw_build --generate 4096 --dim 32 --M 16 \
  --output example-data/index.bin
```

修改 [storage-erdma.env](../example/configs/storage-erdma.env) 中的 `BIND`、`INDEX` 和设备名，然后启动：

```bash
./example/run_storage.sh example/configs/storage-erdma.env
```

在计算实例修改 [compute-erdma.env](../example/configs/compute-erdma.env) 中的 `STORAGE_HOST` 和本机设备名，然后运行：

```bash
./example/run_compute.sh example/configs/compute-erdma.env
```

如果工作目录是 `example/`，对应命令是 `./run_storage.sh configs/storage-erdma.env` 和 `./run_compute.sh configs/compute-erdma.env`。脚本参数是配置文件路径；不传参数时仍使用原有 IB 配置。

计算脚本运行的是自行链接库的 [example/rdma_client.cc](../example/rdma_client.cc)。它先执行 20 次查询和一次 upsert，再创建 4 个查询线程与 1 个更新线程；每个线程独立创建 `ComputeNode`，更新线程执行 5 次 upsert。失败会打印原因并返回非零退出码。该示例会修改指定的索引，请使用为本次功能验证准备的数据。

也可以直接启动同样的服务和示例：

```bash
# 存储实例
./build/storage_server --index example-data/index.bin --transport rdma \
  --rdma-connection cm --bind 10.0.0.8 --port 18515 \
  --ib-device erdma_0 --ib-port 1 --rdma-timeout-ms 10000

# 计算实例
./example/build/rdma_client 10.0.0.8 18515 erdma_0 1 cm 10000
```

将设备参数设为空字符串 `""` 可以取消设备名校验，设备端口号仍会校验。

## 4. 在自己的程序中使用

使用 CMake 的 `rdma_hnsw` target 可自动传递 `libibverbs`、`librdmacm` 和线程库依赖：

```cmake
cmake_minimum_required(VERSION 3.18)
project(my_experiment LANGUAGES CXX)
set(ENABLE_RDMA ON CACHE BOOL "Build verbs transport")
set(ENABLE_RDMA_CM ON CACHE BOOL "Build CM connection backend")
add_subdirectory(/path/to/RDMA-HNSW-lab rdma_hnsw_lab)
add_executable(my_client my_client.cc)
target_compile_features(my_client PRIVATE cxx_std_20)
target_link_libraries(my_client PRIVATE rdma_hnsw)
```

`my_client.cc` 的最小查询和更新程序：

```cpp
#include "rdma_hnsw/runtime/compute_node.h"
#include <exception>
#include <iostream>
#include <stdexcept>

int main() {
  using namespace rdma_hnsw;
  try {
    ComputeNodeConfig config;
    config.transport = TransportKind::Rdma;
    config.rdma_connection = RdmaConnection::Cm;
    config.host = "10.0.0.8";  // 存储实例 ERI IP
    config.port = 18515;
    config.ib_device = "";    // 按路由选卡；可填写本机实际设备名来校验
    config.ib_port = 1;
    config.rdma_timeout_ms = 10000;
    config.ef_search = 64;
    config.k = 10;
    // RDMA 模式不设置 index_path。
    ComputeNode node(config);
    const auto dim = node.query_engine().catalog().header.dim;
    Vector query(dim, 0.5f);  // 实验时替换为自己的查询数据
    const auto ids = node.query(query);
    std::cout << "neighbors=" << ids.size() << '\n';
    const auto update = node.update({update_op::Upsert, 0, query});
    if (!update.ok) throw std::runtime_error(update.error);
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
```

嵌入存储服务使用 `rdma_hnsw/runtime/storage_node.h`，设置 `StorageNodeConfig::index_path`、`transport = TransportKind::Rdma`、`rdma_connection = RdmaConnection::Cm`、`bind` 和 `port`，构造 `StorageNode` 后调用阻塞的 `serve()`。网络服务运行期间，更新应通过计算节点提交，避免同时调用独立 `StorageEngine`/`StorageNode::apply()` 修改同一个索引文件。

## 5. CM 后端如何工作

客户端依次执行地址解析、路由解析、创建 QP、`rdma_connect()`；服务端执行 `rdma_bind_addr()`、`rdma_listen()`，把每个 CONNECT_REQUEST 迁移到独立的事件通道后由独立线程调用 `rdma_accept()`。双方在建连前投递接收缓冲区，连接成功后由客户端先发送 Hello。

服务端回复当前会话的索引快照地址、长度、rkey 和独立更新 mailbox 的描述符。客户端通过 RDMA READ 获取索引 header 和 offset 表，之后仍走原有 `QueryEngine` 和 `NeighborReadStrategy` 接口。单次 `read()` 大于 1 MiB 时，后端拆分为多个 WR 并逐段等待完成。

一次更新的路径是：向该会话的 mailbox RDMA WRITE 向量，等待完成，SEND 更新命令，服务端应用更新并发布该会话的新快照，回复结果及新内存描述符。Erase 不需要写向量，因此成功更新次数不一定等于 RDMA WRITE 次数。`TransportStats` 的 READ/WRITE 计数只统计完成的数据 WR，不包括控制 SEND/RECV，不能直接当作所有网络操作数量。

每个会话独立持有 QP、发送/接收 CQ、控制 MR、数据暂存区和 mailbox。所有客户端共享一个存储更新引擎，应用更新与准备新快照在存储端串行执行。读者访问各自的不可变快照，所以其他会话更新不会覆盖正在被 RDMA READ 的内存。图快照 MR 仅授予远程读取权限，`RemoteTransport::write()` 不允许直接改图；更新必须走 `submit_update()` 或 `ComputeNode::update()`。

需要理解的可见性和开销边界：

- 这是会话快照模型。更新成功后，提交更新的会话切换到新快照；其他已有会话继续读旧快照。调用 `ComputeNode::reload()` 或重连才能看到其他客户端的新版本，不是实时广播更新。
- CM 模式每次成功更新后刷新 header 和 offset 表，防止其他客户端的插入造成地址表过期。当前 `reset_catalog()` 会清空内置长度缓存；自定义读取策略对象仍保留。`reload()` 则重建整个传输和查询引擎，策略、缓存及其统计需由应用重新初始化。因此，设计缓存失效实验时应明确快照刷新和缓存生命周期，不能仅启动写线程就假定长连接读线程已经观察到更新。
- 服务端每个会话持有一份完整索引字节副本并注册 MR；更新发布期间提交更新的会话还短暂持有新旧两份。现有存储引擎也会维护图数据并写出完整索引文件。内存、memlock 和更新耗时会随索引规模、客户端数增长；此实现并未提供增量持久化或共享快照回收系统。
- 控制帧有协议版本和布局检查；索引文件及浮点向量仍使用项目现有的 little-endian 本机布局，建议两端使用相同版本和 ABI 的构建。
- `rdma_timeout_ms` 限制每个建连阶段、每个控制/数据完成等待，默认 10000 ms；不是整个查询或所有建连阶段的总预算，也不限制服务端空闲连接时长。存储端更新处理较慢时，可以在两端提高该值（最大 600000 ms）。
- 传输错误/超时后连接不可继续复用，需要重连。更新失败且提示 `update/publish failed` 或传输中断时，磁盘提交可能已发生；后端不自动重试写操作。检查已提交状态后再决定后续操作。

代码入口为 [rdma_cm_transport.cc](../src/transport/rdma_cm_transport.cc) 和 [cm_protocol.h](../src/transport/cm_protocol.h)。

## 6. 测试与排查

不需要 RDMA 设备的测试：

```bash
ctest --test-dir build --output-on-failure
```

CM 硬件集成测试默认显示 `Skipped`。两机测试时，在存储实例单独准备一个可修改的测试索引（至少 16 个节点，索引文件大于 1 MiB；推荐使用上文的 4096 个 32 维向量），按上文启动 CM 服务，在计算实例执行：

```bash
RDMA_CM_TEST_HOST=10.0.0.8 RDMA_CM_TEST_PORT=18515 \
  RDMA_CM_TEST_DEVICE=erdma_0 ./build/cm_integration_test
```

此测试覆盖握手、分段 READ、更新错误后继续使用连接、旧快照稳定性、reload、4 个读客户端与 2 个写客户端并发、插入/删除、多客户端地址表更新和重连。测试会修改数据，不能指向正式实验索引。

如果设备和网络支持同机 CM 通信，可以从项目根目录执行自动创建临时索引和存储进程的脚本：

```bash
./scripts/test_rdma_cm.py --host 本机RDMA网络IPv4 --bind 本机RDMA网络IPv4 --device 实际设备名
```

脚本只终止自身启动的存储进程，不修改网卡配置；`--build` 可以指定其他构建目录。两台 eRDMA 实例之间的测试应使用前述独立服务和客户端命令。

| 现象 | 检查方向 |
| --- | --- |
| `RDMA CM backend unavailable` | 主工程和示例是否都开启 RDMA/CM，是否找到匹配的开发库，是否仍运行旧二进制 |
| `rdma_create_event_channel` 失败 | RDMA CM 内核支持、设备节点、容器/沙箱是否允许访问 `/dev/infiniband/rdma_cm` |
| 地址/路由解析失败或超时 | ERI IP、选路、驱动和访问规则，先用相同地址的 `rping` 对照 |
| `RDMA_CM_EVENT_REJECTED` | 服务监听、资源、两端配置和 provider；状态码含义与链路相关，不能一律当作 `errno` 解释 |
| `device/port assertion failed` | CM 路由选择的设备与 `IB_DEVICE`/`IB_PORT` 不符；检查 IP 路由，或清空设备名校验 |
| MR 注册失败 | memlock、设备可注册内存、索引大小与会话数量 |
| 控制/数据操作超时 | 对端退出、网络、存储端更新耗时或资源不足；重连前检查更新是否已提交 |