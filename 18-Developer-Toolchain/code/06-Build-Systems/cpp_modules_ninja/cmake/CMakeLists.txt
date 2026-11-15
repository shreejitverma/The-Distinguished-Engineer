cmake_minimum_required(VERSION 3.28)
project(cpp_modules_lab LANGUAGES CXX)

add_library(geom STATIC)
target_compile_features(geom PUBLIC cxx_std_20)
target_sources(geom
  PUBLIC
    FILE_SET CXX_MODULES FILES src/geom.cppm src/geom-vec.cppm
  PRIVATE
    src/geom_impl.cpp)

add_executable(app src/main.cpp)
target_sources(app PRIVATE FILE_SET CXX_MODULES FILES src/report.cppm)
target_link_libraries(app PRIVATE geom)
