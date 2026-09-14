#include "rdma_hnsw/runtime/compute_node.h"
#include <algorithm>
#include <atomic>
#include <barrier>
#include <cstdlib>
#include <cstring>
#include <iostream>
#include <thread>
using namespace rdma_hnsw;
void require(bool ok, const std::string &message) {
  if (!ok)
    throw std::runtime_error(message);
}
std::pair<NodeDiskHeader, Vector> read_node(ComputeNode &n, NodeId id) {
  const auto &c = n.query_engine().catalog();
  std::vector<byte> data(sizeof(NodeDiskHeader) + c.header.dim * sizeof(float));
  auto r = n.transport().read({c.offsets.at(id), 0}, data);
  require(r.ok && r.bytes == data.size(), "read_node: " + r.error);
  NodeDiskHeader h{};
  Vector v(c.header.dim);
  std::memcpy(&h, data.data(), sizeof(h));
  std::memcpy(v.data(), data.data() + sizeof(h), v.size() * sizeof(float));
  return {h, v};
}
int main() {
  const char *host = std::getenv("RDMA_CM_TEST_HOST");
  if (!host) {
    std::cout << "Set RDMA_CM_TEST_HOST to enable hardware test\n";
    return 77;
  }
  try {
    ComputeNodeConfig c;
    c.transport = TransportKind::Rdma;
    c.rdma_connection = RdmaConnection::Cm;
    c.host = host;
    if (auto p = std::getenv("RDMA_CM_TEST_PORT"))
      c.port = std::stoul(p);
    if (auto d = std::getenv("RDMA_CM_TEST_DEVICE"))
      c.ib_device = d;
    ComputeNode old(c), writer(c);
    auto [header, before] = read_node(old, 0);
    require(old.query_engine().catalog().header.node_count >= 16,
            "test index needs >=16 nodes");
    Vector updated(before.size(), 3.25f);
    updated[0] = before[0] == 3.25f ? 4.25f : 3.25f;
    auto invalid =
        writer.update({update_op::Upsert, 0, Vector(before.size() + 1)});
    require(!invalid.ok, "dimension error must be returned");
    auto result = writer.update({update_op::Upsert, 0, updated});
    require(result.ok, "valid update after error: " + result.error);
    require(read_node(writer, 0).second == updated, "writer sees new snapshot");
    require(read_node(old, 0).second == before,
            "other session retains original snapshot");
    old.reload();
    require(read_node(old, 0).second == updated,
            "reload sees committed update");
    std::vector<byte> large((1 << 20) + 17), first(64);
    auto lr = old.transport().read({0, 0}, large);
    require(lr.ok && lr.bytes == large.size(),
            "chunked read larger than staging: " + lr.error);
    require(old.transport().read({0, 0}, first).ok, "small read");
    require(std::equal(first.begin(), first.end(), large.begin()),
            "chunked header mismatch");
    require(!old.transport().write({0, 0}, first).ok,
            "snapshot must be read-only");
    require(!old.transport().read({UINT64_MAX, 0}, first).ok,
            "reject read overflow");
    require(old.transport().read({0, 0}, first).ok,
            "local bounds error must not kill connection");
    std::atomic<unsigned> failures{0}, writes{0};
    std::barrier start(6);
    std::vector<std::jthread> threads;
    for (unsigned i = 0; i < 6; ++i)
      threads.emplace_back([&, i] {
        bool reached = false;
        try {
          ComputeNode worker(c);
          reached = true;
          start.arrive_and_wait();
          for (unsigned j = 0; j < 8; ++j) {
            if (i < 4)
              require(!worker.query(updated).empty(), "empty concurrent query");
            else {
              auto u = worker.update({update_op::Upsert, static_cast<NodeId>(i),
                                      Vector(before.size(), float(i + j))});
              require(u.ok, "concurrent update: " + u.error);
              ++writes;
            }
          }
        } catch (const std::exception &e) {
          if (!reached)
            start.arrive_and_drop();
          ++failures;
          std::cerr << e.what() << '\n';
        }
      });
    for (auto &t : threads)
      t.join();
    require(failures == 0 && writes == 16, "concurrent workers failed");
    writer.reload();
    auto count = writer.query_engine().catalog().header.node_count;
    require(
        writer.update({update_op::Insert, static_cast<NodeId>(count), updated})
            .ok,
        "insert");
    require(writer.query_engine().catalog().header.node_count == count + 1,
            "insert refreshes catalog");
    require(read_node(writer, count).second == updated, "inserted record");
    // old still has the pre-insert layout. Its upsert must install BOTH the
    // latest MR and its catalog, even though it did not perform the insert.
    require(old.update({update_op::Upsert, 1, updated}).ok,
            "upsert after another client inserted");
    require(old.query_engine().catalog().header.node_count == count + 1,
            "upsert refreshes externally grown catalog");
    require(read_node(old, count).second == updated,
            "new snapshot uses new offsets");
    require(!old.query(updated).empty(),
            "query after external insert and own update");
    require(
        writer.update({update_op::Erase, static_cast<NodeId>(count), {}}).ok,
        "erase");
    require(read_node(writer, count).first.deleted != 0, "erase published");
    ComputeNode reconnect(c);
    require(reconnect.query_engine().catalog().header.node_count == count + 1,
            "new connection sees latest version");
    std::cout << "CM integration passed: handshake, large read, errors, "
                 "snapshots, 4 readers + 2 writers, insert/erase/reconnect\n";
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
