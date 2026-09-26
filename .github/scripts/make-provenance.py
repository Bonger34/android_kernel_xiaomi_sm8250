#!/usr/bin/env python3
"""生成产物来源说明（`PROVENANCE.md`）—— 让 artifact **自己解释自己**。

为什么需要它（issue #5）：一个 artifact 目录里躺着几个文件，而「对应哪个上游提交 / 底包是哪一期 /
闸门过没过」这些信息**全在 CI 日志里**。日志会随 artifact 一起被人拿走吗？不会。
⇒ 使用者只能凭文件名猜。这份文件把这些信息钉进目录本身（规格用户故事 14）。

⚠️ **设计上的硬约束：所有 sha256 都由本工具从磁盘现算，不接受外部传入。**
底包 sha256 是典型陷阱 —— 若由 workflow 从别处传进来，它就可能与实际下载的那份不一致，
而来源说明恰恰是用来证明「我用的是哪一份」的。现算让这种不一致**结构上不可能**。
（另有一条独立路径：workflow 用 `sha256sum` 复核一次，见 build.yml 的「复算件」。）

用法:
  python tools/make-provenance.py --dir <产物目录> --base <底包.img>
        [--fetch-log <fetch-base.py 的输出>] [--repo R] [--branch B] [--commit SHA]
        [--upstream-sha SHA] [--run-url URL] [--gate 文本] [--repro 文本]
        [--out 路径] [--self-test]

退出码: 0 = 成功；1 = 输入不合法或**结论不一致**（例如官方公布的 sha256 与实际下载件不符、
        或壳/zip 里的内核与 Image 不同源）；2 = 用法错误
"""
import argparse
import hashlib
import importlib.util
import os
import re
import shutil
import sys
import tempfile
import zipfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SELF = "PROVENANCE.md"
SUMS = "SHA256SUMS.txt"


# ── 既有的「boot.img → kernel 段」唯一实现，不在这里重写一份 ──────────────────
def load_vf():
    """加载 `verify-fields.py`：优先工作区 `tools/`，其次脚本同目录。

    为什么要回退：CI 里这些脚本跑在 `.github/scripts/`（内核树自带的 `tools/` 是上游的，
    不能占用）；`verify-release.py` 里是同一套回退，两边必须一致。
    """
    ws = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = [os.path.join(ws, "tools", "verify-fields.py"),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), "verify-fields.py")]
    for p in cands:
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("vf", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise SystemExit("找不到 verify-fields.py，试过：\n  " + "\n  ".join(cands))


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan(d):
    """产物目录里的全部文件，按名字排序（大小写不敏感），保证可复现。

    ⚠️ 排除 `PROVENANCE.md` 与 `SHA256SUMS.txt` 两份**元数据**文件：
      - 本文件当然不能列自己（哈希不能自指）；
      - `SHA256SUMS.txt` 是**本文件生成之后**才重新生成的（它会多出本文件那一条）
        ⇒ 在此处记下它的哈希，写进文件的那一刻就已经过期了。
    两者都由 workflow 最后的「终检」那一遍闸门去核，不必也不该写在这里。
    """
    rows = []
    for name in sorted(os.listdir(d), key=lambda n: (n.lower(), n)):
        p = os.path.join(d, name)
        if os.path.isfile(p) and name not in (SELF, SUMS):
            rows.append((name, os.path.getsize(p), sha256_file(p)))
    return rows


def parse_fetch_log(path):
    """从 `fetch-base.py` 的输出里取期次 / 来源 / 官方 sha256。

    ⚠️ **这三行的形状是一份契约，不是实现细节。** 两个写入方必须逐字对齐：
      ① `fetch-base.py` 自己（现拉底包那条路）；
      ② `build.yml` 里「复用底包 artifact」那条路 —— 它**不下载**，但 `base-fetch.log` 必须
         写成同样的形状，只是把期次/来源如实写成「复用 artifact：run <id> 的 <name>」。
    2026-09-26 实测踩到：复用那条路第一版把来源写成了 `artifact:<run>/<name>`
    （带 `.`/`/`，原来的正则 `^来源\\s+(\\S+)\\s*$` 与 `^期次\\s+(\\S+)\\s*$` 都匹配不上），
    于是**封装链在写来源说明那一步整条中止** —— 而编译本身是好的。

    ⚠️ **这里的正则故意放宽**（`(.+?)` 而不是 `(\\S+)`）：能解析的成功路径只有一条
    （`fetch-base.py` 自己），把格式卡得过死**只会把失败模式变成「整条链中止」**，
    而不是「写出一份缺字段的说明」。宽松的代价可控（期次/来源只写进说明，不参与任何判定），
    换来的是「复用底包」这类**后续加进来的写入方**不必先去猜那个格式。
    解析不出来**仍然报错** —— 缺了这三项，来源说明就答不出「我用的是哪一份底包」，
    而它正是这份文件存在的理由。
    """
    txt = open(path, encoding="utf-8", errors="replace").read()
    date = re.search(r"^期次\s+(.+?)\s*$", txt, re.M)
    url = re.search(r"^来源\s+(.+?)\s*$", txt, re.M)
    off = re.search(r"^官方\s+(\d+) 字节  sha256 ([0-9a-f]{64})\s*$", txt, re.M)
    if not (date and url and off):
        raise SystemExit(
            "从 %s 里解析不出「期次 / 来源 / 官方 sha256」三行 —— 那一份日志的格式不对。\n"
            "   没有这三项，来源说明就答不出「我用的是哪一份底包」。\n"
            "   ⇒ 两个写入方必须对齐：`fetch-base.py`（现拉），以及 `build.yml` 里\n"
            "     「复用底包 artifact」那一段（它不下载，但日志要写成同样的形状）。\n"
            "     该文件现在的内容：\n%s" % (path, "\n".join("       " + l for l in txt.splitlines()[:12])))
    return {"date": date.group(1), "url": url.group(1),
            "size": int(off.group(1)), "sha256": off.group(2)}


def pick(names, pred):
    hit = [n for n in names if pred(n)]
    return hit[0] if len(hit) == 1 else None


def render(ctx):
    """渲染 `PROVENANCE.md`。只做字符串拼接，判据全在 main() 里。"""
    L = []
    A = L.append
    A("# 产物来源说明（PROVENANCE）")
    A("")
    A("> 本文件由 `make-provenance.py` 生成，**请勿手改**。")
    A("> 里面的每一个 sha256 都是生成时**从磁盘现算**的，不接受外部传入 ——")
    A("> 所以「来源说明里的哈希与实际文件不一致」在结构上不可能发生。")
    A("> ⚠️ 本文件与 `%s` 都**不**列在下面第三节里：清单不列自己（哈希不能自指），" % SUMS)
    A("> 本文件也不列清单 —— 清单在本文件之后重生成（它会多出本文件那一条），")
    A("> 此刻记下的清单哈希当场就会过期。两份元数据由封装链最后的**终检**那一遍闸门去核。")
    A("")
    A("## 一、这批产物是什么")
    A("")
    A("LineageOS 23.2（Android 16）+ KernelSU v0.9.5 的小米 10（`umi`）内核。")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    A("| 仓库 | %s |" % ctx["repo"])
    A("| 分支 | %s |" % ctx["branch"])
    A("| 构建提交（本线分支 HEAD） | `%s` |" % ctx["commit"])
    if ctx["upstream_sha"]:
        A("| **对应的上游提交** | `%s` |" % ctx["upstream_sha"])
    else:
        A("| **对应的上游提交** | （未记录 —— 本批次是 `workflow_dispatch` 手动触发的，"
          "没有合并上游；#8 起由检测器传入） |")
    if ctx["run_url"]:
        A("| CI run | %s |" % ctx["run_url"])
    A("")
    A("⚠️ **本批次从未在真机上刷过** —— 「root 是否真的可用」在本线一贯**未证实**。")
    A("")
    A("## 二、底包（LineageOS 官方 `boot.img`）")
    A("")
    A("| 项 | 值 |")
    A("|---|---|")
    if ctx["fetch"]:
        A("| 期次 | %s |" % ctx["fetch"]["date"])
        A("| 来源 | %s |" % ctx["fetch"]["url"])
    else:
        A("| 期次 / 来源 | （未提供 `--fetch-log`，未记录） |")
    A("| 字节 | %s |" % format(ctx["base"]["size"], ","))
    A("| sha256 | `%s` |" % ctx["base"]["sha256"])
    if ctx["fetch"]:
        ok = ctx["fetch"]["sha256"] == ctx["base"]["sha256"]
        A("| 官方公布 | `%s` %s |" % (ctx["fetch"]["sha256"],
                                      "✅ 与实际下载件逐字相同" if ok else "❌ 与实际下载件**不符**"))
    A("")
    A("底包只用于取 ramdisk / dtb / vbmeta / 头 —— **壳里的那个内核会被整个换掉**。")
    A("⚠️ 本文件是这份底包唯一的记录：artifact 90 天过期后，谁也重建不了它（规格的实现决定 4）。")
    A("")
    A("## 三、目录内产物")
    A("")
    A("| 文件 | 字节 | sha256 |")
    A("|---|---|---|")
    for name, size, sha in ctx["files"]:
        A("| `%s` | %s | `%s` |" % (name, format(size, ","), sha))
    A("")
    A("### 同源关系（三者必须是同一个内核）")
    A("")
    A("| 角色 | 实测 sha256 | 与 Image 相同 |")
    A("|---|---|---|")
    img_sha = ctx["image_sha"]
    for label, sha in ctx["same_source"]:
        if sha is None:
            A("| %s | （取不到） | ❌ |" % label)
        else:
            A("| %s | `%s` | %s |" % (label, sha, "✅" if sha == img_sha else "❌"))
    A("")
    A("⇒ 这与闸门第四项是**同一条判据**，但这里是本文件**独立现算**的一份 ——")
    A("两者都过才说明「一个版本的内核、一个版本的壳」没有混装。")
    A("")
    A("## 四、可复现（同一提交两次构建逐字节相同）")
    A("")
    A(ctx["repro"])
    A("")
    A("## 五、发布闸门")
    A("")
    A("%s" % ctx["gate"])
    A("")
    A("判据入口只有一个：`verify-release.py --line los`（规格的接缝 S1），封装链跑**两遍**。")
    A("")
    A("⚠️ **上面那段是「预检」那一遍的结论。** 本文件写在预检之后、**终检之前** ——")
    A("终检（连本文件与终版 `SHA256SUMS.txt` 一起核）在封装链最后跑。")
    A("⇒ 「读得到本文件」只证明**预检**是过的；**终检的结论要看 CI run 的状态**。")
    A("⚠️ 终检不过时 run 会失败，但**本文件已经落盘**，仍会随失败产物一起留下来供诊断 ——")
    A("那正是「失败产物照样留存」要的，只是别把这里那个 ✅ 当成整条链的最终结论。")
    A("")
    A("## 六、这批产物**不是**什么")
    A("")
    A("- **不是 release。** 本通道只出 CI artifact，发布仍由人手动做（规格「不在范围内」）。")
    A("- **不是真机验证过的。** 能否开机、root 是否可用，只有设备能答。")
    A("- **不含官方未开源的私有驱动对齐**（LOS 线没有原厂可对，四字段整族作废）。")
    A("")
    return "\n".join(L)


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--dir", help="产物目录")
    ap.add_argument("--base", help="实际下载到的底包 boot.img")
    ap.add_argument("--fetch-log", help="fetch-base.py 的输出（用来记期次 / 来源 / 官方 sha256）")
    ap.add_argument("--repo", default="Bonger34/android_kernel_xiaomi_sm8250")
    ap.add_argument("--branch", default="ksu-lineage-23.2")
    ap.add_argument("--commit", default="（未记录）")
    ap.add_argument("--upstream-sha", default="",
                    help="本批次**对应的上游提交**（#8 起由检测器传入；手动触发时通常为空）")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--gate", default="（未记录）")
    ap.add_argument("--repro", default="未做 —— 两次构建逐字节比对由 issue #6 负责。")
    ap.add_argument("--out", help="输出路径（默认 <dir>/PROVENANCE.md）")
    ap.add_argument("--self-test", action="store_true", help="跑内置离线用例（不联网）")
    a = ap.parse_args(argv[1:])

    if a.self_test:
        return self_test()

    # ⚠️ 这两项**不能**用 argparse 的 required —— 那会让 `--self-test` 也被拦住，
    #    而离线用例正是「不联网、不碰真产物」的那条路（sync-check.py 同款写法）。
    if not a.dir or not a.base:
        print("--dir 与 --base 都必填（除非 --self-test）")
        return 2
    if not os.path.isdir(a.dir):
        print("不是目录: %s" % a.dir)
        return 2
    if not os.path.isfile(a.base):
        print("底包不存在: %s" % a.base)
        return 2

    fetch = parse_fetch_log(a.fetch_log) if a.fetch_log else None
    base = {"size": os.path.getsize(a.base), "sha256": sha256_file(a.base)}

    # ★ 这就是 issue #5 的验收之一：「来源说明里的底包 sha256 与实际下载到的那份一致」。
    #   官方公布了值、而实际字节不符 ⇒ 当场失败，绝不写一份自相矛盾的说明。
    #   （字节数也一并报出来 —— 长度不符必然让 sha256 也不符，所以上面这一条就够，
    #    不必再单列一条「字节数不符」的判定：那条永远走不到，是个好看的死代码。）
    if fetch and fetch["sha256"] != base["sha256"]:
        print("❌ 底包与实际下载件不符：")
        print("   官方公布 %s（%d 字节）" % (fetch["sha256"], fetch["size"]))
        print("   实际文件 %s（%d 字节）" % (base["sha256"], base["size"]))
        print("   ⇒ 最可能的原因：下载被截断/被换掉，或官方那一期被重新发布过。")
        return 1

    files = scan(a.dir)
    if not files:
        print("❌ %s 里没有任何文件 —— 封装链前面某一步没产出东西。" % a.dir)
        return 1

    names = [n for n, _s, _h in files]
    imgs = [n for n in names if n == "Image" or n.startswith("Image-")]
    boots = [n for n in names if n.startswith("boot-") and n.endswith(".img")]
    zips = [n for n in names if n.endswith(".zip")]
    # ⚠️ Image 必须**恰好一个**：同源判据要拿它当基准，多个基准就判不了「三者同源」。
    #    别学第一版用 `pick()`（命中数 ≠ 1 就返回 None）—— 那样会静默地少核一项还打 ✅。
    if len(imgs) != 1:
        print("❌ 目录里应当**恰好一个** Image，实际 %d 个：%s"
              % (len(imgs), "、".join(imgs) if imgs else "（一个都没有）"))
        print("   ⇒ 同源判据拿 Image 当基准；基准不唯一，就没法回答「三者是不是同一个内核」。")
        return 1
    img = imgs[0]

    vf = load_vf()
    same_source = []
    for b in boots:                      # **全部** boot-*.img 都要查，不是只查第一个
        same_source.append(("`%s` 内嵌的 kernel 段" % b,
                            hashlib.sha256(vf.load_kernel(os.path.join(a.dir, b))).hexdigest()))
    for z in zips:
        with zipfile.ZipFile(os.path.join(a.dir, z)) as zf:
            got = hashlib.sha256(zf.read("Image")).hexdigest() if "Image" in zf.namelist() else None
        same_source.append(("`%s` 里的 `Image`" % z, got))

    img_sha = dict((n, h) for n, _s, h in files)[img]
    ctx = {"repo": a.repo, "branch": a.branch, "commit": a.commit, "run_url": a.run_url,
           "upstream_sha": a.upstream_sha,
           "fetch": fetch, "base": base, "files": files,
           "image_sha": img_sha, "same_source": same_source,
           "gate": a.gate, "repro": a.repro}

    # ★ 同源关系的**硬判据**放在落盘之前：不合格就立刻失败，**不写文件**。
    #   否则目录里会留下一份「写着自己不合格」的来源说明，下游只会读到它、不会读到退出码。
    bad = [lbl for lbl, sha in same_source if sha != img_sha]
    if bad:
        print("❌ 与 Image 不同源：")
        for lbl in bad:
            print("   - %s" % lbl)
        print("   ⇒ 最可能的原因：组装时把不同批次的产物混在了一起。")
        return 1

    out = a.out or os.path.join(a.dir, SELF)
    with open(out, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(ctx))
    print("已写 %s（%d 字节）" % (out, os.path.getsize(out)))
    print("  目录内 %d 个文件（不含本文件）｜ 底包 sha256 %s" % (len(files), base["sha256"][:16]))
    print("  ✅ 同源：%s" % ("、".join(lbl for lbl, _s in same_source) or
                             "（目录里没有壳/zip —— 这一项**本次没有核**）"))
    return 0


# ─────────────────────────────────────────────────────────────────────────────
# 离线用例：只测「喂进去什么 ⇒ 写出什么 / 什么退出码」，不看内部实现
# ─────────────────────────────────────────────────────────────────────────────
def _touch(path, data):
    with open(path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


def _bootimg(kernel):
    """造一个最小的 header v2 boot.img：magic + kernel_size + page（4096）。"""
    import struct
    hdr = b"ANDROID!" + struct.pack("<I", len(kernel)) + b"\0" * 24
    return (hdr + b"\0" * (4096 - len(hdr))) + kernel


def _zip_with_image(path, kernel):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Image", kernel)


FETCH_OK = """期次   2026-09-20
来源   https://mirrorbits.lineageos.org/full/umi/20260920/boot.img
官方   {size} 字节  sha256 {sha}
输出   base.img
"""


def self_test():
    cases = []

    def case(name, fn):
        cases.append((name, fn))

    def _setup(tmp):
        base = os.path.join(tmp, "base.img")
        kernel = b"K" * 5000
        _touch(base, b"B" * 4096)          # 底包内容随意 —— 本工具的用例不解析它
        d = os.path.join(tmp, "dist")
        os.makedirs(d)
        ki = _touch(os.path.join(d, "Image-4.19.325-cip131-st15-perf-g0000000000-LOS23.2-ksu-0.9.5"), kernel)
        _touch(os.path.join(d, "config-4.19.325-cip131-st15-perf-g0000000000-LOS23.2"), b"CONFIG_KSU=y\n")
        _touch(os.path.join(d, "System.map"), b"ffffffff81000000 T _text\n")
        _touch(os.path.join(d, "boot-LOS23.2-ksu-0.9.5-umi.img"), _bootimg(kernel))
        _zip_with_image(os.path.join(d, "AnyKernel3-LOS23.2-ksu-0.9.5-umi.zip"), kernel)
        return base, d, ki

    def run(base, d, log=None, extra=()):
        """返回 (退出码, 生成的文本)。异常也算作一种结果，不掩盖。"""
        out = os.path.join(d, SELF)
        argv = ["x", "--dir", d, "--base", base, "--out", out]
        if log:
            argv += ["--fetch-log", log]
        argv += list(extra)
        try:
            rc = main(argv)
        except SystemExit as e:
            return ("SystemExit:%s" % e.code), ""
        txt = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        return rc, txt

    # ① 正向：一切正常 ⇒ 0，且每个文件的现算哈希都出现在文本里
    def c1(tmp):
        base, d, ki = _setup(tmp)
        log = os.path.join(tmp, "fetch.log")
        _touch(log, FETCH_OK.format(sha=sha256_file(base), size=os.path.getsize(base)).encode())
        rc, txt = run(base, d, log)
        assert rc == 0, "正向应当成功，得到 %r" % (rc,)
        for name in sorted(os.listdir(d)):
            if name in (SELF, SUMS):
                continue
            assert sha256_file(os.path.join(d, name)) in txt, "%s 的哈希没写进说明" % name
        assert "| `%s` |" % SELF not in txt, "本文件不该列示自己（哈希不能自指）"
        assert "| `%s` |" % SUMS not in txt, "清单会被重写，此刻记下的哈希当场过期"
        return "0，%d 个产物的哈希全部现算入文" % (len(os.listdir(d)) - 2)

    case("正向：生成并覆盖全部文件", c1)

    # ② 官方 sha256 与实际下载件不符 ⇒ 1，且**不写文件**
    def c2(tmp):
        base, d, _ki = _setup(tmp)
        log = os.path.join(tmp, "fetch.log")
        _touch(log, FETCH_OK.format(sha="0" * 64, size=os.path.getsize(base)).encode())
        rc, txt = run(base, d, log)
        assert rc == 1, "不符时应当失败，得到 %r" % (rc,)
        assert txt == "", "失败时不该留下来源说明"
        return "1，且未写出文件"

    case("负向：官方 sha256 与实际不符 ⇒ 失败", c2)

    # ③ fetch 日志格式变了 ⇒ 报错，而不是静默留空
    def c3(tmp):
        base, d, _ki = _setup(tmp)
        log = os.path.join(tmp, "fetch.log")
        _touch(log, b"something completely different\n")
        rc, _txt = run(base, d, log)
        assert isinstance(rc, str) and rc.startswith("SystemExit:"), \
            "格式不认识时应当报错退出，得到 %r" % (rc,)
        return "SystemExit，明说解析不出哪三行"

    case("负向：fetch 日志格式变了 ⇒ 报错", c3)

    # ④ 壳里装的内核与 Image 不同源 ⇒ 1（这正是「混装」那个缺陷）
    def c4(tmp):
        base, d, _ki = _setup(tmp)
        _touch(os.path.join(d, "boot-LOS23.2-ksu-0.9.5-umi.img"), _bootimg(b"X" * 5000))
        rc, txt = run(base, d)
        assert rc == 1, "混装时应当失败，得到 %r" % (rc,)
        assert txt == "", "不合格时不该留下来源说明"
        return "1，指出壳里是另一个内核，且未写文件"

    case("负向：boot.img 内嵌内核与 Image 不同源 ⇒ 失败", c4)

    # ⑤ zip 里没有 Image ⇒ 也算不同源
    def c5(tmp):
        base, d, _ki = _setup(tmp)
        p = os.path.join(d, "AnyKernel3-LOS23.2-ksu-0.9.5-umi.zip")
        os.remove(p)
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("anykernel.sh", "properties() { ''; }\n")
        rc, txt = run(base, d)
        assert rc == 1, "zip 里没有 Image 时应当失败，得到 %r" % (rc,)
        assert txt == "", "不合格时不该留下来源说明"
        return "1，指出 zip 里取不到 Image，且未写文件"

    case("负向：zip 里没有 Image ⇒ 失败", c5)

    # ⑥ 空目录 ⇒ 1（说明封装链前面没产出）
    def c6(tmp):
        base, _d, _ki = _setup(tmp)
        empty = os.path.join(tmp, "empty")
        os.makedirs(empty)
        rc, _txt = run(base, empty)
        assert rc == 1, "空目录应当失败，得到 %r" % (rc,)
        return "1，明说前面某一步没产出"

    case("负向：产物目录是空的 ⇒ 失败", c6)

    # ⑦ 两个 `boot-*.img`（一个同源、一个不同源）⇒ 必须失败。
    #    ⚠️ 第一版用「命中数 ≠ 1 就返回 None」挑壳 ⇒ 有第二个壳时**整条同源检查会消失**，
    #    输出照样带 ✅ —— 而这一节的标题正是「三者必须是同一个内核」。
    def c7(tmp):
        base, d, _ki = _setup(tmp)
        _touch(os.path.join(d, "boot-second.img"), _bootimg(b"X" * 5000))
        rc, txt = run(base, d)
        assert rc == 1, "有第二个（不同源的）boot.img 时应当失败，得到 %r" % (rc,)
        assert txt == "", "不合格时不该留下来源说明"
        return "1，两个壳里的内核都查了（不再只看第一个）"

    case("负向：两个 boot.img，第二个不同源 ⇒ 失败", c7)

    # ⑧ 两个 `Image` ⇒ 必须失败：同源判据拿 Image 当基准，基准不唯一就没法判。
    def c8(tmp):
        base, d, _ki = _setup(tmp)
        _touch(os.path.join(d, "Image-second"), b"K" * 5000)
        rc, txt = run(base, d)
        assert rc == 1, "两个 Image 时应当失败，得到 %r" % (rc,)
        assert txt == "", "基准不唯一时不该留下来源说明"
        return "1，明说「应当恰好一个 Image」"

    case("负向：两个 Image ⇒ 失败", c8)

    fails = 0
    for name, fn in cases:
        tmp = tempfile.mkdtemp(prefix="prov-")
        try:
            note = fn(tmp)
            print("  ✅ %-44s %s" % (name, note))
        except Exception as e:
            # ⚠️ 捕 `Exception` 而不是 `AssertionError`：用例里**抛别的异常**（夹具退化、
            #    zipfile 报错…）本身就是「这条不合格」，不该让它中断整轮、连计数都不给。
            fails += 1
            print("  ❌ %-44s %s: %s" % (name, type(e).__name__, e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("\n%d/%d 通过" % (len(cases) - fails, len(cases)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
