#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""账密登录端到端 CLI —— 不启动 GUI，直接验证整条链。

流程
----
  1. 输入账号（邮箱）与密码（getpass，不回显、不打印、不落盘）
  2. cl.StoveAuth().signin_password(uid, pw)
  3. 若 code==49700 -> captcha.solve_login_captcha() 弹验证码窗口
  4. 拿 token -> signin_password(uid, pw, captcha_token=token) 重试
  5. 成功打印账号信息；失败按返回码说明原因

安全约定
--------
- 密码只进内存，**不打印、不落盘**。
- access_token / refresh_token 只打印长度，不打印值。

用法
----
    python tools/login_password.py        # 在仓库根目录执行

⚠️ 未实测确认的一环（诚实标注）
--------------------------------
「token 放进 `Captcha-Token` 请求头」是从**错误码行为**推出的：
  不带该头 -> 49700；带任意非空 -> 49703；带空串 -> 49700
⇒ 服务端确实把该头当验证码凭证。但**没有直接拍到**一次成功的带 token signin
  （用户抓包时已是登录态，未触发 signin）。若本次报 49703，把日志发我定位。
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import captcha as cap            # noqa: E402
import czn_lite as cl            # noqa: E402


def main() -> int:
    print("=" * 64)
    print("CZN 账号密码登录 —— 端到端验证")
    print("=" * 64)
    print("密码只进内存，不打印、不落盘。")
    print()

    uid = input("账号（STOVE 邮箱）: ").strip()
    if not uid:
        print("已取消。")
        return 1
    pw = getpass.getpass("密码（不回显）: ")
    if not pw:
        print("已取消。")
        return 1

    log = lambda m: print("   " + m)          # noqa: E731

    auth = cl.StoveAuth()
    print()
    print("[1] 直接 signin（provider_cd=SO）...")
    data = auth.signin_password(uid, pw)
    code = data.get("code")
    print("    code = %s | %s" % (code, data.get("message")))

    if code in (0, None):
        print("    ✓ 无需验证码，直接登录成功")
        return _report(auth)

    if code != 49700:
        print()
        print("    ✗ 不是验证码问题，登录未成功。")
        print("      code=49702 通常是账号或密码错误；其他情况把输出发我。")
        return 1

    print()
    print("[2] 需要验证码 —— 弹交互窗口")
    print("    （按提示点选 / 旋转，然后点「提交」）")
    token = cap.solve_login_captcha(auth, on_event=log)

    if not token:
        print()
        print("[3] 验证码未通过。")
        print("    可选兜底：转发官方客户端令牌")
        fwd = cap.OfficialClientForwarder()
        if fwd.available():
            info = fwd.harvest()
            if info:
                print("    ✓ 已从官方日志取到 REQUIRED_INFO：%d 字段，access_token %d 字符"
                      % (len(info), len(str(info.get("access_token", "")))))
                print("    ⇒ 可直接用于管道握手，无需账号密码。")
                return 0
        print("    ✗ 转发也不可用。")
        return 1

    print()
    print("[3] 拿到验证码 token（%d 字符），重试 signin ..." % len(token))
    data2 = auth.signin_password(uid, pw, captcha_token=token)
    code2 = data2.get("code")
    print("    code = %s | %s" % (code2, data2.get("message")))

    if code2 in (0, None):
        return _report(auth)

    print()
    print("    ✗ 带 token 仍失败。")
    if code2 == 49703:
        print("      code=49703 = 'captcha token is not valid'")
        print("      ⇒ token 未被接受。两种可能：")
        print("        a) token 要放到别处（不是 Captcha-Token 头）")
        print("        b) token 已过期/被消费，需重新取一张")
        print("      把本次完整输出发我，我据此定位。")
    elif code2 == 49700:
        print("      code=49700 = 服务端仍认为没带验证码 —— 头名可能不对")
    return 1


def _report(auth) -> int:
    print()
    print("=" * 64)
    print("✓ 登录成功")
    print("=" * 64)
    for k in ("member_no", "guid", "nickname", "expire_in"):
        v = getattr(auth, k, None)
        if v:
            print("   %-12s : %s" % (k, v))
    if auth.launcher_access:
        print("   access_token : <已获取，%d 字符>" % len(auth.launcher_access))
    if auth.launcher_refresh:
        print("   refresh_token: <已获取，%d 字符>" % len(auth.launcher_refresh))
    print()
    print("   下一步：auth.game_token() 兑换游戏级令牌 -> 管道握手 -> 拉起游戏")
    return 0


if __name__ == "__main__":
    sys.exit(main())
