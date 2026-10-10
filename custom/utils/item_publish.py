"""闲鱼商品发布链路：类目识别 + 账号真实地址 + 发布本体（W7）。

移植自 goofish-cli v0.4.0（Apache-2.0）的三个 mtop 接口，payload 字段名逐字照抄：

================  ===========================================  =====  ==================
用途              api                                          ver    spm_cnt
================  ===========================================  =====  ==================
类目识别          ``mtop.taobao.idle.kgraph.property.recommend``  2.0    ``a21ybx.publish.0.0``
默认地址          ``mtop.taobao.idle.local.poi.get``              1.0    ``a21ybx.publish.0.0``
发布              ``mtop.idle.pc.idleitem.publish``               1.0    ``a21ybx.publish.0.0``
================  ===========================================  =====  ==================

签名复用 :func:`utils.xianyu_utils.generate_sign`（纯 Python，与 goofish-cli 的 JS 实现
逐字一致：``md5(f"{token}&{t}&34839810&{data}")``），调用骨架照抄
:mod:`utils.item_polish`（含 ``_merge_cookies`` 回写新的 ``_m_h5_tk``）。

**``origin`` / ``referer`` 必须指 ``https://www.goofish.com``** —— 不能复用
``utils/xianyu_seller_api.py`` 的 ``seller.goofish.com``（指向错会返回误导性的
``FAIL_SYS_SESSION_EXPIRED``）。

写操作（发布）走 :func:`utils.write_guard.acquire_write` 的写限流；命中风控
（RGV587 / FAIL_SYS_USER_VALIDATE / 惩罚页）**不重试**，直接返回 ``risk_control=True``，
由上层决定是否交路线 C / W9 真机兜底 —— 本模块不做滑块硬刷。

对外接口（同步，供 ``app/product_automation`` 这类同步调用方直接使用）::

    recommend_category(cookies_str, title, image_infos) -> {cat_id, cat_name, ...}
    get_default_location(cookies_str) -> {prov, city, area, poi, division_id, all}
    publish_item(cookies_str, *, title, desc, images, price, ...) -> {item_id, ok, ret}
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import time
from typing import Any, Dict, List, Optional

import requests
from loguru import logger

from utils.user_agents import CHROME_UA
from utils.write_guard import WriteRateLimited, acquire_write
from utils.xianyu_utils import generate_sign, trans_cookies

__all__ = [
    "PublishError",
    "recommend_category",
    "get_default_location",
    "publish_item",
    "l3_browser_publish",
]

# ---------------------------------------------------------------- 常量

APP_KEY = "34839810"
MTOP_HOST = "https://h5api.m.goofish.com"
SPM_CNT = "a21ybx.publish.0.0"

CATEGORY_API = "mtop.taobao.idle.kgraph.property.recommend"
CATEGORY_VERSION = "2.0"
LOCATION_API = "mtop.taobao.idle.local.poi.get"
LOCATION_VERSION = "1.0"
PUBLISH_API = "mtop.idle.pc.idleitem.publish"
PUBLISH_VERSION = "1.0"

# goofish-cli 里是硬编码值（拆解为 2026-04-11 的毫秒时间戳 + 3 位后缀）。
# 服务端若不校验新鲜度则一直可用；若校验，publish_item 会按卡片规定做三级回退。
CATEGORY_UNIQUE_CODE = "1775905618164677"
PUBLISH_UNIQUE_CODE = "1775897582791680"

# goofish-cli 硬编码的默认经纬度（上海）。容器 cookie 里只有省（location='四川省'），
# 没有市 / 区 / POI / 坐标，所以沿用这个默认值；``local.poi.get`` 返回的是**账号自己的**
# 常用地址（commonAddresses / selectedPoi），坐标只影响周边 POI 计算。
DEFAULT_LONGITUDE = 121.4737
DEFAULT_LATITUDE = 31.2304

DRY_RUN_KEY = "publish_dry_run"

# 风控 / 登录态 / 参数类关键字（判定顺序：风控 > 登录态 > 参数）
_RISK_KEYWORDS = (
    "RGV587_ERROR",
    "FAIL_SYS_USER_VALIDATE",
    "哎哟喂",
    "/punish",
    "FAIL_SYS_ILLEGAL_ACCESS",
)
_PARAM_KEYWORDS = (
    "uniqueCode",
    "unique_code",
    "参数",
    "ILLEGAL_ARGUMENT",
    "ARGUMENT",
    "PARAM_ERROR",
)

DEFAULT_TIMEOUT = 30


class PublishError(Exception):
    """发布链路的本地异常（网络 / 解析 / 凭据缺失），与平台 ``ret`` 错误区分。"""


# ---------------------------------------------------------------- L3 真机兜底（W9）

L3_FALLBACK_KEY = "real_machine_fallback"


def _l3_enabled() -> bool:
    """``system_settings.real_machine_fallback``：默认 **false**。

    真机兜底是分钟级、会开浏览器标签页的操作，只允许上层显式打开（面板改一个键即可热生效）。
    """
    try:
        from app.db_manager import db_manager

        raw = db_manager.get_system_setting(L3_FALLBACK_KEY)
    except Exception as exc:
        logger.warning(f"读取 real_machine_fallback 失败，按关闭处理: {exc}")
        return False
    return str(raw or "").strip().lower() in ("1", "true", "yes", "on", "y")


def l3_browser_publish(
    cookies_str: str,
    *,
    title: str,
    desc: str,
    images: Any,
    price: float,
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    """L3 兜底：L1（协议）+ L2（路线 C）都失败时，用 VM101 浏览器完整走一遍人工发布。

    永不抛异常。``images`` 需为 **VM101 可见**的路径（或配置 ``REALOPS_STAGE_DIR`` /
    ``REALOPS_STAGE_WIN_DIR`` 做搬运），否则明确返回原因，不做任何猜测。
    """
    try:
        from utils.real_machine_ops import browser_publish
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "item_id": "", "blocked_at": "import",
                "detail": f"real_machine_ops 不可用: {exc}"}
    paths = _normalize_paths(images)
    if not paths:
        return {"ok": False, "item_id": "", "blocked_at": "input", "detail": "images 为空"}
    try:
        return browser_publish(cookies_str, title=title, desc=desc, image_paths=paths,
                               price=price, task_id=task_id)
    except Exception as exc:  # browser_publish 自身不抛；这里只兜底
        return {"ok": False, "item_id": "", "blocked_at": "exception",
                "detail": f"{type(exc).__name__}: {exc}"}


def _l3_try(
    result: Dict[str, Any],
    cookies_str: str,
    *,
    title: str,
    desc: str,
    images: Any,
    price: float,
    cookie_id: str,
    reason: str,
) -> Dict[str, Any]:
    """L1 失败处的钩子：标注 L3 可用性；仅在显式开启时才真的调真机兜底。"""
    result["l3_available"] = True
    if not _l3_enabled():
        result["l3"] = {"skipped": f"{L3_FALLBACK_KEY} 未开启（L1 失败原因：{reason}）"}
        return result
    logger.warning(f"L1 发布失败（{reason}），转 L3 真机兜底")
    fb = l3_browser_publish(cookies_str, title=title, desc=desc, images=images,
                            price=price, task_id=cookie_id)
    result["l3"] = fb
    if fb.get("ok") and fb.get("item_id"):
        result["ok"] = True
        result["item_id"] = str(fb["item_id"])
        result["message"] = f"L1 失败（{reason}），L3 真机兜底发布成功"
    else:
        result["message"] = (f"L1 失败（{reason}）；L3 真机兜底也失败："
                             f"{fb.get('blocked_at')} {str(fb.get('detail'))[:160]}")
    return result


# ---------------------------------------------------------------- Cookie 工具


def _cookie_id_from_cookies(cookies_str: str, cookie_id: Optional[str] = None) -> str:
    """取账号标识：优先显式传入，否则用 cookie 里的 ``unb``（本部署里 ``cookies.id`` 即 unb）。"""
    if cookie_id:
        return str(cookie_id)
    try:
        return str(trans_cookies(cookies_str).get("unb", "") or "")
    except Exception:
        return ""


def _h5_token(cookies_str: str) -> str:
    try:
        raw = trans_cookies(cookies_str).get("_m_h5_tk", "") or ""
    except ValueError:
        raw = ""
    return raw.split("_")[0] if raw else ""


def _default_headers(cookies_str: str) -> Dict[str, str]:
    return {
        "accept": "application/json",
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "referer": "https://www.goofish.com/",
        "user-agent": CHROME_UA,
        "cookie": cookies_str.replace("\n", "").replace("\r", ""),
    }


def _merge_cookies(response, cookies_str: str) -> str:
    """合并响应下发的新令牌（``_m_h5_tk`` / ``_m_h5_tk_enc``），保持后续签名有效。"""
    set_cookies: List[str] = []
    try:
        raw_headers = response.raw.headers
        set_cookies = list(raw_headers.getlist("Set-Cookie") or [])
    except Exception:
        set_cookies = []
    if not set_cookies:
        single = response.headers.get("Set-Cookie")
        if single:
            set_cookies = [single]

    updates: Dict[str, str] = {}
    for raw in set_cookies:
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


# ---------------------------------------------------------------- ret 判定


def _ret_list(raw: Dict[str, Any]) -> List[Any]:
    ret = raw.get("ret") or []
    return ret if isinstance(ret, list) else [ret]


def _ret_text(raw: Dict[str, Any]) -> str:
    return " | ".join(str(x) for x in _ret_list(raw)) or "空响应"


def _is_success(raw: Dict[str, Any]) -> bool:
    return any("SUCCESS" in str(x) for x in _ret_list(raw))


def _has_keyword(raw: Dict[str, Any], keywords) -> bool:
    text = _ret_text(raw)
    return any(k in text for k in keywords)


def _is_risk(raw: Dict[str, Any]) -> bool:
    return _has_keyword(raw, _RISK_KEYWORDS)


def _is_param_error(raw: Dict[str, Any]) -> bool:
    return _has_keyword(raw, _PARAM_KEYWORDS)


# ---------------------------------------------------------------- mtop 调用


def _mtop_call(
    cookies_str: str,
    api: str,
    data: Any,
    *,
    version: str,
    spm_cnt: str = SPM_CNT,
    timeout: int = DEFAULT_TIMEOUT,
):
    """裸 ``requests.post`` 调 mtop（禁用 Session / Retry 适配器）。

    Returns:
        ``(raw_json, merged_cookies_str)``。``merged_cookies_str`` 已合并响应下发的
        ``_m_h5_tk``，调用方应回写。
    """
    t_ms = str(int(time.time() * 1000))
    data_val = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
    token = _h5_token(cookies_str)
    if not token:
        raise PublishError("_m_h5_tk 缺失，Cookie 需重新登录导出")

    url = f"{MTOP_HOST}/h5/{api}/{version}/"
    params = {
        "jsv": "2.7.2",
        "appKey": APP_KEY,
        "t": t_ms,
        "sign": generate_sign(t_ms, token, data_val),
        "v": version,
        "type": "originaljson",
        "accountSite": "xianyu",
        "dataType": "json",
        "timeout": "20000",
        "api": api,
        "sessionOption": "AutoLoginOnly",
        "spm_cnt": spm_cnt,
    }
    response = requests.post(
        url,
        params=params,
        headers=_default_headers(cookies_str),
        data={"data": data_val},
        timeout=timeout,
    )
    merged = _merge_cookies(response, cookies_str)
    try:
        raw = response.json()
    except ValueError:
        raise PublishError(f"[{api}] 响应不是 JSON（HTTP {response.status_code}）")
    if not isinstance(raw, dict):
        raise PublishError(f"[{api}] 响应结构异常")
    return raw, merged


# ---------------------------------------------------------------- 图片处理


def _run_async(coro):
    """在同步上下文里跑协程；若已在事件循环里则放到临时线程执行（避免 RuntimeError）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _normalize_paths(images: Any) -> List[str]:
    """把素材 ``images`` 字段（JSON 字符串 / 路径列表 / dict 列表）归一成路径列表。"""
    if not images:
        return []
    if isinstance(images, str):
        try:
            parsed = json.loads(images)
        except ValueError:
            parsed = [part.strip() for part in images.split(",") if part.strip()]
        return _normalize_paths(parsed)
    out: List[str] = []
    for item in images:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            path = item.get("path") or item.get("local_path") or item.get("file")
            if path:
                out.append(str(path))
    return out


def _local_image_size(path: str) -> tuple:
    """读本地图片宽高；失败返回 (0, 0)（不能让发布链路崩）。"""
    try:
        from PIL import Image

        with Image.open(path) as img:
            return int(img.width), int(img.height)
    except Exception as exc:
        logger.warning(f"读取本地图片尺寸失败 {path}: {exc}")
        return 0, 0


def _normalize_upload_result(res: Any, path: str) -> Dict[str, Any]:
    """兼容 image_uploader 的两种返回：旧版 str（仅 URL）与 W5 的 dict（含 width/height）。"""
    if isinstance(res, dict):
        url = res.get("url") or ""
        width = int(res.get("width") or 0)
        height = int(res.get("height") or 0)
    else:
        url = str(res or "")
        width = height = 0
    if url and (not width or not height):
        local_w, local_h = _local_image_size(path)
        width = width or local_w
        height = height or local_h
    return {"url": url, "width": width, "height": height, "path": path}


def _upload_images(cookies_str: str, images: Any):
    """上传本地图片到闲鱼 CDN。

    Returns:
        ``(image_infos, errors, cookies_str)``；``errors`` 非空即视为整条链路失败。
    """
    paths = _normalize_paths(images)
    if not paths:
        return [], ["没有可用的本地图片路径"], cookies_str

    try:
        from utils.image_uploader import ImageUploader
    except Exception as exc:  # pragma: no cover - 依赖缺失时明确报错
        return [], [f"image_uploader 不可用: {exc}"], cookies_str

    infos: List[Dict[str, Any]] = []
    errors: List[str] = []
    uploader = ImageUploader(cookies_str)
    try:
        for path in paths:
            try:
                res = _run_async(uploader.upload_image(path))
            except Exception as exc:
                errors.append(f"{os.path.basename(path)}: 上传异常 {exc}")
                continue
            if not res:
                errors.append(f"{os.path.basename(path)}: 上传返回空")
                continue
            info = _normalize_upload_result(res, path)
            if not info["url"]:
                errors.append(f"{os.path.basename(path)}: 未取到图片 URL")
                continue
            infos.append(info)
    finally:
        try:
            _run_async(uploader.close_session())
        except Exception:
            pass
    return infos, errors, cookies_str


# ---------------------------------------------------------------- 地址缓存


def _read_cached_addr(cookie_id: str) -> Optional[Dict[str, Any]]:
    try:
        from app.db_manager import db_manager

        return db_manager.get_publish_addr(cookie_id)
    except Exception as exc:
        logger.warning(f"读取发布地址缓存失败: {exc}")
        return None


def _write_cached_addr(cookie_id: str, addr: Dict[str, Any]) -> bool:
    try:
        from app.db_manager import db_manager

        return bool(db_manager.set_publish_addr(cookie_id, addr))
    except Exception as exc:
        logger.warning(f"写入发布地址缓存失败: {exc}")
        return False


# ---------------------------------------------------------------- dry-run


def _is_dry_run() -> bool:
    """``system_settings.publish_dry_run``，缺失或非法一律按 **true**（安全默认）。"""
    try:
        from app.db_manager import db_manager

        raw = db_manager.get_system_setting(DRY_RUN_KEY)
    except Exception as exc:
        logger.warning(f"读取 publish_dry_run 失败，按 dry-run 处理: {exc}")
        return True
    if raw is None:
        return True
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "y", "")


# ---------------------------------------------------------------- payload


def _image_do(img: Dict[str, Any]) -> Dict[str, Any]:
    """单张图的 ``imageInfoDOList`` 元素（字段名逐字照抄 goofish-cli）。"""
    return {
        "extraInfo": {"isH": "false", "isT": "false", "raw": "false"},
        "isQrCode": False,
        "url": img.get("url", ""),
        "heightSize": img.get("height", 0),
        "widthSize": img.get("width", 0),
        "major": True,
        "type": 0,
        "status": "done",
    }


def _build_publish_data(
    *,
    title: str,
    desc: str,
    image_infos: List[Dict[str, Any]],
    price: float,
    original_price: Optional[float],
    delivery: str,
    post_price: float,
    can_self_pickup: bool,
    cat_info: Dict[str, Any],
    location: Dict[str, Any],
    unique_code: str,
) -> Dict[str, Any]:
    """组装 ``mtop.idle.pc.idleitem.publish`` 的 payload（字段名逐字照抄 goofish-cli）。"""
    image_do_list = [_image_do(img) for img in image_infos]

    post_fee: Dict[str, Any] = {
        "canFreeShipping": False,
        "supportFreight": False,
        "onlyTakeSelf": False,
    }
    if delivery == "包邮":
        post_fee["canFreeShipping"] = True
        post_fee["supportFreight"] = True
    elif delivery == "按距离计费":
        post_fee["supportFreight"] = True
        post_fee["templateId"] = "-100"
    elif delivery == "一口价":
        post_fee["supportFreight"] = True
        post_fee["postPriceInCent"] = str(int(post_price * 100))
        post_fee["templateId"] = "0"
    elif delivery == "无需邮寄":
        post_fee["templateId"] = "0"

    price_dto: Dict[str, str] = {}
    default_price = price <= 0
    if not default_price:
        price_dto["priceInCent"] = str(int(price * 100))
    if original_price and original_price > 0:
        price_dto["origPriceInCent"] = str(int(original_price * 100))

    item_addr: Dict[str, Any] = {}
    if location.get("division_id"):
        all_addrs = location.get("all") or []
        first = all_addrs[0] if all_addrs else {}
        item_addr = {
            "area": first.get("area", ""),
            "city": first.get("city", ""),
            "divisionId": first.get("divisionId", ""),
            "gps": f"{first.get('longitude', '')},{first.get('latitude', '')}",
            "poiId": first.get("poiId", ""),
            "poiName": first.get("poi", ""),
            "prov": first.get("prov", ""),
        }

    return {
        "freebies": False,
        "itemTypeStr": "b",
        "quantity": "1",
        "simpleItem": "true",
        "imageInfoDOList": image_do_list,
        "itemTextDTO": {"desc": desc, "title": title, "titleDescSeparate": True},
        "itemLabelExtList": [],
        "itemPriceDTO": price_dto,
        "userRightsProtocols": [{"enable": False, "serviceCode": "SKILL_PLAY_NO_MIND"}],
        "itemPostFeeDTO": post_fee,
        "itemAddrDTO": item_addr,
        "defaultPrice": default_price,
        "itemCatDTO": {
            "catId": cat_info.get("cat_id", ""),
            "catName": cat_info.get("cat_name", ""),
            "channelCatId": cat_info.get("channel_cat_id", ""),
            "tbCatId": cat_info.get("tb_cat_id", ""),
        },
        "onlyTakeSelf": can_self_pickup,
        "uniqueCode": unique_code,
        "sourceId": "pcMainPublish",
        "bizcode": "pcMainPublish",
        "publishScene": "pcMainPublish",
    }


_SECRET_KEY_HINTS = ("cookie", "token", "sign", "password", "secret", "auth")


def _sanitize_payload(payload: Any) -> Any:
    """打印前脱敏：递归屏蔽任何含 cookie / token / sign / password 的键。"""
    if isinstance(payload, dict):
        out = {}
        for key, value in payload.items():
            if any(hint in str(key).lower() for hint in _SECRET_KEY_HINTS):
                out[key] = "***"
            else:
                out[key] = _sanitize_payload(value)
        return out
    if isinstance(payload, list):
        return [_sanitize_payload(item) for item in payload]
    return payload


def _dump_payload(payload: Dict[str, Any]) -> str:
    return json.dumps(_sanitize_payload(payload), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------- 对外接口


def recommend_category(
    cookies_str: str,
    title: str,
    image_infos: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """AI 类目识别（``mtop.taobao.idle.kgraph.property.recommend`` v2.0）。

    Args:
        image_infos: ``[{"url": ..., "width": ..., "height": ...}, ...]``

    Returns:
        ``{cat_id, cat_name, channel_cat_id, tb_cat_id, confidence, ok, ret, raw, cookies_str}``
    """
    data = {
        "title": title,
        "lockCpv": False,
        "multiSKU": False,
        "publishScene": "mainPublish",
        "scene": "newPublishChoice",
        "description": title,
        "imageInfos": [_image_do(img) for img in (image_infos or [])],
        "uniqueCode": CATEGORY_UNIQUE_CODE,
    }
    raw, merged = _mtop_call(cookies_str, CATEGORY_API, data, version=CATEGORY_VERSION)
    predict = ((raw.get("data") or {}).get("categoryPredictResult") or {})
    return {
        "cat_id": str(predict.get("catId", "") or ""),
        "cat_name": predict.get("catName", "") or "",
        "channel_cat_id": str(predict.get("channelCatId", "") or ""),
        "tb_cat_id": str(predict.get("tbCatId", "") or ""),
        "confidence": predict.get("confidence", 0),
        "ok": _is_success(raw),
        "ret": _ret_list(raw),
        "raw": raw,
        "cookies_str": merged,
    }


def get_default_location(
    cookies_str: str,
    *,
    cookie_id: Optional[str] = None,
    use_cache: bool = True,
) -> Dict[str, Any]:
    """账号默认发布地址（``mtop.taobao.idle.local.poi.get`` v1.0）。

    首次调用后把结果缓存进 ``cookies.publish_addr_json``；列非空即视为已覆盖，
    后续直接读缓存（面板覆盖同此语义）。

    Returns:
        ``{prov, city, area, poi, division_id, all, source, ok, ret, raw, cookies_str}``
    """
    cid = _cookie_id_from_cookies(cookies_str, cookie_id)

    if use_cache and cid:
        cached = _read_cached_addr(cid)
        if cached:
            result = dict(cached)
            result.update({"source": "cache", "ok": True, "ret": [], "cookies_str": cookies_str})
            return result

    raw, merged = _mtop_call(
        cookies_str,
        LOCATION_API,
        {"longitude": DEFAULT_LONGITUDE, "latitude": DEFAULT_LATITUDE},
        version=LOCATION_VERSION,
    )
    data = raw.get("data") or {}
    addrs = data.get("commonAddresses") or []
    selected = data.get("selectedPoi") or (addrs[0] if addrs else None)

    if not selected:
        result = {"prov": "", "city": "", "area": "", "poi": "", "division_id": "", "all": []}
    else:
        result = {
            "prov": selected.get("prov", "") or "",
            "city": selected.get("city", "") or "",
            "area": selected.get("area", "") or "",
            "poi": selected.get("poi", "") or "",
            "division_id": str(selected.get("divisionId", "") or ""),
            "all": addrs or [selected],
        }
        if cid:
            _write_cached_addr(cid, result)

    result.update(
        {
            "source": "api",
            "ok": _is_success(raw),
            "ret": _ret_list(raw),
            "raw": raw,
            "cookies_str": merged,
        }
    )
    return result


def publish_item(
    cookies_str: str,
    *,
    title: str,
    desc: str,
    images: Any,
    price: float,
    original_price: Optional[float] = None,
    delivery: str = "无需邮寄",
    post_price: float = 0,
    can_self_pickup: bool = True,
    cookie_id: Optional[str] = None,
) -> Dict[str, Any]:
    """发布一件商品：传图 → 类目 → 地址 → 发布（``mtop.idle.pc.idleitem.publish`` v1.0）。

    Args:
        images: 本地图片路径列表（或素材 ``images`` 字段的 JSON 字符串）。
        price: 单位元；``<= 0`` 时走 ``defaultPrice``（不带 ``priceInDTO``）。
        cookie_id: 写限流用的账号标识；缺省从 cookie 的 ``unb`` 取。

    Returns:
        ``{item_id, ok, ret, cookies_str, dry_run, rate_limited, risk_control,
        category, location, payload, unique_code, message}``

        - ``publish_dry_run`` 为 true（默认）时**只组装 payload 并打印（脱敏）**，
          不发真实发布请求（图片仍会真实上传，因为 payload 需要真实 URL）。
        - 命中风控返回 ``risk_control=True`` 且**不重试**，由上层路由到路线 C / W9 兜底。
    """
    cid = _cookie_id_from_cookies(cookies_str, cookie_id)
    result: Dict[str, Any] = {
        "item_id": "",
        "ok": False,
        "ret": [],
        "cookies_str": cookies_str,
        "dry_run": False,
        "rate_limited": False,
        "risk_control": False,
        "category": None,
        "location": None,
        "payload": None,
        "unique_code": "",
        "message": "",
    }

    # 1) 图片：本地路径 → 闲鱼 CDN，拿 url + width/height
    try:
        image_infos, upload_errors, cookies_str = _upload_images(cookies_str, images)
    except Exception as exc:
        result["message"] = f"图片上传阶段异常: {exc}"
        return result
    result["cookies_str"] = cookies_str
    if upload_errors:
        result["message"] = "图片上传失败: " + "; ".join(upload_errors)
        return result

    # 2) 类目识别
    try:
        cat = recommend_category(cookies_str, title, image_infos)
    except PublishError as exc:
        result["message"] = f"类目识别异常: {exc}"
        return result
    cookies_str = cat.get("cookies_str", cookies_str)
    result["cookies_str"] = cookies_str
    result["category"] = {k: cat.get(k) for k in ("cat_id", "cat_name", "channel_cat_id", "tb_cat_id")}
    if not cat.get("cat_id"):
        result["ret"] = cat.get("ret", [])
        result["risk_control"] = _is_risk({"ret": cat.get("ret", [])})
        result["message"] = "类目识别失败: " + " | ".join(str(x) for x in cat.get("ret", []))
        return result

    # 3) 默认地址
    try:
        loc = get_default_location(cookies_str, cookie_id=cid)
    except PublishError as exc:
        result["message"] = f"地址获取异常: {exc}"
        return result
    cookies_str = loc.get("cookies_str", cookies_str)
    result["cookies_str"] = cookies_str
    result["location"] = {k: loc.get(k) for k in ("prov", "city", "area", "poi", "division_id")}
    if not loc.get("division_id"):
        result["ret"] = loc.get("ret", [])
        result["risk_control"] = _is_risk({"ret": loc.get("ret", [])})
        result["message"] = "默认地址获取失败（无 selectedPoi）"
        return result

    payload_kwargs = {
        "title": title,
        "desc": desc,
        "image_infos": image_infos,
        "price": price,
        "original_price": original_price,
        "delivery": delivery,
        "post_price": post_price,
        "can_self_pickup": can_self_pickup,
        "cat_info": cat,
        "location": loc,
    }

    # 4) dry-run：只组装 payload，不发真实发布请求
    if _is_dry_run():
        payload = _build_publish_data(unique_code=PUBLISH_UNIQUE_CODE, **payload_kwargs)
        result["dry_run"] = True
        result["payload"] = _sanitize_payload(payload)
        result["unique_code"] = PUBLISH_UNIQUE_CODE
        result["message"] = "dry-run：仅组装 payload，未发真实发布请求"
        logger.info("publish_dry_run=true，仅组装 payload（脱敏打印），不发发布请求")
        print(_dump_payload(payload))
        return result

    # 5) 写限流（一次发布 = 一次写操作；三级 uniqueCode 回退共用同一配额）
    try:
        acquire_write(cid)
    except WriteRateLimited as exc:
        result["rate_limited"] = True
        result["message"] = str(exc)
        return result

    # 6) 发布：uniqueCode 三级回退（原值 → 13 位毫秒 → 16 位毫秒+000）
    candidates: List[str] = []
    for code in (PUBLISH_UNIQUE_CODE, str(int(time.time() * 1000)), str(int(time.time() * 1000)) + "000"):
        if code not in candidates:
            candidates.append(code)

    last_raw: Dict[str, Any] = {}
    for index, code in enumerate(candidates):
        payload = _build_publish_data(unique_code=code, **payload_kwargs)
        try:
            raw, cookies_str = _mtop_call(cookies_str, PUBLISH_API, payload, version=PUBLISH_VERSION)
        except PublishError as exc:
            result["message"] = f"发布请求异常: {exc}"
            result["cookies_str"] = cookies_str
            return result

        result["cookies_str"] = cookies_str
        result["payload"] = _sanitize_payload(payload)
        result["unique_code"] = code
        result["ret"] = _ret_list(raw)
        last_raw = raw

        if _is_success(raw):
            item_id = str((raw.get("data") or {}).get("itemId", "") or "")
            result["item_id"] = item_id
            result["ok"] = True
            result["message"] = "发布成功"
            logger.info(f"发布成功 itemId={item_id} uniqueCode={code}")
            return result

        if _is_risk(raw):
            result["risk_control"] = True
            result["message"] = "命中风控（不重试，交路线 C / 真机兜底）: " + _ret_text(raw)
            logger.warning(f"发布命中风控: {_ret_text(raw)}")
            return _l3_try(result, cookies_str, title=title, desc=desc, images=images,
                           price=price, cookie_id=cid, reason="risk_control")

        if index < len(candidates) - 1 and _is_param_error(raw):
            logger.warning(
                f"发布 ret 疑似参数/uniqueCode 错误，换 uniqueCode 重试"
                f"（{index + 2}/{len(candidates)}）: {_ret_text(raw)}"
            )
            continue

        result["message"] = "发布失败: " + _ret_text(raw)
        return _l3_try(result, cookies_str, title=title, desc=desc, images=images,
                       price=price, cookie_id=cid, reason="mtop_fail")

    result["ret"] = _ret_list(last_raw)
    result["message"] = "发布失败（三种 uniqueCode 均被拒）: " + _ret_text(last_raw)
    return _l3_try(result, cookies_str, title=title, desc=desc, images=images,
                   price=price, cookie_id=cid, reason="unique_code_exhausted")
