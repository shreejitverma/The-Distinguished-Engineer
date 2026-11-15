/*
 * probe.c - one shared object that is a loadable builtin for both bash and ksh93.
 *
 * It needs no bash or AST headers: it declares the few bash structures it uses,
 * whose layout has not changed from bash 3.2 to bash 5.3 (builtins.h, command.h).
 * It calls nothing inside the shell, so it also loads into a bash it was not built for.
 *
 * bash:  enable -f ./probe.so probe      looks up the data symbol probe_struct
 * ksh93: builtin -f ./probe.so probe     looks up plugin_version() and b_probe()
 *
 * Modes (first argument):
 *   pid             print the process ID the builtin runs in
 *   args WORD...    print the argument count and each argument in angle brackets
 *   cd DIR          chdir(2) inside the shell process, with no PWD update
 *   fallback        return 262 (bash EX_DISKFALLBACK): ask bash to run the PATH command
 *   crash           abort(3): the fault kills the shell itself
 *
 * A second builtin, probefail, has a load hook that refuses to load (bash 4.4+),
 * and probe has an unload hook that prints a line when `enable -d probe` runs.
 * With PROBE_CTOR set in the environment, a constructor reports from inside dlopen(3).
 *
 * Build: "${CC:-cc}" -shared -fPIC -o probe.so probe.c
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

/* bash command.h: one word of the command line, and the list of words. */
typedef struct word_desc {
  char *word;
  int flags;
} WORD_DESC;

typedef struct word_list {
  struct word_list *next;
  WORD_DESC *word;
} WORD_LIST;

/* bash builtins.h: the table entry that `enable -f` copies into shell_builtins. */
struct builtin {
  char *name;
  int (*function)(WORD_LIST *);
  int flags;
  char *const *long_doc;
  const char *short_doc;
  char *handle;
};

#define BUILTIN_ENABLED 0x01 /* builtins.h */
#define EX_DISKFALLBACK 262  /* shell.h, bash 5.0 and later */

/*
 * A constructor runs inside dlopen(3), before bash looks up any symbol, so
 * `enable -f probe.so anything` executes it even when `anything_struct` is absent.
 * It reports only when PROBE_CTOR is set, so the other tests stay quiet.
 */
__attribute__((constructor)) static void probe_constructor(void) {
  if (getenv("PROBE_CTOR") != NULL) {
    printf("constructor ran in pid %ld\n", (long)getpid());
    fflush(stdout);
  }
}

/* The shared implementation: argv[0] is the builtin name, argv[1] the mode. */
static int probe_main(int argc, char **argv) {
  int i;

  if (argc < 2) {
    fprintf(stderr, "probe: usage: probe pid|args|cd|fallback|crash ...\n");
    return 2;
  }
  if (strcmp(argv[1], "pid") == 0) {
    printf("%ld\n", (long)getpid());
  } else if (strcmp(argv[1], "args") == 0) {
    printf("%d args:", argc - 2);
    for (i = 2; i < argc; i++) {
      printf(" <%s>", argv[i]);
    }
    printf("\n");
  } else if (strcmp(argv[1], "cd") == 0) {
    if (argc < 3 || chdir(argv[2]) != 0) {
      perror("probe: cd");
      return 1;
    }
  } else if (strcmp(argv[1], "fallback") == 0) {
    return EX_DISKFALLBACK;
  } else if (strcmp(argv[1], "crash") == 0) {
    fflush(stdout);
    abort();
  } else {
    fprintf(stderr, "probe: unknown mode: %s\n", argv[1]);
    return 2;
  }
  /* bash and ksh93 share stdout with this code; flush before returning. */
  fflush(stdout);
  return 0;
}

/* bash entry point: convert the WORD_LIST to argv, then share probe_main. */
static int probe_builtin(WORD_LIST *list) {
  char *argv[64];
  int argc = 0;

  argv[argc++] = "probe";
  for (; list != NULL && argc < 63; list = list->next) {
    argv[argc++] = list->word->word;
  }
  argv[argc] = NULL;
  return probe_main(argc, argv);
}

/* Unload hook: `enable -d probe` calls probe_builtin_unload (bash 4.4+). */
void probe_builtin_unload(char *name) {
  printf("unload hook: %s\n", name);
  fflush(stdout);
}

static char *const probe_doc[] = {
    "Report where a loadable builtin runs.",
    "",
    "Modes: pid, args WORD..., cd DIR, fallback, crash.",
    NULL,
};

struct builtin probe_struct = {
    "probe", probe_builtin, BUILTIN_ENABLED, probe_doc, "probe mode [arg ...]", NULL,
};

/* probefail: its load hook returns 0, so bash 4.4+ refuses to install it. */
static int probefail_builtin(WORD_LIST *list) {
  (void)list;
  printf("probefail ran\n");
  fflush(stdout);
  return 0;
}

int probefail_builtin_load(char *name) {
  (void)name;
  return 0;
}

static char *const probefail_doc[] = {"A builtin whose load hook refuses.", NULL};

struct builtin probefail_struct = {
    "probefail", probefail_builtin, BUILTIN_ENABLED, probefail_doc, "probefail", NULL,
};

/*
 * ksh93 entry points. `builtin -f` rejects a library without plugin_version()
 * or with a version older than the shell's SH_PLUGIN_VERSION (libdll dllcheck.c);
 * AST code defines it with the SHLIB() macro from shcmd.h. Without AST headers
 * we return a date later than any released AST_VERSION.
 */
unsigned long plugin_version(void) { return 20991231UL; }

int b_probe(int argc, char **argv, void *context) {
  (void)context;
  return probe_main(argc, argv);
}
