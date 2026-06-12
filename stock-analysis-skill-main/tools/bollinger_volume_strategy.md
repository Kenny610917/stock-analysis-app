# Bollinger + Volume Strategy Tool

This tool reads daily stock data, calculates Bollinger Bands and average volume,
then marks each row with a rules-based action. It can use either a local CSV or
online Yahoo Finance data.

## Direct Online Fetch

In this Codex workspace, use the bundled Python executable:

```powershell
$PY = "C:\Users\kenlin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
```

US stock:

```powershell
& $PY .\stock-analysis-skill-main\tools\bollinger_volume_strategy.py --symbol NVDA --last 5
```

Taiwan stock:

```powershell
& $PY .\stock-analysis-skill-main\tools\bollinger_volume_strategy.py --symbol 2330.TW --last 5
```

Hong Kong stock:

```powershell
& $PY .\stock-analysis-skill-main\tools\bollinger_volume_strategy.py --symbol 0700.HK --last 5
```

Fetch online data, save raw prices, and save full signals:

```powershell
& $PY .\stock-analysis-skill-main\tools\bollinger_volume_strategy.py --symbol 2330.TW --range 1y --save-prices .\2330_prices.csv -o .\2330_signals.csv --last 10
```

Common Yahoo Finance suffixes:

- Taiwan listed stocks: `.TW`, for example `2330.TW`
- Taiwan OTC stocks: `.TWO`, for example `8069.TWO`
- Hong Kong stocks: `.HK`, for example `0700.HK`
- US stocks: no suffix, for example `AAPL`

## Local CSV

Required CSV columns:

- `date`
- `close`
- `volume`

Optional columns:

- `open`
- `high`
- `low`

Example:

```powershell
& $PY .\stock-analysis-skill-main\tools\bollinger_volume_strategy.py .\prices.csv --last 5
```

Write a full signal CSV:

```powershell
& $PY .\stock-analysis-skill-main\tools\bollinger_volume_strategy.py .\prices.csv -o .\signals.csv
```

Default rules:

- `BUY / LOWER_BAND_REBOUND`: price recovers above the lower band with volume.
- `BUY / UPPER_BAND_VOLUME_BREAKOUT`: price breaks above the upper band with volume.
- `SELL / UPPER_BAND_REVERSAL`: price falls back inside the upper band with volume.
- `SELL / MIDDLE_BAND_VOLUME_BREAKDOWN`: price loses the middle band with volume.
- `WAIT_CONFIRMATION`: price is near the lower band but lacks confirmation.
- `WATCH`: price is near the upper band and needs breakout/reversal confirmation.

Default parameters:

- Bollinger period: `20`
- Standard deviation multiplier: `2.0`
- Volume moving average period: `20`
- High-volume threshold: `1.5x` average volume

Risk note: this is a technical research helper, not investment advice.
