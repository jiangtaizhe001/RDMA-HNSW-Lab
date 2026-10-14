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
}

std::unique_ptr<NeighborReadStrategy> make_read_strategy(ReadPolicy p) {
  if (p == ReadPolicy::Fixed)
    return std::make_unique<Baseline1FixedStrategy>();
  if (p == ReadPolicy::HeaderActual)
    return std::make_unique<Baseline2HeaderFirstStrategy>();
  throw std::invalid_argument("read strategy is a student extension; "
                              "implement and inject it before selecting " +
                              to_string(p));
}
} // namespace rdma_hnsw
