#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""字幕遮挡条入口: python3 -m subtitle_cover / ./run.py"""

import sys

from subtitle_cover.core.config import CONFIG_DIR, LOCK_FILE


def acquire_single_instance_lock():
    """单实例锁: 重复启动时直接退出并提示."""
    import fcntl
    import os
    os.makedirs(CONFIG_DIR, exist_ok=True)
    f = open(LOCK_FILE, 'w')
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return f  # 保持引用, 防止锁释放
    except OSError:
        print('字幕遮挡条已在运行 (若不见踪影可按 Ctrl+Alt+B 唤出).',
              file=sys.stderr)
        sys.exit(0)


def main():
    lock = acquire_single_instance_lock()
    del lock
    from Xlib.error import DisplayError
    try:
        from subtitle_cover.app import App
        app = App()
    except DisplayError:
        print('无法连接 X 显示服务器(请确认运行在 X11 会话中).',
              file=sys.stderr)
        return 1
    app.run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
