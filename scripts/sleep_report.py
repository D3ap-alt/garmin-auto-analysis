"""
sleep_report.py — v13 睡眠詳細レポート
=====================================
Garmin Connect から前夜の睡眠データ（ステージ・HRV・安静時HR・Body Battery）を取得し、
Garminスコアに依存しない一次データ分析を Claude で生成して Notion に投稿する。

設計方針:
- Garmin生JSONはPython側で要約してから渡す（トークン節約）
- 履歴は sleep_state.json にローリング保存（35日分）→ ベースライン比較に使用
- 既にレポート済み or データ未同期なら即終了（朝の時間帯に複数回cronで叩く前提）
- Notionの練習履歴（過去7日）と突合して「練習×睡眠」の相関を分析に含める
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from datetime import datetime, timedelta, date as date_cls
from pathlib import Path
from typing import Any

from notion_client import Client as NotionClient
from anthropic import Anthropic

# run_analysis.py の共通部品を再利用（環境変数は同じものを使用）
from run_analysis import (
    JST, ROOT, jp_weekday, build_date_anchor,
    garmin_login, get_season_context, fetch_recent_history,
    fetch_notion_schema, md_to_notion_blocks,
    _generate_complete,
    ANTHROPIC_API_KEY, NOTION_API_KEY, NOTION_DATABASE_ID,
)

SLEEP_STATE_PATH = ROOT / "sleep_state.json"
SLEEP_MODEL = "claude-sonnet-4-6"
HISTORY_KEEP_DAYS = 35


# ====================== 状態管理 ======================
def load_sleep_state() -> dict:
    if SLEEP_STATE_PATH.exists():
        return json.loads(SLEEP_STATE_PATH.read_text(encoding="utf-8"))
    return {"last_report_date": "", "history": []}


def save_sleep_state(state: dict) -> None:
    # 履歴を直近 HISTORY_KEEP_DAYS 日に刈り込み
    hist = sorted(state.get("history", []), key=lambda r: r.get("date", ""))
    state["history"] = hist[-HISTORY_KEEP_DAYS:]
    SLEEP_STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ====================== Garmin 睡眠データ取得 ======================
def _g(d: Any, *keys, default=None):
    """安全なネスト取得"""
    cur = d
    for k in keys:
        if isinstance(cur, dict):
            cur = cur.get(k)
        else:
            return default
    return cur if cur is not None else default


def _ms_to_hhmm(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%H:%M")
    except Exception:
        return ""


def _sec_to_min(sec: Any) -> int | None:
    try:
        return round(int(sec) / 60)
    except Exception:
        return None


def fetch_sleep_summary(client, target_date: date_cls) -> dict | None:
    """前夜（target_dateの朝に終わった睡眠）の要約を構築。データ未同期なら None。"""
    cdate = target_date.isoformat()
    rec: dict[str, Any] = {"date": cdate}

    try:
        raw = client.get_sleep_data(cdate)
    except Exception as e:
        print(f"  ❌ get_sleep_data失敗: {e}")
        return None

    dto = _g(raw, "dailySleepDTO", default={}) or {}
    total_sec = dto.get("sleepTimeSeconds")
    if not total_sec:
        return None  # まだ同期されていない

    rec["total_min"] = _sec_to_min(total_sec)
    rec["deep_min"] = _sec_to_min(dto.get("deepSleepSeconds"))
    rec["light_min"] = _sec_to_min(dto.get("lightSleepSeconds"))
    rec["rem_min"] = _sec_to_min(dto.get("remSleepSeconds"))
    rec["awake_min"] = _sec_to_min(dto.get("awakeSleepSeconds"))
    rec["sleep_start"] = _ms_to_hhmm(dto.get("sleepStartTimestampLocal"))
    rec["sleep_end"] = _ms_to_hhmm(dto.get("sleepEndTimestampLocal"))
    rec["awake_count"] = dto.get("awakeCount")
    rec["resp_avg"] = dto.get("averageRespirationValue")
    rec["score"] = _g(dto, "sleepScores", "overall", "value")
    rec["score_quality"] = _g(dto, "sleepScores", "overall", "qualifierKey")
    rec["deep_pct_quality"] = _g(dto, "sleepScores", "deepPercentage", "qualifierKey")
    rec["rem_pct_quality"] = _g(dto, "sleepScores", "remPercentage", "qualifierKey")

    # トップレベルに入っていることがある指標
    rec["rhr"] = raw.get("restingHeartRate")
    rec["hrv_avg"] = raw.get("avgOvernightHrv")
    rec["bb_change"] = raw.get("bodyBatteryChange")
    rec["restless"] = raw.get("restlessMomentsCount")

    # HRV詳細（ベースライン・ステータス）
    try:
        hrv = client.get_hrv_data(cdate)
        hs = _g(hrv, "hrvSummary", default={}) or {}
        rec["hrv_avg"] = hs.get("lastNightAvg", rec["hrv_avg"])
        rec["hrv_weekly_avg"] = hs.get("weeklyAvg")
        rec["hrv_status"] = hs.get("status")
        bl = hs.get("baseline") or {}
        rec["hrv_baseline_low"] = bl.get("balancedLow")
        rec["hrv_baseline_high"] = bl.get("balancedUpper")
    except Exception as e:
        print(f"  ⚠️ HRV取得スキップ: {e}")

    # 安静時HRフォールバック
    if rec.get("rhr") is None:
        try:
            rhr = client.get_rhr_day(cdate)
            vals = _g(rhr, "allMetrics", "metricsMap",
                      "WELLNESS_RESTING_HEART_RATE", default=[])
            if vals:
                rec["rhr"] = vals[0].get("value")
        except Exception:
            pass

    return rec


# ====================== プロンプト構築 ======================
def _fmt(v, suffix=""):
    return f"{v}{suffix}" if v is not None else "—"


def build_sleep_table(rec: dict) -> str:
    lines = [
        "| 指標 | 値 |",
        "|---|---|",
        f"| Garmin睡眠スコア | {_fmt(rec.get('score'))}（{_fmt(rec.get('score_quality'))}） |",
        f"| 就寝〜起床 | {_fmt(rec.get('sleep_start'))} → {_fmt(rec.get('sleep_end'))} |",
        f"| 総睡眠時間 | {_fmt(rec.get('total_min'), '分')} |",
        f"| 深い睡眠 | {_fmt(rec.get('deep_min'), '分')} |",
        f"| 浅い睡眠 | {_fmt(rec.get('light_min'), '分')} |",
        f"| REM | {_fmt(rec.get('rem_min'), '分')} |",
        f"| 覚醒 | {_fmt(rec.get('awake_min'), '分')} / {_fmt(rec.get('awake_count'), '回')} |",
        f"| 夜間HRV | {_fmt(rec.get('hrv_avg'), 'ms')}（週平均 {_fmt(rec.get('hrv_weekly_avg'), 'ms')}・ステータス {_fmt(rec.get('hrv_status'))}） |",
        f"| HRVバランス域 | {_fmt(rec.get('hrv_baseline_low'))}〜{_fmt(rec.get('hrv_baseline_high'))} ms |",
        f"| 安静時HR | {_fmt(rec.get('rhr'), ' bpm')} |",
        f"| Body Battery変化 | {_fmt(rec.get('bb_change'))} |",
        f"| 平均呼吸数 | {_fmt(rec.get('resp_avg'), ' brpm')} |",
    ]
    return "\n".join(lines)


def build_history_table(history: list[dict], today_iso: str, days: int = 7) -> str:
    rows = [r for r in history if r.get("date", "") < today_iso][-days:]
    if not rows:
        return "（履歴なし — 初回実行）"
    lines = ["| 日付 | スコア | 総睡眠 | 深い | HRV | RHR | 就寝 |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r.get('date','')} | {_fmt(r.get('score'))} | {_fmt(r.get('total_min'),'分')} "
            f"| {_fmt(r.get('deep_min'),'分')} | {_fmt(r.get('hrv_avg'),'ms')} "
            f"| {_fmt(r.get('rhr'))} | {_fmt(r.get('sleep_start'))} |"
        )
    return "\n".join(lines)


SLEEP_SYSTEM_PROMPT = """あなたはアスリート向けの睡眠・回復分析の専門家です。トライアスリート本人のために、
Garminの睡眠スコアに依存しない一次データ（睡眠ステージ・HRV・安静時HR・Body Battery）の分析を行います。
出力はNotion用Markdown。見出しは ## / ### のみ。表はMarkdownテーブル。

分析ルール（厳守）:
- **Garminスコアを鵜呑みにしない。** スコアはハード練習後のHRV低下を「睡眠の質低下」として減点する仕様であり、
  鍛錬期のアスリートでは慢性的に低く出る。スコアは参考値とし、一次データで評価し直す。
- **タイプ判定を必ず行う**: 「時間不足型」（総睡眠が短い）か「回復不全型」（時間はあるが深い睡眠/HRVが不足）か
  「良好」かを、深い睡眠の絶対量(分)・総時間・HRVから判定し、根拠の数値を引用する。
- **HRVはベースライン（バランス域）との比較で評価。** 単発の低下より、週平均からの逸脱・連続日数を重視。
  ハード練習翌晩のHRV低下は正常な適応反応であり、それ単体で警告しない。練習履歴と突き合わせて解釈する。
- **就寝時刻の一貫性**を履歴から評価する（ばらつきはスコアより重要な指標）。
- **練習との関連**: 直近の練習履歴（強度TE・時間帯）と睡眠指標の対応を具体的に指摘する。
  夜練の日と深い睡眠量の関係など、データにある範囲で。データにない要因（食事・飲酒等）は創作しない。
- **今日への示唆**: コンディションに応じて当日の練習強度の調整案を出す。回復不全のサインが複数日続く場合のみ
  強度低減を明確に推奨。レース直前期はシーズンコンテキストに従う。
- **曜日は自分で計算しない。** 日付・曜日対応表のみを使用。
- 医学的診断はしない。データの記述と練習面の提案に留める。
"""


def analyze_sleep(rec: dict, history: list[dict], season_context: str,
                  training_history: str, target_date: date_cls) -> str:
    date_anchor = build_date_anchor(target_date)
    user_prompt = f"""## 日付・曜日対応表（最優先・厳守）

{date_anchor}

---
## シーズンコンテキスト

{season_context}

---
## 直近の練習履歴（Notionより）

{training_history}

---
## 直近7日の睡眠履歴

{build_history_table(history, rec["date"])}

---
## 今夜の睡眠データ（{rec["date"]} {jp_weekday(target_date)}曜の朝に終了した睡眠）

{build_sleep_table(rec)}

---
以下の構成でレポートを作成してください:
## 睡眠サマリ（今夜の一言評価 + 上記テーブルの再掲は不要、注目値のみ）
## タイプ判定（時間不足型 / 回復不全型 / 良好 — 根拠数値つき)
## HRV・自律神経の回復評価（バランス域・週平均・直近推移との比較）
## 練習との関連（直近の練習強度・時間帯と睡眠指標の対応）
## 就寝リズム評価（履歴からの一貫性）
## 今日への示唆（練習強度・就寝行動の具体的提案 1〜3個）
"""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _generate_complete(
        client, model=SLEEP_MODEL,
        system_prompt=SLEEP_SYSTEM_PROMPT,
        user_prompt=user_prompt,
    )


# ====================== Notion 投稿 ======================
def create_sleep_page(notion: NotionClient, schema: dict[str, str],
                      rec: dict, analysis_md: str) -> None:
    title_name = next((n for n, t in schema.items() if t == "title"), "名前")
    props: dict[str, Any] = {
        title_name: {"title": [{"text": {"content": "😴 睡眠レポート"}}]},
    }
    for name, typ in schema.items():
        if typ == "date" and "日" in name:
            props[name] = {"date": {"start": rec["date"]}}
        elif typ == "select" and "種目" in name:
            props[name] = {"select": {"name": "睡眠"}}
        elif typ == "number" and "HR" in name and rec.get("rhr") is not None:
            props[name] = {"number": rec["rhr"]}
        elif typ == "rich_text" and "タイム" in name and rec.get("total_min"):
            h, m = divmod(rec["total_min"], 60)
            props[name] = {"rich_text": [{"text": {"content": f"{h}:{m:02d}"}}]}

    children = md_to_notion_blocks(analysis_md)
    first, remaining = children[:100], children[100:]
    try:
        page = notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=props, children=first,
        )
    except Exception as e:
        print(f"  ❌ プロパティ付き作成失敗: {e} → タイトルのみで再試行")
        page = notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties={title_name: props[title_name]}, children=first,
        )
    page_id = page["id"]
    print(f"  ✅ Sleep page created: {page_id}")
    while remaining:
        batch, remaining = remaining[:100], remaining[100:]
        try:
            notion.blocks.children.append(block_id=page_id, children=batch)
        except Exception as e:
            print(f"  ⚠️ ブロック追記失敗: {e}")
            break


# ====================== main ======================
def main() -> int:
    target_date = datetime.now(JST).date()
    import os
    manual = os.environ.get("TARGET_DATE", "").strip()
    if manual:
        try:
            target_date = datetime.strptime(manual, "%Y-%m-%d").date()
        except ValueError:
            pass
    today_iso = target_date.isoformat()
    print(f"🌙 Sleep report target: {today_iso}")

    state = load_sleep_state()
    if state.get("last_report_date") == today_iso:
        print("⏭️  本日分レポート済み。終了。")
        return 0

    try:
        client = garmin_login()
    except Exception as e:
        print(f"❌ Garmin login failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1

    rec = fetch_sleep_summary(client, target_date)
    if rec is None:
        print("ℹ️ 睡眠データ未同期。次回cronで再試行。")
        return 0

    print(f"  📊 score={rec.get('score')} total={rec.get('total_min')}min "
          f"deep={rec.get('deep_min')}min hrv={rec.get('hrv_avg')}")

    notion = NotionClient(auth=NOTION_API_KEY, notion_version="2022-06-28")
    schema = fetch_notion_schema(notion)
    season_context = get_season_context(target_date)
    training_history = fetch_recent_history(notion, target_date, days=7)

    analysis = analyze_sleep(rec, state.get("history", []),
                             season_context, training_history, target_date)
    create_sleep_page(notion, schema, rec, analysis)

    # 履歴へ保存（同日重複は置換）
    hist = [r for r in state.get("history", []) if r.get("date") != today_iso]
    hist.append(rec)
    state["history"] = hist
    state["last_report_date"] = today_iso
    save_sleep_state(state)
    print("✅ Sleep report done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
