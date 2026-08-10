"""
raw_archive.py  —  Garmin ローデータ保存レイヤ

役割:
  1. アクティビティの FIT ファイル原本を data/raw/fit/ に保存（完全復元用）
  2. 競技別に「即読み用 JSON」を data/raw/json/ に抽出
       run   : laps
       bike  : laps + 5秒平均時系列（NP/IF/VI 再計算可能）
       swim  : laps + lengths（パドル/ハイポ/ドリルの構造復元用）
       ows   : laps + 5秒平均時系列
  3. Notion に貼る raw_url を返す

依存: fitdecode, garminconnect
"""

from __future__ import annotations

import io
import json
import math
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import fitdecode

# ---------------------------------------------------------------- 設定

REPO_OWNER = "D3ap-alt"
REPO_NAME = "garmin-auto-analysis"
REPO_BRANCH = "main"

RAW_ROOT = Path("data/raw")
FIT_DIR = RAW_ROOT / "fit"
JSON_DIR = RAW_ROOT / "json"

DOWNSAMPLE_SEC = 5          # バイク/OWS の時系列間引き幅
BRICK_GAP_MIN = 60          # 同日ブリック判定の最大間隔（分）

RAW_BASE_URL = (
    f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{REPO_BRANCH}/"
)

# 5秒平均を取る対象フィールド（数値のみ）
RECORD_NUMERIC_FIELDS = [
    "power",
    "heart_rate",
    "cadence",
    "speed",
    "altitude",
    "temperature",
    "grade",
    "left_right_balance",
]

# ラップから拾うフィールド（種目共通 + 種目固有をまとめて試行）
LAP_FIELDS = [
    "message_index",
    "start_time",
    "total_elapsed_time",
    "total_timer_time",
    "total_distance",
    "avg_speed",
    "max_speed",
    "avg_heart_rate",
    "max_heart_rate",
    "avg_power",
    "max_power",
    "normalized_power",
    "avg_cadence",
    "max_cadence",
    "total_ascent",
    "total_descent",
    "avg_temperature",
    "total_calories",
    "avg_stance_time",              # GCT
    "avg_vertical_ratio",           # VR
    "avg_vertical_oscillation",
    "avg_stance_time_balance",
    "avg_step_length",
    "total_strokes",
    "avg_swolf",
    "num_active_lengths",
    "swim_stroke",
    "intensity",
    "lap_trigger",
]

LENGTH_FIELDS = [
    "message_index",
    "start_time",
    "total_elapsed_time",
    "total_timer_time",
    "total_strokes",
    "avg_speed",
    "swim_stroke",
    "length_type",
    "avg_swimming_cadence",
    "event",
]

SESSION_FIELDS = [
    "start_time",
    "sport",
    "sub_sport",
    "total_elapsed_time",
    "total_timer_time",
    "total_distance",
    "total_calories",
    "avg_speed",
    "max_speed",
    "avg_heart_rate",
    "max_heart_rate",
    "avg_power",
    "max_power",
    "normalized_power",
    "training_stress_score",
    "intensity_factor",
    "threshold_power",
    "avg_cadence",
    "total_ascent",
    "total_descent",
    "avg_temperature",
    "max_temperature",
    "pool_length",
    "num_lengths",
    "avg_stance_time",
    "avg_vertical_ratio",
]


# ---------------------------------------------------------------- 汎用

def _jsonable(v: Any) -> Any:
    """FIT の値を JSON 化できる形へ。"""
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.isoformat()
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (int, float, str, bool)) or v is None:
        return v
    return str(v)


def _pick(frame: "fitdecode.FitDataMessage", fields: Iterable[str]) -> dict:
    out = {}
    for f in fields:
        try:
            val = frame.get_value(f, fallback=None)
        except Exception:
            val = None
        if val is not None:
            out[f] = _jsonable(val)
    return out


def _mean(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    if not xs:
        return None
    return round(sum(xs) / len(xs), 2)


# ---------------------------------------------------------------- FIT取得

def download_fit(client, activity_id: int | str) -> bytes:
    """
    Garmin から FIT 原本を取得。
    ORIGINAL 形式は ZIP で返るため、中の .fit を取り出して返す。
    """
    blob = client.download_activity(
        activity_id,
        dl_fmt=client.ActivityDownloadFormat.ORIGINAL,
    )
    if blob[:2] == b"PK":  # ZIP
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.lower().endswith(".fit")]
            if not names:
                raise ValueError(f"ZIP内に .fit が見つからない: {zf.namelist()}")
            return zf.read(names[0])
    return blob  # 既に生FITの場合


def save_fit(fit_bytes: bytes, stem: str) -> Path:
    FIT_DIR.mkdir(parents=True, exist_ok=True)
    path = FIT_DIR / f"{stem}.fit"
    path.write_bytes(fit_bytes)
    return path


# ---------------------------------------------------------------- FIT解析

def parse_fit(fit_bytes: bytes) -> dict:
    """FIT を session / laps / lengths / records に分解する。"""
    session: dict = {}
    laps: list[dict] = []
    lengths: list[dict] = []
    records: list[dict] = []

    with fitdecode.FitReader(io.BytesIO(fit_bytes)) as fr:
        for frame in fr:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue

            if frame.name == "session" and not session:
                session = _pick(frame, SESSION_FIELDS)

            elif frame.name == "lap":
                laps.append(_pick(frame, LAP_FIELDS))

            elif frame.name == "length":
                lengths.append(_pick(frame, LENGTH_FIELDS))

            elif frame.name == "record":
                ts = frame.get_value("timestamp", fallback=None)
                if ts is None:
                    continue
                row = {"timestamp": _jsonable(ts)}
                for f in RECORD_NUMERIC_FIELDS:
                    try:
                        row[f] = frame.get_value(f, fallback=None)
                    except Exception:
                        row[f] = None
                row["_epoch"] = ts.timestamp()
                records.append(row)

    return {
        "session": session,
        "laps": laps,
        "lengths": lengths,
        "records": records,
    }


def detect_sport(session: dict) -> str:
    """run / bike / swim / ows / other を返す。"""
    sport = str(session.get("sport", "")).lower()
    sub = str(session.get("sub_sport", "")).lower()

    if "cycl" in sport or "bik" in sport:
        return "bike"
    if "swim" in sport:
        return "ows" if "open_water" in sub else "swim"
    if "run" in sport:
        return "run"
    return "other"


def downsample(records: list[dict], step: int = DOWNSAMPLE_SEC) -> list[dict]:
    """
    N秒バケットで平均化。
    NP は 30秒移動平均の4乗平均なので、5秒粒度があれば十分に再計算できる。
    """
    if not records:
        return []

    t0 = records[0]["_epoch"]
    buckets: dict[int, list[dict]] = defaultdict(list)
    for r in records:
        buckets[int((r["_epoch"] - t0) // step)].append(r)

    out = []
    for idx in sorted(buckets):
        rows = buckets[idx]
        row = {
            "t": idx * step,
            "timestamp": rows[0]["timestamp"],
        }
        for f in RECORD_NUMERIC_FIELDS:
            vals = [r.get(f) for r in rows]
            if any(v is not None for v in vals):
                row[f] = _mean(vals)
        out.append(row)
    return out


# ---------------------------------------------------------------- 書き出し

def write_json(obj: Any, stem: str, suffix: str) -> Path:
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    path = JSON_DIR / f"{stem}_{suffix}.json"
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return path


def archive_activity(
    client,
    activity_id: int | str,
    start_date: str,
    brick_key: str | None = None,
) -> dict:
    """
    メインエントリ。run_analysis.py から1行で呼ぶ。

    returns:
        {
          "stem": ...,
          "sport": "bike",
          "files": {"fit": Path, "laps": Path, "series_5s": Path, ...},
          "urls":  {"fit": url, "laps": url, ...},
          "session": {...},
        }
    """
    stem = f"{start_date}_{activity_id}"
    if brick_key:
        stem = f"{start_date}_{brick_key}_{activity_id}"

    fit_bytes = download_fit(client, activity_id)
    files: dict[str, Path] = {"fit": save_fit(fit_bytes, stem)}

    parsed = parse_fit(fit_bytes)
    sport = detect_sport(parsed["session"])

    meta = {
        "activity_id": str(activity_id),
        "date": start_date,
        "sport": sport,
        "brick_key": brick_key,
        "session": parsed["session"],
        "lap_count": len(parsed["laps"]),
        "length_count": len(parsed["lengths"]),
        "record_count": len(parsed["records"]),
    }
    files["meta"] = write_json(meta, stem, "meta")
    files["laps"] = write_json(parsed["laps"], stem, "laps")

    if sport == "swim":
        # プールスイム：length が構造復元の生命線
        files["lengths"] = write_json(parsed["lengths"], stem, "lengths")
    elif sport in ("bike", "ows"):
        files["series_5s"] = write_json(
            downsample(parsed["records"]), stem, "series_5s"
        )
    elif sport == "run":
        # ランは laps で足りるが、熱ダレ検証用に時系列も残す
        files["series_5s"] = write_json(
            downsample(parsed["records"]), stem, "series_5s"
        )

    urls = {k: RAW_BASE_URL + str(p).replace("\\", "/") for k, p in files.items()}

    return {
        "stem": stem,
        "sport": sport,
        "files": files,
        "urls": urls,
        "session": parsed["session"],
    }


# ---------------------------------------------------------------- ブリック

def assign_brick_keys(activities: list[dict]) -> dict[str, str]:
    """
    同日・間隔60分以内の連続セッションに共通キーを振る。
    activities: [{"activity_id":..., "start_time": datetime, "date": "YYYY-MM-DD"}, ...]
    returns: {activity_id: "brick01", ...}
    """
    by_date: dict[str, list[dict]] = defaultdict(list)
    for a in activities:
        by_date[a["date"]].append(a)

    keys: dict[str, str] = {}
    for date, items in by_date.items():
        items.sort(key=lambda x: x["start_time"])
        group, groups = [items[0]], []
        for prev, cur in zip(items, items[1:]):
            gap = (cur["start_time"] - prev["start_time"]).total_seconds() / 60
            if gap <= BRICK_GAP_MIN:
                group.append(cur)
            else:
                groups.append(group)
                group = [cur]
        groups.append(group)

        n = 0
        for g in groups:
            if len(g) < 2:
                continue
            n += 1
            for a in g:
                keys[str(a["activity_id"])] = f"brick{n:02d}"
    return keys
