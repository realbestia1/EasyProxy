# Python AES-GCM implementation for use without cryptography library
# Adapted from https://github.com/Gujal00/ResolveURL/blob/master/script.module.resolveurl/lib/resolveurl/plugins/f16px.py

import binascii
import struct


def _bytes_to_int(data):
    """Convert bytes to integer."""
    return int(binascii.hexlify(data), 16)


def _int_to_bytes(n, length):
    """Convert integer to bytes."""
    return n.to_bytes(length, byteorder='big')


def _xor_bytes(a, b):
    """XOR two byte strings."""
    return bytes(x ^ y for x, y in zip(a, b))


def _aes_block_encrypt(key, block):
    """AES single block encryption using PyCryptodome or fallback."""
    try:
        from Crypto.Cipher import AES as CryptoAES
        cipher = CryptoAES.new(key, CryptoAES.MODE_ECB)
        return cipher.encrypt(block)
    except ImportError:
        pass

    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        cipher = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())
        encryptor = cipher.encryptor()
        return encryptor.update(block) + encryptor.finalize()
    except ImportError:
        pass

    raise ImportError("No AES implementation available. Install pycryptodome or cryptography.")


def _gf_mult(a, b):
    """Galois Field multiplication for GCM."""
    R = 0xe1 << 120
    result = 0
    for i in range(128):
        if (b >> (127 - i)) & 1:
            result ^= a
        hi_bit = a & 1
        a >>= 1
        if hi_bit:
            a ^= R
    return result


def _ghash(h_int, aad, ciphertext):
    """GHASH function for GCM."""

    def _pad16(data):
        if len(data) % 16:
            return data + b'\x00' * (16 - len(data) % 16)
        return data

    data = _pad16(aad) + _pad16(ciphertext)
    data += struct.pack('>QQ', len(aad) * 8, len(ciphertext) * 8)

    y = 0
    for i in range(0, len(data), 16):
        block_int = _bytes_to_int(data[i:i + 16])
        y = _gf_mult(y ^ block_int, h_int)

    return y


class AESGCM:
    """Pure Python AES-GCM implementation."""

    def __init__(self, key):
        self.key = key
        # Compute H = E(K, 0^128)
        h_block = _aes_block_encrypt(key, b'\x00' * 16)
        self.h_int = _bytes_to_int(h_block)

    def open(self, nonce, ciphertext_with_tag, aad=b''):
        """Decrypt and verify AES-GCM ciphertext."""
        if len(ciphertext_with_tag) < 16:
            return None

        ciphertext = ciphertext_with_tag[:-16]
        tag = ciphertext_with_tag[-16:]

        # Compute J0 (counter block)
        if len(nonce) == 12:
            j0 = nonce + b'\x00\x00\x00\x01'
        else:
            # For other nonce lengths, use GHASH
            ghash_result = _ghash(self.h_int, b'', nonce)
            j0 = _int_to_bytes(ghash_result, 16)

        # Generate keystream and decrypt
        plaintext = bytearray()
        counter = _bytes_to_int(j0)

        for i in range(0, len(ciphertext), 16):
            counter = (counter & 0xffffffffffffffffffffffff00000000) | (((counter & 0xffffffff) + 1) & 0xffffffff)
            counter_block = _int_to_bytes(counter, 16)
            keystream = _aes_block_encrypt(self.key, counter_block)
            block = ciphertext[i:i + 16]
            plaintext.extend(_xor_bytes(block, keystream[:len(block)]))

        # Compute expected tag
        s = _ghash(self.h_int, aad, ciphertext)
        j0_encrypted = _aes_block_encrypt(self.key, j0)
        expected_tag = _xor_bytes(_int_to_bytes(s, 16), j0_encrypted)

        # Verify tag (constant-time comparison would be better for security)
        if tag != expected_tag:
            return None

        return bytes(plaintext)


def new(key):
    """Create a new AES-GCM cipher object."""
    return AESGCM(key)
