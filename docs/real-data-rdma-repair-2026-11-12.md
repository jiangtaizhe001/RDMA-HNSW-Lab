# 真实数据流水线故障修复记录（2026-11-12）

对应计划：[real-data-rdma-improvement-plan.md](real-data-rdma-improvement-plan.md)。本文件只记录故障、根因、修复与受影响结果的范围，不修改既有原始结果。

## 1. 故障清单与根因

### F1 100 万节点索引无法通过 RDMA 建立会话

现象（`sift_n1000000_static`，质量阶段第一条运行）：

```
baseline1_experiment: cannot read remote index offset table
```

根因：0.95 质量的 1M 索引 offset 表为 1 000 000 × 8 B = 8 MB，而客户端暂存缓冲区
`kStagingBytes` 只有 1 MiB。`src/transport/rdma_transport.cc` 中分块读取的实现当时
**尚未编译进 `build/` 里的二进制**（工作区有改动、二进制仍是旧版本），于是把 8 MB
一次性作为单个 SGE 提交，超过本地缓冲区长度，READ 完成状态失败；上层又把具体错误
字符串丢弃，只抛出通用信息，因此日志里看不到 RP/长度错误。

修复：

- 重新编译（`cmake --build build`），使已存在的分块读取路径真正生效；
- `src/runtime/compute_node.cc` 在异常中附带 `ReadResult.error`、已读字节数和期望
  字节数，避免再次出现无法定位的连接期失败。

### F2 4.41 GB 的 GIST 1M 索引超出 32 位线上长度

现象：GIST 1M 无法建立会话（其索引 4 412 000 064 B > 2³²−1）。

根因：`WireInfo::size` 为 `std::uint32_t`，服务端 `static_cast<std::uint32_t>` 截断，
客户端看到的上界只剩 117 MB，任何越过该上界的 READ 都被判为越界。

修复：`size` 改为 `std::uint64_t`（两端共用同一二进制，布局一致），服务端/客户端
两侧的构造同步改为 64 位。设备 `max_mr_size = 0xffffffffffffffff`，4.47 GB 注册正常。

### F3 校准被正常的召回波动中途打断

现象（`calibrate_gist_n1000000`）：

```
ValueError: nonmonotonic recall in the calibration ladder
```

在 ef=424 命中 2374、ef=425 命中 2372。原型语义是"最多扩展 ef_search 个节点"，
在近邻距离接近时，稍大的预算可能用一个近邻换掉另一个，命中数出现个位数的抖动。
这不是控制流错误。

修复（`scripts/calibrate_recall_budget.py`）：

- 不再把任何下降当致命错误：记录 `nonmonotonic_points`，只有下降超过
  0.2 % × (查询数 × k) 才中止；
- 预算仍取"实测命中 ≥ 目标的最小预算"，再用独立重复验证稳定性；
- 同时修正一处真实缺陷：重复验证的 run 目录标签原先不含尝试序号，命中
  `completed.json` 后被静默复用，等同于只测了一次；现在每次尝试都有独立目录。

### F4 计时窗口低于 60 s 下限（协议偏差，非崩溃）

现象：`sift_n100000_static`（v1）中最快的配置计时窗口只有 35.7 s。

根因：同一比较单元共用查询条数，而该条数按**最慢**配置的均值时延推算；最慢配置
达到 60 s 时，最快配置自然不足。

修复（`scripts/run_real_rdma_campaign.py`）：

- 条数改按**最快**配置推算，使所有配置都满足 ≥ 60 s 且 ≥ 10 000 条；
- 每轮运行后按逐查询数据计算计时窗口长度，低于下限写入 `status.json.short_windows`
  并在日志告警，便于报告阶段剔除。

影响范围：v1 的 `sift_n100000_static` 仅 B1/G32 等快配置窗口不足；`capacity`（61.5 s）
与 `gist_n100000_static`（124 s）均达标。v1 不删除，另起 `sift_n100000_static_v2`
（34 000 条/轮，实测 60.4–72.6 s）作为主证据。

## 2. 二进制差异对已完成结果的影响

`WireInfo::size` 与分块读取只改变**单次 READ 超过 1 MiB** 的行为；10 万节点索引的
offset 表为 800 KB，从不触发分块，因此已完成的 `sift_n10000_*`、`sift_n100000_*`、
`gist_n100000_*` 结果在旧二进制与新二进制下走同一条代码路径。差异仅在 1M 规模的
连接期，即 F1/F2 本身。

## 3. 修复后的实测成本（用于 1M 协议可行性）

| 规模 | 预算 | 单查询均值（RDMA loopback） | 10 000 条计时窗口 |
| --- | --- | --- | --- |
| SIFT 1M | 58（0.95 校准值） | 4.06 ms | ≈ 41 s（按最快配置推算条数） |
| SIFT 1M | 142（0.99 校准值） | 9.06 ms | ≈ 91 s |
| GIST 1M | 428（0.95 校准值） | 49.5 ms | ≈ 495 s |

SIFT 1M 全矩阵（7 配置 × 2 质量档 × 5 次）预计约 2 h；GIST 1M 因单查询读取量大
（逐节点向量读取 960 维），全矩阵预计约 1 天，按 `--max-seconds` 上限分段续跑。

## 4. 当前运行状态

- 主流水线：`bash scripts/run_real_data_pipeline.sh`，日志
  `out/real_data_rdma/pipeline2.log`，步骤标记在 `out/real_data_rdma/status/`。
- GIST 1M 预算校准（mmap）：`out/real_data_rdma/logs/calibrate_gist_n1000000_repair.log`。
- 已作废但保留的目录：`out/real_data_rdma/gist/n1000000_prerun_failed`（F3 中断）、
  `out/real_data_rdma/sift_n100000_static`（v1，F4 窗口不足）。
