#pragma once

#include "rdma_hnsw/runtime/storage_engine.h"
#include <cstddef>
#include <string>

namespace rdma_hnsw {

// Configuration for a storage process.  The same object can be used by an
// application embedding the storage engine or by the storage_server binary.
struct StorageNodeConfig {
  std::string index_path;
  TransportKind transport = TransportKind::Mmap;
  std::string bind = "0.0.0.0";
  std::size_t port = 18515;
  std::string ib_device;
  std::size_t ib_port = 1;
  RdmaConnection rdma_connection = RdmaConnection::Ib; // Used only with Rdma.
  std::size_t rdma_timeout_ms = 10000; // CM only; idle sessions do not expire.
};

class StorageNode {
public:
  explicit StorageNode(StorageNodeConfig config);

  // Run the network service until the transport exits.  mmap has no network
  // listener, so serve() returns immediately and the index remains available
  // through apply() for an embedding process.
  int serve();
  // Apply directly when the node is embedded (or using mmap). RDMA serve()
  // owns its transport-side update loop, so callers should submit RDMA updates
  // through ComputeNode instead of mixing both paths.
  UpdateResult apply(const UpdateRequest &request);
  const GraphStore &store() const { return engine_.store(); }
  const StorageNodeConfig &config() const { return config_; }

private:
  StorageNodeConfig config_;
  StorageEngine engine_;
};

} // namespace rdma_hnsw
