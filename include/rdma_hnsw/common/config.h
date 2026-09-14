#pragma once

#include "rdma_hnsw/common/types.h"
#include <cstddef>
#include <string>

namespace rdma_hnsw {

struct Config {
  std::string input;
  std::string output = "index.bin";
  std::string index = "index.bin";
  std::string queries;
  std::string base;
  std::string updates;
  std::string host = "127.0.0.1";
  std::string bind = "0.0.0.0";
  std::string ib_device;
  std::string workload = "query";
  std::string update_op = "upsert";
  TransportKind transport = TransportKind::Mmap;
  DistanceKind distance = DistanceKind::L2;
  ReadPolicy policy = ReadPolicy::Fixed;
  std::size_t generated = 0;
  std::size_t dim = 0;
  std::size_t max_elements = 0;
  std::size_t M = 16;
  std::size_t ef_construction = 100;
  std::size_t ef_search = 64;
  std::size_t k = 10;
  std::size_t query_count = 1000;
  std::size_t update_count = 0;
  std::size_t threads = 1;
  std::size_t cache_capacity = 100000;
  std::size_t prefix_granularity = 4;
  std::size_t port = 18515;
  std::size_t ib_port = 1;
  std::size_t seed = 42;
  double update_ratio = 0.1;
  bool generate = false;
  bool help = false;
  bool verbose = false;
  RdmaConnection rdma_connection = RdmaConnection::Ib;
  std::size_t rdma_timeout_ms = 10000;
};

Config parse_config(int argc, char **argv, const std::string &mode);
  void print_usage(const std::string &mode);
} // namespace rdma_hnsw
