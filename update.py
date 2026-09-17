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
import shutil
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
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
    missing: list = field(default_factory=list)      # 文件不存在
    modified: list = field(default_factory=list)     # 存在但与清单不符（被改过）
    intact: int = 0                                  # 已通过校验、无需动作的文件数
    source: str = ""                                 # 判定依据，进日志

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


def _get_bytes(url, session=None, timeout=30):
    s = _session(session)
    r = s.get(url, timeout=timeout)
    if getattr(r, "status_code", 0) != 200:
        raise RuntimeError("HTTP %s" % r.status_code)
    return r.content


def probe_latest_version(manifest_url_tpl, start, session=None, limit=PROBE_MAX_AHEAD):
    """免鉴权兜底：从 start 起递增探测清单，返回最新可用版本。

    官方游戏 CDN 对不存在的版本返回 404；启动器 CDN 返回 403 —— 两者都视为「不存在」。
    """
    best = 0
    for n in range(start, start + limit + 1):
        url = manifest_url_tpl % n
        try:
            _get_bytes(url, session, timeout=15)
            best = n
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 404):
                break
            break
        except Exception:
            break
    return best


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
def plan_update(root: str, manifest: Manifest, source="", on_event=None, mode="full"):
    """按清单生成本次要做的动作。

    mode='full'  逐文件算 MD5（等价官方 IIV_EXIST_HASH，默认）
    mode='quick' 只比 cacheii.db 的记录，不读文件内容（快，但发现不了文件被改）
    """
    log = on_event or (lambda m: None)
    cache = read_cache(root)
    plan = Plan(target_version=manifest.version, source=source)

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
        # 「缺失」与「被改过」必须分开 —— 后者意味着会覆盖用户的改动（如第三方补丁）
        if os.path.exists(e.at(root)):
            plan.modified.append(e)
            log("[计划] 已被修改：%s（%s）" % (e.rel, why))
        else:
            plan.missing.append(e)
            log("[计划] 缺失：%s" % e.rel)
        plan.downloads.append(e)

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
# 编排
# ====================================================================
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
    probe_url = None
    if url:
        probe_url = url.rsplit("/", 1)[0] + "/STOVE_CHAOSZERO_%d.json"

    live, source = 0, ""
    try:
        api = ("%s/dpms/game/v3.1/live_version?game_id=%s&local_version=%d&pc_room=false"
               % (cl.API, cl.GAME_ID, ver))
        data = json.loads(_get_bytes(api, session, timeout=20).decode("utf-8"))
        val = data.get("value") or {}
        live = int(val.get("live_version") or 0)
        url = str(val.get("live_project_file_url") or url)
        source = "DPMS API"
        log("[更新] 服务端最新版本 %d（%s）" % (live, source))
    except Exception as exc:
        log("[更新] 版本接口不可用（%s）" % exc)
        if allow_probe and probe_url:
            probed = probe_latest_version(probe_url, ver + 1, session)
            if probed:
                live = probed
                url = probe_url % probed
                source = "清单探测"
                log("[更新] 探测到最新版本 %d（%s）" % (live, source))

    return {"local": ver, "live": live, "need_update": bool(live and live > ver),
            "manifest_url": url, "probe_url": probe_url,
            "note": note, "source": source, "root": root}


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
    man, used = fetch_manifest(manifest_url, session)
    log("[校验] 清单 %s（版本 %d，%d 条）" % (used.split("?")[0], man.version, len(man.entries)))
    ok, bad = verify_all(root, man, on_event=log, cancel=cancel)
    total = len(man.files)
    if bad:
        miss = [b for b in bad if b[1] == "缺失"]
        mod = [b for b in bad if b[1] != "缺失"]
        return Result(False, "完整性校验：%d/%d 通过（缺失 %d，被修改 %d）"
                      % (ok, total, len(miss), len(mod)), version=man.version)
    return Result(True, "完整性校验通过（%d/%d）" % (ok, total), version=man.version)


def update(root=None, session=None, on_event=None, cancel=None,
           workers=None, resume=None, backup=None, dry_run=False,
           on_progress=None, force=False, manifest_url=None,
           preserve_modified=None):
    """完整流程：检查 → 计划 → 执行。

    参数为 None 时从 config.json 的 update 段取默认值。

    force=True              忽略版本比较，直接用当前清单做一次全量比对（「修复」）。
    manifest_url=...        直接指定清单，跳过版本检查（排障 / 离线 / 自检用）。
    preserve_modified=True  保留本地被改动过的文件（不覆盖），只在日志里报告。
    """
    log = on_event or (lambda m: None)
    root = _root(root)
    if not root or not os.path.isdir(root):
        return Result(False, "install_root 未配置或不存在")

    if workers is None:
        workers = max(1, min(32, _u_int("workers", 4)))
    if resume is None:
        resume = _u_bool("resume", True)
    if backup is None:
        backup = _u_bool("backup_before_replace", True)
    if preserve_modified is None:
        preserve_modified = _u_bool("preserve_modified", False)

    if manifest_url:
        man_url, source = manifest_url, "指定清单"
        info = {"local": local_version(root)[0], "source": source}
    else:
        info = check(root, session, on_event=log)
        if info.get("error"):
            return Result(False, info["error"])
        man_url = info.get("manifest_url")
        if not man_url:
            return Result(False, "没有可用的清单地址")
        if not force and not info["need_update"]:
            log("[更新] 已是最新版本 %d" % info["local"])
            return Result(True, "已是最新版本 %d" % info["local"], version=info["local"])

    mode = _u_str("verify_mode", "full").lower()
    if mode not in ("full", "quick"):
        log("[更新] verify_mode=%r 无效，按 full 处理" % mode)
        mode = "full"

    man, used = fetch_manifest(man_url, session)
    log("[更新] 目标版本 %d（%d 条记录，其中受管文件 %d 个）"
        % (man.version, len(man.entries), len(man.files)))

    plan = plan_update(root, man, source=info.get("source", ""), on_event=log, mode=mode)
    log("[更新] 计划：下载 %d（缺失 %d / 被修改 %d）/ 删除 %d / 建目录 %d / 占位 %d（已就绪 %d）"
        % (len(plan.downloads), len(plan.missing), len(plan.modified),
           len(plan.removes), len(plan.mkdirs), len(plan.generates), plan.intact))

    # 被修改过的文件（例如用户自己打过补丁）会被官方原版覆盖 —— 必须说清楚
    if plan.modified:
        log("[更新] 注意：以下 %d 个文件不是官方原版，更新将覆盖它们："
            % len(plan.modified))
        for e in plan.modified:
            log("[更新]     %s" % e.rel)
        if preserve_modified:
            keep = {e.idx for e in plan.modified}
            plan.downloads = [e for e in plan.downloads if e.idx not in keep]
            log("[更新] 已按配置保留上述文件（未替换）；其余照常处理")

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
    return res


# ====================================================================
# 命令行
# ====================================================================
def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="CZN 游戏本体更新（DPMS）")
    ap.add_argument("--check", action="store_true", help="只检查版本，不做改动")
    ap.add_argument("--verify", action="store_true", help="全量完整性校验（只读）")
    ap.add_argument("--update", action="store_true", help="检查并执行更新")
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
    if a.verify:
        r = verify(root, on_event=print)
    elif a.update or a.repair:
        r = update(root, on_event=print, workers=a.workers,
                   dry_run=a.dry_run, force=a.repair)
    else:
        info = check(root, on_event=print)
        if info.get("error"):
            print("[x] %s" % info["error"])
            return 1
        r = Result(True, "本地 %d → 最新 %d%s"
                   % (info["local"], info["live"],
                      "（需更新）" if info["need_update"] else "（已最新）"))
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
           "version_no": 7, "root_folder": "SelfTest", "execution": "bin/a.dll",
           "extract_size": str(sa[0] + sb[0] + sc[0]),
           "packed_size": str(sa[1] + sb[1] + sc[1]),
           "files": files}
    man_path = os.path.join(cdn, "manifest.json")
    with open(man_path, "w", encoding="utf-8") as f:
        json.dump(man, f)
    with open(man_path.replace("manifest", "manifest_v2"), "w", encoding="utf-8") as f:
        json.dump(man, f)                                   # v2：同样内容，验证兼容

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

    httpd = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % httpd.server_address[1]

    # 填真实 URL（含 3 个受管文件各自的分组/序号已在 files 里）
    with open(upf_path(root), "w", encoding="utf-8") as f:
        json.dump({"local_version": 6, "game_id": cl.GAME_ID,
                   "install_path": root, "files": [], "locale": "kr",
                   "type_code": 1, "grades": [],
                   "project_url": base + "/manifest.json"}, f)
    return root, httpd, base


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
        root, httpd, base = _selftest_env(tmp)
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

            # ⑭ 版本探测兜底（本地 CDN 没有这些名字，遇 404 应立刻停止）
            probed = probe_latest_version(base + "/none_%d.json", 1, limit=3)
            chk("探测兜底遇 404 停止", probed == 0, "返回 %d" % probed)

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

            # ⑰ 「缺失」与「被修改」必须分开（后者会覆盖用户改动）
            with open(os.path.join(root, "bin", "a.dll"), "wb") as f:
                f.write(b"user-patched")            # 存在但内容不同
            os.remove(os.path.join(root, "bin", "b.dll"))   # 直接删掉
            p3 = plan_update(root, man, on_event=lambda m: None)
            chk("分类-被修改", [x.rel for x in p3.modified] == ["bin" + os.sep + "a.dll"],
                [x.rel for x in p3.modified])
            chk("分类-缺失", [x.rel for x in p3.missing] == ["bin" + os.sep + "b.dll"],
                [x.rel for x in p3.missing])
            rv3 = verify(root, manifest_url=used, on_event=lambda m: None)
            chk("校验报告分类", (not rv3.ok) and "缺失 1" in rv3.message
                and "被修改 1" in rv3.message, rv3.message)
            rp = update(root, manifest_url=used, force=True,
                        preserve_modified=True, on_event=lambda m: None)
            chk("preserve_modified 生效", rp.ok and rp.downloaded == 1
                and md5_file(os.path.join(root, "bin", "a.dll"))
                == hashlib.md5(b"user-patched").hexdigest(),
                "只重下缺失的 %d 个" % rp.downloaded)
            rq = update(root, manifest_url=used, force=True, on_event=lambda m: None)
            chk("默认会覆盖被修改文件", rq.ok and rq.downloaded == 1
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

            # ㉒ 配置项必须真的被读到（防止「声明了没接上」）
            keys = ("check_on_launch", "auto_download", "verify_mode", "workers",
                    "resume", "backup_before_replace", "preserve_modified",
                    "keep_temp", "probe_fallback", "check_vcredist")
            wired = [k for k in keys if _u_bool(k, None) is not None
                     or _u_str(k, "") != "" or _u_int(k, -1) != -1]
            chk("配置段可读", len(wired) == len(keys), "%d/%d" % (len(wired), len(keys)))
        finally:
            httpd.shutdown()

    print()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(main())
