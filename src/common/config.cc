#include "rdma_hnsw/common/config.h"
#include <cstdlib>
#include <iostream>
#include <stdexcept>

namespace rdma_hnsw {
namespace {
std::string value(int &i, int argc, char **argv) {
  if (++i >= argc)
    throw std::invalid_argument("missing value for " +
                                std::string(argv[i - 1]));
  return argv[i];
}
std::size_t as_size(const std::string &v) {
  return static_cast<std::size_t>(std::stoull(v));
}
} // namespace

Config parse_config(int argc, char **argv, const std::string &mode) {
  Config c;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--help" || a == "-h") {
      c.help = true;
      continue;
    }
    if (a == "--input" || a == "-i")
      c.input = value(i, argc, argv);
    else if (a == "--output" || a == "-o")
      c.output = value(i, argc, argv);
    else if (a == "--index")
      c.index = value(i, argc, argv);
    else if (a == "--queries" || a == "-q")
      c.queries = value(i, argc, argv);
    else if (a == "--base")
      c.base = value(i, argc, argv);
    else if (a == "--updates" || a == "-u")
      c.updates = value(i, argc, argv);
    else if (a == "--host")
      c.host = value(i, argc, argv);
    else if (a == "--bind")
      c.bind = value(i, argc, argv);
    else if (a == "--ib-device")
      c.ib_device = value(i, argc, argv);
    else if (a == "--workload")
      c.workload = value(i, argc, argv);
    else if (a == "--update-op")
      c.update_op = value(i, argc, argv);
    else if (a == "--transport") {
      const auto transport = value(i, argc, argv);
      if (transport != "mmap" && transport != "rdma")
        throw std::invalid_argument("transport must be mmap or rdma");
      c.transport = transport_from_string(transport);
    } else if (a == "--distance") {
      const auto distance = value(i, argc, argv);
      if (distance != "l2" && distance != "ip")
        throw std::invalid_argument("distance must be l2 or ip");
      c.distance = distance == "ip" ? DistanceKind::InnerProduct
                                    : DistanceKind::L2;
    }
    else if (a == "--read-strategy") {
      const auto policy = value(i, argc, argv);
      if (policy != "fixed" && policy != "header_actual" &&
          policy != "header" && policy != "cached_length" &&
          policy != "cached" && policy != "prefix")
        throw std::invalid_argument("unknown read strategy: " + policy);
      c.policy = read_policy_from_string(policy);
    }
    else if (a == "--generate") {
      c.generate = true;
      c.generated = as_size(value(i, argc, argv));
    } else if (a == "--dim")
      c.dim = as_size(value(i, argc, argv));
    else if (a == "--max-elements")
      c.max_elements = as_size(value(i, argc, argv));
    else if (a == "--M" || a == "-M")
      c.M = as_size(value(i, argc, argv));
    else if (a == "--ef-construction")
      c.ef_construction = as_size(value(i, argc, argv));
    else if (a == "--ef-search")
      c.ef_search = as_size(value(i, argc, argv));
    else if (a == "--k")
      c.k = as_size(value(i, argc, argv));
    else if (a == "--queries-count")
      c.query_count = as_size(value(i, argc, argv));
    else if (a == "--updates-count")
      c.update_count = as_size(value(i, argc, argv));
    else if (a == "--threads")
      c.threads = as_size(value(i, argc, argv));
    else if (a == "--cache-capacity")
      c.cache_capacity = as_size(value(i, argc, argv));
    else if (a == "--prefix-granularity")
      c.prefix_granularity = as_size(value(i, argc, argv));
    else if (a == "--port")
      c.port = as_size(value(i, argc, argv));
    else if (a == "--ib-port")
      c.ib_port = as_size(value(i, argc, argv));
    else if (a == "--seed")
      c.seed = as_size(value(i, argc, argv));
    else if (a == "--update-ratio")
      c.update_ratio = std::stod(value(i, argc, argv));
    else if (a == "--verbose")
      c.verbose = true;
    else
      throw std::invalid_argument("unknown option: " + a);
  }
  if (c.help) {
    print_usage(mode);
    return c;
  }
  if (mode == "build") {
    if (!c.generate && c.input.empty())
      throw std::invalid_argument("build needs --input or --generate");
    if (c.generate && c.dim == 0)
      throw std::invalid_argument("--generate needs --dim");
  } else if (c.index.empty() && !(mode == "client" && c.transport == TransportKind::Rdma))
    throw std::invalid_argument("missing --index");
  if (c.M < 2 || c.k == 0 || c.ef_search < c.k || c.ib_port == 0 ||
      c.ib_port > 255)
    throw std::invalid_argument("invalid M/k/ef-search/ib-port");
  if (c.workload != "query" && c.workload != "mixed")
    throw std::invalid_argument("workload must be query or mixed");
  if (c.update_op != "insert" && c.update_op != "upsert" &&
      c.update_op != "erase" && c.update_op != "delete")
    throw std::invalid_argument("update-op must be insert, upsert, or erase");
  if (c.update_ratio < 0.0 || c.update_ratio > 1.0)
    throw std::invalid_argument("update-ratio must be between 0 and 1");
  return c;
}

void print_usage(const std::string &mode) {
  std::cout
      << "rdma_hnsw_lab " << mode << " options:\n"
      << "  --input FILE / --generate N --dim D  input vectors\n"
      << "  --output FILE or --index FILE         index path\n"
      << "  --transport mmap|rdma                 transport backend\n"
      << "  --read-strategy fixed       built-in strategy; add custom "
         "strategies through the API\n"
      << "  --M N --ef-construction N --ef-search N --k N\n"
      << "  --queries FILE --updates FILE\n"
      << "  --base FILE                             vectors for exact recall\n"
      << "  --host HOST --bind HOST --port N       RDMA control endpoint\n"
      << "  --ib-device NAME --ib-port N           RDMA device and port (e.g. "
         "mlx5_0:1)\n";
}
} // namespace rdma_hnsw
