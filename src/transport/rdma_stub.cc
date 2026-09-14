#include "rdma_hnsw/transport/rdma_transport.h"
#include <stdexcept>
namespace rdma_hnsw {
std::unique_ptr<RemoteTransport> make_rdma_client(const std::string &,
                                                  std::size_t,
                                                  const std::string &,
                                                  std::size_t) {
  throw std::runtime_error("RDMA transport unavailable; use --transport mmap");
}
int run_rdma_server(const std::string &, std::size_t, const std::string &,
                    const std::string &, std::size_t) {
  throw std::runtime_error("RDMA transport unavailable; use --transport mmap");
}
} // namespace rdma_hnsw
