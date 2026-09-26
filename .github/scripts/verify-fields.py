#!/usr/bin/env python3
"""
从内核 Image 里提取验收四字段，用于与原厂逐项比对。

四字段：vermagic / uname -v / cpio mtime / build-id。
原厂侧直接把 `stock-boot/*.img.gz` 传进来即可（脚本按 header v2 切出 kernel 段，
字段偏移 +8 kernel_size 已由 magiskboot 产物字节级核对）。

⚠️ cpio mtime 是这里最容易取错的一项，见 _find_initramfs() 上方注释。

用法:
  python verify-fields.py <Image 或 boot.img[.gz]> [...]
  python verify-fields.py --table <stock-boot目录> <artifacts目录>   # 生成对照表
"""

import gzip
import hashlib
import os
import re
import struct
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# ---------- 四字段提取 ----------

def _strings(data, minlen=12):
    return re.findall(rb"[\x20-\x7e]{%d,}" % minlen, data)


def get_vermagic(data):
    """形如 `4.19.81-perf-g8819887 SMP preempt mod_unload modversions aarch64`。"""
    for s in _strings(data):
        if s.startswith(b"4.19.") and b" SMP preempt " in s:
            return s.decode("ascii", "replace")
    return None


def get_uname_v(data):
    """形如 `#1 SMP PREEMPT Thu Jan 16 03:38:46 CST 2020`。"""
    for s in _strings(data):
        m = re.match(rb"#1 SMP PREEMPT .*", s)
        if m:
            return m.group(0).decode("ascii", "replace")
    return None


def _align4(n):
    """cpio newc 的 4 字节对齐。注意是 (n+2)&~3，不是向上取整。"""
    return (n + 2) & ~3


def _walk_archive(data, off, limit=5000):
    """从 off 顺序解析 cpio newc，返回 (entries, ok)。

    ⚠️ 对齐规则（实测确认，极易写错）：
       下一项头偏移 = align4(cur + 110 + namesize) + align4(filesize)
    **把 110 这个偏移一起对齐**。写成 `cur + 110 + align4(namesize)`
    会差 2 字节而失步（实测首个条目 `dev` 即失败）。
    """
    entries = []
    cur = off
    for _ in range(limit):
        if data[cur:cur + 6] != b"070701":
            return entries, False
        hdr = data[cur:cur + 110]
        try:
            mtime = int(hdr[46:54], 16)
            filesize = int(hdr[54:62], 16)
            namesize = int(hdr[94:102], 16)
        except ValueError:
            return entries, False
        if not (0 < namesize <= 4096):
            return entries, False
        name = data[cur + 110:cur + 110 + namesize - 1].decode("ascii", "replace")
        entries.append((name, mtime, filesize))
        if name == "TRAILER!!!":
            return entries, True
        cur = _align4(cur + 110 + namesize) + _align4(filesize)
    return entries, False


def get_cpio_mtime(data):
    """返回 (mtime, archive偏移, 条目数) 或 (None, None, 0)。

    ⚠️ 内核镜像**未压缩**，随机字节里出现 `070701` 很常见
    （实测原厂 V11.0.5.0 的 kernel 里有 5 处，只有 1 处是真 initramfs）。
    直接 `data.find(b"070701")` 会命中假阳性（那处的 mtime 字段根本不是十六进制）。
    判别法：真 initramfs **以 `TRAILER!!!` 收尾、各条目 mtime 一致且非零**。
    """
    offs = []
    i = data.find(b"070701")
    while i >= 0:
        offs.append(i)
        i = data.find(b"070701", i + 1)
    best = None
    for off in offs:
        entries, ok = _walk_archive(data, off)
        if not ok or len(entries) < 3:
            continue
        vals = [m for _n, m, _s in entries if m > 0]
        if not vals or len(set(vals)) != 1:
            continue
        if best is None or len(entries) > best[2]:
            best = (vals[0], off, len(entries))
    return best if best else (None, None, 0)


def get_build_id(data):
    """ELF note `GNU\\0`：namesz=4, descsz=20|16|8, type=3，desc 即 build-id。"""
    for descsz in (20, 16, 8):
        needle = struct.pack("<III", 4, descsz, 3) + b"GNU\x00"
        i = data.find(needle)
        if i >= 0:
            return data[i + 16:i + 16 + descsz].hex()
    return None


# ---------- boot.img → kernel 段 ----------

def load_kernel(path):
    """接受 kernel Image，或 boot.img / boot.img.gz（自动切出 kernel 段）。"""
    if path.endswith(".gz"):
        with gzip.open(path, "rb") as f:
            img = f.read()
    else:
        with open(path, "rb") as f:
            img = f.read()
    if img[:8] == b"ANDROID!":
        # 字段偏移已核对：+8 kernel_size, +16 ramdisk_size, +36 page_size
        ksize = struct.unpack("<I", img[8:12])[0]
        return img[4096:4096 + ksize]
    return img  # 本来就是裸 Image


def fields(path):
    data = load_kernel(path)
    mtime, aoff, n = get_cpio_mtime(data)
    return {
        "path": path,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "vermagic": get_vermagic(data),
        "uname_v": get_uname_v(data),
        "cpio_mtime": mtime,
        "cpio_at": aoff,
        "cpio_entries": n,
        "build_id": get_build_id(data),
    }


def show(f):
    print("== %s" % f["path"])
    print("   大小      : %d" % f["size"])
    print("   vermagic  : %s" % (f["vermagic"] or "（未找到）"))
    print("   uname -v  : %s" % (f["uname_v"] or "（未找到）"))
    print("   cpio mtime: %s   (偏移 %s, %s 个条目)" % (f["cpio_mtime"], f["cpio_at"], f["cpio_entries"]))
    print("   build-id  : %s" % (f["build_id"] or "（缺失）"))


def main(argv):
    args = argv[1:]
    if not args:
        print(__doc__)
        return 2
    if args[0] == "--table":
        return table(args[1:])
    for p in args:
        show(fields(p))
        print()
    return 0


def table(args):
    """对照表：原厂 vs artifact（按 run 目录）。"""
    if len(args) != 2:
        print("用法: --table <stock-boot目录> <artifacts目录>")
        return 2
    stock_dir, art_dir = args
    print("=== 原厂基准（stock-boot/*.img.gz）===")
    for n in sorted(os.listdir(stock_dir)):
        if not n.endswith(".img.gz"):
            continue
        f = fields(os.path.join(stock_dir, n))
        print("  %-34s cpio=%-11s %s" % (n[:34], f["cpio_mtime"], f["vermagic"] or "-"))
    print()
    print("=== CI artifact（artifacts/r<runid>）===")
    if os.path.isdir(art_dir):
        for d in sorted(os.listdir(art_dir)):
            img = os.path.join(art_dir, d, "kernel-image", "arch", "arm64", "boot", "Image")
            if not os.path.exists(img):
                continue
            f = fields(img)
            print("  %-14s cpio=%-11s %s" % (d, f["cpio_mtime"], f["uname_v"] or "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
