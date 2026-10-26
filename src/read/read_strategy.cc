#include "rdma_hnsw/read/read_strategy.h"
#include <algorithm>
#include <array>
#include <cstring>
#include <memory>
#include <stdexcept>

namespace rdma_hnsw {
namespace {
// Copy `count` leading NodeIds out of a raw neighbor-slot buffer.  The caller
// must have validated that the buffer holds at least count * sizeof(NodeId)
// bytes.
NeighborReadResult decode(const std::vector<byte> &b, std::size_t count) {
  if (b.size() < count * sizeof(NodeId))
    return {{}, false, "short neighbor read"};
  std::vector<NodeId> out(count);
  if (count)
    std::memcpy(out.data(), b.data(), count * sizeof(NodeId));
  return {std::move(out), true, {}};
}

// Same as decode() but from a raw pointer, used when a strategy already holds
// the bytes in the right place (Baseline 3 slices a single read buffer).
NeighborReadResult decode_from(const byte *p, std::size_t count) {
  std::vector<NodeId> out(count);
  if (count)
    std::memcpy(out.data(), p, count * sizeof(NodeId));
  return {std::move(out), true, {}};
}

void bump_level(std::vector<std::uint64_t> &v, std::uint32_t level) {
  if (v.size() <= level)
    v.resize(level + 1, 0);
  v[level]++;
}
} // namespace

NeighborReadResult Baseline1FixedStrategy::read(const RemoteNodeView &v,
                                                ReadContext &c) {
  // Baseline 1 never derives the read length from the actual degree: it always
  // asks for the whole fixed-capacity slot array plus the leading degree word.
  if (v.capacity == 0)
    return {{}, false, "layer capacity is zero"};

  const std::size_t n = sizeof(std::uint32_t) + v.capacity * sizeof(NodeId);
  std::vector<byte> b(n);
  const auto r = c.transport.read({v.layer_offset, 0}, b);

  if (c.metrics) {
    c.metrics->read_ops++;
    c.metrics->requested_bytes += n;
    c.metrics->layer_reads++;
    c.metrics->capacity_sum += v.capacity;
  }

  // A short read would silently fabricate neighbours from uninitialised bytes,
  // so it is reported as an error instead of being truncated.
  if (!r.ok || r.bytes != n) {
    if (c.metrics)
      c.metrics->failed_reads++;
    return {{}, false,
            "short fixed neighbor read: " +
                (r.error.empty() ? std::string("unexpected byte count")
                                 : r.error)};
  }

  std::uint32_t degree{};
  std::memcpy(&degree, b.data(), sizeof(degree));
  const auto effective = std::min<std::uint32_t>(degree, v.capacity);

  if (c.metrics) {
    c.metrics->useful_bytes +=
        sizeof(std::uint32_t) + effective * sizeof(NodeId);
    c.metrics->wasted_bytes += (v.capacity - effective) * sizeof(NodeId);
    c.metrics->degree_sum += effective;
  }

  // Only the first `effective` slots are real neighbours: the remaining slots
  // are zero padding and must never be returned as neighbour ids.
  return decode(std::vector<byte>(b.begin() + sizeof(std::uint32_t), b.end()),
                effective);
}

NeighborReadResult Baseline2HeaderFirstStrategy::read(const RemoteNodeView &v,
                                                      ReadContext &c) {
  // An empty layer has no header to read: reject before touching the transport
  // (same contract as Baseline 1, docs/baseline2-plan.md section 3.3).
  if (v.capacity == 0)
    return {{}, false, "baseline2_header_first: layer capacity is zero"};

  // ---- first read: only the leading 4-byte degree word -------------------
  std::array<byte, sizeof(std::uint32_t)> hdr{};
  const auto hr = c.transport.read({v.layer_offset, 0}, hdr);
  if (!hr.ok || hr.bytes != hdr.size()) {
    if (c.metrics)
      c.metrics->failed_reads++;
    return {{}, false,
            "baseline2_header_first: header read failed: " +
                (hr.error.empty() ? std::string("unexpected byte count")
                                  : hr.error)};
  }
  if (c.metrics) {
    c.metrics->read_ops++;
    c.metrics->header_reads++; // a dedicated degree read, not bundled
    c.metrics->requested_bytes += hdr.size();
  }

  std::uint32_t raw{};
  std::memcpy(&raw, hdr.data(), sizeof(raw));
  // Remote data is untrusted: a corrupt degree must never drive the second
  // read past the layer's fixed slot array.
  const auto degree =
      static_cast<std::uint32_t>(std::min<std::size_t>(raw, v.capacity));

  // ---- degree == 0: nothing follows, so the dependent read is skipped ----
  if (degree == 0) {
    if (c.metrics) {
      c.metrics->zero_degree_layers++;
      c.metrics->useful_bytes += hdr.size(); // wasted is identically zero
      c.metrics->layer_reads++;
      c.metrics->capacity_sum += v.capacity;
    }
    return {{}, true, {}};
  }

  // ---- second read: offset and length both depend on the first read ------
  const std::size_t n = degree * sizeof(NodeId);
  std::vector<byte> b(n);
  const auto nr =
      c.transport.read({v.layer_offset + sizeof(std::uint32_t), 0}, b);
  if (!nr.ok || nr.bytes != n) {
    if (c.metrics)
      c.metrics->failed_reads++;
    return {{}, false,
            "baseline2_header_first: neighbor read failed: " +
                (nr.error.empty() ? std::string("unexpected byte count")
                                  : nr.error)};
  }
  if (c.metrics) {
    c.metrics->read_ops++;
    c.metrics->dependent_reads++; // offset/length derived from the header read
    c.metrics->requested_bytes += n;
    c.metrics->useful_bytes += hdr.size() + n;
    c.metrics->layer_reads++;
    c.metrics->capacity_sum += v.capacity;
    c.metrics->degree_sum += degree;
  }
  return decode(b, degree);
}

// ===== Baseline 3: compute-side degree cache (docs/baseline3-plan.md) ======
//
// Three states, see docs/baseline3-plan.md section 1.2:
//   A. miss            -> header read, then (d > 0) exact neighbour read
//   B. hit, d <= hint  -> ONE read of [degree][hint prefix]; (hint - d)
//                         slots are counted as waste
//   C. hit, d > hint   -> the hit read exposed the underestimate; a second
//                         read fetches the remaining (d - hint) slots
//
// Every state keeps `requested == useful + wasted` inside the neighbor region.
Baseline3CachedLengthStrategy::Baseline3CachedLengthStrategy(
    std::size_t capacity, SharedLengthCache cache)
    : capacity_(capacity),
      cache_(cache ? std::move(cache) : std::make_shared<LengthCache>()) {}

void Baseline3CachedLengthStrategy::touch(std::uint64_t key) {
  auto it = cache_->entries.find(key);
  if (it == cache_->entries.end())
    return;
  cache_->lru.splice(cache_->lru.begin(), cache_->lru, it->second.lru_pos);
}

std::size_t Baseline3CachedLengthStrategy::put(std::uint64_t key,
                                               Generation generation,
                                               std::uint32_t degree,
                                               std::size_t limit) {
  if (limit == 0)
    return 0; // caching disabled: every read stays a miss
  auto it = cache_->entries.find(key);
  if (it != cache_->entries.end()) {
    it->second.generation = generation;
    it->second.degree = degree;
    touch(key);
    return 0;
  }
  cache_->lru.push_front(key);
  cache_->entries.emplace(
      key, LengthCacheEntry{generation, degree, cache_->lru.begin()});
  // Strict LRU: drop the coldest entries until the cache fits its capacity.
  std::size_t evicted = 0;
  while (cache_->entries.size() > limit && !cache_->lru.empty()) {
    const std::uint64_t victim = cache_->lru.back();
    cache_->lru.pop_back();
    if (cache_->entries.erase(victim))
      ++evicted;
  }
  return evicted;
}

NeighborReadResult
Baseline3CachedLengthStrategy::read(const RemoteNodeView &v, ReadContext &c) {
  auto *m = c.metrics;
  const std::size_t cap = v.capacity;
  if (cap == 0)
    return {{}, false, "baseline3_cached_length: layer capacity is zero"};

  const std::size_t limit = capacity_ ? capacity_ : c.cache_capacity;
  const std::uint64_t key = length_cache_key(v.id, v.level);
  auto it = cache_->entries.find(key);

  // ===== state A: miss (cold start), shaped exactly like Baseline 2 ========
  if (it == cache_->entries.end()) {
    std::array<byte, sizeof(std::uint32_t)> hdr{};
    const auto hr = c.transport.read({v.layer_offset, 0}, hdr);
    if (!hr.ok || hr.bytes != hdr.size()) {
      if (m)
        m->failed_reads++;
      return {{}, false,
              "baseline3_cached_length: header read failed: " +
                  (hr.error.empty() ? std::string("unexpected byte count")
                                    : hr.error)};
    }
    if (m) {
      m->read_ops++;
      m->header_reads++;
      m->cache_misses++;
      m->requested_bytes += hdr.size();
      bump_level(m->cache_misses_by_level, v.level);
    }
    std::uint32_t raw{};
    std::memcpy(&raw, hdr.data(), sizeof(raw));
    // Remote data is untrusted: clamp before it can drive a read length.
    const auto d = static_cast<std::uint32_t>(
        std::min<std::size_t>(raw, static_cast<std::size_t>(cap)));

    if (d == 0) {
      if (m) {
        m->zero_degree_layers++;
        m->useful_bytes += hdr.size();
        m->layer_reads++;
        m->capacity_sum += cap;
      }
      // Caching a zero degree is a real hit: the next visit needs one read.
      const std::size_t evicted = put(key, v.generation, 0, limit);
      if (m)
        m->cache_evictions += evicted;
      return {{}, true, {}};
    }

    const std::size_t n = d * sizeof(NodeId);
    std::vector<byte> b(n);
    const auto nr =
        c.transport.read({v.layer_offset + sizeof(std::uint32_t), 0}, b);
    if (!nr.ok || nr.bytes != n) {
      // A failed read must never leave a bogus hint behind (DR-8).
      if (m)
        m->failed_reads++;
      return {{}, false,
              "baseline3_cached_length: cold-start read failed: " +
                  (nr.error.empty() ? std::string("unexpected byte count")
                                    : nr.error)};
    }
    if (m) {
      m->read_ops++;
      m->dependent_reads++;
      m->cold_start_reads++; // a miss, not a fallback (identity 7)
      m->requested_bytes += n;
      m->useful_bytes += hdr.size() + n;
      m->layer_reads++;
      m->capacity_sum += cap;
      m->degree_sum += d;
    }
    const std::size_t evicted = put(key, v.generation, d, limit);
    if (m)
      m->cache_evictions += evicted;
    return decode(b, d);
  }

  // ===== hit: ONE read carrying both the degree word and the hint prefix ===
  // Defensive clamp: an entry could have been written for a wider layout.
  const auto hint = static_cast<std::uint32_t>(
      std::min<std::size_t>(it->second.degree, cap));
  const std::size_t first = sizeof(std::uint32_t) + hint * sizeof(NodeId);
  std::vector<byte> b(first);
  const auto r = c.transport.read({v.layer_offset, 0}, b);
  if (!r.ok || r.bytes != first) {
    if (m)
      m->failed_reads++;
    return {{}, false,
            "baseline3_cached_length: hint read failed: " +
                (r.error.empty() ? std::string("unexpected byte count")
                                 : r.error)};
  }
  touch(key);
  if (m) {
    m->read_ops++;
    m->hint_reads++;
    m->cache_hits++;
    m->requested_bytes += first;
    m->hint_slots_sum += hint;
    m->layer_reads++;
    m->capacity_sum += cap;
    bump_level(m->cache_hits_by_level, v.level);
  }
  std::uint32_t raw{};
  std::memcpy(&raw, b.data(), sizeof(raw));
  const auto d = static_cast<std::uint32_t>(
      std::min<std::size_t>(raw, static_cast<std::size_t>(cap)));

  if (d <= hint) {
    // ===== state B: the prediction held (or over-predicted) ================
    if (m) {
      m->useful_bytes += sizeof(std::uint32_t) + d * sizeof(NodeId);
      m->wasted_bytes += static_cast<std::uint64_t>(hint - d) * sizeof(NodeId);
      m->over_read_slots_sum += hint - d;
      if (d < hint)
        m->hint_overestimates++;
      m->degree_sum += d;
    }
    if (it->second.degree != d) {
      it->second.degree = d;
      if (m)
        m->cache_updates++;
    }
    it->second.generation = v.generation; // statistics only (DR-5)
    return decode_from(b.data() + sizeof(std::uint32_t), d);
  }

  // ===== state C: underestimate (d > hint) -> fallback read ================
  const std::size_t need = (d - hint) * sizeof(NodeId);
  std::vector<byte> rest(need);
  const auto fr = c.transport.read(
      {v.layer_offset + sizeof(std::uint32_t) + hint * sizeof(NodeId), 0},
      rest);
  if (!fr.ok || fr.bytes != need) {
    if (m)
      m->failed_reads++;
    // Keep the previous entry: a transient failure must not poison the hint.
    return {{}, false,
            "baseline3_cached_length: fallback read failed: " +
                (fr.error.empty() ? std::string("unexpected byte count")
                                  : fr.error)};
  }
  if (m) {
    m->read_ops++;
    m->dependent_reads++;
    m->fallbacks++;
    m->fallback_bytes += need;
    m->cache_invalidations++;
    m->requested_bytes += need;
    m->useful_bytes += sizeof(std::uint32_t) + d * sizeof(NodeId);
    m->degree_sum += d;
  }
  it->second.degree = d;
  it->second.generation = v.generation;
  if (m)
    m->cache_updates++;
  // b[4 .. 4+4*hint) are the first `hint` neighbours, rest holds the tail.
  std::vector<NodeId> out(d);
  if (hint)
    std::memcpy(out.data(), b.data() + sizeof(std::uint32_t),
                hint * sizeof(NodeId));
  if (need)
    std::memcpy(out.data() + hint, rest.data(), need);
  return {std::move(out), true, {}};
}

void ReadMetrics::merge(const ReadMetrics &o) {
  requested_bytes += o.requested_bytes;
  useful_bytes += o.useful_bytes;
  wasted_bytes += o.wasted_bytes;
  read_ops += o.read_ops;
  header_reads += o.header_reads;
  dependent_reads += o.dependent_reads;
  cache_hits += o.cache_hits;
  cache_misses += o.cache_misses;
  cache_invalidations += o.cache_invalidations;
  prefix_reads += o.prefix_reads;
  fallbacks += o.fallbacks;
  layer_reads += o.layer_reads;
  node_reads += o.node_reads;
  node_bytes += o.node_bytes;
  degree_sum += o.degree_sum;
  capacity_sum += o.capacity_sum;
  failed_reads += o.failed_reads;
  zero_degree_layers += o.zero_degree_layers;
  hint_reads += o.hint_reads;
  cold_start_reads += o.cold_start_reads;
  fallback_bytes += o.fallback_bytes;
  cache_evictions += o.cache_evictions;
  hint_overestimates += o.hint_overestimates;
  cache_updates += o.cache_updates;
  hint_slots_sum += o.hint_slots_sum;
  over_read_slots_sum += o.over_read_slots_sum;
  auto grow_add = [](std::vector<std::uint64_t> &dst,
                     const std::vector<std::uint64_t> &src) {
    if (dst.size() < src.size())
      dst.resize(src.size(), 0);
    for (std::size_t i = 0; i < src.size(); ++i)
      dst[i] += src[i];
  };
  grow_add(cache_hits_by_level, o.cache_hits_by_level);
  grow_add(cache_misses_by_level, o.cache_misses_by_level);
}

std::unique_ptr<NeighborReadStrategy> make_read_strategy(ReadPolicy p) {
  if (p == ReadPolicy::Fixed)
    return std::make_unique<Baseline1FixedStrategy>();
  if (p == ReadPolicy::HeaderActual)
    return std::make_unique<Baseline2HeaderFirstStrategy>();
  // Baseline 3 without an external cache: the capacity comes from
  // ReadContext::cache_capacity, so the CLI needs no extra wiring.
  if (p == ReadPolicy::CachedLength)
    return std::make_unique<Baseline3CachedLengthStrategy>();
  throw std::invalid_argument("read strategy is a student extension; "
                              "implement and inject it before selecting " +
                              to_string(p));
}
} // namespace rdma_hnsw
