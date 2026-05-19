# 🏃 Garmin自動分析セットアップ手順

毎朝、前日のトレーニングをClaudeが分析してGoogleドキュメントに追記してくれる仕組みです。
**GitHub未経験でも30〜60分で完了します**。

---

## 全体像

```
[GitHub Actions が毎朝7:00 JSTに起動]
    ↓
[Garmin Connect から前日分のアクティビティ取得]
    ↓
[Claude が分析（あなたのスキル定義 + 個人プロフィールに準拠）]
    ↓
[Googleドキュメントの末尾に追記]
```

---

## STEP 1: GitHubアカウント作成（5分）

1. https://github.com にアクセス
2. 「Sign up」をクリック → メールアドレス・パスワード入力
3. メール認証を完了
4. 無料プラン（Free）でOK

---

## STEP 2: リポジトリ作成（5分）

1. ログイン後、右上の「+」→「New repository」
2. 以下を設定：
   - **Repository name**: `garmin-auto-analysis`
   - **Visibility**: ⚠️ **必ず「Private」を選択**（個人情報を含むため）
   - 「Initialize with README」は**チェックなし**
3. 「Create repository」をクリック

---

## STEP 3: ローカルにファイルを準備（10分）

このZIPに同梱されている以下のファイルを、後でGitHubにアップロードします。
**今は何もしなくてOK**、構成を理解しておくだけです。

```
garmin-auto-analysis/
├── .github/
│   └── workflows/
│       └── daily_analysis.yml      # 毎朝の自動実行設定
├── prompts/
│   ├── garmin_analyzer_skill.md    # 分析スキル定義
│   └── triathlon_profile.md        # あなたの個人プロフィール
├── scripts/
│   └── run_analysis.py             # メインスクリプト
├── .gitignore
├── requirements.txt                 # Pythonパッケージ
├── state.json                       # 重複防止用の状態管理
└── SETUP.md                         # このファイル
```

---

## STEP 4: Anthropic APIキー取得（5分）

1. https://console.anthropic.com にアクセス
2. アカウント作成 → ログイン
3. 左メニュー「API Keys」→「Create Key」
4. 表示された `sk-ant-...` のキーを**メモ帳に保存**（後で使う）
5. **クレジットを少額入金**（Settings → Billing で $5 推奨。月数百円しか使わない）

---

## STEP 5: Google Cloud設定（15分）⭐ ここが一番複雑

### 5-1. Google Cloudプロジェクト作成

1. https://console.cloud.google.com にアクセス（Googleアカウントでログイン）
2. 上部の「プロジェクトを選択」→「新しいプロジェクト」
3. プロジェクト名: `garmin-analysis` → 「作成」

### 5-2. Google Docs APIを有効化

1. 左メニュー「APIとサービス」→「ライブラリ」
2. 「Google Docs API」を検索 → クリック →「有効にする」

### 5-3. サービスアカウント作成

1. 左メニュー「APIとサービス」→「認証情報」
2. 上部「+認証情報を作成」→「サービスアカウント」
3. サービスアカウント名: `garmin-bot` → 「作成して続行」
4. ロール: そのままで「続行」→「完了」

### 5-4. サービスアカウントのJSONキー取得

1. 作成したサービスアカウント（例: `garmin-bot@...iam.gserviceaccount.com`）をクリック
2. 上部「キー」タブ →「鍵を追加」→「新しい鍵を作成」
3. JSON形式を選択 →「作成」→ JSONファイルがダウンロードされる
4. このJSONファイルの**中身全部をコピー**してメモ帳に保存

### 5-5. Googleドキュメント作成

1. https://docs.google.com で新規ドキュメント作成
2. タイトル: `トレーニング分析ログ 2026`
3. URLから**ドキュメントID**を抜き出す
   ```
   https://docs.google.com/document/d/【ここがID】/edit
   ```
4. このIDをメモ帳に保存

### 5-6. サービスアカウントに編集権限を付与

1. ドキュメント右上「共有」をクリック
2. サービスアカウントのメールアドレス（`garmin-bot@xxxxxx.iam.gserviceaccount.com`）を入力
3. 権限を「**編集者**」に設定 → 「送信」

⚠️ これを忘れるとボットが書き込めません

---

## STEP 6: GitHub Secretsに認証情報を登録（10分）

1. GitHubのリポジトリ画面で「**Settings**」タブ
2. 左メニュー「**Secrets and variables**」→「**Actions**」
3. 「**New repository secret**」を押して、以下を**1つずつ**登録：

| Name | Value |
|---|---|
| `GARMIN_EMAIL` | Garmin Connectのメールアドレス |
| `GARMIN_PASSWORD` | Garmin Connectのパスワード |
| `ANTHROPIC_API_KEY` | STEP 4で取得した `sk-ant-...` |
| `GOOGLE_DOC_ID` | STEP 5-5で取得したドキュメントID |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | STEP 5-4のJSONの中身**全部**（コピペ） |

⚠️ Secretsは一度保存すると中身を見られなくなります（更新は可能）

---

## STEP 7: ファイルをGitHubにアップロード（5分）

### 方法A: ブラウザでアップロード（簡単）

1. リポジトリのトップページに戻る
2. 「**Add file**」→「**Upload files**」
3. **このZIPの中身を全部ドラッグ＆ドロップ**
   - ただし `.github` フォルダのアップロードは下記に注意 ↓
4. 「Commit changes」をクリック

⚠️ **重要**: ブラウザのドラッグ＆ドロップだとフォルダ構造が崩れることがあります。
特に `.github/workflows/daily_analysis.yml` が正しい場所に置かれているか確認してください。

### 方法B: GitHub Desktop を使う（推奨・確実）

1. https://desktop.github.com からGitHub Desktopをダウンロード・インストール
2. ログイン → 「Clone a repository」→ 作成したリポジトリを選ぶ
3. ローカルフォルダにファイルが入る
4. ZIPの中身をそのフォルダにコピー
5. GitHub Desktopで「Commit to main」→「Push origin」

---

## STEP 8: 動作テスト（5分）

1. GitHubリポジトリの「**Actions**」タブを開く
2. 左メニュー「Daily Garmin Analysis」をクリック
3. 「**Run workflow**」ボタン → 緑の「Run workflow」を押す
4. 数秒後、実行が開始される（黄色の●）
5. 完了するまで2〜5分待つ
6. 成功（緑の✓）したら：
   - Googleドキュメントを開く → 分析結果が追記されているはず！
7. 失敗（赤の✗）したら：
   - ジョブをクリックしてログを確認
   - よくあるエラーは下記の「トラブルシューティング」参照

---

## STEP 9: 完了！

これで毎朝 **7:00 JST** に自動実行されます。
何もしなくてOK。Googleドキュメントを開けば、前日のトレーニング分析が積み上がっていきます。

---

## 💰 コスト

- **GitHub Actions**: 無料（プライベートリポジトリでも月2,000分まで無料、使うのは月20分程度）
- **Claude API**: 月150〜800円（トレーニング頻度・モデル選択次第）
  - 短時間トレ → Sonnet 4.6（安い）
  - 長時間トレ・15km超 → Opus 4.7（高精度）
  - スクリプトが自動で振り分け
- **Google Docs API**: 無料
- **合計目安**: 月数百円

---

## 🔧 トラブルシューティング

### Garminログインに失敗する

- Garmin Connectで**MFA（2段階認証）を有効にしていると失敗**します
- 設定 → セキュリティ → 2段階認証を**一時的に無効化**してから再実行
- もしくは `garth` の MFA対応版を使う改造が必要（要相談）

### Googleドキュメントに書き込めない

- STEP 5-6 のサービスアカウントへの共有設定を再確認
- ドキュメントの共有設定で、サービスアカウントのメールが「編集者」になっているか

### Claude APIエラー

- console.anthropic.com → Billing → クレジット残高を確認
- API利用上限に達していないかチェック

### 何日も実行されない

- GitHub Actionsは**60日間アクティビティがないと自動で無効化**される仕様
- 毎日動いていれば問題ないが、長期休暇後は要確認

### 「Actions」タブでワークフローが表示されない

- `.github/workflows/daily_analysis.yml` のパスが正しいか確認
- ファイル名がぴったり一致しているか（タイポ注意）

---

## 🛠 カスタマイズ

### 実行時間を変える

`.github/workflows/daily_analysis.yml` の `cron: '0 22 * * *'` を編集。
- UTC時刻なので、JSTから9時間引く
- 例: 朝6時JST → `'0 21 * * *'` (UTC前日21:00)

### レポートをSlackにも送りたい

`scripts/run_analysis.py` の `append_to_google_doc()` の隣に Slack Incoming Webhook 送信を追加。
コードサンプルが必要なら相談してください。

### 個人プロフィールを更新

`prompts/triathlon_profile.md` を編集してcommit/push。次回実行時から反映される。

---

## 📞 困ったら

- GitHub Actionsの実行ログを確認（Actions タブ → 失敗したジョブをクリック）
- エラーメッセージを次回のClaude会話に貼り付けて相談
