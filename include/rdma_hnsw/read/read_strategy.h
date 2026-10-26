#pragma once

#include "rdma_hnsw/transport/transport.h"
#include <cstddef>
#include <cstdint>
#include <list>
#include <memory>
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
  // Added for Baseline 2 instrumentation (docs/baseline2-plan.md T2).
  std::uint64_t failed_reads{};       // transport reads that failed or short-read
  std::uint64_t zero_degree_layers{}; // layers with degree == 0 (no 2nd read)
  // Added for Baseline 3 instrumentation (docs/baseline3-plan.md T3).
  std::uint64_t hint_reads{};       // first read of a hit, width from the cache hint
  std::uint64_t cold_start_reads{}; // miss path's 2nd read (d > 0); NOT a fallback
  std::uint64_t fallback_bytes{};   // bytes requested by fallback reads
  std::uint64_t cache_evictions{};  // entries dropped because the cache was full
  std::uint64_t hint_overestimates{}; // hits where hint > degree (over-read slots)
  std::uint64_t cache_updates{};      // hits that rewrote the cached degree
  std::uint64_t hint_slots_sum{};     // sum of hint (in slots) over hits
  std::uint64_t over_read_slots_sum{}; // sum of (hint - degree) over hits
  std::vector<std::uint64_t> cache_hits_by_level;   // per-level hit counts (H1)
  std::vector<std::uint64_t> cache_misses_by_level; // per-level miss counts
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

// Baseline 2: header-first.  The layer's leading 4-byte word stores the actual
// degree; this strategy reads that word first and then asks the transport for
// exactly `degree` neighbour slots.  The second read's offset and length both
// depend on the first read's result, so the two reads cannot be pipelined --
// that data dependency is the whole point of the experiment
// (docs/baseline2-plan.md sections 0 and 5).  Selected through
// `--read-strategy header_actual` / `ReadPolicy::HeaderActual`.
class Baseline2HeaderFirstStrategy final : public NeighborReadStrategy {
public:
  NeighborReadResult read(const RemoteNodeView &v, ReadContext &c) override;
  const char *name() const override { return "baseline2_header_first"; }
};

// ---- Baseline 3 (docs/baseline3-plan.md) ----------------------------------
// The cache key MUST include the level: level 0 has capacity 2*M while every
// higher level has M, so a NodeId-only key would let a level-0 degree leak into
// a higher level and produce bogus hints.
using LruList = std::list<std::uint64_t>;

struct LengthCacheEntry {
  Generation generation{}; // recorded for statistics only, never used as a
                           // hit/miss predictor (docs/baseline3-plan.md DR-5)
  std::uint32_t degree{};  // last validated degree, already clamped to capacity
  LruList::iterator lru_pos{};
};

// The map and the LRU list live in the same object because entries hold
// iterators into the list: keeping the list inside the (shared) cache is what
// makes it safe for the data to outlive one strategy instance (DR-4).
struct LengthCache {
  std::unordered_map<std::uint64_t, LengthCacheEntry> entries;
  LruList lru; // front == most recently used
};

using SharedLengthCache = std::shared_ptr<LengthCache>;

inline std::uint64_t length_cache_key(NodeId id, std::uint32_t level) {
  return (static_cast<std::uint64_t>(level) << 32) | id;
}

// Baseline 3: cache the neighbor count observed on the compute side and use it
// as the width of the next read, so a hit needs ONE read that carries both the
// degree word and the predicted prefix (instead of Baseline 2's header trip).
// The prediction is validated against the degree that the very same read
// returns: `d <= hint` is a hit, `d > hint` triggers a fallback read of the
// remaining slots.  `generation` is never consulted to predict a hit.
// Selected through `--read-strategy cached_length|cached` /
// `ReadPolicy::CachedLength`.
class Baseline3CachedLengthStrategy final : public NeighborReadStrategy {
public:
  // capacity == 0 -> fall back to ReadContext::cache_capacity.
  // A non-null `cache` is owned by the caller, which lets the experiment driver
  // keep the learned degrees across ComputeNode::update() -> reload() (DR-4).
  explicit Baseline3CachedLengthStrategy(std::size_t capacity = 0,
                                         SharedLengthCache cache = {});
  NeighborReadResult read(const RemoteNodeView &v, ReadContext &c) override;
  const char *name() const override { return "baseline3_cached_length"; }
  std::size_t cache_size() const { return cache_ ? cache_->entries.size() : 0; }
  const SharedLengthCache &cache() const { return cache_; }

private:
  void touch(std::uint64_t key);
  // Insert/refresh the entry and evict LRU entries beyond `limit`.  Returns the
  // number of entries evicted (0 when the cache is disabled, i.e. limit == 0).
  std::size_t put(std::uint64_t key, Generation generation,
                  std::uint32_t degree, std::size_t limit);

  std::size_t capacity_{};
  SharedLengthCache cache_;
};

std::unique_ptr<NeighborReadStrategy> make_read_strategy(ReadPolicy policy);
} // namespace rdma_hnsw