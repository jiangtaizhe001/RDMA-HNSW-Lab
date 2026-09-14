#include "rdma_hnsw/common/data.h"
#include "rdma_hnsw/runtime/compute_node.h"
#include <atomic>
#include <iostream>
#include <latch>
#include <syncstream>
#include <thread>
#include <vector>

using namespace rdma_hnsw;

int main(int argc, char **argv) {
  try {
    if (argc < 3 || argc > 7) {
      std::cerr << "usage: rdma_client <host> <port> [device] [device-port] "
                   "[ib|cm] [timeout-ms]\n";
      return 2;
    }
    ComputeNodeConfig config;
    config.transport = TransportKind::Rdma;
    config.host = argv[1];
    config.port = std::stoul(argv[2]);
    config.ib_device = argc >= 4 ? argv[3] : "";
    config.ib_port = argc >= 5 ? std::stoul(argv[4]) : 1;
    const std::string connection = argc >= 6 ? argv[5] : "ib";
    if (connection != "ib" && connection != "cm")
      throw std::invalid_argument("connection must be ib or cm");
    config.rdma_connection =
        connection == "cm" ? RdmaConnection::Cm : RdmaConnection::Ib;
    config.rdma_timeout_ms = argc >= 7 ? std::stoul(argv[6]) : 10000;
    std::vector<Vector> queries;
    std::size_t nodes;
    {
      ComputeNode node(config);
      nodes = node.query_engine().catalog().header.node_count;
      if (!nodes)
        throw std::runtime_error("example requires a nonempty index");
      queries =
          generate_vectors(20, node.query_engine().catalog().header.dim, 7);
      auto result = node.query_batch(queries);
      auto update = node.update({update_op::Upsert, 0, queries[0]});
      if (!update.ok)
        throw std::runtime_error("initial update failed: " + update.error);
      std::cout << "query: count=" << result.queries
                << " elapsed_ms=" << result.elapsed_ms
                << "\nupdate: ok=1 generation=" << update.generation << '\n';
    }
    // Application-owned scheduling: every thread owns an independent node/QP.
    std::atomic<unsigned> failed{0}, updates{0};
    std::atomic<std::uint64_t> reads{0}, writes{0};
    std::latch start(1);
    std::vector<std::jthread> workers;
    auto workload = [&](int worker_id) {
      start.wait();
      try {
        ComputeNode node(config);
        if (worker_id < 4) {
          auto result = node.query_batch(queries);
          std::osyncstream(std::cout) << "concurrent query thread=" << worker_id
                                      << " count=" << result.queries << '\n';
        } else {
          for (unsigned i = 1; i <= 5; ++i) {
            auto result =
                node.update({update_op::Upsert, static_cast<NodeId>(i % nodes),
                             queries[i]});
            if (!result.ok)
              throw std::runtime_error("update " + std::to_string(i) + ": " +
                                       result.error);
            ++updates;
          }
        }
        reads += node.transport().stats().read_ops.load();
        writes += node.transport().stats().write_ops.load();
      } catch (const std::exception &e) {
        ++failed;
        std::osyncstream(std::cerr)
            << "worker " << worker_id << " failed: " << e.what() << '\n';
      }
    };
    try {
      for (int t = 0; t < 5; ++t)
        workers.emplace_back(workload, t);
    } catch (...) {
      start.count_down();
      throw;
    }
    start.count_down();
    for (auto &worker : workers)
      worker.join();
    std::cout << "concurrent_updates_completed=" << updates
              << " workers_failed=" << failed
              << "\nconcurrent_rdma_read_ops=" << reads
              << " concurrent_rdma_write_ops=" << writes << '\n';
    return failed ? 1 : 0;
  } catch (const std::exception &e) {
    std::cerr << "rdma_client: " << e.what() << '\n';
    return 1;
  }
}
