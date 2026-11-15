# Appends one line per read, so the lab can count how often CMake evaluates a toolchain file.
if(CMAKE_BINARY_DIR MATCHES "/CMakeFiles/CMakeScratch/")
  set(_lab_ctx "try_compile")
else()
  set(_lab_ctx "main")
endif()
file(APPEND "$ENV{LAB_TC_LOG}" "${_lab_ctx} LAB_TC_MODE=[${LAB_TC_MODE}]\n")
