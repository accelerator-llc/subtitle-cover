#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CoverBar: X11 override-redirect 遮挡条窗口.

线程模型(单向数据流, 见 RenderSnapshot):
  * 主线程(GLib): 窗口管理/事件/下方窗口查找缓存(python-xlib 连接),
    并构造渲染快照(不可变)整体原子替换给本类
  * 渲染线程(本类自持, start/stop 控制): 每帧开头读一次快照,
    抓取-模糊-写回(仅使用 FastX 的 ctypes 连接)
python-xlib 连接非线程安全, 渲染路径中严禁出现 python-xlib 调用;
渲染线程不读活配置 dict / App 字段, 也不落盘.
"""

import os
import sys
import threading
import time
from typing import NamedTuple

from PIL import Image, ImageFilter

from Xlib import X, Xcursorfont
from Xlib.ext import shape as xshape
from Xlib.error import XError

from ..core.blur import BlurPipeline
from ..core.config import clamp, MIN_W, MIN_H


class RenderSnapshot(NamedTuple):
    """渲染快照(不可变): App 构造, update_snapshot 整引用原子替换,
    渲染线程每帧开头读一次(ARCH D3/D4); 单一写入点=App, 无需加锁."""

    x: int                      # 窗口位置(物理像素, root 坐标)
    y: int
    blur: int                   # 高斯模糊强度
    tint_color: tuple           # 色罩 RGB
    tint_alpha: float           # 色罩透明度 0~1
    noise: bool                 # 磨砂噪点
    feather: int                # 羽化边缘宽度
    hint_seconds: int           # 轮廓提示持续秒数
    fps: int                    # 渲染帧率上限
    visible: bool               # 遮挡条应显示
    screen_locked: bool         # 锁屏/挂起中(此时不渲染)
    peek_active: bool           # 按住透视中(此时暂停刷新)


class CoverBar:
    """遮挡条窗口本体.

    override-redirect 窗口不经过窗口管理器:
      * 位置尺寸完全由本程序控制, root 坐标即屏幕视觉坐标(已实测);
      * 天然位于窗口管理器管理的窗口(含全屏视频)之上;
      * 不进入 mutter 写回 root 的合成画面, 直抓下方窗口时
        不会把自身拍进去.
    线程与数据流见模块级 docstring(单向, ARCH D4).
    """

    # 渲染线程节奏: 连续静止 N 帧后把探测频率降到 2fps(视频暂停/
    # 看文档时接近零 CPU), 画面一有变化立即恢复满帧率
    IDLE_FRAMES_BEFORE_SLOWDOWN = 10
    IDLE_INTERVAL = 0.5          # 降频后的探测间隔(秒)

    def __init__(self, dpy, x, y, w, h, tint_color):
        """创建遮挡条窗口.

        只接收创建所需的显式参数(不接收配置 dict): 配置初始化属 App 的
        配置边界职责; 运行期配置经快照/显式 setter 下发 (ARCH D3)."""
        self.dpy = dpy
        self.screen = dpy.screen()
        self.root = self.screen.root
        self.depth = self.root.get_geometry().depth
        self.root_w = self.screen.width_in_pixels
        self.root_h = self.screen.height_in_pixels

        # 窗口背景色跟随色调颜色: resize 扩大的新区域在图像铺满前
        # 显示背景色, 若为黑色会与纯色模式来回闪跳(实测); 与纯色一致则无缝
        r, g, b = tint_color
        bg_pixel = (r << 16) | (g << 8) | b
        self.win = self.root.create_window(
            x, y, w, h,
            border_width=0, depth=self.depth,
            window_class=X.InputOutput, visual=X.CopyFromParent,
            override_redirect=True,          # 关键: 绕过窗口管理器
            background_pixel=bg_pixel,
            event_mask=(X.ButtonPressMask | X.ButtonReleaseMask |
                        X.PointerMotionMask | X.ExposureMask))
        self.gc = self.win.create_gc()

        # 光标: 平常左箭头, 拖动时移动十字 (前景黑/背景白, 16 位分量)
        font = dpy.open_font('cursor')
        fg = (0, 0, 0)
        bg = (0xffff, 0xffff, 0xffff)
        self.cursor_normal = font.create_glyph_cursor(
            font, Xcursorfont.left_ptr, Xcursorfont.left_ptr + 1, fg, bg)
        self.cursor_move = font.create_glyph_cursor(
            font, Xcursorfont.fleur, Xcursorfont.fleur + 1, fg, bg)
        # 光标: 调整大小 (水平/垂直/对角)
        self.cursor_resize = {
            'h': font.create_glyph_cursor(
                font, Xcursorfont.sb_h_double_arrow,
                Xcursorfont.sb_h_double_arrow + 1, fg, bg),
            'v': font.create_glyph_cursor(
                font, Xcursorfont.sb_v_double_arrow,
                Xcursorfont.sb_v_double_arrow + 1, fg, bg),
        }
        self.win.change_attributes(cursor=self.cursor_normal)

        # 记录上一帧原始抓取数据, 用于静止画面跳过渲染(省 CPU)
        self._last_raw = None

        # 上一帧最终输出缓存 (字节串, w, h, 快照引用): 静止画面跳帧后
        # 直接重放, 避免落到"root 陈旧快照回退"造成黑条/错位闪跳;
        # 快照按引用比较, 任何配置变化都会使缓存失效而强制重算
        self._last_out = None

        # ctypes 快路径(像素读写直调 libX11, 见 FastX 类说明)
        self.fastx = None
        try:
            from .fastx import FastX
            self.fastx = FastX(os.environ.get('DISPLAY'))
        except (OSError, RuntimeError) as e:
            print('FastX 不可用, 渲染功能不可用:', e, file=sys.stderr)

        # 渲染线程使用的窗口尺寸缓存(主线程 move_resize 时更新),
        # 避免渲染线程调用 python-xlib
        self._wh = (w, h)

        # 遮挡条正下方窗口缓存: (xid, ax, ay, ww, wh)
        # 由主线程周期刷新(python-xlib), 渲染线程只读
        self._below_cache = None

        # 当前位置缓存(主线程维护), 供主线程查找下方窗口时使用
        self._pos = (x, y)

        # 待应用的几何(主线程写入, 渲染线程在同一 fastx 连接内
        # 先 move/resize 再 put —— 同连接顺序由 X 协议保证,
        # 彻底消除 resize 时窗口尺寸与图像尺寸不一致的黑闪)
        self.pending_geo = None

        # 纯色模式: 任何几何变化(拖动/手柄/滚轮/面板/预设)都会刷新
        # solid_until —— 变化期间渲染纯色实底 + 常亮轮廓, 暂停实时模糊
        # 与屏幕抓取(避免"延迟跟随的模糊"的廉价感); 变化停止 0.8s 后
        # 自动恢复磨砂并触发轮廓渐隐提示.
        self.interactive = False       # 按住左键拖动/调整中(经方法置位)
        self.solid_until = 0.0         # 纯色模式的截止时间(单调钟)

        # 轮廓提示: 显示/恢复时在边缘画数秒蚂蚁线, 防止纯色背景下
        # 磨砂条与背景融为一体找不到. _hint_start=0 表示未激活.
        self._hint_start = 0.0
        self._hint_cache = None
        self._hint_cache_key = None

        # 笔记截图请求: 主线程经 request_shot 写入目标路径, 渲染线程在
        # 下一帧用当前抓取帧(清晰原图)保存, 避免额外的屏幕抓取
        self._shot_request = None

        # 渲染线程状态(本类自持线程, 主线程经 start/stop 控制):
        # _snap 为当前渲染快照, 由 App 经 update_snapshot 原子替换
        self._snap = None
        self._render_thread = None
        self._stop = True
        self._idle_frames = 0          # 连续静止帧数(降频探测用)
        # 可唤醒睡眠: 空闲降频时渲染线程一次睡 0.5s, 几何/交互请求
        # 置位本事件可立即结束长睡, 消除拖动/面板调参的首帧迟滞
        self._wake = threading.Event()

    # -------------------------------------------------- 渲染线程
    def update_snapshot(self, snap):
        """接收渲染快照(主线程调用; 整引用原子替换, 无需加锁).

        快照替换即失效静止跳帧的原始数据缓存(参数可能已变, 必须
        重算), 并唤醒渲染线程让配置变化当帧生效(不被空闲长睡拖延)."""
        self._snap = snap
        self._last_raw = None
        self._wake.set()

    def set_interactive(self, active):
        """交互(按住左键拖动/缩放)状态开关(主线程调用).

        True 期间渲染切纯色+轮廓并暂停实时模糊; 机制状态归 Bar 私有."""
        self.interactive = bool(active)
        if active:
            self._wake.set()

    def request_shot(self, path):
        """请求下一帧保存当前清晰画面(笔记截图, 主线程调用)."""
        self._shot_request = path

    def start(self):
        """启动渲染线程(主线程调用一次; 首份快照须已推送)."""
        self._stop = False
        self._render_thread = threading.Thread(
            target=self._render_loop, name='render', daemon=True)
        self._render_thread.start()

    def stop(self):
        """请求渲染线程退出并等待其结束(主线程调用)."""
        self._stop = True
        if self._render_thread is not None:
            self._render_thread.join(timeout=1.0)
            self._render_thread = None

    def _render_loop(self):
        """渲染线程主循环: 每帧开头读一次快照引用(ARCH D4).

        可见性判断全部读快照, 不读 App 的任何内部字段; 其余节奏
        (跳帧/空闲降频)与原顶层 RenderThread 完全一致."""
        while not self._stop:
            snap = self._snap
            fps = max(2, snap.fps) if snap is not None else 2
            t0 = time.monotonic()
            if snap is not None and snap.visible \
                    and not snap.screen_locked and not snap.peek_active:
                try:
                    before = self._last_raw
                    self.render(snap)
                    # 静止检测: 本帧数据与上帧相同即视为静止
                    # (轮廓提示/交互期间视为活跃, 蚂蚁线需要持续动画)
                    if self._hint_start > 0 or self.interactive:
                        self._idle_frames = 0
                    if self._last_raw == before:
                        self._idle_frames += 1
                    else:
                        self._idle_frames = 0
                except Exception as e:
                    # 渲染线程内任何单帧异常都不终止循环
                    print('渲染线程异常:', e, file=sys.stderr)
            else:
                self._idle_frames = 0
            # 基础间隔; 长时间静止后降到低频探测
            interval = 1.0 / fps
            if self._idle_frames >= self.IDLE_FRAMES_BEFORE_SLOWDOWN:
                interval = max(interval, self.IDLE_INTERVAL)
            elapsed = time.monotonic() - t0
            # 可唤醒睡眠: 交互/几何/快照请求置位 _wake 即提前结束本休眠
            self._wake.wait(max(0.005, interval - elapsed))
            self._wake.clear()

    # -------------------------------------------------- 几何/状态操作
    def request_geometry(self, x, y, w, h):
        """请求调整窗口位置尺寸(主线程调用).

        不直接 configure, 而是登记为待处理几何, 由渲染线程在同一
        fastx 连接内先应用几何再写图, 保证两者顺序一致."""
        w = clamp(w, MIN_W, max(MIN_W, self.root_w * 2))
        h = clamp(h, MIN_H, max(MIN_H, self.root_h))
        self.pending_geo = (int(x), int(y), w, h)
        self._wh = (w, h)
        self._pos = (x, y)
        # 几何变化后上一帧输出已失效(位置/尺寸不再匹配), 置空防重放
        self._last_out = None
        # 任何几何变化都进入纯色模式(去抖 0.8s)
        self.solid_until = time.monotonic() + 0.8
        self._wake.set()

    def raise_top(self):
        """把遮挡条压到 X 栈顶, 保证盖住一切(含全屏窗口)."""
        try:
            self.win.configure(stack_mode=X.Above)
            self.dpy.flush()
        except XError:
            pass

    def set_background_color(self, rgb):
        """更新窗口背景色(主线程调用).

        resize 变大时新暴露的区域先显示背景色, 与交互纯色保持一致
        才不会闪黑."""
        r, g, b = rgb
        try:
            self.win.change_attributes(
                background_pixel=(r << 16) | (g << 8) | b)
            self.dpy.flush()
        except XError:
            pass

    def set_opacity(self, opacity):
        """设置整窗不透明度 0.0~1.0 (mutter 支持 _NET_WM_WINDOW_OPACITY).

        主线程调用(python-xlib 连接)."""
        try:
            opacity = clamp(int(clamp(opacity, 0.0, 1.0) * 0xFFFFFFFF),
                            0, 0xFFFFFFFF)
            from Xlib import Xatom
            self.win.change_property(
                self.dpy.intern_atom('_NET_WM_WINDOW_OPACITY'),
                Xatom.CARDINAL, 32, [opacity])
            self.dpy.flush()
        except XError:
            pass

    def show(self, hint=True):
        """显示遮挡条; 默认同时触发数秒的轮廓提示."""
        try:
            self.win.map()
            self.dpy.flush()
            if hint:
                self.show_hint()
        except XError:
            pass

    def show_hint(self):
        """启动轮廓提示(时长由快照 hint_seconds 决定, 0 = 关闭)."""
        self._hint_start = time.monotonic()

    def _hint_overlays(self, size):
        """生成/缓存轮廓提示图层: 黑色衬边(实线) + 白色蚂蚁线(虚线, 8 相位).

        双色调保证亮/暗背景下都醒目; 蒙版按尺寸缓存, 动画只换相位."""
        if self._hint_cache_key == size:
            return self._hint_cache
        from PIL import ImageDraw
        w, h = size
        black_img = Image.new('RGB', size, (15, 15, 15))
        black_mask = Image.new('L', size, 0)
        bd = ImageDraw.Draw(black_mask)
        bd.rectangle([1, 1, w - 3, h - 3], outline=255, width=2)
        white_img = Image.new('RGB', size, (250, 250, 250))
        white_masks = []
        for phase in range(8):
            m = Image.new('L', size, 0)
            wd = ImageDraw.Draw(m)
            dash, gap = 10, 8
            period = dash + gap
            off = phase * 3 % period
            x = -period + off
            while x < w:
                wd.line([(x, 3), (min(x + dash, w - 4), 3)], fill=255, width=2)
                wd.line([(x, h - 5), (min(x + dash, w - 4), h - 5)],
                        fill=255, width=2)
                x += period
            y = -period + off
            while y < h:
                wd.line([(3, y), (3, min(y + dash, h - 4))], fill=255, width=2)
                wd.line([(w - 5, y), (w - 5, min(y + dash, h - 4))],
                        fill=255, width=2)
                y += period
            white_masks.append(m)
        self._hint_cache = (black_img, black_mask, white_img, white_masks)
        self._hint_cache_key = size
        return self._hint_cache

    def hide(self):
        try:
            self.win.unmap()
            self.dpy.flush()
        except XError:
            pass

    def set_input_locked(self, input_locked):
        """输入锁定: 将 X input region 置空 => 鼠标事件完全穿透.

        命名约定(ARCH D1): input_locked=鼠标穿透锁定, 与锁屏的
        screen_locked 区分. 面板/热键仍可操作, 因此不会"死锁".

        解锁态用远超窗口上限的矩形: input region 与窗口边界求交后
        恒等于"整个窗口", 窗口 resize 后无需重新应用 —— 输入区域
        结构性地跟随当前几何, 不存在调用方传陈旧 w/h 的脱节面."""
        try:
            if input_locked:
                # 空矩形列表 => 该窗口对鼠标完全透明
                self.win.shape_rectangles(xshape.SO.Set, xshape.SK.Input,
                                          0, 0, 0, [])
            else:
                self.win.shape_rectangles(xshape.SO.Set, xshape.SK.Input,
                                          0, 0, 0, [(0, 0, 32767, 32767)])
            self.dpy.flush()
        except XError:
            pass

    # -------------------------------------------------- 窗口级命令(D2)
    def window_id(self):
        """返回遮挡条窗口的 XID(App 记录诊断文件/事件过滤用)."""
        return self.win.id

    def _cursor_named(self, name):
        """光标形态名 -> X 光标对象(私有映射, App 只传意图名不摸光标对象)."""
        return {'normal': self.cursor_normal,
                'move': self.cursor_move,
                'resize_h': self.cursor_resize['h'],
                'resize_v': self.cursor_resize['v']}.get(name,
                                                         self.cursor_normal)

    def set_cursor(self, name):
        """设置窗口光标形态(主线程调用).

        name: 'normal' 左箭头 / 'move' 移动 / 'resize_h' 水平缩放 /
        'resize_v' 垂直缩放; 未知名回退常态."""
        try:
            self.win.change_attributes(cursor=self._cursor_named(name))
            self.dpy.flush()
        except XError:
            pass

    def grab_pointer(self, cursor='move'):
        """把指针事件抓到本窗口, 直到按钮松开(主线程调用).

        拖动/缩放期间需要持续收到 Motion/ButtonRelease 事件, 因此
        抓取掩码固定为三类指针事件; cursor 为抓取期间的光标形态名
        (同 set_cursor)."""
        try:
            self.win.grab_pointer(
                False,
                X.ButtonReleaseMask | X.PointerMotionMask |
                X.ButtonPressMask,
                X.GrabModeAsync, X.GrabModeAsync, X.NONE,
                self._cursor_named(cursor), X.CurrentTime)
            self.dpy.flush()
        except XError:
            pass

    def stack_below(self, sibling_xid):
        """把遮挡条压到同级窗口正下方(主线程调用).

        sibling_xid 须与遮挡条同为 root 的直接子窗口(被 mutter
        reparent 进 frame 的窗口要传其 frame 的 XID)."""
        try:
            self.win.configure(stack_mode=X.Below, sibling=sibling_xid)
            self.dpy.flush()
        except XError:
            pass

    # -------------------------------------------------- 渲染
    def render(self, snap):
        """渲染一帧遮挡画面(渲染线程调用; 输入只有渲染快照).

        数据源: 遮挡条下方各窗口的实时内容, 按 z 序从远到近分层拼合
        (与合成器的合成顺序一致, 横跨多窗口时每段都模糊自己
        真正下方的画面). mutter 合成器的输入即各窗口的重定向像素,
        恒为实时; root 快照严重滞后, 不再使用.
        不读活配置 dict / App / GTK (ARCH D4).
        """
        try:
            x, y = snap.x, snap.y
            # 应用待处理几何: move/resize 与 put 同连接同顺序,
            # 窗口尺寸与图像尺寸永远一致(修复 resize 黑闪)
            if self.pending_geo is not None and self.fastx is not None:
                pgx, pgy, pgw, pgh = self.pending_geo
                self.fastx.move_resize_window(self.win.id,
                                              pgx, pgy, pgw, pgh)
                self._wh = (pgw, pgh)
                self.pending_geo = None
            w, h = self._wh
            if w <= 0 or h <= 0:
                return

            # ---- 纯色模式: 任何几何变化(拖动/手柄/滚轮/面板/预设)
            # 期间用纯色实底 + 常亮轮廓, 暂停实时模糊与屏幕抓取, 消除
            # "模糊内容延迟跟随"的廉价感; 变化停止 0.8s 后自动恢复磨砂
            # 并触发轮廓渐隐提示完成视觉过渡.
            if self.interactive:
                # 防卡死: release 事件偶发丢失会让交互状态永久卡住,
                # 这里直接查询 X server 的真实按键状态, 左键已松
                # 就自动退出交互模式
                try:
                    btns = self.fastx.pointer_buttons()
                    if not btns & 0x100:            # Button1Mask
                        self.interactive = False
                except (RuntimeError, OSError):
                    pass
            solid = self.interactive or time.monotonic() < self.solid_until
            if solid:
                canvas = Image.new('RGB', (w, h),
                                   tuple(snap.tint_color))
                phase = int(time.monotonic() * 10) % 8
                black_img, black_mask, white_img, white_masks = (
                    self._hint_overlays(canvas.size))
                canvas = Image.composite(black_img, canvas, black_mask)
                canvas = Image.composite(white_img, canvas,
                                         white_masks[phase])
                out = canvas.tobytes('raw', 'BGRX', w * 4)
                if self.fastx is not None:
                    self.fastx.put(self.win.id, out, w, h, w * 4)
                return

            blur = snap.blur
            pad = 0
            if blur > 0:
                pad = min(64, int(blur * 2))

            feather = snap.feather
            blur_canvas = None
            sharp_canvas = None
            shot_saved = False

            # 轮廓提示激活期间强制渲染(蚂蚁线相位在动)
            hint_s = float(snap.hint_seconds)
            hint_on = (self._hint_start > 0
                       and time.monotonic() - self._hint_start < hint_s)
            layers = self._below_cache or []
            for layer in layers:
                grabbed = self._grab_layer(layer, x, y, w, h, pad)
                if grabbed is None:
                    continue
                data, bpl, depth, gw, gh, gx, gy = grabbed

                # 半透明覆盖层(如桌面图标层)的像素是 ARGB 全透明,
                # RGB 恒黑且不应出现在模糊结果里 => 跳过
                alpha_mean = 255.0
                if depth == 32:
                    sample = data[3::101]           # 抽样 ~1% 像素
                    alpha_mean = sum(sample) / len(sample) if sample else 0
                    if alpha_mean < 3:
                        continue                     # 纯透明覆盖层, 忽略
                    has_alpha = True
                else:
                    has_alpha = False

                # 静止跳过; 但截图请求/提示动画期间需渲染
                if data == self._last_raw and not (
                        self._shot_request or hint_on):
                    continue
                self._last_raw = data
                img = self._decode(data, gw, gh, bpl, has_alpha)
                if img is None:
                    continue
                if not shot_saved:
                    shot_saved = True
                    self._shot_save(img.convert('RGB'),
                                    x, y, w, h, gx, gy, gw, gh)
                blur_img = BlurPipeline.gaussian(img, blur)
                blur_img = BlurPipeline.post_process(
                    blur_img.convert('RGB'), snap.tint_color,
                    snap.tint_alpha, snap.noise)
                cx1, cy1 = max(gx, x), max(gy, y)
                cx2 = min(gx + gw, x + w)
                cy2 = min(gy + gh, y + h)
                if cx2 <= cx1 or cy2 <= cy1:
                    continue
                off = (cx1 - x, cy1 - y)
                if blur_canvas is None:
                    blur_canvas = Image.new('RGB', (w, h), (24, 24, 24))
                crop_box = (cx1 - gx, cy1 - gy, cx2 - gx, cy2 - gy)
                if has_alpha:
                    # 带 alpha 的层: 用(模糊后的)alpha 作蒙版 over 合成,
                    # 透明区域保留下层画面
                    rgba = img
                    alpha_ch = rgba.split()[3].filter(
                        ImageFilter.GaussianBlur(blur))
                    blur_canvas.paste(
                        blur_img.crop(crop_box), off,
                        alpha_ch.crop(crop_box))
                    if feather > 0:
                        if sharp_canvas is None:
                            sharp_canvas = Image.new('RGB', (w, h),
                                                     (24, 24, 24))
                        sharp_canvas.paste(
                            rgba.convert('RGB').crop(crop_box), off,
                            alpha_ch.crop(crop_box))
                else:
                    blur_canvas.paste(blur_img.crop(crop_box), off)
                    if feather > 0:
                        if sharp_canvas is None:
                            sharp_canvas = Image.new('RGB', (w, h),
                                                     (24, 24, 24))
                        sharp_canvas.paste(img.crop(crop_box), off)

            if blur_canvas is None:
                # 提示超时兜底: 重放缓存可能停在渐隐中途(渲染在渐隐
                # 窗口内被门控/长睡打断), 先丢弃半程蚂蚁线的旧输出,
                # 让本帧走下方 root 回退重算出干净画面
                if self._hint_start > 0 and \
                        time.monotonic() - self._hint_start >= \
                        max(0.1, float(snap.hint_seconds)):
                    self._hint_start = 0.0
                    self._last_out = None
                # 静止跳帧(所有层内容都与上帧相同)时直接重放上一帧
                # 输出: 内容本就无需更新, 落到 root 回退反而会把陈旧
                # root 快照拍进条里(黑条/整体错位), 还白费一次抓取;
                # 快照按引用比较, 配置一变即缓存失效, 不会重放过期画面
                last = self._last_out
                if last is not None and last[1] == w and last[2] == h \
                        and last[3] is snap and self.fastx is not None:
                    self.fastx.put(self.win.id, last[0], w, h, w * 4)
                    return
                # 所有层都被 alpha 过滤(如纯透明的桌面覆盖层)或无层:
                # 回退 root 快照 —— 壁纸/桌面是静态内容, 快照滞后无碍
                grabbed = self._grab_root(x, y, w, h, pad)
                if grabbed is None:
                    return
                data, bpl, gw, gh, gx, gy = grabbed
                img = self._decode(data, gw, gh, bpl)
                if img is None:
                    return
                img = BlurPipeline.gaussian(img, blur)
                img = BlurPipeline.post_process(
                    img, snap.tint_color, snap.tint_alpha, snap.noise)
                blur_canvas = Image.new('RGB', (w, h), (24, 24, 24))
                # 裁剪框坐标是 root 抓取图内的绝对偏移, 贴回条画布必须
                # 从 (0,0) 开始; 旧代码误用裁剪框左上角作 paste 偏移,
                # 导致左侧/顶部各留一条宽 pad 的底色黑条, 且内容整体
                # 向右下平移 pad(与正常帧交替即"对角往返"错觉)
                blur_canvas.paste(
                    img.crop((max(0, x - gx), max(0, y - gy),
                              min(gx + gw, x + w) - gx,
                              min(gy + gh, y + h) - gy)),
                    (0, 0))
            if feather > 0 and sharp_canvas is not None:
                canvas = BlurPipeline.compose_feather(blur_canvas,
                                                      sharp_canvas, feather)
            else:
                canvas = blur_canvas

            # 轮廓提示: 黑衬边 + 白蚂蚁线, 后段线性渐隐至消失
            if hint_s > 0 and self._hint_start > 0:
                t = time.monotonic() - self._hint_start
                if t < hint_s:
                    # 前 85% 全亮保持醒目, 末 15% 平滑淡出
                    factor = 1.0 if t < hint_s * 0.85 else max(
                        0.0, 1.0 - (t - hint_s * 0.85) / (hint_s * 0.15))
                    phase = int(t * 10) % 8
                    black_img, black_mask, white_img, white_masks = (
                        self._hint_overlays(canvas.size))
                    if factor < 1.0:
                        bm = black_mask.point(lambda v: int(v * factor))
                        wm = white_masks[phase].point(
                            lambda v: int(v * factor))
                    else:
                        bm, wm = black_mask, white_masks[phase]
                    canvas = Image.composite(black_img, canvas, bm)
                    canvas = Image.composite(white_img, canvas, wm)
                else:
                    self._hint_start = 0.0   # 提示结束(已渐隐, 无残影)

            out = canvas.tobytes('raw', 'BGRX', w * 4)
            # 缓存本帧输出(静止跳帧后重放用, 见回退分支说明)
            self._last_out = (out, w, h, snap)
            # 渲染线程只允许使用 fastx(ctypes 独占连接);
            # fastx 不可用时无法安全渲染, 保持背景色.
            # 注意: XPutImage 的返回值恒为 0, 不代表成败.
            if self.fastx is not None:
                self.fastx.put(self.win.id, out, w, h, w * 4)
        except (XError, ValueError) as e:
            # 单帧失败(如瞬时窗口状态异常)不影响后续帧
            print('渲染跳过:', e, file=sys.stderr)

    def _grab_layer(self, layer, x, y, w, h, pad):
        """抓取单个下层窗口与遮挡条交集的实时内容(仅 fastx).

        layer = (xid, 缓存的绝对x, 缓存的绝对y, 宽, 高);
        窗口当前位置用 XTranslateCoordinates 每帧校准, 防止缓存过期."""
        if self.fastx is None:
            return None
        xid = layer[0]
        geo = self.fastx.get_geometry(xid)
        org = self.fastx.window_origin(xid)
        if geo is None or org is None:
            return None          # 窗口已销毁
        ax, ay = org
        ww, wh = geo[2], geo[3]
        gx = max(ax, x - pad)
        gy = max(ay, y - pad)
        gx2 = min(ax + ww, x + w + pad)
        gy2 = min(ay + wh, y + h + pad)
        gw, gh = gx2 - gx, gy2 - gy
        if gw <= 0 or gh <= 0:
            return None
        try:
            data, bpl, depth = self.fastx.grab_window(xid, gx - ax,
                                                      gy - ay, gw, gh)
            return data, bpl, depth, gw, gh, gx, gy
        except (RuntimeError, OSError):
            return None

    def _shot_save(self, img, x, y, w, h, gx, gy, gw, gh):
        """笔记截图: 把当前清晰帧裁到遮挡条区域保存(渲染线程)."""
        path = self._shot_request
        if not path:
            return
        self._shot_request = None
        try:
            img.crop((max(0, x - gx), max(0, y - gy),
                      min(gw, x - gx + w), min(gh, y - gy + h))).save(path)
        except (OSError, ValueError) as e:
            print('截图保存失败:', e, file=sys.stderr)

    def _grab_root(self, x, y, w, h, pad):
        """回退: 抓 root 快照(区域 clamp 在屏幕内; 仅 fastx)."""
        if self.fastx is None:
            return None
        gx = max(0, x - pad)
        gy = max(0, y - pad)
        gx2 = min(self.root_w, x + w + pad)
        gy2 = min(self.root_h, y + h + pad)
        gw, gh = gx2 - gx, gy2 - gy
        if gw <= 0 or gh <= 0:
            return None
        try:
            data, bpl, _depth = self.fastx.grab(gx, gy, gw, gh)
            return data, bpl, gw, gh, gx, gy
        except (RuntimeError, OSError):
            return None

    def _decode(self, data, gw, gh, bpl, has_alpha=False):
        """把 ZPixmap 原始数据解成 PIL 图.

        has_alpha: depth 32 的 ARGB 窗口, 解成 RGBA(保留 alpha);
        否则解成 RGB. 行距与像素宽不同时告知 PIL 实际行距."""
        bpp = bpl // gw
        if bpp == 4:
            fmt = 'BGRA' if has_alpha else 'BGRX'
            mode = 'RGBA' if has_alpha else 'RGB'
        elif bpp == 3:
            fmt = 'BGR'
            mode = 'RGB'
        else:
            return None
        if bpl == gw * bpp:
            return Image.frombytes(mode, (gw, gh), data, 'raw', fmt)
        # 服务器行对齐与像素宽不同时, 告知 PIL 实际行距
        return Image.frombytes(mode, (gw, gh), data, 'raw', fmt, bpl)

    def refresh_below(self):
        """重新查找遮挡条下方的窗口层叠并更新缓存.

        涉及 python-xlib(仅主线程可用), 由 App 的定时器周期调用;
        渲染线程只读缓存的纯数据.
        遮挡条可能横跨多个窗口, 故收集 z 序从近到远的若干层,
        渲染时逐层拼合(远层先贴, 近层覆盖), 与合成器视觉一致."""
        x, y, w, h = self._pos[0], self._pos[1], self._wh[0], self._wh[1]
        self._below_cache = self._find_layers_below(x, y, w, h)

    def _find_layers_below(self, x, y, w, h):
        """收集 z 序中遮挡条之下、与其相交的可见窗口(从近到远).

        XQueryTree 的子窗口按 stacking 返回(自底向上),
        先定位遮挡条, 再向底层方向逐个收集相交的可见窗口,
        直到无窗口或达到层数上限(4 层足够覆盖常规横跨场景).
        返回纯数据 [(xid, 绝对x, 绝对y, 宽, 高), ...] 或 None.
        仅主线程调用(python-xlib)."""
        tree = self.root.query_tree().children
        my = self.win.id
        idx = None
        for i in range(len(tree) - 1, -1, -1):
            if tree[i].id == my:
                idx = i
                break
        if idx is None:
            return None
        layers = []
        spans = []                     # 已覆盖的 x 区间(遮挡条为水平条带)
        def covered_len():
            total = 0
            end = None
            for s, e in sorted(spans):
                s2 = max(s, end or s)
                if e > s2:
                    total += e - s2
                end = max(end or e, e)
            return total
        for j in range(idx - 1, -1, -1):
            if len(layers) >= 4 or covered_len() >= w - 4:
                break
            cand = tree[j]
            try:
                a = cand.get_attributes()
                if a.map_state != 2:
                    continue
                g = cand.get_geometry()
                t = cand.translate_coords(self.root, 0, 0)
                ax, ay = -t.x, -t.y          # 窗口绝对位置
                ix1 = max(ax, x); iy1 = max(ay, y)
                ix2 = min(ax + g.width, x + w)
                iy2 = min(ay + g.height, y + h)
                if ix2 <= ix1 or iy2 <= iy1:
                    continue
                client = self._resolve_client(cand, ax, ay, g)
                if client is not None:
                    layers.append(client)
                    spans.append((ix1, ix2))
            except XError:
                continue
        return layers or None

    def _resolve_client(self, w, ax, ay, g):
        """把候选窗口解析成可直抓的窗口.

        * 有 EWMH 名字 => 本身就是 client(现代 GNOME 应用均为 CSD);
        * 无名字但有 mapped 子窗口 => 疑似 SSD 的 frame, 下钻取 client;
        * 无名字且无子窗口 => 自身即可直抓(override 菜单/tooltip 等).
        返回纯数据 (xid, 绝对x, 绝对y, 宽, 高) 或 None."""
        name = self.dpy.intern_atom('_NET_WM_NAME')
        try:
            kids = [k for k in w.query_tree().children
                    if k.get_attributes().map_state == 2]
        except XError:
            kids = []
        if w.get_full_property(name, X.AnyPropertyType) is not None \
                or not kids:
            return (w.id, ax, ay, g.width, g.height)
        k = kids[0]
        kg = k.get_geometry()
        return (k.id, ax + kg.x, ay + kg.y, kg.width, kg.height)
