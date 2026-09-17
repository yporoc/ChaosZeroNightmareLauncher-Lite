#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# CZN Launcher Lite —— Chaos Zero Nightmare（STOVE 版）第三方精简启动器
# Copyright (C) 2026 CZN Launcher Lite contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
r"""czn-lite 图形界面（customtkinter，暗色模式）
=================================================================
布局：顶栏（标题 + GitHub/使用说明）｜左：运行日志｜右：操作按钮 + 扫码面板

要点：
  · 业务逻辑全部来自 czn_lite.py，在 worker 线程执行，print 经队列上屏日志窗
  · 二维码来源于本地 qr.png（exe 同目录）：出现即显示、删除即清空，
    点击图片用系统看图器打开；显示尺寸随面板自适应（不写死，任何 DPI 兼容）
  · 启动按钮三态：蓝色可启动 / 任务执行中禁用 / 管道会话建立后「游戏运行中」

运行：py gui.py
打包：build.bat（PyInstaller，依赖全内嵌）
"""
import base64
import ctypes
import io
import json
import os
import queue
import sys
import threading
import time
import contextlib
import customtkinter as ctk
from ctypes import wintypes
from pathlib import Path

import czn_lite as cl          # ← 真逻辑本体(同一目录)

ctk.set_appearance_mode("dark")          # 暗色 only

# ---------------- 配色 ----------------
C_BG        = "#1a1a1c"
C_PANEL     = "#202024"
C_LOG       = "#121214"
C_FG        = "#e4e4e7"
C_DIM       = "#8b8b93"
C_BTN       = "#2b2b30"
C_BTN_HOV   = "#36363c"
C_ACCENT    = "#0a84ff"
C_ACCENT_HV = "#2f97ff"
C_OK        = "#3fb950"

# 日志级别配色（按行前缀，顺序即优先级）
TAG_RULES = [
    ("dbg",  ("[dbg]",),        "#7d8590"),
    ("ok",   ("[+]",),          "#3fb950"),
    ("warn", ("[!]",),          "#d29922"),
    ("err",  ("[x]", "[-]"),    "#f85149"),
    ("info", ("[*]",),          "#58a6ff"),
]


def pick_tag(line: str) -> str:
    for t, prefixes, _c in TAG_RULES:
        if line.startswith(prefixes):
            return t
    return ""


class _LineWriter(io.TextIOBase):
    """把 czn_lite 的 print 逐行接进 GUI 日志队列(线程安全, 行缓冲)。"""

    def __init__(self, sink):
        self._sink = sink
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._sink(line.rstrip("\r"))
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


# ==================== 托盘（可选，默认关闭；零新依赖） ====================
# 纯 pywin32：自建隐藏窗口 + Shell_NotifyIcon。已实测 Tk 的 mainloop 会分发
# 该窗口的消息，所以不需要额外的消息泵，也不需要 pystray 这类新依赖。
class _NOTIFYICONDATA(ctypes.Structure):
    """Shell_NotifyIcon 入参结构（V3）。"""
    _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                ("dwInfoFlags", wintypes.DWORD),
                ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", wintypes.HICON)]


class _Tray:
    """托盘图标。只负责显示图标与接收点击，不含任何业务逻辑。

    ★★ 铁律：WndProc 里**绝对不能调用任何 Tk 方法**。
    Tk 的 mainloop 在阻塞等待消息时会释放 GIL；Windows 正好在这个窗口把托盘
    消息派发进来，pywin32 便带着 NULL 线程状态去执行 Python —— 一旦此时调用
    Tk（哪怕是 app.after），就会触发
        Fatal Python error: PyEval_RestoreThread: ... (the current Python
        thread state is NULL)
    进程直接死。已实测：只跑纯 Python 安全，调用 Tk 必崩。
    所以这里只把「意图」塞进 pending 队列，由 GUI 的 _pump 在正常事件循环里执行。
    """

    _WM_CB = 0x8000 + 1                 # WM_APP + 1
    _ID = 1
    _CLASS = "CznLiteTrayWnd"
    _NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
    _NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x1, 0x2, 0x4, 0x10
    _WM_LBUTTONUP, _WM_RBUTTONUP, _WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203

    def __init__(self, tooltip="CZN Launcher Lite"):
        import win32api
        import win32gui
        self._gui = win32gui
        self._shell = ctypes.windll.shell32
        self._tooltip = tooltip
        self._visible = False
        self._hicon = None
        self.pending = queue.Queue()    # WndProc -> GUI 事件循环的意图队列

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = self._CLASS
        wc.lpfnWndProc = self._wndproc
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass                        # 重复创建时类已存在
        self._hwnd = win32gui.CreateWindow(self._CLASS, "czn-lite-tray", 0,
                                           0, 0, 0, 0, 0, 0, wc.hInstance, None)

    def _wndproc(self, hwnd, msg, wparam, lparam):
        # ★ 这里只允许纯 Python：入队即返回，绝不触碰 Tk（见类文档）
        if msg == self._WM_CB:
            if lparam in (self._WM_LBUTTONUP, self._WM_LBUTTONDBLCLK):
                self.pending.put("restore")
            elif lparam == self._WM_RBUTTONUP:
                self.pending.put("menu")
        return self._gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _icon(self):
        if self._hicon is None:
            import win32con
            self._hicon = self._gui.LoadIcon(0, win32con.IDI_APPLICATION)
        return self._hicon

    def _data(self, tip=None, info=None, title=None):
        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = self._hwnd
        nid.uID = self._ID
        nid.uFlags = self._NIF_MESSAGE | self._NIF_ICON | self._NIF_TIP
        nid.uCallbackMessage = self._WM_CB
        nid.hIcon = self._icon()
        nid.szTip = (tip or self._tooltip)[:127]
        if info:
            nid.uFlags |= self._NIF_INFO
            nid.szInfo = info[:255]
            nid.szInfoTitle = (title or self._tooltip)[:63]
            nid.dwInfoFlags = 0x1       # NIIF_INFO
        return nid

    def show(self, tip=None, info=None, title=None):
        """加/改图标。info 非空时顺带弹一次气泡提示。"""
        op = self._NIM_MODIFY if self._visible else self._NIM_ADD
        ok = bool(self._shell.Shell_NotifyIconW(op,
                                                ctypes.byref(self._data(tip, info, title))))
        self._visible = ok or self._visible
        return ok

    def set_tip(self, tip):
        if self._visible:
            self.show(tip=tip)

    def hide(self):
        if self._visible:
            self._shell.Shell_NotifyIconW(self._NIM_DELETE,
                                          ctypes.byref(self._data()))
            self._visible = False

    def destroy(self):
        """★ 必须先 NIM_DELETE 再销毁窗口，否则托盘里会留下死图标。"""
        self.hide()
        if self._hwnd:
            try:
                self._gui.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self._busy = threading.Event()
        self._cancel = threading.Event()
        self.srv = None                 # cl.PipeServer 引用(停止用)
        self._qr_mtime = None           # QR 即时显示: 上次加载的 mtime
        self._qr_size = None            # 当前已渲染尺寸(防抖)
        self._qr_rendered = None        # 最近一次实际渲染的尺寸(防 Configure 循环)
        self._qr_job = None             # 缩放防抖 after 句柄
        self._in_game = False           # 游戏管道会话进行中(handshake_done)
        self._tray = None               # 托盘（默认不启用）
        self._tray_menu = None          # 托盘右键菜单（需长期持有，见 _tray_popup）
        self._dl_last = 0.0             # 下载进度节流时间戳
        self.var_log = ctk.BooleanVar(value=True)   # 日志窗显示开关
        self.var_qr = ctk.BooleanVar(value=True)    # 二维码面板显示开关
        self.var_tray = ctk.BooleanVar(value=False)  # 托盘模式开关（默认关）

        self.title("ChaosZeroNightmareLauncher-Lite 卡厄斯梦境极简启动器")
        self.geometry("1180x700")
        self.minsize(860, 520)
        self.configure(fg_color=C_BG)
        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)   # 日志列伸缩; QR 列固定宽
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        cl.EVENT_SINK = self._on_event      # 结构化事件实时上屏
        self._build_ui()
        self.after(40, self._pump)
        self._welcome()

    # ================= 基础路径 =================
    def _base_dir(self) -> Path:
        """exe 同目录(冻结) / 脚本同目录(源码) —— qr.png 与日志导出放这里。"""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).parent
        return Path(__file__).parent

    def _qr_path(self) -> Path:
        return self._base_dir() / "qr.png"

    # ================= UI =================
    def _build_ui(self):
        ui_font = ctk.CTkFont("Microsoft YaHei UI", 13)
        title_font = ctk.CTkFont("Microsoft YaHei UI", 24, "bold")

        # ---- 顶栏: 大标题 + 右上角 [Github][使用说明](仿 ctk 样例) ----
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, columnspan=2, sticky="ew", padx=16,
                 pady=(12, 6))
        ctk.CTkLabel(top, text="CZN Launcher Lite", font=title_font, anchor="w",
                     text_color=C_FG).pack(side="left")
        # pack(side=right): 后 pack 的在更左侧 → 从右往左依次为 使用说明、Github、
        # 二维码开关、日志开关
        self.btn_help = ctk.CTkButton(
            top, text="使用说明", command=self._open_help, width=92, height=30,
            font=ctk.CTkFont("Microsoft YaHei UI", 12), fg_color=C_BTN,
            hover_color=C_BTN_HOV, corner_radius=8)
        self.btn_help.pack(side="right", padx=(8, 0))
        self.btn_github = ctk.CTkButton(
            top, text="GitHub", command=self._open_github, width=88, height=30,
            font=ctk.CTkFont("Microsoft YaHei UI", 12), fg_color=C_BTN,
            hover_color=C_BTN_HOV, corner_radius=8)
        self.btn_github.pack(side="right")
        self.chk_qr = ctk.CTkCheckBox(
            top, text="二维码", variable=self.var_qr, command=self._apply_layout,
            width=76, height=30, font=ctk.CTkFont("Microsoft YaHei UI", 12),
            fg_color=C_ACCENT, hover_color=C_ACCENT_HV, corner_radius=8)
        self.chk_qr.pack(side="right", padx=(8, 0))
        self.chk_log = ctk.CTkCheckBox(
            top, text="日志", variable=self.var_log, command=self._apply_layout,
            width=64, height=30, font=ctk.CTkFont("Microsoft YaHei UI", 12),
            fg_color=C_ACCENT, hover_color=C_ACCENT_HV, corner_radius=8)
        self.chk_log.pack(side="right")
        # 托盘开关：默认不勾；勾选=启用托盘模式（需一次确认），决不默认
        self.chk_tray = ctk.CTkCheckBox(
            top, text="托盘", variable=self.var_tray, command=self._toggle_tray,
            width=64, height=30, font=ctk.CTkFont("Microsoft YaHei UI", 12),
            fg_color=C_ACCENT, hover_color=C_ACCENT_HV, corner_radius=8)
        self.chk_tray.pack(side="right")

        # ---- 内容区: 左日志(伸缩) + 右控制/QR 面板(固定宽) ----
        self.log = ctk.CTkTextbox(
            self, fg_color=C_LOG, text_color=C_FG, corner_radius=10,
            font=ctk.CTkFont("Consolas", 13), wrap="word",
            scrollbar_button_color="#333338",
            scrollbar_button_hover_color="#4a4a52",
            padx=12, pady=10)
        self.log.grid(row=1, column=0, sticky="nsew", padx=(16, 8), pady=(0, 10))
        # 日志遮罩: 不透明黑块, 日志开关关闭时盖住整个日志窗(不外泄内容)
        self.log_cover = ctk.CTkFrame(self, fg_color="#0b0b0c", corner_radius=10)
        try:
            self._tag_cfg = self.log.tag_config
        except AttributeError:
            self._tag_cfg = self.log._textbox.tag_configure
        for tag, _prefixes, color in TAG_RULES:
            self._tag_cfg(tag, foreground=color)

        # 右面板: 大按钮置顶(更好按) → 按钮组 → 扫码区
        qr = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=10, width=320)
        qr.grid(row=1, column=1, sticky="nsew", padx=(0, 16), pady=(0, 10))
        qr.grid_propagate(False)
        qr.grid_columnconfigure(0, weight=1)
        qr.grid_columnconfigure(1, weight=1)
        qr.grid_rowconfigure(6, weight=1)        # QR 图像区吃掉余量
        self.qr_panel = qr

        self.btn = {}
        # ★ 大主按钮: 整行加高加粗, 一键全流程
        self.btn["launch"] = ctk.CTkButton(
            qr, text="▶  启动游戏", command=lambda: self._run_task(self._task_launch),
            height=48, font=ctk.CTkFont("Microsoft YaHei UI", 16, "bold"),
            fg_color=C_ACCENT, hover_color=C_ACCENT_HV, corner_radius=10)
        self.btn["launch"].grid(row=0, column=0, columnspan=2, sticky="ew",
                                padx=14, pady=(14, 8))

        def _small(key, text, cmd, row, col):
            b = ctk.CTkButton(qr, text=text, command=cmd, height=36,
                              font=ui_font, fg_color=C_BTN,
                              hover_color=C_BTN_HOV, corner_radius=8)
            b.grid(row=row, column=col, sticky="ew", padx=(14 if col == 0 else 4,
                                                           14 if col == 1 else 4),
                   pady=2)
            self.btn[key] = b

        _small("login",   "续期",      lambda: self._run_task(self._task_renew), 1, 0)
        _small("stop",    "停止",      self._stop,                               1, 1)
        _small("collect", "获取离线信息", lambda: self._run_task(self._task_collect), 2, 0)
        _small("reset",   "清空离线信息", self._confirm_reset,                    2, 1)
        _small("export",  "导出日志",  self._export_log,                           3, 0)
        _small("qrcode",  "扫码登录",  lambda: self._run_task(self._task_qrlogin), 3, 1)
        qr.grid_columnconfigure(1, weight=1)

        # ★ 账号密码登录（整行；二维码区因此下移到 6/7 行）
        _b = ctk.CTkButton(qr, text="账号密码登录",
                           command=lambda: self._run_task(self._task_pwdlogin),
                           height=36, font=ui_font, fg_color=C_BTN,
                           hover_color=C_BTN_HOV, corner_radius=8)
        _b.grid(row=4, column=0, columnspan=2, sticky="ew", padx=14, pady=2)
        self.btn["pwd"] = _b

        # ★ 游戏本体更新（整行两键）：检查更新 / 校验完整性
        _small("update", "检查更新",   lambda: self._run_task(self._task_check_update), 5, 0)
        _small("verify", "校验完整性", lambda: self._run_task(self._task_verify_files), 5, 1)

        # 图像标签: 无文件=完全空白; 尺寸动态适配不写死
        # (旧"扫码登录"文字标题已删 —— 与同名按钮重复, 纯困惑)
        # ★ columnspan=2: 必须跨满两列, 否则二维码被挤进半列宽(实测 217px 缩水)
        self.qr_label = ctk.CTkLabel(qr, text="", fg_color="transparent")
        # 二维码遮罩: 只盖图片区, 不影响上方按钮
        self.qr_cover = ctk.CTkFrame(qr, fg_color="#000000", corner_radius=0)
        self.qr_label.grid(row=6, column=0, columnspan=2, padx=12,
                           pady=(4, 2), sticky="nsew")
        self.qr_label.bind("<Button-1>", self._on_qr_click)   # 点击→打开本地 png
        self.qr_label.bind("<Configure>", self._on_qr_panel_resize)  # 尺寸自校正
        ctk.CTkLabel(qr, text="点击二维码打开本地png",   # 短文案, 防两侧截断
                     font=ctk.CTkFont("Microsoft YaHei UI", 11),
                     text_color=C_DIM, wraplength=272,
                     justify="center").grid(row=7, column=0, columnspan=2,
                                            pady=(4, 12))
        qr.bind("<Configure>", self._on_qr_panel_resize)

        # ---- 状态栏: 左状态 + 右网络路径（点它打开网络设置） ----
        bar = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=8, height=34)
        bar.grid(row=2, column=0, columnspan=2, sticky="ew",
                 padx=16, pady=(0, 14))
        bar.grid_propagate(False)
        bar.grid_columnconfigure(0, weight=1)
        self.status = ctk.CTkLabel(bar, text="状态: 就绪", anchor="w",
                                   text_color=C_FG,
                                   font=ctk.CTkFont("Microsoft YaHei UI", 12))
        self.status.grid(row=0, column=0, sticky="ew", padx=(12, 6))
        self.btn_net = ctk.CTkButton(
            bar, text="网络: —", command=self._open_network, height=24, width=250,
            font=ctk.CTkFont("Microsoft YaHei UI", 12), fg_color="transparent",
            hover_color=C_BTN_HOV, text_color=C_DIM, corner_radius=6)
        self.btn_net.grid(row=0, column=1, sticky="e", padx=(6, 8))
        self._apply_layout()               # 按开关初始状态排布一次

    def _apply_layout(self):
        """按顶栏开关切换黑色遮罩：布局本身永不变动。

        · 日志开关关闭 → 不透明遮罩盖住日志内容（不外泄）
        · 二维码开关关闭 → 遮罩只盖住二维码图片区（各按钮不受影响）"""
        if self.var_log.get():
            self.log_cover.place_forget()
        else:
            self.log_cover.place(in_=self.log, relx=0, rely=0,
                                 relwidth=1, relheight=1)
            self.log_cover.lift()
        if self.var_qr.get():
            self.qr_cover.place_forget()
        else:
            self.qr_cover.place(in_=self.qr_label, relx=0, rely=0,
                                relwidth=1, relheight=1)
            self.qr_cover.lift()

    def _welcome(self):
        self.log_line("[*] czn-lite GUI 就绪")
        self.log_line("[dbg] config      : %s"
                      % (cl._CONFIG_FILE if cl._CONFIG_FILE.exists()
                         else "(仅内置默认值)"))
        for _n in cl.CONFIG_NOTES:
            self.log_line("[!] 配置: %s" % _n)
        self.log_line("[dbg]   install_root: %s"
                      % (cl.INSTALL_ROOT or "未配置 —— 点『获取离线信息』自动探测"))
        self.log_line("[dbg] state.json : %s (凭据 + 设备信息; 删除它 = 退出登录)"
                      % cl.STATE_FILE)
        self.log_line("[dbg] 提示: 启动游戏 = 静默续期/扫码 → 兑换384 → 管道 → 拉起, 一键全流程")
        self._report_route()
        # 配置里预置了托盘时：仍要显式确认一次才真正启用（决不默认生效）
        if cl._cfg_bool("gui", "tray_enabled", default=False):
            self.var_tray.set(True)
            self.after(300, self._toggle_tray)

    # ================= 结构化事件上屏 =================
    def _on_event(self, ev):
        """czn_lite 每记录一条事件就回调一次（可能在 worker 线程）→ 经队列上屏。

        界面日志固定为最细粒度：所有事件、所有字段（含请求头与请求体）全部明文上屏。
        不做分级 —— 别的等级只会丢掉排查所需的信息。
        """
        for line in cl.render_ui(ev):
            self.log_line(line)

    # ================= 网络路径指示 =================
    def _report_route(self):
        """把当前网络路径决策写进日志，并刷新状态栏右侧标签。"""
        try:
            route = cl.resolve_route("https://s-api.onstove.com/")
        except Exception as e:
            self.log_line("[!] 网络路径解析失败：%s" % e)
            self.btn_net.configure(text="网络: 解析失败")
            return
        self.log_line("[*] 网络路径: %s → %s" % (route.source, route.detail))
        target = route.proxies.get("all") or "直连"
        if len(target) > 34:
            target = target[:31] + "…"
        self.btn_net.configure(text="网络: %s" % target)

    def _open_network(self):
        """网络设置：直连 / 系统代理 / 手动代理。写回 config.json 即时生效。"""
        cfg = cl.network_config()
        win = ctk.CTkToplevel(self)
        win.title("网络设置")
        win.geometry("520x420")
        win.minsize(460, 380)
        win.configure(fg_color=C_BG)
        win.transient(self)
        win.grab_set()
        win.after(120, win.lift)
        f = ctk.CTkFont("Microsoft YaHei UI", 12)
        var = ctk.StringVar(value=cfg["mode"] if cfg["mode"] in
                            ("direct", "system", "manual") else "direct")
        box = {"ok": False}

        ctk.CTkLabel(win, text="网络路径", text_color=C_FG,
                     font=ctk.CTkFont("Microsoft YaHei UI", 13, "bold")
                     ).pack(anchor="w", padx=22, pady=(18, 2))
        for value, text in (("direct", "直连（默认；显式锁定，忽略系统代理与环境变量）"),
                            ("system", "跟随系统代理（WinINET / PAC / WPAD）"),
                            ("manual", "手动指定代理")):
            ctk.CTkRadioButton(win, text=text, variable=var, value=value,
                               font=f, fg_color=C_ACCENT,
                               hover_color=C_ACCENT_HV).pack(anchor="w", padx=26, pady=3)

        ctk.CTkLabel(win, text="代理地址（手动模式用，如 http://127.0.0.1:7890 / "
                              "socks5://127.0.0.1:1080）",
                     text_color=C_DIM, font=f, wraplength=460, justify="left"
                     ).pack(anchor="w", padx=22, pady=(12, 2))
        e_url = ctk.CTkEntry(win, width=456, height=32, font=f)
        e_url.insert(0, cfg["manual_url"])
        e_url.pack(padx=22)

        ctk.CTkLabel(win, text="绕过表（分号分隔，支持 * 通配；留空表示不绕过）",
                     text_color=C_DIM, font=f).pack(anchor="w", padx=22, pady=(10, 2))
        e_byp = ctk.CTkEntry(win, width=456, height=32, font=f)
        e_byp.insert(0, cfg["manual_bypass"])
        e_byp.pack(padx=22)

        ctk.CTkLabel(win, text="代理账号 / 密码（可选，仅手动模式；明文存于 config.json）",
                     text_color=C_DIM, font=f).pack(anchor="w", padx=22, pady=(10, 2))
        row = ctk.CTkFrame(win, fg_color="transparent")
        row.pack(padx=22, fill="x")
        e_user = ctk.CTkEntry(row, width=222, height=32, font=f, placeholder_text="用户名")
        e_user.insert(0, cfg["manual_username"])
        e_user.pack(side="left")
        e_pwd = ctk.CTkEntry(row, width=222, height=32, font=f, show="●",
                             placeholder_text="密码")
        e_pwd.insert(0, cfg["manual_password"])
        e_pwd.pack(side="left", padx=(12, 0))

        def ok():
            mode = var.get()
            if mode == "manual" and not e_url.get().strip():
                self.log_line("[!] 手动模式需要填代理地址 —— 已按直连处理")
            cl.save_network_config(mode=mode, manual_url=e_url.get().strip(),
                                   manual_username=e_user.get().strip(),
                                   manual_password=e_pwd.get(),
                                   manual_bypass=e_byp.get().strip())
            box["ok"] = True
            self.log_line("[*] 网络设置已保存并即时生效")
            self._report_route()
            win.grab_release()
            win.destroy()

        def cancel():
            win.grab_release()
            win.destroy()

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=22, pady=(16, 0))
        ctk.CTkButton(bar, text="取消", width=84, fg_color=C_BTN,
                      hover_color=C_BTN_HOV, command=cancel).pack(side="right")
        ctk.CTkButton(bar, text="保存", width=110, fg_color=C_ACCENT,
                      hover_color=C_ACCENT_HV, command=ok).pack(side="right", padx=(0, 8))
        self.wait_window(win)
        return box["ok"]

    # ================= 日志通道 =================
    def log_line(self, msg: str):
        self.q.put(("log", msg))

    def set_status(self, text: str):
        self.q.put(("status", text))

    def _pump(self):
        """UI 线程 40ms 轮询: 批量上屏 + 自动滚底 + QR 监视。
        ★ 整体护栏: 单次异常只记日志, 绝不允许弄死主循环。"""
        try:
            logs, status = [], None
            try:
                while True:
                    kind, payload = self.q.get_nowait()
                    if kind == "log":
                        logs.append(payload)
                    elif kind == "status":
                        status = payload
                    elif kind == "btns":
                        self._set_buttons(payload)     # UI 线程内改控件, 线程安全
            except queue.Empty:
                pass
            if logs:
                at_bottom = self.log.yview()[1] >= 0.999
                for ln in logs:
                    tag = pick_tag(ln)
                    self.log.insert("end", ln + "\n", (tag,) if tag else ())
                # 日志限长：超过上限裁掉最旧的，防止长时间运行后
                # Text 控件越来越大导致界面越来越卡（上限可用 gui.ui_max_log_lines 配置）
                total_lines = int(self.log.index("end-1c").split(".")[0])
                limit = max(200, cl._cfg_int("gui", "ui_max_log_lines", default=2000))
                if total_lines > limit:
                    self.log.delete("1.0", "%d.0" % int(total_lines - limit * 0.75))
                if at_bottom and cl._cfg_bool("gui", "log_autoscroll", default=True):
                    self.log.see("end")
            if status:
                self.status.configure(text=status)
                # 托盘 tooltip 跟随状态，便于隐藏后判断任务是否还在跑
                if self._tray is not None and cl._cfg_bool(
                        "gui", "tray_tooltip_running", default=True):
                    self._tray.set_tip("CZN Launcher Lite —— %s" % status)
            # ★ 游戏会话状态: 管道 handshake_done = 游戏真的在和管道交流
            in_game = bool(self.srv is not None and self.srv.handshake_done.is_set())
            if in_game != self._in_game:
                self._in_game = in_game
                if in_game:
                    self.log_line("[+] 检测到游戏管道会话 —— 游戏运行中")
                    self.set_status("状态: 游戏运行中 (管道保活中)")
                else:
                    self.log_line("[*] 游戏管道会话结束")
                    if not self._busy.is_set():
                        self.set_status("状态: 就绪")
                self._update_launch_btn()
            self._drain_tray()        # ★ 托盘回调只入队，在这里（事件循环内）执行
            self._watch_qr()          # ★ 即时显示: 每 40ms 监视本地 qr.png
        except Exception as e:
            try:
                self.log_line("[x] 内部泵异常（已恢复）：%s" % e)
            except Exception:
                pass
        finally:
            self.after(40, self._pump)   # 放在 finally: 任何异常后泵都继续

    # ================= QR 面板（本地 png 即时显示 / 点击打开 / 空白兜底） =================
    def _watch_qr(self):
        p = self._qr_path()
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._qr_mtime:
            self._qr_mtime = mtime
            self._refresh_qr()

    def _refresh_qr(self):
        """读本地 png → 按面板当前可用尺寸动态渲染; 无文件 = 显示空白。"""
        p = self._qr_path()
        img = None
        if p.exists():
            try:
                from PIL import Image
                from customtkinter import CTkImage
                # ★ [修复 快照残留] PIL 惰性加载会一直握着文件句柄不关,
                #   导致后续 unlink 在 Windows 上被锁失败 → 文件删不掉 →
                #   面板残留已删图片的"快照"。此处确定性关闭句柄后再交给 CTkImage。
                with Image.open(p) as im:
                    im.load()
                    pil = im.copy()
                size = self._qr_display_size()
                img = CTkImage(light_image=pil, size=size)
                self._qr_rendered = size
            except Exception as e:
                self.log_line("[!] QR 渲染失败: %s" % e)
        if img is not None:
            self.qr_label.configure(image=img, text="")
            self.qr_label._qr_img = img          # 保引用防 GC
        else:
            self.qr_label.configure(image=None, text="")   # 文件没有 → 直接空白
            self._qr_rendered = None

    def _qr_display_size(self):
        """由 QR 标签**实际分配到的区域**推导(不写死): 方形边 = min(宽,高) - 8,
        夹在 [140, 460]。先 update_idletasks 落实布局再量, 避免初次渲染量到旧值。"""
        self.qr_label.update_idletasks()
        w = max(self.qr_label.winfo_width(), 160)
        h = max(self.qr_label.winfo_height(), 160)
        s = int(min(w, h)) - 8
        return (max(140, min(s, 460)),) * 2

    def _on_qr_panel_resize(self, _e=None):
        """窗口/面板变化 → 防抖 150ms 后按新尺寸重渲染。"""
        if self._qr_job:
            self.after_cancel(self._qr_job)
        self._qr_job = self.after(150, self._resize_qr_once)

    def _resize_qr_once(self):
        self._qr_job = None
        if self._qr_mtime is not None:
            self._qr_mtime = None        # 强制 _watch_qr 下一拍重渲染
            self._watch_qr()

    def _on_qr_click(self, _e=None):
        """★ 必须点击二维码才打开本地 png; 文件不存在仅提示, 不报错。"""
        p = self._qr_path()
        if p.exists():
            self.log_line("[*] 打开本地二维码: %s" % p)
            os.startfile(str(p))
        else:
            self.log_line("[dbg] 本地暂无 qr.png (面板空白)")
        return "break"

    def _write_qr(self, qr: dict):
        """worker 写入本地 png —— 面板经 _watch_qr 即时上屏。"""
        img = base64.b64decode(qr["image"])
        p = self._qr_path()
        p.write_bytes(img)
        self.log_line("[+] 二维码已生成: %s (%d bytes)" % (p, len(img)))

    def _unlink_qr(self):
        """删除本地二维码。被看图软件等占用时重试; 仍失败**明说**, 不静默吞掉
        (旧版静默吞 OSError → 文件没删成 → 面板一直显示已"删除"的快照)。"""
        p = self._qr_path()
        last = None
        for _attempt in range(3):
            try:
                p.unlink()
                return True
            except FileNotFoundError:
                return True                    # 本来就不在 = 目的已达成
            except OSError as e:
                last = e
                time.sleep(0.2)
        self.log_line("[!] 二维码文件未能删除(可能被看图软件占用, 关掉它再试): "
                      "%s (%s)" % (p, last))
        return False

    # ================= 任务编排（真逻辑, worker 线程） =================
    def _run_task(self, fn):
        if self._busy.is_set():
            self.log_line("[!] 已有任务在运行, 请先停止")
            return
        self._busy.set()
        self._cancel.clear()
        self._set_buttons(running=True)
        self.set_status("状态: 运行中…")

        def wrap():
            try:
                with contextlib.redirect_stdout(_LineWriter(self.log_line)):
                    fn()
            except AssertionError as e:
                self.log_line("[x] 任务断言失败: %s" % e)
                self.set_status("状态: 任务失败")
            except Exception as e:
                self.log_line("[x] 任务异常：%s" % e)
                self.set_status("状态: 任务异常")
            finally:
                self._busy.clear()
                self.q.put(("btns", False))        # 经队列回 UI 线程改按钮

        threading.Thread(target=wrap, daemon=True).start()

    def _set_buttons(self, running: bool):
        """运行中: 只留 停止/导出日志 可点; 启动按钮单独三态管理。"""
        for key, b in self.btn.items():
            if key == "launch":
                continue
            enabled = key in ("stop", "export") or not running
            b.configure(state="normal" if enabled else "disabled")
        self._update_launch_btn()

    def _update_launch_btn(self):
        """启动按钮三态:
           蓝"▶ 启动游戏" = 可启动(常态)
           蓝禁用          = 任务执行中
           灰禁用"游戏运行中" = 游戏管道会话进行中(handshake_done)
           任务结束后恢复蓝色。"""
        if self._in_game:
            self.btn["launch"].configure(
                text="游戏运行中", state="disabled", fg_color="#3a3a40",
                text_color_disabled=C_DIM)
        else:
            self.btn["launch"].configure(
                text="▶  启动游戏", state="normal", fg_color=C_ACCENT)

    def _stop(self):
        # 管道服务常驻复用，停止任务时不销毁它（下次启动直接复用）
        self._cancel.set()
        self.log_line("[*] 已请求停止 (已拉起的游戏进程不受影响)")
        self.set_status("状态: 已请求停止")

    # ================= 托盘（默认关闭；启用需确认；隐藏需第 2 次确认） =================
    def _confirm(self, title, text):
        """模态确认框。★ 不绑定回车 —— 必须显式点「确定」。"""
        win = ctk.CTkToplevel(self)
        win.title(title)
        win.geometry("470x300")
        win.minsize(420, 260)
        win.configure(fg_color=C_BG)
        win.transient(self)
        win.grab_set()
        win.after(120, win.lift)
        box = {"v": False}
        ctk.CTkLabel(win, text=text, text_color=C_FG, justify="left", anchor="nw",
                     font=ctk.CTkFont("Microsoft YaHei UI", 12),
                     wraplength=410).pack(anchor="w", padx=24, pady=(24, 0))
        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=24, pady=(22, 0))

        def _no():
            win.grab_release()
            win.destroy()

        def _yes():
            box["v"] = True
            win.grab_release()
            win.destroy()

        ctk.CTkButton(bar, text="取消", width=84, fg_color=C_BTN,
                      hover_color=C_BTN_HOV, command=_no).pack(side="right")
        ctk.CTkButton(bar, text="确定", width=110, fg_color=C_ACCENT,
                      hover_color=C_ACCENT_HV, command=_yes).pack(side="right",
                                                                  padx=(0, 8))
        self.wait_window(win)
        return box["v"]

    def _toggle_tray(self):
        """顶栏「托盘」开关。启用=第 1 次确认；取消勾选则直接回到「关闭即退出」。"""
        if not self.var_tray.get():
            self._tray_off()
            return
        if not self._confirm(
                "启用托盘模式",
                "启用后：\n"
                "· 点窗口关闭按钮不再退出，而是先询问是否隐藏到托盘\n"
                "· 隐藏后程序继续在后台运行，游戏会话不会中断\n"
                "· 要真正退出：右键托盘图标 → 退出\n\n"
                "确定启用托盘模式吗？"):
            self.var_tray.set(False)
            return
        self._tray_on()

    def _tray_on(self):
        try:
            self._tray = _Tray()
        except Exception as e:
            self._tray = None
            self.var_tray.set(False)
            self.log_line("[x] 托盘启用失败：%s" % e)
            return
        self._tray.show(info="托盘模式已启用 —— 关闭窗口将先询问",
                        title="CZN Launcher Lite")
        self.log_line("[*] 托盘模式已启用：关闭窗口将先询问，再决定是否隐藏")
        self.set_status("状态: 托盘模式已启用")

    def _drain_tray(self):
        """把托盘回调投递的意图在 Tk 事件循环里执行。

        ★ 必须由 _pump（after 回调）调用：WndProc 里碰 Tk 会触发 GIL 致命错误。
        """
        if self._tray is None:
            return
        while True:
            try:
                action = self._tray.pending.get_nowait()
            except queue.Empty:
                return
            if action == "restore":
                self._tray_restore()
            elif action == "menu":
                self._tray_popup()
            elif action == "quit":
                self._tray_quit()

    def _tray_popup(self):
        """托盘右键菜单（Tk 菜单，与主程序同一套 UI）。

        ★ 菜单必须保存在 self 上：tk_popup 是非阻塞的，局部变量在函数返回后
        会被 GC 回收，菜单会立刻消失。
        """
        import tkinter as tk
        try:
            if self._tray_menu is None:
                self._tray_menu = tk.Menu(self, tearoff=0)
                self._tray_menu.add_command(label="显示窗口",
                                            command=self._tray_restore)
                self._tray_menu.add_separator()
                self._tray_menu.add_command(label="退出", command=self._tray_quit)
            self._tray_menu.tk_popup(*self.winfo_pointerxy())
        except Exception as e:
            self.log_line("[!] 托盘菜单打开失败：%s" % e)
        finally:
            try:
                self._tray_menu.grab_release()
            except Exception:
                pass

    def _tray_off(self):
        if self._tray is not None:
            self._tray.destroy()
            self._tray = None
            self.log_line("[*] 托盘模式已关闭：关闭窗口将直接退出")
            self.set_status("状态: 已关闭托盘模式")

    def _tray_restore(self):
        self.deiconify()
        self.lift()
        self.focus_force()
        self.set_status("状态: 已从托盘恢复")

    def _tray_quit(self):
        """托盘菜单「退出」：与关闭窗口走同一条退出路径。"""
        self._really_close()

    def _on_close(self):
        """关闭按钮：托盘开启时先第 2 次确认再隐藏；未开启时行为与原来完全一致。"""
        if self._tray is None:
            self._really_close()
            return
        if cl._cfg_bool("gui", "tray_confirm_on_hide", default=True) and \
                not self._confirm(
                    "隐藏到托盘",
                    "将把窗口隐藏到托盘，程序继续在后台运行。\n"
                    "正在执行的任务与游戏会话不会中断。\n\n"
                    "要真正退出：右键托盘图标 → 退出。\n\n"
                    "确定隐藏到托盘吗？"):
            return
        self.withdraw()
        self._tray.show(
            info="已隐藏到托盘 —— 双击图标恢复，右键菜单可退出"
                 if cl._cfg_bool("gui", "tray_notify_on_hide", default=True)
                 else None,
            title="CZN Launcher Lite")
        self.set_status("状态: 已隐藏到托盘（右键托盘图标可退出）")
        self.log_line("[*] 已隐藏到托盘 —— 程序仍在后台运行，游戏会话保持")

    def _really_close(self):
        """真正的退出路径：置取消 → 停管道 → 摘托盘 → 销毁窗口，进程随即结束。"""
        self._cancel.set()
        cl.EVENT_SINK = None
        # 若右键菜单还挂着，先撤下并释放 grab —— 否则 Tk 可能停在菜单的事件循环里
        if self._tray_menu is not None:
            try:
                self._tray_menu.unpost()
                self._tray_menu.grab_release()
            except Exception:
                pass
        if self.srv is not None:
            self.srv.stop()
            self.srv = None
        if self._tray is not None:
            self._tray.destroy()        # ★ NIM_DELETE 必须先于进程退出
            self._tray = None
        self.destroy()

    # ---- 前置条件检查 (三种登录任务共用; 全部打印到日志) ----
    def _precondition_report(self) -> dict:
        """打印并返回前置条件状态。装路径/loader 是启动硬条件, 凭据是续期硬条件。"""
        st_ok = cl.STATE_FILE.exists()
        has_rt = False
        if st_ok:
            try:
                _st = json.loads(cl.STATE_FILE.read_text(encoding="utf-8"))
                has_rt = bool(_st.get("launcher_refresh"))
            except Exception as e:
                self.log_line("[dbg]   state.json 读取失败: %s" % e)
        loader_ok = cl.loader_probe(cl.INSTALL_ROOT)[0]
        self.log_line("[*] 前置条件检查:")
        self.log_line("[dbg]   state.json    : %s" % ("存在" if st_ok else "缺失"))
        self.log_line("[dbg]   refresh_token : %s"
                      % ("有" if has_rt else "无"))
        self.log_line("[dbg]   install_root  : %s" % (cl.INSTALL_ROOT or "未配置(先获取离线信息)"))
        self.log_line("[dbg]   loader        : %s" % ("存在 ✓" if loader_ok else "缺失 ✗"))
        if cl.NET_PREFLIGHT:
            ok, detail = cl.preflight()
            self.log_line("[%s] 网络预检: %s" % ("+" if ok else "!", detail))
        return {"state": st_ok, "refresh": has_rt, "loader": loader_ok}

    # ---- 任务 1: 续期 (仅续期: 必须已有本地凭据, 与扫码登录职责分离) ----
    def _task_renew(self):
        cl.set_stage("静默续期")
        pre = self._precondition_report()
        if not (pre["state"] and pre["refresh"]):
            self.log_line("[x] 无本地凭据 —— 续期无从谈起, 请点『扫码登录』完成首次登录")
            self.set_status("状态: 无凭据, 请扫码登录")
            return
        self.set_status("状态: 静默续期中…")
        auth = cl.StoveAuth()
        if auth.load():
            self.log_line("[+] 静默续期成功 (新凭据已写回 state.json)")
            self.set_status("状态: 已续期")
        else:
            self.log_line("[x] 续期失败 —— 凭据已失效"
                          "(官方启动器在别处登录过 / 放置过久)。请点『扫码登录』重新登录")
            self.set_status("状态: 续期失败, 请扫码登录")

    # ---- 任务 2: 扫码登录按钮 (用户主动申请二维码 → 即时显示 → 轮询自动登录) ----
    def _task_qrlogin(self):
        cl.set_stage("扫码登录")
        self.log_line("[*] 扫码登录: 全新登录, 不依赖本地凭据 (凭据失效时也用它)")
        self.set_status("状态: 申请二维码…")
        auth = cl.StoveAuth()
        qr = auth.qr_create()
        self._write_qr(qr)               # 面板经 mtime 监视即时上屏
        sess = qr["session"]
        self.log_line("[*] 请用手机 STOVE App 扫码 (二维码已在右侧显示)")
        while not self._cancel.is_set():
            time.sleep(2)
            st = auth.qr_status(sess)
            status = st.get("value", {}).get("status")
            if status == "success":
                auth.signin_qr(sess)
                break
            if status in ("expired", "close"):
                self.log_line("[-] 二维码 %s — 重新生成" % status)
                qr = auth.qr_create()
                self._write_qr(qr)
                sess = qr["session"]
        if self._cancel.is_set():
            self._unlink_qr()
            self.log_line("[*] 扫码已取消")
            self.set_status("状态: 已取消")
            return
        auth.save()
        self._unlink_qr()
        self.log_line("[+] 扫码登录成功 member_no=%s guid=%s(启动器级) nickname=%s"
                      % (auth.member_no, auth.guid, auth.nickname))
        self.set_status("状态: 已登录 (扫码)")

    # ---- 任务 2b: 账号密码登录 ----
    # 验证码无法自动解（click 点选 + rotate 旋转，都是 CV 任务），
    # 因此弹交互窗口由用户作答；协议细节见 captcha.py。
    def _ui_sync(self, fn):
        """worker 线程里调用：切到 UI 线程执行并等返回值。

        Tk 控件只能在 UI 线程碰，而任务跑在 worker 线程，必须这样桥接。
        """
        box, done = {}, threading.Event()

        def run():
            try:
                box["v"] = fn()
            except Exception as e:
                self.log_line("[x] UI 操作异常: %s" % e)
                box["v"] = None
            finally:
                done.set()

        self.after(0, run)
        done.wait()
        return box.get("v")

    def _dialog_credentials(self):
        """UI 线程：弹账号密码输入框，返回 (user_id, password) 或 None。"""
        win = ctk.CTkToplevel(self)
        win.title("账号密码登录")
        win.geometry("440x250")
        win.minsize(380, 230)
        win.configure(fg_color=C_BG)
        win.transient(self)
        win.grab_set()
        win.after(120, win.lift)
        box = {"v": None}
        f = ctk.CTkFont("Microsoft YaHei UI", 12)

        ctk.CTkLabel(win, text="STOVE 账号（邮箱）", text_color=C_DIM,
                     font=f).pack(anchor="w", padx=22, pady=(20, 2))
        e_uid = ctk.CTkEntry(win, width=396, height=34, font=f)
        e_uid.pack(padx=22)

        ctk.CTkLabel(win, text="密码", text_color=C_DIM,
                     font=f).pack(anchor="w", padx=22, pady=(12, 2))
        e_pwd = ctk.CTkEntry(win, width=396, height=34, font=f, show="●")
        e_pwd.pack(padx=22)

        def ok(_e=None):
            u = e_uid.get().strip()
            p = e_pwd.get()
            if not u or not p:
                return
            box["v"] = (u, p)
            win.grab_release()
            win.destroy()

        def cancel():
            win.grab_release()
            win.destroy()

        bar = ctk.CTkFrame(win, fg_color="transparent")
        bar.pack(fill="x", padx=22, pady=(18, 0))
        ctk.CTkButton(bar, text="取消", width=84, fg_color=C_BTN,
                      hover_color=C_BTN_HOV, command=cancel).pack(side="right")
        ctk.CTkButton(bar, text="登录", width=110, fg_color=C_ACCENT,
                      hover_color=C_ACCENT_HV, command=ok).pack(side="right", padx=(0, 8))
        win.bind("<Return>", ok)
        e_uid.focus_set()
        self.wait_window(win)
        return box["v"]

    def _show_captcha(self, step):
        """UI 线程：显示一步验证码，返回 base64 答案。"""
        try:
            from captcha_ui import CaptchaWindow
        except Exception as e:
            self.log_line("[x] 验证码窗口不可用: %s" % e)
            return None
        w = CaptchaWindow(self, step)
        self.wait_window(w)
        return w.result

    def _task_pwdlogin(self):
        cl.set_stage("账号密码登录")
        self.log_line("[*] 账号密码登录（provider_cd=SO）")
        creds = self._ui_sync(self._dialog_credentials)
        if not creds:
            self.log_line("[*] 已取消")
            self.set_status("状态: 就绪")
            return
        uid, pwd = creds

        auth = cl.StoveAuth()
        self.set_status("状态: 账号密码登录中…")
        data = auth.signin_password(uid, pwd)
        code = data.get("code")

        if code in (0, None):
            self._pwdlogin_done(auth)
            return
        if code != 49700:
            self.log_line("[x] 登录失败 code=%s msg=%s"
                          % (code, data.get("message")))
            self.set_status("状态: 登录失败")
            return

        # ---- 49700：需要验证码 ----
        self.log_line("[!] 服务端要求验证码（49700）—— 弹出验证码窗口")
        self.set_status("状态: 等待验证码…")
        try:
            import captcha as cap
        except Exception as e:
            self.log_line("[x] 验证码模块不可用: %s" % e)
            self.set_status("状态: 验证码模块缺失")
            return

        token = cap.solve_login_captcha(auth, ui_ask=self._show_captcha_1,
                                        on_event=self.log_line)
        if not token:
            self.log_line("[x] 验证码未通过")
            self.set_status("状态: 验证码未通过")
            return

        self.log_line("[*] 拿到验证码 token（%d 字符），重试登录…" % len(token))
        data2 = auth.signin_password(uid, pwd, captcha_token=token)
        code2 = data2.get("code")
        if code2 in (0, None):
            self._pwdlogin_done(auth)
            return
        self.log_line("[x] 带验证码仍失败 code=%s msg=%s"
                      % (code2, data2.get("message")))
        if code2 == 49703:
            self.log_line("    code=49703 = captcha token is not valid")
            self.log_line("    ⇒ token 未被接受：可能头名不对，或已过期/被消费")
        self.set_status("状态: 登录失败")

    def _show_captcha_1(self, step):
        """worker 线程里被 captcha 模块调用：转交 UI 线程显示。"""
        return self._ui_sync(lambda: self._show_captcha(step))

    def _pwdlogin_done(self, auth):
        auth.save()
        self.log_line("[+] 账号密码登录成功 member_no=%s guid=%s(启动器级) nickname=%s"
                      % (auth.member_no, auth.guid, auth.nickname))
        self.log_line("[dbg] 凭据已写回 state.json")
        self.set_status("状态: 已登录 (账密)")

    # ================= 游戏本体更新（DPMS） =================
    # 只处理 <install_root>\bin 下的受管文件；资源热更(bin\appdata\cznlive)由游戏自己完成。
    def _upd(self):
        """懒加载 update 模块（放在同目录，缺失时不影响其余功能）。"""
        try:
            import update as upd
            return upd
        except Exception as e:
            self.log_line("[x] 更新模块不可用: %s" % e)
            return None

    def _on_dl_progress(self, delta, done, expect):
        """下载进度节流上屏（默认每 0.4 秒一次）。"""
        now = time.time()
        if now - self._dl_last < 0.4 and done != expect:
            return
        self._dl_last = now
        if expect:
            self.set_status("状态: 下载中 %d/%d KB（%.0f%%）"
                            % (done // 1024, expect // 1024,
                               done * 100.0 / max(1, expect)))
        else:
            self.set_status("状态: 下载中 %d KB" % (done // 1024))

    def _ask_update(self, live, local):
        """确认框必须回 UI 线程弹（worker 线程碰 Tk 会出问题）。"""
        return self._ui_sync(lambda: self._confirm(
            "游戏本体更新",
            "检测到游戏本体新版本 %d（本地 %d）。\n\n"
            "将下载官方补丁并替换游戏目录下 bin 内的受管文件。\n"
            "请先关闭正在运行的游戏。\n\n是否现在更新？" % (live, local)))

    def _do_update(self, upd=None, force=False):
        """执行更新。force=True 用于「校验后修复」—— 版本已最新也要按清单比对一次。"""
        upd = upd or self._upd()
        if upd is None:
            return False
        self.set_status("状态: 游戏本体更新中…")
        self._dl_last = 0.0
        r = upd.update(on_event=self.log_line, cancel=self._cancel.is_set,
                       on_progress=self._on_dl_progress, force=force)
        self.log_line("[%s] %s" % ("+" if r.ok else "x", r.message))
        if r.plan is not None and r.plan.modified:
            self.log_line("[!] 注意：以下 %d 个文件此前不是官方原版，已被覆盖："
                          % len(r.plan.modified))
            for e in r.plan.modified:
                self.log_line("      %s" % e.rel)
        for e, why in r.failed:
            self.log_line("      - %s：%s" % (e.rel, why))
        self.set_status("状态: 更新完成" if r.ok else "状态: 更新失败")
        return r.ok

    def _maybe_update(self):
        """启动流程里的更新检查。返回 True 表示可以继续启动。"""
        upd = self._upd()
        if upd is None:
            return True
        if not cl.INSTALL_ROOT or not os.path.isdir(cl.INSTALL_ROOT):
            self.log_line("[!] 未配置游戏目录，跳过更新检查")
            return True
        self.set_status("状态: 检查游戏本体版本…")
        info = upd.check(on_event=self.log_line)
        if info.get("error"):
            self.log_line("[!] %s" % info["error"])
            return True
        if not info["need_update"]:
            self.log_line("[+] 游戏本体已是最新（%d）" % info["local"])
            return True
        self.log_line("[!] 游戏本体有新版本 %d（本地 %d）"
                      % (info["live"], info["local"]))
        if not cl._cfg_bool("update", "auto_download", default=False):
            if not self._ask_update(info["live"], info["local"]):
                self.log_line("[*] 已跳过更新（可稍后点「检查更新」）")
                return True
        self._do_update(upd)
        return True

    def _task_check_update(self):
        cl.set_stage("检查更新")
        upd = self._upd()
        if upd is None:
            return
        self.set_status("状态: 检查游戏本体版本…")
        info = upd.check(on_event=self.log_line)
        if info.get("error"):
            self.log_line("[x] %s" % info["error"])
            self.set_status("状态: 检查失败")
            return
        if not info["need_update"]:
            self.log_line("[+] 游戏本体已是最新（本地 %d）" % info["local"])
            self.set_status("状态: 已是最新")
            return
        self.log_line("[!] 发现新版本 %d（本地 %d）" % (info["live"], info["local"]))
        if self._ask_update(info["live"], info["local"]):
            self._do_update(upd)
        else:
            self.set_status("状态: 已跳过更新")

    def _task_verify_files(self):
        cl.set_stage("完整性校验")
        upd = self._upd()
        if upd is None:
            return
        self.set_status("状态: 校验游戏本体完整性…")
        r = upd.verify(on_event=self.log_line, cancel=self._cancel.is_set)
        self.log_line("[%s] %s" % ("+" if r.ok else "!", r.message))
        if not r.ok and self._ui_sync(lambda: self._confirm(
                "完整性校验未通过",
                "%s\n\n是否按官方清单重新下载并修复这些文件？" % r.message)):
            self._do_update(upd, force=True)     # 版本可能已是最新，必须 force 才会比对修复
            return
        self.set_status("状态: 校验完成" if r.ok else "状态: 校验发现异常")

    # ---- 任务 3: 启动游戏(全流程一键) ----
    def _task_launch(self):
        cl.set_stage("启动游戏")
        if self._in_game:
            self.log_line("[!] 游戏会话进行中 —— 请先关闭游戏或点停止")
            self.set_status("状态: 游戏已在大厅/运行中")
            return
        pre = self._precondition_report()
        if not pre["loader"]:
            self.log_line("[x] 游戏安装路径/loader 缺失 —— 请先点『获取离线信息』探测, "
                          "或手动修改 config.json → game.install_root")
            self.set_status("状态: loader 缺失")
            return
        auth = cl.StoveAuth()
        if auth.load():
            self.log_line("[+] 静默续期成功")
        else:
            self.set_status("状态: 等待手机扫码…")
            qr = auth.qr_create()
            self._write_qr(qr)
            sess = qr["session"]
            while not self._cancel.is_set():
                time.sleep(2)
                st = auth.qr_status(sess)
                status = st.get("value", {}).get("status")
                if status == "success":
                    auth.signin_qr(sess)
                    break
                if status in ("expired", "close"):
                    self.log_line("[-] 二维码 %s — 重新生成" % status)
                    qr = auth.qr_create()
                    self._write_qr(qr)
                    sess = qr["session"]
            if self._cancel.is_set():
                self._unlink_qr()
                self.log_line("[*] 已取消")
                self.set_status("状态: 已取消")
                return
            auth.save()
            self._unlink_qr()
            self.log_line("[+] 登录成功 member_no=%s guid=%s(启动器级) nickname=%s"
                          % (auth.member_no, auth.guid, auth.nickname))
        if self._cancel.is_set():
            return

        # 兑换游戏级 token (缺此步 = 41002)
        self.set_status("状态: 兑换游戏级 token…")
        gt = auth.game_token()
        if gt:
            auth.apply_game_token(gt)
        else:
            self.log_line("[!] 兑换失败 — 继续, 游戏后端极可能 41002")
        if self._cancel.is_set():
            return

        auth.resolve_guid()
        auth.import_member_fields_from_official_log()
        auth.save()

        # 游戏本体更新：在拉起之前做完自己能做的（可关：config.json 的 update.check_on_launch）
        if cl._cfg_bool("update", "check_on_launch", default=True):
            self._maybe_update()
            if self._cancel.is_set():
                return

        required = cl.build_required_info(auth)
        cl.validate_required_info(required)
        self.log_line("[*] REQUIRED_INFO 自检通过 (%d 字段) access len=%d"
                      % (len(required), len(str(required["access_token"]))))
        env = cl.build_env(auth, {})
        if self._cancel.is_set():
            return

        self.set_status("状态: 开启管道服务…")
        # ★ 管道服务常驻复用（整个 GUI 生命周期只有一个实例）:
        #   每次启动只更新本会话的 REQUIRED_INFO，不销毁重建。
        #   历史教训: 旧实例残留 → 新游戏连到旧会话数据 → LoadUserData 卡断线;
        #   而 stop()+重建 在句柄被占用时又会阻塞 → 二次启动卡死。
        #   常驻单实例同时规避这两个问题。
        if self.srv is None or not self.srv.is_alive():
            self.srv = cl.PipeServer(required)
            self.srv.start()
        else:
            self.srv.info = required           # 同一实例，更新本会话数据
        for _ in range(30):                    # ~3s, 与官方节奏一致; 可随时取消
            if self._cancel.is_set():
                self.log_line("[*] 已取消 (管道保持监听)")
                self.set_status("状态: 已取消")
                return
            time.sleep(0.1)

        self.set_status("状态: 拉起游戏…")
        if cl.launch_game(env):
            self.log_line("[+] 游戏已拉起 (经 ucldr loader)")
            self.log_line("[dbg] 成功判据: %%LOCALAPPDATA%%\\STOVEPCSDK3\\logs\\"
                          "STOVE_CHAOSZERO\\BaseSDK_*.log 出现 Base_SetGameProfileCpp")
            self.set_status("状态: 游戏运行中 (管道保活中)")
        else:
            self.log_line("[x] 启动失败")
            self.set_status("状态: 启动失败")

    # ---- 任务 3: 获取离线信息 (零联网采集 → 实际写入 state.json) ----
    def _task_collect(self):
        cl.set_stage("获取离线信息")
        self.set_status("状态: 采集离线信息 (零联网)…")
        info = cl.collect_local_info()
        # 读-改-写 state.json: 保留已有凭据(launcher_refresh 等), 合并采集值
        st = {}
        if cl.STATE_FILE.exists():
            try:
                st = json.loads(cl.STATE_FILE.read_text(encoding="utf-8"))
            except Exception:
                st = {}
        account_keys = ("guid", "reg_dt", "birth_dt", "member_no",
                        "member_nickname", "country_cd", "account_type",
                        "provider_cd", "person_verify_yn", "parent_verify_yn",
                        "email_verify_yn")
        for k, v in info.items():
            st[k if k in account_keys else "device_" + k] = v
        cl.STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        cl.STATE_FILE.write_text(json.dumps(st, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        self.log_line("[+] 离线信息已写入 json: %s" % cl.STATE_FILE)
        for k, v in info.items():
            self.log_line("[dbg]   %s = %s" % (k, v))
        if "install_root_detected" in info and not info.get("loader_found"):
            # ★ 换设备自主适配: 探测到安装路径 → 写入 config.json 并即时生效
            root = info["install_root_detected"]
            if not root:
                self.log_line("[x] 未能定位游戏目录 —— 请手动修改 config.json 的 "
                              "game.install_root（填包含 bin 子目录的那一层）")
                self.set_status("状态: 未找到游戏目录")
                return
            self.log_line("[*] 检测到安装路径: %s —— 写入 config.json" % root)
            try:
                cl.set_install_root(root)
                self.log_line("[+] 已写入并即时生效 (loader %s)"
                              % ("存在" if os.path.exists(cl.LOADER_EXE) else "仍缺失"))
            except Exception as e:
                self.log_line("[x] 写入 config.json 失败：%s" % e)
        self.set_status("状态: 离线信息已写入 json (零联网)")

    # ---- 任务 4: 清空离线信息 (恢复分发初始状态) ----
    def _confirm_reset(self):
        """UI 线程弹确认框, 通过后才执行删除。"""
        from tkinter import messagebox
        ok = messagebox.askyesno(
            "清空离线信息",
            "将删除本地凭据(state.json)与运行时产物, 恢复到分发初始状态。\n"
            "下次启动需重新扫码登录。确定继续?")
        if ok:
            self._run_task(self._task_reset)

    def _task_reset(self):
        self.set_status("状态: 清空离线信息…")
        removed = []
        # 1) 凭据(exe 同目录) + 旧版主目录遗留
        if cl.STATE_FILE.exists():
            cl.STATE_FILE.unlink()
            removed.append(str(cl.STATE_FILE))
        legacy = Path.home() / ".czn-lite" / "state.json"
        if legacy.exists():
            legacy.unlink()
            removed.append(str(legacy))
        # 2) 配置侧: 本机动态键(install_root)置空回初始态, 常量键不动
        cl.clear_device_config()
        removed.append(str(cl._CONFIG_FILE) + " (game.install_root → 空)")
        # 3) 运行时产物
        for name in ("last_required_info.json", "last_env.json", "qr.png"):
            f = self._base_dir() / name
            if f.exists():
                f.unlink()
                removed.append(str(f))
        self.log_line("[+] 已清除 %d 个本地文件, 恢复到分发初始状态:" % len(removed))
        for r in removed:
            self.log_line("    - %s" % r)
        self.log_line("[*] 下次启动将进入首次登录流程 (QR); "
                      "install_root 已置空, 用『获取离线信息』重新探测安装路径")
        self.set_status("状态: 已恢复初始状态")

    # ---- 日志导出（导出的是「反馈日志」：结构化、全链路、已脱敏） ----
    def _export_log(self):
        """界面日志保持明文（绝对坦诚）；导出的是脱敏后的反馈日志。"""
        try:
            out = cl.export_report(self._base_dir())
        except Exception as e:
            self.log_line("[x] 导出反馈日志失败：%s" % e)
            self.set_status("状态: 导出失败")
            return
        self.log_line("[+] 反馈日志已导出（已脱敏，可直接发送）: %s" % out)
        self.log_line("[dbg] 界面日志为明文（含凭据），外发请用上面这个文件")
        self.set_status("状态: 已导出反馈日志")

    # ---- 右上角: GitHub / 使用说明 (内容均来自 config.json gui 段, 待定可配) ----
    def _open_github(self):
        url = (cl._cfg("gui", "github_url", default="") or "").strip()
        if not url:
            self.log_line("[!] GitHub 地址未配置 —— config.json → gui.github_url")
            self.set_status("状态: GitHub 地址未配置")
            return
        self.log_line("[*] 打开 GitHub: %s" % url)
        import webbrowser
        webbrowser.open(url)
        self.set_status("状态: 已打开 GitHub")

    def _open_help(self):
        """使用说明: 纯文本窗口, 内容 = config.json gui.help_text; 未配置给占位说明。"""
        text = (cl._cfg("gui", "help_text", default="") or "").strip()
        if not text:
            text = ("使用说明待配置。\n\n"
                    "在 config.json 的 \"gui\": { \"help_text\": \"...\" } 里填入\n"
                    "纯文本内容(支持多行), 保存后重新打开本窗口即可。")
        win = ctk.CTkToplevel(self)
        win.title("使用说明")
        win.geometry("640x520")
        win.minsize(420, 320)
        win.after(80, win.lift)          # CTkToplevel 初始偶被主窗遮挡, 抬一下
        tb = ctk.CTkTextbox(win, fg_color=C_LOG, text_color=C_FG, corner_radius=10,
                            font=ctk.CTkFont("Microsoft YaHei UI", 13),
                            wrap="word", padx=14, pady=12,
                            scrollbar_button_color="#333338",
                            scrollbar_button_hover_color="#4a4a52")
        tb.pack(fill="both", expand=True, padx=12, pady=12)
        tb.insert("1.0", text)
        tb.configure(state="disabled")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
