#!/bin/bash
# ダブルクリックでSkookumNetブリッジが起動しブラウザが開きます
cd "$(dirname "$0")"

if ! command -v uv &>/dev/null; then
  echo "エラー: uv が見つかりません。以下のコマンドでインストールしてください:"
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
  echo "インストール後、新しいターミナルウィンドウで再実行してください。"
  read -r -p "Enterキーで閉じます..."
  exit 1
fi

pkill -f "bichoc_bridge.py" 2>/dev/null
sleep 0.3

# 依存はここで直接指定するため、プロジェクトファイル等は不要です
# (--no-project: 同じフォルダに無関係な pyproject.toml があっても影響を受けない)
uv run --no-project --python 3.13 \
  --with pyobjc-framework-MultipeerConnectivity --with websockets \
  python bridge/bichoc_bridge.py "$@" &
CLIENT_PID=$!

sleep 1.0

open contest_log_analyzer.html

echo ""
echo "SkookumNetブリッジ 起動中 (PID: $CLIENT_PID)"
echo "SkookumLogger側でSkookumNetウィンドウを開き、Joinしてください。"
echo "このウィンドウを閉じると停止します。"

trap "kill $CLIENT_PID 2>/dev/null" INT TERM EXIT
wait $CLIENT_PID
