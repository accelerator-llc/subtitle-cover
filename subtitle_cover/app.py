#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App: 总调度 —— X11 事件循环 + 渲染线程 + 热键 + 托盘 + 面板 + 笔记."""

import os
import sys
import time

import gi  # noqa: F401
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Gio  # noqa: E402

from Xlib import X, XK  # noqa: E402
from Xlib import display as xdisplay  # noqa: E402
from Xlib.error import XError, DisplayError  # noqa: E402

from .core.config import (DEFAULT_CONFIG, DEFAULT_HOTKEYS, MAX_BLUR,
                          MIN_W, MIN_H, clamp, load_config, save_config)
from .render import RenderThread
from .x11.cover import CoverBar
from .ui.panel import ControlPanel


class App:
    def __init__(self):
        self.cfg = load_config()
        self.running = True
        self.drag = None
        self.last_render_ms = 0
        self.x_id = None
        self.grabbed_keys = []
        self.locked_now = False   # 屏幕(锁屏/挂起)处于锁定状态

        # 中键按住透视: 按下时刻, 用于区分"单击锁定"与"按住透视"
        self._mid_press_time = 0.0

        self.dpy = xdisplay.Display()
        self.screen = self.dpy.screen()
        self.root = self.screen.root
        self.root_w = self.screen.width_in_pixels
        self.root_h = self.screen.height_in_pixels

        self.bar = CoverBar(self.dpy, self.cfg)
        self.x_id = self.bar.win.id
        # 诊断便利: 记录遮挡条 XID 供外部脚本定位
        try:
            with open(os.path.join(os.path.expanduser(
                    '~/.config/subtitle-cover'), 'bar_xid'), 'w') as f:
                f.write(str(self.x_id))
        except OSError:
            pass

        # 托盘(AppIndicator 不可用时自动降级为无托盘)
        from .ui.tray import Tray
        self.tray = Tray(self)

        self._grab_hotkeys()
        self._watch_root_events()

        # X 事件轮询(30ms): 修复主线程 roundtrip 把 X 事件读进内部缓冲
        # 后 fd 监听永不触发、事件永久滞留导致的"全部交互无响应"
        GLib.timeout_add(30, self._poll_x_events)

        # 面板与笔记窗口按需创建
        self.panel = None
        self.notes_popup = None
        self.notes_viewer = None

        self._setup_dbus_screensaver()

        # 初始应用一次配置(锁定状态 / 显示状态)
        # 注意: 此处绝不能调 bar.render —— 渲染线程独占 fastx 连接,
        # 主线程并发渲染会损坏 libX11 状态(曾导致遮挡条全黑)
        self.bar.set_locked(self.cfg['locked'], self.cfg['w'], self.cfg['h'])
        if self.cfg['visible']:
            self.bar.show()
            self.bar.raise_top()

        # 先刷一次下方窗口缓存, 再启动渲染线程
        self.bar.refresh_below()
        self.render_thread = RenderThread(self)
        self.render_thread.start()
        # 周期刷新下方窗口缓存(窗口切换/移动时渲染源跟随)
        GLib.timeout_add(500, self._refresh_below_tick)
        # 维持弹窗栈序: 面板/便签打开期间持续把遮挡条压到其正下方
        GLib.timeout_add(500, self._maintain_stacking)

    # ================================================================ 热键
    def _grab_hotkeys(self):
        """向 X server 注册全局热键 (XGrabKey, 修饰键含 NumLock 变体)."""
        def grab(keysym_char, extra_mods):
            key = self.dpy.keysym_to_keycode(ord(keysym_char))
            if key == 0:
                return
            for numlock_variant in (0, X.Mod2Mask, X.LockMask,
                                    X.Mod2Mask | X.LockMask):
                mods = extra_mods | numlock_variant
                try:
                    self.root.grab_key(key, mods, False,
                                       X.GrabModeAsync, X.GrabModeAsync)
                    self.grabbed_keys.append((key, mods))
                except XError:
                    pass

        hk = self.cfg.get('hotkeys', DEFAULT_HOTKEYS)
        base = X.ControlMask | X.Mod1Mask                       # Ctrl+Alt
        grab(hk.get('show', 'b'), base)
        grab(hk.get('lock', 'l'), base | X.ShiftMask)           # +Shift
        grab(hk.get('note', 'n'), base)                         # 笔记
        # 按住临时透视: Ctrl+Alt+<peek_key> 的按下与松开都需要捕获
        grab(self.cfg.get('peek_key', 'o'), base)
        grab('p', base)   # Ctrl+Alt+P 循环位置预设
        grab(hk.get('panel', 'c'), base)   # Ctrl+Alt+C 控制面板

    def _ungrab_hotkeys(self):
        for key, mods in self.grabbed_keys:
            try:
                self.root.ungrab_key(key, mods)
            except XError:
                pass
        self.grabbed_keys = []

    # ================================================================ 置顶
    def _watch_root_events(self):
        """监听 root 的子窗口结构变化: 有新窗口/窗口移动时把遮挡条压回顶."""
        try:
            self.root.change_attributes(
                event_mask=X.SubstructureNotifyMask | X.KeyPressMask
                | X.KeyReleaseMask)
            self.dpy.flush()
        except XError as e:
            print('无法监听 root 事件(置顶保障降级为定时):', e,
                  file=sys.stderr)

    # ================================================================ X 事件
    def _poll_x_events(self):
        """主动排空 X 事件队列(30ms 轮询).

        不用 fd 监听的原因: 主线程的同步查询(roundtrip)会把同时到达的
        X 事件读进 python-xlib 内部缓冲, 之后 socket 无新数据, fd 监听
        永远不再触发, 事件永久滞留 => 热键/右键全部无响应(程序假死).
        固定间隔主动排水则没有任何滞留可能."""
        if not self.running:
            return False
        try:
            n = self.dpy.pending_events()   # 内部会做一次 send/recv 读 socket
            self._poll_n = getattr(self, '_poll_n', 0) + 1
            import os
            if os.environ.get('SC_DEBUG'):
                # 心跳: 每 ~1.6s 打印一次, 证明主循环活着
                if self._poll_n % 50 == 0:
                    print('[心跳] poll#%d pending=%d' % (self._poll_n, n),
                          file=sys.stderr)
                if n:
                    print('[调试] poll#%d 收到 %d 个事件' % (self._poll_n, n),
                          file=sys.stderr)
            for _ in range(n):
                ev = self.dpy.next_event()
                self._handle_x_event(ev)
        except DisplayError:
            self.quit()
            return False
        except Exception as e:   # 单个事件异常不退出
            print('X 事件异常:', e, file=sys.stderr)
        return True

    def _handle_x_event(self, ev):
        name = ev.__class__.__name__

        if name == 'KeyPress':
            self._on_hotkey(ev)
        elif name == 'KeyRelease':
            self._on_hotkey_release(ev)
        elif name == 'ButtonPress':
            import os
            if os.environ.get('SC_DEBUG'):
                print('[调试] ButtonPress detail=%s win匹配=%s' % (
                    ev.detail, ev.window.id == self.x_id), file=sys.stderr)
            if ev.window.id == self.x_id:
                self._on_button_press(ev)
        elif name == 'ButtonRelease':
            if ev.window.id == self.x_id:
                self._on_button_release(ev)
        elif name == 'MotionNotify':
            if ev.window.id == self.x_id:
                self._on_drag_motion(ev)
        elif name == 'Expose':
            pass  # 内容由渲染线程持续刷新
        elif name in ('CreateNotify', 'MapNotify', 'ConfigureNotify',
                      'ReparentNotify', 'CirculateNotify'):
            # root 上有窗口动作 => 把遮挡条压回顶层(节流 200ms)
            self._ensure_top_soon()

    def _ensure_top_soon(self, throttle_ms=200):
        now = time.monotonic() * 1000
        if now - getattr(self, '_last_raise_ms', 0) < throttle_ms:
            return
        self._last_raise_ms = now
        if self.cfg['visible']:
            self.bar.raise_top()

    def _on_hotkey(self, ev):
        hk = self.cfg.get('hotkeys', DEFAULT_HOTKEYS)
        key_show = self.dpy.keysym_to_keycode(ord(hk.get('show', 'b')))
        key_lock = self.dpy.keysym_to_keycode(ord(hk.get('lock', 'l')))
        key_note = self.dpy.keysym_to_keycode(ord(hk.get('note', 'n')))
        key_peek = self.dpy.keysym_to_keycode(
            ord(self.cfg.get('peek_key', 'o')))
        key_preset = self.dpy.keysym_to_keycode(ord('p'))
        key_panel = self.dpy.keysym_to_keycode(
            ord(hk.get('panel', 'c')))
        if ev.detail == key_preset:
            self.cycle_preset()
        elif ev.detail == key_panel:
            # 面板跟随当前鼠标位置弹出(锁定穿透时也能呼出)
            try:
                qp = self.root.query_pointer()
                self.toggle_panel(qp.root_x, qp.root_y)
            except XError:
                self.toggle_panel()
        elif ev.detail == key_show:
            self.set_visible(not self.cfg['visible'])
        elif ev.detail == key_lock:
            self.set_locked(not self.cfg['locked'])
        elif ev.detail == key_note:
            self.toggle_notes()
        elif ev.detail == key_peek:
            self.bar.peeking = True    # 按住: 渐变到清晰透传

    def _on_hotkey_release(self, ev):
        hk = self.cfg.get('hotkeys', DEFAULT_HOTKEYS)
        key_peek = self.dpy.keysym_to_keycode(
            ord(self.cfg.get('peek_key', 'o')))
        if ev.detail == key_peek:
            self.bar.peeking = False   # 松开: 渐变回磨砂

    # -------------------------------------------------- 鼠标交互
    def _edge_at(self, ev, w, h):
        """判定指针位于哪条可拖拽边缘, 返回边缘标识或 None.

        返回 'left'/'right'/'top'/'bottom'/'tl'/'tr'/'bl'/'br'.
        """
        margin = 16
        x, y = ev.event_x, ev.event_y
        near_l = x <= margin
        near_r = x >= w - margin
        near_t = y <= margin
        near_b = y >= h - margin
        if near_t and near_l:
            return 'tl'
        if near_t and near_r:
            return 'tr'
        if near_b and near_l:
            return 'bl'
        if near_b and near_r:
            return 'br'
        if near_l:
            return 'left'
        if near_r:
            return 'right'
        if near_t:
            return 'top'
        if near_b:
            return 'bottom'
        return None

    def _on_button_press(self, ev):
        if self.cfg['locked']:
            return
        detail = ev.detail
        if detail == 1:
            # 左键: 边缘 => 调整大小; 中部 => 拖动移动
            # 交互期间渲染切纯色+轮廓, 暂停实时模糊
            self.bar.interactive = True
            g = self.bar._wh
            edge = self._edge_at(ev, g[0], g[1])
            if edge:
                self.resize = {
                    'edge': edge,
                    'start_x': ev.root_x, 'start_y': ev.root_y,
                    'x': self.cfg['x'], 'y': self.cfg['y'],
                    'w': self.cfg['w'], 'h': self.cfg['h'],
                }
                try:
                    cursor = self.bar.cursor_resize.get(
                        'v' if edge in ('left', 'right', 'tl', 'br')
                        else 'h', self.bar.cursor_move)
                    self.bar.win.grab_pointer(
                        False,
                        X.ButtonReleaseMask | X.PointerMotionMask |
                        X.ButtonPressMask,
                        X.GrabModeAsync, X.GrabModeAsync, X.NONE,
                        cursor, X.CurrentTime)
                except XError:
                    pass
            else:
                self.resize = None
                self.drag = (ev.root_x, ev.root_y,
                             self.cfg['x'], self.cfg['y'])
                try:
                    self.bar.win.grab_pointer(
                        False,
                        X.ButtonReleaseMask | X.PointerMotionMask |
                        X.ButtonPressMask,
                        X.GrabModeAsync, X.GrabModeAsync, X.NONE,
                        self.bar.cursor_move, X.CurrentTime)
                except XError:
                    pass
        elif detail == 2:
            # 中键: 按住临时透视; 快速单击则切换锁定
            self._mid_press_time = time.monotonic()
            self.bar.peeking = True
        elif detail == 3:
            # 右键: 控制面板
            self.toggle_panel(ev.root_x, ev.root_y)
        elif detail in (4, 5):
            # 滚轮: 高度 / Shift:宽度 / Ctrl:模糊度
            step = -1 if detail == 4 else 1
            if ev.state & X.ControlMask:
                self.set_blur(self.cfg['blur'] - step * 2)
            elif ev.state & X.ShiftMask:
                self.set_geometry(self.cfg['x'], self.cfg['y'],
                                  self.cfg['w'] - step * 40, self.cfg['h'])
            else:
                self.set_geometry(self.cfg['x'], self.cfg['y'],
                                  self.cfg['w'], self.cfg['h'] - step * 14)

    def _on_button_release(self, ev):
        if ev.detail == 1:
            self.bar.interactive = False
            # 释放后触发轮廓提示: 视线从纯色+轮廓自然过渡回磨砂
            if self.cfg['visible'] and not self.locked_now:
                self.bar.show_hint()
            if getattr(self, 'resize', None):
                self.resize = None
                save_config(self.cfg)
                if self.panel:
                    self.panel.sync_from_config()
                try:
                    self.dpy.ungrab_pointer(X.CurrentTime)
                except XError:
                    pass
                return
            if self.drag is None:
                return
            self.drag = None
            try:
                self.dpy.ungrab_pointer(X.CurrentTime)
            except XError:
                pass
            self.bar.win.change_attributes(cursor=self.bar.cursor_normal)
            save_config(self.cfg)
            if self.panel:
                self.panel.sync_from_config()
        elif ev.detail == 2:
            # 中键松开: 结束透视; 按住 <300ms 视为单击 => 切换锁定
            self.bar.peeking = False
            if self._mid_press_time and \
                    time.monotonic() - self._mid_press_time < 0.3:
                self.set_locked(not self.cfg['locked'])
            self._mid_press_time = 0.0

    def _on_drag_motion(self, ev):
        if self.cfg['locked']:
            return
        # 调整大小
        rz = getattr(self, 'resize', None)
        if rz:
            dx = ev.root_x - rz['start_x']
            dy = ev.root_y - rz['start_y']
            x, y, w, h = rz['x'], rz['y'], rz['w'], rz['h']
            edge = rz['edge']
            if 'l' in edge and edge != 'right':
                nx = x + dx
                nw = w - dx
            elif 'r' in edge:
                nw = w + dx
                nx = x
            else:
                nx, nw = x, w
            if 't' in edge and edge != 'bottom':
                ny = y + dy
                nh = h - dy
            elif 'b' in edge:
                nh = h + dy
                ny = y
            else:
                ny, nh = y, h
            # 防止翻转
            if nw < MIN_W:
                if 'l' in edge and edge != 'right':
                    nx = x + w - MIN_W
                nw = MIN_W
            if nh < MIN_H:
                if 't' in edge and edge != 'bottom':
                    ny = y + h - MIN_H
                nh = MIN_H
            self.cfg['x'], self.cfg['y'] = nx, ny
            self.cfg['w'], self.cfg['h'] = nw, nh
            self.bar.request_geometry(nx, ny, nw, nh)
            self.bar.set_locked(self.cfg['locked'], nw, nh)
            self._ensure_top_soon(throttle_ms=80)
            return
        # 拖动移动
        if not self.drag:
            return
        dx = ev.root_x - self.drag[0]
        dy = ev.root_y - self.drag[1]
        nx = self.drag[2] + dx
        ny = self.drag[3] + dy
        self.cfg['x'] = nx
        self.cfg['y'] = ny
        self.bar.request_geometry(nx, ny, self.cfg['w'], self.cfg['h'])
        self._ensure_top_soon(throttle_ms=80)

    # -------------------------------------------------- 状态修改入口
    def set_visible(self, visible):
        self.cfg['visible'] = bool(visible)
        save_config(self.cfg)
        if visible and not self.locked_now:
            self.bar.show()
            self.bar.raise_top()
            self._last_raise_ms = time.monotonic() * 1000
        else:
            self.bar.hide()
        self.tray.sync()
        if self.panel:
            self.panel.sync_from_config()

    def set_locked(self, locked):
        self.cfg['locked'] = bool(locked)
        save_config(self.cfg)
        self.bar.set_locked(self.cfg['locked'], self.cfg['w'], self.cfg['h'])
        self._update_bar_opacity()
        self.tray.sync()
        if self.panel:
            self.panel.sync_from_config()

    def set_geometry(self, x, y, w, h):
        self.cfg['x'], self.cfg['y'] = int(x), int(y)
        self.cfg['w'], self.cfg['h'] = int(w), int(h)
        self.bar.request_geometry(self.cfg['x'], self.cfg['y'],
                                  self.cfg['w'], self.cfg['h'])
        # 尺寸变化后同步 X input region, 否则新增区域会错误穿透
        self.bar.set_locked(self.cfg['locked'], self.cfg['w'], self.cfg['h'])
        save_config(self.cfg)

    def set_blur(self, blur):
        self.cfg['blur'] = clamp(int(blur), 0, MAX_BLUR)
        save_config(self.cfg)
        if self.panel:
            self.panel.sync_from_config()

    def set_feather(self, feather):
        self.cfg['feather'] = clamp(int(feather), 0, 64)
        save_config(self.cfg)

    def set_tint(self, rgb):
        self.cfg['tint_color'] = [int(c) for c in rgb]
        self.bar.set_background_color(self.cfg['tint_color'])
        save_config(self.cfg)

    def set_tint_alpha(self, alpha):
        self.cfg['tint_alpha'] = clamp(float(alpha), 0.0, 1.0)
        save_config(self.cfg)

    def set_fps(self, fps):
        self.cfg['fps'] = clamp(int(fps), 2, 60)
        save_config(self.cfg)

    def set_hint_seconds(self, seconds):
        self.cfg['hint_seconds'] = clamp(int(seconds), 0, 10)
        save_config(self.cfg)

    def set_noise(self, noise):
        self.cfg['noise'] = bool(noise)
        save_config(self.cfg)

    def on_panel_closed(self):
        """面板关闭(标题栏按钮/Esc)时遮挡条回栈顶."""
        self.bar.raise_top()

    # -------------------------------------------------- 位置预设
    def save_preset(self, name=None):
        """把当前遮挡条几何保存为预设, 缺省按尺寸自动命名."""
        c = self.cfg
        name = name or '%dx%d @%d,%d' % (c['w'], c['h'], c['x'], c['y'])
        c.setdefault('presets', {})[name] = {
            'x': c['x'], 'y': c['y'], 'w': c['w'], 'h': c['h']}
        c['preset'] = name
        save_config(self.cfg)
        if self.panel:
            self.panel.sync_presets()
        return name

    def apply_preset(self, name):
        p = self.cfg.get('presets', {}).get(name)
        if not p:
            return False
        self.cfg['preset'] = name
        self.set_geometry(p['x'], p['y'], p['w'], p['h'])
        # 切换预设后条会跳到新位置, 触发轮廓提示帮助定位
        if self.cfg['visible']:
            self.bar.show_hint()
        return True

    def delete_preset(self, name):
        self.cfg.get('presets', {}).pop(name, None)
        if self.cfg.get('preset') == name:
            self.cfg['preset'] = ''
        save_config(self.cfg)
        if self.panel:
            self.panel.sync_presets()

    def cycle_preset(self):
        """热键: 在预设间循环切换."""
        names = list(self.cfg.get('presets', {}).keys())
        if not names:
            return
        cur = self.cfg.get('preset', '')
        try:
            nxt = names[(names.index(cur) + 1) % len(names)]
        except ValueError:
            nxt = names[0]
        self.apply_preset(nxt)

    def reset_default(self):
        for k in DEFAULT_CONFIG:
            if k not in ('x', 'y', 'visible'):
                self.cfg[k] = DEFAULT_CONFIG[k]
        save_config(self.cfg)
        self.bar.set_locked(self.cfg['locked'], self.cfg['w'], self.cfg['h'])
        if self.panel:
            self.panel.sync_from_config()

    # -------------------------------------------------- 弹窗与遮挡条的栈序
    def toggle_panel(self, root_x=None, root_y=None):
        import os
        if os.environ.get('SC_DEBUG'):
            print('[调试] toggle_panel 进入', file=sys.stderr)
        if self.panel is None:
            self.panel = ControlPanel(self)
        if self.panel.get_visible():
            # 已打开: 提到最前(右键不再翻转开关, 避免连续右键把面板关掉)
            self.panel.present()
            GLib.timeout_add(120, self._update_bar_stacking)
            return
        self.panel.sync_from_config()
        # 打开面板: 不隐藏遮挡条 —— 用户需要边看面板边拖动调整位置.
        scale = max(1, self.root_w / max(1, self._gtk_screen_width()))
        self.panel.move(int(gx), int(gy))
        self.panel.show_all()
        self.panel.present()
        # 面板弹出后把遮挡条压到面板正下方(仍高于其他所有窗口):
        # 面板完整可见, 遮挡效果保持, 且可以实时拖动调整
        GLib.timeout_add(120, self._update_bar_stacking)

    def _update_bar_stacking(self):
        """面板/便签可见时, 把遮挡条压到其正下方 —— 面板完整可见、
        用户可同时拖动遮挡条调整位置, 遮挡条对其他窗口的置顶保持;
        弹窗全部关闭后回栈顶.

        sibling 必须与遮挡条同为 root 的子窗口: 被 mutter reparent
        进 frame 的窗口要取其 frame, 否则 BadMatch."""
        visible = [p for p in (self.panel, self.notes_popup, self.notes_viewer)
                   if p is not None and p.get_visible()]
        try:
            if visible:
                top = visible[-1]   # 最近弹出的弹窗
                xid = top.get_window().get_xid()
                xw = self.dpy.create_resource_object('window', xid)
                parent = xw.query_tree().parent
                sibling = xid if parent.id == self.root.id else parent.id
                self.bar.win.configure(stack_mode=X.Below, sibling=sibling)
            else:
                self.bar.raise_top()
            self._update_bar_opacity()
            self.dpy.flush()
        except XError:
            pass

    def _update_bar_opacity(self):
        """遮挡条整窗透明度: 锁定(穿透)时 70%, 弹窗打开时 55%, 平时 100%.
        让"锁定中"有肉眼可见的状态区分, 避免用户忘记自己锁过."""
        if self.cfg.get('locked'):
            self.bar.set_opacity(0.7)
        elif any(p is not None and p.get_visible()
                 for p in (self.panel, self.notes_popup, self.notes_viewer)):
            self.bar.set_opacity(0.55)
        else:
            self.bar.set_opacity(1.0)

    def _maintain_stacking(self):
        """周期性维持弹窗栈序(mutter 激活弹窗时会 raise 它)."""
        try:
            self._update_bar_stacking()
        except XError:
            pass
        return True

    def _gtk_screen_width(self):
        """GTK 逻辑屏幕宽(GDK 在 HiDPI 下报逻辑分辨率)."""
        d = Gdk.Display.get_default()
        m = d.get_monitor(0)
        if m is not None:
            return m.get_geometry().width
        return self.root_w // 2

    # -------------------------------------------------- 快速笔记
    def notes_dir(self):
        """当前笔记文件夹(绝对路径)."""
        return os.path.expanduser(self.cfg.get(
            'notes_dir', '~/.local/share/subtitle-cover'))

    def notes_file(self):
        return os.path.join(self.notes_dir(), 'notes.md')

    def set_notes_dir(self, path):
        """设置笔记文件夹(仅写配置; 迁移由调用方负责)."""
        self.cfg['notes_dir'] = path
        save_config(self.cfg)

    def toggle_notes(self):
        if self.notes_popup is not None and self.notes_popup.get_visible():
            self.notes_popup.destroy()
            self.notes_popup = None
            return
        from .ui.notes import NotesPopup
        self.notes_popup = NotesPopup(self)
        pos = self._place_on_bar_side()
        if pos and self.notes_popup is not None:
            self.notes_popup.move(int(pos[0]), int(pos[1]))
    
    def show_notes(self):
        if self.notes_viewer is not None and self.notes_viewer.get_visible():
            self.notes_viewer.present()
            return
        from .ui.notes import NotesViewer
        self.notes_viewer = NotesViewer(self)

    def _place_on_bar_side(self):
        """把笔记便签摆在遮挡条附近(GTK 逻辑坐标)."""
        try:
            scale = max(1, self.root_w / max(1, self._gtk_screen_width()))
            bx = self.cfg['x'] // scale
            by = (self.cfg['y'] + self.cfg['h']) // scale
            # 便签放到遮挡条下方, 超出屏幕则放到上方
            scr_h = self._gtk_screen_height()
            if by + 180 > scr_h:
                by = max(0, self.cfg['y'] // scale - 190)
            Gdk.flush()
            return bx, by
        except Exception:
            return None, None

    def _gtk_screen_height(self):
        d = Gdk.Display.get_default()
        m = d.get_monitor(0)
        if m is not None:
            return m.get_geometry().height
        return self.root_h // 2

    # -------------------------------------------------- 锁屏 / 挂起
    def _setup_dbus_screensaver(self):
        """监听 GNOME 锁屏与 logind 挂起, 锁定期间隐藏遮挡条."""
        try:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            bus.signal_subscribe(
                None, 'org.gnome.ScreenSaver', 'ActiveChanged',
                None, None, Gio.DBusSignalFlags.NONE,
                self._on_screensaver)
            bus.signal_subscribe(
                None, 'org.freedesktop.login1', 'PrepareForSleep',
                None, None, Gio.DBusSignalFlags.NONE,
                self._on_prepare_sleep)
            bus.signal_subscribe(
                None, 'org.freedesktop.ScreenSaver', 'ActiveChanged',
                None, None, Gio.DBusSignalFlags.NONE,
                self._on_screensaver)
        except Exception as e:
            print('锁屏监听不可用(不影响使用):', e, file=sys.stderr)

    def _on_screensaver(self, conn, sender, path, iface, signal, params):
        active = bool(params.unpack()[0])
        if active:
            self.on_screen_locked()
        else:
            self.on_screen_unlocked()

    def _on_prepare_sleep(self, conn, sender, path, iface, signal, params):
        suspending = bool(params.unpack()[0])
        if suspending:
            self.on_screen_locked()
        else:
            self.on_screen_unlocked()

    def on_screen_locked(self):
        """锁屏/挂起: 隐藏遮挡条(满足"非锁屏状态"的要求)."""
        self.locked_now = True
        self.bar.hide()
        if self.panel:
            self.panel.hide()

    def on_screen_unlocked(self):
        self.locked_now = False
        if self.cfg['visible']:
            self.bar.show()
            self.bar.raise_top()

    # -------------------------------------------------- 生命周期
    def _refresh_below_tick(self):
        try:
            self.bar.refresh_below()
        except XError:
            pass
        return True

    def quit(self):
        self.running = False
        save_config(self.cfg)
        if getattr(self, 'render_thread', None) is not None:
            self.render_thread.stop_flag = True
        try:
            self.bar.hide()
            self._ungrab_hotkeys()
        except XError:
            pass
        Gtk.main_quit()

    def run(self):
        Gtk.main()
