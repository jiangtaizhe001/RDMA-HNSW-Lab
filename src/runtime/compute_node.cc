#include "rdma_hnsw/runtime/compute_node.h"
#include "rdma_hnsw/transport/rdma_transport.h"
#include "rdma_hnsw/transport/rdma_cm_transport.h"
#include "rdma_hnsw/transport/transport.h"
#include <cstring>
#include <limits>
#include <span>
#include <stdexcept>
#include <utility>

namespace rdma_hnsw {

namespace {
IndexCatalog read_remote_catalog(RemoteTransport &transport) {
  IndexCatalog catalog;
  IndexHeader header{};
  auto header_bytes =
      std::span<byte>(reinterpret_cast<byte *>(&header), sizeof(header));
  const auto hr = transport.read({0, 0}, header_bytes);
  if (!hr.ok || hr.bytes != sizeof(header))
    throw std::runtime_error("cannot read remote index header");
  if (std::memcmp(header.magic, "RHNSW001", 8) != 0 || header.version != 1 ||
      header.dim == 0 || header.M == 0 ||
      header.offsets_begin < sizeof(IndexHeader))
    throw std::runtime_error("remote index returned an invalid header");
  catalog.header = header;
  catalog.offsets.resize(header.node_count);
  auto bytes = std::span<byte>(reinterpret_cast<byte *>(catalog.offsets.data()),
                               catalog.offsets.size() * sizeof(Offset));
  const auto orr = transport.read({header.offsets_begin, 0}, bytes);
  if (!orr.ok || orr.bytes != bytes.size())
    throw std::runtime_error("cannot read remote index offset table");
  return catalog;
}
} // namespace

ComputeNode::ComputeNode(ComputeNodeConfig config)
    : config_(std::move(config)) {
  open();
}

void ComputeNode::open() {
  if (config_.transport == TransportKind::Rdma) {
    transport_ = config_.rdma_connection == RdmaConnection::Cm
        ? make_rdma_cm_client(config_.host, config_.port, config_.ib_device,
                              config_.ib_port, config_.rdma_timeout_ms)
        : make_rdma_client(config_.host, config_.port, config_.ib_device, config_.ib_port);
    catalog_ = read_remote_catalog(*transport_);
  } else {
    if (config_.index_path.empty())
      throw std::invalid_argument("mmap compute node needs index_path");
    catalog_ = GraphStore::load_catalog(config_.index_path);
    transport_ = make_mmap_transport(config_.index_path, false);
    local_storage_ = std::make_unique<StorageEngine>(config_.index_path);
  }
  engine_ = std::make_unique<QueryEngine>(
      catalog_, *transport_, config_.read_policy, config_.ef_search, config_.k,
      config_.prefix_granularity, config_.cache_capacity, config_.distance);
}

void ComputeNode::reload() {
  // Reopen the file and reset the query-side generation cache after a local
  // update or an externally maintained storage index changes its layout.
  transport_.reset();
  engine_.reset();
  local_storage_.reset();
  open();
}

namespace {
void refresh_remote_catalog(IndexCatalog &catalog, QueryEngine &engine,
                            RemoteTransport &transport) {
  IndexHeader header{};
  auto header_bytes =
      std::span<byte>(reinterpret_cast<byte *>(&header), sizeof(header));
  const auto header_result = transport.read({0, 0}, header_bytes);
  if (!header_result.ok || header_result.bytes != sizeof(header))
    throw std::runtime_error("cannot read remote index header after update");
  if (std::memcmp(header.magic, "RHNSW001", 8) != 0 || header.version != 1 ||
      header.dim == 0 || header.M == 0 ||
      header.node_count > std::numeric_limits<NodeId>::max() ||
      header.offsets_begin < sizeof(IndexHeader))
    throw std::runtime_error("remote index returned an invalid header");

  std::vector<Offset> offsets(header.node_count);
  auto offset_bytes = std::span<byte>(reinterpret_cast<byte *>(offsets.data()),
                                      offsets.size() * sizeof(Offset));
  const auto offset_result =
      transport.read({header.offsets_begin, 0}, offset_bytes);
  if (!offset_result.ok || offset_result.bytes != offset_bytes.size())
    throw std::runtime_error(
        "cannot read remote index offset table after update");
  catalog.header = header;
  catalog.offsets = std::move(offsets);
  engine.reset_catalog(catalog);
}
} // namespace

std::vector<NodeId> ComputeNode::query(const Vector &vector) {
  return engine_->search(vector);
}

QueryStats ComputeNode::query_batch(const std::vector<Vector> &queries,
                                    const std::vector<Vector> *ground_truth) {
  return engine_->run(queries, ground_truth);
}

UpdateResult ComputeNode::update(const UpdateRequest &request) {
  if (config_.transport == TransportKind::Mmap) {
    const auto result = local_storage_->apply(request);
    if (result.ok)
      reload();
    return result;
  }
  const auto result = transport_->submit_update(request);
  // A CM update publishes the latest global snapshot to this session. Another
  // client may have inserted nodes since our last snapshot, so even an upsert
  // or erase can change this client's offset table. Keep catalog and MR paired.
  if (result.ok && (request.op == update_op::Insert ||
                    config_.rdma_connection == RdmaConnection::Cm))
    refresh_remote_catalog(catalog_, *engine_, *transport_);
  return result;
}

} // namespace rdma_hnsw
