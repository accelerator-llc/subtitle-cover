#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遮挡条下方背景滚动测试装置(诊断探针, 配合 tools/probe_bar.py).

在遮挡条正下方创建一个 override-redirect 马赛克窗口(不进 WM 管理,
创建于遮挡条之后仍位于其下方——遮挡条会自动把自己压回栈顶),
以恒定速度垂直/水平/对角滚动, 模拟"被盖背景匀速运动".
阶段时间线写入 JSON 供采样器对齐理论位移与计算延迟.

用法:
  python3 tools/probe_device.py                        # 垂直向下 160px/s
  python3 tools/probe_device.py --speed 200 --dir h    # 水平(探针自检)
  python3 tools/probe_device.py --dir vh               # 对角(探针自检)
  python3 tools/probe_device.py --inset-left 30 --inset-top 30
                                  # 装置左/上边缩进条内: 复现"层不从条左上
                                  # 角开始"时左侧/顶部露底(暗条)的场景
"""

import argparse
import json
import random
import time
import os
import sys

from _xprobe import XProbe   # 探针公共库(tools/ 内相对导入)

EVENTS_PATH = '/tmp/sc_probe_events.json'

# 阶段时间线(秒): 静止 -> 滚动 -> 静止 -> 滚动 -> 静止
# 前段静止 ≥3s 用于触发遮挡条的空闲降频(0.5s 探测), 从而测量
# "从静止恢复运动"的最坏端到端延迟
PHASES = [('static1', 3.0), ('scroll1', 2.5), ('static2', 2.5),
          ('scroll2', 2.5), ('tail', 2.0)]

CELL = 60          # 马赛克格子边长(物理像素)
PERIOD = 240       # 图案纵向/横向平铺周期(须为 CELL 整数倍, 且 > 位移搜索范围)
PALETTE = [(240, 40, 40), (40, 160, 240), (250, 210, 40), (40, 200, 90),
           (200, 60, 220), (250, 250, 250), (20, 20, 20), (250, 130, 20)]


def make_pattern(w, h, seed=7):
    """生成纵向/横向都以 PERIOD 平铺的高对比马赛克图案(PIL RGB).

    w/h 为可用尺寸, 实际生成尺寸向上取整到 PERIOD 的整数倍, 保证
    以任意 (ox, oy) ∈ [0, PERIOD) 为源点裁剪装置大小区域时不出界."""
    from PIL import Image, ImageDraw
    rng = random.Random(seed)
    cells_x = -(-w // CELL)          # ceil
    cells_y = -(-h // CELL)
    # 周期块内每格随机取色(种子固定, 多次运行图案一致)
    block = [[rng.choice(PALETTE) for _ in range(PERIOD // CELL)]
             for _ in range(PERIOD // CELL)]
    img = Image.new('RGB', (cells_x * CELL, cells_y * CELL))
    d = ImageDraw.Draw(img)
    for cy in range(cells_y):
        for cx in range(cells_x):
            color = block[cy % (PERIOD // CELL)][cx % (PERIOD // CELL)]
            d.rectangle([cx * CELL, cy * CELL,
                         cx * CELL + CELL - 1, cy * CELL + CELL - 1],
                        fill=color)
    return img


def bar_rect(xp, bar_xid):
    """读取遮挡条窗口实际矩形; 无效则返回 None."""
    if not bar_xid:
        return None
    return xp.window_abs_rect(bar_xid)


def main():
    ap = argparse.ArgumentParser(description='背景滚动测试装置(探针)')
    ap.add_argument('--speed', type=float, default=160.0,
                    help='滚动速度 px/s (正=内容向下/向右)')
    ap.add_argument('--dir', choices=['v', 'h', 'vh', 'none'], default='v',
                    help='滚动方向: v 垂直 / h 水平 / vh 对角(自检)')
    ap.add_argument('--margin', type=int, default=96,
                    help='装置窗口相对遮挡条四周的外扩边距 px(>=pad 上限 64)')
    ap.add_argument('--inset-left', type=int, default=0,
                    help='装置左边缘相对条左边缘向右缩进 px(复现露底)')
    ap.add_argument('--inset-top', type=int, default=0,
                    help='装置上边缘相对条上边缘向下缩进 px(复现露底)')
    ap.add_argument('--bar-xid', type=lambda s: int(s, 0), default=0,
                    help='遮挡条 XID; 缺省读 ~/.config/subtitle-cover/bar_xid')
    ap.add_argument('--step-px', type=int, default=0,
                    help='阶跃滚动步长 px(0=连续; >0 复现滚轮分步滚动的'
                         '静止-跳动交替, 触发遮挡条"静止跳帧->回退"路径)')
    ap.add_argument('--events', default=EVENTS_PATH, help='时间线输出路径')
    args = ap.parse_args()

    xp = XProbe(os.environ.get('DISPLAY'))
    bar_xid = args.bar_xid
    if not bar_xid:
        p = os.path.expanduser('~/.config/subtitle-cover/bar_xid')
        try:
            with open(p) as f:
                bar_xid = int(f.read().strip(), 0)
        except OSError:
            pass
    br = bar_rect(xp, bar_xid)
    if br is None:
        print('未找到遮挡条窗口(应用未运行?)', file=sys.stderr)
        return 1
    bx, by, bw, bh = br
    rw, rh = xp.root_geometry()
    dev_x = max(0, bx - args.margin + args.inset_left)
    dev_y = max(0, by - args.margin + args.inset_top)
    dev_w = min(rw - dev_x, bw + 2 * args.margin - args.inset_left)
    dev_h = min(rh - dev_y, bh + 2 * args.margin - args.inset_top)
    print('遮挡条矩形: %s  装置矩形: (%d,%d,%d,%d)' %
          (br, dev_x, dev_y, dev_w, dev_h))

    # 预渲染图案(装置尺寸 + 一个平铺周期余量, 保证任意源偏移可裁剪)
    pat = make_pattern(dev_w + PERIOD, dev_h + PERIOD)
    win = xp.create_window(dev_x, dev_y, dev_w, dev_h)
    xp.map_window(win)
    xp.lib.XSync(xp.fx.dpy, 0)
    time.sleep(0.15)

    def paint(ox, oy):
        tile = pat.crop((ox, oy, ox + dev_w, oy + dev_h))
        xp.put_pil(win, tile, 24)

    paint(0, 0)
    events = {}
    last_target = None
    tick = 0.033
    try:
        for name, dur in PHASES:
            t0 = time.monotonic()
            events[name] = [t0, None]
            print('[%s] start=%.4f' % (name, t0), flush=True)
            scrolling = (args.dir != 'none'
                         and (name == 'scroll1' or name == 'scroll2'))
            next_t = t0
            while True:
                now = time.monotonic()
                if now - t0 >= dur:
                    break
                if scrolling:
                    # speed>0 定义为"内容向下/向右移动": 源偏移随时间
                    # 递减(取模平铺), 屏幕固定行看到的内容随之下滑
                    dist = int(args.speed * (now - t0))
                    if args.step_px > 0:
                        # 阶跃模式: 每 step_px px 跳一次, 其间内容静止
                        # (模拟滚轮分步滚动), 用于复现回退帧交替
                        target = dist - dist % args.step_px
                        if target == last_target:
                            next_t += tick
                            sleep_for = next_t - time.monotonic()
                            if sleep_for > 0:
                                time.sleep(sleep_for)
                            continue
                        last_target = target
                        dist = target
                    step = (PERIOD - dist % PERIOD) % PERIOD
                    ox = step if 'h' in args.dir else 0
                    oy = step if 'v' in args.dir else 0
                    paint(ox, oy)
                next_t += tick
                sleep_for = next_t - time.monotonic()
                if sleep_for > 0:
                    time.sleep(sleep_for)
                else:
                    next_t = time.monotonic()
            events[name][1] = time.monotonic()
    finally:
        xp.destroy_window(win)
        with open(args.events, 'w') as f:
            json.dump({'bar_rect': [bx, by, bw, bh],
                       'dev_rect': [dev_x, dev_y, dev_w, dev_h],
                       'dir': args.dir, 'speed': args.speed,
                       'events': events}, f, indent=1)
        print('时间线已写入 %s' % args.events)
    return 0


if __name__ == '__main__':
    sys.exit(main())
