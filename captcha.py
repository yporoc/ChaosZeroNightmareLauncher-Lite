#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""验证码处理 —— STOVE 登录的 blockchecker v3.0 协议。

协议（2026-09-15 实抓）
----------------------
  ① POST /blockchecker/v3.0/captcha/keys
       body {"site_key": ...}
       resp {"value": {captcha_key, captcha_type, resource{m_url,p_url}, steps}}

  ② 用户作答（题型二选一）：
       click  —— 在 m_url 场景图里点选与 p_url 相同形状的位置，答案 "x1,y1,x2,y2"
       rotate —— 把 p_url 内圈转到与 m_url 背景对齐，答案 "<角度>"
     答案 base64 编码后作为 captcha_value。

  ③ POST /blockchecker/v3.0/captcha/verify
       body {"captcha_key": ..., "captcha_value": <base64>}
       resp {"code":0, "value":{"token": ...}}
            {"code":49710} 还有下一步；{"code":49702} 答错

  ④ token 放进 signin 的 `Captcha-Token` 请求头。

自动解题不可行：click 是形状检索、rotate 是方向估计，都是 CV 任务。
所以本模块只做协议，作答交给 UI（见 captcha_ui.py）。
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

API = "https://api.onstove.com"

SITE_KEY_SIGN_IN = "4lmwALnopGieNmBSYs9jKsH5meeAbtSj"

KEYS_URL = API + "/blockchecker/v3.0/captcha/keys"
VERIFY_URL = API + "/blockchecker/v3.0/captcha/verify"

# 服务端返回码
CODE_OK = 0
CODE_CAPTCHA_REQUIRED = 49700      # signin 未带 Captcha-Token
CODE_CAPTCHA_WRONG = 49702         # captcha_value 答错
CODE_CAPTCHA_INVALID = 49703       # signin 带了无效 token
CODE_CAPTCHA_MORE = 49710          # 还有下一步
CODE_CAPTCHA_BAD_PARAM = 49314     # 参数结构不对
CODE_CAPTCHA_BAD_FORMAT = 49200    # 参数格式不对


# ====================================================================
# 数据模型
# ====================================================================
@dataclass
class CaptchaStep:
    """一步验证码挑战。"""

    key: str
    type: str                       # "click" | "rotate"
    m_url: str                      # 场景图 / 背景图（.jpg）
    p_url: str                      # 目标形状 / 内圈图（.png）
    current_step: int = 1
    total_steps: int = 1
    m_bytes: Optional[bytes] = None
    p_bytes: Optional[bytes] = None
    fetched_at: float = field(default_factory=time.time)

    def __repr__(self):
        return "<CaptchaStep %s step=%d/%d key=%s…>" % (
            self.type, self.current_step, self.total_steps, self.key[:12])


# ====================================================================
# 答案编码 —— base64
# ====================================================================
def encode_click(points) -> str:
    """点击坐标 -> captcha_value。多个点按逗号扁平化。

    例：[(206,28), (40,141)] -> base64("206,28,40,141") = "MjA2LDI4LDQwLDE0MQ=="
    """
    flat = []
    for x, y in points:
        flat += [int(x), int(y)]
    return base64.b64encode(",".join(str(v) for v in flat).encode()).decode()


def encode_rotate(angle) -> str:
    """旋转角度 -> captcha_value。"""
    return base64.b64encode(str(int(angle)).encode()).decode()


def decode_value(v: str) -> str:
    """反向解码，调试用。"""
    try:
        return base64.b64decode(v).decode("utf-8", "replace")
    except Exception:
        return "<非 base64>"


# ====================================================================
# 协议客户端
# ====================================================================
class BlockCheckerV3:
    """`/blockchecker/v3.0/captcha/*` 客户端。

    传入的 auth 需提供 `.s`（curl_cffi Session）与 `._official_headers()`。
    """

    def __init__(self, auth, site_key: str = SITE_KEY_SIGN_IN):
        self.auth = auth
        self.site_key = site_key

    def fetch(self, site_key: Optional[str] = None) -> CaptchaStep:
        """取一道题。"""
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
        """下载题面两张图。"""
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
        """提交答案。"""
        body = {"captcha_key": st.key, "captcha_value": value_b64}
        r = self.auth.s.post(VERIFY_URL, json=body,
                             headers=self.auth._official_headers(), timeout=20)
        return r.json()

    @staticmethod
    def extract_token(resp: dict) -> Optional[str]:
        """成功响应里字段名是 `token`。"""
        if resp.get("code") not in (0, None):
            return None
        v = resp.get("value") or {}
        return v.get("token") or v.get("captcha_token")


# ====================================================================
# 编排：多步验证码推进
# ====================================================================
class CaptchaFlow:
    """fetch -> 用户作答 -> submit ->（49710 则继续）-> token。

    实抓流程：click(1/2) -> 49710 -> rotate(2/2) -> token
    """

    MAX_STEPS = 5
    MAX_RETRY = 3

    def __init__(self, bc: BlockCheckerV3, answer: Callable,
                 on_event: Optional[Callable] = None):
        self.bc = bc
        self.answer = answer
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
            try:
                val = self.answer(st)
            except Exception as e:
                self._log("[captcha] 作答异常: %r" % (e,))
                return None
            if not val:
                self._log("[captcha] 用户放弃")
                return None
            self._log("[captcha] 提交答案（解码后: %s）" % decode_value(val))
            resp = self.bc.submit(st, val)
            code = resp.get("code")
            if code == CODE_OK:
                tok = self.bc.extract_token(resp)
                if tok:
                    self._log("[captcha] 拿到 token（%d 字符）" % len(tok))
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
# 一站式入口
# ====================================================================
def solve_login_captcha(auth, ui_ask=None, on_event=None, site_key=None):
    """取题 -> 交 UI 作答 -> 提交 ->（多步则继续）-> token。

    auth     : StoveAuth 实例
    ui_ask   : callable(step) -> base64 答案；None 则用内置窗口
    on_event : 日志回调
    返回 token 字符串；取消或失败返回 None。
    """
    log = on_event or (lambda m: None)
    if ui_ask is None:
        try:
            from captcha_ui import ask_captcha      # 延迟导入：无 GUI 也能用本模块
        except Exception as e:
            log("[captcha] 无法加载内置窗口（%s），请自行提供 ui_ask" % e)
            return None
        ui_ask = ask_captcha
    bc = BlockCheckerV3(auth, site_key or SITE_KEY_SIGN_IN)
    return CaptchaFlow(bc, ui_ask, on_event=log).run()


# ====================================================================
# 自检
# ====================================================================
def selftest() -> bool:
    ok = True
    print("=== 答案编码自检 ===")
    c = encode_click([(206, 28), (40, 141)])
    print("  encode_click([(206,28),(40,141)]) = %s  %s"
          % (c, "OK" if c == "MjA2LDI4LDQwLDE0MQ==" else "FAIL"))
    ok &= (c == "MjA2LDI4LDQwLDE0MQ==")

    r = encode_rotate(53)
    print("  encode_rotate(53)  = %-8s %s" % (r, "OK" if r == "NTM=" else "FAIL"))
    ok &= (r == "NTM=")

    r2 = encode_rotate(300)
    print("  encode_rotate(300) = %-8s %s" % (r2, "OK" if r2 == "MzAw" else "FAIL"))
    ok &= (r2 == "MzAw")

    print()
    print("=== 解码验证 ===")
    for v in ("MjA2LDI4LDQwLDE0MQ==", "MzAw", "NTM="):
        print("  %-24s -> %s" % (v, decode_value(v)))

    print()
    print("SELFTEST", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    import sys
    sys.exit(0 if selftest() else 1)
