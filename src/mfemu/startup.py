"""Find a firmware image's startup milestones from the image itself.

Every official build, 1.0.5 to 5.0.0, starts with ST's standard reset
handler: it calls SystemInit, copies .data, zeroes .bss, then makes three
more calls, the last of which is main(). The addresses differ in every
build, so `boot.resc` takes them as variables and this works them out by
following the reset vector:

    calls[0]   SystemInit
    calls[-2]  __libc_init_array
    calls[-1]  main()

Only the 32-bit BL instruction is decoded, because that is all the handler
uses. A custom build with a different startup returns None, and the boot
check then reports that it cannot tell where main() is.
"""
import struct

APP_BASE = 0x0800C000
_SCAN = 0x80        # the handler is 0x3C bytes in every official build


def _bl_target(pc, hw1, hw2):
    s = (hw1 >> 10) & 1
    i1 = 1 - (((hw2 >> 13) & 1) ^ s)
    i2 = 1 - (((hw2 >> 11) & 1) ^ s)
    imm = (s << 24) | (i1 << 23) | (i2 << 22) | ((hw1 & 0x3FF) << 12) | ((hw2 & 0x7FF) << 1)
    if s:
        imm -= 1 << 25
    return pc + 4 + imm


def calls(payload, base=APP_BASE):
    """-> the BL targets of the reset handler, in order, up to its `bx lr`."""
    reset = struct.unpack_from("<I", payload, 4)[0] & ~1
    off = reset - base
    if not 0 <= off < len(payload) - 4:
        return []
    out, end = [], min(len(payload) - 2, off + _SCAN)
    while off < end:
        hw1 = struct.unpack_from("<H", payload, off)[0]
        if hw1 == 0x4770:                               # bx lr
            break
        if hw1 >> 11 in (0b11101, 0b11110, 0b11111):     # a 32-bit instruction
            hw2 = struct.unpack_from("<H", payload, off + 2)[0]
            if hw1 >> 11 == 0b11110 and hw2 & 0xD000 == 0xD000:
                out.append(_bl_target(base + off, hw1, hw2))
            off += 4
        else:
            off += 2
    return out


def milestones(payload, base=APP_BASE):
    """-> {"SystemInit": addr, "__libc_init_array": addr, "main()": addr},
    or None when the reset handler does not have the standard shape."""
    found = calls(payload, base)
    if len(found) < 3:
        return None
    return {"SystemInit": found[0], "__libc_init_array": found[-2],
            "main()": found[-1]}


def resc_lines(path):
    """The Renode variable lines boot.resc reads its hook addresses from.
    For an image whose startup is not recognised the hooks go on address 0,
    which never executes, so no milestone is reported rather than a wrong one."""
    found = milestones(open(path, "rb").read()) or {}
    return ["$ms_systeminit = 0x%08X" % found.get("SystemInit", 0),
            "$ms_libc = 0x%08X" % found.get("__libc_init_array", 0),
            "$ms_main = 0x%08X" % found.get("main()", 0)]
