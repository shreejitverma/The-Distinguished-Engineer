# A small "monorepo" with one slow code generator, built in two target layouts.
#
#   LAYOUT=monolith  core owns util code, five plain TUs, and the generated schema.h.
#                    net and risk link core, so every object file they compile gets an
#                    order-only edge on schema.h through cmake_object_order_depends_target_core.
#   LAYOUT=split     core_util (no generated sources) and core_schema (schema.h plus the one
#                    TU that includes it). net and risk link only core_util.
#                    SCHEMA_DEPS=util makes core_schema link core_util; SCHEMA_DEPS=none keeps
#                    the generator's target a leaf of the target graph.
#
#   GEN_INCLUDE=global  the generated include directory is added with include_directories(),
#                       so every target can #include "core/schema.h" (legacy leftover).
#   GEN_INCLUDE=target  it is a usage requirement of the target that owns schema.h (PUBLIC on
#                       core_schema, PRIVATE on the monolith core, where only serialize.cpp needs it).
#   FILE_SETS=on        monolith only: core's public headers go in a PUBLIC FILE_SET and policy
#                       CMP0154 is NEW, so the generated schema.h counts as private to core.
#
# The sources themselves (umbrella or precise includes) are written by gen_tree.py.
cmake_minimum_required(VERSION 3.20)
project(bottleneck_drill LANGUAGES CXX)

set(LAYOUT "monolith" CACHE STRING "monolith or split")
set(GEN_INCLUDE "global" CACHE STRING "global or target")
set(SCHEMA_DEPS "none" CACHE STRING "split layout only: util (core_schema links core_util) or none")
set(FILE_SETS "off" CACHE STRING "monolith layout only: on declares core's public headers in a FILE_SET")
if(FILE_SETS STREQUAL "on")
  # CMake 3.28+: in targets that use file sets, generated files not in a PUBLIC or INTERFACE
  # file set are private, so dependents' compiles no longer order-depend on them.
  cmake_policy(SET CMP0154 NEW)
endif()
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Python3 REQUIRED COMPONENTS Interpreter)

set(GEN_DIR "${CMAKE_CURRENT_BINARY_DIR}/gen")
set(SCHEMA_H "${GEN_DIR}/core/schema.h")

# The slow step: a schema compiler, standing in for protoc, flatc, or tablegen on a large input.
add_custom_command(
  OUTPUT "${SCHEMA_H}"
  COMMAND "${Python3_EXECUTABLE}" "${CMAKE_CURRENT_SOURCE_DIR}/tools/gen_schema.py"
          "${CMAKE_CURRENT_SOURCE_DIR}/core/schema.def" "${SCHEMA_H}"
  DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/tools/gen_schema.py"
          "${CMAKE_CURRENT_SOURCE_DIR}/core/schema.def"
  COMMENT "Generating core/schema.h"
  VERBATIM)

set(CORE_PLAIN core/util.cpp)
foreach(i RANGE 0 4)
  list(APPEND CORE_PLAIN core/core_${i}.cpp)
endforeach()
set(NET_SRCS)
set(RISK_SRCS)
foreach(i RANGE 0 7)
  list(APPEND NET_SRCS net/net_${i}.cpp)
  list(APPEND RISK_SRCS risk/risk_${i}.cpp)
endforeach()

if(GEN_INCLUDE STREQUAL "global")
  include_directories("${GEN_DIR}")
endif()

if(LAYOUT STREQUAL "monolith")
  add_library(core STATIC ${CORE_PLAIN} core/serialize.cpp "${SCHEMA_H}")
  if(FILE_SETS STREQUAL "on")
    target_sources(core PUBLIC FILE_SET HEADERS BASE_DIRS "${CMAKE_CURRENT_SOURCE_DIR}"
      FILES core/util.h core/serialize.h)
  else()
    target_include_directories(core PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")
  endif()
  if(GEN_INCLUDE STREQUAL "target")
    target_include_directories(core PRIVATE "${GEN_DIR}")
  endif()
  set(BASE_LIB core)
  set(SCHEMA_LIB core)
elseif(LAYOUT STREQUAL "split")
  add_library(core_util STATIC ${CORE_PLAIN})
  target_include_directories(core_util PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")
  add_library(core_schema STATIC core/serialize.cpp "${SCHEMA_H}")
  target_include_directories(core_schema PUBLIC "${CMAKE_CURRENT_SOURCE_DIR}")
  if(SCHEMA_DEPS STREQUAL "util")
    # Looks harmless, but CMake gives every custom command in core_schema an order-only
    # edge on libcore_util.a, so the generator cannot start until core_util is archived.
    target_link_libraries(core_schema PUBLIC core_util)
  endif()
  if(GEN_INCLUDE STREQUAL "target")
    target_include_directories(core_schema PUBLIC "${GEN_DIR}")
  endif()
  set(BASE_LIB core_util)
  set(SCHEMA_LIB core_schema)
else()
  message(FATAL_ERROR "LAYOUT must be monolith or split, got '${LAYOUT}'")
endif()

add_library(net STATIC ${NET_SRCS})
target_link_libraries(net PUBLIC ${BASE_LIB})
add_library(risk STATIC ${RISK_SRCS})
target_link_libraries(risk PUBLIC ${BASE_LIB})

add_executable(app app/main.cpp)
target_link_libraries(app PRIVATE net risk ${SCHEMA_LIB})
