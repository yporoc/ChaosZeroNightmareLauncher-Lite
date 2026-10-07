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
r"""托盘图标（Flet 版专用，零新依赖）。

与旧 Tk 版的差异：Flet 没有会分发 Windows 消息的主循环，因此这里用
**专职线程** 自建隐藏窗口 + Shell_NotifyIcon + GetMessage 消息泵 ——
这是 Win32 编程的标准做法，比 Tk 版「蹭主循环」的写法更直接。

线程安全铁律（与旧版一致）：
  · WndProc / 消息泵线程里**绝不触碰 UI 框架**，只把「意图」塞进
    pending 队列（restore / menu / quit），由 GUI 的界面泵在事件循环
    内执行。pywin32 消息回调线程没有 Flet 的上下文，碰 UI 必出问题。

菜单用原生 TrackPopupMenu（TPM_RETURNCMD 同步取回选项），托盘线程
自己有消息循环，阻塞式弹出是安全的。
"""
from __future__ import annotations

import ctypes
import queue
import threading
from ctypes import wintypes

WM_CB = 0x8000 + 1                      # WM_APP + 1
_ICON_ID = 1
_CLASS = "CznLiteTrayWndF"
_NIM_ADD, _NIM_MODIFY, _NIM_DELETE = 0, 1, 2
_NIF_MESSAGE, _NIF_ICON, _NIF_TIP, _NIF_INFO = 0x1, 0x2, 0x4, 0x10
_WM_LBUTTONUP, _WM_RBUTTONUP, _WM_LBUTTONDBLCLK = 0x0202, 0x0205, 0x0203
_TPM_RETURNCMD = 0x0100
_TPM_RIGHTBUTTON = 0x0002


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


class TrayIcon:
    """托盘图标：只负责显示与点击意图收集，不含任何业务逻辑。

    用法：
        tray = TrayIcon(pending_queue, tooltip="...")
        tray.start(info="首次气泡", title="...")
        ...  # 界面泵轮询 pending_queue
        tray.destroy()
    """

    def __init__(self, pending: "queue.Queue[str]", tooltip="CZN Launcher Lite"):
        self.pending = pending
        self._tooltip = tooltip
        self._hwnd = None
        self._hicon = None
        self._visible = False
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    # ---- 生命周期 ----
    def start(self, info=None, title=None):
        """启动托盘线程并显示图标。info 非空时弹一次气泡。"""
        import win32api
        import win32con
        import win32gui

        self._hicon = win32gui.LoadIcon(0, win32con.IDI_APPLICATION)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="czn-tray")
        self._thread.start()
        if not self._ready.wait(5):
            raise RuntimeError("托盘窗口初始化超时")
        ok = self._show(_NIM_ADD, info=info, title=title)
        self._visible = ok
        return ok

    def _run(self):
        """托盘线程主体：注册窗口类 → 建窗 → 消息泵。"""
        import win32api
        import win32gui

        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = _CLASS
        wc.lpfnWndProc = self._wndproc
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass                    # 类已存在（重复启用）
        self._hwnd = win32gui.CreateWindow(_CLASS, "czn-lite-tray", 0,
                                           0, 0, 0, 0, 0, 0,
                                           wc.hInstance, None)
        self._ready.set()
        try:
            win32gui.PumpMessages()     # 阻塞在本线程，直到 WM_QUIT
        finally:
            try:
                if self._hwnd:
                    win32gui.DestroyWindow(self._hwnd)
            except Exception:
                pass
            self._hwnd = None

    def _wndproc(self, hwnd, msg, wparam, lparam):
        # ★ 这里只允许纯 Python：入队即返回，绝不触碰 UI 框架
        import win32gui

        if msg == WM_CB:
            if lparam in (_WM_LBUTTONUP, _WM_LBUTTONDBLCLK):
                self.pending.put("restore")
            elif lparam == _WM_RBUTTONUP:
                self._popup_menu(hwnd)
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    def _popup_menu(self, hwnd):
        """右键菜单：显示窗口 / 退出。TPM_RETURNCMD 同步返回选项 id。

        ★ pywin32 的 TrackPopupMenu 是 7 参：
        (hMenu, uFlags, x, y, nReserved, hWnd, prcRect) —— 中间有保留位。"""
        import win32gui

        menu = None
        try:
            menu = win32gui.CreatePopupMenu()
            win32gui.AppendMenu(menu, 0, 1, "显示窗口")
            win32gui.AppendMenu(menu, 0, 2, "退出")
            win32gui.SetForegroundWindow(hwnd)      # 菜单能收起的前提
            x, y = win32gui.GetCursorPos()
            cmd = win32gui.TrackPopupMenu(
                menu, _TPM_RETURNCMD | _TPM_RIGHTBUTTON,
                x, y, 0, hwnd, None)
            win32gui.PostMessage(hwnd, 0x0000, 0, 0)    # WM_NULL，吃掉残留点击
            if cmd == 1:
                self.pending.put("restore")
            elif cmd == 2:
                self.pending.put("quit")
        except Exception:
            pass                                        # 菜单失败不致命
        finally:
            if menu:
                try:
                    win32gui.DestroyMenu(menu)
                except Exception:
                    pass

    # ---- 图标操作 ----
    def _data(self, op=None, tip=None, info=None, title=None):
        nid = _NOTIFYICONDATA()
        nid.cbSize = ctypes.sizeof(_NOTIFYICONDATA)
        nid.hWnd = self._hwnd
        nid.uID = _ICON_ID
        nid.uFlags = _NIF_MESSAGE | _NIF_ICON | _NIF_TIP
        nid.uCallbackMessage = WM_CB
        nid.hIcon = self._hicon
        nid.szTip = (tip or self._tooltip)[:127]
        if info:
            nid.uFlags |= _NIF_INFO
            nid.szInfo = info[:255]
            nid.szInfoTitle = (title or self._tooltip)[:63]
            nid.dwInfoFlags = 0x1       # NIIF_INFO
        return nid

    def _show(self, op, info=None, title=None):
        """★ 必须走 Shell_NotifyIconW 原生 ctypes 调用 —— pywin32 的
        win32gui.Shell_NotifyIcon 只认它自己的 NOTIFYIDENTIFIER 类型，
        传 ctypes 结构体直接 SystemError（与旧 gui.py 同款写法）。"""
        if not self._hwnd:
            return False
        return bool(ctypes.windll.shell32.Shell_NotifyIconW(
            op, ctypes.byref(self._data(info=info, title=title))))

    def set_tip(self, tip: str):
        """跟随状态栏文本更新 tooltip。"""
        if self._visible and self._hwnd:
            try:
                self._show(_NIM_MODIFY, tip=tip)
            except Exception:
                pass

    def hide(self):
        if self._visible and self._hwnd:
            try:
                ctypes.windll.shell32.Shell_NotifyIconW(
                    _NIM_DELETE, ctypes.byref(self._data()))
            except Exception:
                pass
            self._visible = False

    def destroy(self):
        """★ 必须 NIM_DELETE + WM_QUIT 停泵，否则托盘留下死图标。"""
        self.hide()
        if self._thread and self._thread.is_alive():
            import win32api
            import win32gui

            try:
                # 给线程自己的消息队列投递 WM_QUIT，结束 PumpMessages
                win32api.PostThreadMessage(self._thread.ident,
                                           0x0012, 0, 0)     # WM_QUIT
            except Exception:
                pass
            self._thread.join(2)
        self._thread = None
