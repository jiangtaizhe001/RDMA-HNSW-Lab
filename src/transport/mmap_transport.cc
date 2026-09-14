#include "rdma_hnsw/transport/mmap_transport.h"
#include <cerrno>
#include <cstring>
#include <fcntl.h>
#include <stdexcept>
#include <sys/stat.h>
#include <unistd.h>

namespace rdma_hnsw {
MmapTransport::MmapTransport(std::string path, bool writable)
    : path_(std::move(path)), writable_(writable) {
  fd_ = ::open(path_.c_str(), writable_ ? O_RDWR : O_RDONLY);
  if (fd_ < 0)
    throw std::runtime_error("cannot open mmap transport file " + path_ + ": " +
                             std::strerror(errno));
}
MmapTransport::~MmapTransport() {
  if (fd_ >= 0)
    ::close(fd_);
}
ReadResult MmapTransport::read(RemoteAddress a, std::span<byte> dst) {
  std::lock_guard lock(mutex_);
  stats_.read_ops.fetch_add(1);
  stats_.rtt.fetch_add(1);
  const auto n =
      ::pread(fd_, dst.data(), dst.size(), static_cast<off_t>(a.offset));
  if (n < 0)
    return {false, 0, std::strerror(errno)};
  stats_.read_bytes.fetch_add(static_cast<std::size_t>(n));
  return {true, static_cast<std::size_t>(n), {}};
}
WriteResult MmapTransport::write(RemoteAddress a, std::span<const byte> src) {
  if (!writable_)
    return {false, 0, "transport is read-only"};
  std::lock_guard lock(mutex_);
  stats_.write_ops.fetch_add(1);
  stats_.rtt.fetch_add(1);
  const auto n =
      ::pwrite(fd_, src.data(), src.size(), static_cast<off_t>(a.offset));
  if (n < 0)
    return {false, 0, std::strerror(errno)};
  stats_.write_bytes.fetch_add(static_cast<std::size_t>(n));
  return {static_cast<std::size_t>(n) == src.size(),
          static_cast<std::size_t>(n),
          {}};
}
UpdateResult MmapTransport::submit_update(const UpdateRequest &) {
  return {false, 0, "mmap updates are applied by storage_server"};
}
std::unique_ptr<RemoteTransport> make_mmap_transport(const std::string &path,
                                                     bool writable) {
  return std::make_unique<MmapTransport>(path, writable);
}
} // namespace rdma_hnsw
