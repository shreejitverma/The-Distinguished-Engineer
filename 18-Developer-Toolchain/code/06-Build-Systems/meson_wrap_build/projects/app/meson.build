project('app', 'c', version: '1.0', meson_version: '>=1.12.0')

# No fallback: keyword. subprojects/mylib/ is a directory without a wrap, so
# Meson treats it as an implicit provider of the dependency name 'mylib'.
mylib_dep = dependency('mylib')

executable('app', 'main.c', dependencies: mylib_dep)
