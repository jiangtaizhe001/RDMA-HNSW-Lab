cmake_minimum_required(VERSION 3.18)
project(rdma_hnsw_example LANGUAGES CXX)

set(RDMA_HNSW_LAB_ROOT "${CMAKE_CURRENT_LIST_DIR}/.." CACHE PATH "RDMA-HNSW-lab source directory")
add_subdirectory("${RDMA_HNSW_LAB_ROOT}" "${CMAKE_BINARY_DIR}/rdma_hnsw_lab" EXCLUDE_FROM_ALL)

add_executable(mmap_client mmap_client.cc)
target_link_libraries(mmap_client PRIVATE rdma_hnsw)
target_compile_features(mmap_client PRIVATE cxx_std_20)

add_executable(rdma_client rdma_client.cc)
target_link_libraries(rdma_client PRIVATE rdma_hnsw)
target_compile_features(rdma_client PRIVATE cxx_std_20)
