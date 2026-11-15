#!/bin/bash
# QQ 分卷邮递 - macOS 图形界面启动入口（双击即可运行）
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
    echo "未找到 python3，请安装 Python 3.10 或更高版本。"
    read -r -p "按回车键退出..."
    exit 1
fi

exec python3 launch_gui.pyw "$@"
