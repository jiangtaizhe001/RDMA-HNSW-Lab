#pragma once
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <endian.h>
#include <stdexcept>
#include <string>

namespace rdma_hnsw::cm_protocol {
constexpr std::uint64_t kMagic = 0x52484e5357434d31ULL; // RHNSWCM1
constexpr std::uint64_t kVersion = 1;
constexpr std::size_t kStagingBytes = 1 << 20;
enum Field {
  Magic,
  Version,
  Kind,
  Operation,
  Node,
  Count,
  Ok,
  SnapshotGeneration,
  Address,
  Size,
  Rkey,
  MailboxAddress,
  MailboxSize,
  MailboxRkey,
  Layout,
  Fields
};
enum KindValue : std::uint64_t {
  Hello = 1,
  Reply = 2,
  Update = 3,
  Goodbye = 4
};
// Fixed wire frame; integer fields are big endian. Index/vector payloads retain
// the project's little-endian, native-layout format (checked during hello).
struct Packet {
  std::array<std::uint64_t, Fields> words{};
  std::array<char, 256> error{};
  std::uint64_t get(Field f) const { return be64toh(words[f]); }
  void set(Field f, std::uint64_t v) { words[f] = htobe64(v); }
  static Packet make(KindValue kind) {
    Packet p;
    p.set(Magic, kMagic);
    p.set(Version, kVersion);
    p.set(Kind, kind);
    return p;
  }
  void validate() const {
    if (get(Magic) != kMagic || get(Version) != kVersion || get(Kind) < Hello ||
        get(Kind) > Goodbye)
      throw std::runtime_error("incompatible RDMA CM protocol frame");
  }
  void set_error(const std::string &text) {
    set(Ok, 0);
    error.fill(0);
    std::memcpy(error.data(), text.data(),
                std::min(text.size(), error.size() - 1));
  }
  std::string error_text() const {
    return std::string(error.data(), strnlen(error.data(), error.size()));
  }
};
inline bool fits(std::uint64_t offset, std::uint64_t bytes,
                 std::uint64_t size) {
  return offset <= size && bytes <= size - offset;
}
} // namespace rdma_hnsw::cm_protocol
