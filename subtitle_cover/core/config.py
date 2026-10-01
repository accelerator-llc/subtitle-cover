#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""配置读写与常量."""

import os
import json

CONFIG_DIR = os.path.expanduser('~/.config/subtitle-cover')
CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
LOCK_FILE = os.path.join(CONFIG_DIR, 'run.lock')
# 默认配置. 坐标/尺寸均为 X 物理像素(root 坐标 = 屏幕视觉坐标, 已实测).
DEFAULT_CONFIG = {
    'x': None,                     # None: 首次运行自动放到屏幕底部居中
    'y': None,
    'w': 1800,
    'h': 110,
    'blur': 14,                    # 高斯模糊强度 0~50
    'tint_color': [64, 64, 64],    # 色罩 RGB
    'tint_alpha': 0.0,             # 色罩透明度 0~1 (0 = 纯无色模糊)
    'feather': 0,                  # 羽化边缘宽度 0~64 (0 = 关闭; 默认关,
                                 # 边缘会露出清晰画面, 对遮字幕场景不佳)
    'noise': False,                # 磨砂噪点
    'fps': 15,                     # 渲染上限 (帧/秒)
    'locked': False,               # 锁定: 鼠标事件完全穿透
    'visible': True,               # 遮挡条是否显示
    'peek_key': 'o',               # 切换透视的组合键字母 (Ctrl+Alt+<key>)
    'note_with_shot': True,        # 快速笔记附带屏幕截图
    'notes_dir': '~/.local/share/subtitle-cover',
                                   # 笔记与截图的保存文件夹(支持 ~)
    'presets': {},                 # 位置预设: 名称 -> {x,y,w,h}
    'preset': '',                  # 当前使用的预设名
    'hint_seconds': 4,             # 轮廓提示持续秒数(0 = 关闭)
}

# 全局热键 (keysym 字符, 修饰掩码), 可在 config.json 中改为其他键
DEFAULT_HOTKEYS = {
    'show': 'b',    # Ctrl+Alt+B       显示/隐藏
    'lock': 'l',    # Ctrl+Alt+Shift+L 锁定/解锁 (Ctrl+Alt+L 是 GNOME 锁屏, 故意错开)
    'note': 'n',    # Ctrl+Alt+N       快速笔记
    'panel': 'c',   # Ctrl+Alt+C       控制面板(锁定穿透时也能呼出)
    'preset': 'p',  # Ctrl+Alt+P       位置预设循环
}

MIN_W, MIN_H = 120, 8              # 遮挡条最小尺寸
MAX_BLUR = 50


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def load_config():
    """加载配置(不存在则补全默认值)."""
    cfg = dict(DEFAULT_CONFIG)
    cfg['hotkeys'] = dict(DEFAULT_HOTKEYS)
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for k in cfg:
            if k in data:
                cfg[k] = data[k]
        # hotkeys 是整体覆盖: 老版本配置可能只存了部分键, 补齐默认值,
        # 否则面板/笔记热键会静默消失且被再次写回配置
        hk = cfg.get('hotkeys')
        if not isinstance(hk, dict):
            hk = {}
        for k, v in DEFAULT_HOTKEYS.items():
            hk.setdefault(k, v)
        cfg['hotkeys'] = hk
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    """保存配置(原子写入)."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        tmp = CONFIG_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_FILE)
    except OSError:
        pass
