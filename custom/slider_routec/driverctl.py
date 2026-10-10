#!/usr/bin/env python3
"""driverctl.py -- 应用机侧：通过反向 SSH 隧道（127.0.0.1:8791）调用 VM101 的
Win32 SendInput 执行器。

只做传输，不做轨迹生成（轨迹见 humanize.py）。
注意：本机有透明 TLS 代理会破坏 keep-alive，因此一律裸 requests.post，禁用 Session。
"""
import json
import os

import requests

DRIVER_URL = os.environ.get("ROUTEC_DRIVER_URL", "http://127.0.0.1:8791/").rstrip("/") + "/"


def call(cmd: dict, timeout: float = 60.0) -> dict:
    r = requests.post(DRIVER_URL, data=json.dumps(cmd),
                      headers={"Content-Type": "application/json"}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def probe() -> dict:
    return call({"action": "probe"})


def windows() -> dict:
    return call({"action": "windows"})


def move(x: int, y: int) -> dict:
    return call({"action": "move", "x": x, "y": y})


def click(x: int, y: int, button: str = "left") -> dict:
    return call({"action": "click", "x": x, "y": y, "button": button})


def drag(start, points, sub: int = 1, sub_gap_us: int = 0,
         press_ms: int = 60, release_ms: int = 100, timeout: float = 120.0) -> dict:
    return call({
        "action": "drag",
        "start": [int(start[0]), int(start[1])],
        "points": [[int(p[0]), int(p[1]), int(p[2])] for p in points],
        "sub": int(sub),
        "sub_gap_us": int(sub_gap_us),
        "press_ms": int(press_ms),
        "release_ms": int(release_ms),
    }, timeout=timeout)


def shutdown() -> dict:
    return call({"action": "shutdown"})


# --- keyboard / text (W2) ---------------------------------------------------
# The text passed to type_text() is typed with SendInput on VM101 and is never
# logged, never echoed back and never returned in the response (only a count).

def type_text(text: str, timeout: float = 60.0) -> dict:
    return call({"action": "type", "text": text}, timeout=timeout)


def key_vk(vk: int, hold_ms: int = 0, timeout: float = 30.0) -> dict:
    return call({"action": "key", "vk": int(vk), "hold_ms": int(hold_ms)}, timeout=timeout)


def tap_vk(vk: int) -> dict:
    return key_vk(vk)


def key_down(vk: int) -> dict:
    return call({"action": "keydown", "vk": int(vk)})


def key_up(vk: int) -> dict:
    return call({"action": "keyup", "vk": int(vk)})


# Common virtual-key codes (the driver takes numbers only, no name mapping).
VK_TAB = 9
VK_ENTER = 13
VK_ESC = 27
VK_BACKSPACE = 8
VK_DELETE = 46
VK_SPACE = 32
VK_CTRL = 17
VK_SHIFT = 16
VK_ALT = 18
VK_LEFT = 37
VK_UP = 38
VK_RIGHT = 39
VK_DOWN = 40
VK_HOME = 36
VK_END = 35
VK_PAGEUP = 33
VK_PAGEDOWN = 34
VK_F5 = 116
VK_A = 65


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "windows":
        print(json.dumps(windows(), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(probe(), ensure_ascii=False, indent=2))
