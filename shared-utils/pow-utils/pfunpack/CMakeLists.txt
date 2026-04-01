cmake_minimum_required(VERSION 3.14)
project(pfunpack)

# Locate pybind11 via its CMake config
find_package(pybind11 REQUIRED)

# Allow overriding where FlatBuffers is installed
set(FLATBUFFERS_INSTALL_DIR "/usr/local"
    CACHE PATH "Root prefix of your FlatBuffers install")

# Find FlatBuffers headers and library
find_path(FLATBUFFERS_INCLUDE_DIR
  NAMES flatbuffers/flatbuffers.h
  HINTS "${FLATBUFFERS_INSTALL_DIR}/include"
)

find_library(FLATBUFFERS_LIBRARY
  NAMES flatbuffers
  HINTS "${FLATBUFFERS_INSTALL_DIR}/lib"
)

if(NOT FLATBUFFERS_INCLUDE_DIR OR NOT FLATBUFFERS_LIBRARY)
  message(FATAL_ERROR
    "Could not locate FlatBuffers.\n"
    "FLATBUFFERS_INSTALL_DIR='${FLATBUFFERS_INSTALL_DIR}'\n"
    "Looked for headers in '${FLATBUFFERS_INCLUDE_DIR}' and\n"
    "library in '${FLATBUFFERS_LIBRARY}'."
  )
endif()

# Build the Python extension
pybind11_add_module(pfunpack
  pfunpack.cpp
)

# Include FlatBuffers headers and this source dir for generated *_generated.h
target_include_directories(pfunpack
  PRIVATE
    ${FLATBUFFERS_INCLUDE_DIR}
    ${CMAKE_CURRENT_SOURCE_DIR}
)

# Link against the FlatBuffers library
target_link_libraries(pfunpack
  PRIVATE
    ${FLATBUFFERS_LIBRARY}
)

# Ensure the output is named exactly pfunpack.so (no lib prefix)
set_target_properties(pfunpack PROPERTIES
  PREFIX ""
  OUTPUT_NAME "pfunpack"
)

