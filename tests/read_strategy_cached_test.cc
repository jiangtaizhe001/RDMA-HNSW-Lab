// Baseline 3 (compute-side degree cache) unit tests.
//
// Covers every case enumerated in docs/baseline3-plan.md section 8: the three
// read states, equivalence with Baseline 1, layer isolation, malformed remote
// data, cache lifetime across strategy re-injection, LRU eviction, and the two
// constraints that are easy to violate by accident:
//   * generation must never be used to predict a hit (DR-5 / case 16),
//   * a failed or short read must never write the cache (DR-8 / case 12-14).
#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/read/read_strategy.h"
#include "rdma_hnsw/transport/transport.h"
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <memory>
#include <span>
#include <string>
#include <vector>

using namespace rdma_hnsw;

namespace {

class FakeTransport final : public RemoteTransport {
public:
  std::vector<byte> data;
  bool fail{false};
  std::size_t truncate{0};
  std::size_t calls{0};
  std::size_t fail_on_call{0};

  ReadResult read(RemoteAddress a, std::span<byte> dst) override {
    ++calls;
    if (fail || (fail_on_call && calls == fail_on_call))
      return {false, 0, "injected read failure"};
    const auto off = static_cast<std::size_t>(a.offset);
    if (off > data.size())
      return {true, 0, {}};
    std::size_t n = std::min(data.size() - off, dst.size());
    if (truncate)
      n = std::min(n, truncate);
    if (n)
      std::memcpy(dst.data(), data.data() + off, n);
    return {true, n, {}};
  }
  WriteResult write(RemoteAddress, std::span<const byte>) override {
    return {false, 0, "unsupported"};
  }
  UpdateResult submit_update(const UpdateRequest &) override {
    return {false, 0, "unsupported"};
  }
  const TransportStats &stats() const override { return stats_; }

private:
  TransportStats stats_{};
};

std::string unique_path(const char *stem) {
  return (std::filesystem::temp_directory_path() /
          (std::string(stem) + ".bin"))
      .string();
}

// Lay out one fake "layer": a uint32 degree followed by `capacity` slots.
void init_layer(FakeTransport &t, std::size_t offset, std::size_t capacity,
                const std::vector<NodeId> &neighbors) {
  const std::size_t need = offset + sizeof(std::uint32_t) +
                           capacity * sizeof(NodeId);
  if (t.data.size() < need)
    t.data.resize(need, byte{0});
  const auto degree = static_cast<std::uint32_t>(neighbors.size());
  std::memcpy(t.data.data() + offset, &degree, sizeof(degree));
  if (!neighbors.empty())
    std::memcpy(t.data.data() + offset + sizeof(std::uint32_t),
                neighbors.data(), neighbors.size() * sizeof(NodeId));
}

// Rewrite only the degree word, leaving the slots untouched.
void set_degree(FakeTransport &t, std::size_t offset, std::uint32_t degree) {
  std::memcpy(t.data.data() + offset, &degree, sizeof(degree));
}

// The eight identities from docs/baseline3-plan.md section 7.4.  They require a
// run without failed transport reads, which is the normal case.
void check_baseline3(const ReadMetrics &m) {
  assert(m.requested_bytes == m.useful_bytes + m.wasted_bytes);
  assert(m.read_ops == m.hint_reads + m.header_reads + m.dependent_reads);
  assert(m.cache_hits == m.hint_reads);
  assert(m.cache_hits + m.cache_misses == m.layer_reads);
  assert(m.cache_misses == m.header_reads);
  assert(m.fallbacks == m.cache_invalidations);
  assert(m.dependent_reads == m.fallbacks + m.cold_start_reads);
  assert(m.wasted_bytes == sizeof(NodeId) * m.over_read_slots_sum);
}

struct Fixture {
  SharedLengthCache cache = std::make_shared<LengthCache>();
  Baseline3CachedLengthStrategy strategy;
  explicit Fixture(std::size_t capacity = 0) : strategy(capacity, cache) {}
  std::uint32_t cached_degree(NodeId id, std::uint32_t level) const {
    return cache->entries.at(length_cache_key(id, level)).degree;
  }
  bool is_cached(NodeId id, std::uint32_t level) const {
    return cache->entries.contains(length_cache_key(id, level));
  }
};

constexpr std::size_t kCap8 = 8;

// --- 1. full equivalence with Baseline 1 on a real index -------------------
void test_equivalence_with_baseline1() {
  const auto path = unique_path("rdma_hnsw_b3_equiv");
  IndexHeader h{};
  h.dim = 2;
  h.M = 2; // cap(0) = 4, cap(l>0) = 2
  h.max_level = 2;
  std::vector<NodeRecord> nodes(6);
  for (NodeId i = 0; i < nodes.size(); ++i) {
    nodes[i].id = i;
    nodes[i].vector = {static_cast<float>(i), 1.0f};
  }
  nodes[0].level = 2;
  nodes[0].neighbors = {{1, 2, 3, 4}, {5, 0}, {1, 2}};
  nodes[1].level = 1;
  nodes[1].neighbors = {{2, 3}, {4}};
  nodes[2].neighbors = {{0, 1, 4}};
  nodes[3].neighbors = {{}}; // degree 0
  nodes[4].neighbors = {{5}};
  nodes[5].neighbors = {{3, 4, 0, 2}};
  GraphStore::write(path, h, nodes);
  auto c = GraphStore::load_catalog(path);
  auto t = make_mmap_transport(path, false);

  auto via_factory = make_read_strategy(ReadPolicy::CachedLength);
  assert(std::string(via_factory->name()) == "baseline3_cached_length");

  Baseline1FixedStrategy b1;
  Fixture f; // one shared cache: the second pass over the graph is all hits
  for (NodeId id = 0; id < nodes.size(); ++id) {
    const auto max_level = std::min<std::uint32_t>(nodes[id].level, h.max_level);
    for (std::uint32_t level = 0; level <= max_level; ++level) {
      const RemoteNodeView v{id, 1, level, node_layer_offset(c, id, level),
                             c.layer_capacity(level)};
      ReadMetrics m1;
      ReadMetrics m3;
      ReadContext ctx1{*t, &m1, 4, 100, {}};
      ReadContext ctx3{*t, &m3, 4, 100, {}};
      const auto r1 = b1.read(v, ctx1);
      const auto r3 = f.strategy.read(v, ctx3);
      assert(r1.ok && r3.ok);
      assert(r1.neighbors == r3.neighbors);
      check_baseline3(m3);
      // Both passes visited the same layer twice; the second one must be a hit.
      const auto r3b = f.strategy.read(v, ctx3);
      assert(r3b.neighbors == r1.neighbors);
      check_baseline3(m3);
    }
  }
  // Every (node, level) pair was read twice: half the lookups must be hits.
  assert(f.cache->entries.size() == 9);
  std::remove(path.c_str());
}

// --- 2. cold (miss) and hot (hit) reads return the same neighbours ---------
void test_cold_and_hot_agree() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {1, 2, 3});
  Fixture f;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const RemoteNodeView v{0, 1, 0, 0, kCap8};
  const auto cold = f.strategy.read(v, ctx);
  const auto hot = f.strategy.read(v, ctx);
  assert(cold.ok && hot.ok);
  assert(cold.neighbors == hot.neighbors);
  assert(m.cache_misses == 1 && m.cache_hits == 1);
  check_baseline3(m);
}

// --- 3. state A: miss with d > 0 (same shape as Baseline 2) ---------------
void test_state_a_miss() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {4, 5});
  Fixture f;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const auto r = f.strategy.read({0, 1, 0, 0, kCap8}, ctx);
  assert(r.ok && r.neighbors.size() == 2);
  assert(t.calls == 2);
  assert(m.read_ops == 2 && m.header_reads == 1 && m.hint_reads == 0);
  assert(m.cache_misses == 1 && m.cache_hits == 0);
  assert(m.cold_start_reads == 1 && m.fallbacks == 0);
  assert(m.wasted_bytes == 0);
  assert(m.requested_bytes == sizeof(std::uint32_t) + 2 * sizeof(NodeId));
  assert(m.useful_bytes == m.requested_bytes);
  assert(m.degree_sum == 2 && m.capacity_sum == kCap8);
  assert(m.cache_misses_by_level.size() == 1 && m.cache_misses_by_level[0] == 1);
  assert(f.cached_degree(0, 0) == 2);
  check_baseline3(m);
}

// --- 4. state B with hint > degree: waste is counted, one read ------------
void test_state_b_overestimate() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {4, 5});
  Fixture f;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const RemoteNodeView v{0, 1, 0, 0, kCap8};
  (void)f.strategy.read(v, ctx); // miss: caches degree 2
  f.cache->entries.at(length_cache_key(0, 0)).degree = 5; // pretend hint 5
  const auto r = f.strategy.read(v, ctx);
  assert(r.ok && r.neighbors.size() == 2);
  assert(t.calls == 3); // header + neighbour (miss), then ONE hint read
  assert(m.read_ops == 3 && m.hint_reads == 1 && m.header_reads == 1);
  assert(m.cache_hits == 1 && m.cache_misses == 1);
  assert(m.fallbacks == 0);
  assert(m.wasted_bytes == 3 * sizeof(NodeId));
  assert(m.over_read_slots_sum == 3 && m.hint_overestimates == 1);
  // miss: 4 + 2 slots; hit: 4 + 5 slots (degree word counted in both reads)
  assert(m.requested_bytes ==
         2 * sizeof(std::uint32_t) + (2 + 5) * sizeof(NodeId));
  assert(f.cached_degree(0, 0) == 2); // refreshed back to the truth
  assert(m.cache_updates == 1);
  check_baseline3(m);
}

// --- 5. state B with hint == degree: no waste -----------------------------
void test_state_b_exact() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {4, 5});
  Fixture f;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const RemoteNodeView v{0, 1, 0, 0, kCap8};
  (void)f.strategy.read(v, ctx);
  const std::size_t before = m.read_ops;
  const auto r = f.strategy.read(v, ctx);
  assert(r.ok && r.neighbors.size() == 2);
  assert(m.read_ops == before + 1); // exactly one read on a hit
  assert(m.hint_reads == 1 && m.header_reads == 1);
  assert(m.wasted_bytes == 0 && m.hint_overestimates == 0);
  assert(m.cache_updates == 0);
  check_baseline3(m);
}

// --- 6. state C: underestimate triggers exactly one fallback read ---------
void test_state_c_underestimate() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {9}); // degree 1
  Fixture f;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const RemoteNodeView v{0, 1, 0, 0, kCap8};
  (void)f.strategy.read(v, ctx); // caches degree 1
  // Grow the layer behind the cache's back: degree 4.
  init_layer(t, 0, kCap8, {11, 12, 13, 14});
  const auto r = f.strategy.read(v, ctx);
  assert(r.ok && r.neighbors.size() == 4);
  assert(r.neighbors[0] == 11 && r.neighbors[3] == 14);
  assert(m.read_ops == 2 + 2); // miss: 2 reads; hit+fallback: 2 reads
  assert(m.hint_reads == 1 && m.header_reads == 1 && m.dependent_reads == 2);
  assert(m.fallbacks == 1 && m.cache_invalidations == 1);
  assert(m.fallback_bytes == 3 * sizeof(NodeId));
  assert(m.requested_bytes ==
         (sizeof(std::uint32_t) + 1 * sizeof(NodeId)) +
             (sizeof(std::uint32_t) + 1 * sizeof(NodeId)) +
             3 * sizeof(NodeId));
  assert(m.wasted_bytes == 0 && m.over_read_slots_sum == 0);
  assert(f.cached_degree(0, 0) == 4);
  check_baseline3(m);
}

// --- 7. cached hint 0 while the layer is non-empty -> state C -------------
void test_hint_zero_then_nonzero() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {});
  Fixture f;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const RemoteNodeView v{0, 1, 0, 0, kCap8};
  (void)f.strategy.read(v, ctx); // caches degree 0
  init_layer(t, 0, kCap8, {7, 8, 9});
  const auto r = f.strategy.read(v, ctx);
  assert(r.ok && r.neighbors.size() == 3);
  assert(r.neighbors[0] == 7 && r.neighbors[2] == 9);
  assert(m.fallbacks == 1 && m.cache_invalidations == 1);
  assert(m.fallback_bytes == 3 * sizeof(NodeId));
  assert(m.wasted_bytes == 0);
  check_baseline3(m);
}

// --- 8. layer isolation: the key contains the level -----------------------
void test_layer_isolation() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {1, 2, 3, 4});   // level 0, cap 8
  init_layer(t, 64, 4, {9});               // level 1, cap 4
  Fixture f;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const RemoteNodeView v0{0, 1, 0, 0, kCap8};
  const RemoteNodeView v1{0, 1, 1, 64, 4};
  (void)f.strategy.read(v0, ctx);
  (void)f.strategy.read(v1, ctx);
  assert(m.cache_misses == 2 && m.cache_hits == 0); // separate keys
  assert(f.cached_degree(0, 0) == 4 && f.cached_degree(0, 1) == 1);
  const auto r0 = f.strategy.read(v0, ctx);
  const auto r1 = f.strategy.read(v1, ctx);
  assert(r0.neighbors.size() == 4 && r1.neighbors.size() == 1);
  assert(m.cache_hits == 2);
  assert(m.hint_overestimates == 0);
  check_baseline3(m);
}

// --- 9. malformed degree > capacity: clamp, never over-read ---------------
void test_degree_greater_than_capacity() {
  {
    // Miss path: the very first read already sees degree 100 with cap 8.
    FakeTransport t;
    init_layer(t, 0, kCap8, {1, 2, 3, 4, 5, 6, 7, 8});
    set_degree(t, 0, 100);
    Fixture f;
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    const auto r = f.strategy.read({0, 1, 0, 0, kCap8}, ctx);
    assert(r.ok && r.neighbors.size() == kCap8);
    assert(r.neighbors[0] == 1 && r.neighbors[7] == 8);
    assert(m.requested_bytes == sizeof(std::uint32_t) + kCap8 * sizeof(NodeId));
    assert(m.wasted_bytes == 0);
    assert(f.cached_degree(0, 0) == kCap8);
    check_baseline3(m);
  }
  {
    // Hit path: the hint was learned from a small degree, then the layer grows
    // past capacity; the fallback must read exactly (cap - hint) slots.
    FakeTransport t;
    init_layer(t, 0, kCap8, {1, 2, 3, 4, 5, 6, 7, 8});
    Fixture f;
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    const RemoteNodeView v{0, 1, 0, 0, kCap8};
    (void)f.strategy.read(v, ctx);
    f.cache->entries.at(length_cache_key(0, 0)).degree = 2;
    set_degree(t, 0, 100); // d = min(100, 8) = 8 > hint 2
    const auto r = f.strategy.read(v, ctx);
    assert(r.ok && r.neighbors.size() == kCap8);
    assert(m.fallbacks == 1);
    assert(m.fallback_bytes == (kCap8 - 2) * sizeof(NodeId));
    check_baseline3(m);
  }
}

// --- 10. degree == 0 in both states --------------------------------------
void test_zero_degree() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {});
  Fixture f;
  {
    ReadMetrics m; // state A
    ReadContext ctx{t, &m, 4, 100, {}};
    const auto r = f.strategy.read({0, 1, 0, 0, kCap8}, ctx);
    assert(r.ok && r.neighbors.empty());
    assert(t.calls == 1);
    assert(m.read_ops == 1 && m.header_reads == 1 && m.dependent_reads == 0);
    assert(m.zero_degree_layers == 1 && m.cache_misses == 1);
    assert(m.requested_bytes == sizeof(std::uint32_t));
    assert(m.useful_bytes == sizeof(std::uint32_t) && m.wasted_bytes == 0);
    assert(f.is_cached(0, 0) && f.cached_degree(0, 0) == 0);
    check_baseline3(m);
  }
  {
    ReadMetrics m; // state B with a cached zero hint
    ReadContext ctx{t, &m, 4, 100, {}};
    const auto r = f.strategy.read({0, 1, 0, 0, kCap8}, ctx);
    assert(r.ok && r.neighbors.empty());
    assert(t.calls == 2); // one 4-byte hint read
    assert(m.read_ops == 1 && m.hint_reads == 1 && m.header_reads == 0);
    assert(m.cache_hits == 1 && m.wasted_bytes == 0);
    check_baseline3(m);
  }
}

// --- 11. capacity == 0: error before touching transport or cache ----------
void test_zero_capacity() {
  FakeTransport t;
  Fixture f;
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const auto r = f.strategy.read({0, 1, 0, 0, 0}, ctx);
  assert(!r.ok);
  assert(m.read_ops == 0 && m.failed_reads == 0);
  assert(t.calls == 0);
  assert(f.cache->entries.empty());
}

// --- 12. a failed cold-start read must not pollute the cache ---------------
void test_failure_does_not_pollute_cache() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {4, 5, 6});
  Fixture f;
  {
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    t.fail_on_call = 2; // header ok, neighbour read fails
    const auto r = f.strategy.read({0, 1, 0, 0, kCap8}, ctx);
    assert(!r.ok && r.neighbors.empty());
    assert(m.failed_reads == 1 && m.cache_misses == 1);
    assert(f.cache->entries.empty()); // nothing was stored
  }
  {
    ReadMetrics m; // the same cache must still report a miss
    ReadContext ctx{t, &m, 4, 100, {}};
    t.fail_on_call = 0;
    const auto r = f.strategy.read({0, 1, 0, 0, kCap8}, ctx);
    assert(r.ok && r.neighbors.size() == 3);
    assert(m.cache_misses == 1 && m.cache_hits == 0);
    check_baseline3(m);
  }
}

// --- 13. a failed fallback read reports an error and keeps the old hint ----
void test_fallback_failure_keeps_old_hint() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {9});
  Fixture f;
  ReadMetrics miss;
  ReadContext ctx{t, &miss, 4, 100, {}};
  const RemoteNodeView v{0, 1, 0, 0, kCap8};
  (void)f.strategy.read(v, ctx); // hint = 1
  check_baseline3(miss);
  init_layer(t, 0, kCap8, {11, 12, 13, 14});
  t.truncate = sizeof(std::uint32_t) + 1 * sizeof(NodeId); // hint read ok
  ReadMetrics failed;
  ReadContext ctx2{t, &failed, 4, 100, {}};
  const auto bad = f.strategy.read(v, ctx2);
  assert(!bad.ok && bad.neighbors.empty());
  assert(failed.failed_reads == 1 && failed.fallbacks == 0);
  assert(f.cached_degree(0, 0) == 1); // hint not poisoned
  // The entry is still the old value, so the next read retries the fallback.
  t.truncate = 0;
  ReadMetrics m;
  ReadContext ctx3{t, &m, 4, 100, {}};
  const auto good = f.strategy.read(v, ctx3);
  assert(good.ok && good.neighbors.size() == 4);
  assert(good.neighbors[3] == 14);
  assert(m.cache_hits == 1 && m.fallbacks == 1);
  check_baseline3(m);
}

// --- 14. header short read / hard failure --------------------------------
void test_header_read_failure() {
  {
    FakeTransport t;
    init_layer(t, 0, kCap8, {1});
    t.truncate = 2; // only 2 of the 4 degree bytes arrive
    Fixture f;
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    const auto r = f.strategy.read({0, 1, 0, 0, kCap8}, ctx);
    assert(!r.ok && r.neighbors.empty());
    assert(m.failed_reads == 1 && m.read_ops == 0 && m.layer_reads == 0);
    assert(t.calls == 1 && f.cache->entries.empty());
  }
  {
    FakeTransport t;
    init_layer(t, 0, kCap8, {1});
    t.fail = true;
    Fixture f;
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    const auto r = f.strategy.read({0, 1, 0, 0, kCap8}, ctx);
    assert(!r.ok && r.neighbors.empty());
    assert(m.failed_reads == 1 && t.calls == 1);
    assert(f.cache->entries.empty());
  }
}

// --- 15. strict LRU eviction --------------------------------------------
void test_lru_eviction() {
  FakeTransport t;
  init_layer(t, 0, 4, {1});
  init_layer(t, 64, 4, {2});
  init_layer(t, 128, 4, {3});
  Fixture f(/*capacity=*/2);
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const RemoteNodeView a{0, 1, 0, 0, 4};
  const RemoteNodeView b{1, 1, 0, 64, 4};
  const RemoteNodeView c{2, 1, 0, 128, 4};
  (void)f.strategy.read(a, ctx);
  (void)f.strategy.read(b, ctx);
  (void)f.strategy.read(c, ctx); // evicts a (least recently used)
  assert(m.cache_evictions == 1);
  assert(m.cache_misses == 3 && m.cache_hits == 0);
  assert(!f.is_cached(0, 0) && f.is_cached(1, 0) && f.is_cached(2, 0));
  (void)f.strategy.read(a, ctx); // miss again, evicts b
  assert(m.cache_misses == 4 && m.cache_evictions == 2);
  const auto rc = f.strategy.read(c, ctx); // still cached -> hit
  assert(rc.ok && rc.neighbors.size() == 1 && rc.neighbors[0] == 3);
  assert(m.cache_hits == 1 && f.strategy.cache_size() == 2);
  check_baseline3(m);
}

// --- 16. generation must never decide a hit (DR-5) ------------------------
void test_generation_is_not_a_predictor() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {4, 5});
  Fixture f;
  {
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    (void)f.strategy.read({0, 7, 0, 0, kCap8}, ctx); // generation 7
    // Same degree, different generation: the hint is still valid.
    const auto r = f.strategy.read({0, 99, 0, 0, kCap8}, ctx);
    assert(r.ok && r.neighbors.size() == 2);
    assert(m.cache_hits == 1 && m.fallbacks == 0);
    assert(m.read_ops == 2 + 1); // miss = 2 reads, hit = 1 read
    check_baseline3(m);
  }
  {
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    // Same generation, different degree: the cache must notice via the read.
    init_layer(t, 0, kCap8, {4, 5, 6, 7});
    const auto r = f.strategy.read({0, 99, 0, 0, kCap8}, ctx);
    assert(r.ok && r.neighbors.size() == 4);
    assert(m.cache_hits == 1 && m.fallbacks == 1);
    assert(m.read_ops == 2);
    check_baseline3(m);
  }
}

// --- 17. the cache outlives a strategy instance (reload / re-injection) ---
void test_cache_survives_reinjection() {
  FakeTransport t;
  init_layer(t, 0, kCap8, {4, 5, 6});
  SharedLengthCache cache = std::make_shared<LengthCache>();
  {
    Baseline3CachedLengthStrategy first(0, cache);
    ReadMetrics m;
    ReadContext ctx{t, &m, 4, 100, {}};
    (void)first.read({0, 1, 0, 0, kCap8}, ctx);
    assert(m.cache_misses == 1);
  }
  // A brand-new strategy object (what set_read_strategy() does after reload).
  Baseline3CachedLengthStrategy second(0, cache);
  ReadMetrics m;
  ReadContext ctx{t, &m, 4, 100, {}};
  const auto r = second.read({0, 1, 0, 0, kCap8}, ctx);
  assert(r.ok && r.neighbors.size() == 3);
  assert(m.cache_hits == 1 && m.cache_misses == 0);
  assert(m.read_ops == 1); // the learned hint survived
  check_baseline3(m);
}

// --- 18. multi-level node: per-level offsets and capacities --------------
void test_multi_level_offsets() {
  const auto path = unique_path("rdma_hnsw_b3_levels");
  IndexHeader h{};
  h.dim = 1;
  h.M = 2; // cap(0) = 4, cap(l>0) = 2
  h.max_level = 2;
  std::vector<NodeRecord> nodes(6);
  for (NodeId i = 0; i < nodes.size(); ++i) {
    nodes[i].id = i;
    nodes[i].vector = {static_cast<float>(i)};
  }
  nodes[0].level = 2;
  nodes[0].neighbors = {{1, 2, 3, 4}, {5, 1}, {2}};
  GraphStore::write(path, h, nodes);
  auto c = GraphStore::load_catalog(path);
  auto t = make_mmap_transport(path, false);

  const std::vector<std::vector<NodeId>> expected = {{1, 2, 3, 4}, {5, 1}, {2}};
  Fixture f;
  for (std::uint32_t level = 0; level <= h.max_level; ++level) {
    ReadMetrics miss;
    ReadContext ctx{*t, &miss, 4, 100, {}};
    const RemoteNodeView v{0, 1, level, node_layer_offset(c, 0, level),
                           c.layer_capacity(level)};
    const auto r = f.strategy.read(v, ctx);
    assert(r.ok && r.neighbors == expected[level]);
    assert(miss.requested_bytes ==
           sizeof(std::uint32_t) + expected[level].size() * sizeof(NodeId));
    assert(miss.wasted_bytes == 0);
    check_baseline3(miss);
    ReadMetrics hit;
    ReadContext ctx2{*t, &hit, 4, 100, {}};
    const auto r2 = f.strategy.read(v, ctx2);
    assert(r2.neighbors == expected[level]);
    assert(hit.read_ops == 1); // one read on a hit, whatever the level
    check_baseline3(hit);
  }
  assert(c.layer_capacity(0) == 4 && c.layer_capacity(1) == 2);
  assert(f.cache->entries.size() == 3);
  std::remove(path.c_str());
}

} // namespace

int main() {
  test_equivalence_with_baseline1();
  test_cold_and_hot_agree();
  test_state_a_miss();
  test_state_b_overestimate();
  test_state_b_exact();
  test_state_c_underestimate();
  test_hint_zero_then_nonzero();
  test_layer_isolation();
  test_degree_greater_than_capacity();
  test_zero_degree();
  test_zero_capacity();
  test_failure_does_not_pollute_cache();
  test_fallback_failure_keeps_old_hint();
  test_header_read_failure();
  test_lru_eviction();
  test_generation_is_not_a_predictor();
  test_cache_survives_reinjection();
  test_multi_level_offsets();
  std::puts("read_strategy_cached_test: all cases passed");
  return 0;
}
