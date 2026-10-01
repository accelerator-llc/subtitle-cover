#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""X 探针公共库: 在 FastX 之上补充窗口创建/写图等诊断用最小绑定.

仅供 tools/ 下的诊断脚本使用(可复用); 使用独立的 X 连接,
不改动应用代码路径, 也不依赖 python-xlib(系统未安装).
"""

import ctypes
import os
import sys

# 项目根目录加入 sys.path, 以便复用 FastX
_PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT not in sys.path:
    sys.path.insert(0, _PROJECT)

from subtitle_cover.x11.fastx import FastX   # noqa: E402


class _XSetWindowAttributes(ctypes.Structure):
    """XSetWindowAttributes 对应结构(x64 布局, 与 X11/Xlib.h 一致).

    勘误: override_redirect 在 do_not_propagate_mask 之后、colormap
    之前(与 X 协议 value-list 位序一致); 曾误插在 backing_pixel 与
    save_under 之间导致赋值写错槽位、窗口被 mutter 当作受管窗口接管
    (截图普查阶段实证, 哨兵值全掩码探测定位)."""
    _fields_ = [
        ('background_pixmap', ctypes.c_ulong),
        ('background_pixel', ctypes.c_ulong),
        ('border_pixmap', ctypes.c_ulong),
        ('border_pixel', ctypes.c_ulong),
        ('bit_gravity', ctypes.c_int),
        ('win_gravity', ctypes.c_int),
        ('backing_store', ctypes.c_int),
        ('backing_planes', ctypes.c_ulong),
        ('backing_pixel', ctypes.c_ulong),
        ('save_under', ctypes.c_int),
        ('event_mask', ctypes.c_long),
        ('do_not_propagate_mask', ctypes.c_long),
        ('override_redirect', ctypes.c_int),
        ('colormap', ctypes.c_ulong),
        ('cursor', ctypes.c_ulong),
    ]


class _XVisualInfo(ctypes.Structure):
    """XVisualInfo 对应结构(探针查找 32 位视觉用)."""
    _fields_ = [
        ('visual', ctypes.c_void_p),       # Visual* 指针(XCreateWindow 用)
        ('visualid', ctypes.c_ulong),
        ('screen', ctypes.c_int),
        ('depth', ctypes.c_int),
        ('c_class', ctypes.c_int),
        ('red_mask', ctypes.c_ulong),
        ('green_mask', ctypes.c_ulong),
        ('blue_mask', ctypes.c_ulong),
        ('colormap_size', ctypes.c_int),
        ('bits_per_rgb', ctypes.c_int),
    ]


class XProbe:
    """诊断探针用 X 连接: 复用 FastX 抓取/写回, 补充窗口创建等绑定."""

    # 属性值掩码(仅探针用到的位), 与 X11 头文件一致.
    # 勘误: 曾误写为 1<<2 / 1<<8 —— 那是 CWBorderPixmap / CWBackingPixel,
    # 且 XSetWindowAttributes 结构体 override_redirect 字段错位, 两处
    # 叠加导致 override_redirect 从未生效, 装置窗口被 mutter 当作受管
    # 窗口接管(改挂 mutter-x11-frames 框架、位置被重排)。已按 Xlib.h
    # 修正: CWBackPixel=1<<1, CWOverrideRedirect=1<<9
    # (2025-10 截图普查阶段实证)
    CW_BACK_PIXEL = 1 << 1
    CW_OVERRIDE_REDIRECT = 1 << 9
    CW_EVENT_MASK = 1 << 11
    INPUT_OUTPUT = 1
    TRUE_COLOR = 4
    ZPIXMAP = 2

    def __init__(self, display_name=None):
        self.fx = FastX(display_name)
        lib = self.fx.lib
        self.lib = lib
        dpy = self.fx.dpy

        lib.XDefaultScreen.argtypes = [ctypes.c_void_p]
        lib.XDefaultScreen.restype = ctypes.c_int
        self.screen_no = lib.XDefaultScreen(dpy)

        # XCreateWindow: visual 参数为 Visual* 指针(NULL=CopyFromParent)
        lib.XCreateWindow.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong,
            ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint,
            ctypes.c_uint, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p,
            ctypes.c_ulong, ctypes.POINTER(_XSetWindowAttributes)]
        lib.XCreateWindow.restype = ctypes.c_ulong

        lib.XMapWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XDestroyWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]

        # XMatchVisualInfo: 查找指定深度的视觉(32 位 ARGB 窗口用)
        lib.XMatchVisualInfo.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(_XVisualInfo)]
        lib.XMatchVisualInfo.restype = ctypes.c_int

    # -------------------------------------------------- 基础信息
    def root_geometry(self):
        """根窗口 (w, h)."""
        g = self.fx.get_geometry(self.fx.root)
        return (g[2], g[3]) if g else (0, 0)

    def window_abs_rect(self, win):
        """窗口绝对矩形 (x, y, w, h); 窗口不存在返回 None."""
        geo = self.fx.get_geometry(win)
        org = self.fx.window_origin(win)
        if geo is None or org is None:
            return None
        return (org[0], org[1], geo[2], geo[3])

    # -------------------------------------------------- 窗口创建
    def match_visual(self, depth):
        """查找指定深度 TrueColor 视觉, 返回 Visual* 指针, 找不到返回 None."""
        info = _XVisualInfo()
        if not self.lib.XMatchVisualInfo(self.fx.dpy, self.screen_no,
                                         depth, self.TRUE_COLOR,
                                         ctypes.byref(info)):
            return None
        return info.visual

    def create_window(self, x, y, w, h, depth=24, visual_ptr=None):
        """创建 override-redirect 输入输出窗口(无事件掩码, 背景黑).

        visual_ptr: Visual* 指针; None 表示 CopyFromParent(默认深度)."""
        attrs = _XSetWindowAttributes()
        attrs.background_pixel = 0
        attrs.override_redirect = 1
        attrs.event_mask = 0
        mask = self.CW_BACK_PIXEL | self.CW_OVERRIDE_REDIRECT | \
            self.CW_EVENT_MASK
        win = self.lib.XCreateWindow(
            self.fx.dpy, self.fx.root, x, y, w, h,
            0, depth, self.INPUT_OUTPUT, visual_ptr,
            mask, ctypes.byref(attrs))
        return win

    def map_window(self, win):
        self.lib.XMapWindow(self.fx.dpy, win)

    def destroy_window(self, win):
        try:
            self.lib.XDestroyWindow(self.fx.dpy, win)
            self.lib.XSync(self.fx.dpy, 0)
        except Exception:
            pass

    # -------------------------------------------------- 写图
    def put_pil(self, win, img, depth):
        """把 PIL 图写进窗口(XPutImage, 显式深度; RGBA 输入)."""
        fx = self.fx
        if depth == 32:
            data = img.tobytes('raw', 'BGRA')
        else:
            data = img.convert('RGB').tobytes('raw', 'BGRX')
        w, h = img.size
        bpl = w * 4
        buf = ctypes.create_string_buffer(data, len(data))
        xi = fx._XImage()
        xi.width, xi.height = w, h
        xi.xoffset = 0
        xi.format = self.ZPIXMAP
        xi.data = ctypes.cast(buf, ctypes.c_void_p)
        xi.byte_order = fx.byte_order
        xi.bitmap_unit = 32
        xi.bitmap_bit_order = 0
        xi.bitmap_pad = fx.bitmap_pad
        xi.depth = depth
        xi.bytes_per_line = bpl
        xi.bits_per_pixel = 32
        xi.red_mask = fx.red_mask
        xi.green_mask = fx.green_mask
        xi.blue_mask = fx.blue_mask
        fx._ensure_gc(win)
        self.lib.XPutImage(fx.dpy, win, fx.gc, ctypes.byref(xi),
                           0, 0, 0, 0, w, h)
        self.lib.XFlush(fx.dpy)

    # -------------------------------------------------- 抓取
    def grab_pil(self, win, x, y, w, h):
        """抓取窗口区域为 PIL RGB(透明像素按其 RGB 通道处理)."""
        data, bpl, depth = self.fx.grab_window(win, x, y, w, h)
        if depth == 32:
            img = self.decode(data, w, h, bpl, 'RGBA', 'BGRA')
            if img is not None:
                img = img.convert('RGB')
            return img
        return self.decode(data, w, h, bpl, 'RGB', 'BGRX')

    @staticmethod
    def decode(data, w, h, bpl, mode, fmt):
        """ZPixmap 原始数据解 PIL 图(行距与像素宽不同时告知实际行距)."""
        from PIL import Image
        bpp = bpl // w if w else 0
        if bpp not in (3, 4):
            return None
        try:
            if bpl == w * bpp:
                return Image.frombytes(mode, (w, h), data, 'raw', fmt)
            return Image.frombytes(mode, (w, h), data, 'raw', fmt, bpl)
        except ValueError:
            return None
