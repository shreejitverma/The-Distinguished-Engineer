#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

#define SSH_SK_VERSION_MAJOR 0x000a0000
#define SSH_SK_ED25519       0x01

struct sk_enroll_response {
    uint8_t flags;
    uint8_t *public_key;
    size_t public_key_len;
    uint8_t *key_handle;
    size_t key_handle_len;
    uint8_t *signature;
    size_t signature_len;
    uint8_t *attestation_cert;
    size_t attestation_cert_len;
    uint8_t *authdata;
    size_t authdata_len;
};

struct sk_resident_key {
    uint32_t alg;
    size_t slot;
    char *application;
    struct sk_enroll_response key;
    uint8_t flags;
    uint8_t *user_id;
    size_t user_id_len;
};

uint32_t sk_api_version(void) {
    return SSH_SK_VERSION_MAJOR;
}

int sk_enroll(uint32_t alg, const uint8_t *challenge, size_t challenge_len,
              const char *application, uint8_t flags, const char *pin,
              void *options, struct sk_enroll_response **enroll_response) {
    struct sk_enroll_response *resp = calloc(1, sizeof(*resp));
    if (!resp) {
        return -1;
    }
    resp->flags = 0x01; /* User presence demonstrated */
    resp->public_key_len = 32;
    resp->public_key = calloc(1, 32);
    if (!resp->public_key) {
        free(resp);
        return -1;
    }
    resp->public_key[0] = 0x42;
    resp->key_handle_len = 16;
    resp->key_handle = (uint8_t *)strdup("MOCK_FIDO_HANDLE");
    *enroll_response = resp;
    return 0;
}

struct sk_option;
struct sk_sign_response;

/* Mock token: it can enroll but never signs, so the key cannot authenticate (SSH_SK_ERR_GENERAL = -1). */
int sk_sign(uint32_t alg, const uint8_t *data, size_t data_len, const char *application,
            const uint8_t *key_handle, size_t key_handle_len, uint8_t flags, const char *pin,
            struct sk_option **options, struct sk_sign_response **sign_response) {
    (void)alg; (void)data; (void)data_len; (void)application; (void)key_handle;
    (void)key_handle_len; (void)flags; (void)pin; (void)options; (void)sign_response;
    return -1;
}

int sk_load_resident_keys(const char *pin, void *options,
                          struct sk_resident_key ***rks, size_t *nrks) {
    return -1;
}
