"""验证码识别（ddddocr 识别服务客户端）。

自建 ddddocr 服务把「图像识别」这一类能力外置到一台常驻主机上，
本模块是它的唯一调用入口。之所以外置而不是内嵌：识别模型体积大、
依赖 OpenCV/ONNX，塞进本项目镜像会让镜像膨胀数百 MB 并拖慢构建，
而识别请求是低频、可失败的旁路能力 —— 服务化后主程序只依赖一个 HTTP 接口。

**默认关闭**（`global_config.yml` 的 `CAPTCHA_RECOGNITION.enabled`，默认 false）。
关闭时所有方法立刻返回 None，不发起任何网络请求。

适用边界（实测结论，勿想当然）：
    闲鱼 punish 用的 nc 滑块是「拖到底」型，**没有缺口需要定位** ——
    `recognize_slide_gap` 对它无用。本模块面向的是**图像型验证码**
    （拼图型滑块 / 点选 / 文字 / 算术），当前流程中尚未出现，
    作为能力储备与「转人工」兜底的替代路径。

设计约束：
    1. 任何失败都不得影响主流程 —— 全部方法吞异常并返回 None，只记日志。
    2. 连接失败要有短退避，避免识别服务挂掉时被高频重试打爆。
    3. 不打印图像内容与识别结果全文（可能含账号相关信息）。
"""
from __future__ import annotations

import asyncio
import base64
import time
from typing import Any, Dict, List, Optional, Union

import aiohttp
from loguru import logger

# 导入配置：与项目其它模块一致，拿不到配置时退回安全默认（关闭）
try:
    from app.config import CAPTCHA_RECOGNITION
except ImportError:  # 脱离主程序单独调用时
    CAPTCHA_RECOGNITION = {}

_DEFAULTS = {
    "enabled": False,
    "base_url": "http://<NAS_IP>:7777",
    "timeout": 30,
    "failure_cooldown": 60,      # 连续失败后的静默期（秒）
    "use_for_puzzle_slider": True,
    "use_for_click_select": True,
    "use_for_text_captcha": True,
    "use_for_calculate": True,
}

# 图像入参类型：http(s) URL / base64 字符串 / 原始字节
ImageInput = Union[str, bytes]


class CaptchaRecognition:
    """ddddocr 识别服务客户端。

    识别服务接口（全部 POST + JSON，图像支持 URL / base64 / bytes）::

        POST /capcode          {"slidingImage","backImage","simpleTarget"} -> {"result": <x>}
        POST /slideComparison  {"slidingImage","backImage"}                -> {"result": <x>}
        POST /classification   {"image"}                                   -> {"result": "<文本>"}
        POST /detection        {"image"}                                   -> {"result": [[x1,y1,x2,y2],...]}
        POST /calculate        {"image"}                                   -> {"result": <算术值>}
        POST /select           {"image"}                                   -> [{"<文本>": [x1,y1,x2,y2]}, ...]
        GET  /                                                             -> "API运行成功！"
    """

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        cfg = dict(_DEFAULTS)
        cfg.update(config or CAPTCHA_RECOGNITION or {})
        self.cfg = cfg

        self.enabled: bool = bool(cfg.get("enabled", False))
        self.base_url: str = str(cfg.get("base_url", "")).rstrip("/")
        self.timeout: int = int(cfg.get("timeout", 30))
        self.failure_cooldown: int = int(cfg.get("failure_cooldown", 60))

        # 连续失败后的静默截止时间戳；到点前不再发请求
        self._blocked_until: float = 0.0

        if self.enabled and not self.base_url:
            logger.warning("验证码识别已启用但未配置 base_url，将自动降级为关闭")
            self.enabled = False

    # ------------------------------------------------------------------ 内部

    def _available(self, capability: str) -> bool:
        """总开关 + 单项开关 + 静默期三重判定。"""
        if not self.enabled:
            return False
        if not self.cfg.get(f"use_for_{capability}", True):
            return False
        if time.monotonic() < self._blocked_until:
            return False
        return True

    @staticmethod
    def _normalize_image(value: ImageInput) -> Optional[str]:
        """统一成服务端能吃的形式：URL 原样传，bytes 转 base64。"""
        if isinstance(value, bytes):
            return base64.b64encode(value).decode("ascii")
        if isinstance(value, str) and value:
            return value
        return None

    async def _post(self, path: str, payload: Dict[str, Any]) -> Optional[Any]:
        """发一次识别请求。失败返回 None，绝不向上抛。"""
        if not self.base_url:
            return None
        url = f"{self.base_url}{path}"
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    body = await resp.json(content_type=None)
                    if resp.status != 200:
                        err = (body or {}).get("error") if isinstance(body, dict) else body
                        logger.warning(f"验证码识别返回 {resp.status}: {str(err)[:120]}")
                        self._on_failure()
                        return None
                    self._blocked_until = 0.0          # 成功即解除静默
                    if isinstance(body, dict):
                        return body.get("result")
                    return body
        except asyncio.TimeoutError:
            logger.warning(f"验证码识别超时（{self.timeout}s）: {path}")
        except aiohttp.ClientError as e:
            logger.warning(f"验证码识别连接失败: {type(e).__name__}")
        except Exception as e:
            logger.warning(f"验证码识别异常: {type(e).__name__}: {str(e)[:120]}")
        self._on_failure()
        return None

    def _on_failure(self) -> None:
        """连续失败后进入静默期，避免识别服务异常时被反复拖慢主流程。"""
        if self.failure_cooldown > 0:
            self._blocked_until = time.monotonic() + self.failure_cooldown
            logger.warning(
                f"验证码识别进入 {self.failure_cooldown}s 静默期（期间不再发起识别请求）"
            )

    # ------------------------------------------------------------ 公开接口

    async def health(self) -> bool:
        """探测识别服务是否在线（不受 enabled 开关限制，供设置页自检用）。"""
        if not self.base_url:
            return False
        try:
            timeout = aiohttp.ClientTimeout(total=min(self.timeout, 10))
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{self.base_url}/") as resp:
                    return resp.status == 200
        except Exception:
            return False

    async def recognize_slide_gap(
        self, sliding_image: ImageInput, back_image: ImageInput,
        simple_target: bool = True,
    ) -> Optional[int]:
        """滑块缺口定位 —— 背景图里缺口的横坐标。

        **仅适用于拼图型滑块。** 闲鱼 punish 的 nc 滑块是「拖到底」型，
        没有缺口，调用本方法无意义（详见模块 docstring 的适用边界）。
        """
        if not self._available("puzzle_slider"):
            return None
        s, b = self._normalize_image(sliding_image), self._normalize_image(back_image)
        if not s or not b:
            return None
        result = await self._post("/capcode", {
            "slidingImage": s, "backImage": b, "simpleTarget": simple_target,
        })
        try:
            return int(result) if result is not None else None
        except (TypeError, ValueError):
            logger.warning(f"缺口定位返回非数值: {str(result)[:60]}")
            return None

    async def compare_slide(
        self, sliding_image: ImageInput, back_image: ImageInput,
    ) -> Optional[int]:
        """滑块对比法定位（与缺口法互补，用于缺口法失败时兜底）。"""
        if not self._available("puzzle_slider"):
            return None
        s, b = self._normalize_image(sliding_image), self._normalize_image(back_image)
        if not s or not b:
            return None
        result = await self._post("/slideComparison", {"slidingImage": s, "backImage": b})
        try:
            return int(result) if result is not None else None
        except (TypeError, ValueError):
            return None

    async def classify(self, image: ImageInput) -> Optional[str]:
        """OCR —— 识别图中的文字/数字（文字型验证码）。"""
        if not self._available("text_captcha"):
            return None
        img = self._normalize_image(image)
        if not img:
            return None
        result = await self._post("/classification", {"image": img})
        return str(result) if result is not None else None

    async def calculate(self, image: ImageInput) -> Optional[str]:
        """算术验证码 —— 识别并直接算出结果（如「1+2=」→「3」）。"""
        if not self._available("calculate"):
            return None
        img = self._normalize_image(image)
        if not img:
            return None
        result = await self._post("/calculate", {"image": img})
        return str(result) if result is not None else None

    async def detect(self, image: ImageInput) -> Optional[List[List[int]]]:
        """目标检测 —— 返回图中所有文字/图标的包围盒 [[x1,y1,x2,y2], ...]。"""
        if not self._available("click_select"):
            return None
        img = self._normalize_image(image)
        if not img:
            return None
        result = await self._post("/detection", {"image": img})
        return result if isinstance(result, list) else None

    async def select(self, image: ImageInput) -> Optional[List[Dict[str, Any]]]:
        """点选验证码 —— 返回 [{文字: [x1,y1,x2,y2]}, ...]，按顺序点击即可。"""
        if not self._available("click_select"):
            return None
        img = self._normalize_image(image)
        if not img:
            return None
        result = await self._post("/select", {"image": img})
        return result if isinstance(result, list) else None


# 全局单例：识别服务是无状态旁路，整个进程共用一个客户端即可
captcha_recognition = CaptchaRecognition()
