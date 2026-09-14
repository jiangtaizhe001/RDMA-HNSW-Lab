#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/graph/hnsw_builder.h"
#include <cassert>
#include <cstdio>

int main() {
  using namespace rdma_hnsw;
  std::vector<Vector> vectors;
  for (int i = 0; i < 20; ++i)
    vectors.push_back({static_cast<float>(i), static_cast<float>(i % 3)});
  const char *path = "/tmp/rdma_hnsw_builder_test.bin";
  HnswBuilder::build_file(path, vectors, 4, 20, 7);
  auto c = GraphStore::load_catalog(path);
  assert(c.header.node_count == vectors.size());
  assert(c.header.dim == 2);
  GraphStore g(c);
  auto n = g.read_node(0);
  assert(n.vector.size() == 2);
  assert(!n.neighbors.empty());
  std::remove(path);
  return 0;
}
