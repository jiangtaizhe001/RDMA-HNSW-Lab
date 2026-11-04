#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/read/read_strategy.h"
#include "rdma_hnsw/transport/transport.h"
#include <cstdint>
#include <iostream>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace {

std::vector<std::size_t> parse_gs(const std::string &text) {
  std::vector<std::size_t> gs;
  std::size_t begin = 0;
  while (begin <= text.size()) {
    const auto end = text.find(',', begin);
    const auto token = text.substr(begin, end == std::string::npos
                                             ? std::string::npos
                                             : end - begin);
    if (!token.empty())
      gs.push_back(static_cast<std::size_t>(std::stoull(token)));
    if (end == std::string::npos)
      break;
    begin = end + 1;
  }
  if (gs.empty())
    throw std::invalid_argument("empty --gs");
  return gs;
}

} // namespace

// Load a built index and require B1/B2/B3 and every prefix G to return the same
// neighbour sequence for every (node, level), cold and hot.  A mismatch exits
// non-zero so an experiment cannot silently run on a broken strategy/index.
int main(int argc, char **argv) {
  try {
    std::string path;
    std::vector<std::size_t> gs{1, 2, 4, 8, 16, 32};
    for (int i = 1; i < argc; ++i) {
      const std::string a = argv[i];
      if (a == "--index") {
        if (++i >= argc)
          throw std::invalid_argument("missing --index value");
        path = argv[i];
      } else if (a == "--gs") {
        if (++i >= argc)
          throw std::invalid_argument("missing --gs value");
        gs = parse_gs(argv[i]);
      } else {
        throw std::invalid_argument("unknown argument: " + a);
      }
    }
    if (path.empty())
      throw std::invalid_argument(
          "usage: check_prefix_index --index FILE [--gs 1,2,4,8,16,32]");

    const auto catalog = rdma_hnsw::GraphStore::load_catalog(path);
    const auto records = rdma_hnsw::GraphStore::read_all(catalog);
    auto transport = rdma_hnsw::make_mmap_transport(path, false);
    const auto region = std::numeric_limits<rdma_hnsw::Offset>::max();

    rdma_hnsw::Baseline1FixedStrategy fixed;
    rdma_hnsw::Baseline2HeaderFirstStrategy header;
    rdma_hnsw::Baseline3CachedLengthStrategy cached;
    rdma_hnsw::ReadMetrics m1, m2, m3;
    rdma_hnsw::ReadContext c1{*transport, &m1, 4, 1000000, {}, {}, region};
    rdma_hnsw::ReadContext c2{*transport, &m2, 4, 1000000, {}, {}, region};
    rdma_hnsw::ReadContext c3{*transport, &m3, 4, 1000000, {}, {}, region};

    std::uint64_t layers = 0, mismatches = 0;
    for (const auto &record : records) {
      for (std::uint32_t level = 0; level <= record.level; ++level) {
        const rdma_hnsw::RemoteNodeView view{
            record.id, record.generation, level,
            rdma_hnsw::node_layer_offset(catalog, record.id, level),
            catalog.layer_capacity(level)};
        const auto expected = fixed.read(view, c1);
        if (!expected.ok) {
          std::cerr << "B1 failed at node " << record.id << " level " << level
                    << ": " << expected.error << '\n';
          return 1;
        }
        ++layers;
        const auto check = [&](const char *name,
                               const rdma_hnsw::NeighborReadResult &r,
                               bool hot) {
          if (!r.ok || r.neighbors != expected.neighbors) {
            if (mismatches < 20)
              std::cerr << name << (hot ? " hot" : " cold")
                        << " mismatch at node " << record.id << " level "
                        << level << '\n';
            ++mismatches;
          }
        };
        check("B2", header.read(view, c2), false);
        check("B3", cached.read(view, c3), false);
        for (const auto g : gs) {
          rdma_hnsw::QuantizedPrefixStrategy prefix(
              std::make_shared<rdma_hnsw::PrefixCache>());
          rdma_hnsw::ReadMetrics mp;
          rdma_hnsw::ReadContext cp{*transport, &mp, g, 1000000, {}, {}, region};
          check("QP cold", prefix.read(view, cp), false);
          check("QP hot", prefix.read(view, cp), true);
        }
      }
    }
    std::cout << "checked " << records.size() << " nodes, " << layers
              << " layers, G=";
    for (std::size_t i = 0; i < gs.size(); ++i)
      std::cout << (i ? "," : "") << gs[i];
    std::cout << "; mismatches=" << mismatches << '\n';
    return mismatches ? 1 : 0;
  } catch (const std::exception &e) {
    std::cerr << "check_prefix_index: " << e.what() << '\n';
    return 1;
  }
}
