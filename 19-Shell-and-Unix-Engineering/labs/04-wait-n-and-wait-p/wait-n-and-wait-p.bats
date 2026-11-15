#!/usr/bin/env bats
# Tests for wait -n and wait -p: which shells have them, and how bash's answers changed by release.
# bash behavior is keyed on the release level (5.2.37 -> 50237) because it changed at 4.3, 5.1,
# 5.2 patch 30, and 5.3; every other family lacks the options, or (BusyBox ash 1.37) accepts -n but
# returns a meaningless status, so the feature probe treats it as unsupported.

setup() {
  load ../lib/lab
  PROBE="$BATS_TEST_DIRNAME/probe.sh"
  POOL="$BATS_TEST_DIRNAME/pool.sh"
}

# bash_level: print the bash release under test as MAJOR*10000 + MINOR*100 + PATCH.
bash_level() {
  local v major minor patch
  v=$(lab_shell_version)
  v=${v%%[!0-9.]*}
  IFS=. read -r major minor patch <<<"$v"
  echo $((major * 10000 + minor * 100 + ${patch:-0}))
}

@test "wait -n returns the first job to end while a slower one still runs, where the shell has it" {
  run_lab "$PROBE" support
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 40300 ]; then
        [ "$output" = "status=7 slow_alive=yes" ]
      else
        [ "$output" = "status=2 slow_alive=yes" ] # bash 3.2 to 4.2: invalid option, usage status
      fi
      ;;
    busybox) [ "$output" = "status=0 slow_alive=no" ] ;;         # 1.37: -n accepted, but dowait returns a flag, not the status
    zsh | zsh-sh) [ "$output" = "status=127 slow_alive=yes" ] ;; # -n is taken as a job name
    dash | ksh93 | yash) [ "$output" = "status=2 slow_alive=yes" ] ;;
    mksh | posh) [ "$output" = "status=1 slow_alive=yes" ] ;;
    *) false ;;
  esac
}

@test "wait -p separates a job that exited 127 from having no job at all" {
  run_lab "$PROBE" ambiguity
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50102 ]; then
        [ "$output" = "first=127 who=job second=127 who=unset" ]
      elif [ "$level" -ge 50100 ]; then
        [ "$output" = "first=127 who=job second=127 who=other" ] # bash51-002 fixed this
      elif [ "$level" -ge 40300 ]; then
        [ "$output" = "unsupported: wait -p" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "wait -n reports ended jobs in job-table order, not in the order they ended" {
  run_lab "$PROBE" order
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 40300 ]; then
        [ "$output" = "first=11 second=22" ] # job 2 exited a second before job 1
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "wait -n PID waits for that PID from bash 5.1; bash 4.3 to 5.0 ignore the operand" {
  run_lab "$PROBE" pidarg
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50100 ]; then
        [ "$output" = "status=4" ]
      elif [ "$level" -ge 40300 ]; then
        [ "$output" = "status=9" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "in a script file, a job that ended during a foreground command is still reported" {
  run_lab "$PROBE" finished
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 40300 ]; then
        [ "$output" = "status=3" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "under sh -c, bash before 5.2 patch 30 loses a job that ended during a foreground command" {
  code=$(cat "$PROBE")
  run_lab_c "$code" probe.sh finished
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50230 ]; then
        [ "$output" = "status=3" ]
      elif [ "$level" -ge 40300 ]; then
        [ "$output" = "status=127" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "a job killed by a signal before wait -n is lost before bash 5.2 patch 30, but wait PID finds it" {
  run_lab "$PROBE" signaled
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50230 ]; then
        [ "$output" = "wait-n=143 wait-pid=143" ]
      elif [ "$level" -ge 40300 ]; then
        [ "$output" = "wait-n=127 wait-pid=143" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "a trapped signal ends wait -n -p with 128+N, runs the trap first, and leaves the variable unset" {
  run_lab "$PROBE" trap
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50100 ]; then
        [ "$output" = "trapped
status=USR1 who=unset" ]
      elif [ "$level" -ge 40300 ]; then
        [ "$output" = "unsupported: wait -p" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "bash 5.3 wait -n also returns a process substitution; earlier releases see no child" {
  run_lab "$PROBE" procsub
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50300 ]; then
        [ "$output" = "status=7 who=procsub" ]
      elif [ "$level" -ge 50100 ]; then
        [ "$output" = "status=127 who=unset" ]
      elif [ "$level" -ge 40300 ]; then
        [ "$output" = "unsupported: wait -p" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "a -p name with an array subscript runs the command substitution inside it" {
  run_lab "$PROBE" subscript
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50100 ]; then
        [ "$output" = "code-ran
status=0" ]
      elif [ "$level" -ge 40300 ]; then
        [ "$output" = "unsupported: wait -p" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "outside POSIX mode a job reported by wait -n can be waited for again" {
  run_lab "$PROBE" rewait
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 40300 ]; then
        [ "$output" = "wait-n=6 wait-pid=6" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

@test "in POSIX mode bash 5.3 forgets a job once wait -n has reported it" {
  run_lab "$PROBE" rewait-posix
  [ "$status" -eq 0 ]
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50300 ]; then
        [ "$output" = "wait-n=6 wait-pid=127" ]
      elif [ "$level" -ge 40300 ]; then
        [ "$output" = "wait-n=6 wait-pid=6" ]
      else
        [ "$output" = "unsupported: wait -n" ]
      fi
      ;;
    busybox | zsh | zsh-sh | dash | ksh93 | yash | mksh | posh) [ "$output" = "unsupported: wait -n" ] ;;
    *) false ;;
  esac
}

# pool_unsupported STATUS OUTPUT: succeed if the pool refused to run for want of wait -n -p.
pool_unsupported() {
  [ "$1" -eq 2 ]
  [ "$2" = "pool.sh: this shell has no usable wait -n -p (bash 5.1 or later)" ]
}

@test "the pool reports tasks as they end and keeps at most MAXJOBS running" {
  run_lab "$POOL" 2 a:2:0 b:0:3 c:1:0 d:0:0
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50100 ]; then
        [ "$status" -eq 1 ]
        [ "$output" = "done b status=3
done c status=0
done d status=0
done a status=0
summary: 4 tasks, 1 failed" ]
      else
        pool_unsupported "$status" "$output"
      fi
      ;;
    zsh | zsh-sh | busybox | dash | ksh93 | yash | mksh | posh) pool_unsupported "$status" "$output" ;;
    *) false ;;
  esac
}

@test "with MAXJOBS 1 the pool runs tasks strictly one after another" {
  run_lab "$POOL" 1 a:1:0 b:0:0
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50100 ]; then
        [ "$status" -eq 0 ]
        [ "$output" = "done a status=0
done b status=0
summary: 2 tasks, 0 failed" ]
      else
        pool_unsupported "$status" "$output"
      fi
      ;;
    zsh | zsh-sh | busybox | dash | ksh93 | yash | mksh | posh) pool_unsupported "$status" "$output" ;;
    *) false ;;
  esac
}

@test "under sh -c the pool recovers a task that wait -n lost, through wait PID" {
  code=$(cat "$POOL")
  POOL_WORK_SECONDS=1 run_lab_c "$code" pool.sh 2 a:0:3 b:2:0
  case $(lab_shell_family) in
    bash)
      level=$(bash_level)
      if [ "$level" -ge 50230 ]; then
        [ "$status" -eq 1 ]
        [ "$output" = "done a status=3
done b status=0
summary: 2 tasks, 1 failed" ]
      elif [ "$level" -ge 50100 ]; then
        [ "$status" -eq 1 ] # wait -n saw only b; a came back through wait PID
        [ "$output" = "done b status=0
done a status=3
summary: 2 tasks, 1 failed" ]
      else
        pool_unsupported "$status" "$output"
      fi
      ;;
    zsh | zsh-sh | busybox | dash | ksh93 | yash | mksh | posh) pool_unsupported "$status" "$output" ;;
    *) false ;;
  esac
}
