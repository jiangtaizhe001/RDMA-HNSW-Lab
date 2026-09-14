#include "rdma_hnsw/graph/graph_store.h"
#include <cassert>
#include <cstdio>
#include <vector>

int main() {
  using namespace rdma_hnsw;
  const char *path = "/tmp/rdma_hnsw_graph_store_test.bin";
  IndexHeader h{};
  h.dim = 2;
  h.M = 2;
  h.max_level = 0;
  h.entrypoint = 0;
  NodeRecord a;
  a.id = 0;
  a.vector = {1, 2};
  a.neighbors = {{1}};
  NodeRecord b;
  b.id = 1;
  b.vector = {3, 4};
  b.neighbors = {{0}};
  GraphStore::write(path, h, {a, b});
  auto c = GraphStore::load_catalog(path);
  assert(c.header.node_count == 2);
  assert(c.header.dim == 2);
  GraphStore g(c);
  auto x = g.read_node(1);
  assert(x.vector[1] == 4);
  assert(x.neighbors[0][0] == 0);
  std::remove(path);
  return 0;
}
