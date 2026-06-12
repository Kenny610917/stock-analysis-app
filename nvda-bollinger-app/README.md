# 股票分析 App

Local dashboard for `stock-analysis-skill-main/tools/bollinger_volume_strategy.py`,
with added KD, RSI, and MACD indicator analysis.

## Run

```powershell
& "C:\Users\kenlin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\nvda-bollinger-app\server.py
```

Open:

```text
http://127.0.0.1:8765
```

The app fetches Yahoo Finance daily bars, calculates Bollinger Bands, volume
signals, KD, RSI, and MACD values, then renders the latest result plus recent
rows in a Traditional Chinese interface. It does not write analysis files.
