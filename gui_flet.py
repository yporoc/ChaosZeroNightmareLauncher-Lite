#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# CZN Launcher Lite —— Chaos Zero Nightmare（STOVE 版）第三方极简启动器
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
r"""czn-lite 图形界面（Flet / Material 3，暗色模式）
=================================================================
布局：顶栏（标题 + GitHub/使用说明/托盘）｜左：运行日志｜右：启动 + 操作 +
扫码面板；底：状态栏（状态 / 网络）

与旧 customtkinter 版（gui.py）的对应关系：
  · 业务逻辑仍然全部来自 czn_lite.py —— 每个任务原样搬运，只换 UI 壳
    （游戏更新/校验/全新安装不在本界面：需要时用命令行
    `python update.py --check|--verify|--update|--install`）
  · Flet 1.0 是单事件循环模型：同步事件处理器直接跑在循环线程上，
    阻塞会把整个界面冻住。因此：
      - 所有耗时任务经 page.run_thread() 落到 worker 线程
      - worker 只往 queue 里投「意图」（日志行 / 状态 / 按钮态），
        由界面泵（事件循环上的 async 任务）统一应用到控件并批量
        page.update() —— 单线程改 UI，零竞争，补丁按拍合并，高性能
      - worker 需要用户输入（确认框 / 账号密码 / 验证码）时经
        page.run_task(coro).result() 阻塞自己，对话框跑在事件循环上
  · 二维码仍然落本地 qr.png，泵按 mtime 监视 → Image 直接吃字节，
    点击图片用系统看图器打开（与旧版一致）
  · 托盘：Flet 没有 Tk 那种会分发消息的主循环，改为专职线程 +
    GetMessage 消息泵（见 tray_flet.py），意图仍走队列由泵执行
  · 「选择游戏路径」按钮：FilePicker.get_directory_path() 调起的
    就是 Windows 原生目录选择对话框，选定后经 set_install_root
    规范化写回 config.json 即时生效

运行：py gui_flet.py
"""
import asyncio
import base64
import contextlib
import io
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import inspect

import flet as ft
import flet_video

import czn_lite as cl          # ← 真逻辑本体(同一目录)

# ---------------- 配色（现代暗色） ----------------
C_BG        = "#0e1013"        # 页面底色
C_PANEL     = "#16181d"        # 卡片
C_PANEL_2   = "#1b1e25"        # 次级面板
C_LOG       = "#0b0d10"        # 日志底
C_FG        = "#e6e8ee"
C_DIM       = "#8a8f9c"
C_ACCENT    = "#3b82f6"
C_OK        = "#3fb950"
C_ERR       = "#f85149"
C_WARN      = "#d29922"
C_BORDER    = "#23262e"
C_GAME      = "#3a3a40"        # 「游戏运行中」的按钮底色

# 日志级别配色（按行前缀，顺序即优先级）
TAG_RULES = [
    ("dbg",  ("[dbg]",),        "#7d8590"),
    ("ok",   ("[+]",),          C_OK),
    ("warn", ("[!]",),          C_WARN),
    ("err",  ("[x]", "[-]"),    C_ERR),
    ("info", ("[*]",),          "#58a6ff"),
]


def pick_tag(line: str) -> str:
    for t, prefixes, _c in TAG_RULES:
        if line.startswith(prefixes):
            return t
    return ""


TAG_COLOR = {t: c for t, _prefixes, c in TAG_RULES}


def _status_color(text: str) -> str:
    """状态栏指示点配色：绿=游戏中，蓝=进行中，红=异常，灰=空闲。"""
    t = text or ""
    if "游戏运行中" in t:
        return C_OK
    if any(k in t for k in ("失败", "异常", "错误", "未通过", "缺失")):
        return C_ERR
    if any(k in t for k in ("运行中", "下载", "等待", "兑换", "采集",
                            "校验", "检查", "续期", "登录中", "安装")):
        return C_ACCENT
    return C_DIM


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


# ==================== 主应用 ====================
class App:
    """Flet 版主界面。一个实例对应一个 Page（会话）。"""

    def __init__(self, page: ft.Page):
        self.page = page
        self.q = queue.Queue()
        self._busy = threading.Event()
        self._cancel = threading.Event()
        self.srv = None                 # cl.PipeServer 引用(停止用)
        self._qr_mtime = None           # QR 即时显示: 上次加载的 mtime
        self._in_game = False           # 游戏管道会话进行中(handshake_done)
        self._tray = None               # 托盘（默认不启用）
        self._tray_intents: "queue.Queue[str] | None" = None
        self._last_route_ts = 0.0       # 网络标签静默刷新节流
        self._qr_show = True            # 二维码面板显示开关（隐私）
        # 原生文件/目录对话框服务（Task 2）。在 main(page) 上下文里构造，
        # Service 会自动注册到当前页。
        self._picker = ft.FilePicker()

        # ---- 页面基础 ----
        p = self.page
        p.title = "ChaosZeroNightmareLauncher-Lite 卡厄思梦境极简启动器"
        p.bgcolor = C_BG
        p.padding = 0
        p.theme_mode = ft.ThemeMode.DARK
        p.theme = ft.Theme(
            font_family="Microsoft YaHei UI",
            color_scheme_seed=C_ACCENT,
            color_scheme=ft.ColorScheme(
                primary=C_ACCENT, on_primary="#ffffff",
                surface=C_PANEL, on_surface=C_FG,
                surface_container=C_PANEL_2, on_surface_variant=C_DIM,
                outline=C_BORDER,
            ),
            scaffold_bgcolor=C_BG,
            dialog_theme=ft.DialogTheme(bgcolor=C_PANEL),
        )
        # 默认窗口尺寸按启动动画（assets/videos/*.mp4 = 1280x720, 16:9）
        # 反推：日志区宽 = 窗宽-400（边距32+右面板368+间距6），
        # 高 = 窗高-194（顶栏+状态栏+边距）。比例恰好 16:9 时
        # CONTAIN 零黑边零变形；用户拉伸后仅在必要方向出最小对称黑边。
        _LOG_CHROME_W, _LOG_CHROME_H = 435, 236   # 实测标定（含边框/内距）
        _VIDEO_RATIO = 16 / 9
        _default_h = 720
        _default_w = int(_LOG_CHROME_W + (_default_h - _LOG_CHROME_H)
                         * _VIDEO_RATIO) + 2
        p.window.width = _default_w          # ≈1297
        p.window.height = _default_h
        p.window.min_width = 900
        p.window.min_height = 600
        p.window.prevent_close = True       # 关闭走确认/退出流程
        p.window.on_event = self._on_window_event
        p.on_resize = self._on_page_resize  # 二维码图区随窗口高度自适应
        p.on_error = self._on_page_error

        cl.EVENT_SINK = self._on_event      # 结构化事件实时上屏
        self._build_ui()
        p.run_task(self._pump)              # 界面泵（唯一改 UI 的地方之一）
        self._welcome()

    # ================= 基础路径 =================
    def _base_dir(self) -> Path:
        """exe 同目录(冻结) / 脚本同目录(源码) —— qr.png 与日志导出放这里。"""
        if getattr(sys, "frozen", False):
            return Path(sys.executable).parent
        return Path(__file__).parent

    def _qr_path(self) -> Path:
        return self._base_dir() / "qr.png"

    # ================= UI 构建 =================
    def _build_ui(self):
        p = self.page
        self.task_buttons: list[ft.Button] = []   # 运行中禁用的任务按钮

        # ---- 顶栏 ----
        head = ft.Container(
            padding=ft.Padding.symmetric(horizontal=16, vertical=8),
            content=ft.Row([
                ft.Column([
                    ft.Text("CZN Launcher Lite", size=22, weight=ft.FontWeight.BOLD,
                            color=C_FG),
                    ft.Text("Chaos Zero Nightmare · 第三方极简启动器",
                            size=12, color=C_DIM),
                ], spacing=0),
                ft.Container(expand=True),
                ft.TextButton(
                    content=ft.Row([ft.Icon(ft.Icons.CODE, size=16),
                                    ft.Text("GitHub", size=13)],
                                   spacing=5, tight=True),
                    height=34, on_click=self._open_github,
                    style=ft.ButtonStyle(color=C_DIM),
                ),
                ft.TextButton(
                    content=ft.Row([ft.Icon(ft.Icons.HELP_OUTLINE, size=16),
                                    ft.Text("使用说明", size=13)],
                                   spacing=5, tight=True),
                    height=34, on_click=self._open_help,
                    style=ft.ButtonStyle(color=C_DIM),
                ),
                ft.Container(width=4),
                self._tray_switch(ft.Switch(label="托盘", value=False,
                                            scale=0.85,
                                            on_change=self._toggle_tray)),
            ], spacing=8),
        )

        # ---- 日志区（左） ----
        # 滚动语义：auto_scroll 常开 —— 有新日志必然滚底，无新日志不动，
        # 用户随便翻（列表不变化时框架不会碰滚动位置）。
        # 跨行选中：SelectionArea 包住整个日志区，行与行可连续选择；
        # 行级 selectable=True 只能选单行，已弃用。
        # 开关「显示」：只切不透明遮罩的可见性，列表保持挂载 ——
        # visible 直切会把整个 ListView 卸载重建，回来时滚动位置丢失且卡顿。
        self.log_view = ft.ListView(
            expand=True, spacing=1, padding=ft.Padding.all(10),
            auto_scroll=bool(cl._cfg_bool("gui", "log_autoscroll", default=True)),
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        # 日志区两层结构：外层 Stack（fit=EXPAND 强制各层铺满——LOOSE 下
        # 有内容层会收缩成 0 尺寸，出现「播放正常但画面黑」）。
        # 底层留给启动动画视频（懒创建），上层是可选中的日志文本。
        # 「显示」开关只切文本层可见性：动画不受影响，列表保持挂载。
        self.log_text_layer = ft.SelectionArea(
            content=self.log_view, expand=True)
        self.log_stack = ft.Stack(
            [self.log_text_layer], expand=True, fit=ft.StackFit.EXPAND,
            clip_behavior=ft.ClipBehavior.HARD_EDGE,
        )
        self.log_card = ft.Container(
            expand=True,
            bgcolor=C_LOG,
            border_radius=10,
            border=ft.Border.all(1, C_BORDER),
            content=ft.Column([
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                    bgcolor=C_PANEL_2,
                    border_radius=ft.BorderRadius.all(10),
                    content=ft.Row([
                        ft.Icon(ft.Icons.TERMINAL, size=16, color=C_DIM),
                        ft.Text("运行日志", size=12, color=C_DIM),
                        ft.Container(expand=True),
                        ft.Text("显示", size=11, color=C_DIM),
                        ft.Switch(value=True, scale=0.7,
                                  on_change=self._toggle_log_visible),
                        self._txt_btn("导出日志", self._export_log, 86),
                    ], spacing=6),
                ),
                self.log_stack,
            ], spacing=0, expand=True),
        )

        # ---- 右侧控制面板 ----
        # 大主按钮: 一键全流程
        self.btn_launch = ft.FilledButton(
            content=ft.Row([ft.Icon(ft.Icons.PLAY_ARROW, size=22),
                            ft.Text("启动游戏", size=17, weight=ft.FontWeight.BOLD)],
                           alignment=ft.MainAxisAlignment.CENTER, spacing=8),
            height=48, style=ft.ButtonStyle(
                bgcolor=C_ACCENT, color="#ffffff",
                shape=ft.RoundedRectangleBorder(radius=10)),
            on_click=self._on_launch_click,
        )
        self.task_buttons.append(self.btn_launch)

        def small(text, cmd, icon=None, danger=False):
            # expand=True：同一行的两个按钮等分宽度，网格对齐不参差
            b = ft.FilledTonalButton(
                content=ft.Row(
                    ([ft.Icon(icon, size=16, color="#f2888b" if danger else None)]
                     if icon else []) +
                    [ft.Text(text, size=13)],
                    alignment=ft.MainAxisAlignment.CENTER, spacing=5),
                height=36, expand=True, style=ft.ButtonStyle(
                    bgcolor="#3a1d1f" if danger else C_PANEL_2,
                    color="#f2888b" if danger else C_FG,
                    shape=ft.RoundedRectangleBorder(radius=8)),
                on_click=cmd)
            self.task_buttons.append(b)
            return b

        self.btn_stop = small("停止", self._stop, ft.Icons.STOP, danger=True)

        # 游戏路径区（含 Task 2 的「选择游戏路径」按钮）
        # 注意：不能加 expand —— 那是纵向弹性，会把按钮撑成大块；
        # 直接作为列子项它天然横向撑满整行。
        self.btn_choose_path = ft.FilledTonalButton(
            content=ft.Row([ft.Icon(ft.Icons.FOLDER_OPEN, size=16),
                            ft.Text("选择游戏路径", size=13)],
                           alignment=ft.MainAxisAlignment.CENTER, spacing=5),
            height=36, style=ft.ButtonStyle(
                bgcolor="#12233f", color="#8ab4ff",
                shape=ft.RoundedRectangleBorder(radius=8)),
            on_click=self.choose_game_path,
        )
        self.task_buttons.append(self.btn_choose_path)
        self.path_text = ft.Text("", size=11, color=C_DIM, max_lines=2,
                                 overflow=ft.TextOverflow.ELLIPSIS,
                                 expand=True)

        # 二维码面板：卡片 expand 撑满右列剩余高度（底边与左侧日志卡片
        # 严格对齐），图区在卡内也 expand 占满，二维码按 _apply_qr_size
        # 的尺寸居中显示。Image 的 src 必填：无码时先垫 1x1 透明 png。
        # 占位层用四边定位铺满整个图区：图标与文字永远在卡片正中央，
        # 不随二维码尺寸变化上下漂移。
        self.qr_image = ft.Image(
            src=base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="),
            fit=ft.BoxFit.CONTAIN,
            visible=False, gapless_playback=True)
        self.qr_placeholder = ft.Container(
            left=0, right=0, top=0, bottom=0,
            alignment=ft.Alignment.CENTER,
            content=ft.Column([
                ft.Icon(ft.Icons.QR_CODE_2, size=64, color="#2a2e38"),
                ft.Text("登录二维码\n显示在这里", size=12, color=C_DIM,
                        text_align=ft.TextAlign.CENTER),
            ], horizontal_alignment=ft.CrossAxisAlignment.CENTER, spacing=6),
        )
        self.qr_card = ft.Container(
            bgcolor=C_PANEL, border_radius=10,
            border=ft.Border.all(1, C_BORDER),
            padding=8, expand=True,
            content=ft.Column([
                ft.Row([
                    ft.Text("扫码登录", size=12, color=C_DIM),
                    ft.Container(expand=True),
                    ft.Text("显示", size=11, color=C_DIM),
                    ft.Switch(value=True, scale=0.7,
                              on_change=self._toggle_qr_visible),
                ]),
                ft.GestureDetector(
                    on_tap=self._on_qr_click,
                    content=ft.Stack([self.qr_placeholder, self.qr_image],
                                     alignment=ft.Alignment.CENTER),
                    expand=True,
                ),
                ft.Text("点击二维码打开本地png", size=11, color=C_DIM,
                        text_align=ft.TextAlign.CENTER),
            ], spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.CENTER),
        )

        # 游戏路径显示框
        self._path_box = ft.Container(
            padding=ft.Padding.symmetric(horizontal=8, vertical=4),
            bgcolor=C_PANEL, border_radius=8,
            border=ft.Border.all(1, C_BORDER),
            content=ft.Row([
                ft.Icon(ft.Icons.FOLDER, size=14, color=C_DIM),
                self.path_text,
            ], spacing=6),
        )

        right = ft.Container(
            width=368,
            padding=ft.Padding.only(left=6),
            content=ft.Column([
                self.btn_launch,
                ft.Row([small("续期", lambda e: self._run_task(self._task_renew),
                              ft.Icons.AUTORENEW),
                        self.btn_stop], spacing=8),
                ft.Row([small("获取离线信息",
                              lambda e: self._run_task(self._task_collect),
                              ft.Icons.SIM_CARD_DOWNLOAD),
                        small("清空离线信息", self._confirm_reset,
                              ft.Icons.DELETE_SWEEP)], spacing=8),
                ft.Row([small("扫码登录",
                              lambda e: self._run_task(self._task_qrlogin),
                              ft.Icons.QR_CODE),
                        small("账号密码登录",
                              lambda e: self._run_task(self._task_pwdlogin),
                              ft.Icons.PERSON)], spacing=8),
                self.btn_choose_path,
                self._path_box,
                self.qr_card,
            ], spacing=7, expand=True),
        )

        # ---- 状态栏 ----
        self.status_dot = ft.Container(width=8, height=8, border_radius=4,
                                       bgcolor=C_DIM)
        self.status_text = ft.Text("状态: 就绪", size=12.5, color=C_FG,
                                   expand=True, max_lines=1,
                                   overflow=ft.TextOverflow.ELLIPSIS)
        self.btn_net = ft.TextButton(
            content=ft.Row([ft.Icon(ft.Icons.LAN, size=15),
                            ft.Text("网络: —", size=12)],
                           spacing=5, tight=True),
            height=32, on_click=self._open_network,
            style=ft.ButtonStyle(color=C_FG),
        )
        status_bar = ft.Container(
            margin=ft.Margin.only(left=16, right=16, bottom=12),
            padding=ft.Padding.only(left=12, right=8),
            height=42, bgcolor=C_PANEL, border_radius=8,
            border=ft.Border.all(1, C_BORDER),
            content=ft.Row([self.status_dot, self.status_text, self.btn_net],
                           spacing=8, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        )

        # ---- 总装配 ----
        # 主区左右 16px 边距：与顶栏文字、底部状态栏的左边缘对齐，
        # 卡片不贴窗口边。★ 外层 Container 必须 expand=True 撑满页面
        # 剩余高度，里面的 Row 展开才有意义（否则主区塌陷成 0 高）。
        p.add(head,
              ft.Container(
                  margin=ft.Margin.symmetric(horizontal=16),
                  expand=True,
                  content=ft.Row([self.log_card, right], spacing=6, expand=True,
                                 vertical_alignment=ft.CrossAxisAlignment.STRETCH),
              ),
              status_bar)
        self._refresh_path_display()
        self._apply_qr_size(p.width or 1180, p.height or 720)   # 初始图区尺寸
        # 配置里预置了托盘时：仍要显式确认一次才真正启用（决不默认生效）
        if cl._cfg_bool("gui", "tray_enabled", default=False):
            p.run_task(self._tray_enable_flow)

    # ---- 小控件工厂 ----
    def _tray_switch(self, switch):
        """记录托盘开关引用（构造时赋值用）。"""
        self.tray_switch = switch
        return switch

    def _txt_btn(self, text, cmd, width=None):
        b = ft.TextButton(text, height=32, on_click=cmd,
                          style=ft.ButtonStyle(color=C_DIM))
        if width:
            b.width = width
        return b

    # ================= 界面泵 =================
    async def _pump(self):
        """事件循环上的常驻任务：批量应用 worker 投递的意图。

        ★ 全应用只有这里和 async 事件处理器改控件 —— worker 线程
        绝不直接碰 UI。异常只记日志，泵永不退出（护栏与旧版一致）。
        """
        while True:
            try:
                logs, status, btns = [], None, None
                path_dirty = False
                qr_dirty = False
                try:
                    while True:
                        kind, payload = self.q.get_nowait()
                        if kind == "log":
                            logs.append(payload)
                        elif kind == "status":
                            status = payload
                        elif kind == "btns":
                            btns = payload
                        elif kind == "path":
                            path_dirty = True
                        elif kind == "qr":
                            qr_dirty = True
                except queue.Empty:
                    pass

                if logs:
                    self._append_logs(logs)
                    self.log_view.update()
                if status is not None:
                    self._apply_status(status)
                if btns is not None:
                    self._set_buttons(btns)
                if path_dirty:
                    self._refresh_path_display()
                if qr_dirty:
                    self._refresh_qr()

                # ★ 游戏会话状态: 管道 handshake_done = 游戏真的在和管道交流
                in_game = bool(self.srv is not None
                               and self.srv.handshake_done.is_set())
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

                self._drain_tray()        # ★ 托盘意图只入队，这里执行
                self._watch_qr()          # ★ 即时显示: 监视本地 qr.png

                # 网络标签静默刷新（系统代理判定 30s TTL，这里跟节奏走）
                now_mono = time.time()
                if now_mono - self._last_route_ts >= 30:
                    self._last_route_ts = now_mono
                    self._report_route(quiet=True)
            except Exception as e:
                try:
                    self.log_line("[x] 内部泵异常（已恢复）：%s" % e)
                except Exception:
                    pass
            await asyncio.sleep(0.1)

    # ---- 日志 ----
    def _append_logs(self, lines):
        controls = self.log_view.controls
        for ln in lines:
            tag = pick_tag(ln)
            controls.append(ft.Text(ln, size=12.5, font_family="Consolas",
                                    color=TAG_COLOR.get(tag, C_FG)))
        # 日志限长：超过上限裁掉最旧的，防止列表无限增长越跑越卡。
        # 裁剪只与追加同拍发生，auto_scroll 随即滚底，不会看到跳变。
        limit = max(200, cl._cfg_int("gui", "ui_max_log_lines", default=2000))
        if len(controls) > limit:
            del controls[:int(len(controls) - limit * 0.75)]

    def log_line(self, msg: str):
        self.q.put(("log", msg))

    def set_status(self, text: str):
        self.q.put(("status", text))

    def _apply_status(self, text):
        self.status_text.value = text
        self.status_dot.bgcolor = _status_color(text)
        self.status_dot.update()
        self.status_text.update()
        # 托盘 tooltip 跟随状态，便于隐藏后判断任务是否还在跑
        if self._tray is not None and cl._cfg_bool(
                "gui", "tray_tooltip_running", default=True):
            self._tray.set_tip("CZN Launcher Lite —— %s" % text)

    def _on_page_error(self, e):
        self.log_line("[x] 界面异常: %s" % (getattr(e, "data", e)))

    # ---- 按钮态 ----
    def _set_buttons(self, running: bool):
        """运行中: 只留 停止/导出日志/托盘 可点; 启动按钮单独三态管理。"""
        for b in self.task_buttons:
            b.disabled = running
        self.btn_stop.disabled = False
        self.btn_launch.disabled = running or self._in_game
        for b in self.task_buttons + [self.btn_launch]:
            b.update()
        self.btn_stop.update()

    def _update_launch_btn(self):
        """启动按钮三态:
           蓝"启动游戏" = 可启动(常态)；任务执行中禁用；
           灰禁用"游戏运行中" = 管道会话进行中(handshake_done)。"""
        if self._in_game:
            self.btn_launch.style.bgcolor = C_GAME
            self.btn_launch.content.controls = [
                ft.Icon(ft.Icons.SMART_TOY, size=20, color=C_DIM),
                ft.Text("游戏运行中", size=16, weight=ft.FontWeight.BOLD,
                        color=C_DIM)]
            self.btn_launch.disabled = True
        else:
            self.btn_launch.style.bgcolor = C_ACCENT
            self.btn_launch.content.controls = [
                ft.Icon(ft.Icons.PLAY_ARROW, size=22, color="#ffffff"),
                ft.Text("启动游戏", size=17, weight=ft.FontWeight.BOLD,
                        color="#ffffff")]
            self.btn_launch.disabled = self._busy.is_set()
        self.btn_launch.update()

    # ---- 开关 ----
    def _toggle_log_visible(self, e=None):
        # 只隐藏日志文本（视频动画不受影响）。用透明度而非 visible：
        # SelectionArea 强制 content 可见，切 visible 会触发红屏错误。
        self.log_view.opacity = 1.0 if e.control.value else 0.0
        self.log_view.update()

    def _toggle_qr_visible(self, e):
        self._qr_show = bool(e.control.value)
        self._refresh_qr()

    # ================= 欢迎语 =================
    def _welcome(self):
        self.log_line("[*] czn-lite GUI 就绪 (Flet)")
        self.log_line("[dbg] config      : %s"
                      % (cl._CONFIG_FILE if cl._CONFIG_FILE.exists()
                         else "(仅内置默认值)"))
        for _n in cl.CONFIG_NOTES:
            self.log_line("[!] 配置: %s" % _n)
        self.log_line("[dbg]   install_root: %s"
                      % (cl.INSTALL_ROOT or "未配置 —— 点『获取离线信息』自动探测"
                         "或点『选择游戏路径』手动指定"))
        self.log_line("[dbg] state.json : %s (凭据 + 设备信息; 删除它 = 退出登录)"
                      % cl.STATE_FILE)
        self.log_line("[dbg] 提示: 启动游戏 = 静默续期/扫码 → 兑换384 → 管道 → 拉起, 一键全流程")
        self._report_route()

    # ================= 结构化事件上屏 =================
    def _on_event(self, ev):
        """czn_lite 每记录一条事件就回调一次（可能在 worker 线程）→ 经队列上屏。

        界面日志固定为最细粒度：所有事件、所有字段（含请求头与请求体）全部明文上屏。
        """
        for line in cl.render_ui(ev):
            self.log_line(line)

    # ================= 网络路径指示 =================
    def _report_route(self, quiet=False):
        """刷新状态栏右侧网络标签。

        quiet=True（界面泵每 30s 调一次）：标签没变就不动；变了才记一条
        变化日志并更新标签 —— 配合系统代理判定的 30s TTL，用户中途
        开/关系统代理，最多半分钟内状态栏就会跟上。
        """
        try:
            route = cl.resolve_route("https://s-api.onstove.com/")
        except Exception as e:
            self.log_line("[!] 网络路径解析失败：%s" % e)
            self.btn_net.content.controls[1].value = "网络: 解析失败"
            self.btn_net.update()
            return
        target = route.proxies.get("all") or "直连"
        if len(target) > 34:
            target = target[:31] + "…"
        label = "网络: %s" % target
        if quiet and label == getattr(self, "_last_route_label", None):
            return                          # 判定没变，不刷
        if quiet:
            self.log_line("[*] 网络路径变化: %s（%s）" % (label, route.source))
        elif route.detail:
            self.log_line("[*] 网络路径: %s → %s" % (route.source, route.detail))
        self._last_route_label = label
        self.btn_net.content.controls[1].value = label
        self.btn_net.update()

    # ================= 游戏路径显示 =================
    def _refresh_path_display(self):
        root = cl.INSTALL_ROOT or "未配置（获取离线信息 / 选择游戏路径）"
        ok, detail = cl.game_exe_probe(cl.INSTALL_ROOT)
        exe_name = cl.game_exe_name()
        mark = ("✓ %s 存在" % exe_name) if ok else (
            ("✗ %s 缺失" % exe_name) if cl.INSTALL_ROOT else "")
        self.path_text.value = "%s\n%s" % (root, mark) if mark else root
        self.path_text.color = C_FG if ok else C_DIM
        self.path_text.update()

    # ================= 对话框（跑在事件循环上） =================
    def _show_dialog(self, dlg):
        """show_dialog 的防重入外壳：已有对话框在开时如实记日志而不是炸泵。"""
        try:
            self.page.show_dialog(dlg)
        except RuntimeError as e:
            self.log_line("[!] 对话框打开失败（可能已有一个在开）：%s" % e)

    async def _ui_confirm(self, title, text) -> bool:
        """模态确认框。★ 不绑定回车 —— 必须显式点「确定」。"""
        fut = self.page.loop.create_future()

        def resolve(v):
            if not fut.done():
                fut.set_result(v)

        def close(v):
            resolve(v)
            self.page.pop_dialog()

        dlg = ft.AlertDialog(
            modal=True, title=ft.Text(title),
            content=ft.Container(ft.Text(text, size=13), width=430),
            actions=[
                ft.TextButton("取消", on_click=lambda e: close(False)),
                ft.FilledButton("确定", on_click=lambda e: close(True)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        # 对话框被旁路关闭（Esc 等）时也要兑现 future，否则等它的
        # worker 线程会永久阻塞。★ 只 resolve，不再 pop（dismiss 本身
        # 已经在出栈，再 pop 会误关下一个对话框）
        dlg.on_dismiss = lambda e: resolve(False)
        self._show_dialog(dlg)
        return await fut

    async def _ui_credentials(self):
        """账号密码输入框，返回 (user_id, password) 或 None。"""
        fut = self.page.loop.create_future()
        e_uid = ft.TextField(label="STOVE 账号（邮箱）", width=380,
                             autofocus=True)
        e_pwd = ft.TextField(label="密码", width=380, password=True,
                             can_reveal_password=True)

        def resolve(v):
            if not fut.done():
                fut.set_result(v)

        def close(v):
            resolve(v)
            self.page.pop_dialog()

        def ok(_e=None):
            u, p_ = (e_uid.value or "").strip(), e_pwd.value or ""
            if not u or not p_:
                return
            close((u, p_))

        e_uid.on_submit = lambda e: e_pwd.focus()
        e_pwd.on_submit = lambda e: ok()
        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("账号密码登录"),
            content=ft.Column([e_uid, e_pwd], spacing=10, tight=True),
            actions=[
                ft.TextButton("取消", on_click=lambda e: close(None)),
                ft.FilledButton("登录", on_click=ok),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        dlg.on_dismiss = lambda e: resolve(None)
        self._show_dialog(dlg)
        return await fut

    def _credentials_sync(self):
        return self.page.run_task(self._ui_credentials).result()

    async def _ui_show_captcha(self, step):
        """显示一步验证码，返回 base64 答案（None=取消）。协议见 captcha.py。

        click  —— 在场景图上按顺序点选，Stack 上叠圆形标记
        rotate —— 滑杆/拖图旋转，PIL 实时合成预览
        """
        from captcha import encode_click, encode_rotate
        from PIL import Image as PILImage

        fut = self.page.loop.create_future()

        def resolve(v):
            if not fut.done():
                fut.set_result(v)

        def close(v):
            resolve(v)
            self.page.pop_dialog()

        try:
            m_img = _pil_from_bytes(step.m_bytes)
            p_img = _pil_from_bytes(step.p_bytes) if step.p_bytes else None
        except Exception as e:
            self.log_line("[x] 验证码图片解码失败: %s" % e)
            return None
        if m_img is None:
            self.log_line("[x] 缺少场景图 m_url，无法显示")
            return None

        # ---- 展示尺寸：素材很小（178~244px），放大 1.6~3.0 倍 ----
        # avail_w 随窗口宽自适应，保证小窗口下对话框也放得下（最小 900）
        page_w = self.page.width or 1180
        avail_w = max(420, min(640, page_w - 360))
        avail_h = 470
        scale = min(avail_w / m_img.width, avail_h / m_img.height)
        scale = max(1.6, min(scale, 3.0))
        disp_w, disp_h = int(m_img.width * scale), int(m_img.height * scale)

        if step.type == "click":
            tip = ("在左侧场景图里，按从左向右的顺序依次点击所有与右侧「目标形状」"
                   "相同的图案（可点多个，但不要多选和漏选，请注意顺序；"
                   "点错了按「重来」）")
            points: list[tuple[int, int]] = []
            markers = ft.Stack(width=disp_w, height=disp_h)
            state = ft.Text("已点 0 个（点错了按「重来」）", size=12, color=C_DIM)
        elif step.type == "rotate":
            tip = ("拖动下方滑杆（或直接在图上按住左右拖动），把中间圆图转到与背景"
                   "对齐；对齐后点「提交」")
            angle = [0]
            state = ft.Text("当前角度 0°", size=20, weight=ft.FontWeight.BOLD,
                            color=C_OK)
        else:
            self.log_line("[x] 未知验证码类型：%s" % step.type)
            return None

        stage_img = ft.Image(src=_pil_to_png(m_img), width=disp_w,
                             height=disp_h, fit=ft.BoxFit.FILL,
                             gapless_playback=True)

        # rotate：碎片作为独立图层叠在背景中央，旋转走控件 rotate 属性
        # （GPU 合成，滑杆/拖动即时跟手），不再用 PIL 逐帧重编 PNG。
        # 旧实现首帧只画背景（洞是空的），这里图层法天然一开始就在位。
        if step.type == "rotate":
            piece_img = ft.Image(
                src=_pil_to_png(p_img) if p_img is not None else _pil_to_png(m_img),
                width=int(p_img.width * scale), height=int(p_img.height * scale),
                fit=ft.BoxFit.FILL,
                rotate=0.0, gapless_playback=True)
            stage_content = [stage_img, piece_img]
        else:
            piece_img = None
            stage_content = [stage_img, markers]

        stage = ft.GestureDetector(
            content=ft.Stack(stage_content, alignment=ft.Alignment.CENTER,
                             clip_behavior=ft.ClipBehavior.HARD_EDGE),
            width=disp_w, height=disp_h,
        )

        def redraw_markers():
            markers.controls.clear()
            for i, (x, y) in enumerate(points, 1):
                cx, cy = x * scale, y * scale
                markers.controls.append(ft.Container(
                    width=26, height=26, left=cx - 13, top=cy - 13,
                    border_radius=13,
                    border=ft.Border.all(3, "#ff3b30"),
                    alignment=ft.Alignment.CENTER,
                    content=ft.Text(str(i), size=12, color="#ff3b30",
                                    weight=ft.FontWeight.BOLD),
                ))
            markers.update()
            state.value = "已点 %d 个（点错了按「重来」）" % len(points)
            state.update()

        def on_tap(e: ft.TapEvent):
            if step.type != "click":
                return
            x = int(round(e.local_position.x / scale))
            y = int(round(e.local_position.y / scale))
            if 0 <= x < m_img.width and 0 <= y < m_img.height:
                points.append((x, y))
                redraw_markers()

        def on_drag(e: ft.DragUpdateEvent):
            if step.type != "rotate":
                return
            set_angle(angle[0] + e.local_delta.x * 0.8)

        stage.on_tap_up = on_tap
        stage.on_horizontal_drag_update = on_drag

        # ---- 右侧面板：目标形状 / 角度滑杆 ----
        side_controls = []
        if p_img is not None:
            p_work = p_img
            if p_work.mode == "RGBA":
                bb = p_work.split()[-1].getbbox()
                if bb and (bb[2] - bb[0]) > 2 and (bb[3] - bb[1]) > 2:
                    p_work = p_work.crop(bb)          # 裁掉透明边距
            tgt_w = 248
            tgt_h = max(1, int(p_work.height * tgt_w / p_work.width))
            if tgt_h > 300:
                tgt_h = 300
                tgt_w = max(1, int(p_work.width * tgt_h / p_work.height))
            pv = p_work.resize((tgt_w, tgt_h))
            bg = PILImage.new("RGB", pv.size, (255, 255, 255))
            if pv.mode == "RGBA":
                bg.paste(pv, (0, 0), pv)
            else:
                bg.paste(pv, (0, 0))
            side_controls += [
                ft.Text("目标形状", size=12, color=C_DIM),
                ft.Image(src=_pil_to_png(bg), width=tgt_w, height=tgt_h),
            ]
        slider = None
        if step.type == "rotate":
            # 与目标形状图同宽：轨道长一点，微调角度更准
            slider = ft.Slider(min=0, max=359, divisions=359, value=0,
                               width=248, label="{value}°")
            side_controls += [
                ft.Text("角度", size=12, color=C_DIM),
                state,
                slider,
                ft.Text("也可以直接在左侧图上按住拖动旋转", size=11, color=C_DIM),
            ]
        else:
            side_controls += [state]

        def set_angle(value):
            """统一入口：滑杆与图上拖动都走这里，只改图层旋转角（即时跟手）。"""
            angle[0] = int(value) % 360
            if piece_img is not None:
                piece_img.rotate = angle[0] * 3.141592653589793 / 180.0
                piece_img.update()
            if slider is not None and int(slider.value or 0) != angle[0]:
                slider.value = angle[0]
                slider.update()
            state.value = "当前角度 %d°" % angle[0]
            state.update()

        def on_slider(e):
            set_angle(e.control.value)

        def reset(_e=None):
            if step.type == "click":
                points.clear()
                redraw_markers()
            else:
                set_angle(0)

        if slider is not None:
            slider.on_change = on_slider

        def submit(_e=None):
            if step.type == "click":
                if not points:
                    state.value = "至少点选一个位置"
                    state.update()
                    return
                close(encode_click(points))
            else:
                close(encode_rotate(angle[0]))

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("验证码 — %s（第 %d/%d 步）"
                          % (step.type, step.current_step, step.total_steps),
                          size=15),
            content=ft.Container(
                width=min(disp_w + 300, page_w - 40),
                content=ft.Column([
                    ft.Text(tip, size=12.5, color=C_FG),
                    ft.Row([
                        stage,
                        ft.Column(side_controls, spacing=8, tight=True),
                    ], spacing=16, vertical_alignment=ft.CrossAxisAlignment.START),
                ], spacing=10, tight=True),
            ),
            actions=[
                ft.TextButton("重来", width=88, height=40, on_click=reset),
                ft.Container(width=12),
                ft.TextButton("取消", width=88, height=40,
                              on_click=lambda e: close(None)),
                ft.Container(width=12),
                ft.FilledButton("提交", width=132, height=40, on_click=submit),
            ],
            actions_padding=ft.Padding.only(left=8, right=8, bottom=10),
            actions_alignment=ft.MainAxisAlignment.END,
        )
        # 只 resolve 不 pop：dismiss 本身在出栈，再 pop 会误关后续对话框
        dlg.on_dismiss = lambda e: resolve(None)
        self._show_dialog(dlg)
        return await fut

    def _show_captcha_1(self, step):
        """worker 线程里被 captcha 模块调用：弹 Flet 验证码对话框并等待。"""
        return self.page.run_task(self._ui_show_captcha, step).result()

    # ================= 网络设置 =================
    async def _open_network(self, e=None):
        """网络设置：直连 / 系统代理 / 手动代理。写回 config.json 即时生效。"""
        cfg = cl.network_config()
        e_url = ft.TextField(label="代理地址（手动模式用，如 http://127.0.0.1:7890 / "
                                   "socks5://127.0.0.1:1080）",
                             value=cfg["manual_url"], width=460)
        e_byp = ft.TextField(label="绕过表（分号分隔，支持 * 通配；留空表示不绕过）",
                             value=cfg["manual_bypass"], width=460)
        e_user = ft.TextField(label="用户名", value=cfg["manual_username"],
                              width=224)
        e_pwd = ft.TextField(label="密码", value=cfg["manual_password"],
                             width=224, password=True, can_reveal_password=True)

        def radio(value, text):
            return ft.Radio(value=value, label=text)

        group = ft.RadioGroup(
            value=cfg["mode"] if cfg["mode"] in ("direct", "system", "manual")
            else "direct",
            content=ft.Column([
                radio("direct", "直连（默认；显式锁定，忽略系统代理与环境变量）"),
                radio("system", "跟随系统代理（WinINET / PAC / WPAD）"),
                radio("manual", "手动指定代理"),
            ], spacing=4, tight=True),
        )

        def save(_e=None):
            mode = group.value or "direct"
            if mode == "manual" and not (e_url.value or "").strip():
                self.log_line("[!] 手动模式需要填代理地址 —— 已按直连处理")
            cl.save_network_config(mode=mode, manual_url=(e_url.value or "").strip(),
                                   manual_username=(e_user.value or "").strip(),
                                   manual_password=e_pwd.value or "",
                                   manual_bypass=(e_byp.value or "").strip())
            self.log_line("[*] 网络设置已保存并即时生效")
            self._report_route()
            self.page.pop_dialog()

        dlg = ft.AlertDialog(
            modal=True, title=ft.Text("网络设置"),
            content=ft.Container(
                width=500,
                content=ft.Column([
                    ft.Text("网络路径", size=13, weight=ft.FontWeight.BOLD),
                    group,
                    e_url, e_byp,
                    ft.Row([e_user, e_pwd], spacing=10),
                    ft.Text("代理账号/密码（可选，仅手动模式；明文存于 config.json）",
                            size=11, color=C_DIM),
                ], spacing=8, tight=True, scroll=ft.ScrollMode.AUTO, height=330),
            ),
            actions=[
                ft.TextButton("取消", on_click=lambda e: self.page.pop_dialog()),
                ft.FilledButton("保存", on_click=save),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self._show_dialog(dlg)

    # ================= 使用说明 / GitHub =================
    def _open_github(self, e=None):
        url = (cl._cfg("gui", "github_url", default="") or "").strip()
        if not url:
            self.log_line("[!] GitHub 地址未配置 —— config.json → gui.github_url")
            self.set_status("状态: GitHub 地址未配置")
            return
        self.log_line("[*] 打开 GitHub: %s" % url)
        import webbrowser
        webbrowser.open(url)
        self.set_status("状态: 已打开 GitHub")

    async def _open_help(self, e=None):
        """使用说明: 内容 = config.json gui.help_text; 未配置给占位说明。"""
        text = (cl._cfg("gui", "help_text", default="") or "").strip()
        if not text:
            text = ("使用说明待配置。\n\n"
                    "在 config.json 的 \"gui\": { \"help_text\": \"...\" } 里填入\n"
                    "纯文本内容(支持多行), 保存后重新打开本窗口即可。")
        dlg = ft.AlertDialog(
            title=ft.Text("使用说明"),
            content=ft.Container(
                width=620, height=440,
                content=ft.Column([ft.Text(text, size=13, selectable=True)],
                                  scroll=ft.ScrollMode.AUTO, tight=False),
            ),
            actions=[ft.FilledButton("关闭", on_click=lambda e: self.page.pop_dialog())],
        )
        self._show_dialog(dlg)

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

        self.page.run_thread(wrap)

    def _stop(self, e=None):
        # 管道服务常驻复用，停止任务时不销毁它（下次启动直接复用）
        self._cancel.set()
        self.log_line("[*] 已请求停止 (已拉起的游戏进程不受影响)")
        self.set_status("状态: 已请求停止")

    # ---- 前置条件检查 (三种登录任务共用; 全部打印到日志) ----
    def _precondition_report(self) -> dict:
        """打印并返回前置条件状态。装路径/主程序是启动硬条件, 凭据是续期硬条件。"""
        st_ok = cl.STATE_FILE.exists()
        has_rt = False
        if st_ok:
            try:
                _st = json.loads(cl.STATE_FILE.read_text(encoding="utf-8"))
                has_rt = bool(_st.get("launcher_refresh"))
            except Exception as e:
                self.log_line("[dbg]   state.json 读取失败: %s" % e)
        exe_ok = cl.game_exe_probe(cl.INSTALL_ROOT)[0]
        self.log_line("[*] 前置条件检查:")
        self.log_line("[dbg]   state.json    : %s" % ("存在" if st_ok else "缺失"))
        self.log_line("[dbg]   refresh_token : %s" % ("有" if has_rt else "无"))
        self.log_line("[dbg]   install_root  : %s" % (cl.INSTALL_ROOT or "未配置(先获取离线信息)"))
        self.log_line("[dbg]   %s : %s" % (cl.game_exe_name(),
                                           "存在 ✓" if exe_ok else "缺失 ✗"))
        if cl.NET_PREFLIGHT:
            ok, detail = cl.preflight()
            self.log_line("[%s] 网络预检: %s" % ("+" if ok else "!", detail))
        return {"state": st_ok, "refresh": has_rt, "game_exe": exe_ok}

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

    # ---- 任务 2: 扫码登录 (申请二维码 → 即时显示 → 轮询自动登录) ----
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
    def _task_pwdlogin(self):
        cl.set_stage("账号密码登录")
        self.log_line("[*] 账号密码登录（provider_cd=SO）")
        creds = self._credentials_sync()
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

    def _pwdlogin_done(self, auth):
        auth.save()
        self.log_line("[+] 账号密码登录成功 member_no=%s guid=%s(启动器级) nickname=%s"
                      % (auth.member_no, auth.guid, auth.nickname))
        self.log_line("[dbg] 凭据已写回 state.json")
        self.set_status("状态: 已登录 (账密)")

    # ---- 任务 3: 启动游戏(全流程一键) ----
    def _task_launch(self):
        cl.set_stage("启动游戏")
        if self._in_game:
            self.log_line("[!] 游戏会话进行中 —— 请先关闭游戏或点停止")
            self.set_status("状态: 游戏已在大厅/运行中")
            return
        pre = self._precondition_report()
        if not pre["game_exe"]:
            self.log_line("[x] 游戏安装路径/%s 缺失 —— 请先点『获取离线信息』探测, "
                          "点『选择游戏路径』手动指定, 或改 config.json → game.install_root"
                          % cl.game_exe_name())
            self.set_status("状态: %s 缺失" % cl.game_exe_name())
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

        # 启动流程不碰更新：检查与下载一律由「下载游戏资源与更新」手动触发

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
            self.log_line("[+] 游戏已拉起 (直接启动主程序)")
            self.log_line("[dbg] 成功判据: %%LOCALAPPDATA%%\\STOVEPCSDK3\\logs\\"
                          "STOVE_CHAOSZERO\\BaseSDK_*.log 出现 Base_SetGameProfileCpp")
            self.set_status("状态: 游戏运行中 (管道保活中)")
        else:
            self.log_line("[x] 启动失败")
            self.set_status("状态: 启动失败")

    # ---- 任务 4: 获取离线信息 (零联网采集 → 实际写入 state.json) ----
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
        if "install_root_detected" in info and not info.get("game_exe_found"):
            # ★ 换设备自主适配: 探测到安装路径 → 写入 config.json 并即时生效
            root = info["install_root_detected"]
            if not root:
                self.log_line("[x] 未能定位游戏目录 —— 请点『选择游戏路径』手动指定，"
                              "或手动修改 config.json 的 game.install_root"
                              "（填包含 bin 子目录的那一层）")
                self.set_status("状态: 未找到游戏目录")
                return
            self.log_line("[*] 检测到安装路径: %s —— 写入 config.json" % root)
            try:
                cl.set_install_root(root)
                self.log_line("[+] 已写入并即时生效 (%s %s)"
                              % (cl.game_exe_name(),
                                 "存在" if os.path.exists(cl.GAME_EXE_PATH) else "仍缺失"))
            except Exception as e:
                self.log_line("[x] 写入 config.json 失败：%s" % e)
        self.q.put(("path", None))
        self.set_status("状态: 离线信息已写入 json (零联网)")

    # ---- 任务 5: 清空离线信息 (恢复分发初始状态) ----
    async def _confirm_reset(self, e=None):
        """UI 线程弹确认框, 通过后才执行删除。"""
        ok = await self._ui_confirm(
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
        self.q.put(("path", None))
        self.q.put(("qr", None))     # 立即刷新二维码面板（文件已删，显示占位）
        self.set_status("状态: 已恢复初始状态")

    # ---- 日志导出（导出的是「反馈日志」：结构化、全链路、已脱敏） ----
    def _export_log(self, e=None):
        """界面日志保持明文（绝对坦诚）；导出的是脱敏后的反馈日志。
        放 worker 线程执行，避免大日志渲染卡住事件循环。"""
        def work():
            try:
                out = cl.export_report(self._base_dir())
            except Exception as exc:
                self.log_line("[x] 导出反馈日志失败：%s" % exc)
                self.set_status("状态: 导出失败")
                return
            self.log_line("[+] 反馈日志已导出（已脱敏，可直接发送）: %s" % out)
            self.log_line("[dbg] 界面日志为明文（含凭据），外发请用上面这个文件")
            self.set_status("状态: 已导出反馈日志")
        self.page.run_thread(work)

    # ================= 启动动画（日志区背景，8 秒，纯装饰） =================
    _VIDEO_SECONDS = 8

    def _ensure_launch_video(self):
        """首次点击「启动游戏」才创建播放器并插入日志 Stack 底层
        （应用启动路径完全不碰它，不拖慢启动）。层级：
        视频 → 半透明遮罩（保日志可读）→ 日志文本。"""
        if getattr(self, "_video", None) is not None:
            return
        self._video_paths = []
        for name in ("launch1.mp4", "launch2.mp4"):
            p = self._base_dir() / "assets" / "videos" / name
            if p.exists():
                self._video_paths.append("/videos/" + name)   # assets 资源路径
        if not self._video_paths:
            self.log_line("[!] 启动动画素材缺失（assets/videos/*.mp4），跳过")
            raise RuntimeError("无启动动画素材")
        self._video = flet_video.Video(
            playlist=[flet_video.VideoMedia(resource=self._video_paths[0])],
            # NONE=播一次停在末帧；SINGLE 在 media_kit 语义里是单曲循环，
        # 会导致播完重播开头几十帧才被暂停
            playlist_mode=flet_video.PlaylistMode.NONE,
            autoplay=False, muted=False, controls=None,
            fit=ft.BoxFit.CONTAIN, fill_color=ft.Colors.BLACK,
            visible=False, expand=True,
        )
        stack = self.log_stack
        stack.controls.insert(0, self._video)   # 视频在最底层，文本在上
        stack.update()

    async def _video_call(self, name, *args):
        """flet_video 方法防御式调用：个别内部路径会提前 return（返回
        None），直接 await 就是「NoneType can't be used in await」红屏；
        这里只 await 真协程，同步返回值一律忽略。"""
        result = getattr(self._video, name)(*args)
        if inspect.iscoroutine(result):
            return await result
        return result

    async def _play_launch_video(self):
        """播放至素材自然结束（上限 15s 防呆）后自动隐藏；
        期间再次点击启动则换素材重播（token 抢占）。"""
        try:
            self._ensure_launch_video()
        except Exception as e:
            self.log_line("[!] 启动动画不可用：%s" % e)
            return
        self._video_token = getattr(self, "_video_token", 0) + 1
        token = self._video_token
        try:
            idx = (getattr(self, "_launch_video_index", -1) + 1)                 % len(self._video_paths)
            self._launch_video_index = idx
            self._video.playlist = [flet_video.VideoMedia(
                resource=self._video_paths[idx])]
            self._video.visible = True
            self._video.update()
            # media_kit 竞态：加载完成前调 play() 会被忽略（实测 is_playing
            # 恒 False）。等 duration 读到正值（媒体已加载）再播。
            duration_s = 8.0
            for _ in range(50):                  # 最多等 5s
                try:
                    d = await self._video_call("get_duration")
                    secs = int(getattr(d, "in_seconds", 0) or 0)
                except Exception:
                    secs = 0
                if secs > 0:
                    duration_s = float(secs)
                    break
                await asyncio.sleep(0.1)
            await self._video_call("play")
            # 播完再收：按媒体实际时长留 0.6s 余量，上限 15s 防呆
            await asyncio.sleep(min(duration_s + 0.6, 15.0))
            if token == self._video_token:      # 期间没有被新一轮抢占
                await self._video_call("pause")
                self._video.visible = False
                self._video.update()
        except Exception as e:
            self.log_line("[!] 启动动画播放失败：%s" % e)

    async def _on_launch_click(self, e=None):
        """「启动游戏」点击：动画协程与真实任务并行，互不阻塞。"""
        self.page.run_task(self._play_launch_video)
        self._run_task(self._task_launch)

    # ================= 二维码面板（本地 png 即时显示 / 点击打开） =================
    def _on_page_resize(self, e):
        """窗口尺寸变化 → 重算二维码图区（事件对象带新 width/height）。"""
        self._apply_qr_size(e.width, e.height)

    def _apply_qr_size(self, page_w, page_h):
        """二维码图区占满右列剩余空间，夹在 [140, 460]（与旧版一致）。

        510 = 页面纵向固定占用：顶栏 ~90 + 状态栏 ~54 + 右列按钮组与卡片
        外壳 ~340 + 余量。算出 140 以下时右列回退滚动，不会裁内容。
        """
        try:
            size = int(max(140, min(page_h - 510, 460)))
        except (TypeError, ValueError):
            size = 190
        if getattr(self, "_qr_size", None) == size:
            return
        self._qr_size = size
        self.qr_image.width = size
        self.qr_image.height = size
        if self.qr_image.page:          # 未上屏前不调 update
            self.qr_image.update()

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
        """读本地 png → Image 直接吃字节; 无文件/面板开关关闭 = 显示占位。"""
        p = self._qr_path()
        img = None
        if self._qr_show and p.exists():
            try:
                img = p.read_bytes()
            except OSError as e:
                self.log_line("[!] QR 读取失败: %s" % e)
        if img is not None:
            self.qr_image.src = img      # 字节变了必产生新补丁 → 强制重载
            self.qr_image.visible = True
            self.qr_placeholder.visible = False
        else:
            self.qr_image.visible = False
            self.qr_placeholder.visible = True
        self.qr_image.update()
        self.qr_placeholder.update()

    def _on_qr_click(self, e=None):
        """★ 必须点击二维码才打开本地 png; 文件不存在仅提示, 不报错。"""
        p = self._qr_path()
        if p.exists():
            self.log_line("[*] 打开本地二维码: %s" % p)
            os.startfile(str(p))
        else:
            self.log_line("[dbg] 本地暂无 qr.png (面板空白)")

    def _write_qr(self, qr: dict):
        """worker 写入本地 png —— 面板经 mtime 监视即时上屏。"""
        img = base64.b64decode(qr["image"])
        p = self._qr_path()
        p.write_bytes(img)
        self.log_line("[+] 二维码已生成: %s (%d bytes)" % (p, len(img)))

    def _unlink_qr(self):
        """删除本地二维码。被看图软件等占用时重试; 仍失败**明说**, 不静默吞掉。"""
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

    # ================= ★ Task 2: 选择游戏路径（Windows 原生目录对话框） =================
    async def choose_game_path(self, e=None):
        """手动选择游戏安装路径。

        FilePicker.get_directory_path() 在 Windows 桌面端调起的就是系统
        原生目录选择对话框。选定后经 normalize_install_root 规范化（容忍
        指到 bin/ 或 exe），主程序校验，写回 config.json 即时生效。
        """
        if self._busy.is_set():
            self.log_line("[!] 已有任务在运行, 请先停止")
            return
        self.log_line("[*] 选择游戏路径: 请在弹出的窗口中选择游戏安装目录"
                      "（包含 bin 子目录的那一层）")
        try:
            path = await self._picker.get_directory_path(
                dialog_title="选择游戏安装目录（包含 bin 子目录的那一层）",
                initial_directory=cl.INSTALL_ROOT or None)
        except Exception as exc:
            self.log_line("[x] 打开目录选择窗口失败：%s" % exc)
            return
        if not path:
            self.log_line("[*] 已取消选择游戏路径")
            return
        root, notes = cl.normalize_install_root(path)
        for n in notes:
            self.log_line("[dbg]   %s" % n)
        ok, detail = cl.game_exe_probe(root)
        if not ok:
            self.log_line("[!] 该目录下未找到 %s（%s）—— 仍将写入配置；"
                          "请确认选的是包含 bin 子目录的那一层"
                          % (cl.game_exe_name(), detail))
        try:
            cl.set_install_root(root)
        except Exception as exc:
            self.log_line("[x] 写入 config.json 失败：%s" % exc)
            return
        self.log_line("[+] 游戏路径已设定: %s (%s %s)"
                      % (root, cl.game_exe_name(), "存在 ✓" if ok else "缺失 ✗"))
        self.set_status("状态: 游戏路径已设定")
        self._refresh_path_display()

    # ================= 托盘（默认关闭；启用需确认；隐藏需第 2 次确认） =================
    async def _tray_enable_flow(self):
        if not await self._ui_confirm(
                "启用托盘模式",
                "启用后：\n"
                "· 点窗口关闭按钮不再退出，而是先询问是否隐藏到托盘\n"
                "· 隐藏后程序继续在后台运行，游戏会话不会中断\n"
                "· 要真正退出：右键托盘图标 → 退出\n\n"
                "确定启用托盘模式吗？"):
            self.tray_switch.value = False
            self.tray_switch.update()
            return
        self._tray_on()

    def _tray_on(self):
        try:
            from tray_flet import TrayIcon
            self._tray_intents = queue.Queue()
            self._tray = TrayIcon(self._tray_intents,
                                  tooltip="CZN Launcher Lite")
            self._tray.start(info="托盘模式已启用 —— 关闭窗口将先询问",
                             title="CZN Launcher Lite")
        except Exception as e:
            self._tray = None
            self._tray_intents = None
            self.tray_switch.value = False
            self.tray_switch.update()
            self.log_line("[x] 托盘启用失败：%s" % e)
            return
        # 启动时配置预置启用 / 用户确认启用两条路径都把开关拨回正确状态
        self.tray_switch.value = True
        self.tray_switch.update()
        self.log_line("[*] 托盘模式已启用：关闭窗口将先询问，再决定是否隐藏")
        self.set_status("状态: 托盘模式已启用")

    def _tray_off(self):
        if self._tray is not None:
            self._tray.destroy()
            self._tray = None
            self._tray_intents = None
            self.log_line("[*] 托盘模式已关闭：关闭窗口将直接退出")
            self.set_status("状态: 已关闭托盘模式")

    async def _toggle_tray(self, e=None):
        if not self.tray_switch.value:
            self._tray_off()
            return
        await self._tray_enable_flow()

    def _drain_tray(self):
        """把托盘回调投递的意图在界面泵（事件循环）里执行。"""
        if self._tray_intents is None:
            return
        while True:
            try:
                action = self._tray_intents.get_nowait()
            except queue.Empty:
                return
            if action == "restore":
                self._tray_restore()
            elif action == "quit":
                self.page.run_task(self._really_close)
                return

    def _tray_restore(self):
        self.page.window.visible = True
        self.page.window.focused = True
        self.page.update()
        self.set_status("状态: 已从托盘恢复")

    # ================= 窗口关闭 / 退出 =================
    async def _on_window_event(self, e: ft.WindowEvent):
        if e.type != ft.WindowEventType.CLOSE:
            return
        if self._tray is None:
            await self._really_close()
            return
        if cl._cfg_bool("gui", "tray_confirm_on_hide", default=True) and \
                not await self._ui_confirm(
                    "隐藏到托盘",
                    "将把窗口隐藏到托盘，程序继续在后台运行。\n"
                    "正在执行的任务与游戏会话不会中断。\n\n"
                    "要真正退出：右键托盘图标 → 退出。\n\n"
                    "确定隐藏到托盘吗？"):
            return
        self.page.window.visible = False
        self.page.update()
        self._tray.set_tip("CZN Launcher Lite —— %s" % self.status_text.value)
        self.log_line("[*] 已隐藏到托盘 —— 程序仍在后台运行，游戏会话保持")

    async def _really_close(self):
        """真正的退出路径：置取消 → 停管道 → 摘托盘 → 销毁窗口，进程随即结束。"""
        self._cancel.set()
        cl.EVENT_SINK = None
        if self.srv is not None:
            self.srv.stop()
            self.srv = None
        if self._tray is not None:
            self._tray.destroy()        # ★ NIM_DELETE 必须先于进程退出
            self._tray = None
        try:
            await self.page.window.destroy()
        except Exception:
            pass


# ==================== PIL 小工具（验证码合成） ====================
def _pil_from_bytes(data):
    if not data:
        return None
    from PIL import Image
    return Image.open(io.BytesIO(data)).convert("RGBA")


def _pil_to_png(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ==================== 入口 ====================
async def main(page: ft.Page):
    app = App(page)
    await page.window.center()


if __name__ == "__main__":
    # host 显式锁 127.0.0.1：排除 localhost 在 IPv4/IPv6 间解析歧义——
    # 多开时偶发客户端一直停在 Working 的缓解措施之一；端口仍随机。
    ft.run(main, host="127.0.0.1", port=0)
