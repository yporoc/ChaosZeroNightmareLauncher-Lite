#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""UI 常量引用静态检查 —— 防止 `self.<模块级常量>` 这类错误。

模块级常量写成 `self.XXX` 会在运行时报 AttributeError，
而 Tk 的 `after()` / `bind()` 回调会吞掉异常，导致控件静默不显示。
这类错误编译期查不出来，用 AST 静态检查拦住。

用法
----
    python tools/check_ui_consts.py [文件...]      # 默认查仓库根目录 *.py
    退出码 0 = 干净，1 = 发现问题
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def module_constants(tree: ast.Module) -> set:
    """模块级全大写赋值的名字（约定为常量）。"""
    out = set()
    for n in tree.body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    out.add(t.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if n.target.id.isupper():
                out.add(n.target.id)
    return out


def check_file(p: Path):
    src = p.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [(e.lineno or 0, f"语法错误: {e.msg}")]
    consts = module_constants(tree)
    bad = []
    for n in ast.walk(tree):
        if (isinstance(n, ast.Attribute)
                and isinstance(n.value, ast.Name)
                and n.value.id == "self"
                and n.attr in consts):
            bad.append((n.lineno, "self.%s —— 这是模块级常量，应直接写 %s"
                        % (n.attr, n.attr)))
    return bad


def main() -> int:
    args = sys.argv[1:]
    files = ([Path(a) for a in args] if args
             else sorted((ROOT).glob("*.py")))
    total = 0
    for p in files:
        bad = check_file(p)
        if bad:
            print("[FAIL] %s" % p)
            for ln, msg in bad:
                print("   L%-5d %s" % (ln, msg))
            total += len(bad)
        else:
            print("[OK]   %s" % p.name)
    print()
    if total:
        print("发现 %d 处问题。" % total)
        return 1
    print("干净。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
