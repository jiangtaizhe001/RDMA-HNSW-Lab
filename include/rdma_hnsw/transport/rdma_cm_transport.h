#pragma once
#include "rdma_hnsw/transport/transport.h"

namespace rdma_hnsw {
// CM selects the device through IP routing. A nonempty device is an assertion
// against that selection, not a substitute for configuring the eRDMA route.
std::unique_ptr<RemoteTransport>
make_rdma_cm_client(const std::string &host, std::size_t port,
                    const std::string &device = {}, std::size_t device_port = 1,
                    std::size_t timeout_ms = 10000);
int run_rdma_cm_server(const std::string &bind, std::size_t port,
                       const std::string &index_path,
                       const std::string &device = {},
                       std::size_t device_port = 1,
                       std::size_t timeout_ms = 10000);
} // namespace rdma_hnsw
