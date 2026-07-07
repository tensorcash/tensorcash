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

# libargon2 — v3 admission puzzle (TIP-0003). Optional at build
# time: without it the module still builds, but pow_v3::argon2id_digest (and
# therefore admission_grind / v3 admission verification) throws at runtime.
find_library(ARGON2_LIB argon2
    HINTS /opt/homebrew/opt/argon2/lib /usr/local/opt/argon2/lib
)
find_path(ARGON2_INCLUDE_DIR argon2.h
    HINTS /opt/homebrew/opt/argon2/include /usr/local/opt/argon2/include
)
if(NOT ARGON2_LIB OR NOT ARGON2_INCLUDE_DIR)
    # pkg-config fallback (Linux distro packages ship libargon2.pc)
    pkg_check_modules(ARGON2_PC libargon2)
    if(ARGON2_PC_FOUND)
        set(ARGON2_LIB ${ARGON2_PC_LINK_LIBRARIES})
        set(ARGON2_INCLUDE_DIR ${ARGON2_PC_INCLUDE_DIRS})
    endif()
endif()

# Main proof_processor module
pybind11_add_module(proof_processor
    proof_processor.cpp
    proof_processor_bindings.cpp
    pow_zmq_writer.cpp
    pow_utils.cpp
    pow_v3.cpp
    bcred_table_r1024.cpp
)

# Link dependencies
target_link_libraries(proof_processor PRIVATE
    proofpack
    ${ZMQ_LIBRARIES}
    OpenSSL::SSL
    OpenSSL::Crypto
    pthread
)

if(ARGON2_LIB AND ARGON2_INCLUDE_DIR)
    target_compile_definitions(proof_processor PRIVATE POW_V3_HAVE_ARGON2)
    target_include_directories(proof_processor PRIVATE ${ARGON2_INCLUDE_DIR})
    target_link_libraries(proof_processor PRIVATE ${ARGON2_LIB})
else()
    message(WARNING
        "libargon2 not found — building WITHOUT v3 admission support: "
        "pow_v3::argon2id_digest/admission_grind will throw at runtime. "
        "Install libargon2 (brew install argon2 / apt install libargon2-dev) "
        "for v3 mining and verification.")
endif()

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