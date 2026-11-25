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

# 動いているブリッジがあれば止めてから起動する（ページへの配信ポートは 1 つのブリッジしか使えない）。
# 別のフォルダ（古い版かもしれない）のブリッジを黙って止めないよう、止める相手の場所と版を表示する。
# 場所はプロセスの作業フォルダから調べるので、稼働中にフォルダ名を変えていれば変更後の名前が出る。
bridge_folder() {
  local pid=$1 cwd script
  cwd=$(lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p')
  script=$(ps -o args= -p "$pid" | tr ' ' '\n' | grep 'bichok_bridge\.py$' | head -n 1)
  [ -n "$cwd" ] && [ -n "$script" ] || return 0   # 調べる間に終わったプロセス
  case "$script" in
    /*) ;;
    *) script="$cwd/$script" ;;
  esac
  (cd "$(dirname "$script")/.." 2>/dev/null && pwd -P)
}
folder_version() {
  local version
  version=$(sed -n "s/^[[:space:]]*const APP_VERSION = '\([^']*\)';.*/\1/p" "$1/contest_log_analyzer.html" 2>/dev/null | head -n 1)
  echo "${version:-不明}"
}
HERE=$(pwd -P)
RUNNING=$(for pid in $(pgrep -f "bichok_bridge.py"); do bridge_folder "$pid"; done | sort -u)
if [ -n "$RUNNING" ]; then
  while IFS= read -r folder; do
    [ -n "$folder" ] || continue
    echo "動いているブリッジを止めます: ${folder}（版 $(folder_version "$folder")）"
    if [ "$folder" != "$HERE" ]; then
      echo "  ※ ここ（${HERE}、版 $(folder_version "$HERE")）とは別のフォルダの BicHok です"
    fi
  done <<< "$RUNNING"
fi

pkill -f "bichok_bridge.py" 2>/dev/null
sleep 0.3

# 依存はここで直接指定するため、プロジェクトファイル等は不要です
# (--no-project: 同じフォルダに無関係な pyproject.toml があっても影響を受けない)
uv run --no-project --python 3.13 \
  --with pyobjc-framework-Cocoa --with websockets \
  python bridge/bichok_bridge.py "$@" &
CLIENT_PID=$!

sleep 1.0

open contest_log_analyzer.html

echo ""
echo "SkookumNetブリッジ 起動中 (PID: $CLIENT_PID)"
echo "SkookumLogger側でSkookumNetウィンドウを開き、Joinしてください。"
echo "このウィンドウを閉じると停止します。"

trap "kill $CLIENT_PID 2>/dev/null" INT TERM EXIT
wait $CLIENT_PID
