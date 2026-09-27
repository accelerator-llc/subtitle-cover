#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""GTK 控制面板: 右键弹出, 调整各种参数."""

import gi  # noqa: F401  (依赖注入已在包 __init__ 完成)
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk, Gdk  # noqa: E402

from ..core.config import MIN_W, MIN_H, MAX_BLUR, MAX_FEATHER


class ControlPanel(Gtk.Window):
    """控制面板窗口. 由 App.toggle_panel() 控制显示/隐藏."""

    def __init__(self, app):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.app = app
        self.set_title('字幕遮挡条 · 控制面板')
        self.set_keep_above(True)
        self.set_resizable(False)
        self.connect('delete-event', self._on_close)

        grid = Gtk.Grid(column_spacing=10, row_spacing=8, margin=12)
        self.add(grid)

        row = 0
        # ---- 显示 / 锁定 ------------------------------------------------
        self.sw_visible = Gtk.Switch()
        self.sw_locked = Gtk.Switch()
        grid.attach(Gtk.Label(label='显示遮挡条', halign=Gtk.Align.END),
                    0, row, 1, 1)
        grid.attach(self.sw_visible, 1, row, 1, 1)
        grid.attach(Gtk.Label(label='锁定 (鼠标穿透)', halign=Gtk.Align.END),
                    2, row, 1, 1)
        grid.attach(self.sw_locked, 3, row, 1, 1)
        self.sw_visible.connect('state-set', self._on_visible)
        self.sw_locked.connect('state-set', self._on_locked)

        # ---- 几何 --------------------------------------------------------
        row += 1
        adj_x = Gtk.Adjustment(value=0, lower=-8192, upper=16384,
                               step_increment=4, page_increment=40)
        adj_y = Gtk.Adjustment(value=0, lower=-8192, upper=16384,
                               step_increment=4, page_increment=40)
        adj_w = Gtk.Adjustment(value=0, lower=MIN_W, upper=7680,
                               step_increment=4, page_increment=80)
        adj_h = Gtk.Adjustment(value=0, lower=MIN_H, upper=2160,
                               step_increment=2, page_increment=20)
        self.spin_x = Gtk.SpinButton(adjustment=adj_x)
        self.spin_y = Gtk.SpinButton(adjustment=adj_y)
        self.spin_w = Gtk.SpinButton(adjustment=adj_w)
        self.spin_h = Gtk.SpinButton(adjustment=adj_h)
        grid.attach(Gtk.Label(label='位置 X/Y (物理像素)',
                              halign=Gtk.Align.END), 0, row, 1, 1)
        hb = Gtk.HBox(spacing=6)
        hb.pack_start(self.spin_x, False, False, 0)
        hb.pack_start(self.spin_y, False, False, 0)
        grid.attach(hb, 1, row, 3, 1)

        row += 1
        grid.attach(Gtk.Label(label='宽 / 高', halign=Gtk.Align.END),
                    0, row, 1, 1)
        hb2 = Gtk.HBox(spacing=6)
        hb2.pack_start(self.spin_w, False, False, 0)
        hb2.pack_start(self.spin_h, False, False, 0)
        grid.attach(hb2, 1, row, 3, 1)
        for s in (self.spin_x, self.spin_y, self.spin_w, self.spin_h):
            s.connect('value-changed', self._on_geometry)
            s.connect('activate', self._on_geometry)

        # ---- 模糊 --------------------------------------------------------
        row += 1
        grid.attach(Gtk.Label(label='模糊强度', halign=Gtk.Align.END),
                    0, row, 1, 1)
        self.scale_blur = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, MAX_BLUR, 1)
        self.scale_blur.set_size_request(220, -1)
        self.scale_blur.set_draw_value(True)
        grid.attach(self.scale_blur, 1, row, 3, 1)
        self.scale_blur.connect('value-changed', self._on_blur)

        # ---- 噪点 / 轮廓提示 ---------------------------------------------
        row += 1
        grid.attach(Gtk.Label(label='磨砂噪点', halign=Gtk.Align.END),
                    0, row, 1, 1)
        self.chk_noise = Gtk.CheckButton()
        grid.attach(self.chk_noise, 1, row, 1, 1)
        grid.attach(Gtk.Label(label='轮廓提示(秒)', halign=Gtk.Align.END),
                    2, row, 1, 1)
        adj_hint = Gtk.Adjustment(value=3, lower=0, upper=10, step_increment=1)
        self.spin_hint = Gtk.SpinButton(adjustment=adj_hint)
        grid.attach(self.spin_hint, 3, row, 1, 1)
        self.chk_noise.connect('toggled', self._on_noise)
        self.spin_hint.connect('value-changed', self._on_hint)

        # ---- 色调 --------------------------------------------------------
        row += 1
        grid.attach(Gtk.Label(label='色调', halign=Gtk.Align.END),
                    0, row, 1, 1)
        self.btn_color = Gtk.ColorButton()
        grid.attach(self.btn_color, 1, row, 1, 1)
        grid.attach(Gtk.Label(label='色调透明度', halign=Gtk.Align.END),
                    2, row, 1, 1)
        self.scale_alpha = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        self.scale_alpha.set_size_request(120, -1)
        self.scale_alpha.set_draw_value(False)
        grid.attach(self.scale_alpha, 3, row, 1, 1)
        self.btn_color.connect('color-set', self._on_color)
        self.scale_alpha.connect('value-changed', self._on_alpha)

        # ---- 位置预设 ------------------------------------------------------
        row += 1
        grid.attach(Gtk.Label(label='位置预设', halign=Gtk.Align.END),
                    0, row, 1, 1)
        self.combo_preset = Gtk.ComboBoxText()
        self.combo_preset.set_size_request(150, -1)
        grid.attach(self.combo_preset, 1, row, 1, 1)
        hb_preset = Gtk.HBox(spacing=4)
        self.btn_preset_save = Gtk.Button(label='存当前')
        self.btn_preset_del = Gtk.Button(label='删除')
        hb_preset.pack_start(self.btn_preset_save, False, False, 0)
        hb_preset.pack_start(self.btn_preset_del, False, False, 0)
        grid.attach(hb_preset, 2, row, 2, 1)
        self.combo_preset.connect('changed', self._on_preset)
        self.btn_preset_save.connect('clicked', self._on_preset_save)
        self.btn_preset_del.connect('clicked', self._on_preset_del)

        # ---- 刷新率 ------------------------------------------------------
        row += 1
        grid.attach(Gtk.Label(label='刷新率 (fps)', halign=Gtk.Align.END),
                    0, row, 1, 1)
        self.scale_fps = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 2, 30, 1)
        self.scale_fps.set_size_request(120, -1)
        self.scale_fps.set_draw_value(False)
        grid.attach(self.scale_fps, 1, row, 1, 1)
        self.scale_fps.connect('value-changed', self._on_fps)

        # ---- 操作说明 ----------------------------------------------------
        row += 1
        tips = ('操作: 左键拖动移动 · 边缘拖拽调大小 · 滚轮调高度 · '
                'Shift+滚轮调宽 · Ctrl+滚轮调模糊\n'
                '中键按住 透视 / 单击锁定 · 右键 开本面板\n'
                '热键: Ctrl+Alt+B 显隐 · Ctrl+Alt+Shift+L 锁定 · '
                'Ctrl+Alt+O 按住透视 · Ctrl+Alt+N 笔记')
        grid.attach(Gtk.Label(label=tips, wrap=True, halign=Gtk.Align.START,
                              valign=Gtk.Align.CENTER), 0, row, 4, 1)

        # ---- 笔记位置 ------------------------------------------------------
        row += 1
        grid.attach(Gtk.Label(label='笔记位置', halign=Gtk.Align.END),
                    0, row, 1, 1)
        import os as _os
        self.fc_notes = Gtk.FileChooserButton(
            title='选择笔记保存文件夹',
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        self.fc_notes.set_current_folder(
            _os.path.expanduser(self.app.cfg.get(
                'notes_dir', '~/.local/share/subtitle-cover')))
        grid.attach(self.fc_notes, 1, row, 3, 1)
        self.fc_notes.connect('file-set', self._on_notes_dir)

        # ---- 按钮 --------------------------------------------------------
        row += 1
        self.btn_note = Gtk.Button(label='📝 快速笔记')
        self.btn_noteview = Gtk.Button(label='查看笔记')
        self.btn_reset = Gtk.Button(label='恢复默认')
        self.btn_quit = Gtk.Button(label='退出程序')
        hb3 = Gtk.HBox(spacing=8)
        hb3.pack_start(self.btn_note, False, False, 0)
        hb3.pack_start(self.btn_noteview, False, False, 0)
        hb3.pack_start(self.btn_reset, False, False, 0)
        hb3.pack_start(self.btn_quit, False, False, 0)
        grid.attach(hb3, 0, row, 4, 1)
        self.btn_note.connect('clicked', self._on_note)
        self.btn_noteview.connect('clicked', self._on_noteview)
        self.btn_reset.connect('clicked', self._on_reset)
        self.btn_quit.connect('clicked', self._on_quit)

        # 注意: 不调用 show_all() —— 由 App.toggle_panel() 控制显示.

    # -------------------------------------------------- 事件回调
    # sync_from_config 刷新控件时会触发 value-changed,
    # 用 _syncing 标志防止"面板刷新 -> 改配置 -> 再刷新"的回环.

    def _synced(self):
        return getattr(self, '_syncing', False)

    def _on_close(self, *a):
        self.hide()
        self.app._update_bar_stacking()   # 面板关闭后遮挡条回栈顶
        return True

    def _on_visible(self, sw, state):
        if not self._synced():
            self.app.set_visible(state)

    def _on_locked(self, sw, state):
        if not self._synced():
            self.app.set_locked(state)

    def _on_geometry(self, *a):
        if self._synced():
            return
        self.app.set_geometry(int(self.spin_x.get_value()),
                              int(self.spin_y.get_value()),
                              int(self.spin_w.get_value()),
                              int(self.spin_h.get_value()))

    def _on_blur(self, scale):
        if not self._synced():
            self.app.set_blur(int(scale.get_value()))

    def _on_color(self, btn):
        if self._synced():
            return
        rgba = btn.get_rgba()
        rgb = (int(rgba.red * 255), int(rgba.green * 255),
               int(rgba.blue * 255))
        self.app.set_tint(rgb)

    def _on_alpha(self, scale):
        if not self._synced():
            self.app.set_tint_alpha(scale.get_value() / 100.0)

    def _on_fps(self, scale):
        if not self._synced():
            self.app.set_fps(int(scale.get_value()))

    def _on_noise(self, chk):
        if not self._synced():
            self.app.set_noise(chk.get_active())

    def _on_hint(self, spin):
        if not self._synced():
            self.app.set_hint_seconds(int(spin.get_value()))

    def _on_preset(self, combo):
        if self._synced():
            return
        name = combo.get_active_text()
        if name:
            self.app.apply_preset(name)

    def _on_preset_save(self, *a):
        name = self.app.save_preset()
        self.sync_presets()

    def _on_preset_del(self, *a):
        name = self.combo_preset.get_active_text()
        if name:
            self.app.delete_preset(name)

    def sync_presets(self):
        """刷新预设下拉与当前选中项."""
        self._syncing = True
        try:
            self.combo_preset.remove_all()
            presets = self.app.cfg.get('presets', {})
            cur = self.app.cfg.get('preset', '')
            active = -1
            for i, name in enumerate(presets):
                self.combo_preset.append_text(name)
                if name == cur:
                    active = i
            if active >= 0:
                self.combo_preset.set_active(active)
        finally:
            self._syncing = False

    def _on_notes_dir(self, btn):
        """更改笔记文件夹; 询问是否迁移现有笔记与截图."""
        import os as _os, shutil
        new_dir = btn.get_file().get_path()
        if not new_dir:
            return
        old_dir = _os.path.expanduser(self.app.cfg.get(
            'notes_dir', '~/.local/share/subtitle-cover'))
        if _os.path.normpath(new_dir) == _os.path.normpath(old_dir):
            return
        old_notes = _os.path.join(old_dir, 'notes.md')
        new_notes = _os.path.join(new_dir, 'notes.md')
        old_shots = _os.path.join(old_dir, 'shots')
        has_old = _os.path.exists(old_notes) or _os.path.isdir(old_shots)
        move = False
        if has_old and not _os.path.exists(new_notes):
            dlg = Gtk.MessageDialog(
                transient_for=self, modal=True,
                message_type=Gtk.MessageType.QUESTION,
                text='是否把现有笔记与截图移动到新位置?')
            dlg.add_buttons('仅更改设置', Gtk.ResponseType.CANCEL,
                            '移动', Gtk.ResponseType.OK)
            move = (dlg.run() == Gtk.ResponseType.OK)
            dlg.destroy()
        self.app.set_notes_dir(new_dir)
        if move:
            _os.makedirs(new_dir, exist_ok=True)
            if _os.path.exists(old_notes):
                shutil.move(old_notes, new_notes)
            if _os.path.isdir(old_shots):
                _os.makedirs(new_shots := _os.path.join(new_dir, 'shots'),
                             exist_ok=True)
                for f in _os.listdir(old_shots):
                    if not _os.path.exists(_os.path.join(new_shots, f)):
                        shutil.move(_os.path.join(old_shots, f), new_shots)

    def _on_note(self, *a):
        self.app.toggle_notes()

    def _on_noteview(self, *a):
        self.app.show_notes()

    def _on_reset(self, *a):
        self.app.reset_default()

    def _on_quit(self, *a):
        self.app.quit()

    # -------------------------------------------------- 状态同步
    def sync_from_config(self):
        """把 app 当前配置反映到面板控件."""
        cfg = self.app.cfg
        self._syncing = True
        try:
            self.sw_visible.set_active(cfg['visible'])
            self.sw_locked.set_active(cfg['locked'])
            self.spin_x.set_value(cfg['x'])
            self.spin_y.set_value(cfg['y'])
            self.spin_w.set_value(cfg['w'])
            self.spin_h.set_value(cfg['h'])
            self.scale_blur.set_value(cfg['blur'])
            self.scale_alpha.set_value(cfg['tint_alpha'] * 100)
            self.scale_fps.set_value(cfg['fps'])
            self.chk_noise.set_active(cfg['noise'])
            self.spin_hint.set_value(cfg.get('hint_seconds', 3))
            r, g, b = cfg['tint_color']
            self.btn_color.set_rgba(Gdk.RGBA(r / 255.0, g / 255.0,
                                             b / 255.0, 1.0))
            self.sync_presets()
        finally:
            self._syncing = False
