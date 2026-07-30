# BICHOK

[日本語 README](README.ja.md)

BICHOK visualises amateur-radio contest logs in a browser: cumulative QSOs, rate
and its trends, band breakdown, and the operating mode you were actually in
(Run, S&P, SO2R, 2BSIQ) reconstructed from the log itself. Load two logs and
they are drawn on one time axis for comparison. On macOS it can also follow a
contest live from SkookumLogger, as it is being logged.

The name is short for Butt In Chair, Hands On Keyboard, and is pronounced
bikhɔ́k.

![BICHOK screenshot](docs/images/en/A-2.png)

## Features

- Cabrillo and ADIF, with cumulative QSOs and rate on one chart
- Rate trends: Rate / EMA / LOESS / ACCEL, each independently switchable
- Band breakdown chart
- Operating mode bar — Run(1R), S&P(1R), SO2R, 2BSIQ, No QSO and Off Time,
  detected from the log
- Base rate line to compare against a target pace
- Off Time skipping, detected automatically for WPX and WAE
- Multiple logs overlaid on one time axis, aligned by contest start or first
  QSO, with a per-log offset
- Panes: rolling windows showing the last N hours at an expanded scale, several
  at once, fixed or following
- Simulation playback, replaying a finished log as if it were live
- Live reception from SkookumLogger over SkookumNet (macOS)
- Daylight shading and sunrise/sunset lines from your grid locator
- Session save and restore in the browser, and a Japanese/English switch
- Works on a phone, in either orientation

## Requirements

- A modern browser (Chrome / Chromium, Safari, Firefox, Edge)
- Nothing else: the page runs from the filesystem, with no server, no Python
  and no installation
- For live reception only: macOS, SkookumLogger, and
  [uv](https://docs.astral.sh/uv/) — the launcher prints the install command if
  `uv` is missing

| Platform | Log file analysis | SkookumNet live connection |
|---|---|---|
| macOS | Yes | Yes |
| Windows | Yes | No — the button is not shown |
| Linux | Yes | No — the button is not shown |
| WSL2 | Yes | No — the button is not shown |

Where live reception is unavailable, only that button is hidden; everything else
works. A log exported from any logger can still be loaded as a file.

## Quick start

1. Download the [latest release zip](https://github.com/kondou/bichok/releases/latest/download/bichok-latest.zip)
   (or clone this repository) and extract it anywhere you like — the folder name
   does not matter. That link always points to the newest version, so it is
   worth bookmarking. Keep `contest_log_analyzer.html` and `chart.min.js`
   together.
2. Open the page:
   - **Windows** — double-click `contest_log_analyzer.html`, or run
     `start_bichok.bat` if that does not open your browser
   - **macOS** — double-click `contest_log_analyzer.html`, or
     `start_skookumnet.command` to bring up the SkookumNet bridge as well
     (first run only: right-click → Open)
   - **Linux** — open the page, or `bash start_bichok.sh`
   - **WSL2** — `bash start_bichok.sh` from a WSL2 terminal, which hands the
     page to the Windows-side browser. Installing `wslu`
     (`sudo apt install wslu`) lets it open the browser by itself
3. Drag a Cabrillo or ADIF file onto the window, or use **Open file**

> **Windows: "The publisher could not be verified"** — clicking Run starts it
> anyway. To stop the warning coming back, open PowerShell in the extracted
> folder and run:
> ```powershell
> Get-ChildItem -Recurse | Unblock-File
> ```
> Files from a freshly downloaded zip carry the internet-zone mark again, so
> re-run this after updating.

To update, extract a newer zip over the same folder and confirm overwrite.
Nothing outside the folder is involved, and there is no data of your own in it —
saved sessions live in the browser.

BICHOK needs no internet connection and never sends your log anywhere. Its only
network use is the WebSocket to the SkookumNet bridge, which runs on your own
machine.

## Documentation

- [User guide (English)](docs/contest_log_analyzer_en.md)
- [ユーザーガイド（日本語）](docs/contest_log_analyzer_ja.md)

Both are also in the release zip as single self-contained HTML files
(`contest_log_analyzer_en.html` / `contest_log_analyzer_ja.html`), readable
offline.

The SkookumNet bridge has [its own README](bridge/README.md) covering the
protocol it speaks and its command-line options.

## License

[MIT](LICENSE). The bridge is separately MIT-licensed and shares no code with
any other SkookumNet client.
