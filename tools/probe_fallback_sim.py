#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""root 回退路径离线模拟探针(不占屏幕, 确定性证明).

逐字复刻 cover.py render() 中 `blur_canvas is None` 回退分支的
crop/paste 代码, 在合成图案上测量:
  1. 画布左侧/顶部未被内容覆盖的"暗条"宽度(背景色 24,24,24);
  2. 内容相对遮挡条画布的错位量(应为 0, 实测 = pad).

再以修复后的正确偏移(0,0)跑同一模拟作对照.
用法: python3 tools/probe_fallback_sim.py [w] [h] [blur]
"""

import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from subtitle_cover.core.blur import BlurPipeline    # noqa: E402

BG = (24, 24, 24)          # blur_canvas 初始底色, 与应用代码一致


def simulate(w, h, blur, paste_fixed):
    """模拟一次 root 回退渲染, 返回 (左暗条宽, 上暗条宽, 内容错位 dx,dy).

    paste_fixed=False 逐字复刻现状代码; True 为修复后行为."""
    pad = 0
    if blur > 0:
        pad = min(64, int(blur * 2))
    x, y = 500, 300          # 假想条位置(离屏幕边远, 无 clamp)
    # _grab_root 的区域计算(逐字对应实现)
    root_w, root_h = 2560, 1600
    gx = max(0, x - pad)
    gy = max(0, y - pad)
    gx2 = min(root_w, x + w + pad)
    gy2 = min(root_h, y + h + pad)
    gw, gh = gx2 - gx, gy2 - gy

    # 合成"root 快照": 马赛克 + 在绝对 (x, y)(条左上角)画白色十字标记
    rng_seed = 11
    img = Image.new('RGB', (gw, gh))
    d = ImageDraw.Draw(img)
    cell = 48
    for cy in range(0, gh, cell):
        for cx in range(0, gw, cell):
            v = ((cx // cell) * 7 + (cy // cell) * 13 + rng_seed) % 255
            d.rectangle([cx, cy, cx + cell - 1, cy + cell - 1],
                        fill=(v, 255 - v, (v * 3) % 255))
    mx, my = x - gx, y - gy           # 条左上角在抓取图内的坐标
    d.line([(mx - 40, my), (mx + 40, my)], fill=(255, 255, 255), width=6)
    d.line([(mx, my - 40), (mx, my + 40)], fill=(255, 255, 255), width=6)

    img = BlurPipeline.gaussian(img, blur)

    blur_canvas = Image.new('RGB', (w, h), BG)
    if paste_fixed:
        # 修复: 抓取图中条的对应区域, 贴到画布 (0,0)
        blur_canvas.paste(
            img.crop((max(0, x - gx), max(0, y - gy),
                      min(gx + gw, x + w) - gx,
                      min(gy + gh, y + h) - gy)),
            (0, 0))
    else:
        # 现状代码(逐字): 裁剪正确, 但 paste 偏移用了裁剪框左上角
        blur_canvas.paste(
            img.crop((max(0, x - gx), max(0, y - gy),
                      min(gx + gw, x + w) - gx,
                      min(gy + gh, y + h) - gy)),
            (max(0, x - gx), max(0, y - gy)))

    # 测量: 左侧/顶部连续背景色列/行数
    px = blur_canvas.load()
    def is_bg(p):
        return abs(p[0] - BG[0]) <= 2 and abs(p[1] - BG[1]) <= 2 \
            and abs(p[2] - BG[2]) <= 2
    left = 0
    for cx in range(w):
        if all(is_bg(px[cx, cy]) for cy in range(0, h, 16)):
            left += 1
        else:
            break
    top = 0
    for cy in range(h):
        if all(is_bg(px[cx, cy]) for cx in range(0, w, 16)):
            top += 1
        else:
            break
    # 内容错位: 白十字横线在画布中的 y 应为 0, 竖线 x 应为 0
    cross_x = cross_y = None
    for cy in range(h):
        for cx in range(0, w, 2):
            p = px[cx, cy]
            if p[0] > 240 and p[1] > 240 and p[2] > 240:
                if cross_x is None or cx < cross_x:
                    cross_x = cx
                if cross_y is None:
                    cross_y = cy
    return left, top, cross_x, cross_y


def main():
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 591
    h = int(sys.argv[2]) if len(sys.argv) > 2 else 710
    blur = int(sys.argv[3]) if len(sys.argv) > 3 else 50
    pad = min(64, int(blur * 2)) if blur > 0 else 0
    print('参数: 条 %dx%d blur=%d -> pad=%d' % (w, h, blur, pad))
    for fixed in (False, True):
        left, top, cx, cy = simulate(w, h, blur, fixed)
        tag = '修复后' if fixed else '现状代码'
        print('%s: 左暗条=%dpx 上暗条=%dpx 十字位置=(%s,%s) (应≈(0,0))'
              % (tag, left, top, cx, cy))


if __name__ == '__main__':
    main()
