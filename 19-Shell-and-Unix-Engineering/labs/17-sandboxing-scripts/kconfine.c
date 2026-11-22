/*
 * kconfine: apply a kernel-enforced "no outbound connect" policy to this process, then exec a
 * command; or probe whether connect(2) is allowed.
 *
 *   kconfine connect            try connect(2) to 127.0.0.1:1 and print what happened
 *   kconfine confine CMD [ARG]  install the policy, then execvp CMD
 *
 * Linux: PR_SET_NO_NEW_PRIVS plus a seccomp-bpf filter that returns EPERM for connect(2).
 *   This is a deny-list demonstration; sendto(2) on a UDP socket is not blocked.
 * macOS: sandbox_init(3) with a Seatbelt profile that denies network-outbound (deprecated API).
 * Both are inherited across fork(2) and execve(2) and cannot be removed by the process.
 */
#include <arpa/inet.h>
#include <errno.h>
#include <netinet/in.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

#if defined(__linux__)
#include <stdint.h>
#include <sys/prctl.h>
#include <sys/syscall.h>

/* The seccomp and BPF ABI constants are written out so no linux/ headers are needed. */
#ifndef PR_SET_NO_NEW_PRIVS
#define PR_SET_NO_NEW_PRIVS 38
#endif
#ifndef PR_SET_SECCOMP
#define PR_SET_SECCOMP 22
#endif
#define KF_SECCOMP_MODE_FILTER 2
#define KF_LD_W_ABS 0x20u /* BPF_LD | BPF_W | BPF_ABS */
#define KF_JEQ_K 0x15u    /* BPF_JMP | BPF_JEQ | BPF_K */
#define KF_JGE_K 0x35u    /* BPF_JMP | BPF_JGE | BPF_K */
#define KF_RET_K 0x06u    /* BPF_RET | BPF_K */
#define KF_OFF_NR 0u      /* offsetof(struct seccomp_data, nr) */
#define KF_OFF_ARCH 4u    /* offsetof(struct seccomp_data, arch) */
#define KF_RET_KILL_PROCESS 0x80000000u
#define KF_RET_ERRNO 0x00050000u
#define KF_RET_ALLOW 0x7fff0000u

#if defined(__x86_64__)
#define KF_AUDIT_ARCH 0xC000003Eu /* AUDIT_ARCH_X86_64 */
#elif defined(__aarch64__)
#define KF_AUDIT_ARCH 0xC00000B7u /* AUDIT_ARCH_AARCH64 */
#else
#error "kconfine.c: add the AUDIT_ARCH_* value for this architecture"
#endif

struct kf_insn {
  uint16_t code;
  uint8_t jt;
  uint8_t jf;
  uint32_t k;
};

struct kf_prog {
  unsigned short len;
  struct kf_insn *filter;
};

static int install_policy(void)
{
  struct kf_insn filter[] = {
      {KF_LD_W_ABS, 0, 0, KF_OFF_ARCH},
      {KF_JEQ_K, 1, 0, KF_AUDIT_ARCH}, /* native ABI: skip the kill */
      {KF_RET_K, 0, 0, KF_RET_KILL_PROCESS},
      {KF_LD_W_ABS, 0, 0, KF_OFF_NR},
#if defined(__x86_64__)
      {KF_JGE_K, 0, 1, 0x40000000u}, /* x32 ABI bit set: kill */
      {KF_RET_K, 0, 0, KF_RET_KILL_PROCESS},
#endif
      {KF_JEQ_K, 0, 1, (uint32_t)SYS_connect},
      {KF_RET_K, 0, 0, KF_RET_ERRNO | (uint32_t)EPERM},
      {KF_RET_K, 0, 0, KF_RET_ALLOW},
  };
  struct kf_prog prog = {(unsigned short)(sizeof filter / sizeof filter[0]), filter};

  if (prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0) {
    perror("kconfine: prctl(PR_SET_NO_NEW_PRIVS)");
    return -1;
  }
  if (prctl(PR_SET_SECCOMP, KF_SECCOMP_MODE_FILTER, &prog, 0, 0) != 0) {
    perror("kconfine: prctl(PR_SET_SECCOMP)");
    return -1;
  }
  return 0;
}

#elif defined(__APPLE__)
#include <sandbox.h>

static int install_policy(void)
{
  char *err = NULL;

  if (sandbox_init("(version 1)(allow default)(deny network-outbound)", 0, &err) != 0) {
    fprintf(stderr, "kconfine: sandbox_init: %s\n", err != NULL ? err : "unknown error");
    if (err != NULL) {
      sandbox_free_error(err);
    }
    return -1;
  }
  return 0;
}

#else
#error "kconfine.c: only Linux and macOS are supported"
#endif

static int probe_connect(void)
{
  struct sockaddr_in addr;
  int fd = socket(AF_INET, SOCK_STREAM, 0);

  if (fd < 0) {
    printf("connect: socket() failed: %s\n", strerror(errno));
    return 1;
  }
  memset(&addr, 0, sizeof addr);
  addr.sin_family = AF_INET;
  addr.sin_port = htons(1);
  addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
  alarm(5); /* bound the probe if a firewall drops the packet */
  if (connect(fd, (struct sockaddr *)&addr, sizeof addr) == 0) {
    printf("connect: connected\n");
  } else if (errno == EPERM) {
    printf("connect: EPERM\n");
  } else {
    printf("connect: errno=%d (%s)\n", errno, strerror(errno));
  }
  close(fd);
  return 0;
}

int main(int argc, char **argv)
{
  if (argc == 2 && strcmp(argv[1], "connect") == 0) {
    return probe_connect();
  }
  if (argc >= 3 && strcmp(argv[1], "confine") == 0) {
    if (install_policy() != 0) {
      return 125;
    }
    execvp(argv[2], &argv[2]);
    perror("kconfine: execvp");
    return 127;
  }
  fprintf(stderr, "usage: kconfine connect | kconfine confine CMD [ARG...]\n");
  return 2;
}
