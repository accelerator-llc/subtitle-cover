#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""残影诊断探针(修复 agent F): 拖动条 A<->B 后 vacated 区域陈旧帧测量.

机制判别(架构候选):
  甲(合成器纹理竞态)  —— root 上条区域内容 != 条窗口自身内容(采到旧纹理/旧几何)
  乙(免合成陈旧帧缓冲) —— 条移开后腾出的区域无人重绘, 残留条的最后像素,
                          直到"视频"下一帧覆盖(残影清除时刻与视频帧对齐)

方法(不抢鼠标, 条几何由本探针直接 XMoveResizeWindow 驱动):
  1. 条下方压一个高对比马赛克装置(configure(stack_mode=Below, sibling=条),
     按 plan §5.11 要求做栈序查询 + 像素自检); 参考帧用装置图案解析生成,
     另抓一帧对照以发现"参考帧本身被残影污染"
  2. 脚本化 A(部分出屏) <-> B(全在屏内) 往返, 每次到达后按 ~30ms 采样
     vacated 区域, 残影判据: 差异像素占比 > 0.5%; 同时比对残影内容与
     "条在起点时的窗口内容"(乙签名)
  3. --scenario fs/fs-bypass1/fs-noredir 时创建受管全屏"视频"窗口(慢帧率
     重绘, 角落方块编码帧号), 验证 mutter 免合成状态(XComposite
     NameWindowPixmap)与残影率/清除时长的关系

用法:
  python3 tools/probe_ghost.py --scenario none --reps 20
  python3 tools/probe_ghost.py --scenario fs         --reps 20
  python3 tools/probe_ghost.py --scenario fs-bypass1 --reps 20
  python3 tools/probe_ghost.py --scenario fs-noredir --reps 20
输出: 逐轮数据 + 汇总(JSON 写 --out)。
"""

import argparse
import json
import os
import sys
import threading
import time

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    _PROJ, '.deps/extracted/usr/lib/python3/dist-packages'))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _xprobe import XProbe            # noqa: E402  (ctypes FastX 抓取/写图)
from PIL import Image, ImageChops, ImageDraw   # noqa: E402
from Xlib import X, display as xdisplay   # noqa: E402
from Xlib.ext import composite        # noqa: E402

GHOST_DIFF = 40        # 残影像素判据: 任一通道差 > 40
GHOST_FRAC = 0.005     # 帧残影判据: 差异像素占比 > 0.5%
CELL, PERIOD = 60, 240
PALETTE = [(240, 40, 40), (40, 160, 240), (250, 210, 40), (40, 200, 90),
           (200, 60, 220), (250, 250, 250), (20, 20, 20), (250, 130, 20)]
XID_FILE = os.path.expanduser('~/.config/subtitle-cover/bar_xid')


def make_pattern(w, h, seed=7):
    """高对比马赛克图案(与 probe_device 同款, PERIOD 平铺保证可平移裁剪)."""
    import random
    rng = random.Random(seed)
    cells_x, cells_y = -(-w // CELL), -(-h // CELL)
    block = [[rng.choice(PALETTE) for _ in range(PERIOD // CELL)]
             for _ in range(PERIOD // CELL)]
    img = Image.new('RGB', (cells_x * CELL, cells_y * CELL))
    d = ImageDraw.Draw(img)
    for cy in range(cells_y):
        for cx in range(cells_x):
            c = block[cy % (PERIOD // CELL)][cx % (PERIOD // CELL)]
            d.rectangle([cx * CELL, cy * CELL,
                         cx * CELL + CELL - 1, cy * CELL + CELL - 1], fill=c)
    return img


def diff_frac(img, ref, bbox=None, thr=GHOST_DIFF):
    """两图(或图内 bbox)差异像素占比(任一通道差 > thr; PIL C 路径实现)."""
    if img is None or ref is None:
        return -1.0
    if bbox:
        img, ref = img.crop(bbox), ref.crop(bbox)
    if img.size != ref.size:
        w = min(img.width, ref.width)
        h = min(img.height, ref.height)
        img, ref = img.crop((0, 0, w, h)), ref.crop((0, 0, w, h))
    n = img.width * img.height
    if n <= 0:
        return -1.0
    d = ImageChops.difference(img, ref).split()
    mask = ImageChops.lighter(ImageChops.lighter(
        d[0].point(lambda v: 255 if v > thr else 0),
        d[1].point(lambda v: 255 if v > thr else 0)),
        d[2].point(lambda v: 255 if v > thr else 0))
    return mask.histogram()[255] / n


def rects_minus(base, holes):
    """矩形减法: base 内不属于任一 hole 的部分(用于计算 vacated 区域)."""
    bx, by, bw, bh = base
    pieces = [(bx, by, bw, bh)]
    for hx, hy, hw, hh in holes:
        nxt = []
        for px, py, pw, ph in pieces:
            ix1, iy1 = max(px, hx), max(py, hy)
            ix2, iy2 = min(px + pw, hx + hw), min(py + ph, hy + hh)
            if ix2 <= ix1 or iy2 <= iy1:
                nxt.append((px, py, pw, ph))
                continue
            # 挖掉交集, 保留四周
            if py < iy1:
                nxt.append((px, py, pw, iy1 - py))
            if iy2 < py + ph:
                nxt.append((px, iy2, pw, py + ph - iy2))
            if px < ix1:
                nxt.append((px, iy1, ix1 - px, iy2 - iy1))
            if ix2 < px + pw:
                nxt.append((ix2, iy1, px + pw - ix2, iy2 - iy1))
        pieces = nxt
    return [(x, y, w, h) for x, y, w, h in pieces if w > 0 and h > 0]


def union_rect(rects):
    x1 = min(r[0] for r in rects)
    y1 = min(r[1] for r in rects)
    x2 = max(r[0] + r[2] for r in rects)
    y2 = max(r[1] + r[3] for r in rects)
    return x1, y1, x2 - x1, y2 - y1


def clip_rect(r, bounds):
    rx, ry, rw, rh = r
    bx, by, bw, bh = bounds
    x1, y1 = max(rx, bx), max(ry, by)
    x2, y2 = min(rx + rw, bx + bw), min(ry + rh, by + bh)
    return (x1, y1, x2 - x1, y2 - y1) if x2 > x1 and y2 > y1 else None


class GhostProbe:
    """残影探针主体: 装置/全屏视频窗口 + 脚本化往返 + 帧采样."""

    def __init__(self, args):
        self.args = args
        self.xp = XProbe(os.environ.get('DISPLAY'))       # ctypes 快路径
        self.xd = xdisplay.Display()                      # python-xlib 慢路径
        self.root = self.xd.screen().root
        self.root_w = self.xd.screen().width_in_pixels
        self.root_h = self.xd.screen().height_in_pixels
        self.dev = None
        self.fs_win = None
        self.fs_frame = None
        self.video_frames = []          # (t, 帧号) "视频"重绘时间线
        self._video_stop = threading.Event()
        self.bar_xid = int(open(XID_FILE).read().strip(), 0)
        self.bar = self.xd.create_resource_object('window', self.bar_xid)

    # -------------------------------------------------- X 状态查询
    def redirected(self, xid, what):
        """XComposite NameWindowPixmap: 能命名 => 被重定向(合成器管辖).

        python-xlib 老式绑定: 方法挂在窗口对象上(composite_name_window_pixmap),
        未重定向时 sync 后能取到 BadMatch 类错误."""
        from Xlib.error import CatchError, XError as XLibError
        try:
            w = self.xd.create_resource_object('window', xid)
            err = CatchError(XLibError)
            w.composite_name_window_pixmap(onerror=err)
            self.xd.flush()
            self.xd.sync()
            ok = err.get_error() is None
            print('  [合成状态] %s(0x%x): %s' % (
                what, xid, '已重定向(合成器管辖)' if ok else '未重定向'))
            return ok
        except Exception as e:
            print('  [合成状态] %s(0x%x): 查询异常(%s: %s)' % (
                what, xid, e.__class__.__name__, e))
            return False

    def bar_content(self, w, h):
        """条窗口自身内容: 重定向窗口直抓会 BadMatch, 优先取合成命名 pixmap.

        命名 pixmap 在窗口 resize/remap 后会指向旧后备存储 —— 每趟开始
        释放重取(显式 FreePixmap 防服务器端泄漏), 抓取失败回退直抓."""
        from Xlib.error import CatchError, XError
        pm = None
        try:
            err = CatchError(XError)
            pm = self.bar.composite_name_window_pixmap(onerror=err)
            self.xd.flush()
            self.xd.sync()
            if err.get_error() is not None:
                pm = None
        except Exception:
            pm = None
        old = getattr(self, '_bar_pm', None)
        if old is not None and old is not False:
            try:
                old.free()
            except Exception:
                pass
        self._bar_pm = pm if pm is not None else False
        try:
            if pm is not None:
                return self.xp.grab_pil(pm.id, 0, 0, w, h)
            return self.xp.grab_pil(self.bar_xid, 0, 0, w, h)
        except (RuntimeError, OSError):
            return None

    def set_bypass(self, xid, value):
        """改他人窗口的 _NET_WM_BYPASS_COMPOSITOR (X 允许跨客户写属性)."""
        from Xlib import Xatom
        w = self.xd.create_resource_object('window', xid)
        w.change_property(
            self.xd.intern_atom('_NET_WM_BYPASS_COMPOSITOR'),
            Xatom.CARDINAL, 32, [value])
        self.xd.flush()
        self.xd.sync()

    # -------------------------------------------------- 装置与场景
    def setup_scene(self):
        if not self._ensure_bar_mapped():
            return False
        g = self.bar.get_geometry()
        t = self.bar.translate_coords(self.root, 0, 0)
        self.bar_whw = (g.width, g.height)
        self.bar_orig = (-t.x, -t.y, g.width, g.height)
        print('遮挡条当前矩形: %s' % (self.bar_orig,))

        # A/B: 可经 --ax/--bx 覆盖(默认 A 部分出屏, 复现用户主诉场景)
        bw, bh = g.width, g.height
        self.A = (self.args.ax, self.args.ay)
        self.B = (self.args.bx, self.args.by)
        ra = (self.A[0], self.A[1], bw, bh)
        rb = (self.B[0], self.B[1], bw, bh)
        self.legs = [(ra, rb), (rb, ra)]        # A->B, B->A 交替

        # 装置覆盖 A/B 往返全程外接矩形(屏内部分) + 边距
        ux, uy, uw, uh = union_rect([ra, rb])
        ux, uy = max(0, ux - 40), max(0, uy - 40)
        uw = min(self.root_w - ux, uw + 80)
        uh = min(self.root_h - uy, uh + 80)
        self.union = (ux, uy, uw, uh)

        # 全屏"视频"窗口(受管, 非 override-redirect): 模拟播放器
        if self.args.scenario != 'none':
            self.fs_win = self.root.create_window(
                100, 100, 800, 600, border_width=0, depth=24,
                window_class=X.InputOutput, visual=X.CopyFromParent,
                event_mask=(X.ExposureMask | X.StructureNotifyMask))
            if self.args.scenario == 'fs-bypass1':
                self.set_bypass(self.fs_win.id, 1)
            self.fs_win.map()
            self.xd.sync()
            time.sleep(0.6)
            self._make_fullscreen()
            g = self.fs_win.get_geometry()
            full = g.width >= self.root_w and g.height >= self.root_h
            print('  [全屏视频] 请求全屏结果: %s (%dx%d)' % (full, g.width,
                  g.height))
            if not full:
                return False
            self.redirected(self.fs_win.id, '全屏视频(请求后)')
            self._start_video(self.args.video_fps)

        # 马赛克装置(override-redirect, 压到条正下方); 参考帧解析生成
        self.pat = make_pattern(uw + PERIOD, uh + PERIOD)
        self.union_img_ref = self.pat.crop((0, 0, uw, uh))
        self.dev = self.root.create_window(
            ux, uy, uw, uh, border_width=0, depth=24,
            window_class=X.InputOutput, visual=X.CopyFromParent,
            override_redirect=True, background_pixel=0)
        self.dev.map()
        self.xd.sync()
        time.sleep(0.15)
        self.xp.put_pil(self.dev.id, self.union_img_ref, 24)
        # 压栈(plan §5.11: 必须 stack_mode=Below + sibling, Restack 不可信)
        self.dev.configure(stack_mode=X.Below, sibling=self.bar)
        self.bar.configure(stack_mode=X.Above)
        self.xd.sync()
        time.sleep(0.3)
        # mutter 场景栈序陷阱(本次实证新发现): 对 override-redirect 窗口,
        # 合成器只认"最后映射的在上", 同 sibling 的 restack 请求会被无视
        # —— X 栈序(query_tree)已变, 视觉栈序未变. 必须 unmap+map 条让
        # 它成为最后映射的 OR 窗口, 装置在视觉上才真正在其下方.
        self.bar.unmap()
        self.xd.sync()
        time.sleep(0.15)
        self.bar.map()
        self.xd.sync()
        time.sleep(0.6)

        # 栈序自检1: root 子窗口顺序中 device 必须紧邻 bar 之下
        tree = self.root.query_tree().children
        ids = [w.id for w in tree]
        ok_order = (self.bar_xid in ids and self.dev.id in ids
                    and ids.index(self.dev.id) < ids.index(self.bar_xid))
        print('  [栈序自检] X栈序 device 在 bar 之下: %s' % ok_order)

        # 栈序自检2(视觉, 关键): 条停原位时, root 上条区域必须 ≈ 条自身
        # 内容(mutter 场景里条在装置之上); 若装置盖在条上, 此处差异巨大
        self._move_bar(self.bar_orig[0], self.bar_orig[1])
        time.sleep(0.6)
        bw_content = self.bar_content(self.bar_orig[2], self.bar_orig[3])
        on_top = -1.0
        if bw_content is not None:
            scr = self._grab(self.bar_orig)
            on_top = diff_frac(scr, bw_content, thr=40)
        print('  [栈序自检] 视觉条在上(条区=root内容 vs 条内容差异): '
              '%.4f' % on_top)
        ok_vis = 0 <= on_top < 0.05

        # 像素自检: 条停屏外采装置参考帧, 图案格心必须命中调色板,
        # 并量化"参考帧污染度"
        self._move_bar(-bw - 60, self.A[1])       # 条停屏外
        time.sleep(0.8)
        r1 = self._grab(self.union)
        contam = diff_frac(r1, self.union_img_ref) if r1 else -1.0
        print('  [参考帧] 污染度(残影证据): %.4f' % contam)
        ok_px = self._selfcheck_pattern(r1)
        print('  [像素自检] 装置图案可见: %s' % ok_px)
        self.ref_contam = contam
        return ok_order and ok_px and ok_vis

    def _selfcheck_pattern(self, img):
        """抽查参考帧格心是否命中调色板(证明装置确实在条下方可见)."""
        if img is None:
            return False
        for gy in range(CELL // 2, min(CELL * 3, img.height), CELL):
            for gx in range(CELL // 2, min(CELL * 4, img.width), CELL):
                px = img.getpixel((gx, gy))
                if not any(abs(px[0] - c[0]) < 8 and abs(px[1] - c[1]) < 8
                           and abs(px[2] - c[2]) < 8 for c in PALETTE):
                    return False
        return True

    def _make_fullscreen(self):
        """_NET_WM_STATE ClientMessage 请求全屏, 等待窗口铺满屏幕."""
        wm_state = self.xd.intern_atom('_NET_WM_STATE')
        fs_atom = self.xd.intern_atom('_NET_WM_STATE_FULLSCREEN')
        from Xlib.protocol import event as xevent
        ev = xevent.ClientMessage(
            window=self.fs_win, client_type=wm_state, format=32,
            data=(32, [1, fs_atom, 0, 0, 0]))
        self.root.send_event(ev, event_mask=(
            X.SubstructureRedirectMask | X.SubstructureNotifyMask))
        self.xd.sync()
        for _ in range(40):
            g = self.fs_win.get_geometry()
            if g.width >= self.root_w and g.height >= self.root_h:
                break
            time.sleep(0.1)
        try:
            self.fs_frame = self.fs_win.query_tree().parent
        except Exception:
            self.fs_frame = None
        time.sleep(0.3)

    def _start_video(self, fps):
        """全屏"视频"重绘线程: 预渲染 6 帧轮播, 角落方块编码帧号.

        注意: 视频线程与主采样线程必须各持独立 X 连接(libX11 非线程安全)."""
        w, h = self.root_w, self.root_h
        base = Image.new('RGB', (w, h))
        d = ImageDraw.Draw(base)
        for gy in range(0, h, 160):
            for gx in range(0, w, 160):
                d.rectangle([gx, gy, gx + 158, gy + 158],
                            fill=PALETTE[(gx // 160 + gy // 160) % 8])
        self._video_imgs = []
        for i in range(6):
            f = base.copy()
            d = ImageDraw.Draw(f)
            band = (i * (w // 6)) % w
            d.rectangle([band, 0, min(w, band + 120), h], fill=(255, 255, 0))
            d.rectangle([0, 0, 62, 62], fill=(i * 40, i * 40, i * 40))
            self._video_imgs.append(f)

        def loop():
            vxp = XProbe(os.environ.get('DISPLAY'))   # 独立连接!
            idx = 0
            interval = 1.0 / max(0.5, fps)
            while not self._video_stop.is_set():
                t = time.monotonic()
                vxp.put_pil(self.fs_win.id, self._video_imgs[idx], 24)
                self.video_frames.append((t, idx))
                if len(self.video_frames) > 8000:
                    del self.video_frames[:4000]
                idx = (idx + 1) % len(self._video_imgs)
                self._video_stop.wait(interval)

        threading.Thread(target=loop, name='video', daemon=True).start()

    def _video_frame_at(self, t):
        """t 之后最近一次"视频"重绘的等待时长(ms)与帧号(bisect, 时间单调)."""
        import bisect
        times = [ft for ft, _ in self.video_frames]
        j = bisect.bisect_left(times, t) if times else 0
        if j >= len(self.video_frames):
            return None, None
        ft, i = self.video_frames[j]
        return (ft - t) * 1000.0, i

    # -------------------------------------------------- 移动与抓取
    def _ensure_bar_mapped(self):
        """条必须处于映射态, 否则移动无视觉效果、抓取 BadMatch(全 0 假数据).

        unmapped 且 cfg visible=true 的典型成因是"切换透视"卡在开
        (peek 不改配置) —— 经热键 Ctrl+Alt+O 关闭; 仍无效再试显隐热键."""
        try:
            if self.bar.get_attributes().map_state == 2:
                return True
        except Exception:
            return False
        from Xlib.ext import xtest
        kc_ctrl = self.xd.keysym_to_keycode(0xffe3)
        kc_alt = self.xd.keysym_to_keycode(0xffe9)
        for keysym in (ord('o'), ord('b'), ord('b')):
            kc = self.xd.keysym_to_keycode(keysym)
            xtest.fake_input(self.xd, X.KeyPress, kc_ctrl)
            xtest.fake_input(self.xd, X.KeyPress, kc_alt)
            xtest.fake_input(self.xd, X.KeyPress, kc)
            self.xd.flush()
            time.sleep(0.05)
            xtest.fake_input(self.xd, X.KeyRelease, kc)
            xtest.fake_input(self.xd, X.KeyRelease, kc_alt)
            xtest.fake_input(self.xd, X.KeyRelease, kc_ctrl)
            self.xd.flush()
            time.sleep(0.4)
            try:
                if self.bar.get_attributes().map_state == 2:
                    print('  [自愈] 遮挡条已恢复映射(经热键)')
                    return True
            except Exception:
                pass
        print('  [自愈失败] 遮挡条无法恢复映射, 数据不可信', file=sys.stderr)
        return False

    def _move_bar(self, x, y):
        w, h = self.bar_whw
        self.xp.fx.move_resize_window(self.bar_xid, x, y, w, h)

    def _grab(self, rect):
        try:
            return self.xp.grab_pil(self.xp.fx.root, rect[0], rect[1],
                                    rect[2], rect[3])
        except (RuntimeError, OSError):
            return None

    def _vacated(self, frm, to):
        """本趟移动腾出的区域: 条带横向范围 - 终点矩形.

        只在条带高度内测量(排除装置上下边距, 避免稀释残影占比)."""
        band_y = min(frm[1], to[1])
        band_h = max(frm[1] + frm[3], to[1] + to[3]) - band_y
        band = (self.union[0], band_y, self.union[2], band_h)
        return rects_minus(band, [to])

    def _xtest_drag(self, frm, to, steps=14):
        """XTest 真实拖动(模拟用户): 按下条内一点, 分步移动, 松开.

        走应用的完整交互路径(抓取/solid模式/落盘), 用于验证真实场景.
        按压点必须取条"屏内可见部分"(XTest 指针坐标被钳在屏幕内,
        负坐标会破坏应用基于 root_x 差值的位移计算)."""
        import ctypes
        xtst = ctypes.CDLL('libXtst.so.6')
        xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_ulong]
        xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        fx_dpy = self.xp.fx.dpy
        lib = self.xp.lib

        def move(x, y):
            xtst.XTestFakeMotionEvent(fx_dpy, 0, x, y, 0)
            lib.XFlush(fx_dpy)

        # 按压点: 条局部横向 1/4 处, 根坐标钳进屏内(条屏外时落在可见部分)
        px = max(2, frm[0] + frm[2] // 4)
        py = frm[1] + frm[3] // 2
        move(px, py)
        time.sleep(0.25)
        xtst.XTestFakeButtonEvent(fx_dpy, 1, 1, 0)
        lib.XFlush(fx_dpy)
        time.sleep(0.15)
        for i in range(1, steps + 1):
            move(px + (to[0] - frm[0]) * i // steps,
                 py + (to[1] - frm[1]) * i // steps)
            time.sleep(0.033)
        time.sleep(0.2)
        xtst.XTestFakeButtonEvent(fx_dpy, 1, 0, 0)
        lib.XFlush(fx_dpy)
        time.sleep(0.1)

    def run_wheel_leg(self, idx):
        """滚轮改高度趟: 每步 XMoveResizeWindow 都会重分配条的后备 pixmap
        (mutter 必须重新绑定), 是移动路径没有的固有竞态窗口."""
        import ctypes
        xtst = ctypes.CDLL('libXtst.so.6')
        xtst.XTestFakeMotionEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_ulong]
        xtst.XTestFakeButtonEvent.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
        fx_dpy = self.xp.fx.dpy
        lib = self.xp.lib

        def move(x, y):
            xtst.XTestFakeMotionEvent(fx_dpy, 0, x, y, 0)
            lib.XFlush(fx_dpy)

        def wheel(up):
            xtst.XTestFakeButtonEvent(fx_dpy, 4 if up else 5, 1, 0)
            lib.XFlush(fx_dpy)
            xtst.XTestFakeButtonEvent(fx_dpy, 4 if up else 5, 0, 0)
            lib.XFlush(fx_dpy)

        # 条外部移到装置覆盖区内(参考帧解析生成), 指针停条内远离边缘
        ux, uy, uw, uh = self.union
        w0, h0 = self.bar_orig[2], self.bar_orig[3]
        x = min(ux + 200, ux + uw - w0 - 20)
        y = self.A[1]
        self._move_bar(x, y)
        time.sleep(0.4)
        move(x + w0 // 3, y + h0 // 2)
        time.sleep(0.2)
        # 收缩 8 步(下滚 detail 5, 高 -14/步) -> vacated = 底部条带,
        # 参考帧由装置图案解析生成(该条带此刻被条盖住, 抓不到纯净版)
        h1 = h0 - 8 * 14
        vac = (x, y + h1, w0, h0 - h1)
        ref = self.union_img_ref.crop(
            (vac[0] - ux, vac[1] - uy,
             vac[0] - ux + vac[2], vac[1] - uy + vac[3]))
        for _ in range(8):
            wheel(up=False)
            time.sleep(0.08)
        time.sleep(0.15)
        n_samp = max(3, int(self.args.hold_ms / 30))
        t0 = time.monotonic()
        frames = []
        for i in range(n_samp):
            t = time.monotonic()
            img = self._grab(vac)
            worst = -1.0
            if img is not None:
                worst = diff_frac(img, ref, thr=GHOST_DIFF)
                if self.args.dump_dir and worst > 0.05 and i < 6:
                    try:
                        os.makedirs(self.args.dump_dir, exist_ok=True)
                        img.save('%s/wheel%s_t%d.png' % (
                            self.args.dump_dir, idx, i * 30))
                    except OSError:
                        pass
            frames.append({'t_ms': round((t - t0) * 1000),
                           'ghost_frac': round(worst, 4)})
            nxt = t + 0.03
            slp = nxt - time.monotonic()
            if slp > 0:
                time.sleep(slp)
        clear = next((f['t_ms'] for f in frames
                      if all(g['ghost_frac'] <= GHOST_FRAC
                             for g in frames[frames.index(f):
                                             frames.index(f) + 3])), None)
        ghosted = sum(1 for f in frames if f['ghost_frac'] > GHOST_FRAC)
        # 展开恢复原高度(上滚 detail 4, 高 +14/步)
        for _ in range(8):
            wheel(up=True)
            time.sleep(0.08)
        time.sleep(0.2)
        return {'leg': 'wheel%s' % idx, 'frm': [x, y, w0, h0],
                'to': [x, y, w0, h1],
                'vac_px': w0 * (h0 - h1), 'frames': frames,
                'ghost_frames': ghosted,
                'max_frac': max((f['ghost_frac'] for f in frames), default=0),
                'clear_ms': clear}

    def run_leg(self, idx, frm, to):
        """单趟 A<->B: 移动 + 到达后连续采样 vacated 区域残影."""
        if not self._ensure_bar_mapped():
            return None
        hold = self.args.hold_ms / 1000.0
        step_n = self.args.steps
        self._move_bar(frm[0], frm[1])
        time.sleep(0.35)
        # 移动: 单步跳变 / 多步模拟拖动节奏(33ms/步); xtest=真实拖动路径
        if self.args.mode == 'xtest':
            self._xtest_drag(frm, to)
        elif step_n <= 1:
            self._move_bar(to[0], to[1])
        else:
            for i in range(1, step_n + 1):
                x = frm[0] + (to[0] - frm[0]) * i // step_n
                y = frm[1] + (to[1] - frm[1]) * i // step_n
                self._move_bar(x, y)
                time.sleep(0.033)
        t_arrive = time.monotonic()
        vac = self._vacated(frm, to)
        # 采样矩形 = vacated 的外接矩形(限定 union 内), 减少每帧抓取量
        vrect = clip_rect(union_rect(vac), self.union)
        if vrect is None:
            return None
        ref = self.union_img_ref.crop(
            (vrect[0] - self.union[0], vrect[1] - self.union[1],
             vrect[0] - self.union[0] + vrect[2],
             vrect[1] - self.union[1] + vrect[3]))
        n_samp = max(3, int(self.args.hold_ms / 30))
        frames = []
        for i in range(n_samp):
            t = time.monotonic()
            img = self._grab(vrect)
            worst = -1.0
            if img is not None:
                for (vx, vy, lw, lh) in vac:
                    lx, ly = vx - vrect[0], vy - vrect[1]
                    bb = (lx, ly, lx + lw, ly + lh)
                    worst = max(worst, diff_frac(img, ref, bb))
                # 残影帧留证: 超阈值时保存画面(--dump-dir 开启)
                if self.args.dump_dir and worst > 0.05 and i < 6:
                    try:
                        os.makedirs(self.args.dump_dir, exist_ok=True)
                        img.save('%s/leg%s_t%d.png' % (
                            self.args.dump_dir, idx, i * 30))
                    except OSError:
                        pass
            vwait, vidx = ((None, None) if self.fs_win is None
                           else self._video_frame_at(t))
            frames.append({'t_ms': round((t - t_arrive) * 1000),
                           'ghost_frac': round(worst, 4),
                           'video_wait_ms': (round(vwait) if vwait
                                             is not None else None),
                           'video_idx': vidx})
            nxt = t + 0.03
            slp = nxt - time.monotonic()
            if slp > 0:
                time.sleep(slp)
        # 清除时刻: 连续 3 帧低于判据的首帧时刻
        clear = None
        for j in range(len(frames) - 2):
            if all(f['ghost_frac'] <= GHOST_FRAC for f in frames[j:j + 3]):
                clear = frames[j]['t_ms']
                break
        ghosted = sum(1 for f in frames if f['ghost_frac'] > GHOST_FRAC)
        return {'leg': idx, 'frm': list(frm), 'to': list(to),
                'vac_px': sum(w * h for _, _, w, h in vac),
                'frames': frames,
                'ghost_frames': ghosted,
                'max_frac': max((f['ghost_frac'] for f in frames), default=0),
                'clear_ms': clear}

    # -------------------------------------------------- 主流程
    def run(self):
        reps = self.args.reps
        print('合成状态: 遮挡条窗口:')
        self.redirected(self.bar_xid, '遮挡条')
        if not self.setup_scene():
            self.cleanup()
            return 1
        legs_out = []
        try:
            if self.args.control_ms > 0:
                # 对照组: 条原地不动连续采样 —— 失配/残影指标的本底噪声
                frm = (self.bar_orig[0], self.bar_orig[1],
                       self.bar_orig[2], self.bar_orig[3])
                r = self.run_leg(-1, frm, frm)
                if r:
                    r['leg'] = 'control-no-move'
                    legs_out.append(r)
            if self.args.mode in ('xtest', 'wheel'):
                # 真实交互会经应用 persist 配置: 先备份, 结束恢复
                import json as _json
                self.cfg_path = os.path.expanduser(
                    '~/.config/subtitle-cover/config.json')
                with open(self.cfg_path) as f:
                    self.cfg_backup = f.read()
            if self.args.mode == 'xtest':
                self._move_bar(*self.A)          # 拖动从 A 起
                time.sleep(0.4)
            for i in range(reps):
                if self.args.mode == 'wheel':
                    r = self.run_wheel_leg(i)
                else:
                    frm, to = self.legs[i % len(self.legs)]
                    r = self.run_leg(i, frm, to)
                if r:
                    legs_out.append(r)
                time.sleep(self.args.rest_ms / 1000.0)
        finally:
            self.cleanup()
        if legs_out:
            self.report(legs_out)
        return 0

    def report(self, legs_out):
        n = len(legs_out)
        clears = [l['clear_ms'] for l in legs_out if l['clear_ms'] is not None]
        print('\n========== 汇总 (%s, reps=%d) ==========' %
              (self.args.scenario, n))
        print('出现残影的趟数       : %d/%d' % (
            sum(1 for l in legs_out if l['ghost_frames'] > 0), n))
        print('残影帧总数           : %d (采样 %d 帧/趟)' % (
            sum(l['ghost_frames'] for l in legs_out),
            len(legs_out[0]['frames'])))
        if clears:
            print('残影清除时长(ms)     : min %d / 中位 %d / max %d' % (
                min(clears), sorted(clears)[len(clears) // 2], max(clears)))
        else:
            print('残影清除时长(ms)     : 采样窗内未清除')
        print('max 残影占比          : 中位 %.3f / max %.3f' % (
            sorted(l['max_frac'] for l in legs_out)[n // 2],
            max(l['max_frac'] for l in legs_out)))
        out = {'scenario': self.args.scenario, 'reps': n,
               'steps': self.args.steps,
               'video_fps': self.args.video_fps,
               'ref_contam': getattr(self, 'ref_contam', None),
               'legs': legs_out}
        with open(self.args.out, 'w') as f:
            json.dump(out, f, indent=1)
        print('明细已写 %s' % self.args.out)

    def cleanup(self):
        self._video_stop.set()
        time.sleep(0.2)
        try:
            self._move_bar(*self.bar_orig[:2])       # 恢复条原位
        except Exception:
            pass
        # 真实拖动模式: 恢复被 persist 的配置(xtest 拖动会写当前位置)
        if getattr(self, 'args', None) is not None \
                and self.args.mode in ('xtest', 'wheel') \
                and getattr(self, 'cfg_backup', None) is not None:
            try:
                with open(self.cfg_path, 'w') as f:
                    f.write(self.cfg_backup)
                print('(已恢复拖动测试期间被改写的 config.json)')
            except OSError as e:
                print('config 恢复失败:', e, file=sys.stderr)
        try:
            if self.dev is not None:
                self.dev.destroy()
            if self.fs_win is not None:
                self.fs_win.destroy()
            self.xd.sync()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser(description='残影诊断探针')
    ap.add_argument('--scenario', default='none',
                    choices=['none', 'fs', 'fs-bypass1', 'fs-noredir'])
    ap.add_argument('--reps', type=int, default=20, help='往返趟数')
    ap.add_argument('--steps', type=int, default=1,
                    help='>1 = 分步移动模拟拖动(33ms/步)')
    ap.add_argument('--mode', default='ext', choices=['ext', 'xtest', 'wheel'],
                    help='ext=探针直改条几何(不抢鼠标); '
                         'xtest=XTest 真实拖动(走应用完整路径); wheel=XTest 滚轮改高')
    ap.add_argument('--video-fps', type=float, default=3.0,
                    help='全屏"视频"重绘帧率(故意慢, 便于看清除对齐)')
    ap.add_argument('--hold-ms', type=int, default=1300,
                    help='每趟到达后的采样时长')
    ap.add_argument('--rest-ms', type=int, default=200, help='趟间隔')
    ap.add_argument('--control-ms', type=int, default=0,
                    help='>0 时先跑一次"条不动"对照趟(本底噪声), 时长同 hold')
    ap.add_argument('--ax', type=int, default=-340, help='A 位置 x(物理像素)')
    ap.add_argument('--ay', type=int, default=600, help='A 位置 y')
    ap.add_argument('--bx', type=int, default=360, help='B 位置 x')
    ap.add_argument('--by', type=int, default=600, help='B 位置 y')
    ap.add_argument('--out', default='/tmp/sc_ghost.json')
    ap.add_argument('--dump-dir', default='',
                    help='非空时保存残影帧 PNG 供人工核验')
    args = ap.parse_args()
    return GhostProbe(args).run()


if __name__ == '__main__':
    sys.exit(main())
