#!/usr/bin/env python3
"""xianyu_api.py -- 闲鱼 mtop token 接口的最小纯协议客户端（含 mtop 令牌轮换处理）。

来源：/opt/reverse-lab/cases/goofish-slider-x5sec-wininput/src/xianyu_api.py
      + work/t6run55/token_refresh.py 的轮换逻辑合并入 fetch_token（T7 集成）。

用途：闭环校验 —— 用路线 C 从浏览器拿到的 x5sec + 账号 cookie 重试
      mtop.taobao.idlemessage.pc.login.token，确认风控已解除（accessToken）。

签名：md5(f"{token}&{t}&{appKey}&{data}")，token 取 Cookie 里 _m_h5_tk 的下划线前段。
注意：本机有透明 TLS 代理会破坏 keep-alive，一律裸 requests.post，禁用 Session。
"""
import hashlib
import json
import random
import time

import requests

TOKEN_API = "https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/"
SIGN_APPKEY = "34839810"
DATA_APPKEY = "444e9908a51d1cb236a27862abc769c9"
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

# 轮换后的 _m_h5_tk / _m_h5_tk_enc（进程内缓存，服务常驻）
_TOK_CACHE = {"tk": None, "enc": None}


def trans_cookies(cookies_str: str) -> dict:
    out = {}
    for part in cookies_str.split("; "):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = v
    return out


def generate_device_id(user_id: str) -> str:
    res = []
    for i in range(36):
        if i in (8, 13, 18, 23):
            res.append("-")
        elif i == 14:
            res.append("4")
        else:
            rv = int(16 * random.random())
            res.append(CHARS[(rv & 0x3) | 0x8] if i == 19 else CHARS[rv])
    return "".join(res) + "-" + user_id


def generate_sign(t: str, token: str, data: str) -> str:
    return hashlib.md5(f"{token}&{t}&{SIGN_APPKEY}&{data}".encode()).hexdigest()


def _sign_call(cookies_str: str, timeout: float):
    cd = trans_cookies(cookies_str)
    tk = _TOK_CACHE["tk"] or cd.get("_m_h5_tk") or ""
    enc = _TOK_CACHE["enc"] or cd.get("_m_h5_tk_enc") or ""
    token = tk.split("_")[0]
    if not token:
        raise RuntimeError("Cookie 缺少 _m_h5_tk")
    ts = str(int(time.time() * 1000))
    device_id = generate_device_id(cd.get("unb", ""))
    data_value = json.dumps({"appKey": DATA_APPKEY, "deviceId": device_id},
                            separators=(",", ":"))
    params = {
        "jsv": "2.7.2", "appKey": SIGN_APPKEY, "t": ts,
        "sign": generate_sign(ts, token, data_value),
        "v": "1.0", "type": "originaljson", "accountSite": "xianyu",
        "dataType": "json", "timeout": "20000",
        "api": "mtop.taobao.idlemessage.pc.login.token",
        "sessionOption": "AutoLoginOnly",
        "dangerouslySetWindvaneParams": "%5Bobject%20Object%5D",
        "smToken": "token", "queryToken": "sm", "sm": "sm",
        "spm_cnt": "a21ybx.im.0.0",
        "spm_pre": "a21ybx.home.sidebar.1.4c053da6vYwnmf",
        "log_id": "4c053da6vYwnmf",
    }
    parts = [p for p in cookies_str.split("; ")
             if not p.startswith("_m_h5_tk=") and not p.startswith("_m_h5_tk_enc=")]
    if tk:
        parts.append("_m_h5_tk=" + tk)
    if enc:
        parts.append("_m_h5_tk_enc=" + enc)
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "user-agent": CHROME_UA,
        "referer": "https://www.goofish.com/",
        "origin": "https://www.goofish.com",
        "cookie": "; ".join(parts),
    }
    r = requests.post(TOKEN_API, params=params, data={"data": data_value},
                      headers=headers, timeout=timeout)
    try:
        payload = r.json()
    except Exception:
        payload = {}
    return r, payload


def _is_expired(payload) -> bool:
    ret = (payload or {}).get("ret") or []
    return any(("TOKEN_EXOIRED" in str(x)) or ("令牌过期" in str(x)) for x in ret)


def _absorb_rotation(r) -> bool:
    """把响应 Set-Cookie 里轮换的新令牌吸收进缓存；返回是否有更新。"""
    got = False
    new_tk = r.cookies.get("_m_h5_tk")
    new_enc = r.cookies.get("_m_h5_tk_enc")
    if new_tk:
        _TOK_CACHE["tk"] = new_tk
        got = True
    if new_enc:
        _TOK_CACHE["enc"] = new_enc
        got = True
    return got


def fetch_token(cookies_str: str, timeout: float = 25.0) -> dict:
    """请求 token 接口（含 _m_h5_tk/_m_h5_tk_enc 成对轮换重试）。

    返回 {'status','ret','data','url','accessToken','raw','token_refreshed'}
    """
    r, payload = _sign_call(cookies_str, timeout)
    refreshed = False
    tries = 0
    while _is_expired(payload) and tries < 2:
        tries += 1
        if not _absorb_rotation(r):
            break
        refreshed = True
        r, payload = _sign_call(cookies_str, timeout)
    _absorb_rotation(r)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    return {"status": r.status_code, "ret": payload.get("ret"), "data": data,
            "url": data.get("url") or "", "accessToken": data.get("accessToken") or "",
            "raw": payload, "token_refreshed": refreshed}


if __name__ == "__main__":
    import pathlib
    import sys
    ck = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                      else "/opt/routec/scratch/cookie.txt")
    rr = fetch_token(ck.read_text().strip())
    print(json.dumps({k: (bool(v) if k == "accessToken" else v)
                      for k, v in rr.items() if k != "raw"}, ensure_ascii=False, indent=1))
