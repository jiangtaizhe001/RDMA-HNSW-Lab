#include "rdma_hnsw/common/config.h"
#include "rdma_hnsw/common/data.h"
#include "rdma_hnsw/runtime/compute_node.h"
#include <algorithm>
#include <chrono>
#include <iostream>
#include <stdexcept>

namespace {
rdma_hnsw::UpdateRequest make_update(std::uint32_t op, rdma_hnsw::NodeId id,
                                     const rdma_hnsw::Vector &vector) {
  return {op, id,
          op == rdma_hnsw::update_op::Erase ? rdma_hnsw::Vector{} : vector};
}
} // namespace

int main(int argc, char **argv) {
  try {
    const auto c = rdma_hnsw::parse_config(argc, argv, "client");
    if (c.help)
      return 0;

    rdma_hnsw::IndexCatalog catalog;
    if (c.transport == rdma_hnsw::TransportKind::Mmap)
      catalog = rdma_hnsw::GraphStore::load_catalog(c.index);
    rdma_hnsw::ComputeNode node(
        {c.index, c.transport, c.host, c.port, c.ib_device, c.ib_port, c.policy,
         c.distance, c.ef_search, c.k, c.prefix_granularity, c.cache_capacity, c.rdma_connection, c.rdma_timeout_ms});
    // RDMA mode obtains the dimension and offset table during connection.
    catalog = node.query_engine().catalog();
    const auto queries =
        c.queries.empty()
            ? rdma_hnsw::generate_vectors(c.query_count, catalog.header.dim,
                                          c.seed)
            : rdma_hnsw::read_fvecs(c.queries, catalog.header.dim);
    const auto updates =
        c.updates.empty()
            ? rdma_hnsw::generate_vectors(c.update_count, catalog.header.dim,
                                          c.seed + 1)
            : rdma_hnsw::read_fvecs(c.updates, catalog.header.dim);
    const auto update_code =
        c.update_op == "insert"
            ? rdma_hnsw::update_op::Insert
            : (c.update_op == "erase" || c.update_op == "delete"
                   ? rdma_hnsw::update_op::Erase
                   : rdma_hnsw::update_op::Upsert);
    const auto base = c.base.empty()
                          ? std::vector<rdma_hnsw::Vector>{}
                          : rdma_hnsw::read_fvecs(c.base, catalog.header.dim);

    rdma_hnsw::QueryStats stats;
    if (c.workload == "mixed" && !updates.empty()) {
      const auto begin = std::chrono::steady_clock::now();
      const auto interval = std::max<std::size_t>(
          1, static_cast<std::size_t>(1.0 / std::max(0.0001, c.update_ratio)));
      for (std::size_t i = 0; i < queries.size(); ++i) {
        (void)node.query(queries[i]);
        if (i % interval == 0) {
          const auto update_index = i / interval;
          const auto id = update_code == rdma_hnsw::update_op::Insert
                              ? static_cast<rdma_hnsw::NodeId>(
                                    catalog.header.node_count + update_index)
                              : static_cast<rdma_hnsw::NodeId>(
                                    update_index % catalog.header.node_count);
          const auto result = node.update(make_update(
              update_code, id, updates[update_index % updates.size()]));
          if (!result.ok)
            throw std::runtime_error(result.error);
        }
      }
      stats = node.query_engine().stats();
      stats.queries = queries.size();
      stats.elapsed_ms = std::chrono::duration<double, std::milli>(
                             std::chrono::steady_clock::now() - begin)
                             .count();
    } else {
      stats = node.query_batch(queries, base.empty() ? nullptr : &base);
    }

    // The binary intentionally prints only a small human-readable summary.
    // Applications and student experiments can inspect QueryStats and
    // TransportStats directly, or attach their own measurement/export layer.
    std::cout << "completed_queries=" << stats.queries
              << " elapsed_ms=" << stats.elapsed_ms << '\n';

    if (c.workload != "mixed" && !updates.empty()) {
      for (std::size_t i = 0; i < updates.size(); ++i) {
        const auto id =
            update_code == rdma_hnsw::update_op::Insert
                ? static_cast<rdma_hnsw::NodeId>(catalog.header.node_count + i)
                : static_cast<rdma_hnsw::NodeId>(i % catalog.header.node_count);
        const auto result =
            node.update(make_update(update_code, id, updates[i]));
        if (!result.ok)
          throw std::runtime_error(result.error);
      }
      std::cout << "applied_updates=" << updates.size() << '\n';
    }
  } catch (const std::exception &e) {
    std::cerr << "compute_client: " << e.what() << '\n';
    return 1;
  }
}
