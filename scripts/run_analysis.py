"""
Garmin Connect → Claude → Google Docs 自動分析パイプライン (v3)

v3変更点:
  - garminconnect 0.3.3 の正しいトークン保存API
  - 環境変数 TARGET_DATE で日付指定可能（YYYY-MM-DD形式）
    指定なし → 前日（JST）を自動選択
    手動実行時に「今日」や「特定日」を分析できる
"""

from __future__ import annotations

import base64
import io
import json
import os
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
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ====================== 設定 ======================
JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent
STATE_PATH = ROOT / "state.json"
PROMPTS_DIR = ROOT / "prompts"
TOKEN_DIR = Path.home() / ".garminconnect"

GARMIN_EMAIL = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_DOC_ID = os.environ["GOOGLE_DOC_ID"]
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GARMIN_TOKENS_BASE64 = os.environ.get("GARMIN_TOKENS_BASE64", "")
TARGET_DATE = os.environ.get("TARGET_DATE", "").strip()  # YYYY-MM-DD or empty


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
            print("✅ Resumed session from saved tokens (no fresh login needed)")
            return client
        except Exception as e:
            print(f"⚠️ Token resume failed: {e}")
            print("   → Falling back to fresh login")
    
    print("🔐 Attempting fresh login (this may take 10-30s)...")
    client = Garmin(email=GARMIN_EMAIL, password=GARMIN_PASSWORD)
    
    try:
        client.login(str(TOKEN_DIR))
    except GarminConnectTooManyRequestsError as e:
        print(f"❌ 429 Too Many Requests: {e}", file=sys.stderr)
        print("   Account is rate-limited. Wait 6-24 hours and retry.", file=sys.stderr)
        raise
    except GarminConnectAuthenticationError as e:
        print(f"❌ Authentication failed: {e}", file=sys.stderr)
        raise
    
    print("✅ Fresh login succeeded")
    _print_token_for_secrets()
    return client


def _print_token_for_secrets() -> None:
    """トークンをbase64化してログ出力。手動でSecretsに登録するため。"""
    try:
        token_files = list(TOKEN_DIR.glob("*"))
        if not token_files:
            print("⚠️ No token files found in", TOKEN_DIR)
            return
        
        print(f"   Token files saved: {[f.name for f in token_files]}")
        
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            tar.add(TOKEN_DIR, arcname=".")
        b64 = base64.b64encode(buf.getvalue()).decode()
        
        print("\n" + "=" * 70)
        print("📋 SAVE THIS TO GitHub Secrets as GARMIN_TOKENS_BASE64:")
        print("=" * 70)
        print(b64)
        print("=" * 70)
        print("(Adding this Secret will skip login on future runs, avoiding 429.)\n")
    except Exception as e:
        print(f"⚠️ Could not export token for Secrets: {e}")
        traceback.print_exc()


# ====================== Garmin データ取得 ======================
def resolve_target_date() -> date_cls:
    """分析対象の日付を決定。TARGET_DATE環境変数が優先。"""
    if TARGET_DATE:
        try:
            target = datetime.strptime(TARGET_DATE, "%Y-%m-%d").date()
            print(f"🎯 Target date (manual): {target}")
            return target
        except ValueError:
            print(f"⚠️ Invalid TARGET_DATE format: {TARGET_DATE}, falling back to yesterday")
    
    # デフォルト: 前日（JST）
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
出力は Google ドキュメントに貼り付ける Markdown 形式。簡潔すぎる回答は不可。

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


# ====================== Google Docs 追記 ======================
def append_to_google_doc(activity_summary: dict, analysis_md: str) -> None:
    creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/documents"]
    )
    service = build("docs", "v1", credentials=credentials)
    
    activity_name = activity_summary.get("activityName", "アクティビティ")
    start_time = activity_summary.get("startTimeLocal", "")
    sport = (activity_summary.get("activityType") or {}).get("typeKey", "")
    
    header = f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    header += f"# {start_time}  {activity_name}({sport})\n"
    header += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    content = header + analysis_md + "\n"
    
    doc = service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    end_index = doc["body"]["content"][-1]["endIndex"] - 1
    
    requests = [{"insertText": {"location": {"index": end_index}, "text": content}}]
    service.documents().batchUpdate(
        documentId=GOOGLE_DOC_ID, body={"requests": requests}
    ).execute()
    print(f"  ✅ Appended to Google Doc")


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
            append_to_google_doc(detail["summary"], analysis)
            
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
