import asyncio
import concurrent.futures
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger


def _json_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        except json.JSONDecodeError:
            return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _json_value(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _as_dict(cursor, row) -> Dict[str, Any]:
    return dict(zip((column[0] for column in cursor.description), row))


# ======================================================================
# W10：状态机常量 / 白名单通道 / 确认令牌 / 定时循环
# ======================================================================
#
# 状态机（写死，见卡片）::
#
#   draft --(面板编辑设为 ready)--> ready
#   ready --(开始执行)--> publishing --成功--> published  (同时回写 published_item_id)
#                                    \--失败--> failed     (记 run + 推 QQ，不自动重试)
#
# 白名单（写死）：
#   发布   `product_materials.auto_approved = 1 AND publish_status = 'ready'`
#   删除   `product_delete_rules.auto_execute = 1 AND enabled = 1`
#
# 默认全 0 / 全关 —— 不显式加白名单、不打开总开关时，只有人工（带确认令牌）能触发。

PUBLISH_STATUS_DRAFT = "draft"
PUBLISH_STATUS_READY = "ready"
PUBLISH_STATUS_PUBLISHING = "publishing"
PUBLISH_STATUS_PUBLISHED = "published"
PUBLISH_STATUS_FAILED = "failed"

#: 总开关（默认关；W17 验收时显式打开）
AUTO_ENABLED_KEY = "product_auto_enabled"
#: 循环间隔（秒，下限 600）
AUTO_INTERVAL_KEY = "product_auto_interval"
AUTO_INTERVAL_DEFAULT = 3600
AUTO_INTERVAL_MIN = 600
#: dry-run 复用 W7 的键
DRY_RUN_KEY = "publish_dry_run"
#: 总开关关闭时工作线程的空转节拍
WORKER_TICK_SECONDS = 30
#: 确认令牌有效期（秒）
CONFIRM_TOKEN_TTL = 300

ACTION_PUBLISH = "publish"
ACTION_DELETE_EXECUTE = "delete_execute"


def _material_resource(material_id: Any) -> str:
    return f"material:{material_id}"


def _delete_rule_resource(rule_id: Any) -> str:
    return f"delete_rule:{rule_id}"


class ConfirmTokenError(PermissionError):
    """确认令牌校验失败。

    ``reason`` 是给路由层用的稳定文案键：
    ``token_missing`` / ``token_expired`` / ``token_mismatch`` / ``token_used``。
    """

    def __init__(self, reason: str, message: str = ""):
        self.reason = reason
        super().__init__(message or reason)


class ConfirmTokenStore:
    """一次性确认令牌（内存 dict，TTL 300 秒，不落库；重启即失效）。

    - 绑定 ``(resource_id, action)``：换素材 / 换操作都用不了（``token_mismatch``）。
    - 一次性：``consume()`` 成功即置 ``used``，重复使用报 ``token_used``。
    - 每次访问先清理；已过期但尚未清理的令牌报 ``token_expired``（而不是 missing），
      这样「过期 / 重复使用 / 不匹配 / 缺失」四种情况能分别报出来。
    """

    def __init__(self, ttl: int = CONFIRM_TOKEN_TTL):
        self.ttl = max(1, int(ttl))
        self._items: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def issue(self, resource_id: Any, action: str) -> str:
        token = uuid.uuid4().hex
        with self._lock:
            self._purge()
            self._items[token] = {
                "resource_id": str(resource_id),
                "action": str(action),
                "expires_at": time.time() + self.ttl,
                "used": False,
            }
        return token

    def consume(self, token: Any, resource_id: Any, action: str) -> None:
        if not token:
            raise ConfirmTokenError("token_missing", "缺少确认令牌")
        key = str(token)
        with self._lock:
            self._purge()
            item = self._items.get(key)
            if item is None:
                raise ConfirmTokenError("token_missing", "确认令牌不存在或已失效")
            if item["used"]:
                raise ConfirmTokenError("token_used", "确认令牌已使用过")
            if item["expires_at"] < time.time():
                del self._items[key]
                raise ConfirmTokenError("token_expired", "确认令牌已过期")
            if item["resource_id"] != str(resource_id) or item["action"] != str(action):
                raise ConfirmTokenError("token_mismatch", "确认令牌与目标不匹配")
            item["used"] = True

    def _purge(self) -> None:
        """清掉「已过期一个 TTL 以上」的条目（过期未清理的仍留一轮，好报 token_expired）。"""
        now = time.time()
        stale = [key for key, item in self._items.items() if item["expires_at"] + self.ttl < now]
        for key in stale:
            del self._items[key]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def _download_image(url: str, timeout: int = 20) -> Optional[str]:
    """把远端图片落到临时文件；失败返回 None（调用方保留原值）。

    ``utils/image_uploader`` 只吃本地路径（PIL 打不开 URL），而素材抓取时
    ``images`` 存的是 CDN 链接，所以这里先下到本地。裸 ``requests.get`` ——
    本机透明 TLS 代理会破坏 keep-alive，禁用 Session / Retry 适配器。
    """
    try:
        import requests

        response = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        if response.status_code != 200 or not response.content:
            logger.warning(f"素材远端图片下载失败 HTTP {response.status_code}")
            return None
        suffix = os.path.splitext(url.split("?")[0])[1].lower()
        if not suffix.startswith(".") or len(suffix) > 6:
            suffix = ".jpg"
        directory = tempfile.mkdtemp(prefix="product-auto-img-")
        path = os.path.join(directory, f"image{suffix}")
        with open(path, "wb") as handle:
            handle.write(response.content)
        return path
    except Exception as exc:
        logger.warning(f"素材远端图片下载异常: {type(exc).__name__}: {exc}")
        return None


class ProductAutomationService:
    """Local product automation with explicit ownership and dry-run deletion."""

    def __init__(self, manager):
        self.db = manager
        self._tokens = ConfirmTokenStore()
        self._worker: Optional[threading.Thread] = None
        self._worker_stop = threading.Event()
        self.init_schema()
        # W10：定时循环随服务实例一起起（reply_server 只 import 一次 → 只有一个线程）。
        # 总开关默认关（system_settings.product_auto_enabled），关着时线程只空转不干活。
        self.start_auto_worker()

    def init_schema(self) -> None:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.executescript(
                """
                CREATE TABLE IF NOT EXISTS product_filter_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    cookie_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    include_keywords TEXT DEFAULT '[]',
                    exclude_keywords TEXT DEFAULT '[]',
                    min_price REAL,
                    max_price REAL,
                    category TEXT DEFAULT '',
                    daily_limit INTEGER DEFAULT 50,
                    enabled INTEGER DEFAULT 1,
                    today_count INTEGER DEFAULT 0,
                    total_count INTEGER DEFAULT 0,
                    counter_date TEXT,
                    last_run_at TIMESTAMP,
                    last_run_status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (cookie_id) REFERENCES cookies(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS product_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    cookie_id TEXT NOT NULL,
                    rule_id INTEGER,
                    source_item_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    category TEXT DEFAULT '',
                    price REAL,
                    images TEXT DEFAULT '[]',
                    source_url TEXT DEFAULT '',
                    short_url TEXT DEFAULT '',
                    delivery_content TEXT DEFAULT '',
                    publish_status TEXT DEFAULT 'draft',
                    published_item_id TEXT DEFAULT '',
                    publish_trace_code TEXT DEFAULT '',
                    auto_card_id INTEGER,
                    auto_approved INTEGER DEFAULT 0,
                    auto_approved_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, cookie_id, source_item_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (cookie_id) REFERENCES cookies(id) ON DELETE CASCADE,
                    FOREIGN KEY (rule_id) REFERENCES product_filter_rules(id) ON DELETE SET NULL,
                    FOREIGN KEY (auto_card_id) REFERENCES cards(id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS product_delete_rules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    cookie_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    min_publish_days INTEGER DEFAULT 30,
                    daily_limit INTEGER DEFAULT 10,
                    skip_reply_activity INTEGER DEFAULT 1,
                    skip_order_activity INTEGER DEFAULT 1,
                    enabled INTEGER DEFAULT 0,
                    auto_execute INTEGER DEFAULT 0,
                    execution_mode TEXT DEFAULT 'dry_run',
                    last_run_at TIMESTAMP,
                    last_run_status TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(user_id, cookie_id),
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                    FOREIGN KEY (cookie_id) REFERENCES cookies(id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS automation_task_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    cookie_id TEXT,
                    task_type TEXT NOT NULL,
                    rule_id INTEGER,
                    execution_mode TEXT DEFAULT 'local',
                    checked_count INTEGER DEFAULT 0,
                    matched_count INTEGER DEFAULT 0,
                    changed_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    summary TEXT DEFAULT '',
                    details TEXT DEFAULT '[]',
                    error_message TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_product_materials_owner
                    ON product_materials(user_id, cookie_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_automation_task_runs_owner
                    ON automation_task_runs(user_id, created_at DESC);
                """
            )
            self.db.conn.commit()
            self._migrate_schema(cursor)

    #: W10 新增列（旧库 ALTER 补齐；新库建表时已带，探测成功即跳过）
    _MIGRATIONS: Tuple[Tuple[str, str, str], ...] = (
        ("product_materials", "auto_approved", "INTEGER DEFAULT 0"),
        ("product_materials", "auto_approved_at", "TIMESTAMP"),
        ("product_delete_rules", "auto_execute", "INTEGER DEFAULT 0"),
    )

    def _migrate_schema(self, cursor) -> None:
        """ALTER TABLE 补列，幂等。

        探测列是否存在用 ``SELECT`` —— 列不存在时 SQLite 报 OperationalError。
        ``ADD COLUMN ... DEFAULT 0`` 会把旧行一并填 0，
        即「迁移后默认只有人工能触发」，与硬约束一致。
        """
        for table, column, ddl in self._MIGRATIONS:
            try:
                cursor.execute(f"SELECT {column} FROM {table} LIMIT 1")
            except sqlite3.OperationalError:
                logger.info(f"product_automation 迁移：为 {table} 添加 {column} 列...")
                cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                logger.info(f"product_automation 迁移完成：{table}.{column}")
            except Exception as exc:  # pragma: no cover - 表缺失等异常不吞
                logger.error(f"product_automation 迁移探测 {table}.{column} 失败: {exc}")
                raise
        self.db.conn.commit()

    def _owned_accounts(self, user_id: int) -> Dict[str, str]:
        return self.db.get_all_cookies(user_id) or {}

    def require_account(self, user_id: int, cookie_id: str) -> None:
        if cookie_id not in self._owned_accounts(user_id):
            raise PermissionError("账号不存在或不属于当前用户")

    def _record_run(
        self,
        user_id: int,
        cookie_id: Optional[str],
        task_type: str,
        rule_id: Optional[int],
        mode: str,
        checked: int,
        matched: int,
        changed: int,
        failed: int,
        summary: str,
        details: Optional[List[Dict[str, Any]]] = None,
        error: str = "",
    ) -> int:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                INSERT INTO automation_task_runs (
                    user_id, cookie_id, task_type, rule_id, execution_mode,
                    checked_count, matched_count, changed_count, failed_count,
                    summary, details, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    cookie_id,
                    task_type,
                    rule_id,
                    mode,
                    checked,
                    matched,
                    changed,
                    failed,
                    summary,
                    json.dumps(details or [], ensure_ascii=False),
                    error,
                ),
            )
            self.db.conn.commit()
            return cursor.lastrowid

    def list_runs(self, user_id: int, limit: int = 50) -> List[Dict[str, Any]]:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                SELECT * FROM automation_task_runs
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (user_id, max(1, min(limit, 200))),
            )
            rows = [_as_dict(cursor, row) for row in cursor.fetchall()]
        for row in rows:
            row["details"] = _json_value(row.get("details"), [])
        return rows

    def list_materials(self, user_id: int, cookie_id: Optional[str] = None) -> List[Dict[str, Any]]:
        params: List[Any] = [user_id]
        condition = "user_id = ?"
        if cookie_id:
            self.require_account(user_id, cookie_id)
            condition += " AND cookie_id = ?"
            params.append(cookie_id)
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                f"""
                SELECT * FROM product_materials
                WHERE {condition}
                ORDER BY updated_at DESC, id DESC
                """,
                params,
            )
            rows = [_as_dict(cursor, row) for row in cursor.fetchall()]
        for row in rows:
            row["images"] = _json_list(row.get("images"))
        return rows

    def update_material(self, user_id: int, material_id: int, changes: Dict[str, Any]) -> Dict[str, Any]:
        allowed = {
            "title",
            "description",
            "category",
            "price",
            "images",
            "source_url",
            "short_url",
            "delivery_content",
            "publish_status",
            "published_item_id",
            "publish_trace_code",
        }
        updates = []
        params: List[Any] = []
        for key, value in changes.items():
            if key not in allowed:
                continue
            updates.append(f"{key} = ?")
            params.append(json.dumps(value, ensure_ascii=False) if key == "images" else value)
        if not updates:
            raise ValueError("没有可更新的字段")
        params.extend([material_id, user_id])
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                f"""
                UPDATE product_materials
                SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                params,
            )
            if cursor.rowcount == 0:
                raise LookupError("素材不存在")
            self.db.conn.commit()
            cursor.execute(
                "SELECT * FROM product_materials WHERE id = ? AND user_id = ?",
                (material_id, user_id),
            )
            result = _as_dict(cursor, cursor.fetchone())
        result["images"] = _json_list(result.get("images"))
        return result

    def delete_material(self, user_id: int, material_id: int) -> bool:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "DELETE FROM product_materials WHERE id = ? AND user_id = ?",
                (material_id, user_id),
            )
            self.db.conn.commit()
            return cursor.rowcount > 0

    def list_filter_rules(self, user_id: int) -> List[Dict[str, Any]]:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                SELECT * FROM product_filter_rules
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            )
            rows = [_as_dict(cursor, row) for row in cursor.fetchall()]
        for row in rows:
            row["include_keywords"] = _json_list(row.get("include_keywords"))
            row["exclude_keywords"] = _json_list(row.get("exclude_keywords"))
            row["enabled"] = bool(row.get("enabled"))
        return rows

    def save_filter_rule(
        self, user_id: int, data: Dict[str, Any], rule_id: Optional[int] = None
    ) -> Dict[str, Any]:
        cookie_id = str(data.get("cookie_id") or "").strip()
        self.require_account(user_id, cookie_id)
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("规则名称不能为空")
        values = {
            "cookie_id": cookie_id,
            "name": name,
            "include_keywords": json.dumps(_json_list(data.get("include_keywords")), ensure_ascii=False),
            "exclude_keywords": json.dumps(_json_list(data.get("exclude_keywords")), ensure_ascii=False),
            "min_price": data.get("min_price"),
            "max_price": data.get("max_price"),
            "category": str(data.get("category") or "").strip(),
            "daily_limit": max(1, min(int(data.get("daily_limit") or 50), 1000)),
            "enabled": 1 if data.get("enabled", True) else 0,
        }
        with self.db.lock:
            cursor = self.db.conn.cursor()
            if rule_id is None:
                cursor.execute(
                    """
                    INSERT INTO product_filter_rules (
                        user_id, cookie_id, name, include_keywords, exclude_keywords,
                        min_price, max_price, category, daily_limit, enabled
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, *values.values()),
                )
                rule_id = cursor.lastrowid
            else:
                cursor.execute(
                    """
                    UPDATE product_filter_rules SET
                        cookie_id = ?, name = ?, include_keywords = ?,
                        exclude_keywords = ?, min_price = ?, max_price = ?,
                        category = ?, daily_limit = ?, enabled = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ?
                    """,
                    (*values.values(), rule_id, user_id),
                )
                if cursor.rowcount == 0:
                    raise LookupError("筛选规则不存在")
            self.db.conn.commit()
        return next(rule for rule in self.list_filter_rules(user_id) if rule["id"] == rule_id)

    def delete_filter_rule(self, user_id: int, rule_id: int) -> bool:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "DELETE FROM product_filter_rules WHERE id = ? AND user_id = ?",
                (rule_id, user_id),
            )
            self.db.conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _price(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
        return float(match.group()) if match else None

    def run_filter_rule(self, user_id: int, rule_id: int) -> Dict[str, Any]:
        rules = [rule for rule in self.list_filter_rules(user_id) if rule["id"] == rule_id]
        if not rules:
            raise LookupError("筛选规则不存在")
        rule = rules[0]
        if not rule["enabled"]:
            raise ValueError("筛选规则未启用")
        self.require_account(user_id, rule["cookie_id"])
        today = datetime.now(timezone.utc).astimezone().date().isoformat()
        used_today = rule["today_count"] if rule.get("counter_date") == today else 0
        remaining = max(0, int(rule["daily_limit"]) - int(used_today or 0))

        items = self.db.get_items_by_cookie(rule["cookie_id"])
        matched: List[Dict[str, Any]] = []
        details: List[Dict[str, Any]] = []
        for item in items:
            title = str(item.get("item_title") or "")
            description = str(item.get("item_description") or item.get("item_detail") or "")
            haystack = f"{title}\n{description}".lower()
            price = self._price(item.get("item_price"))
            include = rule["include_keywords"]
            exclude = rule["exclude_keywords"]
            reason = ""
            if include and not any(keyword.lower() in haystack for keyword in include):
                reason = "未命中包含关键词"
            elif exclude and any(keyword.lower() in haystack for keyword in exclude):
                reason = "命中排除关键词"
            elif rule.get("min_price") is not None and (price is None or price < rule["min_price"]):
                reason = "低于最低价格"
            elif rule.get("max_price") is not None and (price is None or price > rule["max_price"]):
                reason = "高于最高价格"
            elif rule.get("category") and rule["category"].lower() not in str(item.get("item_category") or "").lower():
                reason = "分类不匹配"
            if reason:
                details.append({"item_id": item.get("item_id"), "status": "skipped", "reason": reason})
                continue
            matched.append(item)

        selected = matched[:remaining]
        changed = 0
        with self.db.lock:
            cursor = self.db.conn.cursor()
            for item in selected:
                item_id = str(item.get("item_id") or "")
                image = str(item.get("item_image") or "")
                trace = f"XYB-{item_id[-8:]}"
                cursor.execute(
                    """
                    INSERT INTO product_materials (
                        user_id, cookie_id, rule_id, source_item_id, title,
                        description, category, price, images, source_url,
                        publish_trace_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, cookie_id, source_item_id) DO UPDATE SET
                        rule_id = excluded.rule_id,
                        title = excluded.title,
                        description = CASE
                            WHEN product_materials.description = '' THEN excluded.description
                            ELSE product_materials.description
                        END,
                        category = excluded.category,
                        price = excluded.price,
                        images = CASE
                            WHEN product_materials.images = '[]' THEN excluded.images
                            ELSE product_materials.images
                        END,
                        source_url = excluded.source_url,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        user_id,
                        rule["cookie_id"],
                        rule_id,
                        item_id,
                        item.get("item_title") or f"商品 {item_id}",
                        item.get("item_description") or item.get("item_detail") or "",
                        item.get("item_category") or "",
                        self._price(item.get("item_price")),
                        json.dumps([image] if image else [], ensure_ascii=False),
                        f"https://www.goofish.com/item?id={item_id}",
                        trace,
                    ),
                )
                changed += 1
                details.append({"item_id": item_id, "status": "saved", "reason": "写入素材库"})
            cursor.execute(
                """
                UPDATE product_filter_rules SET
                    today_count = ?, total_count = total_count + ?,
                    counter_date = ?, last_run_at = CURRENT_TIMESTAMP,
                    last_run_status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (used_today + changed, changed, today, "success", rule_id, user_id),
            )
            self.db.conn.commit()

        summary = f"检查 {len(items)} 件，匹配 {len(matched)} 件，写入 {changed} 件"
        run_id = self._record_run(
            user_id, rule["cookie_id"], "material_filter", rule_id, "local",
            len(items), len(matched), changed, 0, summary, details,
        )
        return {"run_id": run_id, "summary": summary, "details": details}

    def list_delete_rules(self, user_id: int) -> List[Dict[str, Any]]:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                SELECT * FROM product_delete_rules
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC
                """,
                (user_id,),
            )
            rows = [_as_dict(cursor, row) for row in cursor.fetchall()]
        for row in rows:
            row["enabled"] = bool(row["enabled"])
            row["auto_execute"] = bool(row["auto_execute"])
            row["skip_reply_activity"] = bool(row["skip_reply_activity"])
            row["skip_order_activity"] = bool(row["skip_order_activity"])
        return rows

    def save_delete_rule(
        self, user_id: int, data: Dict[str, Any], rule_id: Optional[int] = None
    ) -> Dict[str, Any]:
        cookie_id = str(data.get("cookie_id") or "").strip()
        self.require_account(user_id, cookie_id)
        name = str(data.get("name") or "").strip()
        if not name:
            raise ValueError("计划名称不能为空")
        mode = str(data.get("execution_mode") or "dry_run")
        if mode != "dry_run":
            raise ValueError("当前版本仅支持 dry_run 预演")
        values = (
            cookie_id,
            name,
            max(1, int(data.get("min_publish_days") or 30)),
            max(1, min(int(data.get("daily_limit") or 10), 200)),
            1 if data.get("skip_reply_activity", True) else 0,
            1 if data.get("skip_order_activity", True) else 0,
            1 if data.get("enabled", False) else 0,
            "dry_run",
        )
        with self.db.lock:
            cursor = self.db.conn.cursor()
            if rule_id is None:
                cursor.execute(
                    """
                    INSERT INTO product_delete_rules (
                        user_id, cookie_id, name, min_publish_days, daily_limit,
                        skip_reply_activity, skip_order_activity, enabled, execution_mode
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, cookie_id) DO UPDATE SET
                        name = excluded.name,
                        min_publish_days = excluded.min_publish_days,
                        daily_limit = excluded.daily_limit,
                        skip_reply_activity = excluded.skip_reply_activity,
                        skip_order_activity = excluded.skip_order_activity,
                        enabled = excluded.enabled,
                        execution_mode = 'dry_run',
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (user_id, *values),
                )
                cursor.execute(
                    "SELECT id FROM product_delete_rules WHERE user_id = ? AND cookie_id = ?",
                    (user_id, cookie_id),
                )
                rule_id = cursor.fetchone()[0]
            else:
                cursor.execute(
                    """
                    UPDATE product_delete_rules SET
                        cookie_id = ?, name = ?, min_publish_days = ?, daily_limit = ?,
                        skip_reply_activity = ?, skip_order_activity = ?, enabled = ?,
                        execution_mode = 'dry_run', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ?
                    """,
                    (*values, rule_id, user_id),
                )
                if cursor.rowcount == 0:
                    raise LookupError("删除计划不存在")
            self.db.conn.commit()
        return next(rule for rule in self.list_delete_rules(user_id) if rule["id"] == rule_id)

    def delete_delete_rule(self, user_id: int, rule_id: int) -> bool:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "DELETE FROM product_delete_rules WHERE id = ? AND user_id = ?",
                (rule_id, user_id),
            )
            self.db.conn.commit()
            return cursor.rowcount > 0

    def preview_delete_rule(self, user_id: int, rule_id: int) -> Dict[str, Any]:
        rules = [rule for rule in self.list_delete_rules(user_id) if rule["id"] == rule_id]
        if not rules:
            raise LookupError("删除计划不存在")
        rule = rules[0]
        self.require_account(user_id, rule["cookie_id"])
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                SELECT item_id, item_title, created_at,
                       CAST(julianday('now') - julianday(created_at) AS INTEGER) AS age_days
                FROM item_info
                WHERE cookie_id = ?
                ORDER BY created_at ASC, id ASC
                """,
                (rule["cookie_id"],),
            )
            items = [_as_dict(cursor, row) for row in cursor.fetchall()]
            cursor.execute(
                """
                SELECT DISTINCT item_id FROM auto_reply_message_logs
                WHERE cookie_id = ? AND item_id IS NOT NULL AND item_id != ''
                """,
                (rule["cookie_id"],),
            )
            replied = {str(row[0]) for row in cursor.fetchall()}
            cursor.execute(
                """
                SELECT DISTINCT item_id FROM orders
                WHERE cookie_id = ? AND item_id IS NOT NULL AND item_id != ''
                """,
                (rule["cookie_id"],),
            )
            ordered = {str(row[0]) for row in cursor.fetchall()}

        candidates = []
        skipped = []
        for item in items:
            item_id = str(item["item_id"])
            reason = ""
            if int(item.get("age_days") or 0) < int(rule["min_publish_days"]):
                reason = f"上架不足 {rule['min_publish_days']} 天"
            elif rule["skip_reply_activity"] and item_id in replied:
                reason = "存在自动回复活动"
            elif rule["skip_order_activity"] and item_id in ordered:
                reason = "存在订单记录"
            target = {**item, "reason": reason or "符合预演条件"}
            (skipped if reason else candidates).append(target)
        candidates = candidates[: int(rule["daily_limit"])]
        summary = f"检查 {len(items)} 件，候选 {len(candidates)} 件，跳过 {len(skipped)} 件；未执行真实删除"
        run_id = self._record_run(
            user_id, rule["cookie_id"], "delete_preview", rule_id, "dry_run",
            len(items), len(candidates), 0, 0, summary, candidates + skipped,
        )
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                """
                UPDATE product_delete_rules SET
                    last_run_at = CURRENT_TIMESTAMP, last_run_status = 'dry_run',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND user_id = ?
                """,
                (rule_id, user_id),
            )
            self.db.conn.commit()
        return {
            "run_id": run_id,
            "mode": "dry_run",
            "summary": summary,
            "candidates": candidates,
            "skipped": skipped,
        }

    def repair_published_ids(self, user_id: int) -> Dict[str, Any]:
        materials = self.list_materials(user_id)
        owned = self._owned_accounts(user_id)
        checked = matched = changed = failed = 0
        details = []
        with self.db.lock:
            cursor = self.db.conn.cursor()
            for material in materials:
                if material["cookie_id"] not in owned or material.get("published_item_id"):
                    continue
                trace = str(material.get("publish_trace_code") or "").strip()
                if not trace:
                    continue
                checked += 1
                cursor.execute(
                    """
                    SELECT item_id, item_title FROM item_info
                    WHERE cookie_id = ? AND item_title LIKE ?
                    ORDER BY updated_at DESC LIMIT 2
                    """,
                    (material["cookie_id"], f"%{trace}%"),
                )
                rows = cursor.fetchall()
                if len(rows) == 1:
                    matched += 1
                    cursor.execute(
                        """
                        UPDATE product_materials SET
                            published_item_id = ?, publish_status = 'published',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND user_id = ?
                        """,
                        (str(rows[0][0]), material["id"], user_id),
                    )
                    changed += 1
                    details.append({"material_id": material["id"], "status": "repaired", "item_id": str(rows[0][0])})
                else:
                    details.append({
                        "material_id": material["id"],
                        "status": "skipped",
                        "reason": "未找到唯一匹配的追踪标记",
                    })
            self.db.conn.commit()
        summary = f"检查 {checked} 条，匹配 {matched} 条，回写 {changed} 条"
        run_id = self._record_run(
            user_id, None, "published_id_repair", None, "local",
            checked, matched, changed, failed, summary, details,
        )
        return {"run_id": run_id, "summary": summary, "details": details}

    def repair_short_links(self, user_id: int) -> Dict[str, Any]:
        materials = self.list_materials(user_id)
        checked = matched = changed = 0
        details = []
        with self.db.lock:
            cursor = self.db.conn.cursor()
            for material in materials:
                checked += 1
                link = str(material.get("short_url") or material.get("source_url") or "").strip()
                if not link and material.get("published_item_id"):
                    link = f"https://www.goofish.com/item?id={material['published_item_id']}"
                if not link:
                    details.append({"material_id": material["id"], "status": "skipped", "reason": "没有可用链接"})
                    continue
                matched += 1
                description = str(material.get("description") or "").rstrip()
                marker = "购买链接："
                new_description = re.sub(r"(?:\n|^)?购买链接：\S+", "", description).rstrip()
                new_description = f"{new_description}\n{marker}{link}".strip()
                if new_description == description:
                    details.append({"material_id": material["id"], "status": "unchanged"})
                    continue
                cursor.execute(
                    """
                    UPDATE product_materials SET
                        description = ?, short_url = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND user_id = ?
                    """,
                    (new_description, link, material["id"], user_id),
                )
                changed += 1
                details.append({"material_id": material["id"], "status": "repaired", "url": link})
            self.db.conn.commit()
        summary = f"检查 {checked} 条，可修复 {matched} 条，更新 {changed} 条"
        run_id = self._record_run(
            user_id, None, "short_link_repair", None, "local",
            checked, matched, changed, 0, summary, details,
        )
        return {"run_id": run_id, "summary": summary, "details": details}

    def compensate_cards(self, user_id: int) -> Dict[str, Any]:
        materials = self.list_materials(user_id)
        cards = self.db.get_all_cards(user_id)
        rules = self.db.get_all_delivery_rules(user_id)
        checked = matched = changed = failed = 0
        details = []
        card_by_id = {card["id"]: card for card in cards}
        rule_by_keyword = {str(rule.get("keyword") or ""): rule for rule in rules}
        for material in materials:
            item_id = str(material.get("published_item_id") or "").strip()
            content = str(material.get("delivery_content") or "").strip()
            if not item_id or not content:
                continue
            checked += 1
            try:
                card_id = material.get("auto_card_id")
                card = card_by_id.get(card_id)
                if not card:
                    name = f"素材发货-{item_id}"
                    existing = next(
                        (
                            item for item in cards
                            if item.get("name") == name
                            and item.get("type") == "text"
                            and item.get("text_content") == content
                        ),
                        None,
                    )
                    if existing:
                        card_id = existing["id"]
                    else:
                        card_id = self.db.create_card(
                            name=name,
                            card_type="text",
                            text_content=content,
                            description=f"商品自动化补偿，精确绑定商品 {item_id}",
                            enabled=True,
                            user_id=user_id,
                        )
                        cards.append(self.db.get_card_by_id(card_id, user_id))
                    with self.db.lock:
                        cursor = self.db.conn.cursor()
                        cursor.execute(
                            """
                            UPDATE product_materials SET auto_card_id = ?,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = ? AND user_id = ?
                            """,
                            (card_id, material["id"], user_id),
                        )
                        self.db.conn.commit()
                    changed += 1
                if item_id not in rule_by_keyword:
                    rule_id = self.db.create_delivery_rule(
                        keyword=item_id,
                        card_id=card_id,
                        delivery_count=1,
                        enabled=True,
                        description=f"素材自动补偿：{material['title']}",
                        user_id=user_id,
                    )
                    rule_by_keyword[item_id] = {"id": rule_id, "keyword": item_id, "card_id": card_id}
                    changed += 1
                matched += 1
                details.append({"material_id": material["id"], "status": "bound", "item_id": item_id, "card_id": card_id})
            except Exception as exc:
                failed += 1
                logger.exception("素材卡券补偿失败")
                details.append({"material_id": material["id"], "status": "failed", "reason": str(exc)})
        summary = f"检查 {checked} 条，完成绑定 {matched} 条，新增或修复 {changed} 项，失败 {failed} 项"
        run_id = self._record_run(
            user_id, None, "card_compensation", None, "local",
            checked, matched, changed, failed, summary, details,
        )
        return {"run_id": run_id, "summary": summary, "details": details}

    # ==================================================================
    # W10：设置读取
    # ==================================================================

    def _setting(self, key: str) -> Optional[str]:
        try:
            return self.db.get_system_setting(key)
        except Exception as exc:
            logger.warning(f"读取 system_settings.{key} 失败: {type(exc).__name__}: {exc}")
            return None

    @staticmethod
    def _truthy(raw: Any, default: bool = False) -> bool:
        if raw is None:
            return default
        text = str(raw).strip().lower()
        if text == "":
            return default
        return text in ("1", "true", "yes", "on", "y")

    def auto_enabled(self) -> bool:
        """总开关 ``product_auto_enabled``，默认 **false**。"""
        return self._truthy(self._setting(AUTO_ENABLED_KEY), False)

    def auto_interval(self) -> int:
        """循环间隔（秒），默认 3600，下限 600。"""
        raw = self._setting(AUTO_INTERVAL_KEY)
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            value = AUTO_INTERVAL_DEFAULT
        return max(AUTO_INTERVAL_MIN, value)

    def publish_dry_run(self) -> bool:
        """``publish_dry_run``（复用 W7 的键）；缺失 / 非法一律按 **true**。"""
        raw = self._setting(DRY_RUN_KEY)
        if raw is None:
            return True
        return self._truthy(raw, True)

    # ==================================================================
    # W10：单条素材 / 单条计划读取（带归属校验）
    # ==================================================================

    def get_material(self, material_id: Any, user_id: Optional[int] = None) -> Dict[str, Any]:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            if user_id is None:
                cursor.execute("SELECT * FROM product_materials WHERE id = ?", (material_id,))
            else:
                cursor.execute(
                    "SELECT * FROM product_materials WHERE id = ? AND user_id = ?",
                    (material_id, user_id),
                )
            row = cursor.fetchone()
            if row is None:
                raise LookupError("素材不存在")
            material = _as_dict(cursor, row)
        material["images"] = _json_list(material.get("images"))
        return material

    def get_delete_rule(self, rule_id: Any, user_id: Optional[int] = None) -> Dict[str, Any]:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            if user_id is None:
                cursor.execute("SELECT * FROM product_delete_rules WHERE id = ?", (rule_id,))
            else:
                cursor.execute(
                    "SELECT * FROM product_delete_rules WHERE id = ? AND user_id = ?",
                    (rule_id, user_id),
                )
            row = cursor.fetchone()
            if row is None:
                raise LookupError("删除计划不存在")
            rule = _as_dict(cursor, row)
        rule["enabled"] = bool(rule.get("enabled"))
        rule["auto_execute"] = bool(rule.get("auto_execute"))
        rule["skip_reply_activity"] = bool(rule.get("skip_reply_activity"))
        rule["skip_order_activity"] = bool(rule.get("skip_order_activity"))
        return rule

    # ==================================================================
    # W10：白名单开关（面板 auto-approve / auto-execute）
    # ==================================================================

    def set_material_auto_approve(
        self, material_id: Any, enabled: Any, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """把素材加入 / 移出自动发布白名单。默认 0（只能人工触发）。"""
        self.get_material(material_id, user_id)
        flag = 1 if enabled else 0
        with self.db.lock:
            cursor = self.db.conn.cursor()
            if flag:
                cursor.execute(
                    "UPDATE product_materials SET auto_approved = 1, "
                    "auto_approved_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ?",
                    (material_id,),
                )
            else:
                cursor.execute(
                    "UPDATE product_materials SET auto_approved = 0, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (material_id,),
                )
            self.db.conn.commit()
        logger.info(f"素材 {material_id} 自动发布白名单 -> {bool(flag)}")
        return self.get_material(material_id, user_id)

    def set_delete_rule_auto_execute(
        self, rule_id: Any, enabled: Any, user_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """把删除计划加入 / 移出自动执行白名单。默认 0（只能人工触发）。"""
        self.get_delete_rule(rule_id, user_id)
        flag = 1 if enabled else 0
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "UPDATE product_delete_rules SET auto_execute = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (flag, rule_id),
            )
            self.db.conn.commit()
        logger.info(f"删除计划 {rule_id} 自动执行白名单 -> {bool(flag)}")
        return self.get_delete_rule(rule_id, user_id)

    # ==================================================================
    # W10：发布通道（prepare -> 确认令牌 -> execute）
    # ==================================================================

    def prepare_publish(self, material_id: Any, user_id: Optional[int] = None) -> Dict[str, Any]:
        """发一条一次性确认令牌（TTL 300s），并把要发生的事写清楚给面板展示。"""
        material = self.get_material(material_id, user_id)
        self.require_account(material["user_id"], material["cookie_id"])
        status = str(material.get("publish_status") or "")
        if status != PUBLISH_STATUS_READY:
            raise ValueError(f"素材当前状态为 {status or '未知'}，只有 ready 才能发布")
        dry_run = self.publish_dry_run()
        token = self._tokens.issue(_material_resource(material_id), ACTION_PUBLISH)
        summary = (
            f"素材 #{material_id}「{material.get('title') or ''}」将发布到账号 {material['cookie_id']}；"
            f"价格 {material.get('price')}；dry_run={dry_run}；"
            f"令牌 {CONFIRM_TOKEN_TTL} 秒内有效、只能使用一次"
        )
        return {
            "confirm_token": token,
            "summary": summary,
            "material_id": material_id,
            "publish_status": status,
            "dry_run": dry_run,
        }

    def execute_publish(
        self,
        material_id: Any,
        *,
        confirm_token: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """真发布一条素材（状态机 ready -> publishing -> published / failed）。

        两条入口：
        - 人工：带 ``confirm_token``（``prepare_publish`` 发的，一次性）；
        - 白名单：``auto_approved = 1 AND publish_status = 'ready'``（定时循环走这条）。

        失败**不自动重试**：状态落 ``failed`` + 记 run + 推 QQ。
        ``publish_dry_run=true`` 时只组装 payload（状态回到 ready），run 里写明 dry-run。
        """
        material = self.get_material(material_id, user_id)
        owner_id = int(material["user_id"])
        cookie_id = material["cookie_id"]
        self.require_account(owner_id, cookie_id)
        status = str(material.get("publish_status") or "")
        whitelisted = int(material.get("auto_approved") or 0) == 1 and status == PUBLISH_STATUS_READY
        if confirm_token:
            self._tokens.consume(confirm_token, _material_resource(material_id), ACTION_PUBLISH)
        elif not whitelisted:
            raise PermissionError(
                "需要确认令牌：素材既不在自动发布白名单（auto_approved=1），当前状态也不是 ready"
            )
        if status != PUBLISH_STATUS_READY:
            raise ValueError(f"素材当前状态为 {status or '未知'}，只有 ready 才能发布")

        self._set_publish_status(material_id, PUBLISH_STATUS_PUBLISHING)
        outcome = self._publish_material(material)

        details: List[Dict[str, Any]] = [{
            "material_id": material_id,
            "source_item_id": material.get("source_item_id"),
            "cookie_id": cookie_id,
        }]
        mode = "local"
        if outcome["dry_run"]:
            self._set_publish_status(material_id, PUBLISH_STATUS_READY)
            changed = failed = 0
            mode = "dry_run"
            summary = (
                f"素材 #{material_id} dry-run：只组装 payload，未发真实发布请求"
                f"（状态保持 ready）"
            )
            details[0].update({"status": "dry_run", "reason": outcome["message"]})
        elif outcome["rate_limited"]:
            self._set_publish_status(material_id, PUBLISH_STATUS_READY)
            changed = failed = 0
            summary = (
                f"素材 #{material_id} 被写限流拒绝，未尝试发布（状态保持 ready，下轮再说）："
                f"{outcome['message']}"
            )
            details[0].update({"status": "deferred", "reason": outcome["message"]})
        elif outcome["ok"]:
            self._set_publish_status(
                material_id, PUBLISH_STATUS_PUBLISHED, published_item_id=outcome["item_id"]
            )
            changed, failed = 1, 0
            summary = f"素材 #{material_id} 发布成功 itemId={outcome['item_id']}"
            details[0].update({"status": "published", "item_id": outcome["item_id"]})
        else:
            self._set_publish_status(material_id, PUBLISH_STATUS_FAILED)
            changed, failed = 0, 1
            summary = f"素材 #{material_id} 发布失败：{outcome['message']}"
            details[0].update({"status": "failed", "reason": outcome["message"]})
            self._notify_failure(
                cookie_id,
                f"素材 #{material_id} 发布失败（不自动重试）",
                f"标题：{material.get('title') or ''}\n"
                f"账号：{cookie_id}\n"
                f"原因：{outcome['message']}\n"
                f"状态已置 failed，需要人工处理后把素材改回 ready。",
            )

        run_id = self._record_run(
            owner_id, cookie_id, "publish_execute", None, mode,
            1, 1, changed, failed, summary, details,
        )
        return {
            "run_id": run_id,
            "ok": bool(outcome["ok"]),
            "publish_status": self.get_material(material_id, user_id).get("publish_status"),
            "item_id": outcome["item_id"],
            "dry_run": outcome["dry_run"],
            "rate_limited": outcome["rate_limited"],
            "message": outcome["message"],
            "summary": summary,
        }

    def _publish_material(self, material: Dict[str, Any]) -> Dict[str, Any]:
        """调 W7 的 ``publish_item``；永不抛异常，失败原因一律落到 ``message``。"""
        cookie_id = material["cookie_id"]
        cookies_str = self.db.get_cookie(cookie_id)
        if not cookies_str:
            return {"ok": False, "item_id": "", "dry_run": False, "rate_limited": False,
                    "risk_control": False, "message": "账号 Cookie 不存在"}
        try:
            from utils.item_publish import publish_item
        except Exception as exc:
            return {"ok": False, "item_id": "", "dry_run": False, "rate_limited": False,
                    "risk_control": False, "message": f"item_publish 不可用: {exc}"}
        price = material.get("price")
        try:
            price = float(price) if price not in (None, "") else 0.0
        except (TypeError, ValueError):
            price = 0.0
        title = str(material.get("title") or "").strip() or f"商品 {material.get('source_item_id') or ''}"
        try:
            raw = publish_item(
                cookies_str,
                title=title,
                desc=str(material.get("description") or ""),
                images=self._resolve_images(material.get("images")),
                price=price,
                cookie_id=cookie_id,
            )
        except Exception as exc:
            logger.exception("商品发布异常")
            return {"ok": False, "item_id": "", "dry_run": False, "rate_limited": False,
                    "risk_control": False,
                    "message": f"发布异常: {type(exc).__name__}: {exc}"}
        self._writeback_cookies(cookie_id, cookies_str, raw.get("cookies_str"))
        item_id = str(raw.get("item_id") or "")
        return {
            "ok": bool(raw.get("ok")) and bool(item_id),
            "item_id": item_id,
            "dry_run": bool(raw.get("dry_run")),
            "rate_limited": bool(raw.get("rate_limited")),
            "risk_control": bool(raw.get("risk_control")),
            "message": str(raw.get("message") or ""),
        }

    @staticmethod
    def _resolve_images(images: Any) -> List[str]:
        """素材 ``images`` → ``publish_item`` 要的本地路径列表。

        - 本地路径原样透传；
        - ``http(s)://`` / ``//`` 开头的远端图（抓取时存的就是 CDN 链接）先下到临时文件
          —— ``utils/image_uploader`` 只吃本地路径（PIL 打不开 URL）；
        - 下载失败保留原值，让上传阶段报明确错误（不静默丢图）。
        """
        out: List[str] = []
        for item in _json_list(images):
            url = item
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http://") or url.startswith("https://"):
                local = _download_image(url)
                out.append(local or item)
            else:
                out.append(item)
        return out

    def _writeback_cookies(self, cookie_id: str, original: str, merged: Any) -> bool:
        """把 mtop 响应下发的新 ``_m_h5_tk`` 回写 cookies 表。

        CAS：库里的值仍等于本次调用前取到的 ``original`` 才写 —— 主程序的令牌刷新
        循环若已更新过，就跳过，不覆盖对方的更新。失败只记日志。
        """
        if not merged or not isinstance(merged, str) or merged == original:
            return False
        try:
            if self.db.get_cookie(cookie_id) != original:
                logger.info(f"账号 {cookie_id} Cookie 已被其它路径刷新，跳过回写")
                return False
            return bool(self.db.update_cookie_account_info(cookie_id, cookie_value=merged))
        except Exception as exc:
            logger.warning(f"账号 {cookie_id} Cookie 回写失败: {type(exc).__name__}: {exc}")
            return False

    def _set_publish_status(
        self,
        material_id: Any,
        status: str,
        *,
        published_item_id: Optional[str] = None,
    ) -> None:
        fields = ["publish_status = ?", "updated_at = CURRENT_TIMESTAMP"]
        params: List[Any] = [status]
        if published_item_id is not None:
            fields.append("published_item_id = ?")
            params.append(str(published_item_id))
        params.append(material_id)
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                f"UPDATE product_materials SET {', '.join(fields)} WHERE id = ?", params
            )
            self.db.conn.commit()

    # ==================================================================
    # W10：删除通道（prepare -> 确认令牌 -> execute）
    # ==================================================================

    def prepare_delete_execute(self, rule_id: Any, user_id: Optional[int] = None) -> Dict[str, Any]:
        """预演一遍并把候选写进 run，再发一次性确认令牌。"""
        rule = self.get_delete_rule(rule_id, user_id)
        self.require_account(rule["user_id"], rule["cookie_id"])
        preview = self.preview_delete_rule(rule["user_id"], rule_id)
        token = self._tokens.issue(_delete_rule_resource(rule_id), ACTION_DELETE_EXECUTE)
        summary = (
            f"计划 #{rule_id}「{rule.get('name') or ''}」待真删除 {len(preview['candidates'])} 件"
            f"（{preview['summary']}）；令牌 {CONFIRM_TOKEN_TTL} 秒内有效、只能使用一次"
        )
        return {
            "confirm_token": token,
            "candidates": preview["candidates"],
            "summary": summary,
            "preview_run_id": preview["run_id"],
            "rule_id": rule_id,
        }

    def execute_delete_rule(
        self,
        rule_id: Any,
        *,
        confirm_token: Optional[str] = None,
        user_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """把 ``preview_delete_rule`` 的候选接真删除（``com.taobao.idle.item.delete``）。

        两条入口：人工带 ``confirm_token``；白名单 ``auto_execute = 1 AND enabled = 1``。
        平台侧删除成功后本地记录同步删掉（复用二期 T6 墓碑机制，防同步写回）。
        """
        rule = self.get_delete_rule(rule_id, user_id)
        owner_id = int(rule["user_id"])
        cookie_id = rule["cookie_id"]
        self.require_account(owner_id, cookie_id)
        whitelisted = bool(rule["auto_execute"]) and bool(rule["enabled"])
        if confirm_token:
            self._tokens.consume(confirm_token, _delete_rule_resource(rule_id), ACTION_DELETE_EXECUTE)
        elif not whitelisted:
            raise PermissionError(
                "需要确认令牌：计划既未开启自动执行（auto_execute=1），也未启用"
            )
        if not rule["enabled"]:
            raise ValueError("删除计划未启用")

        preview = self.preview_delete_rule(owner_id, rule_id)
        candidates = preview["candidates"]
        skipped = preview["skipped"]
        cookies_str = self.db.get_cookie(cookie_id)
        if not cookies_str:
            raise ValueError("账号 Cookie 不存在，无法执行删除")
        try:
            from utils.item_delete import delete_item
        except Exception as exc:
            raise RuntimeError(f"item_delete 不可用: {exc}") from exc

        details: List[Dict[str, Any]] = []
        changed = failed = 0
        for item in candidates:
            item_id = str(item.get("item_id") or "")
            try:
                result = self._run_coro(delete_item(cookies_str, item_id, cookie_id=cookie_id))
            except Exception as exc:
                failed += 1
                details.append({"item_id": item_id, "status": "failed",
                                "reason": f"调用异常: {type(exc).__name__}: {exc}"})
                continue
            if result.get("rate_limited"):
                # 配额已尽：后面的必然同样被拒，不消耗账号
                details.append({"item_id": item_id, "status": "deferred",
                                "reason": f"写限流拒绝，未执行：{result.get('message')}"})
                break
            if result.get("ok"):
                removed = False
                try:
                    removed = bool(self.db.delete_item_info(cookie_id, item_id))
                except Exception as exc:
                    logger.warning(f"本地记录删除失败 {cookie_id}-{item_id}: {type(exc).__name__}")
                changed += 1
                details.append({
                    "item_id": item_id,
                    "status": "deleted",
                    "reason": "平台删除成功",
                    "semantics": result.get("semantics"),
                    "local_record_removed": removed,
                })
            else:
                failed += 1
                details.append({"item_id": item_id, "status": "failed",
                                "reason": result.get("message") or result.get("category") or "未知错误"})

        skip_reasons: Dict[str, int] = {}
        for item in skipped:
            reason = str(item.get("reason") or "未说明")
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
        details.append({"status": "skipped", "count": len(skipped), "reasons": skip_reasons})

        summary = (
            f"计划 #{rule_id} 真删除：候选 {len(candidates)} 件，删除成功 {changed} 件，"
            f"失败 {failed} 件，跳过 {len(skipped)} 件"
        )
        run_id = self._record_run(
            owner_id, cookie_id, "delete_execute", rule_id, "execute",
            len(candidates) + len(skipped), len(candidates), changed, failed, summary, details,
        )
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "UPDATE product_delete_rules SET last_run_at = CURRENT_TIMESTAMP, "
                "last_run_status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                ("success" if not failed else "partial", rule_id),
            )
            self.db.conn.commit()
        if failed:
            self._notify_failure(
                cookie_id,
                f"删除计划 #{rule_id} 执行有失败（不自动重试）",
                f"计划：{rule.get('name') or ''}\n账号：{cookie_id}\n"
                f"删除成功 {changed} 件，失败 {failed} 件，跳过 {len(skipped)} 件\n"
                f"详见 run #{run_id}。",
            )
        return {
            "run_id": run_id,
            "rule_id": rule_id,
            "candidates": len(candidates),
            "deleted_count": changed,
            "failed_count": failed,
            "skipped_count": len(skipped),
            "summary": summary,
        }

    # ==================================================================
    # W10：定时循环（只处理白名单条目）
    # ==================================================================

    def auto_worker_status(self) -> Dict[str, Any]:
        return {
            "worker_running": bool(self._worker and self._worker.is_alive()),
            "enabled": self.auto_enabled(),
            "interval": self.auto_interval(),
            "dry_run": self.publish_dry_run(),
            "worker_name": self._worker.name if self._worker else "",
        }

    def start_auto_worker(self) -> bool:
        """起后台定时循环线程（daemon）。

        ``PRODUCT_AUTO_WORKER=0`` 时不起（给测试用）。重复调用无副作用。
        """
        if os.environ.get("PRODUCT_AUTO_WORKER", "1").strip() == "0":
            logger.info("PRODUCT_AUTO_WORKER=0，商品自动化定时循环未启动")
            return False
        if self._worker and self._worker.is_alive():
            return False
        self._worker_stop.clear()
        self._worker = threading.Thread(
            target=self._auto_loop, name="product-auto-worker", daemon=True
        )
        self._worker.start()
        logger.info(
            "商品自动化定时循环已启动（总开关 system_settings.product_auto_enabled，默认关）"
        )
        return True

    def stop_auto_worker(self, timeout: float = 5.0) -> None:
        self._worker_stop.set()
        worker = self._worker
        if worker and worker.is_alive() and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        self._worker = None

    def _auto_loop(self) -> None:
        while not self._worker_stop.is_set():
            try:
                if not self.auto_enabled():
                    self._worker_stop.wait(WORKER_TICK_SECONDS)
                    continue
                for result in self.run_auto_cycle():
                    logger.info(f"商品自动化循环: {result.get('summary')}")
                self._worker_stop.wait(self.auto_interval())
            except Exception:
                logger.exception("商品自动化定时循环异常")
                self._worker_stop.wait(WORKER_TICK_SECONDS)

    def run_auto_cycle(self, user_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """跑一轮自动编排：白名单发布 + 白名单删除。

        总开关 ``product_auto_enabled`` 为假时直接返回 ``[]``（没有扫描动作，不落 run）。
        每条路径都落 ``automation_task_runs``（候选 N / 白名单命中 M / 成功 K / 跳过原因）。
        """
        if not self.auto_enabled():
            logger.info("商品自动化总开关 product_auto_enabled 为假，本轮不执行")
            return []
        users = [int(user_id)] if user_id is not None else self._automation_users()
        results = []
        for uid in users:
            publish = self._auto_publish_pass(uid)
            delete = self._auto_delete_pass(uid)
            results.append({
                "user_id": uid,
                "publish": publish,
                "delete": delete,
                "summary": f"user={uid}；发布：{publish['summary']}；删除：{delete['summary']}",
            })
        return results

    def _automation_users(self) -> List[int]:
        with self.db.lock:
            cursor = self.db.conn.cursor()
            cursor.execute(
                "SELECT DISTINCT user_id FROM product_materials "
                "UNION SELECT DISTINCT user_id FROM product_delete_rules"
            )
            return [int(row[0]) for row in cursor.fetchall() if row[0] is not None]

    def _auto_publish_pass(self, user_id: int) -> Dict[str, Any]:
        materials = self.list_materials(user_id)
        details: List[Dict[str, Any]] = []
        hits: List[Dict[str, Any]] = []
        for material in materials:
            status = str(material.get("publish_status") or "")
            if status != PUBLISH_STATUS_READY:
                details.append({
                    "material_id": material["id"], "status": "skipped",
                    "reason": f"状态 {status or '未知'} 非 ready",
                })
                continue
            if int(material.get("auto_approved") or 0) != 1:
                details.append({
                    "material_id": material["id"], "status": "skipped",
                    "reason": "未加入白名单（auto_approved=0）",
                })
                continue
            hits.append(material)

        changed = failed = 0
        for material in hits:
            try:
                outcome = self.execute_publish(material["id"], user_id=user_id)
            except Exception as exc:
                failed += 1
                details.append({"material_id": material["id"], "status": "failed",
                                "reason": f"{type(exc).__name__}: {exc}"})
                continue
            if outcome.get("publish_status") == PUBLISH_STATUS_PUBLISHED:
                changed += 1
            elif outcome.get("publish_status") == PUBLISH_STATUS_FAILED:
                failed += 1
            details.append({
                "material_id": material["id"],
                "status": outcome.get("publish_status"),
                "reason": outcome.get("summary"),
            })

        skipped = len(details) - len(hits)
        summary = (
            f"扫描 {len(materials)} 条素材，白名单命中 {len(hits)} 条，"
            f"发布成功 {changed} 条，失败 {failed} 条，跳过 {skipped} 条"
        )
        run_id = self._record_run(
            user_id, None, "auto_publish", None, "auto",
            len(materials), len(hits), changed, failed, summary, details,
        )
        return {"run_id": run_id, "summary": summary, "checked": len(materials),
                "matched": len(hits), "changed": changed, "failed": failed, "details": details}

    def _auto_delete_pass(self, user_id: int) -> Dict[str, Any]:
        rules = self.list_delete_rules(user_id)
        details: List[Dict[str, Any]] = []
        hits: List[Dict[str, Any]] = []
        for rule in rules:
            if not rule.get("enabled"):
                details.append({"rule_id": rule["id"], "status": "skipped",
                                "reason": "计划未启用（enabled=0）"})
                continue
            if not rule.get("auto_execute"):
                details.append({"rule_id": rule["id"], "status": "skipped",
                                "reason": "未加入白名单（auto_execute=0）"})
                continue
            hits.append(rule)

        changed = failed = 0
        for rule in hits:
            try:
                outcome = self.execute_delete_rule(rule["id"], user_id=user_id)
            except Exception as exc:
                failed += 1
                details.append({"rule_id": rule["id"], "status": "failed",
                                "reason": f"{type(exc).__name__}: {exc}"})
                continue
            changed += int(outcome.get("deleted_count") or 0)
            failed += int(outcome.get("failed_count") or 0)
            details.append({"rule_id": rule["id"], "status": "executed",
                            "reason": outcome.get("summary")})

        skipped = len(details) - len(hits)
        summary = (
            f"扫描 {len(rules)} 条删除计划，白名单命中 {len(hits)} 条，"
            f"真删除 {changed} 件，失败 {failed} 件，跳过 {skipped} 条"
        )
        run_id = self._record_run(
            user_id, None, "auto_delete", None, "auto",
            len(rules), len(hits), changed, failed, summary, details,
        )
        return {"run_id": run_id, "summary": summary, "checked": len(rules),
                "matched": len(hits), "changed": changed, "failed": failed, "details": details}

    # ==================================================================
    # W10：失败告警 + 协程桥
    # ==================================================================

    def _notify_failure(self, cookie_id: str, title: str, message: str) -> int:
        """失败告警：走该账号已启用的通知渠道（QQ / NapCat 等）。返回成功渠道数。

        商品自动化没有独立的事件类型（``app/notification_events.py`` 不属本卡），
        所以这里不按 ``event_types`` 过滤 —— 失败告警宁可多发一条，也不能漏。
        """
        try:
            rules = self.db.get_account_notifications(cookie_id) or []
        except Exception as exc:
            logger.warning(f"读取账号 {cookie_id} 通知渠道失败: {type(exc).__name__}: {exc}")
            return 0
        rules = [rule for rule in rules if rule.get("enabled", True)]
        if not rules:
            logger.warning(f"账号 {cookie_id} 未配置通知渠道，商品自动化失败告警未发送")
            return 0
        try:
            from app.services.notification_sender import send_notification
        except Exception as exc:
            logger.warning(f"notification_sender 不可用: {exc}")
            return 0

        text = f"【商品自动化】{title}\n{message}"

        async def _go() -> int:
            sent = 0
            for rule in rules:
                try:
                    await send_notification(
                        rule.get("channel_type"), rule.get("channel_config"), text
                    )
                    sent += 1
                except Exception as exc:
                    logger.warning(
                        f"通知渠道 {rule.get('channel_name')} 发送失败: "
                        f"{type(exc).__name__}: {str(exc)[:120]}"
                    )
            return sent

        try:
            sent = self._run_coro(_go())
        except Exception as exc:
            logger.warning(f"商品自动化失败告警发送异常: {type(exc).__name__}: {exc}")
            return 0
        logger.info(f"商品自动化失败告警已发送渠道数: {sent}")
        return int(sent or 0)

    @staticmethod
    def _run_coro(coro):
        """在同步上下文里跑协程；已在事件循环里时放到临时线程（避免 RuntimeError）。"""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
