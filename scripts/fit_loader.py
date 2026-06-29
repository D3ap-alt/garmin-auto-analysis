"""FITファイルローダー (v15)

Garmin の .FIT を読み込み、パイプラインが扱う「Garmin API JSON 相当の dict」に
変換する。レース日に手元の FIT（965マルチ / Edge840バイク）を取り込んで
統合分析するための入口。

設計方針:
  - fitparse に依存（requirements に追加）。
  - 1つの FIT が複数 session を持つ（マルチスポーツ）場合、session ごとに
    1アクティビティ相当の dict に分解して返す。
  - 出力 dict のキーは Garmin Connect API の summary に寄せる
    （activityType.typeKey / startTimeGMT / distance / duration / averagePower 等）。
    → run_analysis.py 側の既存ロジック（summarize_laps, build_properties,
      race_merge）をそのまま使い回せる。
  - file_id の garmin_product でデバイスを確定する（推定に頼らない）。

デバイス product ID:
  4315 = Forerunner 965
  4062 = Edge 840
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from fitparse import FitFile
except ImportError:  # 解析環境で未インストールでもimport自体は通す
    FitFile = None  # type: ignore


# product ID → デバイス名（必要に応じて追記）
GARMIN_PRODUCT_NAMES = {
    4315: "Forerunner 965",
    4062: "Edge 840",
    3865: "HRM-Pro",
}

# FIT sport → Garmin Connect typeKey へのマッピング
_SPORT_TO_TYPEKEY = {
    ("swimming", "open_water"): "open_water_swimming",
    ("swimming", "lap_swimming"): "lap_swimming",
    ("swimming", None): "open_water_swimming",
    ("cycling", "road"): "cycling",
    ("cycling", "generic"): "cycling",
    ("cycling", None): "cycling",
    ("running", "generic"): "running",
    ("running", None): "running",
    ("transition", None): "transition",
    ("transition", "generic"): "transition",
}


def _to_gmt_str(dt: Any) -> str:
    """fitparse の datetime（UTC naive または aware）を 'YYYY-MM-DD HH:MM:SS' に。"""
    if not isinstance(dt, datetime):
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _msg_dict(msg) -> dict[str, Any]:
    return {f.name: f.value for f in msg}


def _typekey(sport: str | None, sub: str | None) -> str:
    if (sport, sub) in _SPORT_TO_TYPEKEY:
        return _SPORT_TO_TYPEKEY[(sport, sub)]
    if (sport, None) in _SPORT_TO_TYPEKEY:
        return _SPORT_TO_TYPEKEY[(sport, None)]
    return (sport or "").lower()


def _avg_speed(dist_m: Any, elapsed_s: Any) -> float | None:
    try:
        if dist_m and elapsed_s and elapsed_s > 0:
            return round(float(dist_m) / float(elapsed_s), 4)  # m/s
    except (TypeError, ValueError, ZeroDivisionError):
        pass
    return None


def detect_device(fit_path: str | Path) -> dict[str, Any]:
    """FIT の作成元デバイスを返す: {'product': int|None, 'name': str}。"""
    if FitFile is None:
        raise RuntimeError("fitparse 未インストール")
    fit = FitFile(str(fit_path))
    for m in fit.get_messages("file_id"):
        d = _msg_dict(m)
        prod = d.get("garmin_product") or d.get("product")
        try:
            prod = int(prod) if prod is not None else None
        except (TypeError, ValueError):
            prod = None
        return {"product": prod, "name": GARMIN_PRODUCT_NAMES.get(prod, f"product#{prod}")}
    return {"product": None, "name": "unknown"}


def _lap_to_dict(lap: dict[str, Any]) -> dict[str, Any]:
    """FIT lap → summarize_laps が読める dict（distance 等のキー名に合わせる）。"""
    dist = lap.get("total_distance")
    elapsed = lap.get("total_elapsed_time")
    # ランニングケイデンスは avg_running_cadence（片足）→ spm は ×2
    run_cad = lap.get("avg_running_cadence")
    cadence = lap.get("avg_cadence")
    if cadence is None and run_cad is not None:
        cadence = run_cad * 2
    return {
        "distance": dist,
        "duration": elapsed,
        "movingDuration": lap.get("total_timer_time"),
        "averageHR": lap.get("avg_heart_rate"),
        "maxHR": lap.get("max_heart_rate"),
        "averagePower": lap.get("avg_power"),
        "maxPower": lap.get("max_power"),
        "averageSpeed": _avg_speed(dist, elapsed),
        "averageRunCadence": (run_cad * 2) if run_cad is not None else None,
        "averageBikeCadence": lap.get("avg_cadence"),
        "totalAscent": lap.get("total_ascent"),
        "avgStepLength": lap.get("avg_step_length"),
        "avgVerticalRatio": lap.get("avg_vertical_ratio"),
        "avgGroundContactTime": lap.get("avg_stance_time"),
        "startTimeGMT": _to_gmt_str(lap.get("start_time")),
        "sport": lap.get("sport"),
    }


def _session_to_summary(sess: dict[str, Any], device: dict[str, Any], base_id: int, idx: int) -> dict[str, Any]:
    sport = sess.get("sport")
    sub = sess.get("sub_sport")
    dist = sess.get("total_distance")
    elapsed = sess.get("total_elapsed_time")
    run_cad = sess.get("avg_running_cadence")
    avg_cad = sess.get("avg_cadence")
    if avg_cad is None and run_cad is not None:
        avg_cad = run_cad * 2

    return {
        # race_merge / build_properties が参照するキー
        "activityId": int(f"{base_id}{idx:02d}"),  # 部位ごとに一意な擬似ID
        "activityName": f"{device['name']} {sport}",
        "activityType": {"typeKey": _typekey(sport, sub)},
        "startTimeGMT": _to_gmt_str(sess.get("start_time")),
        "startTimeLocal": _to_gmt_str(sess.get("start_time")),
        "distance": dist,
        "duration": elapsed,
        "movingDuration": sess.get("total_timer_time"),
        "elapsedDuration": elapsed,
        "averageHR": sess.get("avg_heart_rate"),
        "maxHR": sess.get("max_heart_rate"),
        "averagePower": sess.get("avg_power"),
        "normPower": sess.get("normalized_power"),
        "normalizedPower": sess.get("normalized_power"),
        "maxPower": sess.get("max_power"),
        "averageSpeed": _avg_speed(dist, elapsed),
        "averageBikeCadence": avg_cad if sport == "cycling" else None,
        "averageRunningCadence": (run_cad * 2) if run_cad is not None else None,
        "averageRunCadence": (run_cad * 2) if run_cad is not None else None,
        "totalAscent": sess.get("total_ascent"),
        "totalDescent": sess.get("total_descent"),
        "intensityFactor": sess.get("intensity_factor"),
        "trainingStressScore": sess.get("training_stress_score"),
        "calories": sess.get("total_calories"),
        # デバイス確定情報（race_merge が確定判定に使う）
        "_deviceProduct": device["product"],
        "_deviceName": device["name"],
        "_sport": sport,
        "_subSport": sub,
    }


def load_fit(fit_path: str | Path) -> list[dict[str, Any]]:
    """FIT を読み、session ごとに {'summary':..., 'laps':[...]} のリストで返す。

    マルチスポーツFITなら複数要素（swim/transition/bike/transition/run）、
    単一種目FITなら1要素。transition も含めて返す（呼び出し側で扱いを決める）。
    """
    if FitFile is None:
        raise RuntimeError("fitparse 未インストール。requirements.txt に fitparse を追加してください。")

    fit = FitFile(str(fit_path))
    device = detect_device(fit_path)

    # base_id: ファイル名の数字部分（Garmin activityId 相当）を流用
    stem = Path(fit_path).stem.split("_")[0]
    base_id = int(stem) if stem.isdigit() else abs(hash(stem)) % (10**10)

    sessions = [_msg_dict(m) for m in fit.get_messages("session")]
    laps = [_lap_to_dict(_msg_dict(m)) for m in fit.get_messages("lap")]

    results: list[dict[str, Any]] = []
    for idx, sess in enumerate(sessions):
        summary = _session_to_summary(sess, device, base_id, idx)
        sport = sess.get("sport")
        # この session の時間範囲に入る lap を sport 一致で割り当てる
        sess_laps = [l for l in laps if l.get("sport") == sport]
        # transition は lap が無いこともある
        results.append({"summary": summary, "laps": sess_laps})
    return results


def split_multisport(loaded: list[dict[str, Any]]) -> dict[str, Any]:
    """load_fit の結果（マルチスポーツ）を部位別に整理する。

    返り値: {'swim':rec|None, 'bike':rec|None, 'run':rec|None,
             't1':rec|None, 't2':rec|None}
    各 rec は {'summary':..., 'laps':[...]}。
    transition は出現順で t1 / t2 に割り当てる。
    """
    out: dict[str, Any] = {"swim": None, "bike": None, "run": None, "t1": None, "t2": None}
    transitions = []
    for rec in loaded:
        sport = rec["summary"].get("_sport")
        if sport == "swimming" and out["swim"] is None:
            out["swim"] = rec
        elif sport == "cycling" and out["bike"] is None:
            out["bike"] = rec
        elif sport == "running" and out["run"] is None:
            out["run"] = rec
        elif sport == "transition":
            transitions.append(rec)
    if transitions:
        out["t1"] = transitions[0]
    if len(transitions) > 1:
        out["t2"] = transitions[1]
    return out
