#pragma once
#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/transport/transport.h"
#include <mutex>

namespace rdma_hnsw {
class StorageEngine {
public:
  explicit StorageEngine(const std::string &index_path);
  UpdateResult apply(const UpdateRequest &request);
  const GraphStore &store() const { return store_; }

private:
  // Lightweight online maintenance used by the prototype. It preserves the
  // on-disk layout and makes inserted/erased nodes observable to traversal;
  // students can replace it with a fuller dynamic-HNSW algorithm.
  void refresh_base_neighbors(std::vector<NodeRecord> &nodes, NodeId target,
                              std::uint32_t operation);
  GraphStore store_;
  std::mutex mutex_;
};
} // namespace rdma_hnsw
