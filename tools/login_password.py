#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""账密登录端到端 CLI —— 不启动 GUI，直接验证整条链。

流程
----
  1. 输入账号（邮箱）与密码（getpass，不回显）
  2. signin_password(uid, pw)
  3. 若 code==49700 -> solve_login_captcha() 弹验证码窗口
  4. 拿 token -> signin_password(uid, pw, captcha_token=token) 重试

安全
----
密码只进内存，不打印、不落盘；token 只打印长度。

用法
----
    python tools/login_password.py        # 在仓库根目录执行
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
        print("    无需验证码，直接登录成功")
        return _report(auth)

    if code != 49700:
        print()
        print("    不是验证码问题，登录未成功。")
        print("    code=49702 通常是账号或密码错误；其他情况请反馈输出。")
        return 1

    print()
    print("[2] 需要验证码 —— 弹交互窗口")
    print("    （按提示点选 / 旋转，然后点「提交」）")
    token = cap.solve_login_captcha(auth, on_event=log)

    if not token:
        print()
        print("[3] 验证码未通过。")
        return 1

    print()
    print("[3] 拿到验证码 token（%d 字符），重试 signin ..." % len(token))
    data2 = auth.signin_password(uid, pw, captcha_token=token)
    code2 = data2.get("code")
    print("    code = %s | %s" % (code2, data2.get("message")))

    if code2 in (0, None):
        return _report(auth)

    print()
    print("    带 token 仍失败。")
    if code2 == 49703:
        print("      code=49703 = 'captcha token is not valid'")
        print("      => token 未被接受：可能头名不对，或已过期/被消费")
    elif code2 == 49700:
        print("      code=49700 = 服务端仍认为没带验证码 —— 头名可能不对")
    return 1


def _report(auth) -> int:
    print()
    print("=" * 64)
    print("登录成功")
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
