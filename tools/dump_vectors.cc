#include "rdma_hnsw/common/data.h"
#include <cstdint>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {

std::size_t next_size(int &i, int argc, char **argv) {
  if (++i >= argc)
    throw std::invalid_argument("missing value for " + std::string(argv[i - 1]));
  return static_cast<std::size_t>(std::stoull(argv[i]));
}

} // namespace

// Dump the deterministic generator stream as an .fvecs file.  The index,
// query, update and ground-truth workflows must all be reproducible from
// (count, dim, seed); recomputing this in another language is unsafe because
// std::uniform_real_distribution is implementation-defined.
int main(int argc, char **argv) {
  try {
    std::size_t count = 0, dim = 0, seed = 42;
    std::string output;
    for (int i = 1; i < argc; ++i) {
      const std::string a = argv[i];
      if (a == "--count")
        count = next_size(i, argc, argv);
      else if (a == "--dim")
        dim = next_size(i, argc, argv);
      else if (a == "--seed")
        seed = next_size(i, argc, argv);
      else if (a == "--output") {
        if (++i >= argc)
          throw std::invalid_argument("missing --output value");
        output = argv[i];
      } else {
        throw std::invalid_argument("unknown argument: " + a);
      }
    }
    if (!count || !dim || output.empty())
      throw std::invalid_argument(
          "usage: dump_vectors --count N --dim D --seed S --output FILE");

    const auto vectors = rdma_hnsw::generate_vectors(count, dim, seed);
    std::ofstream out(output, std::ios::binary);
    if (!out)
      throw std::runtime_error("cannot open output: " + output);
    for (const auto &v : vectors) {
      const auto d = static_cast<std::int32_t>(v.size());
      out.write(reinterpret_cast<const char *>(&d), sizeof(d));
      out.write(reinterpret_cast<const char *>(v.data()),
                static_cast<std::streamsize>(v.size() * sizeof(float)));
    }
    if (!out)
      throw std::runtime_error("failed writing output: " + output);
    std::cout << "wrote " << vectors.size() << " x " << dim
              << " vectors -> " << output << '\n';
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "dump_vectors: " << e.what() << '\n';
    return 1;
  }
}
