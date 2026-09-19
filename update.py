#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# CZN Launcher Lite —— 游戏本体更新（DPMS）
# Copyright (C) 2026 CZN Launcher Lite contributors
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, version 3 of the License.
r"""游戏本体更新：检查版本 → 完整性校验 → 增量下载与替换。

机制（与官方 InstallLib.dll 等价，均经实机取证）
--------------------------------------------------
  版本  注册表 HKCU\SOFTWARE\SGUP\apps\<GAME_ID>\Version 与
        <install_root>\combinedata_manifest\GameManifest_<GAME_ID>.upf 的 local_version
  清单  .upf 里的 project_url 指向官方 DPMS 清单（明文 JSON）
  下载  清单 URL 同目录 + v<group>/<seq>.gz
  校验  gzip 包 MD5 + 解压后原始 MD5（等价官方的 IIV_EXIST_HASH）
  账本  cacheii.db（SQLite，installed_info）—— 照写官方格式，便于与官方启动器互通

作用域：只处理 <install_root>\bin 下的受管文件（清单里的 F 条目）。
游戏自管的资源热更（bin\appdata\cznlive）由游戏引擎在运行时完成，本模块绝不触碰。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import czn_lite as cl

# ====================================================================
# 常量（全部来自实机取证，硬编码；不作为可调参数）
# ====================================================================
MANIFEST_DIR = "combinedata_manifest"
CACHE_DB = "cacheii.db"
TEMP_DIR = ".stove_temp"
BACKUP_SUFFIX = ".cznbak"

# 清单 URL 兜底探测的起始偏移与上限（官方 DPMS 路径可推导）
PROBE_MAX_AHEAD = 12
# DPMS 清单命名约定基址（实测证据：.upf project_url 与官方抓包同构
# <base>/<GAME_ID>_<N>.json / _v2.json）。全新安装（无 .upf）时由此构造探测模板。
DPMS_BASE_DEFAULT = "http://chaoszero-dl.game.onstove.com/game/dpms_STOVE_CHAOSZERO"

# 单文件重试次数（网络抖动）
RETRY = 3
RETRY_BACKOFF = 1.5
# 下载读取超时：curl 的 read timeout 针对「无数据间隔」，不是总时长
DL_TIMEOUT = 300


# ====================================================================
# 数据模型
# ====================================================================
@dataclass(frozen=True)
class Entry:
    """清单 files[] 里的一条记录。

    官方格式（分隔符 ' | '，前后带空格，列数可变）：
        idx | type | path | group | seq | comp | orig_size | packed_size
            | md5_orig | md5_packed | flag | ...（多余列忽略）
    只解析前 11 列，不假设列数固定。
    """

    idx: int
    kind: str                  # D=目录 F=受管文件 G=空目录占位 R=删除
    path: str                  # 以 '/' 开头的安装内相对路径
    group: str = ""
    seq: str = ""
    comp: str = ""
    size: int = 0
    packed: int = 0
    md5: str = ""
    md5_packed: str = ""

    @property
    def rel(self) -> str:
        """转成本机相对路径（bin/x.dll）。"""
        return self.path.lstrip("/").replace("/", os.sep)

    def at(self, root: str) -> str:
        return os.path.join(root, self.rel)


@dataclass
class Manifest:
    version: int
    service_code: str
    root_folder: str
    execution: str
    entries: list
    raw: dict = field(default_factory=dict)

    @property
    def main_exe(self) -> str:
        """游戏主程序（安装内相对路径，正斜杠）。

        官方 execution 形如 "<加载器> <游戏本体>"，**取最后一个 token 即可** ——
        最后那个就是被补丁/汉化改过的文件。用官方字段而不是猜，逻辑只有一行。
        例：bin\\ucldr_..._loader_x64.exe bin\\ssr-stove-shield.exe → bin/ssr-stove-shield.exe
        """
        toks = str(self.execution or "").strip().split()
        if not toks:
            return ""
        return toks[-1].strip('"').replace("\\", "/").lstrip("/")

    @property
    def files(self) -> list:
        return [e for e in self.entries if e.kind == "F"]

    @property
    def dirs(self) -> list:
        return [e for e in self.entries if e.kind == "D"]

    @property
    def generates(self) -> list:
        return [e for e in self.entries if e.kind == "G"]

    @property
    def removes(self) -> list:
        return [e for e in self.entries if e.kind == "R"]

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.files)

    @property
    def total_packed(self) -> int:
        return sum(e.packed for e in self.files)


@dataclass
class Plan:
    """一次更新要做的事。"""

    target_version: int
    downloads: list = field(default_factory=list)
    removes: list = field(default_factory=list)
    mkdirs: list = field(default_factory=list)
    generates: list = field(default_factory=list)
    missing: list = field(default_factory=list)      # 文件不存在 → 必须补
    damaged: list = field(default_factory=list)      # 大小不符 → 视为损坏，必须修
    modified: list = field(default_factory=list)     # 大小相同但内容不同 → 通常是补丁/汉化
    kept: list = field(default_factory=list)         # 决定保留不动的（补丁/汉化，同版本）
    intact: int = 0                                  # 已通过校验、无需动作的文件数
    source: str = ""                                 # 判定依据，进日志
    upgraded: bool = False                           # 本次是否为「大更新」（版本号变化）

    @property
    def total_packed(self) -> int:
        return sum(e.packed for e in self.downloads)

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.downloads)

    def is_empty(self) -> bool:
        return not (self.downloads or self.removes or self.mkdirs or self.generates)


@dataclass
class Result:
    ok: bool
    message: str
    version: int = 0
    plan: Plan = None
    downloaded: int = 0
    bytes_written: int = 0
    failed: list = field(default_factory=list)


# ====================================================================
# 路径与本地账本
# ====================================================================
def _root(root=None) -> str:
    """安装根目录。默认取 czn_lite 的当前值（「获取离线信息」后会被更新）。"""
    return os.path.normpath(root or cl.INSTALL_ROOT or "")


def _u_bool(key, default=False):
    return cl._cfg_bool("update", key, default=default)


def _u_int(key, default=0):
    return cl._cfg_int("update", key, default=default)


def _u_str(key, default=""):
    return str(cl._cfg("update", key, default=default) or default)


def check_vcredist():
    """游戏依赖的 VC++ 2022 x64 运行时是否就绪（清单 vcredist 字段声明）。

    官方启动器会安装它；游戏目录里不含这些 DLL，缺失时游戏起不来。
    这里只做检测与提示，不擅自装东西。
    """
    if os.name != "nt":
        return True, "非 Windows 环境"
    need = ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")
    sysdir = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "System32")
    missing = [n for n in need if not os.path.exists(os.path.join(sysdir, n))]
    if missing:
        return False, "缺少 VC++ 2022 x64 运行时：%s（游戏可能无法启动）" % "、".join(missing)
    return True, "VC++ 2022 x64 运行时已就绪"


def manifest_dir(root: str) -> str:
    return os.path.join(root, MANIFEST_DIR)


def upf_path(root: str) -> str:
    return os.path.join(manifest_dir(root), "GameManifest_%s.upf" % cl.GAME_ID)


def cache_path(root: str) -> str:
    return os.path.join(manifest_dir(root), CACHE_DB)


def temp_dir(root: str) -> str:
    return os.path.join(root, TEMP_DIR)


def read_upf(root: str):
    """读本地清单头。返回 dict；不存在或损坏返回 None。"""
    p = upf_path(root)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_upf(root: str, version: int, project_url: str, extra=None):
    """回写 .upf。保留原有字段，只更新版本与清单地址。

    这是「本次更新已提交」的标记，必须在所有文件落地之后才写。
    """
    data = read_upf(root) or {}
    if extra:
        data.update(extra)
    data["local_version"] = int(version)
    data["game_id"] = cl.GAME_ID
    data["install_path"] = root
    if project_url:
        data["project_url"] = project_url
    data.setdefault("files", [])
    data.setdefault("locale", "kr")
    data.setdefault("env", "")
    data.setdefault("type_code", 1)
    data.setdefault("grades", [])

    os.makedirs(manifest_dir(root), exist_ok=True)
    tmp = upf_path(root) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)
    os.replace(tmp, upf_path(root))
    return data


def read_cache(root: str):
    """读 cacheii.db → {relpath: (size, md5)}。不存在返回空字典。"""
    p = cache_path(root)
    out = {}
    if not os.path.exists(p):
        return out
    try:
        con = sqlite3.connect("file:%s?mode=ro" % Path(p).as_posix(), uri=True)
        try:
            for name, size, h in con.execute(
                    "SELECT file_name, file_size, file_hash FROM installed_info"):
                out[str(name).replace("\\", "/")] = (int(size), str(h).lower())
        finally:
            con.close()
    except Exception:
        return {}
    return out


def write_cache(root: str, entries):
    """把受管文件的大小/哈希 UPSERT 进 cacheii.db（官方同款 schema）。"""
    os.makedirs(manifest_dir(root), exist_ok=True)
    con = sqlite3.connect(cache_path(root))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS installed_info ("
                    "file_name TEXT NOT NULL,"
                    "file_size BIGINT NOT NULL,"
                    "file_hash VARCHAR(32) NOT NULL,"
                    "PRIMARY KEY(file_name))")
        con.executemany(
            "INSERT INTO installed_info (file_name, file_size, file_hash) "
            "VALUES (?1, ?2, ?3) "
            "ON CONFLICT(file_name) DO UPDATE SET "
            "file_size = excluded.file_size, file_hash = excluded.file_hash",
            [(e.rel.replace(os.sep, "/"), e.size, e.md5) for e in entries])
        con.commit()
    finally:
        con.close()


def local_version(root=None):
    """本地版本：注册表与 .upf 双源。

    两者不一致时取较小值（保守：宁可多校验一次，也不要漏掉更新）。
    返回 (版本, 说明)。
    """
    root = _root(root)
    notes = []
    reg = None
    if os.name == "nt":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"SOFTWARE\SGUP\apps\%s" % cl.GAME_ID) as k:
                reg = int(winreg.QueryValueEx(k, "Version")[0])
            notes.append("注册表=%d" % reg)
        except Exception:
            notes.append("注册表未读到")
    upf = read_upf(root)
    doc = None
    if upf is not None and str(upf.get("local_version", "")).isdigit():
        doc = int(upf["local_version"])
        notes.append(".upf=%d" % doc)
    else:
        notes.append(".upf 未读到")

    vals = [v for v in (reg, doc) if v is not None]
    if not vals:
        return 0, "无本地版本记录（" + "；".join(notes) + "）"
    ver = min(vals)
    if len(set(vals)) > 1:
        notes.append("两源不一致，取较小值 %d" % ver)
    return ver, "；".join(notes)


# ====================================================================
# 清单
# ====================================================================
def parse_manifest(obj) -> Manifest:
    """解析 DPMS 清单。列数自适应，只取前 11 列。"""
    if not isinstance(obj, dict):
        raise ValueError("清单顶层不是 JSON 对象")
    entries = []
    for line in obj.get("files") or []:
        parts = [x.strip() for x in str(line).split("|")]
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        e = Entry(idx=idx, kind=parts[1], path=parts[2])
        if len(parts) > 3:
            e = Entry(idx, e.kind, e.path, parts[3],
                      parts[4] if len(parts) > 4 else "",
                      parts[5] if len(parts) > 5 else "",
                      _int(parts[6] if len(parts) > 6 else 0),
                      _int(parts[7] if len(parts) > 7 else 0),
                      (parts[8] if len(parts) > 8 else "").lower(),
                      (parts[9] if len(parts) > 9 else "").lower())
        entries.append(e)

    ver = obj.get("version_no")
    return Manifest(
        version=int(ver) if str(ver).isdigit() else 0,
        service_code=str(obj.get("service_code") or ""),
        root_folder=str(obj.get("root_folder") or ""),
        execution=str(obj.get("execution") or ""),
        entries=entries, raw=obj)


def _int(v, default=0):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return default


def fetch_manifest(url, session=None, timeout=30):
    """拉清单。带 ?timestamp= 破缓存（官方同款）。"""
    if "?" in url:
        full = url
    else:
        full = "%s?timestamp=%d" % (url, int(time.time()))
    data = _get_bytes(full, session, timeout)
    return parse_manifest(json.loads(data.decode("utf-8-sig"))), full


def build_url(manifest_url: str, e: Entry) -> str:
    """下载地址 = 清单 URL 所在目录 + v<group>/<seq>.gz。"""
    base = manifest_url.split("?")[0].rsplit("/", 1)[0]
    return "%s/v%s/%s.gz" % (base, e.group, e.seq)


def manifest_variants(manifest_url: str) -> list:
    """同一版本清单的 v1/v2 两种形态，v2 在前（优先）。

    证据（2026-09-18 抓包）：官方启动器现在拉的是
    `<dir>/<code>_<N>_v2.json?timestamp=…`（16 列），而 .upf 里记录的
    project_url 仍是 v1 形态 `<dir>/<code>_<N>.json`（13 列）——两种都在线。
    无法识别命名形态时原样返回。
    """
    clean = manifest_url.split("?")[0]
    base, _, name = clean.rpartition("/")
    m = re.match(r"^(?P<code>.+)_(?P<ver>\d+)\.json$", name)
    if not m:
        return [manifest_url]
    return ["%s/%s_%s_v2.json" % (base, m.group("code"), m.group("ver")),
            manifest_url]


def fetch_manifest_preferred(url, session=None, timeout=30):
    """拉清单：优先 _v2 形态（与官方一致），v2 拿不到（403/404）退回 v1。"""
    variants = manifest_variants(url)
    last = None
    for u in variants:
        try:
            return fetch_manifest(u, session, timeout)
        except (_HttpError, urllib.error.HTTPError) as exc:
            last = exc
            if int(getattr(exc, "code", 0)) not in (403, 404):
                raise
    raise last


# ====================================================================
# HTTP（复用 czn_lite 的网络路由与日志）
# ====================================================================
def _session(session=None):
    """优先用传入的会话（复用代理路由与结构化日志）；没有就自建一个。"""
    if session is not None:
        return session
    return cl._LoggedSession(cl.requests.Session(
        impersonate="chrome", default_headers=False,
        http_version=cl.CurlHttpVersion.V1_1))


class _HttpError(Exception):
    """带状态码的 HTTP 错误（curl_cffi 不抛 HTTPError，自己带一个，便于按码分流）。"""

    def __init__(self, code, url=""):
        super().__init__("HTTP %s" % code)
        self.code = int(code)
        self.url = url


def _get_bytes(url, session=None, timeout=30):
    s = _session(session)
    r = s.get(url, timeout=timeout)
    if getattr(r, "status_code", 0) != 200:
        raise _HttpError(r.status_code, url)
    return r.content


def _probe_status(url, session):
    """探测单个 URL：返回 'ok' / '404' / 其它情况的说明文本。"""
    try:
        _get_bytes(url, session, timeout=15)
        return "ok"
    except (_HttpError, urllib.error.HTTPError) as exc:
        code = int(getattr(exc, "code", 0))
        if code == 404:
            return "404"
        return "HTTP %s（CDN 可能限流或拦截）" % (code or "?")
    except Exception as exc:
        return "探测失败：%s" % exc


def probe_latest_version(manifest_url_tpl, start, session=None, limit=PROBE_MAX_AHEAD,
                         sanity_version=0):
    """免鉴权兜底：从 start 起递增探测清单，返回 (最新版本, 错误说明)。

    语义（证据：同一 CDN 对「不存在的版本」昨天回 404、今天回 403 —— 返回码漂移）：
      · 模板可以是 v1/v2 两种形态的列表，任一形态 200 即视为该版本存在；
      · 所有形态都 404 才算「到达边界」（确定没有更新的版本）；
      · 先探测 sanity_version（=本地当前版本）做通道自检：连当前版本的清单
        都拿不到 ⇒ 探测通道不可信，如实报错 —— 绝不能把 403/网络错误悄悄
        当成「没有更新」。
    """
    tpls = list(manifest_url_tpl) if isinstance(manifest_url_tpl, (list, tuple)) \
        else [manifest_url_tpl]
    if sanity_version:
        st = _probe_status(tpls[0] % sanity_version, session)
        if st != "ok":
            return 0, "探测通道不可信（当前版本 %d 清单 %s）" % (sanity_version, st)
    best = 0
    for n in range(start, start + limit + 1):
        sts = [_probe_status(t % n, session) for t in tpls]
        if "ok" in sts:
            best = n
            continue
        if all(s == "404" for s in sts):
            return best, ""
        return best, next((s for s in sts if s != "404"), "?")
    return best, ""


def buildinfo_url(manifest_url: str, version: int) -> str:
    """版本旁证对象地址（证据：2026-09-18 抓包，官方对 v54 拉过两次
    `<dir>/v54/buildInfo.json`，200 返回 PCSDK 配置 JSON）。"""
    base = manifest_url.split("?")[0].rsplit("/", 1)[0]
    return "%s/v%d/buildInfo.json" % (base, version)


def check_buildinfo(manifest_url, version, session=None, timeout=15):
    """旁证：目标版本的 buildInfo.json 可取且 game_id 匹配。

    非权威校验（文件校验仍以清单两级 MD5 为准），只用于确认「目标版本对象
    在 CDN 上完整存在」。失败不阻断 —— 返回 (False, 说明)。
    """
    try:
        data = _get_bytes(buildinfo_url(manifest_url, version), session, timeout)
        info = json.loads(data.decode("utf-8-sig"))
        ok = str(info.get("game_id") or "") == cl.GAME_ID
        note = "buildInfo 旁证通过（pcsdk %s）" % (info.get("pcsdk_version") or "?")
        return ok, note
    except Exception as exc:
        return False, "buildInfo 旁证不可用（%s）" % exc


# ====================================================================
# 校验
# ====================================================================
def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def verify_entry(root: str, e: Entry):
    """等价官方 IIV_EXIST_HASH：存在 + 大小 + MD5。返回 (是否通过, 原因)。"""
    p = e.at(root)
    if not os.path.isfile(p):
        return False, "缺失"
    try:
        actual = os.path.getsize(p)
    except OSError as exc:
        return False, "取大小失败 %s" % exc
    if e.size and actual != e.size:
        return False, "大小不符 %d≠%d" % (actual, e.size)
    if e.md5 and md5_file(p) != e.md5:
        return False, "MD5 不符"
    return True, "ok"


def verify_all(root: str, manifest: Manifest, on_event=None, cancel=None):
    """全量巡检（L2）。返回 (通过数, 失败列表)。"""
    log = on_event or (lambda m: None)
    bad = []
    ok = 0
    total = len(manifest.files)
    for i, e in enumerate(manifest.files, 1):
        if cancel is not None and cancel():
            log("[校验] 已取消（%d/%d）" % (i - 1, total))
            break
        good, why = verify_entry(root, e)
        if good:
            ok += 1
        else:
            bad.append((e, why))
        log("[校验] %d/%d %s %s" % (i, total, e.rel, "OK" if good else "★ " + why))
    return ok, bad


# ====================================================================
# 计划
# ====================================================================
def plan_update(root: str, manifest: Manifest, source="", on_event=None, mode="full",
                replace_modified=False, upgraded=False):
    """按清单生成本次要做的动作。

    mode='full'        逐文件算 MD5（等价官方 IIV_EXIST_HASH，默认）
    mode='quick'       只比 cacheii.db 的记录，不读文件内容（快，但发现不了文件被改）
    replace_modified   是否替换「存在但与官方不符」的文件：
                       · 大更新（版本号变了）必须为 True —— 否则旧的主程序会留下来
                       · 同版本默认 False —— 这类文件通常是用户的补丁/汉化，不能当损坏修掉
    """
    log = on_event or (lambda m: None)
    cache = read_cache(root)
    plan = Plan(target_version=manifest.version, source=source, upgraded=upgraded)

    def _judge(e):
        if mode == "full":
            return verify_entry(root, e)
        p = e.at(root)
        if not os.path.isfile(p):
            return False, "缺失"
        cached = cache.get(e.rel.replace(os.sep, "/"))
        if cached is not None:
            return (cached == (e.size, e.md5)), "账本不符"
        return (os.path.getsize(p) == e.size), "大小不符"

    for e in manifest.files:
        good, why = _judge(e)
        if good:
            plan.intact += 1
            cached = cache.get(e.rel.replace(os.sep, "/"))
            if cached != (e.size, e.md5):
                log("[计划] 文件正确但账本不符，将回写：%s" % e.rel)
            continue

        p = e.at(root)
        if not os.path.isfile(p):
            plan.missing.append(e)
            log("[计划] 缺失：%s" % e.rel)
            plan.downloads.append(e)
            continue

        # 存在但与官方不符。补丁/汉化是**定长重建**（大小不变），所以：
        #   大小不符 = 损坏，必须修；大小相同 = 用户改动，同版本下保留
        plan.modified.append(e)
        if e.size and os.path.getsize(p) != e.size:
            plan.damaged.append(e)
            log("[计划] 大小不符，按损坏处理：%s（%s）" % (e.rel, why))
            plan.downloads.append(e)
        elif replace_modified:
            log("[计划] 与官方不同，将覆盖：%s（%s）" % (e.rel, why))
            plan.downloads.append(e)
        else:
            plan.kept.append(e)
            log("[计划] 与官方不同（通常是补丁/汉化），保留不动：%s（%s）" % (e.rel, why))

    for e in manifest.dirs:
        if not os.path.isdir(e.at(root)):
            plan.mkdirs.append(e)

    for e in manifest.generates:
        p = e.at(root)
        if not os.path.exists(p):
            plan.generates.append(e)

    for e in manifest.removes:
        if os.path.exists(e.at(root)):
            plan.removes.append(e)

    plan.downloads.sort(key=lambda x: x.idx)
    return plan


# ====================================================================
# 下载与替换
# ====================================================================
def _locked(path):
    """文件是否被占用（Windows 上对运行中的 exe 会拒绝写打开）。"""
    if not os.path.exists(path):
        return False
    try:
        fd = os.open(path, os.O_RDWR)
    except OSError:
        return True
    else:
        os.close(fd)
        return False


def disk_free(path):
    try:
        return shutil.disk_usage(path).free
    except Exception:
        return None


def _etag_ok(url, session, md5_packed):
    """HEAD 预检：服务端对象的 ETag 是否与清单的压缩包 MD5 一致。

    证据（2026-09-18 抓包）：v54/1.gz 响应头 ETag "cbf3851a…" == 清单第 9 列
    （md5_packed）—— S3/CloudFront 非 multipart 对象的 ETag 就是内容 MD5。
    返回 True/False；取不到 ETag 或 HEAD 失败时返回 None（无法判断，照常进行）。
    价值：断点续传/复用本地缓存前先 5KB 头部确认对象没换，避免对着已更换的
    CDN 对象续传拼出坏文件、或完整下载后才在 MD5 校验上失败。
    """
    try:
        r = session.head(url, timeout=30)
        if getattr(r, "status_code", 0) != 200:
            return None
        headers = getattr(r, "headers", None) or {}
        et = headers.get("ETag") or headers.get("etag")
        if not et or not md5_packed:
            return None
        return et.strip('"').lower() == str(md5_packed).lower()
    except Exception:
        return None


def download_entry(root: str, e: Entry, manifest_url: str, session=None,
                   resume=True, backup=True, on_progress=None):
    """下载 → 两级 MD5 → 解压 → 原子替换。

    临时文件放在 <root>\\.stove_temp\\<group>_<seq>.gz（与官方一致），
    解压产物先落在目标同目录的 .cznnew，校验通过后再 rename —— 保证替换是原子的。
    """
    os.makedirs(temp_dir(root), exist_ok=True)
    dst = e.at(root)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    gz = os.path.join(temp_dir(root), "%s_%s.gz" % (e.group, e.seq))
    url = build_url(manifest_url, e)
    s = _session(session)

    if _locked(dst):
        raise RuntimeError("目标文件被占用或不可写（游戏可能在运行）")

    # ---- 1) 拿压缩包（支持续传）----
    have = os.path.getsize(gz) if os.path.exists(gz) else 0
    if have and e.md5_packed and resume:
        # ETag 预检：服务端对象已换（ETag ≠ 清单 md5_packed）⇒ 本地任何断点/
        # 缓存都作废，直接重下（省得对着坏对象续传，或下载完才在 MD5 上失败）
        et = _etag_ok(url, s, e.md5_packed)
        if et is False:
            try:
                os.remove(gz)
            except OSError:
                pass
            have = 0
    need_dl = True
    if have and e.packed:
        if have > e.packed:
            os.remove(gz)
            have = 0
        elif have == e.packed and (not e.md5_packed or md5_file(gz) == e.md5_packed):
            need_dl = False                        # 已完整且校验通过
        elif have == e.packed:
            os.remove(gz)
            have = 0

    if need_dl:
        _download_to(gz, url, s, resume=resume and have > 0, have=have,
                     expect_size=e.packed, on_progress=on_progress)

    if e.md5_packed and md5_file(gz) != e.md5_packed:
        if os.path.exists(gz):
            os.remove(gz)
        raise RuntimeError("压缩包 MD5 不符（已丢弃，可重试）")

    # ---- 2) 解压到目标同目录的临时文件 ----
    new = dst + ".cznnew"
    written = 0
    with gzip.open(gz, "rb") as fin, open(new, "wb") as fout:
        while True:
            b = fin.read(1 << 20)
            if not b:
                break
            fout.write(b)
            written += len(b)
    if e.size and written != e.size:
        os.remove(new)
        raise RuntimeError("解压后大小不符 %d≠%d" % (written, e.size))

    # ---- 3) 校验原始 MD5，然后原子替换 ----
    if e.md5 and md5_file(new) != e.md5:
        os.remove(new)
        raise RuntimeError("解压后 MD5 不符（已丢弃，可重试）")

    if backup and os.path.exists(dst):
        try:
            shutil.copy2(dst, dst + BACKUP_SUFFIX)
        except Exception:
            pass                                    # 备份失败不阻断
    os.replace(new, dst)
    return written


def _download_to(dst, url, session, resume=False, have=0, expect_size=0, on_progress=None):
    """HTTP 下载；resume=True 且已有部分内容时用 Range 续传。"""
    headers = {}
    if resume and have:
        headers["Range"] = "bytes=%d-" % have
    last = None
    for attempt in range(1, RETRY + 1):
        try:
            r = session.get(url, headers=headers or None, timeout=DL_TIMEOUT, stream=True)
            code = getattr(r, "status_code", 0)
            if code not in (200, 206):
                raise RuntimeError("HTTP %s" % code)
            if code == 206:
                mode = "ab"
            else:
                mode = "wb"
                have = 0
            total = have
            with open(dst, mode) as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if not chunk:
                        continue
                    f.write(chunk)
                    total += len(chunk)
                    if on_progress:
                        on_progress(len(chunk), total, expect_size)
            if expect_size and os.path.getsize(dst) != expect_size:
                raise RuntimeError("下载大小不符 %d≠%d"
                                   % (os.path.getsize(dst), expect_size))
            return
        except Exception as exc:
            last = exc
            if attempt < RETRY:
                time.sleep(RETRY_BACKOFF * attempt)
    raise RuntimeError("下载失败：%s" % last)


def apply_plan(root: str, manifest: Manifest, plan: Plan, manifest_url: str,
               session=None, workers=1, resume=True, backup=True,
               on_event=None, cancel=None, on_progress=None):
    """执行计划：建目录 → 下载替换 → 删遗留 → 写账本 → 写 .upf。"""
    log = on_event or (lambda m: None)
    res = Result(ok=True, message="", version=manifest.version, plan=plan)

    # 1) 目录与占位（幂等）
    for e in plan.mkdirs:
        os.makedirs(e.at(root), exist_ok=True)
        log("[更新] 建目录 %s" % e.rel)
    for e in plan.generates:
        p = e.at(root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        try:
            open(p, "ab").close()
            log("[更新] 建占位 %s" % e.rel)
        except Exception as exc:
            log("[更新] 占位创建失败（忽略）：%s %s" % (e.rel, exc))

    # 2) 下载（默认串行；workers>1 时每个 worker 各自建会话，避免共享连接）
    done = 0
    total = len(plan.downloads)
    if total:
        log("[更新] 待下载 %d 个文件，共 %.1f MB"
            % (total, plan.total_packed / 1048576))

    def _one(e, sess):
        if cancel is not None and cancel():
            raise RuntimeError("已取消")
        n = download_entry(root, e, manifest_url, sess,
                           resume=resume, backup=backup,
                           on_progress=on_progress)
        return e, n

    if workers > 1 and total > 1:
        # 每个工作线程各持一个会话 —— curl 的 Session 不是线程安全的
        tl = threading.local()

        def _threaded(e):
            s = getattr(tl, "s", None)
            if s is None:
                s = _session(None)
                tl.s = s
            return _one(e, s)

        with ThreadPoolExecutor(max_workers=min(32, workers)) as pool:
            futs = {pool.submit(_threaded, e): e for e in plan.downloads}
            for fut in futs:
                e = futs[fut]
                try:
                    _, n = fut.result()
                    done += 1
                    res.downloaded += 1
                    res.bytes_written += n
                    log("[更新] %d/%d 完成 %s" % (done, total, e.rel))
                except Exception as exc:
                    res.ok = False
                    res.failed.append((e, str(exc)))
                    log("[更新] ★ 失败 %s：%s" % (e.rel, exc))
    else:
        for e in plan.downloads:
            try:
                _, n = _one(e, session)
                done += 1
                res.downloaded += 1
                res.bytes_written += n
                log("[更新] %d/%d 完成 %s" % (done, total, e.rel))
            except Exception as exc:
                res.ok = False
                res.failed.append((e, str(exc)))
                log("[更新] ★ 失败 %s：%s" % (e.rel, exc))

    # 3) 删除遗留（不存在/被占用一律跳过，与官方一致）
    for e in plan.removes:
        p = e.at(root)
        try:
            if os.path.isdir(p):
                os.rmdir(p)
            else:
                os.remove(p)
            log("[更新] 已删除遗留 %s" % e.rel)
        except FileNotFoundError:
            pass
        except OSError as exc:
            log("[更新] 遗留文件跳过（%s）：%s" % (e.rel, exc))

    # 4) 失败就不写账本与版本号 —— 版本号是「本次更新已提交」的标记
    if not res.ok:
        res.message = "有 %d 个文件失败，未提交版本号" % len(res.failed)
        return res

    good = []
    for e in manifest.files:
        ok, _ = verify_entry(root, e)
        if ok:
            good.append(e)
    write_cache(root, good)
    write_upf(root, manifest.version, manifest_url,
              extra={"game_title": manifest.raw.get("service_name") or ""})
    res.message = "已更新到版本 %d" % manifest.version
    log("[更新] %s（%d 个文件，%.1f MB）"
        % (res.message, res.downloaded, res.bytes_written / 1048576))
    return res


# ====================================================================
# 游戏资源层（SSRA / cznlive）—— 识别、检测、报告；不下载、不替换
# --------------------------------------------------------------------
# 游戏由两部分组成，更新通道**完全独立**：
#   ① 本体 DPMS   <game>\bin\*.dll|*.exe          20 个受管文件  269 MB
#                 由官方 STOVE 启动器（InstallLib）负责 → 本模块负责
#   ② 资源 SSRA   <game>\bin\appdata\cznlive\     ~19.4 GB（占整个游戏的 98%）
#                 由**游戏引擎自己在运行时**更新
#
# 生产协议（2026-09-19 MITM 抓包 + 重放实测，方案 v2 §7.1 已关闭）：
#   入口  GET https://live-czn-entry2lx2fz.game.playstove.com:13001/cznlive
#         ?platform=win32&appid=cznlive&build=<N>&lang=..&oslang=..
#         &package=<market_game_id>&device_uid=..&publisher_uid=&buildx=<hex>
#         —— 零鉴权；X-App-Id/X-App-NS 可省；build 参数容错；
#            响应间唯一差异是 _entry_timestamp（nonce）
#   配置  响应即游戏的世界/版本配置（= 生产 verinfo）：
#           cdn.url = https://czn-live-down.game.playstove.com/patch/1.0.46407/WLOP8Q5CZ9HW/
#           cdn.version.policies = "res,media,text"
#           cdn.version_res/media/text.current = 688 / 227 / 688
#           cdn.context = "$(remote.res.version)/$(remote.res.version)-$(local.res.version).tar.lz4"
#           app.api（wss 游戏套接字）、title_movie_cdn、build.version=464
#   比对  引擎实际比对的「本地组修订号」在 data.indices/<组>.pigz 尾部：
#           8 字节 `@ver` + u32（实测 res=688 / media=227 / text=688，
#           与远端逐组一致 ⇒ 无更新；pcrevs 文件名里的 text_ko=685 /
#           text_zht=687 是语言子层修订，不是组级修订）
#
# 本模块对资源层**只做识别、检测、如实报告，绝不下载或替换**：
#   · 19.4 GB，写坏代价极高；增量包（tar.lz4）的落地与校验由游戏引擎完成
#   · data.pack 是加密的、*.pigz 是 PLPcK、pcrevs 是二进制 —— 都是游戏私有格式
#   · 官方启动器同样不碰它；游戏引擎自己会更新
# ====================================================================
GAMEDATA_REL = os.path.join("bin", "appdata", "cznlive")
# 入口 API 与默认参数（均为 2026-09-19 实测值；可在 config.json 覆盖）
GAMERES_ENTRY_DEFAULT = ("https://live-czn-entry2lx2fz.game.playstove.com"
                         ":13001/cznlive")
GAMERES_APPID_DEFAULT = "cznlive"          # exe 内嵌（"cznlive.1.0.464"）
GAMERES_NS_DEFAULT = "ssr-base-260909"     # 抓包实测 X-App-NS（非必需头）
GAMERES_BUILD_DEFAULT = 464                # exe 内嵌版本 1.0.464
GAMERES_INDEX_VER_MARK = b"@ver"           # *.pigz 尾部的组修订记录标记
_PCREV_RE = re.compile(r"^_(?P<group>.+)_(?P<rev>\d+)_(?P<digest>[0-9a-fA-F]{16,})\.pcrevsz$")
_PACK_RE = re.compile(r"^data\.pack(~\d+)?$")


@dataclass(frozen=True)
class DataRevision:
    group: str
    revision: int
    digest: str


def gamedata_dir(root=None) -> str:
    return os.path.join(_root(root), GAMEDATA_REL)


def read_gamedata_revisions(root=None):
    """本地各组资源修订号。

    游戏把「每组资源当前修订」写在
    data.indices/pcrevs/_<组>_<修订>_<哈希>.pcrevsz 的**文件名**里 ——
    这是它自己维护的账本，读文件名即可，不必去解那个二进制。
    """
    d = os.path.join(gamedata_dir(root), "data.indices", "pcrevs")
    out = []
    if not os.path.isdir(d):
        return out
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        m = _PCREV_RE.match(name)
        if m:
            out.append(DataRevision(m.group("group"), int(m.group("rev")),
                                    m.group("digest").lower()))
    out.sort(key=lambda r: r.group)
    return out


def gamedata_summary(root=None) -> dict:
    """本地资源层概况（只读，不解包）。"""
    root = _root(root)
    d = gamedata_dir(root)
    info = {"path": d, "exists": os.path.isdir(d), "packs": 0, "pack_bytes": 0,
            "unpacked_bytes": 0, "revisions": read_gamedata_revisions(root)}
    if not info["exists"]:
        return info
    try:
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if _PACK_RE.match(name) and os.path.isfile(p):
                info["packs"] += 1
                info["pack_bytes"] += os.path.getsize(p)
    except OSError:
        pass
    up = os.path.join(d, "data.unpacked")
    if os.path.isdir(up):
        total = 0
        for base, _dirs, files in os.walk(up):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(base, f))
                except OSError:
                    pass
        info["unpacked_bytes"] = total
    return info


def log_gamedata_local(root=None, on_event=None) -> dict:
    """只报本地资源层概况（不联网，用于每次启动时的一句摘要）。"""
    log = on_event or (lambda m: None)
    s = gamedata_summary(root)
    if not s["exists"]:
        log("[资源] 未找到 %s" % s["path"])
        return s
    log("[资源] 数据包 %d 个 %.2f GB + 已展开 %.2f GB；本地修订 %s"
        % (s["packs"], s["pack_bytes"] / 1073741824, s["unpacked_bytes"] / 1073741824,
           "，".join("%s=%d" % (r.group, r.revision) for r in s["revisions"]) or "无"))
    return s


def read_index_versions(root=None) -> dict:
    """各组资源的「本地补丁修订号」—— 引擎实际用来比对的那个数。

    证据（2026-09-19 实测解包）：data.indices/<组>.pigz（gzip 的 PLPcK 索引）
    末尾 8 字节是 `@ver` + u32 修订号：
      text.pigz → 688 == 远端 cdn.version_text.current=688
      media.pigz → 227 == 远端 cdn.version_media.current=227
      res.pigz  → 688 == 远端 cdn.version_res.current=688
    （pcrevs 文件名里的 text_ko=685 / text_zht=687 是**语言子层**修订，
    不是引擎比对的组级修订 —— 组级修订在索引 @ver 里。）
    """
    d = os.path.join(gamedata_dir(root), "data.indices")
    out = {}
    if not os.path.isdir(d):
        return out
    try:
        names = os.listdir(d)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".pigz"):
            continue
        try:
            with gzip.open(os.path.join(d, name), "rb") as f:
                data = f.read()
        except (OSError, EOFError, gzip.BadGzipFile):
            continue
        if len(data) >= 8 and data[-8:-4] == GAMERES_INDEX_VER_MARK:
            out[name[:-5]] = struct.unpack_from("<I", data, len(data) - 4)[0]
    return out


def fetch_world_config(session=None, on_event=None, entry_url=None) -> dict:
    """拉游戏的入口/版本配置（生产 verinfo，2026-09-19 抓包 + 重放实测）。

    请求形态（照抄游戏原样，参数服务端不校验）：
      GET <entry>?platform=win32&appid=<appid>&build=<build>&lang=..&oslang=..
         &package=<market_game_id>&device_uid=<uuid>&publisher_uid=&buildx=<hex>
    实测：零鉴权；缺 X-App-Id/X-App-NS 头也 200；build=400 一样返回当前配置。
    返回 world.<world>.<branch> 节点（cdn.url / cdn.version_*.current /
    app.api / build.version / title_movie_cdn …）。
    """
    log = on_event or (lambda m: None)
    base = (entry_url or _u_str("gameres_entry_url", GAMERES_ENTRY_DEFAULT)).rstrip("/")
    appid = _u_str("gameres_appid", GAMERES_APPID_DEFAULT)
    build = _u_int("gameres_build", GAMERES_BUILD_DEFAULT)
    world = _u_str("gameres_world", "asia")
    branch = _u_str("gameres_branch", "live")
    qs = urllib.parse.urlencode({
        "platform": "win32", "appid": appid, "build": build,
        "lang": "zht", "oslang": "zhs",
        "package": cl.MARKET_GAME_ID, "device_uid": uuid.uuid4().hex,
        "publisher_uid": "", "buildx": "0" * 64,
    })
    url = base + "?" + qs
    headers = {"X-App-Id": appid,
               "X-App-NS": _u_str("gameres_ns", GAMERES_NS_DEFAULT)}
    r = _session(session).get(url, headers=headers, timeout=20)
    if getattr(r, "status_code", 0) != 200:
        raise _HttpError(r.status_code, url)
    data = json.loads(r.content.decode("utf-8-sig"))
    node = (((data.get("world") or {}).get(world) or {}).get(branch) or {})
    if not node:
        raise ValueError("配置缺 world.%s.%s（现有：%s）"
                         % (world, branch,
                            list((data.get("world") or {}).keys())))
    return node


def check_gamedata(root=None, session=None, on_event=None, entry_url=None):
    """识别并检测资源层（只读；**不下载、不替换**）。

    P1（2026-09-19 生产协议取证后）：逐组比对
      本地  data.indices/<组>.pigz 尾部 @ver 修订号（引擎实际比对的值）
      远端  入口 API world.<world>.<branch> 的 cdn.version_<组>.current
      增量  cdn.context 模板 $(remote)/$(remote)-$(local).tar.lz4（cdn.url 下）
    远端不可达时如实报告，不影响启动；增量包由游戏引擎运行时自取。
    """
    log = on_event or (lambda m: None)
    root = _root(root)
    s = log_gamedata_local(root, log)
    if not s["exists"]:
        return Result(False, "未找到资源目录：%s" % s["path"])

    local_ver = read_index_versions(root)
    if local_ver:
        log("[资源] 本地组修订（索引 @ver）：%s"
            % "，".join("%s=%d" % kv for kv in sorted(local_ver.items())))

    try:
        node = fetch_world_config(session, log, entry_url=entry_url)
    except Exception as exc:
        log("[资源] 入口配置不可达（%s）" % str(exc)[:140])
        log("[资源] 资源层由游戏引擎在运行时自更新，这里只做识别与报告，不影响启动")
        return Result(True, "资源层：本地组修订 %s（远端不可达）"
                      % (dict(sorted(local_ver.items())) or "未知"))

    cdn_url = str(node.get("cdn.url") or "")
    log("[资源] 远端 build.version=%s  cdn.url=%s"
        % (node.get("build.version"), cdn_url))
    policies = [p.strip() for p in
                str(node.get("cdn.version.policies") or "").split(",") if p.strip()]

    diffs = []
    for g in policies:
        remote_v = _int(node.get("cdn.version_%s.current" % g), 0)
        local_v = local_ver.get(g)
        if local_v is None:
            state = "本地未知"
        elif local_v == remote_v:
            state = "一致"
        elif local_v > remote_v:
            state = "本地较新?"
        else:
            state = "可更新"
        log("[资源]   组 %-5s 本地=%s 远端=%d ⇒ %s"
            % (g, local_v if local_v is not None else "?", remote_v, state))
        if local_v is not None and remote_v > local_v:
            diffs.append((g, local_v, remote_v))

    if not diffs:
        return Result(True, "资源层：所有组与远端一致（%s）"
                      % "，".join("%s=%d" % (g, _int(node.get("cdn.version_%s.current" % g)))
                                  for g in policies))

    parts = []
    for g, lv, rv in diffs:
        # cdn.context 模板原文：$(remote)/$(remote)-$(local).tar.lz4（抓包实测）
        delta = "%s%s/%s-%s.tar.lz4" % (cdn_url, rv, rv, lv)
        parts.append("%s %d→%d" % (g, lv, rv))
        log("[资源]   增量包（按 cdn.context 模板推导，游戏运行时自取）：%s" % delta)
    return Result(True, "资源层：%d 组可更新（%s）—— 游戏启动后会自行下载"
                  % (len(diffs), "，".join(parts)))


# ====================================================================
# 编排
# ====================================================================
def _probe_templates(url):
    """探测地址的 v1/v2 双形态模板。

    有 .upf 时从 project_url 推导（现状）；全新安装（无 .upf）时由 DPMS 命名
    约定基址（config: update.dpms_manifest_url_base）构造 —— 本地版本为 0，
    从 local_version+1=1 起探测。"""
    if url:
        base = url.split("?")[0].rsplit("/", 1)[0]
        m = re.search(r"/([^/]+?)_\d+(_v2)?\.json$", url.split("?")[0])
        code = m.group(1) if m else cl.GAME_ID
    else:
        base = _u_str("dpms_manifest_url_base", DPMS_BASE_DEFAULT).rstrip("/")
        code = cl.GAME_ID
    if not base:
        return []
    return [base + "/" + code + "_%d_v2.json",
            base + "/" + code + "_%d.json"]


def check(root=None, session=None, on_event=None, allow_probe=None):
    """只读：判断本地版本与最新版本。

    返回 dict：{local, live, need_update, manifest_url, probe_url, note}
    """
    log = on_event or (lambda m: None)
    if allow_probe is None:
        allow_probe = _u_bool("probe_fallback", True)
    root = _root(root)
    if not root or not os.path.isdir(root):
        return {"error": "install_root 未配置或不存在", "local": 0, "live": 0}

    ver, note = local_version(root)
    log("[更新] 本地版本 %d（%s）" % (ver, note))

    upf = read_upf(root) or {}
    url = str(upf.get("project_url") or "")
    probe_tpls = _probe_templates(url)

    live, source, probe_error = 0, "", ""
    determined = False
    try:
        api = ("%s/dpms/game/v3.1/live_version?game_id=%s&local_version=%d&pc_room=false"
               % (cl.API, cl.GAME_ID, ver))
        data = json.loads(_get_bytes(api, session, timeout=20).decode("utf-8"))
        val = data.get("value") or {}
        live = int(val.get("live_version") or 0)
        url = str(val.get("live_project_file_url") or url)
        source = "DPMS API"
        determined = bool(live)
        log("[更新] 服务端最新版本 %d（%s）" % (live, source))
    except Exception as exc:
        log("[更新] 版本接口不可用（%s）" % exc)
        if allow_probe and probe_tpls:
            probed, perr = probe_latest_version(probe_tpls, ver + 1, session,
                                                sanity_version=ver)
            if perr:
                probe_error = perr
                log("[更新] 清单探测不可用：%s" % perr)
            else:
                # 探测到达 404 边界 = 确定「没有更新的版本」
                determined = True
                if probed:
                    live = probed
                    url = probe_tpls[1] % probed
                    source = "清单探测"
                    log("[更新] 探测到最新版本 %d（%s）" % (live, source))
                else:
                    log("[更新] 探测确认：没有更新的版本")

    # 版本旁证：目标版本的 buildInfo.json（非权威，失败不阻断）
    bi_note = ""
    if determined and live and url and _u_bool("buildinfo_check", True):
        bi_ok, bi_note = check_buildinfo(url, live, session)
        log("[更新] %s%s" % ("" if bi_ok else "★ ", bi_note))

    return {"local": ver, "live": live, "need_update": bool(live and live > ver),
            "determined": determined, "probe_error": probe_error,
            "manifest_url": url, "probe_url": (probe_tpls[1] if probe_tpls else None),
            "buildinfo": bi_note, "note": note, "source": source, "root": root}


def verify(root=None, manifest_url=None, session=None, on_event=None, cancel=None):
    """只读：全量完整性校验（L2）。"""
    log = on_event or (lambda m: None)
    root = _root(root)
    if not root or not os.path.isdir(root):
        return Result(False, "install_root 未配置或不存在")
    if not manifest_url:
        upf = read_upf(root) or {}
        manifest_url = str(upf.get("project_url") or "")
    if not manifest_url:
        return Result(False, "没有可用的清单地址（先「获取离线信息」）")
    man, used = fetch_manifest_preferred(manifest_url, session)
    log("[校验] 清单 %s（版本 %d，%d 条）" % (used.split("?")[0], man.version, len(man.entries)))
    ok, bad = verify_all(root, man, on_event=log, cancel=cancel)
    total = len(man.files)
    if not bad:
        return Result(True, "完整性校验通过（%d/%d）" % (ok, total), version=man.version)

    # 三类分开看：缺失/损坏要修；「大小相同但内容不同」通常是补丁/汉化，不算异常
    miss, dmg, mod = [], [], []
    for e, why in bad:
        p = e.at(root)
        if not os.path.isfile(p):
            miss.append((e, why))
        elif e.size and os.path.getsize(p) != e.size:
            dmg.append((e, why))
        else:
            mod.append((e, why))

    if mod:
        log("[校验] 与官方不同但大小一致（通常是补丁/汉化，不视为异常）：")
        for e, why in mod:
            log("[校验]     %s%s" % (e.rel,
                                     "  ← 游戏主程序"
                                     if e.rel.replace(os.sep, "/") == man.main_exe else ""))
    bad_n = len(miss) + len(dmg)
    if bad_n:
        return Result(False, "完整性校验：%d/%d 通过（缺失 %d，损坏 %d，与官方不同 %d）"
                      % (ok, total, len(miss), len(dmg), len(mod)), version=man.version)
    return Result(True, "完整性校验通过：%d/%d 一致，另有 %d 个文件与官方不同（已保留）"
                  % (ok, total, len(mod)), version=man.version)


def update(root=None, session=None, on_event=None, cancel=None,
           workers=None, resume=None, backup=None, dry_run=False,
           on_progress=None, force=False, manifest_url=None,
           restore_modified=None, install=False):
    """完整流程：检查 → 计划 → 执行。

    参数为 None 时从 config.json 的 update 段取默认值。

    force=True               忽略版本比较，直接用当前清单做一次全量比对（「修复」）。
    manifest_url=...         直接指定清单，跳过版本检查（排障 / 离线 / 自检用）。
    restore_modified=True    同版本时也把「与官方不同」的文件恢复成官方原版。
    install=True             全新安装（方案 v1 §4-G4）：install_root 允许不存在
                             （自动创建）；本地版本按 0 处理 → 计划=全量 20 文件。
                             资源层（cznlive）不由本流程处理 —— 首跑由游戏引擎自建。

    关于「与官方不同」的文件：
      · 大更新（版本号变了）→ 一律按官方清单替换，包括被补丁/汉化改过的游戏主程序；
        日志会逐个点名，并提示更新后需要重新补丁与汉化。
      · 同版本（只是校验/修复）→ 默认**保留不动**：那些文件通常就是补丁与汉化，
        不能当成损坏去「修」，否则会误杀。
      · 热更（bin\\appdata\\cznlive）由游戏自己管，两种情况下都不碰，因此热补丁后无需重新补丁。
    """
    log = on_event or (lambda m: None)
    root = _root(root)
    if install and root:
        try:
            os.makedirs(root, exist_ok=True)
        except OSError as exc:
            return Result(False, "无法创建安装目录 %s：%s" % (root, exc))
    if not root or not os.path.isdir(root):
        return Result(False, "install_root 未配置或不存在")

    if workers is None:
        workers = max(1, min(32, _u_int("workers", 4)))
    if resume is None:
        resume = _u_bool("resume", True)
    if backup is None:
        backup = _u_bool("backup_before_replace", True)
    if restore_modified is None:
        restore_modified = _u_bool("restore_modified", False)

    if manifest_url:
        man_url, source = manifest_url, "指定清单"
        local_ver = local_version(root)[0]
        info = {"local": local_ver, "source": source}
    else:
        info = check(root, session, on_event=log)
        if info.get("error"):
            return Result(False, info["error"])
        man_url = info.get("manifest_url")
        if not man_url:
            return Result(False, "没有可用的清单地址")
        local_ver = info["local"]
        if not force and not info["need_update"]:
            if info.get("determined"):
                log("[更新] 已是最新版本 %d" % local_ver)
                return Result(True, "已是最新版本 %d" % local_ver, version=local_ver)
            # 接口与探测都不可用 —— 不能谎报「已是最新」，明确说清
            why = info.get("probe_error") or "版本接口不可用"
            log("[更新] 无法确认最新版本：%s" % why)
            return Result(False, "无法确认最新版本（%s）" % why)

    mode = _u_str("verify_mode", "full").lower()
    if mode not in ("full", "quick"):
        log("[更新] verify_mode=%r 无效，按 full 处理" % mode)
        mode = "full"

    man, used = fetch_manifest_preferred(man_url, session)
    upgraded = bool(man.version and local_ver and man.version != local_ver)
    log("[更新] 目标版本 %d（%d 条记录，其中受管文件 %d 个）%s"
        % (man.version, len(man.entries), len(man.files),
           "  ← 大更新（本地 %d）" % local_ver if upgraded else ""))
    if man.main_exe:
        log("[更新] 游戏主程序：%s" % man.main_exe)

    plan = plan_update(root, man, source=info.get("source", ""), on_event=log, mode=mode,
                       replace_modified=upgraded or restore_modified, upgraded=upgraded)
    log("[更新] 计划：下载 %d（缺失 %d / 损坏 %d / 与官方不同 %d）/ 保留 %d / 删除 %d / 建目录 %d / 占位 %d（已就绪 %d）"
        % (len(plan.downloads), len(plan.missing), len(plan.damaged), len(plan.modified),
           len(plan.kept), len(plan.removes), len(plan.mkdirs),
           len(plan.generates), plan.intact))

    main_rel = man.main_exe

    def _tag(e):
        return "  ← 游戏主程序" if e.rel.replace(os.sep, "/") == main_rel else ""

    if plan.kept:
        log("[更新] 以下 %d 个文件与官方不同（通常是补丁/汉化），本次保持不动："
            % len(plan.kept))
        for e in plan.kept:
            log("[更新]     %s%s" % (e.rel, _tag(e)))
    if upgraded and plan.modified:
        log("[更新] ★ 大更新：以下文件含你的补丁/汉化，将被官方原版覆盖 ——"
            " 更新后请重新补丁与汉化：")
        for e in plan.modified:
            log("[更新]     %s%s" % (e.rel, _tag(e)))
    elif plan.modified and restore_modified:
        log("[更新] 已按配置把 %d 个与官方不同的文件恢复为官方原版：" % len(plan.modified))
        for e in plan.modified:
            log("[更新]     %s%s" % (e.rel, _tag(e)))

    if dry_run:
        return Result(True, "演练完成（未做任何改动）", version=man.version, plan=plan)

    if plan.is_empty():
        write_cache(root, man.files)
        write_upf(root, man.version, used)
        return Result(True, "已是目标版本 %d，无需改动" % man.version,
                      version=man.version, plan=plan)

    need = int(plan.total_packed * 2.5) + (256 << 20)
    free = disk_free(root)
    if free is not None and free < need:
        return Result(False, "磁盘空间不足：需约 %.1f GB，可用 %.1f GB"
                      % (need / 1073741824, free / 1073741824), plan=plan)

    # VC++ 运行时是游戏能否起来的前置条件（官方启动器会装，这里只提示）
    if _u_bool("check_vcredist", True):
        vc_ok, vc_note = check_vcredist()
        log("[更新] %s%s" % ("" if vc_ok else "★ ", vc_note))

    res = apply_plan(root, man, plan, used, session, workers=workers,
                     resume=resume, backup=backup, on_event=log,
                     cancel=cancel, on_progress=on_progress)

    # 成功后清掉临时目录（失败则保留，供下次断点续传）
    if res.ok and not _u_bool("keep_temp", False):
        shutil.rmtree(temp_dir(root), ignore_errors=True)

    # 官方行为铁证（2026-09-18 官方日志）：同版本下被改过的主程序也会被
    # 无条件重下覆盖。凡是我们把游戏主程序换回了官方原版，都如实提醒用户。
    if res.ok and main_rel and any(
            e.rel.replace(os.sep, "/") == main_rel for e in plan.downloads):
        log("[更新] ★ 游戏主程序 %s 已回到官方原版 —— 更新后请重新补丁与汉化"
            % main_rel)
    return res


# ====================================================================
# 命令行
# ====================================================================
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="CZN 游戏本体更新（DPMS）")
    ap.add_argument("--check", action="store_true", help="只检查版本，不做改动")
    ap.add_argument("--gamedata", action="store_true",
                    help="只识别/检测游戏资源层（cznlive，只读不下载）")
    ap.add_argument("--verify", action="store_true", help="全量完整性校验（只读）")
    ap.add_argument("--update", action="store_true", help="检查并执行更新")
    ap.add_argument("--install", action="store_true",
                    help="全新安装：向 install_root 完整安装游戏本体（资源层由游戏首跑自建）")
    ap.add_argument("--repair", action="store_true", help="按当前清单做一次全量比对修复")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不做改动")
    ap.add_argument("--workers", type=int, default=1, help="并发下载数（1-32，默认 1）")
    ap.add_argument("--root", help="指定安装根目录（默认取 config.json）")
    ap.add_argument("--selftest", action="store_true", help="离线自检（本地 HTTP，不联网）")
    a = ap.parse_args(argv)

    if a.selftest:
        return 0 if selftest() else 1

    root = _root(a.root)
    print("[*] 安装目录：%s" % (root or "(未配置)"))
    if a.gamedata:
        r = check_gamedata(root, on_event=print)
    elif a.verify:
        r = verify(root, on_event=print)
    elif a.install:
        r = update(root, on_event=print, workers=a.workers,
                   dry_run=a.dry_run, install=True)
    elif a.update or a.repair:
        r = update(root, on_event=print, workers=a.workers,
                   dry_run=a.dry_run, force=a.repair)
    else:
        info = check(root, on_event=print)
        if info.get("error"):
            print("[x] %s" % info["error"])
            return 1
        if info["need_update"]:
            verdict = "（需更新）"
        elif info.get("determined"):
            verdict = "（已最新）"
        else:
            # 接口与探测都不可用 —— 不能谎报「已最新」（探测结果带 unverified 语义）
            verdict = "（无法确认，%s）" % (info.get("probe_error") or "版本接口不可用")
        r = Result(True, "本地 %d → 最新 %d%s"
                   % (info["local"], info["live"], verdict))
        print()
        print("---- 游戏资源层（cznlive）----")
        check_gamedata(root, on_event=print)
    print("[%s] %s" % ("+" if r.ok else "x", r.message))
    for e, why in r.failed:
        print("    - %s：%s" % (e.rel, why))
    return 0 if r.ok else 1


# ====================================================================
# 离线自检（本地 HTTP 服务器 + 合成小文件，不联网、不下载大文件）
# ====================================================================
def _selftest_env(tmp):
    """造一个假的安装目录 + 假的 CDN，返回 (root, httpd, url)。"""
    import http.server
    import socketserver
    import threading

    root = os.path.join(tmp, "game")
    cdn = os.path.join(tmp, "cdn")
    os.makedirs(os.path.join(root, "bin"), exist_ok=True)
    os.makedirs(os.path.join(cdn, "v7"), exist_ok=True)

    # buildInfo.json（版本旁证对象，抓包实测 v54 有同名文件）
    with open(os.path.join(cdn, "v7", "buildInfo.json"), "w", encoding="utf-8") as f:
        json.dump({"game_id": cl.GAME_ID, "pcsdk_type": 3,
                   "pcsdk_version": "9.9.9"}, f)

    def pack(name, data):
        """把内容压成 gz 放进 CDN，返回 (orig_size, packed_size, md5, md5_packed)。"""
        gz = gzip.compress(data)
        with open(os.path.join(cdn, "v7", name), "wb") as f:
            f.write(gz)
        return (len(data), len(gz),
                hashlib.md5(data).hexdigest(), hashlib.md5(gz).hexdigest())

    # 三个受管文件：a 已就绪、b 需下载、c 本地被改坏
    A = b"alpha" * 100
    B = b"bravo" * 200
    C = b"charlie" * 300
    sa = pack("1.gz", A)
    sb = pack("2.gz", B)
    sc = pack("3.gz", C)

    with open(os.path.join(root, "bin", "a.dll"), "wb") as f:
        f.write(A)
    with open(os.path.join(root, "bin", "c.dll"), "wb") as f:
        f.write(b"corrupted")

    files = [
        "1 | D | /bin",
        "2 | F | /bin/a.dll | 7 | 1 | gz | %d | %d | %s | %s | a |  |" % sa,
        "3 | F | /bin/b.dll | 7 | 2 | gz | %d | %d | %s | %s | a |  |" % sb,
        "4 | F | /bin/c.dll | 7 | 3 | gz | %d | %d | %s | %s | a |  |" % sc,
        "5 | G | /bin/.keep",
        "6 | R | /bin/gone.dll",
    ]
    man = {"service_code": cl.GAME_ID, "service_name": "SelfTest",
           "version_no": 7, "root_folder": "SelfTest",
           "execution": "bin/ucldr_test_loader.exe bin/a.dll",
           "extract_size": str(sa[0] + sb[0] + sc[0]),
           "packed_size": str(sa[1] + sb[1] + sc[1]),
           "files": files}
    man_path = os.path.join(cdn, "manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(man, f)
    with open(man_path.replace("manifest", "manifest_v2"), "w", encoding="utf-8") as f:
        json.dump(man, f)                                   # v2：同样内容，验证兼容
    # 真实 DPMS 命名形态（<code>_<版本>.json / _v2.json），验证 v2 优先逻辑
    with open(os.path.join(cdn, "STOVE_CHAOSZERO_7.json"), "w", encoding="utf-8") as f:
        json.dump(man, f)
    with open(os.path.join(cdn, "STOVE_CHAOSZERO_7_v2.json"), "w", encoding="utf-8") as f:
        json.dump(man, f)

    # 另一份清单：声明的大小/哈希与实际投递的字节不符（模拟服务端内容被篡改）
    D = b"delta" * 50
    gz_good = gzip.compress(D)
    with open(os.path.join(cdn, "v7", "9.gz"), "wb") as f:
        f.write(b"Z" * len(gz_good))                        # 长度一致、内容错误
    bad = {"service_code": cl.GAME_ID, "service_name": "Tampered", "version_no": 7,
           "root_folder": "SelfTest", "execution": "bin/a.dll",
           "extract_size": str(len(D)), "packed_size": str(len(gz_good)),
           "files": ["1 | F | /bin/d.dll | 7 | 9 | gz | %d | %d | %s | %s | a |  |"
                     % (len(D), len(gz_good), hashlib.md5(D).hexdigest(),
                        hashlib.md5(gz_good).hexdigest())]}
    with open(os.path.join(cdn, "bad.json"), "w", encoding="utf-8") as f:
        json.dump(bad, f)

    # 资源层（cznlive）：造一份最小结构，验证「只识别、不触碰」
    cz = os.path.join(root, "bin", "appdata", "cznlive")
    os.makedirs(os.path.join(cz, "data.indices", "pcrevs"), exist_ok=True)
    os.makedirs(os.path.join(cz, "data.unpacked", "sound"), exist_ok=True)
    with open(os.path.join(cz, "data.pack"), "wb") as f:
        f.write(b"P" * 1024)
    with open(os.path.join(cz, "data.pack~1"), "wb") as f:
        f.write(b"P" * 512)
    with open(os.path.join(cz, "data.unpacked", "sound", "a.bank"), "wb") as f:
        f.write(b"B" * 256)
    for nm in ("_res_688_646441ae6a3df78e65f424a1de67f116.pcrevsz",
               "_text_ko_685_0b08e5bc9f16abb1f8c25343ad8e6b41.pcrevsz",
               "_bin_x86_64_688_4821b5d4dc63483a1a93b12c053c0b03.pcrevsz"):
        with open(os.path.join(cz, "data.indices", "pcrevs", nm), "wb") as f:
            f.write(gzip.compress(b"spdi" + b"\x00" * 52))
    # 各组资源索引（.pigz，末尾 @ver + u32 = 组级修订号 —— 引擎比对的值）
    for grp, ver in (("res", 688), ("media", 227), ("text", 688)):
        with open(os.path.join(cz, "data.indices", grp + ".pigz"), "wb") as f:
            f.write(gzip.compress(b"PLPcK-selftest" + b"\x00" * 24
                                  + b"@ver" + struct.pack("<I", ver)))

    os.makedirs(os.path.join(root, MANIFEST_DIR), exist_ok=True)
    with open(upf_path(root), "w", encoding="utf-8") as f:
        json.dump({"local_version": 6, "game_id": cl.GAME_ID,
                   "install_path": root, "files": [], "locale": "kr",
                   "type_code": 1, "grades": [],
                   "project_url": None}, f)                 # 稍后填真实 URL

    class H(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *a, **k):
            super().__init__(*a, directory=cdn, **k)

        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/blocked"):
                self.send_error(403, "Access Denined")      # 模拟 CDN 拦截
                return
            super().do_GET()

    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    # 填真实 URL（含 3 个受管文件各自的分组/序号已在 files 里）
    with open(upf_path(root), "w", encoding="utf-8") as f:
        json.dump({"local_version": 6, "game_id": cl.GAME_ID,
                   "install_path": root, "files": [], "locale": "kr",
                   "type_code": 1, "grades": [],
                   "project_url": base + "/manifest.json"}, f)

    # 生产 verinfo 形态的入口配置（2026-09-19 抓包原文结构）：全一致版 / res 可更新版
    def world_cfg(res_v, media_v, text_v):
        return {"_appid": "cznlive", "_entry_timestamp": "0",
                "world": {"asia": {"live": {
                    "build.version": "464",
                    "cdn.url": base + "/cdn/patch/1.0.46407/WLOP8Q5CZ9HW/",
                    "cdn.version.policies": "res,media,text",
                    "cdn.version_res.current": res_v,
                    "cdn.version_media.current": media_v,
                    "cdn.version_text.current": text_v,
                    "app.api": "wss://example.test:13701/api/"}}},
                "extra_world_guard": True}

    with open(os.path.join(cdn, "cznlive"), "w", encoding="utf-8") as f:
        json.dump(world_cfg(688, 227, 688), f)
    with open(os.path.join(cdn, "cznlive2"), "w", encoding="utf-8") as f:
        json.dump(world_cfg(689, 227, 688), f)
    return root, httpd, base, cdn


def selftest():
    """离线自检：全部走本地 HTTP 服务器 + 几百字节的合成文件。"""
    import tempfile
    ok = True

    def chk(name, cond, extra=""):
        nonlocal ok
        ok = ok and bool(cond)
        print("  %-4s %-30s %s" % ("PASS" if cond else "FAIL", name, extra))

    print("=" * 64)
    print("CZN 游戏本体更新 —— 离线自检（本地 HTTP，不联网）")
    print("=" * 64)

    with tempfile.TemporaryDirectory() as tmp:
        root, httpd, base, cdn = _selftest_env(tmp)
        try:
            # ① 清单解析
            man, used = fetch_manifest(base + "/manifest.json")
            chk("清单解析", man.version == 7 and len(man.files) == 3
                and len(man.dirs) == 1 and len(man.generates) == 1
                and len(man.removes) == 1,
                "v%d F%d D%d G%d R%d" % (man.version, len(man.files), len(man.dirs),
                                         len(man.generates), len(man.removes)))
            chk("大小合计吻合", man.total_size == int(man.raw["extract_size"])
                and man.total_packed == int(man.raw["packed_size"]))
            chk("主程序识别（取官方 execution 最后一个 exe）",
                man.main_exe == "bin/a.dll", man.main_exe)

            # ② URL 推导
            e = man.files[0]
            u = build_url(used, e)
            chk("下载 URL 推导", u.endswith("/v7/1.gz"), u)

            # ③ 计划：a 就绪、b 缺、c 坏
            plan = plan_update(root, man)
            chk("计划下载数", len(plan.downloads) == 2,
                [x.rel for x in plan.downloads])
            chk("计划建目录", len(plan.mkdirs) == 0, "bin 已存在")
            chk("计划占位", len(plan.generates) == 1, "/bin/.keep")
            chk("计划删除", len(plan.removes) == 0, "gone.dll 不存在")

            # ④ 演练不动文件
            before = md5_file(os.path.join(root, "bin", "c.dll"))
            r = update(root, manifest_url=used, dry_run=True, on_event=lambda m: None)
            chk("dry-run 不改动", md5_file(os.path.join(root, "bin", "c.dll")) == before
                and r.ok and r.plan is not None)

            # ⑤ 真正执行（只有 2 个小文件，几百字节）
            r = update(root, manifest_url=used, on_event=lambda m: None)
            chk("执行成功", r.ok and r.downloaded == 2, r.message)
            for name, data in (("a.dll", b"alpha" * 100), ("b.dll", b"bravo" * 200),
                               ("c.dll", b"charlie" * 300)):
                p = os.path.join(root, "bin", name)
                chk("落地 %s" % name, os.path.exists(p)
                    and md5_file(p) == hashlib.md5(data).hexdigest())
            chk("占位文件已建", os.path.exists(os.path.join(root, "bin", ".keep")))

            # ⑥ 账本与版本号
            cache = read_cache(root)
            chk("cacheii 已写", len(cache) == 3
                and cache.get("bin/a.dll", (0, ""))[0] == len(b"alpha" * 100))
            upf = read_upf(root)
            chk("版本号已回写", upf and upf.get("local_version") == 7,
                "local_version=%s" % (upf or {}).get("local_version"))
            ver, note = local_version(root)
            chk("本地版本读取", ver == 7, note)

            # ⑦ 幂等：再跑一次应该什么都不做
            r2 = update(root, manifest_url=used, on_event=lambda m: None)
            chk("二次运行无动作", r2.ok and r2.downloaded == 0, r2.message)

            # ⑧ 完整性校验
            rv = verify(root, manifest_url=used, on_event=lambda m: None)
            chk("完整性校验通过", rv.ok, rv.message)

            # ⑨ 破坏一个文件后修复
            with open(os.path.join(root, "bin", "a.dll"), "wb") as f:
                f.write(b"broken")
            rv2 = verify(root, manifest_url=used, on_event=lambda m: None)
            chk("能发现损坏", not rv2.ok, rv2.message)
            rr = update(root, manifest_url=used, force=True, on_event=lambda m: None)
            chk("修复成功", rr.ok and rr.downloaded == 1,
                "重下 %d 个" % rr.downloaded)

            # ⑩ 续传：手工把 gz 截断一半，看是否能续上
            e = [x for x in man.files if x.rel.endswith("b.dll")][0]
            gz = os.path.join(temp_dir(root), "%s_%s.gz" % (e.group, e.seq))
            os.makedirs(temp_dir(root), exist_ok=True)
            with open(os.path.join(root, "bin", "b.dll"), "wb") as f:
                f.write(b"x")
            if os.path.exists(gz):
                full = open(gz, "rb").read()
                with open(gz, "wb") as f:
                    f.write(full[:len(full) // 2])
            n = download_entry(root, e, used, resume=True)
            chk("断点续传", md5_file(os.path.join(root, "bin", "b.dll")) == e.md5,
                "%d 字节" % n)

            # ⑪ 服务端投递内容与清单不符，必须发现并拒绝落盘
            bman, bused = fetch_manifest(base + "/bad.json")
            try:
                download_entry(root, bman.files[0], bused)
                chk("篡改检测", False, "未报错")
            except Exception as exc:
                chk("篡改检测", "MD5" in str(exc), str(exc)[:44])
            chk("篡改内容未落盘", not os.path.exists(os.path.join(root, "bin", "d.dll")))

            # ⑫ 被占用文件应拒绝替换（Windows：以独占共享模式打开）
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                k32.CreateFileW.restype = wintypes.HANDLE
                p = os.path.join(root, "bin", "c.dll")
                ce = [x for x in man.files if x.rel.endswith("c.dll")][0]
                h = k32.CreateFileW(p, 0x80000000, 0, None, 3, 0, None)
                try:
                    chk("占用检测（独占句柄）", _locked(p) is True)
                    try:
                        download_entry(root, ce, used)
                        chk("占用时拒绝替换", False, "未报错")
                    except Exception as exc:
                        chk("占用时拒绝替换", "占用" in str(exc), str(exc)[:44])
                finally:
                    k32.CloseHandle(h)
                chk("释放后可写", _locked(p) is False)

            # ⑬ 列数自适应（13 列 vs 16 列）
            m16 = dict(man.raw)
            m16["files"] = [x + " |  | 2 | " if x.count("|") == 10 else x
                            for x in man.raw["files"]]
            chk("列数自适应", len(parse_manifest(m16).files) == 3)

            # ⑭ 探测兜底：404 = 版本不存在（正常边界）；403 = 被 CDN 拦，必须报错
            probed, perr = probe_latest_version(base + "/none_%d.json", 1, limit=3)
            chk("探测：404 到达边界", probed == 0 and perr == "", repr(perr))
            probed2, perr2 = probe_latest_version(base + "/blocked_%d.json", 1, limit=3)
            chk("探测：403 报错不谎报", probed2 == 0 and "403" in perr2, repr(perr2))

            # ⑮ 磁盘空间可读
            chk("磁盘空间可读", disk_free(root) is not None)

            # ⑯ 清单损坏要报错而不是静默
            try:
                parse_manifest({"files": ["坏行", "1 | X"]})
                chk("坏行跳过不崩", True, "仅跳过非法行")
            except Exception as exc:
                chk("坏行跳过不崩", False, str(exc))
            try:
                fetch_manifest(base + "/not-exist.json")
                chk("404 报错", False, "未报错")
            except Exception as exc:
                chk("404 报错", "404" in str(exc), str(exc)[:44])

            # ⑰ 补丁/汉化必须与「缺失/损坏」分开
            #    补丁 = 定长重建 ⇒ 大小不变、内容不同 ⇒ 同版本下保留，不能当损坏误杀
            PAT = b"patched-" + b"x" * (len(b"alpha" * 100) - 8)   # 等长、内容不同
            with open(os.path.join(root, "bin", "a.dll"), "wb") as f:
                f.write(PAT)                                        # 补丁
            os.remove(os.path.join(root, "bin", "b.dll"))           # 缺失
            with open(os.path.join(root, "bin", "c.dll"), "wb") as f:
                f.write(b"short")                                   # 大小不符 = 损坏
            p3 = plan_update(root, man, on_event=lambda m: None)
            chk("分类-与官方不符（全集）",
                sorted(x.rel for x in p3.modified)
                == sorted(["bin" + os.sep + "a.dll", "bin" + os.sep + "c.dll"]),
                [x.rel for x in p3.modified])
            chk("识别缺失", [x.rel for x in p3.missing] == ["bin" + os.sep + "b.dll"])
            chk("识别损坏（大小不符）",
                [x.rel for x in p3.damaged] == ["bin" + os.sep + "c.dll"])
            chk("同版本保留补丁",
                [x.rel for x in p3.kept] == ["bin" + os.sep + "a.dll"]
                and sorted(x.rel for x in p3.downloads)
                == sorted(["bin" + os.sep + "b.dll", "bin" + os.sep + "c.dll"]))
            rv3 = verify(root, manifest_url=used, on_event=lambda m: None)
            chk("校验：只缺/损才算异常", (not rv3.ok)
                and "缺失 1" in rv3.message and "损坏 1" in rv3.message
                and "与官方不同 1" in rv3.message, rv3.message)
            rp = update(root, manifest_url=used, force=True, on_event=lambda m: None)
            chk("修复只补缺失与损坏、不动补丁",
                rp.ok and rp.downloaded == 2
                and md5_file(os.path.join(root, "bin", "a.dll")) == hashlib.md5(PAT).hexdigest(),
                "重下 %d 个" % rp.downloaded)

            # ⑰b 大更新（版本变了）→ 一律替换，含补丁
            p4 = plan_update(root, man, replace_modified=True, upgraded=True)
            chk("大更新替换补丁",
                [x.rel for x in p4.downloads] == ["bin" + os.sep + "a.dll"]
                and not p4.kept and p4.upgraded)

            # ⑰c restore_modified=true → 同版本也恢复官方原版
            rq = update(root, manifest_url=used, force=True, restore_modified=True,
                        on_event=lambda m: None)
            chk("restore_modified 生效", rq.ok and rq.downloaded == 1
                and md5_file(os.path.join(root, "bin", "a.dll"))
                == hashlib.md5(b"alpha" * 100).hexdigest(),
                "重下 %d 个" % rq.downloaded)

            # ⑱ 并发下载（每个工作线程各持一个会话）
            for nm in ("a.dll", "b.dll"):
                with open(os.path.join(root, "bin", nm), "wb") as f:
                    f.write(b"x")
            rp2 = update(root, manifest_url=used, force=True, workers=3,
                         on_event=lambda m: None)
            chk("并发下载", rp2.ok and rp2.downloaded == 2,
                "重下 %d 个" % rp2.downloaded)

            # ⑲ quick 模式：只比账本，不读文件内容
            os.remove(os.path.join(root, "bin", "b.dll"))
            pq = plan_update(root, man, mode="quick")
            chk("quick 模式", len(pq.downloads) == 1
                and [x.rel for x in pq.missing] == ["bin" + os.sep + "b.dll"],
                "下载 %d 个" % len(pq.downloads))

            # ⑳ VC++ 运行时检测可用（只报告，不装东西）
            vc_ok, vc_note = check_vcredist()
            chk("VC++ 运行时检测", isinstance(vc_ok, bool), vc_note[:46])

            # ㉑ 成功后默认清理 .stove_temp（keep_temp=false）
            with open(os.path.join(root, "bin", "a.dll"), "wb") as f:
                f.write(b"x")
            update(root, manifest_url=used, force=True, on_event=lambda m: None)
            chk("默认清理临时目录", not os.path.exists(temp_dir(root)))

            # ㉓ 资源层识别：本地修订号从 pcrevs 文件名读出
            revs = read_gamedata_revisions(root)
            chk("资源层-本地修订",
                [(r.group, r.revision) for r in revs]
                == [("bin_x86_64", 688), ("res", 688), ("text_ko", 685)],
                [("%s=%d" % (r.group, r.revision)) for r in revs])
            s = gamedata_summary(root)
            chk("资源层-体量", s["exists"] and s["packs"] == 2
                and s["pack_bytes"] == 1536 and s["unpacked_bytes"] == 256,
                "packs=%d/%dB unpacked=%dB" % (s["packs"], s["pack_bytes"],
                                               s["unpacked_bytes"]))

            # ㉔ 资源层检测：入口配置可达 → 逐组比对（全一致）
            rg = check_gamedata(root, entry_url=base + "/cznlive",
                                on_event=lambda m: None)
            chk("资源层-逐组一致", rg.ok and "一致" in rg.message, rg.message)

            # ㉕ 资源层检测：远端有新修订 → 报可更新 + 按 cdn.context 模板给增量 URL
            logs5 = []
            rg2 = check_gamedata(root, entry_url=base + "/cznlive2",
                                 on_event=logs5.append)
            chk("资源层-检测到可更新", rg2.ok and "可更新" in rg2.message
                and "res 688→689" in rg2.message, rg2.message)
            chk("资源层-增量 URL 模板",
                any("689/689-688.tar.lz4" in m for m in logs5),
                [m for m in logs5 if "tar.lz4" in m][:1])

            # ㉕b 远端不可达必须如实说，不能假装成功
            rg3 = check_gamedata(root, entry_url=base + "/not-exist",
                                 on_event=lambda m: None)
            chk("资源层-远端失败如实报告", rg3.ok and "不可达" in rg3.message,
                rg3.message)

            # ㉖ 更新流程绝不触碰资源层（只读保证）
            def snap_appdata():
                out = {}
                for b, _d, fs in os.walk(os.path.join(root, "bin", "appdata")):
                    for f in fs:
                        p = os.path.join(b, f)
                        out[p] = (os.path.getsize(p), os.path.getmtime(p))
                return out
            before_app = snap_appdata()
            with open(os.path.join(root, "bin", "a.dll"), "wb") as f:
                f.write(b"x")
            update(root, manifest_url=used, force=True, on_event=lambda m: None)
            chk("更新不触碰资源层", before_app == snap_appdata(),
                "%d 个资源文件" % len(before_app))

            # ㉗b 全新安装模式：空目录 → 全量安装 + 账本（方案 v1 §4-G4）
            iroot = os.path.join(tmp, "fresh")
            os.makedirs(iroot, exist_ok=True)
            ri = update(iroot, manifest_url=used, install=True,
                        on_event=lambda m: None)
            chk("install 模式", ri.ok and ri.downloaded == 3
                and (read_upf(iroot) or {}).get("local_version") == 7, ri.message)
            chk("install 账本", len(read_cache(iroot)) == 3
                and os.path.isdir(os.path.join(iroot, "bin")))
            # 探测模板：无 .upf 时由 DPMS 命名约定构造（全新安装用，纯字符串）
            tpls = _probe_templates(None)
            chk("install 探测模板", len(tpls) == 2
                and all(cl.GAME_ID in t for t in tpls)
                and tpls[0].endswith("_%d_v2.json"), tpls[0] if tpls else "-")

            # ㉗ 配置项必须真的被读到（防止「声明了没接上」）
            keys = ("check_on_launch", "auto_download", "verify_mode", "workers",
                    "resume", "backup_before_replace", "restore_modified",
                    "keep_temp", "probe_fallback", "check_vcredist",
                    "buildinfo_check", "dpms_manifest_url_base",
                    "gameres_entry_url", "gameres_appid",
                    "gameres_ns", "gameres_build", "gameres_world",
                    "gameres_branch")
            wired = [k for k in keys if _u_bool(k, None) is not None
                     or _u_str(k, "") != "" or _u_int(k, -1) != -1]
            chk("配置段可读", len(wired) == len(keys), "%d/%d" % (len(wired), len(keys)))

            # ㉘ v2 清单优先：同一版本优先取 <code>_<N>_v2.json（官方 2026-09 起的形态）
            man2, used2 = fetch_manifest_preferred(
                base + "/STOVE_CHAOSZERO_7.json")
            chk("清单 v2 优先", used2.split("?")[0].endswith("STOVE_CHAOSZERO_7_v2.json")
                and man2.version == 7, used2.rsplit("/", 1)[-1])
            chk("v2 命名推导",
                manifest_variants("http://x/game/dpms_CODE_54.json")[0]
                == "http://x/game/dpms_CODE_54_v2.json",
                str(manifest_variants("http://x/game/dpms_CODE_54.json")))

            # ㉙ 探测通道自检：连「当前版本」都拿不到 ⇒ 通道不可信，如实报错
            probed3, perr3 = probe_latest_version([base + "/blocked_%d.json"], 3,
                                                  sanity_version=1)
            chk("探测：当前版本 403 ⇒ 通道不可信",
                probed3 == 0 and "不可信" in perr3, repr(perr3))

            # ㉚ ETag 预检：服务端对象与清单不符 ⇒ 丢弃本地缓存重下；相符 ⇒ 直接用缓存
            class _FakeResp:
                def __init__(self, code, headers):
                    self.status_code = code
                    self.headers = headers

            class _FakeSession:
                """head 返回固定 ETag；get 透传到真会话并计数。"""

                def __init__(self, etag, real):
                    self.etag = etag
                    self.real = real
                    self.heads = 0
                    self.gets = 0

                def head(self, url, timeout=30):
                    self.heads += 1
                    h = {} if self.etag is None else {"ETag": self.etag}
                    return _FakeResp(200, h)

                def get(self, *a, **k):
                    self.gets += 1
                    return self.real.get(*a, **k)

            e_b = [x for x in man2.files if x.rel.endswith("b.dll")][0]
            real_s = _session(None)
            with open(os.path.join(root, "bin", "b.dll"), "wb") as f:
                f.write(b"x")                                   # 弄坏目标文件
            gz_b = os.path.join(temp_dir(root), "%s_%s.gz" % (e_b.group, e_b.seq))
            os.makedirs(temp_dir(root), exist_ok=True)
            with open(os.path.join(cdn, "v7", "2.gz"), "rb") as f:
                good_gz = f.read()                              # 正确的完整包
            with open(gz_b, "wb") as f:
                f.write(good_gz)                                # 本地已有完整正确缓存
            fs_ok = _FakeSession(e_b.md5_packed, real_s)
            download_entry(root, e_b, used2, session=fs_ok)
            chk("ETag 相符 ⇒ 复用缓存不重下", fs_ok.heads == 1 and fs_ok.gets == 0,
                "heads=%d gets=%d" % (fs_ok.heads, fs_ok.gets))
            with open(os.path.join(root, "bin", "b.dll"), "wb") as f:
                f.write(b"x")
            fs_bad = _FakeSession("deadbeef" * 4, real_s)
            download_entry(root, e_b, used2, session=fs_bad)
            chk("ETag 不符 ⇒ 丢弃缓存重下", fs_bad.heads == 1 and fs_bad.gets == 1
                and md5_file(os.path.join(root, "bin", "b.dll")) == e_b.md5,
                "heads=%d gets=%d" % (fs_bad.heads, fs_bad.gets))
            fs_none = _FakeSession(None, real_s)                # 无 ETag ⇒ 照常
            with open(os.path.join(root, "bin", "b.dll"), "wb") as f:
                f.write(b"x")
            os.remove(gz_b)
            download_entry(root, e_b, used2, session=fs_none)
            chk("无 ETag ⇒ 照常下载", fs_none.gets == 1
                and md5_file(os.path.join(root, "bin", "b.dll")) == e_b.md5)

            # ㉛ buildInfo 旁证
            bi_ok, bi_note = check_buildinfo(used2, 7)
            chk("buildInfo 旁证通过", bi_ok, bi_note)
            bi_ok2, bi_note2 = check_buildinfo(used2, 8)
            chk("buildInfo 缺失如实报", (not bi_ok2) and "不可用" in bi_note2, bi_note2)

            # ㉜ 主程序被换回官方原版 ⇒ 明确提示重新补丁/汉化
            with open(os.path.join(root, "bin", "a.dll"), "wb") as f:
                f.write(b"x")                                   # 主程序 a.dll 被改坏
            msgs = []
            rh = update(root, manifest_url=used2, force=True, on_event=msgs.append)
            chk("主程序替换提示", rh.ok
                and any("重新补丁与汉化" in m for m in msgs)
                and any("已回到官方原版" in m for m in msgs),
                "日志 %d 条" % len(msgs))
        finally:
            httpd.shutdown()

    print()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(main())
