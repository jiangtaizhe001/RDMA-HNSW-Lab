#include "rdma_hnsw/common/data.h"
#include "rdma_hnsw/runtime/compute_node.h"
#include <iostream>
#include <thread>
#include <vector>

using namespace rdma_hnsw;
static UpdateRequest up(NodeId id, const Vector &v) {
  return {update_op::Upsert, id, v};
}

int main() {
  const std::string index = "example-data/index.bin";
  auto q = generate_vectors(20, 32, 7);
  ComputeNode node({index,
                    TransportKind::Mmap,
                    "127.0.0.1",
                    0,
                    {},
                    1,
                    ReadPolicy::Fixed,
                    DistanceKind::L2,
                    64,
                    10,
                    4,
                    1000});
  auto before = node.query_batch(q);
  auto u = node.update(up(0, q[0]));
  std::cout << "query: count=" << before.queries
            << " elapsed_ms=" << before.elapsed_ms << "\nupdate: ok=" << u.ok
            << " generation=" << u.generation << '\n';

  std::vector<std::thread> readers;
  for (int t = 0; t < 4; ++t)
    readers.emplace_back([&, t] {
      ComputeNode worker({index,
                          TransportKind::Mmap,
                          "127.0.0.1",
                          0,
                          {},
                          1,
                          ReadPolicy::Fixed,
                          DistanceKind::L2,
                          64,
                          10,
                          4,
                          1000});
      std::cout << "concurrent query thread=" << t
                << " count=" << worker.query_batch(q).queries << '\n';
    });
  std::thread writer([&] {
    ComputeNode updater({index,
                         TransportKind::Mmap,
                         "127.0.0.1",
                         0,
                         {},
                         1,
                         ReadPolicy::Fixed,
                         DistanceKind::L2,
                         64,
                         10,
                         4,
                         1000});
    for (NodeId i = 1; i < 6; ++i)
      updater.update(up(i, q[i]));
  });
  for (auto &thread : readers)
    thread.join();
  writer.join();
  std::cout << "concurrent query/update: independent ComputeNode per thread\n";
}
