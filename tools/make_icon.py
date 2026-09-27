#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成应用图标 (repo 资源与托盘共用同一设计)."""

from PIL import Image, ImageDraw

img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)

# 屏幕外框
d.rounded_rectangle([16, 40, 240, 216], radius=32,
                    outline=(90, 90, 100, 255), width=12)
# 顶部两条"原文字幕"(细线示意)
d.rectangle([48, 64, 208, 72], fill=(200, 200, 205, 160))
# 中部磨砂遮挡条(渐变横条, 由实到虚模拟磨砂)
for i in range(10):
    a = 235 - i * 20
    d.rectangle([40, 100 + i * 6, 216, 105 + i * 6], fill=(110, 160, 215, a))
# 底部"英文字幕"(保留行)
d.rectangle([48, 176, 180, 184], fill=(240, 240, 240, 200))

img.save('icon.png')
print('icon.png 已生成 (256x256)')
