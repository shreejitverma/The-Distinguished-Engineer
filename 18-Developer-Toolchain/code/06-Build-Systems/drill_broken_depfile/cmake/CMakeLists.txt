cmake_minimum_required(VERSION 3.29)
project(depfile_drill C)

# PYTHON and SCHEMAGEN are passed with -D by the lab driver.
set(gen_header ${CMAKE_CURRENT_BINARY_DIR}/gen/order.h)
add_custom_command(
  OUTPUT ${gen_header}
  COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_CURRENT_BINARY_DIR}/gen
  COMMAND ${PYTHON} ${SCHEMAGEN} --root ${CMAKE_CURRENT_SOURCE_DIR}
          schemas/order.schema gen/order.h gen/order.h.d
  DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/schemas/order.schema ${SCHEMAGEN}
  DEPFILE gen/order.h.d
  COMMENT "GEN gen/order.h"
  VERBATIM)

# Listing the generated header as a source is what gives every object of `app` an
# order-only dependency on it in the Ninja generator.
add_executable(app src/main.c src/report.c ${gen_header})
target_include_directories(app PRIVATE ${CMAKE_CURRENT_BINARY_DIR}/gen)
