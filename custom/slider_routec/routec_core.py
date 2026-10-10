#!/usr/bin/env python3
"""routec_core.py -- 路线 C 单轮编排：前置体检 -> 账号 cookie 注入 -> 新挑战 -> SendInput 拖动 -> x5sec -> 闭环校验。

前置体检（T3）：每轮开始先跑 preflight.check_and_heal()（六项体检 + 自愈），
不通过则直接返回 reason="preflight"（detail=<卡住的步骤>），不发起任何拖动。

移植自 /opt/reverse-lab/cases/goofish-slider-x5sec-wininput/src/runner.py（T4b）
与其压测包装 work/t6run55/step5_rounds.py（T6 run55），适配「多账号 + 服务化」：
  * 每轮先做**整账号 cookie 交换**（clear + add），不再依赖浏览器里预先登录的账号；
  * 返回结构化结果，不打印任何 cookie / x5sec 值。

硬依赖（应用机上的反向隧道落点，均由 VM101 反向 SSH 隧道提供）：
  * 127.0.0.1:8791  driver.ps1（session 1，user32!SendInput 执行器）
  * 127.0.0.1:9222  VM101 Chrome CDP
"""
import pathlib
import random
import re
import sys
import time

from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import driverctl            # noqa: E402
import humanize             # noqa: E402
import xianyu_api           # noqa: E402
import preflight            # noqa: E402  (T3: 六项体检 + 自愈)

DEVICE_APPKEY = "444e9908a51d1cb236a27862abc769c9"
# 驱动/CDP 地址由 preflight 统一从 env 解析（ROUTEC_DRIVER_URL / ROUTEC_CDP_URL），
# 避免两处默认值漂移；preflight 在 import 时已把 driverctl.DRIVER_URL 设好。
DRIVER_URL = preflight.DRIVER_URL
CDP_URL = preflight.CDP_URL
driverctl.DRIVER_URL = DRIVER_URL  # fix(2026-10-08): set at module level so /health targets the tunnel landing (was only set inside run_round)

# 这些 cookie 不进浏览器：挑战痕迹（x5secdata/x5sectag/x5step）会让 punish 页
# 认为挑战仍在进行；x5sec 要清掉才能逼出**新鲜**挑战。
EXCLUDE_COOKIES = {"x5secdata", "x5sectag", "x5step", "x5sec", "bx-cookie-test"}

MTop_CALL = """async (deviceId) => {
  try {
    const r = await window.lib.mtop.request({api: 'mtop.taobao.idlemessage.pc.login.token',
      v: '1.0', type: 'POST', dataType: 'json', data: {appKey: '%s', deviceId: deviceId}});
    return {ret: r && r.ret, hasUrl: !!(r && r.data && r.data.url)};
  } catch (e) { return {rejected: true}; }
}""" % DEVICE_APPKEY

DIAG_JS = r"""() => {
  if (!window.__dg) {
    window.__dg = {ev: [], frames: [], samples: [], md: 0, mu: 0};
    window.addEventListener('pointermove', e => {
      let n = null;
      try { n = e.getCoalescedEvents ? e.getCoalescedEvents().length : 'nofn'; } catch (err) { n = 'err'; }
      if (window.__dg.ev.length < 3000)
        window.__dg.ev.push({k: 'pm', t: e.timeStamp, x: e.clientX, y: e.clientY, n: n});
    }, true);
    window.addEventListener('mousedown', e => { window.__dg.md++; }, true);
    window.addEventListener('mouseup', e => { window.__dg.mu++; }, true);
    window.__dg.iv = setInterval(() => {
      const h = document.querySelector('#nc_1_n1z');
      if (window.__dg.samples.length < 4000)
        window.__dg.samples.push([Math.round(performance.now()),
                                  h ? Math.round(h.getBoundingClientRect().x) : null]);
    }, 10);
  }
  if (!window.__dg.__rafOn) {
    window.__dg.__rafOn = true;
    const raf = () => { window.__dg.frames.push(performance.now()); requestAnimationFrame(raf); };
    requestAnimationFrame(raf);
  }
  window.__dg.ev.length = 0; window.__dg.frames.length = 0; window.__dg.samples.length = 0;
  window.__dg.md = 0; window.__dg.mu = 0;
  return 'ok';
}"""


def uuidish(seed: str) -> str:
    CH = "0123456789ABCDEF"
    rnd = random
    s = "".join(rnd.choice(CH) for _ in range(8)) + "-"
    s += "".join(rnd.choice(CH) for _ in range(4)) + "-4"
    s += "".join(rnd.choice(CH) for _ in range(3)) + "-a"
    s += "".join(rnd.choice(CH) for _ in range(3)) + "-"
    return s + "".join(rnd.choice(CH) for _ in range(12)) + "-" + seed


def per_frame(events, frames):
    buckets = [0] * len(frames)
    fi = 0
    for e in events:
        while fi + 1 < len(frames) and frames[fi + 1] <= e["t"]:
            fi += 1
        if frames[fi] <= e["t"]:
            buckets[fi] += 1
    active = [b for b in buckets if b > 0]
    return {"count": len(events), "frames_with_events": len(active),
            "per_frame_max": max(active) if active else 0,
            "per_frame_hist": {str(k): active.count(k) for k in sorted(set(active))}}


def wait_frame(page, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        for fr in page.frames:
            if "punish" in fr.url:
                try:
                    if fr.evaluate("() => !!document.querySelector('#nc_1_n1z')"):
                        return fr
                except Exception:
                    pass
        time.sleep(0.5)
    return None


def _parse_cookie_str(cookies_str: str):
    pairs = []
    for part in cookies_str.split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k and k not in EXCLUDE_COOKIES:
                pairs.append((k, v))
    return pairs


def check_backend(timeout: float = 6.0):
    """健康检查：复用 T3 preflight 六项体检结果（保持 /health 语义）。

    ok = 六项全通过（preflight.ok）。为兼容旧调用方，保留 driver/chrome 摘要字段。
    `timeout` 是 preflight 的整体预算（秒），内部夹到 ≤90s。
    """
    pf = preflight.check_and_heal(timeout_s=timeout)
    info = {"ok": bool(pf.get("ok")), "preflight": pf,
            "driver": None, "chrome": None, "errors": []}
    by_id = {}
    for s in (pf.get("steps") or []):
        if isinstance(s, dict) and s.get("id"):
            by_id[s["id"]] = s
    if by_id.get("driver", {}).get("ok"):
        info["driver"] = {"session": by_id["driver"].get("session")}
    if by_id.get("cdp", {}).get("ok"):
        info["chrome"] = "cdp-ready"
    if not info["ok"]:
        info["errors"].append("blocked_at=%s: %s" % (pf.get("blocked_at"), pf.get("detail")))
    return info


def _pick_page(ctx):
    for p in ctx.pages:
        try:
            if "goofish" in (p.url or ""):
                return p
        except Exception:
            continue
    return ctx.new_page()


def _new_result():
    return {"ok": False, "reason": "", "elapsed_s": 0.0, "challenge": False,
            "moves": None, "slide_code": None, "x5sec": None, "x5_cookies": {},
            "verify": None, "detail": "", "preflight": None}


def _fail(out, reason, detail=""):
    out["reason"] = reason
    out["detail"] = detail
    return out


def _run(b, pairs, account_id, timeout_s, verify, out):
    ctx = b.contexts[0]
    page = _pick_page(ctx)
    slides = []

    def _on_rsp(rsp):
        try:
            if "_____tmd_____/slide" in rsp.url:
                body = ""
                try:
                    body = rsp.text()[:4000]
                except Exception:
                    body = ""
                m = re.search(r'"code"\s*:\s*(-?\d+)', body)
                slides.append({"t": time.time(),
                               "code": int(m.group(1)) if m else None,
                               "is_upgrade": ("isUpgrade" in body or "captchaconnect" in body)})
        except Exception:
            pass

    page.on("response", _on_rsp)
    rnd = random.Random(int(time.time() * 1000) ^ (hash(account_id) & 0xFFFF))
    t0 = time.time()
    deadline = t0 + max(30.0, timeout_s)

    # ---- 1) 整账号 cookie 交换（清挑战痕迹 + 清 x5sec，逼出新鲜挑战）----
    try:
        ctx.clear_cookies()
    except Exception:
        pass
    time.sleep(0.4)
    exp = time.time() + 30 * 86400
    ctx.add_cookies([{
        "name": k, "value": v, "domain": ".goofish.com", "path": "/",
        "expires": exp, "httpOnly": False, "secure": True, "sameSite": "None",
    } for k, v in pairs])

    # ---- 2) 重载页面并等 punish 挑战 ----
    page.goto("https://www.goofish.com/im", wait_until="domcontentloaded", timeout=45000)
    page.evaluate("() => { window.__lastMM=null; window.addEventListener('mousemove',"
                  " e => { window.__lastMM={x:e.clientX,y:e.clientY}; }, true); }")
    f = wait_frame(page, 13)
    if f is None:
        for _ in range(2):
            if time.time() > deadline:
                break
            t1 = time.time()
            while time.time() - t1 < 10:
                if page.evaluate("() => !!(window.lib && window.lib.mtop && window.lib.mtop.request)"):
                    break
                time.sleep(0.5)
            try:
                page.evaluate(MTop_CALL, uuidish(account_id))
            except Exception:
                pass
            f = wait_frame(page, 22)
            if f:
                break
    if f is None:
        return _fail(out, "no_challenge", "reload + 2x mtop call, no slider")
    out["challenge"] = True

    # ---- 3) 校准 screen<->client 偏移 + 测滑块几何 ----
    driverctl.move(600, 500)
    time.sleep(0.3)
    c1 = page.evaluate("() => window.__lastMM")
    if not c1:
        # T3 实测：光标本来就在 (600,500) 时，SendInput 到同一点**不产生** WM_MOUSEMOVE
        # （Windows 对位置未变的移动不派发事件）。从别处移到 (600,500) → 1 次 mousemove；
        # 已在 (600,500) 再 move → 0 次。若不处理，一次校准失败就会把光标留在校准点，
        # 之后每一轮都必然失败 —— 整条链路永久卡死。先挪开再回来即可逼出真实移动。
        driverctl.move(620, 520)
        time.sleep(0.15)
        driverctl.move(600, 500)
        time.sleep(0.3)
        c1 = page.evaluate("() => window.__lastMM")
    if not c1:
        return _fail(out, "error", "校准失败：未观测到鼠标事件")
    ox, oy = 600 - c1["x"], 500 - c1["y"]

    geo = page.evaluate("""() => { const el = document.querySelector('#baxia-dialog-content');
        const r = el.getBoundingClientRect(); return {x: r.x, y: r.y, w: r.width, h: r.height}; }""")
    sl = f.evaluate("""() => {
        const h = document.querySelector('#nc_1_n1z'), t = document.querySelector('#nc_1_n1t');
        if (!h || !t) return null;
        const rh = h.getBoundingClientRect(), rt = t.getBoundingClientRect();
        return {hx: rh.x, hy: rh.y, hw: rh.width, hh: rh.height,
                tx: rt.x, ty: rt.y, tw: rt.width, th: rt.height};
    }""")
    if sl is None:
        return _fail(out, "error", "slider disappeared before drag")
    sx = round(geo["x"] + sl["hx"] + sl["hw"] / 2 + ox)
    sy = round(geo["y"] + sl["hy"] + sl["hh"] / 2 + oy)
    dx = round(sl["tx"] + sl["tw"] - (sl["hx"] + sl["hw"]))

    # ---- 4) 预运动 + SendInput 拖动 ----
    f.evaluate(DIAG_JS)
    px = sx + rnd.randint(-8, 8)
    py = sy + rnd.randint(-5, 5)
    steps = rnd.randint(40, 48)
    gap_us = rnd.choice([3500, 4000, 4500])
    cursor = driverctl.probe()["data"]["cursor"]
    path = humanize.approach_path(cursor, (geo["x"] + ox, geo["y"] + oy), (px, py), rnd)
    for (qx, qy) in path:
        driverctl.move(int(round(qx)), int(round(qy)))
        time.sleep(0.006)
    time.sleep(0.12)

    pts = humanize.drag_track(dx, steps=steps, seed=rnd.randint(1, 99999))
    r = driverctl.drag([px, py], pts, sub=6, sub_gap_us=gap_us,
                       press_ms=150, release_ms=170, timeout=180)
    out["moves"] = (r.get("data") or {}).get("moves")

    # ---- 5) 等 x5sec ----
    x5 = False
    wd = min(deadline, time.time() + 22)
    while time.time() < wd:
        x5 = any(c["name"] == "x5sec" for c in ctx.cookies())
        if x5:
            break
        time.sleep(0.4)
    if slides:
        out["slide_code"] = slides[-1].get("code")
        if slides[-1].get("is_upgrade"):
            out["detail"] = "slide 响应为 HTML 升级处置页"

    x5_cookies = {c["name"]: c["value"] for c in ctx.cookies()
                  if c["name"].lower().startswith("x5")}
    out["x5_cookies"] = x5_cookies
    out["x5sec"] = x5_cookies.get("x5sec")

    try:
        dg = f.evaluate("() => ({ev: window.__dg.ev, frames: window.__dg.frames, md: window.__dg.md})")
        pmev = [e for e in dg["ev"] if e["k"] == "pm"]
        out["iframe_pm"] = per_frame(pmev, dg["frames"])
        out["iframe_md"] = dg.get("md")
    except Exception as e:
        out["iframe_pm"] = {"err": type(e).__name__}

    if not x5:
        return _fail(out, "slide_reject" if out["slide_code"] is not None else "no_x5sec")

    # ---- 6) 可选闭环校验：x5sec + 账号 cookie 重试原 API ----
    if verify:
        try:
            merged = "; ".join([f"{k}={v}" for k, v in pairs]
                               + [f"{k}={v}" for k, v in x5_cookies.items()])
            rr = xianyu_api.fetch_token(merged)
            ok = (rr["status"] == 200 and bool(rr.get("accessToken"))
                  and "punish" not in (rr.get("url") or ""))
            out["verify"] = {"api_ok": ok, "status": rr["status"],
                             "ret": (rr.get("ret") or [""])[0][:120],
                             "has_token": bool(rr.get("accessToken")),
                             "token_refreshed": rr.get("token_refreshed")}
            if not ok:
                return _fail(out, "api_fail", "x5sec 已取得但闭环 API 未通过")
        except Exception as e:
            out["verify"] = {"api_ok": False,
                             "err": "%s: %s" % (type(e).__name__, str(e)[:150])}
            return _fail(out, "api_fail", "闭环校验异常")

    out["ok"] = True
    out["reason"] = "pass"
    return out


def run_round(cookie_str: str, account_id: str, timeout_s: float = 180.0,
              verify: bool = False) -> dict:
    """执行一轮：前置体检 -> 注入 cookie -> 拿挑战 -> 拖动 -> 取 x5sec（可选闭环校验）。

    永不抛异常；异常一律转成 ok=False 的结构。
    前置体检（T3，六项 + 自愈）在 sync_playwright() 之前运行；不通过则返回
    reason="preflight"、detail=<卡住的步骤>，**不发起任何拖动**。
    """
    out = _new_result()
    t0 = time.time()
    driverctl.DRIVER_URL = DRIVER_URL
    pairs = _parse_cookie_str(cookie_str or "")
    if not any(k == "_m_h5_tk" for k, _ in pairs):
        return _fail(out, "bad_input", "cookie 缺少 _m_h5_tk")

    # ---- 0) 前置体检 + 自愈（T3）：六项体检；失败即在发起任何拖动前中止 ----
    # 这一步不接触账号 cookie，也不产生任何闲鱼请求，所以预检失败不消耗账号。
    try:
        pf = preflight.check_and_heal(timeout_s=timeout_s)
    except Exception as e:
        pf = {"ok": False, "blocked_at": "preflight", "elapsed_s": 0.0, "steps": [],
              "detail": "%s: %s" % (type(e).__name__, str(e)[:150])}
    out["preflight"] = pf
    if not pf.get("ok"):
        return _fail(out, "preflight", str(pf.get("blocked_at") or "unknown"))

    try:
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(CDP_URL)
            try:
                return _run(b, pairs, account_id, timeout_s, verify, out)
            finally:
                try:
                    b.close()
                except Exception:
                    pass
    except Exception as e:
        return _fail(out, "error", "%s: %s" % (type(e).__name__, str(e)[:200]))
    finally:
        out["elapsed_s"] = round(time.time() - t0, 1)


if __name__ == "__main__":
    import argparse
    import json as _json
    ap = argparse.ArgumentParser()
    ap.add_argument("--cookie-file", required=True)
    ap.add_argument("--account", default="cli")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    res = run_round(pathlib.Path(a.cookie_file).read_text().strip(), a.account,
                    verify=a.verify)
    safe = {k: v for k, v in res.items() if k not in ("x5sec", "x5_cookies")}
    safe["x5sec_present"] = bool(res.get("x5sec"))
    print(_json.dumps(safe, ensure_ascii=False, indent=1))
