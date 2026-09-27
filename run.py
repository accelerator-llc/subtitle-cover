#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕遮挡条 (Subtitle Cover) 启动入口.

用法: ./run.py  或  python3 -m subtitle_cover
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from subtitle_cover.__main__ import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
