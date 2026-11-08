#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <locale.h>
#include <fnmatch.h>

/* Fail the lab (exit 1, message on stderr) when the mechanism a section demonstrates is absent. */
#define CHECK(cond) do { if (!(cond)) { fprintf(stderr, "CHECK FAILED line %d: %s\n", __LINE__, #cond); return 1; } } while (0)

int main(void) {
    printf("=== 1. Environment Precedence and Multibyte Character Width ===\n");

    /* Test precedence: set LANG and LC_COLLATE to UTF-8, but LC_ALL to C */
    setenv("LANG", "en_US.UTF-8", 1);
    setenv("LC_COLLATE", "en_US.UTF-8", 1);
    setenv("LC_ALL", "C", 1);

    char *loc_c = setlocale(LC_ALL, "");
    printf("Precedence test (LC_ALL=C overriding LC_COLLATE and LANG):\n");
    printf("  Resolved locale: %s\n", loc_c ? loc_c : "NULL");
    printf("  MB_CUR_MAX: %d\n", (int)MB_CUR_MAX);
    CHECK(loc_c != NULL && strcmp(loc_c, "C") == 0);
    CHECK(MB_CUR_MAX == 1);

    /* Switch to UTF-8 locale */
    setenv("LC_ALL", "en_US.UTF-8", 1);
    char *loc_utf8 = setlocale(LC_ALL, "");
    printf("UTF-8 locale test (LC_ALL=en_US.UTF-8):\n");
    printf("  Resolved locale: %s\n", loc_utf8 ? loc_utf8 : "NULL");
    printf("  MB_CUR_MAX: %d\n", (int)MB_CUR_MAX);
    CHECK(loc_utf8 != NULL && strcmp(loc_utf8, "en_US.UTF-8") == 0);
    CHECK(MB_CUR_MAX >= 4);

    printf("\n=== 2. Collation Sequence vs Binary Byte Order ===\n");
    /* Compare 'a' (ASCII 97) vs 'A' (ASCII 65) */
    int c_cmp = strcmp("a", "A");
    printf("Binary strcmp(\"a\", \"A\") in ASCII order: %s (ASCII 'a'=97 > 'A'=65)\n",
           c_cmp > 0 ? "a > A" : "a <= A");

    int loc_cmp = strcoll("a", "A");
    printf("Locale strcoll(\"a\", \"A\") in en_US.UTF-8: %s (lowercase before uppercase)\n",
           loc_cmp < 0 ? "a < A" : "a >= A");

    CHECK(c_cmp > 0);
    CHECK(loc_cmp < 0);

    printf("\n=== 3. POSIX Bracket Range Pattern Matching (fnmatch) ===\n");
    /* In C locale: ASCII byte range [a-z] spans 97..122, excluding 'B' (66) */
    setlocale(LC_ALL, "C");
    int fn_c = fnmatch("[a-z]", "B", 0);
    printf("fnmatch(\"[a-z]\", \"B\") in C locale: %s\n",
           fn_c == 0 ? "MATCH" : "NOMATCH");

    /* In en_US.UTF-8: collation sequence interleaves a, A, b, B... */
    setlocale(LC_ALL, "en_US.UTF-8");
    int fn_utf8 = fnmatch("[a-z]", "B", 0);
    printf("fnmatch(\"[a-z]\", \"B\") in en_US.UTF-8 locale: %s\n",
           fn_utf8 == 0 ? "MATCH" : "NOMATCH");

    CHECK(fn_c != 0);
#ifdef __APPLE__
    CHECK(fn_utf8 == 0); /* macOS libc interleaves a, A, b, B */
#else
    CHECK(fn_utf8 != 0); /* glibc 2.28+ ranges no longer interleave */
#endif

    printf("\n=== 4. Collation Key Transformation Overhead (strxfrm) ===\n");
    char xfrm_buf[128];
    const char *test_word = "Apple";

    setlocale(LC_ALL, "C");
    size_t len_c = strxfrm(xfrm_buf, test_word, sizeof(xfrm_buf));
    printf("strxfrm(\"%s\") in C locale: transformed_len=%zu, raw_len=%zu (ratio=1.0x)\n",
           test_word, len_c, strlen(test_word));

    setlocale(LC_ALL, "en_US.UTF-8");
    size_t len_utf8 = strxfrm(xfrm_buf, test_word, sizeof(xfrm_buf));
    printf("strxfrm(\"%s\") in en_US.UTF-8: transformed_len=%zu, raw_len=%zu (multi-level expansion)\n",
           test_word, len_utf8, strlen(test_word));

    CHECK(len_c == strlen(test_word));
    CHECK(len_utf8 > len_c);

    return 0;
}
