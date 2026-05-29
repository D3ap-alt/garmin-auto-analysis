"""
Garmin Connect → Claude → Notion 自動分析パイプライン (v10)

v10変更点 (バグ修正・改善):
  - max_tokens を 4096 → 8192 に増量（分析が途中で切れる問題を解決）
  - Notion API の databases.query() を 廃止/旧API両対応に修正
    - data_sources.query → databases.query → notion.request の順でフォールバック
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import sys
import tarfile
import time
import traceback
from datetime import datetime, timedelta, timezone, date as date_cls
from pathlib import Path
from typing import Any

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from anthropic import Anthropic
from notion_client import Client as NotionClient

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent
STATE_PATH = ROOT / "state.json"
PROMPTS_DIR = ROOT / "prompts"
TOKEN_DIR = Path.home() / ".garminconnect"

GARMIN_EMAIL = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
GARMIN_TOKENS_BASE64 = os.environ.get("GARMIN_TOKENS_BASE64", "")
TARGET_DATE = os.environ.get("TARGET_DATE", "").strip()

# 想定するスキーマ（フォールバック用）
# Notion DBのプロパティ名と型がここと一致している必要があります
EXPECTED_SCHEMA = {
    "名前": "title",
    "日付": "date",
    "種目": "select",
    "距離 (km)": "number",
    "タイム": "rich_text",
    "平均HR": "number",
    "TE": "number",
}


# ====================== Garmin認証 ======================
def garmin_login() -> Garmin:
    TOKEN_DIR.mkdir(exist_ok=True)
    
    if GARMIN_TOKENS_BASE64:
        try:
            tar_bytes = base64.b64decode(GARMIN_TOKENS_BASE64)
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
                tar.extractall(TOKEN_DIR)
            
            client = Garmin()
            client.login(str(TOKEN_DIR))
            print("✅ Resumed session from saved tokens")
            return client
        except Exception as e:
            print(f"⚠️ Token resume failed: {e}")
    
    print("🔐 Attempting fresh login...")
    client = Garmin(email=GARMIN_EMAIL, password=GARMIN_PASSWORD)
    client.login(str(TOKEN_DIR))
    print("✅ Fresh login succeeded")
    return client


# ====================== Garmin データ取得 ======================
def resolve_target_date() -> date_cls:
    if TARGET_DATE:
        try:
            target = datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()
            print(f"🎯 Target date (manual): {target}")
            return target
        except ValueError:
            pass
    # v8: デフォルトを「今日」に変更（cron頻度UPに伴い、当日の振り返り重視）
    target = datetime.now(JST).date()
    print(f"🎯 Target date (today JST): {target}")
    return target


def fetch_target_activities(client: Garmin, target_date: date_cls) -> list[dict[str, Any]]:
    date_str = target_date.isoformat()
    activities = client.get_activities_by_date(date_str, date_str)
    print(f"📊 Found {len(activities)} activities on {date_str}")
    return activities


def fetch_activity_detail(client: Garmin, activity_id: int) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    
    try:
        detail["summary"] = client.get_activity(activity_id)
        print(f"  ✅ Got summary: {len(detail['summary'])} fields")
    except Exception as e:
        print(f"  ❌ summary取得失敗: {e}")
    
    try:
        detail["laps"] = client.get_activity_splits(activity_id)
    except Exception as e:
        print(f"  ❌ laps取得失敗: {e}")
    
    return detail


# ====================== シーズン分析機能 (v9) ======================
RACE_SCHEDULE = [
    {"name": "渡良瀬", "date": "2026-05-24", "distance": "OD", "goal": "2:20", "tier": "B"},
    {"name": "海の森", "date": "2026-06-14", "distance": "OD", "goal": "2:18", "tier": "A"},
    {"name": "諏訪子", "date": "2026-06-28", "distance": "ミドル", "goal": "4:30", "tier": "B"},
    {"name": "潮来", "date": "2026-07-12", "distance": "OD", "goal": "2:17", "tier": "A"},
    {"name": "大井川", "date": "2026-07-19", "distance": "OD", "goal": "2:17", "tier": "A"},
    {"name": "いわき", "date": "2026-08-22", "distance": "OD", "goal": "2:17", "tier": "A"},
    {"name": "富士", "date": "2026-09-06", "distance": "OD", "goal": "2:17", "tier": "A"},
    {"name": "村上", "date": "2026-09-27", "distance": "OD", "goal": "2:15", "tier": "A+"},
]


def get_season_context(today: date_cls) -> str:
    """次の大会までの日数と周期化フェーズを計算"""
    # 次の大会を見つける
    next_race = None
    next_a_race = None
    for race in RACE_SCHEDULE:
        race_date = datetime.strptime(race["date"], "%Y-%m-%d").date()
        if race_date >= today:
            if next_race is None:
                next_race = (race, race_date, (race_date - today).days)
            if race["tier"].startswith("A") and next_a_race is None:
                next_a_race = (race, race_date, (race_date - today).days)
                break
    
    if not next_race:
        return "シーズン終了後（オフシーズン）"
    
    race, race_date, days_to_race = next_race
    
    # 周期化フェーズ判定
    if days_to_race <= 7:
        phase = "Race期（試合直前・刺激のみ）"
    elif days_to_race <= 14:
        phase = "Taper期（量60-70%減・強度維持）"
    elif days_to_race <= 28:
        phase = "Peak期（試合に近い質の刺激）"
    elif days_to_race <= 56:
        phase = "Build期（強度上昇・SST/閾値中心）"
    else:
        phase = "Base期（量重視・有酸素持久力構築）"
    
    context = f"""### 次の大会
- **{race['name']}**: {race['date']} ({race['distance']}, 目標 {race['goal']}, 重要度 {race['tier']})
- **あと {days_to_race} 日**

### 現在のシーズン位置づけ
- **{phase}**
"""
    
    if next_a_race and next_a_race[0]["name"] != race["name"]:
        a_race, _, a_days = next_a_race
        context += f"""
### 次のA戦
- **{a_race['name']}**: {a_race['date']} (あと {a_days} 日, 目標 {a_race['goal']})
"""
    
    return context


def fetch_recent_history(notion: NotionClient, today: date_cls, days: int = 7) -> str:
    """Notion DBから過去N日間のトレーニング履歴を取得して要約。
    
    notion-client のバージョンによって API が変わるため、3段階でフォールバック:
    1. data_sources.query  (新API, Notion-Version 2025-09-03 以降)
    2. databases.query     (旧API, Notion-Version 2022-06-28)
    3. notion.request      (raw request、ライブラリのメソッドに依存しない最終手段)
    """
    start_date = (today - timedelta(days=days)).isoformat()
    
    query_params = {
        "filter": {
            "property": "日付",
            "date": {
                "on_or_after": start_date,
            }
        },
        "sorts": [{"property": "日付", "direction": "descending"}],
        "page_size": 20,
    }
    
    response = None
    
    # 試行1: 新APIの data_sources.query
    try:
        if hasattr(notion, 'data_sources') and hasattr(notion.data_sources, 'query'):
            response = notion.data_sources.query(
                data_source_id=NOTION_DATABASE_ID,
                **query_params,
            )
            print("  ℹ️ Using data_sources.query (new API)")
    except Exception as e:
        print(f"  ℹ️ data_sources.query failed: {e}")
        response = None
    
    # 試行2: 旧APIの databases.query
    if response is None:
        try:
            response = notion.databases.query(
                database_id=NOTION_DATABASE_ID,
                **query_params,
            )
            print("  ℹ️ Using databases.query (legacy API)")
        except Exception as e:
            print(f"  ℹ️ databases.query failed: {e}")
            response = None
    
    # 試行3: 直接 raw request を投げる（最も堅牢）
    if response is None:
        try:
            response = notion.request(
                path=f"databases/{NOTION_DATABASE_ID}/query",
                method="POST",
                body=query_params,
            )
            print("  ℹ️ Using raw request (fallback)")
        except Exception as e:
            print(f"  ⚠️ All Notion query methods failed: {e}")
            return "過去1週間のトレーニング履歴: 取得失敗（Notion APIエラー）"
    
    try:
        pages = response.get("results", [])
        if not pages:
            return "過去1週間のトレーニング履歴: データなし"
        
        rows = []
        for p in pages:
            props = p.get("properties", {})
            name = _extract_text(props.get("名前", {}), "title")
            date = _extract_date(props.get("日付", {}))
            sport = _extract_select(props.get("種目", {}))
            distance = _extract_number(props.get("距離 (km)", {})) or _extract_number(props.get("距離 (km) ", {}))
            time_str = _extract_text(props.get("タイム", {}), "rich_text")
            hr = _extract_number(props.get("平均HR", {}))
            te = _extract_number(props.get("TE", {}))
            
            rows.append({
                "date": date,
                "name": name,
                "sport": sport,
                "distance": distance,
                "time": time_str,
                "hr": hr,
                "te": te,
            })
        
        # 表形式で整形
        lines = ["| 日付 | 種目 | 名前 | 距離(km) | タイム | HR | TE |", "|---|---|---|---|---|---|---|"]
        for r in rows:
            lines.append(
                f"| {r['date'] or '-'} | {r['sport'] or '-'} | {r['name'] or '-'} | "
                f"{r['distance'] if r['distance'] is not None else '-'} | "
                f"{r['time'] or '-'} | {r['hr'] if r['hr'] is not None else '-'} | "
                f"{r['te'] if r['te'] is not None else '-'} |"
            )
        
        # 種目別の集計
        sport_counts = {}
        for r in rows:
            s = r['sport'] or "その他"
            sport_counts[s] = sport_counts.get(s, 0) + 1
        sport_summary = ", ".join(f"{k}: {v}回" for k, v in sport_counts.items())
        
        print(f"  ✅ Retrieved {len(rows)} activities from past {days} days")
        
        return f"""### 過去{days}日間のトレーニング履歴（Notion DB から自動取得）

{chr(10).join(lines)}

### 種目別頻度（過去{days}日）
{sport_summary}
"""
    except Exception as e:
        print(f"  ⚠️ 過去履歴の整形失敗: {e}")
        return f"過去履歴の整形失敗: {e}"


def _extract_text(prop: dict, type_key: str) -> str:
    """Notionのtitle/rich_textプロパティからテキスト抽出"""
    if not prop:
        return ""
    items = prop.get(type_key, [])
    if not items:
        return ""
    return "".join(it.get("text", {}).get("content", "") for it in items)


def _extract_date(prop: dict) -> str:
    """Notionのdateプロパティから日付文字列抽出"""
    if not prop:
        return ""
    date_obj = prop.get("date", {})
    if not date_obj:
        return ""
    return date_obj.get("start", "")


def _extract_select(prop: dict) -> str:
    """Notionのselectプロパティから名前抽出"""
    if not prop:
        return ""
    sel = prop.get("select")
    if not sel:
        return ""
    return sel.get("name", "")


def _extract_number(prop: dict) -> Any:
    """Notionのnumberプロパティから数値抽出"""
    if not prop:
        return None
    return prop.get("number")


# ====================== Claude 分析 ======================
def load_prompts() -> tuple[str, str]:
    skill = (PROMPTS_DIR / "garmin_analyzer_skill.md").read_text(encoding="utf-8")
    profile = (PROMPTS_DIR / "triathlon_profile.md").read_text(encoding="utf-8")
    return skill, profile


def select_model(summary: dict) -> str:
    # v8: コスト抑制のため Sonnet 固定（旧: 長時間/長距離時のみOpus）
    # Sonnet 4.6 で個人プロフィール反映の深い分析は十分可能
    return "claude-sonnet-4-6"


def analyze_with_claude(activity_data: dict, season_context: str, history_context: str) -> str:
    skill_md, profile_md = load_prompts()
    model = select_model(activity_data.get("summary", {}))
    
    system_prompt = f"""あなたはGarminトレーニング分析の専門家です。
スキル定義と個人プロフィールに完全に従って分析。出力はNotion用のMarkdown。
- 見出しは ## (h2) または ### (h3) を使用、# は使わない
- 表は Markdownテーブル形式
- **必ず分析冒頭で「シーズン位置づけ（次の大会まで何日、現在のフェーズ）」を明示すること**
- **過去1週間のトレーニング履歴を踏まえた相対的な評価を行うこと**
  - 例: 「今週はラン3回でやや少なめ」「先週同種目より平均HR-5bpm」など
- **次の大会への準備度合いを評価し、残り日数に合わせた次回提案を行うこと**

---
## スキル定義
{skill_md}

---
## 個人プロフィール
{profile_md}
"""
    
    payload = {
        "summary": _trim_summary(activity_data.get("summary", {})),
        "laps": activity_data.get("laps", {}),
    }
    
    user_prompt = f"""## シーズンコンテキスト

{season_context}

---

## 過去のトレーニング履歴

{history_context}

---

## 本日の分析対象

以下はアクティビティデータです。スキル定義の「出力形式」テンプレートに完全準拠して分析してください。
**冒頭に「シーズン位置づけ」セクションを必ず追加し、「次の大会まで○日（{{大会名}}）、現在は{{フェーズ}}」を明記してください。**
**「過去1週間との比較」セクションも追加し、相対評価を行ってください。**
**最後の「次回トレーニング提案」では、次の大会までの残り日数に合わせた具体的なメニュー（曜日別）を提示してください。**

```json
{json.dumps(payload, ensure_ascii=False, default=str)[:80000]}
```
"""
    
    print(f"  🤖 Analyzing with {model}...")
    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = anthropic_client.messages.create(
        model=model,
        max_tokens=8192,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text


def _trim_summary(s: dict) -> dict:
    keys = [
        "activityId", "activityName", "startTimeLocal", "duration", "distance",
        "averageSpeed", "averageHR", "maxHR", "calories", "averagePower",
        "maxPower", "normalizedPower", "averageRunningCadenceInStepsPerMinute",
        "maxRunningCadenceInStepsPerMinute", "groundContactTime",
        "verticalOscillation", "verticalRatio", "elevationGain", "elevationLoss",
        "aerobicTrainingEffect", "anaerobicTrainingEffect", "trainingEffectLabel",
        "activityTrainingLoad", "averagePace", "activityType",
    ]
    return {k: s.get(k) for k in keys if k in s}


# ====================== Notion ======================
def fetch_notion_schema(notion: NotionClient) -> dict[str, str]:
    """
    データベースのプロパティを取得。失敗時は EXPECTED_SCHEMA をフォールバックとして使う。
    
    Notion API 2025-09-03 ではデータソース対応により、databases.retrieve のレスポンス形式が
    若干変わっている可能性があるため、複数のキーをチェック。
    """
    print("\n📋 Fetching Notion Database Schema...")
    
    try:
        db = notion.databases.retrieve(database_id=NOTION_DATABASE_ID)
        
        # デバッグ：レスポンスの構造をログ出力
        print(f"  Response keys: {list(db.keys())}")
        
        # 通常のレスポンス形式
        properties = db.get("properties", {})
        
        # データソース対応のフォールバック
        if not properties and "data_sources" in db:
            ds = db["data_sources"]
            if ds and isinstance(ds, list) and len(ds) > 0:
                first_ds = ds[0]
                print(f"  Found data_sources, using first one: {first_ds.get('id')}")
                # データソースから取得
                ds_detail = notion.request(
                    f"/data_sources/{first_ds['id']}", method="GET"
                )
                properties = ds_detail.get("properties", {})
        
        if properties:
            schema = {}
            print("  ✅ Schema retrieved from API:")
            for name, prop in properties.items():
                ptype = prop.get("type")
                schema[name] = ptype
                print(f"     {repr(name):40} → {ptype}")
            return schema
    except Exception as e:
        print(f"  ⚠️ databases.retrieve failed: {e}")
    
    # フォールバック
    print("  🔧 Using hardcoded EXPECTED_SCHEMA as fallback:")
    for name, ptype in EXPECTED_SCHEMA.items():
        print(f"     {repr(name):40} → {ptype}")
    return EXPECTED_SCHEMA.copy()


def _get_nested(d: dict, *keys) -> Any:
    """複数のキー候補からまず非Noneの値を返す"""
    for k in keys:
        v = d.get(k)
        if v is not None and v != "":
            return v
    return None


def build_properties(summary: dict, schema: dict[str, str]) -> dict[str, Any]:
    """Notionスキーマに基づいてプロパティ辞書を構築。キー名揺れに対応。"""
    # デバッグ：summaryのキー一覧を表示
    print(f"  🔑 summary keys ({len(summary)}): {sorted(summary.keys())}")
    
    activity_name = _get_nested(summary, "activityName", "name") or "アクティビティ"
    start_time = _get_nested(summary, "startTimeLocal", "startTimeGMT", "beginTimestamp") or ""
    
    # 種目情報の取得（ネストがいくつかパターンある）
    activity_type = summary.get("activityType") or {}
    sport_key = ""
    if isinstance(activity_type, dict):
        sport_key = activity_type.get("typeKey") or activity_type.get("type_key") or ""
    elif isinstance(activity_type, str):
        sport_key = activity_type
    if not sport_key:
        sport_key = _get_nested(summary, "activityTypeName", "sportType") or ""
    
    # 距離（複数のキー名候補）
    distance_m = _get_nested(summary, "distance", "distanceInMeters")
    distance_km = round(distance_m / 1000, 2) if distance_m else None
    
    # 時間（複数のキー名候補、ミリ秒の場合も考慮）
    duration_sec = _get_nested(summary, "duration", "durationInSeconds", "elapsedDuration", "movingDuration")
    if duration_sec and duration_sec > 100000:  # ミリ秒っぽい場合は秒に変換
        duration_sec = duration_sec / 1000
    time_str = _format_duration(duration_sec) if duration_sec else ""
    
    avg_hr = _get_nested(summary, "averageHR", "avgHr", "averageHeartRate")
    te = _get_nested(summary, "aerobicTrainingEffect", "trainingEffect")
    
    date_only = ""
    if start_time:
        try:
            s = str(start_time)
            date_only = s.split("T")[0].split(" ")[0][:10]
        except Exception:
            pass
    
    sport_jp = _SPORT_MAP.get(sport_key, sport_key)
    
    print(f"  📝 抽出した値:")
    print(f"     名前: {activity_name}")
    print(f"     日付: {date_only}")
    print(f"     種目(key→jp): {sport_key!r} → {sport_jp!r}")
    print(f"     距離(km): {distance_km}")
    print(f"     タイム: {time_str}")
    print(f"     平均HR: {avg_hr}")
    print(f"     TE: {te}")
    
    desired = {
        "名前": ("title", activity_name[:200]),
        "日付": ("date", date_only),
        "種目": ("select", sport_jp),
        "距離 (km)": ("number", distance_km),
        "タイム": ("rich_text", time_str),
        "平均HR": ("number", round(avg_hr) if avg_hr else None),
        "TE": ("number", round(te, 1) if te else None),
    }
    
    properties: dict[str, Any] = {}
    for logical_name, (expected_type, value) in desired.items():
        if value is None or value == "":
            continue
        
        actual_name = _find_property_name(logical_name, schema)
        if not actual_name:
            print(f"     ⚠️ Property not found in schema: {logical_name!r}")
            continue
        
        actual_type = schema[actual_name]
        if actual_type != expected_type:
            print(f"     ⚠️ Type mismatch for {actual_name!r}: expected {expected_type}, got {actual_type}")
            continue
        
        try:
            if expected_type == "title":
                properties[actual_name] = {"title": [{"text": {"content": str(value)[:200]}}]}
            elif expected_type == "date":
                properties[actual_name] = {"date": {"start": value}}
            elif expected_type == "select":
                properties[actual_name] = {"select": {"name": str(value)[:100]}}
            elif expected_type == "number":
                properties[actual_name] = {"number": value}
            elif expected_type == "rich_text":
                properties[actual_name] = {"rich_text": [{"text": {"content": str(value)[:2000]}}]}
            print(f"     ✅ Will set {actual_name!r} = {value!r}")
        except Exception as e:
            print(f"     ❌ Failed to build {actual_name!r}: {e}")
    
    return properties


def _find_property_name(logical: str, schema: dict[str, str]) -> str | None:
    if logical in schema:
        return logical
    norm_logical = re.sub(r"\s+", "", logical)
    for name in schema:
        if re.sub(r"\s+", "", name) == norm_logical:
            return name
    for name in schema:
        if logical in name or name in logical:
            return name
    return None


_SPORT_MAP = {
    "running": "ラン",
    "trail_running": "トレイルラン",
    "treadmill_running": "トレッドミル",
    "track_running": "トラック",
    "cycling": "バイク",
    "road_biking": "ロードバイク",
    "mountain_biking": "MTB",
    "indoor_cycling": "Zwift",
    "virtual_ride": "Zwift",
    "lap_swimming": "プールスイム",
    "open_water_swimming": "OWS",
    "swimming": "スイム",
    "multi_sport": "マルチスポーツ",
    "strength_training": "筋トレ",
    "walking": "ウォーキング",
}


def _format_duration(sec: float) -> str:
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


# ====================== Markdown → Notion blocks ======================
def md_to_notion_blocks(markdown: str) -> list[dict]:
    blocks: list[dict] = []
    lines = markdown.split("\n")
    i = 0
    
    while i < len(lines):
        line = lines[i].rstrip()
        
        if not line.strip():
            i += 1
            continue
        
        if line.strip() in ("---", "___", "***"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue
        
        if line.startswith("## ") and not line.startswith("### "):
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(line[3:].strip())}
            })
            i += 1
            continue
        
        if line.startswith("### "):
            blocks.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": _rich_text(line[4:].strip())}
            })
            i += 1
            continue
        
        if line.startswith("# "):
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(line[2:].strip())}
            })
            i += 1
            continue
        
        if line.startswith("```"):
            lang = line[3:].strip() or "plain text"
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1
            blocks.append({
                "object": "block", "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines)[:2000]}}],
                    "language": lang if lang in _NOTION_LANGS else "plain text"
                }
            })
            continue
        
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i+1].rstrip()):
            table_lines = [line]
            i += 2
            while i < len(lines) and lines[i].rstrip().startswith("|"):
                table_lines.append(lines[i].rstrip())
                i += 1
            tb = _build_table_block(table_lines)
            if tb:
                blocks.append(tb)
            continue
        
        if re.match(r"^[\-\*]\s+", line):
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(re.sub(r"^[\-\*]\s+", "", line))}
            })
            i += 1
            continue
        
        if re.match(r"^\d+\.\s+", line):
            blocks.append({
                "object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _rich_text(re.sub(r"^\d+\.\s+", "", line))}
            })
            i += 1
            continue
        
        if line.startswith("> "):
            blocks.append({
                "object": "block", "type": "quote",
                "quote": {"rich_text": _rich_text(line[2:])}
            })
            i += 1
            continue
        
        blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(line)}
        })
        i += 1
    
    return blocks


_NOTION_LANGS = {
    "plain text", "bash", "c", "c++", "c#", "css", "diff", "docker", "go", "html",
    "java", "javascript", "json", "kotlin", "markdown", "python", "ruby", "rust",
    "shell", "sql", "swift", "typescript", "yaml",
}


def _rich_text(text: str) -> list[dict]:
    if not text:
        return []
    segments = []
    pattern = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")
    last_end = 0
    for match in pattern.finditer(text):
        if match.start() > last_end:
            segments.append((text[last_end:match.start()], {}))
        m = match.group()
        if m.startswith("**"):
            segments.append((m[2:-2], {"bold": True}))
        elif m.startswith("`"):
            segments.append((m[1:-1], {"code": True}))
        last_end = match.end()
    if last_end < len(text):
        segments.append((text[last_end:], {}))
    if not segments:
        segments = [(text, {})]
    return [
        {"type": "text", "text": {"content": c[:2000]}, "annotations": a}
        for c, a in segments if c
    ]


def _build_table_block(table_lines: list[str]) -> dict | None:
    rows = []
    for line in table_lines:
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    if not rows:
        return None
    table_width = max(len(r) for r in rows)
    rows = [r + [""] * (table_width - len(r)) for r in rows]
    children = [
        {"object": "block", "type": "table_row",
         "table_row": {"cells": [_rich_text(c) for c in row]}}
        for row in rows
    ]
    return {
        "object": "block", "type": "table",
        "table": {
            "table_width": table_width,
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        }
    }


# ====================== Notion ページ作成 ======================
def create_notion_page(notion: NotionClient, schema: dict[str, str], summary: dict, analysis_md: str) -> None:
    properties = build_properties(summary, schema)
    
    children = md_to_notion_blocks(analysis_md)
    
    print(f"  📤 Creating page with {len(properties)} properties, {len(children)} blocks")
    
    first_batch = children[:100]
    remaining = children[100:]
    
    try:
        page = notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=properties,
            children=first_batch,
        )
        page_id = page["id"]
        print(f"  ✅ Page created: {page_id}")
    except Exception as e:
        print(f"  ❌ Page creation failed with all properties: {e}")
        # 最小構成で再試行
        print(f"  🔄 Retrying with title only...")
        title_name = next((n for n, t in schema.items() if t == "title"), "名前")
        minimal = {title_name: {"title": [{"text": {"content": (summary.get("activityName") or "Unknown")[:200]}}]}}
        page = notion.pages.create(
            parent={"database_id": NOTION_DATABASE_ID},
            properties=minimal,
            children=first_batch,
        )
        page_id = page["id"]
        print(f"  ⚠️ Created with minimal: {page_id}")
    
    while remaining:
        batch = remaining[:100]
        remaining = remaining[100:]
        try:
            notion.blocks.children.append(block_id=page_id, children=batch)
        except Exception as e:
            print(f"  ⚠️ Failed to append blocks: {e}")
            break


# ====================== 状態 ======================
def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"analyzed_activity_ids": []}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2))


# ====================== main ======================
def main() -> int:
    state = load_state()
    analyzed_ids: set[int] = set(state.get("analyzed_activity_ids", []))
    
    try:
        client = garmin_login()
    except Exception as e:
        print(f"❌ Garmin login failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    
    target_date = resolve_target_date()
    activities = fetch_target_activities(client, target_date)
    
    if not activities:
        print(f"ℹ️ No activities on {target_date}. Done.")
        return 0
    
    # Notion クライアント（最新APIバージョン明示）
    notion = NotionClient(auth=NOTION_API_KEY, notion_version="2022-06-28")
    schema = fetch_notion_schema(notion)
    
    # v9: シーズンコンテキストと過去履歴を取得
    season_context = get_season_context(target_date)
    print(f"\n📅 Season Context:\n{season_context}")
    
    history_context = fetch_recent_history(notion, target_date, days=7)
    print(f"\n📊 Recent History fetched ({len(history_context)} chars)")
    
    new_count = 0
    for act in activities:
        activity_id = act.get("activityId")
        if activity_id in analyzed_ids:
            print(f"⏭️  Skip (already analyzed): {activity_id}")
            continue
        
        print(f"\n🏃 Activity: {act.get('activityName')} (id={activity_id})")
        try:
            detail = fetch_activity_detail(client, activity_id)
            # actのデータをsummaryにマージ（actにある値で、summaryにないorNoneのものを補完）
            merged_summary = dict(act)  # actのコピーから始める
            if detail.get("summary"):
                # detail['summary']で上書き（より詳細なデータが優先）
                for k, v in detail["summary"].items():
                    if v is not None and v != "":
                        merged_summary[k] = v
            detail["summary"] = merged_summary
            
            analysis = analyze_with_claude(detail, season_context, history_context)
            create_notion_page(notion, schema, detail["summary"], analysis)
            
            analyzed_ids.add(activity_id)
            new_count += 1
            time.sleep(2)
        except Exception as e:
            print(f"❌ Error for {activity_id}: {e}", file=sys.stderr)
            traceback.print_exc()
            continue
    
    state["analyzed_activity_ids"] = sorted(analyzed_ids)[-500:]
    state["last_run"] = datetime.now(JST).isoformat()
    save_state(state)
    
    print(f"\n✅ Done. Analyzed {new_count} new activities.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
