#include <stdio.h>
int main(int argc, char **argv) {
    FILE *f;
    int i;
    if (argc != 2) return 2;
    f = fopen(argv[1], "w");
    if (!f) return 1;
    fprintf(f, "static const int squares[] = {");
    for (i = 0; i < 8; i++) fprintf(f, "%d,", i * i);
    fprintf(f, "};\n");
    return fclose(f) == 0 ? 0 : 1;
}
