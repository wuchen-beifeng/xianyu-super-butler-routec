#!/usr/bin/env python3
"""humanize.py -- 轨迹人性化 + 预运动路径生成（应用机侧）。

输出格式契约（与 driver.ps1 的 DoDrag 严格对应）：
    points = [[cum_dx, cum_dy, dt_ms], ...]
  * cum_dx/cum_dy 是"相对拖动起点的累计偏移"（浮点，driver 内部取整）；
  * dt_ms 是"本步之前的等待毫秒数"；dt=0 表示本步与上一步之间不额外等待
    （用于连续流：靠 sub 子事件提供采样节奏）。

⚠️ 血泪坑（实测）：driver.ps1 按 **累计偏移** 解释 points（$tx = $start + $p[0]）。
   若把"每步增量"误当累计送入，光标只会在起点 ±max(单步增量) 内绕圈，
   滑块永远到不了头 → 服务端只得 8778/300。
   本模块所有生成函数都遵守累计偏移约定（旧版 generate() 曾犯此错，已重写）。

轨迹特征（对齐"真实鼠标"的四个可观测点）：
  1. 速度曲线：smoothstep（起步/收尾慢、中段快），距离域表达；
  2. 微抖动：每步目标带 ±jitter 像素扰动 + 轻微垂直漂移；
  3. 过冲+回拉：末端先冲过终点 3~8px 再回正（真人常见）；
  4. 高频子事件：配合 driver(sub>1, sub_gap_us≈3000) 让 Chrome 合并出
     每帧 2~11 个 coalesced 子采样（见 capture/mousemove_cdp_vs_sendinput.json）。
"""
import math
import random


def _smoothstep(u: float) -> float:
    return u * u * (3 - 2 * u)


def drag_track(dx: float, dy: float = 0.0, steps: int = 38, seed=None,
               jitter: float = 1.2, ydrift: float = 2.2, overshoot: float = 5.0,
               pause_prob: float = 0.6, pause_ms=(25, 70)):
    """生成拖动轨迹（累计偏移格式）。

    pause_prob/pause_ms：以一定概率在轨迹中段插入一次"犹豫停顿"（dt>0），
    模仿真人拖动途中的轻微停顿——全程匀速无停顿反而更像脚本。
    """
    rnd = random.Random(seed)
    pts = []
    for i in range(1, steps + 1):
        u = i / steps
        p = _smoothstep(u)
        x = dx * p
        y = dy * p + ydrift * math.sin(math.pi * u) + rnd.uniform(-jitter, jitter)
        pts.append([round(x, 2), round(y, 2), 0])
    if pause_prob and len(pts) > 12 and rnd.random() < pause_prob:
        idx = rnd.randint(6, len(pts) - 8)
        pts[idx][2] = rnd.randint(*pause_ms)
    over = dx + overshoot * rnd.uniform(0.8, 1.3)
    pts.append([round(over, 2), round(pts[-1][1], 2), 0])   # 过冲
    pts.append([round(dx, 2), round(pts[-1][1], 2), 0])     # 回拉
    return pts


def bezier_path(a, b, n, wob=0.0, rnd=None):
    """两点间 ease 插值路径（屏幕坐标），可选正弦摆动+抖动。用于预运动/接近。"""
    (x0, y0), (x1, y1) = a, b
    out = []
    for i in range(1, n + 1):
        u = i / n
        e = _smoothstep(u)
        x = x0 + (x1 - x0) * e
        y = y0 + (y1 - y0) * e
        if wob and rnd:
            y += wob * math.sin(math.pi * u) + rnd.uniform(-1.2, 1.2)
        out.append((x, y))
    return out


def approach_path(cursor, frame_xy, handle_screen, rnd):
    """开工前的自然接近路径：先逛到 iframe 内中下部，再小弧线接近滑块。

    参数：cursor = 当前光标屏幕坐标；frame_xy = iframe 左上角屏幕坐标；
    handle_screen = 滑块手柄中心屏幕坐标。
    返回 [(x, y), ...]，调用方以 ~6ms 间隔逐点 driverctl.move。
    """
    sx, sy = handle_screen
    fx, fy = frame_xy
    start_pt = (fx + 180, fy + 280)
    w1 = (fx + 60, fy + 200)
    w2 = (fx + 120, fy + 250)
    near = (sx - 46, sy + 12)
    on = (sx - 10, sy + 2)
    p = bezier_path(cursor, start_pt, 12)
    p += bezier_path(start_pt, w1, 8, wob=4, rnd=rnd)
    p += bezier_path(w1, w2, 6)
    p += bezier_path(w2, near, 8, wob=2, rnd=rnd)
    p += bezier_path(near, on, 4)
    p += bezier_path(on, (sx, sy), 3)
    return p


if __name__ == "__main__":
    for s in range(3):
        p = drag_track(256, seed=s)
        print(s, "steps:", len(p), "last cum:", p[-1][0], "over:", max(q[0] for q in p))
