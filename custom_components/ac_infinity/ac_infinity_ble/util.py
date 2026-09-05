"""Bit/byte helpers ported from the vendor app's frame handling.

The conventions here are deliberately odd because they mirror the app's
Java code the protocol was derived from; callers throughout the library and
integration rely on the exact polarities and indexing below.  Do not
"normalize" them.
"""
import ctypes


def get_short(b: bytes, i: int) -> int:
    """Read a SIGNED big-endian 16-bit value at offset ``i``."""
    return ctypes.c_int16((b[i + 1] & 255) | ((b[i] << 8) & 65280)).value


def get_bits(b: int, i: int, i2: int) -> int:
    """Extract ``i2`` bits starting at MSB-indexed position ``i``.

    Position 0 is the MOST significant bit; e.g. ``get_bits(b, 4, 4)`` is
    the low nibble.
    """
    return (b >> ((8 - i) - i2)) & (255 >> (8 - i2))


def get_bit(b: int, i: int) -> bool:
    """Return True when the MSB-indexed bit ``i`` is CLEAR (zero).

    Inverted on purpose: callers test for a bit being SET by negating this
    (``not get_bit(...)`` / ``True ^ get_bit(...)``).  Changing the polarity
    would silently flip every flag parsed from the wire.
    """
    return (b >> (7 - i)) & 1 == 0


def crc16(data: list[int], i: int | None = None, i2: int | None = None) -> list[int]:
    """CRC-16/CCITT-FALSE over ``data[i : i + i2]``, returned as [hi, lo].

    Must match the device firmware's checksum exactly or every frame is
    rejected; verified equivalent to the standard check value
    (b"123456789" -> 0x29B1).
    """
    if i is None or i2 is None:
        i = 0
        i2 = len(data)

    b = 65535
    for i3 in range(i, i + i2):
        b2 = (((b << 8) | (b >> 8)) & 65535) ^ (data[i3] & 255)
        b3 = b2 ^ ((b2 & 255) >> 4)
        b4 = b3 ^ ((b3 << 12) & 65535)
        b = b4 ^ (((b4 & 255) << 5) & 65535)

    b5 = b & 65535
    return [((b5 >> 8) & 255), (b5 & 255)]
