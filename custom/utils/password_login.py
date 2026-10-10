#!/usr/bin/env python3
"""password_login.py -- 密码登录编排（容器内；导航走 CDP，输入走 VM101 真机 SendInput）。

架构（与「路线 C」同源，见 docs/01-路线C滑块链路.md）：
  * 导航 / DOM 读取 / cookie 读写 = Playwright over CDP
    （``connect_over_cdp`` 连 VM101 常驻 Chrome；地址取 ``slider_routec/preflight.py`` 的 CDP_URL）
  * 真正的键盘 / 鼠标输入 = VM101 ``driver.ps1`` 的 ``user32!SendInput``
    （经反向 SSH 隧道 → 容器内 ``127.0.0.1:8791``，见 ``slider_routec/driverctl.py``）

为什么必须这样：容器内浏览器 + CDP 合成事件在本栈实测通过率 0（阿里 nc 能区分
「每帧一个」合成事件与真机「一帧多个子采样」）。所以：

  * ``page.goto`` / ``query_selector`` / ``getBoundingClientRect`` / ``cookies()``
    —— 只读，over-CDP 原样保留；
  * 任何 ``.click()`` / ``.fill()`` / ``.type()`` / ``mouse.*`` —— 一律换成
    ``driverctl.click`` / ``type_text`` / ``tap_vk``（SendInput）。

触发策略（决策写死；配置在 ``system_settings``，面板改完热生效）：

  | 键                                | 默认 | 含义 |
  |-----------------------------------|------|------|
  | ``pwd_login_enabled``             | true | 总开关 |
  | ``pwd_login_fail_threshold``      | 5    | 免密刷新**连续失败**几次才触发密码登录 |
  | ``pwd_login_cooldown_minutes``    | 30   | 触发后冷却（分钟） |
  | ``pwd_login_fail_count:<cid>``    | 0    | 免密刷新连续失败计数（任一次成功即清零） |
  | ``pwd_login_cooldown_until:<cid>``| 0    | 冷却截止（unix 秒） |
  | ``pwd_login_attempt_fail:<cid>``  | 0    | 密码登录**连续失败**次数（≥3 停手，不再消耗账号） |
  | ``pwd_login_status:<cid>``        | —    | 最近一次结果：ok / need_manual / failed / blocked / preflight |

安全红线：
  * 密码只从 DB 读、只经 driver 发出；**不进日志、不进返回值、不进产物**；
  * cookie / token 值不进日志；返回体只含长度、字段名与判定码；
  * 截图落盘不含任何凭据（只是登录页画面）。

不做的：
  * 人脸 / 短信**不自动通过**，只截图 + 通知 + 进冷却（必须人工）；
  * 不回退到容器内浏览器登录（那正是二期删掉的原因）。
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import random
import sqlite3
import sys
import time

from loguru import logger

# ---------------------------------------------------------------------------
# 依赖：路线 C 的现成组件（同镜像 /app/slider_routec）
# ---------------------------------------------------------------------------
_HERE = pathlib.Path(__file__).resolve().parent          # /app/utils
_ROUTEC_DIR = _HERE.parent / "slider_routec"             # /app/slider_routec
if str(_ROUTEC_DIR) not in sys.path:
    sys.path.insert(0, str(_ROUTEC_DIR))

import driverctl            # noqa: E402  (SendInput 传输层)
import humanize             # noqa: E402  (人类化轨迹)
import preflight            # noqa: E402  (六项体检 + 自愈；import 时设好 driverctl.DRIVER_URL)
import xianyu_api           # noqa: E402  (闭环校验 mtop token)

from playwright.sync_api import sync_playwright   # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
LOGIN_URL = "https://www.goofish.com/im"
SHOT_DIR = pathlib.Path(os.getenv("PWD_LOGIN_SHOT_DIR", "/app/data/pwd_login_shots"))

# 这些 cookie 不进浏览器：挑战痕迹会让登录页认为「上一轮验证还在进行」。
EXCLUDE_COOKIES = {"x5secdata", "x5sectag", "x5step", "x5sec", "bx-cookie-test"}

# 密码登录**连续失败**上限：到了就停手，不许继续消耗账号。
MAX_LOGIN_ATTEMPTS = 3

# 表单 / 判定选择器（取自 W3 的 selectors.md，行号见该文档）
# ⚠️ 只认「密码登录」页签的严格 id：短信登录页签的字段是 #fm-sms-login-id / #fm-smscode，
#    宽泛的 placeholder 匹配（如 input[placeholder*="手机号"]）会把短信页签的手机号框
#    误判成账号框，于是密码被送进 6 位验证码框（实测踩过）。
SEL_ID = ["#fm-login-id", 'input[name="fm-login-id"]']
SEL_PWD = ["#fm-login-password"]
SEL_TAB = "a.password-login-tab-item"
SEL_TAB_SMS = "a.sms-login-tab-item"
SEL_SMS_CODE = "#fm-smscode"
SEL_AGREE = "#fm-agreement-checkbox"
SEL_SUBMIT = "button.password-login"
SEL_SLIDER_HANDLE = "#nc_1_n1z"
SEL_SLIDER_TRACK = ["#nc_1_n1t", ".nc_scale"]
SEL_SLIDER_CONTAINER = ["#baxia-dialog-content", ".nc-container"]
SEL_LOGIN_IFRAME = "#alibaba-login-box"

# 账密错误 / 人工验证文案
ERR_TEXTS = ("账密错误", "账号密码错误", "用户名或密码错误")
# 只留「确实是人工验证提示」的文案；不要放「获取验证码」——那是短信页签的按钮，
# 页签默认就渲染，会把「未提交」误判成「需要人工验证」。
MANUAL_TEXTS = ("拍摄脸部", "人脸验证", "人脸识别", "面部验证", "请进行人脸验证",
                "请完成人脸识别", "验证码已发送", "短信验证码已发送", "动态密码",
                "手机短信验证", "短信验证身份", "验证身份", "点击获取验证码")
# 提交后出现这些「要人工填码」的输入框 = 需要人工（实测闲鱼用 #J_Checkcode；
# 短信登录页签用 #fm-smscode）。注意别把密码表单里那个隐藏的 nc_*_captcha_input 算进来。
SEL_MANUAL_CODE = ["#J_Checkcode", "#fm-smscode"]
SUCCESS_TEXT_HINT = ".rc-virtual-list-holder-inner"

CHROME_CLASS = "Chrome_WidgetWin_1"
CHROME_EXE = "chrome"

# ---------------------------------------------------------------------------
# 配置 / 计数（system_settings）
# ---------------------------------------------------------------------------
K_ENABLED = "pwd_login_enabled"
K_THRESHOLD = "pwd_login_fail_threshold"
K_COOLDOWN_MIN = "pwd_login_cooldown_minutes"


def _db():
    from app.db_manager import db_manager
    return db_manager


def _ss_get(key, default=None):
    try:
        raw = _db().get_system_setting(key)
    except Exception:
        return default
    if raw is None or str(raw).strip() == "":
        return default
    return str(raw).strip()


def _ss_set(key, value, description=None):
    try:
        return bool(_db().set_system_setting(key, str(value), description))
    except Exception as e:  # pragma: no cover - DB 不可用时不致命
        logger.warning(f"password_login: 写 system_settings 失败 {key}: {type(e).__name__}")
        return False


def _as_bool(raw, default=False):
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "是")


def _as_int(raw, default=0):
    try:
        return int(str(raw).strip())
    except Exception:
        return default


def config() -> dict:
    return {
        "enabled": _as_bool(_ss_get(K_ENABLED), True),
        "fail_threshold": max(1, _as_int(_ss_get(K_THRESHOLD), 5)),
        "cooldown_minutes": max(1, _as_int(_ss_get(K_COOLDOWN_MIN), 30)),
    }


def fail_key(cid): return f"pwd_login_fail_count:{cid}"
def cooldown_key(cid): return f"pwd_login_cooldown_until:{cid}"
def attempt_fail_key(cid): return f"pwd_login_attempt_fail:{cid}"
def status_key(cid): return f"pwd_login_status:{cid}"


def get_fail_count(cid) -> int:
    return _as_int(_ss_get(fail_key(cid)), 0)


def get_attempt_fail(cid) -> int:
    return _as_int(_ss_get(attempt_fail_key(cid)), 0)


def get_cooldown_until(cid) -> int:
    return _as_int(_ss_get(cooldown_key(cid)), 0)


def in_cooldown(cid) -> bool:
    return time.time() < get_cooldown_until(cid)


def set_cooldown(cid, seconds):
    _ss_set(cooldown_key(cid), int(time.time() + max(1, int(seconds))))


def clear_cooldown(cid):
    _ss_set(cooldown_key(cid), 0)


def note_refresh_success(cid):
    """免密刷新成功 → 计数清零 + 清冷却（这是「成功一次即清零」的落点）。"""
    set_fail_count(cid, 0)
    clear_cooldown(cid)
    _ss_set(attempt_fail_key(cid), 0)
    _ss_set(status_key(cid), "ok")
    logger.info(f"【{cid}】免密刷新成功：密码登录失败计数已清零")


def note_refresh_failure(cid):
    """免密刷新失败 → 计数 +1。返回 (新计数, 是否达到触发阈值)。"""
    n = get_fail_count(cid) + 1
    set_fail_count(cid, n)
    th = config()["fail_threshold"]
    logger.warning(f"【{cid}】免密刷新失败：连续失败计数 {n}/{th}")
    return n, n >= th


def set_fail_count(cid, n):
    _ss_set(fail_key(cid), int(n))


def should_attempt(cid):
    """是否允许发起密码登录。返回 (ok: bool, reason: str)。"""
    cfg = config()
    if not cfg["enabled"]:
        return False, "disabled"
    # 停手优先于冷却：连续失败已到上限时，冷却到期也不该再试（需要人工介入）
    if get_attempt_fail(cid) >= MAX_LOGIN_ATTEMPTS:
        return False, "blocked"
    if in_cooldown(cid):
        return False, "cooldown"
    if get_fail_count(cid) < cfg["fail_threshold"]:
        return False, "below_threshold"
    return True, "ok"


def status_snapshot(cid) -> dict:
    """面板/排障用：当前计数、冷却、开关（不含任何凭据）。"""
    cfg = config()
    cd = get_cooldown_until(cid)
    return {
        "cookie_id": cid,
        "enabled": cfg["enabled"],
        "fail_threshold": cfg["fail_threshold"],
        "cooldown_minutes": cfg["cooldown_minutes"],
        "fail_count": get_fail_count(cid),
        "attempt_fail": get_attempt_fail(cid),
        "cooldown_until": cd,
        "cooldown_remaining_s": max(0, int(cd - time.time())),
        "last_status": _ss_get(status_key(cid), ""),
    }


# ---------------------------------------------------------------------------
# 凭据（只读 DB；值绝不外传）
# ---------------------------------------------------------------------------
def _load_credentials(cid):
    """从容器 sqlite 读 username / password。读不到返回 None。"""
    try:
        db_path = getattr(_db(), "db_path", None)
    except Exception:
        db_path = None
    db_path = db_path or os.getenv("DB_PATH", "data/xianyu_data.db")
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT username, password FROM cookies WHERE id = ?", (cid,)).fetchone()
    finally:
        con.close()
    if not row or not row[0] or not row[1]:
        return None
    return str(row[0]).strip(), str(row[1])


def _load_cookie_str(cid):
    try:
        info = _db().get_cookie_by_id(cid)
    except Exception:
        return ""
    return (info or {}).get("cookies_str") or ""


# ---------------------------------------------------------------------------
# 通知（人工验证提醒）—— 直接走容器通知渠道，不依赖 XianyuLive 实例
# ---------------------------------------------------------------------------
def _captcha_manual_message(cid, screenshot_path=None, verification_url=None):
    lines = [
        "⚠️ 闲鱼密码登录需要人工验证（人脸 / 短信）",
        "",
        f"账号: {cid}",
        f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "请在 VM101 的 Chrome（chrome-wininput 专用 profile）里完成人脸/短信验证；",
        "自动化会在冷却后重试，期间该账号的消息监听可能不可用。",
    ]
    if verification_url:
        lines += ["", f"验证链接: {verification_url}"]
    if screenshot_path:
        lines += ["", f"截图: {screenshot_path}"]
    return "\n".join(lines)


def _send_qq_image(cid, image_path):
    """把截图以 base64 形式推到 QQ 渠道（NapCat 支持 ``[CQ:image,file=base64://...]``）。

    NapCat 跑在别的机器上，容器内的文件路径它读不到，所以只能内联。
    失败只记 warning，不影响主流程。
    """
    import base64
    import json as _json
    import requests
    try:
        rules = _db().get_account_notifications(cid, event_type="captcha_manual") or []
    except Exception:
        return 0
    try:
        with open(image_path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
    except Exception as e:
        logger.warning(f"【{cid}】人脸截图读取失败: {type(e).__name__}")
        return 0
    sent = 0
    for r in rules:
        try:
            ctype = str(r.get("channel_type") or "").lower()
            if ctype not in ("qq", "napcat", "onebot"):
                continue
            cfg = r.get("channel_config")
            cfg = _json.loads(cfg) if isinstance(cfg, str) else (cfg or {})
            base = str(cfg.get("base_url") or "").rstrip("/")
            uid = int(cfg.get("user_id"))
            if not base or not uid:
                continue
            headers = {"Content-Type": "application/json"}
            tok = str(cfg.get("access_token") or "").strip()
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            # 本地有透明 TLS 代理会破坏 keep-alive：一律裸 requests.post
            requests.post(f"{base}/send_private_msg",
                          data=_json.dumps({"user_id": uid,
                                            "message": f"[CQ:image,file=base64://{b64}]"}),
                          headers=headers, timeout=30)
            sent += 1
        except Exception as e:
            logger.warning(f"【{cid}】QQ 截图推送失败: {type(e).__name__}: {str(e)[:120]}")
    return sent


def notify_manual_required(cid, screenshot_path=None, verification_url=None):
    """经容器通知渠道推「人工验证提醒」。返回成功发送的渠道数。"""
    msg = _captcha_manual_message(cid, screenshot_path, verification_url)
    try:
        rules = _db().get_account_notifications(cid, event_type="captcha_manual") or []
    except Exception as e:
        logger.warning(f"【{cid}】读取通知渠道失败: {type(e).__name__}")
        rules = []
    rules = [r for r in rules if r.get("enabled", True)]
    if not rules:
        logger.warning(f"【{cid}】未配置 captcha_manual 通知渠道，人工验证提醒未发送")
        return 0

    from app.services.notification_sender import send_notification

    async def _go():
        n = 0
        for r in rules:
            try:
                await send_notification(r.get("channel_type"), r.get("channel_config"), msg)
                n += 1
            except Exception as e:
                logger.warning(f"【{cid}】通知渠道 {r.get('channel_name')} 发送失败: "
                               f"{type(e).__name__}: {str(e)[:120]}")
        return n

    try:
        sent = asyncio.run(_go())
    except RuntimeError:
        # 理论上不会走到：本函数在 to_thread 的线程里跑，没有运行中的事件循环
        sent = 0
    if sent and screenshot_path:
        _send_qq_image(cid, screenshot_path)
    logger.info(f"【{cid}】人工验证提醒已发送渠道数: {sent}")
    return sent


# ---------------------------------------------------------------------------
# 浏览器侧：CDP 只读辅助
# ---------------------------------------------------------------------------
def _pick_page(ctx):
    for p in ctx.pages:
        try:
            if "goofish" in (p.url or ""):
                return p
        except Exception:
            continue
    return ctx.new_page()


def _frame_offset(page, frame):
    """frame 内容坐标 → 主 frame viewport 的偏移。"""
    ox = oy = 0.0
    f = frame
    while f is not None and f.parent_frame is not None:
        try:
            el = f.frame_element()
            bb = el.bounding_box()
        except Exception:
            bb = None
        if bb:
            ox += bb["x"]
            oy += bb["y"]
        f = f.parent_frame
    return ox, oy


def _find_visible(frame, selectors):
    for sel in selectors:
        try:
            el = frame.query_selector(sel)
        except Exception:
            el = None
        if el:
            try:
                if el.is_visible():
                    return sel, el
            except Exception:
                continue
    return None, None


def _find_form_frame(page, timeout_s=12.0):
    """等「密码登录」表单就绪。

    判定必须**同时**看到账号框与密码框 —— 只认 `#fm-login-id` 不够：
    短信页签的手机号框 `#fm-sms-login-id` 不会被它匹配，但历史上有宽泛选择器
    把短信页签当密码页签的坑（密码会被打进 6 位验证码框），这里用双字段锁死。
    返回 (frame, selector)。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        for fr in [page] + list(page.frames):
            try:
                a = fr.query_selector(SEL_ID[0])
                b = fr.query_selector(SEL_PWD[0])
                if a and b and a.is_visible() and b.is_visible():
                    return fr, SEL_ID[0]
            except Exception:
                continue
        time.sleep(0.4)
    return None, None


def _active_element(frame):
    """当前 activeElement 的 id/name/tagName（用于验证 Tab 是否落到密码框）。"""
    try:
        return frame.evaluate(
            "() => { const e = document.activeElement;"
            " return e ? (e.id || e.name || e.tagName) : null; }")
    except Exception:
        return None


def _submit_effect(page, frame):
    """回车是否真的触发了提交：滑块 / 错误文案 / 短信面板 / 表单消失，任一即算已提交。"""
    if _find_any_frame(page, [SEL_SLIDER_HANDLE])[0] is not None:
        return True
    if _find_any_frame(page, SEL_MANUAL_CODE)[0] is not None:
        return True
    if _detect_error(frame):
        return True
    try:
        el = frame.query_selector(SEL_PWD[0])
        if el is None or not el.is_visible():
            return True
    except Exception:
        return True
    return False


def _find_any_frame(page, selectors):
    for fr in [page] + list(page.frames):
        sel, el = _find_visible(fr, selectors)
        if sel:
            return fr, sel, el
    return None, None, None


def _frame_text(frame):
    try:
        return frame.evaluate("() => document.body ? document.body.innerText : ''") or ""
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 真机输入：屏幕坐标点击 / 键盘
# ---------------------------------------------------------------------------
def _install_mousemove_probe(page):
    try:
        page.evaluate(
            "() => { if (window.__lastMM !== undefined && window.__mmOn) return 'ok';"
            " window.__lastMM = null; window.__mmOn = true;"
            " window.addEventListener('mousemove', e => {"
            "   window.__lastMM = {x: e.clientX, y: e.clientY}; }, true); return 'ok'; }")
        return True
    except Exception:
        return False


def _calibrate(page, timeout_s=6.0):
    """SendInput 把鼠标移到「主 frame 内、不被 iframe 覆盖」的点，读回 client 坐标求偏移。

    返回 (ox, oy)：screen = client + (ox, oy)。
    注意：鼠标事件**不跨 iframe 冒泡**，校准点必须落在主 frame 自己的文档上，
    否则 window.__lastMM 读不到（这是 routec_core 校准点的同类坑）。
    """
    _install_mousemove_probe(page)
    try:
        pt = page.evaluate("""() => {
            const W = window.innerWidth, H = window.innerHeight;
            const cands = [[Math.round(W*0.06), Math.round(H*0.30)],
                           [Math.round(W*0.94), Math.round(H*0.30)],
                           [Math.round(W*0.06), Math.round(H*0.80)],
                           [Math.round(W*0.94), Math.round(H*0.80)],
                           [Math.round(W*0.50), Math.round(H*0.04)]];
            for (const [x, y] of cands) {
                if (x < 2 || y < 2 || x > W-2 || y > H-2) continue;
                const el = document.elementFromPoint(x, y);
                if (!el) continue;
                if (el.tagName === 'IFRAME') continue;
                return {x, y, W, H, tag: el.tagName};
            }
            return null;
        }""")
    except Exception:
        pt = None
    if not pt:
        return None
    x, y = int(pt["x"]), int(pt["y"])
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            driverctl.move(x, y)
        except Exception as e:
            logger.warning(f"password_login: 校准 move 失败: {type(e).__name__}")
            return None
        time.sleep(0.3)
        try:
            c = page.evaluate("() => window.__lastMM")
        except Exception:
            c = None
        if not c:
            # Windows 对「位置未变」的移动不派发 mousemove —— 先挪开再回来
            try:
                driverctl.move(x + 23, y + 17)
            except Exception:
                pass
            time.sleep(0.15)
            continue
        return int(x - c["x"]), int(y - c["y"])
    return None


def _client_point(page, frame, selector):
    """元素中心在「主 frame viewport」的 client 坐标（Playwright 的 bounding_box 已含 iframe 偏移）。"""
    try:
        el = frame.query_selector(selector)
        if el is None:
            return None
        bb = el.bounding_box()
    except Exception:
        return None
    if not bb:
        return None
    return bb["x"] + bb["width"] / 2.0, bb["y"] + bb["height"] / 2.0


def _real_click(page, frame, selector, offset):
    """用 SendInput 点元素（不是 CDP 合成点击）。offset 为 _calibrate 的返回值。"""
    pt = _client_point(page, frame, selector)
    if pt is None:
        return False, "no_rect"
    if offset is None:
        return False, "no_calibration"
    sx = int(round(pt[0] + offset[0]))
    sy = int(round(pt[1] + offset[1]))
    try:
        driverctl.click(sx, sy)
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}"
    return True, ""


def _focus_chrome_window():
    """把 VM101 Chrome 主窗口置前（SendInput 只到前台窗口）。"""
    try:
        r = driverctl.call({"action": "focus", "class": CHROME_CLASS,
                            "exe": CHROME_EXE, "title": "Google Chrome"}, timeout=8.0)
        d = (r.get("data") or {})
        return bool(d.get("found"))
    except Exception as e:
        logger.warning(f"password_login: Chrome 置前失败: {type(e).__name__}")
        return False


def _focus_element(frame, selector):
    """DOM 聚焦（不是输入合成，只是把 activeElement 指到目标）。"""
    try:
        return bool(frame.evaluate(
            "(sel) => { const e = document.querySelector(sel); if (!e) return false;"
            " e.focus(); if (e.select) { try { e.select(); } catch (err) {} } return true; }",
            selector))
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 滑块（登录页内联 nc）：真机 SendInput 拖动
# ---------------------------------------------------------------------------
def _solve_inline_slider(page, rnd, timeout_s=45.0):
    """登录页出现的 nc 滑块 → 用 SendInput 拖动。

    说明：这里**不能**直接调 ``utils/slider_route_c.py`` 的 ``get_route_c_slider()`` ——
    它的后端（``slider_routec/routec_core.run_round``）会先 ``clear_cookies()`` 再
    ``goto`` 到 goofish.com/im 去逼出**新鲜**挑战，等于把正在进行的这次登录整个抹掉。
    所以复用同一套**拖动原语**（driverctl.drag + humanize.drag_track），在当前页面上拖。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        fr, sel, _el = _find_any_frame(page, [SEL_SLIDER_HANDLE])
        if not fr:
            return "absent"
        off = _calibrate(page)
        if off is None:
            return "calibrate_failed"
        try:
            h = fr.query_selector(SEL_SLIDER_HANDLE)
            hb = h.bounding_box()
            tb = None
            for ts in SEL_SLIDER_TRACK:
                t = fr.query_selector(ts)
                if t:
                    bb = t.bounding_box()
                    if bb and bb["width"] > 0:
                        tb = bb
                        break
        except Exception:
            hb = tb = None
        if not hb or not tb:
            return "geometry_failed"

        sx = int(round(hb["x"] + hb["width"] / 2.0 + off[0]))
        sy = int(round(hb["y"] + hb["height"] / 2.0 + off[1]))
        dx = int(round(tb["x"] + tb["width"] - (hb["x"] + hb["width"])))
        if dx <= 0:
            return "bad_distance"

        steps = rnd.randint(40, 48)
        gap_us = rnd.choice([3500, 4000, 4500])
        try:
            cursor = driverctl.probe()["data"]["cursor"]
        except Exception:
            cursor = [sx - 120, sy + 60]
        try:
            path = humanize.approach_path(cursor, (hb["x"] + off[0], hb["y"] + off[1]),
                                          (sx, sy), rnd)
            for (qx, qy) in path:
                driverctl.move(int(round(qx)), int(round(qy)))
                time.sleep(0.006)
            time.sleep(0.12)
            pts = humanize.drag_track(dx, steps=steps, seed=rnd.randint(1, 99999))
            driverctl.drag([sx, sy], pts, sub=6, sub_gap_us=gap_us,
                           press_ms=150, release_ms=170, timeout=180)
        except Exception as e:
            return f"drag_error:{type(e).__name__}"

        # 等容器消失 = 通过
        end = time.time() + 12
        while time.time() < end:
            _fr2, _sel2, el2 = _find_any_frame(page, [SEL_SLIDER_HANDLE])
            if not el2:
                return "pass"
            time.sleep(0.5)
        # 还在 → 再来一轮（不硬刷：单次登录内最多 3 轮）
        if time.time() < deadline:
            time.sleep(2.0)
    return "still_present"


# ---------------------------------------------------------------------------
# 单轮密码登录（同步；在 to_thread 里跑）
# ---------------------------------------------------------------------------
def _new_result():
    return {"ok": False, "status": "failed", "reason": "", "detail": "",
            "elapsed_s": 0.0, "cookie_count": 0, "cookie_len": 0,
            "notified": 0, "preflight": None, "slider": None,
            "login": None, "screenshot": None, "restart": None,
            "switched_from": None, "user_len": 0}


def _read_browser_cookies(ctx):
    try:
        return ctx.cookies()
    except Exception:
        return []


def _cookie_str_from_browser(ctx):
    """把浏览器 cookie 拼成 'k=v; k=v'（排除挑战痕迹）。"""
    out = {}
    for c in _read_browser_cookies(ctx):
        n = c.get("name")
        if not n or n in EXCLUDE_COOKIES:
            continue
        dom = c.get("domain") or ""
        if "goofish" not in dom and "taobao" not in dom and "alibaba" not in dom:
            continue
        out[n] = c.get("value") or ""
    return "; ".join(f"{k}={v}" for k, v in out.items()), out


def _detect_error(frame):
    if frame is None:
        return None
    txt = _frame_text(frame)
    for kw in ERR_TEXTS:
        if kw in txt:
            return kw
    return None


def _detect_manual(page):
    """人脸 / 短信验证的判定（先排滑块）。返回 (bool, detail)。"""
    if _find_any_frame(page, [SEL_SLIDER_HANDLE])[0] is not None:
        return False, "slider"
    for fr in [page] + list(page.frames):
        txt = _frame_text(fr)
        if not txt:
            continue
        for kw in MANUAL_TEXTS:
            if kw in txt:
                return True, kw
    # 提交后登录框切到「填验证码」面板（#J_Checkcode / #fm-smscode）= 需要人工收码
    if _find_any_frame(page, SEL_MANUAL_CODE)[0] is not None:
        return True, "短信验证码"
    return False, ""


def _take_screenshot(page, cid):
    try:
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        for old in SHOT_DIR.glob(f"pwd_login_{cid}_*.jpg"):
            try:
                old.unlink()
            except Exception:
                pass
        path = SHOT_DIR / f"pwd_login_{cid}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
        # page.screenshot 走 CDP Page.captureScreenshot —— 只读，不是输入合成
        page.screenshot(path=str(path), full_page=False, type="jpeg", quality=80)
        return str(path)
    except Exception as e:
        logger.warning(f"【{cid}】截图失败: {type(e).__name__}: {str(e)[:120]}")
        return None


def run_password_login(cid, require_password_form=False, verify_api=True,
                       timeout_s=240.0):
    """执行一次完整密码登录。永不抛异常；返回结构化结果（不含任何凭据值）。

    require_password_form=True：即使注入后仍处于登录态，也清掉浏览器 cookie 强制
    走密码表单（用于验证/修复路径；默认 False —— 注入即可登录时不消耗账号）。
    """
    out = _new_result()
    t0 = time.time()
    rnd = random.Random(int(time.time() * 1000) ^ (hash(cid) & 0xFFFF))
    try:
        # ---- 0) 前置体检（六项 + 自愈）：不过即在发起任何闲鱼请求前中止 ----
        try:
            pf = preflight.check_and_heal(timeout_s=min(90.0, timeout_s))
        except Exception as e:
            pf = {"ok": False, "blocked_at": "preflight", "steps": [],
                  "detail": f"{type(e).__name__}: {str(e)[:150]}"}
        out["preflight"] = {k: v for k, v in pf.items() if k != "steps"}
        if not pf.get("ok"):
            out.update(reason="preflight", status="preflight",
                       detail=str(pf.get("blocked_at") or "unknown"))
            return out

        creds = _load_credentials(cid)
        if not creds:
            out.update(reason="no_credentials", detail="cookies.username/password 为空")
            return out
        username, password = creds
        out["user_len"] = len(username)   # 只留长度：手机号/账号值不得进日志与返回值

        deadline = t0 + timeout_s
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(preflight.CDP_URL)
            try:
                ctx = browser.contexts[0]
                page = _pick_page(ctx)
                try:
                    page.bring_to_front()
                except Exception:
                    pass

                # ---- 2) 打开登录页 ----
                try:
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
                except Exception as e:
                    out.update(reason="goto_failed", detail=f"{type(e).__name__}: {str(e)[:150]}")
                    return out
                time.sleep(2.5)

                # ---- 3) 多账号切换：单 profile，靠 cookie 注入换身份 ----
                cur_unb = ""
                for c in _read_browser_cookies(ctx):
                    if c.get("name") == "unb":
                        cur_unb = c.get("value") or ""
                        break
                out["switched_from"] = cur_unb or None
                if cur_unb != cid:
                    target = _load_cookie_str(cid)
                    pairs = []
                    for part in (target or "").split("; "):
                        if "=" in part:
                            k, v = part.split("=", 1)
                            k = k.strip()
                            if k and k not in EXCLUDE_COOKIES:
                                pairs.append((k, v))
                    try:
                        ctx.clear_cookies()
                    except Exception:
                        pass
                    if pairs:
                        exp = time.time() + 30 * 86400
                        try:
                            ctx.add_cookies([{
                                "name": k, "value": v, "domain": ".goofish.com", "path": "/",
                                "expires": exp, "httpOnly": False, "secure": True,
                                "sameSite": "None",
                            } for k, v in pairs])
                        except Exception as e:
                            out.update(reason="cookie_inject_failed",
                                       detail=f"{type(e).__name__}: {str(e)[:150]}")
                            return out
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass
                    time.sleep(2.5)

                # ---- 表单就绪：默认页签是「短信/二维码」，需要切到「密码登录」 ----
                def _open_form(wait_s):
                    """(frame, id_sel, offset)；找不到返回 (None, None, offset|None)。

                    点完页签必须**验证**密码表单真的出现了（账号框 + 密码框同时可见）；
                    没出现就重试，最后一次退化成 Playwright 点击（非安全关键 UI 切换）。
                    只点一次就往下走是不行的：实测有一次页签没切过去，密码被送进了
                    短信页签的 6 位验证码框。
                    """
                    fr, sel = _find_form_frame(page, timeout_s=2.0)
                    if fr is not None:
                        return fr, sel, None
                    offx = _calibrate(page)
                    for attempt in (1, 2, 3):
                        tf, _ts, te = _find_any_frame(page, [SEL_TAB])
                        if tf is None:
                            break
                        clicked = False
                        if attempt < 3 and offx is not None:
                            clicked, _e = _real_click(page, tf, SEL_TAB, offx)
                        if not clicked:
                            try:
                                te.click()  # 兜底：非安全关键 UI 切换
                                clicked = True
                            except Exception:
                                clicked = False
                        if not clicked:
                            continue
                        time.sleep(1.8)
                        fr, sel = _find_form_frame(page, timeout_s=wait_s if attempt == 1 else 4.0)
                        if fr is not None:
                            return fr, sel, offx
                    return None, None, offx

                frame, id_sel, off = _open_form(8.0)
                if frame is None and require_password_form:
                    # 强制走密码表单：清掉浏览器 cookie 后重载（修复/验证路径）
                    try:
                        ctx.clear_cookies()
                    except Exception:
                        pass
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=45000)
                    except Exception:
                        pass
                    time.sleep(2.5)
                    frame, id_sel, off = _open_form(10.0)
                if frame is None:
                    # 注入的 cookie 还有效 → 已经是登录态，直接算成功
                    okc, ckmap = _cookie_str_from_browser(ctx)
                    if ckmap.get("unb") == cid:
                        api = _verify_api(okc) if verify_api else {"api_ok": True}
                        out.update(ok=True, status="success", reason="already_logged_in",
                                   cookie_count=len(ckmap), cookie_len=len(okc),
                                   login={"api": api}, _cookies=okc)
                        return out
                    out.update(reason="no_login_form", detail="未出现登录表单")
                    return out

                # ---- 4) 真机输入：先勾协议 → 账号 → Tab → 密码 → Enter ----
                if off is None:
                    off = _calibrate(page)
                if off is None:
                    out.update(reason="calibrate_failed",
                               detail="未观测到鼠标事件（Chrome 未置前？）")
                    return out

                login = {"typed_user": False, "agreement": None, "submit": None,
                         "focus_after_tab": None, "fallback_submit": None}
                _focus_chrome_window()
                time.sleep(0.3)

                # 4.1 用户协议先勾（真机点；失败退 focus+Space；再失败退 JS click）。
                #     放在打字之前：勾选会把焦点留在复选框上，若放在回车前，
                #     Enter 打在复选框上不会提交表单（实测踩过）。
                login["agreement"] = _ensure_agreement(page, frame, off)

                # 4.2 账号 → Tab → 密码（全部 SendInput）
                if not _focus_element(frame, SEL_ID[0]):
                    out.update(reason="focus_failed", detail=SEL_ID[0])
                    return out
                time.sleep(0.35)
                driverctl.type_text(username)
                time.sleep(0.5)
                login["typed_user"] = True
                driverctl.tap_vk(driverctl.VK_TAB)
                time.sleep(0.35)
                ae = _active_element(frame)
                login["focus_after_tab"] = ae
                if ae != "fm-login-password":
                    # Tab 没落到密码框（页面结构差异）→ DOM 聚焦（不是输入合成）
                    _focus_element(frame, SEL_PWD[0])
                    time.sleep(0.25)
                driverctl.type_text(password)
                time.sleep(0.4)

                # 4.3 提交：Enter（焦点在密码框上）
                _focus_chrome_window()
                try:
                    driverctl.tap_vk(driverctl.VK_ENTER)
                    login["submit"] = "enter"
                except Exception as e:
                    login["submit"] = f"enter_error:{type(e).__name__}"
                time.sleep(3.0)
                if not _submit_effect(page, frame):
                    # 回车没生效（页面差异）→ 真机点「登录」按钮
                    ok_btn, err_btn = _real_click(page, frame, SEL_SUBMIT, off)
                    login["fallback_submit"] = "click" if ok_btn else f"click_fail:{err_btn}"
                    time.sleep(3.0)

                # ---- 5) 滑块（内联 nc）→ 真机拖动 ----
                if _find_any_frame(page, [SEL_SLIDER_HANDLE])[0] is not None:
                    out["slider"] = _solve_inline_slider(page, rnd, timeout_s=45.0)
                else:
                    out["slider"] = "absent"

                # ---- 6/7) 结果判定：成功 / 人工验证 / 账密错误 / 超时 ----
                end = min(deadline, time.time() + 60.0)
                manual_detail = ""
                err_kw = None
                while time.time() < end:
                    okc, ckmap = _cookie_str_from_browser(ctx)
                    if ckmap.get("unb") == cid and ckmap.get("cookie2"):
                        api = _verify_api(okc) if verify_api else {"api_ok": True}
                        if api.get("api_ok"):
                            out.update(ok=True, status="success", reason="pass",
                                       cookie_count=len(ckmap), cookie_len=len(okc),
                                       login=login, _cookies=okc)
                            return out
                    # 滑块可能延迟出现
                    if _find_any_frame(page, [SEL_SLIDER_HANDLE])[0] is not None:
                        out["slider"] = _solve_inline_slider(page, rnd, timeout_s=45.0)
                        continue
                    is_manual, det = _detect_manual(page)
                    if is_manual:
                        manual_detail = det
                        break
                    err_kw = _detect_error(frame)
                    if err_kw:
                        break
                    time.sleep(1.5)

                if manual_detail:
                    shot = _take_screenshot(page, cid)
                    out.update(status="need_manual", reason="manual_verification",
                               detail=manual_detail, screenshot=shot, login=login)
                    return out
                if err_kw:
                    out.update(reason="bad_credentials", detail=err_kw, login=login)
                    return out
                out.update(reason="timeout", detail="等待登录结果超时", login=login)
                return out
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
    except Exception as e:
        out.update(reason="error", detail=f"{type(e).__name__}: {str(e)[:200]}")
        return out
    finally:
        out["elapsed_s"] = round(time.time() - t0, 1)


def _ensure_agreement(page, frame, off):
    """确保用户协议已勾选。返回 'already' / 'clicked' / 'space' / 'js' / 'failed' / 'absent'。"""
    try:
        el = frame.query_selector(SEL_AGREE)
        if el is None:
            return "absent"
        checked = bool(el.evaluate("e => !!e.checked || e.classList.contains('checked')"))
    except Exception:
        return "absent"
    if checked:
        return "already"
    ok_click, _err = _real_click(page, frame, SEL_AGREE, off)
    if ok_click:
        time.sleep(0.4)
        try:
            if bool(el.evaluate("e => !!e.checked || e.classList.contains('checked')")):
                return "clicked"
        except Exception:
            pass
    # 退路 1：聚焦 + 空格（仍是真机输入）
    if _focus_element(frame, SEL_AGREE):
        try:
            driverctl.tap_vk(driverctl.VK_SPACE)
            time.sleep(0.4)
            if bool(el.evaluate("e => !!e.checked || e.classList.contains('checked')")):
                return "space"
        except Exception:
            pass
    # 退路 2：JS 点击（非安全关键 UI，仅勾选协议）
    try:
        el.click()
        time.sleep(0.3)
        return "js"
    except Exception:
        return "failed"


def _verify_api(cookies_str):
    """闭环校验：用浏览器 cookie 打一次 mtop token 接口。"""
    try:
        rr = xianyu_api.fetch_token(cookies_str)
        ok = (rr.get("status") == 200 and bool(rr.get("accessToken"))
              and "punish" not in (rr.get("url") or ""))
        return {"api_ok": bool(ok), "status": rr.get("status"),
                "ret": (rr.get("ret") or [""])[0][:80],
                "has_token": bool(rr.get("accessToken"))}
    except Exception as e:
        return {"api_ok": False, "err": f"{type(e).__name__}: {str(e)[:120]}"}


# ---------------------------------------------------------------------------
# 回写 + 监听任务重启
# ---------------------------------------------------------------------------
def write_back(cid, cookies_str, restart=True):
    """cookie 回写 cookies.value + 重启监听任务。返回结构化结果（不含 cookie 值）。"""
    res = {"db": False, "restart": "skipped", "error": ""}
    try:
        res["db"] = bool(_db().update_cookie_account_info(cid, cookie_value=cookies_str))
    except Exception as e:
        res["error"] = f"db:{type(e).__name__}"
        logger.error(f"【{cid}】cookie 回写数据库失败: {type(e).__name__}: {str(e)[:120]}")
        return res
    if not restart:
        return res
    try:
        from app.cookie_manager import manager as cookie_manager
        if cookie_manager is None:
            res["restart"] = "unavailable:manager_none"
            return res
        cookie_manager.ensure_cookie_task(cid, cookies_str, restart=True)
        res["restart"] = "requested"
    except Exception as e:
        # 独立进程（非 App 上下文）没有 CookieManager 事件循环 —— 记下来，不致命
        res["restart"] = f"unavailable:{type(e).__name__}"
        res["error"] = f"restart:{type(e).__name__}: {str(e)[:120]}"
        logger.warning(f"【{cid}】监听任务重启未送达: {type(e).__name__}: {str(e)[:120]}")
    return res


# ---------------------------------------------------------------------------
# 对外：async 入口（XianyuLive 调用）
# ---------------------------------------------------------------------------
async def password_login_refresh(cid, trigger_reason="", force=False,
                                 require_password_form=False):
    """密码登录的对外入口。返回 True/False（是否成功拿到并回写了新 cookie）。

    含触发门（开关 / 阈值 / 冷却 / 连续失败停手）与事后处理：
      成功   → 回写 + 重启监听 + 计数清零 + 清冷却
      需人工 → 截图 + 通知 + 状态 need_manual + 进冷却
      失败   → 状态 failed + 进冷却 + 连续失败计数 +1（≥3 停手报 BLOCKED）
    """
    cfg = config()
    if not force:
        ok, why = should_attempt(cid)
        if not ok:
            logger.info(f"【{cid}】密码登录未触发（{why}）"
                        f"｜触发原因: {trigger_reason or '-'}")
            if why == "blocked":
                _ss_set(status_key(cid), "blocked")
                logger.error(f"【{cid}】BLOCKED：密码登录连续失败 {get_attempt_fail(cid)} 次，"
                             f"停止自动尝试，需人工介入")
            return False

    logger.info(f"【{cid}】开始密码登录（触发原因: {trigger_reason or '-'}）")
    res = await asyncio.to_thread(run_password_login, cid, require_password_form)
    safe = {k: v for k, v in res.items() if not k.startswith("_")}
    logger.info(f"【{cid}】密码登录结束: {safe}")

    status = res.get("status")
    if res.get("ok"):
        cookies_str = res.get("_cookies") or ""
        wb = await asyncio.to_thread(write_back, cid, cookies_str, True)
        res["restart"] = wb
        logger.info(f"【{cid}】cookie 已回写: db={wb['db']} restart={wb['restart']}")
        set_fail_count(cid, 0)
        _ss_set(attempt_fail_key(cid), 0)
        clear_cooldown(cid)
        _ss_set(status_key(cid), "ok")
        return True

    if status == "need_manual":
        shot = res.get("screenshot")
        notified = await asyncio.to_thread(notify_manual_required, cid, shot, None)
        res["notified"] = notified
        _ss_set(status_key(cid), "need_manual")
        set_cooldown(cid, cfg["cooldown_minutes"] * 60)
        logger.warning(f"【{cid}】需要人工验证（{res.get('detail')}）：已通知 {notified} 个渠道，"
                       f"冷却 {cfg['cooldown_minutes']} 分钟")
        return False

    if status == "preflight":
        _ss_set(status_key(cid), "preflight")
        logger.warning(f"【{cid}】前置体检未过（{res.get('detail')}），未发起登录、不消耗账号")
        return False

    # 失败：进冷却 + 连续失败计数
    n = get_attempt_fail(cid) + 1
    _ss_set(attempt_fail_key(cid), n)
    set_cooldown(cid, cfg["cooldown_minutes"] * 60)
    _ss_set(status_key(cid), "failed")
    if n >= MAX_LOGIN_ATTEMPTS:
        _ss_set(status_key(cid), "blocked")
        logger.error(f"【{cid}】BLOCKED：密码登录连续失败 {n} 次（上限 {MAX_LOGIN_ATTEMPTS}），"
                     f"停止自动尝试，需人工介入")
    else:
        logger.warning(f"【{cid}】密码登录失败（{res.get('reason')}: {res.get('detail')}），"
                       f"连续失败 {n}/{MAX_LOGIN_ATTEMPTS}")
    return False


# ---------------------------------------------------------------------------
# CLI（排障 / 验收用；密码永不进 argv —— 一律从 DB 读）
# ---------------------------------------------------------------------------
def _main():
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(description="密码登录编排（容器内 CLI）")
    ap.add_argument("--cookie-id", required=True)
    ap.add_argument("--force", action="store_true", help="跳过触发门（阈值/冷却）")
    ap.add_argument("--require-password-form", action="store_true",
                    help="即使注入后已是登录态，也清 cookie 强制走密码表单")
    ap.add_argument("--status", action="store_true", help="只打印触发状态")
    ap.add_argument("--no-writeback", action="store_true", help="不回写 cookie（只做登录）")
    ap.add_argument("--print-cookies-file", default="", help="把新 cookie 写到该文件（0600）")
    a = ap.parse_args()

    if a.status:
        print(_json.dumps(status_snapshot(a.cookie_id), ensure_ascii=False, indent=1))
        return 0

    if a.force:
        res = run_password_login(a.cookie_id, require_password_form=a.require_password_form)
    else:
        res = asyncio.run(password_login_refresh(
            a.cookie_id, trigger_reason="cli", force=False,
            require_password_form=a.require_password_form))

    if isinstance(res, bool):
        print(_json.dumps({"result": res, "status": status_snapshot(a.cookie_id)},
                          ensure_ascii=False, indent=1))
        return 0 if res else 1

    cookies_str = res.pop("_cookies", "") or ""
    if a.print_cookies_file and cookies_str:
        p = pathlib.Path(a.print_cookies_file)
        p.write_text(cookies_str)
        os.chmod(p, 0o600)
        res["cookies_file"] = str(p)
    if a.force and not a.no_writeback and cookies_str and res.get("ok"):
        res["write_back"] = write_back(a.cookie_id, cookies_str, True)
    print(_json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(_main())
