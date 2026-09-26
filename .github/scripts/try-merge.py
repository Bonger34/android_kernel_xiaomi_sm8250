#!/usr/bin/env python3
"""试合并：把上游 HEAD 合进本线分支 —— **在临时工作区里做，绝不 push、绝不移动任何分支**。

规格：`docs/los-line/upstream-sync-spec.md` 实现决定 2（检测与合并）：

- 检测到漂移后，在 CI 工作区**试合并**（不 push）；
- **合并提交的 committer date 锚定到上游 HEAD 的日期**；
- **冲突即干净失败**，分支不动。

## 为什么「同样的输入必须给出同样的合并提交」

这一条不是洁癖，它被**两条既有判据**逼出来：

1. `scripts/setlocalversion` 把 `git rev-parse --short HEAD` 拼进内核版本串
   （已发布件是 `4.19.325-cip131-st15-perf-g464566a06008`）⇒ 两次构建要产出同一个 `Image`，
   **合并提交的 sha 必须逐字相同**；
2. `build.yml` 的 `KBUILD_BUILD_TIMESTAMP` 取自 `git show -s --format=%cd HEAD`
   ⇒ committer date 必须只由**上游提交**决定，不能是「运行时刻」。

⇒ 本工具把 author/committer 的**名字、邮箱、日期**与**提交信息**全部钉死，
于是「同样的 (本线 HEAD, 上游 sha)」在任何机器上给出**同一个合并提交**。
两个 build job 各自重放这一次合并，并断言 sha 与检测器给出的 `--expect-merge-sha` 相同 ——
「你构建的就是我验过的那个合并」因此是**可失败的检查**，不是一句承诺。

## 两条不变量

① **绝不 push，也绝不移动任何分支。** 合并前先 `git checkout --detach <本线 HEAD>`：
   于是「本地分支被合并推进」在结构上不可能发生（detached HEAD 下没有任何分支指向 HEAD）。
   工具里**没有**任何 push / `--force` / 写 ref 的调用。
   远端工作分支没动这件事，由 workflow 用 API 再复核一次（见 `detect.yml`）。

② **失败不留半成品。** 冲突 ⇒ **工作区一个字节都不动**（`merge-tree` 只算不写）；
   期望值不符 ⇒ `git reset --hard` 回到合并前的 HEAD。⚠️ 后者之所以安全，是因为
   **开工前先要求工作区干净**（不干净直接拒绝运行，退出码 2）—— 脏工作区里做
   `reset --hard` 会毁掉别人的改动。

## 为什么不用 `git merge`（porcelain），而用 `merge-tree` + `commit-tree`

① **要的只是一个对象。** `git merge` 是一串「改工作区 → 改索引 → 提交」的副作用，
   失败时要靠 `--abort` 收拾；而试合并要的只是**那个合并提交**。
   `git merge-tree --write-tree` 把合并结果算成**一个 tree 对象**（不碰工作区、不碰索引），
   `git commit-tree` 再把它接上两个父提交 —— 两步都是纯函数式的。
② ⚠️ **`git merge` 在某些环境里根本跑不起来**（实测）：`builtin/merge.c` 的
   `cmd_merge()` 在试任何策略**之前**会**无条件**调用 `save_state()`，而它内部是
   `git stash create` 且 `cp.out = -1`（要建管道）。在禁止创建子进程管道的环境里
   （本机沙箱，实测 `error: cannot create standard output pipe for stash: Permission denied`）
   所有常规策略都当场 die；`merge-tree` / `commit-tree` 都是 builtin，不 spawn 任何东西。
③ ⭐ **`--merge-base=<M>` 把合并基点显式钉住**，于是这次合并**只**取决于
   (合并基点, 本线 HEAD, 上游提交) 三个对象，与本地历史挖到多深无关 ——
   这也正好与「算出来的 merge-base 必须等于 `--merge-base`」那条断言咬合。

## 退出码（都是正常结论，别写成「非零即崩」）

| 结论 | 码 |
|---|---|
| 合并成功（或 `--expect-merge-sha` 相符） | 0 |
| 运行错误（git 失败、取不到对象…） | 1 |
| 用法 / 环境错误（缺参数、脏工作区） | 2 |
| 上游**没有**漂移（上游提交是 HEAD 的祖先） | 3 |
| 合并冲突（**工作区未改动**，分支未动） | 4 |
| 前提不符（merge-base 或 `--expect-merge-sha`） | 5 |

## 取历史（这是本工具唯一与网络有关的地方）

CI 里的检出是 `fetch-depth: 1`（浅仓库），而合并**必须**知道 merge-base。
策略按代价从低到高排，每一档之后重新算一次 merge-base，成功就停：

| 档 | 动作 | 代价 |
|---|---|---|
| ① | 本地已经有 ⇒ 什么都不做 | 0（自测与重复运行走这条） |
| ② | `--depth=1` 取上游那个提交 | 一次往返 |
| ③ | `--depth=1` 取 **merge-base 本身**（为了拿它的日期） | 一次往返 |
| ④ | `--shallow-since=<merge-base 的日期 - 1 天>` 深化两侧 | 只取漂移那一段 |
| ⑤ | `--deepen=500` 反复深化（上限见 `DEPTH_ROUNDS`） | 最坏情况兜底 |

⚠️ **`--merge-base` 是必填的**，而且算出来的 merge-base 必须与它相等（否则退出码 5）。
理由：浅历史里 `git merge-base` 只会返回**它还看得见**的那个共同祖先，
拿一个「截断后的 merge base」去合并会得到一个**错的合并**（把上游早已合过的改动再合一遍），
而那种错**不会报错**。判据只能来自外部 —— 检测器从 `compare` API 拿到的
`merge_base_commit.sha` 就是那个外部权威。

## 用法

```sh
# CI（检测器）：试合并并给出合并提交
python3 .github/scripts/try-merge.py \
    --upstream-url https://github.com/LineageOS/android_kernel_xiaomi_sm8250.git \
    --upstream-sha "$UPSTREAM_SHA" --merge-base "$MERGE_BASE" \
    --work-branch ksu-lineage-23.2 \
    --github-output "$GITHUB_OUTPUT" --json-out merge.json --summary "$GITHUB_STEP_SUMMARY"

# CI（两个 build job）：重放同一次合并，sha 必须与检测器给出的一致
python3 .github/scripts/try-merge.py --upstream-url ... --upstream-sha "$UPSTREAM_SHA" \
    --merge-base "$MERGE_BASE" --work-branch ksu-lineage-23.2 \
    --expect-merge-sha "$MERGE_SHA"

python tools/try-merge.py --self-test      # 离线，临时仓库 + 真 git，不联网
```

⚠️ **本文件有两份，必须逐字节相同**：工作区 `tools/try-merge.py` 与 LOS 工作仓库里的
`.github/scripts/try-merge.py`（内核树**自带**一个 `tools/`，那是上游的，不能占用）。
镜像与复验：`python tools/sync-ci-scripts.py --gen` / `--check`。
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile

EXIT_OK = 0
EXIT_RUN = 1
EXIT_USAGE = 2
EXIT_NO_DRIFT = 3
EXIT_CONFLICT = 4
EXIT_MISMATCH = 5

# 合并提交的身份。**钉死**是「确定性」的一半（另一半是日期与提交信息）。
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"

# ⑤ 那一档每轮加多少个提交、最多几轮（12 × 500 = 6000 个提交的漂移）。
DEPTH_STEP = 500
DEPTH_ROUNDS = 12

UPSTREAM_DEFAULT = "LineageOS/android_kernel_xiaomi_sm8250"


def run(argv, cwd=None, env=None, binary=False):
    """跑一条命令并取回 `(rc, stdout, stderr)`。

    ⚠️ **为什么把输出落临时文件，而不是用 `capture_output=True`**：
    本机（Windows）的沙箱**禁止创建管道** —— 实测 `subprocess.run(..., capture_output=True)`
    抛 `PermissionError(13, '拒绝访问')`，而把 stdout 指向一个**文件**完全正常。
    两条路在 CI 上等价（都只是重定向），所以只留一条：临时文件。
    这是本项目「原生 exe 的输出只能让它自己打到控制台」那条老坑的同族
    （`PROJECT.md` §7 坑表 #3 系列），只是这次连 Python 也被挡住了。
    """
    out = tempfile.TemporaryFile()
    err = tempfile.TemporaryFile()
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, stdout=out, stderr=err)
        out.seek(0)
        err.seek(0)
        enc = None if binary else "utf-8"
        return (p.returncode,
                out.read().decode(enc or "utf-8", "replace"),
                err.read().decode("utf-8", "replace"))
    finally:
        out.close()
        err.close()


def git(repo, *args, **kw):
    """跑一次 git（`check=True` 时非零即抛）。"""
    rc, out, err = run(["git"] + list(args), cwd=repo, env=kw.get("env"))
    if kw.get("check", True) and rc != 0:
        raise RuntimeError("git %s 失败（退出码 %d）\n    %s"
                           % (" ".join(args), rc, (err or out).strip()[:800]))
    return rc, out.strip(), err.strip()


def git_out(repo, *args, **kw):
    return git(repo, *args, **kw)[1]


class Usage(RuntimeError):
    """用法 / 环境错误（退出码 2）—— 与运行错误分开，善后完全不同。"""


class Mismatch(RuntimeError):
    """前提不符（退出码 5）。"""


def assert_sha(name, value):
    """形状守卫：40 位十六进制。写歪的 sha 会让 `git fetch` 去请求一个不存在的东西，
    而那时的报错完全看不出是「参数写错了」。"""
    v = (value or "").strip().lower()
    if len(v) != 40 or any(c not in "0123456789abcdef" for c in v):
        raise Usage("%s 必须是完整的 40 位 sha（收到 %r）—— 缩写 sha 在浅仓库里可能歧义，"
                    "而歧义在这里的后果是一次**错的合并**" % (name, value))
    return v


def merge_message(branch, local_sha, upstream_sha, merge_base):
    """合并提交的信息。**逐字固定**：多一个时间戳或 run URL，两次构建就会不同。"""
    return ("Merge upstream %s into %s\n"
            "\n"
            "上游同步通道的**试合并**（docs/los-line/upstream-sync-spec.md 实现决定 2）。\n"
            "提交信息与 author/committer 的身份、日期都钉死 ⇒ 同样的（本线 HEAD, 上游 sha）\n"
            "在任何机器上给出同一个合并提交；两个 build job 各自重放它并核对 sha。\n"
            "\n"
            "上游提交 %s\n"
            "本线 HEAD %s\n"
            "合并基点 %s\n"
            % (upstream_sha[:12], branch, upstream_sha, local_sha, merge_base))


def have(repo, sha):
    rc, _, _ = git(repo, "cat-file", "-e", "%s^{commit}" % sha, check=False)
    return rc == 0


def merge_base_of(repo, a, b):
    """`git merge-base`；算不出来（浅历史里没有共同祖先）时返回 `None`。"""
    rc, out, _ = git(repo, "merge-base", a, b, check=False)
    return out.strip() if rc == 0 and out.strip() else None


def is_shallow(repo):
    return os.path.exists(os.path.join(repo, ".git", "shallow"))


def ensure_history(a, log):
    """把上游提交与 merge-base 取到本地。返回**当前能算出来的** merge-base（可能为 None）。

    每一档之后都重新算一次 —— 成功就停；全都失败则抛 `RuntimeError`，
    并把「试过哪几档、各自什么结果」一起带出去（排障时最需要的就是这个）。
    """
    if have(a.repo_dir, a.upstream_sha):
        mb = merge_base_of(a.repo_dir, a.local_sha, a.upstream_sha)
        if mb:
            log("本地已有上游提交与共同祖先（merge-base %s）⇒ 不联网" % mb)
            return mb
    if not a.upstream_url:
        raise Usage("本地算不出 merge-base（对象不全），而没给 --upstream-url —— 取不了历史。\n"
                    "    （--merge-base 不能替代它：合并要用的是**共同的祖先提交**本身，"
                    "不是一个 sha 数字。）")

    tried = []
    log("本地对象不全 ⇒ 取历史（策略按代价从低到高，每档之后重算 merge-base）")

    def attempt(what, argv, where=a.repo_dir):
        rc, _, err = run(["git"] + argv, cwd=where)
        tried.append((what, rc, (err or "").strip().splitlines()[:2]))
        log("     %s %s（退出码 %d）" % ("✅" if rc == 0 else "⚠️ ", what, rc))
        return rc == 0

    # ② 上游那个提交。浅仓库里加深浅边界是**目的**，不是副作用（CI 的检出本来就是 depth=1）。
    attempt("② --depth=1 取上游提交",
            ["fetch", "--depth=1", a.upstream_url, a.upstream_sha])
    mb = merge_base_of(a.repo_dir, a.local_sha, a.upstream_sha)
    if mb:
        return mb

    # ③ merge-base 本身：**只为了拿它的日期**（④ 的切点）。
    attempt("③ --depth=1 取 merge-base",
            ["fetch", "--depth=1", a.upstream_url, a.merge_base])
    if have(a.repo_dir, a.merge_base):
        iso = git_out(a.repo_dir, "show", "-s", "--format=%cI", a.merge_base)
        # 减一天：--shallow-since 是「**晚于**该时刻的提交」，卡在当天可能把 merge-base 自己切掉。
        cut = (datetime.datetime.fromisoformat(iso)
               - datetime.timedelta(days=1)).isoformat()
        attempt("④ --shallow-since=%s 深化上游侧" % cut[:10],
                ["fetch", "--shallow-since=" + cut, a.upstream_url, a.upstream_sha])
        if a.work_branch:
            attempt("④ --shallow-since=%s 深化本线侧" % cut[:10],
                    ["fetch", "--shallow-since=" + cut, "origin", a.work_branch])
        mb = merge_base_of(a.repo_dir, a.local_sha, a.upstream_sha)
        if mb:
            return mb

    # ⑤ 兜底：反复 --deepen。⚠️ 只在**本地已经是浅仓库**时才用 —— 在完整仓库上 `--depth`
    #    会把仓库变浅，那是把一个「只是慢」的问题换成一个「判据被截断」的问题。
    if is_shallow(a.repo_dir):
        for i in range(1, DEPTH_ROUNDS + 1):
            # ⚠️ **一次 fetch 失败就停**：那说明不是「挖得不够深」，而是网络/URL/权限的问题
            #    —— 再重复 11 轮只会把一条清楚的错误淹成 24 行噪音（实测踩到过）。
            ok = attempt("⑤ --deepen=%d（第 %d 轮）" % (DEPTH_STEP, i),
                         ["fetch", "--deepen=%d" % DEPTH_STEP, a.upstream_url, a.upstream_sha])
            if ok and a.work_branch:
                attempt("⑤ --deepen=%d 本线侧（第 %d 轮）" % (DEPTH_STEP, i),
                        ["fetch", "--deepen=%d" % DEPTH_STEP, "origin", a.work_branch])
            if not ok:
                break
            mb = merge_base_of(a.repo_dir, a.local_sha, a.upstream_sha)
            if mb:
                return mb
    else:
        tried.append(("⑤ 跳过 --deepen（本地不是浅仓库）", 0, []))

    detail = "\n".join("     %-46s rc=%d %s" % (w, rc, " / ".join(e)) for w, rc, e in tried)
    raise RuntimeError(
        "取不到共同祖先：深化了 %d 轮仍算不出 %s 与 %s 的 merge-base。\n"
        "   试过的每一档与它的结果：\n%s\n"
        "   ⇒ 这多半意味着**漂移太深**（超过 %d 个提交）或上游 URL 指错了。\n"
        "      不要在这里放宽判据：拿一个截断的祖先去合并会得到一次**不报错的错合并**。"
        % (DEPTH_ROUNDS, a.local_sha[:12], a.upstream_sha[:12], detail,
           DEPTH_STEP * DEPTH_ROUNDS))


def merge_env(upstream_sha, repo):
    """钉死 author/committer 的身份与日期。日期 = **上游提交的 committer date**。

    ⚠️ 用 `%cI`（严格 ISO，带原始时区偏移）而不是 `%cd`：后者受本地 TZ 影响，
    而两个 build job 的 TZ 恰好被 `build.yml` 设成了 UTC —— 依赖它等于把确定性
    建在一个**别处的设置**上。
    """
    iso = git_out(repo, "show", "-s", "--format=%cI", upstream_sha)
    if not iso:
        raise RuntimeError("取不到上游提交 %s 的 committer date" % upstream_sha)
    env = dict(os.environ)
    for who in ("AUTHOR", "COMMITTER"):
        env["GIT_%s_NAME" % who] = BOT_NAME
        env["GIT_%s_EMAIL" % who] = BOT_EMAIL
        env["GIT_%s_DATE" % who] = iso
    return env, iso


def run_merge(a, log):
    """本工具的全部行为。返回结果字典（`exit_code` 在里面）。"""
    repo = a.repo_dir
    # 形状守卫放在这里而不是 main()：`run_merge` 才是本体，绕过 `main` 的调用方
    # （自测就是）不该拿到一条**没有守卫**的路径。
    a.upstream_sha = assert_sha("--upstream-sha", a.upstream_sha)
    a.merge_base = assert_sha("--merge-base", a.merge_base)
    a.local_sha = assert_sha("--local-sha", a.local_sha) if a.local_sha else None
    a.expect_merge_sha = (assert_sha("--expect-merge-sha", a.expect_merge_sha)
                          if a.expect_merge_sha else None)
    if not os.path.isdir(os.path.join(repo, ".git")):
        raise Usage("%s 不是一个 git 仓库（没有 .git）" % repo)

    # ── ① 开工前的环境守卫：**要动工作区时，已跟踪文件不许有改动** ──
    # ⚠️ 只有**会改工作区**的那条路（`--checkout`）才需要这条守卫：它失败时会 `reset --hard`
    #    回退，而 `reset --hard` 会**丢掉已跟踪文件的改动** —— 那才是它危险的地方。
    #    `--no-checkout` 全程不碰工作区（`merge-tree` 只算不写、`commit-tree` 只造对象），
    #    而那条路正是**本机（Windows）唯一跑得通的**：内核树里有
    #    `drivers/gpu/drm/nouveau/nvkm/subdev/i2c/aux.c` 这类路径，`aux` 是 Windows
    #    保留设备名，工作区**根本检不出来**（实测 `error: invalid path`），
    #    于是「全删除」会永远被当成脏工作区。
    # ⚠️ **未跟踪文件不算脏**（`--untracked-files=no`）：`git reset --hard` **不删**未跟踪
    #    文件，所以它们不在「回退会毁掉什么」的范围里。这不是放宽 —— build job 里
    #    `actions/download-artifact` 会把 `base.img`（128 MiB）放进工作目录，
    #    若把未跟踪也算脏，那条路会**永远拒绝运行**（实测踩到）。
    if a.checkout:
        dirty = git_out(repo, "status", "--porcelain", "--untracked-files=no")
        if dirty:
            raise Usage("工作区有**已跟踪文件的改动**（%d 处）⇒ 拒绝运行。\n"
                        "   本工具失败时会 `git reset --hard` 回到合并前的 HEAD，"
                        "那会**丢掉这些改动**。\n"
                        "   确实不需要工作区时用 `--no-checkout`（只算合并提交，不碰工作区）。\n"
                        "   前几行：\n%s"
                        % (len(dirty.splitlines()),
                           "\n".join("     " + l for l in dirty.splitlines()[:6])))

    local_sha = git_out(repo, "rev-parse", "HEAD")
    if a.local_sha:
        want = assert_sha("--local-sha", a.local_sha)
        if want != local_sha:
            raise Mismatch("本线 HEAD 与 --local-sha 不符：实际 %s，期望 %s。\n"
                           "   ⇒ 检出与检测器看到的那一刻不是同一个提交（分支动了？ref 派发错了？）。"
                           % (local_sha, want))
    # ⚠️ 把**解析出来的**那个 sha 写回 `a`：`ensure_history()` 与错误消息都用 `a.local_sha`，
    #    而 `--local-sha` 是可选的 —— 不回填的话，不传它时那两处会拿到 `None`
    #    （实测：真取历史那条路上直接 `TypeError: expected str ... not NoneType`）。
    a.local_sha = local_sha
    head_branch = git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")

    # ── ② 确定性的一半：detached HEAD。合并只移动 HEAD，**不可能**推进任何分支 ──
    #    ⚠️ `--no-checkout` 下**连这一步也不做**：那条路根本不移动 HEAD，
    #    于是「任何分支都没动」是结构性的，不需要靠 detach 来保证。
    if a.checkout and head_branch != "HEAD":
        log("先把 HEAD 摘下来（当前在分支 %s 上）—— 于是这次合并推不动任何分支" % head_branch)
        git(repo, "checkout", "--detach", local_sha)

    result = {
        "repo_dir": repo, "work_branch": a.work_branch or head_branch,
        "local_sha": local_sha, "upstream_sha": a.upstream_sha,
        "upstream_url": a.upstream_url, "checked_out": bool(a.checkout),
        "merge_base": a.merge_base, "merge_base_expected": a.merge_base,
        "merge_sha": None, "merge_date": None,
        "detached": bool(a.checkout and head_branch != "HEAD"),
    }

    # ── ③ 上游提交必须在；不在就取（唯一联网的一步）──
    if not have(repo, a.upstream_sha):
        mb = ensure_history(a, log)
    else:
        mb = merge_base_of(repo, local_sha, a.upstream_sha)
    if mb is None:
        mb = ensure_history(a, log)
    if mb is None:
        raise RuntimeError("取完历史仍算不出 merge-base")

    # ── ④ 「没有漂移」不是错误，但**不该走到这里** —— 它是检测器的一个 bug ──
    rc, _, _ = git(repo, "merge-base", "--is-ancestor", a.upstream_sha, local_sha, check=False)
    if rc == 0:
        log("上游提交 %s 已经是本线 HEAD 的祖先 ⇒ 没有漂移" % a.upstream_sha[:12])
        result.update(exit_code=EXIT_NO_DRIFT, state="no-drift", merge_base=mb,
                      head_after=local_sha)
        return result

    # ── ⑤ merge-base 必须与外部权威一致（见文件头「为什么 --merge-base 是必填」）──
    if mb != a.merge_base:
        raise Mismatch(
            "算出来的 merge-base 与 --merge-base 不符：实际 %s，期望 %s。\n"
            "   最可能的原因（按可能性排序）：\n"
            "     ① **历史被截断了** —— 本地只看得见更靠后的那个共同祖先。拿它合并会把上游\n"
            "        早已合过的改动再合一遍，而那种错**不会报错**；\n"
            "     ② 本线 HEAD 与检测器看到的那一刻不是同一个提交；\n"
            "     ③ --upstream-sha 指错了提交。"
            % (mb, a.merge_base))

    log("本线 HEAD   %s（%s）" % (local_sha, head_branch))
    log("上游提交    %s" % a.upstream_sha)
    log("合并基点    %s（与检测器从 API 拿到的一致）" % mb)

    # ── ⑥ 合并 ─────────────────────────────────────────────────────────────
    # ⚠️ **不用 `git merge`**（porcelain）。两个理由，第二个是实测出来的：
    #   ① 它是一串「先改工作区、再改索引、最后提交」的副作用，失败时要靠 abort 收拾；
    #      而这里要的只是一个**合并提交**，`merge-tree` + `commit-tree` 是它的正解；
    #   ② `cmd_merge` 在试任何策略**之前**会无条件跑一次 `git stash create`
    #      （`builtin/merge.c` 的 `save_state()`，`cp.out = -1` 要建管道）—— 本机沙箱
    #      禁管道，于是 `git merge` 在这种环境里**根本跑不起来**（实测），
    #      而 `merge-tree` / `commit-tree` 都是 builtin，不 spawn 任何子进程。
    #   `--merge-base=<M>` 把合并基点**显式钉住**：于是这次合并只取决于
    #   (合并基点, 本线 HEAD, 上游提交) 三个对象，与本地历史挖到多深无关。
    #   `merge.renames` / `merge.directoryRenames` 也钉住：它们的默认值在 git 版本之间
    #   变过，而「同一对输入给出同一个合并提交」不该受运行环境的默认值影响。
    log("合并 = merge-tree（纯 plumbing，--merge-base 显式钉住）")
    rc, out, err = run(["git", "-c", "merge.renames=true", "-c", "merge.directoryRenames=true",
                        "merge-tree", "--write-tree", "--name-only",
                        "--merge-base=" + a.merge_base, local_sha, a.upstream_sha], cwd=repo)
    # 输出是「分节、空行分隔」的（`git-merge-tree(1)` 的 OUTPUT 一节）：
    #   第 1 节 = 顶层 tree OID + **冲突文件清单**（`--name-only` 形态）
    #   第 2 节 = 说明性消息（Auto-merging… / CONFLICT…），只在有内容时出现
    sections = out.split("\n\n")
    head = [l for l in sections[0].splitlines() if l.strip()] if sections else []
    notes = "\n".join(s.strip() for s in sections[1:] if s.strip())
    tree = head[0].strip() if head else ""

    if rc != 0:
        conflicted = sorted(l.strip() for l in head[1:] if l.strip())
        result.update(exit_code=EXIT_CONFLICT, state="conflict", conflicted=conflicted,
                      head_after=local_sha)
        log("❌ 合并冲突（%d 个文件）" % len(conflicted))
        for f in conflicted[:12]:
            log("     %s" % f)
        if len(conflicted) > 12:
            log("     …（还有 %d 个）" % (len(conflicted) - 12))
        if not conflicted:
            log("     （merge-tree 没列出冲突文件，原始输出：%s）" % (out or err).strip()[:400])
        for line in notes.splitlines()[:12]:
            log("     ｜ %s" % line)
        log("   上游 %s 与本线 %s 在同一个文件的同一处都改过。" % (a.upstream_sha[:12], local_sha[:12]))
        log("   ⇒ 按规格「冲突即干净失败」：**不推送、不留半成品、不触发构建**。")
        log("     处理这件事的是人：手工解冲突并推一次，或等上游自己修。")
        log("   ⚠️ 本次**没有**改动工作区（merge-tree 只算不写）—— HEAD 仍是 %s" % local_sha[:12])
        return result
    if not tree or len(tree) != 40:
        raise RuntimeError("merge-tree 没有给出合并后的 tree（输出：%r）" % out[:400])

    env, iso = merge_env(a.upstream_sha, repo)
    result["merge_date"] = iso
    result["tree"] = tree
    log("合并后的 tree %s" % tree[:12])
    log("合并提交的 author/committer = %s <%s>，日期锚定到上游提交的 %s"
        % (BOT_NAME, BOT_EMAIL, iso))
    msg = merge_message(a.work_branch or head_branch, local_sha, a.upstream_sha, mb)
    rc, out, err = run(["git", "commit-tree", tree, "-p", local_sha, "-p", a.upstream_sha,
                        "-m", msg], cwd=repo, env=env)
    if rc != 0:
        raise RuntimeError("commit-tree 失败（退出码 %d）：%s" % (rc, (err or out).strip()[:600]))
    merge_sha = out.strip().splitlines()[-1].strip() if out.strip() else ""

    # 把工作区切到合并结果 —— build job 要**编它**，而「构建的就是验过的那个提交」得能核对。
    # ⚠️ `--no-checkout` 下跳过这一步：只在**对象库**里造出合并提交，工作区与 HEAD 都不动。
    if a.checkout:
        git(repo, "reset", "--hard", merge_sha)
    parents = git_out(repo, "rev-list", "--parents", "-n", "1", merge_sha).split()[1:]
    if parents != [local_sha, a.upstream_sha]:
        raise RuntimeError("合并提交的父提交是 %s，应为 [%s, %s] —— 合并没按预期发生"
                           % (parents, local_sha, a.upstream_sha))
    head_now = git_out(repo, "rev-parse", "HEAD")
    if a.checkout and head_now != merge_sha:
        raise RuntimeError("reset --hard 之后 HEAD 不是刚造出来的合并提交")
    if not a.checkout and head_now != local_sha:
        raise RuntimeError("--no-checkout 下 HEAD 竟然动了：%s → %s" % (local_sha[:12], head_now[:12]))
    changed = git_out(repo, "diff", "--name-only", "%s...%s" % (local_sha, a.upstream_sha))
    result.update(exit_code=EXIT_OK, state="merged", merge_sha=merge_sha,
                  parents=parents, upstream_files=len(changed.splitlines()),
                  head_after=head_now,
                  merge_subject=git_out(repo, "show", "-s", "--format=%s", merge_sha))
    log("✅ 合并完成：%s" % merge_sha)
    log("   父提交      %s（本线 HEAD） + %s（上游）" % (local_sha[:12], a.upstream_sha[:12]))
    log("   上游带来    %d 个文件" % result["upstream_files"])
    if a.checkout:
        log("   工作区      HEAD = 合并提交（detached）—— 任何分支都**一个字节没动**")
    else:
        log("   工作区      **没碰**（--no-checkout）：只在对象库里造了合并提交，")
        log("               HEAD 仍是 %s —— 任何 ref 与工作区都没动" % local_sha[:12])

    # ── ⑦ 与检测器给出的期望值核对（build job 的那条路）──
    if a.expect_merge_sha:
        want = assert_sha("--expect-merge-sha", a.expect_merge_sha)
        if want != merge_sha:
            if a.checkout:
                git(repo, "reset", "--hard", local_sha, check=False)
            result.update(exit_code=EXIT_MISMATCH, state="expect-mismatch",
                          merge_sha=merge_sha, head_after=local_sha)
            log("❌ 合并提交与 --expect-merge-sha 不符 —— 已 `reset --hard` 回到 %s" % local_sha[:12])
            log("   期望 %s" % want)
            log("   实际 %s" % merge_sha)
            log("   ⇒ 最可能的原因：本线 HEAD 或上游提交与检测器那次不是同一对（分支动过？），")
            log("     或者合并环境变了（git 版本 / 合并策略）。**两边构建的不是同一个提交**，")
            log("     所以这一次必须失败，而不是把两份产物当同一批次比。")
            return result
        log("   ✅ 与检测器给出的 --expect-merge-sha 相同（两个 build job 会各自重放出它）")
    return result


def render_summary(r):
    """写进 `$GITHUB_STEP_SUMMARY` 的一段。结论要一眼看得见。"""
    L = ["## 试合并（issue #8）", "", "| 项 | 值 |", "|---|---|",
         "| 本线 HEAD | `%s` |" % r["local_sha"],
         "| 上游提交 | `%s` |" % r["upstream_sha"],
         "| 合并基点 | `%s` |" % (r["merge_base"] or "（没算出来）")]
    if r["state"] == "merged":
        L += ["| **合并提交** | `%s` |" % r["merge_sha"],
              "| 提交日期 | `%s`（锚定到上游提交的日期）|" % r["merge_date"],
              "| 上游带来 | %d 个文件 |" % r.get("upstream_files", 0), "",
              "✅ 合并干净 —— 没有 push、没有移动任何分支。"
              + ("（合并提交造在 detached HEAD 上）" if r.get("checked_out")
                 else "（`--no-checkout`：只在对象库里造了合并提交，工作区与 HEAD 都没碰）"), ""]
    elif r["state"] == "conflict":
        L += ["| **结论** | **冲突**（已 abort，分支未动）|", ""]
        L += ["冲突的文件："] + ["- `%s`" % f for f in r.get("conflicted", [])] + [""]
        L += ["按规格「冲突即干净失败」：不推送、不留半成品、**不触发构建**。", ""]
    elif r["state"] == "no-drift":
        L += ["| **结论** | 上游没有漂移（不该走到这里）|", ""]
    elif r["state"] == "expect-mismatch":
        L += ["| **结论** | 与 `--expect-merge-sha` 不符（已 reset 回合并前）|",
              "| 实际算出的合并提交 | `%s` |" % r["merge_sha"], ""]
    else:
        L += ["| **结论** | 合并失败：%s |" % r["state"], ""]
    return "\n".join(L) + "\n"


# ══════════════════════════════════════════════════════════════════════════════
#  自测：临时仓库 + **真 git**，完全不联网
#
#  ⚠️ 测的是「同样的输入给出同样的合并提交」这条判据本身，以及四条失败路径
#     （冲突 / 无漂移 / 期望不符 / 脏工作区）**真的会失败**。
#     最后一条尤其重要：一条永远不会响的检查只会给假的安全感（坑表 #32 同族）。
# ══════════════════════════════════════════════════════════════════════════════
class Args:
    def __init__(self, **kw):
        self.repo_dir = kw.get("repo_dir")
        self.upstream_url = kw.get("upstream_url")
        self.upstream_sha = kw.get("upstream_sha")
        self.merge_base = kw.get("merge_base")
        self.expect_merge_sha = kw.get("expect_merge_sha")
        self.local_sha = kw.get("local_sha")
        self.work_branch = kw.get("work_branch")
        self.checkout = kw.get("checkout", True)


def _init_repo(path, log=None):
    """造一个小仓库：`A`（分叉点）→ `work`（我方 2 个提交）／`up`（上游 1 个提交）。

    这一对分支**真的分歧**，而且 merge-base 就是 `A` —— 与真上游的拓扑同构
    （我们的跟随分支永远带着自己的提交）。
    """
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "-b", "work")
    git(path, "config", "user.name", "fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    git(path, "config", "commit.gpgsign", "false")

    def commit(text, name, msg):
        with open(os.path.join(path, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        git(path, "add", name)
        git(path, "commit", "-q", "-m", msg)

    commit("base\n", "base.txt", "A: 分叉点")
    a = git_out(path, "rev-parse", "HEAD")
    commit("side\n", "side.txt", "W1: 我方的第一个提交")
    commit("more\n", "more.txt", "W2: 我方的第二个提交")
    work = git_out(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "-b", "up", a)
    commit("upstream\n", "up.txt", "U1: 上游新提交")
    up = git_out(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "work")
    return a, work, up


def _init_conflict_repo(path):
    """冲突夹具：两侧改**同一个文件的同一行**。"""
    os.makedirs(path, exist_ok=True)
    git(path, "init", "-q", "-b", "work")
    git(path, "config", "user.name", "fixture")
    git(path, "config", "user.email", "fixture@example.invalid")
    git(path, "config", "commit.gpgsign", "false")

    def commit(text, msg):
        with open(os.path.join(path, "both.txt"), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        git(path, "add", "both.txt")
        git(path, "commit", "-q", "-m", msg)

    commit("v0\n", "A: 分叉点")
    a = git_out(path, "rev-parse", "HEAD")
    commit("ours\n", "W1: 我方改了它")
    work = git_out(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "-b", "up", a)
    commit("theirs\n", "U1: 上游也改了它")
    up = git_out(path, "rev-parse", "HEAD")
    git(path, "checkout", "-q", "work")
    return a, work, up


def self_test(tmpdir):
    """离线用例。返回失败条数。"""
    os.makedirs(tmpdir, exist_ok=True)
    bad = 0
    n = [0]

    def fail(what, *lines):
        nonlocal bad
        bad += 1
        print("  ❌ #%-2d %s\n       %s" % (n[0], what, "\n       ".join(str(x) for x in lines)))

    def case(what, want_exit, args, **checks):
        """跑一次 `run_merge`，断言退出码 + 若干条不变量。"""
        n[0] += 1
        lines = []
        refs = checks.pop("_refs", {})          # ⚠️ 先摘出来：它不是结果字典里的键
        try:
            r = run_merge(args, log=lines.append)
        except (Usage, Mismatch) as e:
            r = {"exit_code": EXIT_USAGE if isinstance(e, Usage) else EXIT_MISMATCH,
                 "state": "usage" if isinstance(e, Usage) else "mismatch", "error": str(e)}
        except Exception as e:                       # 崩溃本身就是不合格
            fail(what, "抛异常：%r" % (e,))
            return None
        problems = []
        if r["exit_code"] != want_exit:
            problems.append("期望退出码 %d，实际 %d（%s）"
                            % (want_exit, r["exit_code"], r.get("state")))
        for k, v in checks.items():
            if r.get(k) != v:
                problems.append("%s 期望 %r，实际 %r" % (k, v, r.get(k)))
        repo = args.repo_dir
        # ★ 不变量①：**没有任何分支被移动**（不论成败）。这是验收「工作分支完全不变」的落地断言。
        for br, sha in refs.items():
            got = git_out(repo, "rev-parse", "refs/heads/" + br)
            if got != sha:
                problems.append("分支 %s 被移动了：%s → %s" % (br, sha[:12], got[:12]))
        # ★ 不变量②：失败之后**不留半成品**（无 MERGE_HEAD、工作区干净、HEAD 回到合并前）。
        if want_exit != EXIT_OK and r.get("head_after"):
            if git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False)[0] == 0:
                problems.append("失败之后 MERGE_HEAD 还在（半成品）")
            if git_out(repo, "status", "--porcelain"):
                problems.append("失败之后工作区不干净（半成品）")
            if git_out(repo, "rev-parse", "HEAD") != r["head_after"]:
                problems.append("失败之后 HEAD 不是声称回退到的那个提交")
        if problems:
            fail(what, *problems, *lines[-4:])
        else:
            print("  ✅ #%-2d %-44s -> 退出码 %d / %s"
                  % (n[0], what[:44], r["exit_code"], r.get("state")))
        return r

    # ①② 干净合并 + **确定性**（同一对输入两次给出同一个 sha）
    d1 = os.path.join(tmpdir, "clean")
    a, work, up = _init_repo(d1)
    r1 = case("① 干净合并", EXIT_OK, Args(repo_dir=d1, upstream_sha=up, merge_base=a,
                                          work_branch="work", local_sha=work),
              state="merged", _refs={"work": work})
    # ⚠️ 一次成功的试合并会把 HEAD 留在合并提交上（detached）⇒ 重放之前必须回到起点，
    #    否则第二次看到的是「上游已经是我的祖先」，量到的是别的东西。
    #    这条也是给调用方的说明：**同一个工作区只能试合并一次**。
    git(d1, "reset", "--hard", work)
    r2 = case("② 重放同一次合并 ⇒ 同一个提交 sha", EXIT_OK,
              Args(repo_dir=d1, upstream_sha=up, merge_base=a, work_branch="work"),
              state="merged", _refs={"work": work})
    n[0] += 1
    if r1 and r2 and r1["merge_sha"] and r1["merge_sha"] == r2["merge_sha"]:
        print("  ✅ #%-2d %-44s -> %s（两次逐字相同）"
              % (n[0], "② 附属：两次合并提交逐字相同", r1["merge_sha"][:12]))
    else:
        fail("② 附属：两次合并应给出同一个 sha",
             "第一次 %s / 第二次 %s" % (r1 and r1["merge_sha"], r2 and r2["merge_sha"]))

    # ③ `--expect-merge-sha` 相符时通过
    git(d1, "reset", "--hard", work)
    case("③ --expect-merge-sha 相符 ⇒ 通过", EXIT_OK,
         Args(repo_dir=d1, upstream_sha=up, merge_base=a, work_branch="work",
              expect_merge_sha=(r1 or {}).get("merge_sha") or "f" * 40), state="merged")

    # ④ 与期望不符 ⇒ 失败 + **回退到合并前**
    git(d1, "reset", "--hard", work)
    case("④ --expect-merge-sha 不符 ⇒ 失败并回退", EXIT_MISMATCH,
         Args(repo_dir=d1, upstream_sha=up, merge_base=a, work_branch="work",
              expect_merge_sha="f" * 40),
         state="expect-mismatch", head_after=work, _refs={"work": work})

    # ⑤ merge-base 与外部权威不符 ⇒ 失败（**不合并**）
    git(d1, "reset", "--hard", work)
    case("⑤ --merge-base 不符 ⇒ 失败", EXIT_MISMATCH,
         Args(repo_dir=d1, upstream_sha=up, merge_base="e" * 40, work_branch="work"),
         state="mismatch", _refs={"work": work})

    # ⑥ 本线 HEAD 与 --local-sha 不符 ⇒ 失败
    case("⑥ --local-sha 不符 ⇒ 失败", EXIT_MISMATCH,
         Args(repo_dir=d1, upstream_sha=up, merge_base=a, work_branch="work",
              local_sha="d" * 40), state="mismatch", _refs={"work": work})

    # ⑦ 没有漂移（上游提交是 work 的祖先）⇒ 退出码 3，**不做任何合并**
    d7 = os.path.join(tmpdir, "nodrift")
    a7, work7, _ = _init_repo(d7)
    case("⑦ 没有漂移 ⇒ 退出码 3，不合并", EXIT_NO_DRIFT,
         Args(repo_dir=d7, upstream_sha=a7, merge_base=a7, work_branch="work"),
         state="no-drift", merge_sha=None, head_after=work7, _refs={"work": work7})

    # ⑧ 冲突 ⇒ 退出码 4，abort + 回退，分支不动
    d8 = os.path.join(tmpdir, "conflict")
    a8, work8, up8 = _init_conflict_repo(d8)
    r8 = case("⑧ 冲突 ⇒ 退出码 4 + abort + 回退", EXIT_CONFLICT,
              Args(repo_dir=d8, upstream_sha=up8, merge_base=a8, work_branch="work"),
              state="conflict", head_after=work8, _refs={"work": work8})
    n[0] += 1
    if r8 is None:
        pass
    elif r8.get("conflicted") == ["both.txt"]:
        print("  ✅ #%-2d %-44s -> 冲突文件 %s" % (n[0], "⑧ 附属：冲突文件被列出来",
                                                r8["conflicted"]))
    else:
        fail("⑧ 附属：冲突文件应恰好是 both.txt", r8.get("conflicted"))

    # ⑨ 脏工作区 ⇒ **拒绝运行**（两条回退路径的安全前提）。
    #    ⚠️ 夹具必须改**已跟踪**的文件：未跟踪文件不算脏（见 run_merge 里的注释 ——
    #    `reset --hard` 不删它们，而 build job 里就有 `base.img` 这样的未跟踪文件）。
    d9 = os.path.join(tmpdir, "dirty")
    a9, work9, up9 = _init_repo(d9)
    with open(os.path.join(d9, "base.txt"), "a", encoding="utf-8") as f:
        f.write("改动了已跟踪文件\n")
    case("⑨ 已跟踪文件被改过 ⇒ 拒绝运行", EXIT_USAGE,
         Args(repo_dir=d9, upstream_sha=up9, merge_base=a9, work_branch="work"),
         _refs={"work": work9})
    # ⑨b 只有**未跟踪**文件 ⇒ 照常合并（这就是 build job 那条路：工作目录里有 base.img）
    with open(os.path.join(d9, "base.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write("base\n")
    with open(os.path.join(d9, "base.img"), "w", encoding="utf-8") as f:
        f.write("128 MiB 的底包在这里（未跟踪）\n")
    r9b = case("⑨b 只有未跟踪文件 ⇒ 照常合并", EXIT_OK,
               Args(repo_dir=d9, upstream_sha=up9, merge_base=a9, work_branch="work"),
               state="merged", _refs={"work": work9})
    n[0] += 1
    if r9b and os.path.exists(os.path.join(d9, "base.img")):
        print("  ✅ #%-2d %-44s -> 合并后那个未跟踪文件仍在"
              % (n[0], "⑨b 附属：reset --hard 没删未跟踪文件"))
    else:
        fail("⑨b 附属：reset --hard 不该删掉未跟踪文件", "base.img 不见了")

    # ⑩ 形状守卫：缩写 sha / 非 sha 一律拒绝（歧义在浅仓库里会变成一次**错的合并**）
    for what, kw in (("⑪ 缩写 sha ⇒ 拒绝", {"merge_base": a[:12]}),
                     ("⑫ 非十六进制 sha ⇒ 拒绝", {"upstream_sha": "z" * 40})):
        n[0] += 1
        args = Args(repo_dir=d1, upstream_sha=up, merge_base=a, work_branch="work")
        args.__dict__.update(kw)
        try:
            run_merge(args, log=_quiet)
            fail(what, "没拒绝")
        except Usage as e:
            print("  ✅ #%-2d %-44s -> %s" % (n[0], what, str(e).split("\n")[0][:44]))

    # ⑬ 本地对象不全 + 没给 --upstream-url ⇒ 明确报用法错误（不是崩在 git 上）
    #    ⚠️ 用**编出来的** sha：删掉一个分支并不能让对象消失（要等 gc），
    #    那样夹具其实什么都没造出来 —— 它会一路走到底并「合并成功」。
    case("⑬ 对象不全且没给 --upstream-url ⇒ 退出码 2", EXIT_USAGE,
         Args(repo_dir=d1, upstream_sha="a" * 40, merge_base=a, work_branch="work"),
         state="usage")

    # ⑭ `--no-checkout`：**只在对象库里造合并提交**，HEAD 与工作区都不动
    d14 = os.path.join(tmpdir, "nocheckout")
    a14, work14, up14 = _init_repo(d14)
    r14 = case("⑭ --no-checkout：不碰工作区，HEAD 不动", EXIT_OK,
               Args(repo_dir=d14, upstream_sha=up14, merge_base=a14, work_branch="work",
                    checkout=False),
               state="merged", head_after=work14, checked_out=False,
               _refs={"work": work14})
    n[0] += 1
    dirty14 = git_out(d14, "status", "--porcelain")
    if dirty14:
        fail("⑭ 附属：--no-checkout 不该改动工作区", dirty14.splitlines()[:4])
    else:
        print("  ✅ #%-2d %-44s -> HEAD 仍在 %s，工作区干净"
              % (n[0], "⑭ 附属：工作区与 HEAD 都没动", work14[:12]))
    # ⚠️ 同一个仓库再跑一次 `--checkout` 必须仍然正确：说明 no-checkout 那次留下的对象
    #    没有把这个仓库变成「已经合并过」的状态（HEAD 没动 ⇒ 判据也没被污染）。
    if r14:
        git(d14, "update-ref", "refs/heads/work", work14)
        case("⑮ 之后改用 --checkout 仍得到同一个 sha", EXIT_OK,
             Args(repo_dir=d14, upstream_sha=up14, merge_base=a14, work_branch="work",
                  expect_merge_sha=r14["merge_sha"]), state="merged")

    total = n[0]
    print()
    print("结果: %s（%d/%d 通过）" % ("失败" if bad else "全部通过", total - bad, total))
    return bad


def _quiet(*_a, **_kw):
    pass


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        add_help=True, description="试合并上游（绝不 push；见文件头）",
        epilog="退出码：0 合并成功 / 3 没有漂移 / 4 冲突 / 5 前提不符 / 1 运行错误 / 2 用法错误")
    ap.add_argument("--repo-dir", default=".", help="仓库目录（默认当前目录）")
    ap.add_argument("--no-checkout", dest="checkout", action="store_false",
                    help="只在对象库里算出合并提交，**不碰工作区、不移动 HEAD**。"
                         "检测器只需要那个 sha（规格里「试合并在临时工作区」的那一步它不需要）；"
                         "⚠️ 它也是**本机 Windows 上唯一跑得通的模式** —— 内核树里有 "
                         "`drivers/gpu/drm/nouveau/nvkm/subdev/i2c/aux.c` 这类路径，"
                         "`aux` 是 Windows 保留设备名，工作区根本检不出来")
    ap.add_argument("--work-branch", metavar="分支",
                    help="本线工作分支（只用来取历史与写日志；**绝不写它**）")
    ap.add_argument("--upstream-url", metavar="URL",
                    help="上游仓库 URL（本地对象不全时才会用到）")
    ap.add_argument("--upstream-sha", metavar="SHA", help="上游那个提交（40 位）")
    ap.add_argument("--merge-base", metavar="SHA",
                    help="外部权威给出的 merge-base（检测器从 compare API 拿的那个）")
    ap.add_argument("--local-sha", metavar="SHA", help="期望的本线 HEAD（给了就核）")
    ap.add_argument("--expect-merge-sha", metavar="SHA",
                    help="期望的合并提交（build job 用它核对检测器给出的那个）")
    ap.add_argument("--json-out", metavar="文件", help="把结果写一份 JSON")
    ap.add_argument("--github-output", metavar="文件", help="写 `key=value`（喂 $GITHUB_OUTPUT）")
    ap.add_argument("--summary", metavar="文件", help="追加一段 Markdown（喂 $GITHUB_STEP_SUMMARY）")
    ap.add_argument("--self-test", action="store_true", help="离线跑内置用例（临时仓库 + 真 git）")
    ap.add_argument("--self-test-dir", metavar="目录")
    a = ap.parse_args(argv[1:])

    if a.self_test:
        d = a.self_test_dir or os.path.join(tempfile.gettempdir(), "try-merge-selftest")
        if os.path.isdir(d) and not a.self_test_dir:
            shutil.rmtree(d, ignore_errors=True)
        return 1 if self_test(d) else 0

    missing = [n for n in ("upstream_sha", "merge_base") if not getattr(a, n)]
    if missing:
        print("缺必填参数: %s" % " / ".join("--" + m.replace("_", "-") for m in missing))
        print("⚠️ --merge-base 是**必填**：浅历史里 `git merge-base` 可能返回一个被截断的祖先，"
              "拿它合并会得到一次**不报错的错合并**。判据只能来自外部（compare API）。")
        return EXIT_USAGE
    try:
        a.upstream_sha = assert_sha("--upstream-sha", a.upstream_sha)
        a.merge_base = assert_sha("--merge-base", a.merge_base)
        a.local_sha = assert_sha("--local-sha", a.local_sha) if a.local_sha else None
        a.expect_merge_sha = (assert_sha("--expect-merge-sha", a.expect_merge_sha)
                              if a.expect_merge_sha else None)
    except Usage as e:
        print(e)
        return EXIT_USAGE

    try:
        r = run_merge(a, log=print)
    except Usage as e:
        print("❌ 用法 / 环境错误：%s" % e)
        return EXIT_USAGE
    except Mismatch as e:
        print("❌ 前提不符：%s" % e)
        return EXIT_MISMATCH
    except Exception as e:
        # ⚠️ 非预期异常**连栈一起打**：这一层本来是给「git 失败 / 网络失败」用的，
        #    真出了编程错误时，只留一句消息会让排障从十分钟变成一小时。
        import traceback
        traceback.print_exc()
        print("❌ 运行错误：%s" % e)
        return EXIT_RUN

    print()
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(r, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    if a.github_output:
        with open(a.github_output, "a", encoding="utf-8", newline="\n") as f:
            for k in ("exit_code", "state", "merge_sha", "merge_base", "merge_date",
                      "local_sha", "upstream_sha", "upstream_files"):
                f.write("%s=%s\n" % (k, r.get(k) if r.get(k) is not None else ""))
    if a.summary:
        with open(a.summary, "a", encoding="utf-8", newline="\n") as f:
            f.write(render_summary(r))
    return r["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
