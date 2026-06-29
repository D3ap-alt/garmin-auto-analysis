#!/usr/bin/env python3
"""Garmin アクティビティ ID から元データ(FIT)をダウンロードして展開する。

レースFIT手動投入(v15)の Actions 実行用。FITをリポジトリにコミットせず、
実行のたびに Garmin から取得することで公開リポジトリへの生データ流出を防ぐ。

使い方:
    python scripts/fetch_garmin_fit.py <out_dir> <activity_id> [<activity_id> ...]

各 ID につき <out_dir>/<id>.fit を作成する。
env: run_analysis と同じ（GARMIN_TOKENS_BASE64 もしくは GARMIN_EMAIL/PASSWORD）。
"""
from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import run_analysis as ra  # noqa: E402  (garmin_login を再利用)
from garminconnect import Garmin  # noqa: E402


def _extract_fit(raw: bytes, activity_id: str) -> bytes:
    """ORIGINAL ダウンロード結果(通常zip)から .fit バイト列を取り出す。"""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            fits = [n for n in z.namelist() if n.lower().endswith(".fit")]
            if not fits:
                raise SystemExit(
                    f"❌ {activity_id}: ダウンロードzip内に .fit がありません: {z.namelist()}"
                )
            return z.read(fits[0])
    except zipfile.BadZipFile:
        # zip でなく FIT そのものが返るケースのフォールバック
        return raw


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: fetch_garmin_fit.py <out_dir> <activity_id> [...]", file=sys.stderr)
        return 2

    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)
    activity_ids = sys.argv[2:]

    client = ra.garmin_login()

    for aid in activity_ids:
        raw = client.download_activity(
            aid, dl_fmt=Garmin.ActivityDownloadFormat.ORIGINAL
        )
        fit = _extract_fit(raw, aid)
        dest = out_dir / f"{aid}.fit"
        dest.write_bytes(fit)
        print(f"✅ {aid} -> {dest} ({len(fit):,} bytes)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
