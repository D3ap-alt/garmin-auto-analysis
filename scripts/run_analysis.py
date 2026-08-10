"""
Garmin Connect → Claude → Notion 自動分析パイプライン (v15)

v15変更点 (レースマージ機能 / FIT手動投入):
  - race_merge.py を新設。レース日（同日にS/B/R揃い・バイク二重記録・
    マルチスポーツ記録のいずれか）を検出し、部位別に正データを採用して
    1ページに統合分析する。
    - スイム/ラン/トランジション/総合 → Forerunner 965 を正
    - バイクのパワー/NP/IF/VI/ケイデンス/速度/標高 → Edge 840 を正。
      デバイス確定判定（product ID: 965=4315 / Edge840=4062）を最優先、
      パワー指標充実度はフォールバック。965側の重複バイクは破棄。
      バイクのHRのみ965を維持（時系列連続性）。
    - build_race_digest() で採用/破棄ルール・トランジション実測・Edge専用
      指標（最大W/IF/TSS/獲得標高）を機械集計として最優先注入。
  - fit_loader.py を新設。FIT(.fit)を Garmin API JSON 相当の dict に変換。
    マルチスポーツFITを session 単位で S/T1/B/T2/R に分解。
  - analyze_race_fits.py を新設。手元のレースFIT（965マルチ + Edge840バイク）
    を渡すと統合分析→Notion投稿まで行う手動エントリポイント（--dry-run対応）。
  - レース時の距離不一致（マルチFITのsummary距離がラップ合計より過大になる
    ケース）は、ラップ合計を主・summaryを参考とし両値併記する方針をプロンプト注入。
  - process_race_day() を main() に分岐追加。失敗時は従来の個別分析へフォールバック。

v13変更点 (ラップ構造誤認バグの根本対策):
  - summarize_laps() を新設。ラップ配列を機械集計し、距離別の連続ブロック
    （例: 100m×12本, 300m×5本）と合計距離の検算結果をプロンプトに同梱。
    → モデルに本数・距離を目視で数え直させない（300m×5本→×4本の数え違い防止）。
  - システムプロンプトにガード追加:
    - 本数・距離・合計は機械集計を正とする
    - 用具(パドル/プル等)・セット種別(ドリル/レスト/テンポ)をラップデータから断定しない
      （ペース差だけからブロックの意味づけを創作するのを禁止）

v10変更点 (バグ修正・改善):
  - max_tokens を 4096 → 8192 に増量（分析が途中で切れる問題を解決）
  - Notion API の databases.query() を 廃止/旧API両対応に修正
    - data_sources.query → databases.query → notion.request の順でフォールバック
"""

from __future__ import annotations

import base64
import gzip
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

# scripts/ を実行ディレクトリに依らず import 可能にする
# （ワークフローは `python scripts/run_analysis.py` で起動するため sys.path[0] はリポジトリルートになる）
sys.path.insert(0, str(Path(__file__).parent))
import race_merge
from raw_archive import archive_activity, assign_brick_keys, rebuild_index

JST = timezone(timedelta(hours=9))
ROOT = Path(__file__).parent.parent
STATE_PATH = ROOT / "state.json"
PROMPTS_DIR = ROOT / "prompts"
TOKEN_DIR = Path.home() / ".garminconnect"
TOKEN_STORE = ROOT / "garmin_token.b64"  # 更新後トークンをリポジトリに永続化（429回避の要）

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
def _export_tokens_b64() -> str:
    """TOKEN_DIR 内のトークンファイルを tar+gzip して base64 文字列にする。
    mtime 等を 0 に固定し、トークン内容が同じなら毎回まったく同じ出力にする
    （gzip のタイムスタンプ差分による毎時無駄コミットを防ぐ）。"""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for p in sorted(TOKEN_DIR.iterdir()):
            if p.is_file():
                data = p.read_bytes()
                ti = tarfile.TarInfo(name=p.name)
                ti.size = len(data)
                ti.mtime = 0
                ti.uid = ti.gid = 0
                ti.uname = ti.gname = ""
                tar.addfile(ti, io.BytesIO(data))
    gz = io.BytesIO()
    with gzip.GzipFile(fileobj=gz, mode="wb", mtime=0) as g:
        g.write(raw.getvalue())
    return base64.b64encode(gz.getvalue()).decode("ascii")


def _persist_tokens(client: Garmin) -> None:
    """ログイン/リフレッシュ後の最新トークンを TOKEN_DIR に dump し、リポジトリの
    TOKEN_STORE(garmin_token.b64) に書き戻す。次回実行は新鮮なトークンで resume でき、
    GitHub の IP から新規ログイン（429 でブロックされやすい）を踏まずに済む。"""
    try:
        try:
            client.garth.dump(str(TOKEN_DIR))
        except Exception:
            pass
        TOKEN_STORE.write_text(_export_tokens_b64(), encoding="ascii")
        print("💾 Persisted refreshed Garmin tokens to repo store")
    except Exception as e:
        print(f"⚠️ Token persist failed (continuing): {e}")


def _extract_tokens_to_dir() -> bool:
    """resume 用トークンを TOKEN_DIR へ展開する。
    優先順位: リポジトリの TOKEN_STORE（最新・毎回書き戻し）> Secret の GARMIN_TOKENS_BASE64。"""
    src = ""
    if TOKEN_STORE.exists():
        try:
            src = TOKEN_STORE.read_text(encoding="ascii").strip()
            if src:
                print("🗂️ Using committed token store (garmin_token.b64)")
        except Exception as e:
            print(f"⚠️ token store 読込失敗: {e}")
            src = ""
    if not src and GARMIN_TOKENS_BASE64:
        src = GARMIN_TOKENS_BASE64
        print("🗂️ Using GARMIN_TOKENS_BASE64 secret")
    if not src:
        return False
    try:
        tar_bytes = base64.b64decode(src)
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:gz") as tar:
            tar.extractall(TOKEN_DIR)
        return True
    except Exception as e:
        print(f"⚠️ token 展開失敗: {e}")
        return False


def _is_rate_limit(e: Exception) -> bool:
    return (
        isinstance(e, GarminConnectTooManyRequestsError)
        or "429" in str(e)
        or "Too Many Requests" in str(e)
    )


def garmin_login() -> Garmin:
    TOKEN_DIR.mkdir(exist_ok=True)

    # 1) 保存済みトークンで resume（TOKEN_STORE を最優先、無ければ Secret）
    if _extract_tokens_to_dir():
        try:
            client = Garmin()
            client.login(str(TOKEN_DIR))
            print("✅ Resumed session from saved tokens")
            _persist_tokens(client)
            return client
        except Exception as e:
            print(f"⚠️ Token resume failed: {e}")

    # 2) 新規ログイン。GitHub の IP は 429 になりやすいので指数バックオフで数回リトライ
    print("🔐 Attempting fresh login...")
    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            client = Garmin(email=GARMIN_EMAIL, password=GARMIN_PASSWORD)
            client.login(str(TOKEN_DIR))
            print("✅ Fresh login succeeded")
            _persist_tokens(client)
            return client
        except Exception as e:
            last_err = e
            if _is_rate_limit(e) and attempt < 3:
                wait = 30 * attempt
                print(f"⏳ Rate limited (429) on fresh login (attempt {attempt}/3). Waiting {wait}s...")
                time.sleep(wait)
                continue
            raise
    raise last_err if last_err else RuntimeError("Garmin login failed")


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


# ====================== ローデータ保存（raw_archive ラッパ） ======================
def _act_date(act: dict[str, Any]) -> str:
    """アクティビティから 'YYYY-MM-DD' を取り出す（取れなければ空文字）。"""
    s = act.get("startTimeLocal") or act.get("startTimeGMT") or ""
    s = str(s)
    return s.split("T")[0].split(" ")[0][:10] if s else ""


def build_brick_keys(targets: list[dict[str, Any]]) -> dict[str, str]:
    """同日・60分以内の連続セッションに brick キーを振る。
    startTimeLocal が壊れているアクティビティは黙って除外する（分析は止めない）。"""
    rows: list[dict[str, Any]] = []
    for a in targets:
        aid = a.get("activityId")
        raw = a.get("startTimeLocal")
        if aid is None or not raw:
            continue
        try:
            st = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            continue
        rows.append({"activity_id": aid, "date": _act_date(a), "start_time": st})
    if not rows:
        return {}
    try:
        keys = assign_brick_keys(rows)
        if keys:
            print(f"🔗 ブリック検出: {keys}")
        return keys
    except Exception as e:
        print(f"[raw] WARN brick key 付与失敗: {e}")
        return {}


def archive_raw(
    client: Garmin,
    activity_id: Any,
    date_str: str,
    brick_key: str | None = None,
) -> dict | None:
    """FIT原本＋抽出JSONを data/raw/ に保存する。
    ここでの失敗は分析本体を絶対に止めない（必ず None を返して続行）。"""
    if not activity_id or not date_str:
        return None
    try:
        archive = archive_activity(client, activity_id, date_str, brick_key=brick_key)
        print(f"[raw] saved {archive['stem']} sport={archive['sport']} "
              f"laps={len(archive.get('files', {}))}files")
        return archive
    except Exception as e:
        print(f"[raw] WARN {activity_id}: {e}")
        return None


# ====================== 日付・曜日ユーティリティ ======================
_WEEKDAY_JP = ["月", "火", "水", "木", "金", "土", "日"]


def jp_weekday(d: date_cls) -> str:
    """date → '月'〜'日'（date.weekday() は月曜=0）"""
    return _WEEKDAY_JP[d.weekday()]


def build_date_anchor(today: date_cls, days_ahead: int = 16) -> str:
    """モデルに曜日を推定させず、正しい曜日カレンダーを明示的に渡す。
    日別トレーニング提案の曜日ズレ（モデルの曜日自力計算ミス）を防ぐ。"""
    lines = [f"- **本日: {today.isoformat()}（{jp_weekday(today)}）**", "", "今後の曜日対応表（提案の曜日は必ずこの表に一致させること）:"]
    for i in range(days_ahead + 1):
        d = today + timedelta(days=i)
        mark = " ← 本日" if i == 0 else ""
        lines.append(f"  - {d.isoformat()}（{jp_weekday(d)}）{mark}")
    return "\n".join(lines)


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
- **{race['name']}**: {race['date']}（{jp_weekday(race_date)}） ({race['distance']}, 目標 {race['goal']}, 重要度 {race['tier']})
- **あと {days_to_race} 日**

### 現在のシーズン位置づけ
- **{phase}**
"""
    
    if next_a_race and next_a_race[0]["name"] != race["name"]:
        a_race, a_race_date, a_days = next_a_race
        context += f"""
### 次のA戦
- **{a_race['name']}**: {a_race['date']}（{jp_weekday(a_race_date)}） (あと {a_days} 日, 目標 {a_race['goal']})
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


def load_sleep_context(target_date: date_cls) -> str:
    """v13: sleep_state.json から当日朝の睡眠サマリ1行を返す（無ければ空文字）。"""
    path = ROOT / "sleep_state.json"
    if not path.exists():
        return ""
    try:
        hist = json.loads(path.read_text(encoding="utf-8")).get("history", [])
        rec = next((r for r in hist if r.get("date") == target_date.isoformat()), None)
        if not rec:
            return ""
        t = rec.get("total_min")
        dur = f"{t // 60}h{t % 60:02d}m" if t else "—"
        return (
            f"睡眠スコア{rec.get('score', '—')}／総睡眠{dur}"
            f"（深い{rec.get('deep_min', '—')}分・REM{rec.get('rem_min', '—')}分）"
            f"／夜間HRV {rec.get('hrv_avg', '—')}ms"
            f"（週平均{rec.get('hrv_weekly_avg', '—')}ms・{rec.get('hrv_status', '—')}）"
            f"／安静時HR {rec.get('rhr', '—')}bpm"
            f"／Body Battery変化 {rec.get('bb_change', '—')}"
        )
    except Exception as e:
        print(f"  ⚠️ sleep context読込失敗: {e}")
        return ""


def analyze_with_claude(activity_data: dict, season_context: str, history_context: str, target_date: date_cls, sleep_context: str = "", race_digest: str = "") -> str:
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
- **曜日は絶対に自分で計算しないこと。ユーザープロンプトの「日付・曜日対応表」に記載された曜日のみを使用すること。** 表にない日付の曜日には言及しない。
- **ラップの本数・距離・合計は、ユーザープロンプトの「ラップ機械集計」セクションの値を正とすること。** 自分でラップを数え直したり、距離を推定で書き換えたりしない（例: 300m×5本を×4本と書く等の数え違いを禁止）。集計と矛盾する本文は書かない。
- **ラップデータから判別できないことを断定しないこと。** 具体的には次を推測で確定させない:
  - 用具（パドル/プル/フィン/ビート板など）の有無 — APIラップに用具情報は含まれない
  - セット種別（ドリル/レスト/テンポ/メイン等のラベル）
  - ペースが速い/遅い理由（用具補助・流し・全力など）
  これらは本人補足がない限り「ペースの近いラップ群」「速い区間/遅い区間」と中立に記述するに留め、用具やセット名を創作しない。ペース差だけからブロックの意味づけ（例「ここはドリル」「ここはパドル」）を断定するのは禁止。

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

    # v15: レース統合時は部位別の summary/laps をまとめて渡す
    race_parts = activity_data.get("race_parts")
    if race_parts:
        payload["race_parts"] = {
            k: {
                "summary": _trim_summary(v.get("summary", {})),
                "laps": v.get("laps", {}),
            }
            for k, v in race_parts.items()
        }

    laps_digest = summarize_laps(
        activity_data.get("laps", {}),
        activity_data.get("summary", {}),
    )

    # レース時は部位ごとのラップ機械集計も併記
    if race_parts:
        digests = []
        label_map = {"swim": "スイム", "bike": "バイク(Edge)", "run": "ラン", "multi": "マルチ一括"}
        for k, v in race_parts.items():
            d = summarize_laps(v.get("laps", {}), v.get("summary", {}))
            digests.append(f"### {label_map.get(k, k)} のラップ機械集計\n{d}")
        laps_digest = "\n\n".join(digests)
        laps_digest += (
            "\n\n> 📏 **距離の取り扱い（レース統合時の方針）**: "
            "マルチスポーツFITでは各レグの summary 距離が、ラップ化されていない移動や"
            "トランジション境界を巻き込んで実走より大きく出ることがある。"
            "**ラップ合計距離を主の基準**とし、summary 距離は参考値として扱うこと。"
            "両者が食い違う場合（検算 ⚠️ 不一致）は、ラップ合計を採用してペース等を算出し、"
            "本文では『ラップ合計◯km（GPS実測）／summary◯km』のように両値を併記して、"
            "どちらか一方を断定的に唯一の距離としては書かないこと。"
        )

    date_anchor = build_date_anchor(target_date)

    # v15: レース日はレース構成サマリ（機械集計）を最優先で注入
    race_section = ""
    if race_digest:
        race_section = f"""{race_digest}

---

"""

    # v13: 当日朝の睡眠サマリがあれば注入
    condition_section = ""
    if sleep_context:
        condition_section = f"""## 今朝のコンディション（睡眠・参考）

{sleep_context}

> 練習評価に反映すること: 睡眠不良（深い睡眠の大幅不足やHRVのベースライン逸脱）の朝は、
> 同じペースでもHR・主観強度が上振れしやすく、光学心拍も乱れやすい。その場合「不調」と
> 断定せずコンディション要因として言及する。Garminスコア単体での減点解釈は禁止。

---

"""
    
    user_prompt = f"""## 日付・曜日対応表（最優先・厳守）

{date_anchor}

> ⚠️ 提案メニューの曜日表記は、必ず上記の対応表と一致させること。曜日を推定で書かない。

---

{condition_section}## シーズンコンテキスト

{season_context}

---

## 過去のトレーニング履歴

{history_context}

---

## 本日の分析対象

{race_section}以下はアクティビティデータです。スキル定義の「出力形式」テンプレートに完全準拠して分析してください。
**冒頭に「シーズン位置づけ」セクションを必ず追加し、「次の大会まで○日（{{大会名}}）、現在は{{フェーズ}}」を明記してください。**
**「過去1週間との比較」セクションも追加し、相対評価を行ってください。**
**最後の「次回トレーニング提案」では、次の大会までの残り日数に合わせた具体的なメニュー（曜日別）を提示してください。各メニューの曜日は上記「日付・曜日対応表」に厳密に従うこと。**

{laps_digest}

> ⚠️ 上の機械集計と、下の生JSON（laps）が食い違って見えても、**本数・距離・合計は機械集計を正**とすること。生JSONはペースやHR等の詳細を読むために使う。

```json
{json.dumps(payload, ensure_ascii=False, default=str)[:80000]}
```
"""
    
    print(f"  🤖 Analyzing with {model}...")
    anthropic_client = Anthropic(api_key=ANTHROPIC_API_KEY)
    return _generate_complete(
        anthropic_client,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
    )


# Sonnet 4.6 は最大出力 64K トークン対応。1回の上限を引き上げつつ、
# それでも max_tokens で止まった場合は続きを生成してつなぐ。
MAX_TOKENS_PER_CALL = 16000
MAX_CONTINUATIONS = 5  # 安全弁（無限ループ・暴走コスト防止）


def _join_text_blocks(content: list) -> str:
    """レスポンスの全 text ブロックを結合する。
    content[0] が必ず text とは限らず、複数ブロックに分かれる場合もあるため、
    先頭ブロック決め打ちをやめて type=='text' を全て拾う。"""
    return "".join(
        getattr(block, "text", "") for block in content
        if getattr(block, "type", None) == "text"
    )


def _generate_complete(
    client: Anthropic,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
) -> str:
    """stop_reason == 'max_tokens' で打ち切られた場合に継続生成して結合する。
    これが「分析が途中で切れる問題」の根本対策。max_tokens 増量は補助に過ぎない。"""
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]
    full_text = ""

    for attempt in range(MAX_CONTINUATIONS + 1):
        msg = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS_PER_CALL,
            system=system_prompt,
            messages=messages,
        )
        chunk = _join_text_blocks(msg.content)
        full_text += chunk

        if msg.stop_reason != "max_tokens":
            if attempt > 0:
                print(f"  ✅ Completed after {attempt} continuation(s) "
                      f"(stop_reason={msg.stop_reason}, total {len(full_text)} chars)")
            return full_text

        # max_tokens で打ち切られた → 続きを生成させる
        print(f"  ↪️ Hit max_tokens (attempt {attempt + 1}); continuing generation...")
        messages.append({"role": "assistant", "content": chunk})
        messages.append({
            "role": "user",
            "content": "続きを、途中で切れた箇所から自然につなげて出力してください。"
                       "前置きや「続きです」等のメタ発言は不要。本文のみ。",
        })

    print(f"  ⚠️ Reached MAX_CONTINUATIONS={MAX_CONTINUATIONS}; "
          f"output may still be truncated (total {len(full_text)} chars)")
    return full_text


def _coerce_laps_list(laps: Any) -> list[dict]:
    """get_activity_splits の返却から実ラップ配列を取り出す。
    返却形は {'lapDTOs': [...]} が標準だが、まれに list 直、'laps' キー等もあるため吸収。"""
    if isinstance(laps, list):
        return [x for x in laps if isinstance(x, dict)]
    if isinstance(laps, dict):
        for key in ("lapDTOs", "laps", "splits", "splitSummaries"):
            v = laps.get(key)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _lap_distance_m(lap: dict) -> float | None:
    for k in ("distance", "distanceInMeters", "totalDistance"):
        v = lap.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return float(v)
    return None


def summarize_laps(laps: Any, summary: dict) -> str:
    """ラップ配列を *機械的に* 集計し、モデルに渡す検算済みサマリを作る。

    これが構造誤認（300m×5本→4本の数え違い、ペース差でのブロック創作）への
    根本対策。モデルにはこの集計結果を「集計の正」として渡し、本数・距離・合計を
    モデルに目視で数え直させない。
    """
    rows = _coerce_laps_list(laps)
    if not rows:
        return "（ラップ詳細データなし。summaryの距離・時間のみで分析すること）"

    # 距離を 25m 単位に丸めて「同一距離のラップが連続する塊」を検出
    def rounded(m: float | None) -> int | None:
        if m is None:
            return None
        return int(round(m / 25.0) * 25)

    seq: list[int | None] = [rounded(_lap_distance_m(l)) for l in rows]

    # 連続する同一距離をグループ化（例: [350], [100]*12, [300]*5, [25]）
    groups: list[tuple[int | None, int]] = []
    for d in seq:
        if groups and groups[-1][0] == d:
            groups[-1] = (d, groups[-1][1] + 1)
        else:
            groups.append((d, 1))

    total_from_laps = sum((_lap_distance_m(l) or 0.0) for l in rows)
    summary_dist = summary.get("distance") or summary.get("distanceInMeters")

    lines = [
        "### ラップ機械集計（このパイプラインが配列から算出。**本数・距離・合計はこの値を正とすること**）",
        "",
        f"- 総ラップ数: {len(rows)}",
        "- 距離別の連続ブロック（ラップ順）:",
    ]
    lap_cursor = 1
    for dist, count in groups:
        start, end = lap_cursor, lap_cursor + count - 1
        rng = f"Lap{start}" if count == 1 else f"Lap{start}〜{end}"
        if dist is None:
            lines.append(f"  - {rng}: 距離不明 × {count}本")
        else:
            lines.append(f"  - {rng}: {dist}m × {count}本（計 {dist * count}m）")
        lap_cursor += count

    lines.append("")
    lines.append(f"- ラップ合計距離: {int(total_from_laps)}m")
    if summary_dist:
        diff = total_from_laps - float(summary_dist)
        ok = "✅ 一致" if abs(diff) <= 30 else f"⚠️ 不一致（差 {int(diff)}m）"
        lines.append(f"- summary距離: {int(float(summary_dist))}m → 検算 {ok}")

    lines.append("")
    lines.append(
        "> 注意: 上の本数・距離は配列から機械集計した確定値。分析本文で本数を"
        "数え直したり、距離を推定で書き換えたりしないこと。"
    )
    return "\n".join(lines)


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


def build_properties(
    summary: dict,
    schema: dict[str, str],
    archive: dict | None = None,
    activity_id: Any = None,
) -> dict[str, Any]:
    """Notionスキーマに基づいてプロパティ辞書を構築。キー名揺れに対応。

    archive / activity_id を渡すと、ローデータ参照キー
    （activity_id / raw_url / sport）も *スキーマに存在する場合のみ* 付与する。
    Notion 側に該当プロパティが無い環境でもページ作成が落ちないよう、
    既存の schema チェック機構を必ず通す。"""
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

    # --- ローデータ参照キー（後日セッションで再取得なしに分析を再開するため） ---
    # 曖昧一致（_find_property_name の部分一致フォールバック）で
    # "url"/"id" 等の既存プロパティに誤爆しないよう、ここは完全一致のみ許可する。
    def _exact_in_schema(name: str) -> bool:
        return any(n == name for n in schema)

    aid = activity_id if activity_id is not None else _get_nested(summary, "activityId")
    if aid is not None and _exact_in_schema("activity_id"):
        desired["activity_id"] = ("rich_text", str(aid))
    if archive:
        urls = archive.get("urls", {})
        primary = urls.get("lengths") or urls.get("series_5s") or urls.get("laps")
        if primary and _exact_in_schema("raw_url"):
            desired["raw_url"] = ("url", primary)
        if archive.get("sport") and _exact_in_schema("sport"):
            desired["sport"] = ("select", archive["sport"])

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
            elif expected_type == "url":
                properties[actual_name] = {"url": str(value)}
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
def _raw_data_toggle(archive: dict) -> dict | None:
    """本文末尾に付ける「📦 ローデータ」トグル（全URLの一覧）。"""
    urls = archive.get("urls") or {}
    if not urls:
        return None
    return {
        "object": "block", "type": "toggle",
        "toggle": {
            "rich_text": [{"type": "text", "text": {"content": "📦 ローデータ"}}],
            "children": [
                {"object": "block", "type": "bulleted_list_item",
                 "bulleted_list_item": {"rich_text": [
                     {"type": "text",
                      "text": {"content": f"{k}: {u}"[:2000], "link": {"url": u}}}
                 ]}}
                for k, u in urls.items()
            ],
        },
    }


def create_notion_page(
    notion: NotionClient,
    schema: dict[str, str],
    summary: dict,
    analysis_md: str,
    archive: dict | None = None,
    activity_id: Any = None,
) -> None:
    properties = build_properties(summary, schema, archive=archive, activity_id=activity_id)

    children = md_to_notion_blocks(analysis_md)

    # ローデータのURL一覧を本文末尾に添付（失敗しても投稿は続行）
    if archive:
        try:
            toggle = _raw_data_toggle(archive)
            if toggle:
                children.append(toggle)
        except Exception as e:
            print(f"  ⚠️ raw toggle 構築失敗（続行）: {e}")

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
def process_race_day(
    client: Garmin,
    activities: list[dict],
    notion: NotionClient,
    schema: dict,
    season_context: str,
    history_context: str,
    target_date: date_cls,
    sleep_context: str,
    brick_keys: dict[str, str] | None = None,
) -> set[int]:
    """レース日: 同日アクティビティを部位別に採用・統合し、1ページで分析する。

    返り値: 分析済みとしてマークすべき activityId の集合（採用・破棄の両方を含む）。
    破棄したバイク等も再分析されないよう ID をマークする。
    """
    components = race_merge.select_race_components(activities)
    race_digest = race_merge.build_race_digest(components)
    print(f"\n🏁 レース日として統合処理:\n{race_digest}")

    # 各部位の詳細（summary + laps）を取得して1つのdetailに束ねる
    brick_keys = brick_keys or {}
    parts: dict[str, dict] = {}
    consumed_ids: set[int] = set()
    archives: dict[str, dict] = {}
    for key in ("multi", "swim", "bike", "run"):
        act = components.get(key)
        if not act:
            continue
        aid = act.get("activityId")
        detail = fetch_activity_detail(client, aid)
        merged = dict(act)
        if detail.get("summary"):
            for k, v in detail["summary"].items():
                if v is not None and v != "":
                    merged[k] = v
        parts[key] = {"summary": merged, "laps": detail.get("laps", {})}

        # レース日こそ原本を残したい。失敗しても統合分析は止めない。
        arc = archive_raw(
            client, aid, _act_date(merged),
            brick_key=brick_keys.get(str(aid)),
        )
        if arc:
            archives[key] = arc

        consumed_ids.add(aid)
        time.sleep(1)

    # 破棄したバイク等も「分析済み」にして重複分析を防ぐ。
    # ただし破棄バイク（Edge/965の重複）は原本だけは保存しておく。
    for d in components.get("bike_dropped", []):
        if d.get("activityId"):
            consumed_ids.add(d["activityId"])
            archive_raw(
                client, d["activityId"], _act_date(d),
                brick_key=brick_keys.get(str(d["activityId"])),
            )

    # 統合 detail: 部位ごとの summary/laps を race_parts として渡す。
    # 代表 summary はランがあればラン（最終局面・総合評価の軸）、無ければ最初の部位。
    rep_key = "run" if "run" in parts else next(iter(parts))
    combined_detail = {
        "summary": parts[rep_key]["summary"],
        "laps": parts[rep_key].get("laps", {}),
        "race_parts": {k: v for k, v in parts.items()},
    }

    analysis = analyze_with_claude(
        combined_detail, season_context, history_context, target_date,
        sleep_context, race_digest=race_digest,
    )
    # ページ作成用の代表 summary は「総合」を表現したいので、
    # multi があればそれ、無ければランの summary を使う（種目=トライアスロン表記用）。
    page_summary = parts.get("multi", {}).get("summary") or parts[rep_key]["summary"]

    # ローデータは部位分をひとつにまとめて添付（urls キーに部位名を前置）
    race_archive: dict | None = None
    if archives:
        merged_urls: dict[str, str] = {}
        for part_key, arc in archives.items():
            for k, u in (arc.get("urls") or {}).items():
                merged_urls[f"{part_key}_{k}"] = u
        primary_key = next(iter(archives))
        race_archive = {
            "urls": merged_urls,
            "sport": "race",
            "stem": archives[primary_key].get("stem"),
        }

    create_notion_page(
        notion, schema, page_summary, analysis,
        archive=race_archive,
        activity_id=page_summary.get("activityId"),
    )
    print(f"✅ レース統合ページを作成（採用/破棄 {len(consumed_ids)}件をマーク）")
    return consumed_ids


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
    
    # v13: 当日朝の睡眠サマリ（sleep_report.py が生成した sleep_state.json から）
    sleep_context = load_sleep_context(target_date)
    if sleep_context:
        print(f"😴 Sleep context: {sleep_context}")
    
    new_count = 0

    # v15: レース日判定。同日にS/B/R揃い or バイク二重記録 or マルチスポーツ記録があれば
    # 部位別に正データを採用して1ページに統合分析する。
    unanalyzed = [a for a in activities if a.get("activityId") not in analyzed_ids]

    # 同日ブリック（例: バイク→ラン）に共通キーを振り、ローデータのファイル名で紐付ける
    brick_keys = build_brick_keys(unanalyzed)

    if unanalyzed and race_merge.detect_race_day(unanalyzed):
        try:
            consumed = process_race_day(
                client, unanalyzed, notion, schema,
                season_context, history_context, target_date, sleep_context,
                brick_keys=brick_keys,
            )
            analyzed_ids |= consumed
            new_count += 1
        except Exception as e:
            print(f"❌ レース統合処理エラー: {e}", file=sys.stderr)
            traceback.print_exc()
            # 失敗時は従来の個別処理にフォールバックさせる（下のループへ）

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

            # --- ローデータ保存（Claude に投げる前 / 失敗しても分析は止めない） ---
            archive = archive_raw(
                client, activity_id, _act_date(merged_summary),
                brick_key=brick_keys.get(str(activity_id)),
            )

            analysis = analyze_with_claude(detail, season_context, history_context, target_date, sleep_context)
            create_notion_page(
                notion, schema, detail["summary"], analysis,
                archive=archive, activity_id=activity_id,
            )

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

    # ローデータの索引を作り直す（後日セッションから唯一辿れる入口）。失敗しても止めない。
    try:
        p = rebuild_index()
        if p:
            print(f"🗂️ raw index updated: {p}")
    except Exception as e:
        print(f"[raw] WARN index 更新失敗: {e}")
    
    print(f"\n✅ Done. Analyzed {new_count} new activities.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
