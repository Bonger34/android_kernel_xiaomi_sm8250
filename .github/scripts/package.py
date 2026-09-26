#!/usr/bin/env python3
"""封装链：把「编译产物 + LineageOS 底包」变成一整套**可验证**的产物目录。

流水线（每一步失败即整体失败，退出码原样返回）：

    输入件自证 → 组装命名 → 重打包 boot.img → 打 AK3 zip
      → 生成哈希清单 → **预检闸门** → 写来源说明 → 重生成清单 → **终检闸门**

为什么是**一个 Python 脚本**而不是写在 workflow 的 YAML 里：

1. **本地跑得起来。** 本机没有可用的 bash（Windows），YAML 里的步骤只能在 CI 上试，
   一轮 20 分钟。写成脚本，同一条链在本机就能跑通 —— 而且本机有已发布批次的输入件，
   「跑出来的 boot.img / zip 与已发布件逐字节相同」是个**强判据**。
2. **workflow 只做编排**（规格实现决定 8 的同一条道理）：YAML 里只剩「取底包 / 调它 / 传 artifact」。
3. 一致性约束（命名 / profile / 闸门档位三者必须一致）落在一处，不散在 YAML 里。

⚠️ 两遍闸门是有意为之：`PROVENANCE.md` 里要写「闸门结论」，而它自己与最终版
`SHA256SUMS.txt` 又都是目录里的文件。**预检**在写这两份元数据之前跑（核资产本身），
**终检**在最后跑（连元数据一起核）。只跑一遍必有一头是假的。

用法:
  python .github/scripts/package.py --image <Image> --config <.config> --system-map <System.map> \\
      --base <底包.img> --template <AK3 模板.tar.gz> --out-dir <产物目录> \\
      [--template-sha256 SHA] [--repo R] [--branch B] [--commit SHA] [--upstream-sha SHA] \\
      [--run-url URL] [--fetch-log <fetch-base.py 的输出>] \\
      [--line-name LOS23.2] [--ksu-version 0.9.5] [--device umi]

退出码: 本脚本**没有自己的退出码** —— 它返回**失败那一步的**退出码（原样透传），
        好让调用方从码值仍能定位是哪一类问题（用法错误 / 输入不合法 / 判据不过）。
        每一步的语义见那个工具自己的文件头：
          `repack-boot.py` 2=用法 3=输入不合法 ｜ `make-ak3.py` 1=自检不过
          `verify-release.py` 1=判据不过 2=用法 ｜ `make-provenance.py` 1=结论不一致 2=用法
        本脚本自己只返回 1（输入件不齐 / 模板钉值不符 / 推不出版本串）。
"""
import argparse
import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    # ⚠️ `line_buffering=True` 不是为了好看：本脚本**派生子进程**，而子进程是直接写 fd 1 的。
    #    父进程的 stdout 在 CI 里是管道 ⇒ 默认块缓冲 ⇒ 自己的分节标题会**排在子进程输出后面**，
    #    日志读起来像时间倒流。实测撞到过（本地第一次跑）。
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

SD = os.path.dirname(os.path.abspath(__file__))
SUMS = "SHA256SUMS.txt"


def load_vf():
    """`verify-fields.py` 是「从 Image 取版本串」的唯一实现 —— 不在这里重写一份。"""
    cands = [os.path.join(os.path.dirname(SD), "tools", "verify-fields.py"),
             os.path.join(SD, "verify-fields.py")]
    for p in cands:
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location("vf", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    # 报**实际试过的路径**，不是目录 —— 否则读日志的人还得自己拼一遍
    raise SystemExit("找不到 verify-fields.py，试过：\n  " + "\n  ".join(cands))


def cfg_map(path):
    """读 `.config`。

    ⚠️ 与 `verify-release.py` 的 `_cfg_map` **同款**：显式关掉的项（`# CONFIG_X is not set`）
    要记成 `"n"`，**不能**当成「不存在」—— 「有意关掉」与「这棵树没有这一项」在排查时是
    两种结论。这里判档位只看 `== "y"`，两种写法都不影响**判定**；差别在日志里那句话
    会不会把 `n` 说成「（未设）」，从而误导下一个读日志的人。
    """
    out = {}
    for line in open(path, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s.startswith("CONFIG_") and "=" in s:
            k, v = s.split("=", 1)
            out[k] = v
        elif s.startswith("# CONFIG_") and s.endswith(" is not set"):
            out[s[2:-11].strip()] = "n"
    return out


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(step, argv):
    """跑一个子命令，stdio 直通（不捕获）—— 工具自己的输出原样进 CI 日志。"""
    print("\n" + "─" * 72)
    print("▶ %s" % step)
    print("  $ " + " ".join(argv))
    print("─" * 72)
    rc = subprocess.run(argv).returncode
    if rc != 0:
        print("\n❌ %s 失败（退出码 %d）—— 封装链就此中止，不再往下产出。" % (step, rc))
    return rc


def write_sums(d):
    """生成 `sha256sum` 格式的清单：`<sha>␣␣<名字>`，按名字排序（两次构建才会逐字节相同）。"""
    names = sorted((n for n in os.listdir(d)
                    if os.path.isfile(os.path.join(d, n)) and n != SUMS),
                   key=lambda n: (n.lower(), n))
    with open(os.path.join(d, SUMS), "w", encoding="utf-8", newline="\n") as f:
        for n in names:
            f.write("%s  %s\n" % (sha256_file(os.path.join(d, n)), n))
    print("  清单 %d 条：%s" % (len(names), ", ".join(names)))
    return names


def fresh_dir(d):
    """清空重建产物目录。⚠️ 三重保护：绝不对根目录、当前目录、**当前目录的祖先**下手。"""
    ap = os.path.abspath(d)
    cwd = os.getcwd()
    if ap in (os.path.abspath(os.sep), cwd) or os.path.dirname(ap) == ap:
        raise SystemExit("拒绝清空 %s —— 它太靠近根目录了" % ap)
    # ⚠️ 第三种情形最阴：在子目录里跑、把 `--out-dir` 给成工作区根（或它的任何祖先），
    #    `rmtree` 会把**当前所在的整棵树**删掉，而前两条检查都拦不住。
    if cwd.startswith(ap + os.sep):
        raise SystemExit("拒绝清空 %s —— 它是当前目录（%s）的祖先" % (ap, cwd))
    shutil.rmtree(ap, ignore_errors=True)
    os.makedirs(ap)
    return ap


def main(argv):
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--image", help="编出来的裸 Image")
    ap.add_argument("--config", help="out/.config")
    ap.add_argument("--system-map", help="out/System.map")
    ap.add_argument("--base", help="LineageOS 官方底包 boot.img")
    ap.add_argument("--template", help="AnyKernel3 模板 .tar.gz")
    ap.add_argument("--template-sha256", default=None, help="模板的钉值；给了就核")
    ap.add_argument("--out-dir", help="产物目录（会被清空重建）")
    ap.add_argument("--repo", default="Bonger34/android_kernel_xiaomi_sm8250")
    ap.add_argument("--branch", default="ksu-lineage-23.2")
    ap.add_argument("--commit", default="（未记录）")
    ap.add_argument("--upstream-sha", default="",
                    help="本批次**对应的上游提交**（规格 §5 的来源说明四项之一；"
                         "#8 起由检测器传入，手动触发时为空 —— 那时来源说明如实写「未记录」）")
    ap.add_argument("--run-url", default="")
    ap.add_argument("--fetch-log", default=None)
    ap.add_argument("--line-name", default="LOS23.2", help="命名里的线名")
    ap.add_argument("--ksu-version", default="0.9.5")
    ap.add_argument("--device", default="umi")
    a = ap.parse_args(argv[1:])

    for flag in ("image", "config", "system_map", "base", "template", "out_dir"):
        if not getattr(a, flag):
            print("--%s 必填" % flag.replace("_", "-"))
            return 2
    for label, p in (("Image", a.image), ("config", a.config),
                     ("System.map", a.system_map), ("底包", a.base), ("模板", a.template)):
        if not os.path.isfile(p):
            print("%s 不存在：%s" % (label, p))
            return 2

    vf = load_vf()
    py = sys.executable

    # ── 一、输入件自证 ────────────────────────────────────────────────────────
    # 模板钉值：上游 `osm0sis/AnyKernel3@master` 是**会动**的，而它变了 zip 的字节就会变
    # （anykernel.sh 的内容、magiskboot 的二进制都会进 zip）。钉住 = 上游一改版**当场失败**，
    # 而不是悄悄产出一批用别的模板做的 zip。
    tmpl_sha = sha256_file(a.template)
    if a.template_sha256 and tmpl_sha != a.template_sha256.lower():
        print("❌ AK3 模板不是钉死的那一份：")
        print("   期望 %s" % a.template_sha256.lower())
        print("   实际 %s" % tmpl_sha)
        print("   ⇒ 最可能的原因：上游 master 动了。要不要接受新模板是**人的决定**，")
        print("     改钉子之前不该产出字节已经变了的 zip。")
        return 1
    print("输入件：")
    for label, p in (("Image", a.image), ("config", a.config), ("System.map", a.system_map),
                     ("底包", a.base), ("模板", a.template)):
        print("  %-11s %12d 字节  %s" % (label, os.path.getsize(p), sha256_file(p)[:16]))
    print("  %-11s %s" % ("模板钉值", "✅ 相符" if a.template_sha256 else "（本次未钉）"))

    # ── 二、版本串与档位：一律从产物现算，不接受调用方传入 ────────────────────
    # 为什么要这样：命名 / 闸门档位 / 实际配置**三者如果各由一处决定，就必然有对不上的一天**
    # （构建关了 CFI 却按 CFI 档查闸门，或者反过来）。全部从 `.config` 与 Image 现推，
    # 这种不一致在结构上不可能出现。
    cm = cfg_map(a.config)
    cfi = cm.get("CONFIG_CFI_CLANG") == "y"
    if cfi:
        profile, tag = "cfi-experiment", "-cfi"
    else:
        profile, tag = "main", ""
    vm = vf.get_vermagic(open(a.image, "rb").read()) or ""
    krelease = vm.split(" ")[0].replace("-dirty", "")   # `-dirty` 是工作树标记，不是版本
    if not krelease:
        print("❌ 从 Image 里取不到 vermagic —— 推不出内核版本串，而产物命名要它：")
        print("   期望 %s" % "形如 `4.19.325-…-perf-g<hash> SMP preempt …` 的串（≥12 个可见字符）")
        print("   实际 %s（%d 字节，sha256 %s）"
              % (a.image, os.path.getsize(a.image), sha256_file(a.image)[:16]))
        print("   ⇒ 最可能的原因：① 传进来的不是内核 Image（比如把 boot.img 传了进来）；")
        print("     ② 该树没开 CONFIG_PREEMPT —— 那 vermagic 里就没有 ` SMP preempt `；")
        print("     ③ 文件被截断（大文件会静默截断，见 PROJECT.md §7 坑表 #21）。")
        return 1
    print("\n档位 %s ｜ 内核版本串 %s" % (profile, krelease))
    print("  CONFIG_CFI_CLANG=%s ⇒ 命名后缀 %r" % (cm.get("CONFIG_CFI_CLANG", "（未设）"), tag))

    names = {
        "image": "Image-%s-%s%s-ksu-%s" % (krelease, a.line_name, tag, a.ksu_version),
        "config": "config-%s-%s%s" % (krelease, a.line_name, tag),
        "boot": "boot-%s%s-ksu-%s-%s.img" % (a.line_name, tag, a.ksu_version, a.device),
        "zip": "AnyKernel3-%s%s-ksu-%s-%s.zip" % (a.line_name, tag, a.ksu_version, a.device),
    }

    # ── 三、组装 ─────────────────────────────────────────────────────────────
    d = fresh_dir(a.out_dir)
    shutil.copyfile(a.image, os.path.join(d, names["image"]))
    shutil.copyfile(a.config, os.path.join(d, names["config"]))
    shutil.copyfile(a.system_map, os.path.join(d, "System.map"))
    print("产物目录 %s" % d)
    for k in ("image", "config"):
        print("  %s" % names[k])

    rc = run("重打包 boot.img（底包的 ramdisk/dtb/vbmeta + 新内核）",
             [py, os.path.join(SD, "repack-boot.py"), a.base, a.image,
              "-o", os.path.join(d, names["boot"])])
    if rc:
        return rc
    rc = run("打 AnyKernel3 卡刷包",
             [py, os.path.join(SD, "make-ak3.py"), "--kernel", a.image,
              "--template", a.template, "--out", os.path.join(d, names["zip"]),
              "--device", a.device,
              # ⚠️ 把 KSU 版本**显式传进去**：`make-ak3.py` 的默认串里也写着一个 v0.9.5，
              #    不传的话「文件名说 0.9.6、zip 里的 anykernel.sh 说 0.9.5」迟早发生 ——
              #    正是本文件上面那段注释（命名/档位/实际配置三者必须同源）要防的事。
              #    （默认值一致时它**不改字节**：拼出来与 make-ak3.py 的默认串逐字相同。）
              "--kernel-string", "KernelSU v%s for LineageOS by Bonger34" % a.ksu_version])
    if rc:
        return rc

    # ── 四、预检：此刻目录里只有资产，还没有元数据 ───────────────────────────
    print("\n" + "─" * 72)
    print("▶ 生成哈希清单（预检用：此时还没有 PROVENANCE.md）")
    print("─" * 72)
    write_sums(d)
    rc = run("发布闸门（预检）", [py, os.path.join(SD, "verify-release.py"), d,
                                 "--line", "los", "--profile", profile])
    if rc:
        return rc

    # ── 五、元数据：来源说明 → 重生成清单（多出 PROVENANCE.md 那一条）→ 终检 ──
    rc = run("写来源说明（预检结论写进 PROVENANCE.md；终检在它之后跑）",
             [py, os.path.join(SD, "make-provenance.py"), "--dir", d, "--base", a.base,
              "--repo", a.repo, "--branch", a.branch, "--commit", a.commit,
              "--run-url", a.run_url,
              "--upstream-sha", a.upstream_sha,
              # ⚠️ 措辞必须说清这是**哪一遍**闸门的结论：本文件写在预检之后、终检**之前**，
              #    而终检失败时它已经落盘、还会随失败产物一起被上传（`if: always()`）。
              #    写成「闸门通过」就会让一份失败的 artifact 自称通过。
              "--gate", "✅ **预检**通过（`verify-release.py --line los --profile %s`，退出码 0）。"
                        "本文件写于预检之后、**终检之前** —— 整条链的最终结论看 CI run 的状态。"
                        "第六项「两次构建逐字节比对」本次跳过，见第四节。" % profile]
             + (["--fetch-log", a.fetch_log] if a.fetch_log else []))
    if rc:
        return rc
    print("\n" + "─" * 72)
    print("▶ 重生成哈希清单（把 PROVENANCE.md 也纳进来）")
    print("─" * 72)
    write_sums(d)
    rc = run("发布闸门（终检：连元数据一起核）", [py, os.path.join(SD, "verify-release.py"), d,
                                                 "--line", "los", "--profile", profile])
    if rc:
        return rc

    # ── 六、摘要（CI 日志末尾一眼能看到这批是什么）────────────────────────────
    print("\n" + "=" * 72)
    print("✅ 封装链完成：%s" % d)
    print("   profile=%s ｜ 内核版本串 %s" % (profile, krelease))
    for n in sorted(os.listdir(d), key=lambda x: (x.lower(), x)):
        p = os.path.join(d, n)
        print("   %12d  %s  %s" % (os.path.getsize(p), sha256_file(p)[:16], n))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
