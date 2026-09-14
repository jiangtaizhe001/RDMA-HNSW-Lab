#pragma once

#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/read/read_strategy.h"
#include <memory>

namespace rdma_hnsw {
struct QueryStats {
  std::uint64_t queries{};
  std::uint64_t visited_nodes{};
  double elapsed_ms{};
  double recall{};
  ReadMetrics reads;
};

class QueryEngine {
public:
  QueryEngine(IndexCatalog catalog, RemoteTransport &transport,
              ReadPolicy policy, std::size_t ef_search, std::size_t k,
              std::size_t prefix_granularity,
              std::size_t cache_capacity = 100000,
              DistanceKind distance = DistanceKind::L2);
  std::vector<NodeId> search(const Vector &query);
  QueryStats run(const std::vector<Vector> &queries,
                 const std::vector<Vector> *ground_truth_base = nullptr);
  void set_read_strategy(std::unique_ptr<NeighborReadStrategy> strategy);
  void reset_catalog(IndexCatalog catalog);
  const IndexCatalog &catalog() const { return catalog_; }
  const QueryStats &stats() const { return stats_; }

private:
  struct NodeData {
    NodeId id{};
    Generation generation{};
    std::uint32_t level{};
    bool deleted{};
    Vector vector;
  };
  NodeData read_node(NodeId id);
  IndexCatalog catalog_;
  RemoteTransport &transport_;
  std::unique_ptr<NeighborReadStrategy> strategy_;
  ReadContext context_;
  std::size_t ef_search_{};
  std::size_t k_{};
  std::size_t cache_capacity_{};
  DistanceKind distance_kind_{};
  QueryStats stats_;
};
} // namespace rdma_hnsw
