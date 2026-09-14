#include "rdma_hnsw/graph/hnsw_builder.h"
#include "hnswlib/hnswalg.h"
#include "hnswlib/space_l2.h"
#include <algorithm>
#include <cstring>
#include <limits>
#include <stdexcept>

namespace rdma_hnsw {
HnswBuilder::HnswBuilder(std::size_t dim, std::size_t M,
                         std::size_t ef_construction, std::uint64_t seed)
    : dim_(dim), M_(M), ef_construction_(ef_construction), seed_(seed) {
  if (dim_ == 0 || M_ < 2 || ef_construction_ == 0)
    throw std::invalid_argument(
        "builder dimension, M (>=2), and ef_construction must be positive");
}

std::vector<NodeRecord> HnswBuilder::build(const std::vector<Vector> &vectors) {
  if (vectors.empty() || vectors.front().empty())
    throw std::invalid_argument("cannot build empty index");
  const std::size_t dim = vectors.front().size();
  if (dim > std::numeric_limits<std::uint32_t>::max())
    throw std::invalid_argument("vector dimension exceeds index format");
  if (dim_ && dim_ != dim)
    throw std::invalid_argument("builder dimension mismatch");
  for (const auto &vector : vectors)
    if (vector.size() != dim)
      throw std::invalid_argument("vectors have inconsistent dimensions");
  hnswlib::L2Space space(dim);
  hnswlib::HierarchicalNSW<float> index(&space, vectors.size(), M_,
                                        ef_construction_, seed_);
  for (std::size_t i = 0; i < vectors.size(); ++i)
    index.addPoint(vectors[i].data(), i);
  const auto count = index.cur_element_count.load();
  const auto max_level =
      static_cast<std::uint32_t>(std::max(0, index.maxlevel_));
  std::vector<NodeRecord> nodes(count);
  for (std::uint32_t i = 0; i < count; ++i) {
    auto &n = nodes[i];
    n.id = static_cast<NodeId>(index.getExternalLabel(i));
    n.generation = 1;
    n.level = static_cast<std::uint32_t>(std::max(0, index.element_levels_[i]));
    n.vector.resize(dim);
    std::memcpy(n.vector.data(), index.getDataByInternalId(i),
                dim * sizeof(float));
    n.neighbors.resize(max_level + 1);
    auto export_layer = [&](std::uint32_t l, const void *raw_ptr) {
      auto *ptr = static_cast<const char *>(raw_ptr);
      const auto degree =
          *reinterpret_cast<const hnswlib::linklistsizeint *>(ptr);
      const auto cap = l == 0 ? index.maxM0_ : index.maxM_;
      n.neighbors[l].reserve(std::min<std::size_t>(degree, cap));
      auto *ids = reinterpret_cast<const hnswlib::tableint *>(
          ptr + sizeof(hnswlib::linklistsizeint));
      for (std::size_t j = 0; j < std::min<std::size_t>(degree, cap); ++j)
        n.neighbors[l].push_back(
            static_cast<NodeId>(index.getExternalLabel(ids[j])));
    };
    export_layer(0, index.get_linklist0(i));
    for (std::uint32_t l = 1; l <= n.level; ++l)
      export_layer(l, index.linkLists_[i] +
                          (l - 1) * index.size_links_per_element_);
  }
  return nodes;
}

void HnswBuilder::build_file(const std::string &output,
                             const std::vector<Vector> &vectors, std::size_t M,
                             std::size_t ef_construction, std::uint64_t seed) {
  if (vectors.empty())
    throw std::invalid_argument("no vectors");
  if (vectors.front().empty() || M < 2 || ef_construction == 0)
    throw std::invalid_argument(
        "vectors, M (>=2), and ef_construction must be positive");
  for (const auto &vector : vectors)
    if (vector.size() != vectors.front().size())
      throw std::invalid_argument("vectors have inconsistent dimensions");
  if (vectors.front().size() > std::numeric_limits<std::uint32_t>::max())
    throw std::invalid_argument("vector dimension exceeds index format");
  hnswlib::L2Space space(vectors.front().size());
  const std::size_t max_elements =
      std::max<std::size_t>(vectors.size() + 1024, vectors.size());
  hnswlib::HierarchicalNSW<float> index(&space, max_elements, M,
                                        ef_construction, seed);
  for (std::size_t i = 0; i < vectors.size(); ++i)
    index.addPoint(vectors[i].data(), i);
  const auto count = index.cur_element_count.load();
  const auto max_level =
      static_cast<std::uint32_t>(std::max(0, index.maxlevel_));
  std::vector<NodeRecord> nodes(count);
  for (std::uint32_t i = 0; i < count; ++i) {
    auto &n = nodes[i];
    n.id = static_cast<NodeId>(index.getExternalLabel(i));
    n.generation = 1;
    n.level = static_cast<std::uint32_t>(std::max(0, index.element_levels_[i]));
    n.vector.resize(vectors.front().size());
    std::memcpy(n.vector.data(), index.getDataByInternalId(i),
                n.vector.size() * sizeof(float));
    n.neighbors.resize(max_level + 1);
    auto export_layer = [&](std::uint32_t l, const void *raw_ptr) {
      auto *ptr = static_cast<const char *>(raw_ptr);
      const auto degree =
          *reinterpret_cast<const hnswlib::linklistsizeint *>(ptr);
      const auto cap = l == 0 ? index.maxM0_ : index.maxM_;
      auto *ids = reinterpret_cast<const hnswlib::tableint *>(
          ptr + sizeof(hnswlib::linklistsizeint));
      for (std::size_t j = 0; j < std::min<std::size_t>(degree, cap); ++j)
        n.neighbors[l].push_back(
            static_cast<NodeId>(index.getExternalLabel(ids[j])));
    };
    export_layer(0, index.get_linklist0(i));
    for (std::uint32_t l = 1; l <= n.level; ++l)
      export_layer(l, index.linkLists_[i] +
                          (l - 1) * index.size_links_per_element_);
  }
  IndexHeader h{};
  h.dim = vectors.front().size();
  h.M = static_cast<std::uint32_t>(M);
  h.max_level = max_level;
  h.entrypoint =
      static_cast<NodeId>(index.getExternalLabel(index.enterpoint_node_));
  h.generation = 1;
  GraphStore::write(output, h, nodes);
}
} // namespace rdma_hnsw
