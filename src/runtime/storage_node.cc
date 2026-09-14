#include "rdma_hnsw/runtime/storage_node.h"
#include "rdma_hnsw/transport/rdma_transport.h"
#include "rdma_hnsw/transport/rdma_cm_transport.h"
#include <utility>

namespace rdma_hnsw {

StorageNode::StorageNode(StorageNodeConfig config)
    : config_(std::move(config)), engine_(config_.index_path) {}

int StorageNode::serve() {
  if (config_.transport == TransportKind::Mmap)
    return 0;
  if (config_.rdma_connection == RdmaConnection::Cm)
    return run_rdma_cm_server(config_.bind, config_.port, config_.index_path,
                               config_.ib_device, config_.ib_port, config_.rdma_timeout_ms);
  return run_rdma_server(config_.bind, config_.port, config_.index_path,
                         config_.ib_device, config_.ib_port);
}

UpdateResult StorageNode::apply(const UpdateRequest &request) {
  return engine_.apply(request);
}

} // namespace rdma_hnsw
