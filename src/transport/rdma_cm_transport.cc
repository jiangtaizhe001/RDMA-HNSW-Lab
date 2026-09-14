#include "rdma_hnsw/transport/rdma_cm_transport.h"
#include "cm_protocol.h"
#include "rdma_hnsw/runtime/storage_engine.h"
#include <algorithm>
#include <arpa/inet.h>
#include <bit>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fstream>
#include <iostream>
#include <limits>
#include <linux/types.h>
#include <memory>
#include <mutex>
#include <netdb.h>
#include <poll.h>
#include <rdma/rdma_cma.h>
#include <syncstream>
#include <thread>

namespace rdma_hnsw {
namespace {
using namespace cm_protocol;
using Clock = std::chrono::steady_clock;
constexpr std::uint64_t kLayout = (sizeof(IndexHeader) << 16) |
                                  (sizeof(NodeDiskHeader) << 8) |
                                  sizeof(NodeId);
static_assert(
    std::endian::native == std::endian::little,
    "The index and vector payload format currently requires little endian");
void check(int code, const char *action) {
  if (code)
    throw std::runtime_error(std::string(action) + ": " +
                             std::strerror(code > 0 ? code : errno));
}
void validate_options(std::size_t port, std::size_t device_port,
                      std::size_t timeout) {
  if (!port || port > 65535 || !device_port || device_port > 255 || !timeout ||
      timeout > 600000)
    throw std::invalid_argument("invalid CM port/device port/timeout");
}
struct PeerClosed : std::runtime_error {
  PeerClosed() : runtime_error("RDMA CM peer disconnected") {}
};

struct Channel {
  rdma_event_channel *value = rdma_create_event_channel();
  Channel() {
    if (!value)
      check(-1, "rdma_create_event_channel (check /dev/infiniband/rdma_cm)");
  }
  ~Channel() { rdma_destroy_event_channel(value); }
};
struct Event {
  rdma_cm_event *value{};
  explicit Event(rdma_event_channel *channel, int timeout) {
    pollfd p{channel->fd, POLLIN, 0};
    int n;
    do {
      n = ::poll(&p, 1, timeout);
    } while (n < 0 && errno == EINTR);
    if (n == 0)
      throw std::runtime_error("RDMA CM event timeout");
    check(n < 0 ? -1 : 0, "poll RDMA CM event");
    check(rdma_get_cm_event(channel, &value), "rdma_get_cm_event");
  }
  ~Event() {
    if (value)
      rdma_ack_cm_event(value);
  }
  void expect(rdma_cm_event_type type) {
    if (value->event == RDMA_CM_EVENT_DISCONNECTED)
      throw PeerClosed();
    if (value->event != type || value->status != 0)
      throw std::runtime_error(std::string("RDMA CM expected ") +
                               rdma_event_str(type) + ", got " +
                               rdma_event_str(value->event) +
                               " status=" + std::to_string(value->status));
  }
};
struct IdDeleter {
  void operator()(rdma_cm_id *p) const {
    if (p)
      rdma_destroy_id(p);
  }
};
using Id = std::unique_ptr<rdma_cm_id, IdDeleter>;
Id create_id(Channel &ch) {
  rdma_cm_id *id{};
  check(rdma_create_id(ch.value, &id, nullptr, RDMA_PS_TCP), "rdma_create_id");
  return Id(id);
}
sockaddr_in address(const std::string &host, std::size_t port, bool passive) {
  addrinfo hints{}, *raw{};
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_flags = passive ? AI_PASSIVE : 0;
  const auto service = std::to_string(port);
  const int result = getaddrinfo(host.empty() ? nullptr : host.c_str(),
                                 service.c_str(), &hints, &raw);
  if (result)
    throw std::runtime_error(std::string("resolve CM address: ") +
                             gai_strerror(result));
  std::unique_ptr<addrinfo, decltype(&freeaddrinfo)> info(raw, freeaddrinfo);
  return *reinterpret_cast<sockaddr_in *>(info->ai_addr);
}

struct Mr {
  ibv_mr *value{};
  Mr(ibv_pd *pd, void *data, std::size_t size, int access) {
    value = ibv_reg_mr(pd, data, size, access);
    if (!value)
      check(-1, "ibv_reg_mr (check memlock and registered-memory capacity)");
  }
  ~Mr() {
    if (value)
      ibv_dereg_mr(value);
  }
  Mr(const Mr &) = delete;
};
struct Region {
  std::vector<byte> data;
  Mr mr;
  Region(ibv_pd *pd, std::vector<byte> bytes, int access)
      : data(std::move(bytes)), mr(pd, data.data(), data.size(), access) {}
};

// Single-threaded connection object. Each ComputeNode owns one of these.
// Separate TX/RX/staging MRs avoid overwriting posted control receives.
class Connection {
public:
  std::shared_ptr<Channel> channel;
  Id id;
  ibv_pd *pd{};
  ibv_cq *send_cq{}, *recv_cq{};
  Packet tx{}, rx{};
  std::vector<byte> staging = std::vector<byte>(kStagingBytes);
  std::unique_ptr<Mr> tx_mr, rx_mr, staging_mr;
  int timeout;
  bool established = false;
  explicit Connection(std::shared_ptr<Channel> ch, Id handle, std::size_t ms)
      : channel(std::move(ch)), id(std::move(handle)),
        timeout(static_cast<int>(ms)) {}
  ~Connection() {
    if (id && id->qp) {
      if (established)
        rdma_disconnect(id.get());
      rdma_destroy_qp(id.get());
    }
    tx_mr.reset();
    rx_mr.reset();
    staging_mr.reset();
    if (send_cq)
      ibv_destroy_cq(send_cq);
    if (recv_cq)
      ibv_destroy_cq(recv_cq);
    if (pd)
      ibv_dealloc_pd(pd);
    // Ack queued disconnect/errors before destroying the CM id.
    pollfd p{channel->value->fd, POLLIN, 0};
    while (::poll(&p, 1, 0) > 0) {
      rdma_cm_event *event{};
      if (rdma_get_cm_event(channel->value, &event))
        break;
      rdma_ack_cm_event(event);
    }
  }
  void setup(const std::string &device, std::size_t port) {
    if (!id->verbs)
      throw std::runtime_error("CM did not resolve an RDMA device");
    const std::string actual = ibv_get_device_name(id->verbs->device);
    if ((!device.empty() && actual != device) || id->port_num != port)
      throw std::runtime_error(
          "CM route selected " + actual + ":" + std::to_string(id->port_num) +
          "; device/port assertion failed; check the ERI IP route");
    pd = ibv_alloc_pd(id->verbs);
    if (!pd)
      check(-1, "ibv_alloc_pd");
    send_cq = ibv_create_cq(id->verbs, 16, nullptr, nullptr, 0);
    recv_cq = ibv_create_cq(id->verbs, 16, nullptr, nullptr, 0);
    if (!send_cq || !recv_cq)
      check(-1, "ibv_create_cq");
    ibv_qp_init_attr attr{};
    attr.qp_type = IBV_QPT_RC;
    attr.send_cq = send_cq;
    attr.recv_cq = recv_cq;
    attr.cap.max_send_wr = 8;
    attr.cap.max_recv_wr = 2;
    attr.cap.max_send_sge = 1;
    attr.cap.max_recv_sge = 1;
    check(rdma_create_qp(id.get(), pd, &attr), "rdma_create_qp");
    tx_mr = std::make_unique<Mr>(pd, &tx, sizeof(tx), 0);
    rx_mr = std::make_unique<Mr>(pd, &rx, sizeof(rx), IBV_ACCESS_LOCAL_WRITE);
    staging_mr = std::make_unique<Mr>(pd, staging.data(), staging.size(),
                                      IBV_ACCESS_LOCAL_WRITE);
    post_receive();
  }
  void post_receive() {
    ibv_sge sge{reinterpret_cast<std::uintptr_t>(&rx), sizeof(rx),
                rx_mr->value->lkey};
    ibv_recv_wr wr{}, *bad{};
    wr.wr_id = 2;
    wr.sg_list = &sge;
    wr.num_sge = 1;
    check(ibv_post_recv(id->qp, &wr, &bad), "ibv_post_recv");
  }
  ibv_wc completion(ibv_cq *cq, bool idle = false) {
    const auto deadline = Clock::now() + std::chrono::milliseconds(timeout);
    unsigned spins = 0;
    while (true) {
      ibv_wc wc{};
      const int n = ibv_poll_cq(cq, 1, &wc);
      if (n < 0)
        throw std::runtime_error("ibv_poll_cq failed");
      if (n) {
        if (wc.status != IBV_WC_SUCCESS)
          throw std::runtime_error(std::string("CM work completion: ") +
                                   ibv_wc_status_str(wc.status));
        return wc;
      }
      pollfd p{channel->value->fd, POLLIN, 0};
      if (::poll(&p, 1, 0) > 0) {
        Event event(channel->value, timeout);
        if (event.value->event == RDMA_CM_EVENT_DISCONNECTED) {
          established = false;
          throw PeerClosed();
        }
        throw std::runtime_error(std::string("CM connection event: ") +
                                 rdma_event_str(event.value->event));
      }
      if (!idle && Clock::now() >= deadline)
        throw std::runtime_error(
            "CM operation timeout; reconnect before retrying");
      if (++spins < 100)
        std::this_thread::yield();
      else
        std::this_thread::sleep_for(std::chrono::microseconds(50));
    }
  }
  Packet receive(bool idle = false) {
    auto wc = completion(recv_cq, idle);
    if (wc.byte_len != sizeof(Packet))
      throw std::runtime_error("short CM control frame");
    Packet copy = rx;
    copy.validate();
    post_receive(); // ready for next command before sending its response
    return copy;
  }
  void send(const Packet &packet) {
    tx = packet;
    ibv_sge sge{reinterpret_cast<std::uintptr_t>(&tx), sizeof(tx),
                tx_mr->value->lkey};
    ibv_send_wr wr{}, *bad{};
    wr.wr_id = 1;
    wr.sg_list = &sge;
    wr.num_sge = 1;
    wr.opcode = IBV_WR_SEND;
    wr.send_flags = IBV_SEND_SIGNALED;
    check(ibv_post_send(id->qp, &wr, &bad), "ibv_post_send control SEND");
    completion(send_cq);
  }
  void transfer(std::uint64_t remote_address, std::uint32_t rkey, std::size_t n,
                bool write) {
    ibv_sge sge{reinterpret_cast<std::uintptr_t>(staging.data()),
                static_cast<std::uint32_t>(n), staging_mr->value->lkey};
    ibv_send_wr wr{}, *bad{};
    wr.wr_id = 3;
    wr.sg_list = &sge;
    wr.num_sge = 1;
    wr.opcode = write ? IBV_WR_RDMA_WRITE : IBV_WR_RDMA_READ;
    wr.send_flags = IBV_SEND_SIGNALED;
    wr.wr.rdma.remote_addr = remote_address;
    wr.wr.rdma.rkey = rkey;
    check(ibv_post_send(id->qp, &wr, &bad), "ibv_post_send RDMA transfer");
    completion(send_cq);
  }
};
rdma_conn_param parameters() {
  rdma_conn_param p{};
  p.initiator_depth = 1;
  p.responder_resources = 1;
  p.retry_count = 7;
  p.rnr_retry_count = 7;
  return p;
}
std::unique_ptr<Connection> connect(const std::string &host, std::size_t port,
                                    const std::string &device,
                                    std::size_t device_port,
                                    std::size_t timeout) {
  auto ch = std::make_shared<Channel>();
  auto c = std::make_unique<Connection>(ch, create_id(*ch), timeout);
  auto dst = address(host, port, false);
  check(rdma_resolve_addr(c->id.get(), nullptr,
                          reinterpret_cast<sockaddr *>(&dst), timeout),
        "rdma_resolve_addr");
  {
    Event e(ch->value, timeout);
    e.expect(RDMA_CM_EVENT_ADDR_RESOLVED);
  }
  check(rdma_resolve_route(c->id.get(), timeout), "rdma_resolve_route");
  {
    Event e(ch->value, timeout);
    e.expect(RDMA_CM_EVENT_ROUTE_RESOLVED);
  }
  c->setup(device, device_port);
  auto p = parameters();
  check(rdma_connect(c->id.get(), &p), "rdma_connect");
  {
    Event e(ch->value, timeout);
    e.expect(RDMA_CM_EVENT_ESTABLISHED);
  }
  c->established = true;
  return c;
}

class Client final : public RemoteTransport {
  std::unique_ptr<Connection> c_;
  Packet remote_;
  TransportStats stats_;
  std::mutex mutex_;
  bool failed_ = false;
  void accept_descriptor(const Packet &p) {
    if (p.get(Kind) != Reply || !p.get(Ok))
      throw std::runtime_error(p.error_text().empty() ? "CM handshake rejected"
                                                      : p.error_text());
    if (p.get(Layout) != kLayout || p.get(Size) < sizeof(IndexHeader) ||
        p.get(Size) >
            std::numeric_limits<std::uint64_t>::max() - p.get(Address) ||
        p.get(Rkey) > UINT32_MAX || p.get(MailboxRkey) > UINT32_MAX ||
        p.get(MailboxSize) != kStagingBytes ||
        p.get(MailboxAddress) > UINT64_MAX - kStagingBytes)
      throw std::runtime_error(
          "invalid CM memory descriptor or incompatible index ABI");
    remote_ = p;
  }

public:
  Client(const std::string &host, std::size_t port, const std::string &device,
         std::size_t device_port, std::size_t timeout)
      : c_(connect(host, port, device, device_port, timeout)) {
    auto hello = Packet::make(Hello);
    hello.set(Layout, kLayout);
    c_->send(
        hello); // iWARP active side sends first, with receives already posted
    accept_descriptor(c_->receive());
  }
  ~Client() override {
    // Disconnect is enough; never block destructor waiting for a control ACK.
  }
  ReadResult read(RemoteAddress a, std::span<byte> dst) override {
    std::lock_guard lock(mutex_);
    if (failed_)
      return {false, 0, "CM connection failed; reconnect required"};
    if (!fits(a.offset, dst.size(), remote_.get(Size)))
      return {false, 0, "CM read outside snapshot"};
    std::size_t done = 0;
    try {
      while (done < dst.size()) {
        const auto n = std::min(kStagingBytes, dst.size() - done);
        c_->transfer(remote_.get(Address) + a.offset + done, remote_.get(Rkey),
                     n, false);
        std::memcpy(dst.data() + done, c_->staging.data(), n);
        ++stats_.read_ops;
        stats_.read_bytes += n;
        ++stats_.rtt;
        done += n;
      }
      return {true, done, {}};
    } catch (const std::exception &e) {
      failed_ = true;
      return {false, done, e.what()};
    }
  }
  WriteResult write(RemoteAddress, std::span<const byte>) override {
    return {false, 0, "CM graph snapshot is read-only; use submit_update()"};
  }
  UpdateResult submit_update(const UpdateRequest &request) override {
    std::lock_guard lock(
        mutex_); // covers mailbox WRITE, SEND and reply as one operation
    if (failed_)
      return {false, 0, "CM connection failed; reconnect required"};
    if (request.vector.size() > kStagingBytes / sizeof(float))
      return {false, 0, "update vector exceeds CM mailbox"};
    try {
      const auto n = request.vector.size() * sizeof(float);
      if (n) {
        std::memcpy(c_->staging.data(), request.vector.data(), n);
        c_->transfer(remote_.get(MailboxAddress), remote_.get(MailboxRkey), n,
                     true);
        ++stats_.write_ops;
        stats_.write_bytes += n;
        ++stats_.rtt;
      }
      auto wire = Packet::make(Update);
      wire.set(Operation, request.op);
      wire.set(Node, request.id);
      wire.set(Count, request.vector.size());
      c_->send(wire);
      auto reply = c_->receive();
      if (reply.get(Kind) != Reply)
        throw std::runtime_error("invalid CM update response");
      if (!reply.get(Ok))
        return {false, reply.get(SnapshotGeneration), reply.error_text()};
      accept_descriptor(reply);
      return {true, reply.get(SnapshotGeneration), {}};
    } catch (const std::exception &e) {
      failed_ = true;
      return {false, 0, e.what()};
    }
  }
  const TransportStats &stats() const override { return stats_; }
};

std::vector<byte> load_index(const std::string &path) {
  std::ifstream in(path, std::ios::binary | std::ios::ate);
  if (!in || in.tellg() < static_cast<std::streamoff>(sizeof(IndexHeader)))
    throw std::runtime_error("cannot open complete CM index snapshot: " + path);
  const auto size = static_cast<std::size_t>(in.tellg());
  std::vector<byte> data(size);
  in.seekg(0);
  in.read(reinterpret_cast<char *>(data.data()), size);
  if (!in)
    throw std::runtime_error("short CM snapshot file read");
  return data;
}
struct Server {
  std::string path;
  std::mutex update_mutex;
  // All server mutations use one engine and are serialized through publication.
  StorageEngine engine;
  explicit Server(const std::string &p) : path(p), engine(p) {}
};
Packet descriptor(const Region &snapshot, const Region &mailbox) {
  auto p = Packet::make(Reply);
  p.set(Ok, 1);
  p.set(Layout, kLayout);
  IndexHeader h{};
  std::memcpy(&h, snapshot.data.data(), sizeof(h));
  p.set(SnapshotGeneration, h.generation);
  p.set(Address, reinterpret_cast<std::uintptr_t>(snapshot.data.data()));
  p.set(Size, snapshot.data.size());
  p.set(Rkey, snapshot.mr.value->rkey);
  p.set(MailboxAddress, reinterpret_cast<std::uintptr_t>(mailbox.data.data()));
  p.set(MailboxSize, mailbox.data.size());
  p.set(MailboxRkey, mailbox.mr.value->rkey);
  return p;
}
void session(std::unique_ptr<Connection> c, std::shared_ptr<Server> server,
             const std::string &device, std::size_t device_port) {
  try {
    c->setup(device, device_port);
    auto p = parameters();
    check(rdma_accept(c->id.get(), &p), "rdma_accept");
    {
      Event e(c->channel->value, c->timeout);
      e.expect(RDMA_CM_EVENT_ESTABLISHED);
    }
    c->established = true;
    auto hello = c->receive();
    if (hello.get(Kind) != Hello || hello.get(Layout) != kLayout)
      throw std::runtime_error("incompatible CM hello/index layout");
    Region mailbox(c->pd, std::vector<byte>(kStagingBytes),
                   IBV_ACCESS_LOCAL_WRITE | IBV_ACCESS_REMOTE_WRITE);
    std::unique_ptr<Region> snapshot;
    struct Drain {
      Connection &c;
      ~Drain() {
        if (c.id->qp) {
          if (c.established)
            rdma_disconnect(c.id.get());
          rdma_destroy_qp(c.id.get());
        }
      }
    } drain{*c}; // destroyed before either registered data region
    {
      std::lock_guard lock(server->update_mutex);
      snapshot = std::make_unique<Region>(c->pd, load_index(server->path),
                                          IBV_ACCESS_REMOTE_READ);
    }
    c->send(descriptor(*snapshot, mailbox));
    std::osyncstream(std::cerr)
        << "RDMA CM session ready device="
        << ibv_get_device_name(c->id->verbs->device)
        << " snapshot_bytes=" << snapshot->data.size() << '\n';
    try {
      while (true) {
        auto command = c->receive(true);
        if (command.get(Kind) == Goodbye)
          break;
        if (command.get(Kind) != Update)
          throw std::runtime_error("unexpected CM command");
        auto reply = Packet::make(Reply);
        std::unique_ptr<Region> next;
        try {
          const auto count = command.get(Count);
          if (count > kStagingBytes / sizeof(float) ||
              command.get(Node) > UINT32_MAX ||
              command.get(Operation) < update_op::Insert ||
              command.get(Operation) > update_op::Erase)
            throw std::runtime_error(
                "invalid CM update operation/node/vector length");
          UpdateRequest request{
              static_cast<std::uint32_t>(command.get(Operation)),
              static_cast<NodeId>(command.get(Node)), Vector(count)};
          if (count)
            std::memcpy(request.vector.data(), mailbox.data.data(),
                        count * sizeof(float));
          std::lock_guard lock(server->update_mutex);
          auto result = server->engine.apply(request);
          reply.set(SnapshotGeneration, result.generation);
          if (!result.ok)
            reply.set_error(result.error);
          else {
            // Never overwrite another reader's MR. A session is sequential;
            // its old MR stays alive until its reply SEND completes.
            next = std::make_unique<Region>(c->pd, load_index(server->path),
                                            IBV_ACCESS_REMOTE_READ);
            reply = descriptor(*next, mailbox);
          }
        } catch (const std::exception &e) {
          reply.set_error(
              std::string("update/publish failed; reconnect and inspect "
                          "committed index before retry: ") +
              e.what());
        }
        try {
          c->send(reply);
        } catch (...) {
          // The peer may already have received the new descriptor. Stop the
          // QP before next's MR is deregistered during stack unwinding.
          if (c->id->qp)
            rdma_destroy_qp(c->id.get());
          throw;
        }
        if (next)
          snapshot = std::move(next);
      }
    } catch (...) {
      // Stop remote operations before deregistering session MRs, including
      // disconnect/error paths. Connection owns the remaining QP/CQ resources.
      if (c->id->qp) {
        rdma_destroy_qp(c->id.get());
      }
      throw;
    }
    if (c->id->qp)
      rdma_destroy_qp(c->id.get());
  } catch (const PeerClosed &) {
    std::osyncstream(std::cerr) << "RDMA CM session closed by peer\n";
  } catch (const std::exception &e) {
    std::osyncstream(std::cerr)
        << "RDMA CM session error: " << e.what() << '\n';
  }
}
} // namespace

std::unique_ptr<RemoteTransport> make_rdma_cm_client(const std::string &host,
                                                     std::size_t port,
                                                     const std::string &device,
                                                     std::size_t device_port,
                                                     std::size_t timeout) {
  validate_options(port, device_port, timeout);
  return std::make_unique<Client>(host, port, device, device_port, timeout);
}
int run_rdma_cm_server(const std::string &bind, std::size_t port,
                       const std::string &index_path, const std::string &device,
                       std::size_t device_port, std::size_t timeout) {
  validate_options(port, device_port, timeout);
  auto server = std::make_shared<Server>(index_path);
  auto listener_channel = std::make_shared<Channel>();
  auto listener = create_id(*listener_channel);
  auto addr = address(bind, port, true);
  check(rdma_bind_addr(listener.get(), reinterpret_cast<sockaddr *>(&addr)),
        "rdma_bind_addr (use ERI local IP)");
  check(rdma_listen(listener.get(), 32), "rdma_listen");
  std::osyncstream(std::cerr)
      << "RDMA CM server listening on " << bind << ':' << port << '\n';
  while (true) {
    Id request;
    {
      Event event(listener_channel->value, -1);
      event.expect(RDMA_CM_EVENT_CONNECT_REQUEST);
      request.reset(event.value->id);
    } // ACK before migration so the listener remains independent of sessions.
    try {
      auto ch = std::make_shared<Channel>();
      check(rdma_migrate_id(request.get(), ch->value), "rdma_migrate_id");
      auto connection =
          std::make_unique<Connection>(ch, std::move(request), timeout);
      std::thread(session, std::move(connection), server, device, device_port)
          .detach();
    } catch (const std::exception &e) {
      if (request)
        rdma_reject(request.get(), nullptr, 0);
      std::osyncstream(std::cerr)
          << "RDMA CM accept error: " << e.what() << '\n';
    }
  }
}
} // namespace rdma_hnsw
