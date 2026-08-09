"""Decryption for Deezer media streams.

Deezer serves tracks encrypted with Blowfish-CBC applied to every third
2048-byte chunk (the "BF_CBC_STRIPE" cipher). The per-track key is derived from
the MD5 of the track ID XORed against a fixed secret, so no per-session key
exchange is involved.
"""

import hashlib
from collections.abc import Iterator

from Crypto.Cipher import Blowfish

_BF_SECRET = b"g4el58wc0zvf9na1"
_BF_IV = bytes((0, 1, 2, 3, 4, 5, 6, 7))

CHUNK_SIZE = 2048
"""Size of a stripe. Every third stripe is encrypted, the rest are plaintext."""


def blowfish_key(track_id: str | int) -> bytes:
    """Derive the Blowfish key for a track.

    Args:
        track_id: The Deezer song ID (SNG_ID).

    Returns:
        A 16-byte Blowfish key.
    """
    md5_id = hashlib.md5(str(track_id).encode(), usedforsecurity=False).hexdigest().encode()
    return bytes(md5_id[i] ^ md5_id[i + 16] ^ _BF_SECRET[i] for i in range(16))


def decrypt_chunk(chunk: bytes, key: bytes) -> bytes:
    """Decrypt a single 2048-byte stripe.

    Args:
        chunk: Exactly CHUNK_SIZE bytes of ciphertext.
        key: The track's Blowfish key.

    Returns:
        The decrypted stripe.
    """
    return Blowfish.new(key, Blowfish.MODE_CBC, _BF_IV).decrypt(chunk)


def decrypt_stripes(data: bytes, key: bytes, start_index: int = 0) -> tuple[bytes, int]:
    """Decrypt a buffer of whole stripes.

    Only every third stripe is encrypted, and a trailing partial stripe is never
    encrypted. The caller keeps feeding buffers and passing the returned index
    back in so the 1-in-3 cadence survives across chunk boundaries.

    Args:
        data: Buffer whose length is a multiple of CHUNK_SIZE, except possibly
            for the final call.
        key: The track's Blowfish key.
        start_index: Stripe counter to resume from.

    Returns:
        Tuple of (plaintext, next stripe index).
    """
    out = bytearray()
    index = start_index
    for offset in range(0, len(data), CHUNK_SIZE):
        stripe = data[offset : offset + CHUNK_SIZE]
        if index % 3 == 0 and len(stripe) == CHUNK_SIZE:
            out += decrypt_chunk(stripe, key)
        else:
            out += stripe
        index += 1
    return bytes(out), index


def iter_decrypted(chunks: Iterator[bytes], key: bytes) -> Iterator[bytes]:
    """Decrypt an iterator of arbitrary-sized byte chunks.

    Buffers whatever does not align to a stripe boundary and carries it into the
    next chunk, so the caller can stream straight from an HTTP response.

    Args:
        chunks: Iterator of ciphertext chunks of any size.
        key: The track's Blowfish key.

    Yields:
        Plaintext chunks.
    """
    buffer = bytearray()
    index = 0
    for chunk in chunks:
        buffer += chunk
        aligned = len(buffer) - (len(buffer) % CHUNK_SIZE)
        if not aligned:
            continue
        plaintext, index = decrypt_stripes(bytes(buffer[:aligned]), key, index)
        del buffer[:aligned]
        yield plaintext
    if buffer:
        plaintext, _ = decrypt_stripes(bytes(buffer), key, index)
        yield plaintext
