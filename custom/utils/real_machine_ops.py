"""真机兜底（L3）：用 VM101 常驻 Chrome **完整走一遍人工发布 / 下架流程**。

## 定位

链路分层（本板统一口径）：

    L1  协议层    utils/item_publish.py / utils/item_delete.py（mtop 直调）
    L2  过滑块    utils/slider_route_c.py（路线 C：VM101 真机 SendInput 拖滑块）
    L3  真机兜底  本模块 —— L1 + L2 都失败时，用 VM101 的浏览器把**整条人工流程**走完

调用时机由上层（``app/product_automation``）决定；**不要**放进定时循环里高频调用
（真机操作是秒级/步）。

## 硬约束（写死，不越线）

* 页面**关键操作**一律走 driver 的真机 ``SendInput``（``move`` / ``click`` / ``type`` / ``key``）；
  **不用** ``page.click()`` / ``page.type()`` —— 阿里 nc 会拒 CDP 合成事件。
  只有「读 DOM / 读坐标 / 注入 cookie / 设置文件输入」走 CDP。
* **图片上传**优先 CDP ``DOM.setFileInputFiles``（文件选择不是合成输入事件，风控风险低）。
* 单 profile 单账号：只在 VM101 现有 Chrome 里开标签页，不新建实例、不改 profile 路径。
* 元素定位失败 → **报 BLOCKED + 截图**，不猜选择器、不硬试。
* 全流程超时上限 ``DEFAULT_TIMEOUT_S``（600s = 10 分钟），超时即中止报 BLOCKED。
* 不打印 / 不回显 cookie、密码、token。

## 账号

单 profile + cookie 注入：进入前先比对 VM101 Chrome 当前登录的 ``unb`` 与传入
cookie 的 ``unb``；不符则用 CDP ``Network.setCookie`` 注入目标账号 cookie 后重载页面。

## 图片路径

``image_paths`` 是 **VM101 上可见**的路径（Windows 路径，如 ``Z:\\win\\x.jpg`` /
``C:\\win\\x.jpg``）。若传入的是容器内路径，可用环境变量
``REALOPS_STAGE_DIR``（容器内目录）与 ``REALOPS_STAGE_WIN_DIR``（对应 VM101 路径）
做一次拷贝搬运；两者缺一即明确报错，不猜。

## 对外接口

    browser_publish(cookies_str, *, title, desc, image_paths, price, ...) -> dict
    browser_delete(cookies_str, item_id, ...) -> dict

两个函数**永不抛异常**，一律返回结构化结果（含 ``ok`` / ``blocked_at`` / ``evidence_dir``）。
"""

from __future__ import annotations

import base64
import json
import os
import pathlib
import re
import shutil
import sys
import time
from typing import Any, Dict, List, Optional

from loguru import logger

# ---------------------------------------------------------------- 配置

PUBLISH_URL = "https://www.goofish.com/publish"
ITEM_URL = "https://www.goofish.com/item?id=%s"
LOGIN_URL = "https://www.goofish.com/im"

# 证据根目录（容器内路径；宿主对应 /root/docker/xianyu-butler/logs/realops/）
EVIDENCE_ROOT = os.environ.get("REALOPS_EVIDENCE_ROOT", "/app/logs/realops")

# 真机兜底总超时（秒）—— 卡片硬约束 10 分钟
DEFAULT_TIMEOUT_S = float(os.environ.get("REALOPS_TIMEOUT_S", "600"))

# 与路线 C 保持一致的「不进浏览器」cookie（挑战痕迹）
EXCLUDE_COOKIES = {"x5secdata", "x5sectag", "x5step", "x5sec", "bx-cookie-test"}

# 网页版不支持的分类 → 回退类目（无属性、必能网页发布）
DEFAULT_FALLBACK_CATEGORY = os.environ.get("REALOPS_FALLBACK_CATEGORY", "其他闲置")

# 图片搬运（可选）
STAGE_DIR = os.environ.get("REALOPS_STAGE_DIR", "")
STAGE_WIN_DIR = os.environ.get("REALOPS_STAGE_WIN_DIR", "")

# 元素选择器（全部实测过；找不到就 BLOCKED，不猜）
SEL_FILE_INPUT = "input[type=file]"
SEL_DESC_EDITOR = "[contenteditable]"
SEL_CATEGORY = ".ant-select-selector"
SEL_PRICE = 'input[placeholder="0.00"]'

_WIN_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")

# ---------------------------------------------------------------- 小工具


def _driverctl():
    """延迟导入 driverctl（slider_routec 在 /app/slider_routec，不在包路径里）。"""
    root = os.environ.get("ROUTEC_SRC_DIR", "/app/slider_routec")
    if root not in sys.path:
        sys.path.insert(0, root)
    import driverctl  # noqa: WPS433

    return driverctl


def _now() -> float:
    return time.time()


def _evidence_dir(task_id: Optional[str]) -> str:
    tag = task_id or ("run-" + time.strftime("%Y%m%d-%H%M%S"))
    d = os.path.join(EVIDENCE_ROOT, str(tag))
    os.makedirs(d, exist_ok=True)
    return d


def _parse_cookie_str(cookies_str: str) -> List[tuple]:
    pairs = []
    for part in (cookies_str or "").split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            k = k.strip()
            if k and k not in EXCLUDE_COOKIES:
                pairs.append((k, v))
    return pairs


def _cookie_unb(cookies_str: str) -> str:
    for k, v in _parse_cookie_str(cookies_str):
        if k == "unb":
            return v
    return ""


class _Blocked(Exception):
    """元素定位失败 / 页面结构与预期不符 —— 报 BLOCKED，不猜。"""


class _StepError(Exception):
    """本步失败（可继续判断的重试类错误）。"""


# ---------------------------------------------------------------- 页面操作


class _Session:
    """一次真机操作的上下文：CDP 连接、页面、driver 偏移、证据目录、步骤日志。"""

    def __init__(self, task_id: Optional[str], timeout_s: float):
        self.deadline = _now() + max(30.0, timeout_s)
        self.ev = _evidence_dir(task_id)
        self.steps: List[Dict[str, Any]] = []
        self.shots: List[str] = []
        self.offset = None  # (ox, oy)
        self.driver = _driverctl()
        self.pw = None
        self.browser = None
        self.ctx = None
        self.page = None
        self.cdp = None

    # ---- 日志 / 证据
    def step(self, name: str, **kw) -> None:
        rec = {"step": name, "t": round(_now(), 2)}
        rec.update(kw)
        self.steps.append(rec)
        logger.info("realops %s %s", name, json.dumps(kw, ensure_ascii=False)[:400])

    def check_deadline(self) -> None:
        if _now() > self.deadline:
            raise _StepError("整体超时（>%ds），中止" % int(DEFAULT_TIMEOUT_S))

    def shot(self, name: str) -> str:
        try:
            cdp = self.cdp or self.ctx.new_cdp_session(self.page)
            data = cdp.send("Page.captureScreenshot", {"format": "png"})
            path = os.path.join(self.ev, name if name.endswith(".png") else name + ".png")
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(data["data"]))
            if path not in self.shots:
                self.shots.append(path)
            return path
        except Exception as exc:  # 截图失败不致命
            logger.warning("realops 截图失败 %s: %s", name, exc)
            return ""

    def result(self, ok: bool, **kw) -> Dict[str, Any]:
        out = {
            "ok": bool(ok),
            "blocked_at": None,
            "detail": "",
            "evidence_dir": self.ev,
            "steps": self.steps,
        }
        out.update(kw)
        # BLOCKED / 异常分支也要把「已落盘的截图」带回结果里
        out["shots"] = list(dict.fromkeys(list(out.get("shots") or []) + self.shots))
        return out

    # ---- 连接
    def open(self) -> None:
        from playwright.sync_api import sync_playwright

        cdp_url = os.environ.get("ROUTEC_CDP_URL", "http://<VM102_IP>:9222")
        self.pw = sync_playwright().start()
        self.browser = self.pw.chromium.connect_over_cdp(cdp_url)
        self.ctx = self.browser.contexts[0]
        self.page = self._pick_page()
        self.cdp = self.ctx.new_cdp_session(self.page)

    def close(self) -> None:
        for fn in (lambda: self.browser and self.browser.close(),
                   lambda: self.pw and self.pw.stop()):
            try:
                fn()
            except Exception:
                pass

    def _pick_page(self):
        """优先复用「已在闲鱼站内」的标签页；商品详情页/其它站内页只做兜底，
        避免把发布成功后的详情页当成发布页用（那会不断把详情页改成发布页）。"""
        best = None
        for p in self.ctx.pages:
            try:
                u = p.url or ""
            except Exception:
                continue
            if "goofish.com" not in u:
                continue
            if "/im" in u or "/publish" in u:
                return p
            best = best or p
        return best or self.ctx.new_page()

    # ---- 账号
    def account(self) -> str:
        for c in self.ctx.cookies():
            if c.get("name") == "unb":
                return str(c.get("value") or "")
        return ""

    def ensure_account(self, cookies_str: str) -> Dict[str, Any]:
        """比对 VM101 当前登录账号与目标 cookie 的 unb；不符则注入并重载。"""
        want = _cookie_unb(cookies_str)
        before = self.account()
        info = {"want": want, "before": before, "switched": False, "after": before}
        if not want:
            raise _StepError("cookie 缺少 unb，无法确认账号")
        if before == want:
            return info
        pairs = _parse_cookie_str(cookies_str)
        try:
            self.cdp.send("Network.enable")
        except Exception:
            pass
        injected = 0
        for k, v in pairs:
            try:
                self.cdp.send("Network.setCookie", {
                    "name": k, "value": v, "domain": ".goofish.com", "path": "/",
                    "secure": True, "httpOnly": False, "sameSite": "None",
                })
                injected += 1
            except Exception:
                pass
        info["injected"] = injected
        # 清掉目标账号之外的 unb 残留（Network.setCookie 同域同名会覆盖，这里只做兜底）
        self.page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        time.sleep(4)
        after = self.account()
        info["after"] = after
        info["switched"] = after == want
        if not info["switched"]:
            raise _StepError("cookie 注入后账号仍不匹配（before=%s after=%s）" % (before, after))
        return info

    # ---- 元素
    def exists(self, sel: str) -> bool:
        """元素是否在 DOM 里（不看可见性——file input 是隐藏的）。"""
        try:
            return bool(self.page.evaluate("(sel) => !!document.querySelector(sel)", sel))
        except Exception:
            return False

    def box(self, sel: str, visible_only: bool = True) -> Optional[Dict[str, float]]:
        js = """(a) => { const [sel, vis] = a;
          const es = Array.from(document.querySelectorAll(sel));
          for (const e of es) { const r = e.getBoundingClientRect();
            if (!vis || (r.width > 0 && r.height > 0)) return {x: r.x, y: r.y, w: r.width, h: r.height}; }
          return null; }"""
        return self.page.evaluate(js, [sel, visible_only])

    def require_box(self, sel: str, what: str) -> Dict[str, float]:
        b = self.box(sel)
        if not b:
            self.shot("blocked-" + re.sub(r"[^a-z0-9]+", "-", what.lower()))
            raise _Blocked("找不到%s（selector=%s）" % (what, sel))
        return b

    def screen(self, box: Dict[str, float]) -> tuple:
        ox, oy = self.offset or (0, 0)
        return (round(box["x"] + box["w"] / 2 + ox), round(box["y"] + box["h"] / 2 + oy))

    def calibrate(self) -> tuple:
        """用一次已知的 driver move 反推 screen↔client 偏移（沿用路线 C 的做法）。"""
        self.page.evaluate(
            "() => { window.__lastMM=null; window.addEventListener('mousemove',"
            " e => { window.__lastMM={x:e.clientX,y:e.clientY}; }, true); }")
        self.driver.move(600, 500)
        time.sleep(0.4)
        c = self.page.evaluate("() => window.__lastMM")
        if not c:
            # 光标本来就在 (600,500) 时不会产生 WM_MOUSEMOVE —— 先挪开再回来
            self.driver.move(640, 520)
            time.sleep(0.2)
            self.driver.move(600, 500)
            time.sleep(0.4)
            c = self.page.evaluate("() => window.__lastMM")
        if not c:
            raise _StepError("screen↔client 校准失败：未观测到鼠标事件")
        self.offset = (600 - c["x"], 500 - c["y"])
        return self.offset

    def bring_front(self) -> None:
        try:
            self.page.bring_to_front()
        except Exception:
            pass
        try:
            self.driver.call({"action": "focus", "exe": "chrome.exe"}, timeout=20)
        except Exception:
            pass
        time.sleep(0.5)

    # ---- 真机输入
    def scroll_into_view(self, sel: str) -> None:
        """把目标滚到视口中间 —— 底部吸底的「发布」按钮会盖住价格框，必须先滚动。"""
        try:
            self.page.evaluate(
                """(sel) => { const es = Array.from(document.querySelectorAll(sel))
                     .filter(e => e.getBoundingClientRect().width > 0);
                   if (es[0]) es[0].scrollIntoView({block: 'center'}); }""", sel)
            time.sleep(0.5)
        except Exception:
            pass

    def hit_test(self, sel: str, box: Dict[str, float]) -> bool:
        """点击点上最顶层的元素必须属于目标（防吸底按钮 / 遮罩吃掉点击）。"""
        try:
            return bool(self.page.evaluate(
                """(a) => { const [sel, x, y] = a; const e = document.elementFromPoint(x, y);
                   if (!e) return false; return !!(e.closest && e.closest(sel)); }""",
                [sel, box["x"] + box["w"] / 2, box["y"] + box["h"] / 2]))
        except Exception:
            return False

    def click_box(self, box: Dict[str, float], settle: float = 0.8) -> tuple:
        x, y = self.screen(box)
        self.driver.move(x, y)
        time.sleep(0.15)
        self.driver.click(x, y)
        time.sleep(settle)
        return x, y

    def click_sel(self, sel: str, what: str, settle: float = 0.8) -> tuple:
        self.scroll_into_view(sel)
        return self.click_box(self.require_box(sel, what), settle)

    def click_focus(self, sel: str, what: str, focused_js: str, tries: int = 3) -> bool:
        """滚动 + 命中测试 + 点击，直到目标元素真的拿到焦点。"""
        for _ in range(tries):
            try:
                self.scroll_into_view(sel)
                box = self.require_box(sel, what)
            except _Blocked:
                return False
            if not self.hit_test(sel, box):
                # 被吸底按钮/遮罩盖住 → 再滚一次后重试
                self.scroll_into_view(sel)
                box = self.box(sel)
                if not box or not self.hit_test(sel, box):
                    continue
            self.click_box(box, 0.7)
            if self.page.evaluate(focused_js):
                return True
            time.sleep(0.4)
        return False

    def type_text(self, text: str) -> None:
        self.driver.type_text(text)

    def key(self, vk: int) -> None:
        self.driver.key_vk(vk)


# ---------------------------------------------------------------- 图片路径


def _stage_images(image_paths: List[str]) -> List[str]:
    """把容器内路径搬到 VM101 可见目录；已经是 Windows 路径的原样返回。"""
    out = []
    for p in image_paths:
        if _WIN_PATH_RE.match(p):
            out.append(p)
            continue
        if not STAGE_DIR or not STAGE_WIN_DIR:
            raise _StepError(
                "图片路径 %s 不是 VM101 可见路径；需设置 REALOPS_STAGE_DIR / REALOPS_STAGE_WIN_DIR" % p)
        if not os.path.isfile(p):
            raise _StepError("图片不存在: %s" % p)
        os.makedirs(STAGE_DIR, exist_ok=True)
        name = os.path.basename(p)
        dst = os.path.join(STAGE_DIR, name)
        shutil.copyfile(p, dst)
        out.append(STAGE_WIN_DIR.rstrip("\\/") + "\\" + name)
    return out


def _set_file_input(sess: _Session, win_paths: List[str]) -> None:
    """CDP DOM.setFileInputFiles（不是合成输入事件）。"""
    doc = sess.cdp.send("DOM.getDocument", {"depth": -1})
    node = sess.cdp.send("DOM.querySelector",
                         {"nodeId": doc["root"]["nodeId"], "selector": SEL_FILE_INPUT})
    if not node.get("nodeId"):
        sess.shot("blocked-no-file-input")
        raise _Blocked("发布页没有 input[type=file]")
    sess.cdp.send("DOM.setFileInputFiles", {"files": win_paths, "nodeId": node["nodeId"]})


# ---------------------------------------------------------------- 发布


def _cat_state(page) -> Dict[str, Any]:
    return page.evaluate("""() => {
      const t = document.body.innerText;
      const m = t.match(/分类\\s*\\*\\s*([^\\n]+)/);
      return {sel: (document.querySelector('.ant-select-selection-item')||{}).innerText || '',
              body_cat: m ? m[1].trim() : '',
              unsupported: t.includes('网页版暂不支持发布此分类')};
    }""")


def _publish_button_box(sess: _Session) -> Optional[Dict[str, float]]:
    js = """() => { const es = Array.from(document.querySelectorAll('button,div[class*=publish],a[class*=publish]'));
      for (const e of es) { if ((e.innerText||'').trim() !== '发布') continue;
        const r = e.getBoundingClientRect(); if (r.width > 0 && r.height > 0)
          return {x: r.x, y: r.y, w: r.width, h: r.height}; }
      return null; }"""
    for _ in range(3):
        b = sess.page.evaluate(js)
        if b:
            return b
        time.sleep(1.0)
    return None


def _item_ids_in_pages(ctx) -> set:
    ids = set()
    for p in ctx.pages:
        try:
            u = p.url or ""
        except Exception:
            continue
        if "/item" in u:
            m = re.search(r"[?&]id=(\d+)", u)
            if m:
                ids.add(m.group(1))
    return ids


def _new_item_page_id(ctx, before_ids: set, title: str) -> str:
    """只认「本次发布**新开**的详情页」——发布前就存在的详情页标签页绝不能当成本次结果
    （否则测试号被平台拒绝时，会把上一件商品的 itemId 误报为成功）。"""
    for p in ctx.pages:
        try:
            u = p.url or ""
            t = p.title() or ""
        except Exception:
            continue
        if "/item" not in u:
            continue
        m = re.search(r"[?&]id=(\d+)", u)
        if not m or m.group(1) in before_ids:
            continue
        if title and title[:8] and title[:8] not in t:
            continue
        return m.group(1)
    return ""


def _extract_item_id(responses: List[Dict[str, Any]], page_url: str) -> str:
    for r in reversed(responses):
        if r.get("item_id"):
            return str(r["item_id"])
        try:
            data = json.loads(r.get("body") or "{}")
        except Exception:
            continue
        item = ((data.get("data") or {}).get("itemId")) if isinstance(data.get("data"), dict) else None
        if item:
            return str(item)
    m = re.search(r"[?&]id=(\d+)", page_url or "")
    return m.group(1) if m else ""


def browser_publish(
    cookies_str: str,
    *,
    title: str,
    desc: str,
    image_paths: List[str],
    price: float = 1,
    original_price: Optional[float] = None,
    category: Optional[str] = None,
    fallback_category: str = DEFAULT_FALLBACK_CATEGORY,
    task_id: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """用 VM101 浏览器完整发布一件商品（真机 SendInput）。

    Args:
        cookies_str: 账号 cookie 扁平串（含 ``unb``），用于账号比对 / 注入。
        image_paths: **VM101 可见**的图片路径（Windows 路径）。
        category: 期望类目；``None`` 表示接受页面 AI 自动识别结果。
        fallback_category: 自动识别出的类目「网页版不支持」时改选它。

    Returns:
        ``{ok, item_id, blocked_at, detail, evidence_dir, steps, account, category,
        price_value, shots}``。永不抛异常。
    """
    sess = _Session(task_id, timeout_s)
    shots: List[str] = []
    extra: Dict[str, Any] = {"item_id": "", "account": "", "category": "", "price_value": ""}
    try:
        if not image_paths:
            raise _StepError("image_paths 为空")
        win_paths = _stage_images(list(image_paths))
        sess.open()
        extra["account"] = sess.ensure_account(cookies_str)
        sess.step("account", **{k: v for k, v in extra["account"].items()})

        sess.page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            sess.page.wait_for_selector(SEL_FILE_INPUT, state="attached", timeout=30000)
        except Exception:
            pass
        time.sleep(3)
        sess.check_deadline()
        if not sess.exists(SEL_FILE_INPUT):
            sess.shot("blocked-publish-page")
            body = sess.page.evaluate("() => document.body.innerText.replace(/\\s+/g,' ').slice(0,200)")
            raise _Blocked("发布页未加载出文件输入框（url=%s body=%s）" % (sess.page.url[:80], body))
        sess.step("page", url=sess.page.url, title=sess.page.title())

        _set_file_input(sess, win_paths)
        sess.step("upload", files=len(win_paths))
        time.sleep(8)
        if not sess.box(SEL_DESC_EDITOR):
            sess.shot("blocked-after-upload")
            raise _Blocked("上传后未出现描述编辑器")
        shots.append(sess.shot("01-uploaded"))

        sess.bring_front()
        sess.calibrate()
        sess.step("calibrate", offset=list(sess.offset))

        # 1) 描述（标题 + 换行 + 正文）——网页版没有独立标题框，首行即标题
        ok = sess.click_focus(
            SEL_DESC_EDITOR, "描述编辑器",
            "() => { const a=document.activeElement; return !!a && a.getAttribute"
            " && a.getAttribute('contenteditable')==='true'; }")
        if not ok:
            sess.shot("blocked-desc-focus")
            raise _Blocked("描述编辑器点不进去")
        sess.type_text(title)
        time.sleep(0.4)
        sess.key(sess.driver.VK_ENTER)
        time.sleep(0.3)
        if desc:
            sess.type_text(desc)
        time.sleep(5)
        desc_text = sess.page.evaluate(
            "() => (document.querySelector('[contenteditable]')||{}).innerText || ''")
        extra["desc_text"] = desc_text
        if title and title not in desc_text:
            sess.shot("blocked-desc-text")
            raise _Blocked("描述框内未出现标题文本")
        sess.step("desc", chars=len(desc_text))
        shots.append(sess.shot("02-desc"))

        # 2) 类目
        cat = _cat_state(sess.page)
        extra["category"] = cat.get("body_cat") or cat.get("sel") or ""
        if cat.get("unsupported"):
            if not fallback_category:
                sess.shot("blocked-category")
                raise _Blocked("类目「%s」网页版不支持，且未给 fallback" % extra["category"])
            sess.click_sel(SEL_CATEGORY, "分类下拉", 1.2)
            sess.type_text(fallback_category)
            time.sleep(1.2)
            sess.key(sess.driver.VK_ENTER)
            time.sleep(1.5)
            sess.key(sess.driver.VK_ESC)
            time.sleep(0.6)
            cat = _cat_state(sess.page)
            extra["category"] = cat.get("body_cat") or cat.get("sel") or ""
            extra["category_fallback"] = True
            if cat.get("unsupported"):
                sess.shot("blocked-category-2")
                raise _Blocked("回退类目「%s」仍不支持网页发布" % fallback_category)
        sess.step("category", value=extra["category"])
        shots.append(sess.shot("03-category"))

        # 3) 价格
        ok = sess.click_focus(
            SEL_PRICE, "价格输入框",
            "() => { const a=document.activeElement; return !!a && a.getAttribute &&"
            " a.getAttribute('placeholder')==='0.00' && a.getBoundingClientRect().width>0; }")
        if not ok:
            sess.shot("blocked-price-focus")
            raise _Blocked("价格输入框点不进去")
        sess.type_text(str(int(price)) if float(price).is_integer() else str(price))
        time.sleep(1.5)
        vals = sess.page.evaluate("""() => Array.from(document.querySelectorAll('input[placeholder="0.00"]'))
            .filter(e => e.getBoundingClientRect().width > 0).map(e => e.value)""")
        extra["price_value"] = vals[0] if vals else ""
        if not extra["price_value"]:
            sess.shot("blocked-price-value")
            raise _Blocked("价格未写入")
        sess.step("price", value=extra["price_value"])
        shots.append(sess.shot("04-price"))

        # 4) 发布
        sess.check_deadline()
        responses: List[Dict[str, Any]] = []

        def _on_rsp(rsp):
            try:
                if any(k in rsp.url for k in ("idleitem", "item.publish", "publish")):
                    body = ""
                    try:
                        body = rsp.text()[:2000]
                    except Exception:
                        body = ""
                    rec = {"url": rsp.url[:200], "status": rsp.status}
                    try:
                        j = json.loads(body or "{}")
                        rec["ret"] = [str(x) for x in (j.get("ret") or [])][:3]
                        rec["item_id"] = str(((j.get("data") or {}).get("itemId")) or "")
                    except Exception:
                        rec["ret"] = []
                    if rec not in responses:  # page 级 + context 级监听会重复上报同一条
                        responses.append(rec)
            except Exception:
                pass

        sess.page.on("response", _on_rsp)
        try:  # 兜底：发布成功会新开标签页，响应可能落在别的 page 上
            sess.ctx.on("response", _on_rsp)
        except Exception:
            pass
        btn = _publish_button_box(sess)
        if not btn:
            sess.shot("blocked-publish-button")
            raise _Blocked("找不到「发布」按钮")
        before_ids = _item_ids_in_pages(sess.ctx)
        sess.click_box(btn, 2.0)
        sess.step("publish-click", box=[round(v) for v in btn.values()], before_ids=sorted(before_ids))

        item_id = ""
        deadline = _now() + 90
        while _now() < deadline:
            time.sleep(3)
            item_id = (_extract_item_id(responses, sess.page.url)
                       or _new_item_page_id(sess.ctx, before_ids, title))
            if item_id:
                break
        extra["item_id"] = item_id
        extra["responses"] = responses[-4:]
        extra["ret"] = (responses[-1].get("ret") if responses else []) or []
        extra["pages"] = [p.url[:120] for p in sess.ctx.pages]
        extra["after_url"] = sess.page.url[:160]
        extra["after_title"] = sess.page.title()[:60]
        shots.append(sess.shot("05-after-publish"))
        if not item_id:
            detail = "；".join(str(x.get("ret")) for x in responses[-2:]) or "无发布响应"
            raise _Blocked("点击发布后未取到 itemId（平台返回 %s）" % detail[:300])
        sess.step("published", item_id=item_id)
        return sess.result(True, shots=shots, **extra)
    except _Blocked as exc:
        return sess.result(False, blocked_at="blocked", detail=str(exc), shots=shots, **extra)
    except _StepError as exc:
        return sess.result(False, blocked_at="error", detail=str(exc), shots=shots, **extra)
    except Exception as exc:  # noqa: BLE001
        try:
            shots.append(sess.shot("99-exception"))
        except Exception:
            pass
        return sess.result(False, blocked_at="exception",
                           detail="%s: %s" % (type(exc).__name__, str(exc)[:300]),
                           shots=shots, **extra)
    finally:
        sess.close()


# ---------------------------------------------------------------- 下架 / 删除


def _delete_controls(page) -> List[Dict[str, Any]]:
    """商品详情页上「管理」类控件（下架 / 删除）。"""
    return page.evaluate("""() => {
      const out = [];
      const els = Array.from(document.querySelectorAll('button,a,div[class],span[class]'));
      for (const e of els) {
        const t = (e.innerText || '').trim();
        if (!t || t.length > 8) continue;
        if (!/下架|删除|管理|更多/.test(t)) continue;
        const r = e.getBoundingClientRect();
        if (r.width <= 0 || r.height <= 0) continue;
        out.push({text: t, tag: e.tagName, cls: String(e.className).slice(0, 70),
                  box: {x: r.x, y: r.y, w: r.width, h: r.height}});
      }
      return out.slice(0, 20);
    }""")


def browser_delete(
    cookies_str: str,
    item_id: str,
    *,
    mode: str = "offline",
    task_id: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """用 VM101 浏览器把指定商品下架 / 删除（真机 SendInput）。

    Args:
        mode: ``"offline"``（优先「下架」）/ ``"delete"``（优先「删除」）/ ``"auto"``（下架不成再删除）。

    Returns:
        ``{ok, blocked_at, detail, evidence_dir, steps, account, controls, shots}``。
        永不抛异常。
    """
    sess = _Session(task_id, timeout_s)
    shots: List[str] = []
    extra: Dict[str, Any] = {"account": "", "controls": [], "confirm_text": ""}
    try:
        if not item_id:
            raise _StepError("item_id 为空")
        sess.open()
        extra["account"] = sess.ensure_account(cookies_str)
        sess.step("account", **{k: v for k, v in extra["account"].items()})

        sess.page.goto(ITEM_URL % item_id, wait_until="domcontentloaded", timeout=45000)
        time.sleep(6)
        sess.check_deadline()
        sess.bring_front()
        sess.calibrate()
        shots.append(sess.shot("01-item-page"))

        controls = _delete_controls(sess.page)
        extra["controls"] = controls
        sess.step("controls", n=len(controls),
                  texts=[c["text"] for c in controls])
        if not controls:
            sess.shot("blocked-no-controls")
            raise _Blocked("商品页找不到「下架/删除/管理」控件（页面结构或权限不符）")

        # 控件优先级：offline→下架优先，delete→删除优先，auto→下架不成再删除
        order = {"offline": ("下架", "删除", "管理", "更多"),
                 "delete": ("删除", "下架", "管理", "更多"),
                 "auto": ("下架", "删除", "管理", "更多")}.get(mode, ("下架", "删除", "管理", "更多"))
        pick = None
        for want in order:
            for c in controls:
                if c["text"] == want:
                    pick = c
                    break
            if pick:
                break
        def _scroll_text(txt: str) -> None:
            try:
                sess.page.evaluate(
                    """(t) => { const els = Array.from(document.querySelectorAll('button,a,div[class],span[class]'));
                       for (const e of els) { if ((e.innerText||'').trim() !== t) continue;
                         const r = e.getBoundingClientRect();
                         if (r.width > 0 && r.height > 0) { e.scrollIntoView({block: 'center'}); return; } } }""", txt)
                time.sleep(0.5)
            except Exception:
                pass

        _scroll_text(pick["text"])
        fresh = [c for c in _delete_controls(sess.page) if c["text"] == pick["text"]]
        box = fresh[0]["box"] if fresh else pick["box"]
        sess.click_box(box, 1.5)
        sess.step("click-control", text=pick["text"], box=[round(v) for v in box.values()])
        shots.append(sess.shot("02-after-control"))

        # 二次确认弹窗
        conf = sess.page.evaluate("""() => {
          const es = Array.from(document.querySelectorAll('button,div[class],span[class]'));
          for (const e of es) { const t=(e.innerText||'').trim();
            if (t !== '确定' && t !== '确认' && t !== '确认下架' && t !== '确认删除') continue;
            const r = e.getBoundingClientRect();
            if (r.width > 0 && r.height > 0) return {text: t, box: {x: r.x, y: r.y, w: r.width, h: r.height}};
          }
          return null; }""")
        extra["confirm_text"] = (conf or {}).get("text", "")
        if conf:
            sess.click_box(conf["box"], 2.5)
            sess.step("confirm", text=conf["text"])
        time.sleep(6)
        shots.append(sess.shot("03-after-confirm"))

        extra["after_url"] = sess.page.url[:160]
        extra["after_text"] = sess.page.evaluate(
            "() => document.body.innerText.replace(/\\s+/g,' ').slice(0, 300)")
        ok = _verify_deleted(sess, item_id)
        extra["verified"] = ok
        if not ok:
            raise _Blocked("下架/删除后未能确认商品已消失（见截图与 after_text）")
        sess.step("deleted", item_id=item_id)
        return sess.result(True, shots=shots, **extra)
    except _Blocked as exc:
        return sess.result(False, blocked_at="blocked", detail=str(exc), shots=shots, **extra)
    except _StepError as exc:
        return sess.result(False, blocked_at="error", detail=str(exc), shots=shots, **extra)
    except Exception as exc:  # noqa: BLE001
        try:
            shots.append(sess.shot("99-exception"))
        except Exception:
            pass
        return sess.result(False, blocked_at="exception",
                           detail="%s: %s" % (type(exc).__name__, str(exc)[:300]),
                           shots=shots, **extra)
    finally:
        sess.close()


def _verify_deleted(sess: _Session, item_id: str) -> bool:
    """回读校验：重新打开商品详情页，确认已不可售 / 不存在。"""
    try:
        sess.page.goto(ITEM_URL % item_id, wait_until="domcontentloaded", timeout=45000)
        time.sleep(5)
        txt = sess.page.evaluate("() => document.body.innerText.replace(/\\s+/g,' ')")
        sess.step("verify-item", sample=txt[:200])
        gone = ("已下架" in txt or "已被删除" in txt or "不存在" in txt
                or "宝贝不存在" in txt or "已删除" in txt)
        if not gone:
            # 再看「我想要 / 立即购买」这类在售标志是否仍在
            on_sale = ("立即购买" in txt or "我想要" in txt)
            return not on_sale
        return True
    except Exception:
        return False


__all__ = ["browser_publish", "browser_delete"]
