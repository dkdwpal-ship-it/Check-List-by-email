"""Minimal OLE compound-file writer, just enough to build .msg fixtures for tests.

Every stream must be smaller than 4096 bytes: they are all packed into the mini stream (64-byte sectors).
"""

import struct

SECTOR = 512
FREESECT, ENDOFCHAIN, FATSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF


def _dir_entry(name, etype, child=NOSTREAM, left=NOSTREAM, right=NOSTREAM, start=ENDOFCHAIN, size=0):
    raw = name.encode("utf-16-le") + b"\x00\x00"
    e = raw.ljust(64, b"\x00")
    e += struct.pack("<HBB", len(raw), etype, 1)  # name length, type, color=black
    e += struct.pack("<III", left, right, child)
    e += b"\x00" * 16 + b"\x00" * 4 + b"\x00" * 16  # clsid, state bits, times
    e += struct.pack("<IQ", start, size)
    return e


def _cmp_key(name):
    return (len(name), name.upper())


def write_cfb(path, tree):
    """tree: {name: bytes | dict} — nested dicts become storages."""
    entries = []  # [name, type, children(list of idx), data]
    data_sectors = []

    def add(name, node):
        idx = len(entries)
        if isinstance(node, dict):
            entries.append([name, 1, [], None])
            for child_name, child in node.items():
                entries[idx][2].append(add(child_name, child))
        else:
            entries.append([name, 2, [], node])
        return idx

    root = add("Root Entry", tree)
    entries[root][1] = 5

    # pack all streams into the mini stream
    MINI = 64
    mini = b""
    minifat = []
    starts = {}
    for i, (_, etype, _, data) in enumerate(entries):
        if etype == 2 and data:
            assert len(data) < 4096, "test writer supports only small streams"
            n = (len(data) + MINI - 1) // MINI
            first = len(mini) // MINI
            starts[i] = first
            minifat += [first + k + 1 for k in range(n - 1)] + [ENDOFCHAIN]
            mini += data.ljust(n * MINI, b"\x00")

    chains = []
    next_sector = 0

    def place(blob):
        nonlocal next_sector
        if not blob:
            return ENDOFCHAIN, 0
        n = (len(blob) + SECTOR - 1) // SECTOR
        start = next_sector
        chains.append((start, n))
        data_sectors.append(blob.ljust(n * SECTOR, b"\x00"))
        next_sector += n
        return start, n

    mini_start, _ = place(mini)
    minifat_bytes = struct.pack(f"<{len(minifat)}I", *minifat) if minifat else b""
    minifat_start, minifat_n = place(minifat_bytes.ljust(-(-len(minifat_bytes) // SECTOR) * SECTOR, b"\xff"))

    # each storage's children as a degenerate right-linked list sorted by (len, upper)
    child_of, right_of = {}, {}
    for i, (_, etype, children, _) in enumerate(entries):
        if children:
            ordered = sorted(children, key=lambda c: _cmp_key(entries[c][0]))
            child_of[i] = ordered[0]
            for a, b in zip(ordered, ordered[1:]):
                right_of[a] = b

    dir_bytes = b""
    for i, (name, etype, _, data) in enumerate(entries):
        if i == root:
            start, size = mini_start, len(mini)
        else:
            start, size = starts.get(i, ENDOFCHAIN), len(data) if data else 0
        dir_bytes += _dir_entry(
            name, etype, child=child_of.get(i, NOSTREAM), right=right_of.get(i, NOSTREAM),
            start=start, size=size,
        )
    dir_n = (len(dir_bytes) + SECTOR - 1) // SECTOR
    dir_bytes = dir_bytes.ljust(dir_n * SECTOR, b"\x00")
    dir_start = next_sector
    next_sector += dir_n

    total = next_sector
    fat_n = 1
    while total + fat_n > fat_n * (SECTOR // 4):
        fat_n += 1
    fat_start = total
    fat = [FREESECT] * (fat_n * SECTOR // 4)
    for start, n in chains + [(dir_start, dir_n)]:
        for k in range(n):
            fat[start + k] = start + k + 1 if k < n - 1 else ENDOFCHAIN
    for k in range(fat_n):
        fat[fat_start + k] = FATSECT
    assert fat_n <= 109

    header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16
    header += struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
    header += b"\x00" * 6
    header += struct.pack("<IIIIIIIII", 0, fat_n, dir_start, 0, 4096, minifat_start, minifat_n, ENDOFCHAIN, 0)
    difat = [fat_start + k for k in range(fat_n)] + [FREESECT] * (109 - fat_n)
    header += struct.pack("<109I", *difat)

    with open(path, "wb") as fh:
        fh.write(header)
        for chunk in data_sectors:
            fh.write(chunk)
        fh.write(dir_bytes)
        fh.write(struct.pack(f"<{len(fat)}I", *fat))


def build_msg(path, subject, body, sender_name, sender_email, recipients, submit_time_utc, cc=()):
    """recipients/cc: list of (display name, smtp address)."""

    def u(s):
        return s.encode("utf-16-le")

    def props(header_size, items):
        out = b"\x00" * header_size
        for ptype, pid, value in items:
            out += struct.pack("<HHI", ptype, pid, 6) + value
        return out

    from datetime import datetime

    ft = int((submit_time_utc - datetime(1601, 1, 1)).total_seconds() * 10_000_000)
    tree = {
        "__properties_version1.0": props(32, [(0x0040, 0x0039, struct.pack("<Q", ft))]),
        "__substg1.0_0037001F": u(subject),
        "__substg1.0_1000001F": u(body),
        "__substg1.0_0C1A001F": u(sender_name),
        "__substg1.0_5D01001F": u(sender_email),
        "__substg1.0_1035001F": u(f"<{subject.encode().hex()[:16]}@test>"),
    }
    for i, ((name, addr), rtype) in enumerate([(r, 1) for r in recipients] + [(c, 2) for c in cc]):
        tree[f"__recip_version1.0_#{i:08X}"] = {
            "__properties_version1.0": props(8, [(0x0003, 0x0C15, struct.pack("<iI", rtype, 0))]),
            "__substg1.0_3001001F": u(name),
            "__substg1.0_39FE001F": u(addr),
        }
    write_cfb(path, tree)
