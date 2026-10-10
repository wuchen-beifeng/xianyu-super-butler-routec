# custom/vm101/ — 真机侧执行器（Windows）

路线 C 需要一台**真实 Windows 机器**，在上面用 `user32!SendInput` 做鼠标拖动。
本目录提供这台机器上要跑的全部东西。

```
custom/vm101/
  driver.ps1     HTTP 执行器：把 JSON 命令翻译成 SendInput / 窗口操作
  README.md      本文件
```

> 为什么不用 CDP 派发鼠标事件？见 [`../../docs/01-路线C滑块链路.md`](../../docs/01-路线C滑块链路.md) §1：
> 阿里 nc 能区分「每帧一个」的合成事件与真机输入栈的事件，前者通过率 0。

---

## 1. 环境要求

- Windows 10 / 11（本项目在 Win10 LTSC 上实测）
- **Windows PowerShell 5.1**（系统自带，无需装 Python）
- Google Chrome（正式版，非 Chromium-for-Testing —— 后者的指纹会被 nc 识破）
- OpenSSH Client（Windows 可选功能，用于反向隧道）
- 一个可登录的交互用户会话（`SendInput` **必须**在 Session 1 才有意义）

---

## 2. 文件布局（约定，可按需改）

```
C:\reverselab\
  unlock.pw                 # 解锁密码文件（UTF-8 无 BOM，只给 driver 读）
  ssh\
    id_ed25519_rev          # 反向隧道用的私钥
    known_hosts
  logs\
    wininput-tunnel.log
  wininput\
    driver.ps1              # ← 本目录的 driver.ps1
    run-hidden.vbs          # 隐藏窗口拉起 driver.ps1
    revtunnel.cmd           # 反向 SSH 隧道（8791 + 9222 + 9000）
    driver.log              # driver 自己写的日志
```

---

## 3. 计划任务（route C 链路上的四个）

### 3.1 `WinInputDriver` —— 执行器

**必须**以**交互用户**身份运行（`InteractiveToken`），不能用 SYSTEM：
`SendInput` 从 Session 0 发不出去，到不了 Session 1 的桌面。

- Action：`wscript.exe C:\reverselab\wininput\run-hidden.vbs`
- Principal：`<交互用户>`（实测 `WIN10-LTSC\Administrator`）/ `InteractiveToken` / RunLevel Highest
- Trigger：**只有一条** `MSFT_TaskLogonTrigger`（`UserId=WIN10-LTSC\Administrator`、`Delay=PT45S`、Enabled）
- Settings：`StartWhenAvailable=true`、`MultipleInstancesPolicy=IgnoreNew`、`ExecutionTimeLimit=PT0S`（不限时）

> ⚠️ **不要给它加 `MSFT_TaskBootTrigger`**：`InteractiveToken` 的 principal 在开机那一刻拿不到交互令牌，
> 会话 1 还不存在，`SendInput` 没有桌面可发。本机已开**开机自动登录**（§4），
> 所以「开机自启」语义由「自动登录后的 AtLogon」实现。
> 45s `Delay` 用于避开登录风暴；实测重启（2026-10-10 01:20）后：
> 自动登录 01:20:28 → 触发 01:21:13 → `driver.log` 记 `listening ... session=1` @01:21:15，全程无人工干预。
> 判活只看 `probe`（§5），**不要**看任务状态（恒为 `Ready`）。

`run-hidden.vbs` 的内容（**别直接跑 `powershell -File driver.ps1`**，
那样会在交互桌面弹一个控制台窗口，盖住浏览器并抢走鼠标事件）：

```vbs
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\reverselab\wininput\driver.ps1", 0, False
```

> ⚠️ 因为 `WScript.Shell.Run(..., 0, False)` 是**非阻塞**的，任务状态会一直停在 `Ready`。
> **这不代表驱动没跑**。判活一律用 `probe`（见 §5）。

### 3.2 `ChromeWinInput` —— 拉起 Chrome（带 CDP）

- Action：启动 Chrome，带 `--remote-debugging-port=9222` 与独立 `--user-data-dir`
- Principal：`<交互用户>`（实测 `WIN10-LTSC\Administrator`）/ `InteractiveToken` / RunLevel Highest
- Trigger：**只有一条** `MSFT_TaskLogonTrigger`（`UserId=WIN10-LTSC\Administrator`、`Delay=PT45S`、Enabled）
- Settings：`StartWhenAvailable=true`、`MultipleInstancesPolicy=IgnoreNew`、`ExecutionTimeLimit=PT0S`
- 用途：`driver.ps1` 的 `launch-chrome` 动作会 `schtasks /Run /TN ChromeWinInput`

> 同样不要加 BootTrigger（同 §3.1）。`ExecutionTimeLimit` 原为 `PT72H`，会在 3 天后把 Chrome 掐掉 ——
> 已改为 `PT0S`（不限时）。`IgnoreNew` 保证「已在跑」时重复触发被忽略。

实际命令行（实测）：

```
chrome.exe --user-data-dir=C:\reverselab\chrome-wininput --remote-debugging-port=9222 ^
           --remote-allow-origins=* --disable-gpu-sandbox --no-first-run ^
           --no-default-browser-check --disable-features=Translate ^
           --window-position=0,0 --window-size=1920,1080 about:blank
```

### 3.3 `WinInputTunnel` —— 反向 SSH 隧道（**`reverse_agent` 抓包链路用，本应用不用这条**）

把真机的 `8791` / `9222`（以及可选的 `9000` 代理口）反向连到 `<VM100_IP>` ——
这是给 `reverse_agent` 的 Reqable 抓包链路用的，**与本应用无关**；
本应用（管家）的滑块链路走 §3.4 那条直连应用机的隧道。

- Action：`cmd.exe /c C:\reverselab\wininput\revtunnel.cmd`
- Trigger：**开机**（`MSFT_TaskBootTrigger`）
- Principal：`SYSTEM` / ServiceAccount / RunLevel Highest
- Settings：`RestartCount=999`、`RestartInterval=PT1M`、`ExecutionTimeLimit=PT0S`（不限时）、
  `MultipleInstances=IgnoreNew`、`StartWhenAvailable`

`revtunnel.cmd`（单条 ssh 同时转发三个端口 + 断线自愈循环）：

```bat
@echo off
:loop
"C:\Windows\System32\OpenSSH\ssh.exe" -N ^
 -i C:\reverselab\ssh\id_ed25519_rev ^
 -o StrictHostKeyChecking=no ^
 -o UserKnownHostsFile=C:\reverselab\ssh\known_hosts ^
 -o ExitOnForwardFailure=yes ^
 -o ServerAliveInterval=30 ^
 -o ServerAliveCountMax=3 ^
 -o ConnectTimeout=10 ^
 -R 8791:127.0.0.1:8791 ^
 -R 9222:127.0.0.1:9222 ^
 -R 9000:127.0.0.1:9000 ^
 <应用机用户>@<VM102_IP> >> C:\reverselab\logs\wininput-tunnel.log 2>&1
echo [%date% %time%] tunnel exited, retrying in 5s >> C:\reverselab\logs\wininput-tunnel.log
timeout /t 5 /nobreak > NUL
goto loop
```

要点：

- `ExitOnForwardFailure=yes`：端口被占用时立刻退出，交给外层循环重试
  （否则会「连上了但没转发」，变成假活）。
- 杀进程后 **~17s** 自愈（5s 循环 + 一次重连竞争）。
- `<VM102_IP>`（应用机）侧需要有 `<应用机用户>` 的 `authorized_keys` 里对应这把私钥。
- **同一个端口不要开两条隧道**：两条都带 `ExitOnForwardFailure=yes`，后到的那条会直接退出。

### 3.4 `WinInputTunnel2` —— 第二条反向隧道（→ VM102，route C 实际使用）

§3.3 的 `WinInputTunnel` 反连到 `<VM100_IP>`，那是 `reverse_agent` 抓包用的（见上，与本应用无关）；
**本应用**的滑块链路末端由这条**第二条隧道**承载（应用机 `<VM102_IP>` 上 `ss -tln` 可见
`<VM102_IP>:8791` / `:9222`，owner 是 `sshd-session`）。

- Action：`wscript.exe C:\reverselab\wininput\run-hidden-revtunnel2.vbs` → `revtunnel2.cmd`
- Principal：`WIN10-LTSC\Administrator` / `InteractiveToken` / RunLevel Highest
- Trigger：`MSFT_TaskLogonTrigger`（`UserId=WIN10-LTSC\Administrator`，**无 Delay**）
- Settings：`StartWhenAvailable`、`MultipleInstances=IgnoreNew`、`ExecutionTimeLimit=PT0S`、`RestartOnFailure=999 / PT1M`
- 转发：`-R <VM102_IP>:8791:127.0.0.1:8791` 与 `-R <VM102_IP>:9222:127.0.0.1:9222`，登录用户 `root@<VM102_IP>`
  （VM102 侧 `sshd_config.d/10-gatewayports.conf` = `GatewayPorts clientspecified`）
- 日志：`C:\reverselab\logs\revtunnel2.log`

> 它没有 Delay，比 driver/Chrome 先起：ssh 先把 VM102 的 8791/9222 端口占住，driver/Chrome 稍后起来也能立刻被转发到。

---

## 4. 关掉锁屏 / 睡眠（强烈建议）

锁屏后 Windows 把输入桌面切到 Winlogon 安全桌面，`SendInput` **到不了** —— 这是方案固有限制。
`driver.ps1` 的 `unlock` 只是兜底，主防线是**永不锁屏 + 永不休眠 + 开机自动登录**：

```powershell
# 交互用户的 HKCU（注意：在 SYSTEM 上下文里 HKCU 是 SYSTEM 自己的配置单元，别写错）
reg add "HKCU\Control Panel\Desktop" /v ScreenSaveActive /t REG_SZ /d 0 /f
reg add "HKCU\Control Panel\Desktop" /v ScreenSaverIsSecure /t REG_SZ /d 0 /f

# 电源：显示器/待机/休眠都设为「从不」
powercfg /change monitor-timeout-ac 0
powercfg /change monitor-timeout-dc 0
powercfg /change standby-timeout-ac 0
powercfg /change standby-timeout-dc 0
powercfg /change hibernate-timeout-ac 0
powercfg /change hibernate-timeout-dc 0

# 策略：禁止锁定工作站 / 无锁屏
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\System" /v DisableLockWorkstation /t REG_DWORD /d 1 /f
reg add "HKLM\SOFTWARE\Policies\Microsoft\Windows\System" /v InactivityTimeoutSecs /t REG_DWORD /d 0 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Personalization" /v NoLockScreen /t REG_DWORD /d 1 /f

# 开机自动登录（密码明文存在注册表，是 Windows 自动登录的固有代价）
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v AutoAdminLogon /t REG_SZ /d 1 /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultUserName /t REG_SZ /d <用户名> /f
reg add "HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon" /v DefaultDomainName /t REG_SZ /d <计算机名> /f
```

解锁密码文件 `C:\reverselab\unlock.pw`：**UTF-8 无 BOM**，内容就是登录密码，一行。
`driver.ps1` 的 `unlock` 动作读它并用 `SendInput` 逐字符注入（与键盘布局无关），
**不回显、不写日志、不进返回值**。

---

## 5. 协议

`driver.ps1` 在 `127.0.0.1:8791` 上跑一个极简 HTTP 服务，`POST /`，body 是 JSON：

| action | 请求 | 响应（`data`） |
| --- | --- | --- |
| `probe` | — | `session` / `screen` / `fg` / `cursor` |
| `state` | — | `session` / `desktop` / `locked` / `logonui` / `explorer` / `chrome`（进程数）/ `chromeWindow` / `chromeIconic` / `chromeClassWindow` / `chromeWindowHwnd` |
| `windows` | — | 可见顶层窗口列表（`cls` / `exe` / `title` / `rect` / `iconic` / `zoomed`） |
| `focus` | `{"class":"..","title":"..","exe":".."}` | `{found, was_iconic, restored, set_ok, foreground_ok, hwnd}`（`title` 为子串匹配） |
| `unlock` | `{"password_file":"C:\\reverselab\\unlock.pw"}`（可选） | `{locked, unlocked, method}`（`noop` / `dismiss` / `password`） |
| `launch-chrome` | — | `{task, exit_code}`（`schtasks /Run /TN ChromeWinInput`） |
| `move` | `{"x":..,"y":..}` | — |
| `click` | `{"x":..,"y":..,"button":"left"}` | — |
| `drag` | `{"start":[x,y],"points":[[dx,dy,dt],..],"sub":N,"sub_gap_us":N,"press_ms":N,"release_ms":N}` | 每个 SendInput 调用的 QPC 时间戳 |
| `hide` | — | 隐藏自己的控制台窗口 |
| `shutdown` | — | 停监听并退出进程 |

响应统一 `{"ok": true, "data": {...}}`。

字段语义的**两个坑**（详见 `../../docs/01` §6）：

- `chromeWindow` = 存在**属于 `chrome.exe` 进程**的可见 `Chrome_WidgetWin_1` 窗口（**用这个判活**）；
  `chromeClassWindow` 只是纯类名匹配 —— Electron 应用也注册这个窗口类，会假阳性。
- 判驱动是否在跑，**只看 `probe`**，不要看 `WinInputDriver` 任务状态（恒为 `Ready`）。

---

## 6. 自检与重启

```powershell
# 判活（唯一的判活方式）
curl.exe -s -X POST http://127.0.0.1:8791/ -d "{\"action\":\"probe\"}"
# 看状态
curl.exe -s -X POST http://127.0.0.1:8791/ -d "{\"action\":\"state\"}"
```

**重启驱动的规范动作**（改完 `driver.ps1` 后）：

```powershell
# 1) 让 driver 自己退出
curl.exe -s -X POST http://127.0.0.1:8791/ -d "{\"action\":\"shutdown\"}"
# 2) 重新拉起
schtasks /End /TN WinInputDriver
schtasks /Run /TN WinInputDriver
# 3) 等 5s 再 probe，确认 session=1
```

> ⚠️ `driver.ps1` **必须保持纯 ASCII**：PowerShell 5.1 在没有 BOM 时按 ANSI/GBK 读取 `.ps1`，
> 非 ASCII 字节会静默破坏脚本。

---

## 7. 安全

- `driver.ps1` 能注入键盘鼠标 —— **绝不要**把 `8791` 暴露到不可信网络。
  本方案里它只监听 `127.0.0.1`，对外靠反向 SSH 隧道 + 应用机侧白名单。
- 隧道落点侧只放行应用机（`<VM102_IP>`）来源。
- `unlock.pw` 只给 driver 读，不要进版本库、不要进日志。
