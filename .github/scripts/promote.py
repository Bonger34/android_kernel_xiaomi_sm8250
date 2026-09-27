#!/usr/bin/env python3
"""推进工作分支：**构建全绿之后**，才把那次试合并推到工作分支上（接缝 S6 · issue #9）。

规格：`docs/los-line/upstream-sync-spec.md` 解决方案与用户故事 4/5：

> **推进**：**全绿才**把这次合并推到分支；红了就开 issue 并留失败产物，**分支一个字节不动**。

于是「**工作分支 = 可发布状态**」这个不变量由机器守住，而不是靠人的克制。

## 为什么单独做一件工具，而不是 workflow 里一句 `gh api PATCH`

三条**判据**必须可执行，而不只是写在注释里（同 `trigger-build.py` 的理由）：

1. ⭐ **「红的运行一个字节都不动」** —— 本工具**只在被调用时**才写 ref，
   而 workflow 里那一步挂在 `success()` 上 ⇒ 红的那次**根本不会调用它**。
   调用与不调用是两个不同的东西，比「调用它然后指望它克制」可靠。
2. ⭐ **正常路径只有一次写，而且那一次天然是 CAS。**
   全工具**唯一**的写操作是 `PATCH repos/{repo}/git/refs/heads/{branch}`
   带着 `sha` = 合并提交、**`force: false`**。
   `force=false` ⇒ 非快速前进时 GitHub **拒绝**（422），
   于是「覆盖掉别人刚推的东西」在结构上不可能 —— 这正是验收第 4 条
   （「检测器与构建器不会同时写同一条工作分支」）要的那个性质。
3. **「这次合并确实是那个东西」要在推之前独立复核**，而不是信调用方传进来的三个 sha：
   `父[0] == --base-sha`（试合并时的本线 HEAD）且 `父[1] == --upstream-sha`（上游提交）。
   两个父都对不上时**一个请求都不发**（退出码 2）。

> ⚠️ **`--base-sha` 是刻意做成一条判据的**，而不是「某个可传可不传的 sha」：
> 漏传时那条判据会**静默消失** —— 又一次「不完整的全绿」。
> 于是 `--merge-sha` / `--base-sha` / `--upstream-sha` **三个都是必填**。
>
> 🚨 **调用方怎么拿到 `--merge-sha`（这一条第一版写错了，独立审查抓到）**：
> `try-merge.py --no-checkout` **刻意不移动 HEAD**（它自己有断言），
> 所以重放完之后 **`git rev-parse HEAD` 给的是 checkout 出来的分支尖端，不是合并提交**。
> 正确做法是读重放那一步**写出来的输出**：
>
> ```sh
> python3 …/try-merge.py --no-checkout --repo-dir . \
>     --work-branch "$WORK_BRANCH" --upstream-url "$UPSTREAM_URL" \
>     --upstream-sha "$UPSTREAM_SHA" --merge-base "$MERGE_BASE" \
>     --expect-merge-sha "$MERGE_SHA" --github-output "$GITHUB_OUTPUT"
> MSHA="$(sed -n 's/^merge_sha=//p' "$GITHUB_OUTPUT" | tail -1)"
> [ "$MSHA" = "$MERGE_SHA" ] || exit 1     # ⚠️ 这一步别省，见下
> PARENT="$(git rev-parse "$MSHA^1")"      # 父[0] —— 试合并时的本线 HEAD
> python3 …/promote.py --merge-sha "$MSHA" --base-sha "$PARENT" \
>     --upstream-sha "$UPSTREAM_SHA" …
> ```
>
> ⚠️ **那个 `[ "$MSHA" = "$MERGE_SHA" ]` 不是多余的**：`try-merge.py` 的
> `--expect-merge-sha` 已经核过一次，但**它的结果除了「这一步的退出码」之外没有任何人读**
> —— 这一步的退出码会被 `case` 吞掉。多这一句，「要推的 = 编过的」才是一条**看得见**的判据。
>
> ⚠️ 为什么非得绕这一道（本工单实测）：`workflow_dispatch` 的 inputs 不接受未声明的名字，
> 原先给它单开一个 `local_sha` 输入之后，`trigger-build.py` 的派发**当场 422**
> （`Unexpected inputs provided: ["local_sha"]`）。少一个能写歪的输入本来也更好 ——
> 「试合并时的本线 HEAD」就是合并提交的父[0]，不必请人去传。

## 五条不变量

① **绝不强推。** `force` 永远 `false`，而且推之前先读一次远端 ref：
   它不等于 `父[0]` 时**根本不发 PATCH**（退出码 3）—— 那是「分支上已经有别的东西」，
   自己让路比让平台拒绝更早、更清楚。

② **幂等。** 远端已经是合并提交时直接成功（`already`，退出码 0）。
   重复调用、重跑一次 job 都不会出错。

③ **合并提交必须与上游提交对得上。** `父[1] == --upstream-sha` —— 否则这次推的是
   一个「合并了别的东西」的提交，而名字上写着上游同步。

④ **时间锚定要在推之前核一遍**（验收第 2 条：「合并提交的时间锚定到**上游提交的日期**，
   不是运行时刻」）。`try-merge.py` 就是这么造的（`GIT_COMMITTER_DATE` 取自上游提交的
   `%cI`），这里**从产物侧复核**：合并提交的 committer date 必须逐字等于上游提交的
   committer date。⚠️ 它不只是「好看」—— `KBUILD_BUILD_TIMESTAMP` 就取自
   `git show -s --format=%cd HEAD`，日期一飘，两次构建的字节就不同。
   ⚠️ 上游提交不在本地时这一条**降级为「只记不核」**并明说：它只是核对用的输入，
   而「合的是那个提交」已由 ③ 证明 —— 别让一次核对失败拦下一次推进。

⑤ **推完复核。** 再读一次远端 ref，必须等于合并提交，并把这次运行动过的 ref 打出来
   （只有它一条）。⚠️ 那行不只是日志：`--branch` 或 API 路径写歪时，它会是**唯一**
   能看见的东西。

## 退出码（都是正常结论，别写成「非零即崩」）

| 结论 | 码 | 含义 |
|---|---|---|
| `promoted` / `already` | **0** | 已推进（或远端本来就在这个提交上） |
| 运行错误 | 1 | 网络、5xx、git 失败、推完复核不符 |
| 用法 / 前置错误 | 2 | 缺参数、`父[0]/父[1]` 不符、远端没有这条分支、合并提交不在本地 |
| 分支已前进 | 3 | 远端 ref ≠ `父[0]` 且自己不是它的后代 ⇒ **自己让路**（未发任何写请求） |
| 推的时候被抢 | 4 | PATCH 被平台按非快速前进拒掉（并发写；**不是**本工具坏了） |

⚠️ `3` 与 `4` 都**不该**被当成「工具崩了」：它们是「这次不推」，而且**都没动分支**。

## 合并提交从哪来（本工具**取不到**它）

合并提交**不在任何 ref 上**（规格要求「绝不 push」），而 GitHub 的 upload-pack
**不提供不可达对象** —— 本工单实测：

```
$ git fetch --no-tags origin 8f73efefa343ae57845154d90b2d36c965cfdc9d
fatal: remote error: upload-pack: not our ref 8f73efefa343ae57845154d90b2d36c965cfdc9d
```

⇒ 本工具**不 fetch**，只断言「它在本地对象库里」（`assert_local_commit()`）。
调用方在调它之前**重放一次那次合并**即可 —— `try-merge.py` 是确定性的
（同样的输入给出同一个合并提交，issue #8 已实测），而且用 `--no-checkout`
就**不移动 HEAD、不碰工作区**：

```sh
python3 .github/scripts/try-merge.py --no-checkout --repo-dir . \
    --work-branch "$WORK_BRANCH" --upstream-url "$UPSTREAM_URL" \
    --upstream-sha "$UPSTREAM_SHA" --merge-base "$MERGE_BASE" \
    --expect-merge-sha "$MERGE_SHA"
```

⚠️ 这次重放**不是走过场**：`--expect-merge-sha` 与检测器给出的值不符时，
这一步会当场失败 —— 于是「要推的就是那两个 build job 编的那个提交」又被核了一遍
（检测器算一次、两个 build job 各重放一次、**这里第四次**）。

⚠️ 顺带一条实测：`--no-checkout` 在**本机**（Windows、空工作树）跑得通，
而带 checkout 的模式不行 —— 内核树里有 `aux.c` 这类 Windows 保留设备名
（`PROJECT.md` §7 坑表 #41）。

## 用法

```sh
# CI（build.yml 的 repro job，**全绿之后**那一步）
# ① 先重放那次合并（合并提交只在 build job 的对象库里，repro 这个 job 是新的检出）。
#    🚨 `--merge-sha` **不能**用 `git rev-parse HEAD` —— `--no-checkout` 刻意不移动 HEAD，
#       那时 HEAD 是**分支尖端**。从重放那一步写出来的输出里读（见上面的详注）。
python3 .github/scripts/try-merge.py --no-checkout --repo-dir . \
    --work-branch "$WORK_BRANCH" --upstream-url "$UPSTREAM_URL" \
    --upstream-sha "$UPSTREAM_SHA" --merge-base "$MERGE_BASE" \
    --expect-merge-sha "$MERGE_SHA" --github-output "$GITHUB_OUTPUT"
MSHA="$(sed -n 's/^merge_sha=//p' "$GITHUB_OUTPUT" | tail -1)"
[ "$MSHA" = "$MERGE_SHA" ] || { echo "重放出来的不是检测器给的那个"; exit 1; }
# ② 再推（唯一入口；`success()` 保证红的运行走不到这里）
python3 .github/scripts/promote.py \
    --repo "$GITHUB_REPOSITORY" --branch "$WORK_BRANCH" --repo-dir . \
    --merge-sha "$MSHA" --base-sha "$(git rev-parse "$MSHA^1")" \
    --upstream-sha "$UPSTREAM_SHA" \
    --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
    --summary "$GITHUB_STEP_SUMMARY"

# 本机：只读演练（判据全跑，一条写请求都不发）
python tools/promote.py --repo Bonger34/android_kernel_xiaomi_sm8250 \
    --branch ksu-lineage-23.2 --repo-dir build/los \
    --merge-sha <sha> --base-sha <sha> --upstream-sha <sha> --dry-run

python tools/promote.py --self-test      # 离线，临时仓库 + 真 git + 假 API，不联网
```

⚠️ **本文件有两份，必须逐字节相同**：工作区 `tools/promote.py` 与 LOS 工作仓库里的
`.github/scripts/promote.py`（内核树**自带**一个 `tools/`，那是上游的，不能占用）。
镜像与复验：`python tools/sync-ci-scripts.py --gen` / `--check`。
"""

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

API_DEFAULT = "https://api.github.com"
UA = "umi-loskernel-promote/1"
TIMEOUT = 60
TRIES = 3

EXIT_OK = 0
EXIT_RUN = 1
EXIT_USAGE = 2
EXIT_REF_MOVED = 3
EXIT_RACE = 4

# 提交时间必须带时区偏移（`%cI` 的形态）。⚠️ 不转成数字比较：那要再实现一遍
# 时区换算，而这里要证明的只是「两边是同一个时刻」——逐字相等是最强的判据。
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?"
                     r"(Z|[+-]\d{2}:\d{2})$")

# 控制台是 GBK：不先改 stdout 编码，满屏的 ✅/❌ 会直接抛 UnicodeEncodeError。
# ⚠️ 放在**模块级**而不是 `main()` 里：自测与应用都会调本模块的函数，
#    只有 `main()` 里那一份的话，`import` 走的路径没有这道保护。
# ⚠️ 不用 `line_buffering=True`：那个开关是给「**自己派生的子进程直接写 fd 1**」的脚本用的
#    （`package.py` 踩过，坑表 #33）。本文件所有 git 调用都走 `run()`，
#    它们的 stdout/stderr 被重定向到**临时文件**、没有任何子进程写 fd 1 ⇒ 理由不成立。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


class Usage(RuntimeError):
    """用法 / 前置错误（退出码 2）—— 与运行错误分开，善后完全不同。"""


class RefMoved(RuntimeError):
    """分支已经前进（退出码 3）。⚠️ **不是**运行错误：这是「这次不推」。

    与 `RaceLost` 一样带一份 `result`：**没推**也是一个有内容的结果，
    运行摘要要说清「远端前后各是什么、为什么这次不推」。
    """

    def __init__(self, message, result=None):
        RuntimeError.__init__(self, message)
        self.result = result


class RaceLost(RuntimeError):
    """推的时候被平台按非快速前进拒掉（退出码 4）—— 并发写，不是本工具坏了。

    ⚠️ 带一份 `result`：**没推成**也是一个有内容的结果（远端前后各是什么、判据是什么），
    运行摘要与 `--github-output` 要把它说清楚，而不是只留一句「失败了」。
    """

    def __init__(self, message, result=None):
        RuntimeError.__init__(self, message)
        self.result = result


# ══════════════════════════════════════════════════════════════════════════════
#  纯决策：把「读到的 ref 值」映射成「该不该推」
#
#  ⚠️ 抽成纯函数是为了能**穷举三种情形**（相等 / 自己是后代 / 都不是），
#     而不必造三个真仓库 —— `try-merge.py` 的 `parse_merge_tree()` 同理。
# ══════════════════════════════════════════════════════════════════════════════
class Decision(NamedTuple):
    kind: str        # push / already / moved
    exit_code: int
    reason: str


def decide(remote_sha, parent, merge_sha, is_ancestor):
    """`remote_sha`（刚读到的远端 ref）⇒ 该不该推。

    `is_ancestor(a, b)` 回答「a 是不是 b 的祖先」。判据链：

    | 远端 ref | 结论 | 为什么 |
    |---|---|---|
    | 没有这条分支 | **用法错误**（2） | 「全绿才推」的前提是分支存在；不存在说明仓库/分支名写歪了 |
    | `== parent` | `push` | 正常路径：快速前进 |
    | `== merge_sha` | `already` | **幂等**（重跑一次 job 不该红） |
    | 是 `merge_sha` 的祖先 | `push` | 分支自己往前走了但仍在我们后面 ⇒ 照样是快速前进，`force=false` 会放行 |
    | 其他 | `moved`（3） | 分支上有**别的东西**了 ⇒ 自己让路，**不发写请求** |

    ⚠️ 第一行是 `Usage` 而不是 `moved`：这两件事善后完全不同 ——
    「分支不存在」要去查配置，「分支被推过」只需等下一轮。
    """
    if not remote_sha:
        raise Usage("远端没有这条分支（ref 不存在）—— 见上面对应的原因说明。")
    if remote_sha == merge_sha:
        return Decision("already", EXIT_OK,
                        "远端已经是这次合并提交（重复调用/重跑，幂等）")
    if remote_sha == parent:
        return Decision("push", EXIT_OK, "远端 == 合并提交的父[0]，标准快速前进")
    if is_ancestor(remote_sha, merge_sha):
        return Decision("push", EXIT_OK,
                        "远端 %s 是这次合并的祖先（分支自己往前走了，但仍在合并之内）"
                        % remote_sha[:12])
    return Decision("moved", EXIT_REF_MOVED,
                    "远端 %s 既不是父[0] %s，也不是这次合并的后代 —— 分支上有别的东西了"
                    % (remote_sha[:12], parent[:12]))


class Api:
    """GitHub REST 的最小客户端（只用标准库）。

    ⚠️ 与 `trigger-build.py` 一样**故意**不与 `detect.py` 共用一个 HTTP 客户端：
    那会让两件工具互相牵连，而各自需要的东西只有这几十行。
    """

    def __init__(self, token=None):
        self.token = token

    def call(self, method, path, payload=None):
        """返回 `(status, 解析后的 JSON)`。**4xx 不在这里抛** —— 调用方要按码说话
        （422 是「并发写」，403 是「少权限」，两者善后完全不同）。"""
        last = None
        for i in range(TRIES):
            try:
                return self._one(method, path, payload)
            except urllib.error.HTTPError as e:
                if e.code < 500:
                    return e.code, _body(e)
                last = e
            except Exception as e:                       # 网络层（超时、DNS、TLS…）
                last = e
            if i + 1 < TRIES:
                time.sleep(2 * (i + 1))
        raise RuntimeError("%s %s 失败（重试 %d 次）：%s" % (method, path, TRIES, _redact(last)))

    def _one(self, method, path, payload):
        url = "%s/%s" % (API_DEFAULT, path.lstrip("/"))
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read()
        return r.status, (json.loads(body.decode("utf-8")) if body.strip() else {})


def _body(e):
    try:
        return json.loads(e.read().decode("utf-8", "replace"))
    except Exception:
        return {}


def _redact(e):
    """网络层的异常消息里**可能**带上带 token 的 URL（`urllib` 的 `HTTPError` 会把它
    塞进 `str(e)`）。打日志前先把 `gh[pousr]_…` / `github_pat_…` 抹掉 ——
    凭据泄漏进 CI 日志是不可逆的，而这里只是为了一行报错。"""
    return re.sub(r"(gh[pousr]_|github_pat_)[A-Za-z0-9_]+", r"\1<redacted>", str(e))


def qs(x):
    """查询串 / 路径片段里的值（分支名、ref）。斜杠也转义 —— 它在 ref 查询里没有语义。"""
    return urllib.parse.quote(x, safe="")


def assert_sha(name, value):
    """形状守卫：40 位十六进制。写歪的 sha 会让 API 去取一个不存在的东西，
    而那时的报错完全看不出是「参数写错了」。"""
    v = (value or "").strip().lower()
    if len(v) != 40 or any(c not in "0123456789abcdef" for c in v):
        raise Usage("%s 必须是完整的 40 位 sha（收到 %r）—— 缩写 sha 在 API 上是取不到的"
                    % (name, value))
    return v


def assert_branch(name):
    """分支名的形状守卫。⚠️ `refs/` 前缀、空白、`..` 一律拒绝：本工具会把它拼进
    `refs/heads/<branch>`，写歪了就会去写一条**别的** ref —— 而那条 ref 是
    `sync-heartbeat` 或别的什么，谁也没打算动它（`detect.py` 的同名守卫同理）。

    ⚠️ 除了「本项目那几条」之外，这里也把 **git 自己的 ref 规则**里被禁的字符挡掉
    （`~ ^ : ? * [ \\` 与控制字符、`@{`、首尾点、`.lock` 结尾）——
    否则它们只能靠 API 兜底成 404，而 404 在调用方看来是「分支不存在」，
    与本意（「名字写歪了」）差着一层。
    """
    bad = []
    if not name or name.strip() != name:
        bad.append("为空或带首尾空白")
    if any(c in name for c in " \t\n\\~^:?*["):
        bad.append("含空白、反斜杠或 git 禁用的字符（`~ ^ : ? * [`）")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        bad.append("含控制字符")
    if name.startswith("/") or name.endswith("/") or name.endswith("."):
        bad.append("以斜杠或点开头 / 结尾")
    if ".." in name or "@{" in name or name.endswith(".lock"):
        bad.append("含 `..` / `@{`，或以 `.lock` 结尾")
    if name.startswith("refs/"):
        bad.append("以 `refs/` 开头")
    if bad:
        raise Usage("--branch 不合法（%s）：%r" % ("；".join(bad), name))
    return name


def assert_date(what, value):
    """提交时间的形状守卫：必须是带时区偏移的 ISO（`%cI` 形态）。

    ⚠️ 为什么非要带偏移：`KBUILD_BUILD_TIMESTAMP` 由 `build.yml` 用
    `--date=format-local:…` 从它现算，而 `format-local` 受 **TZ** 影响。
    一个不带偏移的本地时间会让「同一个提交在任何机器上给出同一串字节」不成立。
    """
    if not DATE_RE.match(value or ""):
        raise Usage("%s 不是带时区偏移的 ISO 时间（`%%cI` 形态）：%r" % (what, value))
    return value


# ══════════════════════════════════════════════════════════════════════════════
#  git（唯一与「取对象」有关的部分）
# ══════════════════════════════════════════════════════════════════════════════
def run(argv, cwd=None):
    """跑一条命令并取回 `(rc, stdout, stderr)`。

    ⚠️ **输出落临时文件，不用 `capture_output=True`**：本机（Windows）的沙箱
    **禁止创建管道** —— `subprocess.run(..., capture_output=True)` 抛
    `PermissionError(13, '拒绝访问')`，而把 stdout 指向一个**文件**完全正常。
    两条路在 CI 上等价（都只是重定向）。同 `try-merge.py` 的 `run()`。
    """
    out = tempfile.TemporaryFile()
    err = tempfile.TemporaryFile()
    try:
        p = subprocess.run(argv, cwd=cwd, stdout=out, stderr=err)
        out.seek(0)
        err.seek(0)
        return (p.returncode,
                out.read().decode("utf-8", "replace"),
                err.read().decode("utf-8", "replace"))
    finally:
        out.close()
        err.close()


def git(repo, *args, **kw):
    rc, out, err = run(["git"] + list(args), cwd=repo)
    if kw.get("check", True) and rc != 0:
        raise RuntimeError("git %s 失败（退出码 %d）\n    %s"
                           % (" ".join(args), rc, (err or out).strip()[:800]))
    return rc, out.strip(), err.strip()


def git_out(repo, *args):
    """git 的输出当字符串用。

    ⚠️ **把非 ASCII 剔掉**：本机控制台是 GBK，而 git 的输出是 UTF-8（提交信息里就有中文）
    —— 带着 🚫/中文 的字节回到 `print` 会变成
    `UnicodeEncodeError: 'gbk' codec can't encode character …`，
    也就是「本该报 A、实际炸在 B」。这里只用来取 sha / 分支名 / 时间（全 ASCII），
    剔掉非 ASCII 不损失任何东西，却把那条假失败堵住了。
    """
    raw = git(repo, *args)[1]
    if any(ord(c) > 127 for c in raw):
        sys.stderr.write("  ⚠️ git 输出里含非 ASCII 字符（本机 GBK 控制台打不出来），已剔除\n")
        raw = "".join(c for c in raw if ord(c) < 128)
    return raw


def has_commit(repo, sha):
    return git(repo, "cat-file", "-e", "%s^{commit}" % sha, check=False)[0] == 0


def assert_local_commit(repo, sha, what):
    """`sha` 必须在**本地对象库**里。返回 `None`（本地已有）或抛 `Usage`。

    ⚠️ **这一条不是可以「顺手 fetch 一下」补上的** —— 那是本工单实测出来的一个硬约束：

    > 合并提交**不在任何 ref 上**（规格要求「绝不 push」），而 GitHub 的
    > upload-pack **不提供不可达对象**：实测
    > `git fetch --no-tags origin 8f73efefa343…`
    > ⇒ `fatal: remote error: upload-pack: not our ref 8f73efefa343…`。

    所以「本地没有它」只能靠**重新试合并**（`try-merge.py` 是确定性的：同样的输入给出
    同一个合并提交，issue #8 已实测），而**不是**靠取。报错里直接说这条，
    免得下一个人先花半小时去查「为什么 fetch 不到」。
    """
    if has_commit(repo, sha):
        return
    raise Usage(
        "%s %s 不在本地对象库里。\n"
        "   ⇒ **取不到它**：合并提交不在任何 ref 上，而 GitHub 的 upload-pack 不提供\n"
        "     不可达对象（实测 `git fetch origin <sha>` 回\n"
        "     `fatal: remote error: upload-pack: not our ref <sha>`）。\n"
        "   ⇒ 正确做法是在这个检出里**重放那次合并**（`try-merge.py` 是确定性的，\n"
        "     同样的输入给出同一个合并提交）：\n"
        "       python3 .github/scripts/try-merge.py --no-checkout --repo-dir . \\\n"
        "         --work-branch <分支> --upstream-url <URL> --upstream-sha <上游 sha> \\\n"
        "         --merge-base <合并基点> --expect-merge-sha %s\n"
        "     `--no-checkout` ⇒ 只造对象，**不移动 HEAD、不碰工作区**。\n"
        "   ⇒ 另两种可能：① 这次不是上游同步通道触发的（`--merge-sha` 是手填的）；\n"
        "     ② 那个合并提交从来没被造成过（检测器那次试合并失败了）。"
        % (what, sha[:12], sha))


def head_ref(repo):
    """当前检出的那个 ref 名（detached ⇒ `HEAD`）。**只用于日志** ——
    推分支与 HEAD 无关（见文件头 ⑤）。"""
    return git_out(repo, "rev-parse", "--abbrev-ref", "HEAD")


def _load_sibling(name, filename):
    """加载同目录（或工作区 `tools/`）下的另一件工具。

    为什么要回退：CI 里这些脚本跑在 `.github/scripts/` 下（内核树**自带**一个 `tools/`），
    此时 `WS` = `<仓库>/.github`，其下并没有 `tools/<filename>`
    ⇒ 不回退就会「本地永远是对的、到了 CI 才 import 失败」（`PROJECT.md` §7 坑表 #31）。
    """
    ws = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cands = [os.path.join(ws, "tools", filename),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)]
    for p in cands:
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location(name, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise RuntimeError("找不到 %s，试过：\n  %s" % (filename, "\n  ".join(cands)))


# ══════════════════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════════════════
def promote(a, api, log=print):
    """全部行为。返回结果字典（同时是 `--json-out` 与 `--github-output` 的来源）。"""
    a.merge_sha = assert_sha("--merge-sha", a.merge_sha)
    a.base_sha = assert_sha("--base-sha", a.base_sha)
    a.upstream_sha = assert_sha("--upstream-sha", a.upstream_sha)
    a.branch = assert_branch(a.branch)

    # ── ① 仓库与合并提交：先在**本地对象库**里找它 ────────────────────────────
    #    ⚠️ 本工具**故意不检查「检出在哪条分支上」**（第一版检查了，是错的）：
    #    repro job 的 `actions/checkout` 用的是 `ref: ${{ github.sha }}` = **合并提交**
    #    ⇒ 那边永远是 **detached HEAD**。而推分支根本不需要「站在那条分支上」
    #    —— 写的是 `refs/heads/<branch>`（由 `--branch` 声明），不是 HEAD。
    #    真正防呆的是另外几条：`--branch` 的形状守卫、**两个父**、以及推之前读远端 ref。
    if not os.path.isdir(os.path.join(a.repo_dir, ".git")):
        raise Usage("%s 不是一个 git 仓库（没有 .git）" % a.repo_dir)
    log("  检出       %s（HEAD = %s；不要求站在分支上，见注释）"
        % (head_ref(a.repo_dir), git_out(a.repo_dir, "rev-parse", "--short", "HEAD")))
    assert_local_commit(a.repo_dir, a.merge_sha, "合并提交")

    # ── ② 「这次合并确实是那个东西」：两个父各核一遍（验收的落地判据）──────────
    parents = git_out(a.repo_dir, "rev-list", "--parents", "-n", "1", a.merge_sha).split()[1:]
    if len(parents) != 2:
        raise Usage("合并提交 %s 有 %d 个父（应为 2）。\n"
                    "   ⇒ 它不是 `try-merge.py` 造出来的那个合并提交 —— "
                    "别把别的东西推上去。" % (a.merge_sha[:12], len(parents)))
    if parents[0] != a.base_sha:
        raise Usage("合并提交的父[0] 是 %s，而 --base-sha 是 %s —— 对不上。\n"
                    "   ⇒ 一个请求都不发。最可能的原因：\n"
                    "     ① 检测器看到的**本线 HEAD** 与这次构建重放时用的不是同一个；\n"
                    "     ② 合并提交是从别的地方来的（手填？另一条分支？）。"
                    % (parents[0], a.base_sha))
    if parents[1] != a.upstream_sha:
        raise Usage("合并提交的父[1] 是 %s，而 --upstream-sha 是 %s —— 对不上。\n"
                    "   ⇒ 一个请求都不发：那说明这次合并**合的不是那个上游提交**，"
                    "而分支上会留下一条自称「同步上游」的历史。"
                    % (parents[1], a.upstream_sha))
    log("  合并提交   %s" % a.merge_sha)
    log("    父[0] 本线  %s ✅" % parents[0])
    log("    父[1] 上游  %s ✅" % parents[1])

    # ── ③ 时间锚定（验收第 2 条）：从**产物侧**复核一遍，不是信 try-merge 的注释 ──
    #    ⚠️ 上游提交不在本地时**降级为「只记不核」**并明说 —— 它只是核对用的输入
    #    （父[1] 那一条已经证明「合的是那个提交」），不该让一次核对失败拦下一次推进。
    m_date = assert_date("合并提交 %s 的 committer date" % a.merge_sha[:12],
                         git_out(a.repo_dir, "show", "-s", "--format=%cI", a.merge_sha))
    u_date = None
    if has_commit(a.repo_dir, a.upstream_sha):
        u_date = assert_date("上游提交 %s 的 committer date" % a.upstream_sha[:12],
                             git_out(a.repo_dir, "show", "-s", "--format=%cI", a.upstream_sha))
        if m_date != u_date:
            raise Usage("合并提交的时间**没有**锚定到上游提交的日期：\n"
                        "    合并提交 %s\n"
                        "    上游提交 %s\n"
                        "   ⇒ 一个请求都不发。`KBUILD_BUILD_TIMESTAMP` 取自 HEAD 的提交时间，"
                        "日期一飘，「同一个提交两次构建逐字节相同」就不成立。"
                        % (m_date, u_date))
        log("  提交时间   %s（= 上游提交的日期，锚定 ✅）" % m_date)
    else:
        log("  提交时间   %s" % m_date)
        log("    ⚠️ 上游提交 %s 不在本地 ⇒ **时间锚定这一条没核**（只记不核）。"
            % a.upstream_sha[:12])
        log("       它不是推分支的前提：父[1] 那一条已经证明「合的就是那个提交」。")

    # ── ④ 读远端 ref（推之前的**第一道**守卫；第二道是 force=false 本身）──────
    remote = read_ref(api, a.repo, a.branch)
    d = decide(remote, parents[0], a.merge_sha,
               lambda x, y: git(a.repo_dir, "merge-base", "--is-ancestor", x, y,
                                check=False)[0] == 0)
    log("  远端 %s = %s" % (a.branch, remote or "（不存在）"))
    result = {
        "repo": a.repo, "branch": a.branch, "merge_sha": a.merge_sha,
        "base_sha": a.base_sha, "upstream_sha": a.upstream_sha,
        "merge_date": m_date, "upstream_date": u_date,
        "remote_before": remote, "remote_after": None,
        "decision": d.kind, "exit_code": d.exit_code, "reason": d.reason,
        "pushed": False, "dry_run": bool(a.dry_run),
        "run_url": a.run_url, "ref_http_status": None, "files": None,
    }
    if d.kind == "moved":
        log("❌ %s" % d.reason)
        log("   ⇒ **这次不推**（退出码 %d）。分支上一个字节都没动。" % EXIT_REF_MOVED)
        log("     最可能的两种情形：")
        log("       ① 上一轮的合并已经推上去了，而这一轮编的是**更旧**的那个合并")
        log("          （检测器又发现了一次漂移，那时分支已经动了）；")
        log("       ② 有人在构建期间手推了这条分支。")
        log("     两种都不该自动处理：等下一轮检测器按**新的** HEAD 重新试合并。")
        result["exit_code"] = EXIT_REF_MOVED
        # ⚠️ 消息**不带** `d.reason`：上面已经逐字打过一遍了，而 `main()` 的异常处理
        #    还会再打一行 —— 带上的话同一条结论会出现三次（实测踩到）。
        raise RefMoved("分支已前进 ⇒ 本次不推（退出码 %d）" % EXIT_REF_MOVED, result=result)
    if d.kind == "already":
        result["remote_after"] = remote
        log("✅ %s —— 什么都不用做（退出码 0）" % d.reason)
        return result

    if a.dry_run:
        log("⚠️ --dry-run：上面是完整判据，**没有发任何写请求**。")
        log("   将会 PATCH refs/heads/%s → %s（force=false）" % (a.branch, a.merge_sha))
        result["decision"] = "dry_run"
        result["exit_code"] = EXIT_OK
        return result

    # ── ⑤ 唯一的一次写：PATCH ref，**force=false** ⇒ 非快速前进时平台自己拒 ────
    log("  写 refs/heads/%s → %s（force=false ⇒ 非快速前进时平台会拒）" % (a.branch, a.merge_sha))
    st, doc = api.call("PATCH", "repos/%s/git/refs/heads/%s" % (a.repo, qs(a.branch)),
                       {"sha": a.merge_sha, "force": False})
    result["ref_http_status"] = st
    # 🚨 **把平台原文原样带上**（`PROJECT.md` §7 坑表 #45 的教训）：
    #    同一个码 + 两种截然不同的原因 ⇒ 替平台归纳会把人引到错的方向。
    #    这一条端点上 422 **至少**有两种：非快速前进、以及「你指的那个对象不存在」。
    detail = json.dumps(doc, ensure_ascii=False)[:600] if doc else "（平台没有给正文）"
    if st == 422:
        # ⚠️ 这一档**故意**不叫「失败」：最常见的原因就是「另有人在写这条分支」，
        #    而 `force=false` 正是拦住它的那道闸 —— 报成运行错误会让人去查工具。
        #    🚨 但**不许替平台归纳**（坑表 #45）：这一条端点上 422 至少有两种原因，
        #    善后完全相反（「等下一轮」vs「这条路要重新设计」）⇒ 按**原文关键字**分岔，
        #    并**始终**把原文完整打出来。
        low = detail.lower()
        if "fast forward" in low:
            why = ("⇒ 平台说的是**非快速前进** ⇒ 远端在「读」与「写」之间被推走了，"
                   "而且不是这次合并的后代。\n"
                   "   ⚠️ **分支没有被覆盖**（`force: false` 的作用），谁也没丢东西。\n"
                   "   最可能的原因：同一时间另有一轮在推这条分支（检测器刚推完上一轮？）。\n"
                   "   ⇒ 什么都不用做：等下一轮检测器按新的 HEAD 重新试合并。")
        elif "no commit found" in low or "sha" in low and "invalid" in low:
            why = ("⇒ 平台说的是**那个 sha 它不认**（不是并发）。\n"
                   "   本通道里合并提交**不在任何 ref 上**，只存在于本地对象库；\n"
                   "   ⇒ 若平台拒绝接受它，这条路本身要重新设计（#9 的规格要改，不是改参数）。\n"
                   "   ⇒ **别重试**：先看上面的原文，再决定。")
        else:
            why = ("⇒ 平台原文里**没有** `fast forward` 也没有「sha 不认」的字样 ——\n"
                   "   **不要猜**。这一条端点上 422 至少有两种已知原因，善后相反：\n"
                   "     ① 非快速前进（并发写）⇒ 等下一轮；\n"
                   "     ② 服务端不认这个合并提交 ⇒ 这条路要重新设计。\n"
                   "   先按原文去查，别按「最可能」去改。")
        result["exit_code"] = EXIT_RACE
        raise RaceLost("PATCH 被平台拒（HTTP 422）。\n   平台原文：%s\n   %s" % (detail, why),
                       result=result)
    if st == 403:
        result["exit_code"] = EXIT_USAGE
        raise Usage("PATCH 被拒（HTTP 403）。\n"
                    "   平台原文：%s\n"
                    "   ⇒ 最可能的两个原因：\n"
                    "     ① 调用方的 `permissions:` 里**少了 `contents: write`**\n"
                    "        （⚠️ 一旦写了 `permissions:`，没列出的权限一律变 `none`）；\n"
                    "     ② 分支保护规则不允许这个 actor 更新（本项目两个 release / 分支都没设保护）。"
                    % detail)
    if st == 404:
        raise Usage("PATCH 返回 404：仓库 %s 或分支 %s 不存在。\n   平台原文：%s"
                    % (a.repo, a.branch, detail))
    if st not in (200, 201):
        raise RuntimeError("PATCH 返回了意外状态 HTTP %d：%s" % (st, detail))
    result["pushed"] = True
    log("✅ 已推进 %s → %s（HTTP %d）" % (a.branch, a.merge_sha, st))

    # ── ⑥ 推完复核：远端必须**就是**合并提交，并把这次动过的 ref 打出来 ────────
    after = read_ref(api, a.repo, a.branch)
    result["remote_after"] = after
    if after != a.merge_sha:
        raise RuntimeError(
            "推完复核不符：刚写了 %s，读回来却是 %s。\n"
            "   ⇒ 最可能的原因：本工具写了**别的** ref（`--branch` 被拼错？），"
            "而这条分支根本没动。\n"
            "   ⚠️ 这一条是「只有它一条 ref 被动过」这句承诺的唯一证据 —— "
            "没有它，路径拼歪时不会有任何东西报错。" % (a.merge_sha, after))
    log("   ✅ 复核：refs/heads/%s = %s" % (a.branch, after))
    log("   本次运行**写过的 ref 只有这一条**（全工具唯一的写操作就是上面那次 PATCH）。")
    return result


def read_ref(api, repo, branch):
    """读远端分支的 sha；**分支不存在不是错误**（`decide()` 会把它变成一句清楚的话）。"""
    st, doc = api.call("GET", "repos/%s/git/ref/heads/%s" % (repo, qs(branch)))
    if st == 404:
        return None
    if st >= 400:
        raise RuntimeError("读 refs/heads/%s 失败（HTTP %d）：%s"
                           % (branch, st, json.dumps(doc, ensure_ascii=False)[:300]))
    return ((doc or {}).get("object") or {}).get("sha")


def _remote_line(r):
    """远端 ref 那一行的措辞。

    ⚠️ **三种情形不是两种**：`read_ref()` 用 `None` 说「分支不存在（404）」、
    用 sha 说「分支在」、而**键缺失**才说「还没读到」。第一版把前两种都写成
    「未读到——判据没跑完」，把**已经拿到的信息**（分支不存在）丢掉了。
    """
    if "remote_before" not in r:
        return "（未读到 —— 判据没跑到那一步）"
    sha = r.get("remote_before")
    return "`（分支不存在）`" if sha is None else "`%s`" % sha


def render_summary(r, mode="ok"):
    """写进 `$GITHUB_STEP_SUMMARY` 的一段。结论要一眼看得见。

    `mode` = `ok`（判据跑完了）/ `error`（判据没跑完，别把它说成「推失败」）/ `race`。

    ⚠️ **每一条「✅」都必须有对应的事实**（独立审查抓到的一处真问题）：
    第一版拿 `merge_date == upstream_date` 当作「时间锚定 ✅」的判据，
    而**判据没跑完**那条路把两者都写成 `"?"` ⇒ `"?" == "?"` 为真 ⇒
    一份**根本没核过**的摘要上打着「（锚定到上游提交的日期 ✅）」。
    现在改成：两个都**真取到了**才算锚定，否则如实说「未核」。
    """
    dated = bool(r.get("merge_date")) and bool(r.get("upstream_date"))
    anchored = dated and r["merge_date"] == r["upstream_date"]
    L = ["## 推进工作分支（issue #9）", "", "| 项 | 值 |", "|---|---|",
         "| 工作分支 | `%s@%s` |" % (r["repo"], r["branch"]),
         "| 合并提交 | `%s` |" % r["merge_sha"],
         "| 父[0] 本线 | `%s` |" % r["base_sha"],
         "| 父[1] 上游 | `%s` |" % r["upstream_sha"],
         "| 提交时间 | `%s`%s |" % (
             r.get("merge_date") or "（未取到）",
             "（= 上游提交的日期，锚定 ✅）" if anchored
             else "（上游提交的日期未取到 ⇒ **锚定这一条没核**）" if r.get("merge_date")
             else "（判据没跑到这一步）"),
         "| 远端（推之前） | %s |" % _remote_line(r)]
    if r["pushed"]:
        L += ["| 远端（推之后） | `%s` ✅ |" % r["remote_after"],
              "| **结论** | **已推进**（HTTP %s，`force: false`）|" % r["ref_http_status"], "",
              "「工作分支 = 可发布状态」这个不变量由机器守住：**只有整条链全绿**"
              "（哨兵 → 编译 ×2 → 逐字节比对 → 发布闸门）才会走到这一步。", ""]
    elif r["decision"] == "already":
        L += ["| **结论** | 远端已经是这次合并提交 —— **幂等，什么都没做** |", ""]
    elif r["decision"] == "moved":
        L += ["| **结论** | **这次不推**：分支上有别的东西了（退出码 3）|", "",
              "⚠️ 分支**一个字节都没动**，也没有发出任何写请求。",
              "最可能的情形：上一轮的合并已经推上去了，而这一轮编的是更旧的那个合并。",
              "⇒ 等下一轮检测器按**新的** HEAD 重新试合并。", ""]
    elif r["decision"] == "dry_run":
        L += ["| **结论** | `--dry-run`：判据跑完了，**没有发任何写请求** |", ""]
    elif mode == "error":
        L += ["| **结论** | ⚠️ **判据没跑完**（`%s`）—— 分支一个字节都没动 |" % r["decision"], "",
              "⚠️ 这**不是**「推失败」：写请求根本没发出去（前置检查没过）。",
              "原因见 CI 日志里那一句 `用法 / 前置错误：…`。", ""]
    elif mode == "race":
        L += ["| **结论** | ⚠️ **这次不推**：平台按非快速前进拒了（并发写，退出码 4）|", "",
              "⚠️ 分支**没有被覆盖**（`force: false` 的作用），谁也没丢东西。",
              "⇒ 什么都不用做：等下一轮检测器按新的 HEAD 重新试合并。", ""]
    else:
        L += ["| **结论** | %s（退出码 %s）|" % (r["decision"], r["exit_code"]), ""]
    if r.get("run_url"):
        L += ["本次运行：%s" % r["run_url"], ""]
    return "\n".join(L) + "\n"


# ══════════════════════════════════════════════════════════════════════════════
#  自测：临时仓库 + **真 git** + 假 API，完全不联网
#
#  ⚠️ 合并提交由 **`try-merge.py` 本人**造出来（不是手搓的）：
#     于是「两个父对不对」这条判据测的是**产物**，而不是我的夹具。
# ══════════════════════════════════════════════════════════════════════════════
class Args:
    def __init__(self, **kw):
        self.repo = kw.get("repo", "o/r")
        self.branch = kw.get("branch", "work")
        self.repo_dir = kw.get("repo_dir")
        self.merge_sha = kw.get("merge_sha")
        self.base_sha = kw.get("base_sha")
        self.upstream_sha = kw.get("upstream_sha")
        self.dry_run = kw.get("dry_run", False)
        self.run_url = kw.get("run_url", None)


class FakeApi:
    """假 API：**只认实现真正用到的两个端点**，其余一律抛错。

    「多调了一个端点」本身就是实现跑偏的信号，所以这里不给兜底 —— 让它当场炸。
    `writes` 记下每一次**写**请求：自测断言「被写的 ref **逐字**是工作分支那一条」
    以及「该拒绝的时候一次写都没发」。
    ⚠️ `patch_body` 让负向用例能把**平台的原文**放进响应里 ——
    422 有多种原因，而它们**只能靠 message 分辨**（本工单真踩过）。
    """

    def __init__(self, ref=None, patch_status=200, patch_ref=None, patch_body=None):
        self.ref = ref                     # 远端 refs/heads/<branch> 的值（None = 不存在）
        self.patch_status = patch_status
        self.patch_ref = patch_ref         # 若给：PATCH 之后 ref 变成这个值（复核用）
        self.patch_body = patch_body or {}
        self.writes = []                   # 每一次写请求的 (method, path, payload)
        self.calls = []

    def call(self, method, path, payload=None):
        p = path.split("?")[0]
        self.calls.append("%s %s" % (method, p))
        if method == "GET" and "/git/ref/heads/" in p:
            if self.ref is None:
                return 404, {"message": "Not Found"}
            return 200, {"ref": "refs/heads/" + p.rsplit("/", 1)[1], "object": {"sha": self.ref}}
        if method == "PATCH" and "/git/refs/heads/" in p:
            self.writes.append((method, p, payload))
            if self.patch_status in (200, 201):
                self.ref = (payload or {}).get("sha") if self.patch_ref is None else self.patch_ref
                return self.patch_status, {"object": {"sha": self.ref}}
            return self.patch_status, self.patch_body
        raise AssertionError("假 API 不认识这个端点：%s %s（实现跑偏了？）" % (method, path))


def _init_repo(path):
    """造一个小仓库并**用真 try-merge 造出合并提交**，返回 `(merge_sha, local, upstream)`。

    拓扑与真上游同构：`A`（分叉点）→ `work`（我方 2 个提交）／`up`（上游 1 个提交）。
    """
    tm = _load_sibling("tm_for_promote_selftest", "try-merge.py")
    os.makedirs(path, exist_ok=True)

    def g(*args):
        return git(path, *args)

    g("init", "-q", "-b", "work")
    g("config", "user.name", "fixture")
    g("config", "user.email", "fixture@example.invalid")
    g("config", "commit.gpgsign", "false")

    def commit(text, name, msg):
        with open(os.path.join(path, name), "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        g("add", name)
        g("commit", "-q", "-m", msg)

    commit("base\n", "base.txt", "A: 分叉点")
    a = git_out(path, "rev-parse", "HEAD")
    commit("side\n", "side.txt", "W1: 我方的第一个提交")
    commit("more\n", "more.txt", "W2: 我方的第二个提交")
    work = git_out(path, "rev-parse", "HEAD")
    g("checkout", "-q", "-b", "up", a)
    commit("upstream\n", "up.txt", "U1: 上游新提交")
    up = git_out(path, "rev-parse", "HEAD")
    g("checkout", "-q", "work")

    # ★ 合并提交**由 try-merge.py 本人**造（与 CI 上那条路同一个函数）
    r = tm.run_merge(tm.Args(repo_dir=path, upstream_sha=up, merge_base=a,
                             work_branch="work", base_sha=work, checkout=True),
                     log=_quiet)
    if r.get("exit_code") != 0:
        raise RuntimeError("夹具：try-merge 没造出合并提交（%r）" % (r,))
    # ⚠️ 试合并把 HEAD 留在合并提交上（**detached**），而本工具的 CI 现场是
    #    `actions/checkout` 检出的**分支** ⇒ 夹具必须回到那条分支上。
    #    只 `reset --hard work` 是不够的：HEAD 仍是摘下来的，`--abbrev-ref HEAD`
    #    会回 `HEAD` 而不是 `work`（第一版就这么错，于是整轮用例全红在检出守卫上）。
    g("checkout", "-q", "work")
    return r["merge_sha"], work, up


def self_test(tmpdir):
    """离线用例。返回失败条数。"""
    os.makedirs(tmpdir, exist_ok=True)
    bad, n = 0, [0]

    def fail(what, *lines):
        nonlocal bad
        bad += 1
        print("  ❌ #%-2d %s\n       %s" % (n[0], what, "\n       ".join(str(x) for x in lines)))

    def case(what, want_exit, args, api, **checks):
        """跑一次 `promote()`，断言退出码 + 若干条不变量 + **写过哪些 ref**。"""
        n[0] += 1
        want_writes = checks.pop("_writes", None)
        try:
            r = promote(args, api, log=_quiet)
        except (Usage, RefMoved, RaceLost) as e:
            # ⚠️ `RaceLost` 自带一份 result（「没推成」也是结果）；另外两个没有。
            r = getattr(e, "result", None) or {}
            r = dict(r)
            r["exit_code"] = {Usage: EXIT_USAGE, RefMoved: EXIT_REF_MOVED,
                              RaceLost: EXIT_RACE}[type(e)]
            r["error"] = str(e)
        except Exception as e:                       # 崩溃本身就是不合格
            fail(what, "抛异常：%r" % (e,))
            return None
        problems = []
        if r["exit_code"] != want_exit:
            problems.append("期望退出码 %d，实际 %d（%s）"
                            % (want_exit, r["exit_code"], r.get("decision") or r.get("error")))
        for k, v in checks.items():
            if k == "exc":                           # 错误消息里必须出现这个子串
                if v not in (r.get("error") or ""):
                    problems.append("报错里应出现 %r，实际 %r" % (v, r.get("error")))
            elif r.get(k) != v:
                problems.append("%s 期望 %r，实际 %r" % (k, v, r.get(k)))
        # ★ 不变量：被写的 ref **只有**工作分支那一条 —— 而且是**逐字**那一条。
        #   ⚠️ 第一版只断言「至多一条**不同的**路径」，于是 `--branch` 拼歪时
        #   （正是 `assert_branch` 声称要防的那件事）23 条用例**全绿**：
        #   假 API 用 `"/git/refs/heads/" in p` 通配任何分支名，
        #   而推完复核读的又是**同一条**（可能写歪的）分支，会跟着说「✅ 一致」。
        want_path = "repos/%s/git/refs/heads/%s" % (args.repo, args.branch)
        wrong = sorted({p for _m, p, _pl in api.writes if p != want_path})
        if wrong:
            problems.append("写了别的 ref：%s（应当是 %s）" % (wrong, want_path))
        if want_writes is not None and len(api.writes) != want_writes:
            problems.append("写请求次数应为 %d，实际 %d" % (want_writes, len(api.writes)))
        if problems:
            fail(what, *problems)
        else:
            print("  ✅ #%-2d %-46s -> 退出码 %d / 写 %d 次"
                  % (n[0], what[:46], r["exit_code"], len(api.writes)))
        return r, api

    d = os.path.join(tmpdir, "ok")
    msha, work, up = _init_repo(d)
    first_parent = git_out(d, "rev-parse", msha + "^1")

    # ① 正常路径：远端 == 父[0] ⇒ 快速前进
    r1, api1 = case("① 正常：远端 == 父[0] ⇒ 推进", EXIT_OK,
                    Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up),
                    FakeApi(ref=work), decision="push", pushed=True,
                    remote_after=msha, _writes=1)
    n[0] += 1
    w = api1.writes[0][2] if api1.writes else {}
    if w.get("sha") == msha and w.get("force") is False:
        print("  ✅ #%-2d %-46s -> force=false，sha=合并提交"
              % (n[0], "① 附属：请求体形状"))
    else:
        fail("① 附属：请求体必须是 {sha: <merge>, force: false}", w)

    # ② 幂等：远端已经是合并提交 ⇒ 什么都不做（**一次写都不发**）
    case("② 幂等：远端已是合并提交 ⇒ 不写", EXIT_OK,
         Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up),
         FakeApi(ref=msha), decision="already", pushed=False, _writes=0)

    # ③ 分支上有别的东西 ⇒ 让路（退出码 3），**一次写都不发**
    case("③ 分支已前进 ⇒ 让路（3），不发写请求", EXIT_REF_MOVED,
         Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up),
         FakeApi(ref="f" * 40), decision="moved", pushed=False, _writes=0)

    # ④ 并发被抢：读完 ref 之后远端被推走 ⇒ 平台按非快速前进拒（422 ⇒ 退出码 4）。
    #    ⚠️ 这一条正是验收第 4 条（「检测器与构建器不会同时写同一条工作分支」）的落地，
    #    而它**只有真发一次 PATCH 才测得出来** —— 假 API 在这里扮演平台。
    r4, api4 = case("④ 并发被抢 ⇒ 平台拒（422 ⇒ 4），分支没被覆盖", EXIT_RACE,
                    Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up),
                    FakeApi(ref=work, patch_status=422,
                            patch_body={"message": "Update is not a fast forward"}),
                    decision="push", pushed=False, _writes=1,
                    exc="平台原文")
    n[0] += 1
    if r4 and r4.get("exit_code") == EXIT_RACE:
        print("  ✅ #%-2d %-46s -> exit_code=%d（与进程退出码一致）"
              % (n[0], "④ 附属：result 里的 exit_code 不许是 0", r4["exit_code"]))
    else:
        fail("④ 附属：result 里的 exit_code 应当是 4（第一版是 0）",
             (r4 or {}).get("exit_code"))
    n[0] += 1
    if api4 and api4.writes and "Update is not a fast forward" in (
            (r4 or {}).get("error") or ""):
        print("  ✅ #%-2d %-46s -> 原文被原样带出来了" % (n[0], "④ 附属：422 要把平台原文打出来"))
    else:
        fail("④ 附属：422 的报错必须包含平台原文",
             (r4 or {}).get("error"))

    # ④b 422 的**另一种**原因（对象不存在）⇒ 报错要提醒「别重试，先看原文」。
    #     同一个码两种原因、善后相反 —— 这正是「必须把原文打出来」那条教训的落点。
    case("④b 422 说「sha 无效」⇒ 报错要区分，不许一律说「被抢」", EXIT_RACE,
         Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up),
         FakeApi(ref=work, patch_status=422,
                 patch_body={"message": "Invalid request.\n\nNo commit found for SHA"}),
         decision="push", pushed=False, _writes=1, exc="别重试")

    # ⑤ 分支不存在 ⇒ **用法错误**（2），不是「让路」—— 这两件事善后完全不同
    case("⑤ 分支不存在 ⇒ 用法错误（2）", EXIT_USAGE,
         Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up),
         FakeApi(ref=None), _writes=0, exc="没有这条分支")

    # ⑥ 父[1] 不是那个上游提交 ⇒ 一个请求都不发
    case("⑥ 父[1] ≠ --upstream-sha ⇒ 不发请求", EXIT_USAGE,
         Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha="a" * 40),
         FakeApi(ref=work), _writes=0, exc="父[1]")

    # ⑦ 父[0] 不是 --base-sha ⇒ 一个请求都不发
    case("⑦ 父[0] ≠ --base-sha ⇒ 不发请求", EXIT_USAGE,
         Args(repo_dir=d, merge_sha=msha, base_sha="b" * 40, upstream_sha=up),
         FakeApi(ref=work), _writes=0, exc="父[0]")

    # ⑧ detached HEAD 也要能跑 —— **这是 CI 的真实形态**：repro job 的
    #    `actions/checkout` 用 `ref: ${{ github.sha }}`（= 合并提交）⇒ 永远 detached。
    #    （第一版在这里加了「必须检出在 --branch 上」的守卫，于是 CI 上必然失败。）
    git(d, "checkout", "-q", "--detach", msha)
    case("⑧ detached HEAD（CI 的真实形态）⇒ 照常推进", EXIT_OK,
         Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up),
         FakeApi(ref=work), decision="push", pushed=True, _writes=1)
    git(d, "checkout", "-q", "work")

    # ⑨ 分支名形状守卫：`refs/heads/...` 能绕过「相等」判定（与 detect.py 同一个坑）
    for what, br in (("⑨a --branch 带 refs/ 前缀 ⇒ 拒绝", "refs/heads/work"),
                     ("⑨b --branch 含 .. ⇒ 拒绝", "../work")):
        n[0] += 1
        try:
            promote(Args(repo_dir=d, branch=br, merge_sha=msha, base_sha=work,
                         upstream_sha=up), FakeApi(ref=work), log=_quiet)
            fail(what, "没拒绝")
        except Usage as e:
            print("  ✅ #%-2d %-46s -> %s" % (n[0], what, str(e).split("\n")[0][:40]))

    # ⑩ --dry-run：判据跑完，但**一次写都不发**
    case("⑩ --dry-run ⇒ 判据跑完、不写", EXIT_OK,
         Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up, dry_run=True),
         FakeApi(ref=work), decision="dry_run", pushed=False, dry_run=True, _writes=0)

    # ⑪ 缩写 sha ⇒ 拒绝（形状守卫）
    n[0] += 1
    try:
        promote(Args(repo_dir=d, merge_sha=msha[:12], base_sha=work, upstream_sha=up),
                FakeApi(ref=work), log=_quiet)
        fail("⑪ 缩写 sha ⇒ 拒绝", "没拒绝")
    except Usage as e:
        print("  ✅ #%-2d %-46s -> %s" % (n[0], "⑪ 缩写 --merge-sha ⇒ 拒绝",
                                        str(e).split("\n")[0][:40]))

    # ⑫ 合并提交本地没有 ⇒ 明确报「取不到它、去重放那次合并」，**不是**崩在 git 上
    case("⑫ 合并提交不在本地 ⇒ 用法错误，不写", EXIT_USAGE,
         Args(repo_dir=d, merge_sha="c" * 40, base_sha=work, upstream_sha=up),
         FakeApi(ref=work), _writes=0, exc="不在本地对象库里")

    # ⑫b 上游提交不在本地 ⇒ 时间锚定**降级为「只记不核」**、仍可推进。
    #     ⚠️ 为什么只测渲染那一层：真造这个状态是**造不出来**的 —— JSON 提交一旦
    #     能 `rev-list --parents`，它的两个父就都在本地（git 不让你写出缺父的提交）。
    #     所以这一条只钉住「`upstream_date` 缺失时摘要怎么说」，而不是假装能构造它。
    n[0] += 1
    txt = render_summary({"repo": "o/r", "branch": "work", "merge_sha": "m" * 40,
                          "base_sha": "b" * 40, "upstream_sha": "u" * 40,
                          "merge_date": "2026-09-27T03:22:34+08:00", "upstream_date": None,
                          "remote_before": "l" * 40, "remote_after": "m" * 40,
                          "decision": "push", "exit_code": 0, "reason": "", "pushed": True,
                          "dry_run": False, "run_url": None, "ref_http_status": 200})
    if "上游提交的日期未取到" in txt and "已推进" in txt:
        print("  ✅ #%-2d %-46s -> 降级说明与结论都在" % (n[0], "⑫b 锚定核不了 ⇒ 摘要明说"))
    else:
        fail("⑫b 摘要应明说「上游提交的日期未取到」", txt)

    # ⑫c **判据没跑完**时的摘要不许打任何 ✅（独立审查抓到的一处真问题）：
    #     第一版把 merge_date / upstream_date 都写死成 `"?"` ⇒ `"?" == "?"` 为真 ⇒
    #     一份**根本没核过**的摘要上写着「锚定到上游提交的日期 ✅」。
    #     夹具直接喂 `_fallback_result()` 的产物，正是那条路径真实的形状。
    n[0] += 1
    fa = Args(repo_dir=d, merge_sha=msha, base_sha=work, upstream_sha=up)
    for what, fallback in (("Usage", _fallback_result(fa, EXIT_USAGE)),
                           ("运行错误", _fallback_result(fa, EXIT_RUN))):
        txt = render_summary(fallback, mode="error")
        bad_bits = [b for b in ("锚定 ✅", "✅ **已推进**", "上游提交的日期") if b in txt]
        if "✅" in txt or "锚定 ✅" in txt:
            fail("⑫c %s 的摘要不该出现 ✅" % what, [l for l in txt.splitlines() if "✅" in l])
        elif "未取到" not in txt:
            fail("⑫c %s 的摘要该明说时间未取到" % what, bad_bits)
        else:
            print("  ✅ #%-2d %-46s -> 摘要里没有 ✅，如实说「未取到」"
                  % (n[0], "⑫c %s 路径的摘要不说谎" % what))
        n[0] += 1
    # ⑫d `exit_code` 必须与**进程**一致：第一版在运行错误那条路上硬编码成 2。
    n[0] += 1
    codes = (_fallback_result(fa, EXIT_USAGE)["exit_code"],
             _fallback_result(fa, EXIT_RUN)["exit_code"])
    if codes == (EXIT_USAGE, EXIT_RUN):
        n[0] -= 1
        print("  ✅ #%-2d %-46s -> %s" % (n[0], "⑫d 两条出错路径的 exit_code", codes))
    else:
        fail("⑫d 出错路径的 exit_code 应是 (2, 1)", codes)

    # ⑬ 决定性：同一对输入算出来的父[0] 逐字等于本线 HEAD（`setlocalversion` 靠它）
    n[0] += 1
    if first_parent == work:
        print("  ✅ #%-2d %-46s -> %s（= 本线 HEAD）"
              % (n[0], "⑬ 合并提交的父[0] = 本线 HEAD", first_parent[:12]))
    else:
        fail("⑬ 合并提交的父[0] 应等于本线 HEAD", "%s vs %s" % (first_parent[:12], work[:12]))

    # ⑭ `decide()` 是纯函数 ⇒ 三种情形都能离线穷举（同 try-merge 的 parse_merge_tree）
    for what, remote, anc, want in (
            ("⑭a 远端 == 父[0] ⇒ push", "p" * 40, False, "push"),
            ("⑭b 远端 == merge ⇒ already", "m" * 40, False, "already"),
            ("⑭c 远端是 merge 的祖先 ⇒ push", "x" * 40, True, "push"),
            ("⑭d 远端两者都不是 ⇒ moved", "y" * 40, False, "moved")):
        n[0] += 1
        got = decide(remote, "p" * 40, "m" * 40, lambda _a, _b, v=anc: v).kind
        if got == want:
            print("  ✅ #%-2d %-46s -> %s" % (n[0], what[:46], got))
        else:
            fail(what, "期望 %s，实际 %s" % (want, got))

    # ⑮ 日期形状守卫：不带时区偏移的时间会让 `KBUILD_BUILD_TIMESTAMP` 随 TZ 变
    for what, val, want_ok in (("⑮a 带 +08:00 偏移 ⇒ 接受", "2026-09-27T03:22:34+08:00", True),
                               ("⑮b 带 Z ⇒ 接受", "2026-09-26T19:22:34Z", True),
                               ("⑮c 不带偏移 ⇒ 拒绝", "2026-09-27 03:22:34", False)):
        n[0] += 1
        try:
            assert_date("夹具", val)
            ok = True
        except Usage:
            ok = False
        if ok == want_ok:
            print("  ✅ #%-2d %-46s -> %s" % (n[0], what[:46], "接受" if ok else "拒绝"))
        else:
            fail(what, "期望 %s" % ("接受" if want_ok else "拒绝"))

    total = n[0]
    print()
    print("结果: %s（%d/%d 通过）" % ("失败" if bad else "全部通过", total - bad, total))
    return bad


def _quiet(*_a, **_kw):
    pass


def _fallback_result(a, exit_code):
    """出错路径上的**薄结果**：够 `render_summary()` 与 `--json-out` 用。

    ⚠️ 为什么不干脆不写摘要：这条通道的摘要**就是**人唯一会看的东西
    （`detect.yml` 的同款理由）。一次「没推成」的运行如果摘要空白，
    读的人只会看到「红的/绿的」而不知道**为什么**。

    ⚠️ **`remote_before` 这个键在这里故意不出现**：那表示「还没读到」，
    与「读到了、分支不存在」（值是 `None`）是两件事（见 `_remote_line`）。
    ⚠️ `exit_code` 由调用方给：第一版硬编码成 `EXIT_USAGE`，
    于是「工具崩了」（1）在机器可读出口里写成「用法错误」（2）。
    """
    return {"repo": a.repo, "branch": a.branch, "merge_sha": a.merge_sha,
            "base_sha": a.base_sha, "upstream_sha": a.upstream_sha,
            "merge_date": None, "upstream_date": None,
            "remote_after": None, "decision": None, "exit_code": exit_code,
            "reason": "", "pushed": False,
            "dry_run": bool(a.dry_run), "run_url": a.run_url,
            "ref_http_status": None}


def _emit(a, r, mode="ok"):
    """把结果落到 `--json-out` / `--summary`（两条路共用一份形状）。

    ⚠️ **没有 `--github-output`**：那是「只写不读」——`promote.py` 的 outputs
    在两个 workflow 里都没有消费者（独立审查抓到）。本仓库对「只写不读」点过两次，
    所以这一版直接不提供它；`promote.json`（`--json-out`）是给人看的诊断材料。

    `mode` 只影响摘要里结论行怎么措辞：`error` 那一档要说清「**不是**推失败，
    是判据没跑完」—— 两者善后完全不同（一个去查前置条件，一个去查平台/并发）。
    """
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(r, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    if a.summary:
        with open(a.summary, "a", encoding="utf-8", newline="\n") as f:
            f.write(render_summary(r, mode=mode))


def main(argv):
    # 编码已由**模块级**那次 reconfigure 处理（见文件头附近那段注释）——
    # 这里**不再**设 `line_buffering=True`：本文件不派生子进程抢 fd 1
    # （所有 git 调用走 `run()`，它们的 stdout/stderr 被重定向到临时文件），
    # 而模块级已经设过一次了。⚠️ 第一版在这里又设了一次、理由写的还是「子进程写 fd 1」，
    # 与模块级那句注释**互相矛盾**，两句里必有一句在骗读的人（独立审查抓到）。
    ap = argparse.ArgumentParser(
        add_help=True, description="全绿才推分支：把那次试合并推进工作分支（见文件头）",
        epilog="退出码：0 已推进（或幂等）/ 3 分支已前进（这次不推）/ 4 并发被抢 / "
               "1 运行错误 / 2 用法或前置错误")
    ap.add_argument("--repo", metavar="OWNER/REPO", help="本线工作仓库")
    ap.add_argument("--branch", metavar="分支", help="**工作分支**（全工具唯一会写的那条 ref）")
    ap.add_argument("--repo-dir", default=".", help="本地检出的仓库（默认当前目录）")
    ap.add_argument("--merge-sha", metavar="SHA", help="要推上去的合并提交（40 位）")
    ap.add_argument("--base-sha", metavar="SHA",
                    help="试合并时的本线 HEAD —— 必须等于合并提交的**父[0]**")
    ap.add_argument("--upstream-sha", metavar="SHA",
                    help="试合并合进来的上游提交 —— 必须等于合并提交的**父[1]**")
    ap.add_argument("--run-url", metavar="URL", help="写进运行摘要（事后追溯）")
    ap.add_argument("--dry-run", action="store_true",
                    help="跑完全部判据但**不发写请求**（本机只读演练）")
    ap.add_argument("--json-out", metavar="文件", help="把结果写一份 JSON（诊断材料）")
    ap.add_argument("--summary", metavar="文件", help="追加一段 Markdown（喂 $GITHUB_STEP_SUMMARY）")
    ap.add_argument("--self-test", action="store_true", help="离线跑内置用例（不联网）")
    ap.add_argument("--self-test-dir", metavar="目录")
    a = ap.parse_args(argv[1:])

    if a.self_test:
        d = a.self_test_dir or os.path.join(tempfile.gettempdir(), "promote-selftest")
        if os.path.isdir(d) and not a.self_test_dir:
            shutil.rmtree(d, ignore_errors=True)
        return 1 if self_test(d) else 0

    missing = [n for n in ("repo", "branch", "merge_sha", "base_sha", "upstream_sha")
               if not getattr(a, n)]
    if missing:
        print("缺必填参数: %s" % " / ".join("--" + m.replace("_", "-") for m in missing))
        return EXIT_USAGE

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token and not a.dry_run:
        # ⚠️ 没有凭据时**直接失败**，别只 warn 一句再发请求：未认证读私有 ref 会 404，
        #    而 404 在本工具里被解释成「分支不存在」—— 一条**看着很确定、其实完全不对**的结论。
        print("❌ 环境里没有 GH_TOKEN / GITHUB_TOKEN —— 推分支需要凭据"
              "（CI 里传的是 `${{ github.token }}`；本项目不用 PAT）。")
        print("    只想知道判据就加 --dry-run（读 ref 也会走认证，避免 60 次/小时的限流）。")
        return EXIT_USAGE

    api = Api(token)
    try:
        r = promote(a, api, log=print)
    except Usage as e:
        print("❌ 用法 / 前置错误：%s" % e)
        _emit(a, _fallback_result(a, EXIT_USAGE), "error")
        return EXIT_USAGE
    except RefMoved as e:
        print("⚠️ %s" % e)
        _emit(a, e.result or _fallback_result(a, EXIT_REF_MOVED), "moved")
        return EXIT_REF_MOVED
    except RaceLost as e:
        print("⚠️ %s" % e)
        _emit(a, e.result or _fallback_result(a, EXIT_RACE), "race")
        return EXIT_RACE
    except Exception as e:
        # ⚠️ 非预期异常**连栈一起打**：这一层本来是给「git 失败 / 网络失败」用的，
        #    真出了编程错误时，只留一句消息会让排障从十分钟变成一小时。
        import traceback
        traceback.print_exc()
        print("❌ 运行错误：%s" % _redact(e))
        _emit(a, _fallback_result(a, EXIT_RUN), "error")
        return EXIT_RUN

    print()
    _emit(a, r, "ok")
    return r["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
