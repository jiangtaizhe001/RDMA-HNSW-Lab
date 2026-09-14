#include "rdma_hnsw/common/data.h"
#include <fstream>
#include <random>
#include <stdexcept>

namespace rdma_hnsw {
std::vector<Vector> read_fvecs(const std::string &path,
                               std::size_t expected_dim) {
  std::ifstream in(path, std::ios::binary);
  if (!in)
    throw std::runtime_error("cannot open vector file: " + path);
  std::vector<Vector> result;
  while (true) {
    std::int32_t d = 0;
    in.read(reinterpret_cast<char *>(&d), sizeof(d));
    if (in.eof())
      break;
    if (!in || d <= 0 ||
        (expected_dim && static_cast<std::size_t>(d) != expected_dim))
      throw std::runtime_error("invalid fvecs record in " + path);
    Vector v(static_cast<std::size_t>(d));
    in.read(reinterpret_cast<char *>(v.data()),
            static_cast<std::streamsize>(v.size() * sizeof(float)));
    if (!in)
      throw std::runtime_error("truncated fvecs record in " + path);
    result.push_back(std::move(v));
  }
  if (result.empty())
    throw std::runtime_error("empty vector file: " + path);
  return result;
}

std::vector<Vector> generate_vectors(std::size_t count, std::size_t dim,
                                     std::uint64_t seed) {
  std::mt19937_64 gen(seed);
  std::uniform_real_distribution<float> dist(-1.0f, 1.0f);
  std::vector<Vector> result(count, Vector(dim));
  for (auto &v : result)
    for (float &x : v)
      x = dist(gen);
  return result;
}

std::vector<Vector> load_or_generate(const std::string &path, std::size_t count,
                                     std::size_t dim, std::uint64_t seed) {
  return path.empty() ? generate_vectors(count, dim, seed)
                      : read_fvecs(path, dim);
}
} // namespace rdma_hnsw
