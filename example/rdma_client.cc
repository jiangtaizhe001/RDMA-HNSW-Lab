#include "rdma_hnsw/common/data.h"
#include "rdma_hnsw/runtime/compute_node.h"
#include <atomic>
#include <iostream>
#include <mutex>
#include <thread>
#include <vector>

using namespace rdma_hnsw;
static UpdateRequest up(NodeId id, const Vector &v) {
  return {update_op::Upsert, id, v};
}

int main(int argc, char **argv) {
  if (argc < 3 || argc > 5) {
    std::cerr << "usage: rdma_client <host> <port> [device] [ib-port]\n";
    return 2;
  }
  const std::string device = argc >= 4 ? argv[3] : "mlx5_0";
  const auto ib_port =
      argc == 5 ? static_cast<std::size_t>(std::stoul(argv[4])) : 1;
  ComputeNode node({"", TransportKind::Rdma, argv[1],
                    static_cast<std::size_t>(std::stoul(argv[2])), device,
                    ib_port, ReadPolicy::Fixed, DistanceKind::L2, 64, 10, 4,
                    1000});
  const auto dim = node.query_engine().catalog().header.dim;
  auto q = generate_vectors(20, dim, 7);
  auto before = node.query_batch(q);
  auto u = node.update(up(0, q[0]));
  std::cout << "query: count=" << before.queries
            << " elapsed_ms=" << before.elapsed_ms << "\nupdate: ok=" << u.ok
            << " generation=" << u.generation << '\n';

  std::vector<std::thread> readers;
  std::atomic<std::uint64_t> total_read_ops{0}, total_write_ops{0};
  std::mutex output_mutex;
  for (int t = 0; t < 4; ++t)
    readers.emplace_back([&, t] {
      ComputeNode worker({"", TransportKind::Rdma, argv[1],
                          static_cast<std::size_t>(std::stoul(argv[2])), device,
                          ib_port, ReadPolicy::Fixed, DistanceKind::L2, 64, 10,
                          4, 1000});
      const auto result = worker.query_batch(q);
      total_read_ops.fetch_add(worker.transport().stats().read_ops.load());
      std::lock_guard lock(output_mutex);
      std::cout << "concurrent query thread=" << t
                << " count=" << result.queries << '\n';
    });
  std::thread writer([&] {
    ComputeNode updater({"", TransportKind::Rdma, argv[1],
                         static_cast<std::size_t>(std::stoul(argv[2])), device,
                         ib_port, ReadPolicy::Fixed, DistanceKind::L2, 64, 10,
                         4, 1000});
    for (NodeId i = 1; i < 6; ++i)
      updater.update(up(i, q[i]));
    total_write_ops.fetch_add(updater.transport().stats().write_ops.load());
  });
  for (auto &thread : readers)
    thread.join();
  writer.join();
  std::cout << "concurrent query/update: independent ComputeNode per "
               "thread\nrdma_read_ops="
            << total_read_ops.load()
            << " rdma_write_ops=" << total_write_ops.load() << '\n';
}
