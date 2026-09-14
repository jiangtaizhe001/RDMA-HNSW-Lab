#pragma once

#include "rdma_hnsw/runtime/query_engine.h"
#include "rdma_hnsw/runtime/storage_engine.h"
#include "rdma_hnsw/transport/transport.h"
#include <memory>
#include <string>

namespace rdma_hnsw {

struct ComputeNodeConfig {
  std::string index_path;
  TransportKind transport = TransportKind::Mmap;
  std::string host = "127.0.0.1";
  std::size_t port = 18515;
  std::string ib_device;
  std::size_t ib_port = 1;
  ReadPolicy read_policy = ReadPolicy::Fixed;
  DistanceKind distance = DistanceKind::L2;
  std::size_t ef_search = 64;
  std::size_t k = 10;
  std::size_t prefix_granularity = 4;
  std::size_t cache_capacity = 100000;
  RdmaConnection rdma_connection = RdmaConnection::Ib; // Used only with Rdma.
  std::size_t rdma_timeout_ms = 10000; // CM only: per-stage/operation timeout.
};

// Reusable compute-side facade.  It owns the selected transport and query
// engine, while callers decide how to schedule queries, updates, and metrics.
class ComputeNode {
public:
  explicit ComputeNode(ComputeNodeConfig config);

  std::vector<NodeId> query(const Vector &vector);
  QueryStats query_batch(const std::vector<Vector> &queries,
                         const std::vector<Vector> *ground_truth = nullptr);
  UpdateResult update(const UpdateRequest &request);
  void reload();

  QueryEngine &query_engine() { return *engine_; }
  const QueryEngine &query_engine() const { return *engine_; }
  RemoteTransport &transport() { return *transport_; }
  const RemoteTransport &transport() const { return *transport_; }
  const ComputeNodeConfig &config() const { return config_; }

private:
  void open();
  ComputeNodeConfig config_;
  IndexCatalog catalog_;
  std::unique_ptr<RemoteTransport> transport_;
  std::unique_ptr<QueryEngine> engine_;
  std::unique_ptr<StorageEngine> local_storage_;
};

} // namespace rdma_hnsw
