#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/read/read_strategy.h"
#include "rdma_hnsw/transport/transport.h"
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <memory>
#include <span>
#include <string>
#include <vector>

using namespace rdma_hnsw;

namespace {

// In-memory transport used to exercise remote-read shapes (malformed degree,
// truncated/short reads, hard failures) that a well-formed on-disk index cannot
// represent.
class FakeTransport final : public RemoteTransport {
public:
  std::vector<byte> data;
  bool fail{false};
  std::size_t truncate{0}; // if >0, never return more than this many bytes

  ReadResult read(RemoteAddress a, std::span<byte> dst) override {
    if (fail)
      return {false, 0, "injected read failure"};
    const auto off = static_cast<std::size_t>(a.offset);
    if (off > data.size())
      return {true, 0, {}};
    std::size_t n = std::min(data.size() - off, dst.size());
    if (truncate)
      n = std::min(n, truncate);
    if (n)
      std::memcpy(dst.data(), data.data() + off, n);
    return {true, n, {}};
  }
  WriteResult write(RemoteAddress, std::span<const byte>) override {
    return {false, 0, "unsupported"};
  }
  UpdateResult submit_update(const UpdateRequest &) override {
    return {false, 0, "unsupported"};
  }
  const TransportStats &stats() const override { return stats_; }

private:
  TransportStats stats_{};
};

std::string unique_path(const char *stem) {
  return (std::filesystem::temp_directory_path() /
          (std::string(stem) + ".bin"))
      .string();
}

// Verify the neighbor-region byte identity holds for any strategy run.
void check_identity(const ReadMetrics &m) {
  assert(m.requested_bytes == m.useful_bytes + m.wasted_bytes);
}

// --- degree < capacity: wasted slots are counted and excluded --------------
void test_degree_less_than_capacity() {
  const auto path = unique_path("rdma_hnsw_rs_short");
  IndexHeader h{};
  h.dim = 1;
  h.M = 4;
  h.max_level = 0;
  NodeRecord n;
  n.id = 0;
  n.vector = {0};
  n.neighbors = {{1, 2}};
  GraphStore::write(path, h, {n});
  auto c = GraphStore::load_catalog(path);
  auto t = make_mmap_transport(path, false);
  ReadMetrics m;
  auto s = make_read_strategy(ReadPolicy::Fixed);
  assert(std::string(s->name()) == "baseline1_fixed");
  RemoteNodeView v{0, 1, 0, node_layer_offset(c, 0, 0), c.layer_capacity(0)};
  ReadContext ctx{*t, &m, 4, 100, {}};
  auto r = s->read(v, ctx);
  assert(r.ok && r.neighbors.size() == 2);
  assert(r.neighbors[0] == 1 && r.neighbors[1] == 2);
  // Layer zero reserves 2*M=8 slots, of which this record uses two.
  assert(c.layer_capacity(0) == 8);
  assert(m.wasted_bytes == 6 * sizeof(NodeId));
  assert(m.useful_bytes == sizeof(std::uint32_t) + 2 * sizeof(NodeId));
  assert(m.read_ops == 1 && m.layer_reads == 1);
  assert(m.degree_sum == 2 && m.capacity_sum == 8);
  check_identity(m);

  // Empty trailing slots must not be reported as neighbours.
  assert(std::count(r.neighbors.begin(), r.neighbors.end(), 0) == 0);

  // A single real neighbour must yield exactly one result.
  NodeRecord one;
  one.id = 0;
  one.vector = {0};
  one.neighbors = {{3}};
  GraphStore::write(path, h, {one});
  auto c2 = GraphStore::load_catalog(path);
  auto t2 = make_mmap_transport(path, false);
  ReadMetrics m2;
  ReadContext ctx2{*t2, &m2, 4, 100, {}};
  RemoteNodeView v2{0, 1, 0, node_layer_offset(c2, 0, 0), c2.layer_capacity(0)};
  auto r2 = s->read(v2, ctx2);
  assert(r2.ok && r2.neighbors.size() == 1 && r2.neighbors[0] == 3);
  check_identity(m2);

  std::remove(path.c_str());
}

// --- degree == capacity: no waste ------------------------------------------
void test_degree_equals_capacity() {
  const auto path = unique_path("rdma_hnsw_rs_full");
  IndexHeader h{};
  h.dim = 1;
  h.M = 2; // level-0 capacity = 2*M = 4
  h.max_level = 0;
  NodeRecord n;
  n.id = 0;
  n.vector = {0};
  n.neighbors = {{1, 2, 3, 4}};
  GraphStore::write(path, h, {n});
  auto c = GraphStore::load_catalog(path);
  auto t = make_mmap_transport(path, false);
  ReadMetrics m;
  auto s = make_read_strategy(ReadPolicy::Fixed);
  RemoteNodeView v{0, 1, 0, node_layer_offset(c, 0, 0), c.layer_capacity(0)};
  ReadContext ctx{*t, &m, 4, 100, {}};
  auto r = s->read(v, ctx);
  assert(c.layer_capacity(0) == 4);
  assert(r.ok && r.neighbors.size() == 4);
  assert(m.wasted_bytes == 0);
  assert(m.useful_bytes == m.requested_bytes);
  check_identity(m);
  std::remove(path.c_str());
}

// --- multi-layer node: per-level capacity and offsets ----------------------
void test_multi_level_node() {
  const auto path = unique_path("rdma_hnsw_rs_levels");
  IndexHeader h{};
  h.dim = 1;
  h.M = 2; // cap(0)=4, cap(l>0)=2
  h.max_level = 2;
  std::vector<NodeRecord> nodes(6);
  for (NodeId i = 0; i < nodes.size(); ++i) {
    nodes[i].id = i;
    nodes[i].vector = {static_cast<float>(i)};
  }
  nodes[0].level = 2;
  nodes[0].neighbors = {{1, 2, 3, 4}, {5, 1}, {2, 3}};
  GraphStore::write(path, h, nodes);
  auto c = GraphStore::load_catalog(path);
  auto t = make_mmap_transport(path, false);
  auto s = make_read_strategy(ReadPolicy::Fixed);
  assert(c.layer_capacity(0) == 4 && c.layer_capacity(1) == 2 &&
         c.layer_capacity(2) == 2);

  const std::vector<std::vector<NodeId>> expected = {
      {1, 2, 3, 4}, {5, 1}, {2, 3}};
  for (std::uint32_t level = 0; level <= h.max_level; ++level) {
    ReadMetrics m;
    ReadContext ctx{*t, &m, 4, 100, {}};
    RemoteNodeView v{0, 1, level, node_layer_offset(c, 0, level),
                     c.layer_capacity(level)};
    auto r = s->read(v, ctx);
    assert(r.ok);
    assert(r.neighbors == expected[level]);
    assert(m.wasted_bytes == 0);
    check_identity(m);
  }
  std::remove(path.c_str());
}

// --- malformed degree > capacity: truncate, never over-read ----------------
void test_degree_greater_than_capacity() {
  FakeTransport t;
  // Leading degree word claims 100 neighbours; only a handful are present.
  const std::uint32_t fake_degree = 100;
  t.data.resize(sizeof(fake_degree) + 8 * sizeof(NodeId), byte{0});
  std::memcpy(t.data.data(), &fake_degree, sizeof(fake_degree));
  for (std::size_t i = 0; i < 8; ++i) {
    const NodeId id = static_cast<NodeId>(i + 1);
    std::memcpy(t.data.data() + sizeof(fake_degree) + i * sizeof(NodeId), &id,
                sizeof(id));
  }
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  auto s = make_read_strategy(ReadPolicy::Fixed);
  RemoteNodeView v{0, 1, 0, 0, /*capacity=*/4};
  auto r = s->read(v, ctx);
  assert(r.ok);
  assert(r.neighbors.size() == 4); // truncated to capacity
  assert(r.neighbors[0] == 1 && r.neighbors[3] == 4);
  assert(m.wasted_bytes == 0);
  check_identity(m);
}

// --- capacity == 0: error, no crash ----------------------------------------
void test_zero_capacity() {
  FakeTransport t;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  auto s = make_read_strategy(ReadPolicy::Fixed);
  RemoteNodeView v{0, 1, 0, 0, /*capacity=*/0};
  auto r = s->read(v, ctx);
  assert(!r.ok);
  assert(m.read_ops == 0); // rejected before touching the transport
}

// --- short / failed reads must surface an error ----------------------------
void test_short_and_failed_reads() {
  auto s = make_read_strategy(ReadPolicy::Fixed);
  {
    FakeTransport t;
    t.data.resize(10, byte{0}); // fewer bytes than the capacity needs
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    RemoteNodeView v{0, 1, 0, 0, /*capacity=*/4};
    auto r = s->read(v, ctx);
    assert(!r.ok && r.neighbors.empty());
  }
  {
    FakeTransport t;
    t.fail = true;
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    RemoteNodeView v{0, 1, 0, 0, /*capacity=*/4};
    auto r = s->read(v, ctx);
    assert(!r.ok && r.neighbors.empty());
  }
}

} // namespace

int main() {
  test_degree_less_than_capacity();
  test_degree_equals_capacity();
  test_multi_level_node();
  test_degree_greater_than_capacity();
  test_zero_capacity();
  test_short_and_failed_reads();
  std::puts("read_strategy_test: all cases passed");
  return 0;
}
