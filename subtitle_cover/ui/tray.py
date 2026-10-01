#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""系统托盘图标 (AppIndicator).

提供常驻入口: 显示/隐藏、锁定、控制面板、快速笔记、退出.
图标为运行时生成的简单 PNG(遮挡条示意), 无需外部资源.
优先使用 AyatanaAppIndicator3, 回退 AppIndicator3;
两者都不可用时静默降级(仅热键/面板操作).
"""

import os

import gi
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    try:
        gi.require_version('AppIndicator3', '0.1')
        from gi.repository import AppIndicator3 as AppIndicator
    except (ValueError, ImportError):
        AppIndicator = None

from gi.repository import Gtk  # noqa: E402

from ..core.config import CONFIG_DIR


def _make_icon(path):
    """用 PIL 生成托盘图标: 圆角矩形 + 磨砂横条示意."""
    try:
        from PIL import Image, ImageDraw
        img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        # 外框(圆角矩形)表示屏幕
        d.rounded_rectangle([4, 10, 60, 54], radius=8,
                            outline=(80, 80, 90, 255), width=3)
        # 中部磨砂条(渐变横条)
        for i in range(6):
            alpha = 200 - i * 25
            d.rectangle([12, 26 + i * 2, 52, 28 + i * 2],
                        fill=(120, 160, 210, alpha))
        img.save(path)
        return True
    except Exception:
        return False


class Tray:
    """托盘图标与菜单."""

    def __init__(self, app):
        self.app = app
        self.ind = None
        if AppIndicator is None:
            return
        icon_path = os.path.join(CONFIG_DIR, 'icon.png')
        os.makedirs(CONFIG_DIR, exist_ok=True)
        if not os.path.exists(icon_path):
            _make_icon(icon_path)
        self.ind = AppIndicator.Indicator.new(
            'subtitle-cover', icon_path or 'dialog-information',
            AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.ind.set_menu(self._build_menu())

    def _build_menu(self):
        m = Gtk.Menu()
        self.mi_visible = Gtk.CheckMenuItem(label='显示遮挡条 (Ctrl+Alt+B)')
        self.mi_visible.set_active(self.app.cfg['visible'])
        self.mi_visible.connect('toggled', self._on_visible)
        self.mi_input_locked = Gtk.CheckMenuItem(label='锁定 (鼠标穿透, Ctrl+Alt+Shift+L)')
        self.mi_input_locked.set_active(self.app.cfg['locked'])
        self.mi_input_locked.connect('toggled', self._on_input_locked)

        mi_panel = Gtk.MenuItem(label='控制面板')
        mi_panel.connect('activate', self._on_panel)
        mi_note = Gtk.MenuItem(label='快速笔记 (Ctrl+Alt+N)')
        mi_note.connect('activate', self._on_note)
        mi_noteview = Gtk.MenuItem(label='查看笔记')
        mi_noteview.connect('activate', self._on_noteview)
        mi_quit = Gtk.MenuItem(label='退出')
        mi_quit.connect('activate', self._on_quit)

        for item in (self.mi_visible, self.mi_input_locked,
                     Gtk.SeparatorMenuItem(), mi_panel, mi_note,
                     mi_noteview, Gtk.SeparatorMenuItem(), mi_quit):
            m.append(item)
        m.show_all()
        return m

    # -------------------------------------------------- 菜单回调
    def _on_visible(self, item):
        self.app.set_visible(item.get_active())

    def _on_input_locked(self, item):
        self.app.set_input_locked(item.get_active())

    def _on_panel(self, *a):
        self.app.toggle_panel()

    def _on_note(self, *a):
        self.app.toggle_notes()

    def _on_noteview(self, *a):
        self.app.show_notes()

    def _on_quit(self, *a):
        self.app.quit()

    # -------------------------------------------------- 状态同步
    def sync(self):
        """把配置状态反映到菜单勾选(主线程调用)."""
        if self.ind is None:
            return
        try:
            self.mi_visible.set_active(self.app.cfg['visible'])
            self.mi_input_locked.set_active(self.app.cfg['locked'])
        except AttributeError:
            pass

    def bump(self):
        """笔记已保存的轻提示(短暂切换图标状态)."""
        if self.ind is None:
            return
        try:
            # 用 attention 状态闪一下, 1.2 秒后恢复由下次 sync 完成
            self.ind.set_status(AppIndicator.IndicatorStatus.ATTENTION)
            from gi.repository import GLib
            GLib.timeout_add(1200, self._restore_status)
        except Exception:
            pass

    def _restore_status(self):
        if self.ind is not None:
            self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        return False

    @property
    def available(self):
        return self.ind is not None
