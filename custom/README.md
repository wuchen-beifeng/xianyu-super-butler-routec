# custom/ — 叠加构建目录（闲鱼超级管家）

这里放的是**对官方预构建镜像的增量修改**。用它构建出自有镜像，不改上游源码、不重跑编译。

## 为什么要叠加构建

上游 Dockerfile 要跑 `npm ci + vite build + pip 编译 + 下载 Chromium`，
在 VM102（3.8G 内存）上会 CPU 打满、内存耗尽 —— 上游 `docs/deployment.md` 自己也警告
「NAS / 低配设备不要本地构建」。

叠加构建只加一层薄 `COPY`，**复用 `ghcr.io/23star/xianyu-super-butler:latest` 的全部重层**：

| 方式 | 耗时 | 内存峰值 | 风险 |
|---|---|---|---|
| 全量源码构建 | ~10 分钟 | 需 ≥4G（本机不够） | pnpm/playwright 下载易失败 |
| **叠加构建（本目录）** | **~33 秒** | 极低 | 无（不编译、不下载） |

## 文件清单

```
custom/
├── Dockerfile.overlay                 # 叠加构建定义（multi-stage：node 构建前端 + 叠加层）
├── frontend/                          # 前端源码（T7/v2 起入库；AccountList 已去掉「人工验证」投屏入口）
├── utils/captcha_recognition.py       # 新增：验证码识别客户端
├── utils/slider_route_c.py            # 新增(T7)：路线 C 后端客户端 + 熔断 + 一键停用
├── utils/item_search.py               # 修改(T7/v2)：去掉 stealth 回退 + 刮刮乐人工链路
├── app/config.py                      # 修改：CAPTCHA_RECOGNITION + SLIDER_ROUTE_C 配置
├── app/db_manager.py                  # 修改(T6)：商品墓碑
├── app/reply_server.py                # 修改(T6/T7)：去掉人工投屏路由与账号密码登录接口
├── global_config.yml                  # 修改：CAPTCHA_RECOGNITION + SLIDER_ROUTE_C 段
├── XianyuAutoAsync.py                 # 修改(T7/v2)：滑块验证只走路线 C，失败不回退
├── slider_routec/                     # 新增(T7)：容器内求解器（127.0.0.1:8799）
├── entrypoint.sh                      # 新增(T7)：Xvfb + 后台拉起求解器
├── vm101/                             # VM101 侧执行器（driver.ps1 + 部署说明，便于他人复现）
└── README.md                          # 本文件
```

## 构建

```bash
cd /root/docker/xianyu-butler/custom
docker build -f Dockerfile.overlay -t xianyu-super-butler:custom .
```

构建期会自检：`py_compile` 关键 .py + `yaml.safe_load` 校验配置 +
**grep 断言**（被删的三个模块与三个引用点必须彻底消失，否则构建失败）。
**坏代码进不了镜像。**

## 启用（部署）

改 `/root/docker/xianyu-butler/compose.yaml` 的 image 一行：

```diff
-    image: ghcr.io/23star/xianyu-super-butler:latest
+    image: xianyu-super-butler:custom
```

然后：

```bash
cd /root/docker/xianyu-butler
docker compose up -d
```

**回退到官方镜像**：把那一行改回去 + `docker compose up -d` 即可。官方镜像一直在本地，秒回。

## 配置开关

`global_config.yml` 里的 `CAPTCHA_RECOGNITION`：

```yaml
CAPTCHA_RECOGNITION:
  enabled: false                         # 总开关，默认关
  base_url: "http://<NAS_IP>:7777"   # NAS 上的 ddddocr 服务
  timeout: 30
  failure_cooldown: 60
  use_for_puzzle_slider: true
  use_for_click_select: true
  use_for_text_captcha: true
  use_for_calculate: true
```

**改这个文件后需要重建镜像**（它被 baked 进镜像，不是 bind-mount）。
如果只想调开关不想重建，可以改成 bind-mount：

```yaml
volumes:
  - ./custom/global_config.yml:/app/global_config.yml:ro   # 加这一行
```

## 验证记录（2026-10-07）

```
构建        33 秒，产物 xianyu-super-butler:custom，体积与官方一致（2.62 GB，只多一层薄 COPY）
构建期自检  py_compile ✓  yaml.safe_load ✓  →  "overlay: syntax + yaml OK"
镜像内验证  模块可导入 ✓  enabled=False ✓  单项开关可读 ✓
            关闭时 _available() 全 False（零请求）✓  health()=True ✓
            → ALL_CHECKS_PASSED
```

## T8 修复（2026-10-08）：滑块浏览器链路（patchright Chromium + 槽位防泄漏）

> ⚠️ **历史记录**：本节针对 `utils/xianyu_slider_stealth.py` 等文件的修改，随
> **T7/v2（2026-10-09）删除容器内浏览器滑块方案**已一并下线 —— 那些文件不再进入镜像。
> 下方「运行期自检」命令已失效。保留本节是为了留痕（同一批浏览器运行时仍在用，见 Dockerfile 注释）。

**背景**：上游 `latest` 镜像（`657deab…`，2026-09-30 构建）只装了 playwright 1.60 的
Chromium（`chromium-1223`），而 patchright 1.63 期望 `chromium-1243`。滑块验证
（有头 patchright）每次都在 `init_browser` 报
`Executable doesn't exist at /ms-playwright/chromium-1243/chrome-linux64/chrome`，
**浏览器从未真正启动**，账号连续 64 次自动验证失败、风控冷却循环 22 小时。
另有一次初始化失败后 `playwright.stop()` 卡死，把全局浏览器槽位与实例注册
泄漏 16 小时+，此后所有滑块/扫码任务全部排队超时。

**修复内容**（本目录 → 叠加构建）：

| # | 修复 | 位置 |
|---|---|---|
| 1 | 补装 patchright 的 Chromium（Chrome for Testing 153.0.8010.12） | `Dockerfile.overlay`：`patchright install chromium --no-shell --no-remove` |
| 2 | `find_chromium_executable` 兼容新版目录布局 `chrome-linux64/chrome` | ~~`utils/xianyu_slider_stealth.py`~~（T7/v2 已删） |
| 3 | 槽位归还前置 + `playwright.stop()` 15 秒超时守护（防卡死泄漏） | ~~`utils/xianyu_slider_stealth.py`~~（T7/v2 已删） |
| 4 | 落后自动降采样（保设计时间轴）；丢弃数进日志 | ~~`utils/xianyu_slider_stealth.py`~~（T7/v2 已删） |
| 5 | **装系统正式版 Google Chrome** —— 上游实测：Chromium-for-Testing 指纹会被 nc 识破，真人拖也判失败；正版 Chrome + 持久化目录才能过。**T7/v2 后不再服务滑块**，但 `utils/refresh_util.py` 仍在探测它，保留 | `Dockerfile.overlay`：google-chrome-stable deb |

**构建与启用**：

```bash
cd /root/docker/xianyu-butler/custom
docker build -f Dockerfile.overlay -t xianyu-super-butler:custom .
cd /root/docker/xianyu-butler
# compose.yaml 的 image 改为 xianyu-super-butler:custom
docker compose up -d
```

**运行期自检**（T7/v2 后已失效 —— 文件已不在镜像里；等价的断言在 Dockerfile 的 grep 自检中）：

```bash
# T7/v2 起：构建期已断言下列命令「无输出」
docker exec xianyu-super-butler sh -c \
  'grep -rnE "xianyu_slider_stealth|slider_patch|XianyuSliderStealth|manual_captcha|captcha_remote_control|api_captcha_remote" /app --include=*.py'
```

真实滑块尝试记录见工作台 `cases/goofish-slider-x5sec/work/t8/`。

**注意**：

- 建议保留 `--no-remove`：实测本镜像加不加该参数都不会清掉 chromium-1223
  （安装后 `.links` 同时注册 playwright 与 patchright 两个包，两者浏览器都算在用），
  保留它是为了防安装顺序变化时的边缘情况。

## 注意

- **闲鱼 punish 的 nc 滑块是「拖到底」型，没有缺口** ——
  `recognize_slide_gap` 对它无用。本模块面向拼图型滑块 / 点选 / 文字 / 算术等图像型验证码，
  当前流程中尚未出现，属能力储备。
  详细证据见 `cases/goofish-slider-x5sec/notes/ddddocr识别API-资料与适用性评估.md`（reverse_agent 工作台）。
- 本目录**不影响**上游源码。需要同步上游更新时：`docker pull` 官方镜像 → 重新叠加构建。
- 别把凭据/数据放进本目录 —— 它只装代码和配置。

---

## T7 集成（2026-10-08，v2 修订 2026-10-09）：滑块走「路线 C」（VM101 真机 SendInput）

### 为什么

容器内浏览器滑块在本栈实测**通过率 0** —— 阿里 nc 不接受自动化输入（CDP 派发的鼠标事件
「每帧一个」，真机鼠标是「一帧多个子采样」）。该方案已于 **T7/v2 整体删除**。
唯一实测可用的是**路线 C**：VM101 的 Chrome 渲染 + 真机 `user32!SendInput` 拖动，
实测完整闭环 **17/22 = 77.3%**（T6 run55 batch2 6 轮 5 通过）。

### 架构

```
VM102 容器 xianyu-super-butler                VM100 (debian13)            VM101 (win10-ltsc)
  _handle_captcha_verification                  routec-fwd:8791/9222        driver.ps1 (session1)
    └─ utils/slider_route_c.py ──HTTP──►  容器内 routec-solver :8799 ──►   Chrome CDP:9222
         （失败**不回退**，直接记失败+通知）   routec_core/driverctl/humanize   反向 SSH 隧道
                                                                          127.0.0.1:8791 / 9222
```

- 求解器代码：本目录 `slider_routec/`（`solver.py` / `routec_core.py` / `driverctl.py` /
  `humanize.py` / `xianyu_api.py`），由 `entrypoint.sh` 后台拉起，监听容器内 `127.0.0.1:8799`。
- 服务端**只做浏览器编排**，不感知管家；每轮做整账号 cookie 交换（多账号可用）。
- 请求头鉴权 `X-RouteC-Token`，token 在 `slider_routec/token`（600）与
  VM102 `/root/docker/xianyu-butler/.env` 的 `SLIDER_ROUTE_C_TOKEN`。

### 开关（三档，从软到硬）

| 目的 | 做法 | 生效 |
|---|---|---|
| 临时停用路线 C | `docker exec xianyu-super-butler touch /app/data/SLIDER_ROUTE_C_DISABLED` | 立即（每次调用都查该文件） |
| 配置停用 | `global_config.yml` 的 `SLIDER_ROUTE_C.enabled: false` | 重建镜像后 |
| 环境变量停用 | `.env` 加 `SLIDER_ROUTE_C_ENABLED=false` | `docker compose up -d` 后 |

熔断：连续失败 `failure_threshold`（默认 3）次 → 停用 `cooldown`（默认 600）秒，
期间不再请求后端，**滑块直接判定失败**（v2 已无回退路径）；冷却后自动半开重试。

### 日志怎么读

```
【账号】滑块验证：路线C 通过 {"path": "route_c", "reason": "pass", "elapsed_s": 37.7, ...}
【账号】滑块验证：路线C 未通过（无回退路径） {"reason": "slide_reject", "slide_code": 300, ...}
【账号】滑块验证失败（路线 C 是唯一路径，无回退）
```

`logs/captcha_verification.txt` 里对应 `滑块验证成功(路线C)` / `滑块验证失败`。

### 验证记录

见 `/root/docker/xianyu-butler/T7-集成说明.md`（含真实触发记录与回退验证）。

---

## T7/v2（2026-10-09）：删除上游浏览器滑块 + 人工投屏，只留扫码登录

### 删了什么

| 类别 | 文件 / 位置 | 处置 |
|---|---|---|
| 容器内浏览器滑块 | `utils/xianyu_slider_stealth.py`（上游 4553 行） | 从镜像 `rm`，不再 COPY |
| 同上 | `utils/slider_patch.py`（上游 2225 行） | 从镜像 `rm` |
| 同上（引用点） | `XianyuAutoAsync._handle_captcha_verification` 的 stealth 回退分支 | 删除，只走路线 C |
| 同上（引用点） | `XianyuAutoAsync._try_password_login_refresh`（密码登录刷新） | 下线为存根（返回 False） |
| 同上（引用点） | `utils/item_search.py` 的 stealth 回退 + 刮刮乐自动/人工链路 | 删除，改为如实返回 False |
| 人工投屏 | `utils/manual_captcha.py` / `utils/captcha_remote_control.py` / `app/api_captcha_remote.py` | 从镜像 `rm` |
| 人工投屏（路由） | `app/reply_server.py` 的 `POST /api/captcha/manual-session` + `include_router` | 删除 |
| 人工投屏（前端） | `frontend/components/AccountList.tsx` 的「人工验证」按钮 + 投屏弹窗 + WebSocket；`services/api.ts` 的 `startManualCaptchaSession` | 删除，前端重新构建 |
| 账号密码登录 | `app/reply_server.py` 的 `_execute_password_login` / `POST /password-login` / `GET /password-login/check/{sid}` | 删除（v2 只保留扫码登录） |
| 诊断脚本 | `app/scripts/{diag_slider,get_punish_url,train_slider}.py` | 从镜像 `rm`（只服务被删链路） |

### 保留了什么（**不要**一起删）

- **扫码登录**：`utils/qr_login.py`（删了账号没法重新登录）、`browser_pool` / `browser_limit`
- `utils/captcha_recognition.py`（独立开关的外部图像识别服务）
- patchright chromium-1243 + 系统 Google Chrome + mesa GL/CJK 字体
  （不再服务滑块，但扫码/浏览器刷新链路可能用；`utils/refresh_util.py` 会探测 `/usr/bin/google-chrome`）
- 前端「**在我的浏览器打开验证页**」入口（`POST /api/risk-control/{cid}/fresh-captcha-url`）——
  它只是把新鲜 punish 链接交给用户自己的浏览器，不依赖被删模块
- 事件 `captcha_manual`（滑块失败仍走它推 QQ 通知，T5 负责）

### 前端为什么要重建

「人工验证」按钮在被删的 `AccountList.tsx` 里，而 `/app/static` 是预构建产物 ——
删源码不重建，按钮会留着且点击必然 404。所以 `Dockerfile.overlay` 改成 **multi-stage**：
`node:20-alpine` 跑 `npm ci + vite build`（`package-lock.json` 已入库，产物可复现），
再把 `/frontend/dist/` COPY 进 `/app/static/`。构建约 1 分钟，仍不碰 python/chromium 重层。

### 验证口径

```bash
# 1) 镜像内无残留引用（构建期已断言）
docker exec xianyu-super-butler sh -c \
  'grep -rnE "xianyu_slider_stealth|slider_patch|XianyuSliderStealth|manual_captcha|captcha_remote_control|api_captcha_remote" /app --include=*.py'
# 2) 启动无 ImportError
docker logs xianyu-super-butler | grep -iE "traceback|importerror|modulenotfound"
# 3) 面板账号页：无「人工验证」按钮，有「扫码添加账号」
```

