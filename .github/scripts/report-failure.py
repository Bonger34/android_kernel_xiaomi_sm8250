#!/usr/bin/env python3
"""失败上报（issue #10）：**红了就叫人与拉黑**。

对应规格 `docs/los-line/upstream-sync-spec.md` 的实现决定 7 与用户故事 6~11。
三条自动规则（规格原文）：

| 规则 | 本文怎么落地 |
|---|---|
| **同一个上游提交只开一个 issue**，后续失败以**评论**追加 | issue 标题里带上游短 sha（`[上游同步] 失败：上游 <12>`），按**标题精确相等**在 `state=all` 的 issue 列表里找；找到就追加评论（关掉了就顺手打开） |
| **同一个上游提交失败后不再重试** | 把 `failed = {"upstream": <sha>, "at": <今天>}` 写进心跳分支的状态文件 —— 那正是 `sync-check.py` 的 `--marker`，它据此判 `blacklisted`（退出码 20），检测器那一轮的四个动作**一个都不做** |
| **基础设施抖动（超时 / 取消 / 启动失败）不算失败** | 判定走 **job 级**结论：`cancelled` / `timed_out` / `startup_failure` / `stale` / `action_required` ⇒ **不写标记、不开 issue**，下一轮自己重试 |
| 标记 **7 天后自动过期** | 7 天那个常量**不在本文件里**，从 `sync-check.py` 取（`EXPIRE_DAYS`）—— 两处各记一份迟早会漂移 |

## ⚠️ 判定为什么是 job 级，而不是 run 级

规格那句「不记 `cancelled` / `timed_out` / `startup_failure`」说的是 **run 的结论**。
但本工具**看不到自己这一轮的 run 结论**：它跑的这一刻那次 run 还没结束 ——
它自己就是那次 run 的一部分（**两条路里 `notify` 都是一个独立 job**：构建器的
`needs: [prepare, build, repro]`、检测器的 `needs: [detect]`）。
⇒ 判定改看 **`GET /actions/runs/<id>/jobs`** 的逐 job 结论 + 逐 step 结论，
而 **`startup_failure`（run 根本没起来）在这一层是够不着的** —— 但那种情形下
本工具**根本不会被调用**（没有 runner 去跑 `notify`）⇒ 「不写标记」是**结构性**的，
不需要一条永远不会响的检查（同类批评见 `detect.py` 的 `run()` 里删掉的那条
「新旧内容逐字相同 ⇒ 报错」）。

🚨 **2026-09-27 订正（issue #17）**：上面这句原先写的是「检测器那条路里它就是一个 **step**」——
**那个形状根本不会生效**：同一 job 内读 job 列表时，平台对**自己刚失败那一步**的回声
还没出来 ⇒ 判 `V_NO_FAILURE`、静默什么都不做。现已改成**独立 job**，
根因与两条同族教训见 `PROJECT.md` §7 坑表 **#61**。

同理，**取消**也有一半是结构性的：一次被取消的 run 里 `notify` 通常根本不会开始。
但只要它开始了，job 级判定就接管 —— 于是「取消」这一档**有**一条真的会响的检查。

## 两条不变量

**① 只写心跳分支，绝不碰工作分支。** 机制与 `detect.py` **共用同一份代码**
（`write_state` / `list_refs` / `verify` 都从那边 import，不是抄一份）：
写前写后各取一次**全部 ref** 的快照，断言唯一变动的就是心跳分支。
⚠️ 这不是形式主义：状态文件是**两个 writer 共写**的（检测器写 `heartbeat`、本工具写 `failed`），
而 Contents API 的 CAS（改要 `sha`、建不能给 `sha`）让并发写**响亮地失败**，不会互相覆盖。

**② 先叫人、后拉黑 —— 顺序是判据，不是风格。**
两件事都可能失败，而**失败方向必须选**：

| 顺序 | 万一后一步失败 | 后果 |
|---|---|---|
| 先写标记、后开 issue | 标记在、issue 不在 | **最坏**：下一轮直接判 `blacklisted`，而**没有任何人知道**（通道安静地停了） |
| **先开 issue、后写标记**（本文） | issue 在、标记不在 | 下一轮**照常重试** ⇒ 再失败就**追加评论**（去重已经在）⇒ 自愈，且人已经被叫到了 |

⇒ 所以本工具的写顺序是**固定的**：issue → 标记。issue 那一步失败就**直接退出码 1**，
一个字节都不写（连标记也不写）—— 「叫不到人就绝不放行」比「安静地拉黑」好。

## 用法

```sh
# 构建器（build.yml 的 notify job）：红了才调用
python .github/scripts/report-failure.py --repo "$GITHUB_REPOSITORY" --run-id "$GITHUB_RUN_ID" \
    --upstream-sha "$UPSTREAM_SHA" --merge-sha "$MERGE_SHA" --base-sha256 "$BASE_SHA256" \
    --work-branch ksu-lineage-23.2 --heartbeat-branch sync-heartbeat \
    --run-url "$GITHUB_SERVER_URL/$GITHUB_REPOSITORY/actions/runs/$GITHUB_RUN_ID" \
    --summary "$GITHUB_STEP_SUMMARY" --json-out report-failure.json

# 本机：对一次**已经红过**的运行只读复算（不写任何东西）
python tools/report-failure.py --repo Bonger34/android_kernel_xiaomi_sm8250 \
    --run-id 36322878807 --upstream-sha <sha> --dry-run

python tools/report-failure.py --self-test      # 离线，假 API（不联网）
```

退出码：`0` 处置完成（**四种结论都是正常结局**，明细见日志与摘要）/ `1` 运行错误 /
`2` 用法错误。

⚠️ **本文件有两份，必须逐字节相同**：工作区 `tools/report-failure.py` 与 LOS 工作仓库里的
`.github/scripts/report-failure.py`（CI 跑后者 —— 内核树**自带**一个 `tools/`，那是上游的）。
镜像与复验：`python tools/sync-ci-scripts.py --gen` / `--check`。
"""

import argparse
import base64
import datetime
import importlib.util
import json
import os
import re
import sys
import tempfile
import urllib.parse
from typing import NamedTuple

WS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXIT_OK = 0
EXIT_RUN_ERROR = 1
EXIT_USAGE = 2

# 四种结论（**不是**「成功 / 失败」二分：三种都是正常的、无需人工的结局）
V_REPORTED = "reported"          # 真失败：已开/追加 issue + 已写失败标记
V_JITTER = "jitter"              # 基础设施抖动：不写标记、不开 issue，下一轮自己重试
V_NO_FAILURE = "no-failure"      # 这一轮没有红（本工具不该被调用，但不是错误）
V_NO_UPSTREAM = "no-upstream"    # 没有上游提交可记（手动触发，或检测器在判定前就失败）

# job 级结论里「抖动」的那几档（规格实现决定 7 点名前三个，后两个同族：
# `stale` = 排队太久被丢弃、`action_required` = 卡在人工批准上，都不是「构建失败」）
JITTER = ("cancelled", "timed_out", "startup_failure", "stale", "action_required")
FAILURE = "failure"

ISSUE_TITLE = "[上游同步] 失败：上游 %s"
ISSUE_LABEL = "needs-triage"     # 本仓库**已存在**的标签（见 docs/agents/triage-labels.md）

ISSUE_PAGE = 100                 # 列 issue 时一页多少条
ISSUE_MAX_PAGES = 10             # 超过就**拒绝给结论**（宁可响亮地失败，也不要开第二个 issue）
JOBS_PAGE = 100                  # 列 job 时一页多少条（同一个理由）
ARTIFACTS_PAGE = 100             # 列产物时一页多少条（同一个理由）


def _load_sibling(name, filename):
    """加载同目录（或工作区 `tools/`）下的另一件工具。

    为什么要回退：CI 里这些脚本跑在 `.github/scripts/` 下（内核树**自带**一个 `tools/`），
    此时 `WS` = `<仓库>/.github`，其下并没有 `tools/detect.py`
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


def qs(x):
    """查询串里的值（分支名、ref）。斜杠也转义 —— 它在查询里没有语义。"""
    return urllib.parse.quote(x, safe="")


def qp(x):
    """路径里的片段（文件路径）。保留斜杠（仓库内路径本来就有斜杠）。"""
    return urllib.parse.quote(x, safe="/")


def short(sha):
    return (sha or "")[:12]


def assert_sha(name, value):
    """形状守卫：**完整的 40 位十六进制**。

    ⚠️ 为什么必须有它（而不是「反正传错也是人的问题」）：`failed.upstream` 是
    `sync-check.py` 判「同一个提交已试过」的**唯一**依据，而那条比较是**精确相等**
    （`check()` 里 `failed_sha == upstream`）。写歪一个字符（短 sha、带空格、大写没归一）
    的后果**不是报错，而是这条判据永不成立** —— 检测器照常为它派发构建，
    而 issue 正文里那句「7 天内不会再触发构建」变成一句**没人会去核的假话**。
    同族两件工具（`try-merge.py` / `promote.py`）都有同一件东西，理由同源：
    「写歪的 sha 会让 API 去取一个不存在的东西」。
    """
    v = (value or "").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", v.lower()):
        raise UsageError("%s 必须是完整的 40 位十六进制 sha（收到 %r）—— 短 sha / 带空白都会让"
                         "失败标记**永远匹配不上**上游 HEAD，而那是静默的" % (name, value))
    return v.lower()


# ── 判定：**纯函数**，所以「哪一档算抖动」这件事可以离线穷举断言 ──────────────────
class Step(NamedTuple):
    number: int
    name: str


class Failure(NamedTuple):
    """一个真的红了的 job。`steps` 是它**自己**报失败的那些步骤（可能是空的：
    job 级失败而每个 step 都是 success 的情形确实存在 —— 例如 runner 被杀）。"""

    job: str
    steps: tuple

    def headline(self):
        if self.steps:
            s = self.steps[0]
            more = "（另有 %d 个失败步骤）" % (len(self.steps) - 1) if len(self.steps) > 1 else ""
            return "job `%s` 的第 %d 步 `%s`%s" % (self.job, s.number, s.name, more)
        return "job `%s`（job 级失败，没有单独报失败的步骤）" % self.job


class Verdict(NamedTuple):
    kind: str
    reason: str
    failures: tuple       # (Failure, ...) —— 第一个就是**首个失败环节**
    jitter: tuple         # ((job, conclusion), ...)
    pending: tuple        # 还没结束、**没被计入**判断的 job 名


def classify(jobs, self_job=None):
    """把 `GET /actions/runs/<id>/jobs` 的结果判成四选一里的一个。**纯函数。**

    ⚠️ **优先级：抖动 > 真失败 > 没有红。** 一次被取消的运行里往往还夹着
    「因为依赖被取消而报失败」的次生 job —— 把它当成真失败会把一个**基础设施问题**
    变成一次**拉黑**，而拉黑是 7 天。反过来（失败优先）的代价要说全：**既不叫人、也不拉黑**
    （`V_REPORTED` 把「叫人」与「拉黑」绑在同一支上）⇒ 下一次它照常重试、照常红，仍然没人被叫。
    本工具选前者：**「把偶发抖动记成 7 天拉黑」会让通道安静地停摆**，而那比多红一次贵得多。

    ⚠️ `self_job` 从 `GITHUB_JOB` 默认拿到，用来处理「本工具跑在自己的 run 里、
    而这个 job 的结论此刻还是 `null`」：那种情形下只看**已结束且结论为 failure** 的步骤。

    🚨 **#17：这个分支不可靠，不要依赖它。** 2026-09-27 真跑（run 36339159328，冲突夹具）
    证明它会**静默失效**：run 与 job 的结论都是 `failure`，而本工具判 `V_NO_FAILURE`、
    一个字节都不写、issue 也不开。根因是**平台对步骤结论的回声有延迟** ——
    试合并 `18:03:31.304` 失败、上报 `18:03:31.310` 启动（相隔 **6 毫秒**），
    它 `18:03:31.82` 读 API 时那个 `failure` **还没回声出来** ⇒ 步骤列表里看不到失败项。
    ⇒ **正确的用法：把上报放进一个独立的 job**（`needs: <会失败的那个>` + `if: failure()`）——
    那样它天然在对方结束之后才启动，API 早已回声完毕。`build.yml` 的 `notify` 一直如此，
    所以它没这个毛病；`detect.yml` 的 `notify` 自 #17 起也改成了同样的形状。
    ⇒ 根因、量出来的余量（6 ms → 57 s）与两条同族教训见 `PROJECT.md` §7 坑表 **#61**。

    ⚠️ **这一档只对「上报与失败在同一个 job」的旧形状成立** —— 本仓库**两条路都已不是**
    那个形状（见上面 🚨）。新形状下失败在**另一个已经结束的** job 里，`self_job` 就算
    写歪也只是把 `notify` 自己记进 `pending`，结论仍是 `V_REPORTED`；而且 `GITHUB_JOB`
    是自动跟的，CI 上不存在「改 job 名导致漂移」这回事。
    ⇒ `--self-job` 现在是**兼容参数**（给「同 job 上报」的形状留的），
    下面那句「还有 N 个 job 没结束」的 ⚠️ 仍然有用 —— 它报的是**真正还没结束**的 job。
    """
    failures, jitter, pending = [], [], []
    for j in jobs or []:
        name = j.get("name") or "?"
        if j.get("status") != "completed":
            if name == self_job:
                # 本 job 还在跑：只看**已经结束的步骤**（正在跑的那一步结论是 null）
                bad = tuple(Step(s.get("number"), s.get("name") or "?")
                            for s in (j.get("steps") or [])
                            if s.get("status") == "completed" and s.get("conclusion") == FAILURE)
                if bad:
                    failures.append(Failure(name, bad))
            else:
                pending.append(name)
            continue
        conc = j.get("conclusion")
        if conc in JITTER:
            jitter.append((name, conc))
        elif conc == FAILURE:
            bad = tuple(Step(s.get("number"), s.get("name") or "?")
                        for s in (j.get("steps") or []) if s.get("conclusion") == FAILURE)
            failures.append(Failure(name, bad))

    if jitter:
        return Verdict(V_JITTER,
                       "**基础设施抖动**（%s）⇒ 不写标记、不开 issue，下一轮自己重试"
                       % "、".join("job `%s` = `%s`" % (n, c) for n, c in jitter),
                       tuple(failures), tuple(jitter), tuple(pending))
    if failures:
        return Verdict(V_REPORTED, "真失败：%s" % failures[0].headline(),
                       tuple(failures), (), tuple(pending))
    return Verdict(V_NO_FAILURE,
                   "这一轮没有红" + ("（⚠️ %d 个 job 还没结束，未被计入：%s）"
                                   % (len(pending), "、".join(pending)) if pending else ""),
                   (), (), tuple(pending))


# ── 失败标记：**纯函数**，与 `detect.py` 的 `new_state()` 对称（它保留 `failed`，本文保留 `heartbeat`）
class Marker(NamedTuple):
    doc: dict
    changed: bool          # False = 已经有同一条标记且没过期 ⇒ **一个写请求都不发**
    note: str


def new_failed_state(old_text, upstream_sha, today, expire_days):
    """往状态文件里写失败标记，**保留 `heartbeat`**。返回 `(doc, changed, note)`。

    ⚠️ **`heartbeat` 必须原样保留**：它是「防 GitHub 的 60 天无活动停用检测器」的唯一载体
    （规格实现决定 3）。本工具把它抹掉，等于顺手把那只眼睛关掉。
    但「保留」只对**能读懂**的成立：读不出来就丢掉并说明（与 `detect.py` 同一条自愈规则）。

    日期是**粘的**（这是有意的）：

    | 旧标记 | 处置 | 为什么 |
    |---|---|---|
    | 同一个上游提交、**没过期** | 原样保留 `at`，`changed=False` | 「7 天窗口」从**第一次**失败起算。每次失败都重新记日期 ⇒ 一个天天重试的提交会被**永久**拉黑，而那恰好是规格要防的事 |
    | 同一个上游提交、**已过期** | 改写成今天 | 过期意味着「又试了一次」⇒ 这一次失败要重新起算 7 天 |
    | **别的**上游提交 | 换成新的 | 拉黑只对「当前上游 HEAD」有意义（`sync-check.py` 比的就是它）|
    | 写坏 | 丢掉它并自愈 | 一个写坏的字段不该永久卡住整条通道 |
    """
    st, note, keep_hb = {}, None, False
    old = None
    if old_text:
        try:
            old = json.loads(old_text)
        except ValueError:
            old = None
            note = "旧状态文件不是合法 JSON ⇒ 整体重写（自愈）"
    if isinstance(old, dict):
        hb = old.get("heartbeat")
        if isinstance(hb, dict) and hb.get("at"):
            st["heartbeat"] = hb                     # 原样保留（本工具**不写**心跳）
            keep_hb = True
        elif hb is not None:
            note = "旧状态文件的 `heartbeat` 读不出（缺 `at`）⇒ 丢弃它（自愈）"
        f = old.get("failed")
        if isinstance(f, dict) and f.get("upstream") and f.get("at"):
            sha, at = str(f["upstream"]).lower(), str(f["at"])
            if sha == upstream_sha.lower():
                try:
                    age = (today - datetime.date.fromisoformat(at)).days
                except ValueError:
                    age = None
                if age is not None and age < expire_days:
                    st["failed"] = {"upstream": sha, "at": at}
                    return Marker(st, False, _hb_note(
                        "已经有这个提交的失败标记（%s 记的，%d 天前，窗口 %d 天）"
                        "⇒ 不重记日期" % (at, age, expire_days), keep_hb))
                st["failed"] = {"upstream": sha, "at": today.isoformat()}
                if age is None:
                    # `at` 写坏。⚠️ 措辞不能写成「已过期」—— 那不是事实。事实是：
                    # `sync-check.py` 对同样的坏法判「损坏 ⇒ **不拉黑**」，所以**重新起算**才是对的。
                    return Marker(st, True, _hb_note(
                        "旧标记是同一个提交，但 `at` 读不出（%r）⇒ 重新记一次"
                        "（`sync-check` 对同样的坏法判「损坏 ⇒ 不拉黑」，所以这次要自己起算）"
                        % at, keep_hb))
                return Marker(st, True, _hb_note(
                    "旧标记是同一个提交但**已过期**（%s，%d 天前）⇒ 重新记一次"
                    % (at, age), keep_hb))
            st["failed"] = {"upstream": upstream_sha.lower(), "at": today.isoformat()}
            return Marker(st, True, _hb_note(
                "上一个标记是**别的**提交（%s，%s）⇒ 换成这一次的" % (sha[:12], at), keep_hb))
        if f is not None:
            note = "旧状态文件的 `failed` 读不出（缺 `upstream` 或 `at`）⇒ 丢弃它（自愈）"
        elif note is None:
            note = "旧文件里没有失败标记（这是第一次失败）"
    if note is None:
        note = "状态文件不存在（首次运行，正常）"
    st["failed"] = {"upstream": upstream_sha.lower(), "at": today.isoformat()}
    return Marker(st, True, _hb_note(note, keep_hb))


def _hb_note(note, keep_hb):
    """把「心跳那一半怎么样了」拼进说明里。

    ⚠️ 每一行日志都要能回答「**我动了什么、没动什么**」：本工具只写 `failed`，
    而 `heartbeat` 是防 60 天停用规则的那只眼睛（规格实现决定 3）—— 它没被动过这件事
    应当**看得见**，而不是靠读源码去确认。
    """
    return note + ("；`heartbeat` 原样保留" if keep_hb else "")


def marker_msg(upstream_sha, expire_days):
    """写标记那次提交的提交信息。**说清它是什么、什么时候自己消失** ——
    将来有人翻 `sync-heartbeat` 的历史时，这是唯一解释得清那几行的东西。"""
    return ("chore(sync): 拉黑上游 %s（构建失败，%d 天后自动过期）\n\n"
            "由 .github/scripts/report-failure.py 自动写入（issue #10）。\n"
            "`failed.at` 起算 %d 天窗口内，检测器不会再为这个上游提交触发构建；\n"
            "过期后会自动重试。要立刻重试就把 `failed` 改成 null。"
            % (short(upstream_sha), expire_days, expire_days))


# ── issue 正文与摘要：**纯函数**（内容本身是判据的一部分：验收要求「正文含上游提交号与失败环节」）
def issue_title(upstream_sha):
    return ISSUE_TITLE % short(upstream_sha)


def _artifact_lines(artifacts):
    if not artifacts:
        return ["**这次没有产物** —— 失败发生在产出产物之前。"
                "（两份 `dist` 与 `kernel-image-*` 的上传是 `if: always()`，只要产出过就在；"
                "检测器那条路本来就不产出任何东西。）"]
    L = ["| artifact | 字节 | 状态 |", "|---|---|---|"]
    for a in artifacts:
        L.append("| `%s` | %s | %s |"
                 % (a.get("name"), "{:,}".format(a.get("size_in_bytes") or 0),
                    "⏳ 已过期" if a.get("expired") else "可下载"))
    L.append("")
    L.append("产物在运行页的 Artifacts 区（GitHub 保留 **90 天**）。")
    return L


def issue_body(a, verdict, artifacts, marker_note, expire_days):
    """新建 issue 的正文。四项硬要求：**上游提交号**、**失败环节**、产物、以及怎么恢复。

    ⚠️ 「已经自动做的处置」那一节的措辞是**按顺序写的**：本文件先发 issue、**之后**才写标记
    （见文件头不变量 ②）。所以那句不能写成「✅ 已记下…」—— 那种写法在标记写失败时就成了假话
    （坑表 #34 的同形：一份文件里的结论只能覆盖**它写下之前**发生的事）。
    """
    f = verdict.failures[0] if verdict.failures else None
    L = ["## 这次上游同步没能自动跑完（自动开的 issue）", "",
         "> 由 CI 自动创建（`.github/scripts/report-failure.py`，issue #10）。",
         "> **同一个上游提交只会有一个 issue** —— 后续同类失败**追加评论**，不会刷屏。", "",
         "| 项 | 值 |", "|---|---|",
         "| **上游提交** | `%s` |" % a.upstream_sha,
         "| **失败环节** | %s |" % (f.headline() if f else "（未识别）"),
         "| 运行 | [run %s](%s) |" % (_run_id_of(a.run_url), a.run_url),
         "| 工作分支 | `%s`（**一个字节都没动**） |" % a.work_branch,
         "| 合并提交 | %s |" % ("`%s`（不在任何分支上 —— 全绿才会推）" % a.merge_sha
                                if a.merge_sha else "（本次没有合并）"),
         "| 合并基点 | %s |" % ("`%s`" % a.merge_base if a.merge_base else "（未记录）"),
         "| 底包 sha256 | %s |" % ("`%s`" % a.base_sha256 if a.base_sha256 else "（未记录）"),
         "| 失败标记 | %s |" % ("`failed.upstream = %s`（%s）" % (short(a.upstream_sha), marker_note)),
         ""]
    L += ["### 失败在哪个环节", "",
          "```"] + _failure_block(verdict) + ["```", ""]
    if verdict.pending:
        L += ["⚠️ 有 %d 个 job 当时还没结束，**没被计入**：%s"
              % (len(verdict.pending), "、".join(verdict.pending)), ""]
    L += ["### 失败产物", ""] + _artifact_lines(artifacts) + ["",
          "### 这条通道接下来会做什么", "",
          "- `failed.upstream` 会被记成 `%s`（**本文件写在它之前**）⇒ 接下来 %d 天内，"
          "检测器不会再为这个上游提交触发构建（判定在 `sync-check.py`，退出码 20）。"
          % (short(a.upstream_sha), expire_days),
          "  ⚠️ 万一那一步失败（并发写 / 权限），本轮退出码 1 —— 而**下一轮会照常重试**，"
          "不会静默拉黑。",
          "- ✅ 工作分支 `%s` **一个字节都没动**（红的运行根本不会调用 `promote.py`）——"
          "这一条是**结构性**的，不依赖上面那一步。" % a.work_branch,
          "",
          "### 怎么恢复", "",
          "1. 看上面的运行日志与产物，定位原因；",
          "2. 修好之后**不必等 %d 天**：打开 `%s` 分支上的 `%s`，把 `\"failed\"` 改成 `null`"
          % (expire_days, a.heartbeat_branch, a.state_path),
          "   并提交 —— 下一轮检测器就会重新尝试这个提交；",
          "3. 或者什么都不做：标记 **%d 天后自动过期**，届时同一提交会被**重新**尝试。" % expire_days,
          "",
          "> ⚠️ 真机刷入与「这批产物要不要发」仍由人决定 —— 这条通道**只出 artifact、不碰 release**。"]
    return "\n".join(L) + "\n"


def _failure_block(verdict):
    """失败环节的逐行明细（给 issue 与日志共用）。**空列表也要说清为什么空**。"""
    if not verdict.failures:
        return ["（没有识别出失败的 job —— 见上面的结论行）"]
    L = []
    for f in verdict.failures:
        L.append("job %s：" % f.job + ("conclusion = failure" if f.steps else "job 级失败"))
        if not f.steps:
            L.append("   （这个 job 的每个 step 都没有单独报失败 —— runner 被杀 / 超时等）")
        for s in f.steps:
            L.append("   第 %d 步 %s：" % (s.number, s.name) + "failure")
    return L


def _run_id_of(url):
    return (url or "").rstrip("/").rsplit("/", 1)[-1] or "?"


def failed_comment(a, verdict, artifacts, marker_note, expire_days, count):
    """再次失败时追加的评论（比正文短，但**同样**含上游提交号与失败环节）。

    `count` = 「这是这个上游提交的第几次失败」（正文算第 1 次）；⚠️ 它**只在这个分支上**有意义 ——
    第一版把它挂在 `issue_body()` 上，而那条路径永远只有第 1 次（`existing` 为假才会新建）
    ⇒ 一个**到不了**的分支（code-review 抓到）。
    """
    f = verdict.failures[0] if verdict.failures else None
    L = ["### 同一个上游提交**又失败了一次**（自动追加，第 %d 次）" % count, "",
         "| 项 | 值 |", "|---|---|",
         "| 上游提交 | `%s` |" % a.upstream_sha,
         "| 失败环节 | %s |" % (f.headline() if f else "（未识别）"),
         "| 运行 | [run %s](%s) |" % (_run_id_of(a.run_url), a.run_url),
         "| 产物 | %s |" % ("、".join("`%s`" % x.get("name") for x in artifacts)
                            if artifacts else "（这次没有产物）"),
         "| 失败标记 | %s |" % marker_note,
         "",
         "```"] + _failure_block(verdict) + ["```", "",
         "> ⚠️ 标记的日期**没有重记**（%d 天窗口仍从第一次失败起算）—— 否则一个反复重试的提交"
         "会被永久拉黑，而那正是规格要防的事。" % expire_days]
    return "\n".join(L) + "\n"


def render_summary(r):
    """写进 `$GITHUB_STEP_SUMMARY` 的那一段。**结论要一眼能看见** ——
    一次红的运行里，人打开运行页先看到的就是它。"""
    L = ["## 失败上报（issue #10）", "",
         "| 项 | 值 |", "|---|---|",
         "| 结论 | **%s**（退出码 %d） |" % (r["verdict"], r["exit_code"]),
         "| 说明 | %s |" % r["reason"],
         "| 上游提交 | %s |" % ("`%s`" % r["upstream_sha"] if r["upstream_sha"] else "（未记录）"),
         "| 失败标记 | %s |" % r["marker_line"],
         "| issue | %s |" % r["issue_line"], ""]
    if r["verdict"] == V_JITTER:
        L += ["### 不写标记、不开 issue", "",
              "超时 / 取消 / 启动失败**不是构建失败**（规格实现决定 7）：",
              "偶发的基础设施抖动不该把某个上游提交拉黑 %d 天。" % r["expire_days"],
              "下一轮检测器会**自己重试**同一个提交。", ""]
    elif r["verdict"] == V_NO_UPSTREAM:
        L += ["### 没有上游提交可记", "",
              "这次运行**没有可拉黑的对象**：`--upstream-sha` 为空。",
              "⇒ 没有「哪个上游提交已试过」可记，也没有对应的 issue 标题可去重 ⇒ 什么都不做。",
              "⚠️ **两种成因都会走到这里**（措辞不假定是哪一种）：① **人手动触发** —— "
              "构建器那条路本来就没有上游提交；② **检测器在判定之前就失败了**"
              "（checkout 挂 / API 抖动 ⇒ 它没写出 `upstream_sha`）。",
              "两种都**不该**开 issue、也**不该**拉黑谁：前者的失败是人的事，后者是基础设施抖动。", ""]
    return "\n".join(L) + "\n"


# ══════════════════════════════════════════════════════════════════════════════
#  编排：取证据 → 判定 → 叫人 → 拉黑
# ══════════════════════════════════════════════════════════════════════════════
def fetch_jobs(api, repo, run_id):
    """读这次 run 的 job 列表。

    ⚠️ 满一页**拒绝给结论**：只看见 100 个 job 就宣布「哪些红了」是典型的「不完整的绿」
    （坑表 #32）—— 构建器那条路的 run 是 5 个 job（`prepare` + `build ×2` + `repro` + `notify`），
    检测器那条路是 2 个（`detect` + `notify`）；真要撞上这条线，说明**别的**东西也不对了。
    """
    jobs = api.call("GET", "repos/%s/actions/runs/%s/jobs?per_page=%d" % (repo, run_id, JOBS_PAGE))
    items = (jobs or {}).get("jobs") or []
    if len(items) >= JOBS_PAGE:
        raise RuntimeError("这次 run 的 job 数达到一页上限（%d）—— 「哪几个 job 红了」这条"
                           "**给不出结论**（同 detect.py 的 list_refs）" % JOBS_PAGE)
    return items


def fetch_artifacts(api, repo, run_id):
    """读这次 run 的产物清单。

    ⚠️ 与 `fetch_jobs` 同一条规矩：**满一页就拒绝给结论**。产物 >100 个时，
    issue 里的「失败产物」表会**静默地列不全** —— 而那张表存在的理由正是「事后能去下载」。
    """
    a = api.call("GET", "repos/%s/actions/runs/%s/artifacts?per_page=%d"
                 % (repo, run_id, ARTIFACTS_PAGE))
    items = (a or {}).get("artifacts") or []
    if len(items) >= ARTIFACTS_PAGE:
        raise RuntimeError("这次 run 的产物数达到一页上限（%d）—— 「失败产物有哪些」这条"
                           "**给不出完整结论**（同 fetch_jobs 的规矩）" % ARTIFACTS_PAGE)
    return items


def find_issue(api, repo, title):
    """按**标题精确相等**找已有的 issue（`state=all`，PR 不算）。

    ⚠️ 为什么不用 search API：它有索引延迟 —— 第一轮刚建的 issue，第二轮可能**搜不到**，
    于是开出**第二个**。列 issue 是强一致的。两边都 `.strip()` 再比：标题由我们自己逐字写死，
    但**人不该因为多了一个空格就让去重失效**。（标签也能被人工去掉，所以 `--label` 过滤
    同样不可靠 —— 那是次要理由，主因是索引延迟。）
    """
    for page in range(1, ISSUE_MAX_PAGES + 1):
        items = api.call("GET", "repos/%s/issues?state=all&per_page=%d&page=%d"
                         % (repo, ISSUE_PAGE, page))
        for it in items or []:
            if "pull_request" in it:            # issue 列表里混着 PR
                continue
            if (it.get("title") or "").strip() == title.strip():
                return it
        if len(items or []) < ISSUE_PAGE:
            return None
    raise RuntimeError("issue 列表超过 %d 页 ⇒ 「是不是已经开过」这条**给不出结论**。\n"
                       "    （宁可在这里响亮地失败，也不要开出第二个 issue —— 坑表 #32 同族。）"
                       % ISSUE_MAX_PAGES)


def post_issue(api, repo, title, body, log):
    """开 issue。⚠️ 标签**不是**硬要求：它不存在时不该丢掉一次通知。

    ⚠️ **只在 HTTP 422 上重试**（标签 / 参数校验类错误），别的异常**原样抛出去**。
    第一版是 `except Exception` —— 那在网络超时上会**开出两个 issue**：
    请求可能已经在服务端成功了，只是响应没回来。窄判据的代价只是「标签问题被多试一次」，
    而宽判据的代价是**重复通知**（正是去重要防的东西）。
    抛出去之后：工具退出码 1 ⇒ 一个字节都不写 ⇒ 下一轮重试，而去重按标题仍然有效。
    """
    payload = {"title": title, "body": body, "labels": [ISSUE_LABEL]}
    try:
        return api.call("POST", "repos/%s/issues" % repo, payload)
    except Exception as e:                       # 标签被删 / 改名 / 校验不过
        if "HTTP 422" not in str(e):
            raise
        log("⚠️ 带标签 %r 建 issue 失败（HTTP 422）⇒ 去掉标签重试一次："
            "**一次通知不该因为一个标签丢掉**" % ISSUE_LABEL)
        payload.pop("labels")
        return api.call("POST", "repos/%s/issues" % repo, payload)


def read_state(api, repo, branch, path, log):
    """读心跳分支上的状态文件。返回 `(file_sha, branch_sha, text)`。缺失都是**正常状态**。"""
    ref = api.call("GET", "repos/%s/git/ref/heads/%s" % (repo, qs(branch)), allow_404=True)
    branch_sha = ((ref or {}).get("object") or {}).get("sha")
    if not branch_sha:
        log("心跳分支 %s 还不存在（首次运行正常）" % branch)
        return None, None, None
    doc = api.call("GET", "repos/%s/contents/%s?ref=%s" % (repo, qp(path), qs(branch)),
                   allow_404=True)
    if not doc:
        log("心跳分支 %s 上还没有状态文件（首次运行正常）" % branch)
        return None, branch_sha, None
    return doc.get("sha"), branch_sha, base64.b64decode(doc.get("content") or "").decode("utf-8")


def verify_marker(api, repo, a, sc, upstream_sha, today, log):
    """写完之后**回读**：这份标记真的会让检测器判 `blacklisted` 吗？

    ⚠️ 这一条不是仪式。`failed.upstream` 与上游 HEAD 是**精确相等**比较，
    而 issue 正文里那句「7 天内不会再触发构建」是**对外的承诺** ——
    没有这条回读，「说了会拉黑」就只是一句话。
    （同 #9 的那处 P0：把「要推的 = 编过的」变成一条**会响**的检查，而不是文档里的一句宣称。）
    """
    _, _, text = read_state(api, repo, a.heartbeat_branch, a.state_path, log)
    if not text:
        raise RuntimeError("刚写完失败标记，但 %s 上的状态文件**读不回来** —— "
                           "「7 天内不会再触发构建」这句话没有依据" % a.heartbeat_branch)
    path = os.path.join(tempfile.gettempdir(), "report-failure-verify.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    # ⚠️ 用**消费者自己的代码**（`sync-check.py`）来判，而不是在本文件里另写一遍规则
    oc = sc.check(upstream_sha, upstream_sha, upstream_sha, path, today)
    if not oc.blacklisted:
        raise RuntimeError(
            "回读复核没过：这份标记**不会**让检测器判 blacklisted（它判成了 %s）⇒\n"
            "    issue 里那句「7 天内不会再触发构建」是假的。按「叫不到人就绝不放行」，"
            "这里必须失败。\n"
            "    最可能的原因：写进 `failed.upstream` 的值与上游 HEAD 不是**同一个 sha**"
            "（大小写 / 空白 / 短 sha / 记的是别的提交）。" % oc.state)
    log("✅ 回读复核：这份标记确实会让 sync-check 判 blacklisted（退出码 20）")


def run(a, api, log=print, dm=None, today=None):
    """一次失败上报。返回结果字典（`--json-out` 与 `--summary` 都从它来）。"""
    dm = dm or _load_sibling("dm", "detect.py")
    sc = _load_sibling("sc", "sync-check.py")
    dm.assert_heartbeat_branch(a.work_branch, a.heartbeat_branch)
    dm.assert_state_path(a.state_path)
    today = today or datetime.datetime.utcnow().date()

    # ⚠️ **上游提交是唯一会喂给判据的 sha ⇒ 它必须过形状守卫**（见 `assert_sha`）。
    #    另两个（合并提交 / 合并基点）只进正文与 JSON，是**说明性**的：
    #    对它们硬失败会变成「叫不到人」，那是更坏的失败方向 ⇒ 只做空白归一。
    if a.upstream_sha:
        a.upstream_sha = assert_sha("--upstream-sha", a.upstream_sha)
    for k in ("merge_sha", "merge_base", "base_sha256"):
        setattr(a, k, (getattr(a, k) or "").strip())

    r = {"repo": a.repo, "run_id": a.run_id, "run_url": a.run_url,
         "upstream_sha": a.upstream_sha, "merge_sha": a.merge_sha,
         "merge_base": a.merge_base, "base_sha256": a.base_sha256,
         "work_branch": a.work_branch, "heartbeat_branch": a.heartbeat_branch,
         "state_path": a.state_path, "today": today.isoformat(),
         "dry_run": bool(a.dry_run), "expire_days": sc.EXPIRE_DAYS,
         "marker": None, "marker_changed": False, "marker_note": None, "marker_how": None,
         "issue": None, "issue_action": None,
         "artifacts": [], "verdict": None, "reason": None, "exit_code": EXIT_OK}

    # ── ① 判定：**从 job 列表来**（理由见文件头：run 级的结论这一刻还不存在）──────────
    jobs = fetch_jobs(api, a.repo, a.run_id)
    v = classify(jobs, a.self_job)
    r.update(verdict=v.kind, reason=v.reason,
             failures=[{"job": f.job, "steps": [dict(number=s.number, name=s.name)
                                                for s in f.steps]} for f in v.failures],
             jitter=[{"job": n, "conclusion": c} for n, c in v.jitter],
             pending=list(v.pending))
    log("这次 run 的 %d 个 job：%s" % (len(jobs), "、".join(sorted(j.get("name") or "?" for j in jobs))))
    if v.jitter:
        for n, c in v.jitter:
            log("   ⚠️ job %s = %s" % (n, c))
    for f in v.failures:
        log("   ❌ %s" % f.headline())

    if v.kind == V_JITTER:
        r["marker_line"] = "**没写**（抖动不算失败）"
        r["issue_line"] = "**没开**（抖动不该刷屏）"
        log("")
        log("结论: %s —— %s" % (V_JITTER, v.reason))
        log("      ⇒ 一个字节都不写：不记标记（否则某个提交会被拉黑 7 天）、不开 issue。")
        return r
    if v.kind == V_NO_FAILURE:
        r["marker_line"] = "**没写**（这一轮没有红）"
        r["issue_line"] = "**没开**（没有失败可报）"
        log("")
        log("结论: %s —— %s" % (V_NO_FAILURE, v.reason))
        return r

    # ── ② 没有上游提交 ⇒ 什么都不做 ──────────────────────────────────────────────
    # ⚠️ 这一档**不是**错误：没有「哪个上游提交已试过」可记，也没有稳定的 issue 标题可去重。
    # ⚠️ 两种成因（见 `render_summary()` 里同一条）：**人手动触发**（构建器那条路根本没有
    #    上游提交）；**检测器在判定之前就失败**（checkout 挂 / API 抖动 ⇒ `upstream_sha`
    #    没写出来）。⚠️ 后者靠的是 `detect.py` 把 `--github-output` 写在**最后** ——
    #    谁把它挪早，「一次抖动」就会变成「reported ⇒ 拉黑 7 天」。
    if not r["upstream_sha"]:
        r.update(verdict=V_NO_UPSTREAM,
                 reason="这次运行没有记录上游提交（`--upstream-sha` 为空）："
                        "没有可拉黑的对象、也没有可去重的 issue 标题 ⇒ 不写、不开")
        r["marker_line"] = "**没写**（没有上游提交可记）"
        r["issue_line"] = "**没开**（没有可拉黑的对象 —— 手动触发、或检测器判定前就失败）"
        log("")
        log("结论: %s —— %s" % (V_NO_UPSTREAM, r["reason"]))
        return r

    artifacts = fetch_artifacts(api, a.repo, a.run_id)
    r["artifacts"] = [{"name": x.get("name"), "size_in_bytes": x.get("size_in_bytes"),
                       "expired": bool(x.get("expired"))} for x in artifacts]
    log("产物 %d 个：%s" % (len(r["artifacts"]),
                          "、".join(x["name"] or "?" for x in r["artifacts"]) or "（没有）"))

    file_sha, branch_sha, old_text = read_state(api, a.repo, a.heartbeat_branch, a.state_path, log)
    mk = new_failed_state(old_text, r["upstream_sha"], today, sc.EXPIRE_DAYS)
    r.update(marker=mk.doc, marker_changed=mk.changed, marker_note=mk.note)
    log("失败标记：%s" % mk.note)

    title = issue_title(r["upstream_sha"])
    existing = find_issue(api, a.repo, title)
    n_seen = 1
    if existing:
        n_seen = int(existing.get("comments") or 0) + 2      # 正文 + 已有评论 = 第几次
    r["issue"] = existing.get("number") if existing else None

    if a.dry_run:
        r["marker_line"] = "**没写**（--dry-run）"
        r["issue_line"] = ("追加评论到 #%d" % r["issue"]) if existing else "新建一个 issue"
        log("")
        log("⚠️ --dry-run：上面是完整判据，**一条写请求都没发**。")
        log("    将会 %s；%s"
            % (r["issue_line"],
               "把失败标记写进 %s" % a.heartbeat_branch if mk.changed
               else "**不写**标记（已经有同一条且没过期）"))
        return r

    # ── ③ **先叫人**（见文件头的不变量 ②：宁可不拉黑，也不能安静地停下）──────────────
    if existing:
        n = existing["number"]
        api.call("POST", "repos/%s/issues/%d/comments" % (a.repo, n),
                 {"body": failed_comment(a, v, r["artifacts"], mk.note, sc.EXPIRE_DAYS,
                                         count=n_seen)})
        r["issue_action"] = "commented"
        log("✅ 已把这次失败**追加为评论**到 issue #%d（%s）—— 同一个上游提交只开一个 issue"
            % (n, existing.get("state")))
        if existing.get("state") != "open":
            # 一条**关掉**的通知不是通知：人当时处理完关掉了它，而这次又失败了
            api.call("PATCH", "repos/%s/issues/%d" % (a.repo, n), {"state": "open"})
            log("   ⚠️ 它此前是关闭的 ⇒ 已**重新打开**（关掉的 issue 不会提醒任何人）")
    else:
        body = issue_body(a, v, r["artifacts"], mk.note, sc.EXPIRE_DAYS)
        doc = post_issue(api, a.repo, title, body, log)
        r["issue"] = doc.get("number")
        r["issue_action"] = "created"
        log("✅ 已新建 issue #%s：%s" % (r["issue"], title))

    # ── ④ **后拉黑**：写失败标记（CAS 写路径与 detect.py 共用）─────────────────────
    if mk.changed:
        refs_before = dm.list_refs(api, a.repo)
        how = dm.write_state(api, a.repo, a.heartbeat_branch, a.state_path,
                             dm.state_text(mk.doc), file_sha, branch_sha,
                             message=marker_msg(r["upstream_sha"], sc.EXPIRE_DAYS))
        dm.verify(a, api, refs_before, log)
        r["marker_how"] = how
        r["marker_line"] = ("✅ 已写入 `%s`（%s）：`failed.upstream = %s`，`at = %s`"
                            % (a.heartbeat_branch, how, short(r["upstream_sha"]),
                               mk.doc["failed"]["at"]))
        log("✅ 失败标记已写入 %s（%s）：%s" % (a.heartbeat_branch, how, mk.note))
    else:
        r["marker_line"] = "⏸ **没写**（%s）" % mk.note
        log("⏸ 没有写标记：%s" % mk.note)

    # ── ⑤ 回读复核：**这份标记真的会让检测器判 blacklisted 吗？** ────────────────────
    #    （见 verify_marker 的注释：issue 里那句「7 天内不会再触发构建」是承诺，必须核过。）
    verify_marker(api, a.repo, a, sc, r["upstream_sha"], today, log)

    r["issue_line"] = ("#%s（%s）" % (r["issue"], "新建" if r["issue_action"] == "created"
                                     else "追加评论"))
    log("")
    log("结论: %s —— %s" % (V_REPORTED, v.reason))
    log("      issue %s ｜ 标记 %s" % (r["issue_line"], r["marker_line"]))
    return r


# ══════════════════════════════════════════════════════════════════════════════
#  自测：假 API + 真编排（不联网）
# ══════════════════════════════════════════════════════════════════════════════
def _job(name, conclusion, steps=(), status="completed"):
    return {"name": name, "status": status, "conclusion": conclusion,
            "steps": [{"number": n, "name": s, "status": "completed", "conclusion": c}
                      for n, s, c in steps]}


class FakeApi:
    """假 API：**只认实现真正用到的那些端点**，其余一律抛错（多调一个端点就是实现跑偏）。

    记下每一次调用与每一次写：不变量 ①（只写心跳分支）在自测里是**断言**，不是注释。

    ⚠️ 写进去的内容要**真的落进 `self.state`**：`run()` 末尾有一次**回读复核**
    （`verify_marker()`，用 `sync-check.py` 自己的判据再判一遍）。假 API 若只记下 payload
    而不更新状态，那次回读读到的就是**旧内容**，自测会红在一个假问题上 ——
    而如果为了让它绿去把关掉回读，那条复核就白写了。
    """

    def __init__(self, jobs, artifacts=(), branch_sha="c0mmit", state=None,
                 issues=(), repo="o/r", fail_issue_with_label=False, fail_issue_always=None):
        self.jobs, self.artifacts = jobs, list(artifacts)
        self.branch_sha, self.state = branch_sha, state
        self.issues = list(issues)
        self.repo = repo
        self.fail_issue_with_label = fail_issue_with_label
        self.fail_issue_always = fail_issue_always
        # `posts` 只记**成功**的写（断言「只发了一次评论」用它）；
        # `post_tries` 记**尝试**次数（断言「非 422 不许重试」用它）。
        # ⚠️ `comments` 与 `posts` **分开**：评论与「建 issue」是两条不同的通路，
        #    混在一个列表里会让「只追加了评论」这种断言变成一句空话（code-review 抓到过）。
        self.calls, self.puts, self.posts, self.patches = [], [], [], []
        self.comments = []
        self.post_tries, self.wrote = 0, False

    def call(self, method, path, payload=None, allow_404=False):
        p = path.split("?")[0]
        self.calls.append("%s %s" % (method, p))
        if method == "GET":
            if "/actions/runs/" in p and p.endswith("/jobs"):
                return {"jobs": self.jobs}
            if "/actions/runs/" in p and p.endswith("/artifacts"):
                return {"artifacts": self.artifacts}
            if "/git/refs/heads" in p:                     # 写前/写后的**全部**分支快照
                d = {"refs/heads/ksu-lineage-23.2": "l" * 40, "refs/heads/lineage-23.2": "u" * 40}
                if self.wrote:
                    d["refs/heads/sync-heartbeat"] = "commit-new"
                return [{"ref": k, "object": {"sha": v}} for k, v in d.items()]
            if "/git/trees/" in p:
                return {"tree": [{"path": "sync-state.json", "type": "blob"}]}
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
                return {"sha": "blob-old", "content": _b64(self.state)}
            if p.endswith("/issues") or "/issues?" in path:
                return self.issues
            raise AssertionError("假 API 不认识这个端点：%s %s" % (method, path))
        if method == "POST" and p.endswith("/issues"):
            self.post_tries += 1
            if self.fail_issue_always:
                raise RuntimeError(self.fail_issue_always)
            if self.fail_issue_with_label and payload and payload.get("labels"):
                raise RuntimeError("HTTP 422（模拟：标签不存在 / 无权限）")
            self.posts.append(payload)
            return {"number": 77, "html_url": "https://example/i/77"}
        if method == "POST" and "/comments" in p:
            self.comments.append(payload)               # 与 `posts`（含建 issue）分开记
            return {"id": 1}
        if method == "PATCH" and "/issues/" in p:
            self.patches.append(payload)
            return {"number": int(p.rsplit("/", 1)[-1]), "state": payload.get("state")}
        if method == "PUT" and "/contents/" in p:
            self.puts.append(payload)
            self.wrote = True
            # ⚠️ 内容与**分支**都要真的落进状态（回读复核读的就是它们），见类文档
            self.state = base64.b64decode(payload["content"]).decode("utf-8")
            self.branch_sha = self.branch_sha or "commit-new"
            return {"content": {"sha": "new"}}
        if method == "POST" and p.endswith("/git/blobs"):
            self.state = payload["content"]          # 孤儿分支那条路：blob 就是新状态
            return {"sha": "blob-new"}
        if method == "POST" and p.endswith("/git/trees"):
            return {"sha": "tree-new"}
        if method == "POST" and p.endswith("/git/commits"):
            return {"sha": "commit-new"}
        if method == "POST" and p.endswith("/git/refs"):
            self.wrote = True
            self.branch_sha = "commit-new"           # 孤儿分支建出来了 ⇒ 分支从此存在
            return {"ref": payload["ref"]}
        raise AssertionError("假 API 不认识这个端点：%s %s" % (method, path))


def _b64(text):
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


UP = "7f0ef5269cafd532079dc92347052f28a7a516bf"
MSHA = "8f73efefa343ae57845154d90b2d36c965cfdc9d"
TODAY = datetime.date(2026, 10, 1)
WORK = "ksu-lineage-23.2"


def _args(**kw):
    base = dict(repo="o/r", run_id="12345", run_url="https://example/o/r/actions/runs/12345",
                upstream_sha=UP, merge_sha=MSHA, merge_base="71b13e62" + "0" * 32,
                base_sha256="87" * 32, work_branch=WORK, heartbeat_branch="sync-heartbeat",
                state_path="sync-state.json", self_job="notify", dry_run=False)
    base.update(kw)
    return argparse.Namespace(**base)


def _quiet(*_a, **_kw):
    pass


# 表驱动用例：前六条各自钉住一种**结论**（其中三种是「什么都不写」），后面钉细节。
# ⚠️ 条数**不写在注释里** —— 它已经错过一次（写「22 条」而实际 31 条）。看 `--self-test` 自报的数。
CLASSIFY_CASES = [
    ("① 编译真失败 ⇒ reported",
     [_job("prepare", "success"), _job("build", "failure", [(12, "Build", "failure")]),
      _job("repro", "failure", [(6, "Gate", "failure")])], V_REPORTED),
    ("② job 被取消 ⇒ jitter",
     [_job("prepare", "success"), _job("build", "cancelled"), _job("repro", "cancelled")], V_JITTER),
    ("③ job 超时 ⇒ jitter",
     [_job("prepare", "success"), _job("build", "timed_out"),
      _job("repro", "failure", [(2, "Download", "failure")])], V_JITTER),
    ("④ 启动失败 ⇒ jitter",
     [_job("prepare", "startup_failure")], V_JITTER),
    ("⑤ 全绿 ⇒ no-failure",
     [_job("prepare", "success"), _job("build", "success")], V_NO_FAILURE),
    ("⑥ skipped 不算失败，但另一个 job 真红了 ⇒ 仍要报",
     [_job("prepare", "failure"), _job("build", "skipped")], V_REPORTED),
]
#      ↑ ⑥ 的期望故意写 V_REPORTED：**skipped 既不是失败也不是抖动**（它是「没轮到」），
#        而同一轮里 prepare 真红了 ⇒ 该报。把 skipped 当成「红过」或「没红过」都是错的。


def self_test():
    """离线用例。返回失败条数。

    **测的是编排**：判定（`classify` 与 `new_failed_state` 都是纯函数）与写路径
    （只写心跳分支、先 issue 后标记、写后回读复核、dry-run 一条写请求都不发）。

    ⚠️ **没有 `--self-test-dir`**（同族的 `detect.py` / `sync-check.py` 有）：那两个工具
    的自测要往磁盘上落真文件（marker / 夹具），本工具的自测**全是纯函数 + 假 API** ——
    留一个不收不用的目录参数就是一处「只写不读」，本仓库对那一族点过两次名。
    （唯一落盘的地方是 `verify_marker()` 的临时文件，那是**生产路径**要的，与自测无关。）
    """
    bad, n = 0, 0

    def fail(what, *lines):
        nonlocal bad
        bad += 1
        print("  ❌ #%-2d %s\n       %s" % (n, what, "\n       ".join(str(x) for x in lines)))

    def ok(what, note=""):
        print("  ✅ #%-2d %-46s %s" % (n, what.replace("`", "")[:46], note))

    def case(what, jobs, want, **kw):
        """跑一次 `run()`，断言 `(结论, issue 动作, 是否写了标记)` 与「写过的 ref」。"""
        nonlocal n
        n += 1
        api_kw = {k: kw.pop(k) for k in ("branch_sha", "state", "issues", "artifacts",
                                         "fail_issue_with_label") if k in kw}
        api = FakeApi(jobs, **api_kw)
        try:
            r = run(_args(**kw), api, log=_quiet, today=TODAY)
        except Exception as e:
            fail(what, "抛异常：%r" % (e,))
            return None, api
        got = (r["verdict"], r["issue_action"], r["marker_changed"])
        want3 = (want[0], want[1], want[2])
        if got != want3:
            fail(what, "期望 %s，实际 %s" % (want3, got))
            return r, api
        # 不变量 ①：工作分支**只读**（假 API 记下了每一次调用，逐条查）
        for c in api.calls:
            if not c.startswith("GET") and WORK in c:
                fail(what, "对工作分支做了非 GET 调用：%s" % c)
                return r, api
        for payload in api.puts:
            if payload.get("branch") != "sync-heartbeat":
                fail(what, "写到了非心跳分支：%r" % payload.get("branch"))
                return r, api
        ok(what, "-> %s" % (got,))
        return r, api

    print("失败上报自测（假 API，不联网）：判定 / 标记 / 编排三类，逐条断言")
    print()

    # ── 判定：六种 job 组合 ────────────────────────────────────────────────────
    for what, jobs, want in CLASSIFY_CASES:
        n += 1
        v = classify(jobs, "notify")
        if v.kind != want:
            fail(what, "期望 %s，实际 %s（%s）" % (want, v.kind, v.reason))
        else:
            ok(what, "-> %s" % v.kind)

    # ── 本 job 还在跑时，**它的失败步骤不能被漏掉** ──
    # ⚠️ 这一档**只对「上报与失败在同一个 job」的旧形状成立**。本仓库两条路都已改成
    #    **独立 job**（检测器那条路自 issue #17 起）⇒ **生产上走不到这里**。
    #    用例留着，是为了 `--self-job` 这个兼容参数还有回归网（详见 `classify()` 的 docstring）。
    n += 1
    v = classify([_job("detect", None, [(3, "检测", "success"), (5, "试合并", "failure")],
                       status="in_progress")], "detect")
    if v.kind != V_REPORTED or v.failures[0].steps[0].name != "试合并":
        fail("⑦ 自己的 job 还在跑 ⇒ 仍要认出它的失败步骤", v)
    else:
        ok("⑦ 自己的 job 还在跑 ⇒ 仍要认出失败步骤", "-> %s" % v.failures[0].headline())

    n += 1
    v = classify([_job("detect", None, [(3, "检测", "success")], status="in_progress")], "detect")
    if v.kind != V_NO_FAILURE or v.pending:
        fail("⑧ 自己的 job 还在跑但没有失败步骤 ⇒ 不报", v)
    else:
        ok("⑧ 自己的 job 还没红 ⇒ no-failure")

    n += 1
    v = classify([_job("build", None, [], status="in_progress")], "notify")
    if v.kind != V_NO_FAILURE or list(v.pending) != ["build"]:
        fail("⑨ 别的 job 还没结束 ⇒ 明说「没被计入」", v)
    else:
        ok("⑨ 别的 job 还没结束 ⇒ 进 pending 并说明", "-> %s" % v.reason[-24:])

    # ── 标记（纯函数）：日期是**粘的** ─────────────────────────────────────────
    cases = [
        ("⑩ 首次失败 ⇒ 写今天的日期",
         None, UP, True, "首次"),
        ("⑪ 同一提交、没过期 ⇒ **不重记日期**（7 天窗口从第一次起算）",
         '{"failed": {"upstream": "%s", "at": "2026-09-28"}, "heartbeat": {"at": "2026-09-27"}}'
         % UP, UP, False, "不重记"),
        ("⑫ 同一提交、已过期（8 天）⇒ 重新记一次",
         '{"failed": {"upstream": "%s", "at": "2026-09-23"}, "heartbeat": {"at": "2026-09-27"}}'
         % UP, UP, True, "过期"),
        ("⑬ 换了一个上游提交 ⇒ 换成新的",
         '{"failed": {"upstream": "%s", "at": "2026-09-30"}, "heartbeat": {"at": "2026-09-27"}}'
         % ("9" * 40), UP, True, "别的"),
        ("⑭ 旧标记写坏 ⇒ 丢弃并自愈",
         '{"failed": "oops", "heartbeat": {"at": "2026-09-27"}}', UP, True, "自愈"),
        ("⑮ 保留 heartbeat（本工具不写心跳）",
         '{"failed": null, "heartbeat": {"at": "2026-09-27", "upstream": "%s"}}' % ("8" * 40),
         UP, True, "保留"),
        # ⚠️ `at` 写坏 + 同一个上游提交：`sync-check` 对同样的坏法判「损坏 ⇒ **不拉黑**」，
        #    所以这里必须**重新起算**（而措辞不能说成「已过期」—— 那不是事实）。
        ("⑮b 同一提交但 `at` 读不出 ⇒ 重新起算（不许说「已过期」）",
         '{"failed": {"upstream": "%s", "at": "2026/09/27"}, "heartbeat": {"at": "2026-09-27"}}'
         % UP, UP, True, "读不出"),
    ]
    for what, old, up, want_changed, keyword in cases:
        n += 1
        try:
            mk = new_failed_state(old, up, TODAY, 7)
        except Exception as e:
            fail(what, "抛异常：%r" % (e,))
            continue
        problems = []
        if mk.changed != want_changed:
            problems.append("changed 应为 %s，实际 %s（%s）" % (want_changed, mk.changed, mk.note))
        if mk.doc["failed"]["upstream"] != up:
            problems.append("`failed.upstream` 不是这次的上游提交：%r" % mk.doc["failed"])
        if keyword not in mk.note:
            problems.append("说明里没提到 %r：%s" % (keyword, mk.note))
        if old and '"heartbeat"' in old and "heartbeat" not in mk.doc:
            problems.append("把 `heartbeat` 弄丢了：%r" % mk.doc)
        if problems:
            fail(what, *problems)
        else:
            ok(what, "-> changed=%s %s" % (mk.changed, mk.note[:26]))

    # ── 编排：叫人 + 拉黑 ─────────────────────────────────────────────────────
    FAIL = [_job("prepare", "success"), _job("build", "failure", [(12, "Build", "failure")])]
    HB = '{"failed": null, "heartbeat": {"at": "2026-09-27", "upstream": "%s"}}' % ("8" * 40)

    r, api = case("⑯ 真失败 + 没有 issue ⇒ 新建 issue、写标记", FAIL,
                  (V_REPORTED, "created", True), branch_sha="c0mmit", state=HB,
                  artifacts=[{"name": "los23.2-umi-x-a", "size_in_bytes": 10, "expired": False}])
    n += 1
    if r is None:
        pass
    else:
        body = api.posts[0]["body"]
        problems = [w for w in ("上游提交", UP[:12], "失败环节", "第 12 步", "Build",
                                "los23.2-umi-x-a", "7 天") if w not in body]
        if problems:
            fail("⑯ 附属：issue 正文必须含上游提交号与失败环节", "缺：%s" % problems)
        else:
            ok("⑯ 附属：正文含上游提交号与失败环节", "（%d 字节）" % len(body))

    # ★ 标题的字面形状：**不许用 `issue_title()` 现算**（那会夹具与实现同源：
    #   标题哪天退化成不含 sha，去重就变成「所有上游共用一个 issue」，而用例照样全绿）。
    n += 1
    want_title = "[上游同步] 失败：上游 %s" % UP[:12]
    if issue_title(UP) != want_title or api.posts[0]["title"] != want_title:
        fail("⑯b 标题必须是 `[上游同步] 失败：上游 <12 位短 sha>`（字面量钉住）",
             "issue_title=%r，实际 %r" % (issue_title(UP), api.posts[0].get("title")))
    else:
        ok("⑯b 标题形状（字面量，不靠现算）", "-> %s" % want_title)

    # ★ 写标记那次提交的提交信息：`message=` 这个参数存在的**全部理由**就是它 ——
    #   不钉住的话，实现哪天退回「心跳那条信息」，31 条用例照样全绿（code-review 抓到）。
    n += 1
    msg = (api.puts[0].get("message") or "") if api.puts else ""
    if UP[:12] not in msg or "拉黑" not in msg or "7 天" not in msg:
        fail("⑯c 写标记的提交信息要说清「拉黑了谁、多久过期」", "实际：%r" % msg[:80])
    else:
        ok("⑯c 提交信息含短 sha + 拉黑 + 7 天", "-> %s" % msg.split("\n")[0][:34])

    r, api = case("⑰ 已有同名 issue（开着）⇒ **只追加评论**，不新建", FAIL,
                  (V_REPORTED, "commented", True), branch_sha="c0mmit", state=HB,
                  issues=[{"number": 12, "title": issue_title(UP), "state": "open", "comments": 0}])
    n += 1
    # ⚠️ 断言要钉在**评论那条通路**上：第一版写的是 `"issues" not in api.calls[-1]`，
    #    而那一刻最后一条调用是写标记之后的 `GET /git/trees/…` —— 一个恒真的空断言
    #    （即使真开出第二个 issue 也照样通过）。见自测里那份「空集合 ≠ 没给条件」的教训（坑表 #35）。
    if r is None:
        pass
    elif len(api.comments) == 1 and not any("POST repos/o/r/issues" == c for c in api.calls):
        cbody = api.comments[0]["body"]
        miss = [w for w in (UP[:12], "第 12 步", "Build", "第 2 次") if w not in cbody]
        if miss:
            fail("⑰ 附属：评论正文也要含上游提交号与失败环节", "缺：%s" % miss)
        else:
            ok("⑰ 附属：恰好一条评论，且含 sha + 失败环节 + 第 N 次", "-> issue #%s" % r["issue"])
    else:
        fail("⑰ 附属：应当**恰好一次**评论、且不新建 issue",
             "comments=%d，calls=%s" % (len(api.comments), api.calls[-2:]))

    case("⑱ 已有同名 issue（**关着**）⇒ 追加评论并重新打开", FAIL,
         (V_REPORTED, "commented", True), branch_sha="c0mmit", state=HB,
         issues=[{"number": 12, "title": issue_title(UP), "state": "closed", "comments": 2}])

    n += 1
    api = FakeApi(FAIL, branch_sha="c0mmit", state=HB,
                  issues=[{"number": 12, "title": issue_title(UP), "state": "closed"}])
    run(_args(), api, log=_quiet, today=TODAY)
    if [p.get("state") for p in api.patches] != ["open"]:
        fail("⑱ 附属：关掉的 issue 必须被重新打开", api.patches)
    else:
        ok("⑱ 附属：PATCH 把它改回 open")

    case("⑲ 同一提交、标记没过期 ⇒ **不写**（省一次 CAS 写），但仍叫人", FAIL,
         (V_REPORTED, "created", False),
         branch_sha="c0mmit",
         state='{"failed": {"upstream": "%s", "at": "2026-09-28"}, "heartbeat": {"at": "2026-09-27"}}' % UP)

    case("⑳ 抖动（被取消）⇒ **不写标记、不开 issue**", [_job("build", "cancelled")],
         (V_JITTER, None, False), branch_sha="c0mmit", state=HB)
    case("㉑ 抖动（超时）⇒ 同上，即使另有一个 job 报 failure", 
         [_job("build", "timed_out"), _job("repro", "failure", [(2, "Download", "failure")])],
         (V_JITTER, None, False), branch_sha="c0mmit", state=HB)
    case("㉒ 没有上游提交（手动触发 / 检测器判定前就失败）⇒ 不写、不开", FAIL,
         (V_NO_UPSTREAM, None, False), upstream_sha="", branch_sha="c0mmit", state=HB)

    # ── dry-run：判据全跑，**一条写请求都不发** ────────────────────────────────
    n += 1
    api = FakeApi(FAIL, branch_sha="c0mmit", state=HB)
    r = run(_args(dry_run=True), api, log=_quiet, today=TODAY)
    if api.puts or api.posts or api.patches:
        fail("㉓ --dry-run 必须一条写请求都不发", api.puts, api.posts, api.patches)
    elif r["verdict"] != V_REPORTED:
        fail("㉓ --dry-run 仍要跑完判据", r["verdict"])
    else:
        ok("㉓ --dry-run ⇒ 判据跑完、零写请求", "-> %s" % r["issue_line"])

    # ── 标签不存在时，通知不能被丢掉（去掉标签重试一次）─────────────────────────
    n += 1
    api = FakeApi(FAIL, branch_sha="c0mmit", state=HB, fail_issue_with_label=True)
    try:
        r = run(_args(), api, log=_quiet, today=TODAY)
        if r["issue_action"] != "created" or api.posts[0].get("labels") or api.post_tries != 2:
            fail("㉔ 标签不可用 ⇒ 去掉标签重试（一次通知不该因标签丢掉）", api.posts, api.post_tries)
        else:
            ok("㉔ 标签不可用 ⇒ 去掉标签重试一次", "-> issue #%s" % r["issue"])
    except Exception as e:
        fail("㉔ 标签不可用 ⇒ 去掉标签重试（一次通知不该因标签丢掉）", "抛异常：%r" % (e,))

    # ── 但**非 422** 的失败不许重试：那会在网络超时上开出第二个 issue ──────────────
    n += 1
    api = FakeApi(FAIL, branch_sha="c0mmit", state=HB, fail_issue_always="HTTP 500 服务端炸了")
    try:
        run(_args(), api, log=_quiet, today=TODAY)
        fail("㉔b 非 422 的建 issue 失败 ⇒ **不许**重试", "没抛异常")
    except Exception as e:
        if api.post_tries != 1 or "500" not in str(e):
            fail("㉔b 非 422 的建 issue 失败 ⇒ 不重试、原样抛出",
                 "尝试 %d 次；异常 %r" % (api.post_tries, e))
        else:
            ok("㉔b 非 422（500）⇒ 不重试、原样抛出", "尝试 1 次")

    # ── 分支 / 文件都不存在（首次）：走孤儿提交那条路 ──────────────────────────
    n += 1
    api = FakeApi(FAIL, branch_sha=None, state=None)
    try:
        r = run(_args(), api, log=_quiet, today=TODAY)
        if any(c == "POST repos/o/r/git/refs" for c in api.calls):
            ok("㉕ 分支与状态文件都不存在 ⇒ 建孤儿分支", "-> %s" % r["marker_line"][:34])
        else:
            fail("㉕ 首次（无分支）应走孤儿提交那条路", api.calls)
    except Exception as e:
        fail("㉕ 首次（无分支）应走孤儿提交那条路", "抛异常：%r" % (e,))

    # ── 名字守卫：联网之前就该拒绝（与 detect.py 共用同一个函数）───────────────
    for what, hb in (("㉖ 心跳分支 == 工作分支 ⇒ 拒绝", WORK),
                     ("㉗ 心跳分支带 `refs/` 前缀 ⇒ 拒绝", "refs/heads/ksu-lineage-23.2")):
        n += 1
        try:
            run(_args(heartbeat_branch=hb), FakeApi(FAIL), log=_quiet, today=TODAY)
            fail(what, "没拒绝")
        except Exception as e:
            if type(e).__name__ == "UsageError":
                ok(what, "-> UsageError")
            else:
                fail(what, "抛的是 %r" % (e,))

    total = n
    print()
    print("结果: %s（%d/%d 通过）" % ("失败" if bad else "全部通过", total - bad, total))
    return bad


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    # ⚠️ 先加载 `detect.py`：下面两个默认值要从**它**取（心跳分支名与状态文件路径在那边定义）。
    #    各自再写一份字面量＝同一个事实两处各记一份 —— 而本文件已经在从 `sync-check.py` 取
    #    `EXPIRE_DAYS`、从 `detect.py` 取 `Api`/`write_state`，这里没有理由例外。
    dm = _load_sibling("dm", "detect.py")
    ap = argparse.ArgumentParser(
        add_help=True, description="失败上报（红了叫人与拉黑，见文件头）",
        epilog="退出码：0 已处置（四种结论都是正常结局）/ 1 运行错误 / 2 用法错误")
    ap.add_argument("--repo", metavar="OWNER/REPO", help="工作仓库（心跳分支在它上面）")
    ap.add_argument("--run-id", metavar="ID", help="出问题的那次 run 的数字 id")
    ap.add_argument("--run-url", metavar="URL", help="那次运行的 URL（写进 issue 与摘要）")
    ap.add_argument("--upstream-sha", metavar="SHA", default="",
                    help="这次构建对应的**上游提交**（拉黑的对象，必须 40 位；留空 = 手动触发的运行）")
    ap.add_argument("--merge-sha", metavar="SHA", default="", help="那次试合并的合并提交（进正文）")
    ap.add_argument("--merge-base", metavar="SHA", default="", help="那次合并的基点（进正文）")
    ap.add_argument("--base-sha256", metavar="SHA", default="", help="那次用的底包 sha256（进正文）")
    ap.add_argument("--work-branch", metavar="分支", default="ksu-lineage-23.2",
                    help="工作分支（**只读它**，写它一次都算事故）")
    ap.add_argument("--heartbeat-branch", metavar="分支", default=dm.HEARTBEAT_BRANCH,
                    help="心跳分支（失败标记写在它的状态文件里；默认取 detect.py 的常量）")
    ap.add_argument("--state-path", metavar="路径", default=dm.STATE_PATH,
                    help="心跳分支上的状态文件（默认取 detect.py 的常量）")
    ap.add_argument("--self-job", metavar="名字", default=os.environ.get("GITHUB_JOB"),
                    help="**本工具自己所在的 job**（它的结论此刻还是 null，不能漏掉它的失败步骤；"
                         "CI 上默认取 GITHUB_JOB，两个 workflow 因此**不必**各记一份 job 名）")
    ap.add_argument("--today", metavar="YYYY-MM-DD", help="「今天」（默认取 UTC 当天）")
    ap.add_argument("--dry-run", action="store_true", help="判据全跑，**一条写请求都不发**")
    ap.add_argument("--json-out", metavar="文件", help="把结果写一份 JSON")
    ap.add_argument("--summary", metavar="文件", help="追加一段 Markdown（喂 $GITHUB_STEP_SUMMARY）")
    ap.add_argument("--self-test", action="store_true", help="离线跑内置用例（不联网、不读参数）")
    a = ap.parse_args(argv[1:])

    if a.self_test:
        return 1 if self_test() else 0

    missing = [n for n in ("repo", "run_id", "run_url") if not getattr(a, n)]
    if missing:
        print("缺必填参数: %s" % ", ".join("--" + m.replace("_", "-") for m in missing))
        return EXIT_USAGE

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token and not a.dry_run:
        print("⚠️  没有 GH_TOKEN / GITHUB_TOKEN：读接口是公开的，但**写与开 issue 会 403**。")
        print("    只想知道结论就加 --dry-run。")
    api = dm.Api(token)
    try:
        today = (datetime.date.fromisoformat(a.today) if a.today
                 else datetime.datetime.utcnow().date())
    except ValueError:
        print("--today 不是 YYYY-MM-DD：%r" % a.today)
        return EXIT_USAGE
    try:
        r = run(a, api, log=print, dm=dm, today=today)
    except dm.UsageError as e:
        print("用法错误：%s" % e)
        return EXIT_USAGE
    except (dm.ApiError, RuntimeError) as e:
        print("❌ 失败上报本身出错：%s" % e)
        print("    （退出码 %d = 运行错误。⚠️ 这时**可能**已经开过 issue 但没写标记 —— "
              "顺序是先叫人后拉黑，见文件头的不变量 ②）" % EXIT_RUN_ERROR)
        return EXIT_RUN_ERROR

    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(r, f, ensure_ascii=False, indent=1, sort_keys=True)
            f.write("\n")
    if a.summary:
        with open(a.summary, "a", encoding="utf-8", newline="\n") as f:
            f.write(render_summary(r))
    return r["exit_code"]


if __name__ == "__main__":
    sys.exit(main(sys.argv))
