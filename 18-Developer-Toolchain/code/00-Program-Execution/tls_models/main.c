#include <stdio.h>

extern int get_gd(void);
extern int get_ld(void);
extern int get_ie(void);
extern int get_le(void);

int main(void) {
    printf("Values: gd=%d ld=%d ie=%d le=%d\n", get_gd(), get_ld(), get_ie(), get_le());
    return 0;
}
