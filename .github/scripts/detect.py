#!/usr/bin/env python3
"""检测器（issue #7）：**漂移发现 + 心跳**。只判定，**不构建**。

对应规格 `docs/los-line/upstream-sync-spec.md`：

| 规格 | 本文怎么落地 |
|---|---|
| 实现决定 1（两个 workflow 职责分开） | 本 workflow 是**唯一**带 `schedule` 的那个 ⇒ 只有它会被平台的 inactivity 规则停用；构建器只带 `workflow_dispatch`，永远点得动 |
| 实现决定 2（直接比上游，判据是 merge-base） | 取上游 HEAD / 本地 HEAD / `compare` 的 `merge_base_commit.sha` 三个值 |
| 实现决定 3（心跳：孤儿分支 + **带真实内容**的提交 + 距上次 >= 30 天） | 见下「两条不变量」 |
| 实现决定 7（失败标记与心跳共用一个状态文件） | 状态文件就是 `sync-check.py` 的 `--marker` |

## 判定不在这里

判定核心是接缝 **S3**：`tools/sync-check.py`（纯决策、零 I/O、零网络，28 条离线用例）。
本文件是它的**编排侧** —— 把四个输入从网上取回来（上游 HEAD、本地 HEAD、merge-base、状态文件），
调它，再按它的结论决定要不要写心跳。**判定逻辑一行都不在这里**（重复一份判定，
就是给自己造第二个「谁说了算」）。

⚠️ **退出码沿用 S3 的那一套，不是「0 成功 / 非 0 失败」**：

| 结论 | 退出码 |
|---|---|
| `in-sync`（无漂移） | 0 |
| `drifted`（有漂移） | 10 |
| `blacklisted`（该上游提交已试过且失败） | 20 |
| `heartbeat-due`（心跳到期） | 30 |
| 运行错误（网络 / API / 写失败） | 1 |
| 用法错误 | 2 |

前四个**都是正常结论**，调用方不许写成「非零即报错」—— 那样一次正常的「有漂移」
会被当成工具崩了。

## 两条不变量

**① 只往心跳分支写，绝不碰工作分支。**
三层，一层比一层硬：`--heartbeat-branch` 与 `--branch` 相同、或落在 `ksu-` / `lineage-`
这两个命名空间里（那是**工作分支**与**上游镜像分支**的名字）⇒ **联网之前就拒绝**；
写完之后**再读一次工作分支 HEAD**，与运行开始时读到的不一样就报错；
自测里还有一条断言：整个自测过程中被写过的 ref **只有** `refs/heads/<心跳分支>`。

**② 心跳提交必须带真实内容，且落在一条真正的孤儿分支上。**
「内容确有变化」是**结构性**的，不靠检查：写心跳的前提是旧的 `heartbeat.at` 至少 **30 天**前，
而新内容的 `at` 是今天 ⇒ 两个字段必然不同。内容里另带 `at_utc`（秒级）与 `run`（本次运行的 URL），
那是给**事后追溯**用的（哪一次运行写的心跳），顺带让同一天里写两次也不会是同一条内容。
「孤儿」则有**写后复核**：读心跳分支的顶层树，必须是「只有状态文件」——
从工作分支切出来的分支顶层是 `Makefile` / `arch` / `drivers`…，一眼可辨。

> ⚠️ 第一版这里写的是一句 `if new_text == old_text: raise` —— 它**到不了**
> （见上：`at` 必然不同），一条永远不会响的检查只会给假的安全感，已删。
> 真正会破坏这条验收的是「30 天窗口被改小」，而那件事挡在 `sync-check.py` 的
> `--heartbeat-days >= 1` 那行上。

## 状态文件（心跳分支上唯一的东西）

```json
{
  "failed": null,
  "heartbeat": {"at": "2026-09-26", "at_utc": "2026-09-26T12:34:56Z",
                "upstream": "<40 位 sha>", "run": "<本次运行的 URL>"}
}
```

`failed` 是 **#10 的失败标记**（`sync-check.py` 的 `--marker` 读它），本工具**只保留、不写**；
`heartbeat` 由本工具写。两个字段共用一个文件是规格实现决定 7 定的。

## 为什么心跳走 API 而不是 `git push`

检测器只写**一个文件**，且**必须**写在一条与工作分支无共同历史的**孤儿分支**上。
Git Data API 表达这件事比 git 更直接：`parents: []` 就是孤儿提交（`git checkout --orphan`
要先清索引、再提防把工作树带过去）。附带两个好处：状态文件的读改写天然是 **CAS**
（Contents API 要 `sha`）⇒ 与 #10 的失败标记并发写会**响亮地 409**，而不是互相覆盖；
以及整个工具在 Windows 本机也能跑（本项目没有可用的 bash，见 `PROJECT.md` §6.5）。

## 用法

```sh
# 本机只读实跑（不写任何东西；读接口是公开的，不需要凭据）
python tools/detect.py --repo Bonger34/android_kernel_xiaomi_sm8250 \
    --branch ksu-lineage-23.2 --dry-run

# CI：写心跳（凭据从 GH_TOKEN / GITHUB_TOKEN 环境变量来）
python .github/scripts/detect.py --repo "$GITHUB_REPOSITORY" --branch ksu-lineage-23.2 \
    --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
    --summary "$GITHUB_STEP_SUMMARY" --github-output "$GITHUB_OUTPUT"

python tools/detect.py --self-test       # 离线，假 API，16 条断言（不联网）
```

⚠️ **本文件有两份，必须逐字节相同**：工作区 `tools/detect.py` 与 LOS 工作仓库里的
`.github/scripts/detect.py`（CI 跑后者 —— 内核树**自带**一个 `tools/`，那是上游的，
项目自己的 CI 工具一律放 `.github/`，同 `repack-boot.py`）。镜像与复验：
`python tools/sync-ci-scripts.py --gen` / `--check`。
"""

import argparse
import base64
import datetime
import importlib.util
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import NamedTuple

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_DEFAULT = "https://api.github.com"
UA = "umi-loskernel-detect/1"
TIMEOUT = 60
TRIES = 3

EXIT_RUN_ERROR = 1
EXIT_USAGE = 2

# 心跳分支与状态文件的名字。**两处常量而不是散在各处的字面量** —— 自测与 workflow
# 都引用它们，写歪一个就会变成「心跳写到了别处」。
HEARTBEAT_BRANCH = "sync-heartbeat"
STATE_PATH = "sync-state.json"
HEARTBEAT_MSG = "chore(sync): 心跳 %s（防 GitHub 的 60 天无活动规则停用检测器）"

# 心跳分支不许落在这两个命名空间里：`ksu-*` 是本线的工作分支，`lineage-*` 是上游镜像分支。
FORBIDDEN_PREFIXES = ("ksu-", "lineage-")


def qs(x):
    """查询串里的值（分支名、ref）。斜杠也转义 —— 它在查询里没有语义。"""
    return urllib.parse.quote(x, safe="")


def qp(x):
    """路径里的片段（文件路径）。保留斜杠（仓库内路径本来就有斜杠）。"""
    return urllib.parse.quote(x, safe="/")


def _load_sibling(name, filename):
    """加载同目录（或工作区 `tools/`）下的另一件工具。

    为什么要回退：CI 里这些脚本跑在 `.github/scripts/` 下（内核树**自带**一个 `tools/`），
    此时 `WS` = `<仓库>/.github`，其下并没有 `tools/sync-check.py`
    ⇒ 不回退就会「本地永远是对的、到了 CI 才 import 失败」（`PROJECT.md` §7 坑表 #31）。
    """
    cands = [os.path.join(WS, "tools", filename),
             os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)]
    for p in cands:
        if os.path.exists(p):
            spec = importlib.util.spec_from_file_location(name, p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise SystemExit("找不到 %s，试过：\n  %s" % (filename, "\n  ".join(cands)))


class ApiError(RuntimeError):
    """一次 API 调用失败。

    `retryable` 区分「等一等可能会好」（网络抖动 / 5xx）与「重试多少次都一样」（4xx）——
    对 4xx 重试只会把一次清楚的失败拖成三次一样的失败。
    """

    def __init__(self, message, retryable=False):
        RuntimeError.__init__(self, message)
        self.retryable = retryable


class UsageError(RuntimeError):
    """用法错误（退出码 2），与运行错误（1）分开 —— 两者善后完全不同。"""


class Api:
    """GitHub REST 的最小客户端。只用标准库（`urllib` 走 OpenSSL，本机与 CI 都能跑）。

    ⚠️ 凭据只从环境变量来（`GH_TOKEN` / `GITHUB_TOKEN`）：`PROJECT.md` §5.3 的规矩是
    **任何脚本都不得硬编码 PAT**。读接口是公开的，所以 `--dry-run` 下没有凭据也能跑。
    """

    def __init__(self, base=API_DEFAULT, token=None, timeout=TIMEOUT, tries=TRIES):
        self.base = base.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.tries = tries
        self.calls = []                 # 自测会读它 —— 「到底碰了哪些端点」要有据可查

    def call(self, method, path, payload=None, allow_404=False):
        """返回解析后的 JSON；`allow_404` 时返回 `None`（**「明确说没有」是正常状态**：
        心跳分支首次运行时就是不存在，那不是错误）。"""
        last = None
        for i in range(self.tries):
            try:
                self.calls.append("%s %s" % (method, path.split("?")[0]))
                return self._one(method, path, payload, allow_404)
            except ApiError as e:
                if not e.retryable:
                    raise
                last = e
            if i + 1 < self.tries:
                time.sleep(2 * (i + 1))
        raise last

    def _one(self, method, path, payload, allow_404):
        url = "%s/%s" % (self.base, path.lstrip("/"))
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
        if data is not None:
            req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 and allow_404:
                return None
            detail = e.read().decode("utf-8", "replace")[:400]
            raise ApiError("%s %s -> HTTP %d\n    %s" % (method, url, e.code, detail),
                           retryable=(e.code >= 500))
        except Exception as e:                       # 网络层（超时、DNS、TLS…）
            raise ApiError("%s %s -> %r" % (method, url, e), retryable=True)
        return json.loads(body.decode("utf-8")) if body.strip() else {}


# ── 心跳内容：**纯函数**，所以「写什么」这件事可以离线断言 ────────────────────────
class Heartbeat(NamedTuple):
    """`new_state()` 的返回值。`note` 是给人看的一句话（保留了哪个失败标记、丢了什么）。"""

    doc: dict
    note: str


def new_state(old_text, today, now_utc, upstream, run_url):
    """把心跳写进状态文件，**不碰 `failed`**。

    ⚠️ **保留 `failed` 是硬要求**：那是「这个上游提交已试过且失败」的唯一载体（#10 写它，
    `sync-check.py` 读它）。心跳若把它抹掉，被拉黑的提交会在下一次心跳后**静默复活**
    —— 一天一次的重试又回来了，而每轮都是 15 分钟 × 2 的编译。

    但「保留」只对**能读懂的** `failed` 成立：拿不出可信的 `{"upstream", "at"}` 就丢掉它，
    并把这件事说出来。理由与 `sync-check.py` 同源 —— 一个写坏的字段不该永久卡住整条通道，
    而写坏的东西留在文件里，下一轮还会是坏的（丢掉 = 自愈）。
    """
    st, note = {}, "状态文件不存在（首次运行，正常）"
    old = None
    if old_text:
        try:
            old = json.loads(old_text)
        except ValueError:
            note = "旧状态文件不是合法 JSON ⇒ 整体重写（自愈）"
    # `failed` 这个键**总是**出现（没有就是 null）：文件的形状稳定，读的人一眼看得出
    # 「这个字段在这里、现在是空的」；形状随内容变的文件，diff 起来全是噪音。
    st["failed"] = None
    if isinstance(old, dict):
        f = old.get("failed")
        if isinstance(f, dict) and f.get("upstream") and f.get("at"):
            st["failed"] = {"upstream": str(f["upstream"]), "at": str(f["at"])}
            note = "保留了失败标记 %s（%s）" % (str(f["upstream"])[:12], f["at"])
        elif f is not None:
            note = "旧状态文件的 `failed` 读不出（缺 `upstream` 或 `at`）⇒ 丢弃它（自愈）"
        elif old.get("heartbeat"):
            note = "只更新心跳（旧文件里没有失败标记）"
    hb = {"at": today, "at_utc": now_utc, "upstream": upstream}
    if run_url:
        hb["run"] = run_url
    st["heartbeat"] = hb
    return Heartbeat(st, note)


def state_text(doc):
    """状态文件的字节形态。**排序 + 固定缩进 + 末尾换行** ⇒ 同样的输入给出同样的字节
    （「内容确有变化」这条断言要拿两次的字节比，格式不稳定就比不出结论）。"""
    return json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def check_branch_guard(working, heartbeat):
    """不变量 ① 的落地：**心跳分支不许是工作分支**（也不许落进那两个命名空间）。"""
    if not heartbeat or heartbeat.strip() != heartbeat or " " in heartbeat:
        raise UsageError("--heartbeat-branch 不合法：%r" % heartbeat)
    if heartbeat == working:
        raise UsageError("心跳分支不能等于工作分支（%s）—— 心跳是**孤儿分支**，"
                         "绝不向工作分支写入是这条通道的硬约束" % working)
    if heartbeat.startswith(FORBIDDEN_PREFIXES):
        raise UsageError("心跳分支 %r 落在 %s 命名空间里 —— 那是工作分支 / 上游镜像分支的名字，"
                         "换个名字（默认 %s）" % (heartbeat, "/".join(FORBIDDEN_PREFIXES),
                                                  HEARTBEAT_BRANCH))


def write_state(api, repo, branch, path, text, file_sha, branch_sha):
    """把状态文件写到心跳分支上。返回这次走的是哪条路（`contents` / `orphan` / `commit`）。

    三条路各自对应一种真实状态，**不是三个分支的同义反复**：

    | 情形 | 走法 |
    |---|---|
    | 分支在、文件也在 | Contents API：`sha` 就是 **CAS** —— 并发写会 409，而不是互相覆盖 |
    | 分支不在（首次运行） | blob → tree（**无 `base_tree`**）→ commit（**`parents: []`**）→ 建 ref ⇒ **孤儿提交** |
    | 分支在、文件不在 | 同上，但带 `base_tree` 与父提交，再 `PATCH` ref |

    ⚠️ 第三条不是假想：状态文件被手工删掉、或将来 #10 换了文件名，都会落到它上面。
    只写前两条的实现会在那里撞一个 422「Reference already exists」—— 一句与实际原因无关的报错。
    """
    if file_sha:
        api.call("PUT", "repos/%s/contents/%s" % (repo, qp(path)),
                 {"message": HEARTBEAT_MSG % _date_of(text), "content": _b64(text),
                  "sha": file_sha, "branch": branch})
        return "contents"
    blob = api.call("POST", "repos/%s/git/blobs" % repo, {"content": text, "encoding": "utf-8"})
    tree = {"tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]}]}
    if branch_sha:
        base = api.call("GET", "repos/%s/git/commits/%s" % (repo, branch_sha))
        tree["base_tree"] = base["tree"]["sha"]
    t = api.call("POST", "repos/%s/git/trees" % repo, tree)
    c = api.call("POST", "repos/%s/git/commits" % repo,
                 {"message": HEARTBEAT_MSG % _date_of(text), "tree": t["sha"],
                  "parents": [branch_sha] if branch_sha else []})
    if branch_sha:
        api.call("PATCH", "repos/%s/git/refs/heads/%s" % (repo, qs(branch)),
                 {"sha": c["sha"], "force": False})
        return "commit"
    api.call("POST", "repos/%s/git/refs" % repo,
             {"ref": "refs/heads/" + branch, "sha": c["sha"]})
    return "orphan"


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def verify(a, api, local_sha, log):
    """写完之后的两条复核。**两条都能真的失败** —— 它们是回归闸，不是仪式。

    一次心跳（约 30 天一次）多两次 API 调用，换来两条验收从此**每次运行都被查一遍**：

    | 查什么 | 怎么查 | 失败说明什么 |
    |---|---|---|
    | 工作分支没被动过 | 再读一次它的 HEAD，与运行开始时读到的比 | 有东西写了它 —— 而「不向任何工作分支写入」是这条通道的硬约束 |
    | 心跳分支是**孤儿**分支 | 读它的**顶层树**，看是不是只有状态文件 | 它多半是从工作分支上切出来的 —— 那种分支带着整棵内核树，也就不再是「不碰工作分支」 |

    ⚠️ 第二条为什么不用「走一遍父提交看有没有共同的根」：那是 N 次调用，而**孤儿分支的顶层
    只有一个文件**是它的充分特征（从工作分支切出来的分支顶层是 `Makefile` / `arch` / `drivers`…）。
    """
    got = api.call("GET", "repos/%s/commits/%s" % (a.repo, qs(a.branch)))["sha"]
    if got != local_sha:
        raise RuntimeError(
            "工作分支 %s 被动过了：运行开始时是 %s，现在是 %s。\n"
            "    检测器只许写 %s —— 见文件头的不变量 ①。"
            % (a.branch, local_sha[:12], got[:12], a.heartbeat_branch))
    tree = api.call("GET", "repos/%s/git/trees/%s" % (a.repo, qs(a.heartbeat_branch)))
    names = sorted(e.get("path") for e in (tree.get("tree") or []))
    if names != [a.state_path]:
        raise RuntimeError(
            "心跳分支 %s 的顶层不是「只有 %s」，而是 %s（共 %d 项）。\n"
            "    它多半是从工作分支上切出来的 —— 那样它带着整棵内核树，就不再是**孤儿**分支了。"
            % (a.heartbeat_branch, a.state_path, names[:6], len(names)))
    log("✅ 复核：工作分支一个字节没动（%s）；心跳分支是孤儿（顶层只有 %s）"
        % (local_sha[:12], a.state_path))


def _date_of(text):
    """从状态文件内容里取回日期，只用于提交信息（**不再算一次「今天」** —— 那样两处可能不一致）。"""
    try:
        return json.loads(text)["heartbeat"]["at"]
    except Exception:
        return "?"


def run(a, api, log=print, now_utc=None):
    """一次检测。返回结果字典（它同时是 `--json-out` 的内容与 `--github-output` 的来源）。"""
    check_branch_guard(a.branch, a.heartbeat_branch)
    sc = _load_sibling("sc", "sync-check.py")

    # ── ① 三个 sha。判据是 **merge-base**，不是「上游 sha == 本地 sha」──────────────
    # 我们的跟随分支**永远**带着自己的提交（KSU 集成 + CI + 修复），所以它在正常情况下
    # 也永远不等于上游 HEAD。「有没有漂移」问的是「上游那个 HEAD 在不在我们的历史里」。
    up_ref = a.upstream_sha or a.upstream_branch
    up = api.call("GET", "repos/%s/commits/%s" % (a.upstream_repo, qs(up_ref)))
    lo = api.call("GET", "repos/%s/commits/%s" % (a.repo, qs(a.branch)))
    cmp = api.call("GET", "repos/%s/compare/%s...%s" % (a.repo, up["sha"], lo["sha"]))
    subject = ((up.get("commit") or {}).get("message") or "").split("\n")[0]

    # ── ② 状态文件（心跳 + 失败标记）。**分支不在 = 没有标记**，不是错误 ──────────────
    ref = api.call("GET", "repos/%s/git/ref/heads/%s" % (a.repo, qs(a.heartbeat_branch)),
                   allow_404=True)
    branch_sha = ((ref or {}).get("object") or {}).get("sha")
    file_sha, old_text = None, None
    if branch_sha:
        doc = api.call("GET", "repos/%s/contents/%s?ref=%s"
                       % (a.repo, qp(a.state_path), qs(a.heartbeat_branch)), allow_404=True)
        if doc:
            file_sha = doc.get("sha")
            old_text = base64.b64decode(doc.get("content") or "").decode("utf-8")

    # **落成文件再交给 S3** —— `sync-check.py` 收的是路径（它是从心跳分支上取一个文件，
    # 多一次「读出内容再当参数传」只会多一个可能出错的环节，见 tickets.md 的遗留说明）。
    # ⚠️ 没有内容时**也要把路径交出去**（并确认它真的不在）：交给它 `None` 会让输出写成
    #    「未传 --marker」，而实情是「分支上还没有这个文件」—— 两句话对读日志的人意思完全不同。
    #    确认删除是必要的：上一轮本机跑剩下的同名文件会被当成真的标记读进去。
    marker_path = os.path.join(a.work_dir, "sync-detect-marker.json")
    if old_text is not None:
        with open(marker_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(old_text)
        log("心跳分支 %s 上的状态文件 %s 已取到（%d 字节）"
            % (a.heartbeat_branch, a.state_path, len(old_text)))
    else:
        if os.path.exists(marker_path):
            os.remove(marker_path)
        log("心跳分支 %s 上还没有状态文件（首次运行正常）" % a.heartbeat_branch)

    # ── ③ 判定。**全部判据都在 S3 里**，这里一行都不重复 ─────────────────────────
    today = datetime.date.fromisoformat(a.today) if a.today else datetime.datetime.utcnow().date()
    oc = sc.check(up["sha"], lo["sha"], cmp["merge_base_commit"]["sha"], marker_path, today)

    # ── ④ 心跳：**到期就写，与结论正交** ────────────────────────────────────────
    # 为什么不做成「只在 in-sync 时写」：心跳要防的是「仓库 60 天无活动 ⇒ 检测器被停用」。
    # 一个连着 60 天都在报「有漂移」（而 #8 之后是「一直在构建」）的仓库，恰恰**最不能**
    # 让检测器被停用 —— 它是唯一还活着的那只眼睛。
    written, how, skip = False, None, None
    new_text, note = None, None
    if not oc.heartbeat_due:
        note = "心跳未到期（%s 天前）" % oc.heartbeat_age_days
    elif a.dry_run:
        skip = "--dry-run：只报「该写了」，不写"
    else:
        hb = new_state(old_text, today.isoformat(), now_utc or _utc_now(), up["sha"], a.run_url)
        note = hb.note
        new_text = state_text(hb.doc)
        # 「内容确有变化」这条验收**不需要**再查一遍，它是结构性的：写心跳的前提是旧的
        # `heartbeat.at` 至少 30 天前，而新的 `at` 是今天 ⇒ 两个字段必然不同。
        # （第一版这里写了一句 `if new_text == old_text: raise` —— 那**到不了**，
        #   一条永远不会响的检查只会给假的安全感。真要防的是「哪天窗口被改小」，
        #   而那件事在 `sync-check.py` 的 `--heartbeat-days >= 1` 那行上。）
        how = write_state(api, a.repo, a.heartbeat_branch, a.state_path, new_text,
                          file_sha, branch_sha)
        written = True
        log("✅ 心跳已写入 %s（%s）：%s" % (a.heartbeat_branch, how, note))
        verify(a, api, lo["sha"], log)

    log("")
    for line in sc.render(oc, subject).split("\n"):
        log(line)
    log("心跳: %s" % ("已写入 %s（%s）" % (a.heartbeat_branch, how) if written
                    else ("该写但被 %s 跳过" % skip if skip else note)))
    if new_text is not None:
        log("      新状态：%s" % new_text.replace("\n", " ").strip())

    return {
        "state": oc.state, "exit_code": oc.exit, "reason": oc.reason,
        "upstream_repo": a.upstream_repo, "upstream_ref": up_ref, "upstream_sha": up["sha"],
        "upstream_subject": subject,
        "local_repo": a.repo, "local_branch": a.branch, "local_sha": lo["sha"],
        "merge_base": cmp["merge_base_commit"]["sha"],
        "behind_by": cmp.get("behind_by"), "ahead_by": cmp.get("ahead_by"),
        "marker": oc.marker, "marker_note": oc.marker_note,
        "heartbeat_branch": a.heartbeat_branch, "heartbeat_branch_exists": bool(branch_sha),
        "state_path": a.state_path,
        "heartbeat_due": bool(oc.heartbeat_due), "heartbeat_written": written,
        "heartbeat_how": how, "heartbeat_skip": skip, "heartbeat_note": note,
        "heartbeat_age_days": oc.heartbeat_age_days,
        "dry_run": bool(a.dry_run), "today": today.isoformat(),
        "run_url": a.run_url, "now_utc": now_utc or _utc_now(),
    }


def _utc_now():
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def render_summary(r):
    """写进 `$GITHUB_STEP_SUMMARY` 的那一段。**结论要一眼能看见** —— 一天一次的运行，
    没人会去翻日志。"""
    L = ["## 上游漂移检测（检测器 · issue #7）", "",
         "| 项 | 值 |", "|---|---|",
         "| 上游 | `%s@%s` = `%s` |"
         % (r["upstream_repo"], r["upstream_ref"], r["upstream_sha"]),
         "| 本地 | `%s@%s` = `%s` |" % (r["local_repo"], r["local_branch"], r["local_sha"]),
         "| merge-base | `%s` |" % r["merge_base"],
         "| 相差 | 上游领先我们 **%s** 个提交 ｜ 我们领先上游 %s 个 |"
         % (r["behind_by"], r["ahead_by"]),
         "| 上游 HEAD | `%s` |" % r["upstream_subject"],
         "| **结论** | **%s**（退出码 %d） |" % (r["state"], r["exit_code"]),
         "| 心跳 | %s |" % _hb_line(r), ""]
    if r["state"] == "drifted":
        # 说清「到此为止」：本工单只发现，不构建（#8 才接上）。
        L += ["### 有漂移 —— 本工单到此为止", "",
              "检测器**只报结论，不构建**（触发构建是 issue #8 的活）。",
              "上游那个提交还没有进到我们的分支里。", ""]
    elif r["state"] == "in-sync":
        L += ["### 无漂移 —— 立即结束", "",
              "没有任何构建被触发（这条通道本来就没有构建步骤，见 issue #7 的验收）。", ""]
    elif r["state"] == "blacklisted":
        L += ["### 该上游提交已试过且失败 —— 今天不做", "",
              "标记 7 天后自动过期（规格实现决定 7），过期后会自动重试。", ""]
    return "\n".join(L) + "\n"


def _hb_line(r):
    if r["heartbeat_written"]:
        return "✅ 已写入 `%s`（%s）" % (r["heartbeat_branch"], r["heartbeat_how"])
    if r["heartbeat_skip"]:
        return "⚠️ 到期但没写：%s" % r["heartbeat_skip"]
    return "⏸ 未到期（%s 天前）" % r["heartbeat_age_days"]


# ══════════════════════════════════════════════════════════════════════════════
#  自测：假 API + 真编排
#
#  ⚠️ **测的是编排，不是判定** —— 判定由 `sync-check.py --self-test`（28 条）负责。
#     这里要证的是「四个输入取对了、结论用对了、写只写在心跳分支上」。
# ══════════════════════════════════════════════════════════════════════════════
class FakeApi:
    """假 API：**只认实现真正用到的那些端点**，其余一律抛错。

    「多调了一个端点」本身就是实现跑偏的信号，所以这里不给兜底 —— 让它当场炸。
    另记 `refs_written`：不变量 ①（只写心跳分支）在自测里是一条**断言**，不是注释。
    """

    def __init__(self, upstream_sha, local_sha, merge_base, branch_sha=None, state=None,
                 upstream_subject="camera: fix something", repo="o/r",
                 upstream_repo="LineageOS/android_kernel_xiaomi_sm8250",
                 tree_paths=None, moved_sha=None):
        self.up_sh, self.lo_sh, self.mb = upstream_sha, local_sha, merge_base
        self.branch_sha, self.state = branch_sha, state
        self.subject = upstream_subject
        self.repo, self.upstream_repo = repo, upstream_repo
        self.tree_paths = tree_paths if tree_paths is not None else [STATE_PATH]
        self.moved_sha = moved_sha        # 非 None ⇒ 第二次读工作分支 HEAD 时返回它（模拟被别人推了）
        self.local_reads = 0
        self.calls, self.refs_written = [], []
        self.blob_n = 0

    def call(self, method, path, payload=None, allow_404=False):
        self.calls.append("%s %s" % (method, path.split("?")[0]))
        p = path.split("?")[0]
        if method == "GET":
            if "/git/commits/" in p:                       # 取某个提交的 tree（第三条写路径用）
                return {"tree": {"sha": "tree-base"}}
            if "/git/trees/" in p:                         # 写后复核：心跳分支的**顶层**树
                return {"tree": [{"path": x, "type": "blob"} for x in self.tree_paths]}
            # ⚠️ 按**仓库**分辨两个 `commits/<ref>`：上游那个传的是**分支名**而不是 sha，
            #    按 sha 匹配会静默返回本地 HEAD —— 自测就会在一条错的输入上「通过」。
            if "/commits/" in p and not p.startswith("repos/%s/commits/" % self.repo):
                return {"sha": self.up_sh, "commit": {"message": self.subject + "\n\nbody"}}
            if "/commits/" in p:                           # 本地 HEAD（按分支名取）
                self.local_reads += 1
                if self.moved_sha and self.local_reads > 1:
                    return {"sha": self.moved_sha}
                return {"sha": self.lo_sh}
            if "/compare/" in p:
                return {"merge_base_commit": {"sha": self.mb}, "behind_by": 0, "ahead_by": 7}
            if "/git/ref/heads/" in p:
                if self.branch_sha is None:
                    if not allow_404:
                        raise AssertionError("假 API：分支不存在时必须 allow_404")
                    return None
                return {"object": {"sha": self.branch_sha}}
            if "/contents/" in p:
                if self.state is None:
                    if not allow_404:
                        raise AssertionError("假 API：文件不存在时必须 allow_404")
                    return None
                return {"sha": "blob-sha-old", "content": _b64(self.state)}
        if method == "PUT" and "/contents/" in p:
            self.refs_written.append(payload["branch"])
            return {"content": {"sha": "new"}}
        if method == "POST" and p.endswith("/git/blobs"):
            self.blob_n += 1
            return {"sha": "blob-%d" % self.blob_n}
        if method == "POST" and p.endswith("/git/trees"):
            return {"sha": "tree-new"}
        if method == "POST" and p.endswith("/git/commits"):
            return {"sha": "commit-new"}
        if method == "POST" and p.endswith("/git/refs"):
            self.refs_written.append(payload["ref"].split("/")[-1])
            return {"ref": payload["ref"]}
        if method == "PATCH" and "/git/refs/heads/" in p:
            self.refs_written.append(p.split("/git/refs/heads/")[-1])
            return {"object": {"sha": payload["sha"]}}
        raise AssertionError("假 API 不认识这个端点：%s %s" % (method, path))


UP = "a" * 40          # 上游 HEAD
LO = "b" * 40          # 本地 HEAD
MB_SAME = UP           # merge-base == 上游 HEAD ⇒ 无漂移
MB_OLD = "c" * 40      # merge-base 停在上游历史里 ⇒ 有漂移
WORK = "ksu-lineage-23.2"
TODAY = "2026-09-26"
NOW = "2026-09-26T12:00:00Z"


def _args(**kw):
    base = dict(repo="o/r", branch=WORK,
                upstream_repo="LineageOS/android_kernel_xiaomi_sm8250",
                upstream_branch="lineage-23.2", upstream_sha=None,
                heartbeat_branch=HEARTBEAT_BRANCH, state_path=STATE_PATH,
                work_dir=tempfile.gettempdir(), run_url="https://example/run/1",
                today=TODAY, dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _quiet(*_a, **_kw):
    pass


def self_test(tmpdir):
    """11 条用例。返回失败条数。

    **测的是编排，不是判定**（判定由 `sync-check.py --self-test` 的 28 条负责）。
    每条都断言四件事：结论 / 退出码 / 心跳写不写 / **写过的 ref 是不是只有心跳分支**。
    最后那一条是不变量 ① 的落地方式 —— 验收里「不向任何工作分支写入」不该只靠人读代码。
    """
    os.makedirs(tmpdir, exist_ok=True)
    bad = 0
    n = [0]
    hb_doc = {"failed": None, "heartbeat": {"at": TODAY, "at_utc": NOW, "upstream": UP,
                                            "run": "https://example/run/1"}}

    def fail(what, *lines):
        nonlocal bad
        bad += 1
        print("  ❌ #%-2d %s\n       %s" % (n[0], what, "\n       ".join(str(x) for x in lines)))

    def case(what, want, **kw):
        """跑一次 `run()`。`want = (结论, 退出码, 是否写心跳)`；`None` = 只跑不断言四元组。"""
        n[0] += 1
        api_kw = {"merge_base": kw.pop("merge_base", MB_SAME)}
        for k in ("branch_sha", "state", "upstream_subject", "tree_paths", "moved_sha"):
            if k in kw:
                api_kw[k] = kw.pop(k)
        api = FakeApi(UP, LO, **api_kw)
        try:
            r = run(_args(work_dir=tmpdir, **kw), api, log=_quiet, now_utc=NOW)
        except Exception as e:                       # 崩溃本身就是不合格
            fail(what, "抛异常：%r" % (e,))
            return None, api
        problems = []
        if want is not None and (r["state"], r["exit_code"], r["heartbeat_written"]) != want:
            problems.append("期望 %s，实际 %s"
                            % (want, (r["state"], r["exit_code"], r["heartbeat_written"])))
        refs = sorted(set(api.refs_written))
        if refs and refs != [HEARTBEAT_BRANCH]:
            problems.append("写过的 ref 应只有 ['%s']，实际 %s" % (HEARTBEAT_BRANCH, refs))
        # 不变量 ①：工作分支**只读**。假 API 记下了每一次调用，这里逐条查。
        for c in api.calls:
            if not c.startswith("GET") and WORK in c:
                problems.append("对工作分支做了非 GET 调用：%s" % c)
        if problems:
            fail(what, *problems)
        else:
            print("  ✅ #%-2d %-46s -> %s / %d / 心跳%s"
                  % (n[0], what.replace("`", "")[:46], r["state"], r["exit_code"],
                     "已写" if r["heartbeat_written"] else "未写"))
        return r, api

    # ① 首次运行：无漂移、从未写过心跳 ⇒ 判 heartbeat-due，并在**孤儿分支**上写一个提交
    r, api = case("① 无漂移 + 从未写过心跳 ⇒ 写心跳", ("heartbeat-due", 30, True))
    n[0] += 1
    if r is None:
        pass
    elif (r["heartbeat_how"] == "orphan" and api.blob_n == 1
          and "POST repos/o/r/git/refs" in api.calls):
        print("       └ #%d 走的是**孤儿提交**：blobs → trees（无 base_tree）"
              "→ commits（parents=[]）→ 建 ref" % n[0])
    else:
        fail("① 附属：首次写必须走孤儿提交（blob+tree+commit+建 ref）",
             "how=%s，blob 数 %d" % (r["heartbeat_how"], api.blob_n))

    # ② 心跳刚写过，同一天再跑 ⇒ 不再写（验收 3：未满 30 天不重复写）
    case("② 心跳刚写过，同一天再跑 ⇒ 不再写", ("in-sync", 0, False),
         branch_sha="c0mmit", state=state_text(hb_doc))

    # ③ 有漂移 + 心跳未到期 ⇒ drifted、不写；**本工单不构建**（假 API 里根本没有 dispatch 端点，
    #    真调了就会炸 —— 那就是「无漂移/有漂移都不构建」这条验收的结构性证据）
    case("③ 有漂移 + 心跳未到期 ⇒ drifted / 不写", ("drifted", 10, False),
         merge_base=MB_OLD, branch_sha="c0mmit", state=state_text(hb_doc))

    # ④ 有漂移 + 心跳到期 ⇒ 结论仍是 drifted（优先级），心跳照样写（正交输出）
    case("④ 有漂移 + 心跳到期 ⇒ drifted，心跳照样写", ("drifted", 10, True),
         merge_base=MB_OLD, branch_sha="c0mmit", state='{"heartbeat": {"at": "2026-08-16"}}')

    # ⑤ 已拉黑 + 心跳到期 ⇒ blacklisted 优先，心跳也照样写（别让检测器自己也死掉）
    case("⑤ 已拉黑 + 心跳到期 ⇒ blacklisted，心跳照样写", ("blacklisted", 20, True),
         branch_sha="c0mmit", state='{"failed": {"upstream": "%s", "at": "2026-09-24"}}' % UP)

    # ⑥ 不变量：写心跳**不得抹掉** `failed`（抹掉 = 被拉黑的提交在下一轮静默复活）
    other, old = "d" * 40, '{"failed": {"upstream": "%s", "at": "2026-09-24"}}' % ("d" * 40)
    r6, _ = case("⑥ 写心跳必须保留 `failed`", ("drifted", 10, True),
                 merge_base=MB_OLD, branch_sha="c0mmit", state=old)
    n[0] += 1
    if r6 is None:
        pass
    elif new_state(old, TODAY, NOW, UP, "u").doc.get("failed", {}).get("upstream") == other:
        print("       └ #%d 纯函数 `new_state` 里 `failed` 原样保留（upstream=%s…）"
              % (n[0], other[:8]))
    else:
        fail("⑥ 附属：`new_state` 丢了 `failed`", new_state(old, TODAY, NOW, UP, "u"))

    # ⑦ `failed` 写坏 ⇒ 写心跳时**丢弃它并自愈**（坏字段留在文件里，下一轮还是坏的）
    n[0] += 1
    hb7 = new_state('{"failed": "oops"}', TODAY, NOW, UP, "u")
    if hb7.doc.get("failed") is not None or "丢弃" not in hb7.note:
        fail("⑦ `failed` 写坏 ⇒ 丢弃并说明", "实际：%r / %s" % (hb7.doc, hb7.note))
    else:
        print("  ✅ #%-2d %-46s -> 自愈：%s" % (n[0], "⑦ `failed` 写坏 ⇒ 丢弃并说明", hb7.note[:30]))

    # ⑧ 分支在、**文件不在** ⇒ 第三条写路径：带 `base_tree` 与父提交，然后 **PATCH ref**
    #    （而不是去建一个已经存在的 ref —— 那会撞 422，报一句与实际原因无关的错）。
    #    这条不是假想：状态文件被手工删掉、或将来 #10 换了文件名，都会落到它上面。
    r8, api8 = case("⑧ 分支在但状态文件不在 ⇒ PATCH ref，不建 ref", ("drifted", 10, True),
                    merge_base=MB_OLD, branch_sha="c0mmit", state=None)
    n[0] += 1
    if r8 is None:
        pass
    elif (r8["heartbeat_how"] == "commit"
          and "PATCH repos/o/r/git/refs/heads/" + HEARTBEAT_BRANCH in api8.calls
          and "POST repos/o/r/git/refs" not in api8.calls):
        print("       └ #%d 走的是 PATCH ref（blobs → trees(base_tree) → commits(有父) → "
              "PATCH refs）" % n[0])
    else:
        fail("⑧ 附属：文件不在时该走 PATCH ref 那条路",
             "how=%s，调用 %s" % (r8["heartbeat_how"], [c for c in api8.calls if "refs" in c]))

    # ⑨ / ⑩ / ⑪ 不变量 ① 的守卫：**联网之前**就该拒绝
    for what, hb in (("⑨ 心跳分支 == 工作分支 ⇒ 拒绝", WORK),
                     ("⑩ 心跳分支落在 ksu- 命名空间 ⇒ 拒绝", "ksu-heartbeat"),
                     ("⑪ 心跳分支落在 lineage- 命名空间 ⇒ 拒绝", "lineage-heartbeat")):
        n[0] += 1
        try:
            run(_args(work_dir=tmpdir, heartbeat_branch=hb), FakeApi(UP, LO, MB_SAME), log=_quiet)
            fail(what, "没拒绝")
        except UsageError:
            print("  ✅ #%-2d %-46s -> UsageError" % (n[0], what))

    # ⑫ / ⑬ 写后复核的两条：**两条都真的会失败**（回归闸，不是仪式）
    for what, api_kw in (("⑫ 复核：工作分支被动过 ⇒ 报错", {"moved_sha": "e" * 40}),
                         ("⑬ 复核：心跳分支不是孤儿（顶层不止一个文件）⇒ 报错",
                          {"tree_paths": ["Makefile", "arch", "drivers", STATE_PATH]})):
        n[0] += 1
        try:
            run(_args(work_dir=tmpdir),
                FakeApi(UP, LO, MB_OLD, branch_sha="c0mmit", state=None, **api_kw), log=_quiet)
            fail(what, "没报错")
        except RuntimeError as e:
            print("  ✅ #%-2d %-46s -> %s" % (n[0], what, str(e).split("\n")[0][:40]))

    total = n[0]
    print()
    print("结果: %s（%d/%d 通过）" % ("失败" if bad else "全部通过", total - bad, total))
    return bad


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        add_help=True, description="检测器：漂移发现 + 心跳（只判定，不构建；见文件头）",
        epilog="退出码：0 无漂移 / 10 有漂移 / 20 已拉黑 / 30 该写心跳了 / 1 运行错误 / 2 用法错误")
    ap.add_argument("--repo", metavar="OWNER/REPO", help="本线工作仓库（写心跳的分支在它上面）")
    ap.add_argument("--branch", metavar="分支", help="**工作分支**（只读它，绝不写它）")
    ap.add_argument("--upstream-repo", metavar="OWNER/REPO", default="LineageOS/android_kernel_xiaomi_sm8250")
    ap.add_argument("--upstream-branch", metavar="分支", default="lineage-23.2")
    ap.add_argument("--upstream-sha", metavar="SHA", default=None,
                    help="钉住上游某个提交（默认取 --upstream-branch 的 HEAD）")
    ap.add_argument("--heartbeat-branch", metavar="分支", default=HEARTBEAT_BRANCH,
                    help="心跳分支（**孤儿分支**；默认 %s）" % HEARTBEAT_BRANCH)
    ap.add_argument("--state-path", metavar="路径", default=STATE_PATH,
                    help="心跳分支上的状态文件（默认 %s）" % STATE_PATH)
    ap.add_argument("--run-url", metavar="URL", default=None,
                    help="写进心跳内容（内容因此每次都不同 ⇒ 不可能是空提交）")
    ap.add_argument("--dry-run", action="store_true", help="只判定，不写任何东西")
    ap.add_argument("--today", metavar="YYYY-MM-DD", help="「今天」（默认取 UTC 当天）")
    ap.add_argument("--now-utc", metavar="ISO8601", default=None,
                    help="心跳内容里的秒级时刻（默认取当前 UTC；自测/复现用）")
    ap.add_argument("--work-dir", metavar="目录", default=None,
                    help="放从心跳分支取回的状态文件（默认系统临时目录）")
    ap.add_argument("--api-url", metavar="URL", default=API_DEFAULT)
    ap.add_argument("--json-out", metavar="文件", help="把结果写一份 JSON")
    ap.add_argument("--github-output", metavar="文件", help="写 `key=value`（喂 $GITHUB_OUTPUT）")
    ap.add_argument("--summary", metavar="文件", help="追加一段 Markdown（喂 $GITHUB_STEP_SUMMARY）")
    ap.add_argument("--self-test", action="store_true", help="离线跑内置用例（不联网、不读参数）")
    ap.add_argument("--self-test-dir", metavar="目录", default=None)
    a = ap.parse_args(argv[1:])

    if a.self_test:
        return 1 if self_test(a.self_test_dir or
                              os.path.join(tempfile.gettempdir(), "detect-selftest")) else 0

    if not a.repo or not a.branch:
        print("缺必填参数: --repo / --branch")
        return EXIT_USAGE
    a.work_dir = a.work_dir or tempfile.gettempdir()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token and not a.dry_run:
        print("⚠️  没有 GH_TOKEN / GITHUB_TOKEN：读接口是公开的，但**写心跳会 403**。")
        print("    只想知道结论就加 --dry-run。")
    api = Api(a.api_url, token)
    try:
        r = run(a, api, log=print, now_utc=a.now_utc)
    except UsageError as e:
        print("用法错误：%s" % e)
        return EXIT_USAGE
    except (ApiError, RuntimeError) as e:
        print("❌ 检测器失败：%s" % e)
        print("    （退出码 %d = 运行错误；四个正常结论是 0/10/20/30）" % EXIT_RUN_ERROR)
        return EXIT_RUN_ERROR

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(r, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
    if a.github_output:
        with open(a.github_output, "a", encoding="utf-8", newline="\n") as f:
            for k in ("state", "exit_code", "heartbeat_due", "heartbeat_written",
                      "upstream_sha", "local_sha", "merge_base", "behind_by", "ahead_by",
                      "heartbeat_branch_exists", "dry_run"):
                f.write("%s=%s\n" % (k, str(r[k]).lower()))
    if a.summary:
        with open(a.summary, "a", encoding="utf-8", newline="\n") as f:
            f.write(render_summary(r))
    return r["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
