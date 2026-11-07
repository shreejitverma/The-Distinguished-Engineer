#include <stdio.h>
#include <pthread.h>
#include <stdint.h>

extern __thread int var_gd;
extern int get_gd(void);

static void *worker(void *arg) {
    long id = (long)arg;
    var_gd += (int)id * 100;
    printf("Thread %ld: var_gd=%d\n", id, get_gd());
    return NULL;
}

int main(void) {
    pthread_t t1, t2;
    printf("Main thread before: var_gd=%d\n", get_gd());
    pthread_create(&t1, NULL, worker, (void*)1);
    pthread_join(t1, NULL);
    pthread_create(&t2, NULL, worker, (void*)2);
    pthread_join(t2, NULL);
    printf("Main thread after: var_gd=%d\n", get_gd());
    return 0;
}
