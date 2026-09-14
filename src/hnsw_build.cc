#include "rdma_hnsw/common/config.h"
#include "rdma_hnsw/common/data.h"
#include "rdma_hnsw/graph/hnsw_builder.h"
#include <iostream>

int main(int argc, char **argv) {
  try {
    const auto c = rdma_hnsw::parse_config(argc, argv, "build");
    if (c.help)
      return 0;
    auto data = c.generate
                    ? rdma_hnsw::generate_vectors(c.generated, c.dim, c.seed)
                    : rdma_hnsw::read_fvecs(c.input, c.dim);
    rdma_hnsw::HnswBuilder::build_file(c.output, data, c.M, c.ef_construction,
                                       c.seed);
    std::cout << "built " << data.size() << " vectors -> " << c.output << "\n";
  } catch (const std::exception &e) {
    std::cerr << "hnsw_build: " << e.what() << '\n';
    return 1;
  }
}
