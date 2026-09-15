#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""验证码处理层 —— 基于 2026-09-15 实抓的**真实协议**重写。

⚠️ 重要更正（必须先读）
=======================
本模块此前基于 `/blockchecker/v1.0/captcha/keys`（无 body）实现，
那返回的是 **240x80 四位数字图片验证码**，我还为它做了 OCR（90% 准确率）。
**但那次努力打错了目标** —— 官方登录根本不用那个。

实抓（2026-09-15，产物 `czn-mitm-login/out/login_capture.jsonl`）证明：
官方登录走 **`/blockchecker/v3.0/captcha/*` + `site_key`**，
验证码是**两步交互式**：先 `click`（点选形状）再 `rotate`（旋转对齐）。
**不是文字验证码，OCR 无从下手。**

真实协议（全部有实抓支撑）
--------------------------
```
① POST /blockchecker/v3.0/captcha/keys
     body: {"site_key": "4lmwALnopGieNmBSYs9jKsH5meeAbtSj"}
     resp: {"code":0,"value":{
              "captcha_key": "<64hex>",
              "captcha_type": "click" | "rotate",
              "resource": {"m_url": "...m.jpg", "p_url": "...p.png"},
              "steps": {"current_step": 1, "total_steps": 2}}}

② 用户交互（click：点选 m_url 场景里所有 p_url 形状；rotate：把内圈转到正）
   答案 base64 后放进 captcha_value：
     click  -> "x1,y1,x2,y2"   实抓 base64("206,28,40,141")
     rotate -> "<角度>"         实抓 base64("53") 成功 / base64("300") 答错

③ POST /blockchecker/v3.0/captcha/verify
     body: {"captcha_key": "...", "captcha_value": "<base64>"}
     resp: {"code":0,   "value":{"token":"<448hex>"}}            ← 成功
           {"code":49710,"message":"additional captcha is required"} ← 还有下一步
           {"code":49702,"message":"captcha is not correct"}         ← 答错

④ token 放进 **signin 的 `Captcha-Token` 请求头**
   （该通道已单独实测：不带 -> 49700；带非空 -> 49703；带空串 -> 49700）
```

可行性判断（实事求是）
----------------------
- **协议层：100% 闭环**，无未知项。
- **自动解题：不可行**。click 要在场景里找形状、rotate 要判断正确角度，
  都是真 CV 任务。且风控会连续换题（实抓里换了 3 次）。
- **出路三条**：
    A. 自建交互 UI：显示 m_url / p_url，用户点/转，编码后提交
    B. 复用官方 WebView2 页面（accounts.onstove.com/auth/captcha），截获 captchaValidated
    C. 兜底转发：从官方客户端日志取令牌（已实测可用）
"""

from __future__ import annotations

import base64
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

API = "https://api.onstove.com"

# ★ 实抓得到的 site key（对应 STOVE.exe 的 CAPTCHA_SITE_SIGN_IN_KEY）
SITE_KEY_SIGN_IN = "4lmwALnopGieNmBSYs9jKsH5meeAbtSj"

KEYS_URL = API + "/blockchecker/v3.0/captcha/keys"
VERIFY_URL = API + "/blockchecker/v3.0/captcha/verify"

# 服务端返回码（全部实抓确认）
CODE_OK = 0
CODE_CAPTCHA_REQUIRED = 49700      # signin 无 Captcha-Token 头
CODE_CAPTCHA_WRONG = 49702         # captcha_value 答错
CODE_CAPTCHA_INVALID = 49703       # signin 带了无效 token
CODE_CAPTCHA_MORE = 49710          # 还有下一步（多步验证码）
CODE_CAPTCHA_BAD_PARAM = 49314     # 参数结构不对（拿 v1.0 的 key 打 v3.0 会这样）
CODE_CAPTCHA_BAD_FORMAT = 49200    # 参数格式不对（body 传成了数组）


# ====================================================================
# 数据模型
# ====================================================================
@dataclass
class CaptchaStep:
    """一步验证码挑战。[✓ 实抓]"""
    key: str
    type: str                       # "click" | "rotate"
    m_url: str                      # 场景图 / 背景图（.jpg）
    p_url: str                      # 目标形状 / 内圈图（.png）
    current_step: int = 1
    total_steps: int = 1
    m_bytes: Optional[bytes] = None
    p_bytes: Optional[bytes] = None
    fetched_at: float = field(default_factory=time.time)

    def save_images(self, prefix: str = "captcha") -> tuple:
        m, p = Path(prefix + "_m.jpg"), Path(prefix + "_p.png")
        if self.m_bytes:
            m.write_bytes(self.m_bytes)
        if self.p_bytes:
            p.write_bytes(self.p_bytes)
        return m, p

    def __repr__(self):
        return "<CaptchaStep %s step=%d/%d key=%s…>" % (
            self.type, self.current_step, self.total_steps, self.key[:12])


# ====================================================================
# 答案编码 —— 实抓确认是 base64
# ====================================================================
def encode_click(points) -> str:
    """click：点击坐标 -> captcha_value。

    [✓ 实抓] base64("206,28,40,141") = "MjA2LDI4LDQwLDE0MQ=="
    两个点击点 (206,28) 与 (40,141)，逗号分隔扁平列表。
    """
    flat = []
    for x, y in points:
        flat += [int(x), int(y)]
    return base64.b64encode(",".join(str(v) for v in flat).encode()).decode()


def encode_rotate(angle) -> str:
    """rotate：旋转角度 -> captcha_value。

    [✓ 实抓] base64("53")="NTM=" 成功；base64("300")="MzAw" 得 49702 答错。
    """
    return base64.b64encode(str(int(angle)).encode()).decode()


def decode_value(v: str) -> str:
    """反向解码，便于调试。"""
    try:
        return base64.b64decode(v).decode("utf-8", "replace")
    except Exception:
        return "<非 base64>"


# ====================================================================
# 协议客户端 —— 复用 czn_lite 的会话与请求头
# ====================================================================
class BlockCheckerV3:
    """`/blockchecker/v3.0/captcha/*` 客户端。

    传入的 auth 只需有 `.s`（curl_cffi Session）与 `._official_headers()`。
    """

    def __init__(self, auth, site_key: str = SITE_KEY_SIGN_IN):
        self.auth = auth
        self.site_key = site_key

    def fetch(self, site_key: Optional[str] = None) -> CaptchaStep:
        """[✓ 实抓] body 只有 site_key 一个字段。"""
        body = {"site_key": site_key or self.site_key}
        r = self.auth.s.post(KEYS_URL, json=body,
                             headers=self.auth._official_headers(), timeout=20)
        data = r.json()
        v = data.get("value") or {}
        if not v.get("captcha_key"):
            raise RuntimeError("[captcha] keys 无 captcha_key: %s"
                               % json.dumps(data, ensure_ascii=False)[:200])
        res = v.get("resource") or {}
        steps = v.get("steps") or {}
        return CaptchaStep(
            key=v["captcha_key"],
            type=v.get("captcha_type") or "unknown",
            m_url=res.get("m_url") or "",
            p_url=res.get("p_url") or "",
            current_step=steps.get("current_step", 1),
            total_steps=steps.get("total_steps", 1),
        )

    def download(self, st: CaptchaStep) -> CaptchaStep:
        """下载两张图，供 UI 显示或后续 CV。"""
        for attr, url in (("m_bytes", st.m_url), ("p_bytes", st.p_url)):
            if not url:
                continue
            try:
                rr = self.auth.s.get(url, timeout=20,
                                     headers=self.auth._official_headers())
                if rr.status_code == 200 and rr.content:
                    setattr(st, attr, rr.content)
            except Exception:
                pass
        return st

    def submit(self, st: CaptchaStep, value_b64: str) -> dict:
        """[✓ 实抓] body = {captcha_key, captcha_value}，值是 base64。"""
        body = {"captcha_key": st.key, "captcha_value": value_b64}
        r = self.auth.s.post(VERIFY_URL, json=body,
                             headers=self.auth._official_headers(), timeout=20)
        return r.json()

    @staticmethod
    def extract_token(resp: dict) -> Optional[str]:
        """[✓ 实抓] 成功响应里字段名是 `token`（不是 captcha_token）。"""
        if resp.get("code") not in (0, None):
            return None
        v = resp.get("value") or {}
        return v.get("token") or v.get("captcha_token")


# ====================================================================
# 答案来源 —— 自动解不了，只能靠人（或未来接 CV）
# ====================================================================
class AnswerSource:
    """把 CaptchaStep 变成 captcha_value（base64）。

    官方这两种题型都不是 OCR 能解决的：
      click  —— 在场景图里找出所有目标形状的位置
      rotate —— 判断内圈该转多少度才对得齐
    默认实现是「交给上层 UI」，由用户点/转。
    """

    def answer(self, st: CaptchaStep) -> Optional[str]:
        raise NotImplementedError


class CallbackAnswer(AnswerSource):
    """把题交给上层（GUI / 控制台），拿回 base64 答案。

    callback(step, m_path, p_path) -> base64 字符串
    上层负责：显示两张图、收集用户操作、调用 encode_click / encode_rotate。
    """

    def __init__(self, callback: Callable):
        self.callback = callback

    def answer(self, st: CaptchaStep) -> Optional[str]:
        m = p = None
        if st.m_bytes or st.p_bytes:
            m, p = st.save_images()
        try:
            v = self.callback(st, m, p)
            return v or None
        finally:
            for f in (m, p):
                if f and f.exists():
                    try:
                        f.unlink()
                    except OSError:
                        pass


# ====================================================================
# 编排：多步验证码自动推进
# ====================================================================
class CaptchaFlow:
    """fetch -> 用户作答 -> submit -> （49710 则继续）-> token。

    实抓流程：click(1/2) -> 49710 -> rotate(2/2) -> token
    """

    MAX_STEPS = 5
    MAX_RETRY = 3

    def __init__(self, bc: BlockCheckerV3, source: AnswerSource,
                 on_event: Optional[Callable] = None):
        self.bc = bc
        self.source = source
        self._log = on_event or (lambda m: None)

    def run(self) -> Optional[str]:
        for attempt in range(1, self.MAX_RETRY + 1):
            token = self._one_pass()
            if token:
                return token
            self._log("[captcha] 第 %d 轮未通过，重来" % attempt)
        return None

    def _one_pass(self) -> Optional[str]:
        for _ in range(self.MAX_STEPS):
            st = self.bc.download(self.bc.fetch())
            self._log("[captcha] 题型=%s  第 %d/%d 步"
                      % (st.type, st.current_step, st.total_steps))
            val = self.source.answer(st)
            if not val:
                self._log("[captcha] 用户放弃")
                return None
            self._log("[captcha] 提交答案（解码后: %s）" % decode_value(val))
            resp = self.bc.submit(st, val)
            code = resp.get("code")
            if code == CODE_OK:
                tok = self.bc.extract_token(resp)
                if tok:
                    self._log("[captcha] ★ 拿到 token（%d 字符）" % len(tok))
                    return tok
                self._log("[captcha] code=0 但无 token: %s"
                          % json.dumps(resp, ensure_ascii=False)[:160])
                return None
            if code == CODE_CAPTCHA_MORE:
                self._log("[captcha] 还有下一步，继续")
                continue
            if code == CODE_CAPTCHA_WRONG:
                self._log("[captcha] 答错了，重新出题")
                return None
            self._log("[captcha] 未预期返回: %s"
                      % json.dumps(resp, ensure_ascii=False)[:160])
            return None
        return None


# ====================================================================
# 一站式入口 —— GUI / CLI 只需要调这一个
# ====================================================================
def solve_login_captcha(auth, ui_ask=None, on_event=None, site_key=None):
    """登录验证码一条龙：取题 -> 交 UI 作答 -> 提交 -> （多步则继续）-> token。

    参数
        auth     : StoveAuth 实例（提供 .s 与 ._official_headers()）
        ui_ask   : callable(step) -> base64 答案；None 则用内置窗口
        on_event : 日志回调
    返回
        token 字符串；用户取消或失败返回 None

    用法（GUI 里已按此接线）：
        tok = solve_login_captcha(auth, ui_ask=self._ask_captcha_sync)
        if tok:
            auth.signin_password(uid, pw, captcha_token=tok)
    """
    log = on_event or (lambda m: None)
    if ui_ask is None:
        try:
            from captcha_ui import ask_captcha      # 延迟导入：无 GUI 环境也能用本模块
        except Exception as e:
            log("[captcha] 无法加载内置窗口（%s），请自行提供 ui_ask" % e)
            return None
        ui_ask = ask_captcha
    bc = BlockCheckerV3(auth, site_key or SITE_KEY_SIGN_IN)
    return CaptchaFlow(bc, _CallbackSource(ui_ask), on_event=log).run()


class _CallbackSource(AnswerSource):
    """把 ui_ask(step) -> base64 包成 AnswerSource。"""

    def __init__(self, fn):
        self.fn = fn

    def answer(self, st: CaptchaStep):
        try:
            return self.fn(st) or None
        except Exception:
            return None


# ====================================================================
# 兜底：转发官方客户端（已实测可用）
# ====================================================================
class OfficialClientForwarder:
    """从官方启动器日志取明文 REQUIRED_INFO。

    [✓ 实测] `%LOCALAPPDATA%\\STOVE\\Logs\\StoveLauncher\\*.log` 里
    `sendRequiredInfo ... decrypted value : {json}` 是完整 39 字段，
    access_token 384 字符（正是管道握手需要的游戏级令牌）。

    为什么默认开启
    --------------
    这是**唯一绕开验证码**的路线（click/rotate 都是 CV 任务，自动解不可行）。
    做成可关闭是因为它读的是官方客户端日志：
      · 日志是官方客户端在**它自己登录成功后**写下的
      · 所以这条路线要求"官方客户端先登录过一次"
    不想走这条路、只要纯账密的，可把 config 的 captcha.forward_to_official_client
    设为 false。
    """

    LOG_DIR = Path.home() / "AppData" / "Local" / "STOVE" / "Logs" / "StoveLauncher"
    CONFIG_PATH = ("captcha", "forward_to_official_client")

    def __init__(self, log_dir: Optional[Path] = None):
        self.log_dir = Path(log_dir) if log_dir else self.LOG_DIR

    @staticmethod
    def enabled() -> bool:
        """读 config.json 的开关；缺省 True（保持既有行为不变）。

        取不到配置就按 True —— 这是兜底路线，宁可多一条可用路径。
        """
        try:
            import czn_lite
            return bool(czn_lite._cfg(*OfficialClientForwarder.CONFIG_PATH,
                                      default=True))
        except Exception:
            return True

    def available(self) -> bool:
        if not self.enabled():
            return False
        return self.log_dir.exists() and any(self.log_dir.glob("*.log"))

    def harvest(self, max_logs: int = 30) -> Optional[dict]:
        """扫最近的日志找 REQUIRED_INFO。

        ⚠️ 扫描数量要放宽：实测踩过 —— 只扫最近 3 个会失败，
        因为抓包/浏览期间新生成的日志里没有 `decrypted value`（那次没启动游戏），
        真正含令牌的旧日志被挤出窗口。
        """
        if not self.available():
            return None
        logs = sorted(self.log_dir.glob("*.log"),
                      key=lambda p: p.stat().st_mtime, reverse=True)[:max_logs]
        for log in logs:
            try:
                text = log.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if "decrypted value" not in text:
                continue
            # 不要用 `decrypted value[^\n]*` —— [^\n]* 会吃掉整行，
            # 而 JSON 就在同一行，m.end() 会跳到行尾把 JSON 跳过（实测踩过）。
            for m in re.finditer(r"decrypted value", text):
                obj = _first_json_object(text[m.end(): m.end() + 60000])
                if obj and ("access_token" in obj or "accessToken" in obj):
                    return obj
        return None


def _first_json_object(s: str) -> Optional[dict]:
    """抠出第一个能解析的平衡 JSON 对象。"""
    start = s.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(s[start:i + 1])
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return None


# ====================================================================
# 自检 —— 对着实抓值验算编码
# ====================================================================
def selftest() -> bool:
    ok = True
    print("=== 答案编码自检（对着 2026-09-15 实抓值验算）===")
    c = encode_click([(206, 28), (40, 141)])
    print("  encode_click([(206,28),(40,141)]) = %s  %s"
          % (c, "✓" if c == "MjA2LDI4LDQwLDE0MQ==" else "✗"))
    ok &= (c == "MjA2LDI4LDQwLDE0MQ==")

    r = encode_rotate(53)
    print("  encode_rotate(53)  = %-8s %s" % (r, "✓" if r == "NTM=" else "✗"))
    ok &= (r == "NTM=")
    r2 = encode_rotate(300)
    print("  encode_rotate(300) = %-8s %s" % (r2, "✓" if r2 == "MzAw" else "✗"))
    ok &= (r2 == "MzAw")

    print()
    print("=== 解码验证 ===")
    for v in ("MjA2LDI4LDQwLDE0MQ==", "MzAw", "NTM="):
        print("  %-24s -> %s" % (v, decode_value(v)))

    print()
    print("=== 转发路线可用性 ===")
    f = OfficialClientForwarder()
    print("  日志目录存在:", f.available())
    if f.available():
        info = f.harvest()
        if info:
            print("  取到 REQUIRED_INFO: %d 字段, access_token %d 字符"
                  % (len(info), len(str(info.get("access_token", "")))))
        else:
            print("  取到 REQUIRED_INFO: 否")

    print()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
