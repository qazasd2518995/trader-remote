# 黃金跟單會員端

Member client for the gold copy-trading system. Connects to a remote Hub
(hosted in the cloud), polls for parsed signals, and executes the trades on
the local MT5 terminal via the file bridge EA.

## Build

### Windows (`.exe`)

```bat
py -m pip install -r requirements.txt
py -m PyInstaller --noconfirm packaging\pyinstaller\client-windows.spec
```

Output: `dist\黃金跟單會員端\黃金跟單會員端.exe`

### macOS (`.app` / `.dmg`)

```sh
python3 -m pip install -r requirements.txt
python3 -m PyInstaller --noconfirm packaging/pyinstaller/client-macos.spec
```

Output: `dist/黃金跟單會員端.app` — package into a `.dmg` with `hdiutil`:

```sh
hdiutil create -volname "黃金跟單會員端" \
  -srcfolder dist/黃金跟單會員端.app \
  -ov -format UDZO \
  dist/黃金跟單會員端.dmg
```

## Layout

```
copy_trader/
  central/
    client_agent_web.py    # PyInstaller entry
    web_launcher.py        # Browser-based control panel
    mt5_client_agent.py    # Polls Hub, dispatches to MT5
  trade_manager/           # Order state machine
  signal_parser/           # regex_parser only (AI parsers are cloud-side)
  platform/                # Windows / macOS abstractions
  config.py                # Shared config (no AI keys in client build)
mt5_ea/
  MT5_File_Bridge_Enhanced.mq5   # MT5 EA — copy to MT5 MQL5/Experts/
packaging/pyinstaller/
  client-windows.spec
  client-macos.spec
```

## Setup on member machine

1. Install MT5 and load `MT5_File_Bridge_Enhanced.mq5` onto a chart.
2. Run the built `黃金跟單會員端` app.
3. In the browser control panel, fill in **Hub URL** and **Hub 密碼**.
4. Click **啟動服務**. Trades from the cloud Hub will be relayed to MT5.
