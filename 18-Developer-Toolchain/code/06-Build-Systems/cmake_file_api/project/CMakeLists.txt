cmake_minimum_required(VERSION 3.25)
project(PresetsLab LANGUAGES CXX)

include(CheckCXXSourceCompiles)
check_cxx_source_compiles("int main() { return 0; }" LAB_HAVE_TRIVIAL)
if(NOT DEFINED CACHE{LAB_CHECK_LOGGED})
  message(CONFIGURE_LOG "lab check summary\nLAB_HAVE_TRIVIAL=${LAB_HAVE_TRIVIAL}\n")
  set(LAB_CHECK_LOGGED 1 CACHE INTERNAL "")
endif()

# The configure preset's environment is visible here and nowhere in the cache.
file(WRITE "${CMAKE_BINARY_DIR}/lab-env.txt" "$ENV{LAB_PATH}")

set(LAB_LEVEL 3 CACHE STRING "Value of the CORE_LEVEL definition")

add_library(core STATIC core.cpp)
target_compile_definitions(core PUBLIC CORE_LEVEL=${LAB_LEVEL})
target_compile_features(core PUBLIC cxx_std_20)

add_library(iface INTERFACE)

add_executable(app main.cpp)
target_link_libraries(app PRIVATE core iface)

enable_testing()
add_test(NAME app COMMAND app)
