#include "rdma_hnsw/runtime/storage_engine.h"
#include <algorithm>
#include <cmath>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

namespace rdma_hnsw {
namespace {
float dist(const Vector &a, const Vector &b) {
  float x = 0;
  for (std::size_t i = 0; i < a.size(); ++i) {
    const float d = a[i] - b[i];
    x += d * d;
  }
  return x;
}
} // namespace
StorageEngine::StorageEngine(const std::string &path)
    : store_(GraphStore::load_catalog(path)) {}
void StorageEngine::refresh_base_neighbors(std::vector<NodeRecord> &nodes,
                                           NodeId target,
                                           std::uint32_t operation) {
  if (target >= nodes.size())
    return;
  if (operation == update_op::Erase) {
    for (auto &other : nodes) {
      if (other.id == target || other.neighbors.empty())
        continue;
      auto &neighbors = other.neighbors[0];
      const auto old_size = neighbors.size();
      neighbors.erase(std::remove(neighbors.begin(), neighbors.end(), target),
                      neighbors.end());
      if (neighbors.size() != old_size)
        ++other.generation;
    }
    return;
  }
  if (nodes[target].deleted)
    return;
  const auto cap = store_.catalog().layer_capacity(0);
  auto &n = nodes[target];
  std::vector<std::pair<float, NodeId>> candidates;
  candidates.reserve(nodes.size());
  for (const auto &other : nodes)
    if (!other.deleted && other.id != n.id)
      candidates.emplace_back(dist(n.vector, other.vector), other.id);
  std::sort(candidates.begin(), candidates.end());
  n.neighbors.resize(std::max<std::size_t>(
      n.neighbors.size(), store_.catalog().header.max_level + 1));
  n.neighbors[0].clear();
  for (std::size_t i = 0; i < std::min(cap, candidates.size()); ++i)
    n.neighbors[0].push_back(candidates[i].second);

  if (operation == update_op::Insert || operation == update_op::Upsert) {
    // Make a newly inserted node reachable from the old component.  This is
    // deliberately small maintenance for a teaching prototype: connect it to
    // the closest base-layer nodes and preserve their fixed slot capacity.
    const auto reverse_count = std::min(cap, candidates.size());
    for (std::size_t i = 0; i < reverse_count; ++i) {
      auto &other = nodes[candidates[i].second];
      if (std::find(other.neighbors[0].begin(), other.neighbors[0].end(),
                    target) != other.neighbors[0].end())
        continue;
      if (other.neighbors[0].size() < cap) {
        other.neighbors[0].push_back(target);
      } else if (cap != 0) {
        other.neighbors[0].back() = target;
      }
      ++other.generation;
    }
  }
}
UpdateResult StorageEngine::apply(const UpdateRequest &request) {
  std::lock_guard lock(mutex_);
  const auto lock_path = store_.catalog().path + ".lock";
  const int lock_fd = ::open(lock_path.c_str(), O_CREAT | O_RDWR, 0666);
  if (lock_fd < 0 || ::flock(lock_fd, LOCK_EX) != 0)
    return {false, 0, "cannot acquire index update lock"};
  struct Guard {
    int fd;
    ~Guard() {
      ::flock(fd, LOCK_UN);
      ::close(fd);
    }
  } guard{lock_fd};
  auto nodes = store_.snapshot();
  if (request.vector.size() != store_.catalog().header.dim && request.op != 3)
    return {false, 0, "update vector dimension mismatch"};
  if (request.op != update_op::Insert && request.op != update_op::Upsert &&
      request.op != update_op::Erase)
    return {false, 0, "unknown update operation"};
  if (request.op == update_op::Insert) {
    if (request.id != nodes.size())
      return {false, 0, "insert ID must equal current node count"};
    NodeRecord n;
    n.id = request.id;
    n.generation = 1;
    n.level = 0;
    n.vector = request.vector;
    n.neighbors.resize(store_.catalog().header.max_level + 1);
    nodes.push_back(std::move(n));
  } else if (request.id >= nodes.size())
    return {false, 0, "unknown node id"};
  if (request.op == update_op::Upsert) {
    nodes[request.id].vector = request.vector;
    nodes[request.id].deleted = false;
    ++nodes[request.id].generation;
  }
  if (request.op == update_op::Erase) {
    nodes[request.id].deleted = true;
    ++nodes[request.id].generation;
  }
  refresh_base_neighbors(nodes, request.id, request.op);
  store_.replace_nodes(std::move(nodes), store_.catalog().header.entrypoint);
  return {true, store_.catalog().header.generation, {}};
}
} // namespace rdma_hnsw
