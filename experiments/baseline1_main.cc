// Read-strategy experiment driver (docs/baseline1-plan.md T4, baseline2-plan.md T4).
//
// Runs any built-in NeighborReadStrategy selected with --read-strategy
// (fixed|baseline1 or header_actual|header).  Baseline 1 measures the cost of
// reading the full fixed-capacity neighbour slot array; Baseline 2 reads the
// degree word first and then only the actual neighbours.  Both share the exact
// same workload, metric accounting and CSV contract so their rows pair up.
//
// Purpose of the driver: measure the cost of every (node, level) neighbour read
// touched by a query, with per-query latency, neighbour-region bytes,
// node-region bytes and read-op counts, and export the raw samples as CSV.
//
// The driver is deliberately self-contained: it never modifies the query engine
// or the read strategy internals, it only injects a NeighborReadStrategy and
// reads the public metrics surface.  This keeps the comparison baseline shared
// by every strategy (docs/baseline1-plan.md D3 / DR-4).
//
// Key correctness detail: in mmap mode ComputeNode::update() calls reload(),
// which rebuilds the transport and query engine and therefore resets the
// QueryStats counters.  Per-query metric deltas are taken around each single
// query (before -> after), so no reads are ever lost to a reset and no update
// can smuggle bytes into the wrong query.  Aggregate totals are the sum of the
// per-query deltas.
#include "rdma_hnsw/common/data.h"
#include "rdma_hnsw/common/types.h"
#include "rdma_hnsw/graph/graph_store.h"
#include "rdma_hnsw/graph/hnsw_builder.h"
#include "rdma_hnsw/read/read_strategy.h"
#include "rdma_hnsw/runtime/compute_node.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <iostream>
#include <memory>
#include <numeric>
#include <stdexcept>
#include <string>
#include <unordered_set>
#include <vector>

namespace rh = rdma_hnsw;

namespace {

struct Options {
  std::string index;
  std::string out;
  std::string summary_out;
  std::string queries;
  std::string base;
  std::string gt;
  std::string updates;
  std::string label = "synthetic";
  std::string transport = "mmap";
  std::string host = "127.0.0.1";
  std::string update_op = "none";
  std::string read_strategy = "baseline1";
  std::string dataset = "synthetic";
  std::string distance = "l2";
  std::size_t port = 18515;
  std::size_t query_count = 1000;
  std::size_t update_count = 0;
  std::size_t k = 10;
  std::size_t ef_search = 64;
  std::size_t ef_construction = 100;
  std::size_t warmup = 0;
  std::size_t seed = 42;
  std::size_t dim = 32;
  std::size_t M = 16;
  double update_ratio = 0.1;
  rh::ReadPolicy policy = rh::ReadPolicy::Fixed;
  bool smoke = false;
  bool help = false;
};

// Human-readable label recorded in the CSV so Baselines 1 and 2 can be paired.
const char *strategy_label(rh::ReadPolicy p) {
  return p == rh::ReadPolicy::HeaderActual ? "baseline2_header_first"
                                           : "baseline1_fixed";
}

std::string next_value(int &i, int argc, char **argv) {
  if (++i >= argc)
    throw std::invalid_argument("missing value for " + std::string(argv[i - 1]));
  return argv[i];
}
std::size_t as_size(const std::string &v) {
  return static_cast<std::size_t>(std::stoull(v));
}
double as_double(const std::string &v) { return std::stod(v); }

void print_usage() {
  std::cout
      << "baseline1_experiment options:\n"
      << "  --index FILE               index to query\n"
      << "  --transport mmap|rdma      transport backend (default mmap)\n"
      << "  --host HOST --port N       RDMA control endpoint\n"
      << "  --queries FILE             query vectors (.fvecs)\n"
      << "  --queries-count N          synthetic queries when --queries is absent\n"
      << "  --base FILE                base vectors for exact recall\n"
      << "  --gt FILE                  precomputed ground-truth ids (offline recall)\n"
      << "  --k N --ef-search N        search parameters\n"
      << "  --updates FILE             update vectors (.fvecs)\n"
      << "  --updates-count N          synthetic updates when --updates is absent\n"
      << "  --workload query|mixed     static or mixed query+update workload\n"
      << "  --update-op insert|upsert|erase\n"
      << "  --update-ratio R           fraction of queries followed by an update\n"
      << "  --warmup N                 warm-up queries excluded from samples\n"
      << "  --out FILE                 per-query CSV output\n"
      << "  --summary-out FILE         append a one-row summary CSV\n"
      << "  --dataset --label NAME     labels recorded in the summary\n"
      << "  --dim N --M N --seed N     synthetic data / smoke parameters\n"
      << "  --ef-construction N        recorded in the summary (not derivable)\n"
      << "  --read-strategy NAME       fixed|baseline1|header_actual|header\n"
      << "  --smoke                    build a tiny index if missing and query it\n"
      << "  --help                     show this help\n";
}

void parse_workload(Options &o, const std::string &w) {
  if (w == "mixed")
    o.update_op = (o.update_op == "none") ? "upsert" : o.update_op;
  else if (w != "query")
    throw std::invalid_argument("workload must be query or mixed");
}

Options parse(int argc, char **argv) {
  Options o;
  for (int i = 1; i < argc; ++i) {
    const std::string a = argv[i];
    if (a == "--help" || a == "-h")
      o.help = true;
    else if (a == "--index")
      o.index = next_value(i, argc, argv);
    else if (a == "--out")
      o.out = next_value(i, argc, argv);
    else if (a == "--summary-out")
      o.summary_out = next_value(i, argc, argv);
    else if (a == "--queries")
      o.queries = next_value(i, argc, argv);
    else if (a == "--base")
      o.base = next_value(i, argc, argv);
    else if (a == "--gt")
      o.gt = next_value(i, argc, argv);
    else if (a == "--updates")
      o.updates = next_value(i, argc, argv);
    else if (a == "--dataset")
      o.dataset = next_value(i, argc, argv);
    else if (a == "--label")
      o.label = next_value(i, argc, argv);
    else if (a == "--transport")
      o.transport = next_value(i, argc, argv);
    else if (a == "--host")
      o.host = next_value(i, argc, argv);
    else if (a == "--port")
      o.port = as_size(next_value(i, argc, argv));
    else if (a == "--distance")
      o.distance = next_value(i, argc, argv);
    else if (a == "--workload")
      parse_workload(o, next_value(i, argc, argv));
    else if (a == "--update-op")
      o.update_op = next_value(i, argc, argv);
    else if (a == "--update-ratio")
      o.update_ratio = as_double(next_value(i, argc, argv));
    else if (a == "--read-strategy")
      o.read_strategy = next_value(i, argc, argv);
    else if (a == "--queries-count")
      o.query_count = as_size(next_value(i, argc, argv));
    else if (a == "--updates-count")
      o.update_count = as_size(next_value(i, argc, argv));
    else if (a == "--k")
      o.k = as_size(next_value(i, argc, argv));
    else if (a == "--ef-search")
      o.ef_search = as_size(next_value(i, argc, argv));
    else if (a == "--ef-construction")
      o.ef_construction = as_size(next_value(i, argc, argv));
    else if (a == "--warmup")
      o.warmup = as_size(next_value(i, argc, argv));
    else if (a == "--seed")
      o.seed = as_size(next_value(i, argc, argv));
    else if (a == "--dim")
      o.dim = as_size(next_value(i, argc, argv));
    else if (a == "--M")
      o.M = as_size(next_value(i, argc, argv));
    else if (a == "--smoke")
      o.smoke = true;
    else
      throw std::invalid_argument("unknown option: " + a);
  }
  if (o.transport != "mmap" && o.transport != "rdma")
    throw std::invalid_argument("transport must be mmap or rdma");
  if (o.update_op != "none" && o.update_op != "insert" &&
      o.update_op != "upsert" && o.update_op != "erase")
    throw std::invalid_argument("update-op must be none, insert, upsert or erase");
  if (o.read_strategy == "fixed" || o.read_strategy == "baseline1")
    o.policy = rh::ReadPolicy::Fixed;
  else if (o.read_strategy == "header_actual" || o.read_strategy == "header")
    o.policy = rh::ReadPolicy::HeaderActual;
  else
    throw std::invalid_argument(
        "unsupported --read-strategy (use fixed|baseline1|header_actual|header)");
  if (o.k == 0 || o.ef_search < o.k)
    throw std::invalid_argument("require k > 0 and ef-search >= k");
  if (o.update_ratio < 0.0 || o.update_ratio > 1.0)
    throw std::invalid_argument("update-ratio must be within [0,1]");
  return o;
}

float distance(const rh::Vector &a, const rh::Vector &b, bool inner) {
  float d = 0;
  const auto n = std::min(a.size(), b.size());
  if (inner) {
    for (std::size_t i = 0; i < n; ++i)
      d -= a[i] * b[i];
    return d;
  }
  for (std::size_t i = 0; i < n; ++i) {
    const float x = a[i] - b[i];
    d += x * x;
  }
  return d;
}

std::vector<rh::NodeId> exact_topk(const rh::Vector &q,
                                   const std::vector<rh::Vector> &base,
                                   std::size_t k, bool inner) {
  std::vector<std::pair<float, rh::NodeId>> all;
  all.reserve(base.size());
  for (rh::NodeId i = 0; i < base.size(); ++i)
    all.emplace_back(distance(q, base[i], inner), i);
  const auto take = std::min(k, all.size());
  std::partial_sort(all.begin(), all.begin() + take, all.end());
  std::vector<rh::NodeId> ids(take);
  for (std::size_t i = 0; i < take; ++i)
    ids[i] = all[i].second;
  return ids;
}

std::vector<std::vector<rh::NodeId>> read_ground_truth(const std::string &path,
                                                       std::size_t k) {
  std::ifstream in(path, std::ios::binary);
  if (!in)
    throw std::runtime_error("cannot open ground-truth file: " + path);
  std::vector<std::vector<rh::NodeId>> rows;
  while (true) {
    std::vector<rh::NodeId> row(k);
    in.read(reinterpret_cast<char *>(row.data()),
            static_cast<std::streamsize>(row.size() * sizeof(rh::NodeId)));
    if (in.gcount() == 0)
      break;
    if (in.gcount() !=
        static_cast<std::streamsize>(row.size() * sizeof(rh::NodeId)))
      throw std::runtime_error("truncated ground-truth file: " + path);
    rows.push_back(std::move(row));
  }
  if (rows.empty())
    throw std::runtime_error("empty ground-truth file: " + path);
  return rows;
}

rh::ReadMetrics diff_metrics(const rh::ReadMetrics &a, const rh::ReadMetrics &b) {
  rh::ReadMetrics d;
  d.requested_bytes = a.requested_bytes - b.requested_bytes;
  d.useful_bytes = a.useful_bytes - b.useful_bytes;
  d.wasted_bytes = a.wasted_bytes - b.wasted_bytes;
  d.read_ops = a.read_ops - b.read_ops;
  d.header_reads = a.header_reads - b.header_reads;
  d.dependent_reads = a.dependent_reads - b.dependent_reads;
  d.zero_degree_layers = a.zero_degree_layers - b.zero_degree_layers;
  d.failed_reads = a.failed_reads - b.failed_reads;
  d.node_reads = a.node_reads - b.node_reads;
  d.node_bytes = a.node_bytes - b.node_bytes;
  d.layer_reads = a.layer_reads - b.layer_reads;
  d.degree_sum = a.degree_sum - b.degree_sum;
  d.capacity_sum = a.capacity_sum - b.capacity_sum;
  return d;
}

struct Sample {
  double latency_us{};
  rh::ReadMetrics m;
};

double percentile(std::vector<double> v, double p) {
  if (v.empty())
    return 0.0;
  std::sort(v.begin(), v.end());
  const double idx = p * (v.size() - 1);
  const auto lo = static_cast<std::size_t>(std::floor(idx));
  const auto hi = static_cast<std::size_t>(std::ceil(idx));
  if (lo == hi)
    return v[lo];
  return v[lo] + (v[hi] - v[lo]) * (idx - static_cast<double>(lo));
}

struct Totals {
  std::uint64_t requested{}, useful{}, wasted{}, read_ops{}, header_reads{},
      dependent_reads{}, zero_degree_layers{}, failed_reads{}, node_reads{},
      node_bytes{}, layer_reads{}, degree_sum{}, capacity_sum{};
};

void add(Totals &t, const rh::ReadMetrics &m) {
  t.requested += m.requested_bytes;
  t.useful += m.useful_bytes;
  t.wasted += m.wasted_bytes;
  t.read_ops += m.read_ops;
  t.header_reads += m.header_reads;
  t.dependent_reads += m.dependent_reads;
  t.zero_degree_layers += m.zero_degree_layers;
  t.failed_reads += m.failed_reads;
  t.node_reads += m.node_reads;
  t.node_bytes += m.node_bytes;
  t.layer_reads += m.layer_reads;
  t.degree_sum += m.degree_sum;
  t.capacity_sum += m.capacity_sum;
}

std::string csv_escape(const std::string &s) {
  if (s.find_first_of(",\"\n") == std::string::npos)
    return s;
  std::string out = "\"";
  for (const char c : s) {
    if (c == '"')
      out += '"';
    out += c;
  }
  out += '"';
  return out;
}

void apply_smoke_defaults(Options &o) {
  if (o.index.empty())
    o.index = "baseline1_smoke.bin";
  if (o.query_count > 50)
    o.query_count = 50;
  o.ef_search = std::max<std::size_t>(o.ef_search, o.k);
  o.warmup = 5;
  if (o.transport == "mmap" && o.dim == 0)
    o.dim = 16;
}

} // namespace

int main(int argc, char **argv) {
  try {
    Options o = parse(argc, argv);
    if (o.help) {
      print_usage();
      return 0;
    }
    if (o.smoke)
      apply_smoke_defaults(o);
    if (o.transport == "mmap" && o.index.empty())
      throw std::invalid_argument("mmap mode needs --index");

    const bool inner = o.distance == "ip";
    const auto t_start = std::chrono::steady_clock::now();

    // --- index: build a tiny one for smoke runs if necessary ---------------
    if (o.smoke && o.transport == "mmap" &&
        !std::ifstream(o.index).good()) {
      auto data = rh::generate_vectors(2000, o.dim, o.seed);
      rh::HnswBuilder::build_file(o.index, data, o.M > 0 ? o.M : 8,
                                  o.ef_construction, o.seed);
    }

    rh::ComputeNodeConfig cfg;
    cfg.index_path = o.index;
    cfg.transport = o.transport == "rdma" ? rh::TransportKind::Rdma
                                          : rh::TransportKind::Mmap;
    cfg.host = o.host;
    cfg.port = o.port;
    cfg.read_policy = o.policy;
    cfg.distance = inner ? rh::DistanceKind::InnerProduct : rh::DistanceKind::L2;
    cfg.ef_search = o.ef_search;
    cfg.k = o.k;

    rh::ComputeNode node(cfg);
    // Re-install the configured strategy through the same factory the CLI uses,
    // so the experiment can never drift from the production wiring.
    auto inject = [&node, &o] {
      node.query_engine().set_read_strategy(rh::make_read_strategy(o.policy));
    };
    inject();

    const auto catalog = node.query_engine().catalog();
    const std::size_t dim = catalog.header.dim;
    const std::size_t start_nodes = catalog.header.node_count;

    // --- queries -----------------------------------------------------------
    const auto queries =
        o.queries.empty()
            ? rh::generate_vectors(o.query_count, dim, o.seed)
            : rh::read_fvecs(o.queries, dim);
    const auto base =
        o.base.empty() ? std::vector<rh::Vector>{}
                       : rh::read_fvecs(o.base, dim);
    const auto gt =
        o.gt.empty() ? std::vector<std::vector<rh::NodeId>>{}
                     : read_ground_truth(o.gt, o.k);

    const auto updates =
        o.updates.empty()
            ? rh::generate_vectors(o.update_count, dim, o.seed + 1)
            : rh::read_fvecs(o.updates, dim);

    const auto update_code =
        o.update_op == "insert"
            ? rh::update_op::Insert
            : (o.update_op == "erase" ? rh::update_op::Erase
                                      : rh::update_op::Upsert);

    // --- warm-up -----------------------------------------------------------
    for (std::size_t i = 0; i < o.warmup && i < queries.size(); ++i)
      (void)node.query(queries[i]);

    const std::size_t update_interval =
        o.update_op == "none"
            ? 0
            : std::max<std::size_t>(
                  1, static_cast<std::size_t>(
                         1.0 / std::max(0.0001, o.update_ratio)));

    // --- measured loop -----------------------------------------------------
    std::vector<Sample> samples;
    samples.reserve(queries.size());
    Totals totals;
    double recall_sum = 0.0;
    std::uint64_t recall_queries = 0;
    std::uint64_t updates_applied = 0;

    const auto loop_start = std::chrono::steady_clock::now();
    for (std::size_t i = 0; i < queries.size(); ++i) {
      const rh::ReadMetrics before = node.query_engine().stats().reads;
      const auto t0 = std::chrono::steady_clock::now();
      const auto ids = node.query(queries[i]);
      const auto t1 = std::chrono::steady_clock::now();
      const rh::ReadMetrics after = node.query_engine().stats().reads;

      Sample s;
      s.latency_us =
          std::chrono::duration<double, std::micro>(t1 - t0).count();
      s.m = diff_metrics(after, before);
      samples.push_back(s);
      add(totals, s.m);

      // Recall: exact recall if a base is given, else precomputed ground truth.
      if (!base.empty() || !gt.empty()) {
        std::vector<rh::NodeId> truth;
        if (!base.empty())
          truth = exact_topk(queries[i], base, o.k, inner);
        else if (i < gt.size())
          truth.assign(gt[i].begin(), gt[i].end());
        if (!truth.empty()) {
          const std::unordered_set<rh::NodeId> ts(truth.begin(), truth.end());
          for (const auto id : ids)
            if (ts.contains(id))
              recall_sum += 1.0 / static_cast<double>(std::max<std::size_t>(1, o.k));
          ++recall_queries;
        }
      }

      if (update_interval && (i % update_interval) == 0) {
        const auto ui = i / update_interval;
        if (ui < updates.size()) {
          const auto count = node.query_engine().catalog().header.node_count;
          const rh::NodeId id =
              update_code == rh::update_op::Insert
                  ? static_cast<rh::NodeId>(count + ui)
                  : static_cast<rh::NodeId>(ui % count);
          const rh::UpdateRequest req{
              update_code, id,
              update_code == rh::update_op::Erase ? rh::Vector{} : updates[ui % updates.size()]};
          const auto r = node.update(req);
          if (r.ok) {
            ++updates_applied;
            inject(); // reload() rebuilt the engine; re-install Baseline 1
          }
        }
      }
    }
    const auto loop_end = std::chrono::steady_clock::now();
    const double loop_ms =
        std::chrono::duration<double, std::milli>(loop_end - loop_start).count();

    // --- aggregate ---------------------------------------------------------
    std::vector<double> lat;
    lat.reserve(samples.size());
    for (const auto &s : samples)
      lat.push_back(s.latency_us);
    const double latency_total_us =
        std::accumulate(lat.begin(), lat.end(), 0.0);
    const double mean_us =
        lat.empty() ? 0.0 : latency_total_us / static_cast<double>(lat.size());
    // "Query throughput": derived from the per-query latencies only, so it is
    // comparable across strategies regardless of how much update work a mixed
    // workload interleaves.  The end-to-end rate (updates included) is
    // reported separately as qps_end_to_end.
    const double qps =
        latency_total_us > 0 ? 1e6 * samples.size() / latency_total_us : 0.0;
    const double qps_end_to_end =
        loop_ms > 0 ? 1000.0 * samples.size() / loop_ms : 0.0;
    const double wall_s =
        std::chrono::duration<double>(std::chrono::steady_clock::now() - t_start)
            .count();
    const double waste_ratio =
        totals.requested ? static_cast<double>(totals.wasted) / totals.requested
                         : 0.0;
    const double cap_util =
        totals.capacity_sum
            ? static_cast<double>(totals.degree_sum) / totals.capacity_sum
            : 0.0;
    const double avg_degree =
        totals.layer_reads
            ? static_cast<double>(totals.degree_sum) / totals.layer_reads
            : 0.0;
    const double neighbor_share =
        (totals.requested + totals.node_bytes)
            ? static_cast<double>(totals.requested) /
                  (totals.requested + totals.node_bytes)
            : 0.0;
    const double recall =
        recall_queries ? recall_sum / recall_queries : 0.0;

    // --- CSV: per-query samples -------------------------------------------
    if (!o.out.empty()) {
      std::ofstream out(o.out, std::ios::trunc);
      if (!out)
        throw std::runtime_error("cannot write --out file: " + o.out);
      out << "query_idx,latency_us,requested_bytes,useful_bytes,wasted_bytes,"
             "read_ops,header_reads,dependent_reads,zero_degree_layers,"
             "failed_reads,node_bytes,layer_reads,degree_sum,capacity_sum\n";
      for (std::size_t i = 0; i < samples.size(); ++i) {
        const auto &s = samples[i];
        out << i << ',' << s.latency_us << ',' << s.m.requested_bytes << ','
            << s.m.useful_bytes << ',' << s.m.wasted_bytes << ','
            << s.m.read_ops << ',' << s.m.header_reads << ','
            << s.m.dependent_reads << ',' << s.m.zero_degree_layers << ','
            << s.m.failed_reads << ',' << s.m.node_bytes << ','
            << s.m.layer_reads << ',' << s.m.degree_sum << ','
            << s.m.capacity_sum << '\n';
      }
    }

    // --- CSV: one summary row ---------------------------------------------
    if (!o.summary_out.empty()) {
      const bool fresh = !std::ifstream(o.summary_out).good();
      std::ofstream out(o.summary_out, std::ios::app);
      if (!out)
        throw std::runtime_error("cannot write --summary-out file: " +
                                 o.summary_out);
      if (fresh)
        out << "strategy,dataset,label,dim,node_count_start,node_count_end,M,"
               "ef_construction,ef_search,k,transport,update_op,update_ratio,"
               "queries,updates,qps,qps_end_to_end,wall_s,latency_mean_us,"
               "latency_p50_us,latency_p95_us,latency_p99_us,requested_bytes,"
               "useful_bytes,wasted_bytes,waste_ratio,node_bytes,"
               "neighbor_share,read_ops,layer_reads,header_reads,"
               "dependent_reads,zero_degree_layers,failed_reads,"
               "capacity_utilization,avg_degree,recall\n";
      const auto end_nodes = node.query_engine().catalog().header.node_count;
      out << strategy_label(o.policy) << ',' << csv_escape(o.dataset) << ','
          << csv_escape(o.label) << ',' << dim << ',' << start_nodes << ','
          << end_nodes << ',' << catalog.header.M << ',' << o.ef_construction
          << ',' << o.ef_search << ',' << o.k << ',' << o.transport << ','
          << o.update_op << ',' << o.update_ratio << ',' << samples.size() << ','
          << updates_applied << ',' << qps << ',' << qps_end_to_end << ','
          << wall_s << ',' << mean_us << ',' << percentile(lat, 0.50) << ','
          << percentile(lat, 0.95) << ',' << percentile(lat, 0.99) << ','
          << totals.requested << ',' << totals.useful << ',' << totals.wasted
          << ',' << waste_ratio << ',' << totals.node_bytes << ','
          << neighbor_share << ',' << totals.read_ops << ',' << totals.layer_reads
          << ',' << totals.header_reads << ',' << totals.dependent_reads << ','
          << totals.zero_degree_layers << ',' << totals.failed_reads << ','
          << cap_util << ',' << avg_degree << ',' << recall << '\n';
    }

    // --- stdout summary ----------------------------------------------------
    std::cout << "strategy=" << strategy_label(o.policy)
              << " dim=" << dim << " M=" << catalog.header.M
              << " ef_search=" << o.ef_search << " k=" << o.k
              << " update_op=" << o.update_op
              << " update_ratio=" << o.update_ratio << '\n'
              << "queries=" << samples.size()
              << " updates=" << updates_applied << " qps=" << qps
              << " qps_end_to_end=" << qps_end_to_end
              << " latency_mean_us=" << mean_us
              << " latency_p50_us=" << percentile(lat, 0.50)
              << " latency_p95_us=" << percentile(lat, 0.95)
              << " latency_p99_us=" << percentile(lat, 0.99) << '\n'
              << "requested_bytes=" << totals.requested
              << " useful_bytes=" << totals.useful
              << " wasted_bytes=" << totals.wasted
              << " waste_ratio=" << waste_ratio
              << " node_bytes=" << totals.node_bytes
              << " neighbor_share=" << neighbor_share
              << " read_ops=" << totals.read_ops
              << " layer_reads=" << totals.layer_reads
              << " header_reads=" << totals.header_reads
              << " dependent_reads=" << totals.dependent_reads
              << " zero_degree_layers=" << totals.zero_degree_layers << '\n'
              << "capacity_utilization=" << cap_util
              << " avg_degree=" << avg_degree << " recall=" << recall
              << " wall_s=" << wall_s << '\n';
    if (totals.requested != totals.useful + totals.wasted)
      throw std::runtime_error(
          "neighbor byte identity violated: requested != useful + wasted");
    if (o.policy == rh::ReadPolicy::HeaderActual &&
        totals.read_ops != totals.header_reads + totals.dependent_reads)
      throw std::runtime_error(
          "Baseline 2 identity violated: read_ops != header_reads + "
          "dependent_reads");
    return 0;
  } catch (const std::exception &e) {
    std::cerr << "baseline1_experiment: " << e.what() << '\n';
    return 1;
  }
}
