#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""拖动延迟探针(诊断用): XTest 合成左键拖动遮挡条, 测端到端延迟.

以 200Hz 轮询遮挡条窗口几何, 对比每次 XTest 移动指令发出时刻与
窗口位置实际变化时刻, 输出:
  * 首次移动的端到端延迟(按下后第一次位置变化 - 第一次移动指令);
  * 拖动期间位置更新间隔分布(反映渲染线程的写帧节奏);
  * 拖动总耗时内位置追上指令的滞后像素(最大值).

拖动路径为 +40px 后 -40px 往返, 净位移 0(配置最终不变).
用法: python3 tools/probe_drag.py [--dist 40]
"""

import argparse
import ctypes
import os
import statistics
import sys
import time

from _xprobe import XProbe


def main():
    ap = argparse.ArgumentParser(description='拖动延迟探针')
    ap.add_argument('--dist', type=int, default=40, help='拖动距离 px')
    ap.add_argument('--rate', type=float, default=200.0, help='几何采样 Hz')
    args = ap.parse_args()

    xp = XProbe(os.environ.get('DISPLAY'))
    lib = xp.lib
    dpy = xp.fx.dpy
    screen = xp.screen_no
    # 轮询用独立 X 连接(libX11 非线程安全, 与 XTest/flush 线程共用
    # 连接会破坏协议流导致采样错乱)
    xpp = XProbe(os.environ.get('DISPLAY'))

    # XTest 绑定
    xtst = ctypes.CDLL('libXtst.so.6')
    xtst.XTestFakeMotionEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]

    xid_p = os.path.expanduser('~/.config/subtitle-cover/bar_xid')
    with open(xid_p) as f:
        xid = int(f.read().strip(), 0)
    rect = xpp.window_abs_rect(xid)
    if rect is None:
        print('遮挡条窗口不存在', file=sys.stderr)
        return 1
    bx, by, bw, bh = rect
    cx, cy = bx + bw // 2, by + bh // 2
    print('遮挡条: %s, 从 (%d,%d) 拖动 +%dpx 再拖回' % (rect, cx, cy, args.dist))

    # 几何采样线程(同连接: X 客户端串行化, 轮询与 Fake 事件互不干扰)
    samples = []
    import threading
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            t = time.monotonic()
            r = xpp.window_abs_rect(xid)
            if r:
                samples.append((t, r[0], r[1]))
            time.sleep(1.0 / args.rate)

    th = threading.Thread(target=poll, daemon=True)
    th.start()
    time.sleep(0.3)

    def move(x, y):
        xtst.XTestFakeMotionEvent(dpy, screen, x, y, 0)
        lib.XFlush(dpy)
        return time.monotonic()

    def press(v):
        xtst.XTestFakeButtonEvent(dpy, 1, v, 0)
        lib.XFlush(dpy)
        return time.monotonic()

    timeline = []          # (t_send, kind, x, y)
    # 先把指针移到条中心再按下(XTest 按下作用于指针当前位置,
    # 不先移动会点中用户桌面上其他窗口)
    timeline.append((move(cx, cy), 'park', cx, cy))
    time.sleep(0.25)
    timeline.append((press(1), 'down', cx, cy))
    time.sleep(0.15)
    steps = []
    n_steps = 10
    for i in range(1, n_steps + 1):
        x, y = cx + args.dist * i // n_steps, cy
        steps.append(move(x, y))
        timeline.append((steps[-1], 'move', x, y))
        time.sleep(0.06)
    time.sleep(0.2)
    timeline.append((press(0), 'up', cx + args.dist, cy))
    time.sleep(0.5)
    # 拖回原位
    timeline.append((move(cx + args.dist, cy), 'park2', cx + args.dist, cy))
    time.sleep(0.25)
    timeline.append((press(1), 'down2', cx + args.dist, cy))
    time.sleep(0.15)
    for i in range(1, n_steps + 1):
        x, y = cx + args.dist - args.dist * i // n_steps, cy
        timeline.append((move(x, y), 'move2', x, y))
        time.sleep(0.06)
    time.sleep(0.2)
    timeline.append((press(0), 'up2', cx, cy))
    time.sleep(0.5)
    stop.set()
    th.join(timeout=1.0)

    # ---------------- 分析 ----------------
    if len(samples) < 4:
        print('几何采样过少', file=sys.stderr)
        return 1
    # 首次移动延迟: 第一个 move 指令后, 位置首次变化(x 方向)的时刻
    t_first_move = next(t for t, k, x, y in timeline if k == 'move')
    base = [(t, x, y) for t, x, y in samples
            if t <= t_first_move]
    base_x = base[-1][1]
    lat = None
    for t, x, y in samples:
        if t > t_first_move and abs(x - base_x) >= 2:
            lat = (t - t_first_move) * 1000
            break
    # 拖动期间(第一次 down 后 up 前)位置更新间隔与跟随滞后
    t_down = next(t for t, k, x, y in timeline if k == 'down')
    t_up = next(t for t, k, x, y in timeline if k == 'up')
    seg = [(t, x, y) for t, x, y in samples if t_down <= t <= t_up]
    moves_t = [t for t, k, x, y in timeline if k == 'move']
    # 指令时刻应有的 x; 滞后 = 当前指令 x - 窗口实际 x 的最大值
    lag_max = 0
    seg_iter = iter(seg)
    lag_list = []
    for t_cmd, k, x_cmd, y_cmd in timeline:
        if k != 'move':
            continue
        # 找指令后 200ms 内的实际位置序列, 取最终位置误差与中间最大滞后
        window = [(t, x) for t, x, _ in seg
                  if t_cmd <= t <= t_cmd + 0.2]
        if not window:
            continue
        worst = max((x_cmd - x) for _, x in window)
        lag_list.append(worst)
        lag_max = max(lag_max, worst)
    ivs = []
    last_x = None
    last_t = None
    for t, x, y in seg:
        if last_x is not None and x != last_x:
            ivs.append((t - last_t) * 1000)
        if last_x is None or x != last_x:
            last_x, last_t = x, t
    print('首次移动端到端延迟: %s ms' %
          (round(lat) if lat is not None else '未观测到'))
    print('拖动中位置更新间隔(ms): 中位 %d / min %d / max %d (n=%d)' % (
        statistics.median(ivs) if ivs else -1,
        min(ivs) if ivs else -1, max(ivs) if ivs else -1, len(ivs)))
    print('每次 move 后 200ms 内最大滞后 px: 中位 %d / max %d' % (
        statistics.median(lag_list) if lag_list else -1,
        lag_max))
    with open('/tmp/sc_probe_drag.json', 'w') as f:
        import json
        json.dump({'latency_ms': lat, 'ivs_ms': ivs, 'lag_list': lag_list},
                  f, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
