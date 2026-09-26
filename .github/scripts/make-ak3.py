#!/usr/bin/env python3
"""
从 AnyKernel3 模板 + 自建内核，打出一个可刷的 zip。

为什么需要它：AnyKernel3 是**在设备上**就地替换 boot 分区里的内核、保留当前 ramdisk。
umi **不是 A/B 机型**、`recovery` 是**独立分区**，所以这个形态**与 LineageOS 版本无关** ——
比 `boot.img` 更适合跨版本分发（见 docs/los-line/spec.md 实现决定 15）。

🔧 订正 2026-09-25 —— **原来写的是**：「umi 是 A/B 机型、`boot` 分区同时装 recovery」。
**这句是错的**（官方六重一手证据）：

- `LineageOS/android_device_xiaomi_sm8250-common` 的 `BoardConfigCommon.mk`：umi 落
  `AB_OTA_UPDATER := false` 的 else 支，该支**另有** `BOARD_RECOVERYIMAGE_PARTITION_SIZE := 134217728`
  （真 VAB 支是 boot 192 MiB，且根本不发 recovery.img）。
- 官方 `fstab.qcom` 有独立 `/dev/block/bootdevice/by-name/recovery → /recovery`，全文件**无 `slotselect`**。
- 官方设备树的 releasetools.py 写分区名**不带 `_a`/`_b` 槽位后缀**。
- 官方镜像头实测：`boot.img` 的 ramdisk = **1,494,000 B**（普通 boot ramdisk），
  recovery.img 的 ramdisk = **15,082,826 B** —— 同一颗内核，**boot 里没有 recovery**。
- 官方 wiki yml：`recovery_partition_name: recovery`（真 A/B 的 Pixel 是 `vendor_boot`）。

⇒ 正确说法：**umi 不是 A/B，`recovery` 是独立分区。**
⚠️ **「与 LineageOS 版本无关」这个结论仍然成立**，但**理由要换**：不是「A/B + recovery-in-boot」，
而是「**recovery 独立 ⇒ 可换第三方 recovery、或用内核管理器就地打补丁；zip 就地改 boot，
与具体 ROM 构建无关**」。

⚠️ **Windows 特有的坑**：zip 里 `anykernel.sh`、`META-INF/com/google/android/update-binary`、
`tools/*` **必须带可执行位**，而 Windows 磁盘上根本没有这个概念（提取出来就丢了）。
所以本工具**直接从 tarball 里读每个文件的 mode**，不经过磁盘提取那一步。

时间戳固定为 1980-01-01，因此**同一输入产出逐字节相同的 zip**（与 T4 的可复现要求一致）。

用法:
  python tools/make-ak3.py --kernel <Image> --template <AnyKernel3 tar.gz> --out <zip>
                           [--device umi] [--kernel-string "..."]

模板获取（本机原生 HTTPS 用不了，走 Python —— 见 docs/PROJECT.md §7 坑表 #20）:
  python -c "import urllib.request;open('ak3.tar.gz','wb').write(
      urllib.request.urlopen('https://codeload.github.com/osm0sis/AnyKernel3/tar.gz/refs/heads/master').read())"
"""

import argparse
import hashlib
import io
import os
import re
import sys
import tarfile
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

# 可执行位规则 —— 从模板 tarball 实测得到：0755 的是 anykernel.sh、update-binary 与 tools/*
EXEC_EXACT = {"anykernel.sh", "META-INF/com/google/android/update-binary"}
EXEC_PREFIX = ("tools/",)

PROPS = """kernel.string={ks}
do.devicecheck=1
do.modules=0
do.systemless=1
do.cleanup=1
do.cleanuponabort=0
device.name1={dev}
device.name2=
device.name3=
device.name4=
device.name5=
supported.versions=
supported.patchlevels=
supported.vendorpatchlevels="""


def mode_for(name):
    """按实测规则给权限：可执行的给 0755，其余 0644。"""
    if name in EXEC_EXACT or name.startswith(EXEC_PREFIX):
        return 0o755
    return 0o644


def patch_anykernel(text, dev, ks):
    """只改必要的地方，保留上游的样板（dump_boot / write_boot / ak3-core.sh）。"""
    n = len(re.findall(r"properties\(\) \{ '.*?'; \} # end properties", text, re.S))
    if n != 1:
        raise SystemExit("模板的 properties 块匹配到 %d 处（应为 1），上游可能改版了" % n)
    text = re.sub(r"properties\(\) \{ '.*?'; \} # end properties",
                  "properties() { '\n%s\n'; } # end properties" % PROPS.format(ks=ks, dev=dev),
                  text, flags=re.S)

    if not re.search(r"^BLOCK=", text, re.M):
        raise SystemExit("模板里找不到 BLOCK= —— 上游可能改版了")
    text = re.sub(r"^BLOCK=.*$", "BLOCK=boot;", text, flags=re.M)

    # ⚠️ **`IS_SLOT_DEVICE` 必须是 `0`**（2026-09-25 裁定并改）。
    # 原文写 `1` 是「umi 是 A/B 机型」那个错误前提留下的**行为后果** —— 不只是文字错。
    # **代码级证据**（拆开已发布的 zip 逐行追）：`tools/ak3-core.sh` 第 908-910 行
    #     if [ ! "$SLOT" -a "$IS_SLOT_DEVICE" == 1 ]; then
    #       abort "Unable to determine active slot. Aborting..."; fi;
    # umi 不是 A/B ⇒ `ro.boot.slot_suffix` 与 `androidboot.slot*` **永远取不到** ⇒ `SLOT` 为空
    # ⇒ 设成 `1` 时**每次刷入都必然 abort**。设成 `0` 则不匹配 `case $IS_SLOT_DEVICE in 1|auto)`，
    # 整段槽位探测被跳过，第 965 行的 `for part in $name$SLOT $name` 会去试不带后缀的 `boot`。
    # ⇒ 已发布的旧 zip（`98419f8a…`）是**刷不进去的**；本次改值后重打重发。
    if not re.search(r"^IS_SLOT_DEVICE=", text, re.M):
        raise SystemExit("模板里找不到 IS_SLOT_DEVICE= —— 上游可能改版了")
    text = re.sub(r"^IS_SLOT_DEVICE=.*$", "IS_SLOT_DEVICE=0;", text, flags=re.M)

    # 模板自带的设备专属 ramdisk 改动（Galaxy Nexus 的 init.rc / fstab.tuna）全部删掉 ——
    # 我们只换内核，**不动 ramdisk 内容**（这正是选 AK3 的理由）。
    text, k = re.subn(r"\n# init\.rc\n.*?\n(write_boot;)", r"\n\1", text, flags=re.S)
    if k != 1:
        raise SystemExit("模板的设备专属 ramdisk 段匹配到 %d 处（应为 1）" % k)

    text = text.replace("### AnyKernel3 Ramdisk Mod Script",
                        "### AnyKernel3 Ramdisk Mod Script\n"
                        "## 为小米 10（%s）生成 —— 只替换内核，不动 ramdisk 内容\n"
                        "## 由 tools/make-ak3.py 生成，请勿手改" % dev, 1)
    return text


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--kernel", required=True, help="自建内核 Image 的路径")
    ap.add_argument("--template", required=True, help="AnyKernel3 模板 .tar.gz")
    ap.add_argument("--out", required=True, help="输出 zip 路径")
    ap.add_argument("--device", default="umi")
    ap.add_argument("--kernel-string", default="KernelSU v0.9.5 for LineageOS by Bonger34")
    a = ap.parse_args(argv[1:])

    kernel = open(a.kernel, "rb").read()
    ksha = hashlib.sha256(kernel).hexdigest()
    print("内核: %s  %d 字节  sha256=%s" % (a.kernel, len(kernel), ksha[:16]))

    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    entries = []          # (name, bytes, mode)
    with tarfile.open(a.template, "r:gz") as t:
        prefix = None
        for m in t.getmembers():
            if not m.isfile():
                continue
            name = m.name.split("/", 1)[1] if "/" in m.name else m.name
            if prefix is None:
                prefix = m.name.split("/")[0]
            data = t.extractfile(m).read()
            if name == "anykernel.sh":
                data = patch_anykernel(data.decode("utf-8"), a.device, a.kernel_string).encode("utf-8")
            entries.append((name, data, mode_for(name)))
        if prefix is None:
            raise SystemExit("模板是空的")
        print("模板: %s  %d 个文件（顶层目录 %s）" % (a.template, len(entries), prefix))

    entries.append(("Image", kernel, 0o644))   # 内核放 zip 根，AK3 按名取用

    with zipfile.ZipFile(a.out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data, mode in sorted(entries):
            zi = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)   # 固定时间戳 → 可复现
            zi.external_attr = (mode & 0xFFFF) << 16
            zi.compress_type = zipfile.ZIP_DEFLATED
            # ⚠️ 必须**显式**钉住 `create_system`：`zipfile` 默认按宿主平台填它
            #    （`sys.platform == 'win32'` ⇒ 0，否则 3），而它落在**中央目录的
            #    `version made by` 字段**里 ⇒ 同一份输入在 Windows 与 Linux 上产出的
            #    zip **不是同一串字节**（实测差 18 个字节，2026-09-26 踩到：
            #    本机 3.10.11/Windows 给 0x0014，CI 3.10.12/Ubuntu 给 0x0314）。
            #    CI 跑在 Linux，所以取 3 —— 与**已发布批次**一致（那批就是 CI 产出的），
            #    本机 Windows 上跑出来的也就与它逐字节相同了。
            #    见 PROJECT.md §7 坑表 #36。
            zi.create_system = 3
            z.writestr(zi, data)

    # 自检：回读 zip，核对结构与权限
    print("\n=== 自检：回读 zip ===")
    fails = []
    with zipfile.ZipFile(a.out) as z:
        names = z.namelist()
        got = {i.filename: (i.external_attr >> 16) & 0o777 for i in z.infolist()}
        need = ["anykernel.sh", "Image", "META-INF/com/google/android/update-binary",
                "META-INF/com/google/android/updater-script", "tools/ak3-core.sh",
                "tools/magiskboot"]
        for n in need:
            ok = n in names
            if not ok:
                fails.append("缺 " + n)
            print("  %-52s %s" % (n, "✅" if ok else "❌"))
        for n in ["anykernel.sh", "META-INF/com/google/android/update-binary",
                  "tools/ak3-core.sh", "tools/magiskboot"]:
            if n in got:
                ok = got[n] == 0o755
                if not ok:
                    fails.append("%s 权限 %o != 755" % (n, got[n]))
                print("  %-52s %s 权限 %o" % (n, "✅" if ok else "❌", got[n]))
        # ⚠️ 字节级可复现的两项：`version made by` 里的宿主字节必须钉死，
        #    而 `version needed` 必须仍是 20（`zipfile` 会按压缩方式抬高它，
        #    一旦抬高，zip 的字节就跟着变）。见上面 `create_system` 的注释。
        for i in z.infolist()[:1]:
            ok = (i.create_system, i.create_version, i.extract_version) == (3, 20, 20)
            if not ok:
                fails.append("zip 头部没有钉住：create_system=%d create_version=%d "
                             "extract_version=%d（应为 3/20/20）"
                             % (i.create_system, i.create_version, i.extract_version))
            print("  %-52s %s create_system=%d create_version=%d extract_version=%d"
                  % ("zip 头部与宿主平台无关", "✅" if ok else "❌",
                     i.create_system, i.create_version, i.extract_version))
        if "Image" in names:
            same = hashlib.sha256(z.read("Image")).hexdigest() == ksha
            if not same:
                fails.append("zip 里的 Image 与输入不一致")
            print("  %-52s %s" % ("Image == 输入内核", "✅" if same else "❌"))
        ak = z.read("anykernel.sh").decode("utf-8")
        for pat, why in ((r"device\.name1=%s" % a.device, "设备检查含 %s" % a.device),
                         (r"^BLOCK=boot;$", "BLOCK=boot"),
                         (r"^IS_SLOT_DEVICE=0;$", "IS_SLOT_DEVICE=0（**必须是 0**，见 patch_anykernel 的注释）")):
            ok = re.search(pat, ak, re.M) is not None
            if not ok:
                fails.append(why)
            print("  %-52s %s" % (why, "✅" if ok else "❌"))

    zsha = hashlib.sha256(open(a.out, "rb").read()).hexdigest()
    print("\n输出: %s  %d 字节  sha256=%s" % (a.out, os.path.getsize(a.out), zsha))
    if fails:
        print("\n❌ 自检不通过：")
        for x in fails:
            print("   - " + x)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
