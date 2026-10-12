#include "rdma_hnsw/runtime/query_engine.h"
#include <algorithm>
#include <cmath>
#include <cstring>
#include <queue>
#include <stdexcept>
#include <unordered_set>

namespace rdma_hnsw {
namespace {
float distance(const Vector &a, const Vector &b, DistanceKind kind) {
  float d = 0;
  const auto n = std::min(a.size(), b.size());
  if (kind == DistanceKind::InnerProduct) {
    for (std::size_t i = 0; i < n; ++i)
      d -= a[i] * b[i];
    return d;
  }
  for (std::size_t i = 0; i < n; ++i) {
    const float x = a[i] - b[i];
    d += x * x;
  }
  return d;
}
} // namespace
QueryEngine::QueryEngine(IndexCatalog catalog, RemoteTransport &t, ReadPolicy p,
                         std::size_t ef, std::size_t k, std::size_t g,
                         std::size_t cap, DistanceKind distance_kind)
    : catalog_(std::move(catalog)), transport_(t),
      strategy_(make_read_strategy(p)),
      context_{transport_, &stats_.reads, g, cap, {}}, ef_search_(ef), k_(k),
      cache_capacity_(cap), distance_kind_(distance_kind) {}

void QueryEngine::set_read_strategy(
    std::unique_ptr<NeighborReadStrategy> strategy) {
  if (!strategy)
    throw std::invalid_argument("read strategy cannot be null");
  strategy_ = std::move(strategy);
  context_.length_cache.clear();
}

void QueryEngine::reset_catalog(IndexCatalog catalog) {
  catalog_ = std::move(catalog);
  context_.length_cache.clear();
}

QueryEngine::NodeData QueryEngine::read_node(NodeId id) {
  if (id >= catalog_.offsets.size())
    throw std::out_of_range("query references invalid node");
  const auto n = sizeof(NodeDiskHeader) + catalog_.header.dim * sizeof(float);
  std::vector<byte> b(n);
  const auto r = transport_.read({catalog_.offsets[id], 0}, b);
  // Node (header + vector) traffic is accounted separately from the neighbor
  // region so that `requested_bytes == useful_bytes + wasted_bytes` holds for
  // neighbor reads and node traffic can be reported on its own.
  stats_.reads.node_reads++;
  stats_.reads.node_bytes += n;
  if (!r.ok || r.bytes != n)
    throw std::runtime_error("remote node read failed");
  NodeDiskHeader dh{};
  std::memcpy(&dh, b.data(), sizeof(dh));
  NodeData out{dh.id, dh.generation, dh.level, dh.deleted != 0,
               Vector(catalog_.header.dim)};
  std::memcpy(out.vector.data(), b.data() + sizeof(dh),
              out.vector.size() * sizeof(float));
  return out;
}

std::vector<NodeId> QueryEngine::search(const Vector &query) {
  if (query.size() != catalog_.header.dim)
    throw std::invalid_argument("query dimension mismatch");
  if (catalog_.header.node_count == 0)
    return {};
  struct Candidate {
    float d;
    NodeId id;
    bool operator>(const Candidate &o) const { return d > o.d; }
    bool operator<(const Candidate &o) const { return d < o.d; }
  };
  std::priority_queue<Candidate, std::vector<Candidate>, std::greater<>>
      frontier;
  std::priority_queue<Candidate> best;
  std::unordered_set<NodeId> visited;
  frontier.push({0, catalog_.header.entrypoint});
  std::size_t expanded = 0;
  while (!frontier.empty() && expanded < ef_search_) {
    const auto cur = frontier.top();
    frontier.pop();
    if (!visited.insert(cur.id).second)
      continue;
    const auto node = read_node(cur.id);
    if (node.deleted)
      continue;
    const auto d = distance(query, node.vector, distance_kind_);
    ++expanded;
    ++stats_.visited_nodes;
    best.push({d, node.id});
    if (best.size() > ef_search_)
      best.pop();
    const auto max_level =
        std::min<std::uint32_t>(node.level, catalog_.header.max_level);
    for (std::uint32_t level = 0; level <= max_level; ++level) {
      RemoteNodeView view{node.id, node.generation, level,
                          node_layer_offset(catalog_, node.id, level),
                          catalog_.layer_capacity(level)};
      const auto neighbors = strategy_->read(view, context_);
      if (!neighbors.ok)
        throw std::runtime_error(neighbors.error);
      for (const auto id : neighbors.neighbors)
        if (id < catalog_.header.node_count && !visited.contains(id)) {
          const auto nn = read_node(id);
          if (!nn.deleted)
            frontier.push({distance(query, nn.vector, distance_kind_), id});
        }
    }
  }
  std::vector<NodeId> result;
  while (!best.empty()) {
    result.push_back(best.top().id);
    best.pop();
  }
  std::reverse(result.begin(), result.end());
  if (result.size() > k_)
    result.resize(k_);
  return result;
}

QueryStats QueryEngine::run(const std::vector<Vector> &queries,
                            const std::vector<Vector> *base) {
  if (base) {
    for (const auto &vector : *base)
      if (vector.size() != catalog_.header.dim)
        throw std::invalid_argument("ground-truth vector dimension mismatch");
  }
  const auto start = std::chrono::steady_clock::now();
  double recall_sum = 0;
  for (const auto &q : queries) {
    const auto result = search(q);
    if (base && !base->empty()) {
      std::vector<std::pair<float, NodeId>> exact;
      exact.reserve(base->size());
      for (NodeId i = 0; i < base->size(); ++i)
        exact.emplace_back(distance(q, (*base)[i], distance_kind_), i);
      const auto truth_size = std::min(k_, exact.size());
      std::partial_sort(exact.begin(), exact.begin() + truth_size, exact.end());
      std::unordered_set<NodeId> truth;
      for (std::size_t i = 0; i < truth_size; ++i)
        truth.insert(exact[i].second);
      for (const auto id : result)
        if (truth.contains(id))
          recall_sum += 1.0 / std::max<std::size_t>(1, k_);
    }
    ++stats_.queries;
  }
  stats_.elapsed_ms = std::chrono::duration<double, std::milli>(
                          std::chrono::steady_clock::now() - start)
                          .count();
  stats_.recall = base && !queries.empty() ? recall_sum / queries.size() : 0.0;
  return stats_;
}
} // namespace rdma_hnsw
