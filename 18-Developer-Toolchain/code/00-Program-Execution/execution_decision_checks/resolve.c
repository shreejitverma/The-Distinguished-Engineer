#include <netdb.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>

int main(void) {
    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof hints);
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    int rc = getaddrinfo("localhost", NULL, &hints, &res);
    printf("getaddrinfo: %s;", rc == 0 ? "resolved" : gai_strerror(rc));
    if (res) freeaddrinfo(res);
    int nss = 0;
    char line[512];
    FILE *maps = fopen("/proc/self/maps", "r");
    while (maps && fgets(line, sizeof line, maps))
        if (strstr(line, "libnss")) nss++;
    if (maps) fclose(maps);
    printf("libnss objects mapped: %d;\n", nss);
    return rc == 0 ? 0 : 1;
}
