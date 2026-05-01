# 黃金跟單系統 — 完整原始碼

兩支獨立執行檔同源，由 `role` 切換：

- **黃金跟單會員端** (`client`) — Member client；poll 雲端 Hub、把指令丟給本機 MT5。
- **黃金訊號中心** (`central`) — Signal collector；抓 LINE 群訊號、推到雲端 Hub。

雲端 Hub 部署在 fly.io（見 `Dockerfile` + `fly.toml`），會員端與訊號中心都連那一個 Hub。

## 給客戶（會員端 only）

直接下載 Windows 安裝檔執行：

```
https://github.com/qazasd2518995/trader-remote/raw/main/releases/黃金跟單會員端_安裝檔.exe
```

## 從原始碼 build（自己編譯）

### 會員端（黃金跟單會員端）

**Windows**
```bat
py -m pip install -r requirements.txt
py -m PyInstaller --noconfirm packaging\pyinstaller\client-windows.spec
```
輸出：`dist\黃金跟單會員端\黃金跟單會員端.exe`

製作安裝檔（需安裝 [Inno Setup](https://jrsoftware.org/isinfo.php)）：
```bat
ISCC packaging\inno\client-windows.iss
```
→ `dist\installers\黃金跟單會員端_安裝檔.exe`

**macOS**
```sh
python3 -m pip install -r requirements.txt
python3 -m PyInstaller --noconfirm packaging/pyinstaller/client-macos.spec
hdiutil create -volname "黃金跟單會員端" \
  -srcfolder dist/黃金跟單會員端.app \
  -ov -format UDZO \
  dist/黃金跟單會員端.dmg
```

### 訊號中心（黃金訊號中心）

**Windows**
```bat
build_signal_center_windows.bat
```
輸出：`dist\黃金訊號中心\黃金訊號中心.exe`，安裝檔在 `dist\installers\黃金訊號中心_安裝檔.exe`（如果有裝 Inno Setup）

**macOS**
```sh
python3 -m pip install -r requirements.txt
python3 -m PyInstaller --noconfirm packaging/pyinstaller/central-macos.spec
```

訊號中心需要 `pywin32`（Windows）或 macOS 的 LINE 桌面版（剪貼板擷取）才能運作。

### 雲端 Hub（fly.io）

```sh
fly deploy
fly secrets set COPY_TRADER_HUB_TOKEN=<your-token>
```

## 目錄結構

```
copy_trader/
  central/
    client_agent_web.py            # 會員端 PyInstaller entry
    web_launcher.py                # 瀏覽器控制台（兩個 role 共用）
    mt5_client_agent.py            # 會員端：poll Hub → 寫 MT5 commands
    central_signal_center_web.py   # 訊號中心 PyInstaller entry
    signal_collector.py            # 訊號中心：LINE → 解析 → 推 Hub
  signal_capture/                  # LINE 剪貼板抓取（訊號中心專用）
  signal_parser/                   # regex_parser 共用；AI parser 為 optional import
  trade_manager/                   # 會員端訂單狀態機 + 馬丁
  platform/                        # Windows / macOS abstractions
  config.py                        # 共享 config dataclass（AI keys 已清空）
mt5_ea/
  MT5_File_Bridge_Enhanced.mq5     # MT5 EA — 接會員端寫的 commands.json
packaging/
  pyinstaller/
    client-windows.spec  client-macos.spec
    central-windows.spec central-macos.spec
  inno/
    client-windows.iss  central-windows.iss
Dockerfile  fly.toml                # 雲端 Hub 部署（在 fly.io）
releases/                           # 預先 build 好的 Windows 安裝檔
```

## 會員端設定

啟動 `黃金跟單會員端` 後瀏覽器會自動開控制台，左欄填：

- **連線**：中央 Hub URL / Hub 密碼 / MT5 Files 路徑 / 輪詢秒數
- **下單**：預設手數 / 啟用馬丁 / 馬丁倍數 / 馬丁最大層級 / 自訂每層手數 / 分批 TP 比例
- **風控**：掛單取消秒數 / 取消偏離 % / 訊號 dedup 分鐘 / 最大同時持倉

右欄會顯示馬丁狀態 / 最近事件（收到訊號 / 送 MT5 / 跳過 / 失敗）/ MT5 訂單狀態。

## 訊號中心設定

啟動 `黃金訊號中心` 後左欄填：

- **雲端 Hub URL**：例 `https://gold-signal-hub-tw.fly.dev`
- **Hub 密碼**：與雲端 Hub `COPY_TRADER_HUB_TOKEN` secret 一致

右欄會顯示「複製內容預覽」— 每次抓到的 LINE 剪貼板原文 + 解析出的訊息。

## 架構

```
LINE 桌面 (Win/Mac)
   ↓ 全選複製 (clipboard)
訊號中心 (黃金訊號中心.exe)
   ↓ HTTPS POST /signals
雲端 Hub (gold-signal-hub-tw.fly.dev)
   ↑ HTTPS GET /signals?after=N
會員端 (黃金跟單會員端.exe)
   ↓ 寫 commands.json
MT5 + EA (MT5_File_Bridge_Enhanced.mq5)
   ↓
下單到券商
```
