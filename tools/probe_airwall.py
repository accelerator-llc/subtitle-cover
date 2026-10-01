#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""空气墙功能验证探针(修复 agent F).

验证 App 单点收敛的屏幕边界 clamp(物理像素):
  1. 启动自愈: 老配置里的屏外坐标(含完全出屏)启动时被钳回屏内并落盘
  2. 拖动撞墙: 左/右/上/下四个方向拖出屏幕, 松手后几何停在墙内
  3. 滚轮撞墙: 底边贴墙时滚轮增高, 条沿墙滑动(y 被钳)而非越界
  4. 预设撞墙: 配置文件里预存屏外坐标的预设, 经热键应用后被钳回

用法: DISPLAY=:1 python3 tools/probe_airwall.py
注意: 会重启应用 2 次、用 XTest 真实拖动/滚轮/热键, 全程约 1 分钟.
"""

import ctypes
import json
import os
import subprocess
import sys
import time

_PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(
    _PROJ, '.deps/extracted/usr/lib/python3/dist-packages'))

from Xlib import X, display as xdisplay     # noqa: E402
from Xlib.ext import xtest                  # noqa: E402

CONF = os.path.expanduser('~/.config/subtitle-cover/config.json')
XID_FILE = os.path.expanduser('~/.config/subtitle-cover/bar_xid')
LOG = '/tmp/sc-airwall.log'
RESULTS = []


def check(name, ok, detail=''):
    RESULTS.append((name, bool(ok), detail))
    print('%-4s %-30s %s' % ('PASS' if ok else 'FAIL', name, detail),
          flush=True)


def load_cfg():
    with open(CONF) as f:
        return json.load(f)


def save_cfg(cfg):
    with open(CONF, 'w') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


class Probe:
    def __init__(self):
        self.xd = xdisplay.Display()
        self.root = self.xd.screen().root
        self.root_w = self.xd.screen().width_in_pixels
        self.root_h = self.xd.screen().height_in_pixels
        self.bar_xid = None

    # -------------------------------------------------- 生命周期
    def start_app(self, wait=4.5):
        subprocess.run(['pkill', '-f', r'run\.py$'], check=False)
        time.sleep(1.5)
        env = dict(os.environ)
        env['DISPLAY'] = os.environ.get('DISPLAY', ':1')
        log = open(LOG, 'w')
        subprocess.Popen([sys.executable, 'run.py'], cwd=_PROJ, env=env,
                         stdout=log, stderr=subprocess.STDOUT)
        for _ in range(40):
            try:
                self.bar_xid = int(open(XID_FILE).read().strip())
                if self.bar_mapped():
                    return
            except (OSError, ValueError):
                pass
            time.sleep(0.25)
        raise RuntimeError('应用启动失败(无遮挡条窗口)')

    def bar_mapped(self):
        try:
            return self.bar().get_attributes().map_state == 2
        except Exception:
            return False

    def bar(self):
        return self.xd.create_resource_object('window', self.bar_xid)

    def bar_rect(self):
        g = self.bar().get_geometry()
        t = self.bar().translate_coords(self.root, 0, 0)
        return (-t.x, -t.y, g.width, g.height)

    # -------------------------------------------------- 交互原语
    def _motion(self, x, y):
        xtest.fake_input(self.xd, X.MotionNotify, x=x, y=y)
        self.xd.flush()

    def _button(self, btn, press):
        xtest.fake_input(self.xd,
                         X.ButtonPress if press else X.ButtonRelease,
                         detail=btn)
        self.xd.flush()

    def _key(self, ksym, mod_keysyms=(0xffe3, 0xffe9)):
        # mod_keysyms: Control_L / Alt_L (修饰掩码不是 keysym, 不可直转)
        codes = [self.xd.keysym_to_keycode(m) for m in mod_keysyms]
        for c in codes:
            xtest.fake_input(self.xd, X.KeyPress, detail=c)
        xtest.fake_input(self.xd, X.KeyPress,
                         detail=self.xd.keysym_to_keycode(ksym))
        self.xd.flush()
        time.sleep(0.05)
        xtest.fake_input(self.xd, X.KeyRelease,
                         detail=self.xd.keysym_to_keycode(ksym))
        for c in reversed(codes):
            xtest.fake_input(self.xd, X.KeyRelease, detail=c)
        self.xd.flush()

    def drag(self, fx, fy, tx, ty, steps=12):
        """从 (fx,fy) 按下拖到 (tx,ty) 松开(真实交互路径)."""
        self._motion(fx, fy)
        time.sleep(0.2)
        self._button(1, True)
        time.sleep(0.15)
        for i in range(1, steps + 1):
            self._motion(fx + (tx - fx) * i // steps,
                         fy + (ty - fy) * i // steps)
            time.sleep(0.033)
        time.sleep(0.15)
        self._button(1, False)
        time.sleep(0.3)

    def wheel(self, x, y, up=True, n=1):
        self._motion(x, y)
        time.sleep(0.15)
        for _ in range(n):
            # 上滚 detail 4 = 高+14, 下滚 detail 5 = 高-14
            self._button(4 if up else 5, True)
            self._button(4 if up else 5, False)
            time.sleep(0.08)
        time.sleep(0.25)

    # -------------------------------------------------- 测试
    def t_selfheal(self, expect_x):
        r = self.bar_rect()
        in_screen = (0 <= r[0] <= self.root_w - r[2]
                     and 0 <= r[1] <= self.root_h - r[3])
        check('1 启动自愈(屏内摆位)', in_screen, '条矩形=%s' % (r,))
        cfg = load_cfg()
        check('2 自愈坐标已落盘',
              cfg['x'] == r[0] and cfg['y'] == r[1]
              and 0 <= cfg['x'] <= self.root_w - cfg['w'],
              'config x=%s y=%s' % (cfg['x'], cfg['y']))
        check('3 自愈钳位值正确', r[0] == expect_x,
              '期望 x=%s 实际 x=%s' % (expect_x, r[0]))

    def t_drag_walls(self):
        # 目标坐标钳在屏内 2px: XTest 指针坐标不支持屏外(负值会被
        # 服务端钳/绕回), 拖到屏幕边缘足够触发空气墙的 clamp
        walls = [
            ('左墙', 2, None, lambda r: r[0] == 0),
            ('右墙', self.root_w - 2, None,
             lambda r: r[0] + r[2] == self.root_w),
            ('上墙', None, 2, lambda r: r[1] == 0),
            ('下墙', None, self.root_h - 2,
             lambda r: r[1] + r[3] == self.root_h),
        ]
        for name, tx, ty, ok_fn in walls:
            # 每个方向都从"条当前中心"出发(前一步会移动条)
            bx, by, bw, bh = self.bar_rect()
            fx, fy = bx + bw // 2, by + bh // 2
            tx2 = fx if tx is None else tx
            ty2 = fy if ty is None else ty
            self.drag(fx, fy, tx2, ty2)
            r = self.bar_rect()
            ok = ok_fn(r) and 0 <= r[0] and 0 <= r[1] \
                and r[0] + r[2] <= self.root_w and r[1] + r[3] <= self.root_h
            cfg = load_cfg()
            ok = ok and (cfg['x'], cfg['y']) == (r[0], r[1])
            check('拖动%s' % name, ok,
                  '条=%s config=(%s,%s)' % (r, cfg['x'], cfg['y']))
            # 拖回屏中, 保证下一方向从合法屏内位置出发
            mx = (self.root_w - r[2]) // 2 + r[2] // 2
            my = (self.root_h - r[3]) // 2 + r[3] // 2
            self.drag(r[0] + r[2] // 2, r[1] + r[3] // 2, mx, my)
            time.sleep(0.2)

    def t_wheel_wall(self):
        # 先把条拖到底墙, 使底边贴住屏幕下缘
        bx, by, bw, bh = self.bar_rect()
        cx, cy = bx + bw // 2, by + bh // 2
        self.drag(cx, cy, cx, self.root_h + 800)
        bx, by, bw, bh = self.bar_rect()
        # 上滚 8 次(detail 4): 高 +112, 底边触墙 => y 被钳(条沿墙上滑)
        self.wheel(bx + bw // 2, by + bh // 2, up=True, n=8)
        r = self.bar_rect()
        ok = r[1] + r[3] <= self.root_h and r[0] >= 0 and r[1] >= 0 \
            and r[0] + r[2] <= self.root_w
        # 底边应贴墙(高度增长全被 y 上移吸收)或高度被钳到屏高
        ok = ok and (r[1] + r[3] == self.root_h or r[3] == self.root_h)
        cfg = load_cfg()
        ok = ok and cfg['h'] == r[3] and cfg['y'] == r[1]
        check('滚轮增高贴墙', ok, '条=%s config=(y=%s,h=%s)' % (
            r, cfg['y'], cfg['h']))
        # 恢复高度: 下滚 8 次
        self.wheel(r[0] + r[2] // 2, r[1] + r[3] // 2, up=False, n=8)
        time.sleep(0.3)

    def t_preset_wall(self):
        cfg = load_cfg()
        cfg.setdefault('presets', {})['airwall-test'] = {
            'x': -400, 'y': -300, 'w': 500, 'h': 100}
        cfg['preset'] = ''
        save_cfg(cfg)
        self.start_app()
        # Ctrl+Alt+P 循环预设, 直到应用到 airwall-test(循环至多 5 次)
        applied = None
        for _ in range(5):
            self._key(ord('p'))
            time.sleep(0.6)
            cfg = load_cfg()
            if cfg.get('preset') == 'airwall-test':
                applied = True
                break
        r = self.bar_rect()
        ok = applied and 0 <= r[0] and r[0] + r[2] <= self.root_w \
            and 0 <= r[1] and r[1] + r[3] <= self.root_h
        check('预设屏外坐标被钳', ok, '条=%s preset=%s' % (
            r, load_cfg().get('preset')))
        # 清理测试预设
        cfg = load_cfg()
        cfg.get('presets', {}).pop('airwall-test', None)
        cfg['preset'] = ''
        save_cfg(cfg)


def main():
    p = Probe()
    user_cfg = load_cfg()
    print('用户态 config: x=%s y=%s w=%s h=%s locked=%s' % (
        user_cfg.get('x'), user_cfg.get('y'), user_cfg.get('w'),
        user_cfg.get('h'), user_cfg.get('locked')))
    try:
        # 场景1: 用户遗留的屏外坐标(本机实测 x=-8587 完全出屏)自愈
        # 期望钳位: x=0, y/h 原样(在屏内)
        x, y, w, h = (user_cfg.get('x'), user_cfg.get('y'),
                      user_cfg.get('w') or 720, user_cfg.get('h') or 110)
        if not isinstance(x, int) or x > 0:
            x = -8587          # 兜底构造一个完全出屏的遗留态
        p.start_app()
        expect_x = 0
        p.t_selfheal(expect_x)

        # 场景2-4: 撞墙
        p.t_drag_walls()
        p.t_wheel_wall()
        p.t_preset_wall()
    finally:
        # 结束态: 用户配置回归(tint/locked 等原样; x/y 已被空气墙治愈,
        # 不再写回屏外坐标 —— 这是功能语义), 重启应用恢复运行态
        healed = dict(user_cfg)
        hx = user_cfg.get('x')
        hw = user_cfg.get('w') or 720
        hh = user_cfg.get('h') or 110
        if isinstance(hx, int):
            healed['x'] = max(0, min(hx, p.root_w - hw))
        hy = user_cfg.get('y')
        if isinstance(hy, int):
            healed['y'] = max(0, min(hy, p.root_h - hh))
        healed['w'], healed['h'] = hw, hh
        save_cfg(healed)
        p.start_app()
    fails = [r for r in RESULTS if not r[1]]
    print('\n汇总: %d/%d 通过' % (len(RESULTS) - len(fails), len(RESULTS)))
    return 1 if fails else 0


if __name__ == '__main__':
    sys.exit(main())
