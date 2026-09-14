#pragma once

#include "rdma_hnsw/common/types.h"
#include <atomic>
#include <cstddef>
#include <memory>
#include <span>
#include <string>

namespace rdma_hnsw {
struct TransportStats {
  std::atomic<std::uint64_t> read_bytes{0}, write_bytes{0}, read_ops{0},
      write_ops{0}, rtt{0};
};
struct ReadResult {
  bool ok{false};
  std::size_t bytes{};
  std::string error;
};
struct WriteResult {
  bool ok{false};
  std::size_t bytes{};
  std::string error;
};
namespace update_op {
constexpr std::uint32_t Insert = 1;
constexpr std::uint32_t Upsert = 2;
constexpr std::uint32_t Erase = 3;
} // namespace update_op
struct UpdateRequest {
  std::uint32_t op{};
  NodeId id{};
  std::vector<float> vector;
};
struct UpdateResult {
  bool ok{false};
  Generation generation{};
  std::string error;
};

class RemoteTransport {
public:
  virtual ~RemoteTransport() = default;
  virtual ReadResult read(RemoteAddress address, std::span<byte> dst) = 0;
  virtual WriteResult write(RemoteAddress address,
                            std::span<const byte> src) = 0;
  virtual UpdateResult submit_update(const UpdateRequest &) = 0;
  virtual const TransportStats &stats() const = 0;
};

std::unique_ptr<RemoteTransport> make_mmap_transport(const std::string &path,
                                                     bool writable);
} // namespace rdma_hnsw
