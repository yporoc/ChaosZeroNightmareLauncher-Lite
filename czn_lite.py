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
  3. launch    组装 31 个环境变量，经 ucldr loader 拉起游戏进程
  4. keepalive 游戏运行期间由本进程内的管道服务保活

安全声明：仅供个人学习研究。仅支持二维码登录，不接触密码；
refresh_token 保存在本机 state.json 中，请妥善保管、切勿外传。
"""
import argparse
import base64
import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import time
import types
import uuid
import winreg
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
try:
    _CONFIG = json.loads(_CONFIG_FILE.read_text(encoding="utf-8")) \
        if _CONFIG_FILE.exists() else {}
    if not isinstance(_CONFIG, dict):
        _CONFIG = {}
except Exception as e:
    print("[!] config.json 读取失败（%s），使用内置默认值" % e)
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
# loader 相对路径与参数：文件名为游戏写死常量，仅目录随设备变化
LOADER_REL = _cfg("game", "loader_exe",
                  default=r"bin\ucldr_chaoszeronightmare_gl_loader_x64.exe")
LOADER_ARGS = _cfg("game", "loader_args", default=r"bin\ssr-stove-shield.exe")

# ---- 本机动态值（换设备会变，源码不给默认值） ----
INSTALL_ROOT = _cfg("game", "install_root", default="")
LOADER_EXE = os.path.join(INSTALL_ROOT, LOADER_REL) if INSTALL_ROOT else LOADER_REL

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
    安装路径与 loader 存在性；loader 未命中时在固定盘符的常见层级做
    有界探测（loader 文件名是游戏写死常量，跨设备可靠）。"""
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
    info["loader_found"] = os.path.exists(LOADER_EXE)
    if not info["loader_found"]:
        import glob
        for drive in ("C:", "D:", "E:", "F:"):
            patterns = [
                drive + r"\ChaosZeroNightmare\bin\ucldr_*.exe",
                drive + r"\Games\ChaosZeroNightmare\bin\ucldr_*.exe",
                drive + r"\Games\*\bin\ucldr_chaos*.exe",
                drive + r"\*\Games\ChaosZeroNightmare\bin\ucldr_*.exe",
            ]
            for pattern in patterns:
                hits = glob.glob(pattern)
                if hits:
                    detected = os.path.dirname(os.path.dirname(hits[0]))
                    info["install_root_detected"] = detected
                    break
            if "install_root_detected" in info:
                break
    return info


def set_install_root(root):
    """把本机安装路径写入 config.json 并即时生效（换设备自主适配）。
    只修改 game.install_root 一个键，其余配置保持不变。"""
    data = {}
    if _CONFIG_FILE.exists():
        try:
            data = json.loads(_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data.setdefault("game", {})["install_root"] = root
    _CONFIG_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    global INSTALL_ROOT, LOADER_EXE
    INSTALL_ROOT = root
    LOADER_EXE = os.path.join(root, LOADER_REL)


def clear_device_config():
    """「清空离线信息」的配置侧：只把本机动态键（install_root）置空回初始态，
    常量键一律不动。"""
    set_install_root("")


# ====================================================================
# 0. 账号密码登录用的密码字段变换
# --------------------------------------------------------------------
# [实测 2026-09-15] signin(provider_cd="SO") 的 provider_data.password
# 不是明文，是：AES-128-ECB(PINE key, PKCS7(utf8(pw))).hex().upper()
#   · 与实捕值 85DF63B28F9B4E7DCDD0339EB9AA67FF 逐字符一致
#   · 反向复算：解密该密文 -> PKCS7 自洽、8 字节、全可打印
#     （2026-09-15 复核：正向 stove_password_field("ep666666")
#       == 85DF63B28F9B4E7DCDD0339EB9AA67FF，双向自洽）
# 密钥来源：IdentityLib 3.2.28 运行时提取（静态搜索全失败，不存在于本地文件）
#   ⚠️ 版本绑定：IdentityLib 3.2.22 -> 3.2.28 时变换函数偏移已变过一次，
#      STOVE 更新后若登录异常，需重新提取（见 docs/FINDINGS.md）。
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
class StoveAuth:
    """STOVE 认证：二维码登录 → 令牌续期 → 游戏级令牌兑换。"""

    def __init__(self, gds=None):
        # curl_cffi 的 impersonate="chrome" 只为 TLS/JA3 指纹：STOVE 的 CDN 按
        # JA3 拦截（Python 原生指纹直接被重置），Chrome 指纹放行，必须保留。
        # HTTP 层与官方对齐：
        #   default_headers=False  关闭 libcurl 注入的 Chrome 浏览器头
        #   http_version=HTTP/1.1  官方所有端点均为 HTTP/1.1（Chrome 指纹默认 h2）
        #   会话头清空，每个请求经 _official_headers() 按官方线序完整构造
        self.s = requests.Session(impersonate="chrome", default_headers=False,
                                  http_version=CurlHttpVersion.V1_1)
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
        caller_detail = _caller_detail()
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
        return self._signin("QR", {"qr_login_session": qr_session})

    # ---- 账号密码登录（provider_cd="SO"）----
    # [反编译] "SO" 即 STOVE 账密分支（IdentityLib
    # SigninRequest::MakeService：SO 分支写 user_id+password）。
    # 请求体与 QR 同构，只换 provider_data。
    # 验证码通道是 **Captcha-Token 请求头**（[实测] 不带 -> 49700；
    # 带任意非空 -> 49703；带空串 -> 49700）。
    #   ⚠️ 注意证据边界：上面三条只能证明「服务端把这个头当验证码凭证」，
    #      并没有直接拍到一次"带着有效 token 且 code=0"的 signin
    #      （抓包时已是登录态，189 条记录里零个 /sign/）。
    #      所以「有效 token 放这里就能过」属 [推断]，见 docs/FINDINGS.md 2.3。
    # 验证码怎么解不归本模块管，由调用方通过 captcha_token 传入（见 captcha.py）。
    # 返回响应 dict 而**不抛异常**：49700 是需要验证码的正常中间态，不是错误。
    def signin_password(self, user_id, password, captcha_token=None):
        """账号密码登录。返回响应 dict；`code==49700` 表示需要验证码。"""
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
        headers = self._official_headers({
            "Authorization": "bearer " + str(token),
            "market-name": "PC_MARKET",
            "Captcha-Token": "",
        })
        print("[dbg][gc/check] Transaction-ID = device_key = %s（会话级同值）"
              % self.session_tid)
        r = self.s.post(API_BASE + "/gc/v1.4/check/" + GAME_ID, json=body,
                        headers=headers, timeout=20)
        try:
            return r.status_code, r.json(), r.text
        except Exception:
            return r.status_code, None, r.text

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
                win32pipe.DisconnectNamedPipe(handle)
                self.handshake_done.clear()  # 会话结束，GUI 按钮回落
            except Exception:
                pass
        try:
            win32file.CloseHandle(handle)
        except Exception:
            pass
        self._pipe_handle = None

    def stop(self):
        """停止服务：置位 running 并唤醒阻塞中的 ConnectNamedPipe。

        Windows 的同步 ConnectNamedPipe 无法被 DisconnectNamedPipe 取消
        （已实测：关闭窗口时这样调用会永久卡死），因此这里改用标准做法：
        起一个线程对自家管道做一次「连接后立即关闭」，让挂起的
        ConnectNamedPipe 返回，服务线程发现 running=False 后干净退出
        并释放管道名。"""
        self.running = False

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
    """通过 ucldr loader 拉起游戏。

    首选 ShellExecuteExW（与官方启动方式一致），但它在存在残留 loader
    或触发隐藏对话框时可能永久阻塞（调用线程没有消息泵），因此：
      · 放到临时线程执行，最多等待 wait_seconds 秒
      · 超时后检查 loader/游戏是否其实已经启动（避免双开）
      · 确认未启动则回退 CreateProcessW 直启（不会阻塞）
    失败时打印 Win32 错误码便于定位。"""
    for key, value in env.items():
        if value is not None:
            os.environ[key] = str(value)
    sei = _SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x40  # SEE_MASK_NOCLOSEPROCESS
    sei.lpVerb = "open"
    sei.lpFile = LOADER_EXE
    sei.lpParameters = LOADER_ARGS
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

    # 回退前先确认 loader / 游戏是否其实已经启动，避免双开
    listing = subprocess.run(["tasklist"], capture_output=True).stdout \
        .decode("utf-8", errors="replace")
    for probe in ("ucldr_chaos", "ssr-stove-shield"):
        if probe in listing:
            print("[+] 检测到 %s 已在运行，视为已拉起" % probe)
            return True

    try:
        process = subprocess.Popen([LOADER_EXE, LOADER_ARGS], cwd=INSTALL_ROOT)
        print("[launch] 游戏进程已创建（CreateProcessW，pid=%s）" % process.pid)
        return True
    except Exception as e:
        print("[x] 拉起失败：%s（file=%s args=%s dir=%s）"
              % (e, LOADER_EXE, LOADER_ARGS, INSTALL_ROOT))
        return False


def launch_game_capture_stdout(env, out_path=None):
    """诊断用：以 CreateProcessW 启动 loader 并重定向 stdout/stderr 到文件。
    注意 loader 会脱离重定向启动游戏，因此游戏自身日志可能仍不可见。"""
    for key, value in env.items():
        if value is not None:
            os.environ[key] = str(value)
    out_path = out_path or str(Path(__file__).with_name("game_stdout.log"))
    log = open(out_path, "wb")
    print("[launch] 诊断模式：stdout/stderr → %s" % out_path)
    process = subprocess.Popen([LOADER_EXE, LOADER_ARGS], cwd=INSTALL_ROOT,
                               stdout=log, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL)
    print("[launch] loader pid=%s" % process.pid)
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
    print("[+] ShellExecute 参数：file=%s params=%s dir=%s"
          % (LOADER_EXE, LOADER_ARGS, INSTALL_ROOT))
    print("[+] loader 存在：%s" % os.path.exists(LOADER_EXE))
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
        print("[+] 游戏已拉起（经 ucldr loader）")
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
