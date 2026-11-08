int target_var = 42;
int *ptr_target = &target_var;

int target_func(void) {
    return target_var;
}

extern int caller(void);
extern int do_call(void);

int main(void) {
    return caller() + do_call();
}
