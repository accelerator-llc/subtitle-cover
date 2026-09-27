#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CoverBar: X11 override-redirect 遮挡条窗口.

线程模型:
  * 主线程(GLib): 窗口管理/事件/下方窗口查找缓存(python-xlib 连接)
  * 渲染线程: 每帧抓取-模糊-写回(仅使用 FastX 的 ctypes 连接)
python-xlib 连接非线程安全, 渲染路径中严禁出现 python-xlib 调用.
"""

import os
import sys
import time

from PIL import Image, ImageFilter

from Xlib import X, Xcursorfont
from Xlib.ext import shape as xshape
from Xlib.error import XError

from ..core.blur import BlurPipeline
from ..core.config import clamp, MIN_W, MIN_H


class CoverBar:
    """遮挡条窗口本体.

    override-redirect 窗口不经过窗口管理器:
      * 位置尺寸完全由本程序控制, root 坐标即屏幕视觉坐标(已实测);
      * 天然位于窗口管理器管理的窗口(含全屏视频)之上;
      * 不进入 mutter 写回 root 的合成画面, 直抓下方窗口时
        不会把自身拍进去.
    """

    def __init__(self, dpy, cfg):
        self.dpy = dpy
        self.screen = dpy.screen()
        self.root = self.screen.root
        self.depth = self.root.get_geometry().depth
        self.root_w = self.screen.width_in_pixels
        self.root_h = self.screen.height_in_pixels

        # 首次运行: 摆到屏幕底部居中, 大约盖住最下方一行字幕
        if cfg.get('x') is None or cfg.get('y') is None:
            cfg['w'] = int(self.root_w * 0.72)
            cfg['h'] = 110
            cfg['x'] = (self.root_w - cfg['w']) // 2
            cfg['y'] = int(self.root_h * 0.80)
            from ..core.config import save_config
            save_config(cfg)

        # 窗口背景色跟随色调颜色: resize 扩大的新区域在图像铺满前
        # 显示背景色, 若为黑色会与纯色模式来回闪跳(实测); 与纯色一致则无缝
        r, g, b = cfg['tint_color']
        bg_pixel = (r << 16) | (g << 8) | b
        self.win = self.root.create_window(
            cfg['x'], cfg['y'], cfg['w'], cfg['h'],
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

        # ctypes 快路径(像素读写直调 libX11, 见 FastX 类说明)
        self.fastx = None
        try:
            from .fastx import FastX
            self.fastx = FastX(os.environ.get('DISPLAY'))
        except (OSError, RuntimeError) as e:
            print('FastX 不可用, 渲染功能不可用:', e, file=sys.stderr)

        # 渲染线程使用的窗口尺寸缓存(主线程 move_resize 时更新),
        # 避免渲染线程调用 python-xlib
        self._wh = (cfg['w'], cfg['h'])

        # 遮挡条正下方窗口缓存: (xid, ax, ay, ww, wh)
        # 由主线程周期刷新(python-xlib), 渲染线程只读
        self._below_cache = None

        # 当前位置缓存(主线程维护), 供主线程查找下方窗口时使用
        self.cfg_pos = (cfg['x'], cfg['y'])

        # 待应用的几何(主线程写入, 渲染线程在同一 fastx 连接内
        # 先 move/resize 再 put —— 同连接顺序由 X 协议保证,
        # 彻底消除 resize 时窗口尺寸与图像尺寸不一致的黑闪)
        self.pending_geo = None

        # 临时透视状态: True 时渲染输出改为清晰透传(渲染线程读取)
        self.peeking = False

        # 纯色模式: 任何几何变化(拖动/手柄/滚轮/面板/预设)都会刷新
        # solid_until —— 变化期间渲染纯色实底 + 常亮轮廓, 暂停实时模糊
        # 与屏幕抓取(避免"延迟跟随的模糊"的廉价感); 变化停止 0.8s 后
        # 自动恢复磨砂并触发轮廓渐隐提示.
        self.interactive = False       # 按住左键拖动/调整中
        self.solid_until = 0.0         # 纯色模式的截止时间(单调钟)

        # 轮廓提示: 显示/恢复时在边缘画数秒蚂蚁线, 防止纯色背景下
        # 磨砂条与背景融为一体找不到. _hint_start=0 表示未激活.
        self._hint_start = 0.0
        self._hint_cache = None
        self._hint_cache_key = None
        # 透视渐变系数: 1=完全模糊, 0=完全清晰
        self.peek_factor = 1.0

        # 笔记截图请求: 主线程写入目标路径, 渲染线程在下一帧
        # 用当前抓取帧(清晰原图)保存, 避免额外的屏幕抓取
        self.shot_request = None

    # -------------------------------------------------- 几何/状态操作
    def request_geometry(self, x, y, w, h):
        """请求调整窗口位置尺寸(主线程调用).

        不直接 configure, 而是登记为待处理几何, 由渲染线程在同一
        fastx 连接内先应用几何再写图, 保证两者顺序一致."""
        w = clamp(w, MIN_W, max(MIN_W, self.root_w * 2))
        h = clamp(h, MIN_H, max(MIN_H, self.root_h))
        self.pending_geo = (int(x), int(y), w, h)
        self._wh = (w, h)
        self.cfg_pos = (x, y)
        # 任何几何变化都进入纯色模式(去抖 0.8s)
        self.solid_until = time.monotonic() + 0.8

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
        """启动轮廓提示(时长由配置 hint_seconds 决定, 0 = 关闭)."""
        self._hint_start = time.monotonic()

    def hint_active(self):
        """轮廓提示是否仍在显示(渲染线程/主线程均可调用)."""
        return self._hint_start > 0

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

    def set_locked(self, locked, w, h):
        """锁定: 将 X input region 置空 => 鼠标事件完全穿透.

        面板/热键仍可操作, 因此不会"死锁".
        """
        try:
            if locked:
                # 空矩形列表 => 该窗口对鼠标完全透明
                self.win.shape_rectangles(xshape.SO.Set, xshape.SK.Input,
                                          0, 0, 0, [])
            else:
                self.win.shape_rectangles(xshape.SO.Set, xshape.SK.Input,
                                          0, 0, 0, [(0, 0, w, h)])
            self.dpy.flush()
        except XError:
            pass

    # -------------------------------------------------- 渲染
    def render(self, cfg):
        """渲染一帧遮挡画面(渲染线程调用).

        数据源: 遮挡条下方各窗口的实时内容, 按 z 序从远到近分层拼合
        (与合成器的合成顺序一致, 横跨多窗口时每段都模糊自己
        真正下方的画面). mutter 合成器的输入即各窗口的重定向像素,
        恒为实时; root 快照严重滞后, 不再使用.
        """
        try:
            x, y = cfg['x'], cfg['y']
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
                                   tuple(cfg['tint_color']))
                phase = int(time.monotonic() * 10) % 8
                black_img, black_mask, white_img, white_masks = (
                    self._hint_overlays(canvas.size))
                canvas = Image.composite(black_img, canvas, black_mask)
                canvas = Image.composite(white_img, canvas,
                                         white_masks[phase])
                out = canvas.tobytes('raw', 'BGRX', w * 4)
                if self.fastx is not None:
                    self.fastx.put(self.win.id, out, w, h, w * 4)
                # 纯色模式结束(非拖动按住的自然超时): 保存配置并
                # 触发轮廓渐隐提示, 视线从纯色+轮廓平滑过渡回磨砂
                if not self.interactive and \
                        time.monotonic() >= self.solid_until:
                    from ..core.config import save_config as _sc
                    _sc(self.cfg)
                    self.show_hint()
                    self.solid_until = 0.0
                return

            blur = cfg['blur']
            # 临时透视: 模糊强度按渐变系数衰减, 视觉上平滑"看清"
            if self.peeking:
                blur = int(blur * self.peek_factor)
            pad = 0
            if blur > 0:
                pad = min(64, int(blur * 2))

            feather = cfg.get('feather', 0)
            blur_canvas = None
            sharp_canvas = None
            shot_saved = False

            # 轮廓提示激活期间强制渲染(蚂蚁线相位在动)
            hint_s = float(cfg.get('hint_seconds', 3))
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

                # 静止跳过; 但透视渐变/截图请求期间需渲染
                if data == self._last_raw and not (
                        (0.001 < self.peek_factor < 0.999)
                        or self.shot_request or hint_on):
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
                    blur_img.convert('RGB'), cfg['tint_color'],
                    cfg['tint_alpha'], cfg['noise'])
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
                    img, cfg['tint_color'], cfg['tint_alpha'], cfg['noise'])
                blur_canvas = Image.new('RGB', (w, h), (24, 24, 24))
                blur_canvas.paste(
                    img.crop((max(0, x - gx), max(0, y - gy),
                              min(gx + gw, x + w) - gx,
                              min(gy + gh, y + h) - gy)),
                    (max(0, x - gx), max(0, y - gy)))
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
        path = self.shot_request
        if not path:
            return
        self.shot_request = None
        try:
            img.crop((max(0, x - gx), max(0, y - gy),
                      min(gw, x - gx + w), min(gh, y - gy + h))).save(path)
        except (OSError, ValueError) as e:
            print('截图保存失败:', e, file=sys.stderr)

    def update_peek(self):
        """渲染线程每帧调用: peek_factor 向目标值(按住 0 / 松开 1)收敛,
        实现"呼吸式"的模糊 <-> 清晰过渡."""
        target = 0.0 if self.peeking else 1.0
        diff = target - self.peek_factor
        self.peek_factor = target if abs(diff) < 0.08 \
            else self.peek_factor + diff * 0.45

    def drag_end_cleanup(self):
        """交互结束的公共清理: 保存配置 + 轮廓提示.

        主线程(release 事件)与渲染线程(防卡死兜底)都会调用."""
        from ..core.config import save_config
        save_config(self.cfg)
        if not self.peeking:
            self.show_hint()

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
        x, y, w, h = self.cfg_pos[0], self.cfg_pos[1], self._wh[0], self._wh[1]
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
