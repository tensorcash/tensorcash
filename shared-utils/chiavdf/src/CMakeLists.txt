CMAKE_MINIMUM_REQUIRED(VERSION 3.14 FATAL_ERROR)

option(BUILD_CHIAVDFC "Build the chiavdfc shared library" OFF)
option(BUILD_PYTHON "Build the python bindings for chiavdf" ON)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

IF(NOT CMAKE_BUILD_TYPE)
  SET(CMAKE_BUILD_TYPE "RELEASE")
ENDIF()

project(chiavdf)

# ========================================
# CRITICAL OPTIMIZATION FLAGS
# ========================================

# Clear ALL default flags to prevent conflicts
set(CMAKE_CXX_FLAGS_RELEASE "")
set(CMAKE_C_FLAGS_RELEASE "")
set(CMAKE_CXX_FLAGS_DEBUG "")
set(CMAKE_C_FLAGS_DEBUG "")
set(CMAKE_CXX_FLAGS_RELWITHDEBINFO "")
set(CMAKE_C_FLAGS_RELWITHDEBINFO "")

#— make tuning conditional on host CPU
# CHIAVDF_MARCH env var overrides -march (default: native).
# Set to "haswell" or "x86-64-v3" for portable builds targeting older CPUs.
if (CMAKE_HOST_SYSTEM_PROCESSOR STREQUAL "x86_64")
  if(DEFINED ENV{CHIAVDF_MARCH})
    set(_MARCH "$ENV{CHIAVDF_MARCH}")
  else()
    set(_MARCH "native")
  endif()
  message(STATUS "→ x86_64 host: -march=${_MARCH}")
  set(OPT_FLAGS_LIST
    -Ofast
    -march=${_MARCH}
    -mtune=generic
    -mbmi2
    -mavx2
    -funroll-loops
    -fomit-frame-pointer
    -DNDEBUG
  )
else()
  message(STATUS "→ non-x86_64 host: using generic optimization flags")
  set(OPT_FLAGS_LIST
    -Ofast
    -DNDEBUG
  )
endif()

# Join them into a single string for CMAKE_FLAGS
string(JOIN " " OPTIMIZATION_FLAGS ${OPT_FLAGS_LIST})

# Apply to both C and C++
set(CMAKE_CXX_FLAGS "${OPTIMIZATION_FLAGS}")
set(CMAKE_C_FLAGS "${OPTIMIZATION_FLAGS}")

# Force assembly optimizations (x86_64 only — ARM64 has no x86 ASM support).
# Disabled on Windows: the inline ASM uses GCC/GAS syntax which is incompatible
# with MSVC, and the MPIR library (used instead of GMP on Windows) has its own
# ASM paths. Enabling both causes hangs/crashes in CheckProofOfTimeNWesolowski.
if (CMAKE_HOST_SYSTEM_PROCESSOR STREQUAL "x86_64" AND NOT WIN32)
  add_definitions(-DSQUARE_ASM=1)
  add_definitions(-DENABLE_ASM=1)
  add_definitions(-DGMP_USE_ASM=1)
  add_definitions(-DFLINT_ENABLE_ASM=1)
endif()

# Debug output to verify flags
message(STATUS "=== OPTIMIZATION FLAGS APPLIED ===")
message(STATUS "CMAKE_CXX_FLAGS: ${CMAKE_CXX_FLAGS}")
message(STATUS "CMAKE_C_FLAGS: ${CMAKE_C_FLAGS}")
message(STATUS "CMAKE_BUILD_TYPE: ${CMAKE_BUILD_TYPE}")
message(STATUS "====================================")

# ========================================
# END OPTIMIZATION SECTION
# ========================================

set(CMAKE_MODULE_PATH
    ${CMAKE_CURRENT_LIST_DIR}/cmake
    ${CMAKE_MODULE_PATH}
)

if(MSVC)
  add_compile_options(/EHsc)
endif()

if(WIN32)
  set(MPIR_LIBRARY_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../mpir_gc_x64")
  set(MPIR_INCLUDE_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../mpir_gc_x64")
  include_directories(
    ${INCLUDE_DIRECTORIES}
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${MPIR_INCLUDE_DIR}
  )
  find_library(MPIR_LIBRARY NAMES mpir PATHS ${MPIR_LIBRARY_DIR} NO_DEFAULT_PATH)
  if(MPIR_LIBRARY)
    message(STATUS "MPIR library found at ${MPIR_LIBRARY}")
    link_libraries(${MPIR_LIBRARY})
  else()
    message(FATAL_ERROR "MPIR library not found")
  endif()

  list(APPEND CMAKE_PREFIX_PATH "${CMAKE_CURRENT_SOURCE_DIR}/../mpir_gc_x64")
else()
  find_package(GMP REQUIRED)
  find_package(GMPXX REQUIRED)

  include_directories(
    ${INCLUDE_DIRECTORIES}
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${GMP_INCLUDE_DIR}
    ${GMPXX_INCLUDE_DIR}
  )
  
  # Verify we're using the correct GMP
  message(STATUS "GMP_LIBRARIES: ${GMP_LIBRARIES}")
  message(STATUS "GMP_INCLUDE_DIR: ${GMP_INCLUDE_DIR}")
endif()

# CMake 3.14+
include(FetchContent)

if(BUILD_PYTHON)
  FetchContent_Declare(
    pybind11-src
    GIT_REPOSITORY https://github.com/pybind/pybind11.git
    GIT_TAG        v2.13.6
  )
  FetchContent_MakeAvailable(pybind11-src)

  # Use add_library instead of pybind11_add_module to avoid pybind11's automatic flags
  add_library(chiavdf MODULE
    ${CMAKE_CURRENT_SOURCE_DIR}/python_bindings/fastvdf.cpp
    ${CMAKE_CURRENT_SOURCE_DIR}/refcode/lzcnt.c
  )

  # Set properties manually instead of using pybind11_add_module
  set_target_properties(chiavdf PROPERTIES
    CXX_VISIBILITY_PRESET "hidden"
    INTERPROCEDURAL_OPTIMIZATION TRUE
    PREFIX "${PYTHON_MODULE_PREFIX}"
    SUFFIX "${PYTHON_MODULE_EXTENSION}"
  )

  # Apply optimization flags as individual compile options to avoid quoting
  target_compile_options(chiavdf PRIVATE ${OPT_FLAGS_LIST})
  
  # Link pybind11 and other libraries
  target_link_libraries(chiavdf PRIVATE pybind11::module ${GMP_LIBRARIES} ${GMPXX_LIBRARIES})
  
  if(UNIX)
    target_link_libraries(chiavdf PRIVATE -pthread)
  endif()
  if (WIN32)
    target_compile_definitions(chiavdf PUBLIC _DISABLE_CONSTEXPR_MUTEX_CONSTRUCTOR)
  endif()
endif()

add_executable(verifier_test
  ${CMAKE_CURRENT_SOURCE_DIR}/verifier_test.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/refcode/lzcnt.c
)
add_executable(stress_test
  ${CMAKE_CURRENT_SOURCE_DIR}/stress_test.cpp
  ${CMAKE_CURRENT_SOURCE_DIR}/refcode/lzcnt.c
)

# Apply optimization flags to executables using the list
target_compile_options(verifier_test PRIVATE ${OPT_FLAGS_LIST})
target_compile_options(stress_test PRIVATE ${OPT_FLAGS_LIST})

target_link_libraries(verifier_test PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES})
target_link_libraries(stress_test PRIVATE ${GMP_LIBRARIES} ${GMPXX_LIBRARIES})

if(UNIX)
  target_link_libraries(verifier_test PRIVATE -pthread)
  target_link_libraries(stress_test PRIVATE -pthread)
endif()

if(BUILD_CHIAVDFC)
  add_library(chiavdfc_shared SHARED
          ${CMAKE_CURRENT_SOURCE_DIR}/c_bindings/c_wrapper.cpp
          ${CMAKE_CURRENT_SOURCE_DIR}/refcode/lzcnt.c
  )
  add_library(chiavdfc_static STATIC
          ${CMAKE_CURRENT_SOURCE_DIR}/c_bindings/c_wrapper.cpp
          ${CMAKE_CURRENT_SOURCE_DIR}/refcode/lzcnt.c
  )
  
  # Apply optimization flags to libraries using the list
  target_compile_options(chiavdfc_shared PRIVATE ${OPT_FLAGS_LIST})
  target_compile_options(chiavdfc_static PRIVATE ${OPT_FLAGS_LIST})
  
  target_link_libraries(chiavdfc_shared ${GMP_LIBRARIES} ${GMPXX_LIBRARIES})
  target_link_libraries(chiavdfc_static ${GMP_LIBRARIES} ${GMPXX_LIBRARIES})

  set_target_properties(chiavdfc_shared PROPERTIES
          OUTPUT_NAME chiavdfc
          LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib/shared$<0:>"
          RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib/shared$<0:>"
  )

  set_target_properties(chiavdfc_static PROPERTIES
          OUTPUT_NAME chiavdfc
          ARCHIVE_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/lib/static$<0:>"
  )
endif()