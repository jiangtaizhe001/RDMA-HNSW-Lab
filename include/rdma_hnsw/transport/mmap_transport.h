#pragma once
#include "rdma_hnsw/transport/transport.h"
#include <mutex>

namespace rdma_hnsw {
class MmapTransport final : public RemoteTransport {
public:
  MmapTransport(std::string path, bool writable);
  ~MmapTransport() override;
  ReadResult read(RemoteAddress, std::span<byte>) override;
  WriteResult write(RemoteAddress, std::span<const byte>) override;
  UpdateResult submit_update(const UpdateRequest &) override;
  const TransportStats &stats() const override { return stats_; }

private:
  std::string path_;
  int fd_{-1};
  bool writable_{};
  mutable std::mutex mutex_;
  TransportStats stats_;
};
} // namespace rdma_hnsw
