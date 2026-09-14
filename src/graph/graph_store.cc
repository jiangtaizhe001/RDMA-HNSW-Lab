#include "rdma_hnsw/graph/graph_store.h"
#include <algorithm>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <unistd.h>
#include <utility>

namespace rdma_hnsw {
namespace {
constexpr char kMagic[8] = {'R', 'H', 'N', 'S', 'W', '0', '0', '1'};
struct DiskNodeHeader {
  NodeId id;
  Generation generation;
  std::uint32_t level;
  std::uint8_t deleted;
  std::uint8_t pad[3];
};
template <typename T> void write_pod(std::ostream &out, const T &x) {
  out.write(reinterpret_cast<const char *>(&x), sizeof(x));
}
template <typename T> T read_pod(const std::vector<byte> &b, std::size_t &p) {
  if (p + sizeof(T) > b.size())
    throw std::runtime_error("corrupt index record");
  T x{};
  std::memcpy(&x, b.data() + p, sizeof(T));
  p += sizeof(T);
  return x;
}
std::vector<byte> read_file(const std::string &path) {
  std::ifstream in(path, std::ios::binary | std::ios::ate);
  if (!in)
    throw std::runtime_error("cannot open index: " + path);
  const auto n = static_cast<std::size_t>(in.tellg());
  in.seekg(0);
  std::vector<byte> b(n);
  in.read(reinterpret_cast<char *>(b.data()), static_cast<std::streamsize>(n));
  return b;
}
} // namespace

std::size_t serialized_node_size(const NodeRecord &node, std::uint32_t dim,
                                 std::uint32_t M, std::uint32_t max_level) {
  std::size_t n = sizeof(DiskNodeHeader) + sizeof(float) * dim;
  for (std::uint32_t l = 0; l <= max_level; ++l)
    n += sizeof(std::uint32_t) + sizeof(NodeId) * (l == 0 ? M * 2 : M);
  (void)node;
  return n;
}

IndexCatalog GraphStore::load_catalog(const std::string &path) {
  std::ifstream in(path, std::ios::binary);
  if (!in)
    throw std::runtime_error("cannot open index: " + path);
  IndexCatalog c;
  c.path = path;
  in.read(reinterpret_cast<char *>(&c.header), sizeof(c.header));
  if (!in || std::memcmp(c.header.magic, kMagic, sizeof(kMagic)) != 0 ||
      c.header.version != 1)
    throw std::runtime_error("invalid RHNSW index header");
  if (c.header.dim == 0 || c.header.M == 0 ||
      c.header.node_count > std::numeric_limits<NodeId>::max() ||
      c.header.offsets_begin < sizeof(IndexHeader))
    throw std::runtime_error("invalid RHNSW index dimensions or offset table");
  c.offsets.resize(c.header.node_count);
  in.seekg(static_cast<std::streamoff>(c.header.offsets_begin));
  in.read(reinterpret_cast<char *>(c.offsets.data()),
          static_cast<std::streamsize>(c.offsets.size() * sizeof(Offset)));
  if (!in)
    throw std::runtime_error("truncated index offset table");
  return c;
}

std::vector<NodeRecord> GraphStore::read_all(const IndexCatalog &c) {
  const auto bytes = read_file(c.path);
  std::vector<NodeRecord> nodes;
  nodes.reserve(c.header.node_count);
  for (NodeId id = 0; id < c.header.node_count; ++id) {
    std::size_t p = static_cast<std::size_t>(c.offsets[id]);
    NodeRecord n;
    const auto dh = read_pod<DiskNodeHeader>(bytes, p);
    n.id = dh.id;
    n.generation = dh.generation;
    n.level = dh.level;
    n.deleted = dh.deleted != 0;
    n.vector.resize(c.header.dim);
    std::memcpy(n.vector.data(), bytes.data() + p,
                n.vector.size() * sizeof(float));
    p += n.vector.size() * sizeof(float);
    n.neighbors.resize(c.header.max_level + 1);
    for (std::uint32_t l = 0; l <= c.header.max_level; ++l) {
      const auto degree = read_pod<std::uint32_t>(bytes, p);
      const auto cap = c.layer_capacity(l);
      n.neighbors[l].resize(std::min<std::size_t>(degree, cap));
      for (std::size_t j = 0; j < cap; ++j) {
        const auto v = read_pod<NodeId>(bytes, p);
        if (j < n.neighbors[l].size())
          n.neighbors[l][j] = v;
      }
    }
    nodes.push_back(std::move(n));
  }
  return nodes;
}

void GraphStore::write(const std::string &path, IndexHeader header,
                       const std::vector<NodeRecord> &nodes) {
  if (header.dim == 0 || header.M == 0)
    throw std::invalid_argument("index dimension and M must be positive");
  if (nodes.size() > std::numeric_limits<NodeId>::max())
    throw std::invalid_argument("index contains too many nodes");
  for (std::size_t i = 0; i < nodes.size(); ++i) {
    if (nodes[i].id != i || nodes[i].vector.size() != header.dim ||
        nodes[i].level > header.max_level)
      throw std::invalid_argument("node records do not match index layout");
  }
  std::memcpy(header.magic, kMagic, sizeof(kMagic));
  header.node_count = nodes.size();
  header.offsets_begin = sizeof(IndexHeader);
  header.records_begin = header.offsets_begin + nodes.size() * sizeof(Offset);
  std::vector<Offset> offsets(nodes.size());
  Offset cursor = header.records_begin;
  for (std::size_t i = 0; i < nodes.size(); ++i) {
    offsets[i] = cursor;
    cursor +=
        serialized_node_size(nodes[i], header.dim, header.M, header.max_level);
  }
  const auto parent = std::filesystem::path(path).parent_path();
  if (!parent.empty())
    std::filesystem::create_directories(parent);
  static std::atomic<unsigned> sequence{0};
  const auto temp = path + ".tmp." +
                    std::to_string(static_cast<long long>(::getpid())) + "." +
                    std::to_string(sequence.fetch_add(1));
  std::ofstream out(temp, std::ios::binary | std::ios::trunc);
  if (!out)
    throw std::runtime_error("cannot write index: " + temp);
  write_pod(out, header);
  out.write(reinterpret_cast<const char *>(offsets.data()),
            static_cast<std::streamsize>(offsets.size() * sizeof(Offset)));
  for (const auto &node : nodes) {
    DiskNodeHeader dh{node.id,
                      node.generation,
                      node.level,
                      static_cast<std::uint8_t>(node.deleted),
                      {0, 0, 0}};
    write_pod(out, dh);
    out.write(reinterpret_cast<const char *>(node.vector.data()),
              static_cast<std::streamsize>(node.vector.size() * sizeof(float)));
    for (std::uint32_t l = 0; l <= header.max_level; ++l) {
      const auto cap = l == 0 ? header.M * 2 : header.M;
      const auto degree = static_cast<std::uint32_t>(std::min<std::size_t>(
          node.neighbors.size() > l ? node.neighbors[l].size() : 0, cap));
      write_pod(out, degree);
      for (std::size_t j = 0; j < cap; ++j) {
        const NodeId v = j < degree ? node.neighbors[l][j] : 0;
        write_pod(out, v);
      }
    }
  }
  if (!out)
    throw std::runtime_error("failed writing index");
  out.close();
  if (std::rename(temp.c_str(), path.c_str()) != 0) {
    std::remove(temp.c_str());
    throw std::runtime_error("cannot atomically publish index: " + path);
  }
}

GraphStore::GraphStore(IndexCatalog catalog)
    : catalog_(std::move(catalog)), nodes_(read_all(catalog_)) {}
NodeRecord GraphStore::read_node(NodeId id) const {
  std::lock_guard lock(mutex_);
  if (id >= nodes_.size())
    throw std::out_of_range("node id");
  return nodes_[id];
}
NodeRecord GraphStore::read_node_at(Offset offset) const {
  for (NodeId id = 0; id < catalog_.offsets.size(); ++id)
    if (catalog_.offsets[id] == offset)
      return read_node(id);
  throw std::out_of_range("node offset");
}
std::vector<byte> GraphStore::read_bytes(Offset offset,
                                         std::size_t length) const {
  std::ifstream in(catalog_.path, std::ios::binary);
  if (!in)
    throw std::runtime_error("cannot open index");
  in.seekg(static_cast<std::streamoff>(offset));
  std::vector<byte> b(length);
  in.read(reinterpret_cast<char *>(b.data()),
          static_cast<std::streamsize>(length));
  b.resize(static_cast<std::size_t>(in.gcount()));
  return b;
}
void GraphStore::replace_nodes(std::vector<NodeRecord> nodes,
                               NodeId entrypoint) {
  std::lock_guard lock(mutex_);
  nodes_ = std::move(nodes);
  catalog_.header.entrypoint = entrypoint;
  ++catalog_.header.generation;
  write(catalog_.path, catalog_.header, nodes_);
  catalog_ = load_catalog(catalog_.path);
}
void GraphStore::update_node(NodeRecord node) {
  std::lock_guard lock(mutex_);
  if (node.id >= nodes_.size())
    throw std::out_of_range("node id");
  nodes_[node.id] = std::move(node);
  ++catalog_.header.generation;
  write(catalog_.path, catalog_.header, nodes_);
  catalog_ = load_catalog(catalog_.path);
}
std::vector<NodeRecord> GraphStore::snapshot() const {
  std::lock_guard lock(mutex_);
  return nodes_;
}
} // namespace rdma_hnsw
