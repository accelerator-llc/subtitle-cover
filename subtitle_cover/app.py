#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""App: 总调度 —— X11 事件循环 + 状态机 + 快照下发 + 热键 + 托盘 + 面板 + 笔记."""

import os
import sys
import time

import gi  # noqa: F401
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk, GLib, Gio  # noqa: E402

from Xlib import X  # noqa: E402
from Xlib import display as xdisplay  # noqa: E402
from Xlib.error import XError, DisplayError  # noqa: E402

from .core.config import (DEFAULT_CONFIG, DEFAULT_HOTKEYS, MAX_BLUR,
                          MIN_W, MIN_H, clamp, load_config, save_config)
from .x11.cover import CoverBar, RenderSnapshot
from .ui.panel import ControlPanel


class App:
    def __init__(self):
        self.cfg = load_config()
        self.running = True
        self.drag = None
        self.x_id = None
        self.grabbed_keys = []
        self.hotkey_status = {}      # 动作 -> 注册成败(面板展示用)
        self.screen_locked = False   # 屏幕(锁屏/挂起)处于锁定状态
        self._peek_active = False  # 中键/热键按住透视(状态归 App, 经快照下发)
        self._peek_grab = False  # 中键透视进行中(指针已抓取, 待松开恢复)

        self.dpy = xdisplay.Display()
        self.screen = self.dpy.screen()
        self.root = self.screen.root
        self.root_w = self.screen.width_in_pixels
        self.root_h = self.screen.height_in_pixels

        # 加载几何(启动边界: cfg['x'/'y'/'w'/'h'] 下标仅出现在此处与
        # persist); 运行期几何统一持有在不可变元组 self.geo 中.
        # 老配置可能存有屏外坐标(空气墙加入前的遗留), 启动时经
        # _clamp_geo 自愈到屏内并立即落盘.
        x, y, w, h = self.cfg['x'], self.cfg['y'], self.cfg['w'], self.cfg['h']
        first_run = x is None or y is None
        if first_run:
            # 首次运行: 摆到屏幕底部居中, 大约盖住最下方一行字幕
            w = int(self.root_w * 0.72)
            h = 110
            x = (self.root_w - w) // 2
            y = int(self.root_h * 0.80)
        raw_geo = (x, y, w, h)
        x, y, w, h = self._clamp_geo(x, y, w, h)
        self.geo = (x, y, w, h)        # 运行期几何 (x, y, w, h)
        if first_run or (x, y, w, h) != raw_geo:
            self.persist()

        self.bar = CoverBar(self.dpy, *self.geo,
                            tint_color=tuple(self.cfg['tint_color']))
        self.x_id = self.bar.window_id()
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

        # 初始应用一次配置(输入锁定 / 显示状态)
        # 注意: 此处绝不能调 bar.render —— 渲染线程独占 fastx 连接,
        # 主线程并发渲染会损坏 libX11 状态(曾导致遮挡条全黑)
        self.bar.set_input_locked(self.cfg['locked'])
        if self.cfg['visible']:
            self.bar.show()
            self.bar.raise_top()

        # 先刷一次下方窗口缓存, 推送首份渲染快照, 再由 Bar 自持渲染线程
        # (渲染线程收归 x11 层, 不再反向读 App; 见 ARCH D0/D4)
        self.bar.refresh_below()
        self._push_snapshot()
        self.bar.start()
        # 周期刷新下方窗口缓存(窗口切换/移动时渲染源跟随)
        GLib.timeout_add(500, self._refresh_below_tick)
        # 维持弹窗栈序: 面板/便签打开期间持续把遮挡条压到其正下方
        GLib.timeout_add(500, self._maintain_stacking)

    # ================================================================ 热键
    def _grab_hotkeys(self):
        """向 X server 注册全局热键 (XGrabKey, 修饰键含 NumLock 变体).

        注册失败(BadAccess = 组合键已被其他客户端抢占)是异步到达的,
        grab_key 调用点本身不抛错 —— 必须经 onerror=CatchError 收集,
        在 flush+sync 之后逐一检查. 失败的热键记入 self.hotkey_status,
        由面板展示「注册失败(可能被系统占用)」."""
        from Xlib.error import CatchError

        hk = self.cfg.get('hotkeys', DEFAULT_HOTKEYS)
        base = X.ControlMask | X.Mod1Mask                       # Ctrl+Alt
        plan = [('show', hk.get('show', 'b'), base),
                ('lock', hk.get('lock', 'l'), base | X.ShiftMask),
                ('note', hk.get('note', 'n'), base),
                ('peek', self.cfg.get('peek_key', 'o'), base),
                ('preset', hk.get('preset', 'p'), base),
                ('panel', hk.get('panel', 'c'), base)]

        def grab(keysym_char, extra_mods):
            """注册单个热键的全部变体; 返回 (keycode, 各变体错误捕获器)."""
            key = self.dpy.keysym_to_keycode(ord(keysym_char))
            if key == 0:
                return None, []
            errs = []
            for numlock_variant in (0, X.Mod2Mask, X.LockMask,
                                    X.Mod2Mask | X.LockMask):
                err = CatchError(XError)
                self.root.grab_key(key, extra_mods | numlock_variant,
                                   False, X.GrabModeAsync, X.GrabModeAsync,
                                   onerror=err)
                errs.append((key, extra_mods | numlock_variant, err))
            return key, errs

        self.hotkey_status = {}
        for action, keysym_char, extra_mods in plan:
            key, errs = grab(keysym_char, extra_mods)
            if key is None:
                # 键码映射不到(键盘布局异常): 同视为注册失败
                self.hotkey_status[action] = False
                print('热键 %s(%s) 注册失败: 键码映射不到' % (action,
                      keysym_char), file=sys.stderr)
                continue
            self.dpy.flush()
            self.dpy.sync()          # 处理异步到达的 X 错误
            failed = [e for _k, _m, e in errs if e.get_error() is not None]
            # 基础组合键(无 NumLock 变体)失败 => 视为该热键整体失败;
            # 已抢到的变体一并撤销, 避免"开着 NumLock 才生效"的半残热键
            if failed:
                for k, m, e in errs:
                    if e.get_error() is None:
                        try:
                            self.root.ungrab_key(k, m)
                        except XError:
                            pass
                self.dpy.flush()
            self.hotkey_status[action] = not failed
            if failed:
                print('热键 Ctrl+Alt+%s(%s) 注册失败(可能被系统占用)'
                      % (keysym_char.upper(), action), file=sys.stderr)
            else:
                self.grabbed_keys += [(k, m) for k, m, e in errs]

    def hotkey_label(self, action, default_key, shift=False):
        """动作的组合键展示名(面板/托盘共用; 修饰键按实际注册)."""
        hk = self.cfg.get('hotkeys', DEFAULT_HOTKEYS)
        key = (self.cfg.get('peek_key', 'o') if action == 'peek'
               else hk.get(action, default_key))
        return 'Ctrl+Alt+%s%s' % ('Shift+' if shift else '', key.upper())

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
        elif name == 'ButtonPress':
            import os
            if os.environ.get('SC_DEBUG'):
                print('[调试] ButtonPress detail=%s win匹配=%s' % (
                    ev.detail, ev.window.id == self.x_id), file=sys.stderr)
            if ev.window.id == self.x_id:
                self._on_button_press(ev)
        elif name == 'ButtonRelease':
            # 中键透视期间指针被抓取, 松开事件落在抓取窗口(root)上而非
            # 横条 —— 也须接收, 否则透视永远无法恢复(ARCH D5)
            if ev.window.id == self.x_id \
                    or (self._peek_grab and ev.detail == 2):
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
        key_preset = self.dpy.keysym_to_keycode(ord(hk.get('preset', 'p')))
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
            self.set_input_locked(not self.cfg['locked'])
        elif ev.detail == key_note:
            # ev.time 是 X server 侧本次按键的真实时间戳, 作为 user_time
            # 传给弹窗: mutter 焦点防抢占凭它放行聚焦请求
            self.toggle_notes(user_time=ev.time)
        elif ev.detail == key_peek:
            # 切换式透视(拍板): 按一下开、再按关, 不依赖松开事件 ——
            # 松开事件会因修饰键先弹而脱离 grab 投递丢失, 旧"按住式"
            # 一旦丢失即永久卡在透视态(渲染停更的根因链)
            self._set_peek(not self._peek_active, grab=False)

    # -------------------------------------------------- 中键透视(ARCH D5)
    def _set_peek(self, active, grab):
        """透视状态唯一变更入口(状态归 App, 经快照下发; ARCH D1).

        grab=True  中键按住式: 另需指针抓取(松开事件路由依赖)与看门狗;
        grab=False 热键切换式: 无按键可等, 不抓取 —— 开/关都只由
        明确的按键事件驱动, 不存在"等一个可能丢失的事件"的悬挂态.
        透视期间遮挡条整体隐藏(与中键透视一致的"看真实画面"语义)."""
        if active:
            if self._peek_active:
                return
            self._peek_active = True
            self._push_snapshot()
            self.bar.hide()
            if grab:
                # 先置"待恢复"态: 即使抓取失败, 也有看门狗兜底恢复
                self._peek_grab = True
                GLib.timeout_add(1000, self._peek_watchdog_tick)
                try:
                    # owner_events=False: 期间所有指针事件只报给本连接;
                    # 仅订阅 ButtonRelease(按下/移动不投递, 其余窗口不受干扰)
                    status = self.root.grab_pointer(
                        False, X.ButtonReleaseMask,
                        X.GrabModeAsync, X.GrabModeAsync,
                        X.NONE, X.NONE, X.CurrentTime)
                    if status != X.GrabSuccess:
                        print('中键透视指针抓取失败(status=%d), 由看门狗兜底'
                              % status, file=sys.stderr)
                except XError as e:
                    print('中键透视指针抓取异常:', e, file=sys.stderr)
        else:
            self._peek_off()

    def _peek_off(self):
        """结束透视: 解除指针抓取并恢复映射(中键松开/看门狗/热键共用).

        异常路径也必须 ungrab —— 抓取悬挂期间全局 ButtonRelease 都会被
        本连接吞掉, 其他程序无法收到点击."""
        if not self._peek_active and not self._peek_grab:
            return
        self._peek_active = False
        if self._peek_grab:
            self._peek_grab = False
            try:
                self.dpy.ungrab_pointer(X.CurrentTime)
            except XError:
                pass
        self._push_snapshot()
        # 遮挡条带磨砂回归(不触发轮廓提示, 用户刚看过背景)
        if self.cfg['visible'] and not self.screen_locked:
            self.bar.show(hint=False)

    def _peek_watchdog_tick(self):
        """透视看门狗: 松开事件偶发丢失时兜底恢复(plan §5.8 XTest 假丢失).

        每秒核对真实指针按键状态: Button2 已不在按下态即强制 _peek_off;
        仍在按住则续期. 抓取解除/透视结束后自动停止(返回 False)."""
        if not self._peek_grab:
            return False
        try:
            if not self.root.query_pointer().mask & X.Button2Mask:
                self._peek_off()
                return False
        except XError:
            self._peek_off()
            return False
        return True

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
            self.bar.set_interactive(True)
            gx, gy, gw, gh = self.geo
            edge = self._edge_at(ev, gw, gh)
            if edge:
                self.resize = {
                    'edge': edge,
                    'start_x': ev.root_x, 'start_y': ev.root_y,
                    'x': gx, 'y': gy, 'w': gw, 'h': gh,
                }
                # 抓取指针: 缩放期间持续收 Motion/Release
                # (光标形态沿用既有映射: 纵向系边缘用垂直箭头)
                self.bar.grab_pointer(
                    'resize_v' if edge in ('left', 'right', 'tl', 'br')
                    else 'resize_h')
            else:
                self.resize = None
                self.drag = (ev.root_x, ev.root_y, gx, gy)
                self.bar.grab_pointer('move')
        elif detail == 2:
            # 中键按下: 按住透视. 已是透视态(如热键刚开)则忽略;
            # 先取消可能进行中的左键拖动/缩放 —— 透视抓取会替换拖动
            # 抓取, 左键的 Release 将收不到
            if self._peek_active:
                return
            self.drag = None
            self.resize = None
            self.bar.set_interactive(False)
            self._set_peek(True, grab=True)
        elif detail == 3:
            # 右键: 显示面板(只开不关, 关闭用 Esc/标题栏/Ctrl+Alt+C)
            self.show_panel(ev.root_x, ev.root_y)
        elif detail in (4, 5):
            # 滚轮: 高度 / Shift:宽度 / Ctrl:模糊度
            step = -1 if detail == 4 else 1
            if ev.state & X.ControlMask:
                self.set_blur(self.cfg['blur'] - step * 2)
            else:
                gx, gy, gw, gh = self.geo
                if ev.state & X.ShiftMask:
                    self.set_geometry(gx, gy, gw - step * 40, gh)
                else:
                    self.set_geometry(gx, gy, gw, gh - step * 14)

    def _on_button_release(self, ev):
        if ev.detail == 1:
            self.bar.set_interactive(False)
            # 轮廓渐隐提示由纯色模式结束(0.8s 去抖)统一触发
            if getattr(self, 'resize', None):
                self.resize = None
                self.persist()
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
            self.bar.set_cursor('normal')
            self.persist()
            if self.panel:
                self.panel.sync_from_config()
        elif ev.detail == 2:
            # 中键松开: 结束透视, 遮挡条带磨砂回归
            self._peek_off()

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
            self._apply_geo(nx, ny, nw, nh)   # 空气墙: 边缘撞墙
            self._ensure_top_soon(throttle_ms=80)
            return
        # 拖动移动
        if not self.drag:
            return
        dx = ev.root_x - self.drag[0]
        dy = ev.root_y - self.drag[1]
        nx = self.drag[2] + dx
        ny = self.drag[3] + dy
        _x, _y, w, h = self.geo
        self._apply_geo(nx, ny, w, h)     # 空气墙: 拖不出屏幕
        self._ensure_top_soon(throttle_ms=80)

    # -------------------------------------------------- 运行期几何
    def _clamp_geo(self, x, y, w, h):
        """空气墙(唯一边界收敛点): 任何几何来源(拖动/边缘缩放/滚轮/
        面板/预设/启动摆位)统一在此做一次物理像素 clamp.

        * 条完整限制在屏幕显示范围内(x∈[0,root_w-w], y∈[0,root_h-h]);
        * 条宽/高超过屏幕时位置钉在 (0,0)(此时 x/y 无可让空间);
        * 尺寸下限沿用 MIN_W/MIN_H, 上限与 request_geometry 的服务端
          约束一致(宽 ≤ 2×root_w, 高 ≤ root_h), 保证 self.geo 与
          窗口真实几何永不脱节."""
        w = clamp(int(w), MIN_W, max(MIN_W, self.root_w * 2))
        h = clamp(int(h), MIN_H, max(MIN_H, self.root_h))
        if w >= self.root_w or h >= self.root_h:
            return 0, 0, w, h
        return (clamp(int(x), 0, self.root_w - w),
                clamp(int(y), 0, self.root_h - h), w, h)

    def _apply_geo(self, x, y, w, h):
        """运行期几何唯一写入口: 空气墙 clamp 一次 → 同步 self.geo →
        下发条几何 → 推渲染快照. 拖动/缩放等高频路径不在此落盘,
        落盘仍由调用点在交互结束时经 persist() 统一执行."""
        x, y, w, h = self._clamp_geo(x, y, w, h)
        self.geo = (x, y, w, h)
        self.bar.request_geometry(x, y, w, h)
        self._push_snapshot()

    # -------------------------------------------------- 配置持久化
    def persist(self):
        """配置持久化唯一入口; 运行期几何在此唯一写回 cfg (加载/持久化边界).

        Bar/渲染线程一律不落盘; 拖动等高频路径只改内存, 松手时统一落盘."""
        self.cfg['x'], self.cfg['y'], self.cfg['w'], self.cfg['h'] = self.geo
        save_config(self.cfg)

    def _push_snapshot(self):
        """构造不可变渲染快照并原子替换给遮挡条(单一写入点=App, D3/D4)."""
        x, y, _w, _h = self.geo
        self.bar.update_snapshot(RenderSnapshot(
            x=x, y=y,
            blur=self.cfg['blur'],
            tint_color=tuple(self.cfg['tint_color']),
            tint_alpha=self.cfg['tint_alpha'],
            noise=self.cfg['noise'],
            feather=self.cfg['feather'],
            hint_seconds=self.cfg.get('hint_seconds', 3),
            fps=self.cfg.get('fps', 15),
            visible=self.cfg['visible'],
            screen_locked=self.screen_locked,
            peek_active=self._peek_active))

    def _apply(self):
        """配置变更统一出口: 持久化 + 推送渲染快照."""
        self.persist()
        self._push_snapshot()

    # -------------------------------------------------- 状态修改入口
    def set_visible(self, visible):
        self.cfg['visible'] = bool(visible)
        self._apply()
        if visible and not self.screen_locked:
            self.bar.show()
            self.bar.raise_top()
            self._last_raise_ms = time.monotonic() * 1000
        else:
            self.bar.hide()
        self.tray.sync()
        if self.panel:
            self.panel.sync_from_config()

    def set_input_locked(self, input_locked):
        """鼠标穿透锁定开关(config 键名保持 'locked' 兼容旧配置文件)."""
        self.cfg['locked'] = bool(input_locked)
        self.persist()
        self.bar.set_input_locked(self.cfg['locked'])
        self._update_bar_opacity()
        self.tray.sync()
        if self.panel:
            self.panel.sync_from_config()

    def set_geometry(self, x, y, w, h):
        """滚轮/面板/预设共用的几何设置入口; 空气墙收敛在 _apply_geo."""
        self._apply_geo(x, y, w, h)
        # 输入区域结构性跟随几何(无限矩形), resize 后无需重新应用
        self.persist()

    def set_blur(self, blur):
        self.cfg['blur'] = clamp(int(blur), 0, MAX_BLUR)
        self._apply()
        if self.panel:
            self.panel.sync_from_config()

    def set_feather(self, feather):
        self.cfg['feather'] = clamp(int(feather), 0, 64)
        self._apply()

    def set_tint(self, rgb):
        self.cfg['tint_color'] = [int(c) for c in rgb]
        self.bar.set_background_color(self.cfg['tint_color'])
        self._apply()

    def set_tint_alpha(self, alpha):
        self.cfg['tint_alpha'] = clamp(float(alpha), 0.0, 1.0)
        self._apply()

    def set_fps(self, fps):
        self.cfg['fps'] = clamp(int(fps), 2, 60)
        self._apply()

    def set_hint_seconds(self, seconds):
        self.cfg['hint_seconds'] = clamp(int(seconds), 0, 10)
        self._apply()

    def set_noise(self, noise):
        self.cfg['noise'] = bool(noise)
        self._apply()

    def on_panel_closed(self):
        """面板关闭(标题栏按钮/Esc)时遮挡条回栈顶."""
        self.bar.raise_top()

    # -------------------------------------------------- 位置预设
    def save_preset(self, name=None):
        """把当前遮挡条几何保存为预设, 缺省按尺寸自动命名."""
        x, y, w, h = self.geo
        name = name or '%dx%d @%d,%d' % (w, h, x, y)
        self.cfg.setdefault('presets', {})[name] = {
            'x': x, 'y': y, 'w': w, 'h': h}
        self.cfg['preset'] = name
        self.persist()
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
        self.persist()
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
        # 几何的运行期状态同步默认宽高(位置/可见性保持, 与原行为一致:
        # 现存窗口不动, 下次启动生效); 空气墙同点收敛
        x, y, _w, _h = self.geo
        self._apply_geo(x, y, DEFAULT_CONFIG['w'], DEFAULT_CONFIG['h'])
        self.persist()
        if self.panel:
            self.panel.sync_from_config()

    # -------------------------------------------------- 弹窗与遮挡条的栈序
    def toggle_panel(self, root_x=None, root_y=None):
        """Ctrl+Alt+C: 面板开/关切换."""
        if self.panel is not None and self.panel.get_visible():
            self.panel.hide()
            # 面板关闭: 遮挡条回栈顶
            self.bar.raise_top()
            return
        self.show_panel(root_x, root_y)

    def show_panel(self, root_x=None, root_y=None):
        """右键: 显示面板(幂等 —— 已开则提到最前, 永不关闭)."""
        if self.panel is None:
            self.panel = ControlPanel(self)
        # 打开面板: 不隐藏遮挡条 —— 用户需要边看面板边拖动调整位置.
        scale = max(1, self.root_w / max(1, self._gtk_screen_width()))
        if root_x is not None:
            gx = max(8, min(root_x // scale,
                            self._gtk_screen_width() - 440))
            gy = max(8, min(root_y // scale,
                            self._gtk_screen_height() - 200))
        else:
            gx, gy = 60, 60
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
                self.bar.stack_below(sibling)
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
        self.persist()

    def toggle_notes(self, user_time=0):
        """开/关快速笔记便签.

        user_time: 触发本次打开的用户事件时间戳(X 事件 ev.time);
        传给 present_with_time 供 WM 放行焦点(热键唤起无 GTK 事件上下文)."""
        if self.notes_popup is not None:
            try:
                visible = self.notes_popup.get_visible()
            except RuntimeError:
                visible = False         # 引用已失效(控件已销毁)
            if visible:
                self.notes_popup.destroy()
                self.notes_popup = None
                return
            # 不可见(已销毁/隐藏): 先清掉遗留弹窗再重建, 防止孤儿窗口
            # 残留 X 窗口树, 干扰栈序维护与外部按标题查找
            try:
                self.notes_popup.destroy()
            except RuntimeError:
                pass
            self.notes_popup = None
        from .ui.notes import NotesPopup
        self.notes_popup = NotesPopup(self, user_time=user_time)
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
            bx, by, _bw, bh = self.geo
            bx = bx // scale
            by = (by + bh) // scale
            # 便签放到遮挡条下方, 超出屏幕则放到上方
            scr_h = self._gtk_screen_height()
            if by + 180 > scr_h:
                by = max(0, self.geo[1] // scale - 190)
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
        self.screen_locked = True
        self._push_snapshot()      # 渲染线程经快照得知锁屏, 停止刷新
        self.bar.hide()
        if self.panel:
            self.panel.hide()

    def on_screen_unlocked(self):
        self.screen_locked = False
        self._push_snapshot()
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
        self.persist()
        # 先停渲染线程(等待其退出)再做 X 清理, 避免退出期间继续写窗
        self.bar.stop()
        try:
            self.bar.hide()
            self._ungrab_hotkeys()
        except XError:
            pass
        Gtk.main_quit()

    def run(self):
        Gtk.main()
