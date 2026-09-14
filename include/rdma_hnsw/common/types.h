#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace rdma_hnsw {
using byte = std::byte;
using NodeId = std::uint32_t;
using Generation = std::uint64_t;
using Offset = std::uint64_t;
using Vector = std::vector<float>;
using Bytes = std::vector<byte>;

struct RemoteAddress {
  Offset offset{};
  std::uint32_t key{};
};

enum class TransportKind { Mmap, Rdma };
enum class RdmaConnection { Ib, Cm };
enum class DistanceKind { L2, InnerProduct };
enum class ReadPolicy { Fixed, HeaderActual, CachedLength, Prefix };

inline std::string to_string(ReadPolicy p) {
  switch (p) {
  case ReadPolicy::Fixed:
    return "fixed";
  case ReadPolicy::HeaderActual:
    return "header_actual";
  case ReadPolicy::CachedLength:
    return "cached_length";
  case ReadPolicy::Prefix:
    return "prefix";
  }
  return "fixed";
}
inline ReadPolicy read_policy_from_string(const std::string &s) {
  if (s == "header_actual" || s == "header")
    return ReadPolicy::HeaderActual;
  if (s == "cached_length" || s == "cached")
    return ReadPolicy::CachedLength;
  if (s == "prefix")
    return ReadPolicy::Prefix;
  return ReadPolicy::Fixed;
}
inline TransportKind transport_from_string(const std::string &s) {
  return s == "rdma" ? TransportKind::Rdma : TransportKind::Mmap;
}
} // namespace rdma_hnsw
