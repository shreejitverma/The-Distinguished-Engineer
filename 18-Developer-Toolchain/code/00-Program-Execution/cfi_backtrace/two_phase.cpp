// Observes the Itanium two-phase protocol directly: the link step (see run.sh) wraps
// __gxx_personality_v0, so every personality call made on behalf of this program's frames is logged
// with its _Unwind_Action bits and its return code before the real personality runs.
#include <cstdio>
#include <cstring>
#include <exception>
#include <unistd.h>
#include <unwind.h>

extern "C" _Unwind_Reason_Code __real___gxx_personality_v0(int, _Unwind_Action, _Unwind_Exception_Class,
                                                           _Unwind_Exception *, _Unwind_Context *);

static int search_calls = 0;
static int cleanup_calls = 0;

static const char *action_name(_Unwind_Action a) {
    switch (a) {
    case _UA_SEARCH_PHASE: return "SEARCH";
    case _UA_CLEANUP_PHASE: return "CLEANUP";
    case _UA_CLEANUP_PHASE | _UA_HANDLER_FRAME: return "CLEANUP|HANDLER_FRAME";
    default: return "OTHER";
    }
}

static const char *result_name(_Unwind_Reason_Code r) {
    switch (r) {
    case _URC_HANDLER_FOUND: return "HANDLER_FOUND";
    case _URC_INSTALL_CONTEXT: return "INSTALL_CONTEXT";
    case _URC_CONTINUE_UNWIND: return "CONTINUE_UNWIND";
    default: return "OTHER";
    }
}

extern "C" _Unwind_Reason_Code __wrap___gxx_personality_v0(int version, _Unwind_Action actions,
                                                           _Unwind_Exception_Class cls,
                                                           _Unwind_Exception *exc, _Unwind_Context *ctx) {
    _Unwind_Reason_Code r = __real___gxx_personality_v0(version, actions, cls, exc, ctx);
    if (actions & _UA_SEARCH_PHASE) {
        search_calls++;
    } else {
        cleanup_calls++;
    }
    std::printf("  [personality] %s -> %s\n", action_name(actions), result_name(r));
    std::fflush(stdout);
    return r;
}

struct Resource {
    const char *name;
    explicit Resource(const char *n) : name(n) {
        std::printf("  [RAII] acquire: %s\n", name);
        std::fflush(stdout);
    }
    ~Resource() {
        std::printf("  [RAII] destruct: %s\n", name);
        std::fflush(stdout);
    }
};

static void my_terminate() {
    std::printf("  [terminate] personality calls: search=%d cleanup=%d\n", search_calls, cleanup_calls);
    std::fflush(stdout);
    _exit(0);
}

__attribute__((noinline)) void will_throw_caught() {
    Resource r("caught_scope");
    throw 42;
}

__attribute__((noinline)) void will_throw_uncaught() {
    Resource r("uncaught_scope");
    throw 99;
}

int main(int argc, char **argv) {
    if (argc > 1 && std::strcmp(argv[1], "--uncaught") == 0) {
        std::set_terminate(my_terminate);
        will_throw_uncaught();
        return 0;
    }

    try {
        will_throw_caught();
    } catch (int val) {
        std::printf("  [catch] caught integer exception: %d\n", val);
        std::printf("  [catch] personality calls: search=%d cleanup=%d\n", search_calls, cleanup_calls);
    }
    return 0;
}
