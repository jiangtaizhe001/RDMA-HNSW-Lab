#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/read/read_strategy.h"
#include "rdma_hnsw/transport/transport.h"
#include <cassert>
#include <cstdio>

int main() {
  using namespace rdma_hnsw;
  const char *path = "/tmp/rdma_hnsw_read_test.bin";
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
  RemoteNodeView v{0, 1, 0, node_layer_offset(c, 0, 0), c.layer_capacity(0)};
  ReadContext ctx{*t, &m, 4, 100, {}};
  auto r = s->read(v, ctx);
  assert(r.ok && r.neighbors.size() == 2);
  // Layer zero reserves 2*M=8 slots, of which this record uses two.
  assert(m.wasted_bytes == 6 * sizeof(NodeId));
  std::remove(path);
  return 0;
}
