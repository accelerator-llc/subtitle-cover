#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""快速笔记: 便签弹窗 + 笔记查看器.

笔记以 Markdown 形式追加保存, 可选附带当前遮挡条区域的截图
(由渲染线程的实时抓取帧提供, 无额外屏幕抓取开销).
"""

import os
import time

import gi  # noqa: F401
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk  # noqa: E402



class NotesPopup(Gtk.Window):
    """快速笔记便签: 输入回车即保存."""

    def __init__(self, app):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.app = app
        self.set_title('快速笔记')
        self.set_keep_above(True)
        self.set_resizable(False)
        self.set_default_size(420, 120)
        self.connect('delete-event', self._on_close)

        box = Gtk.VBox(spacing=6, margin=10)
        self.add(box)

        self.chk_shot = Gtk.CheckButton(
            label='附带当前画面截图' if self.app.cfg.get('note_with_shot', True)
            else '附带当前画面截图')
        self.chk_shot.set_active(
            bool(self.app.cfg.get('note_with_shot', True)))
        box.pack_start(self.chk_shot, False, False, 0)

        self.txt = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR)
        scroll = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        scroll.set_size_request(-1, 60)
        scroll.add(self.txt)
        box.pack_start(scroll, True, True, 0)

        hb = Gtk.HBox(spacing=6)
        btn_save = Gtk.Button(label='保存 (Ctrl+S/Enter)')
        btn_close = Gtk.Button(label='关闭 (Esc)')
        btn_save.connect('clicked', lambda b: self._save())
        btn_close.connect('clicked', lambda b: (self.app.notes_popup and
                                                self.app.notes_popup.destroy()))
        hb.pack_start(btn_save, False, False, 0)
        hb.pack_start(btn_close, False, False, 0)
        box.pack_start(hb, False, False, 0)

        self.hint = Gtk.Label(
            label='Enter 换行 · Ctrl+S/Ctrl+Enter 保存 · Esc 关闭',
            halign=Gtk.Align.START)
        self.hint.get_style_context().add_class('dim-label')
        box.pack_start(self.hint, False, False, 0)

        self.connect('key-press-event', self._on_key)
        self.show_all()
        self.present()          # 抢键盘焦点, 保证打字直接进入便签
        self.txt.grab_focus()

    def _on_key(self, w, event):
        """键位约定: Enter 换行(TextView 默认); Ctrl+Enter 保存并清空;
        Esc 关闭. 其余按键不拦截."""
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) \
                and event.state & Gdk.ModifierType.CONTROL_MASK:
            self._save()
            self.txt.get_buffer().set_text('')
            return True
        if event.keyval == Gdk.KEY_s and \
                event.state & Gdk.ModifierType.CONTROL_MASK:
            self._save()               # Ctrl+S 保存(不清空, 可继续编辑)
            return True
        if event.keyval == Gdk.KEY_Escape:
            self.app.notes_popup = None
            self.destroy()
            self.app._update_bar_stacking()
            return True
        return False

    def _notes_file(self):
        return self.app.notes_file()

    def _shots_dir(self):
        return os.path.join(self.app.notes_dir(), 'shots')

    def _save(self):
        buf = self.txt.get_buffer()
        text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(),
                            False).strip()
        if not text:
            return
        shot = None
        if self.chk_shot.get_active():
            shots_dir = self._shots_dir()
            os.makedirs(shots_dir, exist_ok=True)
            shot = os.path.join(
                shots_dir,
                'note-%s.png' % time.strftime('%Y%m%d-%H%M%S'))
            # 请求渲染线程在下一帧保存当前清晰画面
            self.app.bar.shot_request = shot
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        nfile = self._notes_file()
        os.makedirs(os.path.dirname(nfile), exist_ok=True)
        with open(nfile, 'a', encoding='utf-8') as f:
            if not os.path.exists(nfile) or \
                    os.path.getsize(nfile) == 0:
                f.write('# 字幕遮挡条 · 学习笔记\n')
            f.write('\n## %s\n' % ts)
            for line in text.splitlines():
                f.write('- %s\n' % line)
            if shot:
                # 稍等渲染线程落盘(通常 <100ms), 记录相对引用
                f.write('  ![shot](shots/%s)\n' % os.path.basename(shot))
        # 界面反馈保存位置
        self.hint.set_text('已保存到 ' + self._notes_file())
        if self.app.tray is not None:
            self.app.tray.bump()   # 托盘提示笔记已更新

    def _on_close(self, *a):
        self.app.notes_popup = None
        self.destroy()
        self.app._update_bar_stacking()
        return True


class NotesViewer(Gtk.Window):
    """笔记查看器: 只读展示 Markdown 内容, 支持清空."""

    def __init__(self, app):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)
        self.app = app
        self.set_title('学习笔记')
        self.set_keep_above(True)
        self.set_default_size(520, 420)
        self.connect('delete-event', self._on_close)

        box = Gtk.VBox(spacing=6, margin=10)
        self.add(box)

        scroll = Gtk.ScrolledWindow(shadow_type=Gtk.ShadowType.IN)
        self.view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self.view.get_buffer().set_text(self._load())
        scroll.add(self.view)
        box.pack_start(scroll, True, True, 0)

        hb = Gtk.HBox(spacing=8)
        btn_open = Gtk.Button(label='打开文件')
        btn_folder = Gtk.Button(label='打开所在文件夹')
        btn_clear = Gtk.Button(label='清空笔记')
        btn_open.connect('clicked', self._on_open)
        btn_folder.connect('clicked', self._on_open_folder)
        btn_clear.connect('clicked', self._on_clear)
        hb.pack_start(btn_open, False, False, 0)
        hb.pack_start(btn_folder, False, False, 0)
        hb.pack_start(btn_clear, False, False, 0)
        tip = Gtk.Label(label='笔记与截图保存在 ~/.local/share/subtitle-cover/',
                        halign=Gtk.Align.START)
        tip.get_style_context().add_class('dim-label')
        box.pack_start(tip, False, False, 0)
        box.pack_start(hb, False, False, 0)
        self.show_all()

    def _load(self):
        try:
            with open(self.app.notes_file(), 'r', encoding='utf-8') as f:
                content = f.read()
            return content if content.strip() else '(还没有笔记)'
        except OSError:
            return '(还没有笔记)'

    def _on_open(self, *a):
        p = self.app.notes_file().replace('"', '')
        os.system('xdg-open "%s" &' % p)

    def _on_open_folder(self, *a):
        p = self.app.notes_dir().replace('"', '')
        os.system('xdg-open "%s" &' % p)

    def _on_clear(self, *a):
        dlg = Gtk.MessageDialog(transient_for=self, modal=True,
                                message_type=Gtk.MessageType.QUESTION,
                                text='确定清空全部笔记?')
        dlg.add_buttons('取消', Gtk.ResponseType.CANCEL,
                        '清空', Gtk.ResponseType.OK)
        if dlg.run() == Gtk.ResponseType.OK:
            try:
                os.remove(self.app.notes_file())
            except OSError:
                pass
            self.view.get_buffer().set_text('(还没有笔记)')
        dlg.destroy()

    def _on_close(self, *a):
        self.app.notes_viewer = None
        self.destroy()
        self.app._update_bar_stacking()
        return True
