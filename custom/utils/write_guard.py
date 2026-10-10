"""写操作限流：读写分桶 + 面板可配 + 每日上限。

读操作仍由 :mod:`utils.risk_control` 的令牌桶控制（30 rpm，**行为未变**）；
写操作（擦亮 / 发布 / 下架 / 删除等 mtop 写接口）走这里**独立的桶**，
不再和读共用同一个令牌桶。

配置从 ``system_settings`` **热读**（5 秒 TTL 缓存），改设置无需重启：

=====================  ======  ============================
key                    默认    说明
=====================  ======  ============================
``write_rate_enabled``  true   写限流总开关
``write_rate_per_minute``  1   写操作每分钟上限
``write_daily_limit``   20     单账号每日写操作上限
=====================  ======  ============================

配置项不存在时用上表默认值；值非法时也回落默认值，绝不让面板的脏数据
把限流关掉。

超限一律 **fail fast**：抛 :class:`WriteRateLimited`（带 ``retry_after`` 秒），
**绝不 sleep 等待** —— 等待会卡住事件循环，比拒绝更糟。

每日计数落在独立表 ``write_op_counters(cookie_id, day, count)``，
按 ``Asia/Shanghai`` 自然日隔离，跨天天然归零（无需定时清零），
**不写进 system_settings**（避免污染面板的设置列表）。

存储不可用时 **fail closed**（拒绝本次写操作）—— 宁可少写一次，
也不要在计数失灵的情况下放开账号。
"""

import datetime
import math
import threading
import time
from typing import Any, Dict, Optional

from loguru import logger


# ---------------------------------------------------------------- 配置

CONFIG_ENABLED = "write_rate_enabled"
CONFIG_RATE = "write_rate_per_minute"
CONFIG_DAILY = "write_daily_limit"

DEFAULT_ENABLED = True
DEFAULT_RATE_PER_MINUTE = 1
DEFAULT_DAILY_LIMIT = 20

# 配置缓存 TTL（秒）：改完 system_settings 最长 5 秒后生效
CONFIG_TTL_SECONDS = 5.0

# 每日计数表（独立于 system_settings）
COUNTER_TABLE = "write_op_counters"


# ---------------------------------------------------------------- 时区 / 自然日

def _make_tz():
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo("Asia/Shanghai")
    except Exception:  # 容器缺 tzdata 时回落固定 UTC+8（中国无夏令时，等价）
        return datetime.timezone(datetime.timedelta(hours=8))


TZ = _make_tz()


def today_key() -> str:
    """当前自然日（Asia/Shanghai），``YYYY-MM-DD``。"""
    return datetime.datetime.now(TZ).strftime("%Y-%m-%d")


def seconds_until_tomorrow() -> int:
    """距下一个自然日零点的秒数（至少 1）。"""
    now = datetime.datetime.now(TZ)
    tomorrow = (now + datetime.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(1, int(math.ceil((tomorrow - now).total_seconds())))


# ---------------------------------------------------------------- 异常

class WriteRateLimited(Exception):
    """写操作被限流拒绝。调用方应跳过本次写操作，不要重试等待。"""

    def __init__(
        self,
        cookie_id: str,
        retry_after: int,
        reason: str = "rate",
        message: Optional[str] = None,
    ):
        self.cookie_id = cookie_id
        self.retry_after = max(1, int(retry_after))
        self.reason = reason
        base = message or (
            "今日额度已用完" if reason == "daily" else "写操作过于频繁"
        )
        super().__init__(
            f"账号 {cookie_id} {base}，请 {self.retry_after} 秒后重试"
            f"（retry_after={self.retry_after}，reason={self.reason}）"
        )


# ---------------------------------------------------------------- 配置读取

def _to_bool(raw, default: bool) -> bool:
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if text in ("1", "true", "yes", "on", "y"):
        return True
    if text in ("0", "false", "no", "off", "n", ""):
        return False
    return default


def _to_int(raw, default: int, minimum: int = 1) -> int:
    if raw is None:
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


class _ConfigCache:
    """system_settings 三键的 5 秒 TTL 缓存。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._loaded_at = 0.0
        self._value: Optional[Dict[str, Any]] = None

    def get(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self._lock:
            if self._value is not None and (now - self._loaded_at) < CONFIG_TTL_SECONDS:
                return self._value
            self._value = self._load()
            self._loaded_at = now
            return self._value

    def invalidate(self) -> None:
        with self._lock:
            self._value = None
            self._loaded_at = 0.0

    @staticmethod
    def _load() -> Dict[str, Any]:
        enabled = DEFAULT_ENABLED
        rate = DEFAULT_RATE_PER_MINUTE
        daily = DEFAULT_DAILY_LIMIT
        try:
            from app.db_manager import db_manager

            enabled = _to_bool(
                db_manager.get_system_setting(CONFIG_ENABLED), DEFAULT_ENABLED
            )
            rate = _to_int(
                db_manager.get_system_setting(CONFIG_RATE),
                DEFAULT_RATE_PER_MINUTE,
                minimum=1,
            )
            daily = _to_int(
                db_manager.get_system_setting(CONFIG_DAILY),
                DEFAULT_DAILY_LIMIT,
                minimum=1,
            )
        except Exception as exc:  # 读不到就按默认值走，不抛给调用方
            logger.warning(f"写限流配置读取失败，使用默认值: {exc}")
        return {"enabled": enabled, "rate_per_minute": rate, "daily_limit": daily}


_config = _ConfigCache()


def reload_config() -> Dict[str, Any]:
    """丢弃缓存并立刻重读（改完设置想马上生效时可用，正常靠 5 秒 TTL）。"""
    _config.invalidate()
    return _config.get()


def current_config() -> Dict[str, Any]:
    """当前生效的写限流配置（含 TTL 缓存）。"""
    return _config.get()


# ---------------------------------------------------------------- 每日计数

_table_ready = False


def _with_conn(fn):
    from app.db_manager import db_manager

    with db_manager.lock:
        conn = db_manager.get_connection()
        _ensure_table(conn)
        return fn(conn)


def _ensure_table(conn) -> None:
    global _table_ready
    if _table_ready:
        return
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {COUNTER_TABLE} ("
        "cookie_id TEXT NOT NULL, "
        "day TEXT NOT NULL, "
        "count INTEGER NOT NULL DEFAULT 0, "
        "PRIMARY KEY (cookie_id, day))"
    )
    conn.commit()
    _table_ready = True


def _read_count(conn, cookie_id: str, day: str) -> int:
    cursor = conn.execute(
        f"SELECT count FROM {COUNTER_TABLE} WHERE cookie_id = ? AND day = ?",
        (cookie_id, day),
    )
    row = cursor.fetchone()
    return int(row[0]) if row else 0


def get_daily_used(cookie_id: str, day: Optional[str] = None) -> int:
    """该账号当天的写操作计数（只读，不消费）。失败时抛异常，由调用方决定。"""
    day = day or today_key()
    return _with_conn(lambda conn: _read_count(conn, cookie_id, day))


def _bump_daily(cookie_id: str, day: str) -> int:
    def _do(conn):
        conn.execute(
            f"INSERT INTO {COUNTER_TABLE} (cookie_id, day, count) VALUES (?, ?, 1) "
            "ON CONFLICT(cookie_id, day) DO UPDATE SET count = count + 1",
            (cookie_id, day),
        )
        conn.commit()
        return _read_count(conn, cookie_id, day)

    try:
        return _with_conn(_do)
    except Exception:
        # 表可能被外部重建/换库，重建一次再试
        global _table_ready
        _table_ready = False
        return _with_conn(_do)


def _storage_error(cookie_id: str) -> WriteRateLimited:
    return WriteRateLimited(
        cookie_id,
        retry_after=60,
        reason="storage",
        message="写限流计数不可用，已拒绝本次写操作",
    )


# ---------------------------------------------------------------- 写令牌桶

class WriteGuard:
    """单个账号的写操作令牌桶（分钟级）+ 只读余量查询。

    容量 = ``write_rate_per_minute``，按秒匀速补充。
    速率被改时**立即按新速率重置为满桶** —— 保证「改设置即时生效」。
    """

    def __init__(self, cookie_id: str):
        self.cookie_id = cookie_id
        self._capacity: Optional[float] = None
        self._tokens: float = 0.0
        self._last_refill: float = time.monotonic()

    def _available(self, rate: float) -> float:
        """只读估算当前可用令牌数（不改状态）。"""
        if self._capacity is None or self._capacity != float(rate):
            return float(rate)
        elapsed = max(0.0, time.monotonic() - self._last_refill)
        return min(self._capacity, self._tokens + elapsed * (self._capacity / 60.0))

    def available(self, cfg: Optional[Dict[str, Any]] = None) -> float:
        """只读：当前可用令牌数（浮点）。"""
        cfg = cfg or current_config()
        if not cfg["enabled"]:
            return float(cfg["rate_per_minute"])
        return self._available(float(cfg["rate_per_minute"]))

    def remaining(self, cfg: Optional[Dict[str, Any]] = None) -> int:
        """只读：当前可用令牌数（向下取整）。"""
        return int(math.floor(self.available(cfg)))

    def consume(self, cfg: Dict[str, Any]) -> None:
        """消费一个令牌；不足则抛 WriteRateLimited（不等待）。"""
        rate = float(cfg["rate_per_minute"])
        now = time.monotonic()
        if self._capacity is None or self._capacity != rate:
            # 首次使用 / 速率变更：满桶起步
            self._capacity = rate
            self._tokens = rate
            self._last_refill = now
        else:
            elapsed = max(0.0, now - self._last_refill)
            self._last_refill = now
            self._tokens = min(
                self._capacity, self._tokens + elapsed * (self._capacity / 60.0)
            )

        if self._tokens < 1.0:
            need = (1.0 - self._tokens) * (60.0 / self._capacity)
            raise WriteRateLimited(
                self.cookie_id,
                retry_after=max(1, int(math.ceil(need))),
                reason="rate",
                message=f"写操作过于频繁（每分钟上限 {int(self._capacity)}）",
            )
        self._tokens -= 1.0


class WriteGuardRegistry:
    """按账号维护 :class:`WriteGuard`（进程内，重启重置）。"""

    def __init__(self):
        self._guards: Dict[str, WriteGuard] = {}

    def get(self, cookie_id: str) -> WriteGuard:
        guard = self._guards.get(cookie_id)
        if guard is None:
            guard = WriteGuard(cookie_id)
            self._guards[cookie_id] = guard
        return guard

    def snapshot_for(self, cookie_id: str) -> Dict[str, Any]:
        """面板用：``write_*`` 扁平键 + ``write`` 嵌套字典（**只读，不消费**）。"""
        try:
            state = write_status(cookie_id)
        except Exception as exc:  # 状态查询永不把异常抛给面板
            logger.debug(f"写限流状态查询失败: {exc}")
            state = {}
        merged = {f"write_{key}": value for key, value in state.items()}
        merged["write"] = state
        return merged


write_guard_registry = WriteGuardRegistry()


# ---------------------------------------------------------------- 对外接口

def write_status(cookie_id: str, cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """只读状态：本分钟已用/剩余、今日已用/剩余。不消费令牌。"""
    cfg = cfg or current_config()
    enabled = bool(cfg["enabled"])
    limit_rate = int(cfg["rate_per_minute"])
    limit_daily = int(cfg["daily_limit"])

    try:
        used_daily = get_daily_used(cookie_id)
    except Exception as exc:
        logger.debug(f"写限流每日计数读取失败: {exc}")
        used_daily = 0

    guard = write_guard_registry.get(cookie_id)
    remaining_rate = guard.remaining(cfg) if enabled else limit_rate
    remaining_rate = max(0, min(limit_rate, remaining_rate))

    return {
        "cookie_id": cookie_id,
        "enabled": enabled,
        "per_minute_limit": limit_rate,
        "per_minute_used": max(0, limit_rate - remaining_rate),
        "per_minute_remaining": remaining_rate,
        "daily_limit": limit_daily,
        "daily_used": used_daily,
        "daily_remaining": max(0, limit_daily - used_daily),
        "retry_after": 0,
    }


def check_write(cookie_id: str) -> Dict[str, Any]:
    """只读检查：现在能不能写。不能写则抛 :class:`WriteRateLimited`。"""
    cfg = current_config()
    if not cfg["enabled"]:
        return write_status(cookie_id, cfg=cfg)

    day = today_key()
    try:
        used_daily = get_daily_used(cookie_id, day)
    except Exception as exc:
        logger.error(f"写限流每日计数读取失败: {exc}")
        raise _storage_error(cookie_id)

    if used_daily >= int(cfg["daily_limit"]):
        raise WriteRateLimited(
            cookie_id,
            retry_after=seconds_until_tomorrow(),
            reason="daily",
            message="今日额度已用完",
        )

    guard = write_guard_registry.get(cookie_id)
    if guard.remaining(cfg) < 1:
        rate = float(cfg["rate_per_minute"])
        need = (1.0 - guard.available(cfg)) * (60.0 / rate)
        raise WriteRateLimited(
            cookie_id,
            retry_after=max(1, int(math.ceil(need))),
            reason="rate",
            message=f"写操作过于频繁（每分钟上限 {int(rate)}）",
        )
    return write_status(cookie_id, cfg=cfg)


def acquire_write(cookie_id: str) -> Dict[str, Any]:
    """占用一次写操作配额。

    Raises:
        WriteRateLimited: 本分钟速率超限 / 今日额度用完 / 计数存储不可用。
            **fail fast，不阻塞等待** —— 调用方应跳过本次写操作。
    """
    cfg = current_config()
    if not cfg["enabled"]:
        return write_status(cookie_id, cfg=cfg)

    day = today_key()

    try:
        used_daily = get_daily_used(cookie_id, day)
    except Exception as exc:
        logger.error(f"写限流每日计数读取失败: {exc}")
        raise _storage_error(cookie_id)

    if used_daily >= int(cfg["daily_limit"]):
        raise WriteRateLimited(
            cookie_id,
            retry_after=seconds_until_tomorrow(),
            reason="daily",
            message="今日额度已用完",
        )

    guard = write_guard_registry.get(cookie_id)
    guard.consume(cfg)

    try:
        _bump_daily(cookie_id, day)
    except Exception as exc:
        logger.error(f"写限流每日计数写入失败: {exc}")
        raise _storage_error(cookie_id)

    return write_status(cookie_id, cfg=cfg)
