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
r"""czn-lite —— Chaos Zero Nightmare（STOVE 版）第三方精简启动器（核心逻辑）

不经官方 STOVE 客户端，直接完成登录、令牌兑换与游戏拉起：

  1. auth      二维码扫码登录 → signin(RT) 获得 299 字符启动器级令牌
                 → POST /gc/v1.4/check 换取 384 字符游戏级令牌
  2. pipesrv   命名管道 \\.\pipe\{GUID}\STOVE_CHAOSZERO 服务端
                 握手 1000 → 2000(RSA) → 2001(AES)，心跳 1001 只收不应答
  3. launch    组装 31 个环境变量，直接拉起游戏主程序 exe
  4. keepalive 游戏运行期间由本进程内的管道服务保活

安全声明：仅供个人学习研究。仅支持二维码登录，不接触密码；
refresh_token 保存在本机 state.json 中，请妥善保管、切勿外传。
"""
import argparse
import base64
import collections
import ctypes
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import types
import uuid
import winreg
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from curl_cffi import CurlHttpVersion, requests
from Crypto.Cipher import AES, PKCS1_v1_5
from Crypto.PublicKey import RSA

# ====================================================================
# 配置
# --------------------------------------------------------------------
# 数据文件只有两个：
#   config.json  全部配置（客户端写死常量 + 本机值 install_root/gds），随包分发
#   state.json   账号凭据与本机采集信息（exe 同目录），不随包分发
# 源码内置默认值只允许客户端写死常量；本机值（install_root）默认为空，
# 由「获取离线信息」探测后写入。「清空离线信息」把它置空即回到初始状态。
# ====================================================================
_APP_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).parent

# Windows 下 stdout 被重定向时按 GBK 编码，日志里的 ⇒ ✓ ⚠ 这类字符会抛
# UnicodeEncodeError。只放宽错误处理（编不了的替换成 ?），不让打日志弄死流程。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass


def _find_config_file():
    candidates = []
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).parent / "config.json")
    candidates.append(Path(__file__).with_name("config.json"))
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


_CONFIG_FILE = _find_config_file()

# 配置读取诊断。窗口模式 exe 没有控制台，print 出去的报错用户根本看不见 ——
# 于是「配置坏了」在用户侧永远表现为「找不到路径」。必须收集起来供界面显示。
CONFIG_NOTES = []


def _decode_config(raw):
    """config.json 字节 -> 文本。

    utf-8-sig 排第一是为了吃掉 BOM：记事本保存会加 BOM，
    而标准 json.loads 遇到 BOM 会直接抛 JSONDecodeError。
    """
    for enc in ("utf-8-sig", "utf-8", "gbk", "cp936", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc != "utf-8-sig":
                CONFIG_NOTES.append("配置以 %s 编码解码" % enc)
            return text
        except UnicodeDecodeError:
            continue
    return None


_LONE_ESCAPE = re.compile(r"(?<!\\)\\(?!\\)")      # 单独的反斜杠（前后都不是）


def _repair_path_values(text):
    """把「路径类键」的值按字面反斜杠重新转义。

    JSON 里 \\b \\f \\n \\r \\t 都是**合法**转义，用户手写的 `D:\\bin` 会被
    静默解析成 `D:<退格>in`。这里只补单独的反斜杠（已配对的 \\\\ 不动），
    因此可无条件先跑；只动已知路径键，help_text 里的 \\n 绝不能碰。
    """
    keys = ("install_root", "game_exe", "loader_exe", "loader_args",
            "game_path", "install_path", "path", "working_dir")
    pat = re.compile(r'"(%s)"(\s*:\s*)"([^"]*)"' % "|".join(keys))

    def _fix(m):
        key, sep, val = m.group(1), m.group(2), m.group(3)
        # 替换串里的 "\\\\" 只表示一个反斜杠，必须用 lambda 才能返回两个
        return '"%s"%s"%s"' % (key, sep,
                               _LONE_ESCAPE.sub(lambda _m: "\\\\", val))

    return pat.sub(_fix, text)


def _loads_config(text):
    """解析配置文本，容忍手工编辑的常见错误。

    逐级尝试，任一级成功即返回，每级都在 CONFIG_NOTES 留痕。
    路径键规范化排最前：\\b \\t 这类合法转义原样解析会「成功」却改坏路径。
    """

    def _try(candidate):
        try:
            return json.loads(candidate), None
        except Exception as e:
            return None, e

    repaired = _repair_path_values(text)
    no_comma = re.sub(r",(\s*[}\]])", r"\1", text)

    # 1) 路径键按字面反斜杠规范化
    if repaired != text:
        data, err = _try(repaired)
        if data is not None:
            CONFIG_NOTES.append("已自动修复：路径键里的单反斜杠按字面处理")
            return data
        CONFIG_NOTES.append("路径键规范化后仍失败：%s" % err)

    # 2) 原样
    data, err = _try(text)
    if data is not None:
        return data
    CONFIG_NOTES.append("严格解析失败：%s" % err)

    # 3) 只去尾逗号
    if no_comma != text:
        data, err = _try(no_comma)
        if data is not None:
            CONFIG_NOTES.append("已自动修复：去掉多余的尾逗号")
            return data
        CONFIG_NOTES.append("去尾逗号后仍失败：%s" % err)

    # 4) 路径键规范化 + 去尾逗号
    if repaired != text:
        both = re.sub(r",(\s*[}\]])", r"\1", repaired)
        if both != repaired:
            data, err = _try(both)
            if data is not None:
                CONFIG_NOTES.append("已自动修复：单反斜杠 + 多余的尾逗号")
                return data
            CONFIG_NOTES.append("路径键规范化+去尾逗号后仍失败：%s" % err)

    # 5) 通用非法转义修复（非路径键也可能写坏）
    generic = re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', repaired)
    if generic != repaired:
        data, err = _try(generic)
        if data is not None:
            CONFIG_NOTES.append("已自动修复：非法转义序列")
            return data
        CONFIG_NOTES.append("通用修复后仍失败：%s" % err)

    return None


try:
    if _CONFIG_FILE.exists():
        _raw = _CONFIG_FILE.read_bytes()
        _text = _decode_config(_raw)
        _CONFIG = _loads_config(_text) if _text else None
        if _CONFIG is None:
            CONFIG_NOTES.append("配置无法解析，已退化为内置默认值")
            _CONFIG = {}
        elif not isinstance(_CONFIG, dict):
            CONFIG_NOTES.append("配置顶层不是 JSON 对象，已忽略")
            _CONFIG = {}
    else:
        CONFIG_NOTES.append("未找到 config.json：%s" % _CONFIG_FILE)
        _CONFIG = {}
except Exception as e:
    CONFIG_NOTES.append("读取 config.json 异常：%r" % (e,))
    _CONFIG = {}


def _cfg(*path, default=None):
    """按路径读取 config.json 的嵌套字段，任一层缺失即返回 default。"""
    node = _CONFIG
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return default
        node = node[key]
    return node


# ---- 客户端写死常量（跨设备相同，可用 config.json 覆盖） ----
GAME_ID = _cfg("game", "game_id", default="STOVE_CHAOSZERO")
GAME_NO = _cfg("game", "game_no", default="6583")
MARKET_GAME_ID = _cfg("game", "market_game_id",
                      default="com.smilegate.chaoszero.stove.pc")
PIPE_GUID = _cfg("game", "pipe_guid",
                 default="15BADE76-0E87-422B-A75B-B2049FE5F5F5")
PIPE_NAME = r"\\.\pipe\%s\%s" % (PIPE_GUID, GAME_ID)
APP_KEY = _cfg("game", "app_key", default=(
    "def9dc2a3eac179de3ae4f54e75de3929bd56cced5ef09b0470ad66c3ec7e2da"))
API_BASE = _cfg("platform", "api_base", default="https://s-api.onstove.com")
API = _cfg("platform", "api", default="https://api.onstove.com")
CLIENT_ID = _cfg("platform", "client_id", default=(
    "5faa0926311687ccc34a598d9640a48909a86a0e93afdf586ab088a3d01a93d3"))
# 启动器版本串：随 STOVE 客户端版本更新，STOVE 升级后改配置即可
CALLER_ID = _cfg("platform", "caller_id", default="STOVE_LAUNCHER_VER.3.2.28.733")
# 游戏主程序相对路径：文件名为游戏写死常量，仅目录随设备变化。
# 不经 ucldr loader —— 实测 loader 只是官方启动器激活保护的中间层，
# 管道服务就绪后直接起主程序即可进游戏，环境变量通道完全一致。
GAME_EXE_REL = _cfg("game", "game_exe",
                    default=r"bin\ssr-stove-shield.exe")


# ====================================================================
# 网络与日志配置（全部有默认值 —— 不写这两段也能跑）
# ====================================================================
def _cfg_bool(*path, default=False):
    value = _cfg(*path, default=default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "on")
    return default if value is None else bool(value)


def _cfg_int(*path, default=0):
    try:
        return int(_cfg(*path, default=default))
    except (TypeError, ValueError):
        return default


NET_MODE = str(_cfg("network", "mode", default="direct") or "direct").strip().lower()
NET_READ_WININET = _cfg_bool("network", "system_read_wininet", default=True)
NET_READ_WINHTTP = _cfg_bool("network", "system_read_winhttp", default=True)
NET_USE_PAC = _cfg_bool("network", "system_use_pac", default=True)
NET_SYS_BYPASS_EXTRA = str(_cfg("network", "system_bypass_extra", default="") or "")
NET_MANUAL_URL = str(_cfg("network", "manual_url", default="") or "")
NET_MANUAL_USER = str(_cfg("network", "manual_username", default="") or "")
NET_MANUAL_PASS = str(_cfg("network", "manual_password", default="") or "")
NET_MANUAL_BYPASS = str(_cfg("network", "manual_bypass", default="") or "")
NET_HOST_OVERRIDES = _cfg("network", "host_overrides", default=None)
NET_READ_TIMEOUT = _cfg_int("network", "read_timeout", default=20)
NET_CONNECT_TIMEOUT = _cfg_int("network", "connect_timeout", default=10)
NET_RETRY = _cfg_int("network", "retry", default=0)
NET_VERIFY_TLS = _cfg_bool("network", "verify_tls", default=True)
NET_PREFLIGHT = _cfg_bool("network", "preflight", default=False)
NET_LOG_DECISIONS = _cfg_bool("network", "log_decisions", default=True)

LOG_EXPORT_REVEAL = _cfg_bool("log", "export_reveal_secrets", default=False)
LOG_MASK_TAIL = _cfg_int("log", "mask_keep_tail", default=4)
LOG_RING_SIZE = max(100, _cfg_int("log", "ring_size", default=5000))
LOG_FILE_ENABLED = _cfg_bool("log", "file_enabled", default=False)
LOG_FILE_MAX_MB = _cfg_int("log", "file_max_mb", default=8)
LOG_ENV_SNAPSHOT = _cfg_bool("log", "include_env_snapshot", default=True)
LOG_TS_MS = _cfg_bool("log", "timestamp_ms", default=True)

# 版本号仅用于日志文件名与报告抬头，未配置则不写（不臆造版本）
APP_VERSION = str(_cfg("app", "version", default="") or "")

# ====================================================================
# 网络路径解析
# --------------------------------------------------------------------
# 三条实测结论决定了这里的写法：
#  ① curl_cffi 未显式传 proxies 时不会 setopt(CURLOPT_PROXY)，libcurl 遂回落到
#     读 http_proxy/https_proxy/all_proxy 环境变量 —— 会「静默走代理」，
#     使 README 承诺的「免代理裸连」失效。
#  ② 因此直连必须显式传 {"all": ""}：只有空串才会真正关掉代理；
#     proxies={"all": None} 与 proxy="" 都会被 curl_cffi 的判断吃掉，无效。
#  ③ PAC 是 per-URL 的，所以路由按 host 解析并缓存，不能只算一次。
# ====================================================================
@dataclass(frozen=True)
class Route:
    """一次请求的网络路径决策结果。"""
    proxies: dict          # 直接喂给 curl_cffi；直连 = {"all": ""}
    source: str            # 默认直连 / 手动 / 按域名覆盖 / 系统(WinINET|PAC|WPAD)
    detail: str            # 人可读依据，进日志
    host: str = ""


_DIRECT = {"all": ""}
# 系统代理判定的缓存秒数：过期后重判（判定结果没变就静默续期）。
# 用户中途在系统里开/关代理，最多 TTL 秒后被看见。
_SYSTEM_ROUTE_TTL = 30.0
_NET_CACHE = {}
_NET_LOCK = threading.Lock()
_STAGE = {"name": "启动"}


def set_stage(name):
    """标注当前流程阶段，供请求日志归类。"""
    _STAGE["name"] = str(name)


def stage():
    return _STAGE["name"]


def _host_of(url):
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/:?#]+)", str(url or ""))
    return m.group(1).lower() if m else ""


def _scheme_of(url):
    m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*)://", str(url or ""))
    return m.group(1).lower() if m else "http"


def _bypass_match(host, bypass):
    """绕过表匹配：';' 分隔，'*' 通配，'<local>' = 不含点的主机名。"""
    if not host or not bypass:
        return False
    for item in str(bypass).split(";"):
        item = item.strip().lower()
        if not item:
            continue
        if item == "<local>":
            if "." not in host:
                return True
            continue
        if item.startswith("*"):
            if host.endswith(item[1:]):
                return True
        elif item.endswith("*"):
            if host.startswith(item[:-1]):
                return True
        elif host == item or host.endswith("." + item):
            return True
    return False


def _normalize_proxy(text):
    """补全代理串：缺 scheme 补 http://；配了账号密码则注入。"""
    text = str(text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    if NET_MANUAL_USER:
        scheme, sep, rest = text.partition("://")
        if sep and "@" not in rest:
            from urllib.parse import quote
            auth = "%s:%s" % (quote(NET_MANUAL_USER, safe=""),
                              quote(NET_MANUAL_PASS or "", safe=""))
            text = "%s://%s@%s" % (scheme, auth, rest)
    return text


def _split_proxy(proxy, url):
    """系统代理串可能是 'host:port'，也可能是 'http=h:p;https=h:p;ftp=...'。"""
    proxy = str(proxy or "").strip()
    if "=" not in proxy:
        return proxy
    table = {}
    for item in proxy.split(";"):
        key, _, val = item.partition("=")
        table[key.strip().lower()] = val.strip()
    return table.get(_scheme_of(url)) or table.get("http") or ""


# ---- WinHTTP API（读系统代理；PAC/WPAD 求值 libcurl 自己做不到） ----
_WINHTTP = None

_WINHTTP_AUTOPROXY_AUTO_DETECT = 0x00000001
_WINHTTP_AUTOPROXY_CONFIG_URL = 0x00000002
_WINHTTP_AUTO_DETECT_DHCP = 0x00000001
_WINHTTP_AUTO_DETECT_DNS_A = 0x00000002
_WINHTTP_ACCESS_TYPE_NAMED_PROXY = 3


class _IE_PROXY_CONFIG(ctypes.Structure):
    _fields_ = [("fAutoDetect", wintypes.BOOL),
                ("lpszAutoConfigUrl", wintypes.LPWSTR),
                ("lpszProxy", wintypes.LPWSTR),
                ("lpszProxyBypass", wintypes.LPWSTR)]


class _AUTOPROXY_OPTIONS(ctypes.Structure):
    _fields_ = [("dwFlags", wintypes.DWORD),
                ("dwAutoDetectFlags", wintypes.DWORD),
                ("lpszAutoConfigUrl", wintypes.LPCWSTR),
                ("lpvReserved", ctypes.c_void_p),
                ("dwReserved", wintypes.DWORD),
                ("fAutoLogonIfChallenged", wintypes.BOOL)]


class _PROXY_INFO(ctypes.Structure):
    _fields_ = [("dwAccessType", wintypes.DWORD),
                ("lpszProxy", wintypes.LPWSTR),
                ("lpszProxyBypass", wintypes.LPWSTR)]


def _winhttp():
    """惰性加载 winhttp.dll。★ WinHttpOpen 返回 HANDLE，必须显式声明 restype，
    否则 ctypes 默认 c_int 会截断指针，后续调用全部报 err=6（无效句柄）。"""
    global _WINHTTP
    if _WINHTTP is None:
        try:
            lib = ctypes.WinDLL("winhttp", use_last_error=True)
            lib.WinHttpOpen.restype = wintypes.HANDLE
            lib.WinHttpOpen.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                        wintypes.LPCWSTR, wintypes.LPCWSTR,
                                        wintypes.DWORD]
            lib.WinHttpCloseHandle.argtypes = [wintypes.HANDLE]
            lib.WinHttpGetIEProxyConfigForCurrentUser.restype = wintypes.BOOL
            lib.WinHttpGetProxyForUrl.restype = wintypes.BOOL
            lib.WinHttpGetProxyForUrl.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR,
                                                  ctypes.c_void_p, ctypes.c_void_p]
            _WINHTTP = lib
        except Exception:
            _WINHTTP = False
    return _WINHTTP or None


def _system_proxy_static():
    """读系统代理配置。返回 (proxy, bypass, pac_url, autodetect) 或 None。"""
    lib = _winhttp()
    if not lib:
        return None
    try:
        cfg = _IE_PROXY_CONFIG()
        if not lib.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(cfg)):
            return None
        return (cfg.lpszProxy or "", cfg.lpszProxyBypass or "",
                cfg.lpszAutoConfigUrl or "", bool(cfg.fAutoDetect))
    except Exception:
        return None


def _system_proxy_for_url(url, pac_url=None, autodetect=False):
    """按 URL 求值系统代理（PAC / WPAD）。返回 (是否成功, 代理串或 None, 说明)。"""
    lib = _winhttp()
    if not lib:
        return False, None, "winhttp 不可用"
    handle = lib.WinHttpOpen("czn-lite", 0, None, None, 0)
    if not handle:
        return False, None, "WinHttpOpen 失败"
    try:
        opts = _AUTOPROXY_OPTIONS()
        opts.fAutoLogonIfChallenged = True
        if pac_url:
            opts.dwFlags |= _WINHTTP_AUTOPROXY_CONFIG_URL
            opts.lpszAutoConfigUrl = pac_url
        if autodetect:
            opts.dwFlags |= _WINHTTP_AUTOPROXY_AUTO_DETECT
            opts.dwAutoDetectFlags = _WINHTTP_AUTO_DETECT_DHCP | _WINHTTP_AUTO_DETECT_DNS_A
        if not opts.dwFlags:
            return False, None, "无可用的自动代理配置"
        info = _PROXY_INFO()
        if not lib.WinHttpGetProxyForUrl(handle, url, ctypes.byref(opts),
                                         ctypes.byref(info)):
            return False, None, "WinHttpGetProxyForUrl 失败(err=%d)" \
                % ctypes.get_last_error()
        if info.dwAccessType == _WINHTTP_ACCESS_TYPE_NAMED_PROXY:
            return True, (info.lpszProxy or None), "按 URL 求值成功"
        return True, None, "PAC/WPAD 判定直连"
    except Exception as e:
        return False, None, "求值异常 %s" % e
    finally:
        try:
            lib.WinHttpCloseHandle(handle)
        except Exception:
            pass


def _system_route(url, host):
    """把系统代理配置转成 Route（静态 → PAC → WPAD → 直连）。"""
    if not (NET_READ_WININET or NET_READ_WINHTTP):
        return Route(_DIRECT, "系统代理", "配置里关掉了系统代理读取 → 直连", host)
    cfg = _system_proxy_static()
    if cfg is None:
        return Route(_DIRECT, "系统代理", "读不到系统代理配置 → 直连", host)
    proxy, bypass, pac_url, autodetect = cfg
    bypass_all = ";".join(x for x in (bypass, NET_SYS_BYPASS_EXTRA) if x)

    if pac_url and NET_USE_PAC:
        ok, found, note = _system_proxy_for_url(url, pac_url=pac_url)
        if not ok:
            return Route(_DIRECT, "系统(PAC)",
                         "PAC 求值失败（%s）→ 直连" % note, host)
        if not found:
            return Route(_DIRECT, "系统(PAC)", "PAC 判定直连", host)
        return Route({"all": _normalize_proxy(found)}, "系统(PAC)", found, host)

    if autodetect and NET_USE_PAC:
        ok, found, note = _system_proxy_for_url(url, autodetect=True)
        if ok and found:
            return Route({"all": _normalize_proxy(found)}, "系统(WPAD)", found, host)
        if ok:
            return Route(_DIRECT, "系统(WPAD)", "WPAD 判定直连", host)

    if not proxy:
        return Route(_DIRECT, "系统代理", "系统未启用代理 → 直连", host)
    if _bypass_match(host, bypass_all):
        return Route(_DIRECT, "系统代理", "命中系统绕过表 → 直连", host)
    found = _split_proxy(proxy, url)
    return Route({"all": _normalize_proxy(found)}, "系统(WinINET)", found, host)


def resolve_route(url):
    """解析一次请求的网络路径：手动 > 按域名覆盖 > 系统代理 > 默认直连。

    结果按 (模式, host, 覆盖项) 缓存 —— 因为 PAC 是 per-URL 的。
    系统代理模式的判定有 TTL：用户可能中途在系统里开/关代理，
    永久缓存会让「判定那一刻没开代理」变成永远直连。过期重判时
    若结论没变就静默续期（不刷日志），变了才记一条变化事件。
    """
    host = _host_of(url)
    override = None
    if isinstance(NET_HOST_OVERRIDES, dict):
        override = NET_HOST_OVERRIDES.get(host)
        if override is None:
            for key, val in NET_HOST_OVERRIDES.items():
                key = str(key)
                if key.startswith("*") and host.endswith(key[1:]):
                    override = val
                    break

    cache_key = (NET_MODE, host, str(override))
    now = time.time()
    with _NET_LOCK:
        cached = _NET_CACHE.get(cache_key)
    if cached is not None:
        cached_ts, cached_route = cached
        if NET_MODE != "system" or now - cached_ts <= _SYSTEM_ROUTE_TTL:
            return cached_route
        stale = cached_route
    else:
        stale = None

    if override is not None:
        text = str(override).strip()
        if text.lower() in ("direct", "none", "off", ""):
            route = Route(_DIRECT, "按域名覆盖", "%s → 直连" % host, host)
        else:
            route = Route({"all": _normalize_proxy(text)}, "按域名覆盖",
                          "%s → %s" % (host, _normalize_proxy(text)), host)
    elif NET_MODE == "manual":
        manual = _normalize_proxy(NET_MANUAL_URL)
        if not manual:
            route = Route(_DIRECT, "手动代理",
                          "network.manual_url 为空 → 直连", host)
        elif _bypass_match(host, NET_MANUAL_BYPASS):
            route = Route(_DIRECT, "手动代理", "命中 manual_bypass → 直连", host)
        else:
            route = Route({"all": manual}, "手动代理", manual, host)
    elif NET_MODE == "system":
        route = _system_route(url, host)
    else:
        route = Route(_DIRECT, "默认直连", "network.mode=%s" % NET_MODE, host)

    with _NET_LOCK:
        _NET_CACHE[cache_key] = (now, route)

    changed = (stale is not None and
               (stale.source, stale.proxies.get("all"))
               != (route.source, route.proxies.get("all")))
    if changed and NET_LOG_DECISIONS:
        record("route", stage=stage(), host=host, source=route.source,
               proxy=(route.proxies.get("all") or "直连"),
               detail="系统代理判定变化: %s → %s"
                      % (stale.proxies.get("all") or "直连",
                         route.proxies.get("all") or "直连"))
    elif stale is None and NET_LOG_DECISIONS:
        record("route", stage=stage(), host=host, source=route.source,
               proxy=(route.proxies.get("all") or "直连"), detail=route.detail)
    return route


def reset_route_cache():
    """配置变化后清缓存（GUI 改设置时调用）。"""
    with _NET_LOCK:
        _NET_CACHE.clear()


def preflight(timeout=8):
    """代理连通性预检：按当前路由访问一次 API 根，返回 (是否通, 说明)。

    仅在 network.preflight 打开时被调用；失败不致命，只写日志。
    """
    url = API + "/"
    route = resolve_route(url)
    target = route.proxies.get("all") or "直连"
    started = time.time()
    try:
        session = requests.Session(impersonate="chrome", default_headers=False,
                                   http_version=CurlHttpVersion.V1_1)
        session.headers.clear()
        session.get(url, timeout=(min(5, timeout), timeout),
                    proxies=route.proxies)
        return True, "预检通过（%s，%dms）" % (target,
                                              int((time.time() - started) * 1000))
    except Exception as e:
        return False, "预检失败（%s）：%s" % (target, str(e)[:160])


def network_summary():
    """当前网络路径摘要，供界面状态条显示。"""
    probe = "%s://%s/" % ("https", "s-api.onstove.com")
    try:
        route = resolve_route(probe)
    except Exception as e:
        return "网络：解析失败（%s）" % e
    target = route.proxies.get("all") or "直连"
    return "网络：%s（%s）" % (target, route.source)


# ====================================================================
# 结构化事件 · 双渲染（界面绝对坦诚 / 导出字段级脱敏）
# --------------------------------------------------------------------
# 界面与导出走同一份事件数据，脱敏只发生在「导出渲染器」里 ——
# 这样「不泄漏」是结构性保证，而不是靠事后正则清洗。
# ====================================================================
_EVENTS = collections.deque(maxlen=LOG_RING_SIZE)
_EV_SEQ = [0]
_EV_LOCK = threading.Lock()
# GUI 把事件实时上屏用；CLI 下为 None
EVENT_SINK = None


def record(kind, **fields):
    """记录一条结构化事件。返回事件本身（便于测试断言）。

    若设置了 EVENT_SINK（GUI 会设），顺带实时回调一次 —— 界面与反馈日志
    共用同一份数据，回调里只做渲染，不做脱敏。
    """
    with _EV_LOCK:
        _EV_SEQ[0] += 1
        ev = {"seq": _EV_SEQ[0], "t": time.time(), "kind": kind, "f": fields}
        _EVENTS.append(ev)
    sink = EVENT_SINK
    if sink is not None:
        try:
            sink(ev)
        except Exception:
            pass
    _write_log_file(ev)
    return ev


def events():
    """取事件快照（副本，避免并发改动）。"""
    with _EV_LOCK:
        return list(_EVENTS)


def clear_events():
    with _EV_LOCK:
        _EVENTS.clear()
        _EV_SEQ[0] = 0


# ---- 脱敏（只作用于导出渲染） ----
_LEN_ONLY = {"access-token", "refresh-token", "launcher-refresh", "launcher-access",
             "game-access-token", "game-refresh-token", "password", "captcha-token",
             "captcha-key", "captcha-value", "authorization", "bearer", "token"}
_KEEP_TAIL = {"member-no", "guid", "game-member-no", "game-guid"}
_FULL_MASK = {"nickname", "member-nickname", "birth-dt", "reg-dt", "machine-guid"}
_PREFIX8 = {"session", "qr-login-session", "device-key", "transaction-id",
            "session-tid", "ref-session-id"}
# 代理串可能内嵌账号密码（_normalize_proxy 注入，或环境变量自带），
# 而它的字段名不在上表里 —— 必须按 URL 用户信息统一抹掉。
_URL_USERINFO = re.compile(r"://[^/@\s:]+:[^/@\s]*@")


def mask_value(key, value):
    """按字段名脱敏。字段名不认识时原样返回（长度等非敏感信息保留）。"""
    if isinstance(value, str):
        value = _URL_USERINFO.sub("://***:***@", value)
    name = str(key or "").strip().lower().replace("_", "-")
    text = "" if value is None else str(value)

    if name in _LEN_ONLY:
        return "<len=%d>" % len(text)
    if name == "caller-detail":
        return "<len=%d sha256:%s>" % (
            len(text), hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()[:8])
    if name in ("user-id", "userid", "email", "member-account"):
        if "@" in text:
            local, _, domain = text.partition("@")
            return (local[:1] or "*") + "***@" + domain
        return "***"
    if name in _KEEP_TAIL:
        keep = max(0, LOG_MASK_TAIL)
        if keep == 0 or len(text) <= keep:
            return "***"
        return "*" * (len(text) - keep) + text[-keep:]
    if name in _FULL_MASK:
        return "***"
    if name in _PREFIX8:
        return (text[:8] + "…") if len(text) > 8 else "…"
    return value


def mask_obj(obj, depth=0):
    """递归脱敏：dict 按键名、list 逐项。"""
    if depth > 6:
        return "…"
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = mask_obj(v, depth + 1) if isinstance(v, (dict, list)) \
                else mask_value(k, v)
        return out
    if isinstance(obj, (list, tuple)):
        return [mask_obj(x, depth + 1) for x in obj]
    return obj


def _fmt_ts(t):
    base = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))
    return base + (".%03d" % int((t % 1) * 1000) if LOG_TS_MS else "")


_LEAD_KEYS = ("stage", "method", "url", "host", "proxy", "route_source", "status",
              "ms", "code", "message", "tag", "event", "what", "step", "ok",
              "type", "where", "source", "detail", "value_len", "keys")


def _render(ev, mask):
    kind, fields = ev["kind"], dict(ev["f"])
    if mask:
        fields = mask_obj(fields)
    lead = []
    for key in _LEAD_KEYS:
        if key in fields:
            lead.append("%s=%s" % (key, fields.pop(key)))
    head = "%s  #%04d [%-5s]" % (_fmt_ts(ev["t"]), ev["seq"], kind)
    lines = [head + ("  " + " ".join(lead) if lead else "")]
    for key, val in fields.items():
        if isinstance(val, (dict, list)):
            lines.append("      %-12s %s"
                         % (key, json.dumps(val, ensure_ascii=False)))
        else:
            lines.append("      %-12s %s" % (key, val))
    return lines


def render_ui(ev):
    """界面渲染：明文，绝对坦诚。"""
    return _render(ev, mask=False)


def render_report(ev, reveal=None):
    """导出渲染：字段级脱敏。reveal=True 时明文（log.export_reveal_secrets 可覆盖默认）。"""
    if reveal is None:
        reveal = LOG_EXPORT_REVEAL
    return _render(ev, mask=not reveal)


# ---- 落盘反馈日志（默认关闭；开启后超限自动换新文件） ----
_LOG_FILE = {"path": None, "size": 0}
_LOG_FILE_LOCK = threading.Lock()


def _write_log_file(ev):
    """log.file_enabled 打开时把事件实时落盘（走脱敏渲染器）。"""
    if not LOG_FILE_ENABLED:
        return
    try:
        with _LOG_FILE_LOCK:
            path = _LOG_FILE["path"]
            limit = max(1, LOG_FILE_MAX_MB) * 1048576
            if path is None or _LOG_FILE["size"] > limit:
                path = _APP_DIR / ("czn-lite-session-%s.log"
                                   % time.strftime("%Y%m%d-%H%M%S"))
                _LOG_FILE["path"] = path
                fresh = not path.exists()
                _LOG_FILE["size"] = path.stat().st_size if not fresh else 0
                if fresh:
                    with open(path, "a", encoding="utf-8") as f:
                        f.write("CZN Launcher Lite 会话日志（已脱敏）\n")
                        f.write("启动 %s | 版本 %s | 脱敏 %s\n"
                                % (time.strftime("%Y-%m-%d %H:%M:%S"),
                                   APP_VERSION or "(未标注)",
                                   "关（明文）" if LOG_EXPORT_REVEAL else "开"))
                        _LOG_FILE["size"] = f.tell()
            text = "\n".join(render_report(ev)) + "\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
            _LOG_FILE["size"] += len(text.encode("utf-8"))
    except Exception:
        pass


def env_snapshot():
    """环境快照：系统代理 / PAC / 环境变量 / 最终路径决策。"""
    snap = {"network_mode": NET_MODE}
    if not LOG_ENV_SNAPSHOT:
        return snap
    cfg = _system_proxy_static()
    if cfg:
        proxy, bypass, pac_url, autodetect = cfg
        snap["system_proxy"] = proxy or "(未启用)"
        snap["system_bypass"] = bypass[:200]
        snap["pac_url"] = pac_url or "(无)"
        snap["autodetect"] = autodetect
    else:
        snap["system_proxy"] = "(读取失败)"
    snap["env_proxy"] = {k: os.environ[k] for k in
                         ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
                          "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
                         if os.environ.get(k)} or "(未设置)"
    snap["verify_tls"] = NET_VERIFY_TLS
    snap["route"] = network_summary()
    return snap


def export_report(out_dir=None, revealed=None):
    """把结构化事件渲染成反馈日志并落盘。返回写入路径。

    revealed 为 None 时按 log.export_reveal_secrets 决定是否脱敏。
    """
    out_dir = Path(out_dir) if out_dir else _APP_DIR
    reveal = LOG_EXPORT_REVEAL if revealed is None else bool(revealed)
    name = "czn-lite-report-%s%s.txt" % (
        (APP_VERSION + "-") if APP_VERSION else "",
        time.strftime("%Y%m%d-%H%M%S"))
    path = out_dir / name

    lines = []
    lines.append("CZN Launcher Lite 反馈日志")
    lines.append("生成时间 : %s" % time.strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("版本     : %s" % (APP_VERSION or "(未标注)"))
    lines.append("可执行   : %s" % sys.executable)
    lines.append("冻结运行 : %s" % bool(getattr(sys, "frozen", False)))
    lines.append("脱敏状态 : %s" % ("★ 未脱敏（含明文凭据，切勿外发）" if reveal
                                    else "已脱敏，可直接发送"))
    lines.append("说明     : 本文件按字段级规则脱敏：令牌/密码只保留长度，"
                 "邮箱保留首字母与域名，member_no/guid 保留后几位，"
                 "会话类 ID 保留前 8 位。")
    lines.append("")
    if LOG_ENV_SNAPSHOT:
        lines.append("---- 环境快照 ----")
        # 快照里含系统/环境变量代理串，必须与事件流走同一套脱敏
        for key, val in mask_obj(env_snapshot()).items():
            lines.append("%-14s %s" % (key, val))
        lines.append("")
    lines.append("---- 事件流 ----")
    for ev in events():
        lines.extend(render_report(ev, reveal=reveal))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _record_api(tag, status, data):
    """把一次 API 响应的要点记成事件（token 只记原文，导出时按字段名脱敏）。"""
    fields = {"stage": stage(), "tag": tag, "status": status}
    if isinstance(data, dict):
        fields["code"] = data.get("code")
        fields["message"] = str(data.get("message") or "")[:120]
        value = data.get("value")
        if isinstance(value, dict):
            fields["keys"] = sorted(value.keys())[:20]
            for key in ("access_token", "refresh_token"):
                if value.get(key):
                    fields[key] = value[key]
    record("api", **fields)

# ---- 路径规范化与校验 ----
# 用户填 install_root 的方式千奇百怪：正斜杠、尾部多一个反斜杠、带引号、
# 指到 bin 子目录、甚至直接指到某个 exe。这里统一收敛成「游戏根目录」。
def normalize_install_root(raw):
    """把各种写法统一成游戏根目录。返回 (路径, 说明列表)。"""
    notes = []
    if not raw:
        return "", notes
    s = str(raw).strip().strip('"').strip("'").strip()
    if not s:
        return "", notes
    if s != str(raw).strip():
        notes.append("去掉首尾空白/引号：%r -> %r" % (raw, s))
    s = os.path.expandvars(os.path.expanduser(s))
    if "/" in s:
        notes.append("正斜杠已转为反斜杠")
        s = s.replace("/", "\\")

    if s.lower().endswith(".exe"):
        notes.append("指向了 exe 文件，上跳两级取游戏根")
        s = os.path.dirname(os.path.dirname(s))
    elif os.path.basename(s.rstrip("\\/")).lower() == "bin":
        notes.append("指向了 bin 目录，上跳一级取游戏根")
        # 先去掉尾部分隔符再 dirname —— 否则 "...\bin\" 只会被削成 "...\bin"
        s = os.path.dirname(s.rstrip("\\/"))

    norm = os.path.normpath(s)
    if norm != s:
        notes.append("normpath：%r -> %r" % (s, norm))
    return norm, notes


def game_exe_probe(root, exe_rel=None):
    """检查 root 下是否有游戏主程序。返回 (是否存在, 完整路径)。

    精确路径即可 —— Windows 文件系统不区分大小写，配置里写哪种大小写都能命中。

    exe_rel 必须运行时取全局：默认参数在 def 时就绑死了，玩家改了
    config 里的 game.game_exe 也不会生效。
    """
    if not root:
        return False, "install_root 为空"
    exe = os.path.join(root, exe_rel or GAME_EXE_REL)
    return os.path.exists(exe), exe


def game_exe_name():
    """主程序文件名，供界面文案使用（随 game.game_exe 变化，不写死）。"""
    return os.path.basename(GAME_EXE_REL)


def detect_install_root_from_registry():
    """从注册表取官方记录的安装路径（权威来源）。

    官方安装器写在 HKCU\\SOFTWARE\\SGUP\\apps\\<GAME_ID>\\GamePath。
    比全盘盲扫可靠：不受盘符、目录层级、文件夹改名影响。
    返回 (路径, 说明)。
    """
    if os.name != "nt":
        return "", "非 Windows 环境"
    sub = r"SOFTWARE\SGUP\apps\%s" % GAME_ID
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub) as key:
            gp, _ = winreg.QueryValueEx(key, "GamePath")
    except OSError as e:
        return "", "注册表 %s 读取失败：%s" % (sub, e)
    if not gp:
        return "", "注册表 GamePath 为空"
    gp, _n = normalize_install_root(gp)
    ok, detail = game_exe_probe(gp)
    if ok:
        return gp, "注册表 %s\\GamePath" % sub
    return "", "注册表 GamePath=%r 下未找到游戏主程序（%s）" % (gp, detail)


# ---- 本机动态值（换设备会变，源码不给默认值） ----
INSTALL_ROOT_RAW = _cfg("game", "install_root", default="")
INSTALL_ROOT, _ROOT_NOTES = normalize_install_root(INSTALL_ROOT_RAW)
CONFIG_NOTES.extend(_ROOT_NOTES)
GAME_EXE_PATH = os.path.join(INSTALL_ROOT, GAME_EXE_REL) if INSTALL_ROOT else GAME_EXE_REL

# 账号服务区（gds）：官方上报的是它检测到的出口地区；直连场景下没有
# 「可检测的服务区」，因此取账号服务区（默认 JP），可在 config.json 调整。
_DEFAULT_GDS = {"is_default": False, "nation": "JP", "regulation": "ETC",
                "timezone": "Asia/Tokyo", "utc_offset": 540, "lang": "en"}
_gds_cfg = _cfg("gds", default=None)
GDS_INFO = dict(_gds_cfg) if isinstance(_gds_cfg, dict) and _gds_cfg else dict(_DEFAULT_GDS)

# ---- 本机状态文件 ----
STATE_FILE = Path(_cfg("state_file",
                       default=str(_APP_DIR / "state.json"))).expanduser()


def _migrate_legacy_state():
    """旧版本把 state.json 放在 ~/.czn-lite/，这里一次性复制到 exe 同目录。
    旧文件保留不删（防呆），失败不影响启动。"""
    try:
        if STATE_FILE.exists():
            return
        legacy = Path.home() / ".czn-lite" / "state.json"
        if legacy.exists():
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_bytes(legacy.read_bytes())
            print("[+] 已迁移旧凭据：%s -> %s" % (legacy, STATE_FILE))
    except Exception as e:
        print("[!] 旧凭据迁移失败（不影响启动）：%s" % e)


_migrate_legacy_state()


# ====================================================================
# 本机信息采集
# ====================================================================
def _caller_detail():
    """读取注册表 HKCU\\SOFTWARE\\SGUP\\CallerDetail（官方安装时写入的安装指纹，
    平台 API 公共请求头 Caller-Detail 的值，缺失时平台接口会拒绝请求）。"""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"SOFTWARE\SGUP")
        value, _ = winreg.QueryValueEx(key, "CallerDetail")
        winreg.CloseKey(key)
        return value
    except Exception:
        return None


def _ensure_caller_detail():
    """拿到 CallerDetail；全新环境（从未装过官方 STOVE）没有就生成并写回。

    依据《从0环境完整进游戏_全新安装方案_v1》§4-G1：官方 STOVESetup 会在
    HKCU\\SOFTWARE\\SGUP 写 40-hex 安装指纹，我们模仿同款行为（值格式对齐
    4a54eb8fe96aa7644aae18adc4bd5dcfd2ae9f7b）。服务端是否校验指纹值未验证，
    故另有 70702 自愈重试（StoveAuth.game_check 内）。"""
    value = _caller_detail()
    if value:
        return value
    import secrets
    value = secrets.token_hex(20)
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"SOFTWARE\SGUP",
                                 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "CallerDetail", 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        print("[*] 全新环境：已生成并写入 CallerDetail 安装指纹")
    except Exception as exc:
        print("[!] CallerDetail 写注册表失败（本次请求仍携带生成值）：%s" % exc)
    return value


def _rotate_caller_detail():
    """强制换新 CallerDetail 并写回注册表（gc/check 70702 自愈重试用）。"""
    import secrets
    value = secrets.token_hex(20)
    try:
        key = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"SOFTWARE\SGUP",
                                 0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(key, "CallerDetail", 0, winreg.REG_SZ, value)
        winreg.CloseKey(key)
        print("[*] CallerDetail 已轮换（自愈重试）")
    except Exception as exc:
        print("[!] CallerDetail 轮换写注册表失败：%s" % exc)
    return value


def machine_guid():
    """读取注册表 HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid。
    仅用于诊断输出；官方 signin 请求体经抓包确认不含 device_id。"""
    if os.name != "nt":
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                            r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
            return winreg.QueryValueEx(key, "MachineGuid")[0]
    except Exception as e:
        print("[!] 读取 MachineGuid 失败：%s" % e)
        return ""


def import_required_info_from_official_log():
    """只读解析官方启动器日志，取最近一次 REQUIRED_INFO 明文（完整 dict）。

    官方启动器会把管道 2001 的明文写入日志：
      %LOCALAPPDATA%\\STOVE\\Logs\\StoveLauncher\\StoveLauncher*.log
      行内容形如 `sendRequiredInfo decrypted value : {完整 JSON}`
    其中包含 guid、reg_dt、birth_dt 等登录响应里拿不到的账号固有字段，
    是这些字段的权威来源。注意通配须同时匹配 StoveLauncher.log 与
    StoveLauncher_*.log。找不到返回 None，绝不修改任何文件。"""
    base = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                        "STOVE", "Logs", "StoveLauncher")
    if not os.path.isdir(base):
        return None
    import glob
    marker = "sendRequiredInfo decrypted value : "
    best = None  # (mtime, dict)
    for fn in glob.glob(os.path.join(base, "StoveLauncher*.log")):
        try:
            with open(fn, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    pos = line.find(marker)
                    if pos < 0:
                        continue
                    try:
                        data = json.loads(line[pos + len(marker):].strip())
                    except Exception:
                        continue
                    if not data.get("guid"):
                        continue
                    mtime = os.path.getmtime(fn)
                    if best is None or mtime >= best[0]:
                        best = (mtime, data)
        except Exception:
            continue
    return best[1] if best else None


def import_guid_from_official_log():
    """从官方启动器日志导入 guid（publisher 标识，与 member_no 不同）。"""
    data = import_required_info_from_official_log()
    if not data:
        return None
    guid = str(data.get("guid"))
    print("[+] 已从官方启动器日志导入 guid：%s" % guid)
    return guid


def collect_local_info():
    """零联网采集本机与本账号可发现的固有信息（全部只读）。

    采集内容：MachineGuid、CallerDetail、官方日志中的账号固有字段、
    安装路径与游戏主程序存在性；主程序未命中时在固定盘符的常见层级做
    有界探测（主程序文件名是游戏写死常量，跨设备可靠）。"""
    info = {}
    machine_guid_value = machine_guid()
    if machine_guid_value:
        info["machine_guid"] = machine_guid_value
    caller_detail = _caller_detail()
    if caller_detail:
        info["caller_detail"] = caller_detail

    log_data = import_required_info_from_official_log()
    info["official_log_found"] = bool(log_data)
    if log_data:
        for key in ("guid", "reg_dt", "birth_dt", "member_no", "member_nickname",
                    "country_cd", "account_type", "provider_cd",
                    "person_verify_yn", "parent_verify_yn", "email_verify_yn"):
            if log_data.get(key) not in (None, ""):
                info[key] = log_data[key]

    info["install_root"] = INSTALL_ROOT
    info["game_exe_found"] = os.path.exists(GAME_EXE_PATH)
    if not info["game_exe_found"]:
        detected, trace = detect_install_root()
        if not detected:
            trace.append("全部失败 —— 需在 config.json 里设置 game.install_root")
        info["install_root_detected"] = detected
        info["detect_trace"] = trace
    return info


def detect_install_root():
    """定位游戏安装目录。返回 (路径 或 "", 轨迹列表)。

    顺序（可靠性由高到低）：
      1. 注册表 apps\\<GAME_ID>\\GamePath —— 官方写入，权威
      2. 常见层级的有界探测 —— 兜底，覆盖有限
    每一步都记录轨迹，失败时能看清「试过什么、为什么没中」。
    """
    import glob
    trace = []

    # ① 注册表（权威）
    reg_path, reg_note = detect_install_root_from_registry()
    trace.append("注册表: %s" % reg_note)
    if reg_path:
        return reg_path, trace

    # ② 有界探测：盘符 + 层级都放宽，并逐条记录
    drives = []
    for letter in "CDEFGHIJ":
        d = letter + ":\\"
        if os.path.exists(d):
            drives.append(letter + ":")
    trace.append("可用盘符: %s" % drives)

    exe_name = os.path.basename(GAME_EXE_REL)
    trace.append("探测目标: %s" % GAME_EXE_REL)
    patterns = [
        r"{d}\ChaosZeroNightmare\bin\{e}",
        r"{d}\Games\ChaosZeroNightmare\bin\{e}",
        r"{d}\Games\*\bin\{e}",
        r"{d}\*\Games\ChaosZeroNightmare\bin\{e}",
        r"{d}\*\*\Games\ChaosZeroNightmare\bin\{e}",
        r"{d}\*\Games\*\bin\{e}",
        r"{d}\Games\*\*\bin\{e}",
        r"{d}\SteamLibrary\steamapps\common\ChaosZeroNightmare\bin\{e}",
    ]
    for tpl in patterns:
        for d in drives:
            pat = tpl.format(d=d, e=exe_name)
            try:
                hits = glob.glob(pat)
            except Exception as e:
                trace.append("异常 %s -> %r" % (pat, e))
                continue
            if hits:
                found = os.path.dirname(os.path.dirname(hits[0]))
                trace.append("命中 %s -> %s" % (pat, found))
                return found, trace
    trace.append("探测模式全部未命中（共 %d 条 × %d 盘符）"
                 % (len(patterns), len(drives)))
    return "", trace


def set_install_root(root):
    """把本机安装路径写入 config.json 并即时生效（换设备自主适配）。
    只修改 game.install_root 一个键，其余配置保持不变。
    写入前先规范化，避免把 bin/ 或 exe 这种错误层级存进去。"""
    root, _notes = normalize_install_root(root)
    # 必须走容错链读取：否则配置有语法错时会把其余键全部丢掉（用户配置被清空）
    data = {}
    if _CONFIG_FILE.exists():
        try:
            data = _loads_config(
                _decode_config(_CONFIG_FILE.read_bytes()) or "") or {}
        except OSError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("game", {})["install_root"] = root
    _CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    global INSTALL_ROOT, GAME_EXE_PATH
    INSTALL_ROOT = root
    GAME_EXE_PATH = os.path.join(root, GAME_EXE_REL) if root else GAME_EXE_REL
    return root


def clear_device_config():
    """「清空离线信息」的配置侧：只把本机动态键（install_root）置空回初始态，
    常量键一律不动。"""
    set_install_root("")


# ---- 网络设置（界面可改，写回 config.json 并即时生效） ----
def network_config():
    """当前网络设置（供界面回显）。"""
    return {"mode": NET_MODE, "manual_url": NET_MANUAL_URL,
            "manual_username": NET_MANUAL_USER,
            "manual_password": NET_MANUAL_PASS,
            "manual_bypass": NET_MANUAL_BYPASS}


def save_network_config(mode=None, manual_url=None, manual_username=None,
                        manual_password=None, manual_bypass=None):
    """只改 network 段的这几个键，其余配置保持不变；写完清路由缓存即时生效。"""
    data = {}
    if _CONFIG_FILE.exists():
        try:
            data = _loads_config(
                _decode_config(_CONFIG_FILE.read_bytes()) or "") or {}
        except OSError:
            data = {}
    if not isinstance(data, dict):
        data = {}
    node = data.get("network")
    if not isinstance(node, dict):
        node = {}
        data["network"] = node
    for key, value in (("mode", mode), ("manual_url", manual_url),
                       ("manual_username", manual_username),
                       ("manual_password", manual_password),
                       ("manual_bypass", manual_bypass)):
        if value is not None:
            node[key] = value
    _CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")

    global NET_MODE, NET_MANUAL_URL, NET_MANUAL_USER, NET_MANUAL_PASS, \
        NET_MANUAL_BYPASS
    NET_MODE = str(node.get("mode", NET_MODE) or "direct").strip().lower()
    NET_MANUAL_URL = str(node.get("manual_url", NET_MANUAL_URL) or "")
    NET_MANUAL_USER = str(node.get("manual_username", NET_MANUAL_USER) or "")
    NET_MANUAL_PASS = str(node.get("manual_password", NET_MANUAL_PASS) or "")
    NET_MANUAL_BYPASS = str(node.get("manual_bypass", NET_MANUAL_BYPASS) or "")
    reset_route_cache()
    return network_config()


# ====================================================================
# 0. 账号密码登录用的密码字段变换
# --------------------------------------------------------------------
# signin(provider_cd="SO") 的 provider_data.password 不是明文，而是：
#     AES-128-ECB(PINE_KEY, PKCS7(utf8(pw))).hex().upper()
# 密钥来自 IdentityLib 3.2.28 运行时提取。它与库版本绑定，
# STOVE 更新后若登录异常需重新提取。
# ====================================================================
PINE_ENCRYPT_KEY = "5d41037aadbc92a755ee6b86257d5ee9"


def stove_password_field(password, pine_key=PINE_ENCRYPT_KEY):
    """明文密码 -> 32 位大写 hex。"""
    key = bytes.fromhex(pine_key)
    if len(key) != 16:
        raise ValueError("PINE key 必须是 16 字节（32 hex）")
    data = password.encode("utf-8")
    pad = 16 - len(data) % 16
    return AES.new(key, AES.MODE_ECB).encrypt(data + bytes([pad]) * pad).hex().upper()


# ====================================================================
# 1. 认证链
# --------------------------------------------------------------------
# 登录与令牌兑换的完整链路（与官方启动器逐字段/逐头对齐）：
#   ① POST /sign/v2.1/pc/signin（provider_cd=RT, refresh_token）
#        → 299 字符启动器级令牌，refresh_token 同时被轮换（必须保存新值）
#   ② POST /gc/v1.4/check/{game_id}，Authorization: bearer <①的令牌>
#        → 384 字符游戏级令牌（REQUIRED_INFO 必须使用它，否则 41002）
# 游戏级响应中的 value.user.user_id 即 REQUIRED_INFO 的 guid（与 member_no 不同）。
# ====================================================================
class _LoggedSession:
    """包一层 curl_cffi Session：统一解析网络路径 + 记录请求/响应事件。

    这样全部调用点（czn_lite 的 11 处 + captcha 的 3 处）零改动即可获得
    路由决策与详细日志 —— 「全覆盖」是结构性的，不会漏点。
    """

    def __init__(self, session):
        self._s = session

    # ---- 属性透传（调用方仍在用 .headers 等） ----
    @property
    def headers(self):
        return self._s.headers

    @property
    def cookies(self):
        return self._s.cookies

    def __getattr__(self, name):
        return getattr(self._s, name)

    def get(self, url, **kw):
        return self._request("GET", url, **kw)

    def post(self, url, **kw):
        return self._request("POST", url, **kw)

    def request(self, method, url, **kw):
        return self._request(method, url, **kw)

    def _request(self, method, url, **kw):
        route = resolve_route(url)
        # ★ 必须显式传：直连时为 {"all": ""}，否则被环境变量静默劫持
        kw.setdefault("proxies", route.proxies)
        # 超时统一成 (连接, 读取)：连接上界取 network.connect_timeout，
        # 读取沿用调用方给的值（调用方给的是总超时，这里只补连接上界）
        read_t = kw.get("timeout") or NET_READ_TIMEOUT
        if NET_CONNECT_TIMEOUT > 0 and isinstance(read_t, (int, float)):
            kw["timeout"] = (min(NET_CONNECT_TIMEOUT, read_t), read_t)
        else:
            kw["timeout"] = read_t
        if not NET_VERIFY_TLS:
            kw["verify"] = False
        if NET_RETRY:
            kw.setdefault("retry", NET_RETRY)

        body = kw.get("json")
        # 实际发出的请求头 = 会话级 + 本次请求级（调用方都用后者）
        sent_headers = dict(self._s.headers or {})
        sent_headers.update(kw.get("headers") or {})
        record("req", stage=stage(), method=method, url=url, host=route.host,
               proxy=(route.proxies.get("all") or "直连"),
               route_source=route.source,
               headers=sent_headers,
               body=body if isinstance(body, dict) else None,
               params=kw.get("params"))
        started = time.time()
        try:
            resp = self._s.request(method, url, **kw)
        except Exception as e:
            record("error", stage=stage(), where="%s %s" % (method, url),
                   type=type(e).__name__, message=str(e)[:400],
                   ms=int((time.time() - started) * 1000))
            raise
        record("resp", stage=stage(), method=method, host=route.host,
               status=getattr(resp, "status_code", None),
               ms=int((time.time() - started) * 1000))
        return resp


class StoveAuth:
    """STOVE 认证：二维码登录 → 令牌续期 → 游戏级令牌兑换。"""

    def __init__(self, gds=None):
        # curl_cffi 的 impersonate="chrome" 只为 TLS/JA3 指纹：STOVE 的 CDN 按
        # JA3 拦截（Python 原生指纹直接被重置），Chrome 指纹放行，必须保留。
        # HTTP 层与官方对齐：
        #   default_headers=False  关闭 libcurl 注入的 Chrome 浏览器头
        #   http_version=HTTP/1.1  官方所有端点均为 HTTP/1.1（Chrome 指纹默认 h2）
        #   会话头清空，每个请求经 _official_headers() 按官方线序完整构造
        # 再包一层 _LoggedSession：统一解析网络路径 + 记录请求/响应事件
        self.s = _LoggedSession(requests.Session(
            impersonate="chrome", default_headers=False,
            http_version=CurlHttpVersion.V1_1))
        self.s.headers.clear()
        self.gds = gds or dict(GDS_INFO)
        # 官方一次启动会话内所有请求共用同一个 Transaction-ID，且 gc/check
        # 请求体里的 device_key 与它是同一个 UUID（会话级，非每请求随机）
        self.session_tid = str(uuid.uuid4())

        # 启动器级令牌（signin/续期结果；refresh_token 每次轮换，须持久化新值）
        self.launcher_access = None
        self.launcher_refresh = None
        # 账号字段（登录/续期响应）
        self.member_no = None
        self.guid = None
        self.nickname = None
        self.user_id = None
        self.expire_in = None
        self.provider_cd = None
        self.required_provider_cd = None
        self.country_cd = None
        self.cafe_key = None
        self.person_verify_yn = None
        self.email_verify_yn = None
        self.parent_verify_yn = None
        self.reg_dt = None
        self.birth_dt = None
        self.member_account_type = None
        self.signin_provider_cd = None
        # 游戏级令牌信息（gc/v1.4/check 兑换结果，REQUIRED_INFO 使用它）
        self.game_access_token = None
        self.game_refresh_token = None
        self.game_member_no = None
        self.game_guid = None
        self.game_nickname = None
        self.game_expire_in = None
        self.game_cafe_key = None
        self.game_provider_cd = None
        self.game_account_type = None
        self.game_member = None

    # ---- 请求头 ----
    def _api_headers(self):
        """官方平台 API 公共请求头（值来自官方流量抓包）：
        Caller-Detail 来自注册表；Transaction-ID 为会话级 UUID；
        X-Lang 是启动器 UI 语言（官方恒为 zh-cn），与 gds_info.lang 是两回事。"""
        headers = {
            "Caller-ID": CALLER_ID,
            "X-Lang": "zh-cn",
            "X-Nation": self.gds.get("nation", "JP"),
            "X-Timezone": self.gds.get("timezone", "Asia/Tokyo"),
            "X-Utc-Offset": str(int(self.gds.get("utc_offset", 540))),
        }
        caller_detail = _ensure_caller_detail()
        if caller_detail:
            headers["Caller-Detail"] = caller_detail
        headers["Transaction-ID"] = self.session_tid
        return headers

    def _official_headers(self, extra=None):
        """构造与官方完全一致的请求头集。

        官方线序 = Host（libcurl 自动）+ 其余头按名称字母序 + Content-Length。
        所有请求统一经此构造，官方没有的头一个都不多发。"""
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "deflate, gzip",
            "Accept-Language": "zh-CN",
            "Content-Type": "application/json",
            "User-Agent": "STOVEAPIClient/3.0",
        }
        headers.update(self._api_headers())
        if extra:
            headers.update(extra)
        return dict(sorted(headers.items(), key=lambda kv: kv[0].lower()))

    # ---- 基础请求 ----
    def _parse(self, response, tag):
        try:
            data = response.json()
        except Exception:
            raise RuntimeError("[%s] 非 JSON 响应 %d: %s"
                               % (tag, response.status_code, response.text[:200]))
        # 事件记录：界面明文、导出按字段名脱敏（access_token / refresh_token 等）
        _record_api(tag, response.status_code, data)
        limit = 2000 if ("signin" in tag or "refresh" in tag) else 400
        print("[dbg][%s] HTTP %d: %s"
              % (tag, response.status_code,
                 json.dumps(data, ensure_ascii=False)[:limit]))
        if response.status_code != 200:
            raise RuntimeError("[%s] HTTP %d" % (tag, response.status_code))
        return data

    # ---- 二维码登录 ----
    def qr_create(self):
        """申请扫码登录二维码。官方请求体恰为 4 键（不含 client_id/service_id）。"""
        set_stage("扫码登录")
        body = {"login_type": "QR_LOGIN_LAUNCHER", "gds_info": self.gds,
                "width": 180, "height": 180}
        r = self.s.post(API + "/auth-secure/v1.0/qr", json=body,
                        headers=self._official_headers(), timeout=15)
        return self._parse(r, "qr_create").get("value", {})

    def qr_status(self, session):
        """轮询扫码状态：init → ongoing → success / expired / close。"""
        r = self.s.get(API + "/auth-secure/v1.0/qr/status",
                       params={"session": session},
                       headers=self._official_headers(), timeout=15)
        return self._parse(r, "qr_status")

    def signin_qr(self, qr_session):
        """扫码成功后的登录确认。"""
        set_stage("扫码登录")
        return self._signin("QR", {"qr_login_session": qr_session})

    # ---- 账号密码登录（provider_cd="SO"）----
    # 请求体与扫码同构，只换 provider_data。
    # 验证码 token 走 `Captcha-Token` 请求头；怎么拿 token 由 captcha.py 负责。
    # 返回响应 dict 而不抛异常：49700 表示需要验证码，是正常中间态。
    def signin_password(self, user_id, password, captcha_token=None):
        """账号密码登录。返回响应 dict；`code==49700` 表示需要验证码。"""
        set_stage("账号密码登录")
        body = {"client_id": CLIENT_ID, "service_id": "Launcher",
                "provider_cd": "SO",
                "provider_data": {"user_id": user_id,
                                  "password": stove_password_field(password)},
                "gds_info": self.gds}
        extra = {"Captcha-Token": captcha_token} if captcha_token else None
        r = self.s.post(API_BASE + "/sign/v2.1/pc/signin", json=body,
                        headers=self._official_headers(extra), timeout=20)
        data = self._parse(r, "signin_so")
        if data.get("code") in (0, None):
            self.signin_provider_cd = "SO"
            self._apply_launcher(data)
        return data

    def _signin(self, provider_cd, provider_data):
        """登录。官方请求体不含 device_id；验证码重试走 Captcha-Token 请求头。"""
        for _attempt in range(3):
            body = {"client_id": CLIENT_ID, "service_id": "Launcher",
                    "provider_cd": provider_cd, "provider_data": provider_data,
                    "gds_info": self.gds}
            extra = None
            if getattr(self, "_captcha_token", None):
                extra = {"Captcha-Token": self._captcha_token}
            r = self.s.post(API_BASE + "/sign/v2.1/pc/signin", json=body,
                            headers=self._official_headers(extra), timeout=15)
            data = self._parse(r, "signin")
            if data.get("code") == 49700:
                print("[!] 服务端要求图片验证码")
                self._captcha_token = self._do_image_captcha()
                continue
            if data.get("code") not in (0, None):
                raise RuntimeError("[signin] code=%s msg=%s"
                                   % (data.get("code"), data.get("message")))
            self.signin_provider_cd = provider_cd
            self._apply_launcher(data)
            return data
        raise RuntimeError("[signin] 验证码三次未通过")

    def _do_image_captcha(self):
        """图片验证码：取验证码 → 打开图片 → 用户输入 → 校验换取 captchaToken。
        临时图片保存在脚本同目录，用完即删。"""
        data = self.captcha_keys().get("value", {})
        captcha_key = data.get("captcha_key")
        image_url = data.get("image_url")
        if not image_url:
            raise RuntimeError(
                "[captcha] 服务端未返回验证码图片地址，验证码流程不可用；"
                "请使用扫码登录")
        png = Path(__file__).with_name("captcha.png")
        try:
            png.write_bytes(self.s.get(image_url, timeout=15,
                                       headers=self._official_headers()).content)
            print("[+] 验证码图片已保存：%s（%d 字节）" % (png, png.stat().st_size))
            os.startfile(str(png))
            answer = input("验证码图片已打开，输入图中字符：").strip()
            r = self.s.post(API + "/blockchecker/v3.0/captcha/verify",
                            json={"captcha_key": captcha_key,
                                  "captcha_value": answer},
                            headers=self._official_headers(), timeout=15)
            result = self._parse(r, "captcha_verify")
            token = (result.get("value", {}).get("captcha_token")
                     or result.get("value", {}).get("token"))
            if not token:
                raise RuntimeError("[captcha] 校验响应中没有 token: %s"
                                   % json.dumps(result)[:200])
            return token
        finally:
            if png.exists():
                png.unlink()
                print("[*] 临时验证码图片已清理：%s" % png)

    def captcha_keys(self):
        r = self.s.post(API + "/blockchecker/v1.0/captcha/keys",
                        headers=self._official_headers(), timeout=15)
        return self._parse(r, "captcha_keys")

    def captcha_verify(self, captcha_key, user_input):
        r = self.s.post(API + "/blockchecker/v1.0/captcha/verify",
                        json={"captcha_key": captcha_key, "input": user_input},
                        headers=self._official_headers(), timeout=15)
        return self._parse(r, "captcha_verify")

    # ---- 登录响应解析 ----
    def _apply_launcher(self, data):
        """从登录/续期响应中递归提取账号字段。

        guid 的键名是 value.user.user_id（下划线），登录响应里它的值等于
        member_no，仅作占位；真正的 guid（游戏级）在 gc/check 兑换响应中，
        由 resolve_guid() 兜底从官方启动器日志导入。"""
        def find(obj, keys):
            found = {}
            if isinstance(obj, dict):
                for key, value in obj.items():
                    if key in keys and isinstance(value, (str, int)):
                        found[key] = value
                    elif isinstance(value, (dict, list)):
                        found.update(find(value, keys))
            elif isinstance(obj, list):
                for item in obj:
                    found.update(find(item, keys))
            return found

        flat = find(data, {"access_token", "refresh_token", "member_no",
                           "memberNumber", "guid", "user_id", "userId",
                           "nickname", "expires_in", "expire_in", "provider_cd",
                           "country_cd", "cafe_key", "account_type", "reg_dt",
                           "birth_dt", "person_verify_yn", "email_verify_yn",
                           "parent_verify_yn"})
        self.launcher_access = flat.get("access_token") or self.launcher_access
        self.launcher_refresh = flat.get("refresh_token") or self.launcher_refresh
        if not self.member_no:
            self.member_no = str(flat.get("member_no")
                                 or flat.get("memberNumber") or "")
        guid = (flat.get("guid") or flat.get("user_id")
                or flat.get("userId") or self.guid)
        self.guid = str(guid) if guid is not None else self.guid
        self.nickname = flat.get("nickname") or self.nickname
        self.expire_in = flat.get("expires_in") or flat.get("expire_in") or self.expire_in
        if flat.get("provider_cd") is not None:
            self.provider_cd = flat.get("provider_cd")
        self.country_cd = flat.get("country_cd") or self.country_cd
        self.cafe_key = flat.get("cafe_key") or self.cafe_key
        self.person_verify_yn = flat.get("person_verify_yn") or self.person_verify_yn
        self.email_verify_yn = flat.get("email_verify_yn") or self.email_verify_yn
        self.parent_verify_yn = flat.get("parent_verify_yn") or self.parent_verify_yn
        if flat.get("reg_dt") is not None:
            self.reg_dt = flat.get("reg_dt")
        if flat.get("birth_dt") is not None:
            self.birth_dt = flat.get("birth_dt")
        if flat.get("account_type") is not None:
            self.member_account_type = flat.get("account_type")

    # ---- 续期与游戏级令牌兑换 ----
    def renew_by_signin_rt(self):
        """静默续期（官方同款）：POST /sign/v2.1/pc/signin，provider_cd=RT，
        provider_data 携带 refresh_token。请求体与官方逐键一致（无 device_id）。

        成功返回 (access_token, refresh_token, member, user)；
        失败返回 None。refresh_token 每次轮换，调用方必须保存新值。"""
        set_stage("令牌续期")
        if not self.launcher_refresh:
            print("[!] 没有可用的 refresh_token，跳过续期")
            return None
        body = {"client_id": CLIENT_ID, "service_id": "Launcher",
                "provider_cd": "RT",
                "provider_data": {"refresh_token": self.launcher_refresh},
                "gds_info": self.gds}
        r = self.s.post(API_BASE + "/sign/v2.1/pc/signin", json=body,
                        headers=self._official_headers(), timeout=15)
        data = self._parse(r, "signin_RT")
        if data.get("code") not in (0, None):
            print("[!] 续期失败 code=%s msg=%s"
                  % (data.get("code"), data.get("message")))
            if data.get("code") == 44010:
                print("[!] 44010 表示该 refresh_token 已失效（官方启动器在别处"
                      "登录过同一账号，或凭据放置过久），请重新扫码登录")
            return None
        value = data.get("value") or {}
        if not isinstance(value, dict):
            return None
        member = value.get("member") if isinstance(value.get("member"), dict) else {}
        user = value.get("user") if isinstance(value.get("user"), dict) else {}
        if not user and member.get("user"):
            user = member["user"]
        access, refresh = value.get("access_token"), value.get("refresh_token")
        self._last_token_response = value
        print("[+] 续期成功：member_no=%s user_id=%s"
              % (member.get("member_no"), user.get("user_id")))
        return access, refresh, member, user

    def game_check(self, skip_provider=False, skip_session=False):
        """POST /gc/v1.4/check/{game_id}：官方启动序列中 signin 之后的预检。

        注意两点（均经官方抓包/实机确认）：
          · 鉴权只认 299 字符启动器级令牌（384 游戏级令牌会被 400000 拒绝）
          · device_key 与请求头 Transaction-ID 是同一个会话级 UUID
        返回 (http_status, json 或 None, 原始文本)。"""
        set_stage("兑换游戏级令牌")
        body = {
            "device_info": {"device_key": str(self.session_tid)},
            "gds_info": {
                "is_default": bool(self.gds.get("is_default", False)),
                "nation": self.gds.get("nation", "JP"),
                "regulation": self.gds.get("regulation", "ETC"),
                "timezone": self.gds.get("timezone", "Asia/Tokyo"),
                "utc_offset": int(self.gds.get("utc_offset", 540)),
                "lang": self.gds.get("lang", "en"),
            },
            "skip_provider_check": bool(skip_provider),
            "skip_session_check": bool(skip_session),
        }
        token = self.launcher_access or self.game_access_token
        # 没有任何令牌时**不发** Authorization 头 —— 发 "bearer None" 是假凭据，
        # 既污染日志又可能被服务端按异常请求记账
        auth = {"Authorization": "bearer " + str(token)} if token else {}
        print("[dbg][gc/check] Transaction-ID = device_key = %s（会话级同值）"
              % self.session_tid)
        headers = self._official_headers({
            **auth,
            "market-name": "PC_MARKET",
            "Captcha-Token": "",
        })
        r = self.s.post(API_BASE + "/gc/v1.4/check/" + GAME_ID, json=body,
                        headers=headers, timeout=20)
        try:
            data = r.json()
        except Exception:
            data = None
        # 70702 = 服务端拒绝 Caller-Detail（全新环境指纹缺失/失效）：
        # 轮换安装指纹后原样重试一次（自愈路径）
        if isinstance(data, dict) and str(data.get("code")) == "70702":
            if _rotate_caller_detail():
                headers = self._official_headers({
                    **auth,
                    "market-name": "PC_MARKET",
                    "Captcha-Token": "",
                })
                r = self.s.post(API_BASE + "/gc/v1.4/check/" + GAME_ID, json=body,
                                headers=headers, timeout=20)
                try:
                    data = r.json()
                except Exception:
                    data = None
        return r.status_code, data, r.text

    def game_token(self):
        """兑换游戏级令牌（两步，缺第二步必报 41002）：
          ① 续期获得 299 字符启动器级令牌
          ② gc/v1.4/check 用它兑换 384 字符游戏级令牌
        成功返回 (access_token, refresh_token, member, user)。"""
        renewed = self.renew_by_signin_rt()
        if renewed:
            self.launcher_access = renewed[0]
            if renewed[1]:
                self.launcher_refresh = renewed[1]
            print("[+] 启动器级令牌长度 %d（仅此级别可通过 gc/check 鉴权）"
                  % len(str(renewed[0])))

        try:
            status, data, _text = self.game_check()
            if isinstance(data, dict) and isinstance(data.get("value"), dict):
                value = data["value"]
                access = value.get("access_token")
                if access:
                    print("[+] 游戏级令牌兑换成功，长度 %d" % len(access))
                    return (access,
                            value.get("refresh_token")
                            or (renewed[1] if renewed else self.launcher_refresh),
                            value.get("member") or (renewed[2] if renewed else {}),
                            value.get("user") or (renewed[3] if renewed else {}))
            print("[!] gc/v1.4/check 未返回令牌（HTTP %s；401=鉴权失败，"
                  "400000=令牌级别不符），回退启动器级令牌" % status)
        except Exception as e:
            print("[!] gc/v1.4/check 异常，回退启动器级令牌：%s" % e)

        # 兜底：/sign/v2.0/pc/refresh（官方从不调用，仅在续期失败时使用）。
        # 该端点响应为 Launcher 作用域，轮换出的 refresh_token 可以持久化。
        if renewed:
            return renewed
        if not self.launcher_refresh:
            print("[!] 没有 refresh_token，无法兑换游戏级令牌")
            return None
        gds = {
            "is_default": bool(self.gds.get("is_default", False)),
            "nation": self.gds.get("nation", "JP"),
            "regulation": self.gds.get("regulation", "ETC"),
            "timezone": self.gds.get("timezone", "Asia/Tokyo"),
            "utc_offset": int(self.gds.get("utc_offset", 540)),
            "lang": self.gds.get("lang", "en"),
        }
        body = {"client_id": CLIENT_ID, "service_id": "Launcher",
                "refresh_token": self.launcher_refresh,
                "is_default": gds["is_default"], "nation": gds["nation"],
                "regulation": gds["regulation"], "timezone": gds["timezone"],
                "utc_offset": gds["utc_offset"], "lang": gds["lang"],
                "gds_info": gds}
        r = self.s.post(API_BASE + "/sign/v2.0/pc/refresh", json=body,
                        headers=self._official_headers(), timeout=15)
        data = self._parse(r, "refresh_game_token")
        if data.get("code") not in (0, None):
            print("[!] 刷新失败 code=%s msg=%s"
                  % (data.get("code"), data.get("message")))
            return None
        value = data.get("value") or {}
        if not isinstance(value, dict):
            return None
        member = value.get("member") if isinstance(value.get("member"), dict) else {}
        user = value.get("user") if isinstance(value.get("user"), dict) else {}
        if not user and isinstance(member, dict):
            user = member.get("user") if isinstance(member.get("user"), dict) else {}
        access, refresh = value.get("access_token"), value.get("refresh_token")
        if refresh:
            self.launcher_refresh = refresh
        self._last_token_response = value
        print("[+] 兜底刷新成功：member_no=%s user_id=%s"
              % (member.get("member_no"), user.get("user_id")))
        return access, refresh, member, user

    def apply_game_token(self, result):
        """把兑换结果落到 game_* 属性，供 REQUIRED_INFO 与环境变量使用。

        注意：result[1] 是 gc/check 兑换出的游戏作用域 refresh_token，
        绝不能写回 launcher_refresh（否则 state.json 被毒化，下次续期 44010）。
        launcher 级 refresh 只由续期与兜底刷新路径更新。"""
        if not result:
            return False
        access, refresh, member, user = result
        member = member if isinstance(member, dict) else {}
        user = user if isinstance(user, dict) else {}
        self.game_access_token = access
        self.game_refresh_token = refresh
        if access:
            self.launcher_access = access
        self.game_member_no = str(member.get("member_no")
                                  or member.get("memberNumber") or "")
        self.game_nickname = member.get("nickname") or ""
        last = getattr(self, "_last_token_response", None) or {}
        expire = last.get("expires_in") or last.get("expire_in")
        self.game_expire_in = str(expire) if expire else None
        self.game_cafe_key = member.get("cafe_key") or ""
        provider = member.get("provider_cd")
        self.game_provider_cd = (int(provider)
                                 if str(provider).lstrip("-").isdigit() else None)
        # 保留完整 member 对象：verify 标志、reg_dt、birth_dt 都要照抄
        self.game_member = member
        self.game_person_verify_yn = member.get("person_verify_yn")
        self.game_parent_verify_yn = member.get("parent_verify_yn")
        self.game_email_verify_yn = member.get("email_verify_yn")
        self.game_reg_dt = member.get("reg_dt")
        self.game_birth_dt = member.get("birth_dt")
        self.game_member_account_type = member.get("account_type")
        # 游戏级 guid = value.user.user_id
        guid = user.get("user_id") or user.get("userId") or member.get("guid")
        self.game_guid = str(guid) if guid else None
        # 同步账号级标识，保证环境变量与 REQUIRED_INFO 一致
        if self.game_member_no:
            self.member_no = self.game_member_no
        if self.game_guid:
            self.guid = self.game_guid
        if self.game_nickname:
            self.nickname = self.game_nickname
        print("[+] 游戏级令牌已应用：guid=%s member_no=%s"
              % (self.game_guid, self.game_member_no))
        return True

    def resolve_guid(self, explicit=None):
        """确定 REQUIRED_INFO 的 guid（游戏 auth 包使用的账号标识）。

        优先级：显式参数/state.json > 官方启动器日志导入 > 登录响应 user_id。
        登录响应给的 user_id 等于 member_no，并不是正确的 guid。"""
        if explicit:
            self.guid = str(explicit)
        if not self.guid or self.guid == self.member_no:
            guid = import_guid_from_official_log()
            if guid:
                self.guid = guid
        self.game_guid = self.guid
        if self.guid:
            print("[*] REQUIRED_INFO.guid = %s" % self.guid)
        else:
            print("[!] 未能确定 guid，游戏登录极可能报 41002；"
                  "可在 state.json 中写入 \"guid\": \"...\" 显式指定")
        return self.guid

    def import_member_fields_from_official_log(self):
        """从官方启动器日志补齐账号固有字段（reg_dt、birth_dt 等）。

        登录/续期响应缺少这些字段时，空值会导致游戏后端报 41002。
        只读官方日志，缺失时保留原值。"""
        data = import_required_info_from_official_log()
        if not data:
            print("[!] 官方日志中没有 REQUIRED_INFO 明文，"
                  "reg_dt/birth_dt 可能缺失（41002 风险）")
            return False
        member = getattr(self, "game_member", None)
        if not isinstance(member, dict):
            member = {}
        filled = []
        for key in ("reg_dt", "birth_dt", "member_nickname", "country_cd",
                    "account_type", "person_verify_yn", "parent_verify_yn",
                    "email_verify_yn", "member_no"):
            value = data.get(key)
            if value in (None, ""):
                continue
            current = member.get(key)
            if current in (None, "") or key in ("reg_dt", "birth_dt"):
                member[key] = value
                filled.append("%s=%s" % (key, value))
        self.game_member = member
        if member.get("member_no"):
            self.game_member_no = str(member["member_no"])
            self.member_no = self.game_member_no
        if member.get("member_nickname"):
            self.game_nickname = member["member_nickname"]
            self.nickname = self.game_nickname
        if member.get("reg_dt"):
            self.game_reg_dt = member["reg_dt"]
            self.reg_dt = member["reg_dt"]
        if member.get("birth_dt"):
            self.game_birth_dt = member["birth_dt"]
            self.birth_dt = member["birth_dt"]
        if filled:
            print("[+] 已从官方日志补齐账号字段：%s" % ", ".join(filled))
        return True

    # ---- 凭据持久化 ----
    def save(self):
        """凭据与账号字段写入 state.json（exe 同目录，不随包分发）。"""
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({
            "launcher_refresh": self.launcher_refresh,
            "member_no": self.member_no,
            "guid": self.guid,
            "provider_cd": self.required_provider_cd,
            "reg_dt": self.reg_dt,
            "birth_dt": self.birth_dt,
            "nickname": self.nickname,
            "country_cd": self.country_cd,
            "expire_in": self.expire_in,
            "person_verify_yn": self.person_verify_yn,
            "parent_verify_yn": self.parent_verify_yn,
            "email_verify_yn": self.email_verify_yn,
        }, ensure_ascii=False, indent=1), encoding="utf-8")

    def load(self, offline=False):
        """静默续期：读取 state.json，走完整的令牌兑换链路。

        offline=True 时绝不联网，只用本地凭据填充占位值（供字段诊断）；
        正常路径下 refresh_token 每次轮换，成功后会把新值写回 state.json
        （game_token 内部已正确更新 launcher 级 refresh，此处直接保存）。"""
        if not STATE_FILE.exists():
            return False
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.member_no = state.get("member_no")
            self.guid = state.get("guid")
            self.required_provider_cd = state.get("provider_cd")
            for key in ("reg_dt", "birth_dt", "nickname", "country_cd",
                        "expire_in", "person_verify_yn", "parent_verify_yn",
                        "email_verify_yn"):
                value = state.get(key)
                if value is not None and getattr(self, key, None) in (None, ""):
                    setattr(self, key, value)
            refresh = state.get("launcher_refresh")
            if refresh:
                self.launcher_refresh = refresh
                if offline:
                    self.launcher_access = refresh
                    return True
                result = self.game_token()
                if result:
                    self.launcher_access = result[0]
                    self.apply_game_token(result)
                    self.save()
                    return True
        except Exception as e:
            print("[!] 静默续期失败：%s" % e)
        return False


# ====================================================================
# 2. 命名管道服务
# --------------------------------------------------------------------
# 帧格式：[u16LE 整帧总长（含 4 字节头）][u16LE packet_id][UTF-8 JSON]
# 信封：  {"code":0,"message":"Success","value":"..."}
#   1000  游戏→启动器  {"value":{"publicKey":RSA 公钥,"pid":...}}
#   2000  启动器→游戏  base64(RSA(pub, JSON{"aesKey":...}))，72 列换行
#                       换行符为字面反斜杠+n 两字符（游戏会先做字面替换）
#   2001  启动器→游戏  REQUIRED_INFO 的 AES-256-CBC 密文大写 HEX
#   1001  游戏→启动器  心跳（每 5 秒），只收不应答（游戏分发表中没有它）
# ====================================================================
class PipeServer(threading.Thread):
    """命名管道服务端。AES 密钥为 32 字符可打印 ASCII：前 32 字节作
    AES-256 密钥，前 16 字节作 IV（游戏侧直接按字节取用，不做解码）。"""

    AES_KEY_TEXT = "0123456789abcdef0123456789abcdef"

    def __init__(self, required_info):
        super().__init__(daemon=True)
        self.info = required_info
        self.aes_key_text = self.AES_KEY_TEXT
        self.aes_key = self.aes_key_text.encode("ascii")
        self.aes_iv = self.aes_key[:16]
        self.game_pubkey = None
        self.game_pid = None
        self.running = True
        self.handshake_done = threading.Event()
        self.frames = []
        self._pipe_handle = None
        self._watchdog_thread = None
        self._watchdog_stop = threading.Event()
        self._pipe_dup = None            # 看门狗专用的句柄副本

    def run(self):
        self.serve()

    def serve(self):
        """创建管道并循环服务。1000 内联握手；1001 只收不应答。"""
        import win32file
        import win32pipe
        import pywintypes
        security = pywintypes.SECURITY_ATTRIBUTES()
        security.SetSecurityDescriptorDacl(True, None, False)  # 与官方一致
        handle = win32pipe.CreateNamedPipe(
            PIPE_NAME,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE
            | win32pipe.PIPE_WAIT,
            255, 65536, 65536, 5000, security)
        self._pipe_handle = handle
        self._pipe_dup = self._dup_handle(handle)
        print("[pipesrv] 监听 %s" % PIPE_NAME)
        while self.running:
            try:
                win32pipe.ConnectNamedPipe(handle, None)
            except Exception:
                break
            if not self.running:
                break
            try:
                while self.running:
                    frame = self._read_frame(handle)
                    if frame is None:
                        break
                    packet, payload = frame
                    if packet == 1000:
                        self._handle_1000(handle, payload)
                    elif packet == 1001:
                        pass  # 心跳只收不应答：游戏分发表中没有 1001，回包会污染读循环
                    else:
                        print("[pipesrv] 忽略未知包 %d" % packet)
            except Exception as e:
                print("[pipesrv] 会话异常：%s" % e)
            try:
                # 先复位状态再断开：Disconnect 偶发失败也不能让 GUI
                # 卡在「游戏运行中」
                self.handshake_done.clear()
                self.game_pid = None
                win32pipe.DisconnectNamedPipe(handle)
            except Exception:
                pass
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass
        self._pipe_handle = None

    @staticmethod
    def _pid_alive(pid):
        """进程是否仍在运行（PROCESS_QUERY_LIMITED_INFORMATION 即可，
        无需额外依赖）。打不开 = 已退出；查询失败按活着处理（不误杀）。"""
        try:
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return code.value == 259          # STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(h)
        except Exception:
            return True

    def _dup_handle(self, handle):
        """给看门狗复制一份管道句柄（持有同一管道对象的独立引用：
        即使服务线程先关闭原句柄，副本仍然有效，不存在句柄值复用
        被误解的竞态）。"""
        try:
            k32 = ctypes.windll.kernel32
            k32.DuplicateHandle.argtypes = [
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
                ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            dup = ctypes.c_void_p()
            cur = k32.GetCurrentProcess()
            ok = k32.DuplicateHandle(cur, ctypes.c_void_p(int(handle)), cur,
                                     ctypes.byref(dup), 0, False, 2)
            return dup.value if ok else None    # 2 = DUPLICATE_SAME_ACCESS
        except Exception:
            return None

    def _watch_game_pid(self):
        """游戏进程看门狗：游戏退出后其管道句柄可能被残留进程（SDK
        助手/盾进程等）持有，服务端 ReadFile 永远等不到 broken pipe，
        会话就一直挂着（GUI 卡在「游戏运行中」）。握手 1000 里游戏上报
        了自己的 pid，每 3 秒探活一次；进程消失后只做两件事——复位
        状态、对自家句柄副本 CancelIoEx 取消挂起读取。断开与关闭由
        服务线程在自己的原始句柄上完成，看门狗不碰别人线程的句柄。"""
        while not self._watchdog_stop.wait(3):
            pid = self.game_pid
            if not pid:
                return                              # 会话已结束，看门狗收工
            if not self._pid_alive(int(pid)):
                print("[pipesrv] 游戏进程 %s 已退出，取消挂起读取以结束会话" % pid)
                record("pipe", event="游戏进程退出，结束管道会话", pid=pid)
                # 状态先行复位：后续任何失败都不影响 GUI 回落
                self.handshake_done.clear()
                self.game_pid = None
                try:
                    if self._pipe_dup is not None:
                        k32 = ctypes.windll.kernel32
                        k32.CancelIoEx.argtypes = [ctypes.c_void_p,
                                                   ctypes.c_void_p]
                        k32.CancelIoEx(ctypes.c_void_p(self._pipe_dup), None)
                except Exception:
                    pass
                return

    def stop(self):
        """停止服务：置位 running 并唤醒阻塞中的 ConnectNamedPipe。

        Windows 的同步 ConnectNamedPipe 无法被 DisconnectNamedPipe 取消
        （已实测：关闭窗口时这样调用会永久卡死），因此这里改用标准做法：
        起一个线程对自家管道做一次「连接后立即关闭」，让挂起的
        ConnectNamedPipe 返回，服务线程发现 running=False 后干净退出
        并释放管道名。"""
        self.running = False
        self._watchdog_stop.set()            # 看门狗立即退出
        t = self._watchdog_thread
        if t and t.is_alive():
            t.join(4)                        # 等它收尾，再关句柄副本
        try:
            if self._pipe_dup is not None:
                ctypes.windll.kernel32.CloseHandle(
                    ctypes.c_void_p(self._pipe_dup))
        except Exception:
            pass
        self._pipe_dup = None

        def _wake():
            try:
                import win32file
                handle = win32file.CreateFile(
                    PIPE_NAME, 0, 0, None,
                    win32file.OPEN_EXISTING, 0, None)
                win32file.CloseHandle(handle)
            except Exception:
                pass

        threading.Thread(target=_wake, daemon=True).start()

    # ---- 帧编解码 ----
    def _read_frame(self, handle):
        """读取一帧。头部 u16 是整帧总长（含 4 字节头），游戏解包时会减 4。"""
        import win32file
        try:
            _, raw = win32file.ReadFile(handle, 65536)
        except Exception:
            return None
        if not raw:
            return None
        self.frames.append(("recv", len(raw), raw[:64]))
        if len(raw) >= 4 and raw[4:5] in (b"{", b"["):
            total, packet = struct.unpack("<HH", raw[:4])
            json_len = total - 4 if 4 <= total <= len(raw) else len(raw) - 4
            return packet, raw[4:4 + json_len]
        if raw[:1] in (b"{", b"["):
            try:
                data = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                data = {}
            if "publicKey" in data:
                return 1000, raw
            if "code" in data and "message" in data:
                return 1001, raw
            return 9999, raw
        self.frames.append(("recv_unknown", raw[:64].hex()))
        return None

    def _write_frame(self, handle, packet_id, payload):
        """写一帧。头部 u16 = 整帧总长 = 4 + 载荷长度。"""
        import win32file
        frame = struct.pack("<HH", len(payload) + 4, packet_id) + payload
        win32file.WriteFile(handle, frame)
        self.frames.append(("send", packet_id, payload[:200]))

    @staticmethod
    def _envelope(code, message, value):
        return json.dumps({"code": code, "message": message, "value": value},
                          ensure_ascii=False, separators=(",", ":")).encode()

    # ---- 1000 握手 ----
    def _handle_1000(self, handle, payload):
        print("[pipesrv] 收到 1000 握手（%d 字节）" % len(payload))
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = payload.decode("utf-16-le")
            except Exception:
                text = payload.decode("utf-8", "replace")
        print("[dbg][pipesrv] 1000 内容：%s" % text[:200])
        data = json.loads(text)
        value = data.get("value") if isinstance(data.get("value"), dict) else {}
        self.game_pubkey = (value.get("publicKey") or data.get("publicKey")
                            or value.get("public_key") or "")
        self.game_pid = value.get("pid")
        if not self.game_pubkey:
            print("[pipesrv] 1000 中没有 publicKey：%s" % list(value.keys()))
            return
        print("[pipesrv] 公钥已接收（%d 字符）" % len(self.game_pubkey))

        # 2000：RSA 公钥加密 JSON 文本 {"aesKey": ...}（游戏按 rapidjson 解析），
        # base64 后按 72 列换行，换行为字面反斜杠+n 两字符（游戏先替换再解码）
        public_key = RSA.import_key(_b64_any(self.game_pubkey))
        aes_json = json.dumps({"aesKey": self.aes_key_text},
                              separators=(",", ":")).encode("ascii")
        cipher = PKCS1_v1_5.new(public_key).encrypt(aes_json)
        encoded = base64.b64encode(cipher).decode()
        wrapped = "\\n".join(encoded[i:i + 72]
                             for i in range(0, len(encoded), 72)) + "\\n"
        self._write_frame(handle, 2000, self._envelope(0, "Success", wrapped))

        # 两帧之间留 20ms：官方两次写入间隔 20ms，游戏需要时间处理 2000
        time.sleep(0.02)

        # 2001：REQUIRED_INFO → PKCS7 → AES-256-CBC → 大写 HEX
        plain = json.dumps(self.info, ensure_ascii=False,
                           separators=(",", ":")).encode()
        pad = 16 - len(plain) % 16
        plain += bytes([pad]) * pad
        encrypted = AES.new(self.aes_key, AES.MODE_CBC,
                            self.aes_iv).encrypt(plain)
        self._write_frame(handle, 2001,
                          self._envelope(0, "Success", encrypted.hex().upper()))
        self.handshake_done.set()
        # 看门狗：游戏退出但句柄被残留进程持有时强制收尾（防 GUI 卡运行中）
        t = self._watchdog_thread
        if not (t and t.is_alive()):
            self._watchdog_thread = threading.Thread(
                target=self._watch_game_pid, daemon=True, name="pipesrv-watchdog")
            self._watchdog_thread.start()
        print("[pipesrv] 握手完成（2000/2001 已推送）")


def _b64_any(text):
    """兼容 PEM 与裸 base64 两种公钥格式。"""
    text = text.strip()
    if "-----BEGIN" in text:
        return RSA.import_key(text).export_key("DER")
    return base64.b64decode(text.replace("\n", "").replace("\r", ""))


# ====================================================================
# 3. 环境变量与游戏拉起
# ====================================================================
def build_env(auth, gds):
    """组装游戏进程所需的 31 个环境变量（主通道，优先于管道 2001）。

    令牌/账号字段与 REQUIRED_INFO 同源（游戏级）；expire_in 为秒、
    expires_in 为毫秒，注意两者单位不同。"""
    game_access = getattr(auth, "game_access_token", None)
    game_refresh = getattr(auth, "game_refresh_token", None)
    game_member_no = getattr(auth, "game_member_no", None)
    game_guid = getattr(auth, "game_guid", None)
    game_nickname = getattr(auth, "game_nickname", None)

    raw_expire = (str(getattr(auth, "game_expire_in", None) or "")
                  or str(getattr(auth, "expire_in", None) or "") or "21599")
    expire_value = int(raw_expire) if raw_expire.isdigit() else 21599
    expire_seconds = str(expire_value // 1000) if expire_value > 100000 else str(expire_value)
    expire_millis = str(expire_value) if expire_value > 100000 else str(expire_value * 1000)

    return {
        "StoveLauncherData": "FromEnvironment",
        "access_token": game_access or auth.launcher_access,
        "refresh_token": game_refresh or auth.launcher_refresh,
        "game_id": GAME_ID,
        "game_no": GAME_NO,
        "market_game_id": MARKET_GAME_ID,
        "member_no": game_member_no or auth.member_no or "",
        "member_nickname": game_nickname or getattr(auth, "nickname", None) or "",
        "guid": game_guid or auth.guid or "",
        "account_type": "11",
        "country_cd": getattr(auth, "country_cd", None) or gds.get("country_cd", "JP"),
        "language": "zh-cn",
        "game_type": "ONLINE",
        "service_protocol": "shortcut",
        "gds_isdefault_yn": "n",
        "gds_nation": gds.get("nation", "JP"),
        "gds_regulation": gds.get("regulation", "ETC"),
        "gds_timezone": gds.get("timezone", "Asia/Tokyo"),
        "gds_utcoffset": str(gds.get("utc_offset", 540)),
        "store_opt": "STOVE",
        "ref_session_id": str(uuid.uuid4()),
        "ref_source_type": "stove_launcher",
        "expire_in": expire_seconds,
        "expires_in": expire_millis,
        "dlc_list": "",
        "cafe_key": "",
        "power_save_yn": "n",
        "streaming_yn": "n",
        "studio_game_yn": "n",
        "uuid": "",
    }


class _SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("fMask", ctypes.c_ulong),
                ("hwnd", ctypes.c_void_p), ("lpVerb", ctypes.c_wchar_p),
                ("lpFile", ctypes.c_wchar_p), ("lpParameters", ctypes.c_wchar_p),
                ("lpDirectory", ctypes.c_wchar_p), ("nShow", ctypes.c_int),
                ("hInstApp", ctypes.c_void_p), ("lpIDList", ctypes.c_void_p),
                ("lpClass", ctypes.c_void_p), ("hkeyClass", ctypes.c_ulong),
                ("dwHotKey", ctypes.c_ulong), ("hIconOrMonitor", ctypes.c_void_p),
                ("hProcess", ctypes.c_void_p)]


def launch_game(env, wait_seconds=12):
    """直接拉起游戏主程序（不经 ucldr loader）。

    loader 只是官方启动器为激活保护加的中间层：管道服务已经在本进程里
    开着，环境变量也已就位，主程序自己会连管道取 REQUIRED_INFO。

    首选 ShellExecuteExW（与官方启动方式一致），但它在存在残留进程
    或触发隐藏对话框时可能永久阻塞（调用线程没有消息泵），因此：
      · 放到临时线程执行，最多等待 wait_seconds 秒
      · 超时后确认主程序是否其实已经启动（避免双开）
      · 确认未启动则回退 CreateProcessW 直启（不会阻塞）
    失败时打印 Win32 错误码便于定位。"""
    # 前置校验：路径不对时立刻给出可读原因，而不是抛 WinError 123
    ok, detail = game_exe_probe(INSTALL_ROOT)
    if not ok:
        print("[x] 无法拉起：游戏目录未配置或 %s 不存在" % game_exe_name())
        print("    install_root = %r" % INSTALL_ROOT)
        print("    期望 %s = %s" % (game_exe_name(), detail))
        if INSTALL_ROOT:
            print("    该目录存在   = %s" % os.path.isdir(INSTALL_ROOT))
            print("    其下 bin     = %s"
                  % os.path.isdir(os.path.join(INSTALL_ROOT, "bin")))
        else:
            print("    提示：请在 config.json 里设置 game.install_root"
                  "（填包含 bin 子目录的那一层）。")
        return False

    for key, value in env.items():
        if value is not None:
            os.environ[key] = str(value)
    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "open"
    sei.lpFile = GAME_EXE_PATH
    sei.lpParameters = None          # 主程序无参数（参数是 loader 用来指定主程序的）
    sei.lpDirectory = INSTALL_ROOT
    sei.nShow = 1

    outcome = {}

    def _try():
        ok = ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei))
        outcome["done"] = True
        outcome["ok"] = bool(ok and sei.hProcess)
        outcome["error"] = 0 if ok else ctypes.GetLastError()

    worker = threading.Thread(target=_try, daemon=True)
    worker.start()
    worker.join(wait_seconds)

    if outcome.get("done"):
        if outcome["ok"]:
            print("[launch] 游戏进程已创建（ShellExecuteExW，句柄 %s）" % sei.hProcess)
            return True
        print("[!] ShellExecuteExW 失败（Win32 错误码 %d），回退 CreateProcessW 直启"
              % outcome["error"])
    else:
        print("[!] ShellExecuteExW %d 秒未返回（可能被残留进程或隐藏对话框阻塞）"
              % wait_seconds)

    # 回退前先确认主程序是否其实已经启动，避免双开。
    # 判据必须大小写不敏感（tasklist 按真实文件名的大小写输出），且不能
    # 拿超过 25 字符的名字去比 —— tasklist 的 Image Name 列宽 25，长名会被截断。
    listing = subprocess.run(["tasklist"], capture_output=True).stdout \
        .decode("utf-8", errors="replace").lower()
    probe = os.path.basename(GAME_EXE_REL).lower()
    if probe in listing:
        print("[+] 检测到 %s 已在运行，视为已拉起" % probe)
        return True

    try:
        # cwd 必须是非空有效目录：空串会抛 OSError WinError 123
        process = subprocess.Popen([GAME_EXE_PATH],
                                   cwd=INSTALL_ROOT or None)
        print("[launch] 游戏进程已创建（CreateProcessW，pid=%s）" % process.pid)
        return True
    except Exception as e:
        print("[x] 拉起失败：%s（file=%s dir=%s）"
              % (e, GAME_EXE_PATH, INSTALL_ROOT))
        return False


def launch_game_capture_stdout(env, out_path=None):
    """诊断用：以 CreateProcessW 启动游戏主程序并重定向 stdout/stderr 到文件。"""
    for key, value in env.items():
        if value is not None:
            os.environ[key] = str(value)
    out_path = out_path or str(Path(__file__).with_name("game_stdout.log"))
    log = open(out_path, "wb")
    print("[launch] 诊断模式：stdout/stderr → %s" % out_path)
    process = subprocess.Popen([GAME_EXE_PATH],
                               cwd=INSTALL_ROOT or None,
                               stdout=log, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL)
    print("[launch] 主程序 pid=%s" % process.pid)
    return process


# ====================================================================
# 4. REQUIRED_INFO（管道 2001 载荷）
# ====================================================================
def build_required_info(auth):
    """构建 REQUIRED_INFO（39 字段，管道 2001 的内层载荷）。

    字段约束（与游戏 BaseSDK 的解析逻辑逐字段对齐）：
      · 所有字段必须是 JSON 字符串，缺失或类型错误会导致 SDK 初始化失败
      · expire_time/member_no 等走 stoi/stoll，必须是数字字符串
      · game_type="ONLINE"、provider_cd="SO" 是普通字符串，不走 stoi
      · access_token/refresh_token/guid 必须来自游戏级令牌兑换结果
      · reg_dt/birth_dt 为账号固有字段，缺失会导致 41002
    """
    game_access = getattr(auth, "game_access_token", None)
    game_refresh = getattr(auth, "game_refresh_token", None)
    game_guid = getattr(auth, "game_guid", None)
    game_member_no = getattr(auth, "game_member_no", None)
    game_nickname = getattr(auth, "game_nickname", None)
    game_expire_in = getattr(auth, "game_expire_in", None)

    if not game_access:
        print("[!] 缺少游戏级令牌，回退启动器级（游戏后端将报 41002）")

    member_no = game_member_no or auth.member_no or "0"
    nickname = game_nickname or getattr(auth, "nickname", None) or ""
    guid = game_guid or auth.guid or ""

    def member_field(key, default):
        game_member = getattr(auth, "game_member", None) or {}
        if isinstance(game_member, dict) and game_member.get(key) is not None:
            return game_member.get(key)
        value = getattr(auth, key, None)
        return value if value is not None else default

    def yes_no(value, default="n"):
        if value is None:
            return default
        text = str(value).strip().lower()
        if text in ("y", "yes", "true", "1"):
            return "y"
        if text in ("n", "no", "false", "0"):
            return "n"
        return default

    # 过期时间：expires_in 保留响应毫秒原值，expire_time = 当前时间 + 有效期
    expire_seconds, expire_millis = None, None
    for candidate in (game_expire_in, getattr(auth, "expire_in", None)):
        if candidate and str(candidate).isdigit():
            value = int(candidate)
            if value > 100000:      # 毫秒原值
                expire_seconds, expire_millis = str(value // 1000), str(value)
            else:                   # 已是秒
                expire_seconds, expire_millis = str(value), str(value * 1000)
            break
    expire_seconds = expire_seconds or "21599"
    expire_millis = expire_millis or str(int(expire_seconds) * 1000)
    expire_time_ms = str(int(time.time() * 1000) + int(expire_millis))

    # provider_cd 是账号的登录渠道（SO=STOVE 自有账号），可按账号覆盖
    provider_cd = getattr(auth, "required_provider_cd", None) or "SO"

    return {
        "caller_id": CALLER_ID,
        "game_id": GAME_ID,
        "market_game_id": MARKET_GAME_ID,
        "env": "live",
        "game_type": "ONLINE",
        "heartbeat_interval": "300",
        "playtime_interval": "1800",
        "playtime_retry_interval": "300",
        "exit_when_launcher_yn": "y",
        "ref_session_id": str(uuid.uuid4()),
        "ref_source_type": "stove_launcher",
        "uuid": "",
        "service_protocol": "shortcut",
        "studio_game_yn": "n",
        "gds_isdefault_yn": "n",
        "gds_nation": GDS_INFO["nation"],
        "gds_regulation": GDS_INFO["regulation"],
        "gds_timezone": GDS_INFO["timezone"],
        "gds_utcoffset": str(GDS_INFO["utc_offset"]),
        "language": "zh-cn",
        "gds_language": "en",
        "access_token": game_access or auth.launcher_access,
        "refresh_token": game_refresh or auth.launcher_refresh,
        "expire_in": expire_seconds,
        "expires_in": expire_millis,
        "expire_time": expire_time_ms,
        # account_type 恒为官方 REQUIRED_INFO 实测值 "11"（与登录响应 member
        # 里的 1 不是同一个字段，禁止用响应值覆盖）
        "account_type": "11",
        "guid": guid,
        "cloud_saving_path": "",
        "provider_cd": str(provider_cd),
        "country_cd": member_field("country_cd", "JP"),
        "member_no": str(member_no),
        "member_nickname": nickname or "0",
        "person_verify_yn": yes_no(member_field("person_verify_yn", None), "n"),
        "parent_verify_yn": yes_no(member_field("parent_verify_yn", None), "n"),
        "email_verify_yn": yes_no(member_field("email_verify_yn", None), "y"),
        "reg_dt": str(member_field("reg_dt", "") or ""),
        "birth_dt": str(member_field("birth_dt", "") or ""),
        "streaming_yn": "n",
    }


# REQUIRED_INFO 自检：必填字段 / 必须是数字字符串的字段
RI_MUST_HAVE = ["expire_in", "expire_time", "heartbeat_interval",
                "playtime_interval", "playtime_retry_interval",
                "access_token", "refresh_token", "guid", "market_game_id",
                "member_no", "game_type", "reg_dt", "birth_dt"]
RI_NUMERIC = ["expire_time", "member_no", "gds_utcoffset", "account_type",
              "heartbeat_interval", "playtime_interval",
              "playtime_retry_interval"]
RI_PROTOCOL_KEYS = set(RI_MUST_HAVE) | {
    "caller_id", "game_id", "env", "exit_when_launcher_yn", "ref_session_id",
    "ref_source_type", "uuid", "service_protocol", "studio_game_yn",
    "dlc_list", "gds_isdefault_yn", "gds_nation", "gds_regulation",
    "gds_timezone", "gds_utcoffset", "language", "gds_language",
    "expire_in", "expires_in", "account_type", "cloud_saving_path",
    "provider_cd", "country_cd", "member_nickname", "person_verify_yn",
    "parent_verify_yn", "email_verify_yn", "reg_dt", "birth_dt",
    "streaming_yn",
}


def validate_required_info(info):
    """REQUIRED_INFO 自检：必填字段齐全 + 数值字段合法 + 令牌为游戏级。"""
    for key in RI_MUST_HAVE:
        value = info.get(key)
        assert value, "[REQUIRED_INFO] 缺少必填字段 %s" % key
    for key in RI_NUMERIC:
        value = str(info.get(key) or "")
        assert value.lstrip("-").isdigit(), \
            "[REQUIRED_INFO] %s 必须是数字字符串，实际 %s" % (key, value)
    for key in info:
        if key not in RI_PROTOCOL_KEYS:
            print("[!] REQUIRED_INFO 含未知字段 %s（协议表中不存在）" % key)


# ====================================================================
# 5. 诊断与命令行入口
# ====================================================================
def dry_run():
    """自检：用假令牌验证管道握手与环境变量组装（不登录、不联网、不起游戏）。"""
    print("=== DRY RUN：管道握手 + 环境变量组装验证 ===")
    fake_auth = types.SimpleNamespace(
        launcher_access="FAKE_LAUNCHER_" + "x" * 280,
        launcher_refresh="FAKE_REFRESH_" + "y" * 280,
        member_no="100000001", guid="100000002",
        nickname="DemoUser", user_id="100000002",
        expire_in="21599", provider_cd=0, country_cd="JP",
        person_verify_yn="y", email_verify_yn="y", cafe_key="",
        game_access_token="FAKE_GAME_" + "z" * 360,
        game_refresh_token="FAKE_GAME_REFRESH_" + "w" * 340,
        game_member_no="100000001", game_guid="100000002",
        game_nickname="DemoUser", game_expire_in="21599",
        game_cafe_key="", game_provider_cd=0, game_account_type="11",
        game_member={"reg_dt": "1700000000000", "birth_dt": "946684800000",
                     "member_nickname": "DemoUser", "country_cd": "JP",
                     "person_verify_yn": "n", "parent_verify_yn": "n",
                     "email_verify_yn": "y"},
        reg_dt="", birth_dt="")

    env = build_env(fake_auth, {})
    assert len(env) >= 29, len(env)
    assert env["access_token"].startswith("FAKE_GAME_")
    print("[+] 环境变量组装 OK（%d 个变量，令牌来自游戏级）" % len(env))
    assert machine_guid() or os.name != "nt", "读取 MachineGuid 失败"
    print("[+] MachineGuid = %s" % (machine_guid() or "<非 Windows>"))

    required = build_required_info(fake_auth)
    validate_required_info(required)
    assert required["access_token"].startswith("FAKE_GAME_")
    print("[+] REQUIRED_INFO 校验 OK（%d 字段，游戏级令牌）" % len(required))

    server = PipeServer(required)
    server.start()
    time.sleep(0.3)

    # 模拟游戏客户端执行完整握手：1000 → 2000 → 2001
    import win32file
    pair = RSA.generate(2048)
    public_b64 = base64.b64encode(pair.publickey().export_key("DER")).decode()
    handle = win32file.CreateFile(PIPE_NAME,
                                  win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                                  0, None, win32file.OPEN_EXISTING, 0, None)

    def send(packet, payload):
        win32file.WriteFile(handle, struct.pack("<HH", len(payload) + 4, packet)
                            + payload)

    def recv():
        buffer = b""
        while len(buffer) < 4:
            _, chunk = win32file.ReadFile(handle, 65536)
            buffer += chunk
        total, packet = struct.unpack("<HH", buffer[:4])
        while len(buffer) < total:
            _, chunk = win32file.ReadFile(handle, 65536)
            if not chunk:
                break
            buffer += chunk
        return packet, buffer[4:total]

    send(1000, json.dumps({"code": 0, "message": "Success",
                           "publicKey": public_b64,
                           "pid": os.getpid()}).encode())
    packet, payload = recv()
    assert packet == 2000
    value = json.loads(payload.decode())["value"]
    # 游戏侧先把字面 "\n"（反斜杠+n 两字符）剥掉再做 base64 解码
    cipher_b64 = value.replace("\\n", "")
    key_json = PKCS1_v1_5.new(pair).decrypt(base64.b64decode(cipher_b64), b"FAIL")
    key_text = json.loads(key_json.decode("ascii"))["aesKey"]
    assert len(key_text) >= 32, "aesKey 须不少于 32 字符"
    aes_key = key_text.encode("ascii")[:32]
    aes_iv = key_text.encode("ascii")[:16]
    packet, payload = recv()
    assert packet == 2001
    plain = AES.new(aes_key, AES.MODE_CBC, aes_iv).decrypt(
        bytes.fromhex(json.loads(payload.decode())["value"]))
    info = json.loads(plain[:-plain[-1]].decode())
    assert info["access_token"] == fake_auth.game_access_token
    validate_required_info(info)
    win32file.CloseHandle(handle)
    server.running = False
    print("[+] 管道握手自测 OK（1000→2000→2001，1001 只收不应答）")
    print("[+] ShellExecute 参数：file=%s dir=%s（主程序无参数）"
          % (GAME_EXE_PATH, INSTALL_ROOT))
    print("[+] %s 存在：%s" % (game_exe_name(), os.path.exists(GAME_EXE_PATH)))
    print("=== DRY RUN PASS ===")



def main():
    parser = argparse.ArgumentParser(
        description="czn-lite 控制台版（与 GUI 共用同一套核心逻辑）")
    parser.add_argument("--keepalive-only", action="store_true",
                        help="只运行管道保活服务（配合已运行的游戏）")
    parser.add_argument("--dry-run", action="store_true",
                        help="自检：验证管道握手与环境变量组装，不登录不联网")
    parser.add_argument("--offline", action="store_true",
                        help="不联网：仅用 state.json 构建诊断数据，绝不轮换凭据")
    parser.add_argument("--no-launch", action="store_true",
                        help="完成登录与兑换后停止，不拉起游戏")
    parser.add_argument("--guid",
                        help="显式指定 REQUIRED_INFO 的 guid（默认自动导入）")
    parser.add_argument("--capture-stdout", action="store_true",
                        help="诊断：重定向游戏进程 stdout 到文件")
    args = parser.parse_args()

    if args.dry_run:
        dry_run()
        return

    auth = StoveAuth()
    # offline 必须在任何联网动作之前拦截：续期会轮换 refresh_token，
    # 联网诊断会把官方启动器的登录态挤掉
    if args.offline:
        auth.load(offline=True)
        args.no_launch = True
        print("[*] --offline：仅用 state.json 构建诊断数据，不联网")
    elif not auth.load():
        print("[*] 无有效凭据，进入二维码扫码登录（手机 STOVE App）")
        qr = auth.qr_create()
        image = base64.b64decode(qr["image"])
        qr_file = Path(__file__).with_name("qr.png")
        try:
            qr_file.write_bytes(image)
            print("[+] 二维码已保存：%s（%d 字节）" % (qr_file, len(image)))
            print("[+] 扫码地址：%s" % qr.get("url", ""))
            os.startfile(str(qr_file))
            session = qr["session"]
            while True:
                time.sleep(2)
                status = auth.qr_status(session).get("value", {}).get("status")
                print("[qr] %s" % status)
                if status == "success":
                    auth.signin_qr(session)
                    break
                if status in ("expired", "close"):
                    print("[-] 二维码已%s，重新生成" % status)
                    qr = auth.qr_create()
                    qr_file.write_bytes(base64.b64decode(qr["image"]))
                    os.startfile(str(qr_file))
                    session = qr["session"]
        finally:
            if qr_file.exists():
                qr_file.unlink()
                print("[*] 临时二维码已清理：%s" % qr_file)
        auth.save()
        print("[+] 登录成功 member_no=%s guid=%s（启动器级）nickname=%s"
              % (auth.member_no, auth.guid, auth.nickname))
    else:
        print("[+] 静默续期成功")

    # 游戏级令牌兑换（缺此步骤游戏后端报 41002）
    if not args.offline:
        print("[*] 兑换游戏级令牌：signin(RT) → gc/v1.4/check…")
        token = auth.game_token()
        if token:
            auth.apply_game_token(token)
        else:
            print("[!] 兑换失败，游戏后端极可能报 41002")

    auth.resolve_guid(explicit=args.guid)
    auth.import_member_fields_from_official_log()
    auth.save()

    # 启动流程不碰更新：只有显式 --update / --verify-files 才执行
    required = build_required_info(auth)
    validate_required_info(required)
    print("[*] REQUIRED_INFO 自检通过（%d 字段）" % len(required))
    print("    guid=%s member_no=%s provider_cd=%s 令牌长度=%d"
          % (required["guid"], required["member_no"], required["provider_cd"],
             len(str(required["access_token"]))))

    env = build_env(auth, {})
    print("[*] 环境变量已组装（%d 个）" % len(env))

    if args.keepalive_only:
        # 保活模式：游戏已由别的会话拉起，这里只补一个响应握手的管道服务
        if args.offline:
            print("[!] --keepalive-only 需要真实令牌，不能与 --offline 同用")
            return
        print("[*] --keepalive-only：只运行管道保活服务（不拉起游戏）")
        server = PipeServer(required)
        server.start()
        print("[*] 管道保活中，Ctrl+C 退出")
        try:
            while True:
                time.sleep(5)
        except KeyboardInterrupt:
            print("\n[*] 退出")

    if args.offline or args.no_launch:
        print("[*] --offline/--no-launch：停在启动前")
        return

    server = PipeServer(required)
    server.start()
    print("[*] 3 秒后拉起游戏…")
    time.sleep(3)
    if args.capture_stdout:
        launch_game_capture_stdout(env)
    elif launch_game(env):
        print("[+] 游戏已拉起（直接启动主程序）")
    else:
        print("[-] 拉起失败")
        return
    print("[*] 主进程保活中，Ctrl+C 退出（游戏可继续运行）")
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[*] 退出")


if __name__ == "__main__":
    main()
