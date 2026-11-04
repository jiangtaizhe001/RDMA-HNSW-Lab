#include "rdma_hnsw/read/read_strategy.h"
#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/graph/hnsw_builder.h"
#include <algorithm>
#include <cassert>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <limits>
#include <memory>
#include <numeric>
#include <string>
#include <vector>

using namespace rdma_hnsw;

namespace {
class FakeTransport final : public RemoteTransport {
public:
  std::vector<byte> data;
  std::vector<std::pair<Offset, std::size_t>> reads;
  std::size_t fail_on{};
  std::size_t short_on{};
  void layer(std::size_t capacity, std::size_t degree) {
    data.resize(4 + 4 * capacity);
    const auto raw = static_cast<std::uint32_t>(degree);
    std::memcpy(data.data(), &raw, 4);
    for (std::size_t slot = 0; slot < capacity; ++slot) {
      const auto id = static_cast<NodeId>(slot + 1);
      std::memcpy(data.data() + 4 + 4 * slot, &id, 4);
    }
  }
  ReadResult read(RemoteAddress address, std::span<byte> dst) override {
    reads.emplace_back(address.offset, dst.size());
    if (reads.size() == fail_on)
      return {false, 0, "injected failure"};
    if (address.offset > data.size())
      return {false, 0, "out of bounds"};
    auto count = std::min(dst.size(), data.size() - address.offset);
    if (reads.size() == short_on && count)
      --count;
    std::memcpy(dst.data(), data.data() + address.offset, count);
    return {true, count, {}};
  }
  WriteResult write(RemoteAddress, std::span<const byte>) override { return {}; }
  UpdateResult submit_update(const UpdateRequest &) override { return {}; }
  const TransportStats &stats() const override { return stats_; }
private:
  TransportStats stats_;
};

void identities(const ReadMetrics &metrics) {
  assert(metrics.failed_reads == 0);
  assert(metrics.layer_attempts == metrics.layer_reads);
  assert(metrics.cache_hits + metrics.cache_misses == metrics.layer_reads);
  assert(metrics.prefix_reads == metrics.layer_reads);
  assert(metrics.read_ops == metrics.prefix_reads + metrics.fallbacks);
  assert(metrics.dependent_reads == metrics.fallbacks);
  assert(metrics.fallbacks == metrics.cold_fallbacks + metrics.cached_fallbacks);
  assert(metrics.fallback_bytes == metrics.cold_fallback_bytes + metrics.cached_fallback_bytes);
  assert(metrics.requested_bytes == metrics.useful_bytes + metrics.wasted_bytes);
  assert(metrics.wasted_bytes == metrics.rounding_waste_bytes + metrics.drift_waste_bytes +
                                 metrics.cold_prefix_waste_bytes);
  assert(metrics.successful_reads == metrics.read_ops);
  assert(metrics.received_bytes == metrics.requested_bytes);
}

void enumeration() {
  for (const auto capacity : {1u, 8u, 18u, 32u})
    for (const auto granularity : {1u, 2u, 4u, 8u, 16u, 64u}) {
      FakeTransport transport;
      QuantizedPrefixStrategy prefix;
      Baseline1FixedStrategy fixed;
      Baseline2HeaderFirstStrategy header;
      Baseline3CachedLengthStrategy cached;
      ReadMetrics metrics;
      ReadContext context{transport, &metrics, granularity, 100, {}};
      ReadContext baseline{transport};
      const RemoteNodeView view{7, 1, 0, 0, capacity};
      for (std::size_t degree = 0; degree <= capacity; ++degree) {
        transport.layer(capacity, degree);
        const auto expected = fixed.read(view, baseline);
        assert(header.read(view, baseline).neighbors == expected.neighbors);
        assert(cached.read(view, baseline).neighbors == expected.neighbors);
        for (int repeat = 0; repeat < 2; ++repeat) {
          const auto before = transport.reads.size();
          const auto result = prefix.read(view, context);
          assert(result.ok && result.neighbors == expected.neighbors);
          assert(transport.reads.size() - before <= 2);
        }
      }
      for (std::size_t degree = capacity; degree > 0; --degree) {
        transport.layer(capacity, degree - 1);
        assert(prefix.read(view, context).neighbors == fixed.read(view, baseline).neighbors);
      }
      identities(metrics);
      ReadMetrics merged;
      merged.merge(metrics);
      identities(merged);
    }
}

void growth_and_lifetime() {
  FakeTransport transport;
  auto cache = std::make_shared<PrefixCache>();
  QuantizedPrefixStrategy strategy(cache);
  ReadMetrics metrics;
  ReadContext context{transport, &metrics, 4, 100, {}};
  RemoteNodeView view{1, 1, 0, 0, 32};
  transport.layer(32, 21);
  assert(strategy.read(view, context).ok);
  QuantizedPrefixStrategy reloaded(cache);
  transport.layer(32, 23);
  view.generation = 99;
  auto before = transport.reads.size();
  assert(reloaded.read(view, context).ok);
  assert(transport.reads.size() == before + 1);
  assert(transport.reads.back().second == 100);
  transport.layer(32, 25);
  assert(reloaded.read(view, context).ok);
  transport.layer(32, 32);
  before = transport.reads.size();
  assert(reloaded.read(view, context).ok);
  assert(transport.reads.size() == before + 2);
  assert(transport.reads.back() == std::make_pair(Offset{116}, std::size_t{16}));
  identities(metrics);
  context.prefix_granularity = 8;
  transport.layer(32, 0);
  assert(reloaded.read(view, context).ok);
  assert(transport.reads.back().second == 36);
  assert(cache->entries.at(length_cache_key(1, 0)).level_hint == 0);
  transport.layer(32, 2);
  assert(reloaded.read(view, context).ok);
  assert(transport.reads[transport.reads.size() - 2].second == 4);
}

void failures() {
  for (const bool hot : {false, true})
    for (const bool short_read : {false, true})
      for (const std::size_t stage : {1u, 2u}) {
        FakeTransport transport;
        QuantizedPrefixStrategy strategy;
        ReadMetrics metrics;
        ReadContext context{transport, &metrics, 4, 10, {}};
        const RemoteNodeView view{1, 1, 0, 0, 32};
        if (hot) {
          transport.layer(32, 2);
          assert(strategy.read(view, context).ok);
        }
        transport.layer(32, 9);
        const auto target = transport.reads.size() + stage;
        if (short_read)
          transport.short_on = target;
        else
          transport.fail_on = target;
        const auto result = strategy.read(view, context);
        assert(!result.ok && result.neighbors.empty());
        assert(metrics.failed_reads == 1);
        assert(metrics.prefix_failures == (stage == 1));
        assert(metrics.tail_failures == (stage == 2));
        assert(strategy.cache()->entries.size() == (hot ? 1 : 0));
        if (hot)
          assert(strategy.cache()->entries.at(length_cache_key(1, 0)).last_degree == 2);
        transport.short_on = transport.fail_on = 0;
        assert(strategy.read(view, context).ok);
      }
}

void cache_and_invalid_inputs() {
  FakeTransport transport;
  transport.layer(8, 2);
  QuantizedPrefixStrategy strategy;
  ReadMetrics metrics;
  ReadContext context{transport, &metrics, 4, 2, {}};
  RemoteNodeView view{1, 1, 0, 0, 8};
  assert(strategy.read(view, context).ok);
  view.level = 1;
  assert(strategy.read(view, context).ok);
  view.level = 0;
  assert(strategy.read(view, context).ok);
  view.id = 2;
  assert(strategy.read(view, context).ok);
  assert(!strategy.cache()->entries.contains(length_cache_key(1, 1)));
  assert(metrics.cache_evictions == 1);
  identities(metrics);
  context.cache_capacity = 0;
  const auto hits = metrics.cache_hits;
  assert(strategy.read(view, context).ok);
  assert(strategy.read(view, context).ok);
  assert(metrics.cache_hits == hits);
  context.valid_neighbor = [](NodeId id) { return id != 2; };
  assert(!strategy.read(view, context).ok);
  context.valid_neighbor = {};
  transport.layer(8, 9);
  assert(!strategy.read(view, context).ok);
  assert(metrics.malformed_degrees == 1);
  transport.layer(8, 2);
  const auto calls = transport.reads.size();
  context.prefix_granularity = 0;
  assert(!strategy.read(view, context).ok);
  context.prefix_granularity = 4;
  view.capacity = 0;
  assert(!strategy.read(view, context).ok);
  view.capacity = std::numeric_limits<std::size_t>::max();
  assert(!strategy.read(view, context).ok);
  view.capacity = 8;
  view.layer_offset = std::numeric_limits<Offset>::max() - 2;
  assert(!strategy.read(view, context).ok);
  view.layer_offset = 0;
  context.region_bytes = 8;
  assert(!strategy.read(view, context).ok);
  assert(transport.reads.size() == calls);
}

std::string unique_path(const char *stem) {
  return (std::filesystem::temp_directory_path() /
          (std::string(stem) + ".bin"))
      .string();
}

// The cache stores only the level, never neighbour payload.  Rewriting the
// same degree with different ids must therefore return the new ids.
void same_degree_new_content() {
  FakeTransport transport;
  QuantizedPrefixStrategy strategy;
  ReadMetrics metrics;
  ReadContext context{transport, &metrics, 4, 10, {}};
  const RemoteNodeView view{1, 1, 0, 0, 16};
  transport.layer(16, 3);
  const auto first = strategy.read(view, context);
  assert(first.ok);
  assert((first.neighbors == std::vector<NodeId>{1, 2, 3}));
  for (std::size_t slot = 0; slot < 3; ++slot) {
    const auto id = static_cast<NodeId>(100 + slot);
    std::memcpy(transport.data.data() + 4 + 4 * slot, &id, sizeof(id));
  }
  const auto second = strategy.read(view, context);
  assert(second.ok);
  assert((second.neighbors == std::vector<NodeId>{100, 101, 102}));
  assert(metrics.cache_hits == 1 && metrics.cached_fallbacks == 0);
  identities(metrics);
}

// Build a real HNSW file and require B1/B2/B3/prefix to return the same
// neighbour sequence for every (node, level), cold and hot, across G values.
void real_index_layers() {
  const auto path = unique_path("rdma_hnsw_prefix_real");
  std::vector<Vector> vectors(80, Vector(8));
  for (std::size_t i = 0; i < vectors.size(); ++i)
    for (std::size_t d = 0; d < vectors[i].size(); ++d)
      vectors[i][d] =
          static_cast<float>((i * 31 + d * 7) % 101) / 101.0f;
  HnswBuilder::build_file(path, vectors, 6, 40, 11);
  const auto catalog = GraphStore::load_catalog(path);
  auto transport = make_mmap_transport(path, false);
  GraphStore store(catalog);
  const auto records = store.snapshot();

  Baseline1FixedStrategy fixed;
  Baseline2HeaderFirstStrategy header;
  Baseline3CachedLengthStrategy cached;
  ReadMetrics m1, m2, m3;
  const auto region = std::numeric_limits<Offset>::max();
  ReadContext c1{*transport, &m1, 4, 100000, {}, {}, region};
  ReadContext c2{*transport, &m2, 4, 100000, {}, {}, region};
  ReadContext c3{*transport, &m3, 4, 100000, {}, {}, region};

  for (const auto &record : records) {
    assert(record.neighbors.size() == catalog.header.max_level + 1);
    for (std::uint32_t level = 0; level <= record.level; ++level) {
      const RemoteNodeView view{
          record.id, record.generation, level,
          node_layer_offset(catalog, record.id, level),
          catalog.layer_capacity(level)};
      const auto expected = fixed.read(view, c1);
      assert(expected.ok);
      assert(expected.neighbors == record.neighbors[level]);
      assert(header.read(view, c2).neighbors == expected.neighbors);
      assert(cached.read(view, c3).neighbors == expected.neighbors);
      for (const auto granularity : {1u, 2u, 4u, 8u, 16u, 64u}) {
        QuantizedPrefixStrategy prefix;
        ReadMetrics mp;
        ReadContext cp{*transport, &mp, granularity, 100000, {}, {}, region};
        const auto cold = prefix.read(view, cp);
        assert(cold.ok && cold.neighbors == expected.neighbors);
        const auto hot = prefix.read(view, cp);
        assert(hot.ok && hot.neighbors == expected.neighbors);
        identities(mp);
      }
    }
  }
  std::remove(path.c_str());
}
} // namespace

int main() {
  assert(std::string(make_read_strategy(ReadPolicy::Prefix)->name()) == "quantized_prefix");
  enumeration();
  growth_and_lifetime();
  failures();
  cache_and_invalid_inputs();
  same_degree_new_content();
  real_index_layers();
}