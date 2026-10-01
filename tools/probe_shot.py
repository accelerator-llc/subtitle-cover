#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_shot: 截取「含遮挡条真实画面」的屏幕截图(诊断/出图工具).

机制(截图普查实证, REFACTOR_PLAN §5.15): 常规 root 直抓(scrot/import/xwd
等)拿到的是 mutter 极少写回的 root 快照, 条区域是陈旧磨砂帧甚至缺失;
org.gnome.Shell.Screenshot D-Bus 被 GNOME 41+ 白名单拒绝外部调用; 可靠
路径 = xdg-desktop-portal Screenshot(interactive=false 免弹窗), 由
gnome-shell 从合成输出读像素, 条区与条自身内容逐像素一致(推荐用法).
单次调用一次完成, 不做周期采样; 全程不做 NameWindowPixmap 调用(陷阱14),
截完自检条渲染线程仍在更新.

用法:
  python3 tools/probe_shot.py                          # 条区域+边距 -> /tmp/sc_shot.png
  python3 tools/probe_shot.py -o hero.png --margin 0 --rect 480,350,1600,900
  python3 tools/probe_shot.py --win 0x1a000ee -o panel.png   # 直抓窗口完整内容(保留 alpha)
"""
import argparse
import json
import os
import sys
import time
import urllib.parse

from _xprobe import XProbe   # 探针公共库(tools/ 内相对导入)


def bar_xid():
    with open(os.path.expanduser('~/.config/subtitle-cover/bar_xid')) as f:
        return int(f.read().strip(), 0)


def grab_portal():
    """portal 截图(interactive=false, 免弹窗) -> PIL RGB 全屏图; 失败 None."""
    import gi
    gi.require_version('Gio', '2.0')
    from gi.repository import Gio, GLib
    loop = GLib.MainLoop()
    out, want = {}, []

    def on_resp(conn, sender, path, iface, signal, params):
        if want and path != want[0]:
            return
        out['code'] = params[0]
        out['res'] = {k: str(v) for k, v in params[1].items()}
        loop.quit()

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    # 先订阅再调用: portal 响应可能快于 call_sync 返回, 后订阅会丢信号
    bus.signal_subscribe(None, 'org.freedesktop.portal.Request', 'Response',
                         None, None, Gio.DBusSignalFlags.NONE, on_resp)
    handle = bus.call_sync(
        'org.freedesktop.portal.Desktop', '/org/freedesktop/portal/desktop',
        'org.freedesktop.portal.Screenshot', 'Screenshot',
        GLib.Variant('(sa{sv})', ('', {
            'handle_token': GLib.Variant('s', 'scprobe%d' % os.getpid()),
            'interactive': GLib.Variant('b', False)})),
        GLib.VariantType('(o)'), Gio.DBusCallFlags.NONE, 3000, None)
    want.append(handle.unpack()[0])
    GLib.timeout_add(9000, loop.quit)
    loop.run()
    uri = urllib.parse.unquote(out.get('res', {}).get('uri', ''))
    path = uri[len('file://'):] if uri.startswith('file://') else ''
    for _ in range(60):                  # portal 响应先于文件落盘, 轮询等
        if path and os.path.exists(path):
            break
        time.sleep(0.05)
    if out.get('code') != 0 or not path or not os.path.exists(path):
        return None
    from PIL import Image
    img = Image.open(path).convert('RGB')
    os.remove(path)                      # 临时产物不留在用户图片目录
    return img


def mad(a, b):
    """灰度平均绝对差(自检指标)."""
    from PIL import ImageChops, ImageStat
    return ImageStat.Stat(ImageChops.difference(
        a.convert('L'), b.convert('L'))).mean[0]


def main():
    ap = argparse.ArgumentParser(description='含遮挡条的真实截图(经合成器, 推荐)')
    ap.add_argument('-o', default='/tmp/sc_shot.png', help='输出 PNG 路径')
    ap.add_argument('--margin', type=int, default=160,
                    help='条四周外扩边距 px(默认 160)')
    ap.add_argument('--rect', default='',
                    help='显式裁剪区 x,y,w,h(物理像素; 优先于 --margin)')
    ap.add_argument('--win', type=lambda s: int(s, 0), default=0,
                    help='改为直抓指定 XID 窗口完整内容(保留 alpha)')
    args = ap.parse_args()

    xp = XProbe(os.environ.get('DISPLAY'))
    xid = bar_xid()
    bx, by, bw, bh = xp.window_abs_rect(xid)
    t0 = time.monotonic()
    if args.win:                                   # 直抓窗口自身(如面板)
        g = xp.window_abs_rect(args.win)
        if g is None:
            print('目标窗口不存在', file=sys.stderr)
            return 1
        win = xp.grab_pil(args.win, 0, 0, g[2], g[3])
        if win is None:
            print('窗口抓取失败', file=sys.stderr)
            return 1
        win.save(args.o)
        print('已保存 %s (%dx%d, 直抓窗口自身)' % (args.o, win.width, win.height))
        return 0
    if args.rect:
        tx, ty, tw, th = (int(v) for v in args.rect.split(','))
    else:
        tx = max(0, bx - args.margin)
        ty = max(0, by - args.margin)
        rw, rh = xp.root_geometry()
        tw = min(rw - tx, bw + 2 * args.margin)
        th = min(rh - ty, bh + 2 * args.margin)

    bar_before = xp.grab_pil(xid, 0, 0, bw, bh)
    full = grab_portal()
    if full is None:
        print('portal 截图不可用(需 GNOME 会话的 xdg-desktop-portal)',
              file=sys.stderr)
        return 1
    shot = full.crop((tx, ty, tx + tw, ty + th))
    bar_after = xp.grab_pil(xid, 0, 0, bw, bh)

    # 自检: 两次抓条内容有差异 => 渲染线程仍在更新(陷阱14 防冻结);
    # 截图条区与条自身内容 MAD<=8 => 条区新鲜且确为磨砂内容
    rep = {'backend': 'portal(合成输出, 含条)', 'rect': [tx, ty, tw, th],
           'bar_content_changed_during_capture':
               bool(mad(bar_before, bar_after) > 0.5)}
    crop = (bx - tx, by - ty, bx - tx + bw, by - ty + bh)
    if 0 <= crop[0] and crop[2] <= shot.width \
            and 0 <= crop[1] and crop[3] <= shot.height:
        m = mad(shot.crop(crop), bar_after)
        rep['bar_region_mad_vs_bar_content'] = round(m, 2)
        rep['bar_region_fresh'] = bool(m <= 8)
    rep['elapsed_s'] = round(time.monotonic() - t0, 2)
    rep['saved'] = args.o
    shot.save(args.o)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0


if __name__ == '__main__':
    sys.exit(main())
