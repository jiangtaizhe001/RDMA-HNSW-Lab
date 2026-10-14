cmake_minimum_required(VERSION 3.18)
project(rdma_hnsw_lab LANGUAGES CXX)

option(ENABLE_RDMA "Build the ibverbs transport" ON)
option(ENABLE_RDMA_CM "Build the librdmacm connection backend" ON)
option(BUILD_TESTING "Build unit tests" ON)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

find_package(Threads REQUIRED)
add_library(rdma_hnsw_core
  src/common/config.cc
  src/common/data.cc
  src/graph/graph_store.cc
  src/graph/hnsw_builder.cc
  src/transport/mmap_transport.cc
  src/read/read_strategy.cc
  src/runtime/storage_engine.cc
  src/runtime/query_engine.cc)
target_include_directories(rdma_hnsw_core PUBLIC include third_party)
target_link_libraries(rdma_hnsw_core PUBLIC Threads::Threads)
target_compile_options(rdma_hnsw_core PRIVATE -Wall -Wextra -Wpedantic)

add_executable(hnsw_build src/hnsw_build.cc)
target_link_libraries(hnsw_build PRIVATE rdma_hnsw_core)
add_executable(storage_server src/storage_server.cc)
add_executable(compute_client src/compute_client.cc)

add_library(rdma_hnsw_runtime
  src/runtime/storage_node.cc
  src/runtime/compute_node.cc)
target_include_directories(rdma_hnsw_runtime PUBLIC include third_party)
target_link_libraries(rdma_hnsw_runtime PUBLIC rdma_hnsw_core)
target_compile_options(rdma_hnsw_runtime PRIVATE -Wall -Wextra -Wpedantic)

add_executable(baseline1_experiment experiments/baseline1_main.cc)
target_link_libraries(baseline1_experiment PRIVATE rdma_hnsw_runtime)
target_compile_options(baseline1_experiment PRIVATE -Wall -Wextra -Wpedantic)

target_link_libraries(storage_server PRIVATE rdma_hnsw_runtime)
target_link_libraries(compute_client PRIVATE rdma_hnsw_runtime)

include(GNUInstallDirs)
install(TARGETS rdma_hnsw_core rdma_hnsw_runtime
        ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
        LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
        RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
install(DIRECTORY include/rdma_hnsw DESTINATION ${CMAKE_INSTALL_INCLUDEDIR})

if(ENABLE_RDMA)
  find_path(IBVERBS_INCLUDE_DIR infiniband/verbs.h)
  find_library(IBVERBS_LIBRARY NAMES ibverbs)
  # Prefer the system shared provider.  Some development environments expose
  # a static /usr/local/libibverbs.a that requires private libnl internals.
  find_file(IBVERBS_SHARED_LIBRARY NAMES libibverbs.so
    PATHS /lib /lib/x86_64-linux-gnu /usr/lib /usr/lib/x86_64-linux-gnu
    NO_DEFAULT_PATH)
  if(IBVERBS_SHARED_LIBRARY)
    set(IBVERBS_LIBRARY "${IBVERBS_SHARED_LIBRARY}")
  endif()
  if(IBVERBS_INCLUDE_DIR AND IBVERBS_LIBRARY)
    add_library(rdma_hnsw_rdma src/transport/rdma_transport.cc)
    target_include_directories(rdma_hnsw_rdma PUBLIC include ${IBVERBS_INCLUDE_DIR})
    target_link_libraries(rdma_hnsw_rdma PUBLIC rdma_hnsw_core ${IBVERBS_LIBRARY} Threads::Threads)
    target_compile_definitions(rdma_hnsw_rdma PUBLIC RDMA_HNSW_HAS_IBVERBS=1)
    target_compile_options(rdma_hnsw_rdma PRIVATE -Wall -Wextra -Wpedantic)
    target_link_libraries(storage_server PRIVATE rdma_hnsw_rdma)
    target_link_libraries(compute_client PRIVATE rdma_hnsw_rdma)
    target_link_libraries(rdma_hnsw_runtime PUBLIC rdma_hnsw_rdma)
    install(TARGETS rdma_hnsw_rdma
            ARCHIVE DESTINATION ${CMAKE_INSTALL_LIBDIR}
            LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}
            RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
    message(STATUS "ibverbs transport enabled")
  else()
    message(WARNING "ibverbs headers/library not found; mmap transport remains available")
    target_sources(rdma_hnsw_core PRIVATE src/transport/rdma_stub.cc)
  endif()
else()
  target_sources(rdma_hnsw_core PRIVATE src/transport/rdma_stub.cc)
endif()

# Public umbrella target: applications that use ComputeNode/StorageNode can
# link one target and receive the selected transport implementation.
add_library(rdma_hnsw INTERFACE)
target_link_libraries(rdma_hnsw INTERFACE rdma_hnsw_runtime)

if(ENABLE_RDMA_CM AND TARGET rdma_hnsw_rdma)
  find_path(RDMACM_INCLUDE_DIR rdma/rdma_cma.h)
  find_library(RDMACM_LIBRARY NAMES rdmacm)
  if(RDMACM_INCLUDE_DIR AND RDMACM_LIBRARY)
    target_sources(rdma_hnsw_rdma PRIVATE src/transport/rdma_cm_transport.cc)
    target_include_directories(rdma_hnsw_rdma PRIVATE ${RDMACM_INCLUDE_DIR})
    target_link_libraries(rdma_hnsw_rdma PUBLIC ${RDMACM_LIBRARY})
    target_compile_definitions(rdma_hnsw_rdma PUBLIC RDMA_HNSW_HAS_RDMACM=1)
    set(RDMA_CM_AVAILABLE TRUE)
    message(STATUS "librdmacm connection backend enabled")
  else()
    message(WARNING "librdmacm development files not found; CM backend unavailable")
  endif()
endif()
if(NOT RDMA_CM_AVAILABLE)
  target_sources(rdma_hnsw_core PRIVATE src/transport/rdma_cm_stub.cc)
endif()

include(CTest)
if(BUILD_TESTING)
  add_executable(cm_protocol_test tests/cm_protocol_test.cc)
  target_link_libraries(cm_protocol_test PRIVATE rdma_hnsw_core)
  add_test(NAME cm_protocol_test COMMAND cm_protocol_test)
  if(RDMA_CM_AVAILABLE)
    add_executable(cm_integration_test tests/cm_integration_test.cc)
    target_link_libraries(cm_integration_test PRIVATE rdma_hnsw)
    add_test(NAME cm_integration_test COMMAND cm_integration_test)
    set_tests_properties(cm_integration_test PROPERTIES SKIP_RETURN_CODE 77 TIMEOUT 120)
  endif()
  add_executable(graph_store_test tests/graph_store_test.cc)
  target_link_libraries(graph_store_test PRIVATE rdma_hnsw_core)
  add_executable(read_strategy_test tests/read_strategy_test.cc)
  target_link_libraries(read_strategy_test PRIVATE rdma_hnsw_core)
  add_executable(hnsw_builder_test tests/hnsw_builder_test.cc)
  target_link_libraries(hnsw_builder_test PRIVATE rdma_hnsw_core)
  add_executable(runtime_api_test tests/runtime_api_test.cc)
  target_link_libraries(runtime_api_test PRIVATE rdma_hnsw)
  add_test(NAME graph_store_test COMMAND graph_store_test)
  add_test(NAME read_strategy_test COMMAND read_strategy_test)
  add_test(NAME hnsw_builder_test COMMAND hnsw_builder_test)
  add_test(NAME runtime_api_test COMMAND runtime_api_test)
  # Baseline 1 experiment smoke test: build a tiny index on the fly, run a few
  # queries, and export CSV.  Proves the measurement/export path is wired up.
  add_test(NAME baseline1_experiment_smoke
           COMMAND baseline1_experiment --smoke
                   --index ${CMAKE_BINARY_DIR}/baseline1_smoke.bin
                   --out ${CMAKE_BINARY_DIR}/baseline1_smoke.csv
                   --summary-out ${CMAKE_BINARY_DIR}/baseline1_smoke_summary.csv)
  # Baseline 2 (header-first) smoke test: same driver, header_actual strategy.
  add_test(NAME baseline2_experiment_smoke
           COMMAND baseline1_experiment --smoke --read-strategy header_actual
                   --index ${CMAKE_BINARY_DIR}/baseline2_smoke.bin
                   --out ${CMAKE_BINARY_DIR}/baseline2_smoke.csv
                   --summary-out ${CMAKE_BINARY_DIR}/baseline2_smoke_summary.csv)
  # Existing tests use assert(), including operations inside the assertions.
  # Keep them active when students use the default Release build.
  foreach(test_target cm_protocol_test graph_store_test read_strategy_test hnsw_builder_test runtime_api_test)
    target_compile_options(${test_target} PRIVATE $<$<CXX_COMPILER_ID:GNU,Clang>:-UNDEBUG>)
  endforeach()
endif()
