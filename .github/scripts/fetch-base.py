#!/usr/bin/env python3
"""
取底包：按「最新」或指定日期，把 LineageOS 官方 `boot.img` 取到指定路径，打印 sha256。

对应 `docs/los-line/upstream-sync-spec.md` 的接缝 **S4**（实现决定 4 与测试决定 S4）。
**纯 I/O、零决策** —— 它不判断该不该构建、也不判断该用哪一期，只负责「取下来 + 报哈希」。
决策在 `tools/sync-check.py`（那份零 I/O）。两者分开的理由见规格「实现决定 8」：
混在一起，决策逻辑就必须联网才能测。

## 为什么不用 `curl` / `Invoke-WebRequest`

本机实测（`PROJECT.md` §7 坑 20）：原生 HTTPS 走 schannel，`curl.exe` 报
`SEC_E_NO_CREDENTIALS`、`Invoke-WebRequest` 报 `Authentication failed`。
**Python 的 `urllib` 走 OpenSSL，能用** —— 本工具只用标准库，因此本机与 CI 上都能跑。

## 两个必须做的核对（`PROJECT.md` §7 坑 21）

128 MiB 的镜像**会被静默截断**：实测下到 134,151,836 / 134,217,728 字节就「正常结束」，
`urllib` **不抛异常**。所以：

1. **断点续传 + 重试** —— 中断后带 `Range: bytes=N-` 接着下，不从头再来；
2. **核对字节数与官方公布的 sha256** —— 官方 API 就给出这两个值，直接对。

「没报错」不等于「下完了」。缺了第 2 条，本工具会安静地产出一个坏底包。

## 官方 API（一次 GET，拿全部元数据）

```
https://download.lineageos.org/api/v2/devices/<设备>/builds
```

返回一个数组，**按日期倒序**，每项含 `date` / `datetime` / `files[]`，
`files[]` 里 `filename == "boot.img"` 的那条带 `sha256` / `size` / `url`。

⚠️ **`?date=` 这个查询参数官方 API 不支持**（实测：带上它仍返回全部）。所以
`--date` 是在客户端过滤 —— 而且官方**只留最近 3 期**（FAQ：「we currently keep
the last 3 builds per device」），更早的日期取不到。取不到就说清楚，不要静默换一期。

## 用法

```sh
python tools/fetch-base.py --latest                       # 最新一期
python tools/fetch-base.py --date 2026-09-20               # 指定日期
python tools/fetch-base.py --latest --list                 # 只列出可选的期次（不下载）
python tools/fetch-base.py --latest --print-only           # 只打印元数据（不下载）
python tools/fetch-base.py --latest --out stock-boot-los/boot_LOS23.2-20260920.img
python tools/fetch-base.py --latest --expect-sha256 <已记录的值>   # 额外的外部钉值
```

退出码：0 = 成功；1 = 取不到 / 校验不符 / 该资源不存在（4xx）；2 = 用法错误；3 = 网络失败（重试用尽）。

⚠️ **本文件有**两份，必须逐字节相同**：工作区的 `tools/fetch-base.py`，与 LOS 工作仓库里的
`.github/scripts/fetch-base.py`（CI 跑**后者** —— 内核树自带一个 `tools/`，那是上游的，
项目自己的 CI 工具一律放 `.github/`，同 `repack-boot.py`）。
镜像与复验：`python tools/sync-ci-scripts.py --gen` / `--check`（改一份就同步改另一份）。
"""

import argparse
import datetime
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

DEFAULT_DEVICE = "umi"
API = "https://download.lineageos.org/api/v2/devices/%s/builds"
MIRROR = "https://mirrorbits.lineageos.org"
WANT_FILE = "boot.img"          # 底包就是这一份；其余（recovery/dtbo/vbmeta）本工具不取

CHUNK = 1 << 20                 # 1 MiB
TRIES = 4                       # 每个请求的重试次数（含首次）
TIMEOUT = 60                    # 单次 urlopen 的超时
PART, SRC = ".part", ".src"     # 半成品与「它来自哪个 URL」的记号（见 download）


class UpstreamMissing(RuntimeError):
    """上游**明确说没有**（4xx）。

    与「网络失败」分开，是因为善后完全不同：网络失败该重试、该报退出码 3；
    上游说没有（设备代号打错、期次被清理）重试多少次都一样，该报退出码 1。
    """


def http_get(url, offset=0, timeout=TIMEOUT):
    """带可选 `Range` 的 GET。返回 response 对象（调用方负责流式读完）。"""
    req = urllib.request.Request(url, headers={"User-Agent": "umi-loskernel-fetch-base/1"})
    if offset:
        req.add_header("Range", "bytes=%d-" % offset)
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_json(url):
    """取一次元数据。5xx / 网络抖动重试；4xx 立刻抛 `UpstreamMissing`（重试不会变好）。"""
    last = None
    for i in range(TRIES):
        try:
            with http_get(url) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise UpstreamMissing("HTTP %d: %s" % (e.code, url))
            last = e
        except Exception as e:
            last = e
        if i + 1 < TRIES:
            time.sleep(2 * (i + 1))
    raise RuntimeError("取元数据失败: %s -> %s" % (url, last))


def builds_of(doc):
    """把 API 的数组规整成 `[{date, epoch, boot:{sha256,size,url}}, ...]`，按日期**倒序**（最新在前）。

    官方已按日期倒序返回；这里**仍然自己排一次** —— 排序是判据的一部分（「最新」靠它），
    不能依赖一个没有写进契约的顺序。
    """
    out = []
    for b in doc if isinstance(doc, list) else []:
        boot = None
        for f in b.get("files") or []:
            if f.get("filename") == WANT_FILE:
                boot = {"sha256": (f.get("sha256") or "").lower(),
                        "size": f.get("size"), "url": f.get("url")}
                break
        if boot is None or not boot["sha256"] or not boot["url"]:
            # 这一期没发 boot.img（或元数据不全）—— 跳过，但不算失败
            continue
        out.append({"date": b.get("date"), "epoch": b.get("datetime"), "boot": boot})
    out.sort(key=lambda x: x["date"] or "", reverse=True)
    return out


def pick(builds, date=None):
    """选一期。找不到就返回 None —— **绝不静默换一期**（换期等于换了产物输入）。"""
    if date:
        return next((b for b in builds if b["date"] == date), None)
    return builds[0] if builds else None


def default_out(device, target_date):
    """默认输出名：`boot-<设备>-<YYYYMMDD>.img`（`stock-boot-los/` 里那份的命名同族）。"""
    return "boot-%s-%s.img" % (device, (target_date or "unknown").replace("-", ""))


def part_paths(out_path):
    """半成品与来源记号的路径。**只有这一处拼后缀** —— 散在多处时总会漏掉一个。"""
    return out_path + PART, out_path + SRC


def drop_partial(out_path):
    """丢掉半成品与来源记号（`--no-resume`，以及续传判据不成立时）。"""
    for p in part_paths(out_path):
        if os.path.exists(p):
            os.remove(p)


def sha256_file(path, upto=None):
    """算 sha256；`upto` 给了就只算前 N 字节（续传时用来核对已落盘的那一段）。"""
    h = hashlib.sha256()
    left = upto
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            if left is not None:
                block = block[:left]
                left -= len(block)
            h.update(block)
            if left is not None and left <= 0:
                break
    return h.hexdigest()


def file_len(path):
    return os.path.getsize(path) if os.path.exists(path) else 0


def download(url, out_path, expect_size=None):
    """断点续传下载，返回落盘字节数。

    ⚠️ 续传的**前提**是「本地已有的是同一个文件的前缀」。只有一处能证明这件事：
    上一次是从**同一个 url** 断的。调用方用 `<输出>.src` 记下那个 url，
    本函数发现 url 变了就**丢弃半成品重下** —— 否则会把两个不同版本的字节拼在一起，
    而且**拼接出来的文件长度可能正好等于期望值**，长度检查拦不住它。
    """
    part, src = part_paths(out_path)
    parent = os.path.dirname(os.path.abspath(out_path))
    if parent:
        os.makedirs(parent, exist_ok=True)      # 输出目录不存在时自己建（别让用户先 mkdir）
    have = file_len(part)
    if have:
        prev = None
        try:
            with open(src, "r", encoding="utf-8") as f:
                prev = f.read().strip()
        except OSError:
            prev = None
        if prev != url:
            print("  ⚠️ 半成品来自另一个 URL（%s），丢弃重下" % (prev or "未知"))
            drop_partial(out_path)
            have = 0
    if not have:
        with open(src, "w", encoding="utf-8") as f:
            f.write(url + "\n")

    for i in range(TRIES):
        try:
            have = _one_pass(url, part, have, expect_size)
            return have
        except UpstreamMissing:
            raise                          # 4xx：重试不会变好，直接上抛
        except Exception as e:
            print("  ⚠️ 第 %d 次下载中断于 %d 字节：%s" % (i + 1, have, e))
            have = file_len(part)          # 断在半路：以磁盘上的实际长度为准再续
            if i + 1 < TRIES:
                time.sleep(2 * (i + 1))
    raise RuntimeError("下载失败（重试 %d 次用尽），半成品留在 %s" % (TRIES, part))


def _one_pass(url, part, have, expect_size):
    """一次下载尝试，返回落盘字节数。抽出来是为了让重试循环只有一份善后逻辑。"""
    # 进度每 8 MiB 报一次：128 MiB 的包每 MiB 报一次会刷出 128 行，日志没法看
    step, mark = 8 * CHUNK, have
    try:
        with http_get(url, offset=have) as r:
            # 服务器忽略 Range 时会回 200 + 整份内容 ⇒ 必须从头写，不能追加
            mode = "ab" if (have and r.status == 206) else "wb"
            if mode == "wb":
                have = mark = 0
            with open(part, mode) as f:
                while True:
                    block = r.read(CHUNK)
                    if not block:
                        break
                    f.write(block)
                    have += len(block)
                    if expect_size and have - mark >= step:
                        mark = have
                        print("  ... %d / %d 字节 (%.1f%%)"
                              % (have, expect_size, 100.0 * have / expect_size))
        return have
    except urllib.error.HTTPError as e:
        if 400 <= e.code < 500:
            # 上游说没有（例如该期次已被清理）—— 重试不会变好，别当成网络问题
            raise UpstreamMissing("HTTP %d: %s" % (e.code, url))
        raise


def report_failure(target, got_size, got_sha, out_path, url, pins):
    """校验不符的出口。**必须说清四件事**：期望什么、实际得到什么、输入是什么、最可能的原因。

    `pins` = 外部钉值（`--expect-sha256` / `--expect-size`）里**没对上的**那些。
    它们必须单独列出来：只报「官方的值」会出现「期望与实际的哈希逐字相同」这种看不懂的输出
    （实测踩到过 —— 官方值对得上、挂掉的是外部钉值）。
    """
    print()
    print("❌ 底包校验未通过 —— 不能当作可用的输入")
    print("   期次   : %s" % target["date"])
    print("   来源   : %s" % url)
    print("   文件   : %s" % out_path)
    print("   期望   : %d 字节  sha256 %s   ← 官方 API 公布的" % (target["boot"]["size"],
                                                             target["boot"]["sha256"]))
    print("   实际   : %d 字节  sha256 %s" % (got_size, got_sha))
    if pins:
        print("   ⚠️ 与官方值一致、但**外部钉值**不符的是：")
        for what, want, got in pins:
            print("      %s 期望 %s，实际 %s" % (what, want, got))
    print("   最可能的原因（按可能性排序）:")
    print("     ① **下载被静默截断**（本项目的已知坑）。官方 API 的 size 就是判据 ——")
    print("        长度对上但哈希不对，才轮到下面两条。")
    print("     ② **期次被换掉了**。官方只留最近 3 期，同一日期的包被重新发布时哈希会变；")
    print("        这时要用 --expect-sha256 钉住你记录的那一份，而不是相信 API 的当前值。")
    print("     ③ **镜像回源到了别的节点/别的内容**。换一个官方镜像域名重试（--mirror）。")


def self_test():
    """离线自测：验「选期次」「排倒序」「默认输出名」这三件**不需要联网**的事。

    下载本身只能真跑（规格「测试决定 S4」：只能真下载测），所以不在这里假装测它。
    """
    bad, n = 0, 0
    doc = [
        {"date": "2026-09-06", "datetime": 3, "files": [
            {"filename": "boot.img", "sha256": "c" * 64, "size": 3, "url": MIRROR + "/c"}]},
        {"date": "2026-09-20", "datetime": 1, "files": [
            {"filename": "boot.img", "sha256": "a" * 64, "size": 1, "url": MIRROR + "/a"},
            {"filename": "recovery.img", "sha256": "x" * 64, "size": 9, "url": MIRROR + "/x"}]},
        {"date": "2026-09-13", "datetime": 2, "files": [
            {"filename": "boot.img", "sha256": "b" * 64, "size": 2, "url": MIRROR + "/b"}]},
        {"date": "2026-09-01", "datetime": 0, "files": [
            {"filename": "dtbo.img", "sha256": "y" * 64, "size": 9, "url": MIRROR + "/y"}]},
    ]
    b = builds_of(doc)
    cases = [
        ("按日期倒序（最新在前）", [x["date"] for x in b],
         ["2026-09-20", "2026-09-13", "2026-09-06"]),
        ("没有 boot.img 的期次被剔除", "2026-09-01" in [x["date"] for x in b], False),
        ("按 URL 取到 boot 那一份（不是 recovery）", b[0]["boot"]["url"], MIRROR + "/a"),
        ("--latest 取最新", pick(b)["date"], "2026-09-20"),
        ("--date 命中", pick(b, "2026-09-13")["boot"]["sha256"], "b" * 64),
        ("--date 未命中 ⇒ None（不静默换期）", pick(b, "2020-01-01"), None),
        ("API 顺序被打乱也仍然取最新",
         pick(builds_of(list(reversed(doc))))["date"], "2026-09-20"),
        ("默认输出名", default_out(DEFAULT_DEVICE, "2026-09-20"), "boot-umi-20260920.img"),
        ("默认输出名跟着 --device 走（别拿常量拼）",
         default_out("raven", "2026-09-20"), "boot-raven-20260920.img"),
        ("半成品与来源记号成对", part_paths("/x/b.img"), ("/x/b.img.part", "/x/b.img.src")),
    ]
    for what, got, want in cases:
        n += 1
        if got == want:
            print("  ✅ %-40s -> %s" % (what, got))
        else:
            bad += 1
            print("  ❌ %-40s 期望 %s，实际 %s" % (what, want, got))
    print()
    print("结果: %s（%d/%d 通过）" % ("失败" if bad else "全部通过", n - bad, n))
    return bad


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(add_help=True, description="取 LineageOS 官方底包（纯 I/O，见文件头注释）")
    ap.add_argument("--device", default=DEFAULT_DEVICE, help="设备代号（默认 umi）")
    ap.add_argument("--latest", action="store_true", help="取最新一期（默认行为）")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="取指定日期那一期（官方只留最近 3 期）")
    ap.add_argument("--out", metavar="路径", help="输出路径（默认 boot-<设备>-<YYYYMMDD>.img）")
    ap.add_argument("--list", action="store_true", help="只列出可选的期次，不下载")
    ap.add_argument("--print-only", action="store_true", help="只打印选中期次的元数据，不下载")
    ap.add_argument("--expect-sha256", metavar="SHA256", help="额外的外部钉值（例如清单里记录的那份）")
    ap.add_argument("--expect-size", type=int, metavar="字节", help="额外的外部字节数钉值")
    ap.add_argument("--mirror", default=MIRROR, help="镜像域名（默认官方 mirrorbits）")
    ap.add_argument("--no-resume", action="store_true", help="丢弃半成品，从头下")
    ap.add_argument("--self-test", action="store_true", help="跑内置的离线用例（不联网）")
    a = ap.parse_args(argv[1:])

    if a.self_test:
        return 1 if self_test() else 0

    if a.date:
        try:
            datetime.date.fromisoformat(a.date)
        except ValueError:
            print("--date 不是 YYYY-MM-DD：%r" % a.date)
            return 2
    url_api = API % a.device
    try:
        builds = builds_of(fetch_json(url_api))
    except UpstreamMissing as e:
        # 「上游说没有」不是网络故障：重试多少次都一样 ⇒ 退出码 1，不是 3
        print("上游没有这个设备的构建列表：%s" % e)
        print("   先核对设备代号：--device 现在是 %r。" % a.device)
        return 1
    except RuntimeError as e:
        print("取期次列表失败（网络）：%s" % e)
        return 3
    if not builds:
        print("官方 API 没有返回任何带 boot.img 的期次：%s" % url_api)
        return 1

    if a.list:
        print("可选的期次（官方只留最近 3 期）:")
        for b in builds:
            print("  %s  %12d 字节  sha256 %s" % (b["date"], b["boot"]["size"], b["boot"]["sha256"][:16] + "…"))
        return 0

    target = pick(builds, a.date)
    if target is None:
        print("官方没有 %s 这一期的底包。" % a.date)
        print("⚠️ 官方**只留最近 3 期**（FAQ：we currently keep the last 3 builds per device），")
        print("   更早的期次被清理后取不回来。当前可选：%s"
              % ", ".join(b["date"] for b in builds))
        print("   换一期会换掉产物的输入 —— 所以本工具**不**替你挑一期。")
        return 1

    # 官方 API 给的 url 优先；--mirror 只用来替换它的域名（保留同样的路径）
    src_url = target["boot"]["url"]
    if a.mirror != MIRROR:
        path = src_url.split(MIRROR, 1)[-1] if MIRROR in src_url else src_url
        src_url = a.mirror.rstrip("/") + (path if path.startswith("/") else "/" + path)

    out_path = a.out or default_out(a.device, target["date"])
    print("期次   %s" % target["date"])
    print("来源   %s" % src_url)
    print("官方   %d 字节  sha256 %s" % (target["boot"]["size"], target["boot"]["sha256"]))
    print("输出   %s" % out_path)

    if a.print_only:
        print("（--print-only：没有下载任何东西）")
        return 0

    if a.no_resume:
        drop_partial(out_path)

    try:
        got_size = download(src_url, out_path, target["boot"]["size"])
    except UpstreamMissing as e:
        print("底包取不到（上游明确说没有）：%s" % e)
        return 1
    except RuntimeError as e:
        print("下载失败（网络）：%s" % e)
        return 3

    part, src = part_paths(out_path)
    got_sha = sha256_file(part)
    pins = []
    if a.expect_sha256 and got_sha != a.expect_sha256.lower():
        pins.append(("--expect-sha256", a.expect_sha256.lower(), got_sha))
    if a.expect_size and got_size != a.expect_size:
        pins.append(("--expect-size", a.expect_size, got_size))
    ok = (got_size == target["boot"]["size"] and got_sha == target["boot"]["sha256"] and not pins)

    if not ok:
        report_failure(target, got_size, got_sha, out_path, src_url, pins)
        return 1

    os.replace(part, out_path)          # 只有校验通过才改名 —— 半成品永远不叫正式名
    if os.path.exists(src):
        os.remove(src)
    if target.get("epoch"):
        # 把 mtime 钉到官方发布时间：同一期次在任何机器上落成同样的时间戳
        os.utime(out_path, (target["epoch"], target["epoch"]))

    print("字节   %d ✅ 与官方一致" % got_size)
    print("sha256 %s ✅ 与官方一致" % got_sha)
    if target.get("epoch"):
        print("时间戳 %s（官方 datetime=%d）"
              % (datetime.datetime.utcfromtimestamp(target["epoch"]).strftime("%Y-%m-%d %H:%M:%SZ"),
                 target["epoch"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
