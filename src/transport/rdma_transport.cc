#include "rdma_hnsw/transport/rdma_transport.h"
#include "rdma_hnsw/runtime/storage_engine.h"

#ifdef RDMA_HNSW_HAS_IBVERBS

#include <arpa/inet.h>
#include <infiniband/verbs.h>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <cerrno>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <utility>
#include <vector>

namespace rdma_hnsw {
namespace {

constexpr std::size_t kStagingBytes = 1U << 20;
constexpr std::size_t kServerGrowthBytes = 64U << 20;
constexpr std::uint32_t kUpdateMagic = 0x52485550; // RHUP

// The TCP bootstrap is deliberately small and versionless for this teaching
// prototype. Both endpoints run the same binary and therefore share layout.
struct WireInfo {
  std::uint32_t qp_num{};
  std::uint32_t psn{};
  std::uint32_t rkey{};
  std::uint32_t size{};
  std::uint16_t lid{};
  std::uint16_t port{};
  std::uint64_t addr{};
};

struct UpdateWire {
  std::uint32_t magic{};
  std::uint32_t op{};
  std::uint32_t id{};
  std::uint32_t dim{};
  std::uint64_t mailbox_offset{};
};

struct UpdateReply {
  std::uint32_t magic{};
  std::uint32_t ok{};
  std::uint32_t error_size{};
  std::uint64_t generation{};
};

void control_io(int fd, void *data, std::size_t bytes, bool sending) {
  auto *cursor = static_cast<char *>(data);
  while (bytes != 0) {
    const auto count = sending ? ::send(fd, cursor, bytes, MSG_NOSIGNAL)
                               : ::recv(fd, cursor, bytes, MSG_WAITALL);
    if (count < 0 && errno == EINTR)
      continue;
    if (count == 0)
      throw std::runtime_error("RDMA control socket closed by peer");
    if (count < 0)
      throw std::runtime_error("RDMA control socket closed: " +
                               std::string(std::strerror(errno)));
    cursor += count;
    bytes -= static_cast<std::size_t>(count);
  }
}

int connect_socket(const std::string &host, std::size_t port) {
  addrinfo hints{};
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo *result = nullptr;
  const auto service = std::to_string(port);
  if (::getaddrinfo(host.c_str(), service.c_str(), &hints, &result) != 0)
    throw std::runtime_error("cannot resolve RDMA host " + host);

  int fd = -1;
  for (auto *entry = result; entry != nullptr; entry = entry->ai_next) {
    fd = ::socket(entry->ai_family, entry->ai_socktype, entry->ai_protocol);
    if (fd >= 0 && ::connect(fd, entry->ai_addr, entry->ai_addrlen) == 0)
      break;
    if (fd >= 0) {
      ::close(fd);
      fd = -1;
    }
  }
  ::freeaddrinfo(result);
  if (fd < 0)
    throw std::runtime_error("cannot connect RDMA control socket to " + host +
                             ":" + std::to_string(port));
  return fd;
}

int listen_socket(const std::string &bind_address, std::size_t port) {
  const int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0)
    throw std::runtime_error("cannot create RDMA control socket");

  int reuse = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &reuse, sizeof(reuse));
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(static_cast<std::uint16_t>(port));
  address.sin_addr.s_addr = bind_address == "0.0.0.0"
                                ? INADDR_ANY
                                : ::inet_addr(bind_address.c_str());
  if (::bind(fd, reinterpret_cast<sockaddr *>(&address), sizeof(address)) < 0 ||
      ::listen(fd, 32) < 0) {
    ::close(fd);
    throw std::runtime_error("cannot listen on RDMA control port " +
                             std::to_string(port));
  }
  return fd;
}

class VerbsResources {
public:
  VerbsResources(std::size_t bytes, int access, const std::string &device_name,
                 std::size_t port)
      : buffer(bytes), port_(static_cast<std::uint16_t>(port)) {
    if (port == 0 || port > std::numeric_limits<std::uint16_t>::max())
      throw std::invalid_argument("RDMA port is out of range");

    int device_count = 0;
    auto **devices = ibv_get_device_list(&device_count);
    if (devices == nullptr || device_count == 0) {
      throw std::runtime_error(
          "ibv_get_device_list returned no device/provider; check libibverbs "
          "providers and /dev/infiniband (requested device=" +
          (device_name.empty() ? std::string("<auto>") : device_name) + ")");
    }

    for (int i = 0; i < device_count; ++i) {
      if (!device_name.empty() &&
          device_name != ibv_get_device_name(devices[i]))
        continue;
      context_ = ibv_open_device(devices[i]);
      if (context_ != nullptr)
        break;
    }
    ibv_free_device_list(devices);
    if (context_ == nullptr) {
      throw std::runtime_error(
          "cannot open requested RDMA device " +
          (device_name.empty() ? std::string("<auto>") : device_name));
    }

    protection_domain_ = ibv_alloc_pd(context_);
    completion_queue_ = ibv_create_cq(context_, 32, nullptr, nullptr, 0);
    if (protection_domain_ == nullptr || completion_queue_ == nullptr)
      throw std::runtime_error("cannot allocate ibverbs PD/CQ");

    ibv_qp_init_attr qp_attributes{};
    qp_attributes.send_cq = completion_queue_;
    qp_attributes.recv_cq = completion_queue_;
    qp_attributes.qp_type = IBV_QPT_RC;
    qp_attributes.cap.max_send_wr = 32;
    qp_attributes.cap.max_recv_wr = 1;
    qp_attributes.cap.max_send_sge = 1;
    qp_attributes.cap.max_recv_sge = 1;
    queue_pair_ = ibv_create_qp(protection_domain_, &qp_attributes);
    if (queue_pair_ == nullptr)
      throw std::runtime_error("cannot create ibverbs queue pair");

    ibv_port_attr port_attributes{};
    if (ibv_query_port(context_, port_, &port_attributes) != 0)
      throw std::runtime_error("ibv_query_port failed for port " +
                               std::to_string(port_));
    if (port_attributes.state != IBV_PORT_ACTIVE)
      throw std::runtime_error("RDMA port " + std::to_string(port_) +
                               " is not active");
    if (port_attributes.lid == 0)
      throw std::runtime_error(
          "selected RDMA port has no InfiniBand LID (RoCE/GRH is not "
          "implemented by this prototype)");

    lid_ = port_attributes.lid;
    psn_ = static_cast<std::uint32_t>(
               (::getpid() ^ reinterpret_cast<std::uintptr_t>(this))) &
           0x00ffffffU;
    memory_region_ =
        ibv_reg_mr(protection_domain_, buffer.data(), buffer.size(), access);
    if (memory_region_ == nullptr)
      throw std::runtime_error("ibv_reg_mr failed");
  }

  ~VerbsResources() {
    if (memory_region_ != nullptr)
      ibv_dereg_mr(memory_region_);
    if (queue_pair_ != nullptr)
      ibv_destroy_qp(queue_pair_);
    if (completion_queue_ != nullptr)
      ibv_destroy_cq(completion_queue_);
    if (protection_domain_ != nullptr)
      ibv_dealloc_pd(protection_domain_);
    if (context_ != nullptr)
      ibv_close_device(context_);
  }

  VerbsResources(const VerbsResources &) = delete;
  VerbsResources &operator=(const VerbsResources &) = delete;

  ibv_context *context() const { return context_; }
  ibv_qp *qp() const { return queue_pair_; }
  ibv_mr *mr() const { return memory_region_; }
  ibv_cq *cq() const { return completion_queue_; }
  std::uint16_t lid() const { return lid_; }
  std::uint16_t port() const { return port_; }
  std::uint32_t psn() const { return psn_; }
  std::vector<byte> buffer;

private:
  ibv_context *context_{};
  ibv_pd *protection_domain_{};
  ibv_cq *completion_queue_{};
  ibv_qp *queue_pair_{};
  ibv_mr *memory_region_{};
  std::uint16_t lid_{};
  std::uint16_t port_{};
  std::uint32_t psn_{};
};

void transition_to_init(VerbsResources &resources) {
  ibv_qp_attr attributes{};
  attributes.qp_state = IBV_QPS_INIT;
  attributes.pkey_index = 0;
  attributes.port_num = resources.port();
  attributes.qp_access_flags = IBV_ACCESS_REMOTE_READ | IBV_ACCESS_REMOTE_WRITE;
  const auto mask =
      IBV_QP_STATE | IBV_QP_PKEY_INDEX | IBV_QP_PORT | IBV_QP_ACCESS_FLAGS;
  if (ibv_modify_qp(resources.qp(), &attributes, mask) != 0)
    throw std::runtime_error("QP transition to INIT failed");
}

void transition_to_rtr_rts(VerbsResources &resources, const WireInfo &remote) {
  ibv_qp_attr attributes{};
  attributes.qp_state = IBV_QPS_RTR;
  attributes.path_mtu = IBV_MTU_1024;
  attributes.dest_qp_num = remote.qp_num;
  attributes.rq_psn = remote.psn;
  attributes.max_dest_rd_atomic = 1;
  attributes.min_rnr_timer = 12;
  attributes.ah_attr.is_global = 0;
  attributes.ah_attr.dlid = remote.lid;
  attributes.ah_attr.sl = 0;
  attributes.ah_attr.src_path_bits = 0;
  attributes.ah_attr.port_num = resources.port();
  const auto rtr_mask = IBV_QP_STATE | IBV_QP_AV | IBV_QP_PATH_MTU |
                        IBV_QP_DEST_QPN | IBV_QP_RQ_PSN |
                        IBV_QP_MAX_DEST_RD_ATOMIC | IBV_QP_MIN_RNR_TIMER;
  if (ibv_modify_qp(resources.qp(), &attributes, rtr_mask) != 0)
    throw std::runtime_error("QP transition to RTR failed");

  attributes = {};
  attributes.qp_state = IBV_QPS_RTS;
  attributes.sq_psn = resources.psn();
  attributes.timeout = 14;
  attributes.retry_cnt = 7;
  attributes.rnr_retry = 7;
  attributes.max_rd_atomic = 1;
  const auto rts_mask = IBV_QP_STATE | IBV_QP_SQ_PSN | IBV_QP_TIMEOUT |
                        IBV_QP_RETRY_CNT | IBV_QP_RNR_RETRY |
                        IBV_QP_MAX_QP_RD_ATOMIC;
  if (ibv_modify_qp(resources.qp(), &attributes, rts_mask) != 0)
    throw std::runtime_error("QP transition to RTS failed");
}

void wait_for_completion(VerbsResources &resources) {
  ibv_wc completion{};
  int count = 0;
  while ((count = ibv_poll_cq(resources.cq(), 1, &completion)) == 0)
    std::this_thread::yield();
  if (count < 0)
    throw std::runtime_error("ibv_poll_cq failed");
  if (completion.status != IBV_WC_SUCCESS)
    throw std::runtime_error("RDMA work completion failed: " +
                             std::string(ibv_wc_status_str(completion.status)));
}

class RdmaClient final : public RemoteTransport {
public:
  RdmaClient(const std::string &host, std::size_t tcp_port,
             const std::string &device_name, std::size_t ib_port)
      : resources_(kStagingBytes, IBV_ACCESS_LOCAL_WRITE, device_name, ib_port),
        socket_(connect_socket(host, tcp_port)) {
    transition_to_init(resources_);
    const WireInfo local{
        resources_.qp()->qp_num,
        resources_.psn(),
        resources_.mr()->rkey,
        static_cast<std::uint32_t>(resources_.buffer.size()),
        resources_.lid(),
        resources_.port(),
        reinterpret_cast<std::uint64_t>(resources_.buffer.data())};
    control_io(socket_, const_cast<WireInfo *>(&local), sizeof(local), true);
    control_io(socket_, &remote_, sizeof(remote_), false);
    transition_to_rtr_rts(resources_, remote_);
  }

  ~RdmaClient() override {
    if (socket_ >= 0)
      ::close(socket_);
  }

  ReadResult read(RemoteAddress address, std::span<byte> destination) override {
    if (destination.empty())
      return {true, 0, {}};
    if (destination.size() > resources_.buffer.size())
      return {false, 0, "RDMA read exceeds local staging buffer"};
    if (address.offset > remote_.size ||
        destination.size() > remote_.size - address.offset)
      return {false, 0, "RDMA read exceeds remote registered buffer"};

    std::lock_guard lock(mutex_);
    ibv_sge sge{reinterpret_cast<std::uintptr_t>(resources_.buffer.data()),
                static_cast<std::uint32_t>(destination.size()),
                resources_.mr()->lkey};
    ibv_send_wr work_request{}, *bad_request = nullptr;
    work_request.wr_id = 1;
    work_request.sg_list = &sge;
    work_request.num_sge = 1;
    work_request.opcode = IBV_WR_RDMA_READ;
    work_request.send_flags = IBV_SEND_SIGNALED;
    work_request.wr.rdma.remote_addr = remote_.addr + address.offset;
    work_request.wr.rdma.rkey = remote_.rkey;
    if (ibv_post_send(resources_.qp(), &work_request, &bad_request) != 0)
      return {false, 0, "ibv_post_send RDMA READ failed"};
    wait_for_completion(resources_);
    std::memcpy(destination.data(), resources_.buffer.data(),
                destination.size());
    stats_.read_ops.fetch_add(1);
    stats_.read_bytes.fetch_add(destination.size());
    stats_.rtt.fetch_add(1);
    return {true, destination.size(), {}};
  }

  WriteResult write(RemoteAddress address,
                    std::span<const byte> source) override {
    if (source.empty())
      return {true, 0, {}};
    if (source.size() > resources_.buffer.size())
      return {false, 0, "RDMA write exceeds local staging buffer"};
    if (address.offset > remote_.size ||
        source.size() > remote_.size - address.offset)
      return {false, 0, "RDMA write exceeds remote registered buffer"};

    std::lock_guard lock(mutex_);
    std::memcpy(resources_.buffer.data(), source.data(), source.size());
    ibv_sge sge{reinterpret_cast<std::uintptr_t>(resources_.buffer.data()),
                static_cast<std::uint32_t>(source.size()),
                resources_.mr()->lkey};
    ibv_send_wr work_request{}, *bad_request = nullptr;
    work_request.wr_id = 2;
    work_request.sg_list = &sge;
    work_request.num_sge = 1;
    work_request.opcode = IBV_WR_RDMA_WRITE;
    work_request.send_flags = IBV_SEND_SIGNALED;
    work_request.wr.rdma.remote_addr = remote_.addr + address.offset;
    work_request.wr.rdma.rkey = remote_.rkey;
    if (ibv_post_send(resources_.qp(), &work_request, &bad_request) != 0)
      return {false, 0, "ibv_post_send RDMA WRITE failed"};
    wait_for_completion(resources_);
    stats_.write_ops.fetch_add(1);
    stats_.write_bytes.fetch_add(source.size());
    stats_.rtt.fetch_add(1);
    return {true, source.size(), {}};
  }

  UpdateResult submit_update(const UpdateRequest &request) override {
    const auto mailbox =
        remote_.size > kStagingBytes ? remote_.size - kStagingBytes : 0;
    try {
      if (!request.vector.empty()) {
        const auto bytes = std::as_bytes(std::span<const float>(
            request.vector.data(), request.vector.size()));
        const auto result = write({mailbox, 0}, bytes);
        if (!result.ok)
          return {false, 0, result.error};
      }

      std::lock_guard lock(mutex_);
      UpdateWire wire{kUpdateMagic, request.op, request.id,
                      static_cast<std::uint32_t>(request.vector.size()),
                      mailbox};
      control_io(socket_, &wire, sizeof(wire), true);
      UpdateReply reply{};
      control_io(socket_, &reply, sizeof(reply), false);
      if (reply.magic != kUpdateMagic)
        return {false, 0, "invalid RDMA update reply"};
      std::string error(reply.error_size, '\0');
      if (!error.empty())
        control_io(socket_, error.data(), error.size(), false);
      return {reply.ok != 0, reply.generation, std::move(error)};
    } catch (const std::exception &error) {
      return {false, 0, error.what()};
    }
  }

  const TransportStats &stats() const override { return stats_; }

private:
  VerbsResources resources_;
  int socket_{-1};
  WireInfo remote_{};
  mutable std::mutex mutex_;
  TransportStats stats_;
};

} // namespace

std::unique_ptr<RemoteTransport> make_rdma_client(const std::string &host,
                                                  std::size_t port,
                                                  const std::string &ib_device,
                                                  std::size_t ib_port) {
  return std::make_unique<RdmaClient>(host, port, ib_device, ib_port);
}

void serve_rdma_session(int socket, const std::string &index_path,
                        const std::string &ib_device, std::size_t ib_port,
                        StorageEngine &engine) {
  std::ifstream input(index_path, std::ios::binary | std::ios::ate);
  if (!input)
    throw std::runtime_error("cannot open index for RDMA server");
  const auto index_bytes = static_cast<std::size_t>(input.tellg());
  input.seekg(0);
  VerbsResources resources(
      std::max<std::size_t>(index_bytes + kServerGrowthBytes,
                            kStagingBytes + 1),
      IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_READ | IBV_ACCESS_REMOTE_WRITE,
      ib_device, ib_port);
  input.read(reinterpret_cast<char *>(resources.buffer.data()),
             static_cast<std::streamsize>(index_bytes));
  if (!input)
    throw std::runtime_error("cannot read index for RDMA server");
  transition_to_init(resources);
  WireInfo remote{};
  control_io(socket, &remote, sizeof(remote), false);
  const WireInfo local{
      resources.qp()->qp_num,
      resources.psn(),
      resources.mr()->rkey,
      static_cast<std::uint32_t>(resources.buffer.size()),
      resources.lid(),
      resources.port(),
      reinterpret_cast<std::uint64_t>(resources.buffer.data())};
  control_io(socket, const_cast<WireInfo *>(&local), sizeof(local), true);
  transition_to_rtr_rts(resources, remote);

  std::cerr << "RDMA session ready device="
            << (ib_device.empty() ? "<auto>" : ib_device) << ":" << ib_port
            << " index_bytes=" << index_bytes << '\n';

  while (true) {
    UpdateWire wire{};
    try {
      control_io(socket, &wire, sizeof(wire), false);
      if (wire.magic != kUpdateMagic || wire.dim > (1U << 20))
        throw std::runtime_error("invalid RDMA update request");

      UpdateRequest request{wire.op, wire.id, std::vector<float>(wire.dim)};
      if (wire.dim != 0) {
        const auto bytes = static_cast<std::size_t>(wire.dim) * sizeof(float);
        if (wire.mailbox_offset != 0 &&
            wire.mailbox_offset <= resources.buffer.size() &&
            bytes <= resources.buffer.size() - wire.mailbox_offset) {
          std::memcpy(request.vector.data(),
                      resources.buffer.data() + wire.mailbox_offset, bytes);
        } else {
          control_io(socket, request.vector.data(), bytes, false);
        }
      }

      auto result = engine.apply(request);
      if (result.ok) {
        std::ifstream refreshed(index_path, std::ios::binary | std::ios::ate);
        const auto refreshed_size =
            refreshed ? static_cast<std::size_t>(refreshed.tellg()) : 0;
        if (!refreshed || refreshed_size > resources.buffer.size()) {
          result.ok = false;
          result.error =
              "updated index exceeds the RDMA registered storage buffer";
        } else {
          refreshed.seekg(0);
          refreshed.read(reinterpret_cast<char *>(resources.buffer.data()),
                         static_cast<std::streamsize>(refreshed_size));
          if (!refreshed) {
            result.ok = false;
            result.error = "cannot republish updated index";
          }
        }
      }

      const UpdateReply reply{kUpdateMagic, result.ok ? 1U : 0U,
                              static_cast<std::uint32_t>(result.error.size()),
                              result.generation};
      control_io(socket, const_cast<UpdateReply *>(&reply), sizeof(reply),
                 true);
      if (!result.error.empty())
        control_io(socket, result.error.data(), result.error.size(), true);
    } catch (const std::exception &error) {
      std::cerr << "RDMA session stopped: " << error.what() << '\n';
      break;
    }
  }

  // The accept worker owns this descriptor and closes it exactly once, after
  // session resources are destroyed. Closing here as well can close a reused
  // descriptor belonging to a newly accepted client.
}

int run_rdma_server(const std::string &bind, std::size_t port,
                    const std::string &index_path, const std::string &ib_device,
                    std::size_t ib_port) {
  const int listener = listen_socket(bind, port);
  StorageEngine engine(index_path);
  std::cerr << "RDMA server listening on port " << port
            << " device=" << (ib_device.empty() ? "<auto>" : ib_device) << ":"
            << ib_port << '\n';
  while (true) {
    const int socket = ::accept(listener, nullptr, nullptr);
    if (socket < 0) {
      if (errno == EINTR)
        continue;
      ::close(listener);
      throw std::runtime_error("cannot accept RDMA control connection");
    }
    std::thread([socket, index_path, ib_device, ib_port, &engine] {
      try {
        serve_rdma_session(socket, index_path, ib_device, ib_port, engine);
      } catch (const std::exception &e) {
        std::cerr << "RDMA session error: " << e.what() << '\n';
      }
      ::close(socket);
    }).detach();
  }
}

} // namespace rdma_hnsw

#else

#include <stdexcept>

namespace rdma_hnsw {
std::unique_ptr<RemoteTransport> make_rdma_client(const std::string &,
                                                  std::size_t,
                                                  const std::string &,
                                                  std::size_t) {
  throw std::runtime_error("RDMA transport unavailable");
}

int run_rdma_server(const std::string &, std::size_t, const std::string &,
                    const std::string &, std::size_t) {
  throw std::runtime_error("RDMA transport unavailable");
}
} // namespace rdma_hnsw

#endif
