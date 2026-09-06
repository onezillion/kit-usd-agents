"""Producer/consumer SHM attach + low-level copy helpers shared by Kit-side and
encoder-side code. Kept separate from shm_ring.py ring logic."""

from __future__ import annotations

import ctypes

# These are configured once on the Kit side; ctypes.pythonapi is process-global.
_PyCapsule_GetPointer = ctypes.pythonapi.PyCapsule_GetPointer
_PyCapsule_GetPointer.restype = ctypes.c_void_p
_PyCapsule_GetPointer.argtypes = (ctypes.py_object, ctypes.c_char_p)

_PyCapsule_GetName = ctypes.pythonapi.PyCapsule_GetName
_PyCapsule_GetName.restype = ctypes.c_char_p
_PyCapsule_GetName.argtypes = (ctypes.py_object,)


def capsule_pointer(capsule, capsule_name) -> int:
    """Raw CPU address of a ByteCapture capsule payload (0 on failure)."""
    ptr = _PyCapsule_GetPointer(capsule, capsule_name)
    return int(ptr) if ptr else 0


def capsule_name(capsule) -> bytes | None:
    try:
        return _PyCapsule_GetName(capsule)
    except Exception:
        return None


def copy_address_into(dst: bytearray | memoryview, dst_offset: int,
                      src_addr: int, nbytes: int) -> None:
    """ctypes.memmove from a raw address into a writable buffer at an offset.

    ``dst`` is a SHM ``.buf`` (supports the buffer protocol). Prefer the ring's own
    base-address fast path for the hot loop; this helper is for one-off copies.
    """
    base = ctypes.addressof(ctypes.c_char.from_buffer(dst))
    ctypes.memmove(base + dst_offset, src_addr, nbytes)
