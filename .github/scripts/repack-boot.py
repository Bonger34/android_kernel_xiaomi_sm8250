"""纯 Python 重打包 Android boot 镜像（header v2）—— 把底包的 kernel 换成新编的 Image。

用法:
  python repack-boot.py <底包 boot.img> <新 Image> [-o 输出路径] [--expect <sha256>]

为什么需要它:
  `tools/repack-boot.ps1` 是 PowerShell + 仓库里那份 Windows 版 `magiskboot.exe`，
  **Ubuntu CI 上跑不了**。本脚本只用 CPython 标准库（zlib / hashlib / struct），
  **跨平台、零依赖**，而且已实测能**逐字节复现**已发布的
  `boot-LOS23.2-ksu-0.9.5-umi.img`
  （sha256 `4c556237ded71dba55b27aa2dce18d36b9f2f3de55dbb3df36d52e2b4dc79035`）。

已验证的参数（**不要改**，改一个字节就复现不出）:
  · ramdisk 重压缩 = `zlib.compressobj(9, DEFLATED, -15)`（裸 deflate、level 9、默认 memLevel/strategy）
    ＋**手写** 10 字节 gzip 头 `1f 8b 08 00 00 00 00 00 02 0a`（MTIME=0 / XFL=2 / OS=10）
  · header 的 `id` 字段 = AOSP mkbootimg **v2 的完整 sha1 公式**（连 recovery_dtbo 与 dtb 一起算），
    只取前 20 字节写回偏移 576
  · AVB footer 要改**两个**字段：`original_image_size`（偏移 12）与 `vbmeta_offset`（偏移 20），
    都是大端 u64 —— **只改前一个会差 4 字节**

⚠️ 已知边界（都是**实测**才知道的，别当成普适结论）:
  1. 只在 23.2 的底包 + 那一个 Image 上验证过，**未跨底包泛化**
  2. `vbmeta` 与 `dtb` 是**原样搬运**；只在 23.2 的底包上确认过它们与发布件相同
  3. **zlib 版本会改字节**：原版 1.2.11 / 1.2.13 输出一致，Chromium 的 motley 分支明显不同；
     **zlib 1.3.x 未实测** ⇒ CI 里别随便换 runner 镜像（现在用 `ubuntu-22.04`）
  4. 本脚本**不做 AVB 重签**，只搬 footer/vbmeta（理由见第 2 条）

退出码: 0 = 成功（给了 --expect 且不符则返回 1）；2 = 用法错误；3 = 输入不合法。

⚠️ 本文件有**两份，必须逐字节相同**：
   工作区的 `tools/repack-boot.py`，与 LOS 工作仓库里的 `.github/scripts/repack-boot.py`
   （CI 在 checkout 出来的内核树里跑**后者**，见 `.github/workflows/build.yml` 的环境哨兵步骤）。
   为什么仓库那份不放进 `tools/`：内核树**自带一个 `tools/`**（上游的，会被上游 merge 触碰），
   项目自己的 CI 工具一律放 `.github/` 下。改一份就同步改另一份。
"""

import argparse
import hashlib
import os
import struct
import sys
import zlib

PAGE_FALLBACK = 4096
GZIP_HEAD = bytes([0x1F, 0x8B, 0x08, 0x00, 0, 0, 0, 0, 0x02, 0x0A])  # MTIME=0 / XFL=2 / OS=10
AVB_MAGIC = b"AVBf"
BOOT_MAGIC = b"ANDROID!"
ID_OFF = 576          # header v2 的 id[8] 字段
RECOVERY_DTBO_SIZE_OFF = 1632
DTB_SIZE_OFF = 1648
MIN_HEADER = 1660     # header v2 用到的最后一个字段之后


def align(n, page):
    """向上取整到页。"""
    return (n + page - 1) // page * page


def parse_header(buf):
    """读出 header v2 里我们需要的字段；不是 v2 就直接拒绝。"""
    if len(buf) < MIN_HEADER or buf[:8] != BOOT_MAGIC:
        raise ValueError("不是 Android boot 镜像（magic 不是 ANDROID!）")
    hdr_version = struct.unpack_from("<I", buf, 40)[0]
    if hdr_version != 2:
        raise ValueError("只支持 header v2，本文件是 v%d" % hdr_version)
    return {
        "kernel_size": struct.unpack_from("<I", buf, 8)[0],
        "ramdisk_size": struct.unpack_from("<I", buf, 16)[0],
        "second_size": struct.unpack_from("<I", buf, 24)[0],
        "page_size": struct.unpack_from("<I", buf, 36)[0],
        "recovery_dtbo_size": struct.unpack_from("<I", buf, RECOVERY_DTBO_SIZE_OFF)[0],
        "dtb_size": struct.unpack_from("<I", buf, DTB_SIZE_OFF)[0],
    }


def segment_offsets(h, page):
    """按 AOSP 的排布规则算出各段起始偏移（每段都页对齐）。"""
    kernel = page
    ramdisk = kernel + align(h["kernel_size"], page)
    second = ramdisk + align(h["ramdisk_size"], page)
    rd_dtbo = second + align(h["second_size"], page)
    dtb = rd_dtbo + align(h["recovery_dtbo_size"], page)
    vbmeta = dtb + align(h["dtb_size"], page)
    return {"kernel": kernel, "ramdisk": ramdisk, "second": second,
            "rd_dtbo": rd_dtbo, "dtb": dtb, "vbmeta": vbmeta}


def recompress_ramdisk(gz):
    """把底包的 ramdisk 解压后**按发布件的参数**重新压缩，得到逐字节相同的 gzip 流。"""
    raw = zlib.decompress(gz[10:-8], -15)          # 跳过 10 字节 gzip 头 + 8 字节尾
    co = zlib.compressobj(9, zlib.DEFLATED, -15)   # 裸 deflate、level 9
    return (GZIP_HEAD + co.compress(raw) + co.flush()
            + struct.pack("<II", zlib.crc32(raw) & 0xFFFFFFFF, len(raw) & 0xFFFFFFFF))


def boot_id(kernel, ramdisk, second, rd_dtbo, dtb):
    """AOSP mkbootimg 的 v2 公式：五段内容各自跟一个 LE u32 长度，取 sha1 前 20 字节。"""
    s = hashlib.sha1()
    for blob in (kernel, ramdisk, second, rd_dtbo, dtb):
        s.update(blob)
        s.update(struct.pack("<I", len(blob)))
    return s.digest()


def repack(stock, kernel):
    """底包 + 新 Image -> 新的 boot.img 字节串。分段与填充规则照着原镜像走。"""
    h = parse_header(stock)
    page = h["page_size"] or PAGE_FALLBACK
    off = segment_offsets(h, page)

    second = stock[off["second"]:off["second"] + h["second_size"]]
    rd_dtbo = stock[off["rd_dtbo"]:off["rd_dtbo"] + h["recovery_dtbo_size"]]
    dtb = stock[off["dtb"]:off["dtb"] + h["dtb_size"]]
    ramdisk = recompress_ramdisk(stock[off["ramdisk"]:off["ramdisk"] + h["ramdisk_size"]])

    # AVB footer 是整份镜像最后的 64 字节；从它取 vbmeta 的大小与位置
    if stock[-64:-60] != AVB_MAGIC:
        raise ValueError("末尾没有 AVB footer（magic 不是 AVBf）—— 本工具只处理带 footer 的镜像")
    footer = bytearray(stock[-64:])
    old_vb_off = struct.unpack_from(">Q", footer, 20)[0]
    vbmeta_size = struct.unpack_from(">Q", footer, 28)[0]
    vbmeta = stock[old_vb_off:old_vb_off + vbmeta_size]

    # header 页：照抄底包，只改两个尺寸字段并重算 id
    hdr = bytearray(stock[:page])
    struct.pack_into("<I", hdr, 8, len(kernel))
    struct.pack_into("<I", hdr, 16, len(ramdisk))
    hdr[ID_OFF:ID_OFF + 20] = boot_id(kernel, ramdisk, second, rd_dtbo, dtb)

    # 按新尺寸重排偏移；footer 的**两个**字段都要跟着改
    new_off = segment_offsets({"kernel_size": len(kernel), "ramdisk_size": len(ramdisk),
                               "second_size": len(second), "recovery_dtbo_size": len(rd_dtbo),
                               "dtb_size": len(dtb)}, page)
    vb_off = new_off["vbmeta"]
    struct.pack_into(">Q", footer, 12, vb_off)   # original_image_size
    struct.pack_into(">Q", footer, 20, vb_off)   # vbmeta_offset（容易漏）

    # ⚠️ 整份镜像要**保持与底包一样的总大小**（那是 boot 分区的大小，128 MiB），
    #    不是「内容对齐到页」。vbmeta 之后到 footer 之间是一大片零，别省掉它。
    total = len(stock)
    need = align(vb_off + vbmeta_size, page) + 64
    if need > total:
        raise ValueError("新内容 (%d) 装不进底包的大小 (%d)" % (need, total))
    out = bytearray(total)
    for offset, blob in ((0, bytes(hdr)),
                         (new_off["kernel"], kernel),
                         (new_off["ramdisk"], ramdisk),
                         (new_off["second"], second),
                         (new_off["rd_dtbo"], rd_dtbo),
                         (new_off["dtb"], dtb),
                         (vb_off, vbmeta),
                         (total - 64, bytes(footer))):
        out[offset:offset + len(blob)] = blob
    return bytes(out)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def env_line():
    """一行环境指纹。重打包对 **zlib 版本**敏感，所以每次都把它打出来 —— 成功时是证据，
    失败时是判据（不然读到失败信息的人第一件事还是得去问"你是哪个 zlib"）。"""
    return "python %s / zlib %s (runtime %s)" % (
        sys.version.split()[0], zlib.ZLIB_VERSION, zlib.ZLIB_RUNTIME_VERSION)


def report_mismatch(expect, got, stock_path, kernel_path):
    """--expect 不符时的出口。**必须说清三件事**：期望什么、实际得到什么、最可能的原因是什么。

    这是环境哨兵（CI 构建流程的第一步）唯一的失败出口 —— 它得自己解释自己，
    因为读这段的人正要判断「是环境变了，还是产物变了」。
    """
    print()
    print("❌ 重打包结果与 --expect 不符 —— 本机的重打包环境与产出该期望值的那次不是同一个")
    print("   期望   : %s" % expect.lower())
    print("   实际   : %s" % got)
    print("   底包   : %s  sha256 %s" % (stock_path, sha256_file(stock_path)))
    print("   新内核 : %s  sha256 %s" % (kernel_path, sha256_file(kernel_path)))
    print("   环境   : %s" % env_line())
    print("   最可能的原因（按可能性排序）:")
    print("     ① **压缩库版本不同**。ramdisk 要用原版 zlib 重新压缩，输出字节随版本变。")
    print("        已实测 zlib 1.2.11 与 1.2.13 输出一致；**1.3.x 在已验证区间外**。")
    print("        ⇒ CI 的 `runs-on` 必须留在 ubuntu-22.04；换 runner 镜像前先重跑环境哨兵。")
    print("     ② **两个输入件不是期望的那一份**。先核对上面两个 sha256 是否与清单")
    print("        （docs/manifests/los-releases-sha256.txt）逐字一致。")
    print("     ③ 本脚本、或它依赖的标准库行为被改动过 —— 那会直接改变输出字节。")


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(add_help=True, description="纯 Python 重打包 boot 镜像（见文件头注释）")
    ap.add_argument("stock", help="底包 boot.img")
    ap.add_argument("kernel", help="新的裸 Image")
    ap.add_argument("-o", "--out", default=None, help="输出路径（默认 boot-repacked.img）")
    ap.add_argument("--expect", default=None, help="期望的 sha256；不符则返回 1")
    a = ap.parse_args(argv[1:])

    out_path = a.out or "boot-repacked.img"
    try:
        stock = open(a.stock, "rb").read()
        kernel = open(a.kernel, "rb").read()
    except OSError as e:
        print("读输入失败: %s" % e)
        return 3
    try:
        data = repack(stock, kernel)
    except ValueError as e:
        print("输入不合法: %s" % e)
        return 3

    with open(out_path, "wb") as f:
        f.write(data)
    got = hashlib.sha256(data).hexdigest()
    print("底包   %s (%d 字节)" % (a.stock, len(stock)))
    print("新内核 %s (%d 字节)" % (a.kernel, len(kernel)))
    print("输出   %s (%d 字节)" % (out_path, len(data)))
    print("sha256 %s" % got)
    print("环境   %s" % env_line())
    if a.expect:
        ok = got.lower() == a.expect.lower()
        print("期望   %s" % a.expect)
        if not ok:
            report_mismatch(a.expect, got, a.stock, a.kernel)
            return 1
        print("结果   ✅ 逐字节相同")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
