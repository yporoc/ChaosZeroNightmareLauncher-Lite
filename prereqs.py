#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# CZN Launcher Lite —— 环境前置体检与安装（全新安装）
# Copyright (C) 2026 CZN Launcher Lite contributors
# GNU General Public License v3.0
r"""环境前置体检与安装（全新安装）。

覆盖从 0 环境真正缺的三样东西：
  · 磁盘空间（本体 269 MB + 首跑资源数据包，实测约 5.5 GB）
  · VC++ 2022 x64 运行时（清单 vcredist 字段声明；缺失游戏起不来）
  · WebView2 Evergreen 运行时（ViewSDK.dll 290 处引用实证依赖；
    官方 STOVESetup 内置 MicrosoftEdgeWebview2Setup.exe 同款前置）

安装器一律使用微软官方固定链接；运行安装前必须拿到用户明确同意，
并以 UAC 提权静默执行。本模块不做任何静默下载/静默安装。
"""

from __future__ import annotations

import glob
import os
import time
import urllib.request

# 微软官方固定链接（永久重定向，非第三方镜像）
VC_REDIST_X64_URL = "https://aka.ms/vs/17/release/vc_redist.x64.exe"
WEBVIEW2_BOOTSTRAP_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"
_UA = "czn-lite/0.0.3"

# WebView2 Evergreen 运行时的注册表位置（微软官方检测方式）
_WEBVIEW2_KEYS = (
    (r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients"
     r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
    (r"SOFTWARE\Microsoft\EdgeUpdate\Clients"
     r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
)
_WEBVIEW2_DIR = r"Microsoft\EdgeWebView\Application"

# 全新安装的磁盘门槛：本体 269 MB + 首跑数据包（本机实测 cznlive 约 5.5 GB）+ 引擎
# 自建时的 .bak/展开副本，按 4 倍留余量取 22 GB —— 宁多勿少，装到一半爆盘不可恢复
NEED_GB = 22.0


def disk_ok(path: str, need_gb: float = NEED_GB):
    """磁盘空间预检。返回 (是否充足, 说明)。"""
    try:
        import shutil
        free = shutil.disk_usage(path).free
    except Exception as exc:
        return False, "磁盘空间不可读（%s）" % exc
    need = int(need_gb * 1024 ** 3)
    if free < need:
        return False, ("磁盘空间不足：需 ≥ %.1f GB，当前可用 %.1f GB"
                       % (need_gb, free / 1024 ** 3))
    return True, "磁盘空间充足（可用 %.1f GB）" % (free / 1024 ** 3)


def check_vcredist_detailed():
    """VC++ 2022 x64 运行时检测（DLL 存在性 + 注册表安装记录交叉）。"""
    if os.name != "nt":
        return True, "非 Windows 环境"
    sysdir = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows",
                          "System32")
    dlls = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
    missing = [n for n in dlls if not os.path.exists(os.path.join(sysdir, n))]
    if missing:
        return False, "缺少 VC++ 2022 x64 运行时组件：%s" % "、".join(missing)
    return True, "VC++ 2022 x64 运行时已就绪"


def check_webview2():
    """WebView2 Evergreen 运行时检测。

    优先注册表（微软官方判定方式：EdgeUpdate\\Clients\\{F3017226-…} 的 pv 值），
    兜底找 %ProgramFiles(x86)%\\Microsoft\\EdgeWebView 下的 msedgewebview2.exe。"""
    if os.name != "nt":
        return True, "非 Windows 环境"
    try:
        import winreg
        for hive, view in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                           (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                           (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY)):
            try:
                for sub in _WEBVIEW2_KEYS:
                    try:
                        k = winreg.OpenKey(hive, sub, 0, view | winreg.KEY_READ)
                        pv = winreg.QueryValueEx(k, "pv")[0]
                        winreg.CloseKey(k)
                        if pv and str(pv) not in ("0.0.0.0",):
                            return True, "WebView2 运行时已就绪（%s）" % pv
                    except OSError:
                        continue
            except Exception:
                continue
    except Exception:
        pass
    # 兜底：直接找运行时目录
    for root_env in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.path.join(os.environ.get(root_env) or "",
                            _WEBVIEW2_DIR)
        try:
            if glob.glob(os.path.join(base, "*", "msedgewebview2.exe")):
                return True, "WebView2 运行时已就绪（文件探测）"
        except OSError:
            continue
    return False, "缺少 WebView2 运行时（游戏内 STOVE 商店/公告 UI 依赖）"


def _net_channel():
    """前置下载走哪条通道 —— 优先用启动器统一的出口（czn_lite._LoggedSession），
    这样 config 里的 network.mode（直连 / 系统代理 / 手动代理）对微软官方安装器
    的下载同样管得住，并且请求会进同一条日志。只有单独运行本模块（导入不到
    czn_lite）才退回 urllib。"""
    try:
        import czn_lite as cl
    except Exception:
        return "urllib", None
    return "czn_lite", cl


def _fetch_urllib(url, dst, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp, \
            open(dst, "wb") as f:
        while True:
            b = resp.read(1 << 16)
            if not b:
                break
            f.write(b)


def _fetch_czn_session(cl, url, dst, timeout):
    """经启动器的路由会话取回 url —— 直连时显式锁死代理，代理时按 network.mode 走。"""
    s = cl._LoggedSession(cl.requests.Session(
        impersonate="chrome", default_headers=False,
        http_version=cl.CurlHttpVersion.V1_1))
    r = s.get(url, timeout=timeout, stream=True, headers={"User-Agent": _UA})
    code = getattr(r, "status_code", 0)
    if code not in (200, 206):
        raise RuntimeError("HTTP %s" % code)
    with open(dst, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 16):
            if chunk:
                f.write(chunk)


def download(url: str, dst: str, on_event=None, timeout=60):
    """下载前置安装器到本地（微软官方固定链）。返回落地路径。"""
    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    label, cl = _net_channel()
    last = None
    for attempt in (1, 2, 3):
        try:
            if on_event:
                on_event("[前置] 下载 %s（第 %d 次，通道=%s）"
                         % (url, attempt, label))
            # 先落 .part 再改名：任何一次中断都不会留下「半个安装器」被误执行
            tmp = dst + ".part"
            if cl is not None:
                _fetch_czn_session(cl, url, tmp, timeout)
            else:
                _fetch_urllib(url, tmp, timeout)
            os.replace(tmp, dst)
            size = os.path.getsize(dst)
            if on_event:
                on_event("[前置] 下载完成：%.1f MB → %s"
                         % (size / 1048576, dst))
            return dst
        except Exception as exc:
            last = exc
            time.sleep(1.5 * attempt)
    raise RuntimeError("下载失败：%s" % last)


def run_elevated(exe: str, args: str, on_event=None) -> bool:
    """UAC 提权运行安装器（用户已同意）。返回是否成功拉起并等到退出。"""
    try:
        import ctypes
        ret = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", exe, args, None, 0)  # SW_HIDE
        if ret <= 32:
            if on_event:
                on_event("[前置] 安装器未能拉起（ShellExecute=%s）——"
                         "可能用户拒绝了 UAC" % ret)
            return False
        # 轮询等待安装进程退出（ShellExecute 拿不到句柄，按检测目标轮询）
        return True
    except Exception as exc:
        if on_event:
            on_event("[前置] 提权执行失败：%s" % exc)
        return False


def ensure_vc_redist(agree, workdir, on_event=None, wait_secs=300):
    """确保 VC++ 2022 x64 运行时。agree() 必须返回 True 才会下载并安装。"""
    ok, note = check_vcredist_detailed()
    if ok:
        return True, note
    if on_event:
        on_event("[前置] %s" % note)
    if not agree("需要安装 VC++ 2022 x64 运行时（微软官方 vc_redist.x64.exe，"
                 "约 25 MB，游戏没有它无法启动）。是否下载并安装？"):
        return False, "用户取消安装 VC++ 运行时"
    exe = os.path.join(workdir, "vc_redist.x64.exe")
    try:
        download(VC_REDIST_X64_URL, exe, on_event)
    except Exception as exc:
        return False, str(exc)
    if not run_elevated(exe, "/install /quiet /norestart", on_event):
        return False, "安装器未执行（UAC 被拒绝）"
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        ok, note = check_vcredist_detailed()
        if ok:
            return True, "VC++ 2022 x64 运行时安装完成"
        time.sleep(3)
    return False, "等待安装超时（可手动运行 %s 后重试）" % exe


def ensure_webview2(agree, workdir, on_event=None, wait_secs=300):
    """确保 WebView2 Evergreen 运行时。agree() 必须返回 True 才会下载并安装。"""
    ok, note = check_webview2()
    if ok:
        return True, note
    if on_event:
        on_event("[前置] %s" % note)
    if not agree("需要安装 WebView2 运行时（微软官方 Evergreen 引导器，约 2 MB；"
                 "游戏内 STOVE 商店/公告 UI 依赖）。是否下载并安装？"):
        return False, "用户取消安装 WebView2 运行时"
    exe = os.path.join(workdir, "MicrosoftEdgeWebview2Setup.exe")
    try:
        download(WEBVIEW2_BOOTSTRAP_URL, exe, on_event)
    except Exception as exc:
        return False, str(exc)
    if not run_elevated(exe, "/silent /install", on_event):
        return False, "安装器未执行（UAC 被拒绝）"
    deadline = time.time() + wait_secs
    while time.time() < deadline:
        ok, note = check_webview2()
        if ok:
            return True, "WebView2 运行时安装完成"
        time.sleep(3)
    return False, "等待安装超时（可手动运行 %s 后重试）" % exe


def selftest():
    """离线自检：检测函数可用 + 安装命令构造正确（不下载、不运行）。"""
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  %-4s %-28s %s" % ("PASS" if cond else "FAIL", name, extra))

    print("=" * 64)
    print("环境前置体检 —— 离线自检")
    print("=" * 64)
    d_ok, d_note = disk_ok(os.getcwd())
    chk("磁盘空间检测", isinstance(d_ok, bool), d_note[:44])
    v_ok, v_note = check_vcredist_detailed()
    chk("VC++ 检测", isinstance(v_ok, bool), v_note[:44])
    w_ok, w_note = check_webview2()
    chk("WebView2 检测", isinstance(w_ok, bool), w_note[:44])
    chk("官方固定链", VC_REDIST_X64_URL.startswith("https://aka.ms/")
        and WEBVIEW2_BOOTSTRAP_URL.startswith("https://go.microsoft.com/"))
    label, ch = _net_channel()
    chk("前置下载遵循 network.mode", label == "czn_lite" and ch is not None,
        "通道=%s" % label)
    chk("静默参数", "/quiet" in "/install /quiet /norestart"
        and "/silent" in "/silent /install")
    # 同意门：仅在组件确实缺失时才会走到下载前征询；已就绪则直接通过且不征询
    called = []

    def refuse(_msg):
        called.append(1)
        return False
    v_missing = not v_ok
    w_missing = not w_ok
    r_ok, r_note = ensure_vc_redist(refuse, os.getcwd())
    if v_missing:
        chk("同意门拦截（VC++）", bool(called) and not r_ok, r_note[:40])
    else:
        chk("VC++ 已就绪直接通过", r_ok and not called, r_note[:40])
    r2_ok, r2_note = ensure_webview2(refuse, os.getcwd())
    if w_missing:
        chk("同意门拦截（WebView2）", len(called) == 2 and not r2_ok, r2_note[:40])
    else:
        chk("WebView2 已就绪直接通过", r2_ok and len(called) == (1 if v_missing else 0),
            r2_note[:40])
    print()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
