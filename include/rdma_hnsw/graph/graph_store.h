#pragma once

#include "rdma_hnsw/common/types.h"
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace rdma_hnsw {

struct IndexHeader {
  char magic[8]{};
  std::uint32_t version{1};
  std::uint32_t dim{};
  std::uint32_t M{};
  std::uint32_t max_level{};
  NodeId entrypoint{};
  std::uint64_t node_count{};
  Generation generation{1};
  std::uint64_t offsets_begin{};
  std::uint64_t records_begin{};
};

struct NodeRecord {
  NodeId id{};
  Generation generation{1};
  std::uint32_t level{};
  bool deleted{false};
  Vector vector;
  std::vector<std::vector<NodeId>> neighbors;
};

struct NodeDiskHeader {
  NodeId id;
  Generation generation;
  std::uint32_t level;
  std::uint8_t deleted;
  std::uint8_t pad[3];
};
constexpr std::size_t kNodeDiskHeaderBytes = sizeof(NodeDiskHeader);

struct IndexCatalog {
  IndexHeader header{};
  std::vector<Offset> offsets;
  std::string path;
  std::size_t layer_capacity(std::uint32_t level) const {
    return level == 0 ? header.M * 2 : header.M;
  }
};

class GraphStore {
public:
  explicit GraphStore(IndexCatalog catalog);
  static IndexCatalog load_catalog(const std::string &path);
  static void write(const std::string &path, IndexHeader header,
                    const std::vector<NodeRecord> &nodes);
  static std::vector<NodeRecord> read_all(const IndexCatalog &catalog);

  const IndexCatalog &catalog() const { return catalog_; }
  NodeRecord read_node(NodeId id) const;
  NodeRecord read_node_at(Offset offset) const;
  std::vector<byte> read_bytes(Offset offset, std::size_t length) const;
  void replace_nodes(std::vector<NodeRecord> nodes, NodeId entrypoint);
  void update_node(NodeRecord node);
  std::vector<NodeRecord> snapshot() const;

private:
  IndexCatalog catalog_;
  mutable std::mutex mutex_;
  std::vector<NodeRecord> nodes_;
};

std::size_t serialized_node_size(const NodeRecord &node, std::uint32_t dim,
                                 std::uint32_t M, std::uint32_t max_level);
inline Offset node_layer_offset(const IndexCatalog &c, NodeId id,
                                std::uint32_t level) {
  Offset p = c.offsets.at(id) + sizeof(NodeDiskHeader) +
             static_cast<Offset>(c.header.dim) * sizeof(float);
  for (std::uint32_t l = 0; l < level; ++l)
    p += sizeof(std::uint32_t) +
         static_cast<Offset>(c.layer_capacity(l)) * sizeof(NodeId);
  return p;
}
inline Offset node_vector_offset(const IndexCatalog &c, NodeId id) {
  return c.offsets.at(id) + sizeof(NodeDiskHeader);
}
} // namespace rdma_hnsw
