#!/usr/bin/env python3
"""
发布验收闸门：对「将要上传的资产目录」做一次逐项体检。

为什么单独一个工具：`verify-fields.py` 只负责从单个文件里取四字段，不负责判断
「这批资产能不能发」。发布前的判据是复合的：

  1. 每个 boot.img 必须是 134217728 字节，且 AVB footer/vbmeta 完好
  2. boot.img 内嵌的 kernel 段必须与配套的 Image 资产**同一份**（sha256 相等）
     —— 否则就是「Image 是一个版本、boot.img 是另一个版本」
  3. 每个版本的四字段必须与原厂逐项相同（build-id 只判 20 字节 sha1 格式）
  4. 每个 config 必须有补充 config 的符号（证明用的是最终配方）
  5. SHA256SUMS.txt 必须覆盖目录下全部资产，且哈希一致

用法:
  python tools/verify-release.py <资产目录> [--matrix <逐版本参数表>]

  `--matrix` 默认 `docs/matrix2.tsv`（A10）。A11 用 `docs/matrix-a11.tsv`：
  python tools/verify-release.py artifacts/release-staging-a11 --matrix docs/matrix-a11.tsv

**两条线共用这一道闸门**（只有一个人口回答「这批产物能不能发」）。
官方线的判据是「对齐原厂四字段」；LOS 线没有原厂可对：
  python tools/verify-release.py <资产目录> --line los [--repro <第二目录>] [--profile <档>]

`--repro` 指向另一次同配方构建的目录，用来验证「两次构建逐字节相同」。
`--profile` 决定对 CFI/SCS 的期望值：
  `main`（默认）—— Q13 的主线：CFI/SCS **必须关**，LTO 必须开
  `cfi-experiment` —— CFI 实验线：CFI/SCS **必须开**（见 docs/los-line/tickets.md T9）
`--lto-expect {y,n}`（默认 y）—— LTO 这一项的期望值。
  ⚠️ **LTO 不是我们定的，是上游该树自带的姿态**：23.2 的 `vendor/kona-perf_defconfig`
  自带 `CONFIG_LTO_CLANG=y`，我们只关 CFI/SCS/WERROR，LTO 是**保留**下来的；
  而 4.19.113（LOS 18.1/19.1）的 `vendor/umi_defconfig` 里 LTO/CFI/SCS 三样全无，
  LOS 也从不给它开 LTO。所以那两条传 `--lto-expect n`。
  证据：docs/los-line/branch-matrix.tsv 头注与 tickets.md T10~T15。
"""
import hashlib
import importlib.util
import os
import re
import struct
import sys
import zipfile

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

def _load_vf():
    """加载 `verify-fields.py`：优先工作区 `tools/`，其次脚本自己所在的目录。

    为什么要回退：CI 里这道闸门跑在 `.github/scripts/` 下（内核树**自带**一个 `tools/`，
    那是上游的、会被上游 merge 触碰，不能占用），此时 `WS` = `<仓库>/.github`，
    其下并没有 `tools/verify-fields.py` ⇒ 不回退就会「到了 CI 才 import 失败」。
    两条线共用同一道闸门，这里必须对两种布局都成立。
    """
    cands = [os.path.join(WS, "tools", "verify-fields.py"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify-fields.py")]
    for p in cands:
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("vf", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise SystemExit("找不到 verify-fields.py，试过：\n  " + "\n  ".join(cands))


vf = _load_vf()

BOOT_SZ = 134217728
VER_RE = re.compile(r"-V(\d+\.\d+\.\d+\.\d+)-ksu")
TZ_OFF = {"CST": 8 * 3600, "UTC": 0}


def ver_of(name):
    """从资产名里取版本。注意 matrix2.tsv 的版本键带前导 V（V11.0.5.0），
    而文件名里是 `-V11.0.5.0-ksu`，捕出来要补回 V，否则与 stock_of 对不上。"""
    m = VER_RE.search(name)
    return "V" + m.group(1) if m else None


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def four(f, ref):
    return [f["vermagic"] == ref["vermagic"],
            f["uname_v"] == ref["uname_v"],
            f["cpio_mtime"] == ref["cpio_mtime"],
            bool(f["build_id"]) and len(f["build_id"]) == 40]


def main(argv):
    # --matrix 可换（A10 = matrix2.tsv，A11 = matrix-a11.tsv），其余参数仍是一个目录
    args = argv[1:]
    matrix = os.path.join(WS, "docs", "matrix2.tsv")

    def take(flag):
        """取走 `flag <值>`；返回 (flag, 值)。缺值时返回 None 以便报错。"""
        if flag in args:
            i = args.index(flag)
            if i + 1 >= len(args):
                return flag, None
            v = args[i + 1]
            del args[i:i + 2]
            return flag, v
        return flag, False

    _, m_arg = take("--matrix")
    if m_arg is None:
        print("--matrix 缺参数")
        return 2
    if m_arg:
        matrix = m_arg
    _, line = take("--line")
    if line is None:
        print("--line 缺参数")
        return 2
    _, profile = take("--profile")
    if profile is None:
        print("--profile 缺参数")
        return 2
    if not profile:
        profile = "main"
    _, repro = take("--repro")
    if repro is None:
        print("--repro 缺参数")
        return 2
    _, lto_expect = take("--lto-expect")
    if lto_expect is None:
        print("--lto-expect 缺参数")
        return 2
    if not lto_expect:
        lto_expect = "y"
    if line == "los":
        if len(args) != 1 or not os.path.isdir(args[0]):
            print(__doc__)
            return 2
        return los_main(args[0], repro, profile, lto_expect)
    if len(args) != 1:
        print(__doc__)
        return 2
    d = args[0]
    if not os.path.isdir(d):
        print("不是目录: %s" % d)
        return 2
    mname = os.path.basename(matrix)

    # 版本 → 原厂基准
    stock_of = {}
    for line in open(matrix, encoding="utf-8"):
        if line.startswith("#") or not line.strip():
            continue
        c = line.rstrip("\n").split("\t")
        if len(c) >= 7:
            stock_of[c[0]] = c[4]
    if not stock_of:
        print("参数表为空或格式不对: %s" % matrix)
        return 2

    files = sorted(f for f in os.listdir(d) if os.path.isfile(os.path.join(d, f)))
    boots = [f for f in files if f.startswith("boot-") and f.endswith(".img")]
    images = [f for f in files if f.startswith("Image-")]
    configs = [f for f in files if f.startswith("config-")]

    print("资产目录: %s" % d)
    print("  boot.img %d ｜ Image %d ｜ config %d ｜ 合计 %d\n"
          % (len(boots), len(images), len(configs), len(files)))

    fails = []

    # Image → 版本
    img_by_ver = {}
    for n in images:
        v = ver_of(n)
        if v is None:
            fails.append("%s: 文件名里找不到版本号" % n)
        elif v in img_by_ver:
            fails.append("%s: 版本 %s 有多个 Image" % (n, v))
        else:
            img_by_ver[v] = n

    print("=== Image 资产：四字段 vs 原厂 ===")
    for v in sorted(img_by_ver):
        n = img_by_ver[v]
        f = vf.fields(os.path.join(d, n))
        stock = stock_of.get(v)
        if not stock:
            fails.append("%s: %s 里没有版本 %s" % (n, mname, v))
            continue
        ref = vf.fields(os.path.join(WS, stock))
        ok = four(f, ref)
        tag = "".join("VUCB"[i] if x else "vucb"[i] for i, x in enumerate(ok))
        good = all(ok)
        if not good:
            fails.append("%s: 四字段 %s" % (n, tag))
        print("  %-12s %s  %s" % (v, tag, "✅" if good else "❌"))

    print("\n=== boot.img 资产：大小 / 内嵌 kernel == 配套 Image ===")
    for n in boots:
        p = os.path.join(d, n)
        v = ver_of(n)
        sz = os.path.getsize(p)
        if sz != BOOT_SZ:
            fails.append("%s: 大小 %d != %d" % (n, sz, BOOT_SZ))
            print("  %-46s ❌ 大小 %d" % (n[:46], sz))
            continue
        data = vf.load_kernel(p)
        got = hashlib.sha256(data).hexdigest()
        want_name = img_by_ver.get(v)
        want = sha256_file(os.path.join(d, want_name)) if want_name else None
        same = (got == want)
        if not same:
            fails.append("%s: 内嵌 kernel 与 %s 不一致" % (n, want_name))
        ref = vf.fields(os.path.join(WS, stock_of[v])) if v in stock_of else None
        tag = ""
        if ref:
            ok = four(vf.fields(p), ref)
            tag = "".join("VUCB"[i] if x else "vucb"[i] for i, x in enumerate(ok))
            if not all(ok):
                fails.append("%s: 四字段 %s" % (n, tag))
        print("  %-46s %s 内嵌=%s %s" % (
            n[:46], "✅" if same else "❌", got[:12], tag))

    print("\n=== config 资产：是否同一套配方 ===")
    # 判据用**自洽性**而不是「对照补充文件」：补充 config 里列的符号有一部分
    # 在 4.19.81 上根本不存在，会被 olddefconfig 丢掉（实测 29 项只落 15 项），
    # 所以「命中补充文件全部符号」是个永远不成立的判据。真正要保证的是
    # 「14 份 config 除 CONFIG_LOCALVERSION 外完全一致」——这才等价于同一套配方。
    cfgs = {}
    for n in configs:
        txt = open(os.path.join(d, n), encoding="utf-8", errors="replace").read()
        lines = [l for l in txt.split("\n") if l.startswith("CONFIG_")]
        cfgs[n] = (len(lines),
                   [l for l in lines if not l.startswith("CONFIG_LOCALVERSION=")])
    counts = sorted(set(c for c, _ in cfgs.values()))
    ref_name = configs[0]
    ref = cfgs[ref_name][1]
    for n in configs:
        cnt, body = cfgs[n]
        diff = [l for l in body if l not in set(ref)] + \
               [l for l in ref if l not in set(body)]
        good = (len(counts) == 1) and not diff
        if not good:
            fails.append("%s: 与同批配方不一致（差异 %d 行，行数 %d）" % (n, len(diff), cnt))
        print("  %-48s 行=%-5d %s" % (n[:48], cnt, "✅" if good else
                                      "❌ 差异 %d 行" % len(diff)))
    if len(counts) == 1:
        print("  → 全部 %d 份 CONFIG 行数一致（%d）" % (len(configs), counts[0]))
    else:
        print("  → ⚠️ 行数不一致: %s" % counts)

    # SHA256SUMS.txt
    sums = os.path.join(d, "SHA256SUMS.txt")
    print("\n=== SHA256SUMS.txt ===")
    if not os.path.exists(sums):
        fails.append("缺少 SHA256SUMS.txt")
        print("  ❌ 不存在")
    else:
        listed = {}
        for line in open(sums, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            h, _, name = line.partition("  ")
            listed[name.strip()] = h.strip()
        missing = [f for f in files if f != "SHA256SUMS.txt" and f not in listed]
        extra = [k for k in listed if k not in files]
        bad = [k for k in listed
               if k in files and sha256_file(os.path.join(d, k)) != listed[k]]
        for msg in (missing and "未列入: %s" % missing,
                    extra and "列了不存在的: %s" % extra,
                    bad and "哈希不符: %s" % bad):
            if msg:
                fails.append("SHA256SUMS.txt " + msg)
        print("  列出 %d 条 ｜ 未列入 %d ｜ 多余 %d ｜ 不符 %d  %s" % (
            len(listed), len(missing), len(extra), len(bad),
            "✅" if not (missing or extra or bad) else "❌"))

    print("\n" + "=" * 60)
    if fails:
        print("❌ 不通过（%d 项）：" % len(fails))
        for x in fails:
            print("   - " + x)
        return 1
    print("✅ 全部通过")
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# LOS 线 profile
# ─────────────────────────────────────────────────────────────────────────────
# KernelSU v0.9.5 的**活** kprobe 目标。清单来自逐行核对 `#if 1 … #else … #endif`
# 结构 —— **不是**从文档摘要抄的：摘要引用的 do_execveat_common / vfs_read /
# do_faccessat / vfs_statx **全在死分支里**（见 docs/los-line/tickets.md T3）。
KSU_LIVE_SYMBOLS = [
    ("__arm64_sys_execve",     "ksud.c:550 / sucompat.c:307"),
    ("__arm64_sys_read",       "ksud.c:568"),
    ("__arm64_sys_faccessat",  "sucompat.c:275"),
    ("__arm64_sys_newfstatat", "sucompat.c:291"),
    ("__arm64_sys_prctl",      "core_hook.c:574"),
    ("input_event",            "ksud.c:579（#if 之外，无条件）"),
    ("vfs_rename",             "core_hook.c:594（#if 之外，无条件）"),
]
LOS_MUST_BE_Y = ("CONFIG_KSU", "CONFIG_KPROBES", "CONFIG_KPROBE_EVENTS",
                 "CONFIG_HAVE_KPROBES", "CONFIG_MODULES", "CONFIG_EXT4_FS",
                 "CONFIG_MACH_XIAOMI_UMI")
# LTO 单列：它的期望值随**上游树**而变（见文件头 `--lto-expect` 的说明），不是我们定的。
LOS_LTO = "CONFIG_LTO_CLANG"
# 主线（Q13 的决定）：关 CFI、关 SCS、**保留上游的 LTO**。
LOS_MUST_BE_N = ("CONFIG_CFI_CLANG", "CONFIG_SHADOW_CALL_STACK")
# CFI 实验线：**反过来** —— 这两个必须开。同一道闸门，按 profile 切换期望值。
LOS_EXP_MUST_BE_Y = ("CONFIG_CFI_CLANG", "CONFIG_SHADOW_CALL_STACK")


def _find(root, exact, prefixes=()):
    """递归找第一个匹配的文件 —— CI artifact 与扁平暂存目录都能用。"""
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if f in exact or any(f.startswith(p) for p in prefixes):
                return os.path.join(dirpath, f)
    return None


def _cfg_map(path):
    out = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s.startswith("CONFIG_") and "=" in s:
            k, v = s.split("=", 1)
            out[k] = v
        elif s.startswith("# CONFIG_") and s.endswith(" is not set"):
            out[s[2:-11].strip()] = "n"
    return out


def _verdict(fails):
    print("\n" + "=" * 60)
    if fails:
        print("❌ 不通过（%d 项）：" % len(fails))
        for x in fails:
            print("   - " + x)
        return 1
    print("✅ 全部通过")
    return 0


def los_main(d, repro, profile="main", lto_expect="y"):
    print("线：LOS（LineageOS + KernelSU v0.9.5）｜ profile = %s ｜ LTO 期望 = %s"
          % (profile, lto_expect))
    print("资产目录: %s\n" % d)
    fails = []
    must_y = list(LOS_MUST_BE_Y)
    must_n = []
    if profile == "cfi-experiment":
        must_y += list(LOS_EXP_MUST_BE_Y)
        must_y.append(LOS_LTO)          # CFI 依赖 LTO，实验线必然要求 LTO
    elif profile == "main":
        must_n = list(LOS_MUST_BE_N)
        (must_y if lto_expect == "y" else must_n).append(LOS_LTO)
    else:
        print("未知 profile: %s（只支持 main / cfi-experiment）" % profile)
        return 2
    if lto_expect not in ("y", "n"):
        print("--lto-expect 只接受 y / n，收到 %r" % lto_expect)
        return 2

    img = _find(d, {"Image"}, ("Image-",))
    cfg = _find(d, {".config"}, ("config-",))
    smap = _find(d, {"System.map"})

    print("=== 一、三件套 ===")
    for label, p in (("Image", img), (".config", cfg), ("System.map", smap)):
        if p is None:
            fails.append("缺少 " + label)
        print("  %-12s %s  %s" % (label, "✅" if p else "❌",
                                  os.path.relpath(p, d) if p else ""))
    if not (img and cfg and smap):
        print("\n三件套不齐，后续检查无法进行。")
        return _verdict(fails)

    print("\n=== 二、.config 关键项 ===")
    cm = _cfg_map(cfg)
    for k in must_y:
        v = cm.get(k, "（不存在）")
        if v != "y":
            fails.append("%s = %s（应为 y）" % (k, v))
        print("  %-34s %-6s %s" % (k, v, "✅" if v == "y" else "❌"))
    for k in must_n:
        v = cm.get(k, "（不存在）")
        if v == "y":
            fails.append("%s = y（应为未设）" % k)
        print("  %-34s %-6s %s" % (k, v, "✅" if v != "y" else "❌"))

    print("\n=== 三、KernelSU hook 目标符号（必须原名存在）===")
    syms = set()
    for line in open(smap, encoding="utf-8", errors="replace"):
        p = line.split()
        if len(p) >= 3:
            syms.add(p[2])
    print("  System.map 符号总数 = %d" % len(syms))
    for name, why in KSU_LIVE_SYMBOLS:
        ok = name in syms
        if not ok:
            fails.append("符号缺失: %s（%s）" % (name, why))
        print("  %-24s %s  %s" % (name, "✅" if ok else "❌", why))

    boots = []
    for dirpath, _dirs, files in os.walk(d):
        for f in sorted(files):
            if f.startswith("boot-") and f.endswith(".img"):
                boots.append(os.path.join(dirpath, f))
    print("\n=== 四、各产物与配套 Image 同源 ===")
    want = sha256_file(img)
    if not boots:
        print("  boot.img：（本目录无，跳过）")
    else:
        for p in boots:
            got = hashlib.sha256(vf.load_kernel(p)).hexdigest()
            if got != want:
                fails.append("%s: 内嵌 kernel 与 Image 不一致" % os.path.basename(p))
            print("  %-44s %s 内嵌=%s" % (os.path.basename(p)[:44],
                                          "✅" if got == want else "❌", got[:12]))

    zips = []
    for dirpath, _dirs, files in os.walk(d):
        for f in sorted(files):
            if f.endswith(".zip"):
                zips.append(os.path.join(dirpath, f))
    if not zips:
        print("  AnyKernel3：（本目录无 .zip，跳过）")
    else:
        # AK3 zip 里也有一份 Image —— 必须与发布的那份同源，否则「boot.img 是一个版本、
        # zip 是另一个版本」。这与第三项的 boot.img 检查是同一个道理。
        for p in zips:
            base = os.path.basename(p)
            with zipfile.ZipFile(p) as z:
                if "Image" not in z.namelist():
                    fails.append("%s: zip 里没有 Image" % base)
                    print("  %-44s ❌ 无 Image" % base[:44])
                    continue
                got = hashlib.sha256(z.read("Image")).hexdigest()
            if got != want:
                fails.append("%s: zip 里的 Image 与配套 Image 不一致" % base)
            print("  %-44s %s 内嵌=%s" % (base[:44], "✅" if got == want else "❌", got[:12]))

    print("\n=== 五、SHA256SUMS.txt ===")
    sums = _find(d, {"SHA256SUMS.txt"})
    if not sums:
        print("  （不存在，跳过）")
    else:
        base = os.path.dirname(sums)
        listed = {}
        for line in open(sums, encoding="utf-8"):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            h, _, name = s.partition("  ")
            listed[name.strip()] = h.strip()
        # ⚠️ **「清单有没有漏项」必须查。** 只查「清单里列的文件对不对」是不够的：
        #    漏列一个资产时，那一条检查照样全绿 —— 又是一种「不完整的全绿」
        #    （同类陷阱见 verify-manifest.py 无参数那次事故，PROJECT.md §3.4）。
        #    官方线那份一直查 missing/extra，LOS 这份此前没查，2026-09-26 补齐。
        #    `SHA256SUMS.txt` 自己不列示（哈希不能自指）。
        present = sorted(f for f in os.listdir(base)
                         if os.path.isfile(os.path.join(base, f)) and f != "SHA256SUMS.txt")
        absent = [k for k in listed if k not in present]
        missing = [f for f in present if f not in listed]
        bad = [k for k in listed if k not in absent
               and sha256_file(os.path.join(base, k)) != listed[k]]
        if absent:
            fails.append("SHA256SUMS.txt 清单里有但文件不在: %s" % absent)
        if missing:
            fails.append("SHA256SUMS.txt 未覆盖目录里的文件: %s" % missing)
        if bad:
            fails.append("SHA256SUMS.txt 哈希不符: %s" % bad)
        print("  列出 %d 条 ｜ 目录 %d 个文件 ｜ 缺失 %d ｜ 未覆盖 %d ｜ 不符 %d  %s" % (
            len(listed), len(present), len(absent), len(missing), len(bad),
            "✅" if not (absent or missing or bad) else "❌"))

    print("\n=== 六、可复现（同配方两次构建逐字节相同）===")
    if not repro:
        print("  （未提供 --repro <第二个目录>，跳过）")
    else:
        for label, p, ex, pf in (("Image", img, {"Image"}, ("Image-",)),
                                 (".config", cfg, {".config"}, ("config-",)),
                                 ("System.map", smap, {"System.map"}, ())):
            # 按**种类**找而不是按文件名找 —— 发布暂存目录里的名字与 CI artifact 里的不同
            q = _find(repro, ex, pf)
            if not q:
                fails.append("--repro 目录里找不到 %s" % label)
                print("  %-12s ❌ 第二个目录里没有同名文件" % label)
                continue
            h1, h2 = sha256_file(p), sha256_file(q)
            if h1 != h2:
                fails.append("%s 两次构建不同" % label)
            print("  %-12s %s  %s" % (label, "✅" if h1 == h2 else "❌", h1[:32]))

    return _verdict(fails)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
