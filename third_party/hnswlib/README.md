# hnswlib

This directory contains the hnswlib headers used by `hnsw_build`. The headers
are vendored so that students use the same HNSW implementation on every node.
The exporter reads the public `HierarchicalNSW` storage fields to create the
project's stable `GraphStore` records. Runtime query code does not depend on
hnswlib pointers or its private allocation layout.

Upstream project: https://github.com/nmslib/hnswlib

The upstream headers are distributed under the Apache License 2.0. Consult
the upstream repository for the complete license text and notices.
