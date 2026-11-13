#!/usr/bin/env bats
# Tests for fg, bg, jobs and the terminal's foreground process group.
# probe.sh gives the shell under test a fresh pseudo-terminal and fgpg plays the user: it types each
# command line at the shell's prompt and the SUSP character at a running foreground job.
# The jobs are fgpg helpers that log their own role (foreground or background) and every SIGCONT.

setup() {
  load ../lib/lab
  export LAB_SHELL="${LAB_SHELL:-sh}"
  export FGPG="${BATS_TEST_TMPDIR}/fgpg"
  export LOGDIR="${BATS_TEST_TMPDIR}/log"
  mkdir -p "${LOGDIR}"
  "${CC:-cc}" -O2 -Wall -Wextra -Werror "${BATS_TEST_DIRNAME}/fgpg.c" -o "${FGPG}"
  PROBE="${BATS_TEST_DIRNAME}/probe.sh"
  SCRIPT="${BATS_TEST_DIRNAME}/script.sh"
  FAMILY=$(lab_shell_family)
  VERSION=$(lab_shell_version)
}

# assert_posh_has_no_job_control: posh has no -m option and no fg, bg or jobs at all, so the
# terminal sessions cannot run there; assert exactly that instead.
assert_posh_has_no_job_control() {
  run_lab_c 'set -m'
  [ "${status}" -ne 0 ]
  run_lab_c 'fg'
  [ "${status}" -eq 127 ]
  run_lab_c 'bg'
  [ "${status}" -eq 127 ]
  run_lab_c 'jobs'
  [ "${status}" -eq 127 ]
}

# run_session MODE: run a typed session and require the shell to have left through the typed `exit`.
run_session() {
  run_lab "${PROBE}" "$1"
  [ "${status}" -eq 0 ]
  [ "${output}" = child_exit=0 ]
}

# logged LINE [FILE]: fail unless FILE (default session.log) has exactly this line.
logged() {
  grep -qxF -- "$1" "${LOGDIR}/${2:-session.log}"
}

# not_logged PATTERN FILE: fail if a line of FILE matches PATTERN.
not_logged() {
  if grep -q -- "$1" "${LOGDIR}/$2"; then
    return 1
  fi
}

# matches FILE REGEX: fail unless a line of FILE matches the extended regular expression.
matches() {
  grep -Eq -- "$2" "${LOGDIR}/$1"
}

# is_empty FILE: fail unless FILE exists and is empty.
is_empty() {
  [ -e "${LOGDIR}/$1" ]
  [ ! -s "${LOGDIR}/$1" ]
}

# line_count FILE: print the number of lines in FILE.
line_count() {
  awk 'END { print NR }' "${LOGDIR}/$1"
}

# first_cont and last_cont: the first and the last SIGCONT line job a logged.
first_cont() {
  awk '/^cont/ { print; exit }' "${LOGDIR}/a.log"
}
last_cont() {
  awk '/^cont/ { line = $0 } END { print line }' "${LOGDIR}/a.log"
}

# tstp_status: the exit status this shell family reports for a job stopped by SIGTSTP.
# Most shells report 128 + N; yash reports 384 + N; ksh93 and mksh report 0, and it is their own 0:
# the session runs `(exit 5)` just before the job, so an untouched $? would read 5.
# N comes from the C headers (18 on macOS, 20 on Linux), because `kill -l TSTP` is not portable.
tstp_status() {
  local number
  number=$("${FGPG}" tstp)
  case ${FAMILY} in
    bash | dash | busybox | zsh | zsh-sh) echo $((128 + number)) ;;
    yash) echo $((384 + number)) ;;
    ksh93 | mksh) echo 0 ;;
    *) echo unknown ;;
  esac
}

# bash_posix_mode: succeed when the bash under test runs in POSIX mode (invoked as sh).
bash_posix_mode() {
  # shellcheck disable=SC2016 # $SHELLOPTS must expand in the shell under test, not in bats
  run_lab_c 'case :$SHELLOPTS: in *:posix:*) echo posix ;; *) echo default ;; esac'
  [ "${output}" = posix ]
}

# marks_of FILE: one word per job, in job-number order: the number, its + or - mark (or _),
# and R for a running job or S for a stopped one. Example: "1+S 2_R 3-R ".
marks_of() {
  awk '
    /^\[[0-9]+\]/ {
      number = $0
      sub(/^\[/, "", number)
      sub(/\].*/, "", number)
      rest = $0
      sub(/^\[[0-9]+\]/, "", rest)
      mark = "_"
      if (match(rest, /^ *[+-]/)) {
        mark = substr(rest, RLENGTH, 1)
        rest = substr(rest, RLENGTH + 1)
      }
      sub(/^[ \t]*/, "", rest)
      low = tolower(rest)
      state = "?"
      if (low ~ /^(stopped|suspended)/) state = "S"
      else if (low ~ /^running/) state = "R"
      words[number + 0] = number mark state
      if (number + 0 > max) max = number + 0
    }
    END {
      for (i = 1; i <= max; i++) if (i in words) printf "%s ", words[i]
    }' "${LOGDIR}/$1"
}

# pid_of NAME and pgid_of NAME: what the helper NAME published about itself.
pid_of() {
  cut -d' ' -f1 "${LOGDIR}/$1.pid"
}
pgid_of() {
  cut -d' ' -f2 "${LOGDIR}/$1.pid"
}

@test "fg gives a running background job the terminal, waits for it, and returns its exit status" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session fg_running
  # The job started in the background as the leader of its own process group ...
  logged 'start role=bg leader=yes' a.log
  # ... saw itself become the terminal's foreground group, and exited with status 7.
  logged 'role fg' a.log
  logged 'exit write_failed=0' a.log
  logged 'fg_status=7'
  # Once the job is gone the shell's own group owns the terminal again.
  logged 'after_fg tty_fg=shell'
}

@test "fg writes the job's command line to standard output, except in zsh and yash" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session fg_running
  local lines
  is_empty fg.err
  lines=$(line_count fg.out)
  # shellcheck disable=SC2016 # the patterns name the literal text $FGPG, as the shells print it
  case ${FAMILY} in
    bash | mksh | ksh93)
      # POSIX format: "%s\n", <command>
      [ "${lines}" -eq 1 ]
      matches fg.out '^"\$FGPG" job "\$LOGDIR" a bg 7 ?$'
      ;;
    dash | busybox)
      # the same format; these shells print the command back from the parse tree, with braces
      [ "${lines}" -eq 1 ]
      matches fg.out '^"\$\{FGPG\}" job "\$\{LOGDIR\}" a bg 7$'
      ;;
    yash)
      # yash puts the job number in front unless it runs in its POSIX mode
      [ "${lines}" -eq 1 ]
      matches fg.out '^\[1\] "\$\{FGPG\}" job "\$\{LOGDIR\}" a bg 7$'
      ;;
    zsh | zsh-sh)
      # zsh writes the line to the terminal, not to standard output
      [ "${lines}" -eq 0 ]
      matches tty.out '"\$FGPG" job "\$LOGDIR" a bg 7'
      ;;
    *) false ;;
  esac
}

@test "whether fg sends SIGCONT to a job that is already running depends on the shell" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session fg_running
  case ${FAMILY} in
    dash | busybox | mksh | yash)
      logged 'cont role=fg' a.log
      ;;
    bash)
      # bash 5.3 continues the group unconditionally; 3.2 to 5.2 only when the job was stopped
      case ${VERSION} in
        3.* | 4.* | 5.0* | 5.1* | 5.2*) not_logged '^cont' a.log ;;
        *) logged 'cont role=fg' a.log ;;
      esac
      ;;
    zsh | zsh-sh | ksh93)
      not_logged '^cont' a.log
      ;;
    *) false ;;
  esac
}

@test "the SUSP character suspends the foreground job and the shell takes the terminal back" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session suspend
  local want marks
  want=$(tstp_status)
  logged 'start role=fg leader=yes' a.log
  logged "susp_status=${want}"
  logged 'stopped tty_fg=shell'
  # The suspended job is the current job: one line, marked +, in a stopped state.
  marks=$(marks_of jobs_stopped.out)
  [ "${marks}" = "1+S " ]
}

@test "jobs names the state of a suspended job in each shell's own words" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session suspend
  local system
  system=$(uname -s)
  # jobs_stopped.out: stopped by SIGTSTP from the keyboard; jobs_restopped.out: stopped by SIGSTOP.
  case ${FAMILY} in
    bash)
      if bash_posix_mode; then
        matches jobs_stopped.out '^\[1\]\+ +Stopped\(SIGTSTP\) +"'
        matches jobs_restopped.out '^\[1\]\+ +Stopped\(SIGSTOP\) +"'
      else
        matches jobs_stopped.out '^\[1\]\+ +Stopped +"'
        matches jobs_restopped.out '^\[1\]\+ +Stopped +"'
      fi
      ;;
    dash | busybox)
      # the text is strsignal(3) of the stop signal, so it follows the C library
      case ${system} in
        Darwin)
          matches jobs_stopped.out '^\[1\] ?\+ +Suspended: 18 +"'
          matches jobs_restopped.out '^\[1\] ?\+ +Suspended \(signal\): 17 +"'
          ;;
        Linux)
          matches jobs_stopped.out '^\[1\] ?\+ +Stopped +"'
          matches jobs_restopped.out '^\[1\] ?\+ +Stopped \(signal\) +"'
          ;;
        *) false ;;
      esac
      ;;
    mksh)
      matches jobs_stopped.out '^\[1\] \+ Stopped +"'
      matches jobs_restopped.out '^\[1\] \+ Stopped \(signal\) +"'
      ;;
    ksh93)
      matches jobs_stopped.out '^\[1\] \+ Stopped +"'
      matches jobs_restopped.out '^\[1\] \+ Stopped \(SIGSTOP\) +"'
      ;;
    yash)
      matches jobs_stopped.out '^\[1\] \+ Stopped\(SIGTSTP\) +"'
      matches jobs_restopped.out '^\[1\] \+ Stopped\(SIGSTOP\) +"'
      ;;
    zsh | zsh-sh)
      matches jobs_stopped.out '^\[1\]  \+ suspended  "'
      matches jobs_restopped.out '^\[1\]  \+ suspended \(signal\)  "'
      ;;
    *) false ;;
  esac
}

@test "bg sends SIGCONT to a suspended job and leaves the terminal with the shell" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session suspend
  local cont marks
  logged 'bg_status=0'
  # The first SIGCONT reached the job while it was not the terminal's foreground group,
  # and it went on running there.
  cont=$(first_cont)
  [ "${cont}" = 'cont role=bg' ]
  logged 'role bg' a.log
  logged 'after_bg tty_fg=shell'
  marks=$(marks_of jobs_running.out)
  [ "${marks}" = "1+R " ]
  is_empty bg.err
  # shellcheck disable=SC2016 # the patterns name the literal text $FGPG, as the shells print it
  case ${FAMILY} in
    bash)
      # POSIX format: "[%d] %s\n"; bash adds " &", and the current-job mark outside POSIX mode
      if bash_posix_mode; then
        matches bg.out '^\[1\] "\$FGPG" job "\$LOGDIR" a fg 7 &$'
      else
        matches bg.out '^\[1\]\+ "\$FGPG" job "\$LOGDIR" a fg 7 &$'
      fi
      ;;
    dash | busybox | yash) matches bg.out '^\[1\] "\$\{FGPG\}" job "\$\{LOGDIR\}" a fg 7$' ;;
    mksh) matches bg.out '^\[1\] "\$FGPG" job "\$LOGDIR" a fg 7 ?$' ;;
    ksh93) matches bg.out '^\[1\][[:space:]]+"\$FGPG" job "\$LOGDIR" a fg 7 ?&$' ;;
    zsh | zsh-sh)
      is_empty bg.out
      matches tty.out '"\$FGPG" job "\$LOGDIR" a fg 7'
      ;;
    *) false ;;
  esac
}

@test "bg makes the resumed job the one that \$! names, except in dash and BusyBox ash" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session suspend
  case ${FAMILY} in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) logged 'bang_after_bg=job' ;;
    dash | busybox) logged 'bang_after_bg=other' ;;
    *) false ;;
  esac
}

@test "fg makes a stopped job the terminal's foreground group before it sends SIGCONT" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session suspend
  local marks cont count
  # The job was stopped again with `kill -s STOP %%`: the kernel showed it stopped, and so did jobs.
  not_logged 'not_stopped_again' session.log
  marks=$(marks_of jobs_restopped.out)
  [ "${marks}" = "1+S " ]
  # The handler of the second SIGCONT already found the job in the foreground group.
  cont=$(last_cont)
  [ "${cont}" = 'cont role=fg' ]
  count=$(grep -c '^cont' "${LOGDIR}/a.log")
  [ "${count}" -eq 2 ]
  logged 'exit write_failed=0' a.log
  # fg returns the status of the job it waited for, and the shell owns the terminal afterwards.
  logged 'fg_status=7'
  logged 'after_fg tty_fg=shell'
}

@test "only ksh93 and mksh give a suspended job its own terminal settings back on fg" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session ttymode
  # The job cleared ECHO and was suspended; the log says what ECHO was while the shell had the
  # terminal, and what the job found when fg continued it.
  logged 'set echo=off was=on' t.log
  logged 'stopped tty_fg=shell'
  logged 'fg_status=0'
  case ${FAMILY} in
    ksh93 | mksh)
      # saves the job's settings, restores its own, and puts the job's back before SIGCONT
      logged 'while_stopped echo=on'
      logged 'cont role=fg echo=off' t.log
      ;;
    bash | zsh | zsh-sh)
      # restores its own settings and leaves them in place for the resumed job
      logged 'while_stopped echo=on'
      logged 'cont role=fg echo=on' t.log
      ;;
    dash | busybox | yash)
      # never touches the settings: the shell reads its next command with ECHO still off
      logged 'while_stopped echo=off'
      logged 'cont role=fg echo=off' t.log
      ;;
    *) false ;;
  esac
}

@test "the current job (+) is a stopped job whenever one exists, and the previous job (-) too" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session markers
  local running one two
  running=$(marks_of jobs_running.out)
  one=$(marks_of jobs_one_stopped.out)
  two=$(marks_of jobs_two_stopped.out)
  echo "running=${running} one=${one} two=${two}"
  case ${FAMILY} in
    bash | dash | busybox | mksh | ksh93 | yash | zsh | zsh-sh)
      # three running jobs: the newest is current, the one before it previous
      [ "${running}" = "1_R 2-R 3+R " ]
      # job 1 stopped: it becomes current although it is the oldest (POSIX.1-2024 jobs, STDOUT)
      [ "${one}" = "1+S 2_R 3-R " ]
      # jobs 1 and 2 stopped: both marks sit on stopped jobs, the most recently stopped is current
      [ "${two}" = "1-S 2+S 3_R " ]
      ;;
    *) false ;;
  esac
}

@test "jobs -l inserts the process group ID and jobs -p prints nothing else, except in zsh" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session markers
  local name number=0 pid pgid listed wanted lines
  wanted=
  for name in one two three; do
    number=$((number + 1))
    # each job is one process that leads its own group, so the two IDs it published agree
    pid=$(pid_of "${name}")
    pgid=$(pgid_of "${name}")
    [ "${pid}" = "${pgid}" ]
    matches jobs_l.out "^\[${number}\][ +-]*${pgid}[[:space:]]+[Rr]unning"
    # the plain listing does not have the ID
    not_logged "${pgid}" jobs_running.out
    wanted="${wanted}${pgid} "
  done
  case ${FAMILY} in
    bash | dash | busybox | mksh | ksh93 | yash)
      # POSIX format: "%d\n", one process group ID per line
      listed=$(sort -n "${LOGDIR}/jobs_p.out")
      listed="${listed//$'\n'/ } "
      [ "${listed}" = "${wanted}" ]
      ;;
    zsh | zsh-sh)
      # zsh prints its long listing for -p
      lines=$(line_count jobs_p.out)
      [ "${lines}" -eq 3 ]
      pgid=$(pgid_of one)
      matches jobs_p.out "^\[1\] +${pgid} running +\"[$]FGPG\" hold"
      ;;
    *) false ;;
  esac
}

@test "every POSIX job ID form resolves to the right job, with two exceptions" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session jobids
  logged 'numbers=1,2'
  logged '%first=alpha'
  logged '%second=bravo'
  # %+ and %% are the current job (the newer one), %- the previous job
  logged '%+=bravo'
  logged '%%=bravo'
  logged '%-=alpha'
  # a job number or a string that names no job is an error
  logged '%99=none'
  logged '%zulu=none'
  # %string matches the start of the command, %?string any part of it
  logged '%?rav=bravo'
  case ${FAMILY} in
    bash | dash | busybox | yash | zsh | zsh-sh)
      logged '%alp=alpha'
      logged '%?lph=alpha'
      ;;
    mksh)
      # mksh lists the job as `\alpha`, so no command starts with "alp"
      matches jobs.out '^\[1\] - Running +\\alpha ?$'
      logged '%alp=none'
      logged '%?lph=alpha'
      ;;
    ksh93)
      case ${VERSION} in
        *93u+m*)
          # job_byname in ksh93u+m 1.0.10 tests only the current job, which is bravo
          logged '%alp=none'
          logged '%?lph=none'
          ;;
        *)
          logged '%alp=alpha'
          logged '%?lph=alpha'
          ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "jobs in a command substitution sees the job table only in some shells" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session jobids
  case ${FAMILY} in
    bash | ksh93 | zsh | mksh | yash)
      logged 'comsub_simple=2'
      logged 'comsub_function=2'
      ;;
    busybox)
      # the job table survives the fork only when the substitution is the simple command `jobs`
      logged 'comsub_simple=2'
      logged 'comsub_function=0'
      ;;
    dash)
      # dash 0.5.12 clears the table in every forked child; 0.5.13 keeps it for a simple `jobs`.
      # dash has no version variable, so both answers are accepted for the simple command.
      logged 'comsub_function=0'
      matches session.log '^comsub_simple=[02]$'
      ;;
    zsh-sh)
      logged 'comsub_simple=0'
      logged 'comsub_function=0'
      ;;
    *) false ;;
  esac
}

@test "a pipeline job is one process group and \$! names its last process, except in ksh93u+m" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session pipeline
  case ${FAMILY} in
    bash | dash | busybox | mksh | yash)
      logged 'one_group=yes'
      logged 'bang=last'
      logged 'leader=first'
      logged 'jobs_p=pgid'
      ;;
    zsh | zsh-sh)
      logged 'one_group=yes'
      logged 'bang=last'
      logged 'leader=first'
      # zsh prints its long listing for jobs -p
      logged 'jobs_p=other'
      ;;
    ksh93)
      case ${VERSION} in
        *93u+m*)
          # $! is a forked ksh that leads the group of the last process; the first process leads
          # a group of its own
          logged 'one_group=no'
          logged 'bang=other'
          logged 'leader=first'
          logged 'jobs_p=other'
          ;;
        *)
          # ksh93u+ 2012: one group, led by the last process
          logged 'one_group=yes'
          logged 'bang=last'
          logged 'leader=last'
          logged 'jobs_p=pgid'
          ;;
      esac
      ;;
    *) false ;;
  esac
}

@test "kill with the process ID from \$! reaches one process; kill with a job ID reaches the group" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_session pipeline
  # Whatever $! named, the first process of the pipeline outlived `kill $!`.
  logged 'after_kill_bang_first=alive'
  case ${FAMILY}:${VERSION} in
    ksh93:*93u+m*)
      # $! named neither helper, so the last process survived too; and the first process is not
      # in the job's process group, so `kill %%` missed it
      logged 'last_survived'
      logged 'after_kill_job_first=alive'
      ;;
    bash:* | dash:* | busybox:* | mksh:* | yash:* | zsh:* | zsh-sh:* | ksh93:*)
      not_logged 'last_survived' session.log
      logged 'after_kill_job_first=gone'
      ;;
    *) false ;;
  esac
}

@test "in a script, set -m alone lets fg hand over the terminal only in some shells" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_lab "${PROBE}" script_fg
  [ "${status}" -eq 0 ]
  logged 'start role=bg leader=yes' a.log
  case ${FAMILY}:${VERSION} in
    bash:* | dash:* | busybox:* | yash:*)
      logged 'role fg' a.log
      logged 'fg_status=7' script.log
      ;;
    zsh:* | zsh-sh:* | mksh:* | ksh93:*93u+m*)
      # the job is waited for but never becomes the foreground group, so it gives up after 3 s
      logged 'timeout role=bg' a.log
      logged 'fg_status=99' script.log
      ;;
    ksh93:*)
      # ksh93u+ 2012: fg fails without a word and the job is left alone
      not_logged '^role' a.log
      not_logged '^timeout' a.log
      logged 'fg_status=1' script.log
      ;;
    *) false ;;
  esac
}

@test "a script's bash finds the terminal through standard error, so fg 2>file hands nothing over" {
  if [ "${FAMILY}" = posh ]; then
    assert_posh_has_no_job_control
    return 0
  fi
  run_lab "${PROBE}" script_fg_stderr
  [ "${status}" -eq 0 ]
  logged 'start role=bg leader=yes' a.log
  is_empty fg.err
  case ${FAMILY}:${VERSION} in
    bash:*)
      # the positive twin is the previous test: the same fg without the redirection hands over
      logged 'timeout role=bg' a.log
      logged 'fg_status=99' script.log
      ;;
    dash:* | busybox:* | yash:*)
      # these shells open /dev/tty themselves
      logged 'role fg' a.log
      logged 'fg_status=7' script.log
      ;;
    zsh:* | zsh-sh:* | mksh:* | ksh93:*93u+m*)
      logged 'timeout role=bg' a.log
      logged 'fg_status=99' script.log
      ;;
    ksh93:*)
      not_logged '^role' a.log
      not_logged '^timeout' a.log
      logged 'fg_status=1' script.log
      ;;
    *) false ;;
  esac
}

@test "without job control fg and bg fail and leave the job alone" {
  run_lab "${SCRIPT}" nojc
  [ "${status}" -eq 0 ]
  local want pid
  case ${FAMILY} in
    bash | zsh | zsh-sh | ksh93 | mksh | yash) want=1 ;;
    dash | busybox) want=2 ;;
    posh) want=127 ;; # posh has no fg and no bg
    *) false ;;
  esac
  logged "fg_status=${want}" script.log
  logged "bg_status=${want}" script.log
  logged 'job=alive' script.log
  is_empty fg.out
  is_empty bg.out
  # The job is still listed: with job control off, jobs -p prints its process ID.
  pid=$(pid_of h)
  case ${FAMILY} in
    bash | dash | busybox | ksh93 | mksh) logged 'jobs_p=pid' script.log ;;
    yash)
      # yash prints the process group ID it recorded for the job, and without job control that is 0
      logged '0' jobs_p.out
      ;;
    zsh | zsh-sh) matches jobs_p.out "^\[[0-9]+\] +\+ ${pid} running" ;;
    posh) matches jobs_p.out 'jobs: not found' ;;
    *) false ;;
  esac
}

@test "tcsetpgrp refuses a process group that does not exist or belongs to another session" {
  run "${FGPG}" ttyerr
  [ "${status}" -eq 0 ]
  local system
  system=$(uname -s)
  # The errno for a process group ID that names no group is the kernel's choice.
  case ${system} in
    Linux) [ "${output}" = 'own_group=ok no_such_group=ESRCH other_session=EPERM' ] ;;
    Darwin) [ "${output}" = 'own_group=ok no_such_group=EINVAL other_session=EPERM' ] ;;
    *) false ;;
  esac
}
