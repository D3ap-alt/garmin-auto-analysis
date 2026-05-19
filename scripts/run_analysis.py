"""
Garmin Connect → Claude → Notion 自動分析パイプライン (v4)

v4変更点:
  - 出力先を Google Docs → Notion データベースに変更
  - 1アクティビティ = 1Notionページ として追加
  - プロパティ自動入力: 名前/日付/種目/距離(km)/タイム/平均HR/TE
  - 分析結果本文は MarkdownブロックとしてNotionページ内に展開
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

# ====================== 設定 ======================
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
    
    try:
        client.login(str(TOKEN_DIR))
    except GarminConnectTooManyRequestsError as e:
        print(f"❌ 429 Too Many Requests: {e}", file=sys.stderr)
        raise
    except GarminConnectAuthenticationError as e:
        print(f"❌ Authentication failed: {e}", file=sys.stderr)
        raise
    
    print("✅ Fresh login succeeded")
    _print_token_for_secrets()
    return client


def _print_token_for_secrets() -> None:
    try:
        token_files = list(TOKEN_DIR.glob("*"))
        if not token_files:
            return
        
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(TOKEN_DIR, arcname=".")
        b64 = base64.b64encode(buf.getvalue()).decode()
        
        print("\n" + "=" * 70)
        print("📋 SAVE THIS TO GitHub Secrets as GARMIN_TOKENS_BASE64:")
        print("=" * 70)
        print(b64)
        print("=" * 70 + "\n")
    except Exception as e:
        print(f"⚠️ Could not export token: {e}")


# ====================== Garmin データ取得 ======================
def resolve_target_date() -> date_cls:
    if TARGET_DATE:
        try:
            target = datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()
            print(f"🎯 Target date (manual): {target}")
            return target
        except ValueError:
            print(f"⚠️ Invalid TARGET_DATE: {TARGET_DATE}, fallback to yesterday")
    
    target = (datetime.now(JST).date() - timedelta(days=1))
    print(f"🎯 Target date (yesterday JST): {target}")
    return target


def fetch_target_activities(client: Garmin, target_date: date_cls) -> list[dict[str, Any]]:
    date_str = target_date.isoformat()
    try:
        activities = client.get_activities_by_date(date_str, date_str)
    except Exception as e:
        print(f"❌ Failed to fetch activities: {e}", file=sys.stderr)
        return []
    
    print(f"📊 Found {len(activities)} activities on {date_str}")
    return activities


def fetch_activity_detail(client: Garmin, activity_id: int) -> dict[str, Any]:
    detail: dict[str, Any] = {}
    
    try:
        detail["summary"] = client.get_activity(activity_id)
    except Exception as e:
        print(f"  summary取得失敗: {e}")
    
    try:
        detail["laps"] = client.get_activity_splits(activity_id)
    except Exception as e:
        print(f"  laps取得失敗: {e}")
    
    try:
        detail["typed_splits"] = client.get_activity_typed_splits(activity_id)
    except Exception:
        pass
    
    return detail


# ====================== Claude 分析 ======================
def load_prompts() -> tuple[str, str]:
    skill = (PROMPTS_DIR / "garmin_analyzer_skill.md").read_text(encoding="utf-8")
    profile = (PROMPTS_DIR / "triathlon_profile.md").read_text(encoding="utf-8")
    return skill, profile


def select_model(activity_summary: dict) -> str:
    duration_sec = activity_summary.get("duration", 0) or 0
    distance_m = activity_summary.get("distance", 0) or 0
    activity_type = (activity_summary.get("activityType", {}) or {}).get("typeKey", "")
    
    if duration_sec > 3600 or distance_m > 15000 or "race" in str(activity_type).lower():
        return "claude-opus-4-7"
    return "claude-sonnet-4-6"


def analyze_with_claude(activity_data: dict) -> str:
    skill_md, profile_md = load_prompts()
    model = select_model(activity_data.get("summary", {}))
    
    system_prompt = f"""あなたはGarminトレーニング分析の専門家です。
以下のスキル定義と個人プロフィールに**完全に従って**分析してください。
出力はNotionに貼り付ける Markdown 形式。簡潔すぎる回答は不可。
- 見出しは ## (h2) または ### (h3) を使う（# は使わない、ページタイトル扱いのため）
- 表は Markdownテーブル形式（| ... |）で出力 → Notionが自動でテーブルブロックに変換

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
    
    user_prompt = f"""## 本日の分析対象

以下はアクティビティデータです。スキル定義の「出力形式」テンプレートに完全準拠して分析してください。

- 「ラップ深掘り」「個別の発見（Lap X現象 等）」「改善余地と限界」「次回トレーニング提案」を必ず含める
- 数字は必ずベンチマーク比較とコンテキストつきで提示
- Lap 9 がある場合は個人プロフィールの「Lap 9 練習パターン」を踏まえる
- 見出しは ## (h2) または ### (h3) を使用、# は使わない

```json
{json.dumps(payload, ensure_ascii=False, default=str)[:80000]}
```
"""
    
    print(f"  🤖 Analyzing with {model}...")
    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = anthropic_client.messages.create(
        model=model,
        max_tokens=4096,
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


# ====================== Notion: Markdownブロック変換 ======================
def md_to_notion_blocks(markdown: str) -> list[dict]:
    """
    MarkdownテキストをNotionブロックの配列に変換する。
    対応: h2, h3, paragraph, bulleted_list_item, numbered_list_item, table, code, divider
    """
    blocks: list[dict] = []
    lines = markdown.split("\n")
    i = 0
    
    while i < len(lines):
        line = lines[i].rstrip()
        
        # 空行
        if not line.strip():
            i += 1
            continue
        
        # 区切り線
        if line.strip() in ("---", "___", "***"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            i += 1
            continue
        
        # 見出し（## h2）
        if line.startswith("## ") and not line.startswith("### "):
            text = line[3:].strip()
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(text)}
            })
            i += 1
            continue
        
        # 見出し（### h3）
        if line.startswith("### "):
            text = line[4:].strip()
            blocks.append({
                "object": "block", "type": "heading_3",
                "heading_3": {"rich_text": _rich_text(text)}
            })
            i += 1
            continue
        
        # 見出し（# h1）→ NotionはページタイトルがH1なので、本文ではH2扱い
        if line.startswith("# "):
            text = line[2:].strip()
            blocks.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": _rich_text(text)}
            })
            i += 1
            continue
        
        # コードブロック
        if line.startswith("```"):
            lang = line[3:].strip() or "plain text"
            code_lines = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 閉じる```をスキップ
            code_text = "\n".join(code_lines)
            blocks.append({
                "object": "block", "type": "code",
                "code": {
                    "rich_text": [{"type": "text", "text": {"content": code_text[:2000]}}],
                    "language": lang if lang in _NOTION_LANGS else "plain text"
                }
            })
            continue
        
        # テーブル
        if line.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s\-:|]+\|$", lines[i+1].rstrip()):
            table_lines = [line]
            i += 1  # ヘッダ行
            # 区切り行をスキップ
            sep_line = lines[i].rstrip()
            i += 1
            # データ行を集める
            while i < len(lines) and lines[i].rstrip().startswith("|"):
                table_lines.append(lines[i].rstrip())
                i += 1
            
            table_block = _build_table_block(table_lines)
            if table_block:
                blocks.append(table_block)
            continue
        
        # 箇条書き
        if re.match(r"^[\-\*]\s+", line):
            text = re.sub(r"^[\-\*]\s+", "", line)
            blocks.append({
                "object": "block", "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(text)}
            })
            i += 1
            continue
        
        # 番号付きリスト
        if re.match(r"^\d+\.\s+", line):
            text = re.sub(r"^\d+\.\s+", "", line)
            blocks.append({
                "object": "block", "type": "numbered_list_item",
                "numbered_list_item": {"rich_text": _rich_text(text)}
            })
            i += 1
            continue
        
        # 引用
        if line.startswith("> "):
            text = line[2:]
            blocks.append({
                "object": "block", "type": "quote",
                "quote": {"rich_text": _rich_text(text)}
            })
            i += 1
            continue
        
        # 通常の段落
        blocks.append({
            "object": "block", "type": "paragraph",
            "paragraph": {"rich_text": _rich_text(line)}
        })
        i += 1
    
    return blocks


_NOTION_LANGS = {
    "plain text", "abap", "arduino", "bash", "basic", "c", "clojure", "coffeescript",
    "c++", "c#", "css", "dart", "diff", "docker", "elixir", "elm", "erlang", "flow",
    "fortran", "f#", "gherkin", "glsl", "go", "graphql", "groovy", "haskell", "html",
    "java", "javascript", "json", "julia", "kotlin", "latex", "less", "lisp", "livescript",
    "lua", "makefile", "markdown", "markup", "matlab", "mermaid", "nix", "objective-c",
    "ocaml", "pascal", "perl", "php", "powershell", "prolog", "protobuf", "python", "r",
    "reason", "ruby", "rust", "sass", "scala", "scheme", "scss", "shell", "sql", "swift",
    "typescript", "vb.net", "verilog", "vhdl", "visual basic", "webassembly", "xml", "yaml",
}


def _rich_text(text: str) -> list[dict]:
    """Notionのrich_text配列を作る。最大2000文字制限。**bold**, *italic*, `code` 簡易対応。"""
    if not text:
        return []
    
    # 簡易マークダウン処理（**bold**, `code`）
    segments = []
    pattern = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")
    last_end = 0
    
    for match in pattern.finditer(text):
        if match.start() > last_end:
            segments.append((text[last_end:match.start()], {}))
        
        m = match.group()
        if m.startswith("**") and m.endswith("**"):
            segments.append((m[2:-2], {"bold": True}))
        elif m.startswith("`") and m.endswith("`"):
            segments.append((m[1:-1], {"code": True}))
        
        last_end = match.end()
    
    if last_end < len(text):
        segments.append((text[last_end:], {}))
    
    if not segments:
        segments = [(text, {})]
    
    result = []
    for content, annotations in segments:
        if content:
            result.append({
                "type": "text",
                "text": {"content": content[:2000]},
                "annotations": annotations,
            })
    return result


def _build_table_block(table_lines: list[str]) -> dict | None:
    """Markdownテーブル行をNotionのtableブロックに変換"""
    rows = []
    for line in table_lines:
        # |col1|col2|col3| → ['col1', 'col2', 'col3']
        cells = [c.strip() for c in line.strip("|").split("|")]
        rows.append(cells)
    
    if not rows:
        return None
    
    table_width = max(len(r) for r in rows)
    # 行の長さを揃える
    rows = [r + [""] * (table_width - len(r)) for r in rows]
    
    children = []
    for idx, row in enumerate(rows):
        children.append({
            "object": "block", "type": "table_row",
            "table_row": {
                "cells": [_rich_text(c) for c in row]
            }
        })
    
    return {
        "object": "block", "type": "table",
        "table": {
            "table_width": table_width,
            "has_column_header": True,
            "has_row_header": False,
            "children": children,
        }
    }


# ====================== Notion: ページ作成 ======================
def create_notion_page(activity_summary: dict, analysis_md: str) -> None:
    """Notionデータベースに新規ページを作成して分析結果を書き込む"""
    notion = NotionClient(auth=NOTION_API_KEY)
    
    # プロパティ抽出
    activity_name = activity_summary.get("activityName") or "アクティビティ"
    start_time = activity_summary.get("startTimeLocal") or ""
    sport_key = (activity_summary.get("activityType") or {}).get("typeKey") or ""
    
    distance_m = activity_summary.get("distance") or 0
    distance_km = round(distance_m / 1000, 2) if distance_m else None
    
    duration_sec = activity_summary.get("duration") or 0
    time_str = _format_duration(duration_sec) if duration_sec else ""
    
    avg_hr = activity_summary.get("averageHR")
    te = activity_summary.get("aerobicTrainingEffect")
    
    # 日付（YYYY-MM-DD）
    date_only = ""
    if start_time:
        try:
            date_only = start_time.split("T")[0][:10]
            if " " in date_only:
                date_only = date_only.split(" ")[0]
        except Exception:
            pass
    
    # 種目を日本語化
    sport_jp = _SPORT_MAP.get(sport_key, sport_key)
    
    # プロパティを構築
    properties: dict[str, Any] = {
        "名前": {"title": [{"text": {"content": activity_name[:200]}}]},
    }
    if date_only:
        properties["日付"] = {"date": {"start": date_only}}
    if sport_jp:
        properties["種目"] = {"select": {"name": sport_jp[:100]}}
    if distance_km is not None:
        properties["距離 (km)"] = {"number": distance_km}
    if time_str:
        properties["タイム"] = {"rich_text": [{"text": {"content": time_str}}]}
    if avg_hr:
        properties["平均HR"] = {"number": round(avg_hr)}
    if te:
        properties["TE"] = {"number": round(te, 1)}
    
    # 本文ブロックに変換
    children = md_to_notion_blocks(analysis_md)
    
    # Notion APIは1リクエストあたり100ブロックまで → 分割対応
    first_batch = children[:100]
    remaining = children[100:]
    
    page = notion.pages.create(
        parent={"database_id": NOTION_DATABASE_ID},
        properties=properties,
        children=first_batch,
    )
    
    # 残りのブロックは追記
    page_id = page["id"]
    while remaining:
        batch = remaining[:100]
        remaining = remaining[100:]
        notion.blocks.children.append(block_id=page_id, children=batch)
    
    print(f"  ✅ Created Notion page: {activity_name}")


_SPORT_MAP = {
    "running": "ラン",
    "trail_running": "トレイルラン",
    "treadmill_running": "トレッドミル",
    "track_running": "トラック",
    "cycling": "バイク",
    "road_biking": "ロードバイク",
    "mountain_biking": "MTB",
    "indoor_cycling": "Zwift/室内バイク",
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


# ====================== 状態管理 ======================
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
    except GarminConnectTooManyRequestsError:
        return 1
    except Exception as e:
        print(f"❌ Garmin login failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    
    target_date = resolve_target_date()
    
    try:
        activities = fetch_target_activities(client, target_date)
    except Exception as e:
        print(f"❌ Failed to fetch activities: {e}", file=sys.stderr)
        return 1
    
    if not activities:
        print(f"ℹ️ No activities on {target_date}. Done.")
        return 0
    
    new_count = 0
    for act in activities:
        activity_id = act.get("activityId")
        if activity_id in analyzed_ids:
            print(f"⏭️  Skip (already analyzed): {activity_id}")
            continue
        
        print(f"\n🏃 Activity: {act.get('activityName')} (id={activity_id})")
        try:
            detail = fetch_activity_detail(client, activity_id)
            if not detail.get("summary"):
                detail["summary"] = act
            
            analysis = analyze_with_claude(detail)
            create_notion_page(detail["summary"], analysis)
            
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
