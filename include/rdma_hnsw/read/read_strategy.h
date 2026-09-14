#pragma once

#include "rdma_hnsw/transport/transport.h"
#include <cstddef>
#include <cstdint>
#include <unordered_map>

namespace rdma_hnsw {
struct ReadMetrics {
  std::uint64_t requested_bytes{};
  std::uint64_t useful_bytes{};
  std::uint64_t wasted_bytes{};
  std::uint64_t read_ops{};
  std::uint64_t header_reads{};
  std::uint64_t dependent_reads{};
  std::uint64_t cache_hits{};
  std::uint64_t cache_misses{};
  std::uint64_t cache_invalidations{};
  std::uint64_t prefix_reads{};
  std::uint64_t fallbacks{};
  void merge(const ReadMetrics &o);
};

struct RemoteNodeView {
  NodeId id{};
  Generation generation{};
  std::uint32_t level{};
  Offset layer_offset{};
  std::size_t capacity{};
};

struct NeighborReadResult {
  std::vector<NodeId> neighbors;
  bool ok{false};
  std::string error;
};

struct ReadContext {
  RemoteTransport &transport;
  // Optional instrumentation owned by the caller. A strategy may ignore it
  // completely, which keeps experiment-specific measurements out of the core.
  ReadMetrics *metrics{};
  std::size_t prefix_granularity{4};
  std::size_t cache_capacity{100000};
  std::unordered_map<NodeId, std::pair<Generation, std::uint32_t>> length_cache;
};

class NeighborReadStrategy {
public:
  virtual ~NeighborReadStrategy() = default;
  virtual NeighborReadResult read(const RemoteNodeView &, ReadContext &) = 0;
  virtual const char *name() const = 0;
};

std::unique_ptr<NeighborReadStrategy> make_read_strategy(ReadPolicy policy);
} // namespace rdma_hnsw
