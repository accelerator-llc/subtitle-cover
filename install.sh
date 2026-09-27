#!/usr/bin/env bash
# 字幕遮挡条 (Subtitle Cover) 安装脚本
# 用法: ./install.sh [--autostart]
#   --autostart  同时设置开机自启
#
# 本脚本只写入用户目录, 不需要 root:
#   1. 检测 Python / GTK / Pillow 依赖
#   2. 缺 python-xlib 或 python3-gi-cairo 时, 从发行版仓库下载
#      deb 解包到项目 .deps/ (随附副本已存在则跳过), 无需 root
#   3. 安装桌面菜单入口
#   4. 生成托盘图标

set -e
cd "$(dirname "$0")"

AUTOSTART=0
[ "$1" = "--autostart" ] && AUTOSTART=1

echo "==> 字幕遮挡条安装"

# ---------- 1. 基础依赖 ----------
fail() { echo "  [缺少] $1" >&2; FAILED=1; }
FAILED=0

command -v python3 >/dev/null || fail "python3"
python3 -c "import gi" 2>/dev/null || fail "python3-gi (PyGObject)"
python3 -c "import gi; gi.require_version('Gtk','3.0')" 2>/dev/null || fail "gir1.2-gtk-3.0"
python3 -c "import PIL" 2>/dev/null || fail "python3-pil (Pillow)"
python3 -c "import Xlib" 2>/dev/null || NEED_XLIB=1
python3 -c "import gi; from gi.repository import Gtk, Gdk; w=Gtk.Window()" 2>/dev/null || NEED_GI_CAIRO=1

if [ "$FAILED" = 1 ]; then
    cat >&2 <<'EOT'

  请先安装上述系统包, 例如 Ubuntu/Debian:
    sudo apt install python3 python3-gi gir1.2-gtk-3.0 python3-pil
  然后重新运行本脚本.
EOT
    exit 1
fi
echo "  [OK] 基础依赖齐全"

# ---------- 2. python-xlib / gi-cairo (无 root 时用项目自带副本) ----------
DEPS_DIR=".deps/extracted/usr/lib/python3/dist-packages"
if [ -n "$NEED_XLIB" ] || [ -n "$NEED_GI_CAIRO" ]; then
    if [ -d "$DEPS_DIR" ] && python3 -c "
import sys; sys.path.insert(0, '$DEPS_DIR'); import Xlib" 2>/dev/null; then
        echo "  [OK] 使用项目随附的 .deps 副本 (python-xlib / gi-cairo)"
    else
        echo "  [..] 系统缺 python-xlib / python3-gi-cairo, 尝试无 root 方案..."
        mkdir -p .deps && cd .deps
        apt download python3-xlib python3-gi-cairo 2>/dev/null || {
            echo "  [失败] apt download 不可用, 请手动安装:" >&2
            echo "         sudo apt install python3-xlib python3-gi-cairo" >&2
            exit 1
        }
        for d in *.deb; do dpkg-deb -x "$d" extracted/; done
        cd ..
        echo "  [OK] 已解包到 .deps/"
    fi
else
    echo "  [OK] 系统已安装 python-xlib / gi-cairo"
fi

# ---------- 3. 托盘支持检测 (可选) ----------
if python3 -c "
import gi
try:
    gi.require_version('AyatanaAppIndicator3', '0.1')
    from gi.repository import AyatanaAppIndicator3
except (ValueError, ImportError):
    raise SystemExit(1)" 2>/dev/null; then
    echo "  [OK] 托盘可用 (AyatanaAppIndicator3)"
else
    echo "  [提示] 未安装 AppIndicator, 托盘不可用(热键不受影响)."
    echo "         如需托盘: sudo apt install gir1.2-ayatanaappindicator3-0.1"
fi

# ---------- 4. 图标与桌面入口 ----------
mkdir -p ~/.local/share/applications
python3 - <<'PY'
import os
from PIL import Image, ImageDraw
cfg_dir = os.path.expanduser('~/.config/subtitle-cover')
os.makedirs(cfg_dir, exist_ok=True)
icon = os.path.join(cfg_dir, 'icon.png')
img = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([16, 40, 240, 216], radius=32, outline=(90, 90, 100, 255), width=12)
for i in range(8):
    a = 220 - i * 24
    d.rectangle([48, 108 + i * 8, 208, 114 + i * 8], fill=(110, 160, 215, a))
img.save(icon)
print("  [OK] 图标: %s" % icon)
PY

sed "s|Exec=.*|Exec=python3 $PWD/run.py|; s|Path=.*|Path=$PWD|" \
    subtitle-cover.desktop > ~/.local/share/applications/subtitle-cover.desktop
echo "  [OK] 桌面入口: ~/.local/share/applications/subtitle-cover.desktop"

# ---------- 5. 开机自启 (可选) ----------
if [ "$AUTOSTART" = 1 ]; then
    mkdir -p ~/.config/autostart
    cp ~/.local/share/applications/subtitle-cover.desktop ~/.config/autostart/
    echo "  [OK] 已设置开机自启"
fi

# ---------- 完成 ----------
cat <<'EOT'

  安装完成!
  - 应用菜单搜 "字幕遮挡条" 或 "Subtitle Cover" 启动
  - 或命令行: ./run.py
  - 常用热键: Ctrl+Alt+B 显隐 | Ctrl+Alt+Shift+L 锁定 | Ctrl+Alt+N 笔记
EOT
