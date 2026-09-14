#include "rdma_hnsw/common/config.h"
#include "rdma_hnsw/runtime/storage_node.h"
#include <atomic>
#include <chrono>
#include <csignal>
#include <iostream>
#include <thread>

namespace {
std::atomic_bool stop{false};
void signal_handler(int) { stop = true; }
} // namespace

int main(int argc, char **argv) {
  try {
    const auto c = rdma_hnsw::parse_config(argc, argv, "server");
    if (c.help)
      return 0;
    rdma_hnsw::StorageNode node(
        {c.index, c.transport, c.bind, c.port, c.ib_device, c.ib_port, c.rdma_connection, c.rdma_timeout_ms});
    if (c.transport == rdma_hnsw::TransportKind::Rdma)
      return node.serve();

    std::signal(SIGINT, signal_handler);
    std::signal(SIGTERM, signal_handler);
    std::cout << "storage_server ready index=" << c.index << " transport=mmap\n"
              << std::flush;
    while (!stop)
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
  } catch (const std::exception &e) {
    std::cerr << "storage_server: " << e.what() << '\n';
    return 1;
  }
}
