#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""图像渲染管线: 高斯模糊 / 色罩 / 噪点 / 羽化边缘.

纯图像处理, 不接触任何 X 资源(可在渲染线程安全使用).
所有大图缓存按 (尺寸, 参数) 键控复用, 避免每帧重复分配.
"""

from PIL import Image, ImageFilter, ImageChops

from ..core.config import clamp


class BlurPipeline:

    _noise_cache = None   # 噪点纹理缓存 (按尺寸)
    _mask_cache = None    # 羽化渐变蒙版缓存 (按尺寸+羽化宽度)

    @staticmethod
    def gaussian(img, sigma):
        """高斯模糊. 强度大时先缩小再模糊再放大, 兼顾速度与质量."""
        if sigma <= 0:
            return img
        if sigma > 8:
            down = 4
            sw = max(1, img.width // down)
            sh = max(1, img.height // down)
            small = img.resize((sw, sh), Image.BILINEAR)
            small = small.filter(ImageFilter.GaussianBlur(sigma / down))
            return small.resize((img.width, img.height), Image.BILINEAR)
        return img.filter(ImageFilter.GaussianBlur(sigma))

    @classmethod
    def post_process(cls, img, tint_rgb, tint_alpha, noise):
        """在模糊层上叠加色罩与噪点."""
        if tint_alpha > 0 and tint_rgb is not None:
            overlay = Image.new('RGB', img.size, tuple(tint_rgb))
            img = Image.blend(img, overlay, clamp(tint_alpha, 0.0, 1.0))
        if noise:
            tex = cls.noise_layer(img.size)
            img = ImageChops.overlay(img, tex)
        return img

    @classmethod
    def noise_layer(cls, size):
        """生成/缓存磨砂噪点纹理."""
        if cls._noise_cache is None or cls._noise_cache.size != size:
            img = Image.effect_noise(size, 12).convert('RGB')
            cls._noise_cache = img
        return cls._noise_cache

    @classmethod
    def feather_mask(cls, size, feather):
        """生成/缓存羽化渐变蒙版 (L 模式).

        蒙版取值 0(边缘)~255(中心): 模糊层乘蒙版、清晰原图乘反蒙版,
        叠加后即得到"边缘渐变清晰、中心磨砂"的羽化效果.
        四边线性渐变逐像素取较小值, 角部自然收敛."""
        key = (size, feather)
        if cls._mask_cache is None or cls._mask_cache[0] != key:
            w, h = size
            f = max(1, min(feather, min(w, h) // 2))
            col = Image.new('L', (w, 1))
            row = Image.new('L', (1, h))
            cp, rp = col.load(), row.load()
            for i in range(w):
                cp[i, 0] = min(255, int(255 * min(i, w - 1 - i) / f))
            for j in range(h):
                rp[0, j] = min(255, int(255 * min(j, h - 1 - j) / f))
            mask = Image.new('L', (w, h), 255)
            grad_x = col.resize((w, h))
            grad_y = row.resize((w, h))
            mask = ImageChops.darker(mask, grad_x)
            mask = ImageChops.darker(mask, grad_y)
            cls._mask_cache = (key, mask)
        return cls._mask_cache[1]

    @classmethod
    def compose_feather(cls, blur_canvas, sharp_canvas, feather):
        """把清晰画布与模糊画布按羽化蒙版混合.

        边缘处显示清晰画面(与背景无缝衔接), 中心保持磨砂."""
        mask = cls.feather_mask(blur_canvas.size, feather)
        return Image.composite(blur_canvas, sharp_canvas, mask)
