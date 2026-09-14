#pragma once
#include "rdma_hnsw/common/types.h"
#include <string>
#include <vector>

namespace rdma_hnsw {
std::vector<Vector> read_fvecs(const std::string &path,
                               std::size_t expected_dim = 0);
std::vector<Vector> generate_vectors(std::size_t count, std::size_t dim,
                                     std::uint64_t seed);
std::vector<Vector> load_or_generate(const std::string &path, std::size_t count,
                                     std::size_t dim, std::uint64_t seed);
} // namespace rdma_hnsw
