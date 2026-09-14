#include "../src/transport/cm_protocol.h"
#include "rdma_hnsw/common/config.h"
#include <iostream>
#include <limits>
using namespace rdma_hnsw;
using namespace rdma_hnsw::cm_protocol;
void require(bool ok, const char *message) {
  if (!ok)
    throw std::runtime_error(message);
}
int main() {
  try {
    auto p = Packet::make(Reply);
    p.set(Address, 0x123456789abcdef0ULL);
    p.set(Size, 1ULL << 34);
    p.validate();
    require(p.get(Address) == 0x123456789abcdef0ULL &&
                p.get(Size) == (1ULL << 34),
            "descriptor roundtrip");
    auto *bytes = reinterpret_cast<const unsigned char *>(&p.words[Address]);
    require(bytes[0] == 0x12 && bytes[7] == 0xf0, "wire must be big endian");
    p.set(Version, 999);
    bool rejected = false;
    try {
      p.validate();
    } catch (...) {
      rejected = true;
    }
    require(rejected, "reject incompatible protocol version");
    p.set_error(std::string(500, 'x'));
    require(p.error_text().size() == 255, "bound error response");
    p.set_error("short");
    require(p.error_text() == "short", "reused error frame must be terminated");
    require(fits(128, 0, 128) && !fits(129, 0, 128), "zero read bounds");
    require(!fits(UINT64_MAX - 1, 10, UINT64_MAX), "overflow bounds");
    char a0[] = "client", a1[] = "--transport", a2[] = "rdma",
         a3[] = "--rdma-connection", a4[] = "cm";
    char *args[]{a0, a1, a2, a3, a4};
    require(parse_config(5, args, "client").rdma_connection ==
                RdmaConnection::Cm,
            "CM CLI option");
    char bad[] = "unknown";
    args[4] = bad;
    rejected = false;
    try {
      parse_config(5, args, "client");
    } catch (...) {
      rejected = true;
    }
    require(rejected, "invalid connection option");
    std::cout << "CM protocol and option tests passed\n";
  } catch (const std::exception &e) {
    std::cerr << e.what() << '\n';
    return 1;
  }
}
