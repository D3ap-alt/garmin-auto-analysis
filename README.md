# Garmin Auto Analysis

毎朝、Garminに記録した前日のトレーニングをClaudeが自動分析し、Googleドキュメントに追記する仕組み。

## セットアップ

[SETUP.md](./SETUP.md) を参照。

## 構成

- **GitHub Actions** (cron): 毎日 07:00 JST に発火
- **garth**: Garmin Connect API クライアント（非公式）
- **Anthropic Claude API**: 分析エンジン（Opus 4.7 / Sonnet 4.6 自動切替）
- **Google Docs API**: 分析結果の追記先

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
