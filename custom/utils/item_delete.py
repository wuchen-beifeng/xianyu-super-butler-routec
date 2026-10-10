"""闲鱼商品下架 / 删除。

接口 ``com.taobao.idle.item.delete``（H5 端，**version 1.1**）。

命名空间是 ``com.taobao.idle.*`` —— 与擦亮（``mtop.taobao.idle.item.polish``）、
搜索（``mtop.taobao.idlemtopsearch.pc.search``）**都不同**，照抄别的前缀会直接
``接口不存在``。来源：goofish-cli ``commands/item/delete.py``（全文 32 行）。

该接口在平台上到底是「下架」还是「真删除」—— goofish-cli 自己的文档三处措辞
不一致（README「下架/删除」、mcp-setup「下架商品」、compliance「发布与下架」）。

**实测结论（2026-10-10，主号 2206931765880，itemId 1087146242588）：真删除（delete），
不可逆。** 调用返回 ``['SUCCESS::调用成功']`` 后，该商品同时从
``mtop.idle.web.xyh.item.list`` 在售列表消失，且 ``mtop.taobao.idle.pc.detail``
回 ``FAIL_BIZ_ITEM_DEL_NOT_FOUND::您要看的宝贝不存在或已被删除啦!`` ——
列表与详情都不再可取，是**删除**而非下架。详见同卡 ``RESULT.md``。

本模块**不猜**：把实测结论写死在 :data:`SEMANTICS`，并在返回值里带 ``semantics``
字段，让上层（W10 编排）不必自己判断。

写操作必须过 :mod:`utils.write_guard` 的写限流（``acquire_write``）：
超限一律 **fail fast**，返回 ``ok=False`` + ``rate_limited=True``，**不 sleep 等待**
（等待会卡住事件循环）。
"""

import json
import time
from typing import Any, Dict, List, Optional

import aiohttp
from loguru import logger

from utils.user_agents import CHROME_UA
from utils.xianyu_utils import generate_sign, trans_cookies


DELETE_API = "com.taobao.idle.item.delete"
DELETE_URL = f"https://h5api.m.goofish.com/h5/{DELETE_API}/1.1/"
APP_KEY = "34839810"
USER_AGENT = CHROME_UA

# 该接口的版本是 1.1（不是擦亮的 2.0，也不是搜索的 1.0）
API_VERSION = "1.1"
SPM_CNT = "a21ybx.item.0.0"

# ---------------------------------------------------------------- L3 真机兜底（W9）

L3_FALLBACK_KEY = "real_machine_fallback"


def _l3_enabled() -> bool:
    """``system_settings.real_machine_fallback``：默认 **false**（真机兜底只允许显式开启）。"""
    try:
        from app.db_manager import db_manager

        raw = db_manager.get_system_setting(L3_FALLBACK_KEY)
    except Exception as exc:
        logger.warning(f"读取 real_machine_fallback 失败，按关闭处理: {exc}")
        return False
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on", "y")


async def l3_browser_delete(
    cookies_str: str,
    item_id: str,
    *,
    mode: str = "offline",
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """L3 兜底：L1（协议）+ L2 都失败时，用 VM101 浏览器把商品下架 / 删除。

    ``real_machine_ops.browser_delete`` 是同步阻塞实现，放到线程里跑，避免卡住事件循环。
    永不抛异常。
    """
    try:
        from utils.real_machine_ops import browser_delete
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "blocked_at": "import", "detail": f"real_machine_ops 不可用: {exc}"}
    try:
        import asyncio

        return await asyncio.to_thread(browser_delete, cookies_str, item_id,
                                       mode=mode, task_id=task_id)
    except Exception as exc:
        return {"ok": False, "blocked_at": "exception", "detail": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------- 实测语义
# 实测（2026-10-10，无尘明确授权用主号 2206931765880 做这一件测试）：
#   目标 itemId 1087146242588（标题「电脑软件：Windows 桌面工具…」，在售，8.80；
#   同标题另有 4 条重复商品 → 选它做测试，影响最小）。
#   调用前：在售列表有它（itemStatus=0），pc.detail 可取到。
#   调用后：ret = ['SUCCESS::调用成功']；
#           在售列表总数 15→14，该商品消失；
#           pc.detail 回 FAIL_BIZ_ITEM_DEL_NOT_FOUND::您要看的宝贝不存在或已被删除啦!
#   → 结论 = 真删除（delete），**不可逆**：既不在售、详情也取不到。
#   （与「不存在的 itemId」不同：后者 delete 回 FAIL_BIZ_BAD_REQUEST，
#     而真商品被删后 detail 回 FAIL_BIZ_ITEM_DEL_NOT_FOUND —— 平台能区分。）
SEMANTICS = "delete"  # 实测值：offline=下架(记录仍在) / delete=真删除(不可逆)

# 风控关键词（照抄 goofish-cli core/mtop.py 的 _RISK_KEYWORDS）
RISK_KEYWORDS = ("RGV587_ERROR", "FAIL_SYS_USER_VALIDATE", "哎哟喂", "/punish")

# 令牌过期：mtop 首次请求会回 FAIL_SYS_TOKEN_EXOIRED 并下发新的 _m_h5_tk，
# 用新令牌重签重试一次即可成功（与 utils/xianyu_seller_api.py 同款处理）。
TOKEN_KEYWORDS = ("FAIL_SYS_TOKEN_EXOIRED", "FAIL_SYS_TOKEN_EMPTY", "令牌过期", "TOKEN_EXPIRED")

# 「商品不存在 / 已删除」类错误
NOT_EXIST_KEYWORDS = (
    "ITEM_NOT_EXIST",
    "ITEM_NOT_FOUND",
    "NOT_EXIST",
    "ITEM_DELETED",
    "不存在",
    "已被删除",
    "商品已删除",
)

MAX_TOKEN_RETRY = 2


def _join_ret(result: Any) -> List[str]:
    if isinstance(result, dict):
        ret = result.get("ret")
        if isinstance(ret, list):
            return [str(v) for v in ret]
        if ret is not None:
            return [str(ret)]
    return []


def _has_keyword(ret_list: List[str], keywords) -> bool:
    joined = " ".join(ret_list)
    return any(kw in joined for kw in keywords)


def _classify(ret_list: List[str]) -> str:
    """把 ``ret`` 归到 ``success`` / ``risk`` / ``not_found`` / ``token_expired`` / ``other``。"""
    if any("SUCCESS" in v for v in ret_list):
        return "success"
    if _has_keyword(ret_list, RISK_KEYWORDS):
        return "risk"
    if _has_keyword(ret_list, TOKEN_KEYWORDS):
        return "token_expired"
    if _has_keyword(ret_list, NOT_EXIST_KEYWORDS):
        return "not_found"
    return "other"


def _message_for(category: str, ret_list: List[str]) -> str:
    raw = "; ".join(ret_list) or "未知响应"
    if category == "success":
        # 实测本接口是「真删除」而非「下架」（见 SEMANTICS），文案据实描述
        return f"删除成功（{raw}）"
    if category == "risk":
        return f"被风控拦截，请稍后再试（{raw}）"
    if category == "not_found":
        return f"商品不存在或已被删除（{raw}）"
    if category == "token_expired":
        return f"令牌过期且重试后仍失败（{raw}）"
    return f"下架失败（{raw}）"


def _merge_cookies(response, cookies_str: str) -> str:
    """合并响应下发的新令牌，保持后续请求签名有效。"""
    if "set-cookie" not in response.headers:
        return cookies_str

    updates = {}
    for raw in response.headers.getall("set-cookie", []):
        pair = raw.split(";", 1)[0].strip()
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        if key in ("_m_h5_tk", "_m_h5_tk_enc"):
            updates[key] = value

    if not updates:
        return cookies_str
    try:
        current = trans_cookies(cookies_str) if cookies_str else {}
    except ValueError:
        current = {}
    current.update(updates)
    return "; ".join(f"{k}={v}" for k, v in current.items())


async def _request_once(
    session: aiohttp.ClientSession,
    cookies_str: str,
    item_id: str,
    timeout: int,
) -> Dict[str, Any]:
    """单次请求，返回 ``{"ret": [...], "cookies_str": ...}``。"""
    data_val = json.dumps({"itemId": str(item_id)}, separators=(",", ":"))
    timestamp = str(int(time.time() * 1000))
    try:
        token_value = trans_cookies(cookies_str).get("_m_h5_tk", "")
    except ValueError:
        token_value = ""
    token = token_value.split("_")[0] if token_value else ""

    params = {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": timestamp,
        "sign": generate_sign(timestamp, token, data_val),
        "v": API_VERSION,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": DELETE_API,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": SPM_CNT,
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "referer": "https://www.goofish.com/",
        "user-agent": USER_AGENT,
        "cookie": cookies_str.replace("\n", "").replace("\r", ""),
    }

    async with session.post(
        DELETE_URL,
        params=params,
        data={"data": data_val},
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as response:
        result = await response.json(content_type=None)
        cookies_str = _merge_cookies(response, cookies_str)

    return {"ret": _join_ret(result), "cookies_str": cookies_str}


async def delete_item(
    cookies_str: str,
    item_id: str,
    *,
    cookie_id: Optional[str] = None,
    session: Optional[aiohttp.ClientSession] = None,
    timeout: int = 20,
) -> Dict[str, Any]:
    """下架 / 删除单个商品（``com.taobao.idle.item.delete`` v1.1）。

    写操作先过 :func:`utils.write_guard.acquire_write`：本分钟速率 + 单账号每日上限。
    超限 **直接判失败**（不等待），返回 ``ok=False`` + ``rate_limited=True`` + ``retry_after``。

    Args:
        cookies_str: 账号 Cookie 扁平串（须含 ``unb`` + ``_m_h5_tk``）。
        item_id: 商品 ID。
        cookie_id: 限流与日志用的账号标识；缺省从 Cookie 的 ``unb`` 取。
        session: 复用外部 aiohttp 会话；缺省自建（用完即关）。
        timeout: 单次请求超时秒数。

    Returns:
        ``{"ok", "ret", "message", "semantics", "category", "cookies_str",
           "rate_limited", "retry_after"}``。
        ``cookies_str`` 是合并响应后的最新 Cookie，调用方应回写。
    """
    base = {
        "ok": False,
        "ret": [],
        "message": "",
        "semantics": SEMANTICS,
        "category": "other",
        "cookies_str": cookies_str,
        "rate_limited": False,
        "retry_after": 0,
    }

    if not item_id or not cookies_str:
        base["message"] = "缺少商品ID或Cookie"
        base["category"] = "invalid"
        return base

    # ---- 写限流（W4）：fail fast，绝不等待 ----
    from utils.write_guard import WriteRateLimited, acquire_write

    if not cookie_id:
        try:
            cookie_id = trans_cookies(cookies_str).get("unb") or "unknown"
        except ValueError:
            cookie_id = "unknown"

    try:
        acquire_write(cookie_id)
    except WriteRateLimited as exc:
        base["message"] = str(exc)
        base["category"] = "rate_limited"
        base["rate_limited"] = True
        base["retry_after"] = exc.retry_after
        logger.warning(f"【{cookie_id}】下架 {item_id} 被写限流拒绝: {exc}")
        return base

    # ---- 真实请求（令牌过期重签重试一次）----
    own_session = session is None
    if own_session:
        session = aiohttp.ClientSession()
    try:
        ret_list: List[str] = []
        for attempt in range(MAX_TOKEN_RETRY):
            try:
                outcome = await _request_once(session, cookies_str, item_id, timeout)
            except Exception as exc:
                base["message"] = f"请求异常: {exc}"
                base["category"] = "error"
                return base

            cookies_str = outcome["cookies_str"]
            ret_list = outcome["ret"]
            if _classify(ret_list) != "token_expired" or attempt == MAX_TOKEN_RETRY - 1:
                break
            logger.info(f"【{cookie_id}】下架 {item_id} 令牌过期，重签重试")
    finally:
        if own_session:
            await session.close()

    category = _classify(ret_list)
    base.update(
        {
            "ok": category == "success",
            "ret": ret_list,
            "category": category,
            "message": _message_for(category, ret_list),
            "cookies_str": cookies_str,
        }
    )
    logger.info(
        f"【{cookie_id}】下架 {item_id}: {category} -> {'; '.join(ret_list) or '无 ret'}"
    )

    # ---- L1 失败处的 L3 钩子（真机浏览器下架 / 删除）----
    base["l3_available"] = True
    if category != "success" and _l3_enabled():
        logger.warning(f"L1 下架失败（{category}），转 L3 真机兜底")
        fb = await l3_browser_delete(cookies_str, item_id, task_id=cookie_id)
        base["l3"] = fb
        if fb.get("ok"):
            base["ok"] = True
            base["category"] = "success"
            base["message"] = f"L1 失败（{category}），L3 真机兜底下架成功"
        else:
            base["message"] = (f"L1 失败（{category}）；L3 真机兜底也失败："
                               f"{fb.get('blocked_at')} {str(fb.get('detail'))[:160]}")
    elif category != "success":
        base["l3"] = {"skipped": f"{L3_FALLBACK_KEY} 未开启"}
    return base
