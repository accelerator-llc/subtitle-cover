#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FastX: ctypes 直调 libX11 的 XGetImage/XPutImage (性能关键路径).

python-xlib 的协议层是纯 Python 序列化, 每帧 MB 级图像往返会
占用大量 CPU; 本类通过 libX11 的 C 实现完成抓取与写回.

线程模型: 本类的连接由渲染线程独占使用; 主线程(GLib)使用
python-xlib 连接, 两者不共享, 避免 libX11 非线程安全问题.
初始化失败时上层自动降级, 功能不受影响.
"""

import ctypes
import os


def _os_exit(code):
    os._exit(code)


class FastX:
    ZPIXMAP = 2
    ALLPLANES = 0xFFFFFFFF

    # X 错误回调类型: int (*)(Display*, XErrorEvent*)
    XErrorCallback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p,
                                      ctypes.c_void_p)
    # IO 错误回调: int (*)(Display*)
    XIOErrorHandler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)

    class _XImage(ctypes.Structure):
        # 对应 X11/Xlib.h 的 XImage(前 15 个字段, x64 布局)
        _fields_ = [
            ('width', ctypes.c_int), ('height', ctypes.c_int),
            ('xoffset', ctypes.c_int), ('format', ctypes.c_int),
            ('data', ctypes.c_void_p),
            ('byte_order', ctypes.c_int), ('bitmap_unit', ctypes.c_int),
            ('bitmap_bit_order', ctypes.c_int), ('bitmap_pad', ctypes.c_int),
            ('depth', ctypes.c_int), ('bytes_per_line', ctypes.c_int),
            ('bits_per_pixel', ctypes.c_int),
            ('red_mask', ctypes.c_ulong), ('green_mask', ctypes.c_ulong),
            ('blue_mask', ctypes.c_ulong),
        ]

    def __init__(self, display_name=None):
        lib = ctypes.CDLL('libX11.so.6')
        self.lib = lib
        cname = display_name.encode() if display_name else None
        lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        lib.XOpenDisplay.restype = ctypes.c_void_p
        self.dpy = lib.XOpenDisplay(cname)
        if not self.dpy:
            raise RuntimeError('XOpenDisplay 失败')
        lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        lib.XDefaultRootWindow.restype = ctypes.c_uint32
        self.root = lib.XDefaultRootWindow(self.dpy)
        lib.XDefaultDepth.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.XDefaultDepth.restype = ctypes.c_int
        self.depth = lib.XDefaultDepth(self.dpy, 0)
        lib.XGetImage.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.c_int, ctypes.c_int,
                                  ctypes.c_uint, ctypes.c_uint,
                                  ctypes.c_ulong, ctypes.c_int]
        lib.XGetImage.restype = ctypes.POINTER(self._XImage)
        lib.XPutImage.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.c_void_p,
                                  ctypes.POINTER(self._XImage),
                                  ctypes.c_int, ctypes.c_int,
                                  ctypes.c_int, ctypes.c_int,
                                  ctypes.c_uint, ctypes.c_uint]
        lib.XPutImage.restype = ctypes.c_int
        lib.XDestroyImage.argtypes = [ctypes.POINTER(self._XImage)]
        lib.XDestroyImage.restype = ctypes.c_int
        lib.XCreateGC.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                  ctypes.c_ulong, ctypes.c_void_p]
        lib.XCreateGC.restype = ctypes.c_void_p
        lib.XFreeGC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        lib.XFlush.argtypes = [ctypes.c_void_p]
        # X 错误处理器: 抓取请求(BadMatch/BadWindow 等)只计数不崩溃
        # (libX11 默认 handler 会直接终止进程, 对"窗口突然消失"
        #  这类正常竞争场景不可接受)
        self.error_count = 0

        def _xerr_handler(dpy, evt):
            self.error_count += 1
            return 0

        self._err_cb = self.XErrorCallback(_xerr_handler)
        lib.XSetErrorHandler.argtypes = [self._err_cb.__class__]
        lib.XSetErrorHandler.restype = ctypes.c_int
        lib.XSetErrorHandler(self._err_cb)
        # XIO 错误处理器: 连接致命错误时打印并退出 —— libX11 默认行为
        # 是直接 exit(0) 且无任何输出, 排查问题会一头雾水
        self.io_error = False

        def _xio_handler():
            self.io_error = True
            import sys as _sys
            print('X 连接发生致命 IO 错误, 程序退出. '
                  '(如反复出现请到项目 issue 反馈)', file=_sys.stderr)
            _os_exit(1)

        self._io_cb = self.XIOErrorHandler(_xio_handler)
        lib.XSetIOErrorHandler.argtypes = [self._io_cb.__class__]
        lib.XSetIOErrorHandler.restype = ctypes.c_int
        lib.XSetIOErrorHandler(self._io_cb)
        # 服务器像素参数(首次抓取后从返回的 XImage 中学习)
        self.byte_order = 0        # LSBFirst
        self.bitmap_pad = 32
        self.red_mask = 0xFF0000
        self.green_mask = 0xFF00
        self.blue_mask = 0xFF
        self._params_known = False
        self.gc = None
        self.gc_win = 0

    def _learn_params(self, xi):
        """从 XGetImage 返回的 XImage 中提取服务器端像素布局参数."""
        if not self._params_known:
            self.byte_order = xi.byte_order
            self.bitmap_pad = xi.bitmap_pad
            self.red_mask = xi.red_mask
            self.green_mask = xi.green_mask
            self.blue_mask = xi.blue_mask
            self._params_known = True

    def _ensure_gc(self, win):
        if self.gc is None or self.gc_win != win:
            if self.gc:
                self.lib.XFreeGC(self.dpy, self.gc)
            self.gc = self.lib.XCreateGC(self.dpy, win, 0, None)
            self.gc_win = win

    def grab_window(self, drawable, x, y, w, h):
        """抓取任意 drawable 区域.

        返回 (像素数据, 每行字节数, drawable深度) 三元组;
        depth 32 表示 ARGB(含 alpha 通道).
        区域越界(BadMatch)或窗口已消失(BadWindow)时抛 RuntimeError,
        由调用方回退; X 错误已被 error handler 拦截, 不会杀进程."""
        xi = self.lib.XGetImage(self.dpy, drawable, x, y, w, h,
                                self.ALLPLANES, self.ZPIXMAP)
        if not xi:
            raise RuntimeError('XGetImage 失败')
        try:
            self._learn_params(xi.contents)
            bpl = xi.contents.bytes_per_line
            depth = xi.contents.depth
            buf = ctypes.string_at(xi.contents.data, bpl * h)
        finally:
            self.lib.XDestroyImage(xi)
        return buf, bpl, depth

    def grab(self, x, y, w, h):
        """抓取 root 区域, 返回 (像素数据, 每行字节数)."""
        return self.grab_window(self.root, x, y, w, h)

    def get_geometry(self, drawable):
        """XGetGeometry: 返回 (x, y, w, h) 或 None(窗口已销毁)."""
        lib = self.lib
        lib.XGetGeometry.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                     ctypes.POINTER(ctypes.c_uint32),
                                     ctypes.POINTER(ctypes.c_int),
                                     ctypes.POINTER(ctypes.c_int),
                                     ctypes.POINTER(ctypes.c_uint),
                                     ctypes.POINTER(ctypes.c_uint),
                                     ctypes.POINTER(ctypes.c_uint),
                                     ctypes.POINTER(ctypes.c_uint)]
        lib.XGetGeometry.restype = ctypes.c_int
        root_ret = ctypes.c_uint32()
        rx, ry = ctypes.c_int(), ctypes.c_int()
        rw, rh = ctypes.c_uint(), ctypes.c_uint()
        bw, depth = ctypes.c_uint(), ctypes.c_uint()
        if not lib.XGetGeometry(self.dpy, drawable,
                                ctypes.byref(root_ret), ctypes.byref(rx),
                                ctypes.byref(ry), ctypes.byref(rw),
                                ctypes.byref(rh), ctypes.byref(bw),
                                ctypes.byref(depth)):
            return None
        return rx.value, ry.value, rw.value, rh.value

    def move_resize_window(self, drawable, x, y, w, h):
        """XMoveResizeWindow. 与 put 在同一连接内, 顺序由 X 协议保证,
        因此"调整尺寸"与"写图"之间不会出现尺寸不一致的竞态."""
        lib = self.lib
        lib.XMoveResizeWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                          ctypes.c_int, ctypes.c_int,
                                          ctypes.c_uint, ctypes.c_uint]
        lib.XMoveResizeWindow.restype = ctypes.c_int
        lib.XMoveResizeWindow(self.dpy, drawable, x, y, w, h)
        lib.XFlush(self.dpy)

    def window_origin(self, drawable):
        """窗口左上角的绝对位置(root 坐标系), 窗口销毁返回 None."""
        lib = self.lib
        lib.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint32)]
        lib.XTranslateCoordinates.restype = ctypes.c_int
        dx, dy = ctypes.c_int(), ctypes.c_int()
        child = ctypes.c_uint32()
        if not lib.XTranslateCoordinates(self.dpy, drawable, self.root,
                                         0, 0, ctypes.byref(dx),
                                         ctypes.byref(dy),
                                         ctypes.byref(child)):
            return None
        return dx.value, dy.value

    def pointer_buttons(self):
        """返回当前鼠标按键掩码(渲染线程防交互卡死用)."""
        lib = self.lib
        lib.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
                                      ctypes.POINTER(ctypes.c_uint32),
                                      ctypes.POINTER(ctypes.c_uint32),
                                      ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_int),
                                      ctypes.POINTER(ctypes.c_uint)]
        lib.XQueryPointer.restype = ctypes.c_int
        r_ret = ctypes.c_uint32(); c_ret = ctypes.c_uint32()
        rx, ry = ctypes.c_int(), ctypes.c_int()
        wx, wy = ctypes.c_int(), ctypes.c_int()
        mask = ctypes.c_uint()
        ok = lib.XQueryPointer(self.dpy, self.root,
                               ctypes.byref(r_ret), ctypes.byref(c_ret),
                               ctypes.byref(rx), ctypes.byref(ry),
                               ctypes.byref(wx), ctypes.byref(wy),
                               ctypes.byref(mask))
        if not ok:
            return 0
        return mask.value

    def put(self, win, data, w, h, bpl):
        """把像素数据写回窗口(Xlib 内部自动处理请求分块)."""
        self._ensure_gc(win)
        buf = ctypes.create_string_buffer(data, len(data))
        xi = self._XImage()
        xi.width, xi.height = w, h
        xi.xoffset = 0
        xi.format = self.ZPIXMAP
        xi.data = ctypes.cast(buf, ctypes.c_void_p)
        xi.byte_order = self.byte_order
        xi.bitmap_unit = 32
        xi.bitmap_bit_order = 0
        xi.bitmap_pad = self.bitmap_pad
        xi.depth = self.depth
        xi.bytes_per_line = bpl
        xi.bits_per_pixel = (bpl // w) * 8
        xi.red_mask = self.red_mask
        xi.green_mask = self.green_mask
        xi.blue_mask = self.blue_mask
        r = self.lib.XPutImage(self.dpy, win, self.gc,
                               ctypes.byref(xi), 0, 0, 0, 0, w, h)
        self.lib.XFlush(self.dpy)
        return r
