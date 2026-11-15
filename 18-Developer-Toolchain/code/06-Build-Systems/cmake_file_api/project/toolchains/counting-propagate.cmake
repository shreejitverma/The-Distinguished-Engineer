include("${CMAKE_CURRENT_LIST_DIR}/counting.cmake")
# Ask try_compile() to forward LAB_TC_MODE into its generated test projects.
set(CMAKE_TRY_COMPILE_PLATFORM_VARIABLES LAB_TC_MODE)
