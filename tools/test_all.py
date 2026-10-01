#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕遮挡条 全功能自动测试 (回归测试工具).

用法: DISPLAY=:1 python3 tools/test_all.py [--no-restart]

测试内容:
  启动/渲染/热键显隐/拖动/边缘缩放/滚轮纯色模式/右键面板(幂等)/
  面板与遮挡条共存/Ctrl+Alt+C 关面板/锁定透明度/笔记弹窗/中键透视/预设切换

设计要点:
  * 测试前把 tint_color 临时改成醒目的红色, 使"纯色模式"可被可靠识别
    (纯色=纯红, 磨砂=背景内容), 测试结束恢复原配置
  * 所有坐标动态获取(从 bar_xid 文件读 XID), 不依赖配置文件位置
  * 每个测试后清理现场(关面板/关笔记/解锁/复位)

注意: 测试会真实移动鼠标并点击(XTest 合成事件), 运行期间请勿操作鼠标键盘.
"""

import os
import re
import subprocess
import sys
import time
import json

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJ, '.deps/extracted/usr/lib/python3/dist-packages'))

from Xlib import X, XK, display as xdisplay          # noqa: E402
from Xlib.ext import xtest                            # noqa: E402

CONF = os.path.expanduser('~/.config/subtitle-cover/config.json')
XID_FILE = os.path.expanduser('~/.config/subtitle-cover/bar_xid')
LOG = '/tmp/sc-test.log'

TEST_TINT = [200, 30, 30]     # 测试用醒目色(纯色模式必须显示这个颜色)
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    print('%-4s %-34s %s' % ('PASS' if ok else 'FAIL', name, detail), flush=True)


def load_cfg():
    with open(CONF) as f:
        return json.load(f)


def save_cfg(cfg):
    with open(CONF, 'w') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class T:
    def __init__(self):
        self.XD = xdisplay.Display()
        self.root = self.XD.screen().root
        self.proc = None
        self.orig_cfg = None

    # -------------------------------------------------- 生命周期
    def start(self, restart=True):
        if restart:
            # 锚定 cmdline 结尾: 进程可能是 `python3 run.py` 或
            # `/usr/bin/python3 run.py`(本测试用 sys.executable 拉起),
            # 锚定开头的 ^python3 匹配不到后者 => 重启成空操作(12.1 假失败)
            subprocess.run(['pkill', '-f', r'run\.py$'], check=False)
            time.sleep(1.5)
            env = dict(os.environ)
            env['DISPLAY'] = os.environ.get('DISPLAY', ':1')
            log = open(LOG, 'w')
            self.proc = subprocess.Popen(
                [sys.executable, 'run.py'], cwd=PROJ, env=env,
                stdout=log, stderr=subprocess.STDOUT)
            time.sleep(4.5)
        # 读取遮挡条 XID
        for _ in range(20):
            try:
                self.bar_xid = int(open(XID_FILE).read().strip())
                break
            except (OSError, ValueError):
                time.sleep(0.2)
        else:
            raise RuntimeError('无法读取遮挡条 XID')

    def stop(self):
        subprocess.run(['pkill', '-f', r'run\.py$'], check=False)

    # -------------------------------------------------- 基础查询
    def cfg(self):
        return load_cfg()

    def bar(self):
        return self.XD.create_resource_object('window', self.bar_xid)

    def bar_geom(self):
        w = self.bar()
        g = w.get_geometry()
        t = w.translate_coords(self.root, 0, 0)
        return (-t.x, -t.y, g.width, g.height)

    def bar_mapped(self):
        try:
            return self.bar().get_attributes().map_state == 2
        except Exception:
            return False

    def opacity(self):
        try:
            v = self.bar().get_full_property(
                self.XD.intern_atom('_NET_WM_WINDOW_OPACITY'),
                X.AnyPropertyType)
            return round(v.value[0] / 0xFFFFFFFF, 2) if v and v.value else 1.0
        except Exception:
            return -1

    def find_win(self, keyword, exact=False, mapped_only=False):
        """全窗口树搜索指定标题的窗口(返回 XID 列表).

        exact: 精确等于标题(默认子串); mapped_only: 只要已映射窗口.
        精确匹配用于排除环境撞词 —— 用户浏览器标签页标题可能恰好含
        "快速笔记"字样(如正在浏览本项目页面), 子串匹配会撞上并污染
        测试 11 的点击坐标与可见性判定."""
        found = []

        def walk(w):
            try:
                kids = w.query_tree().children
            except Exception:
                return
            for k in kids:
                try:
                    nm = k.get_full_property(
                        self.XD.intern_atom('_NET_WM_NAME'),
                        X.AnyPropertyType)
                    if nm:
                        title = nm.value.decode('utf8', 'replace')
                        ok = (title == keyword) if exact \
                            else (keyword in title)
                        if ok:
                            found.append(k)
                except Exception:
                    pass
                walk(k)
        walk(self.root)
        if not mapped_only:
            return found
        picked = []
        for w in found:
            try:
                if w.get_attributes().map_state == 2:
                    picked.append(w)
            except Exception:
                pass
        return picked

    def panel_vis(self):
        for w in self.find_win('控制面板'):
            try:
                if w.get_attributes().map_state == 2:
                    return True
            except Exception:
                pass
        return False

    def notes_vis(self):
        # 精确匹配 + 仅已映射: 排除用户浏览器标题撞词(见 find_win 注释)
        for w in self.find_win('快速笔记', exact=True, mapped_only=True):
            try:
                if w.get_attributes().map_state == 2:
                    return True
            except Exception:
                pass
        return False

    # -------------------------------------------------- 内容采样
    def sample(self):
        """采样遮挡条中部区域.

        返回 (r, g, b, uniq): XPutImage 用 BGRX 小端, 故数据流中
        d[0]=B d[1]=G d[2]=R."""
        try:
            x, y, w, h = self.bar_geom()
            sx = max(1, int(w * 0.4))
            sy = max(1, int(h * 0.35))
            sw = max(8, int(w * 0.2))
            sh = max(8, int(h * 0.3))
            raw = self.bar().get_image(sx, sy, sw, sh, X.ZPixmap, 0xffffffff)
            d = raw.data
            n = len(d) // 4
            b = sum(d[0::4]) / n
            g = sum(d[1::4]) / n
            r = sum(d[2::4]) / n
            uniq = len(set(d[0:min(len(d), 40000):5]))
            return round(r), round(g), round(b), uniq
        except Exception:
            return -1, -1, -1, -1

    def is_solid(self, tint):
        """纯色模式: 中部为单一 tint 色(三通道均匹配且方差极小)."""
        r, g, b, u = self.sample()
        return (u <= 3 and abs(r - tint[0]) <= 10
                and abs(g - tint[1]) <= 10 and abs(b - tint[2]) <= 10)

    def is_blur(self, tint):
        return not self.is_solid(tint)

    # -------------------------------------------------- 输入合成
    def move(self, x, y):
        xtest.fake_input(self.XD, X.MotionNotify, x=int(x), y=int(y))
        self.XD.sync()
        time.sleep(0.15)

    def click(self, x, y, btn=1):
        self.move(x, y)
        xtest.fake_input(self.XD, X.ButtonPress, btn)
        self.XD.sync()
        time.sleep(0.06)
        xtest.fake_input(self.XD, X.ButtonRelease, btn)
        self.XD.sync()
        time.sleep(0.35)

    def press(self, btn=1, at=None):
        if at is not None:
            self.move(*at)
        xtest.fake_input(self.XD, X.ButtonPress, btn)
        self.XD.sync()
        time.sleep(0.08)

    def release(self, btn=1):
        xtest.fake_input(self.XD, X.ButtonRelease, btn)
        self.XD.sync()
        time.sleep(0.35)

    def hotkey(self, *keysyms):
        # 先释放所有修饰键: 若上一轮测试残留修饰键按下, XGrabKey 的
        # 修饰键组合不匹配 => 热键被 X server 静默丢弃(XTest 常见假失败)
        for mod in (XK.XK_Control_L, XK.XK_Alt_L, XK.XK_Shift_L, XK.XK_Super_L):
            xtest.fake_input(self.XD, X.KeyRelease,
                             self.XD.keysym_to_keycode(mod))
        self.XD.sync()
        codes = [self.XD.keysym_to_keycode(k) for k in keysyms]
        for c in codes:
            xtest.fake_input(self.XD, X.KeyPress, c)
        self.XD.sync()
        time.sleep(0.06)
        for c in reversed(codes):
            xtest.fake_input(self.XD, X.KeyRelease, c)
        self.XD.sync()
        time.sleep(0.5)

    def ensure(self, check, target, *keysyms, tries=5):
        """反复发送热键直到状态达标(容忍 XTest 偶发丢事件)."""
        for _ in range(tries):
            if check() == target:
                return True
            self.hotkey(*keysyms)
        return check() == target

    def center(self):
        x, y, w, h = self.bar_geom()
        return x + w // 2, y + h // 2

    # -------------------------------------------------- 日志检查
    def log_errors(self):
        try:
            txt = open(LOG, encoding='utf8', errors='replace').read()
        except OSError:
            return []
        pats = ('Traceback', 'UnboundLocalError', 'AttributeError',
                'NameError', 'TypeError', 'is not defined',
                '渲染线程异常', '渲染跳过', 'X 事件异常')
        return [l.strip() for l in txt.splitlines()
                if any(p in l for p in pats)]


def main():
    no_restart = '--no-restart' in sys.argv
    t = T()
    t.orig_cfg = load_cfg()

    # 测试用醒目 tint(纯色模式可被可靠识别)
    cfg = dict(t.orig_cfg)
    cfg['tint_color'] = TEST_TINT
    cfg['visible'] = True
    cfg['locked'] = False
    cfg['hint_seconds'] = 4
    save_cfg(cfg)

    print('=' * 62)
    print('字幕遮挡条 全功能测试')
    print('=' * 62)

    try:
        t.start(restart=not no_restart)

        # ---- 1. 启动与渲染 ----
        check('1.1 遮挡条已显示', t.bar_mapped())
        r, g, b, u = t.sample()
        check('1.2 启动即渲染磨砂内容', t.is_blur(TEST_TINT),
              'rgb=(%d,%d,%d) uniq=%d' % (r, g, b, u))
        errs = t.log_errors()
        check('1.3 启动无异常', not errs, errs[0] if errs else '')

        # ---- 1.4 渲染源正确性: 遮挡条正下方放一个纯绿窗口,
        #      遮挡条内容应呈现(模糊后的)绿色 —— 验证渲染管线读的是
        #      真正下方的画面, 而不是退化成兜底填充色
        under = None
        try:
            x, y, w, h = t.bar_geom()
            under = t.root.create_window(
                x, y, w, h, 0, t.XD.screen().root_depth,
                X.InputOutput, X.CopyFromParent,
                background_pixel=0x0000FF00,      # 纯绿
                override_redirect=1,
                event_mask=X.ExposureMask)
            under.map()
            t.XD.sync()
            time.sleep(0.4)
            under.configure(stack_mode=X.Below, sibling=t.bar_xid)
            t.XD.sync()
            time.sleep(1.2)
            r, g, b, u = t.sample()
            check('1.4 渲染下方纯色窗口(绿)', g > 120 and g > r + 60 and g > b + 60,
                  'rgb=(%d,%d,%d) uniq=%d' % (r, g, b, u))
        except Exception as e:
            check('1.4 渲染下方纯色窗口(绿)', False, '异常: %s' % e)
        finally:
            if under is not None:
                try:
                    under.destroy()
                    t.XD.sync()
                except Exception:
                    pass
                time.sleep(0.6)

        # ---- 2. 热键显隐 ----
        hidden = t.ensure(lambda: not t.bar_mapped(), True,
                          XK.XK_Control_L, XK.XK_Alt_L, ord('b'))
        shown = t.ensure(lambda: t.bar_mapped(), True,
                         XK.XK_Control_L, XK.XK_Alt_L, ord('b'))
        check('2.1 Ctrl+Alt+B 显隐', hidden and shown,
              '隐藏=%s 恢复=%s' % (hidden, shown))

        # ---- 3. 拖动移动 ----
        x0 = t.cfg()['x']
        cx, cy = t.center()
        t.press(1, at=(cx, cy))
        for dx in (20, 40, 60):
            xtest.fake_input(t.XD, X.MotionNotify, x=cx + dx, y=cy)
            t.XD.sync()
            time.sleep(0.05)
        solid_during_drag = t.is_solid(TEST_TINT)
        t.release(1)
        x1 = t.cfg()['x']
        check('3.1 左键拖动移动', x1 != x0, 'x %d -> %d' % (x0, x1))
        check('3.2 拖动中纯色+轮廓', solid_during_drag)
        time.sleep(1.3)
        r, g, b, u = t.sample()
        check('3.3 拖动结束自动恢复磨砂', t.is_blur(TEST_TINT),
              'rgb=(%d,%d,%d) uniq=%d' % (r, g, b, u))

        # ---- 4. 边缘缩放 ----
        x, y, w, h = t.bar_geom()
        w0 = t.cfg()['w']
        t.press(1, at=(x + w - 6, y + h // 2))
        for dx in (20, 40, 60):
            xtest.fake_input(t.XD, X.MotionNotify, x=x + w - 6 + dx, y=y + h // 2)
            t.XD.sync()
            time.sleep(0.05)
        t.release(1)
        w1 = t.cfg()['w']
        check('4.1 右边缘拖拽缩放', 40 <= w1 - w0 <= 80, 'w %d -> %d' % (w0, w1))

        # ---- 5. 滚轮: 纯色模式 + 自动恢复 ----
        h0 = t.cfg()['h']
        cx, cy = t.center()
        t.click(cx, cy, btn=4)
        time.sleep(0.15)
        solid_wheel = t.is_solid(TEST_TINT)
        h1 = t.cfg()['h']
        check('5.1 滚轮改变高度', h0 != h1, 'h %d -> %d' % (h0, h1))
        r, g, b, u = t.sample()
        check('5.2 滚轮触发纯色模式', solid_wheel,
              'rgb=(%d,%d,%d) uniq=%d' % (r, g, b, u))
        time.sleep(1.4)
        r, g, b, u = t.sample()
        check('5.3 滚轮后自动恢复磨砂', t.is_blur(TEST_TINT),
              'rgb=(%d,%d,%d) uniq=%d' % (r, g, b, u))

        # ---- 6. 右键面板(幂等) ----
        cx, cy = t.center()
        t.click(cx, cy, btn=3)
        p1 = t.panel_vis()
        t.click(cx, cy, btn=3)
        p2 = t.panel_vis()
        check('6.1 右键打开面板', p1)
        check('6.2 连续右键保持打开(幂等)', p2)

        # ---- 7. 面板与遮挡条共存 ----
        check('7.1 面板打开时遮挡条仍显示', t.bar_mapped())
        # 面板打开时拖动遮挡条应生效
        x0 = t.cfg()['x']
        cx, cy = t.center()
        t.press(1, at=(cx, cy))
        for dx in (20, 40):
            xtest.fake_input(t.XD, X.MotionNotify, x=cx + dx, y=cy)
            t.XD.sync()
            time.sleep(0.05)
        t.release(1)
        check('7.2 面板打开时可拖动遮挡条', t.cfg()['x'] != x0,
              'x %d -> %d' % (x0, t.cfg()['x']))
        time.sleep(1.3)

        # ---- 8. 关闭面板 ----
        closed = t.ensure(lambda: not t.panel_vis(), True,
                          XK.XK_Control_L, XK.XK_Alt_L, ord('c'))
        check('8.1 Ctrl+Alt+C 关闭面板', closed)
        check('8.2 关面板后遮挡条回顶且可见', t.bar_mapped())

        # ---- 9. 锁定透明度 ----
        locked = t.ensure(lambda: t.cfg()['locked'], True,
                          XK.XK_Control_L, XK.XK_Alt_L, XK.XK_Shift_L, ord('l'))
        time.sleep(0.6)          # 等透明度维护定时器应用
        op_locked = t.opacity()
        check('9.1 热键锁定', locked, 'locked=%s' % locked)
        check('9.2 锁定时半透明提示', 0.6 <= op_locked <= 0.8,
              'opacity=%s' % op_locked)
        unlocked = t.ensure(lambda: not t.cfg()['locked'], True,
                            XK.XK_Control_L, XK.XK_Alt_L, XK.XK_Shift_L, ord('l'))
        time.sleep(0.6)
        check('9.3 解锁恢复不透明', unlocked and t.opacity() >= 0.95,
              'opacity=%s' % t.opacity())

        # ---- 10. 中键透视 ----
        cx, cy = t.center()
        t.move(cx, cy)
        xtest.fake_input(t.XD, X.ButtonPress, 2)
        t.XD.sync()
        time.sleep(0.5)
        peek_hidden = not t.bar_mapped()
        xtest.fake_input(t.XD, X.ButtonRelease, 2)
        t.XD.sync()
        # 松开后必须恢复: 最多等 2.5s(中键 unmap 后松开事件可能丢失,
        # 这是关键回归点)
        restored = False
        t0 = time.monotonic()
        while time.monotonic() - t0 < 2.5:
            if t.bar_mapped():
                restored = True
                break
            time.sleep(0.15)
        check('10.1 中键按住遮挡条消失', peek_hidden)
        check('10.2 松开恢复显示', restored,
              '%.1fs 内恢复=%s' % (time.monotonic() - t0, restored))
        if not restored:
            # 复位: 再按一次中键+松开, 尽量让后续测试可继续
            xtest.fake_input(t.XD, X.ButtonPress, 2)
            t.XD.sync()
            time.sleep(0.2)
            xtest.fake_input(t.XD, X.ButtonRelease, 2)
            t.XD.sync()
            time.sleep(0.5)

        # ---- 11. 快速笔记 ----
        t.hotkey(XK.XK_Control_L, XK.XK_Alt_L, ord('n'))
        time.sleep(0.8)
        notes = t.notes_vis()
        check('11.1 Ctrl+Alt+N 弹出便签', notes)
        if notes:
            # 精确匹配 + 仅已映射: 点击坐标必须来自真正的便签弹窗,
            # 否则会点到标题撞词的无关窗口(如用户浏览器)导致焦点错位
            wins = t.find_win('快速笔记', exact=True, mapped_only=True)
            if wins:
                g = wins[0].get_geometry()
                tt = wins[0].translate_coords(t.root, 0, 0)
                nx, ny = -tt.x, -tt.y
                t.click(nx + 200, ny + 75)      # 点文本区拿焦点
                esc = t.XD.keysym_to_keycode(XK.XK_Escape)
                xtest.fake_input(t.XD, X.KeyPress, esc)
                t.XD.sync()
                time.sleep(0.05)
                xtest.fake_input(t.XD, X.KeyRelease, esc)
                t.XD.sync()
                time.sleep(0.6)
            closed = not t.notes_vis()
            if not closed:
                # 再试一次(先点标题栏区域拿焦点)
                t.click(nx + 60, ny + 15)
                esc = t.XD.keysym_to_keycode(XK.XK_Escape)
                xtest.fake_input(t.XD, X.KeyPress, esc)
                t.XD.sync()
                time.sleep(0.05)
                xtest.fake_input(t.XD, X.KeyRelease, esc)
                t.XD.sync()
                time.sleep(0.6)
                closed = not t.notes_vis()
            check('11.2 Esc 关闭便签', closed)

        # ---- 12. 预设切换 ----
        cfg = t.cfg()
        cfg['presets'] = {'__test__': {'x': 100, 'y': 300, 'w': 600, 'h': 80}}
        cfg['preset'] = ''
        save_cfg(cfg)
        # 需要程序读到新预设: 重启
        t.start(restart=True)
        applied = t.ensure(lambda: t.cfg()['w'] == 600, True,
                           XK.XK_Control_L, XK.XK_Alt_L, ord('p'))
        c2 = t.cfg()
        check('12.1 Ctrl+Alt+P 应用预设', applied,
              'preset=%s w=%s' % (c2.get('preset'), c2.get('w')))

        # ---- 13. 全流程无异常 ----
        errs = t.log_errors()
        check('13.1 全流程无异常日志', not errs,
              ('%d 条: %s' % (len(errs), errs[0])) if errs else '')

    finally:
        # 恢复原配置并清理现场
        cfg = t.orig_cfg
        cfg.pop('presets', {}).pop('__test__', None)
        save_cfg(cfg)
        t.stop()
        time.sleep(0.5)
        env = dict(os.environ)
        env['DISPLAY'] = os.environ.get('DISPLAY', ':1')
        subprocess.Popen([sys.executable, 'run.py'], cwd=PROJ, env=env,
                         stdout=open(LOG, 'a'), stderr=subprocess.STDOUT)

    # ---- 汇总 ----
    print('=' * 62)
    fails = [r for r in RESULTS if not r[1]]
    print('结果: %d/%d 通过, %d 失败' % (len(RESULTS) - len(fails),
                                        len(RESULTS), len(fails)))
    for name, _, detail in fails:
        print('  FAIL %s %s' % (name, detail))
    print('=' * 62)
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
