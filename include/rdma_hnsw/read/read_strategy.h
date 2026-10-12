#pragma once

#include "rdma_hnsw/transport/transport.h"
#include <cstddef>
#include <cstdint>
#include <unordered_map>

namespace rdma_hnsw {
// Instrumentation shared by every strategy.  Fields are only ever added, never
// repurposed, so strategies and experiments can rely on a stable contract.
//
// Byte accounting is split into two independent regions:
//   * neighbor region: requested_bytes / useful_bytes / wasted_bytes / read_ops
//     describe the neighbor-list reads performed by a NeighborReadStrategy;
//   * node region: node_reads / node_bytes describe the QueryEngine reading a
//     node's header + vector (the dominant traffic in HNSW search).
// Keeping them apart makes `requested == useful + wasted` an identity inside
// the neighbor region (see docs/baseline1-plan.md section 6).
struct ReadMetrics {
  std::uint64_t requested_bytes{};   // neighbor region bytes requested
  std::uint64_t useful_bytes{};      // neighbor region bytes actually used
  std::uint64_t wasted_bytes{};      // neighbor region bytes never used
  std::uint64_t read_ops{};          // neighbor region read operations
  std::uint64_t header_reads{};
  std::uint64_t dependent_reads{};
  std::uint64_t cache_hits{};
  std::uint64_t cache_misses{};
  std::uint64_t cache_invalidations{};
  std::uint64_t prefix_reads{};
  std::uint64_t fallbacks{};
  // Added for Baseline 1 instrumentation (docs/baseline1-plan.md T2).
  std::uint64_t layer_reads{};    // (node, level) neighbor reads performed
  std::uint64_t node_reads{};     // header+vector reads by the QueryEngine
  std::uint64_t node_bytes{};     // bytes requested for node region reads
  std::uint64_t degree_sum{};     // sum of min(degree, capacity) over reads
  std::uint64_t capacity_sum{};   // sum of capacity over reads
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

// Baseline 1: read the full fixed-capacity neighbor slot array of the layer in
// a single transport read, regardless of the true degree, then truncate to the
// degree encoded in the leading uint32.  This mirrors the reference
// `ReadPolicy::Fixed` behaviour and SHINE's "read the whole neighbor table in
// one RDMA READ" design (docs/baseline1-plan.md sections 1-3).
class Baseline1FixedStrategy final : public NeighborReadStrategy {
public:
  NeighborReadResult read(const RemoteNodeView &v, ReadContext &c) override;
  const char *name() const override { return "baseline1_fixed"; }
};

std::unique_ptr<NeighborReadStrategy> make_read_strategy(ReadPolicy policy);
} // namespace rdma_hnsw
