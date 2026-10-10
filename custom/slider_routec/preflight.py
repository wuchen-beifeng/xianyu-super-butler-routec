#!/usr/bin/env python3
"""preflight.py -- 路线 C 前置体检 + 自愈（六项，T3）。

在 `routec_core.run_round()` 里、`sync_playwright()` **之前**调用；任何一项
不可恢复即返回 `ok=False` / `blocked_at=<step>`，调用方据此**在发起任何滑块
请求之前中止**（不消耗账号）。

六项（按序）：
  1 driver   ROUTEC_DRIVER_URL 可达（driver `probe`）      自愈：无（T4 负责），失败即中止
  2 lock     driver `state` 的 `locked`                    自愈：driver `unlock`（仍锁 → 中止）
  3 chrome   driver `state` 的 `chrome` 进程数             自愈：driver `launch-chrome` + 轮询 CDP ≤45s
  4 cdp      CDP `{CDP_URL}/json/version`                  自愈：同 3
  5 window   Chrome 主窗口 `iconic` 或未置前                  自愈：driver `focus`（还原 + 置前）
  6 page     CDP 里有 goofish target                       自愈：无（`_pick_page` 会新建），仅记录

返回体（**不含任何凭据**）：
  {"ok": true, "elapsed_s": 1.2, "steps": [...], "blocked_at": null, "detail": ""}

注：第 5 项的「置前」不只是好看 —— `routec_core._run()` 拖动前会用 SendInput 移动鼠标
做 screen↔client 校准，Chrome 若被别的窗口（如 WorkBuddyAI）盖住，鼠标事件到不了页面，
校准必然失败（实测 `校准失败：未观测到鼠标事件`）。

安全红线：不打印、不返回 cookie / token / 密码；异常文本一律截断。
"""
import os
import re
import sys
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import driverctl  # noqa: E402

# 与 routec_core 共用同一组环境变量（默认值必须一致）。
DRIVER_URL = os.environ.get("ROUTEC_DRIVER_URL", "http://<VM102_IP>:8791/")
CDP_URL = os.environ.get("ROUTEC_CDP_URL", "http://<VM102_IP>:9222").rstrip("/")
driverctl.DRIVER_URL = DRIVER_URL

OVERALL_MAX_S = 90.0      # 整体硬上限（硬约束：check_and_heal ≤ 90s）
CHROME_WAIT_S = 45.0      # 拉起 Chrome 后轮询 CDP 的上限
DRIVER_TIMEOUT_S = 8.0    # 单次 driver 调用超时
UNLOCK_TIMEOUT_S = 30.0   # unlock 单次超时（驱动内部约 2.5s）
CDP_TIMEOUT_S = 6.0       # CDP HTTP 探测超时
POLL_S = 1.5              # 轮询 CDP 的间隔
CHROME_CLASS = "Chrome_WidgetWin_1"
CHROME_EXE = "chrome"


def _deadline(timeout_s):
    """整体截止时刻（monotonic）。预算夹到 [5, 90] 秒。"""
    try:
        budget = float(timeout_s)
    except Exception:
        budget = 60.0
    return time.monotonic() + max(5.0, min(budget, OVERALL_MAX_S))


def _ms(t0):
    return int(round((time.monotonic() - t0) * 1000))


def _cdp_version():
    """CDP /json/version 是否就绪。返回 (ok, detail)。"""
    try:
        r = requests.get(CDP_URL + "/json/version", timeout=CDP_TIMEOUT_S)
        if r.status_code == 200 and (r.json() or {}):
            return True, ""
        return False, "CDP /json/version HTTP %s" % r.status_code
    except Exception as e:
        return False, "CDP 不可达：%s: %s" % (type(e).__name__, str(e)[:120])


def _launch_and_wait_cdp(dl):
    """调 driver `launch-chrome`，再轮询 CDP 直到就绪或超时。"""
    try:
        driverctl.call({"action": "launch-chrome"}, timeout=DRIVER_TIMEOUT_S)
    except Exception:
        pass
    end = min(dl, time.monotonic() + CHROME_WAIT_S)
    while time.monotonic() < end:
        ok, _ = _cdp_version()
        if ok:
            return True
        time.sleep(POLL_S)
    return False


def _parse_win_line(line):
    """解析 driver `windows` 的一行：fg / hwnd / rect 面积 / iconic / title。"""
    d = {"fg": "fg=True" in line, "hwnd": 0, "area": 0, "iconic": "iconic=True" in line,
         "title": ""}
    m = re.search(r"hwnd=(\d+)", line)
    if m:
        d["hwnd"] = int(m.group(1))
    m = re.search(r"rect=(-?\d+),(-?\d+),(-?\d+),(-?\d+)", line)
    if m:
        l, t, r, b = (int(x) for x in m.groups())
        d["area"] = max(0, r - l) * max(0, b - t)
    m = re.search(r"title='(.*)'", line)
    if m:
        d["title"] = m.group(1)
    return d


def _pick_chrome_window(wins):
    """挑出「主」Chrome 窗口。

    优先标题含 `Google Chrome` 的（Chrome 主窗口标题恒为 `<页面> - Google Chrome`），
    这样能避开 Chrome 自己弹的独立小窗（例如「要恢复页面吗？」崩溃恢复气泡，
    它同样注册 Chrome_WidgetWin_1，先命中的话 SetForegroundWindow 只会把气泡置前）。
    否则退化为面积最大的那个。返回 (line, info) 或 (None, None)。
    """
    cands = []
    for line in (wins or []):
        if ("cls='%s'" % CHROME_CLASS) in line and ("exe='%s'" % CHROME_EXE) in line:
            cands.append((line, _parse_win_line(line)))
    if not cands:
        return None, None
    for line, info in cands:
        if "google chrome" in info["title"].lower():
            return line, info
    cands.sort(key=lambda x: x[1]["area"], reverse=True)
    return cands[0]


def check_and_heal(timeout_s=60.0):
    """按序六项体检 + 自愈。永不抛异常；返回结构化结论（不含凭据）。"""
    t0 = time.monotonic()
    dl = _deadline(timeout_s)
    steps = []
    out = {"ok": False, "elapsed_s": 0.0, "steps": steps,
           "blocked_at": None, "detail": ""}

    def stop(step_id, detail):
        out["blocked_at"] = step_id
        out["detail"] = detail
        out["ok"] = False
        out["elapsed_s"] = round(time.monotonic() - t0, 1)
        return out

    # ---- 1) driver 可达（probe）----
    s = time.monotonic()
    st_data = {}
    try:
        r = driverctl.call({"action": "probe"}, timeout=DRIVER_TIMEOUT_S)
        if not r.get("ok"):
            steps.append({"id": "driver", "ok": False, "ms": _ms(s)})
            return stop("driver", "driver probe 返回 ok=false")
        data = r.get("data") or {}
        steps.append({"id": "driver", "ok": True, "ms": _ms(s),
                      "session": data.get("session")})
    except Exception as e:
        steps.append({"id": "driver", "ok": False, "ms": _ms(s)})
        return stop("driver", "%s: %s" % (type(e).__name__, str(e)[:150]))

    # ---- 2) 锁屏 ----
    s = time.monotonic()
    try:
        st = driverctl.call({"action": "state"}, timeout=DRIVER_TIMEOUT_S)
        st_data = st.get("data") or {}
        locked = bool(st_data.get("locked"))
    except Exception as e:
        steps.append({"id": "lock", "ok": False, "locked": None,
                      "unlocked": False, "method": "state_error", "ms": _ms(s)})
        return stop("lock", "driver state 调用失败：%s" % str(e)[:120])

    if not locked:
        steps.append({"id": "lock", "ok": True, "locked": False,
                      "unlocked": False, "method": "noop", "ms": _ms(s)})
    else:
        unlocked, method = False, "error"
        try:
            u = driverctl.call({"action": "unlock"}, timeout=UNLOCK_TIMEOUT_S)
            ud = u.get("data") or {}
            unlocked = bool(ud.get("unlocked"))
            method = str(ud.get("method") or "unknown")
        except Exception:
            pass
        steps.append({"id": "lock", "ok": unlocked, "locked": True,
                      "unlocked": unlocked, "method": method, "ms": _ms(s)})
        if not unlocked:
            return stop("lock", "锁屏且 unlock 未成功（method=%s）" % method)

    # ---- 3) Chrome 进程 ----
    s = time.monotonic()
    try:
        chrome = int(st_data.get("chrome") or 0)
    except Exception:
        chrome = 0
    healed_chrome = False
    if chrome > 0:
        steps.append({"id": "chrome", "ok": True, "chrome": chrome,
                      "healed": False, "ms": _ms(s)})
    else:
        healed_chrome = _launch_and_wait_cdp(dl)
        steps.append({"id": "chrome", "ok": healed_chrome, "chrome": chrome,
                      "healed": True, "ms": _ms(s)})
        if not healed_chrome:
            return stop("chrome", "Chrome 未运行，launch-chrome 后 CDP 未就绪（≤%ds）"
                        % int(CHROME_WAIT_S))

    # ---- 4) CDP ----
    s = time.monotonic()
    cdp_ok, cdp_detail = _cdp_version()
    if not cdp_ok and not healed_chrome:
        _launch_and_wait_cdp(dl)
        cdp_ok, cdp_detail = _cdp_version()
    steps.append({"id": "cdp", "ok": cdp_ok, "ms": _ms(s)})
    if not cdp_ok:
        return stop("cdp", cdp_detail)

    # ---- 5) Chrome 窗口：最小化（iconic）/ 未置前 ----
    s = time.monotonic()
    try:
        w = driverctl.call({"action": "windows"}, timeout=DRIVER_TIMEOUT_S)
        wins = (w.get("data") or {}).get("wins") or []
        line, info = _pick_chrome_window(wins)
    except Exception as e:
        steps.append({"id": "window", "ok": False, "restored": False, "ms": _ms(s)})
        return stop("window", "driver windows 调用失败：%s" % str(e)[:120])

    if info is None:
        steps.append({"id": "window", "ok": False, "restored": False, "ms": _ms(s)})
        return stop("window", "未找到 Chrome 窗口（cls=%s, exe=%s）" % (CHROME_CLASS, CHROME_EXE))

    iconic, is_fg = bool(info["iconic"]), bool(info["fg"])
    # 未最小化 + 已是前台 = 无需处理。否则调 focus（还原 + 置前）——
    # 注意「置前」不只是好看：拖动前 _run() 会用 SendInput 移动鼠标做 screen↔client
    # 校准，若 Chrome 被别的窗口（如 WorkBuddyAI）盖住，鼠标事件到不了页面，校准必失败。
    if (not iconic) and is_fg:
        steps.append({"id": "window", "ok": True, "restored": False,
                      "iconic": False, "foreground": True, "ms": _ms(s)})
    else:
        cmd = {"action": "focus", "class": CHROME_CLASS, "exe": CHROME_EXE}
        if "google chrome" in info["title"].lower():
            # 只认主窗口，避开「要恢复页面吗？」这类同 class 的独立小窗
            cmd["title"] = "Google Chrome"
        healed = False
        try:
            fr = driverctl.call(cmd, timeout=DRIVER_TIMEOUT_S)
            fd = fr.get("data") or {}
            healed = bool(fd.get("found")) and (bool(fd.get("restored"))
                                                or bool(fd.get("foreground_ok")))
        except Exception:
            pass
        steps.append({"id": "window", "ok": healed, "restored": healed,
                      "iconic": iconic, "foreground": is_fg, "ms": _ms(s)})
        if not healed:
            return stop("window", "Chrome 窗口最小化或未置前，focus 还原/置前失败")

    # ---- 6) CDP 里是否有 goofish target（仅记录，_pick_page 会新建）----
    s = time.monotonic()
    url = ""
    try:
        r = requests.get(CDP_URL + "/json/list", timeout=CDP_TIMEOUT_S)
        for t in (r.json() or []):
            u = t.get("url") or ""
            if "goofish" in u:
                url = u
                break
    except Exception:
        pass
    steps.append({"id": "page", "ok": True, "url": url, "found": bool(url), "ms": _ms(s)})

    out["ok"] = True
    out["elapsed_s"] = round(time.monotonic() - t0, 1)
    return out


if __name__ == "__main__":
    import argparse
    import json as _json
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=60.0)
    a = ap.parse_args()
    print(_json.dumps(check_and_heal(a.timeout), ensure_ascii=False, indent=1))
