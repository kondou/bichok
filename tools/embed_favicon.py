#!/usr/bin/env python3
"""Embed icons/ の SVG/PNG を HTML の <head> に favicon 等として埋め込む。

使い方（リポジトリルートで）:
    python3 tools/embed_favicon.py contest_log_analyzer.html

- 既存の favicon 系 <link>（このスクリプトが挿した BEGIN/END マーカー区間）は置換する。
  無ければ <title>...</title> の直後に挿入する。
- 埋め込むのは 3 点:
    rel="icon"             image/svg+xml  (icons/FINAL_BicHok_pureblack.svg)
    rel="icon"  48x48      image/png      (icons/FINAL_BicHok_pureblack_48.png)
    rel="apple-touch-icon" 180x180        (icons/FINAL_BicHok_pureblack_180.png)
"""
import base64
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ICONS = ROOT / "icons"

BEGIN = "<!-- favicon:begin -->"
END = "<!-- favicon:end -->"


def b64(path: pathlib.Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def favicon_block() -> str:
    svg = b64(ICONS / "FINAL_BicHok_pureblack.svg")
    png48 = b64(ICONS / "FINAL_BicHok_pureblack_48.png")
    png180 = b64(ICONS / "FINAL_BicHok_pureblack_180.png")
    return (
        f"{BEGIN}\n"
        f'<link rel="icon" type="image/png" sizes="48x48" href="data:image/png;base64,{png48}">\n'
        f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml;base64,{svg}">\n'
        f'<link rel="apple-touch-icon" sizes="180x180" href="data:image/png;base64,{png180}">\n'
        f"{END}"
    )


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    target = pathlib.Path(sys.argv[1])
    html = target.read_text(encoding="utf-8")
    block = favicon_block()

    marker = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if marker.search(html):
        html = marker.sub(block, html, count=1)
        action = "replaced"
    else:
        m = re.search(r"</title>", html)
        if not m:
            print(f"error: no </title> in {target}")
            return 1
        html = html[: m.end()] + "\n" + block + html[m.end() :]
        action = "inserted"

    target.write_text(html, encoding="utf-8")
    print(f"{action}: {target} (+{len(block)} bytes block)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
