#pragma once
#include "rdma_hnsw/transport/transport.h"
#include <memory>

namespace rdma_hnsw {
std::unique_ptr<RemoteTransport>
make_rdma_client(const std::string &host, std::size_t port,
                 const std::string &ib_device = {}, std::size_t ib_port = 1);
int run_rdma_server(const std::string &bind, std::size_t port,
                    const std::string &index_path,
                    const std::string &ib_device = {}, std::size_t ib_port = 1);
} // namespace rdma_hnsw
