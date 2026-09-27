#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕遮挡条 (Subtitle Cover) 包入口.

依赖注入: 优先加载随附的 .deps 解包目录(python-xlib / python3-gi-cairo),
使程序在未安装相应系统包的机器上也能直接运行.
本模块必须最先被导入, 以保证注入发生在所有 Xlib/GTK 导入之前.
"""

import os
import sys

__version__ = "2.0.0"

# ---------------------------------------------------------------- 依赖注入
_HERE = os.path.dirname(os.path.abspath(__file__))
_DEPS = os.path.normpath(os.path.join(_HERE, '..', '.deps', 'extracted',
                                      'usr', 'lib', 'python3',
                                      'dist-packages'))
if os.path.isdir(_DEPS) and _DEPS not in sys.path:
    sys.path.insert(0, _DEPS)
