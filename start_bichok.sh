#!/bin/bash
# BicHoc launcher for Linux and WSL2 (it works on macOS too).
#
# BicHoc is a single page that runs straight from the filesystem: there is no
# server to start and nothing to install, so this script only opens the page in
# your default browser. Opening contest_log_analyzer.html by hand does the same
# thing; this exists mainly for WSL2, where the page has to be handed to the
# Windows-side browser.
#
# SkookumNet live reception is macOS-only and has its own launcher,
# start_skookumnet.command.

cd "$(dirname "$0")" || exit 1

PAGE="contest_log_analyzer.html"

# ---- i18n -------------------------------------------------------------------
if [[ "${LANG:-}" == ja* ]] || [[ "${LC_ALL:-}" == ja* ]] || [[ "${LANGUAGE:-}" == ja* ]]; then
  _bh_L=ja
else
  _bh_L=en
fi
_bh_msg() { [[ "$_bh_L" == ja ]] && echo "$1" || echo "$2"; }
# -----------------------------------------------------------------------------

if [ ! -f "$PAGE" ]; then
  _bh_msg "$PAGE が見つかりません。このスクリプトは $PAGE と同じフォルダに置いてください。" \
          "$PAGE was not found. Keep this script in the same folder as $PAGE."
  exit 1
fi

if [ ! -f chart.min.js ]; then
  _bh_msg "警告: chart.min.js が同じフォルダにありません。グラフが描画されません。" \
          "Warning: chart.min.js is not in this folder, so no chart will be drawn." >&2
fi

TARGET="$PWD/$PAGE"

# open: macOS. wslview: WSL2 (opens the Windows-side default browser, if wslu
# is installed). xdg-open: typical Linux desktop. If none are available, print
# the path and let the user open it.
if command -v open >/dev/null 2>&1; then
  open "$TARGET"
elif command -v wslview >/dev/null 2>&1; then
  wslview "$TARGET"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "$TARGET" >/dev/null 2>&1
else
  _bh_msg "ブラウザを自動で開けませんでした。以下のファイルをブラウザで開いてください:" \
          "Could not open a browser automatically. Please open this file in your browser:"
  echo "  $TARGET"
  _bh_msg "（WSL2 で Windows 側のブラウザを自動で開くには wslu が必要です: sudo apt install wslu）" \
          "(On WSL2, opening the Windows-side browser automatically needs wslu: sudo apt install wslu)"
fi

unset -f _bh_msg 2>/dev/null
unset _bh_L 2>/dev/null
