## 1. 系统整体设计

系统将一次 HNSW 查询拆成四层：

```text
应用 workload
    ↓
ComputeNode / QueryEngine
    ↓
NeighborReadStrategy
    ↓
RemoteTransport（mmap 或 RDMA）
    ↓
StorageNode / 索引文件
```

`QueryEngine` 负责 HNSW 搜索流程：读取节点 header 和向量、计算距离、维护候选队列、访问节点的每一层邻居。它不应该知道某种邻居读取策略如何取得 degree，也不应该直接调用 verbs。

`NeighborReadStrategy` 只负责一件事：给定一个节点某一层的远程地址和固定容量，读取并返回有效邻居 ID。三个 Baseline 和分级前缀机制都应实现这个接口。

`RemoteTransport` 屏蔽传输方式：

- mmap 后端使用本地文件的 `pread`；
- RDMA 后端使用 one-sided RDMA READ/WRITE；
- 策略只调用 `ReadContext::transport.read()`，不直接依赖 ibverbs。

## 2. 索引文件和远程地址

索引由三部分组成：

```text
IndexHeader
Offset[node_count]
NodeRecord[0 ... node_count-1]
```

`IndexHeader` 中的重要字段包括：

- `dim`：向量维度；
- `M`：HNSW 参数；
- `max_level`：最大层数；
- `entrypoint`：查询入口节点；
- `node_count`：节点数；
- `generation`：索引版本；
- `offsets_begin`：offset 表起始位置；
- `records_begin`：节点记录起始位置。

每个节点记录的磁盘布局是：

```text
NodeDiskHeader
vector[dim]
for level = 0 ... max_level:
    uint32 degree
    NodeId slots[layer_capacity(level)]
```

其中：

```cpp
layer_capacity(0) = 2 * M;
layer_capacity(level > 0) = M;
```

给定 `IndexCatalog catalog`、节点 ID 和层号，可以使用：

```cpp
Offset p = node_layer_offset(catalog, node_id, level);
```

该地址指向该层的 `degree` 字段。邻居 slot 紧跟在 degree 后面：

```text
p + sizeof(uint32_t)
```

注意 degree 是实际有效邻居数量，slot 容量是该层固定分配容量。Baseline1 的差异就在于是否读取容量中的全部 slot。

## 3. 查询过程

`QueryEngine::search()` 的主要步骤如下：

1. 从 catalog 的 entrypoint 开始；
2. 读取当前节点的 `NodeDiskHeader` 和向量；
3. 计算当前节点与查询向量的距离；
4. 对当前节点的每一层调用 `strategy_->read(view, context_)`；
5. 将返回的邻居 ID 放入候选队列；
6. 重复扩展，直到达到 `ef_search`；
7. 从候选集合中返回前 `k` 个节点。

策略返回的邻居 ID 必须满足：

- 只能来自实际读取到的 slot；
- 不能超过 `view.capacity`；
- 需要检查读取长度和返回字节数；
- 发现传输错误时返回 `{ {}, false, error }`。

## 4. 策略接口

公开接口位于 `include/rdma_hnsw/read/read_strategy.h`：

```cpp
class NeighborReadStrategy {
public:
  virtual ~NeighborReadStrategy() = default;
  virtual NeighborReadResult read(const RemoteNodeView&, ReadContext&) = 0;
  virtual const char* name() const = 0;
};
```

`RemoteNodeView` 提供：

```cpp
NodeId id;                  // 节点 ID
Generation generation;      // 节点版本
uint32_t level;             // 当前层
Offset layer_offset;        // degree 字段地址
size_t capacity;            // 当前层固定 slot 容量
```

`ReadContext` 提供：

```cpp
RemoteTransport& transport; // 读取远程数据
ReadMetrics* metrics;       // 可选统计对象
size_t prefix_granularity;  // 分级前缀粒度
size_t cache_capacity;      // 缓存容量
length_cache;               // 默认可保存 (generation, degree)，乐观缓存可主要使用 degree
```

基本读取代码可以写成：

```cpp
std::vector<std::byte> bytes(n);
auto result = ctx.transport.read({offset, 0}, bytes);
if (!result.ok || result.bytes != n)
    return {{}, false, "short remote read"};
```

不要在策略中创建 `ComputeNode`、修改 catalog 或直接操作 `ibv_qp`。这些职责属于上层或 transport。

## 5. Baseline1：完整读取所有邻居 slot

原型内置的 `ReadPolicy::Fixed` 已经实现了该模式，可以直接作为参考和 Baseline1：

```text
读取长度 = sizeof(degree) + capacity * sizeof(NodeId)
```

读取完成后解析 degree，但传输已经包含全部固定 slot。统计时：

```text
requested_bytes = sizeof(degree) + capacity * sizeof(NodeId)
useful_bytes    = sizeof(degree) + degree * sizeof(NodeId)
wasted_bytes    = (capacity - degree) * sizeof(NodeId)
```

实现或验证时需要注意：即使空 slot 的值是 0，也不能把它们当作有效邻居；只能返回前 `degree` 个 slot。

Baseline1 的优点是只有一次读取、实现简单、没有依赖往返。缺点是实际 degree 较小时传输了大量无效 slot。

## 6. Baseline2：先读 header，再读实际邻居长度

Baseline2 将一次固定读取拆成两次：

```text
第一次：读取 sizeof(uint32_t)，获得 degree
第二次：读取 degree * sizeof(NodeId)，获得有效邻居
```

伪代码：

```cpp
uint32_t degree = 0;
read(view.layer_offset, &degree, sizeof(degree));
degree = std::min<uint32_t>(degree, view.capacity);
read(view.layer_offset + sizeof(degree), neighbors,
     degree * sizeof(NodeId));
```

建议统计：

- `header_reads++`；
- `dependent_reads++`；
- `read_ops`；
- 两次读取的总延迟；
- 与 Baseline1 相比减少的字节数。

第二次读取依赖第一次读取的结果，RDMA 下会产生额外的操作完成等待；不要把两次读取合并，否则就失去了 Baseline2 的实验含义。

异常情况包括 degree 大于容量、header 读取不足、第二次读取失败。degree 应截断到 `view.capacity`，不能根据远程数据构造越界长度。

## 7. Baseline3：计算侧缓存邻居长度

Baseline3 维护如下缓存：

```text
key   = node_id（也可以进一步区分 level）
value = degree
```

读取流程：

1. 查找 `ctx.length_cache[view.id]`；
2. 命中时直接按照缓存 degree 发起一次邻居读取；
3. 第一次读取仍应包含 degree 字段，用于验证缓存预测；
4. 如果真实 degree 大于缓存 degree，说明缓存 under-hint，fallback 读取剩余邻居；
5. 如果真实 degree 小于或等于缓存 degree，可以直接返回有效邻居，同时用真实 degree 更新缓存；
6. 未命中时读取一个初始范围，解析 degree 后建立缓存；
7. 超过 `cache_capacity` 时执行淘汰。

伪代码：

```cpp
uint32_t cached_degree = 0;
auto it = ctx.length_cache.find(view.id);
if (it != ctx.length_cache.end()) {
    cached_degree = it->second.second;
    metrics->cache_hits++;
    // 按 cached_degree 读取 degree 字段和邻居前缀。
    // 若真实 degree > cached_degree，再 fallback 读取剩余邻居。
} else {
    metrics->cache_misses++;
    // 读取初始范围，得到真实 degree 并建立缓存
}
```

Baseline3 不要求在读取前检查 generation。它采用乐观预测：缓存可能过期，但第一次读取会带回 degree；当真实 degree 超过预测范围时执行 fallback，随后用真实 degree 更新缓存。建议分别测试：

- 无更新时的稳定命中率；
- 固定更新比例下的命中率；
- 高频更新下的失效率；
- 不同 cache capacity；
- 更新目标集中或均匀分布时的差异。

## 8. 分级前缀读取机制

本课题中的分级前缀读取是 Baseline2 和 Baseline3 之间的折中。它仍在计算侧缓存每个节点（或节点每层）的邻居长度，但缓存的是按粒度量化后的长度等级。命中时直接使用等级计算传输宽度，避免 Baseline2 的前置 header 往返；代价是向上取整产生少量传输浪费。它与 Baseline3 一样采用乐观缓存读取，不要求读取前先确认 generation。

设实际邻居长度为 `degree`、分级粒度为 `G`，则：

```text
level = ceil(degree / G)
hinted_degree = min(level * G, capacity)
read_bytes = sizeof(degree) + hinted_degree * sizeof(NodeId)
```

例如 `capacity=32`、`G=4`、缓存等级为 3 时，计算侧按 12 个邻居的宽度读取。

一种分级布局是：

```text
capacity = 32, granularity = 4
prefix 1: slot [0, 4)
prefix 2: slot [4, 8)
prefix 3: slot [8, 12)
...
```

建议的实现步骤：

1. 查找节点缓存的等级；
2. 命中时将等级乘以 `prefix_granularity`，得到 hinted degree；
3. 按 hinted degree 一次读取 degree 字段和邻居前缀；
4. 解析真实 degree；
5. 若真实 degree 不超过 hinted degree，返回有效邻居，并用真实 degree 更新等级；
6. 若真实 degree 大于 hinted degree，记录 under-hint，并 fallback 读取剩余邻居或完整邻居区域；
7. 用真实 degree 重新计算等级并更新缓存；
8. 统计首次分级读取的字节、fallback 次数和 fallback 读取字节。

关键点是“缓存等级预测一次读取宽度，预测不足时再 fallback”，而不是根据候选队列逐级决定是否继续读取。它应当以少量向上取整的传输浪费换取更高的缓存命中率和更少的 header 往返。

注意：`NeighborReadStrategy::read()` 返回时必须给出完整的邻居集合。第一次分级读取不足时，必须在该函数内完成 fallback，不能把未读取的 slot 伪造为有效邻居。

可以考虑的分级依据包括：

- 固定 prefix 粒度；
- 缓存中的 degree 估计；
- 高层和底层使用不同粒度；
- 对频繁更新节点采用更保守的等级。

必须明确并验证：

- prefix 粒度为 1、2、4、8、16 时的变化；
- degree 小于一个 prefix 时的读取量；
- degree 超出 hint 时的 fallback 次数；
- 缓存命中和缓存失效时的行为；
- 缓存 hint 小于真实 degree 时是否正确 fallback；
- 缓存 hint 大于真实 degree 时是否正确计算传输浪费并收缩缓存等级；
- 读取失败时是否安全返回错误。

## 9. 如何把策略接入 QueryEngine

项目默认只创建 fixed 策略。学生策略通常在自己的应用中构造并注入：

```cpp
class MyStrategy final : public rdma_hnsw::NeighborReadStrategy {
public:
  rdma_hnsw::NeighborReadResult read(
      const rdma_hnsw::RemoteNodeView& view,
      rdma_hnsw::ReadContext& context) override {
    // 实现自己的读取逻辑
  }
  const char* name() const override { return "my_strategy"; }
};

node.query_engine().set_read_strategy(
    std::make_unique<MyStrategy>());
```

命令行解析器中的 `header_actual`、`cached_length` 和 `prefix` 名称只是为实验扩展保留的名称；当前核心库不会自动创建未实现的策略。建议实验程序直接通过 API 注入，避免修改核心工厂。

## 10. 指标如何记录

`QueryStats` 和 `ReadMetrics` 是可选的内存统计，不会自动生成实验文件。常用字段含义如下：

| 字段 | 含义 |
| --- | --- |
| `requested_bytes` | 策略请求的邻居字节数 |
| `useful_bytes` | header 加有效邻居所占字节数 |
| `wasted_bytes` | 已请求但不包含有效邻居的字节数 |
| `read_ops` | 策略发起的读取次数 |
| `header_reads` | header/degree 读取次数 |
| `dependent_reads` | 依赖前一次读取结果的后续读取次数 |
| `cache_hits` / `cache_misses` | 长度缓存命中和未命中 |
| `cache_invalidations` | 缓存预测被真实 degree 证伪后的失效/修正次数 |
| `prefix_reads` | 分级前缀读取次数 |
| `fallbacks` | 策略回退到完整读取或其他路径的次数 |

建议实验程序在每轮 workload 结束后保存：

```text
strategy, dataset, node_count, threads, update_ratio,
queries, updates, latency, qps, requested_bytes,
useful_bytes, wasted_bytes, read_ops, cache_hit_rate
```

不要只记录平均延迟。建议同时记录 p50、p95、p99、更新延迟和 recall。
