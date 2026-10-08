"""路线 C 滑块后端客户端（VM101 真机 SendInput）。

背景（T7 集成 / T5 重试策略）：
  容器内浏览器方案在本栈实测通过率 **0**（阿里 nc 不接受自动化输入），
  该方案连同人工投屏链路已于 v2（T7）**整体删除**；
  唯一实测可用的路线是 **路线 C**：VM101 的 Chrome + 真机 `SendInput` 拖动，
  由容器内 `routec-solver` 服务编排（T7 整合进镜像）（VM101 反向 SSH 隧道 → 8791 驱动 / 9222 CDP）。

  本模块只做一件事：把账号 cookie 交给后端，换回 x5sec。它**不做**任何浏览器操作。

重试策略（T5）：
  一次 `solve()` = **整个重试序列**。序列内每轮失败后间隔 ≥3s 再重来一轮
  （每轮都是独立的 `POST /solve`，后端会**重新注入 cookie、取新挑战**），
  最多 `max_attempts` 轮（默认 5）。全部失败才判定失败。
  **熔断器按整个序列计一次失败** —— 否则 5 次重试会被 `failure_threshold=3`
  的阈值提前打断。序列内不再检查熔断状态（进入前检查一次）。

  `max_attempts` 三级可配（优先级从高到低）：
    1. 环境变量 `SLIDER_ROUTE_C_MAX_ATTEMPTS`
    2. `system_settings['slider_route_c_max_attempts']`（**面板可改，热生效**）
    3. `global_config.yml` 的 `SLIDER_ROUTE_C.max_attempts`（默认 5）

调用契约（POST {endpoint}/solve，头 X-RouteC-Token）：
  请求  {"account": "<cookie_id>", "cookie": "<账号 cookie 串>",
         "timeout_s": 180, "verify": false}
  响应  {"ok": true,  "x5sec": "...", "x5_cookies": {...}, "elapsed_s": 37.7,
         "preflight": {...}, ...}
        {"ok": false, "reason": "preflight|no_challenge|slide_reject|api_fail|busy|error", ...}

前置体检（T3）：
  后端每轮在拖动前先跑六项体检 + 自愈（driver / lock / chrome / cdp / window / page）。
  体检不过 → reason="preflight"（detail 是卡住的那一步），**不会发起任何滑块请求**。
  结论随响应体 `preflight` 字段透传进 `info`，供面板日志/通知文案显示「卡在哪一步」。
  `preflight` 属不可重试判定码（本机链路问题，立刻重试无意义）。

安全红线：
  * cookie 与 x5sec 值**绝不**写日志、绝不进异常文本；
  * 返回给上层的 info 字典只含状态/耗时/判定码。

配置（优先级从高到低）：
  1. 环境变量 SLIDER_ROUTE_C_ENABLED / _ENDPOINT / _TOKEN / _TIMEOUT /
     _FAILURE_THRESHOLD / _COOLDOWN / _VERIFY / _MAX_ATTEMPTS / _RETRY_INTERVAL
  2. global_config.yml 的 SLIDER_ROUTE_C 段
  3. 内置默认值（enabled=True，指向容器内 127.0.0.1:8799）

一键熔断（不改配置、不重建容器）：
  `docker exec xianyu-super-butler touch /app/data/SLIDER_ROUTE_C_DISABLED`
  —— 该文件存在时路线 C 立即停用，滑块**直接判定失败**（v2 已无回退路径）。
"""

import json
import os
import pathlib
import threading
import time

import requests

DEFAULTS = {
    "enabled": True,
    "endpoint": "http://127.0.0.1:8799",
    "token": "",
    "timeout": 180,            # 单轮求解超时（秒），服务端一轮 ~37s
    "connect_timeout": 5,      # 连接/健康检查超时：后端不在就快速失败
    "failure_threshold": 3,    # 连续失败 N 个「序列」→ 熔断
    "cooldown": 600,           # 熔断冷却（秒）
    "verify": False,           # 是否让后端顺带做一次闭环 API 校验
    "max_attempts": 5,         # 一个求解序列内最多尝试的轮数（默认 5）
    "retry_interval": 3,       # 序列内相邻两轮的最小间隔（秒），硬下限 3
}

KILL_SWITCH = "/app/data/SLIDER_ROUTE_C_DISABLED"

# 序列内相邻两轮之间的硬下限：闲鱼风控下，太密的重试只是白白消耗账号请求。
MIN_RETRY_INTERVAL = 3.0
# 防呆上限：面板/环境变量写错一个 0 也不至于把账号打死。
MAX_ATTEMPTS_CEILING = 20
# 这些判定码重试没有意义（配置/输入/本机链路问题，不是「这一轮没拖动成功」）：
#   disabled / bad_input / unauthorized —— 配置或输入问题；
#   preflight（T3）—— 六项体检（含自愈）都没过，说明本机链路（驱动/隧道/Chrome）
#                     不可用，立刻重试同一套链路只是白等，直接交给熔断器 + 人工处理。
_NON_RETRYABLE_REASONS = frozenset({"disabled", "bad_input", "unauthorized", "preflight"})

_lock = threading.Lock()
_instance = None


def _env(name, cast):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    try:
        return cast(raw)
    except Exception:
        return None


def _as_bool(raw):
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _clamp_attempts(value, fallback=5):
    try:
        parsed = int(str(value).strip())
    except Exception:
        return fallback
    return max(1, min(parsed, MAX_ATTEMPTS_CEILING))


class RouteCSlider:
    """路线 C 后端客户端（带重试序列 + 熔断）。"""

    def __init__(self, cfg=None):
        cfg = dict(DEFAULTS, **(cfg or {}))
        self.endpoint = str(cfg["endpoint"]).rstrip("/")
        self.token = str(cfg.get("token") or "")
        self.timeout = float(cfg["timeout"])
        self.connect_timeout = float(cfg["connect_timeout"])
        self.failure_threshold = int(cfg["failure_threshold"])
        self.cooldown = int(cfg["cooldown"])
        self.verify = _as_bool(cfg.get("verify"))
        self._cfg_enabled = _as_bool(cfg.get("enabled"))
        # global_config.yml 档（最低优先级）—— env / system_settings 覆盖它
        self.max_attempts = _clamp_attempts(cfg.get("max_attempts"), DEFAULTS["max_attempts"])
        try:
            self.retry_interval = max(MIN_RETRY_INTERVAL, float(cfg.get("retry_interval", MIN_RETRY_INTERVAL)))
        except Exception:
            self.retry_interval = MIN_RETRY_INTERVAL
        # 可注入，便于单测不打真实 sleep
        self._sleep = time.sleep

        self._consecutive_failures = 0
        self._open_until = 0.0
        self._last = None
        self._stats = {"calls": 0, "ok": 0, "fail": 0, "sequences": 0}

    # ---------------- 开关 ----------------

    @property
    def enabled(self):
        """运行期开关：env 覆盖配置；熔断文件一票否决。"""
        env = _env("SLIDER_ROUTE_C_ENABLED", _as_bool)
        base = self._cfg_enabled if env is None else env
        if not base:
            return False
        try:
            if pathlib.Path(KILL_SWITCH).exists():
                return False
        except Exception:
            pass
        return True

    @property
    def circuit_open(self):
        return time.monotonic() < self._open_until

    def status(self):
        return {
            "enabled": self.enabled,
            "endpoint": self.endpoint,
            "circuit_open": self.circuit_open,
            "retry_in_s": max(0, int(self._open_until - time.monotonic())),
            "consecutive_failures": self._consecutive_failures,
            "failure_threshold": self.failure_threshold,
            "cooldown_s": self.cooldown,
            "max_attempts": self.max_attempts,
            "retry_interval_s": self.retry_interval,
            "last": self._last,
            "stats": dict(self._stats),
        }

    # ---------------- 重试次数 / 间隔解析 ----------------

    def resolve_max_attempts(self):
        """三级解析 max_attempts（env > system_settings > global_config.yml）。"""
        env_value = _env("SLIDER_ROUTE_C_MAX_ATTEMPTS", int)
        if env_value is not None:
            return _clamp_attempts(env_value, self.max_attempts)
        # 面板可改：每次求解都读一次 DB，改完立即生效，不用重启容器
        try:
            from app.db_manager import db_manager
            raw = db_manager.get_system_setting("slider_route_c_max_attempts")
            if raw is not None and str(raw).strip() != "":
                return _clamp_attempts(raw, self.max_attempts)
        except Exception:
            pass
        return _clamp_attempts(self.max_attempts, DEFAULTS["max_attempts"])

    def resolve_retry_interval(self):
        env_value = _env("SLIDER_ROUTE_C_RETRY_INTERVAL", float)
        if env_value is not None:
            return max(MIN_RETRY_INTERVAL, float(env_value))
        return max(MIN_RETRY_INTERVAL, float(self.retry_interval))

    # ---------------- 单轮求解 ----------------

    def _solve_once(self, account_id, cookies_str):
        """发起一轮 `POST /solve`。返回 (success, cookies|None, info)。永不抛异常。"""
        info = {"path": "route_c", "account": str(account_id)}
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-RouteC-Token"] = self.token
        payload = {
            "account": str(account_id),
            "cookie": cookies_str,
            "timeout_s": self.timeout,
            "verify": self.verify,
        }
        t0 = time.time()
        with _lock:
            self._stats["calls"] += 1
        try:
            # 本机有透明 TLS 代理会破坏 keep-alive —— 一律裸 requests.post，禁用 Session
            r = requests.post(self.endpoint + "/solve", data=json.dumps(payload),
                              headers=headers, timeout=(self.connect_timeout, self.timeout + 60))
            if r.status_code == 401:
                info["reason"] = "unauthorized"
            elif r.status_code == 503:
                body = r.json() if r.content else {}
                info["reason"] = body.get("reason") or "backend_unavailable"
            else:
                body = r.json()
                for k in ("reason", "elapsed_s", "challenge", "moves",
                          "slide_code", "detail", "verify", "iframe_pm", "preflight"):
                    if k in body:
                        info[k] = body[k]
                x5 = body.get("x5_cookies") or {}
                if body.get("ok") and x5.get("x5sec"):
                    info["reason"] = "pass"
                    info["x5_cookie_names"] = sorted(x5.keys())
                    return True, dict(x5), info
                info.setdefault("reason", "unknown")
        except Exception as e:
            # 异常文本里可能带上 URL，但绝不会带 cookie（cookie 只在 body 里）
            info["reason"] = "backend_unavailable"
            info["detail"] = "%s: %s" % (type(e).__name__, str(e)[:150])
        finally:
            info.setdefault("elapsed_s", round(time.time() - t0, 1))
        return False, None, info

    # ---------------- 求解（含重试序列） ----------------

    def solve(self, account_id, cookies_str, verification_url=None):
        """把账号 cookie 交给路线 C 后端，换回 x5 系列 cookie。

        一个序列 = 最多 `max_attempts` 轮，每轮失败后间隔 ≥3s 再重来。
        返回 (success: bool, cookies: dict|None, info: dict)。
        永不抛异常；info 不含任何凭据值。
        """
        info = {"path": "route_c", "account": str(account_id)}
        if not self.enabled:
            info["reason"] = "disabled"
            return False, None, info
        if self.circuit_open:
            info["reason"] = "circuit_open"
            info["retry_in_s"] = max(0, int(self._open_until - time.monotonic()))
            return False, None, info
        if not cookies_str or "_m_h5_tk" not in cookies_str:
            info["reason"] = "bad_input"
            return False, None, info

        max_attempts = self.resolve_max_attempts()
        interval = self.resolve_retry_interval()

        with _lock:
            self._stats["sequences"] += 1

        attempt_reasons = []
        last_info = {}
        success = False
        cookies = None
        t0 = time.time()

        for attempt in range(1, max_attempts + 1):
            if attempt > 1:
                # 序列内间隔 ≥3s：重来一轮 = 后端重新注入 cookie、取新挑战
                self._sleep(interval)
            ok, attempt_cookies, attempt_info = self._solve_once(account_id, cookies_str)
            last_info = attempt_info
            reason = attempt_info.get("reason") or "unknown"
            attempt_reasons.append(reason)
            if ok and attempt_cookies:
                success, cookies = True, attempt_cookies
                break
            if reason in _NON_RETRYABLE_REASONS:
                # 配置/输入类问题，重试无意义（也不该白等）
                break

        elapsed = round(time.time() - t0, 1)
        info["attempts"] = len(attempt_reasons)
        info["max_attempts"] = max_attempts
        info["attempt_reasons"] = attempt_reasons
        info["elapsed_s"] = elapsed

        if success:
            for k in ("challenge", "moves", "slide_code", "detail", "verify", "iframe_pm"):
                if k in last_info:
                    info[k] = last_info[k]
            info["preflight"] = last_info.get("preflight")
            info["reason"] = "pass"
            info["x5_cookie_names"] = sorted(cookies.keys())
            with _lock:
                self._stats["ok"] += 1
                self._consecutive_failures = 0
                self._open_until = 0.0
            self._last = dict(info)
            return True, cookies, info

        # 整个重试序列算**一次**失败（熔断器不被序列内的中间轮次打断）
        info["reason"] = attempt_reasons[-1] if attempt_reasons else "unknown"
        if "detail" in last_info:
            info["detail"] = last_info["detail"]
        info["preflight"] = last_info.get("preflight")
        with _lock:
            self._stats["fail"] += 1
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._open_until = time.monotonic() + self.cooldown
                info["circuit_opened"] = True
        self._last = dict(info)
        return False, None, info


def get_route_c_slider():
    """取全局客户端；未启用时返回 None（调用方据此直接判定失败）。"""
    global _instance
    cfg = None
    try:
        from app.config import SLIDER_ROUTE_C as cfg
    except Exception:
        cfg = None
    if _instance is None:
        merged = dict(cfg or {})
        for env_key, cfg_key, cast in (
            ("SLIDER_ROUTE_C_ENDPOINT", "endpoint", str),
            ("SLIDER_ROUTE_C_TOKEN", "token", str),
            ("SLIDER_ROUTE_C_TIMEOUT", "timeout", float),
            ("SLIDER_ROUTE_C_FAILURE_THRESHOLD", "failure_threshold", int),
            ("SLIDER_ROUTE_C_COOLDOWN", "cooldown", int),
            ("SLIDER_ROUTE_C_VERIFY", "verify", _as_bool),
            ("SLIDER_ROUTE_C_MAX_ATTEMPTS", "max_attempts", int),
            ("SLIDER_ROUTE_C_RETRY_INTERVAL", "retry_interval", float),
        ):
            v = _env(env_key, cast)
            if v is not None:
                merged[cfg_key] = v
        _instance = RouteCSlider(merged)
    if not _instance.enabled:
        return None
    return _instance
