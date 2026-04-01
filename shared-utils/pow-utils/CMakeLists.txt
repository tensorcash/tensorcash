cmake_minimum_required(VERSION 3.14)
project(proof_processor)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# Find Python and pybind11
find_package(Python3 REQUIRED COMPONENTS Interpreter Development)
find_package(pybind11 REQUIRED)

# Find dependencies
find_package(PkgConfig REQUIRED)
pkg_check_modules(ZMQ REQUIRED libzmq)
find_package(OpenSSL REQUIRED)

# Find or set FlatBuffers
if(NOT FLATBUFFERS_INCLUDE_DIRS)
    # Try to find FlatBuffers
    find_path(FLATBUFFERS_INCLUDE_DIRS 
        NAMES flatbuffers/flatbuffers.h
        PATHS /usr/include /usr/local/include
    )
    if(NOT FLATBUFFERS_INCLUDE_DIRS)
        # Use local version if available
        set(FLATBUFFERS_INCLUDE_DIRS "${CMAKE_CURRENT_SOURCE_DIR}/tests/build/flatbuffers-headers")
    endif()
endif()

# Set path for generated headers
set(GENERATED_HEADERS_DIR "${CMAKE_CURRENT_SOURCE_DIR}/tests/build/generated-headers")

# Ensure generated headers exist
if(NOT EXISTS "${GENERATED_HEADERS_DIR}/proof_generated.h")
    message(FATAL_ERROR "Generated headers not found. Run build_pfunpack_simple.sh first to generate FlatBuffer headers.")
endif()

# Shared proof packing library
add_library(proofpack STATIC
    pfunpack/libproofpack.cpp
)

target_include_directories(proofpack PUBLIC
    ${CMAKE_CURRENT_SOURCE_DIR}/pfunpack
    ${FLATBUFFERS_INCLUDE_DIRS}
    ${GENERATED_HEADERS_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}
)

# Main proof_processor module
pybind11_add_module(proof_processor
    proof_processor.cpp
    proof_processor_bindings.cpp
    pow_zmq_writer.cpp
    pow_utils.cpp
)

# Link dependencies
target_link_libraries(proof_processor PRIVATE
    proofpack
    ${ZMQ_LIBRARIES}
    OpenSSL::SSL
    OpenSSL::Crypto
    pthread
)

# Include directories
target_include_directories(proof_processor PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}/pfunpack
    ${ZMQ_INCLUDE_DIRS}
    ${FLATBUFFERS_INCLUDE_DIRS}
    ${GENERATED_HEADERS_DIR}
    ${OPENSSL_INCLUDE_DIR}
)

# Set RPATH for the module to find dependencies
set_target_properties(proof_processor PROPERTIES
    INSTALL_RPATH_USE_LINK_PATH TRUE
)

# Optional: Install target
install(TARGETS proof_processor
    LIBRARY DESTINATION ${Python3_SITELIB}
)