project('mylib', 'c', version: '0.3.0', meson_version: '>=1.12.0')

# static: true makes a fallback subproject build with default_library=static.
zlib_dep = dependency('zlib', version: '>=1.3', static: true)

inc = include_directories('include')
mylib = static_library('mylib', 'mylib.c',
  include_directories: inc,
  dependencies: zlib_dep)

# The usage requirements travel with the dependency object:
# include path, a compile define, the archive, and zlib transitively.
mylib_dep = declare_dependency(
  include_directories: inc,
  compile_args: ['-DMYLIB_FEATURE=1'],
  link_with: mylib,
  dependencies: zlib_dep)

meson.override_dependency('mylib', mylib_dep)
