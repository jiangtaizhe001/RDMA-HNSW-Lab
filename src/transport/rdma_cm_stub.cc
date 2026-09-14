#include "rdma_hnsw/transport/rdma_cm_transport.h"
#include <stdexcept>
namespace rdma_hnsw {
std::unique_ptr<RemoteTransport> make_rdma_cm_client(const std::string &,
                                                     std::size_t,
                                                     const std::string &,
                                                     std::size_t, std::size_t) {
  throw std::runtime_error(
      "RDMA CM backend unavailable: rebuild with ENABLE_RDMA=ON, "
      "ENABLE_RDMA_CM=ON and librdmacm development files");
}
int run_rdma_cm_server(const std::string &, std::size_t, const std::string &,
                       const std::string &, std::size_t, std::size_t) {
  throw std::runtime_error(
      "RDMA CM backend unavailable: rebuild with ENABLE_RDMA=ON, "
      "ENABLE_RDMA_CM=ON and librdmacm development files");
}
} // namespace rdma_hnsw
