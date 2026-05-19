# Garmin Auto Analysis

毎時、Garminに記録されたトレーニングをClaudeが自動分析し、Notionデータベースに追記する仕組み。

## セットアップ

[SETUP.md](./SETUP.md) を参照。

## 構成

- **GitHub Actions** (cron): 毎時45分に発火（1時間ごと）
- **python-garminconnect**: Garmin Connect API クライアント（非公式）
- **Anthropic Claude API**: Sonnet 4.6 で分析
- **Notion API**: 分析結果をデータベースに自動追加

## ファイル構成

```
.
├── .github/workflows/daily_analysis.yml  # GitHub Actions cron定義
├── prompts/
│   ├── garmin_analyzer_skill.md          # 分析スキル定義
│   └── triathlon_profile.md              # 個人プロフィール
├── scripts/run_analysis.py               # メインスクリプト
├── state.json                            # 分析済みID（自動更新）
├── requirements.txt
├── SETUP.md
└── README.md
```
