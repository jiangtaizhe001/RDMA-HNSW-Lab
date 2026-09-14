#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/runtime/compute_node.h"
#include <cassert>
#include <filesystem>

int main() {
  const auto path =
      (std::filesystem::temp_directory_path() / "rdma_hnsw_runtime_api.bin")
          .string();
  rdma_hnsw::IndexHeader header{};
  header.dim = 2;
  header.M = 2;
  header.max_level = 0;
  header.entrypoint = 0;
  std::vector<rdma_hnsw::NodeRecord> nodes(3);
  for (rdma_hnsw::NodeId i = 0; i < nodes.size(); ++i) {
    nodes[i].id = i;
    nodes[i].vector = {static_cast<float>(i), 0.0F};
    nodes[i].neighbors = {{i == 0 ? 1U : 0U, i == 2 ? 1U : 2U}};
  }
  rdma_hnsw::GraphStore::write(path, header, nodes);

  rdma_hnsw::ComputeNode compute({path,
                                  rdma_hnsw::TransportKind::Mmap,
                                  "127.0.0.1",
                                  18515,
                                  {},
                                  1,
                                  rdma_hnsw::ReadPolicy::Fixed,
                                  rdma_hnsw::DistanceKind::L2,
                                  8,
                                  1,
                                  2,
                                  8});
  const auto result = compute.query({0.05F, 0.0F});
  assert(!result.empty() && result.front() == 0);

  const auto update = compute.update({2, 1, {0.1F, 0.0F}});
  assert(update.ok);
  compute.reload();
  assert(compute.query({0.1F, 0.0F}).front() == 1);
  const auto after = rdma_hnsw::GraphStore::load_catalog(path);
  rdma_hnsw::GraphStore updated(after);
  assert(updated.read_node(0).generation == 1);
  assert(updated.read_node(1).generation == 2);
  assert(compute.update({rdma_hnsw::update_op::Insert, 3, {0.2F, 0.0F}}).ok);
  const auto inserted = rdma_hnsw::GraphStore::load_catalog(path);
  assert(inserted.header.node_count == 4);
  assert(compute.query({0.2F, 0.0F}).front() == 3);
  assert(compute.update({rdma_hnsw::update_op::Erase, 3, {}}).ok);
  std::filesystem::remove(path);
}
