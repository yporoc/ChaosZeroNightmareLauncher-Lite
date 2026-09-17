#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# CZN Launcher Lite —— 网络层自检（离线；不联网、不改任何系统设置）
# Copyright (C) 2026 CZN Launcher Lite contributors
# SPDX-License-Identifier: GPL-3.0-only
r"""把网络层「容易悄悄坏掉」的假设固化成断言。

用法：python tools/net_selftest.py      退出码 0=全过 / 1=有失败

全程离线：用本地 HTTP 服务器与本地 PAC；环境变量用完还原；不碰注册表。
用例 1/2 是重点 —— 「直连锁定」依赖 curl_cffi 内部行为，换版本可能失效，
一旦失效，README 承诺的「默认直连」会静默变成「走代理」。
"""
import ctypes
import http.server
import os
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import czn_lite as cl                                   # noqa: E402
from curl_cffi import CurlHttpVersion, requests as ccr   # noqa: E402

ENV_KEYS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")
DEAD = "http://127.0.0.1:9"                              # 必然连不上的代理
PAC = b'function FindProxyForURL(u, h) {\n' \
      b'  if (dnsDomainIs(h, ".onstove.com")) return "PROXY 127.0.0.1:7890";\n' \
      b'  return "DIRECT";\n}\n'

RESULTS = []


# ---------- 环境与本地服务器 ----------
def env_save():
    return {k: os.environ.get(k) for k in ENV_KEYS}


def env_restore(snap):
    for k, v in snap.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        pac = self.path.startswith("/proxy.pac")
        body = PAC if pac else b"ok"
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/x-ns-proxy-autoconfig" if pac else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def raw_session():
    s = ccr.Session(impersonate="chrome", default_headers=False,
                    http_version=CurlHttpVersion.V1_1)
    s.headers.clear()
    return s


# ---------- 用例 ----------
def c_direct_lock():
    """① 环境里有假代理时，显式 {"all": ""} 必须仍能直连。"""
    srv, port = serve()
    snap = env_save()
    try:
        os.environ["http_proxy"] = DEAD
        url = "http://127.0.0.1:%d/" % port
        route = cl.resolve_route(url)
        if route.proxies != {"all": ""}:
            return False, "路由未锁定直连：%r" % (route.proxies,)
        r = raw_session().get(url, timeout=8, proxies=route.proxies)
        return r.status_code == 200, "HTTP %d，proxies=%r" % (r.status_code,
                                                              route.proxies)
    finally:
        env_restore(snap)
        srv.shutdown()


def c_direct_lock_guard():
    """② 反面：同环境不传 proxies 必须失败，否则用例 ① 是假阳性。"""
    srv, port = serve()
    snap = env_save()
    try:
        os.environ["http_proxy"] = DEAD
        try:
            raw_session().get("http://127.0.0.1:%d/" % port, timeout=8)
        except Exception:
            return True, "不传 proxies 时确实被环境变量劫持并失败"
        return False, "不传 proxies 却成功了 —— 用例 ① 无法证明锁定生效"
    finally:
        env_restore(snap)
        srv.shutdown()


def c_route_direct():
    """③ direct 模式下路由必须是默认直连且来源可追溯。"""
    r = cl.resolve_route("https://s-api.onstove.com/sign/v2.1/pc/signin")
    return (r.proxies == {"all": ""} and r.source == "默认直连",
            "proxies=%r source=%s" % (r.proxies, r.source))


def c_system_proxy():
    """④ 系统代理配置可读取（无系统代理也算通过）。"""
    cfg = cl._system_proxy_static()
    if cfg is None:
        return True, "读不到系统代理（视为未启用）"
    proxy, _bypass, pac, autodetect = cfg
    return True, "proxy=%r pac=%r autodetect=%s" % (proxy or "(空)",
                                                    pac or "(无)", autodetect)


def c_pac():
    """⑤ PAC 求值：匹配域名返回代理、不匹配返回直连。"""
    srv, port = serve()
    try:
        pac = "http://127.0.0.1:%d/proxy.pac" % port
        ok1, hit, _n = cl._system_proxy_for_url("https://api.onstove.com/x", pac_url=pac)
        ok2, miss, _n = cl._system_proxy_for_url("https://example.com/", pac_url=pac)
        if not (ok1 and ok2):
            return False, "PAC 求值调用失败"
        return (hit == "127.0.0.1:7890" and miss is None,
                "onstove→%r  example→%r（None=直连）" % (hit, miss))
    finally:
        srv.shutdown()


def c_winhttp_handle():
    """⑥ WinHttpOpen 必须返回有效句柄（restype=HANDLE 声明正确）。"""
    lib = cl._winhttp()
    if not lib:
        return True, "winhttp 不可用，跳过"
    h = lib.WinHttpOpen("czn-selftest", 0, None, None, 0)
    if not h:
        return False, "WinHttpOpen 返回 0，err=%d" % ctypes.get_last_error()
    lib.WinHttpCloseHandle(h)
    return True, "句柄有效（0x%X）" % (h or 0)


def c_bypass():
    """⑦ 绕过表匹配：* 通配 / <local> / 网段前缀 / 精确。"""
    table = [("www.zhihu.com", "*zhihu.com;localhost", True),
             ("api.onstove.com", "*zhihu.com;127.*", False),
             ("localhost", "<local>", True),
             ("myhost", "<local>", True),
             ("a.b.local", "<local>", False),
             ("127.0.0.1", "127.*", True),
             ("10.1.2.3", "10.*", True),
             ("192.168.1.5", "192.168.1.5", True),
             ("", "127.*", False)]
    bad = [(h, b) for h, b, want in table if cl._bypass_match(h, b) != want]
    return not bad, ("%d 组全部符合预期" % len(table)) if not bad else ("不符：%s" % bad)


def _sample_event():
    token = "T" * 299
    cl.clear_events()
    cl.record("req", stage="自检", method="POST", url="https://s-api.onstove.com/x",
              host="s-api.onstove.com", proxy="直连",
              headers={"Authorization": "bearer " + token},
              body={"access_token": token, "refresh_token": token,
                    "member_no": "250460606", "user_id": "abc@outlook.com",
                    "guid": "20082355795", "nickname": "someone"})
    return token, cl.events()[0]


def c_export_masked():
    """⑧ 导出渲染必须脱敏：明文凭据一律不得出现。"""
    token, ev = _sample_event()
    text = "\n".join(cl.render_report(ev))
    leaks = [n for s, n in ((token, "token"), ("abc@outlook.com", "邮箱"),
                            ("250460606", "member_no"), ("20082355795", "guid"),
                            ("someone", "昵称")) if s in text]
    return not leaks, "已脱敏" if not leaks else "★ 泄漏：%s" % leaks


def c_ui_plain():
    """⑨ 反面：界面渲染必须保持明文。"""
    token, ev = _sample_event()
    return token in "\n".join(cl.render_ui(ev)), "界面渲染含明文 token"


def c_defaults():
    """⑩ 缺 network / log 段时全部键取到预期默认（向后兼容）。"""
    old = cl._CONFIG
    try:
        cl._CONFIG = {"game": {}}
        checks = [("mode", cl._cfg("network", "mode", default="direct"), "direct"),
                  ("use_pac", cl._cfg_bool("network", "system_use_pac", default=True), True),
                  ("verify_tls", cl._cfg_bool("network", "verify_tls", default=True), True),
                  ("read_timeout", cl._cfg_int("network", "read_timeout", default=20), 20),
                  ("ring_size", cl._cfg_int("log", "ring_size", default=5000), 5000),
                  ("reveal", cl._cfg_bool("log", "export_reveal_secrets", default=False), False),
                  ("tray", cl._cfg_bool("gui", "tray_enabled", default=False), False),
                  ("maxlines", cl._cfg_int("gui", "ui_max_log_lines", default=2000), 2000)]
        bad = [(k, got, want) for k, got, want in checks if got != want]
        return not bad, ("%d 项默认值正确" % len(checks)) if not bad else ("不符：%s" % bad)
    finally:
        cl._CONFIG = old


CASES = [("直连锁定（正面）", c_direct_lock),
         ("直连锁定（反面）", c_direct_lock_guard),
         ("direct 模式路由", c_route_direct),
         ("系统代理读取", c_system_proxy),
         ("PAC 求值", c_pac),
         ("WinHTTP 句柄", c_winhttp_handle),
         ("绕过表匹配", c_bypass),
         ("导出脱敏", c_export_masked),
         ("界面明文", c_ui_plain),
         ("配置默认值", c_defaults)]


def main():
    print("=" * 64)
    print("CZN Launcher Lite 网络层自检（离线；不改系统设置）")
    print("=" * 64)
    snap = env_save()
    try:
        for name, fn in CASES:
            try:
                ok, detail = fn()
            except Exception as e:
                ok, detail = False, "异常 %s: %s" % (type(e).__name__, e)
            RESULTS.append((name, ok))
            print("  %-4s %-22s %s" % ("PASS" if ok else "FAIL", name, detail))
    finally:
        env_restore(snap)
        cl.clear_events()
    failed = [n for n, ok in RESULTS if not ok]
    print()
    print("通过 %d / %d%s" % (len(RESULTS) - len(failed), len(RESULTS),
                             ("    失败：" + "、".join(failed)) if failed else ""))
    print("环境变量已还原：%s" % ("一致" if env_save() == snap else "★ 不一致"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
