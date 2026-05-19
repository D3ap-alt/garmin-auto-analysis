"""
Garmin Connect → Claude → Google Docs 自動分析パイプライン
GitHub Actionsで毎朝実行される本番スクリプト

処理フロー:
  1. Garmin Connect から前日分のアクティビティ一覧取得（garth使用、トークン永続化）
  2. 既に分析済みのIDをstate.jsonでチェック → 重複防止
  3. 各アクティビティのラップ詳細・統計を取得
  4. Claude APIに送信（garmin-analyzerスキル + 個人プロフィールをsystem promptに）
  5. Google Docs API で「トレーニング分析ログ」ドキュメントに追記
  6. state.jsonを更新してリポジトリにcommit/push
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import garth
from anthropic import Anthropic
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ====================== 設定 ======================
JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent  # scripts/ の親
STATE_PATH = ROOT / "state.json"
PROMPTS_DIR = ROOT / "prompts"
GARTH_TOKEN_DIR = ROOT / ".garth"  # CI環境では環境変数から復元

# 環境変数（GitHub Secretsから注入）
GARMIN_EMAIL = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD = os.environ["GARMIN_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GOOGLE_DOC_ID = os.environ["GOOGLE_DOC_ID"]  # 追記先ドキュメントのID
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]
GARTH_TOKENS_BASE64 = os.environ.get("GARTH_TOKENS_BASE64", "")  # 任意：前回のトークン


# ====================== Garmin認証 ======================
def garmin_login() -> None:
    """garthでGarminにログイン。トークンを永続化して2回目以降のログインを省略。"""
    GARTH_TOKEN_DIR.mkdir(exist_ok=True)
    
    # 前回のトークンがあれば復元（GitHub Secretsに保存しておく運用）
    if GARTH_TOKENS_BASE64:
        import base64, tarfile, io
        try:
            tar_bytes = base64.b64decode(GARTH_TOKENS_BASE64)
            with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
                tar.extractall(GARTH_TOKEN_DIR)
            garth.resume(str(GARTH_TOKEN_DIR))
            # トークン有効性チェック
            garth.client.username
            print("✅ Resumed from cached token")
            return
        except Exception as e:
            print(f"⚠️ Token resume failed ({e}), re-login")
    
    # 新規ログイン
    garth.login(GARMIN_EMAIL, GARMIN_PASSWORD)
    garth.save(str(GARTH_TOKEN_DIR))
    print("✅ Fresh login")


# ====================== Garmin データ取得 ======================
def fetch_yesterday_activities() -> list[dict[str, Any]]:
    """前日（JST 0:00〜23:59）に開始されたアクティビティを取得"""
    now_jst = datetime.now(JST)
    yesterday = now_jst.date() - timedelta(days=1)
    start_str = yesterday.isoformat()
    end_str = yesterday.isoformat()
    
    # garth経由でREST API叩く
    activities = garth.connectapi(
        f"/activitylist-service/activities/search/activities",
        params={"startDate": start_str, "endDate": end_str, "limit": 20},
    )
    print(f"📊 Found {len(activities)} activities on {start_str}")
    return activities


def fetch_activity_detail(activity_id: int) -> dict[str, Any]:
    """ラップ・統計含む詳細データを取得"""
    detail: dict[str, Any] = {}
    
    # サマリー
    try:
        detail["summary"] = garth.connectapi(f"/activity-service/activity/{activity_id}")
    except Exception as e:
        print(f"  summary取得失敗: {e}")
    
    # ラップ（splits）
    try:
        detail["laps"] = garth.connectapi(
            f"/activity-service/activity/{activity_id}/splits"
        )
    except Exception as e:
        print(f"  laps取得失敗: {e}")
    
    # 追加メトリクス（TE等）
    try:
        detail["typed_splits"] = garth.connectapi(
            f"/activity-service/activity/{activity_id}/typedsplits"
        )
    except Exception:
        pass
    
    return detail


# ====================== Claude 分析 ======================
def load_prompts() -> tuple[str, str]:
    """システムプロンプト用にスキル本体と個人プロフィールを読み込む"""
    skill = (PROMPTS_DIR / "garmin_analyzer_skill.md").read_text(encoding="utf-8")
    profile = (PROMPTS_DIR / "triathlon_profile.md").read_text(encoding="utf-8")
    return skill, profile


def select_model(activity_summary: dict) -> str:
    """活動内容に応じてモデルを自動選択（コスト最適化）"""
    duration_sec = activity_summary.get("duration", 0) or 0
    distance_m = activity_summary.get("distance", 0) or 0
    
    # 60分超 or 15km超 or レースは高精度モデル
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
    
    # JSONをそのまま渡すと長すぎることがあるので、主要部分のみ
    payload = {
        "summary": _trim_summary(activity_data.get("summary", {})),
        "laps": activity_data.get("laps", {}),
    }
    
    user_prompt = f"""## 本日の分析対象

以下は前日のアクティビティです。スキル定義の「出力形式」テンプレートに完全準拠して分析してください。

- 「ラップ深掘り」「個別の発見（Lap X現象 等）」「改善余地と限界」「次回トレーニング提案」を必ず含める
- 数字は必ずベンチマーク比較とコンテキストつきで提示
- Lap 9 がある場合は個人プロフィールの「Lap 9 練習パターン」を踏まえる

```json
{json.dumps(payload, ensure_ascii=False, default=str)[:80000]}
```
"""
    
    print(f"  🤖 Analyzing with {model}...")
    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    msg = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text


def _trim_summary(s: dict) -> dict:
    """サマリから重要キーだけ抜く（プロンプト圧縮）"""
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
    """分析結果をGoogleドキュメントの末尾に追記"""
    creds_info = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
    credentials = Credentials.from_service_account_info(
        creds_info, scopes=["https://www.googleapis.com/auth/documents"]
    )
    service = build("docs", "v1", credentials=credentials)
    
    # ヘッダ生成
    activity_name = activity_summary.get("activityName", "アクティビティ")
    start_time = activity_summary.get("startTimeLocal", "")
    sport = (activity_summary.get("activityType") or {}).get("typeKey", "")
    
    header = f"\n\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    header += f"# {start_time}  {activity_name}（{sport}）\n"
    header += f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    
    content = header + analysis_md + "\n"
    
    # ドキュメント末尾のインデックスを取得
    doc = service.documents().get(documentId=GOOGLE_DOC_ID).execute()
    end_index = doc["body"]["content"][-1]["endIndex"] - 1
    
    # 末尾に挿入
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
        garmin_login()
    except Exception as e:
        print(f"❌ Garmin login failed: {e}", file=sys.stderr)
        traceback.print_exc()
        return 1
    
    try:
        activities = fetch_yesterday_activities()
    except Exception as e:
        print(f"❌ Failed to fetch activities: {e}", file=sys.stderr)
        return 1
    
    if not activities:
        print("ℹ️ No activities yesterday. Done.")
        return 0
    
    new_count = 0
    for act in activities:
        activity_id = act.get("activityId")
        if activity_id in analyzed_ids:
            print(f"⏭️  Skip (already analyzed): {activity_id}")
            continue
        
        print(f"\n🏃 Activity: {act.get('activityName')} (id={activity_id})")
        try:
            detail = fetch_activity_detail(activity_id)
            # summary が空ならリストAPIの軽量データで代用
            if not detail.get("summary"):
                detail["summary"] = act
            
            analysis = analyze_with_claude(detail)
            append_to_google_doc(detail["summary"], analysis)
            
            analyzed_ids.add(activity_id)
            new_count += 1
            time.sleep(2)  # API レート対策
        except Exception as e:
            print(f"❌ Error for {activity_id}: {e}", file=sys.stderr)
            traceback.print_exc()
            # 1件失敗しても他は続ける
            continue
    
    # 状態保存（最新500件分のみ）
    state["analyzed_activity_ids"] = sorted(analyzed_ids)[-500:]
    state["last_run"] = datetime.now(JST).isoformat()
    save_state(state)
    
    print(f"\n✅ Done. Analyzed {new_count} new activities.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
