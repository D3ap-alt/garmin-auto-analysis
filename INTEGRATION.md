# ローデータ保存レイヤ 組み込み手順

## 1. ファイル配置

```
garmin-auto-analysis/
├── scripts/
│   ├── run_analysis.py      ← 3箇所を編集
│   └── raw_archive.py       ← 新規（今回追加）
└── data/raw/                ← 自動生成
    ├── fit/                 完全復元用の原本
    └── json/                チャットに貼る即読み用
```

`requirements.txt` に追記：

```
fitdecode>=0.10.0
```

---

## 2. run_analysis.py の編集

### (a) import 追加

```python
from raw_archive import archive_activity, assign_brick_keys
```

### (b) 未分析アクティビティを確定した直後にブリックキーを振る

```python
# targets = 今回分析する未処理アクティビティのリスト
brick_keys = assign_brick_keys([
    {
        "activity_id": a["activityId"],
        "date":        a["startTimeLocal"][:10],
        "start_time":  datetime.fromisoformat(a["startTimeLocal"]),
    }
    for a in targets
])
```

### (c) 各アクティビティのループ内、Claude API に投げる**前**に呼ぶ

```python
for act in targets:
    aid  = act["activityId"]
    date = act["startTimeLocal"][:10]

    # --- ローデータ保存（失敗しても分析は止めない） ---
    archive = None
    try:
        archive = archive_activity(
            client, aid, date,
            brick_key=brick_keys.get(str(aid)),
        )
        print(f"[raw] saved {archive['stem']} sport={archive['sport']}")
    except Exception as e:
        print(f"[raw] WARN {aid}: {e}")

    # --- 以下は既存の分析処理 ---
    ...
```

### (d) Notion 書き込み時に参照キーを載せる

プロパティに以下を追加（Notion 側で先に作成しておく）：

| プロパティ名 | 型 |
|---|---|
| `activity_id` | テキスト |
| `raw_url` | URL |
| `sport` | セレクト |

```python
props = {
    # ... 既存 ...
    "activity_id": {"rich_text": [{"text": {"content": str(aid)}}]},
}
if archive:
    primary = (archive["urls"].get("lengths")
               or archive["urls"].get("series_5s")
               or archive["urls"]["laps"])
    props["raw_url"] = {"url": primary}
    props["sport"]   = {"select": {"name": archive["sport"]}}
```

さらに本文末尾に toggle で全URLを入れておくと、後から辿るのが速い：

```python
children.append({
    "object": "block", "type": "toggle",
    "toggle": {
        "rich_text": [{"text": {"content": "📦 ローデータ"}}],
        "children": [
            {"object": "block", "type": "bulleted_list_item",
             "bulleted_list_item": {"rich_text": [
                 {"text": {"content": f"{k}: {u}", "link": {"url": u}}}
             ]}}
            for k, u in archive["urls"].items()
        ],
    },
})
```

---

## 3. ワークフローに commit ステップを追加

`.github/workflows/daily_analysis.yml` の実行ステップの**後**に：

```yaml
      - name: Commit raw data & state
        run: |
          git config user.name  "garmin-bot"
          git config user.email "garmin-bot@users.noreply.github.com"
          git add data/raw state.json
          if git diff --cached --quiet; then
            echo "no changes"
          else
            git commit -m "raw: $(date -u +%Y-%m-%dT%H:%M)"
            git pull --rebase --autostash
            git push
          fi
```

`permissions: contents: write` がジョブに必要。毎時 cron なので `git pull --rebase` は入れておくこと（並行実行時のリジェクト回避）。

---

## 4. 出力される中身

| 種目 | 保存されるJSON | 後から何ができるか |
|---|---|---|
| バイク | `_laps` `_series_5s` | NP / IF / VI の再計算、コーナー立ち上がり240W超過の検出、温度×パワー相関 |
| プールスイム | `_laps` `_lengths` | パドル/プル/ハイポ/キャッチアップの構造復元、SWOLF の装備別比較 |
| OWS | `_laps` `_series_5s` | ペース変動、HRスパイク（オーストラリアン・エグジット）の切り分け |
| ラン | `_laps` `_series_5s` | GCT/VR の推移、7km熱ダレの再検証、Lap6現象の追跡 |

`_meta.json` にセッション要約と `lap_count / length_count / record_count` が入るので、**まずこれだけ貼れば何が残っているか即わかる**。

---

## 5. 後日セッションでの使い方

```
このURLのローデータで7km以降のGCT推移を見て
https://raw.githubusercontent.com/D3ap-alt/garmin-auto-analysis/main/data/raw/json/2026-09-06_xxxxx_series_5s.json
```

Notion の `raw_url` プロパティをコピーして貼るだけで、再取得なしに分析を再開できる。

---

## 6. 過去分の遡り取り込み / 単発テスト

`scripts/backfill_raw.py` を使う。分析・Notion書き込みは通らないので、
**既に分析済みのアクティビティでもNotionページを重複させずに**ローデータだけ後から揃えられる。

GitHub Actions の **Backfill Raw Data** ワークフロー（`workflow_dispatch`）から手動実行する。

| 入力 | 意味 |
|---|---|
| `start_date` / `end_date` | 期間指定。空欄なら今日 |
| `activity_ids` | ID直指定（カンマ区切り）。指定すると日付は無視 |
| `limit` | 1回の上限件数（既定30 / 0で無制限） |
| `force` | 保存済みでも再取得 |

ローカルからでも動く：

```bash
BACKFILL_START=2026-07-01 BACKFILL_END=2026-07-31 python scripts/backfill_raw.py
```

Garmin は連続DLに厳しいので1件ごとに既定3秒待つ（`BACKFILL_SLEEP` で変更可）。
`data/raw/fit/` に既にあるIDは自動でスキップされるので、`limit` で打ち切られても
同じ条件で再実行すれば続きから進む。数百件ある場合は月単位で区切るのが安全。
