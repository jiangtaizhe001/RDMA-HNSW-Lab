#pragma once

#include "rdma_hnsw/graph/graph_store.h"
#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace rdma_hnsw {
class HnswBuilder {
public:
  HnswBuilder(std::size_t dim, std::size_t M, std::size_t ef_construction,
              std::uint64_t seed);
  std::vector<NodeRecord> build(const std::vector<Vector> &vectors);
  static void build_file(const std::string &output,
                         const std::vector<Vector> &vectors, std::size_t M,
                         std::size_t ef_construction, std::uint64_t seed);

private:
  std::size_t dim_{}, M_{}, ef_construction_{};
  std::uint64_t seed_{};
};
} // namespace rdma_hnsw
