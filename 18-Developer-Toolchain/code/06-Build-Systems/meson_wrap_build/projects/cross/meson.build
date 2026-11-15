project('cross', 'c', meson_version: '>=1.12.0')

message('is_cross_build=@0@ can_run_host_binaries=@1@'.format(
  meson.is_cross_build(), meson.can_run_host_binaries()))
message('build_machine=@0@/@1@ host_machine=@2@/@3@'.format(
  build_machine.system(), build_machine.cpu_family(),
  host_machine.system(), host_machine.cpu_family()))

# native: true compiles gen with the build machine's compiler, so it can run
# during the build even when the host binaries cannot.
gen = executable('gen', 'gen.c', native: true)
table_h = custom_target('table',
  output: 'table.h',
  command: [gen, '@OUTPUT@'])

executable('app', 'app.c', table_h)
