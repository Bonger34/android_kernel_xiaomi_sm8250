#!/usr/bin/env python3
"""用仓库自带的 `GITHUB_TOKEN` 触发构建 workflow（`workflow_dispatch`）。

对应规格 `docs/los-line/upstream-sync-spec.md` 实现决定 1（两个 workflow 职责分开）与
#8 的正文（「用仓库自带令牌触发构建 workflow，并把上游提交与底包 sha256 传过去」）。

## 为什么单独做一件工具，而不是 workflow 里一句 `gh workflow run`

三条**判据**必须可执行，而不只是写在注释里：

1. ⭐ **「该传的必须真的传过去」。** `--require NAME` 列出的入参缺一个、或值为空，
   **一个请求都不发**（退出码 2）。空值是最坏的一种失败：构建照跑，
   只是它是一批**没有合并上游**的东西 —— 而运行是绿的。
2. **被触发的 workflow 必须在默认分支上、且必须是活的。** 这是 `workflow_dispatch`
   的三条硬性前提里的第三条（规格实现决定 1）；不满足时平台的报错是一个
   看不出所以然的 404，而这里的报错会直接说清是哪一条不满足、怎么恢复。
3. **403 要点名 `actions: write`。** 漏了它就是
   `Resource not accessible by integration`，社区广泛把它误传成「平台禁止
   workflow 之间互相触发」（规格 §「要写进坑表的新条目」第 1 条）。

外加一条现实理由：本机（Windows）没有可用的 bash，写在 YAML 里的步骤只能上 CI 试
（一轮十几分钟），而写在这里的东西 `--dry-run` 与 `--self-test` 都能离线验。

## 凭据

只认环境变量 `GH_TOKEN` / `GITHUB_TOKEN` —— 与项目既有规矩一致（`PROJECT.md` §5.3：
任何脚本都不得硬编码 PAT，规格用户故事 32：这条通道不该有任何长期凭据）。
CI 里传的是 `${{ github.token }}`。

## 退出码

| 结论 | 码 |
|---|---|
| 已派发（或 `--dry-run` 检查通过、未派发） | 0 |
| 运行错误（网络、5xx、平台拒绝） | 1 |
| 用法 / 前提错误（缺必填、workflow 不在默认分支、被停用…） | 2 |

## 用法

```sh
# CI：检测器发现漂移、试合并、取完底包之后
python3 .github/scripts/trigger-build.py --repo "$GITHUB_REPOSITORY" \
    --workflow build.yml --ref ksu-lineage-23.2 \
    --input upstream_sha="$UPSTREAM_SHA" --input merge_sha="$MERGE_SHA" \
    --input base_run_id="$GITHUB_RUN_ID" --input base_artifact="base-img-$MERGE_SHA" \
    --input base_sha256="$BASE_SHA256" \
    --require upstream_sha --require merge_sha \
    --require base_run_id --require base_artifact --require base_sha256

python tools/trigger-build.py --self-test            # 离线：假 API，不联网
python tools/trigger-build.py --dry-run --repo ... --workflow build.yml --ref ...   # 只读预检
```

⚠️ **本文件有两份，必须逐字节相同**：工作区 `tools/trigger-build.py` 与 LOS 工作仓库里的
`.github/scripts/trigger-build.py`（内核树**自带**一个 `tools/`，那是上游的，不能占用）。
镜像与复验：`python tools/sync-ci-scripts.py --gen` / `--check`。
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_DEFAULT = "https://api.github.com"
UA = "umi-loskernel-trigger-build/1"
TIMEOUT = 60
TRIES = 3

EXIT_OK = 0
EXIT_RUN = 1
EXIT_USAGE = 2
TRIES = 3


class Usage(RuntimeError):
    """用法 / 前提错误（退出码 2）—— 与运行错误分开，善后完全不同。

    一次用法错误**不该**被当成一次网络抖动去重试（重试只会把一条清楚的错误拖成三条）。
    """


class Api:
    """GitHub REST 的最小客户端（只用标准库）。

    ⚠️ 这里**故意**不与 `detect.py` 共用一个 HTTP 客户端：那会让两件工具互相牵连，
    而各自需要的东西只有这几十行。判定逻辑（`sync-check.py`）才是值得共用的那一层。
    """

    def __init__(self, token=None):
        self.token = token

    def call(self, method, path, payload=None):
        """返回 `(status, 解析后的 JSON)`。**204 是正常结果**（dispatch 就是 204 无正文）。"""
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
                import time
                time.sleep(2 * (i + 1))
        raise RuntimeError("%s %s 失败（重试 %d 次）：%r" % (method, path, TRIES, last))

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


def parse_inputs(pairs):
    """`NAME=VALUE` 列表 ⇒ 字典。⚠️ 按**第一个** `=` 切：值里可以再有 `=`。"""
    out = {}
    for p in pairs or []:
        if "=" not in p:
            raise Usage("--input 要写成 NAME=VALUE（收到 %r）" % p)
        k, v = p.split("=", 1)
        if not k.strip():
            raise Usage("--input 的名字是空的：%r" % p)
        out[k.strip()] = v
    return out


def preflight(api, repo, workflow, log=print):
    """派发前的只读检查（规格实现决定 1 的硬性条件）。

    返回 workflow 在仓库里的路径；任何一条不满足都抛 `Usage`。
    ⚠️ `ref` **不在这里检查**：平台的这三条前提与 ref 无关，
    而「ref 存不存在」由派发那一步用 404/422 当场回答（多一次 GET 换不来更多信息）。
    """
    st, wf = api.call("GET", "repos/%s/actions/workflows/%s" % (repo, urllib.parse.quote(workflow)))
    if st == 404:
        raise Usage(
            "仓库 %s 上找不到 workflow %r（HTTP 404）。\n"
            "   ① 文件名写错了？`workflow_dispatch` 的派发按**文件名**定位（如 `build.yml`）；\n"
            "   ② 这个 workflow 还不在**默认分支**上？—— 那是平台的硬性前提之一。"
            % (repo, workflow))
    if st >= 400:
        raise Usage("读 workflow %r 失败（HTTP %d）：%s" % (workflow, st, json.dumps(wf)[:300]))
    state, path = wf.get("state"), wf.get("path")
    if state != "active":
        raise Usage(
            "workflow %s 现在**不是活的**（state=%s）。\n"
            "   被平台按 inactivity 规则停用的会写成 `disabled_inactivity`，手工停用是\n"
            "   `disabled_manually` —— 两者都**点不动**，也就发不出去。\n"
            "   恢复：仓库 → Actions → 选中该 workflow → 右侧 `Enable workflow`（点几下，不需要命令）。\n"
            "   ⚠️ 本通道**只有检测器**带 `schedule`，所以只有它会被 inactivity 停用；\n"
            "      构建器只带 `workflow_dispatch`，它被停用一定是**人**干的。" % (path, state))
    log("   workflow   %s  state=%s" % (path, state))

    st, doc = api.call("GET", "repos/%s" % repo)
    if st >= 400:
        raise Usage("读仓库信息失败（HTTP %d）" % st)
    default = doc.get("default_branch")
    st, _cnt = api.call("GET", "repos/%s/contents/%s?ref=%s"
                        % (repo, urllib.parse.quote(path), urllib.parse.quote(default or "")))
    if st == 404:
        raise Usage(
            "workflow 文件 %s **不在默认分支 %s 上** —— 这是 `workflow_dispatch` 的硬性前提之一\n"
            "   （规格实现决定 1：被触发的 workflow 文件必须存在于默认分支上且带 `on: workflow_dispatch`）。\n"
            "   ⇒ 把它合并 / 推到默认分支之后再派发；否则平台只会回一个看不出所以然的 404。"
            % (path, default))
    if st >= 400:
        raise Usage("读默认分支上的 %s 失败（HTTP %d）" % (path, st))
    log("   默认分支    %s 上有这个文件 ✅" % default)
    return path


def trigger(a, api, log=print):
    """派发一次。返回排好序的说明行（同时给 `--json-out` 用）。"""
    inputs = parse_inputs(a.input)
    missing = [k for k in (a.require or []) if not inputs.get(k)]
    if missing:
        # ⚠️ 这里**一个请求都不发**：空入参会派发出一次「没有合并上游」的构建，
        #    而那次运行是完全绿的 —— 最坏的一种失败。
        raise Usage("这些入参必须有非空值，实际缺失/为空：%s\n"
                    "   已给出的入参：%s\n"
                    "   ⇒ 不发请求。空值会派发出一次「没有合并上游」的构建，而它看起来是成功的。"
                    % ("、".join(missing), ", ".join(sorted(inputs)) or "（一个都没有）"))
    if not a.ref or not a.ref.strip():
        raise Usage("--ref 不能为空（它决定这次构建检出哪个提交）")

    path = preflight(api, a.repo, a.workflow, log=log)
    log("   ref        %s" % a.ref)
    for k in sorted(inputs):
        log("   input      %s=%s" % (k, inputs[k] or "（空）"))

    if a.dry_run:
        log("⚠️ --dry-run：**没有派发**。上面就是将要发出的请求体。")
        return {"dispatched": False, "workflow": path, "ref": a.ref, "inputs": inputs}

    st, _doc = api.call("POST", "repos/%s/actions/workflows/%s/dispatches"
                        % (a.repo, urllib.parse.quote(a.workflow)),
                        {"ref": a.ref, "inputs": inputs})
    if st == 403:
        raise Usage(
            "派发被拒（HTTP 403 `Resource not accessible by integration`）。\n"
            "   ⇒ 最可能的原因：调用方的 `permissions:` 里**少了 `actions: write`**。\n"
            "      规格实现决定 1 已实测过：漏了它就是这一条 403（\n"
            "      `PROJECT.md` §7 坑表里那条「社区把它误传成平台禁止」的坑）。\n"
            "   ⚠️ 另一个前提：一旦写了 `permissions:`，**没列出的权限一律变 none** ——\n"
            "      检测器那边还要 `contents: write`（写心跳）与 `contents: read`（checkout）。")
    if st == 404:
        raise Usage("派发请求 404：workflow %r 或 ref %r 在 %s 上不存在。" % (a.workflow, a.ref, a.repo))
    if st == 422:
        raise Usage("派发请求 422：ref 或 inputs 不被接受（分支不存在？inputs 名写错？）。")
    if st not in (200, 201, 204):
        raise RuntimeError("派发返回了意外状态 HTTP %d" % st)
    log("✅ 已派发：%s @ %s（HTTP %d）" % (path, a.ref, st))
    log("   看这次运行：https://github.com/%s/actions/workflows/%s" % (a.repo, a.workflow))
    log("   ⚠️ dispatch 接口**不返回 run id** —— 「哪一次」靠时间 + head 提交去对（#10 会用到）。")
    return {"dispatched": True, "workflow": path, "ref": a.ref, "inputs": inputs,
            "http_status": st}


# ══════════════════════════════════════════════════════════════════════════════
#  自测：假 API，不联网
#
#  ⚠️ 每一条负向用例都断言**「没有发出 POST」** —— 「该拒绝的时候真的没发请求」
#     才是这些检查的全部意义（一条拒绝了、但请求照样发出去的检查等于没有）。
# ══════════════════════════════════════════════════════════════════════════════
class FakeApi:
    def __init__(self, wf_state="active", wf_http=200, on_default=True, post_status=204,
                 default_branch="ksu-lineage-23.2"):
        self.wf_state, self.wf_http, self.on_default = wf_state, wf_http, on_default
        self.post_status, self.default_branch = post_status, default_branch
        self.calls, self.posts = [], []

    def call(self, method, path, payload=None):
        self.calls.append("%s %s" % (method, path.split("?")[0]))
        if method == "GET" and "/actions/workflows/" in path:
            if self.wf_http != 200:
                return self.wf_http, {"message": "Not Found"}
            return 200, {"state": self.wf_state, "path": ".github/workflows/build.yml"}
        if method == "GET" and path.split("?")[0] == "repos/o/r":
            return 200, {"default_branch": self.default_branch}
        if method == "GET" and "/contents/" in path:
            return (200, {"name": "build.yml"}) if self.on_default else (404, {"message": "Not Found"})
        if method == "POST" and path.endswith("/dispatches"):
            self.posts.append(payload)
            return self.post_status, {}
        raise AssertionError("假 API 不认识这个端点：%s %s（实现跑偏了？）" % (method, path))


def _a(**kw):
    base = dict(repo="o/r", workflow="build.yml", ref="ksu-lineage-23.2", dry_run=False,
                input=["upstream_sha=" + "a" * 40, "merge_sha=" + "b" * 40,
                       "base_run_id=123", "base_artifact=base-img-x", "base_sha256=" + "c" * 64],
                require=["upstream_sha", "merge_sha", "base_run_id", "base_artifact",
                         "base_sha256"])
    base.update(kw)
    return argparse.Namespace(**base)


def self_test():
    bad, n = 0, [0]

    def case(what, want_exit, args, api_kw=None, want_posts=1, **checks):
        nonlocal bad
        n[0] += 1
        api = FakeApi(**(api_kw or {}))
        try:
            r = trigger(args, api, log=_quiet)
            code, exc = EXIT_OK, None
        except Usage as e:
            r, code, exc = None, EXIT_USAGE, str(e)
        except Exception as e:                       # 崩溃本身就是不合格
            r, code, exc = None, EXIT_RUN, repr(e)
        problems = []
        if code != want_exit:
            problems.append("期望退出码 %d，实际 %d（%s）" % (want_exit, code, exc))
        for k, v in checks.items():
            if k == "exc":                           # 错误消息里必须出现这个子串
                if v not in (exc or ""):
                    problems.append("报错里应出现 %r，实际 %r" % (v, exc))
            elif (r or {}).get(k) != v:
                problems.append("%s 期望 %r，实际 %r" % (k, v, (r or {}).get(k)))
        if len(api.posts) != want_posts:
            problems.append("POST 次数应为 %d，实际 %d" % (want_posts, len(api.posts)))
        if problems:
            bad += 1
            print("  ❌ #%-2d %s\n       %s" % (n[0], what, "\n       ".join(problems)))
        else:
            print("  ✅ #%-2d %-46s -> 退出码 %d / POST %d 次"
                  % (n[0], what[:46], code, len(api.posts)))
        return r, api

    r, api = case("① 正常派发：五个入参原样传过去", EXIT_OK, _a())
    n[0] += 1
    want = {"upstream_sha": "a" * 40, "base_sha256": "c" * 64, "base_run_id": "123"}
    got = (api.posts[0] if api.posts else {})
    if got.get("ref") == "ksu-lineage-23.2" and all(got.get("inputs", {}).get(k) == v
                                                    for k, v in want.items()):
        print("  ✅ #%-2d %-46s -> ref 与 inputs 逐项相符" % (n[0], "① 附属：请求体形状"))
    else:
        bad += 1
        print("  ❌ #%-2d ① 附属：请求体形状\n       %r" % (n[0], got))

    # ★ 最重要的一条：缺了必传的入参时**一个请求都不发**
    case("② 缺 upstream_sha ⇒ 拒绝**且不发请求**", EXIT_USAGE,
         _a(input=["merge_sha=" + "b" * 40], require=["upstream_sha", "merge_sha"]),
         want_posts=0, exc="upstream_sha")
    case("③ 入参给了但**值为空** ⇒ 同样拒绝", EXIT_USAGE,
         _a(input=["upstream_sha=", "base_sha256=" + "c" * 64],
            require=["upstream_sha", "base_sha256"]),
         want_posts=0, exc="upstream_sha")
    case("④ --ref 为空 ⇒ 拒绝", EXIT_USAGE, _a(ref="   "), want_posts=0, exc="ref")
    case("⑤ workflow 不存在（404）⇒ 拒绝且不发请求", EXIT_USAGE,
         _a(), api_kw={"wf_http": 404}, want_posts=0, exc="找不到 workflow")
    case("⑥ workflow 被停用（disabled_inactivity）⇒ 拒绝 + 恢复提示", EXIT_USAGE,
         _a(), api_kw={"wf_state": "disabled_inactivity"}, want_posts=0,
         exc="Enable workflow")
    case("⑦ workflow 不在默认分支上 ⇒ 拒绝", EXIT_USAGE,
         _a(), api_kw={"on_default": False}, want_posts=0, exc="默认分支")
    case("⑧ 平台回 403 ⇒ 报错点名 `actions: write`", EXIT_USAGE,
         _a(), api_kw={"post_status": 403}, exc="actions: write")
    case("⑨ --dry-run ⇒ 只预检、不派发", EXIT_OK, _a(dry_run=True), want_posts=0,
         dispatched=False)
    case("⑩ --input 写成没有 `=` 的样子 ⇒ 用法错误", EXIT_USAGE,
         _a(input=["nope"]), want_posts=0, exc="NAME=VALUE")

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
        add_help=True, description="用 GITHUB_TOKEN 触发构建 workflow（见文件头）",
        epilog="退出码：0 已派发 / 1 运行错误 / 2 用法或前提错误")
    ap.add_argument("--repo", metavar="OWNER/REPO", help="本线工作仓库")
    ap.add_argument("--workflow", default="build.yml",
                    help="workflow 的**文件名**（默认 build.yml）")
    ap.add_argument("--ref", metavar="分支", help="派发到哪个 ref（＝哪个提交会被检出）")
    ap.add_argument("--input", action="append", default=[], metavar="NAME=VALUE",
                    help="workflow_dispatch 的入参，可重复")
    ap.add_argument("--require", action="append", default=[], metavar="NAME",
                    help="这些入参必须存在且非空，否则**一个请求都不发**；可重复")
    ap.add_argument("--dry-run", action="store_true",
                    help="只做只读预检并打印请求体，不派发（⚠️ 预检里的读接口仍需凭据）")
    ap.add_argument("--json-out", metavar="文件", help="把结果写一份 JSON")
    ap.add_argument("--self-test", action="store_true", help="离线跑内置用例（假 API，不联网）")
    a = ap.parse_args(argv[1:])

    if a.self_test:
        return 1 if self_test() else 0
    if not a.repo:
        print("缺必填参数: --repo")
        return EXIT_USAGE

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        # ⚠️ 没有凭据时**直接失败**，别只 warn 一句再发请求：未认证的 dispatch 端点回的是
        #    404，而 404 在本工具里被解释成「workflow 或 ref 不存在」—— 一条**看着很确定、
        #    其实完全不对**的结论。凭据只从环境变量来（本项目不用 PAT，见文件头）。
        print("❌ 环境里没有 GH_TOKEN / GITHUB_TOKEN —— 派发需要凭据"
              "（CI 里传的是 `${{ github.token }}`；本项目不用 PAT）。")
        return EXIT_USAGE
    try:
        r = trigger(a, Api(token), log=print)
    except Usage as e:
        print("❌ %s" % e)
        return EXIT_USAGE
    except Exception as e:
        print("❌ 运行错误：%s" % e)
        return EXIT_RUN
    if a.json_out:
        with open(a.json_out, "w", encoding="utf-8", newline="\n") as f:
            json.dump(r, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv))
