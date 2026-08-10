"""
backfill_raw.py — ローデータ（FIT原本 + 抽出JSON）の遡り取り込み / 単発テスト

run_analysis.py の分析・Notion書き込みは一切通さず、raw_archive.archive_activity
だけを回す。既に分析済み（state.json 登録済み）のアクティビティでも、
Notionページを重複させずにローデータだけ後から揃えられる。

使い方（環境変数。GitHub Actions の workflow_dispatch から渡す想定）:

  BACKFILL_START=2026-08-01 BACKFILL_END=2026-08-10   期間を Garmin から取得して一括
  BACKFILL_IDS=23923679015,23905637075                ID直指定（カンマ区切り）
  BACKFILL_LIMIT=30            1回の実行で処理する上限（既定30 / 0で無制限）
  BACKFILL_SLEEP=3             1件ごとの待機秒（既定3。Garminは連続DLに厳しい）
  BACKFILL_FORCE=1             保存済みでも再取得する

BACKFILL_IDS が指定されていればそちらを優先。どちらも無ければ「今日」を対象にする。

ローカル実行も可能:
  python scripts/backfill_raw.py            # 今日の分
  BACKFILL_START=2026-07-01 BACKFILL_END=2026-07-31 python scripts/backfill_raw.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime
from pathlib import Path

# scripts/ を import 可能にする（リポジトリルートから実行される前提）
sys.path.insert(0, str(Path(__file__).parent))

# garmin_login() / build_brick_keys() / _act_date() を再利用する。
# run_analysis は import 時に GARMIN_* / ANTHROPIC_API_KEY / NOTION_* を要求するため、
# ワークフロー側で同じ secrets を渡すこと（分析自体は実行しない）。
import run_analysis as RA
from raw_archive import FIT_DIR, archive_activity, rebuild_index

SLEEP_SEC = float(os.environ.get("BACKFILL_SLEEP", "3"))
LIMIT = int(os.environ.get("BACKFILL_LIMIT", "30"))
FORCE = os.environ.get("BACKFILL_FORCE", "").strip() not in ("", "0", "false", "False")


def already_archived(activity_id) -> bool:
    """data/raw/fit/ に該当IDのFITが既にあるか（ブリックキー有無の両方を見る）。"""
    return any(FIT_DIR.glob(f"*_{activity_id}.fit"))


# 日付がぶら下がりうるネストキー。get_activities_by_date はトップレベルに持つが、
# get_activity（詳細API）は summaryDTO の中に入れて返す。
_DATE_NESTS = ("summaryDTO", "activitySummary", "summary", "metadataDTO", "activity")


def extract_date(act: dict, _depth: int = 0) -> str:
    """アクティビティ dict から 'YYYY-MM-DD' を掘り出す。ネストと epoch にも対応。"""
    if not isinstance(act, dict):
        return ""

    d = RA._act_date(act)
    if d:
        return d

    # startTimeGMT がミリ秒 epoch で来るケース
    for key in ("beginTimestamp", "startTimeInMillis", "beginTimestampLocal"):
        v = act.get(key)
        if isinstance(v, (int, float)) and v > 0:
            ts = v / 1000 if v > 1e11 else v
            try:
                return datetime.fromtimestamp(ts, RA.JST).date().isoformat()
            except Exception:
                pass

    if _depth >= 2:
        return ""
    for key in _DATE_NESTS:
        sub = act.get(key)
        if isinstance(sub, dict):
            d = extract_date(sub, _depth + 1)
            if d:
                return d
    return ""


def normalize(act: dict, fallback_id=None) -> dict:
    """後段（build_brick_keys / archive）が読める形に整える。
    startTimeLocal をトップレベルへ引き上げ、activityId を確定させる。"""
    out = dict(act)
    if fallback_id is not None:
        out.setdefault("activityId", fallback_id)
    if not out.get("activityId"):
        for key in _DATE_NESTS:
            sub = act.get(key)
            if isinstance(sub, dict) and sub.get("activityId"):
                out["activityId"] = sub["activityId"]
                break

    date_str = extract_date(act)
    if date_str and not RA._act_date(out):
        # build_brick_keys は startTimeLocal を datetime として読むので時刻も探して補う
        t = ""
        for src in (act, *(act.get(k) for k in _DATE_NESTS if isinstance(act.get(k), dict))):
            if isinstance(src, dict):
                t = src.get("startTimeLocal") or src.get("startTimeGMT") or ""
                if t:
                    break
        out["startTimeLocal"] = str(t) if t else f"{date_str} 00:00:00"
    return out


def collect_targets(client) -> list[dict]:
    """処理対象のアクティビティ（activityId / startTimeLocal を含む dict）を集める。"""
    ids_raw = os.environ.get("BACKFILL_IDS", "").strip()
    if ids_raw:
        targets = []
        for token in ids_raw.replace("\n", ",").split(","):
            aid = token.strip()
            if not aid:
                continue
            try:
                # 日付を知るために summary だけ引く（詳細APIは summaryDTO にネストして返す）
                act = normalize(client.get_activity(aid), fallback_id=aid)
                if not extract_date(act):
                    print(f"⚠️ id={aid} 日付を特定できません。応答のキー: "
                          f"{sorted(k for k in act)[:20]}")
                targets.append(act)
            except Exception as e:
                print(f"⚠️ id={aid} の summary 取得失敗、スキップ: {e}")
            time.sleep(1)
        print(f"🎯 ID指定モード: {len(targets)}件")
        return targets

    start = os.environ.get("BACKFILL_START", "").strip()
    end = os.environ.get("BACKFILL_END", "").strip()
    if not start:
        start = end = RA.resolve_target_date().isoformat()
    if not end:
        end = start
    print(f"🎯 期間モード: {start} 〜 {end}")

    activities = client.get_activities_by_date(start, end)
    print(f"📊 Garmin から {len(activities)}件")
    return [normalize(a) for a in activities]


def main() -> int:
    try:
        client = RA.garmin_login()
    except Exception as e:
        print(f"❌ Garmin login failed: {e}", file=sys.stderr)
        return 1

    targets = collect_targets(client)
    if not targets:
        print("ℹ️ 対象なし。終了。")
        return 0

    # 同日・60分以内の連続セッションに共通キーを振る（本番フローと同じ規則）
    brick_keys = RA.build_brick_keys(targets)

    pending = []
    skipped = 0
    for act in targets:
        aid = act.get("activityId")
        if aid is None:
            continue
        if not FORCE and already_archived(aid):
            skipped += 1
            continue
        pending.append(act)

    if skipped:
        print(f"⏭️  保存済みのためスキップ: {skipped}件（再取得するなら BACKFILL_FORCE=1）")

    truncated = 0
    if LIMIT > 0 and len(pending) > LIMIT:
        truncated = len(pending) - LIMIT
        pending = pending[:LIMIT]

    print(f"📦 今回処理: {len(pending)}件（1件ごとに {SLEEP_SEC}秒待機）")

    ok = 0
    failed: list[tuple] = []
    for i, act in enumerate(pending, 1):
        aid = act["activityId"]
        date_str = extract_date(act)
        if not date_str:
            print(f"  [{i}/{len(pending)}] id={aid} 日付不明のためスキップ")
            failed.append((aid, "日付不明"))
            continue
        try:
            arc = archive_activity(
                client, aid, date_str,
                brick_key=brick_keys.get(str(aid)),
            )
            print(f"  [{i}/{len(pending)}] ✅ {arc['stem']} sport={arc['sport']} "
                  f"({', '.join(k for k in arc['files'] if k != 'fit')})")
            ok += 1
        except Exception as e:
            print(f"  [{i}/{len(pending)}] ❌ id={aid} {type(e).__name__}: {e}")
            failed.append((aid, str(e)))
        time.sleep(SLEEP_SEC)

    try:
        p = rebuild_index()
        if p:
            idx = __import__("json").loads(p.read_text(encoding="utf-8"))
            print(f"🗂️ raw index updated: {idx['count']}件 / {len(idx['dates'])}日分")
    except Exception as e:
        print(f"[raw] WARN index 更新失敗: {e}")

    print(f"\n✅ 完了: 成功 {ok}件 / 失敗 {len(failed)}件 / スキップ {skipped}件")
    if failed:
        print("失敗一覧:")
        for aid, msg in failed:
            print(f"  - {aid}: {msg}")
    if truncated:
        print(f"⚠️ BACKFILL_LIMIT={LIMIT} により {truncated}件を今回は処理していません。"
              f"同じ条件でもう一度実行すれば続きから進みます（保存済みは自動スキップ）。")

    # 失敗があっても 0 を返す（部分的に保存できたぶんはコミットさせたい）
    return 0


if __name__ == "__main__":
    sys.exit(main())
