"""レースマージ機能 (v15)

トライアスロンのレース日は、同一日に複数のアクティビティが記録される:
  - Forerunner 965: スイム / バイク / ラン（全部位を一括 or 種目別）
  - Edge 840: バイクのみ（パワーメーター連携・実測精度が高い）

このモジュールは同日アクティビティ群から「1レース」を検出し、
  - スイム・ラン・トランジション・総合 → 965 由来を正
  - バイクのパワー/ケイデンス/速度/標高 → Edge 840 由来を正
として1つの統合アクティビティに束ねる。

純粋関数として実装し（I/O を持たない）、run_analysis 側からデータを渡して使う。
単体テスト可能。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# ====================== 種目分類 ======================
SWIM_KEYS = {"lap_swimming", "open_water_swimming", "swimming"}
BIKE_KEYS = {"cycling", "road_biking", "indoor_cycling", "virtual_ride", "gravel_cycling", "mountain_biking"}
RUN_KEYS = {"running", "trail_running", "treadmill_running", "track_running", "street_running"}
# マルチスポーツ（965が1アクティビティでS→B→Rを記録した場合）
MULTISPORT_KEYS = {"multi_sport", "triathlon"}

# デバイス確定判定用の Garmin product ID
EDGE_PRODUCT_IDS = {4062}            # Edge 840（必要に応じ他Edgeも追記）
FORERUNNER_PRODUCT_IDS = {4315}     # Forerunner 965


def _type_key(act: dict[str, Any]) -> str:
    at = act.get("activityType") or {}
    if isinstance(at, dict):
        return (at.get("typeKey") or at.get("type_key") or "").lower()
    return (act.get("activityTypeName") or act.get("sportType") or "").lower()


def _discipline(act: dict[str, Any]) -> str | None:
    """アクティビティの種目を 'swim' | 'bike' | 'run' | 'multi' | None で返す。"""
    k = _type_key(act)
    if k in SWIM_KEYS:
        return "swim"
    if k in BIKE_KEYS:
        return "bike"
    if k in RUN_KEYS:
        return "run"
    if k in MULTISPORT_KEYS:
        return "multi"
    return None


# ====================== 時刻・指標ヘルパ ======================
def _start_dt(act: dict[str, Any]) -> datetime | None:
    """startTimeGMT（無ければLocal）を datetime に。比較用なので tz は無視。"""
    for key in ("startTimeGMT", "startTimeLocal"):
        s = act.get(key)
        if not s:
            continue
        s = str(s).replace("T", " ").replace("Z", "").strip()
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(s[:26], fmt)
            except ValueError:
                continue
    # beginTimestamp（ミリ秒エポック）フォールバック
    ts = act.get("beginTimestamp")
    if isinstance(ts, (int, float)):
        try:
            return datetime.utcfromtimestamp(ts / 1000)
        except (ValueError, OSError):
            return None
    return None


def _has_power(act: dict[str, Any]) -> bool:
    """パワーデータを持つ（=パワーメーター連携、Edge 840 の蓋然性が高い）か。"""
    for k in ("averagePower", "normPower", "normalizedPower", "maxPower"):
        v = act.get(k)
        if isinstance(v, (int, float)) and v > 0:
            return True
    return False


def _power_richness(act: dict[str, Any]) -> int:
    """パワー関連フィールドの充実度スコア。Edge優先判定の決め手。"""
    score = 0
    for k in ("averagePower", "normPower", "normalizedPower", "maxPower",
              "avgPowerToWeight", "leftRightBalance", "avgLeftPowerPhase",
              "trainingStressScore", "intensityFactor"):
        v = act.get(k)
        if v not in (None, "", 0):
            score += 1
    return score


# ====================== レース検出 ======================
def detect_race_day(activities: list[dict[str, Any]]) -> bool:
    """同日アクティビティ群がレース構成かを判定する。

    レースとみなす条件（いずれか）:
      A. マルチスポーツ(965一括) アクティビティが1件以上ある
      B. swim / bike / run の3種目すべてが揃っている
      C. bike が2件以上（965+Edge の二重記録の蓋然性）かつ run か swim が同日にある
    """
    disciplines = [_discipline(a) for a in activities]
    has_multi = any(d == "multi" for d in disciplines)
    if has_multi:
        return True

    swims = [a for a, d in zip(activities, disciplines) if d == "swim"]
    bikes = [a for a, d in zip(activities, disciplines) if d == "bike"]
    runs = [a for a, d in zip(activities, disciplines) if d == "run"]

    if swims and bikes and runs:
        return True
    if len(bikes) >= 2 and (runs or swims):
        return True
    return False


def _pick_primary_bike(bikes: list[dict[str, Any]]) -> dict[str, Any]:
    """バイクが複数（965 と Edge 840）あるとき、Edge 由来を採用する。

    判定の優先順位:
      1. デバイス確定情報 `_deviceProduct`（FITローダー由来）。
         Edge系 product ID を最優先で採用する（推定ではなく確定）。
      2. パワーデータの充実度（API JSON 等でデバイス情報が無い場合のフォールバック）。
      3. 同点なら距離が長い方。
    """
    if len(bikes) == 1:
        return bikes[0]

    def device_rank(a: dict[str, Any]) -> int:
        # Edge 系 = 2、それ以外で確定情報あり = 1、不明 = 0
        prod = a.get("_deviceProduct")
        if prod in EDGE_PRODUCT_IDS:
            return 2
        if prod is not None:
            return 1
        return 0

    return sorted(
        bikes,
        key=lambda a: (device_rank(a), _power_richness(a), a.get("distance") or 0),
        reverse=True,
    )[0]


def select_race_components(activities: list[dict[str, Any]]) -> dict[str, Any]:
    """レース日のアクティビティ群から、採用する各部位のアクティビティを選ぶ。

    返り値:
      {
        "swim":  act | None,
        "bike":  act | None,   # 複数あれば Edge 由来（パワー豊富な方）
        "run":   act | None,
        "multi": act | None,   # 965 一括（S→B→R）があればこれ
        "bike_dropped": [act, ...],   # 不採用にした965側バイク等
        "all": [act, ...],     # 時系列順の全アクティビティ
      }
    """
    indexed = sorted(activities, key=lambda a: (_start_dt(a) or datetime.min))
    by_disc: dict[str, list[dict[str, Any]]] = {"swim": [], "bike": [], "run": [], "multi": []}
    for a in indexed:
        d = _discipline(a)
        if d in by_disc:
            by_disc[d].append(a)

    bikes = by_disc["bike"]
    primary_bike = _pick_primary_bike(bikes) if bikes else None
    dropped = [b for b in bikes if b is not primary_bike] if bikes else []

    return {
        "swim": by_disc["swim"][0] if by_disc["swim"] else None,
        "bike": primary_bike,
        "run": by_disc["run"][-1] if by_disc["run"] else None,
        "multi": by_disc["multi"][0] if by_disc["multi"] else None,
        "bike_dropped": dropped,
        "all": indexed,
    }


# ====================== マージ済み統合ペイロード ======================
def _fmt_hms(sec: Any) -> str:
    try:
        sec = float(sec)
    except (TypeError, ValueError):
        return "—"
    if sec > 100000:  # ミリ秒
        sec /= 1000
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _km(act: dict[str, Any]) -> str:
    d = act.get("distance") or act.get("distanceInMeters")
    if not d:
        return "—"
    return f"{d / 1000:.2f}km"


def _as_summary(rec: Any) -> dict[str, Any]:
    """コンポーネント要素を summary dict に正規化する。

    FIT経路は {'summary':..., 'laps':...} のラッパー、
    API JSON経路は素の activity dict。どちらでも summary を取り出す。
    """
    if isinstance(rec, dict) and "summary" in rec and isinstance(rec["summary"], dict):
        return rec["summary"]
    return rec if isinstance(rec, dict) else {}


def build_race_digest(components: dict[str, Any]) -> str:
    """LLM に渡す「レース構成サマリ（機械集計）」を生成する。

    どのアクティビティが各部位の正データか、バイクは何由来を採用したか、
    どれを破棄したかを明示し、LLM が取り違えないようにする。
    summarize_laps と同じ思想で、推測の余地を機械側で潰す。
    """
    lines = ["## 🏁 レース構成（機械集計・最優先）", ""]
    lines.append("これはトライアスロンのレース日です。以下の部位別データ採用ルールに厳密に従うこと:")
    lines.append("")
    lines.append("- **スイム・ラン・トランジション・総合タイム**: Forerunner 965 由来を正とする")
    lines.append("- **バイクのパワー/NP/IF/VI/ケイデンス/速度/標高**: Edge 840 由来（パワーデータが豊富な方）を正とする")
    lines.append("- 同一バイク区間が2台で二重記録されている場合、965側バイクの速度・パワーは破棄しEdge側で置換済み")
    lines.append("- W/kg・FTP照合（195W基準）・温度補正パワーキャップ（26-30℃で170-175W）は必ずEdge側パワーで評価")
    lines.append("")

    multi = components.get("multi")
    if multi:
        m = _as_summary(multi)
        lines.append(f"- **マルチスポーツ記録(965一括)**: {m.get('activityName', '—')} "
                     f"／総合 {_fmt_hms(m.get('duration'))}（S→B→R一括・トランジション含む）")

    # T1/T2 実測（FIT経路のみ存在）
    tt = transition_times(components) if components.get("t1") or components.get("t2") else None

    order = [("swim", "スイム", "965"), ("bike", "バイク", "Edge 840採用"), ("run", "ラン", "965")]
    lines.append("")
    lines.append("| 部位 | 採用アクティビティ | 距離 | タイム | 採用デバイス |")
    lines.append("|---|---|---|---|---|")
    for key, label, dev in order:
        rec = components.get(key)
        if not rec:
            lines.append(f"| {label} | （記録なし） | — | — | — |")
            continue
        act = _as_summary(rec)
        rich = _power_richness(act)
        dev_note = dev
        if key == "bike":
            # マージ済み（FIT経路）なら専用ラベル、そうでなければ充実度で表示
            if act.get("_bikeMerged"):
                dev_note = "Edge 840（パワー/標高）＋965（HR）"
            else:
                dev_note = f"Edge 840相当（パワー指標{rich}項目）" if rich >= 2 else "965（パワー乏しいが代替なし）"
        lines.append(
            f"| {label} | {act.get('activityName', '—')} | {_km(act)} | "
            f"{_fmt_hms(act.get('duration'))} | {dev_note} |"
        )
        # バイクの行の直後にEdge専用指標を補足
        if key == "bike":
            extras = []
            if act.get("maxPower"):
                extras.append(f"最大{act['maxPower']}W")
            if act.get("normPower") or act.get("normalizedPower"):
                extras.append(f"NP{act.get('normPower') or act.get('normalizedPower')}W")
            if act.get("intensityFactor"):
                extras.append(f"IF{act['intensityFactor']}")
            if act.get("trainingStressScore"):
                extras.append(f"TSS{act['trainingStressScore']}")
            if act.get("totalAscent"):
                extras.append(f"獲得標高{act['totalAscent']}m")
            if act.get("averageHR"):
                extras.append(f"平均HR{act['averageHR']}(965)")
            if extras:
                lines.append(f"|  | ↳ {' / '.join(extras)} |  |  |  |")

    if tt:
        lines.append("")
        lines.append(f"**トランジション実測**: T1 {tt['T1']} / T2 {tt['T2']}")

    dropped = components.get("bike_dropped") or []
    if dropped:
        lines.append("")
        lines.append("**破棄したバイク記録（965側の重複・速度/パワーは不採用）**:")
        for d in dropped:
            da = _as_summary(d)
            lines.append(f"- {da.get('activityName', '—')} {_km(da)} / {_fmt_hms(da.get('duration'))}"
                         f"（パワー指標{_power_richness(da)}項目）")

    lines.append("")
    lines.append("> ⚠️ 上の採用ルールに反する記述（例: バイクパワーを965値で書く、破棄した記録を別セッション扱いする）は禁止。")
    return "\n".join(lines)


# ====================== FIT 入力からのレース組み立て (v15) ======================
def _merge_bike_with_edge(bike_965: dict[str, Any], bike_edge: dict[str, Any]) -> dict[str, Any]:
    """965マルチ内のバイクと Edge840 バイクを統合する。

    採用ルール:
      - パワー/速度/標高/IF/TSS/ケイデンス = Edge840 を正
      - HR = 965（本体の光学/チェストストラップで連続記録される側）を正
      - スイム・ランとの時系列連続性のため、開始時刻・トランジション境界は965側を残す
    返り値は {'summary':..., 'laps':...}（laps は Edge 側の詳細を採用）。
    """
    s965 = bike_965["summary"]
    sedge = bike_edge["summary"]

    merged = dict(s965)  # 土台は965（時系列キーを保持）
    # Edge を正とするパワー系・標高系フィールド
    edge_primary_keys = [
        "averagePower", "normPower", "normalizedPower", "maxPower",
        "averageSpeed", "averageBikeCadence", "totalAscent", "totalDescent",
        "intensityFactor", "trainingStressScore", "calories", "distance",
        "duration", "elapsedDuration", "movingDuration",
    ]
    for k in edge_primary_keys:
        v = sedge.get(k)
        if v is not None and v != "":
            merged[k] = v
    # HR は965を維持（既に merged に入っている）。ただし965側が欠損なら Edge で補完
    for k in ("averageHR", "maxHR"):
        if not merged.get(k) and sedge.get(k):
            merged[k] = sedge[k]

    merged["activityName"] = "バイク（Edge 840採用・HRは965）"
    merged["_deviceName"] = "Edge 840 (power) + Forerunner 965 (HR)"
    merged["_bikeMerged"] = True

    return {"summary": merged, "laps": bike_edge.get("laps", [])}


def assemble_race_from_fits(
    multisport_parts: dict[str, Any],
    edge_bike: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """FIT由来データからレース統合コンポーネントを組み立てる。

    引数:
      multisport_parts: fit_loader.split_multisport() の返り値
                        （swim/bike/run/t1/t2 の各 {'summary','laps'} or None）
      edge_bike:        Edge840 単独FITの load_fit()[0]（{'summary','laps'}）。
                        無ければ965マルチのバイクをそのまま使う。

    返り値: select_race_components と同じ形のコンポーネント dict。
            （swim/bike/run/multi/bike_dropped/all + transitions）
    """
    swim = multisport_parts.get("swim")
    bike = multisport_parts.get("bike")
    run = multisport_parts.get("run")
    t1 = multisport_parts.get("t1")
    t2 = multisport_parts.get("t2")

    dropped = []
    if edge_bike and bike:
        # 965バイクは破棄扱いにして記録、統合バイクを採用
        dropped.append(bike["summary"])
        bike = _merge_bike_with_edge(bike, edge_bike)
    elif edge_bike and not bike:
        bike = edge_bike

    all_parts = [p for p in (swim, t1, bike, t2, run) if p]

    return {
        "swim": swim,
        "bike": bike,
        "run": run,
        "multi": None,  # FIT分解時はmulti一括ではなく部位別に持つ
        "t1": t1,
        "t2": t2,
        "bike_dropped": dropped,
        "all": all_parts,
    }


def transition_times(components: dict[str, Any]) -> dict[str, str]:
    """T1/T2 の実測タイムを 'M:SS' で返す（トランジションセッションの経過時間）。"""
    def fmt(rec):
        if not rec:
            return "—"
        dur = rec["summary"].get("duration")
        if not dur:
            return "—"
        dur = int(dur)
        m, s = divmod(dur, 60)
        return f"{m}:{s:02d}"
    return {"T1": fmt(components.get("t1")), "T2": fmt(components.get("t2"))}
