/* Tentative definition: 32 integers (128 bytes) */
int shared_comm[32];

int main(void) {
    return sizeof(shared_comm);
}
