# driver.ps1 -- VM101-side Win32 real-input executor.  (v2)
#
# Runs a small HTTP service on 127.0.0.1:8791 inside VM101's INTERACTIVE session.
# The app host sends JSON commands through a reverse SSH tunnel; the driver injects
# mouse/keyboard events with user32!SendInput, i.e. through the OS input stack,
# not via CDP Input.dispatchMouseEvent.
#
# Requires only Windows PowerShell 5.1 (no Python on this VM).
# Launch: scheduled task WinInputDriver, InteractiveToken, user administrator --
# SendInput from session 0 would target session 0's desktop and never reach the
# browser.  The task runs C:\reverselab\wininput\run-hidden.vbs (window style 0)
# so no console window covers the browser.
# NOTE: keep this file ASCII-only. PowerShell 5.1 reads .ps1 as ANSI (GBK here)
# unless a BOM is present, and non-ASCII bytes silently corrupt the script.
#
# Protocol: POST / with JSON body
#   {"action":"probe"}                       -> session / screen / foreground / cursor
#   {"action":"state"}                       -> session / desktop / locked / logonui /
#                                               explorer / chrome / chromeWindow (visible
#                                               Chrome_WidgetWin_1 owned by chrome.exe) /
#                                               chromeIconic / chromeClassWindow (class only) /
#                                               chromeWindowHwnd
#   {"action":"windows"}                     -> visible top-level windows (cls/exe/iconic/zoomed)
#   {"action":"focus","class":"..","title":"..","exe":".."}
#                                            -> find window, SW_RESTORE if iconic,
#                                               SetForegroundWindow; {found,was_iconic,restored,foreground_ok}
#   {"action":"unlock","password_file":"C:\\reverselab\\unlock.pw"}
#                                            -> noop when not locked; else key/mouse dismiss or
#                                               password entry; {locked,unlocked,method}
#   {"action":"launch-chrome"}               -> schtasks /Run /TN ChromeWinInput
#   {"action":"move","x":..,"y":..}
#   {"action":"click","x":..,"y":..,"button":"left"}
#   {"action":"drag", ...}
#   {"action":"type","text":".."}             -> type the whole string with SendInput
#                                               (KEYEVENTF_UNICODE, char by char)
#                                               -> {typed:<char count>,us:<elapsed us>}
#   {"action":"key","vk":<int>,"hold_ms":0}   -> press a virtual key and release it
#                                               (hold_ms>0 sleeps between down/up)
#   {"action":"keydown","vk":<int>}           -> press and hold (for key combos)
#   {"action":"keyup","vk":<int>}             -> release a held key
#   {"action":"hide"}                        -> hide own console window
#   {"action":"shutdown"}
# drag fields:
#   start:[x,y]            press point, screen coordinates
#   points:[[dx,dy,dt],..] cumulative offset from start + wait ms before the step
#   sub:0|N                split each step into N SendInput calls (0 -> 1)
#   sub_gap_us:0           busy-wait gap between sub-events, microseconds
#   press_ms / release_ms  pause after press / before release
# Response: {"ok":true,"data":{...}} with a QPC timestamp per SendInput call.
#
# SECURITY: the unlock password is read from disk, typed with SendInput and is
# NEVER written to the log, NEVER returned in a response and NEVER echoed.
# The same rule applies to the 'type' action: the text is typed with SendInput
# and is NEVER written to the log, NEVER returned in a response and NEVER echoed.

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

$Port = 8791
$Prefix = "http://127.0.0.1:$Port/"
$LogDir = 'C:\reverselab\wininput'
New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
$LogFile = Join-Path $LogDir 'driver.log'

function Log([string]$m) {
    $line = "[{0}] {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $m
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using System.Text;

public class WinInput {
    [StructLayout(LayoutKind.Sequential)]
    public struct MOUSEINPUT {
        public int dx; public int dy; public uint mouseData;
        public uint dwFlags; public uint time; public IntPtr dwExtraInfo;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct INPUT {
        public uint type; public MOUSEINPUT mi;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct KEYBDINPUT {
        public ushort wVk; public ushort wScan;
        public uint dwFlags; public uint time; public IntPtr dwExtraInfo;
    }
    // Same size as INPUT (40 bytes on x64) so it can be handed to SendInput.
    [StructLayout(LayoutKind.Explicit, Size = 40)]
    public struct INPUTK {
        [FieldOffset(0)] public uint type;
        [FieldOffset(8)] public KEYBDINPUT ki;
    }
    [StructLayout(LayoutKind.Sequential)]
    public struct POINT { public int X; public int Y; }
    [StructLayout(LayoutKind.Sequential)]
    public struct RECT { public int Left, Top, Right, Bottom; }

    public delegate bool EnumProc(IntPtr h, IntPtr l);

    [DllImport("user32.dll", SetLastError = true)]
    public static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);
    [DllImport("user32.dll", SetLastError = true, EntryPoint = "SendInput")]
    public static extern uint SendInputK(uint nInputs, INPUTK[] pInputs, int cbSize);
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int c);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
    [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr h);
    [DllImport("user32.dll")] public static extern IntPtr GetDesktopWindow();
    [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT r);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetWindowTextW(IntPtr h, System.Text.StringBuilder s, int n);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern int GetClassNameW(IntPtr h, System.Text.StringBuilder s, int n);
    [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
    [DllImport("user32.dll")] public static extern int GetSystemMetrics(int i);
    [DllImport("kernel32.dll")] public static extern IntPtr GetConsoleWindow();
    [DllImport("user32.dll")] static extern bool EnumWindows(EnumProc cb, IntPtr l);
    [DllImport("user32.dll")] static extern bool IsWindowVisible(IntPtr h);
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);

    // Input-desktop probing: "Winlogon" while the secure lock screen is up,
    // "Default" otherwise.
    [DllImport("user32.dll", SetLastError = true)]
    static extern IntPtr OpenInputDesktop(uint dwFlags, bool fInherit, uint dwDesiredAccess);
    [DllImport("user32.dll", SetLastError = true)]
    static extern bool CloseDesktop(IntPtr h);
    [DllImport("user32.dll", SetLastError = true)]
    static extern IntPtr GetThreadDesktop(uint dwThreadId);
    [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
    [DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool GetUserObjectInformationW(IntPtr hObj, int nIndex,
        System.Text.StringBuilder pvInfo, uint nLength, out uint lpnLengthNeeded);

    public const uint INPUT_MOUSE = 0;
    public const uint INPUT_KEYBOARD = 1;
    public const uint MOVE        = 0x0001;
    // NOTE: these flag constants are deliberately prefixed. PowerShell resolves
    // members case-insensitively, so a const named LEFTDOWN collides with the
    // method LeftDown() -- the binder picks the field and the call dies with
    // "does not contain a method named 'LeftDown'".
    public const uint MEF_LEFTDOWN  = 0x0002;
    public const uint MEF_LEFTUP    = 0x0004;
    public const uint MEF_RIGHTDOWN = 0x0008;
    public const uint MEF_RIGHTUP   = 0x0010;
    public const uint ABSOLUTE    = 0x8000;
    public const uint VIRTUALDESK = 0x4000;
    public const uint KEF_EXTENDED = 0x0001;
    public const uint KEF_KEYUP    = 0x0002;
    public const uint KEF_UNICODE  = 0x0004;

    public const int SW_RESTORE = 9;
    public const uint VK_SPACE  = 0x20;
    public const uint VK_RETURN = 0x0D;

    static int VW, VH;

    public static void Init() {
        try { SetProcessDPIAware(); } catch {}
        VW = GetSystemMetrics(78); // SM_CXVIRTUALSCREEN
        VH = GetSystemMetrics(79); // SM_CYVIRTUALSCREEN
        if (VW <= 0) VW = GetSystemMetrics(0);
        if (VH <= 0) VH = GetSystemMetrics(1);
    }

    // Hide our own console window, otherwise it covers the browser and steals
    // the mouse events.
    public static void HideConsole() {
        try { IntPtr h = GetConsoleWindow(); if (h != IntPtr.Zero) ShowWindow(h, 0); } catch {}
    }

    // Visible top-level windows of this session (proves nothing covers Chrome).
    public static System.Collections.Generic.List<string> Windows() {
        System.Collections.Generic.List<string> res = new System.Collections.Generic.List<string>();
        IntPtr fg = GetForegroundWindow();
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            uint p; GetWindowThreadProcessId(h, out p);
            System.Text.StringBuilder sb = new System.Text.StringBuilder(256);
            GetWindowTextW(h, sb, 256);
            System.Text.StringBuilder cb = new System.Text.StringBuilder(256);
            GetClassNameW(h, cb, 256);
            RECT r; GetWindowRect(h, out r);
            bool vis = IsWindowVisible(h);
            if (vis || h == fg) {
                res.Add(string.Format("{0} fg={1} pid={2} hwnd={3} rect={4},{5},{6},{7} iconic={8} zoomed={9} cls='{10}' exe='{12}' title='{11}'",
                    vis ? "VIS" : "hid", (h == fg), p, h, r.Left, r.Top, r.Right, r.Bottom,
                    IsIconic(h), IsZoomed(h), cb.ToString(), sb.ToString(), WindowProcessName(h)));
            }
            return true;
        }, IntPtr.Zero);
        return res;
    }

    public static string WindowProcessName(IntPtr h) {
        try {
            uint pid; GetWindowThreadProcessId(h, out pid);
            if (pid == 0) return "";
            return System.Diagnostics.Process.GetProcessById((int)pid).ProcessName;
        } catch { return ""; }
    }

    // First visible top-level window matching class, (substring) title and
    // owning process name. Any argument may be empty to skip that filter.
    // The exe filter matters: Electron apps (e.g. WorkBuddyAI) also register
    // the Chrome_WidgetWin_1 window class, so a class-only match is a false
    // positive for "Chrome is running".
    public static IntPtr FindWindowEx2(string cls, string title, string exe) {
        IntPtr found = IntPtr.Zero;
        EnumWindows(delegate(IntPtr h, IntPtr l) {
            if (!IsWindowVisible(h)) return true;
            if (cls != null && cls.Length > 0) {
                System.Text.StringBuilder cb = new System.Text.StringBuilder(256);
                GetClassNameW(h, cb, 256);
                if (!string.Equals(cb.ToString(), cls, StringComparison.OrdinalIgnoreCase)) return true;
            }
            if (title != null && title.Length > 0) {
                System.Text.StringBuilder tb = new System.Text.StringBuilder(512);
                GetWindowTextW(h, tb, 512);
                if (tb.ToString().IndexOf(title, StringComparison.OrdinalIgnoreCase) < 0) return true;
            }
            if (exe != null && exe.Length > 0) {
                if (!string.Equals(WindowProcessName(h), exe, StringComparison.OrdinalIgnoreCase)) return true;
            }
            found = h; return false;
        }, IntPtr.Zero);
        return found;
    }

    static string DesktopName(IntPtr h) {
        try {
            uint need;
            System.Text.StringBuilder sb = new System.Text.StringBuilder(256);
            if (GetUserObjectInformationW(h, 2 /*UOI_NAME*/, sb, (uint)(sb.Capacity * 2), out need))
                return sb.ToString();
        } catch {}
        return null;
    }

    // Name of the desktop that currently receives input for this session.
    public static string InputDesktopName() {
        IntPtr h = OpenInputDesktop(0, false, 0x0100 /*DESKTOP_SWITCHDESKTOP*/);
        if (h != IntPtr.Zero) {
            string n = DesktopName(h);
            CloseDesktop(h);
            if (n != null) return n;
        }
        IntPtr t = GetThreadDesktop(GetCurrentThreadId());
        string n2 = (t != IntPtr.Zero) ? DesktopName(t) : null;
        return (n2 == null) ? "unknown" : n2;
    }

    public static bool IsLocked() {
        if (System.Diagnostics.Process.GetProcessesByName("LogonUI").Length > 0) return true;
        string d = InputDesktopName();
        return !string.Equals(d, "Default", StringComparison.OrdinalIgnoreCase);
    }

    static int NX(int x) { return (int)Math.Round((double)x * 65535.0 / (VW - 1)); }
    static int NY(int y) { return (int)Math.Round((double)y * 65535.0 / (VH - 1)); }

    static uint Send(uint flags, int dx, int dy) {
        INPUT[] inp = new INPUT[1];
        inp[0].type = INPUT_MOUSE;
        inp[0].mi.dx = dx; inp[0].mi.dy = dy;
        inp[0].mi.dwFlags = flags;
        inp[0].mi.time = 0;
        inp[0].mi.dwExtraInfo = IntPtr.Zero;
        return SendInput(1, inp, Marshal.SizeOf(typeof(INPUT)));
    }

    // Absolute positioning through SendInput (produces a real WM_MOUSEMOVE).
    public static uint AbsMove(int x, int y) { return Send(MOVE | ABSOLUTE | VIRTUALDESK, NX(x), NY(y)); }
    // Relative displacement -- the native form of a hardware mouse report.
    public static uint RelMove(int dx, int dy) { return Send(MOVE, dx, dy); }
    public static uint LeftDown()  { return Send(MEF_LEFTDOWN, 0, 0); }
    public static uint LeftUp()    { return Send(MEF_LEFTUP, 0, 0); }
    public static uint RightDown() { return Send(MEF_RIGHTDOWN, 0, 0); }
    public static uint RightUp()   { return Send(MEF_RIGHTUP, 0, 0); }

    // --- keyboard ---------------------------------------------------------
    public static uint KeyVk(uint vk, bool up) {
        INPUTK[] a = new INPUTK[1];
        a[0].type = INPUT_KEYBOARD;
        a[0].ki.wVk = (ushort)vk; a[0].ki.wScan = 0;
        a[0].ki.dwFlags = up ? KEF_KEYUP : 0;
        a[0].ki.time = 0; a[0].ki.dwExtraInfo = IntPtr.Zero;
        return SendInputK(1, a, 40);
    }
    // Layout-independent character injection (KEYEVENTF_UNICODE).
    public static uint KeyChar(char c, bool up) {
        INPUTK[] a = new INPUTK[1];
        a[0].type = INPUT_KEYBOARD;
        a[0].ki.wVk = 0; a[0].ki.wScan = (ushort)c;
        a[0].ki.dwFlags = KEF_UNICODE | (up ? KEF_KEYUP : 0);
        a[0].ki.time = 0; a[0].ki.dwExtraInfo = IntPtr.Zero;
        return SendInputK(1, a, 40);
    }
    public static uint TapVk(uint vk) {
        uint n = KeyVk(vk, false);
        System.Threading.Thread.Sleep(30);
        n += KeyVk(vk, true);
        return n;
    }
    public static int TypeText(string s) {
        int n = 0;
        foreach (char c in s) {
            n += (int)KeyChar(c, false);
            n += (int)KeyChar(c, true);
            System.Threading.Thread.Sleep(15);
        }
        return n;
    }

    public static string FgTitle() {
        IntPtr h = GetForegroundWindow();
        System.Text.StringBuilder sb = new System.Text.StringBuilder(512);
        GetWindowTextW(h, sb, 512);
        return h.ToInt64().ToString() + "|" + sb.ToString();
    }
    public static long FgHwnd() { return GetForegroundWindow().ToInt64(); }
    public static int[] Cursor() {
        POINT p; GetCursorPos(out p); return new int[] { p.X, p.Y };
    }
    public static int[] Screen() { return new int[] { VW, VH }; }
}
'@

[WinInput]::Init()
[WinInput]::HideConsole()
Log (("wininput methods: " + (([WinInput].GetMethods() | Where-Object { $_.Name -match 'Move|Down|Up|Windows|Key|Type|Lock' } | ForEach-Object { $_.Name }) -join ',')))

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add($Prefix)
try { $listener.Start() } catch { Log ("START_FAIL " + $_.Exception.Message); throw }
Log ("listening " + $Prefix + " session=" + (Get-Process -Id $PID).SessionId)

function NowUs { [long]([System.Diagnostics.Stopwatch]::GetTimestamp() * 1000000.0 / [System.Diagnostics.Stopwatch]::Frequency) }

function Respond($ctx, $obj) {
    $json = $obj | ConvertTo-Json -Depth 8 -Compress
    $bytes = [Text.Encoding]::UTF8.GetBytes($json)
    $ctx.Response.StatusCode = 200
    $ctx.Response.ContentType = 'application/json; charset=utf-8'
    $ctx.Response.ContentLength64 = $bytes.Length
    $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    $ctx.Response.Close()
}

function DoDrag($cmd) {
    $marks = New-Object System.Collections.Generic.List[object]
    $start = $cmd.start
    $pts = $cmd.points
    $sub = 0; if ($cmd.sub) { $sub = [int]$cmd.sub }
    $gapUs = 0; if ($cmd.sub_gap_us) { $gapUs = [int]$cmd.sub_gap_us }
    $pressMs = 60; if ($cmd.press_ms) { $pressMs = [int]$cmd.press_ms }
    $releaseMs = 100; if ($cmd.release_ms) { $releaseMs = [int]$cmd.release_ms }

    $t0 = NowUs
    [WinInput]::AbsMove([int]$start[0], [int]$start[1]) | Out-Null
    $marks.Add(@{ t = (NowUs - $t0); kind = 'abs_move'; x = [int]$start[0]; y = [int]$start[1] })
    Start-Sleep -Milliseconds $pressMs
    [WinInput]::LeftDown() | Out-Null
    $marks.Add(@{ t = (NowUs - $t0); kind = 'left_down' })

    $cx = [int]$start[0]; $cy = [int]$start[1]
    $lastX = $cx; $lastY = $cy
    foreach ($p in $pts) {
        $tx = [int]$start[0] + [int]$p[0]
        $ty = [int]$start[1] + [int]$p[1]
        $dt = [int]$p[2]
        if ($dt -gt 0) { Start-Sleep -Milliseconds $dt }
        $dx = $tx - $cx; $dy = $ty - $cy
        if ($sub -gt 1) {
            # Split this step into `sub` absolute moves -- emulates a hardware
            # mouse reporting several samples between two frame boundaries.
            for ($i = 1; $i -le $sub; $i++) {
                $sx = [int][Math]::Round($dx * $i / $sub)
                $sy = [int][Math]::Round($dy * $i / $sub)
                $px = $cx + $sx; $py = $cy + $sy
                [WinInput]::AbsMove($px, $py) | Out-Null
                $marks.Add(@{ t = (NowUs - $t0); kind = 'move'; x = $px; y = $py; sub = $i })
                if ($gapUs -gt 0 -and $i -lt $sub) {
                    # PS 5.1 has no Start-Sleep -Microseconds; busy-wait instead.
                    $until = (NowUs - $t0) + $gapUs
                    while ((NowUs - $t0) -lt $until) { }
                }
            }
        } else {
            [WinInput]::AbsMove($tx, $ty) | Out-Null
            $marks.Add(@{ t = (NowUs - $t0); kind = 'move'; x = $tx; y = $ty; sub = 1 })
        }
        $cx = $tx; $cy = $ty; $lastX = $tx; $lastY = $ty
    }

    Start-Sleep -Milliseconds $releaseMs
    [WinInput]::LeftUp() | Out-Null
    $marks.Add(@{ t = (NowUs - $t0); kind = 'left_up' })
    $total = (NowUs) - $t0

    return @{
        ok = $true
        data = @{
            action = 'drag'
            total_us = $total
            moves = @($marks | Where-Object { $_.kind -eq 'move' }).Count
            marks = $marks
            end = @($lastX, $lastY)
            cursor = [WinInput]::Cursor()
        }
    }
}

function Get-StateData {
    $logonui = @(Get-Process -Name LogonUI -ErrorAction SilentlyContinue).Count
    $explorer = @(Get-Process -Name explorer -ErrorAction SilentlyContinue).Count
    $chrome = @(Get-Process -Name chrome -ErrorAction SilentlyContinue).Count
    $desktop = [WinInput]::InputDesktopName()
    $hwndAny = [WinInput]::FindWindowEx2('Chrome_WidgetWin_1', '', '')
    $hwndChrome = [WinInput]::FindWindowEx2('Chrome_WidgetWin_1', '', 'chrome')
    $chromeWindow = ($hwndChrome -ne [IntPtr]::Zero)
    $chromeClassWindow = ($hwndAny -ne [IntPtr]::Zero)
    $chromeIconic = $false
    if ($chromeWindow) { $chromeIconic = [WinInput]::IsIconic($hwndChrome) }
    $locked = (($logonui -gt 0) -or ($desktop -ne 'Default'))
    return @{
        action = 'state'
        session = (Get-Process -Id $PID).SessionId
        desktop = $desktop
        locked = $locked
        logonui = $logonui
        explorer = $explorer
        chrome = $chrome
        chromeWindow = $chromeWindow
        chromeIconic = $chromeIconic
        chromeClassWindow = $chromeClassWindow
        chromeWindowHwnd = $hwndChrome.ToInt64()
        ts = (Get-Date -Format 'o')
    }
}

function DoUnlock($cmd) {
    $pf = 'C:\reverselab\unlock.pw'
    if ($cmd.password_file) { $pf = [string]$cmd.password_file }

    $before = [WinInput]::IsLocked()
    if (-not $before) {
        return @{ ok = $true; data = @{ action = 'unlock'; locked = $false; unlocked = $true; method = 'noop'; desktop = [WinInput]::InputDesktopName() } }
    }

    $hasPw = $false
    try { $hasPw = (Test-Path -LiteralPath $pf) } catch { $hasPw = $false }
    $method = 'dismiss'
    if ($hasPw) { $method = 'password' }

    # Wake the screen: space + a small mouse move.
    [WinInput]::TapVk([WinInput]::VK_SPACE) | Out-Null
    [WinInput]::RelMove(1, 0) | Out-Null
    [WinInput]::RelMove(-1, 0) | Out-Null
    Start-Sleep -Milliseconds 400

    if ($hasPw) {
        # SECURITY: never log/echo/return this value.
        $pw = [IO.File]::ReadAllText($pf, [Text.Encoding]::UTF8)
        $pw = $pw.TrimEnd([char]13, [char]10)
        [WinInput]::TypeText($pw) | Out-Null
        Start-Sleep -Milliseconds 250
    }

    [WinInput]::TapVk([WinInput]::VK_RETURN) | Out-Null
    Start-Sleep -Milliseconds 1500

    $after = [WinInput]::IsLocked()
    return @{ ok = $true; data = @{
        action = 'unlock'
        locked = $after
        unlocked = (-not $after)
        method = $method
        desktop = [WinInput]::InputDesktopName()
    } }
}

function DoType($cmd) {
    $text = ''
    if ($cmd.text) { $text = [string]$cmd.text }
    $t0 = NowUs
    # SECURITY: never log/echo/return this value.
    [WinInput]::TypeText($text) | Out-Null
    $t1 = NowUs
    return @{ ok = $true; data = @{
        action = 'type'
        typed = $text.Length
        us = ($t1 - $t0)
        qpc_us = $t1
    } }
}

function DoKey($cmd) {
    $act = 'key'
    if ($cmd.action) { $act = [string]$cmd.action }
    $vk = 0
    if ($cmd.vk) { $vk = [int]$cmd.vk }
    $holdMs = 0
    if ($cmd.hold_ms) { $holdMs = [int]$cmd.hold_ms }
    $t0 = NowUs
    if ($act -eq 'keydown') {
        [WinInput]::KeyVk([uint32]$vk, $false) | Out-Null
    } elseif ($act -eq 'keyup') {
        [WinInput]::KeyVk([uint32]$vk, $true) | Out-Null
    } elseif ($holdMs -gt 0) {
        [WinInput]::KeyVk([uint32]$vk, $false) | Out-Null
        Start-Sleep -Milliseconds $holdMs
        [WinInput]::KeyVk([uint32]$vk, $true) | Out-Null
    } else {
        [WinInput]::TapVk([uint32]$vk) | Out-Null
    }
    $t1 = NowUs
    return @{ ok = $true; data = @{
        action = $act
        vk = $vk
        us = ($t1 - $t0)
        qpc_us = $t1
    } }
}

while ($true) {
    $ctx = $null
    try { $ctx = $listener.GetContext() } catch { Log ("GETCTX_ERR " + $_.Exception.Message); break }
    try {
        $reader = New-Object System.IO.StreamReader($ctx.Request.InputStream, [Text.Encoding]::UTF8)
        $body = $reader.ReadToEnd()
        $reader.Close()
        $cmd = $null
        if ($body -and $body.Trim().Length -gt 0) { $cmd = $body | ConvertFrom-Json }
        $act = 'probe'
        if ($cmd -and $cmd.action) { $act = [string]$cmd.action }

        switch ($act) {
            'probe' {
                Respond $ctx @{ ok = $true; data = @{
                    action = 'probe'
                    session = (Get-Process -Id $PID).SessionId
                    pid = $PID
                    screen = [WinInput]::Screen()
                    cursor = [WinInput]::Cursor()
                    fg = [WinInput]::FgTitle()
                    ts = (Get-Date -Format 'o')
                } }
            }
            'state' { Respond $ctx @{ ok = $true; data = (Get-StateData) } }
            'windows' {
                Respond $ctx @{ ok = $true; data = @{
                    action = 'windows'
                    session = (Get-Process -Id $PID).SessionId
                    wins = [WinInput]::Windows()
                } }
            }
            'focus' {
                $cls = ''; if ($cmd.class) { $cls = [string]$cmd.class }
                $ttl = ''; if ($cmd.title) { $ttl = [string]$cmd.title }
                $exe = ''; if ($cmd.exe) { $exe = [string]$cmd.exe }
                $h = [WinInput]::FindWindowEx2($cls, $ttl, $exe)
                if ($h -eq [IntPtr]::Zero) {
                    Respond $ctx @{ ok = $true; data = @{
                        action = 'focus'; found = $false; was_iconic = $false
                        restored = $false; foreground_ok = $false
                    } }
                } else {
                    $iconic = [WinInput]::IsIconic($h)
                    $restored = $false
                    if ($iconic) {
                        [WinInput]::ShowWindow($h, [WinInput]::SW_RESTORE) | Out-Null
                        $restored = $true
                        Start-Sleep -Milliseconds 300
                    }
                    [WinInput]::BringWindowToTop($h) | Out-Null
                    $setok = [WinInput]::SetForegroundWindow($h)
                    Start-Sleep -Milliseconds 200
                    $fgnow = ([WinInput]::FgHwnd() -eq $h.ToInt64())
                    Respond $ctx @{ ok = $true; data = @{
                        action = 'focus'; found = $true; hwnd = $h.ToInt64()
                        was_iconic = $iconic; restored = $restored
                        set_ok = $setok; foreground_ok = $fgnow
                    } }
                }
            }
            'unlock' { Respond $ctx (DoUnlock $cmd) }
            'launch-chrome' {
                $p = Start-Process -FilePath 'schtasks.exe' -ArgumentList '/Run','/TN','ChromeWinInput' -NoNewWindow -PassThru -Wait
                Respond $ctx @{ ok = ($p.ExitCode -eq 0); data = @{
                    action = 'launch-chrome'; task = 'ChromeWinInput'; exit_code = $p.ExitCode
                } }
            }
            'move' {
                $r = [WinInput]::AbsMove([int]$cmd.x, [int]$cmd.y)
                Respond $ctx @{ ok = ($r -eq 1); data = @{ action = 'move'; cursor = [WinInput]::Cursor() } }
            }
            'click' {
                $btn = 'left'; if ($cmd.button) { $btn = [string]$cmd.button }
                [WinInput]::AbsMove([int]$cmd.x, [int]$cmd.y) | Out-Null
                Start-Sleep -Milliseconds 40
                if ($btn -eq 'right') { [WinInput]::RightDown() | Out-Null; Start-Sleep -Milliseconds 60; [WinInput]::RightUp() | Out-Null }
                else { [WinInput]::LeftDown() | Out-Null; Start-Sleep -Milliseconds 60; [WinInput]::LeftUp() | Out-Null }
                Respond $ctx @{ ok = $true; data = @{ action = 'click'; cursor = [WinInput]::Cursor() } }
            }
            'drag' { Respond $ctx (DoDrag $cmd) }
            'type' { Respond $ctx (DoType $cmd) }
            'key' { Respond $ctx (DoKey $cmd) }
            'keydown' { Respond $ctx (DoKey $cmd) }
            'keyup' { Respond $ctx (DoKey $cmd) }
            'hide' { [WinInput]::HideConsole(); Respond $ctx @{ ok = $true; data = @{ action = 'hide' } } }
            'shutdown' { Respond $ctx @{ ok = $true; data = @{ action = 'shutdown' } }; $listener.Stop(); exit 0 }
            default { Respond $ctx @{ ok = $false; error = ("unknown action: " + $act) } }
        }
        Log (("$act ok"))
    } catch {
        Log ("HANDLE_ERR " + $_.Exception.Message)
        try { Respond $ctx @{ ok = $false; error = $_.Exception.Message } } catch {}
    }
}
