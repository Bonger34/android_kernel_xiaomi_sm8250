#!/usr/bin/env python3
"""
漂移判定：喂进「上游 SHA / 本地 SHA / 标记内容 / 今天日期」，吐出一个结论。

对应 `docs/los-line/upstream-sync-spec.md` 的接缝 **S3**（实现决定 8 与测试决定 S3）。
**纯决策、零 I/O、零网络** —— 所有输入都从命令行来，所以几十个用例可以离线跑完
（`--self-test`）。这正是把它从 workflow 的 YAML 里拆出来的理由：混在一起，
决策逻辑就必须联网才能测。

## 三个输入量的语义

| 输入 | 含义 |
|---|---|
| `--upstream` | 上游分支 HEAD 的 sha |
| `--local` | 我们跟随分支 HEAD 的 sha |
| `--merge-base` | 两者的 merge-base（`gh api .../compare/A...B` 的 `merge_base_commit.sha`） |
| `--marker` | 心跳分支上的状态文件（JSON，见下）；不存在或缺省 = 没有任何标记 |

⚠️ **为什么不能只比 `--upstream` 与 `--local` 是否相等**：我们的分支永远带着自己的提交
（KSU 集成 + CI + 修复合计 5~8 个），所以**它在正常情况下也永远不等于上游 HEAD**。
「有没有漂移」的真实判据是 **merge-base 是不是上游 HEAD** ——
即上游那个 HEAD 是否已经在我们的历史里。实测依据见
`artifacts/tmp-drift-probe/upstream-drift-report.md` §2（7 条分支 `behind_by` 全为 0、
`merge_base == 上游 HEAD`）。`--merge-base` 因此是**必填**，本工具不自己算祖先关系
（那要读对象库／联网）。

## 标记文件（`--marker`）的格式与语义

心跳分支上的一个 JSON 文件同时承载两件事：**失败标记**（哪个上游提交已被拉黑）
与**心跳时间**（上次写心跳是哪天）。缺字段一律当真值为空。

```json
{
  "failed": {"upstream": "<40 位 sha>", "at": "2026-09-25"},
  "heartbeat": {"at": "2026-09-25"}
}
```

| 规则 | 取值 | 依据 |
|---|---|---|
| 失败标记有效期 | **7 天**（`--expire-days`） | 规格「实现决定 7」：标记 7 天自动过期，防止某个提交被永久拉黑 |
| 心跳间隔 | **30 天**（`--heartbeat-days`） | 规格「实现决定 3」：对 GitHub 的 60 天计时器留 2 倍余量 |
| 过期判据 | `(今天 − 标记日期) >= 天数` | 即 7 天窗口是 `[标记日, 标记日+7)` |

⚠️ **只对「同一个上游提交」拉黑**（规格原文：「同一个上游提交失败后不再重试」）。
`failed.upstream` 与 `--upstream` 不同 ⇒ 这个标记与本次无关，**不算拉黑**。

⚠️ **标记写坏时的行为：按「无标记」处理，并说清是哪一种坏。**
理由：一个读不出来的状态文件不能让整条通道停摆 —— 最坏后果只是多试一次已被拉黑的提交
（构建失败 + 开 issue 都有幂等去重），而没有这个兜底，损坏的文件会**永久卡死每一次检测**。

⚠️ **但「按无标记处理」这条只对结论真正依赖的那一半成立**（这一条是 code-review 抓出来的，
第一版就是反的）：两个字段的权重不同 —— **`failed` 决定拉黑，`heartbeat` 只决定该不该写心跳**。

| 坏在哪 | 判定 | 为什么 |
|---|---|---|
| `failed` 坏（或整个文件读不出来/不是 JSON） | `损坏` ⇒ **不拉黑**，按无标记继续 | 拿不出可信的「这个提交已试过」，就不该阻止重试 |
| 只有 `heartbeat` 坏、`failed` 完好 | **`有效`** ⇒ 拉黑照旧生效，心跳按「从未记录」算 | 拉黑完全由 `failed` 决定；若在这里改判「按无标记」，输出就会**说一套做一套**（说会重试、退出码却是 20）—— 那比坏掉本身更坏，它让日志说谎 |

⇒ 不变量：**只要输出里打印了「按无标记处理」，结论就不可能是 `blacklisted`**。自测里有专门断言。

## 四选一的结论与退出码

按**优先级**判定，只给一个结论（规格「测试决定 S3」：四选一）：

| 优先级 | 结论 | 退出码 | 含义 |
|---|---|---|---|
| 1 | `blacklisted` | **20** | 上游 HEAD 已试过且失败，标记未过期 ⇒ 今天不做 |
| 2 | `drifted` | **10** | merge-base ≠ 上游 HEAD ⇒ 有漂移，该试合并 + 构建 |
| 3 | `heartbeat-due` | **30** | 心跳到期 ⇒ 该写心跳 |
| 4 | `in-sync` | **0** | merge-base = 上游 HEAD ⇒ 无漂移，今天到此为止 |

⚠️ **心跳排在漂移后面，是因为它只有「该写一个心跳提交」这一个动作**，而且规格里
心跳与漂移是两件**互不相干**的事：有漂移时把那次构建跑好就是全部，顺带写心跳只多一个提交。
反过来若把心跳排在前面，一台长期无人看管的机器会**只剩心跳、永不构建**
—— 那恰好把这条通道的目的（跟上上游）丢掉了。心跳不会被漏掉：没有漂移的那些天
（常态）一律会走到它。

> 首次运行的状态文件里两件事都没有 ⇒ 判 2（有漂移就构建），而不是判 3。
> 心跳的后果只是晚一天写，没有代价；而「有漂移却不构建」是有代价的。

⚠️ **退出码不是「0 成功 / 非 0 失败」** —— 上表四个值都是**正常结论**。
调用方（检测器 workflow）按数值分支取动作，**不要写成「非零即报错」**：
那样一次正常的「有漂移」会被当成工具崩了。

**一个正交输出**：心跳到期与否是**独立的**事实，所以结论后面另有一行
`心跳:` —— 判 1/2 时它可能是 `到期`。需要它的调用方不必为此再跑一次判定。

## 用法

```sh
python tools/sync-check.py --upstream <sha> --local <sha> --merge-base <sha> [选项]
python tools/sync-check.py --self-test          # 离线表驱动，覆盖验收要求的全部场景
```

选项：`--marker <文件>` `--today <YYYY-MM-DD>` `--expire-days <n>` `--heartbeat-days <n>`
      `--upstream-subject <文本>`（只进输出，给 issue 标题用）`--json`
      `--self-test`（跑内置用例）`--self-test-dir <目录>`（自测落临时文件的位置）

退出码：0 / 10 / 20 / 30 = 上表四种结论；2 = 用法错误（缺必填参数等）。

⚠️ **本文件有**两份，必须逐字节相同**：工作区的 `tools/sync-check.py`，与 LOS 工作仓库里的
`.github/scripts/sync-check.py`（CI 跑**后者** —— 内核树自带一个 `tools/`，那是上游的，
项目自己的 CI 工具一律放 `.github/`，同 `repack-boot.py`）。
镜像与复验：`python tools/sync-ci-scripts.py --gen` / `--check`（改一份就同步改另一份）。
"""

import argparse
import datetime
import json
import os
import sys
from typing import NamedTuple

# ── 两个天数都是**规格里的决定**，不是随手取的常数（见文件头表格）─────────────
EXPIRE_DAYS = 7
HEARTBEAT_DAYS = 30

# 四选一。数值是退出码 —— 用有语义的数，不用「0/1」把四种结论压成两种。
ID_IN_SYNC, ID_DRIFTED, ID_BLACKLISTED, ID_HEARTBEAT = "in-sync", "drifted", "blacklisted", "heartbeat-due"
EXIT = {ID_IN_SYNC: 0, ID_DRIFTED: 10, ID_BLACKLISTED: 20, ID_HEARTBEAT: 30}
EXIT_USAGE = 2

NO_MARKER = "无标记"
MARKER_OK = "有效"
MARKER_EXPIRED = "已过期"
MARKER_BROKEN = "损坏"
MARKER_ABSENT = "不存在"
MARKER_NO_FAILURE = "无失败条目"


def parse_date(text, what):
    """严格 `YYYY-MM-DD`。**不接受 `2026-9-5`** —— 状态文件是机器写的，宽松解析只会掩盖写坏的值。"""
    try:
        return datetime.date.fromisoformat(text)
    except (ValueError, TypeError):
        raise ValueError("%s 不是 YYYY-MM-DD：%r" % (what, text))


def _one_state(doc, today, expire_days):
    """解析状态文件里的**两个字段**，返回 `(state, 错误列表)`。错误项形如 `("failed", 说明)`。

    ⚠️ 两个字段**各自独立解析**：`failed` 写坏了不代表 `heartbeat` 也读不出来。
    一个字段坏掉就连另一个的信息一起丢掉，会让「心跳该不该写」这种与失败无关的判断跟着失真
    （实测抓到的：原先把两者串在一起解析，一处损坏就把心跳一起抹掉了）。
    调用方（`load_marker`）再按**是哪一个字段坏了**决定整体判定 —— 因为两个字段的
    结论权重并不相同（`failed` 决定拉黑，`heartbeat` 只决定「该不该写心跳」）。
    """
    state = dict(failed_sha=None, failed_at=None, failed_age=None, expired=None, heartbeat_at=None)
    errs = []

    failed = doc.get("failed")
    if failed is not None:
        if not isinstance(failed, dict):
            errs.append(("failed", "`failed` 不是对象"))
        else:
            sha, at = failed.get("upstream"), failed.get("at")
            if not sha or not at:
                errs.append(("failed", "`failed` 缺 `upstream` 或 `at`"))
            else:
                try:
                    fd = parse_date(at, "failed.at")
                    age = (today - fd).days
                    state.update(failed_sha=str(sha).lower(), failed_at=fd, failed_age=age,
                                 expired=age >= expire_days)
                except ValueError as e:
                    errs.append(("failed", str(e)))

    hb = doc.get("heartbeat")
    if hb is not None:
        if not isinstance(hb, dict):
            errs.append(("heartbeat", "`heartbeat` 不是对象"))
        elif not hb.get("at"):
            errs.append(("heartbeat", "`heartbeat` 缺 `at`"))
        else:
            try:
                state["heartbeat_at"] = parse_date(hb["at"], "heartbeat.at")
            except ValueError as e:
                errs.append(("heartbeat", str(e)))
    return state, errs


def load_marker(path, today, expire_days):
    """读标记文件，返回 `(分类, 状态字典, 说明)`。

    分类：`无标记`（压根没有/空文件/两件事都没记）、`有效`、`已过期`、`损坏`。
    **本函数只判断「标记本身的状态」，不判断「与本次上游提交有没有关系」** ——
    后者要拿 upstream 去比，在 `check()` 里做，这样两件事各自可测。

    损坏时**仍返回能解析出来的那一半**（见 `_one_state`），`check()` 会按「无标记」继续。
    """
    if not path:
        return NO_MARKER, {}, "未传 --marker"
    if not os.path.exists(path):
        # 首次运行时的**正常**状态，不是错误：还没失败过，自然没有标记
        return NO_MARKER, {}, "%s 不存在（首次运行时正常）" % path
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        return MARKER_BROKEN, {}, "读不出 %s：%s" % (path, e)
    if not raw.strip():
        return NO_MARKER, {}, "%s 是空文件" % path
    try:
        doc = json.loads(raw)
    except ValueError as e:
        return MARKER_BROKEN, {}, "%s 不是合法 JSON：%s" % (path, e)
    if not isinstance(doc, dict):
        return MARKER_BROKEN, {}, "%s 的顶层不是对象（%s）" % (path, type(doc).__name__)

    state, errs = _one_state(doc, today, expire_days)
    if errs:
        if state["failed_sha"] is not None:
            # ⚠️ `heartbeat` 写坏**不影响** `failed` 的判定 —— 两个字段是独立的，而
            #    「同一个提交已拉黑」这条结论**完全由 failed 决定**。若在这里把整个标记
            #    判成「损坏 → 按无标记处理」，就会**说一套做一套**：输出说会重试、
            #    退出码却是 20（今天不做）。那比损坏本身更坏 —— 它让日志说谎。
            #    代价（心跳时间戳丢了 ⇒ 判成「该写心跳了」，见 check()）是良性的。
            return MARKER_OK, state, ("`heartbeat` 写坏（%s）⇒ 心跳按「从未记录」算；"
                                      "`failed` 完好，拉黑判定不受影响"
                                      % "；".join(m for _f, m in errs))
        return MARKER_BROKEN, state, "%s 里 %s" % (path, "；".join(m for _f, m in errs))
    if state["failed_sha"] is None and state["heartbeat_at"] is None:
        # 合法 JSON，但两件事都没记 —— 当作「没有标记」，不是损坏
        return MARKER_NO_FAILURE, state, "%s 里既无 failed 也无 heartbeat" % path
    if state["failed_sha"] is not None:
        if state["expired"]:
            return MARKER_EXPIRED, state, "标记 %s 天前（>= %d 天）⇒ 失效" % (state["failed_age"], expire_days)
        note = "标记于 %s（%s）" % (state["failed_at"], state["failed_sha"][:12])
    else:
        note = "只有心跳记录，没有失败标记"
    return MARKER_OK, state, note


def heartbeat_due(heartbeat_at, today, days):
    """心跳是否到期。**没有记录过 = 到期**（第一次运行就该写一个，让计时器从今天起算）。"""
    if heartbeat_at is None:
        return True, None
    age = (today - heartbeat_at).days
    return age >= days, age


class Outcome(NamedTuple):
    """`check()` 的返回值。**具名字段**而不是裸 dict —— 它有 14 项，字符串键散在
    三处（`render` / `main` / 自测）取值，打错一个键要到运行时才发现。
    仍然是 tuple，所以 `--json` 与解包用法都不受影响（`_asdict()` 直接喂 json）。
    """
    state: str
    exit: int
    reason: str
    marker: str
    marker_note: str
    blacklisted: bool
    drift: bool
    heartbeat_due: bool
    heartbeat_age_days: int
    upstream: str
    local: str
    merge_base: str
    failed_sha: str
    failed_age_days: int


def check(upstream, local, merge_base, marker_path, today, expire_days=EXPIRE_DAYS,
          heartbeat_days=HEARTBEAT_DAYS):
    """纯决策核心。返回 `Outcome`（不打印、不退出），便于表驱动测试与 `--json`。"""
    upstream = (upstream or "").lower()
    local = (local or "").lower()
    merge_base = (merge_base or "").lower()

    kind, state, note = load_marker(marker_path, today, expire_days)
    # 「损坏 / 不存在」都按**无标记**继续（理由见文件头），但把实情留在 `marker` 字段里
    failed_sha = state.get("failed_sha")
    blacklisted = bool(failed_sha) and failed_sha == upstream and not state.get("expired")

    due, hb_age = heartbeat_due(state.get("heartbeat_at"), today, heartbeat_days)
    drift = merge_base != upstream

    if blacklisted:
        ident, reason = ID_BLACKLISTED, ("上游 %s 已于 %s 试过并失败（%s 天前，%d 天窗口未过）"
                                         % (upstream[:12], state["failed_at"], state["failed_age"], expire_days))
    elif drift:
        ident = ID_DRIFTED
        reason = "merge-base %s ≠ 上游 HEAD %s ⇒ 上游有我们没跟上的提交" % (merge_base[:12], upstream[:12])
        if due:
            reason += "；顺带该写心跳（%s）" % ("从未写过" if hb_age is None else "%d 天前" % hb_age)
    elif due:
        ident = ID_HEARTBEAT
        last = state.get("heartbeat_at")
        reason = ("没有漂移，但心跳到期（上次 %s，%s）—— 仓库 60 天无活动会被 GitHub 自动停用检测器"
                  % (last or "从未记录", "从未写过" if hb_age is None else "%d 天前" % hb_age))
    else:
        ident, reason = ID_IN_SYNC, "merge-base = 上游 HEAD %s ⇒ 已跟上" % upstream[:12]

    return Outcome(state=ident, exit=EXIT[ident], reason=reason, marker=kind, marker_note=note,
                   blacklisted=blacklisted, drift=drift, heartbeat_due=due,
                   heartbeat_age_days=hb_age, upstream=upstream, local=local,
                   merge_base=merge_base, failed_sha=failed_sha,
                   failed_age_days=state.get("failed_age"))


def render(r, subject=None, heartbeat_days=HEARTBEAT_DAYS):
    """人类可读的两行（结论 + 心跳）。心跳那一行**总是**打印 —— 它是正交输出。"""
    lines = ["结论: %s  退出码 %d" % (r.state, r.exit), "       %s" % r.reason]
    if r.heartbeat_due:
        lines.append("心跳: 到期 —— 该往孤儿分支写一个心跳提交（与本次有没有漂移无关）")
    elif r.heartbeat_age_days is None:
        # 走不到（没记录过就必然到期）；留着是为了让「未到期」那行**不可能**打印出 None
        lines.append("心跳: 未到期")
    else:
        lines.append("心跳: 未到期（%s 天前，还有 %d 天）"
                     % (r.heartbeat_age_days, heartbeat_days - r.heartbeat_age_days))
    if r.marker != MARKER_OK:
        lines.append("标记: %s（%s）" % (r.marker, r.marker_note))
    if subject:
        lines.append("上游: %s  %s" % (r.upstream[:12], subject))
    return "\n".join(lines)


# ── 表驱动用例：每条 = (说明, marker 内容, upstream, merge_base, 结论, 退出码, 心跳是否到期)
# 「今天」固定为 2026-09-25。前 4 条各自钉住验收点名的一种结论，其余按类别排：
# 拉黑边界 / 心跳边界 / 损坏兜底。全部离线。
J1 = ("j1" * 20)[:40]          # 上游 HEAD（也在我们的历史里）
J2 = ("j2" * 20)[:40]          # 上游的新 HEAD
J3 = ("j3" * 20)[:40]          # 另一个上游提交
LOCAL = ("cc" * 20)[:40]
HB_1DAY = '{"heartbeat": {"at": "2026-09-24"}}'                       # 1 天前 ⇒ 未到期
HB_40DAYS = '{"heartbeat": {"at": "2026-08-16"}}'                     # 40 天前 ⇒ 到期
HB_5DAYS = '{"heartbeat": {"at": "2026-09-20"}}'
BL = '{"failed": {"upstream": "%s", "at": "2026-09-22"}, "heartbeat": {"at": "2026-09-24"}}'
NO_HB = '{"failed": {"upstream": "%s", "at": "2026-09-22"}}'

CASES = [
    # —— 验收点名的四种结论 ——
    ("① 无漂移（merge-base = 上游 HEAD）", HB_1DAY, J1, J1, ID_IN_SYNC, 0, False),
    ("② 有漂移（merge-base 落在上游历史里）", HB_1DAY, J2, J1, ID_DRIFTED, 10, False),
    ("③ 同一提交已拉黑", BL % J2, J2, J1, ID_BLACKLISTED, 20, False),
    ("④ 该写心跳了（30 天没写）", HB_40DAYS, J1, J1, ID_HEARTBEAT, 30, True),
    # —— 心跳边界 ——
    ("心跳未到期（1 天前）", HB_1DAY, J2, J1, ID_DRIFTED, 10, False),
    ("心跳第 29 天：还没到", HB_5DAYS, J1, J1, ID_IN_SYNC, 0, False),
    ("从未写过心跳 ⇒ 到期", '{"failed": null}', J1, J1, ID_HEARTBEAT, 30, True),
    ("首次运行：无标记 + 无漂移 ⇒ 该写心跳", None, J1, J1, ID_HEARTBEAT, 30, True),
    ("首次运行：无标记 + 有漂移 ⇒ 有漂移（顺带记心跳到期）", None, J2, J1, ID_DRIFTED, 10, True),
    ("有漂移且心跳到期 ⇒ 判漂移，心跳那行标注到期", HB_40DAYS, J2, J1, ID_DRIFTED, 10, True),
    # —— 失败标记的边界 ——
    ("标记已过期（24 天 > 7 天窗口）⇒ 重新试", '{"failed": {"upstream": "%s", "at": "2026-09-01"}, '
     '"heartbeat": {"at": "2026-09-24"}}' % J2, J2, J1, ID_DRIFTED, 10, False),
    ("标记 7 天整 ⇒ 到期（窗口是 [标记日, +7)）",
     '{"failed": {"upstream": "%s", "at": "2026-09-18"}, "heartbeat": {"at": "2026-09-24"}}' % J2,
     J2, J1, ID_DRIFTED, 10, False),
    ("标记 6 天 ⇒ 仍拉黑",
     '{"failed": {"upstream": "%s", "at": "2026-09-19"}, "heartbeat": {"at": "2026-09-24"}}' % J2,
     J2, J1, ID_BLACKLISTED, 20, False),
    ("标记是另一个提交 ⇒ 不拉黑", BL % J3, J2, J1, ID_DRIFTED, 10, False),
    ("只有失败标记、没有心跳字段 ⇒ 拉黑优先，心跳那行说到期", NO_HB % J2, J2, J1,
     ID_BLACKLISTED, 20, True),
    # —— 损坏兜底：`failed` 坏了 ⇒ 按无标记继续，绝不停摆 ——
    ("标记文件为空 ⇒ 按无标记（有漂移就构建）", "", J2, J1, ID_DRIFTED, 10, True),
    ("合法但只记了心跳 ⇒ 无失败标记", '{"heartbeat": {"at": "2026-09-24"}}', J2, J1,
     ID_DRIFTED, 10, False),
    ("标记文件损坏（不是 JSON）", "{这不是 JSON", J2, J1, ID_DRIFTED, 10, True),
    ("标记文件损坏（顶层是数组）", "[1, 2, 3]", J2, J1, ID_DRIFTED, 10, True),
    ("标记文件损坏（日期写坏）",
     '{"failed": {"upstream": "%s", "at": "2026/09/22"}, "heartbeat": {"at": "2026-09-24"}}' % J2,
     J2, J1, ID_DRIFTED, 10, False),
    ("标记文件损坏（failed 缺 at）",
     '{"failed": {"upstream": "%s"}, "heartbeat": {"at": "2026-09-24"}}' % J2,
     J2, J1, ID_DRIFTED, 10, False),
    ("标记文件损坏（failed 是字符串）",
     '{"failed": "oops", "heartbeat": {"at": "2026-09-24"}}', J2, J1, ID_DRIFTED, 10, False),
    ("标记文件损坏 + 同一提交 + 心跳未到期 ⇒ 仍重试（不拉黑）",
     '{"failed": {"upstream": "%s", "at": "2026/09/22"}, "heartbeat": {"at": "2026-09-24"}}' % J2,
     J2, J1, ID_DRIFTED, 10, False),
    # —— 只有 heartbeat 坏：拉黑照旧（`failed` 完好），心跳按「从未记录」算 ——
    ("只有 heartbeat 写坏（是空对象）⇒ 拉黑照旧、心跳说到期",
     '{"failed": {"upstream": "%s", "at": "2026-09-22"}, "heartbeat": {}}' % J2,
     J2, J1, ID_BLACKLISTED, 20, True),
    ("只有 heartbeat 写坏（不是对象）⇒ 拉黑照旧",
     '{"failed": {"upstream": "%s", "at": "2026-09-22"}, "heartbeat": 7}' % J2,
     J2, J1, ID_BLACKLISTED, 20, True),
    ("只有 heartbeat 写坏（日期写坏）⇒ 拉黑照旧，心跳那行说到期（良性代价）",
     '{"failed": {"upstream": "%s", "at": "2026-09-22"}, "heartbeat": {"at": "2026/09/24"}}' % J2,
     J2, J1, ID_BLACKLISTED, 20, True),
    ("标记是别的提交 + heartbeat 写坏 ⇒ 有漂移，照常构建",
     '{"failed": {"upstream": "%s", "at": "2026-09-22"}, "heartbeat": {}}' % J3,
     J2, J1, ID_DRIFTED, 10, True),
    ("标记文件损坏 + 心跳 40 天 ⇒ 心跳仍判到期（半个标记也算数）",
     '{"failed": "oops", "heartbeat": {"at": "2026-08-16"}}', J1, J1, ID_HEARTBEAT, 30, True),
]


def self_test(tmpdir):
    """跑上面的表。返回失败条数。**任何一条不符就报错并列出期望/实际**。

    除了四元组（结论 / 退出码 / 心跳 / 标记分类），还断言一条**不变量**：
    **输出里打印了「按无标记处理」的行，就绝不可能同时判 `blacklisted`。**
    这条不变量是 code-review 逼出来的 —— 第一版正是「输出说要重试、退出码却是 20」。
    没有它，那类自相矛盾只能靠人读日志发现。
    """
    bad, today = 0, datetime.date(2026, 9, 25)
    os.makedirs(tmpdir, exist_ok=True)
    print("表驱动自测：%d 条用例，今天 = %s，窗口 = 标记 %d 天 / 心跳 %d 天"
          % (len(CASES), today, EXPIRE_DAYS, HEARTBEAT_DAYS))
    print()
    for i, (what, marker, up, mb, want_id, want_exit, want_hb) in enumerate(CASES, 1):
        path = os.path.join(tmpdir, "marker-%02d.json" % i)
        if marker is None:
            # 「没有标记文件」本身就是一个用例（首次运行），所以要确保它真的不存在
            if os.path.exists(path):
                os.remove(path)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(marker)
        try:
            r = check(up, LOCAL, mb, path, today)
            shown = render(r)                       # 调用方真正会看到的那些行
        except Exception as e:                       # 崩溃本身就是不合格
            print("  ❌ #%-2d %s\n       抛异常：%r" % (i, what, e))
            bad += 1
            continue
        got = (r.state, r.exit, r.heartbeat_due, r.marker)
        want = (want_id, want_exit, want_hb,
                MARKER_BROKEN if ("损坏" in what and "heartbeat" not in what) else None)
        problems = []
        if got[:3] != want[:3]:
            problems.append("期望 %s，实际 %s" % (want[:3], got[:3]))
        if want[3] and r.marker != want[3]:
            problems.append("标记分类应为「%s」，实际「%s」" % (want[3], r.marker))
        if "按**无标记**处理" in shown and r.state == ID_BLACKLISTED:
            problems.append("自相矛盾：既说按无标记处理，又判 blacklisted")
        if problems:
            bad += 1
            print("  ❌ #%-2d %s\n       %s" % (i, what, "\n       ".join(problems)))
        else:
            print("  ✅ #%-2d %-48s -> %s / %d / 心跳%s"
                  % (i, what[:48], r.state, r.exit, "到期" if r.heartbeat_due else "未到期"))
    print()
    print("结果: %s（%d/%d 通过）" % ("失败" if bad else "全部通过", len(CASES) - bad, len(CASES)))
    return bad


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(
        add_help=True, description="漂移判定（纯决策，见文件头注释）",
        epilog="退出码：0 无漂移 / 10 有漂移 / 20 已拉黑 / 30 该写心跳了 / 2 用法错误")
    ap.add_argument("--upstream", metavar="SHA", help="上游分支 HEAD")
    ap.add_argument("--local", metavar="SHA", help="我们跟随分支 HEAD")
    ap.add_argument("--merge-base", metavar="SHA", help="两者的 merge-base（必填，本工具不自己算）")
    ap.add_argument("--marker", metavar="文件", help="心跳分支上的状态文件（JSON）")
    ap.add_argument("--today", metavar="YYYY-MM-DD", help="「今天」（默认取 UTC 当天）")
    ap.add_argument("--expire-days", type=int, default=EXPIRE_DAYS, metavar="N",
                    help="失败标记有效期（默认 7）")
    ap.add_argument("--heartbeat-days", type=int, default=HEARTBEAT_DAYS, metavar="N",
                    help="心跳间隔（默认 30）")
    ap.add_argument("--upstream-subject", metavar="文本",
                    help="上游 HEAD 的提交标题，只进输出（给 issue 标题用）")
    ap.add_argument("--json", action="store_true", help="输出 JSON（给脚本吃）")
    ap.add_argument("--self-test", action="store_true", help="跑内置的表驱动用例（不联网、不读参数）")
    ap.add_argument("--self-test-dir", metavar="目录", default=None,
                    help="自测用的临时目录（默认系统临时目录）")
    a = ap.parse_args(argv[1:])

    # 注意：`-h/--help` 到不了这里 —— argparse 自己打印帮助并以 0 退出（那是对的，
    # 帮助不是用法错误）。文件头那份说明留给读源码的人。

    if a.self_test:
        import tempfile
        tmp = a.self_test_dir or os.path.join(tempfile.gettempdir(), "sync-check-selftest")
        return 1 if self_test(tmp) else 0

    missing = [n for n in ("upstream", "merge_base") if not getattr(a, n)]
    if missing:
        print("缺必填参数: %s" % ", ".join("--" + m for m in missing))
        print("（--merge-base 是必填的：本工具不做祖先关系判断，见文件头）")
        return EXIT_USAGE
    if a.expire_days < 1 or a.heartbeat_days < 1:
        print("--expire-days / --heartbeat-days 必须 >= 1")
        return EXIT_USAGE
    try:
        today = parse_date(a.today, "--today") if a.today else datetime.datetime.utcnow().date()
    except ValueError as e:
        print(str(e))
        return EXIT_USAGE

    r = check(a.upstream, a.local, a.merge_base, a.marker, today, a.expire_days, a.heartbeat_days)
    if a.json:
        print(json.dumps(r._asdict(), ensure_ascii=False, indent=1, default=str))
    else:
        print(render(r, a.upstream_subject, a.heartbeat_days))
        if r.marker == MARKER_BROKEN:
            # 必须说清「哪坏了」以及为什么它不致命 —— 否则这行会被读成噪音
            print("⚠️  标记文件损坏，按**无标记**处理：%s" % r.marker_note)
            print("    代价是可能重试一个已被拉黑的提交；不这样做的代价是损坏文件永久卡死检测器。")
            print("    能解析出来的那一半仍然算数（例如心跳时间）。")
        elif r.marker_note.startswith("`heartbeat` 写坏"):
            # 这一种**不是**「按无标记处理」：拉黑判定不受影响，只说清丢了什么
            print("⚠️  %s" % r.marker_note)
    return r.exit


if __name__ == "__main__":
    sys.exit(main(sys.argv))
