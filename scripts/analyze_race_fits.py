#!/usr/bin/env python3
"""レースFIT手動投入 → 統合分析 → Notion投稿 (v15)

レース日は手元に FIT がある（965マルチ + Edge840バイク）。
それらを渡すと、部位別に正データを採用して統合分析し、Notionに1ページ作成する。

使い方:
    python scripts/analyze_race_fits.py \
        --multisport path/to/965_multisport.fit \
        --edge-bike  path/to/edge840_bike.fit \
        --date 2026-06-28 \
        --race 諏訪子

  --multisport : 965 のマルチスポーツFIT（S→B→R一括）。必須。
  --edge-bike  : Edge840 のバイク単独FIT。任意（無ければ965バイクをそのまま使用）。
  --date       : レース日 YYYY-MM-DD。省略時はFITのバイク開始日。
  --race       : レース名（任意・ログ表示用）。
  --dry-run    : Notionに投稿せず、生成した分析MarkdownとダイジェストをstdoutへMix。

env: GARMIN_* は不要。ANTHROPIC_API_KEY / NOTION_API_KEY / NOTION_DATABASE_ID は必要。
     --dry-run 時は NOTION_* も不要。
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, date as date_cls
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fit_loader
import race_merge


def _infer_date(parts: dict) -> date_cls:
    bike = parts.get("bike") or parts.get("swim") or parts.get("run")
    if bike:
        s = bike["summary"].get("startTimeGMT", "")
        try:
            return datetime.strptime(s[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return datetime.now().date()


def build_components(multisport_fit: str, edge_fit: str | None) -> dict:
    parts = fit_loader.split_multisport(fit_loader.load_fit(multisport_fit))
    edge_bike = None
    if edge_fit:
        edge_loaded = fit_loader.load_fit(edge_fit)
        # Edge は単一バイクセッション想定。cycling を拾う。
        edge_bike = next(
            (r for r in edge_loaded if r["summary"].get("_sport") == "cycling"),
            edge_loaded[0] if edge_loaded else None,
        )
    return parts, race_merge.assemble_race_from_fits(parts, edge_bike=edge_bike)


def components_to_detail(components: dict) -> dict:
    """race_merge コンポーネント → analyze_with_claude が読む detail に変換。

    run_analysis.process_race_day と同じ race_parts 構造を作る。
    代表 summary はラン（総合評価の軸）。
    """
    race_parts = {}
    for key in ("swim", "bike", "run"):
        rec = components.get(key)
        if rec:
            race_parts[key] = {"summary": rec["summary"], "laps": rec.get("laps", [])}

    rep = race_parts.get("run") or race_parts.get("bike") or next(iter(race_parts.values()))
    return {
        "summary": rep["summary"],
        "laps": rep.get("laps", []),
        "race_parts": race_parts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="レースFIT統合分析")
    ap.add_argument("--multisport", required=True, help="965マルチスポーツFIT")
    ap.add_argument("--edge-bike", default=None, help="Edge840バイクFIT（任意）")
    ap.add_argument("--date", default=None, help="レース日 YYYY-MM-DD")
    ap.add_argument("--race", default="", help="レース名（任意）")
    ap.add_argument("--dry-run", action="store_true", help="Notion投稿せず標準出力")
    args = ap.parse_args()

    parts, components = build_components(args.multisport, args.edge_bike)
    digest = race_merge.build_race_digest(components)
    print(f"\n🏁 レース統合: {args.race or '(無名)'}\n", file=sys.stderr)
    print(digest, file=sys.stderr)

    target_date = (
        datetime.strptime(args.date, "%Y-%m-%d").date() if args.date else _infer_date(parts)
    )

    # run_analysis を import（dry-runでも analyze は実行するので ANTHROPIC は必要）
    import run_analysis as ra

    detail = components_to_detail(components)
    season = ra.get_season_context(target_date)

    if args.dry_run:
        history = "（dry-run: 履歴取得スキップ）"
        sleep = ""
    else:
        from notion_client import Client as NotionClient
        notion = NotionClient(auth=ra.NOTION_API_KEY, notion_version="2022-06-28")
        schema = ra.fetch_notion_schema(notion)
        history = ra.fetch_recent_history(notion, target_date, days=7)
        sleep = ra.load_sleep_context(target_date)

    analysis = ra.analyze_with_claude(
        detail, season, history if not args.dry_run else "（dry-run）",
        target_date, sleep, race_digest=digest,
    )

    if args.dry_run:
        print("\n" + "=" * 60)
        print("生成された分析 Markdown（dry-run・Notion未投稿）")
        print("=" * 60)
        print(analysis)
        return 0

    # ページタイトルはレース名を優先（無指定時は元アクティビティ名のまま）
    page_summary = dict((components.get("run") or components.get("bike"))["summary"])
    if args.race:
        page_summary["activityName"] = f"🏁 {args.race}"
    ra.create_notion_page(notion, schema, page_summary, analysis)
    print(f"\n✅ Notionにレース統合ページを作成しました（{target_date} {args.race}）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
