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
    "first_lap_index",
    "num_laps",
]

# マルチスポーツ（トライアスロン）FIT で「種目」として数えないセッション
TRANSITION_SPORTS = {"transition"}

# 重複判定（同一ライドを別デバイスで二重記録）のしきい値
DUP_OVERLAP_RATIO = 0.5     # 短い方の時間の何割が重なれば同一とみなすか
DUP_DISTANCE_TOL = 0.15     # 距離の相対差の許容


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
    """数値以外（FIT が稀に返す文字列・enum）は黙って捨てて平均する。"""
    nums = [
        float(x) for x in xs
        if isinstance(x, (int, float)) and not isinstance(x, bool)
        and not (isinstance(x, float) and (math.isnan(x) or math.isinf(x)))
    ]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 2)


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
    sessions: list[dict] = []
    laps: list[dict] = []
    lengths: list[dict] = []
    records: list[dict] = []

    with fitdecode.FitReader(io.BytesIO(fit_bytes)) as fr:
        for frame in fr:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue

            if frame.name == "session":
                sessions.append(_pick(frame, SESSION_FIELDS))

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

    # 単一種目の従来コード互換：最初の非トランジションを "session" として残す
    primary = {}
    for sess in sessions:
        if str(sess.get("sport", "")).lower() not in TRANSITION_SPORTS:
            primary = sess
            break
    if not primary and sessions:
        primary = sessions[0]

    return {
        "session": primary,
        "sessions": sessions,
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


# ---------------------------------------------------------------- マルチスポーツ分割

def _parse_iso(v) -> datetime | None:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except Exception:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def is_multisport(parsed: dict) -> bool:
    """非トランジションのセッションが2つ以上なら、1本のFITに複数種目が入っている。"""
    return len(real_sessions(parsed)) > 1


def real_sessions(parsed: dict) -> list[dict]:
    return [
        sess for sess in (parsed.get("sessions") or [])
        if str(sess.get("sport", "")).lower() not in TRANSITION_SPORTS
    ]


def split_legs(parsed: dict) -> list[dict]:
    """
    マルチスポーツ FIT を種目ごとの「レグ」に割る。

    laps は session.first_lap_index / num_laps で厳密に切る（FIT が持つ正解）。
    それが無い古いFITだけ start_time + total_elapsed_time の時間窓で切る。
    records / lengths は常に時間窓で切る。
    トランジションは種目として出さない（レグ側に transition_before として秒数だけ残す）。
    """
    sessions = parsed.get("sessions") or []
    laps = parsed.get("laps") or []
    lengths = parsed.get("lengths") or []
    records = parsed.get("records") or []

    legs: list[dict] = []
    pending_transition = 0.0
    leg_no = 0

    for sess in sessions:
        sport_raw = str(sess.get("sport", "")).lower()
        start = _parse_iso(sess.get("start_time"))
        elapsed = sess.get("total_elapsed_time") or 0

        if sport_raw in TRANSITION_SPORTS:
            pending_transition += float(elapsed or 0)
            continue

        leg_no += 1

        # --- laps
        fli = sess.get("first_lap_index")
        nl = sess.get("num_laps")
        if isinstance(fli, int) and isinstance(nl, int) and nl > 0:
            leg_laps = laps[fli:fli + nl]
        elif start:
            end = start.timestamp() + float(elapsed or 0) + 1
            leg_laps = [
                l for l in laps
                if (lt := _parse_iso(l.get("start_time")))
                and start.timestamp() - 1 <= lt.timestamp() <= end
            ]
        else:
            leg_laps = []

        # --- records / lengths（時間窓）
        if start:
            t0 = start.timestamp() - 1
            t1 = t0 + float(elapsed or 0) + 2
            leg_records = [r for r in records if t0 <= r.get("_epoch", 0) <= t1]
            leg_lengths = [
                x for x in lengths
                if (xt := _parse_iso(x.get("start_time")))
                and t0 <= xt.timestamp() <= t1
            ]
        else:
            leg_records, leg_lengths = [], []

        legs.append({
            "leg": leg_no,
            "session": sess,
            "sport": detect_sport(sess),
            "laps": leg_laps,
            "lengths": leg_lengths,
            "records": leg_records,
            "transition_before_s": round(pending_transition, 3) or None,
        })
        pending_transition = 0.0

    return legs


# ---------------------------------------------------------------- 書き出し

def write_json(obj: Any, stem: str, suffix: str) -> Path:
    JSON_DIR.mkdir(parents=True, exist_ok=True)
    path = JSON_DIR / f"{stem}_{suffix}.json"
    path.write_text(
        json.dumps(obj, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return path


def _emit_entry(
    stem: str,
    sport: str,
    session: dict,
    laps: list,
    lengths: list,
    records: list,
    base_meta: dict,
) -> dict[str, Path]:
    """1エントリ分（= index.json の1行になる単位）のJSONを書き出す。"""
    meta = dict(base_meta)
    meta.update({
        "stem": stem,
        "sport": sport,
        "session": session,
        "lap_count": len(laps),
        "length_count": len(lengths),
        "record_count": len(records),
    })
    files: dict[str, Path] = {
        "meta": write_json(meta, stem, "meta"),
        "laps": write_json(laps, stem, "laps"),
    }
    if sport == "swim":
        # プールスイム：length が構造復元の生命線
        files["lengths"] = write_json(lengths, stem, "lengths")
    elif sport in ("bike", "ows", "run"):
        # バイク/OWS は NP/IF/VI 再計算用、ランは熱ダレ検証用
        files["series_5s"] = write_json(downsample(records), stem, "series_5s")
    return files


def archive_activity(
    client,
    activity_id: int | str,
    start_date: str,
    brick_key: str | None = None,
) -> dict:
    """
    メインエントリ。run_analysis.py から1行で呼ぶ。

    マルチスポーツ（1本のFITにスイム→バイク→ランが入っているトライアスロン形式）は
    種目ごとのレグに分割し、レグ単位で index.json に載る。
    FIT原本は親stemに1本だけ保存し、各レグの meta に fit_stem として親を記録する。

    returns:
        {
          "stem": ...,
          "sport": "bike" | "multisport",
          "legs": [...],                 # マルチスポーツのときのみ
          "files": {"fit": Path, "laps": Path, "series_5s": Path, ...},
          "urls":  {"fit": url, "laps": url, ...},
          "session": {...},
        }
    """
    stem = f"{start_date}_{activity_id}"
    if brick_key:
        stem = f"{start_date}_{brick_key}_{activity_id}"

    fit_bytes = download_fit(client, activity_id)
    fit_path = save_fit(fit_bytes, stem)
    files: dict[str, Path] = {"fit": fit_path}

    parsed = parse_fit(fit_bytes)
    legs = split_legs(parsed)

    base_meta = {
        "activity_id": str(activity_id),
        "date": start_date,
        "brick_key": brick_key,
        "fit_stem": stem,
    }

    if len(legs) > 1:
        # ---- マルチスポーツ：レグごとに独立エントリ化
        leg_summaries = []
        for leg in legs:
            leg_stem = f"{stem}_l{leg['leg']}_{leg['sport']}"
            leg_meta = dict(
                base_meta,
                multisport=True,
                parent_stem=stem,
                leg=leg["leg"],
                leg_count=len(legs),
                transition_before_s=leg["transition_before_s"],
            )
            leg_files = _emit_entry(
                leg_stem, leg["sport"], leg["session"],
                leg["laps"], leg["lengths"], leg["records"], leg_meta,
            )
            for k, v in leg_files.items():
                files[f"l{leg['leg']}_{leg['sport']}_{k}"] = v
            leg_summaries.append({
                "leg": leg["leg"],
                "sport": leg["sport"],
                "stem": leg_stem,
                "distance_m": leg["session"].get("total_distance"),
                "duration_s": leg["session"].get("total_timer_time"),
                "transition_before_s": leg["transition_before_s"],
            })

        # 親は index に載せない（*_meta.json にしない）サマリだけ残す
        files["multisport"] = write_json(
            dict(base_meta, leg_count=len(legs), legs=leg_summaries),
            stem, "multisport",
        )
        sport = "multisport"
        session = legs[0]["session"]
    else:
        # ---- 単一種目：従来どおり
        sport = detect_sport(parsed["session"])
        session = parsed["session"]
        files.update(_emit_entry(
            stem, sport, session,
            parsed["laps"], parsed["lengths"], parsed["records"], base_meta,
        ))
        leg_summaries = []

    urls = {k: RAW_BASE_URL + str(v).replace("\\", "/") for k, v in files.items()}

    return {
        "stem": stem,
        "sport": sport,
        "legs": leg_summaries,
        "files": files,
        "urls": urls,
        "session": session,
    }


# ---------------------------------------------------------------- 索引

INDEX_PATH = RAW_ROOT / "index.json"
INDEX_URL = RAW_BASE_URL + "data/raw/index.json"


def _window(entry: dict) -> tuple[float, float] | None:
    st = _parse_iso(entry.get("start_time"))
    if not st:
        return None
    dur = entry.get("duration_s") or 0
    return (st.timestamp(), st.timestamp() + float(dur or 0))


def mark_duplicates(entries: list[dict]) -> None:
    """
    同じ運動を2台のデバイスが別々に記録した重複（例: フォアランナーのマルチスポーツの
    バイクレグ と Edge 840 の単独バイク）に共通の dup_group を振る。

    削除はしない。どちらが主かを dup_primary で示すだけにして、
    ブリックの文脈（マルチスポーツ側）もパワー等の精度（Edge側）も失わないようにする。
    """
    n = 0
    for i, a in enumerate(entries):
        wa = _window(a)
        if not wa or a.get("dup_group"):
            continue
        group = [a]
        for b in entries[i + 1:]:
            if b.get("sport") != a.get("sport") or b.get("dup_group"):
                continue
            wb = _window(b)
            if not wb:
                continue
            overlap = min(wa[1], wb[1]) - max(wa[0], wb[0])
            shorter = min(wa[1] - wa[0], wb[1] - wb[0]) or 1
            if overlap / shorter < DUP_OVERLAP_RATIO:
                continue
            da, db = a.get("distance_m") or 0, b.get("distance_m") or 0
            if da and db and abs(da - db) / max(da, db) > DUP_DISTANCE_TOL:
                continue
            group.append(b)
        if len(group) < 2:
            continue
        n += 1
        gid = f"dup{n:02d}_{a.get('date')}_{a.get('sport')}"
        # 主 = 記録点数が多い方（パワー等を持つ実機ログを優先）
        primary = max(group, key=lambda e: (e.get("record_count") or 0, e.get("lap_count") or 0))
        for e in group:
            e["dup_group"] = gid
            e["dup_primary"] = e is primary


def rebuild_index() -> Path | None:
    """data/raw/json/*_meta.json を走査して data/raw/index.json を作り直す。

    GitHub のディレクトリ一覧は API も HTML も外から読めない（403 / robots.txt）ため、
    「何が保存されているか」を知る唯一の入口がこの索引になる。
    後日セッションではこの1ファイルを取得すれば、日付・種目から目的のURLを引ける。

    毎回まるごと作り直すので、どの経路（毎時分析／backfill）で足されたファイルも拾える。
    """
    if not JSON_DIR.exists():
        return None

    entries = []
    for meta_path in sorted(JSON_DIR.glob("*_meta.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        stem = meta_path.name[: -len("_meta.json")]

        files = {}
        # マルチスポーツのレグは FIT 原本を親stemと共有する
        fit_stem = meta.get("fit_stem") or meta.get("parent_stem") or stem
        fit = FIT_DIR / f"{fit_stem}.fit"
        if fit.exists():
            files["fit"] = RAW_BASE_URL + str(fit).replace("\\", "/")
        for kind in ("meta", "laps", "lengths", "series_5s"):
            p = JSON_DIR / f"{stem}_{kind}.json"
            if p.exists():
                files[kind] = RAW_BASE_URL + str(p).replace("\\", "/")

        session = meta.get("session") or {}
        entries.append({
            "date": meta.get("date"),
            "activity_id": meta.get("activity_id"),
            "sport": meta.get("sport"),
            "brick_key": meta.get("brick_key"),
            "stem": stem,
            "multisport": meta.get("multisport") or None,
            "parent_stem": meta.get("parent_stem"),
            "leg": meta.get("leg"),
            "transition_before_s": meta.get("transition_before_s"),
            "start_time": session.get("start_time"),
            "distance_m": session.get("total_distance"),
            "duration_s": session.get("total_timer_time"),
            "avg_hr": session.get("avg_heart_rate"),
            "lap_count": meta.get("lap_count"),
            "length_count": meta.get("length_count"),
            "record_count": meta.get("record_count"),
            "urls": files,
        })

    entries.sort(key=lambda e: (e.get("date") or "", e.get("start_time") or ""))
    mark_duplicates(entries)
    index = {
        "repo": f"{REPO_OWNER}/{REPO_NAME}",
        "branch": REPO_BRANCH,
        "base_url": RAW_BASE_URL,
        "count": len(entries),
        "duplicate_groups": sorted({e["dup_group"] for e in entries if e.get("dup_group")}),
        "dates": sorted({e["date"] for e in entries if e.get("date")}),
        "activities": entries,
    }
    RAW_ROOT.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps(index, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return INDEX_PATH


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
