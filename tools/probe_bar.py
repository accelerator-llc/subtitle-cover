#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""遮挡条窗口内容采样器(诊断探针, 配合 tools/probe_device.py).

连续抓取遮挡条窗口自身像素(override-redirect 窗口不进合成器,
XGetImage 直抓即所得), 对相邻两帧做帧间配准(行/列剖面互相关),
输出每个采样时刻的相对位移 (dx,dy) —— 正值 = 内容向右/向下移动;
同时统计:
  * 边缘探针: 左/上边缘前几列(行)的亮度均值 vs 中心区, 判定暗条;
  * 延迟探针: 滚动阶段开始后画面首次变化时刻 - 阶段开始时刻
    (静止期 ≥3s 已触发空闲降频, 测得的是最坏端到端延迟);
  * 写帧节奏: 相邻两次内容变化的时间间隔分布(近似 XPutImage 间隔).

用法:
  python3 tools/probe_bar.py --dur 13 --rate 30 --out /tmp/sc_probe_bar.json \
      --save-dir /tmp/sc_frames --save-at 1.0,5.0,10.0
"""

import argparse
import hashlib
import json
import os
import statistics
import sys
import time

from _xprobe import XProbe

DEFAULT_EVENTS = '/tmp/sc_probe_events.json'


def profile_bytes(gray):
    """灰度图降为行均值剖面(1×H)与列均值剖面(1×W)的字节串."""
    w, h = gray.size
    return gray.resize((1, h)).tobytes(), gray.resize((w, 1)).tobytes()


def match_axis(cur_b, prev_b, rng, stride=2):
    """一维剖面互相关找最优位移(整数 px).

    搜索 s 使 cur[y] ≈ prev[y+s]; 内容实际移动量 = -s
    (返回值已取负: 正 = 内容向正方向(右/下)移动).
    err 为平均绝对差(0~255), 越小越可信."""
    n = len(cur_b)
    best, best_err = 0, None
    for s in range(-rng, rng + 1):
        lo = max(0, -s)
        hi = n - max(0, s)
        if hi - lo < 32:
            continue
        tot = 0
        cnt = 0
        for y in range(lo, hi, stride):
            tot += abs(cur_b[y] - prev_b[y + s])
            cnt += 1
        err = tot / cnt
        if best_err is None or err < best_err:
            best, best_err = s, err
    return -best, best_err


def region_mean(gray, box):
    """矩形区域亮度均值(0~255)."""
    from PIL import ImageStat
    return ImageStat.Stat(gray.crop(box)).mean[0]


def main():
    ap = argparse.ArgumentParser(description='遮挡条内容采样器(探针)')
    ap.add_argument('--dur', type=float, default=13.0, help='采样时长(秒)')
    ap.add_argument('--rate', type=float, default=30.0, help='采样频率 Hz')
    ap.add_argument('--xid', type=lambda s: int(s, 0), default=0,
                    help='遮挡条 XID; 缺省读 ~/.config/subtitle-cover/bar_xid')
    ap.add_argument('--events', default=DEFAULT_EVENTS, help='装置时间线文件')
    ap.add_argument('--out', default='', help='逐采样 JSON 输出路径')
    ap.add_argument('--save-dir', default='', help='保存 PNG 帧的目录')
    ap.add_argument('--save-at', default='',
                    help='在这些采样时刻(秒)保存 PNG, 逗号分隔')
    ap.add_argument('--range', type=int, default=96, help='位移搜索范围 px')
    ap.add_argument('--max-err', type=float, default=25.0,
                    help='剖面配准平均绝对差上限, 超过记 nomatch')
    args = ap.parse_args()

    from PIL import ImageStat   # noqa: F401  (region_mean 依赖, 预热导入)
    xp = XProbe(os.environ.get('DISPLAY'))
    xid = args.xid
    if not xid:
        p = os.path.expanduser('~/.config/subtitle-cover/bar_xid')
        with open(p) as f:
            xid = int(f.read().strip(), 0)
    rect = xp.window_abs_rect(xid)
    if rect is None:
        print('遮挡条窗口不存在(应用未运行?)', file=sys.stderr)
        return 1
    bx, by, bw, bh = rect
    print('采样目标: xid=%s rect=%s' % (hex(xid), rect), flush=True)

    save_at = [float(s) for s in args.save_at.split(',') if s.strip()]
    if args.save_dir:
        os.makedirs(args.save_dir, exist_ok=True)

    # ---------------- 采样主循环 ----------------
    samples = []
    prev_gray = None
    t_start = time.monotonic()
    next_t = t_start
    idx = 0
    while time.monotonic() - t_start < args.dur:
        try:
            grab = xp.grab_pil(xid, 0, 0, bw, bh)
        except (RuntimeError, OSError):
            grab = None
        t = time.monotonic()
        rec = {'i': idx, 't': round(t, 4), 'el': round(t - t_start, 4)}
        if grab is not None:
            g = grab.convert('L')
            hsh = hashlib.md5(g.tobytes()).hexdigest()[:10]
            rec['hash'] = hsh
            # 边缘与中心亮度: 左/右各前 4 列, 上/下各前 4 行
            center = region_mean(g, (bw // 4, bh // 4,
                                     bw - bw // 4, bh - bh // 4))
            col_l = [region_mean(g, (k, 0, k + 1, bh)) for k in range(4)]
            col_r = [region_mean(g, (bw - 1 - k, 0, bw - k, bh))
                     for k in range(4)]
            row_t = [region_mean(g, (0, k, bw, k + 1)) for k in range(4)]
            row_b = [region_mean(g, (0, bh - 1 - k, bw, bh - k))
                     for k in range(4)]
            rec['edge'] = {'L': [round(v, 1) for v in col_l],
                           'R': [round(v, 1) for v in col_r],
                           'T': [round(v, 1) for v in row_t],
                           'B': [round(v, 1) for v in row_b],
                           'C': round(center, 1)}
            if prev_gray is not None and g.size == prev_gray.size:
                if prev_hash == hsh:
                    rec['dx'], rec['dy'], rec['err'] = 0, 0, 'same'
                else:
                    pc = profile_bytes(g)
                    pp = profile_bytes(prev_gray)
                    dy, err_y = match_axis(pc[0], pp[0], args.range)
                    # 已知垂直位移 dy(正=内容向下)后, 取对齐的水平中带
                    # 找水平位移: cur[r] = prev[r - dy]
                    band_h = max(8, bh // 4)
                    y0c = min(bh - band_h, max(0, bh // 2 - band_h // 2))
                    y0p = min(bh - band_h, max(0, y0c - dy))
                    bc = profile_bytes(g.crop((0, y0c, bw, y0c + band_h)))[1]
                    bp = profile_bytes(
                        prev_gray.crop((0, y0p, bw, y0p + band_h)))[1]
                    dx, err_x = match_axis(bc, bp, args.range)
                    rec['dx'], rec['dy'] = dx, dy
                    err = min(err_y, err_x)
                    rec['err'] = round(err, 2) if err <= args.max_err \
                        else 'nomatch'
            else:
                rec['dx'], rec['dy'], rec['err'] = 0, 0, 'first'
            prev_hash = hsh
            prev_gray = g
            for ts in save_at:
                if abs(rec['el'] - ts) <= 0.5 / args.rate:
                    grab.save(os.path.join(
                        args.save_dir, 'frame_%05.1f.png' % rec['el']))
            samples.append(rec)
        idx += 1
        next_t += 1.0 / args.rate
        sleep_for = next_t - time.monotonic()
        if sleep_for > 0:
            time.sleep(sleep_for)
        else:
            next_t = time.monotonic()

    # ---------------- 分析与报告 ----------------
    report = analyze(samples, args.events)
    if args.out:
        with open(args.out, 'w') as f:
            json.dump({'rect': [bx, by, bw, bh], 'samples': samples,
                       'report': report}, f, indent=1)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


def analyze(samples, events_path):
    """汇总 (dx,dy) 序列/边缘/延迟/写帧节奏, 输出紧凑报告 dict."""
    rep = {'n_samples': len(samples)}
    ev_full = {}
    if os.path.exists(events_path):
        with open(events_path) as f:
            ev_full = json.load(f)
    ev = ev_full.get('events', {})
    rep['device'] = {'dir': ev_full.get('dir'), 'speed': ev_full.get('speed')}
    # --- 各滚动阶段: 位移序列 / dx 是否恒零 / 帧间隔 / 首变延迟 / 速度 ---
    for phase in ('scroll1', 'scroll2'):
        if phase not in ev:
            continue
        t0, t1 = ev[phase]
        seg = [s for s in samples if t0 <= s['t'] <= t1 + 0.8]
        changed = [s for s in seg
                   if isinstance(s.get('dy'), int)
                   and s.get('err') not in ('same', 'first', 'nomatch')]
        ivs = [round((seg[i + 1]['t'] - seg[i]['t']) * 1000)
               for i in range(len(seg) - 1)
               if seg[i].get('hash') != seg[i + 1].get('hash')]
        # 速度估计: 有位移样本的 dy 总和 / 阶段时长 (px/s)
        dy_sum = sum(s['dy'] for s in changed)
        dx_sum = sum(s['dx'] for s in changed)
        dur = max(0.001, t1 - t0)
        # 延迟: 阶段开始后画面相对静止基线首次变化的时刻
        prev_phase = 'static1' if phase == 'scroll1' else 'static2'
        lat = None
        if prev_phase in ev:
            pre = [s for s in samples
                   if s['t'] < ev[prev_phase][1] + 0.1]
            if pre:
                base_hash = pre[-1].get('hash')
                for s in samples:
                    if s['t'] >= t0 and s.get('hash') != base_hash:
                        lat = round(s['t'] - t0, 3)
                        break
        rep[phase] = {
            'n_changed': len(changed),
            'dy_seq': [(round(s['t'] - t0, 2), s['dx'], s['dy'])
                       for s in changed],
            'dy_med': statistics.median([s['dy'] for s in changed])
                      if changed else None,
            'dx_med': statistics.median([s['dx'] for s in changed])
                      if changed else None,
            'dx_max_abs': max((abs(s['dx']) for s in changed), default=None),
            'n_nonzero_dx': sum(1 for s in changed if abs(s['dx']) > 2),
            'dx_sum': dx_sum,
            'vy_est': round(dy_sum / dur, 1),
            'vx_est': round(dx_sum / dur, 1),
            'frame_iv_ms': ivs,
            'latency_s': lat,
        }
    # --- 边缘探针汇总(全部帧中位数) ---
    if samples:
        def med(key, k=0):
            if key == 'C':   # 中心区是标量
                vals = [s['edge'][key] for s in samples if 'edge' in s]
            else:
                vals = [s['edge'][key][k] for s in samples if 'edge' in s]
            return round(statistics.median(vals), 1) if vals else None
        rep['edge_med'] = {
            'left_cols0_3': [med('L', k) for k in range(4)],
            'right_cols0_3': [med('R', k) for k in range(4)],
            'top_rows0_3': [med('T', k) for k in range(4)],
            'bottom_rows0_3': [med('B', k) for k in range(4)],
            'center': med('C'),
        }
        c = rep['edge_med']['center']
        if c:
            def dark(seq):
                w = 0
                for m in seq:
                    if m < c * 0.6:
                        w += 1
                    else:
                        break
                return w
            rep['dark_strip_px'] = {
                'left': dark(rep['edge_med']['left_cols0_3']),
                'right': dark(rep['edge_med']['right_cols0_3']),
                'top': dark(rep['edge_med']['top_rows0_3']),
                'bottom': dark(rep['edge_med']['bottom_rows0_3']),
            }
    return rep


if __name__ == '__main__':
    sys.exit(main())
