#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""渲染线程: 每帧 抓取 -> 模糊 -> 写回, 与主循环解耦."""

import sys
import time
import threading


class RenderThread(threading.Thread):
    """独立渲染线程.

    渲染(抓取/模糊/写回)是同步阻塞调用, 放在 GLib 主循环里会因
    X server 忙时阻塞事件响应(拖动/热键卡顿), 也会产生渲染跳拍;
    独立线程后主循环零渲染负担, 渲染节奏也只受自身影响.
    配置(cfg 字典)跨线程只做简单读取, GIL 下安全.

    性能: 连续静止若干帧后自动把探测频率降到 2fps(视频暂停/
    看文档时接近零 CPU), 画面一有变化立即恢复满帧率.
    """

    IDLE_FRAMES_BEFORE_SLOWDOWN = 10   # 连续静止 N 帧后降频
    IDLE_INTERVAL = 0.5                # 降频后的探测间隔(秒)

    def __init__(self, app):
        super().__init__(daemon=True)
        self.app = app
        self.stop_flag = False
        self._idle_frames = 0

    def run(self):
        while not self.stop_flag:
            cfg = self.app.cfg
            fps = max(2, cfg.get('fps', 15))
            t0 = time.monotonic()
            if cfg.get('visible') and not self.app.locked_now:
                try:
                    bar = self.app.bar
                    # 每帧都让透视系数向目标值收敛(按住->0, 松开->1)
                    bar.update_peek()
                    before = bar._last_raw
                    bar.render(cfg)
                    # 静止检测: 本帧数据与上帧相同即视为静止
                    # (轮廓提示期间视为活跃, 蚂蚁线需要持续动画)
                    if bar.hint_active() or bar.interactive:
                        self._idle_frames = 0
                    if bar._last_raw == before:
                        self._idle_frames += 1
                    else:
                        self._idle_frames = 0
                except Exception as e:
                    # 渲染线程内任何单帧异常都不终止循环
                    print('渲染线程异常:', e, file=sys.stderr)
            else:
                self._idle_frames = 0
            # 基础间隔; 长时间静止后降到低频探测
            interval = 1.0 / fps
            if self._idle_frames >= self.IDLE_FRAMES_BEFORE_SLOWDOWN:
                interval = max(interval, self.IDLE_INTERVAL)
            elapsed = time.monotonic() - t0
            time.sleep(max(0.005, interval - elapsed))
