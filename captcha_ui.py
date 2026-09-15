#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""交互式验证码窗口 —— 方案 A 的 UI 部分。

素材真实结构（2026-09-15 用实抓样本验证）
------------------------------------------
  click :  m_url = 场景图（如 244x178），p_url = 目标形状小图（如 68x44，圆形/异形遮罩）
  rotate:  m_url = 背景图（如 178x178），**中心是一个白洞**
           p_url = 填洞的圆形碎片（如 130x130，四角透明 = 圆形遮罩）

  合成方式：把 p 按用户角度旋转后，**居中贴到 m 的白洞上**。
  实测验证：rotate 素材在 270° 时考拉头像与背景完全对齐 ✓

为什么必须自己做 UI
--------------------
两种题型都不是 OCR 能解决的（形状匹配 / 方向估计），只能把图给用户看、
把用户的操作编码成 captcha_value。

本模块只负责「显示 + 收集操作 -> base64」，协议部分在 captcha.py。
"""

from __future__ import annotations

import io
import math
import sys
import tkinter as tk
from typing import Optional

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from captcha import CaptchaStep, encode_click, encode_rotate

ctk.set_appearance_mode("dark")

C_BG = "#1a1a1c"
C_PANEL = "#202024"
C_STAGE = "#0b0b0c"
C_FG = "#e4e4e7"
C_DIM = "#8b8b93"
C_BTN = "#2b2b30"
C_BTN_HOV = "#36363c"
C_ACCENT = "#0a84ff"
C_OK = "#3fb950"

# 显示缩放：素材本身很小（178~244px），必须放大才看得清
MIN_SCALE = 1.6
MAX_SCALE = 3.0

# 右侧面板：目标形状图尽量占满面板横向宽度（用户 2026-09-15 两次反馈）
SIDE_W = 300          # 面板固定宽
SIDE_PAD = 16         # 文字标签的左右内边距
TARGET_PAD = 8        # ★ 目标图专用内边距 —— 要比文字小，好让图尽量占满
TARGET_MAX_H = 320    # 目标图高度上限（防止竖长图把面板撑爆）


class CaptchaWindow(ctk.CTkToplevel):
    """显示一步验证码，返回 base64 答案（或 None = 用户取消）。"""

    def __init__(self, master, step: CaptchaStep):
        super().__init__(master)
        self.step = step
        self.result: Optional[str] = None

        self._m_img = (Image.open(io.BytesIO(step.m_bytes)).convert("RGB")
                       if step.m_bytes else None)
        self._p_img = (Image.open(io.BytesIO(step.p_bytes)).convert("RGBA")
                       if step.p_bytes else None)
        if self._m_img is None:
            raise RuntimeError("缺少场景图 m_url，无法显示")

        self._points: list[tuple[int, int]] = []
        self._angle = 0
        self._tk_img = None
        self._scale = 2.0                 # 先给个默认，_build 后再按实际区域算
        self._drag_last = None            # rotate 拖拽用

        self.title("验证码 — %s（第 %d/%d 步）"
                   % (step.type, step.current_step, step.total_steps))
        self.geometry("1000x740")
        self.minsize(760, 560)
        self.configure(fg_color=C_BG)
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self._cancel)

        self._build()
        self._render()
        self.after(140, self._recalc_and_render)
        self.after(200, self.lift)

    # ================= UI =================
    def _build(self):
        f_tip = ctk.CTkFont("Microsoft YaHei UI", 14, "bold")
        f_lab = ctk.CTkFont("Microsoft YaHei UI", 12)
        f_big = ctk.CTkFont("Consolas", 22, "bold")

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 4))
        if self.step.type == "click":
            tip = ("在左侧场景图里，按从左向右的顺序依次点击所有与右侧「目标形状」"
                   "相同的图案（可点多个，但不要多选和漏选，请注意顺序；"
                   "点错了按「重来」）")
        elif self.step.type == "rotate":
            tip = ("拖动下方滑杆（或直接在图上手势旋转），把中间圆图转到与背景对齐；"
                   "对齐后点「提交」")
        else:
            tip = "未知验证码类型：%s" % self.step.type
        ctk.CTkLabel(head, text=tip, text_color=C_FG, font=f_tip,
                     wraplength=940, justify="left").grid(row=0, column=0, sticky="w")

        # ---- 舞台（图）----
        stage = ctk.CTkFrame(self, fg_color=C_STAGE, corner_radius=10)
        stage.grid(row=1, column=0, sticky="nsew", padx=(18, 8), pady=8)
        stage.grid_rowconfigure(0, weight=1)
        stage.grid_columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(stage, bg=C_STAGE, highlightthickness=0)
        self.canvas.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Configure>", lambda _e: self._recalc_and_render())

        # ---- 右侧面板 ----
        side = ctk.CTkFrame(self, fg_color=C_PANEL, corner_radius=10, width=SIDE_W)
        side.grid(row=1, column=1, sticky="nsew", padx=(0, 18), pady=8)
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        self.side = side

        ctk.CTkLabel(side, text="目标形状", text_color=C_DIM,
                     font=f_lab).grid(row=0, column=0, sticky="w",
                                      padx=SIDE_PAD, pady=(16, 4))
        # 目标图：宽度尽量占满面板（尺寸在 _render_target 里按实际宽度算）
        self.lbl_target = ctk.CTkLabel(side, text="", fg_color="transparent")
        self.lbl_target.grid(row=1, column=0, padx=TARGET_PAD, pady=(0, 10))
        # 面板尺寸定下来后重算一次；窗口缩放时也跟着重算
        side.bind("<Configure>", lambda _e: self._render_target())
        self.after(80, self._render_target)

        if self.step.type == "rotate":
            ctk.CTkLabel(side, text="角度", text_color=C_DIM,
                         font=f_lab).grid(row=2, column=0, sticky="w", padx=16)
            self.lbl_angle = ctk.CTkLabel(side, text="0°", text_color=C_OK, font=f_big)
            self.lbl_angle.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 2))
            self.slider = ctk.CTkSlider(side, from_=0, to=359, number_of_steps=359,
                                        command=self._on_slider)
            self.slider.set(0)
            self.slider.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 4))
            hint = ctk.CTkLabel(side, text="也可以直接在左侧图上按住拖动旋转",
                                text_color=C_DIM,
                                font=ctk.CTkFont("Microsoft YaHei UI", 11),
                                wraplength=250, justify="left")
            hint.grid(row=5, column=0, sticky="w", padx=16)

        self.lbl_state = ctk.CTkLabel(side, text="", text_color=C_DIM, font=f_lab,
                                      wraplength=250, justify="left")
        self.lbl_state.grid(row=6, column=0, sticky="w", padx=16, pady=(14, 0))

        # ---- 底部按钮 ----
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(4, 16))
        ctk.CTkButton(bar, text="重来", width=90, height=36, fg_color=C_BTN,
                      hover_color=C_BTN_HOV, command=self._reset).pack(side="left")
        ctk.CTkButton(bar, text="取消", width=90, height=36, fg_color=C_BTN,
                      hover_color=C_BTN_HOV, command=self._cancel).pack(side="right")
        ctk.CTkButton(bar, text="提交", width=130, height=36, fg_color=C_OK,
                      hover_color="#4fc463", font=ctk.CTkFont("Microsoft YaHei UI", 13, "bold"),
                      command=self._submit).pack(side="right", padx=(0, 10))

        self.grid_rowconfigure(1, weight=1)
        self.grid_columnconfigure(0, weight=1)

    # ================= 目标形状 =================
    def _render_target(self):
        """目标图渲染的**安全外壳**。

        ★ 2026-09-15 踩过的坑：这个方法一旦抛异常（当时把模块级常量写成了
        `self.TARGET_PAD`），`after()` 只会把 traceback 打到 stderr，
        **目标图就静默消失了**。所以这里必须兜住并**显式报错**，
        绝不能让面板变成空白。
        """
        if self._p_img is None or not hasattr(self, "lbl_target"):
            return
        try:
            self._render_target_impl()
        except Exception as e:
            print("[captcha_ui] 目标图渲染失败: %r" % (e,), file=sys.stderr)
            try:
                self.lbl_target.configure(image=None, text="（目标图渲染失败）",
                                          text_color="#ff6b6b")
            except Exception:
                pass

    def _render_target_impl(self):
        """把「目标形状」**裁掉透明边距**后按面板宽度铺满。

        ★ 实测到的坑：
          click  的 p_url 是 68x44，但**内容只有 53x18** —— 透明边距占高度 59%！
          rotate 的 p_url 是 130x130，无透明边距。
        所以光把白框做宽没用：click 的白框变宽了，**里面的图形还是小的**；
        rotate 因为没边距，看起来就正常变大了。

        修法：先按 alpha 通道 autocrop，再按面板宽度缩放。
        """
        # ① 裁掉透明边距（只在确实有边距时才裁，避免误伤）
        p = self._p_img
        if p.mode == "RGBA":
            bb = p.split()[-1].getbbox()
            if bb and (bb[2] - bb[0]) > 2 and (bb[3] - bb[1]) > 2:
                p = p.crop(bb)

        # ② 按面板实际宽度算尺寸
        #    ⚠️ TARGET_PAD / SIDE_W 是**模块级常量**，绝不能写成 self.xxx
        try:
            avail = self.side.winfo_width() - TARGET_PAD * 2
        except Exception:
            avail = 0
        if avail < 60:                       # 面板还没布局完，用标称值兜底
            avail = SIDE_W - TARGET_PAD * 2

        pw = avail
        ph = max(1, int(p.height * pw / p.width))
        if ph > TARGET_MAX_H:                # 太高就改成按高度约束
            ph = TARGET_MAX_H
            pw = max(1, int(p.width * ph / p.height))

        # ③ 白底渲染，和官方观感一致
        pv = p.resize((pw, ph), Image.LANCZOS)
        bg = Image.new("RGB", pv.size, (255, 255, 255))
        bg.paste(pv, (0, 0), pv)
        self._p_tk = ImageTk.PhotoImage(bg)
        self.lbl_target.configure(image=self._p_tk, text="")

    # ================= 尺寸 =================
    def _recalc_and_render(self):
        """按画布实际可用区域重算缩放（素材小，必须放大）。"""
        self.canvas.update_idletasks()
        cw = max(self.canvas.winfo_width(), 200)
        ch = max(self.canvas.winfo_height(), 200)
        if self._m_img is None:
            return
        s = min((cw - 20) / self._m_img.width, (ch - 20) / self._m_img.height)
        self._scale = max(MIN_SCALE, min(MAX_SCALE, s))
        self._render()

    # ================= 渲染 =================
    def _compose(self):
        """返回当前应显示的图（PIL）。"""
        img = self._m_img.copy()
        if self.step.type == "rotate" and self._p_img is not None:
            # ★ 把碎片按当前角度旋转后居中贴到 m 的白洞上
            #   （实测验证：这样在正确角度下与背景完全对齐）
            piece = self._p_img.rotate(-self._angle, resample=Image.BICUBIC,
                                       expand=False)
            px = (img.width - piece.width) // 2
            py = (img.height - piece.height) // 2
            img.paste(piece, (px, py), piece)
        return img

    def _render(self):
        if self._m_img is None:
            return
        img = self._compose()
        w = max(1, int(img.width * self._scale))
        h = max(1, int(img.height * self._scale))
        disp = img.resize((w, h), Image.LANCZOS)

        d = ImageDraw.Draw(disp)
        if self.step.type == "click":
            for i, (x, y) in enumerate(self._points, 1):
                cx, cy = x * self._scale, y * self._scale
                r = 12
                d.ellipse([cx - r, cy - r, cx + r, cy + r], outline="#ff3b30", width=3)
                d.text((cx + r + 3, cy - r - 2), str(i), fill="#ff3b30")

        self._tk_img = ImageTk.PhotoImage(disp)
        self.canvas.delete("all")
        self.canvas.create_image(self.canvas.winfo_width() // 2,
                                 self.canvas.winfo_height() // 2,
                                 anchor="center", image=self._tk_img)
        self._img_center = (self.canvas.winfo_width() // 2,
                            self.canvas.winfo_height() // 2)
        self._img_size = (w, h)
        self._update_state()

    def _update_state(self):
        if self.step.type == "click":
            self.lbl_state.configure(text="已点 %d 个\n（点错了按「重来」）"
                                          % len(self._points))
        else:
            self.lbl_state.configure(text="当前角度 %d°" % self._angle)

    # ================= 交互 =================
    def _to_image_xy(self, ev):
        """画布坐标 -> 原图坐标。"""
        cx, cy = getattr(self, "_img_center", (0, 0))
        w, h = getattr(self, "_img_size", (1, 1))
        ox = ev.x - (cx - w / 2)
        oy = ev.y - (cy - h / 2)
        x = int(round(ox / self._scale))
        y = int(round(oy / self._scale))
        if not (0 <= x < self._m_img.width and 0 <= y < self._m_img.height):
            return None
        return x, y

    def _on_press(self, ev):
        if self.step.type == "click":
            xy = self._to_image_xy(ev)
            if xy:
                self._points.append(xy)
                self._render()
        else:
            # rotate：按住拖动旋转
            self._drag_last = (ev.x, ev.y)

    def _on_drag(self, ev):
        if self.step.type != "rotate" or not self._drag_last:
            return
        lx, ly = self._drag_last
        cx, cy = getattr(self, "_img_center", (0, 0))
        a0 = math.degrees(math.atan2(ly - cy, lx - cx))
        a1 = math.degrees(math.atan2(ev.y - cy, ev.x - cx))
        self._angle = int((self._angle + (a1 - a0)) % 360)
        self._drag_last = (ev.x, ev.y)
        self.slider.set(self._angle)
        self.lbl_angle.configure(text="%d°" % self._angle)
        self._render()

    def _on_release(self, _ev):
        self._drag_last = None

    def _on_slider(self, v):
        self._angle = int(v) % 360
        self.lbl_angle.configure(text="%d°" % self._angle)
        self._render()

    def _reset(self):
        self._points.clear()
        self._angle = 0
        if hasattr(self, "slider"):
            self.slider.set(0)
            self.lbl_angle.configure(text="0°")
        self._render()

    def _submit(self):
        if self.step.type == "click":
            if not self._points:
                self.lbl_state.configure(text="至少点选一个位置")
                return
            self.result = encode_click(self._points)
        elif self.step.type == "rotate":
            self.result = encode_rotate(self._angle)
        else:
            self.result = None
        self._close()

    def _cancel(self):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.grab_release()
        except Exception:
            pass
        self.destroy()


class _Root(ctk.CTk):
    """独立测试用的隐藏主窗。"""

    def __init__(self):
        super().__init__()
        self.withdraw()


def ask_captcha(step: CaptchaStep, master=None) -> Optional[str]:
    """显示验证码并返回 base64 答案；用户取消返回 None。"""
    own = master is None
    root = master or _Root()
    win = CaptchaWindow(root, step)
    root.wait_window(win)
    if own:
        try:
            root.destroy()
        except Exception:
            pass
    return win.result
