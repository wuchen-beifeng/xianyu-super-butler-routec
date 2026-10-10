# xianyu-super-butler-routec

基于上游 [`23Star/xianyu-super-butler`](https://github.com/23Star/xianyu-super-butler) 的**二次修改版**：
把「闲鱼滑块验证」改成走**外部真机**（一台 Windows 虚拟机）的 `user32!SendInput` 拖动，
并补上「商品在本界面隐藏 / 恢复」与「QQ 通知渠道」。

> 上游本项目自带「容器内浏览器自动拖动滑块」方案，在本机实测通过率 **0**
> （阿里 nc 不接受 CDP 派发的合成鼠标事件）。本仓库保留上游的扫码登录、发货、AI 回复等全部能力，
> 只替换滑块这一环。

- 预构建镜像（公开）：`docker pull wuchen986/xianyu-super-butler:latest` —— 免本地构建（见 §3.1 方式 A）
- 许可证：**AGPL-3.0**（与上游一致，见 [`LICENSE`](LICENSE)）
- 上游基线提交：`697bdb4f750311d20e3fe46d16666d464eda4903`（2026-10-08）

---

## 1. 这是什么

一句话：**闲鱼超级管家 + 路线 C 滑块 + 隐藏商品 + QQ 通知**。

三台机器分工（下文用占位符，请替换成你自己的地址）：

| 角色 | 占位符 | 干什么 |
| --- | --- | --- |
| 应用机 | `<VM102_IP>` | 跑容器（面板 + 后端 + 容器内求解器 `routec-solver`） |
| 转发机 | `<VM100_IP>` | 把 `<VM102_IP>` 的两个端口转发到 Windows 真机（或直接同机） |
| 真机 | `<VM101_IP>` | Windows，跑 Chrome + 一个 HTTP 执行器（`driver.ps1`），真正做拖动 |
| 可选 | `<NAS_IP>` | 通知服务（NapCat）、可选的 ddddocr 图像识别服务 |

数据流（滑块）：

```
闲鱼触发滑块
  └─ 容器 XianyuAutoAsync._handle_captcha_verification()
       └─ utils/slider_route_c.py  ──HTTP(127.0.0.1:8799)──▶  容器内 slider_routec/solver.py
                                                                 ├─ preflight 六项体检（失败即在发起请求前中止）
                                                                 ├─ 注入账号 cookie，逼出新挑战
                                                                 └─ driverctl ──HTTP──▶ <VM100_IP>:8791 driver.ps1
                                                                                        └─ SendInput 拖动真机 Chrome
                                                                                            （CDP 在 <VM100_IP>:9222）
```

滑块**只有这一条路**：失败不回退容器内浏览器，重试 `max_attempts` 轮后仍失败 → 记失败 + 推通知。

---

## 2. 相对上游改了什么

完整逐文件清单见 [`docs/04-变更清单.md`](docs/04-变更清单.md)。摘要：

| 方向 | 内容 |
| --- | --- |
| **滑块（路线 C）** | 新增容器内求解器 `slider_routec/`、客户端 `utils/slider_route_c.py`、六项前置体检 `preflight.py`；`XianyuAutoAsync` 改为只走路线 C |
| **删除容器内浏览器滑块** | 移除上游 `xianyu_slider_stealth.py`（4553 行）、`slider_patch.py`（2225 行）及其回退分支 |
| **删除人工投屏** | 移除 `manual_captcha.py` / `captcha_remote_control.py` / `api_captcha_remote.py` 及路由、前端入口（扫码登录保留） |
| **商品隐藏 / 恢复** | 后端墓碑表补 `item_title/item_price/item_image` 快照；前端删除按钮语义澄清为「仅在本界面隐藏」+「已隐藏商品」页签 + 恢复按钮 |
| **重试策略** | 一次求解序列最多重试 5 轮（可配），熔断按「整个序列」计一次失败 |
| **QQ 通知** | 新增 `qq` 渠道类型（NapCat / OneBot v11），滑块序列耗尽走既有 `captcha_manual` 事件 |
| **构建** | 叠加构建（overlay）：复用上游预构建镜像的全部重层，只加薄薄一层 COPY；前端用 multi-stage `node:20-alpine` 现场构建 |
| **发布** | 叠加构建产物已推到 Docker Hub 公开仓库 `wuchen986/xianyu-super-butler`（发布镜像**不含** `slider_routec/token`，密钥一律运行时挂载） |
| **商品发布（三期 W7/W9）** | 新增 `utils/item_publish.py`（类目识别 + 账号默认地址 + 发布本体，L1 协议 mtop）；`app/product_automation.py` 加白名单通道 / run 记录 / 状态机 / 一次性确认令牌；前端素材行「发布」按钮 |
| **商品下架 / 删除（三期 W8/W9）** | 新增 `utils/item_delete.py`（`com.taobao.idle.item.delete` **v1.1**）；前端删除规则「执行」按钮。**实测语义 = 真删除，不可逆** |
| **账号密码自动登录（三期 W6）** | 新增 `utils/password_login.py`：免密刷新连续失败 N 次触发；CDP 导航 + VM101 真机 SendInput 输入账号 / 密码 / 提交，拿回 cookie 回写并重启监听任务 |
| **真机兜底 L3（三期 W9）** | 新增 `utils/real_machine_ops.py`：L1（协议）+ L2（路线 C 过滑块）都失败时，用 VM101 常驻 Chrome 把整条人工流程走完（默认关闭） |
| **写操作限流（三期 W4）** | 新增 `utils/write_guard.py`：写操作走**独立**令牌桶 + 每日上限（与读分桶）；`item_polish` 一并收口 |
| **图片上传补尺寸（三期 W5）** | `utils/image_uploader.py` 返回 `{url,width,height,size}`（发布 payload 的 `imageInfoDOList` 需要），此前只返回 url 字符串 |
| **二次确认（三期 W10/W11）** | `ConfirmTokenStore` 一次性令牌（TTL 300s，不落库）+ 6 条确认路由；**白名单是自动执行的唯一入口**，默认全关 |

**两处「删除」别混**：面板商品列表里的「删除」按钮语义仍是**仅在本界面隐藏**（写墓碑 `deleted_items`，
不碰闲鱼）；三期的**闲鱼侧删除**是另一条显式链路（`utils/item_delete.py` + 面板「删除规则 → 执行」），
**实测是真删除、不可逆**，两者互不影响。

---

## 3. 部署步骤

### 3.0 前置

- Docker + Docker Compose v2
- 一台 Windows（真机或虚拟机）用于滑块，能跑 Chrome；见 [`custom/vm101/README.md`](custom/vm101/README.md)
- 三台机器之间 TCP 可达：`<VM102_IP>` → `<VM100_IP>:8791` / `:9222` → `<VM101_IP>`

### 3.1 应用机（`<VM102_IP>`）

两种方式二选一：**方式 A** 直接用发布好的预构建镜像（推荐，不用构建），**方式 B** 从仓库叠加构建。

先拉代码与配置（两种方式都要）：

```bash
git clone <本仓库地址> xianyu-butler && cd xianyu-butler
cp docker-compose.example.yml docker-compose.yml
cp .env.example .env && chmod 600 .env
#    改掉 .env 里所有 <...> 占位符
```

#### 方式 A（推荐）：用发布好的预构建镜像

镜像已推到 Docker Hub **公开仓库**（`docker-compose.example.yml` 默认就指向它）：

```bash
docker pull wuchen986/xianyu-super-butler:latest
# 也可固定到日期版本，例如：wuchen986/xianyu-super-butler:20261010
```

> ⚠️ **发布镜像里不含 `slider_routec/token`**（公开镜像不放任何密钥）。求解器要读到 token 文件才开启鉴权，二选一：
>
> **① 挂载自己的 token（推荐）**
>
> ```bash
> mkdir -p custom/slider_routec
> printf '%s' "$(openssl rand -hex 24)" > custom/slider_routec/token
> chmod 600 custom/slider_routec/token
> # 把上面这个值原样填进 .env 的 SLIDER_ROUTE_C_TOKEN
> ```
>
> ```yaml
> volumes:
>   # …原有的 data / logs / backups / browser_data…
>   - ./custom/slider_routec/token:/app/slider_routec/token:ro
> ```
>
> **② 不鉴权**：`.env` 的 `SLIDER_ROUTE_C_TOKEN` 留空、也不挂 token 文件。
> 求解器只监听容器内 `127.0.0.1:8799`，不对宿主机暴露，所以可以接受；
> 启动日志会打印 `token=off`。

起服务：

```bash
docker compose up -d
docker compose ps                       # 等 healthy
curl -fsS http://localhost:8080/health
```

#### 方式 B：从仓库叠加构建

改过 `custom/` 下任何文件、或不想用发布镜像时用。

注意 `custom/global_config.yml`（构建期被 `COPY` 进镜像）与 `custom/slider_routec/token`
（运行期 `start.sh --token-file token`）**都被 `.gitignore` 忽略**，克隆出来没有这两个文件 ——
前者缺了**构建直接失败**，后者缺了**求解器起不来**。所以先造出来再构建：

```bash
# 1) 补两个被忽略的文件
#    1a. 容器配置：仓库里只有脱敏模板，复制成实际使用的 global_config.yml
cp custom/global_config.example.yml custom/global_config.yml
#    1b. 路线 C 令牌：容器内求解器与客户端用同一个，且必须与 .env 的 SLIDER_ROUTE_C_TOKEN 一致
TOKEN="$(head -c 32 /dev/urandom | base64)"
mkdir -p custom/slider_routec
printf '%s' "$TOKEN" > custom/slider_routec/token
chmod 600 custom/slider_routec/token
sed -i "s|^SLIDER_ROUTE_C_TOKEN=.*|SLIDER_ROUTE_C_TOKEN=$TOKEN|" .env

# 2) 叠加构建（秒级；不编译 python、不下载 Chromium）
cd custom && docker build -f Dockerfile.overlay -t xianyu-super-butler:custom .
cd ..

# 3) 把 docker-compose.yml 的 image 改成 xianyu-super-butler:custom，再起服务
docker compose up -d
docker compose ps                       # 等 healthy
curl -fsS http://localhost:8080/health
```

> `SLIDER_ROUTE_C_TOKEN`（`.env`，客户端用）与 `custom/slider_routec/token`（镜像内，求解器用）
> **必须一致**，否则客户端带 `X-RouteC-Token` 请求求解器会被 401 拒掉。
>
> ⚠️ 叠加构建的 `COPY slider_routec /app/slider_routec` 会把 `custom/slider_routec/token`
> **一起打进镜像**，所以本地构建出的 `xianyu-super-butler:custom` 含你的生产 token。
> **别把它推到公开仓库。** 想推公开镜像请用方式 A，或在 `custom/` 下放一份
> `.dockerignore`（内容一行 `slider_routec/token`）并改为挂载 token。

打开 `http://<VM102_IP>:8080`，用 `.env` 里的 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 登录
（注意：`ADMIN_PASSWORD` **只在首次建库时生效**，之后改请在面板里改）。

> **只想用上游原版？** 把 image 换成 `ghcr.io/23star/xianyu-super-butler:latest` ——
> 但那样**不含本仓库的任何改动**。

### 3.2 真机（`<VM101_IP>`，Windows）

按 [`custom/vm101/README.md`](custom/vm101/README.md) 做三件事：

1. 放好 `driver.ps1` 并注册计划任务 `WinInputDriver`（**必须以交互用户身份运行**，
   `SendInput` 从 session 0 发不到 session 1）；
2. 注册反向 SSH 隧道（把真机的 `8791` / `9222` 反连到 `<VM100_IP>`）；
3. 关掉锁屏 / 睡眠（`driver.ps1` 的 `unlock` 只是兜底，主防线是不锁屏）。

### 3.3 转发机（`<VM100_IP>`）

在 `<VM100_IP>` 上把 `127.0.0.1:8791` / `127.0.0.1:9222`（即反向隧道落点）
再转发给 `<VM102_IP>`。**只需要端口转发**，不限实现方式：

- 简单做法：`socat` 或一个小 Python 转发脚本 + systemd 服务；
- 注意两点坑：① 开机时网卡可能还没就绪 → 绑定要重试，服务要 `Restart=always`；
  ② 只放行 `<VM102_IP>` 来源，避免把真机输入接口暴露给全网段。

> 若 `<VM102_IP>` 与 Windows 真机在同一网段且你能直连，可省掉这一跳，
> 把 `ROUTEC_DRIVER_URL` / `ROUTEC_CDP_URL` 直接指向真机即可（见 3.4）。

### 3.4 指向你的地址（替换占位符）

仓库里的默认值全是占位符（`<VM100_IP>` 等），**必须替换**：

| 位置 | 变量 | 默认值 |
| --- | --- | --- |
| `custom/slider_routec/preflight.py` | `ROUTEC_DRIVER_URL` | `http://<VM100_IP>:8791/` |
| 同上 | `ROUTEC_CDP_URL` | `http://<VM100_IP>:9222` |
| `custom/global_config.example.yml` | `SLIDER_ROUTE_C.endpoint` | `http://127.0.0.1:8799`（容器内求解器，一般不用改） |
| 同上 | `CAPTCHA_RECOGNITION.base_url` | `http://<NAS_IP>:7777` |

改完 `custom/` 下任何文件都要重新叠加构建（`docker build -f Dockerfile.overlay ...`）再 `docker compose up -d`。
也可以不重建、改用环境变量覆盖：`ROUTEC_DRIVER_URL` / `ROUTEC_CDP_URL` 直接写进 `.env`。

### 3.5 验证链路通不通

```bash
# 容器内体检（六项，见 docs/01）
docker exec xianyu-super-butler python /app/slider_routec/preflight.py --timeout 60
# 或看求解器健康接口
docker exec xianyu-super-butler sh -c 'curl -s http://127.0.0.1:8799/health'
```

`ok:true` 且 `blocked_at:null` 才算通。哪一步挂了一目了然（`driver` / `lock` / `chrome` / `cdp` / `window`）。

---

## 4. 使用方法

### 4.1 隐藏 / 恢复商品

- **隐藏**：商品列表里点删除，弹窗文案已明确为「**仅在本界面隐藏**」（不会去闲鱼下架）。
  后端会写一条墓碑（`deleted_items` 表），之后**同步不会再把它写回来**。
- **恢复**：进入「**已隐藏商品**」页签 → 每行右侧「恢复」→ 墓碑删除，下次同步重新拉取。

对应接口：

```
GET    /items                                   # 在售商品
GET    /item-tombstones                         # 已隐藏列表 → {"success": true, "items": [...]}
DELETE /items/{cookie_id}/{item_id}             # 隐藏（写墓碑）
DELETE /item-tombstones/{cookie_id}/{item_id}   # 恢复（删墓碑）
```

> 注意 `GET /item-tombstones` 返回的是 `{"success":true,"items":[...]}`，**不是裸数组**。

墓碑默认保留 30 天（`item_tombstone_ttl_days`），到期由 `cleanup_item_tombstones()` 清理。

### 4.2 路线 C 开关与 kill-switch

从软到硬三档：

| 目的 | 做法 | 生效 |
| --- | --- | --- |
| **临时停用**（最快） | `docker exec xianyu-super-butler touch /app/data/SLIDER_ROUTE_C_DISABLED` | 立即（每次调用都查这个文件） |
| 环境变量停用 | `.env` 加 `SLIDER_ROUTE_C_ENABLED=false` | `docker compose up -d` 后 |
| 配置停用 | `global_config.example.yml` 的 `SLIDER_ROUTE_C.enabled: false` | 重新构建镜像后 |

恢复：删掉 `SLIDER_ROUTE_C_DISABLED` 文件即可。

熔断：连续失败 `failure_threshold`（默认 3）个**重试序列** → 冷却 `cooldown`（默认 600）秒，
期间不再请求后端；冷却后自动半开重试。

### 4.3 重试次数在哪改

优先级从高到低：

1. 环境变量 `SLIDER_ROUTE_C_MAX_ATTEMPTS`（`.env`）
2. **面板「设置 → 滑块验证（路线 C）→ 失败重试次数」**（写 `system_settings`，热生效，不用重启）
3. `global_config.example.yml` 的 `SLIDER_ROUTE_C.max_attempts`（默认 5）

范围 1–20，轮间隔至少 3 秒（`retry_interval`，硬下限 3）。

### 4.4 QQ 通知怎么配

前置：有一个 OneBot v11 实现（如 [NapCat](https://github.com/NapNeko/NapCatQQ)）在跑，
且它的 HTTP API 可达。

1. 面板「通知」页 → 新增渠道 → 类型选 **`QQ（NapCat/OneBot）`**
2. 填：
   - `base_url`：`http://<NAS_IP>:3000`（NapCat 的 HTTP 服务地址，**不带** `/send_private_msg`）
   - `user_id`：接收通知的 QQ 号（正整数）
   - `access_token`（可选）：NapCat 配了 token 就填，会以 `Authorization: Bearer <token>` 发出
3. 保存后用面板的「测试」按钮发一条。

实现细节：`POST {base_url}/send_private_msg`，body `{"user_id": <int>, "message": "<text>"}`；
**成功判据是响应体 `retcode == 0`**，HTTP 200 不算成功（NapCat 用 200 + `retcode != 0` 表达业务失败）。

滑块重试序列耗尽时走既有事件 `captcha_manual`（「人工验证提醒」，critical）触发通知。

### 4.5 日志怎么读

```bash
docker logs xianyu-super-butler | grep -E 'preflight|滑块验证'
```

```
【账号】滑块验证：路线C 通过 {"path": "route_c", "reason": "pass", "elapsed_s": 37.7, ...}
【账号】滑块验证：路线C 未通过（无回退路径） {"reason": "slide_reject", "slide_code": 300, ...}
```

`solver` 的启动日志在容器内 `/app/logs/routec-solver-boot.log`，同时 `tee` 到容器 stdout。

### 4.6 发布商品（L1 协议 / L3 真机兜底）

三档链路：**L1 协议**（`utils/item_publish.py` 直调 mtop）→ 撞滑块时 **L2 路线 C** 过滑块后回 L1 重发 →
L1/L2 都失败且 `real_machine_fallback=true` 时 **L3 真机兜底**（`utils/real_machine_ops.py`，
VM101 常驻 Chrome 把整条人工流程走完）。

- 面板「商品自动化」素材行点「发布」→ 后端返回**摘要 + 一次性确认令牌**（TTL 300s）→
  弹窗确认后带令牌执行；**摘要没回来前按钮 disabled**。
- 定时自动发布只处理**白名单**（`product_materials.auto_approved=1 AND publish_status='ready'`），
  非白名单跳过并记原因；总开关 `product_auto_enabled` 默认 **false**。
- `publish_dry_run` 默认 **true**（只组装 payload、脱敏打印，不发请求）—— 真发布前必须显式关掉。

接口清单与版本号：

| 用途 | api | version | spm_cnt |
| --- | --- | --- | --- |
| 发布 | `mtop.idle.pc.idleitem.publish` | 1.0 | `a21ybx.publish.0.0` |
| 类目识别 | `mtop.taobao.idle.kgraph.property.recommend` | 2.0 | `a21ybx.publish.0.0` |
| 默认地址 | `mtop.taobao.idle.local.poi.get` | 1.0 | `a21ybx.publish.0.0` |

### 4.7 下架 / 删除（真删除，不可逆）

面板「删除规则」点「执行」→ 同样是**摘要 + 确认令牌**两段式；自动执行只处理白名单
（`product_delete_rules.auto_execute=1 AND enabled=1`）。

链路：`utils/item_delete.py` 调 `com.taobao.idle.item.delete` **v1.1**（注意不是 1.0，spm `a21ybx.item.0.0`）。
**实测语义 = 真删除**：返回 `['SUCCESS::调用成功']` 后商品从在售列表消失、`pc.detail` 回
`FAIL_BIZ_ITEM_DEL_NOT_FOUND`，**不可恢复**。返回值带 `semantics` 字段，不要自己猜。

### 4.8 账号密码自动登录

**触发**：免密刷新（cookie 续期）**连续失败 `pwd_login_fail_threshold`（默认 5）次**后自动走密码登录；
任一次刷新成功即清零。

**动作**：CDP 连 VM101 常驻 Chrome 做导航 / 读 DOM / 取坐标 / cookie 读写；账号、密码、提交
**全部走真机 SendInput**（`type` / `key` / `keydown` / `keyup`）。人脸 / 短信**不自动过** ——
截图 + 推 QQ + 状态 `need_manual` + 进冷却，必须人工。

手动触发：面板账号页，或 `POST /password-login`（body `cookie_id`）→ 轮询
`GET /password-login/check/{session_id}`（processing / success / need_manual / failed）；
重置冷却 `POST /password-login/reset-cooldown`。

### 4.9 写操作限流

写操作（擦亮 / 发布 / 下架 / 删除）走 `utils/write_guard.py` 的**独立**令牌桶，不再与读共用。
超限 **fail fast**（返回 `rate_limited=true` + `retry_after` 秒），**不排队等待**。
每日计数按 `Asia/Shanghai` 自然日，存独立表 `write_op_counters`。

### 4.10 二次确认令牌

`app/product_automation.py` 的 `ConfirmTokenStore`：一次性、TTL 300s、内存 dict、**不落库**（重启即失效）。
发布 / 执行删除都先 `prepare` 拿令牌（同时返回「将要发生什么」的摘要），再 `execute` 带令牌。
**白名单是自动执行的唯一入口**；没进白名单、没开总开关时，只有人工 + 令牌能触发。

### 4.11 面板配置键总表

这些键都在 `system_settings`，用 `GET /system-settings` / `PUT /system-settings/{key}` 读写
（`PUT` 接受任意新键），面板改完**热生效、不用重建镜像**：

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `write_rate_enabled` | true | 写限流总开关 |
| `write_rate_per_minute` | 1 | 写操作每分钟上限 |
| `write_daily_limit` | 20 | 单账号每日写操作上限 |
| `pwd_login_enabled` | true | 密码登录总开关 |
| `pwd_login_fail_threshold` | 5 | 免密刷新连续失败几次触发密码登录 |
| `pwd_login_cooldown_minutes` | 30 | 触发后冷却（分钟） |
| `publish_dry_run` | true | 发布只组装 payload，不发请求 |
| `real_machine_fallback` | false | L1+L2 失败时自动走 L3 真机兜底 |
| `product_auto_enabled` | false | 商品自动化定时循环总开关 |
| `product_auto_interval` | 3600 | 循环间隔（秒，下限 600） |
| `slider_route_c_max_attempts` | 5 | 路线 C 一次求解序列的最大轮数 |

> 每个账号还有 `pwd_login_fail_count:<cookie_id>` / `pwd_login_cooldown_until:<cookie_id>` /
> `pwd_login_attempt_fail:<cookie_id>` / `pwd_login_status:<cookie_id>` 这几个运行时键。

---

## 5. 已知限制

- **路线 C 依赖一台真实 Windows 机器**（Chrome + 交互会话）。没有它滑块就跑不了；
  容器内浏览器方案已移除，**没有回退**。
- **人工投屏链路已移除**：不再有「把验证页投到面板上人工拖」的功能。
  账号页仍保留「在我的浏览器打开验证页」（把新鲜 punish 链接交给用户自己的浏览器），
  它不依赖被删模块。
- **面板商品列表的「删除」= 仅本界面隐藏**（写墓碑 `deleted_items`）；**真正的闲鱼侧删除**是另一条显式链路
  （面板「删除规则 → 执行」，`utils/item_delete.py`），实测 `com.taobao.idle.item.delete` **v1.1** 是
  **真删除、不可逆**。两者不要混。
- Windows 锁屏后 `SendInput` 到不了桌面（安全桌面限制）；`driver.ps1` 的 `unlock` 是兜底，
  主防线是关闭锁屏 / 睡眠。
- 上游 `latest` 镜像与源码可能比本仓库新；本仓库基于上文标注的提交。

---

## 6. 上游致谢

- [`23Star/xianyu-super-butler`](https://github.com/23Star/xianyu-super-butler) —— 本项目的全部基础
  （面板、自动发货、AI 回复、扫码登录、商品同步等）。本仓库以 AGPL-3.0 继续分发，
  所有上游代码的著作权归原作者。
- 思路参考：[`bixipeng/Xianyu-Auto`](https://github.com/bixipeng/Xianyu-Auto)（MTOP 接口信封与签名方式）。

---

## 7. 免责声明

本项目仅用于**学习与技术研究**。使用者需自行承担因使用本软件产生的全部风险与后果，
包括但不限于账号被限制、风控、数据丢失等。请遵守闲鱼 / 淘宝的用户协议与相关法律法规，
不要用于任何违反平台规则的用途。
