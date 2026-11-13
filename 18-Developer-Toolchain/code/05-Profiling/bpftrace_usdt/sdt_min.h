/*
 * sdt_min.h - a teaching reduction of SystemTap's <sys/sdt.h> (USDT, note format v3).
 *
 * Each SDT_PROBEn() expands to exactly one `nop` in the instruction stream plus one
 * record in the non-allocated ELF section .note.stapsdt. Nothing else is emitted at
 * the probe site: no call, no branch, no relocation in loaded memory.
 *
 * The note layout is the one documented in the SystemTap wiki page
 * "UserSpaceProbeImplementation" and emitted by systemtap's includes/sys/sdt.h:
 *
 *   Elf64_Nhdr { n_namesz = 8, n_descsz, n_type = 3 }, name "stapsdt\0"
 *   desc: 8-byte probe PC, 8-byte link-time address of .stapsdt.base,
 *         8-byte semaphore address (0 = none), then three NUL-terminated strings:
 *         provider, probe name, argument format ("N@OP N@OP ...").
 *
 * Limits of this reduction (use the real <sys/sdt.h> or libbpf's usdt.h in products):
 *   - LP64 targets only (8-byte addresses); built and run on aarch64, assembled for x86-64.
 *   - Integer arguments only, at most three per probe.
 *   - No SHF_LINK_ORDER handling, so do not combine it with -Wl,--gc-sections.
 */
#ifndef SDT_MIN_H
#define SDT_MIN_H

#if !defined(__LP64__)
#error "sdt_min.h supports LP64 targets only"
#endif

/* Signed byte size, as the note wants it: -4 is a signed 32-bit value, 8 an unsigned 64-bit one. */
#define SDT_ARGSZ(x) ((int)sizeof(x) * (((__typeof__(x))-1) < (__typeof__(x))1 ? -1 : 1))

/* A USDT semaphore: a 2-byte counter in section .probes that a tracer increments while attached. */
#define SDT_DEFINE_SEMAPHORE(sema) \
	volatile unsigned short sema __attribute__((section(".probes"), used, visibility("hidden"))) = 0
#define SDT_ENABLED(sema) __builtin_expect((sema) != 0, 0)

/* `%c[...]` prints a constant operand bare; `%[...]` prints the operand the compiler chose. */
#define SDT_NOTE_(provider, name, sema, argfmt)                                 \
	"990:	nop\n"                                                              \
	"	.pushsection .note.stapsdt,\"?\",\"note\"\n"                            \
	"	.balign 4\n"                                                            \
	"	.4byte 992f-991f, 994f-993f, 3\n"                                       \
	"991:	.asciz \"stapsdt\"\n"                                               \
	"992:	.balign 4\n"                                                        \
	"993:	.8byte 990b\n"                                                      \
	"	.8byte _.stapsdt.base\n"                                                \
	"	.8byte " #sema "\n"                                                     \
	"	.asciz \"" #provider "\"\n"                                             \
	"	.asciz \"" #name "\"\n"                                                 \
	"	.asciz \"" argfmt "\"\n"                                                \
	"994:	.balign 4\n"                                                        \
	"	.popsection\n"                                                          \
	".ifndef _.stapsdt.base\n"                                                  \
	"	.pushsection .stapsdt.base,\"aG\",\"progbits\",.stapsdt.base,comdat\n" \
	"	.weak _.stapsdt.base\n"                                                 \
	"	.hidden _.stapsdt.base\n"                                               \
	"_.stapsdt.base:	.space 1\n"                                             \
	"	.size _.stapsdt.base, 1\n"                                              \
	"	.popsection\n"                                                          \
	".endif\n"

/* `sema` is a semaphore symbol name, or 0 for a probe without one. */
#define SDT_PROBE2(provider, name, sema, a1, a2)                                              \
	__asm__ __volatile__(SDT_NOTE_(provider, name, sema, "%c[s1]@%[v1] %c[s2]@%[v2]")         \
			     :                                                                        \
			     : [s1] "n"(SDT_ARGSZ(a1)), [v1] "nor"(a1),                               \
			       [s2] "n"(SDT_ARGSZ(a2)), [v2] "nor"(a2))

#define SDT_PROBE3(provider, name, sema, a1, a2, a3)                                          \
	__asm__ __volatile__(SDT_NOTE_(provider, name, sema,                                      \
				       "%c[s1]@%[v1] %c[s2]@%[v2] %c[s3]@%[v3]")                      \
			     :                                                                        \
			     : [s1] "n"(SDT_ARGSZ(a1)), [v1] "nor"(a1),                               \
			       [s2] "n"(SDT_ARGSZ(a2)), [v2] "nor"(a2),                               \
			       [s3] "n"(SDT_ARGSZ(a3)), [v3] "nor"(a3))

#endif /* SDT_MIN_H */
