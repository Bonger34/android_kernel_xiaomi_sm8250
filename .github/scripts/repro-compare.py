#!/usr/bin/env python3
"""两次构建的**逐字节**比对，并把它写成一份人能读、CI 运行摘要能贴的报告。

对应 issue #6「两次构建逐字节比对」（规格 `docs/los-line/upstream-sync-spec.md`
实现决定 5 与用户故事 12）。

## 它和 `verify-release.py --repro` 的分工

| | 职责 |
|---|---|
| `verify-release.py --line los --repro <第二目录>` | **判定**：产品必须两份都有、且逐字节相同，否则退出 1 |
| **本工具** | **取证**：差异落在哪个文件、哪一段、差多少字节；并输出 Markdown 报告 |

两者都要跑。只有判定 ⇒ 失败时只有一句「不一样」，没法诊断；只有取证 ⇒
「谁说了算」就没有唯一入口了（规格要求判据只有一个入口）。**判定入口仍是闸门。**

⚠️ **`PROVENANCE.md` 里有一栏是 `CI run` 的 URL，它按定义每次运行都不同**
⇒ 逐字节比对必须**排除元数据**，只比产品（`Image` / `.config` / `System.map` /
`boot-*.img` / `AK3 *.zip`）。顺带把它解析出来：两份的**底包 sha256** 若不同，
就明说「两次编译用的不是同一份底包」—— 那是最容易被误读成「构建不可复现」的情形。

## 用法

```sh
python tools/repro-compare.py <目录A> <目录B>                     # 打印报告
python tools/repro-compare.py <目录A> <目录B> --summary out.md    # 另写一份 Markdown
python tools/repro-compare.py <目录A> <目录B> --allow-partial     # 允许缺整个角色（比三件套时用）
python tools/repro-compare.py --self-test                         # 内置离线用例
```

退出码：0 = 逐字节相同（或 `--self-test` 全过）；1 = 有差异 / 有缺件；
2 = 用法错误。

⚠️ **本文件有两份，必须逐字节相同**：工作区 `tools/repro-compare.py`
与 LOS 工作仓库里的 `.github/scripts/repro-compare.py`（CI 跑的是**后者** ——
内核树自带一个上游的 `tools/`，项目自己的 CI 工具一律放 `.github/`）。
镜像与复验：`python tools/sync-ci-scripts.py --gen` / `--check`。
"""
import argparse
import hashlib
import os
import re
import shutil
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

CHUNK = 1 << 20
SUMS = "SHA256SUMS.txt"
PROV = "PROVENANCE.md"

# 角色判定。**顺序有意义**：先判 zip / boot / Image / config，剩下的才是元数据。
# 为什么按角色而不是按文件名：两次构建的 Image 名字里带**提交短 sha**，
# 换了提交名字就变，而要比的正是「同一个提交的两次编译」—— 名字对不上不该算缺件。
RULES = (
    ("Image", lambda n: n == "Image" or n.startswith("Image-")),
    ("config", lambda n: n == ".config" or n.startswith("config-")),
    ("System.map", lambda n: n == "System.map"),
    ("boot", lambda n: n.startswith("boot-") and n.endswith(".img")),
    ("zip", lambda n: n.endswith(".zip")),
    ("provenance", lambda n: n == PROV),
    ("sums", lambda n: n == SUMS),
)
# 产品 = 参与逐字节比对的那些。元数据两类（`provenance` / `sums`）不参与判定：
# 前者含本次运行的 URL，后者跟着前者走 —— 见文件头与 `compare()` 的注释。
PRODUCT_ROLES = ("Image", "config", "System.map", "boot", "zip")

IMG_VER_RE = re.compile(r"^Image-(\S+?)-LOS\d")


def role_of(name):
    for role, pred in RULES:
        if pred(name):
            return role
    return None


def walk(root):
    """递归收集普通文件，返回 {相对路径: 绝对路径}。"""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            if os.path.isfile(p):
                out[os.path.relpath(p, root).replace("\\", "/")] = p
    return out


def classify(root):
    """把目录里的文件按角色归类。返回 {角色: [(相对路径, 绝对路径), ...]}。

    ⚠️ 同名文件在两个不同相对路径下各有一份（CI artifact 里 `kernel-image/` 与
    `dist/` 并列时就是这样）⇒ 同一个角色会有多个命中。**不静默挑一个** ——
    调用方会把「两边命中数不一致」报成失败（本项目反复踩的「不完整的绿」，
    见 `PROJECT.md` §7 坑表 #32）。
    """
    out = {}
    for rel, abs_ in walk(root).items():
        role = role_of(os.path.basename(rel))
        if role:
            out.setdefault(role, []).append((rel, abs_))
    for v in out.values():
        v.sort()
    return out


def _pick(cat, role):
    """角色 → 唯一命中；0 个返回 None，多于 1 个返回 `False`（= 不唯一，判失败）。

    ⚠️ 别学「命中数≠1 就当成没有」那种写法：那会让**整条检查静默消失**，
    而输出照样 ✅（2026-09-26 在 `make-provenance.py` 里修过同一个形状）。
    """
    hit = cat.get(role, [])
    if not hit:
        return None
    return hit[0] if len(hit) == 1 else False


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def diff_detail(a, b):
    """逐块比两个文件，返回 (首个不同字节的偏移, 已比字节数, 相异字节数)。

    ⚠️ 不做「先读进内存」：`boot-*.img` 是 128 MiB，两边一起读就是 256 MiB。
    分块比 + 只在块内定位第一个差异 —— 与 sha256 相比不额外花时间。

    相异字节数是**确数**（整份都要过一遍才能得出）。要快速判定时看 `None`：
    长度不同时不必数完 —— 那本身就已经是结论。
    """
    off = 0
    nbytes = 0
    first = None
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ba, bb = fa.read(CHUNK), fb.read(CHUNK)
            if not ba and not bb:
                break
            n = min(len(ba), len(bb))
            if ba[:n] != bb[:n]:
                for i in range(n):
                    if ba[i] != bb[i]:
                        first = off + i
                        break
                nbytes += sum(1 for i in range(n) if ba[i] != bb[i])
            if len(ba) != len(bb):
                # 长度不同：后面的字节无从对齐，报「较短的那份到此为止」
                nbytes += abs(len(ba) - len(bb))
                break
            off += n
    return first, off, nbytes


def count_diff(a, b):
    """相异字节的**确数**（整份扫完）。"""
    n = 0
    with open(a, "rb") as fa, open(b, "rb") as fb:
        while True:
            ba, bb = fa.read(CHUNK), fb.read(CHUNK)
            if not ba and not bb:
                break
            for i in range(min(len(ba), len(bb))):
                if ba[i] != bb[i]:
                    n += 1
            n += abs(len(ba) - len(bb))
    return n


def parse_provenance(p):
    """从 `PROVENANCE.md` 里取三项：底包 sha256 / 构建提交 / 上游提交。

    解析不出来**不报错** —— 这是元数据，缺了不影响产品比对；但会在报告里
    明说「没解析到」，免得读的人以为比对覆盖了它。
    """
    try:
        txt = open(p, encoding="utf-8", errors="replace").read()
    except OSError:
        return {}
    out = {}
    m = re.search(r"\|\s*sha256\s*\|\s*`([0-9a-f]{64})`\s*\|", txt)
    if m:
        out["base_sha256"] = m.group(1)
    m = re.search(r"\|\s*构建提交（本线分支 HEAD）\s*\|\s*`([^`]+)`\s*\|", txt)
    if m:
        out["commit"] = m.group(1)
    m = re.search(r"\|\s*\*\*对应的上游提交\*\*\s*\|\s*`([^`]+)`\s*\|", txt)
    if m:
        out["upstream_sha"] = m.group(1)
    return out


def krelease_of(cat):
    """从 Image 的文件名里取内核版本串（报告标题用）。取不到就返回 None。"""
    for rel, _p in cat.get("Image", []):
        m = IMG_VER_RE.match(os.path.basename(rel))
        if m:
            return m.group(1)
    return None


def compare(a, b, allow_partial=False):
    """比两个目录。返回 (报告文本, 差异列表, 备注列表)。

    差异 = 会让退出码变成 1 的那些；备注 = 只解释、不判定（例如「底包不是同一份」，
    它**必然**导致产品不同，所以产品不同才是判定的那一条）。

    `allow_partial`：允许某一侧缺少**整个角色**（例如只比 `kernel-image` 那种
    「三件套」artifact —— 它里面本来就没有壳与 zip）。
    默认 `False`：发布产物必须五件俱全，缺一件就判失败 ——
    「缺了就不比」会让一次**不完整的比对**看起来像全绿。
    """
    fails, notes = [], []
    ca, cb = classify(a), classify(b)

    # 缺件先判。不唯一命中（同名文件出现在两个相对路径下）**也要判失败**：
    # 那时「比的是哪一个」本身就没定，静默挑一个等于让检查悄悄换了对象。
    todo = []
    for role in PRODUCT_ROLES:
        xa, xb = _pick(ca, role), _pick(cb, role)
        if xa is False or xb is False:
            fails.append("%s：同名文件在同一目录里出现多次 —— 比哪一个没有定义" % role)
            continue
        if xa is None or xb is None:
            if allow_partial and xa is None and xb is None:
                continue
            if allow_partial:
                fails.append("%s：只有一侧有（A %s ｜ B %s）"
                             % (role, "有" if xa else "无", "有" if xb else "无"))
            else:
                fails.append("%s：%s" % (role, "A 侧没有" if xa is None else "B 侧没有"))
            continue
        todo.append((role, xa[1], xb[1]))

    if fails:
        # ⚠️ 缺件时**也**要出一份报告：CI 摘要与失败产物是一起留档的，
        #    摘要里只写一句「不相同」而不说缺了什么，事后诊断就没有材料。
        return render(a, b, [], fails, notes, krelease_of(ca)), fails, notes

    rows = []
    for role, pa, pb in todo:
        sa, sb = os.path.getsize(pa), os.path.getsize(pb)
        ha, hb = sha256_file(pa), sha256_file(pb)
        same = (ha == hb)
        detail = ""
        if not same:
            if sa != sb:
                # 长度不同：先报长度，再给「较短的那份能对到哪一位」
                first, scanned, _n = diff_detail(pa, pb)
                detail = ("长度 %d vs %d ｜ 首个相异字节 @%s（已比 %d 字节）"
                          % (sa, sb, first if first is not None else "—", scanned))
            else:
                first, _scanned, _n = diff_detail(pa, pb)
                n = count_diff(pa, pb)
                detail = ("长度相同 ｜ 首个相异字节 @%s（0x%X）｜ 相异 %d / %d 字节（%.4f%%）"
                          % (first, first or 0, n, sa, 100.0 * n / sa if sa else 0.0))
            fails.append("%s 两次构建不同：%s" % (role, detail))
        rows.append((role, os.path.basename(pa), sa, ha, sb, hb, same, detail))

    # 元数据：不参与判定，但要把「为什么可能不同」说清楚
    def _meta(cat):
        p = _pick(cat, "provenance")
        return parse_provenance(p[1]) if p else {}

    pa_, pb_ = _meta(ca), _meta(cb)
    if pa_.get("base_sha256") and pb_.get("base_sha256"):
        if pa_["base_sha256"] != pb_["base_sha256"]:
            notes.append("⚠️ 两次的**底包不是同一份**（%s… vs %s…）—— "
                         "产品不同是它的必然结果，先看这一条再看别的。"
                         % (pa_["base_sha256"][:12], pb_["base_sha256"][:12]))
        else:
            notes.append("底包同一份：`%s`" % pa_["base_sha256"])
    else:
        notes.append("（两侧没能都读到 `%s` 的底包 sha256 —— 那一项**没被核**）" % PROV)

    return render(a, b, rows, fails, notes, krelease_of(ca)), fails, notes


def render(a, b, rows, fails, notes, krelease):
    L = []
    A = L.append
    A("# 两次构建逐字节比对")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| 目录 A | `%s` |" % a)
    A("| 目录 B | `%s` |" % b)
    if krelease:
        A("| 内核版本串 | `%s` |" % krelease)
    A("| 结论 | %s |" % ("✅ **逐字节相同**" if not fails else
                          "❌ **不相同**（%d 项）" % len(fails)))
    A("")
    A("## 产品（参与判定）")
    A("")
    if not rows:
        A("⚠️ **没有可比的产品** —— 有一侧缺了下面列出的东西，逐字节比对**根本没跑**。")
        A("")
    A("| 角色 | 文件 | A 字节 | A sha256 | B 字节 | B sha256 | |")
    A("|---|---|---|---|---|---|---|")
    for role, name, sa, ha, sb, hb, same, detail in rows:
        A("| %s | `%s` | %s | `%s` | %s | `%s` | %s |"
          % (role, name, format(sa, ","), ha[:16], format(sb, ","), hb[:16],
             "✅" if same else "❌"))
        if detail:
            A("| | ↳ | | | | | %s |" % detail)
    A("")
    A("⚠️ 逐字节比对**只覆盖产品**。`%s` 里有一栏是本次运行的 URL，" % PROV)
    A("它按定义每次都不同 ⇒ 元数据不参与判定，只用来解释差异。")
    A("")
    if notes:
        A("## 备注")
        A("")
        for n in notes:
            A("- %s" % n)
        A("")
    if fails:
        A("## 差异")
        A("")
        for f in fails:
            A("- %s" % f)
        A("")
    else:
        A("## 差异")
        A("")
        A("无 —— 参与比对的 %d 个产品两份逐字节相同。" % len(rows))
        A("")
    A("## 判据")
    A("")
    A("判定入口只有一个：`verify-release.py --line los --repro <第二目录>`。")
    A("本工具的结论与它必须一致；不一致时**以闸门为准**（闸门还会核三件套齐不齐、")
    A("KSU 符号、同源关系与哈希清单）。")
    return "\n".join(L)


def self_test():
    """离线用例：全部喂真文件（临时目录里造），只看输出与退出码。"""
    cases = []

    def case(name, fn):
        cases.append((name, fn))

    def mk(root, files):
        for rel, data in files.items():
            p = os.path.join(root, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
        return root

    def pair(tmp, same=True, boot=True, zip_=True):
        a, b = os.path.join(tmp, "a"), os.path.join(tmp, "b")
        base = {"Image-x-LOS23.2-ksu-0.9.5": b"K" * 4096,
                "config-x-LOS23.2": b"CONFIG_KSU=y\n",
                "System.map": b"ffffffff81000000 T _text\n"}
        if boot:
            base["boot-LOS23.2-ksu-0.9.5-umi.img"] = b"B" * 8192
        if zip_:
            base["AnyKernel3-LOS23.2-ksu-0.9.5-umi.zip"] = b"Z" * 1024
        prov = ("| sha256 | `%s` |\n" % ("c" * 64)).encode()
        base[PROV] = prov
        base[SUMS] = b"%s  Image-x-LOS23.2-ksu-0.9.5\n" % (b"a" * 64)
        mk(a, base)
        other = dict(base)
        if not same:
            other["Image-x-LOS23.2-ksu-0.9.5"] = b"K" * 4095 + b"X"
        mk(b, other)
        return a, b

    def c1(tmp):
        a, b = pair(tmp)
        txt, fails, _n = compare(a, b)
        assert not fails, "正向应当无差异：%s" % fails
        assert "逐字节相同" in txt
        return "0，五个产品全同"

    case("正向：两次构建逐字节相同", c1)

    def c2(tmp):
        a, b = pair(tmp, same=False)
        txt, fails, _n = compare(a, b)
        assert fails, "有一个字节不同就必须失败"
        assert "首个相异字节 @4095" in txt, txt[-800:]
        return "1，报出首个相异字节的偏移"

    case("负向：Image 差一个字节 ⇒ 失败且给出偏移", c2)

    def c3(tmp):
        a, b = pair(tmp, boot=False)
        _txt, fails, _n = compare(a, b)
        assert fails and any("boot" in f for f in fails), fails
        return "1，明说 boot 缺在哪一侧"

    case("负向：一侧缺 boot.img ⇒ 失败", c3)

    def c4(tmp):
        a, b = pair(tmp, zip_=False)
        _txt, fails, _n = compare(a, b)
        assert fails and any("zip" in f for f in fails), fails
        return "1，明说 zip 缺在哪一侧"

    case("负向：一侧缺 AK3 zip ⇒ 失败", c4)

    def c5(tmp):
        a, b = pair(tmp)
        # 元数据不同（run URL）**不该**影响判定 —— 这正是它被排除的理由
        with open(os.path.join(b, PROV), "a", encoding="utf-8") as f:
            f.write("| CI run | https://example.invalid/2 |\n")
        with open(os.path.join(b, SUMS), "w", encoding="utf-8") as f:
            f.write("0" * 64 + "  Image-x-LOS23.2-ksu-0.9.5\n")
        _txt, fails, _n = compare(a, b)
        assert not fails, "元数据按定义会不同，不该判失败：%s" % fails
        return "0，元数据不同不影响判定"

    case("边界：PROVENANCE/SHA256SUMS 不同 ⇒ 仍判相同", c5)

    def c6(tmp):
        a, b = pair(tmp)
        for n in (PROV, SUMS):
            os.remove(os.path.join(b, n))
        txt, fails, notes = compare(a, b)
        assert not fails, "元数据缺失不该判失败：%s" % fails
        assert any("没被核" in n for n in notes), notes
        return "0，且明说底包那一项没被核"

    case("边界：一侧没有 PROVENANCE ⇒ 不失败但要说清楚", c6)

    def c7(tmp):
        a, _b = pair(tmp)
        other = os.path.join(tmp, "c")
        mk(other, {"Image-y-LOS23.2-ksu-0.9.5": b"K" * 4096})
        _txt, fails, _n = compare(a, other)
        assert fails and any("config" in f for f in fails), fails
        return "1，缺件各自列出"

    case("负向：第二个目录只有 Image ⇒ 失败", c7)

    def c8(tmp):
        a, b = pair(tmp, same=False)
        os.makedirs(os.path.join(b, "dist"), exist_ok=True)
        shutil.copyfile(os.path.join(b, "Image-x-LOS23.2-ksu-0.9.5"),
                        os.path.join(b, "dist", "Image-x-LOS23.2-ksu-0.9.5"))
        _txt, fails, _n = compare(a, b)
        assert fails and any("比哪一个没有定义" in f for f in fails), fails
        return "1，同名文件出现两次时判失败，而不是静默挑一个"

    case("边界：嵌套目录里的同名文件 ⇒ 判失败", c8)

    def c10(tmp):
        # 三件套 artifact（**两侧都**没有壳与 zip）：默认判失败，--allow-partial 才比
        a, b = pair(tmp)
        for d in (a, b):
            for n in ("boot-LOS23.2-ksu-0.9.5-umi.img",
                      "AnyKernel3-LOS23.2-ksu-0.9.5-umi.zip"):
                os.remove(os.path.join(d, n))
        _txt, fails, _n = compare(a, b)
        assert fails, "默认必须要求五件俱全（缺了就不比 = 不完整的绿）"
        txt2, fails2, _n = compare(a, b, allow_partial=True)
        assert not fails2, fails2
        assert "3 个产品" in txt2, txt2[-400:]
        return "默认 1；--allow-partial 时 0，且报告里写明只比了 3 个"

    case("边界：三件套 artifact 需要 --allow-partial", c10)

    def c9(tmp):
        # 长度不同：必须报长度差，而不是硬找「首个相异字节」
        a, b = pair(tmp)
        with open(os.path.join(b, "System.map"), "ab") as f:
            f.write(b"extra\n")
        txt, fails, _n = compare(a, b)
        assert fails and "长度" in txt
        return "1，长度不同时先报长度"

    case("负向：System.map 长度不同 ⇒ 失败", c9)

    fails = 0
    for name, fn in cases:
        tmp = tempfile.mkdtemp(prefix="repro-")
        try:
            note = fn(tmp)
            print("  ✅ %-46s %s" % (name, note))
        except Exception as e:
            # 捕 `Exception` 而不是 `AssertionError`：用例里抛别的异常本身就是不合格
            fails += 1
            print("  ❌ %-46s %s: %s" % (name, type(e).__name__, e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d/%d 通过" % (len(cases) - fails, len(cases)))
    return 1 if fails else 0


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("dirs", nargs="*", help="两个产物目录（A 与 B）")
    ap.add_argument("--summary", metavar="路径",
                    help="额外写一份 Markdown（CI 里给 $GITHUB_STEP_SUMMARY）")
    ap.add_argument("--allow-partial", action="store_true",
                    help="允许某一侧缺整个角色（比 `kernel-image` 那种三件套 artifact 时用）；"
                         "默认要求五件俱全 —— 「缺了就不比」会让不完整的比对看起来像全绿")
    ap.add_argument("--self-test", action="store_true", help="跑内置离线用例（不联网）")
    a = ap.parse_args(argv[1:])

    if a.self_test:
        return self_test()
    if len(a.dirs) != 2:
        print(__doc__)
        return 2
    for d in a.dirs:
        if not os.path.isdir(d):
            print("不是目录: %s" % d)
            return 2

    txt, fails, _notes = compare(a.dirs[0], a.dirs[1], a.allow_partial)
    print(txt)
    if a.summary:
        # ⚠️ **先写摘要再退非零**：CI 里 `run:` 一旦非零，后面的步骤就不跑了，
        #    而「不一致时摘要里也要看得见」正是本工具的用途之一。
        with open(a.summary, "a", encoding="utf-8", newline="\n") as f:
            f.write(txt + "\n")
        print("\n（报告已追加到 %s）" % a.summary)
    print()
    if fails:
        print("❌ 两次构建**不是**逐字节相同（%d 项）—— 两份产物都保留，供事后诊断。" % len(fails))
        return 1
    print("✅ 两次构建逐字节相同（Image / .config / System.map / boot.img / AK3 zip 中实际参与比对的那些）。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
