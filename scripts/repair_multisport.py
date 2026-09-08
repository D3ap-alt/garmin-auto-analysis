"""
repair_multisport.py — 保存済みローデータの「マルチスポーツ取りこぼし」を修復する

背景:
  raw_archive.py は当初、1本の FIT から最初の session だけを見ていた。
  フォアランナーのマルチスポーツ（スイム→T1→バイク→T2→ラン）で保存した日は
    - 種目が先頭種目（OWS）だけになる
    - laps に全種目のラップが混ざる
    - series_5s に全種目の時系列が混ざる
    - バイク／ランのレグが index.json から丸ごと消える
  という壊れ方をしていた。

やること（Garmin へは一切アクセスしない。保存済み FIT の読み直しだけ）:
  1. data/raw/fit/*.fit を全走査し、非トランジションの session が2つ以上の FIT を検出
  2. その FIT を種目レグに分割し、レグ単位の meta/laps/lengths/series_5s を書き出す
  3. 旧・親stemの meta/laps/lengths/series_5s（中身が混ざっている）を削除
     ※ FIT 原本と *_multisport.json は残す
  4. index.json を作り直す（Edge 840 との二重記録には dup_group が付く）

使い方:
  python scripts/repair_multisport.py --dry-run   # 何もせず対象と内訳だけ表示
  python scripts/repair_multisport.py             # 実行
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from raw_archive import (  # noqa: E402
    FIT_DIR,
    JSON_DIR,
    _emit_entry,
    parse_fit,
    rebuild_index,
    real_sessions,
    split_legs,
    write_json,
)

STALE_SUFFIXES = ("meta", "laps", "lengths", "series_5s")


def load_parent_meta(stem: str) -> dict:
    p = JSON_DIR / f"{stem}_meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def parse_stem(stem: str) -> tuple[str, str | None, str]:
    """'2026-07-12_brick01_23565270848' → (date, brick_key, activity_id)"""
    parts = stem.split("_")
    date = parts[0]
    activity_id = parts[-1]
    brick = parts[1] if len(parts) >= 3 else None
    return date, brick, activity_id


def find_targets() -> list[tuple[Path, list]]:
    targets = []
    for fit_path in sorted(FIT_DIR.glob("*.fit")):
        stem = fit_path.stem
        # 既に修復済み（レグの meta がある）ならスキップ
        if list(JSON_DIR.glob(f"{stem}_l*_meta.json")):
            continue
        try:
            parsed = parse_fit(fit_path.read_bytes())
        except Exception as e:
            print(f"  ! {stem}: 解析失敗 {type(e).__name__}: {e}")
            continue
        if len(real_sessions(parsed)) > 1:
            targets.append((fit_path, parsed))
    return targets


def repair(fit_path: Path, parsed: dict, dry_run: bool) -> list[dict]:
    stem = fit_path.stem
    date, brick, activity_id = parse_stem(stem)
    old = load_parent_meta(stem)

    base_meta = {
        "activity_id": old.get("activity_id") or activity_id,
        "date": old.get("date") or date,
        "brick_key": old.get("brick_key", brick),
        "fit_stem": stem,
    }

    legs = split_legs(parsed)
    out = []
    for leg in legs:
        leg_stem = f"{stem}_l{leg['leg']}_{leg['sport']}"
        sess = leg["session"]
        info = {
            "leg": leg["leg"],
            "sport": leg["sport"],
            "stem": leg_stem,
            "distance_m": sess.get("total_distance"),
            "duration_s": sess.get("total_timer_time"),
            "laps": len(leg["laps"]),
            "lengths": len(leg["lengths"]),
            "records": len(leg["records"]),
            "transition_before_s": leg["transition_before_s"],
        }
        out.append(info)
        if dry_run:
            continue
        _emit_entry(
            leg_stem, leg["sport"], sess,
            leg["laps"], leg["lengths"], leg["records"],
            dict(
                base_meta,
                multisport=True,
                parent_stem=stem,
                leg=leg["leg"],
                leg_count=len(legs),
                transition_before_s=leg["transition_before_s"],
                repaired_by="repair_multisport.py",
            ),
        )

    if not dry_run:
        write_json(
            dict(base_meta, leg_count=len(legs), legs=[
                {k: v for k, v in i.items() if k != "records"} for i in out
            ]),
            stem, "multisport",
        )
        # 中身が混ざっている旧ファイルを撤去（FIT原本は残す）
        for suf in STALE_SUFFIXES:
            p = JSON_DIR / f"{stem}_{suf}.json"
            if p.exists():
                p.unlink()

    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="書き込まず対象だけ表示")
    args = ap.parse_args()

    if not FIT_DIR.exists():
        print("data/raw/fit が無い。何もしない。")
        return 0

    print("🔎 マルチスポーツFITを走査中...")
    targets = find_targets()
    if not targets:
        print("✅ 修復対象なし（すべて単一種目、または修復済み）")
        if not args.dry_run:
            p = rebuild_index()
            if p:
                idx = json.loads(p.read_text(encoding="utf-8"))
                print(f"🗂️ index: {idx['count']}件 / 重複グループ {len(idx.get('duplicate_groups', []))}")
        return 0

    print(f"📦 対象 {len(targets)}件" + ("（dry-run）" if args.dry_run else ""))
    total_legs = 0
    for fit_path, parsed in targets:
        print(f"\n▼ {fit_path.stem}")
        legs = repair(fit_path, parsed, args.dry_run)
        total_legs += len(legs)
        for i in legs:
            d = (i["distance_m"] or 0) / 1000
            t = (i["duration_s"] or 0) / 60
            print(
                f"   leg{i['leg']} {i['sport']:5s} {d:7.2f}km {t:6.1f}min "
                f"laps={i['laps']:3d} lengths={i['lengths']:3d} rec={i['records']:5d} "
                f"→ {i['stem']}"
            )

    print(f"\n合計レグ数: {total_legs}")

    if args.dry_run:
        print("\n(dry-run のため書き込みなし)")
        return 0

    p = rebuild_index()
    if p:
        idx = json.loads(p.read_text(encoding="utf-8"))
        print(f"\n🗂️ index 再構築: {idx['count']}件 / {len(idx['dates'])}日分")
        dups = idx.get("duplicate_groups", [])
        if dups:
            print(f"⚠️ 二重記録グループ {len(dups)}件: {', '.join(dups)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
