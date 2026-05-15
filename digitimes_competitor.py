"""DIGITIMES notebook competitor data contract.

This module keeps DIGITIMES credential automation separate from the ASUSTIMES
war-room API. The page can already consume locally exported CSV/JSON records,
while the monthly login/download probe can be added after authorization is
confirmed on this machine.
"""

from __future__ import annotations

import calendar
import csv
import json
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

TW_TZ = timezone(timedelta(hours=8))
SOURCE_URL = "https://www.digitimes.com.tw/research/datacharts/notebooks/"
SCHEMA_VERSION = 1

DATA_DIR = Path(os.environ.get("DIGITIMES_COMPETITOR_DATA_DIR", "data/digitimes_competitor"))
STATE_FILE = Path(os.environ.get("DIGITIMES_COMPETITOR_STATE_FILE", DATA_DIR / "state.json"))
RECORDS_FILE_ENV = "DIGITIMES_COMPETITOR_RECORDS_FILE"

_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "period": (
        "period",
        "report_period",
        "report month",
        "date",
        "month",
        "year_month",
        "年月",
        "月份",
        "月",
        "期別",
        "資料期別",
        "統計期",
        "季度",
        "季",
    ),
    "brand": (
        "brand",
        "vendor",
        "maker",
        "company",
        "oem",
        "notebook_brand",
        "品牌",
        "廠商",
        "公司",
        "品牌廠",
        "筆電品牌",
    ),
    "shipments": (
        "shipments",
        "shipment",
        "units",
        "volume",
        "出貨",
        "出貨量",
        "出貨台數",
        "出貨規模",
    ),
    "market_share": (
        "market_share",
        "market share",
        "share",
        "mkt_share",
        "市占",
        "市占率",
        "市佔",
        "市佔率",
        "占比",
        "佔比",
    ),
    "yoy": (
        "yoy",
        "y/y",
        "year over year",
        "year_over_year",
        "annual_growth",
        "年增",
        "年增率",
        "年成長",
    ),
    "qoq": (
        "qoq",
        "q/q",
        "quarter over quarter",
        "quarter_over_quarter",
        "mom",
        "m/m",
        "季增",
        "季增率",
        "月增",
        "月增率",
    ),
}


def _field_token(value: Any) -> str:
    return re.sub(r"[\s_\-/%()（）]+", "", str(value or "").strip().lower())


_FIELD_LOOKUP = {
    _field_token(alias): canonical
    for canonical, aliases in _FIELD_ALIASES.items()
    for alias in aliases
}


def _now_tw() -> datetime:
    return datetime.now(TW_TZ)


def _iso_now() -> str:
    return _now_tw().isoformat(timespec="seconds")


def _refresh_day() -> int:
    raw = os.environ.get("DIGITIMES_COMPETITOR_REFRESH_DAY", "5").strip()
    try:
        return min(28, max(1, int(raw)))
    except ValueError:
        return 5


# Multi-day refresh schedule: 5/15/30 (3 runs per month) — matches the
# Windows Scheduled Task entries ASUSTIMES_DigitimesPipeline + ASUSTIMES_SyncWarRoom.
# Day 30 is capped to month_last_day for Feb (28/29) inside _next_monthly_run.
_REFRESH_DAYS = (5, 15, 30)


def _next_monthly_run(today: date | None = None, day: int | None = None) -> str:
    """Return ISO date of the next scheduled refresh.
    Uses multi-day schedule (5/10/15/20/25/月底) by default; falls back to
    single-day mode if `day` is explicitly provided."""
    today = today or _now_tw().date()

    # Single-day mode (legacy / explicit override)
    if day is not None:
        _, month_last_day = calendar.monthrange(today.year, today.month)
        candidate = date(today.year, today.month, min(day, month_last_day))
        if candidate < today:
            year = today.year + (1 if today.month == 12 else 0)
            month = 1 if today.month == 12 else today.month + 1
            _, month_last_day = calendar.monthrange(year, month)
            candidate = date(year, month, min(day, month_last_day))
        return candidate.isoformat()

    # Multi-day schedule: smallest day STRICTLY AFTER today (so a refresh day
    # that has already happened today doesn't appear as "next update").
    # Days exceeding the month's last day are capped (e.g. day 30 → Feb 28/29).
    _, month_last_day = calendar.monthrange(today.year, today.month)
    candidates = sorted({min(d, month_last_day) for d in _REFRESH_DAYS})
    for d in candidates:
        if d > today.day:
            return date(today.year, today.month, d).isoformat()

    # All candidates this month already passed (or today) → first refresh day next month
    year = today.year + (1 if today.month == 12 else 0)
    month = 1 if today.month == 12 else today.month + 1
    _, next_last_day = calendar.monthrange(year, month)
    return date(year, month, min(_REFRESH_DAYS[0], next_last_day)).isoformat()


def _read_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return _default_state()
    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return _default_state()
        return _merge_default_state(data)
    except Exception as exc:
        logger.warning("DIGITIMES competitor state read failed: %s", exc)
        return _default_state(last_error=f"state read failed: {exc}")


def _write_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    tmp.replace(STATE_FILE)


def _default_state(last_error: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "name": "DIGITIMES Research - Notebooks",
            "url": SOURCE_URL,
            "license_note": "僅限已授權 DIGITIMES 帳號與內部使用權限。",
        },
        "connector": {
            "mode": "monthly_login_download",
            "status": "not_configured",
            "refresh_day": _refresh_day(),
            "next_scheduled_date": _next_monthly_run(),
            "last_attempted_at": None,
            "last_success_at": None,
            "last_error": last_error,
            "env": {
                "username": "DIGITIMES_USERNAME",
                "password": "DIGITIMES_PASSWORD",
                "storage_state": "DIGITIMES_STORAGE_STATE",
                "records_file": RECORDS_FILE_ENV,
                "refresh_day": "DIGITIMES_COMPETITOR_REFRESH_DAY",
            },
        },
        "records": [],
        "snapshots": [],
    }


def _merge_default_state(data: dict[str, Any]) -> dict[str, Any]:
    default = _default_state()
    merged = {**default, **data}
    merged["source"] = {**default["source"], **(data.get("source") or {})}
    merged["connector"] = {**default["connector"], **(data.get("connector") or {})}
    merged["connector"]["env"] = {
        **default["connector"]["env"],
        **((data.get("connector") or {}).get("env") or {}),
    }
    merged.setdefault("records", [])
    merged.setdefault("snapshots", [])
    return merged


def _credential_status() -> dict[str, Any]:
    username_env = "DIGITIMES_USERNAME"
    password_env = "DIGITIMES_PASSWORD"
    storage_env = "DIGITIMES_STORAGE_STATE"
    storage_path = os.environ.get(storage_env, "").strip()
    return {
        "has_username": bool(os.environ.get(username_env)),
        "has_password": bool(os.environ.get(password_env)),
        "has_storage_state": bool(storage_path and Path(storage_path).exists()),
        "storage_state_path": storage_path or None,
    }


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        text = str(value).replace("%", "").replace("％", "").replace(",", "").strip()
        if not text or text.lower() in {"na", "n/a", "none", "-", "--"}:
            return None
        return float(text)
    except (TypeError, ValueError):
        return None


def _normalize_period(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    compact = text.replace("年", "-").replace("月", "").replace("/", "-").replace(".", "-")
    compact = re.sub(r"\s+", "", compact)

    m = re.match(r"^(\d{4})-(\d{1,2})(?:-\d{1,2})?$", compact)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"

    m = re.match(r"^(\d{4})(\d{2})$", compact)
    if m:
        month = int(m.group(2))
        if 1 <= month <= 12:
            return f"{m.group(1)}-{month:02d}"

    m = re.match(r"^(\d{4})-?[qQ季]([1-4])$", compact)
    if m and ("q" in compact.lower() or "季" in text):
        return f"{m.group(1)}-Q{m.group(2)}"

    return text


def _period_sort_key(period: str | None) -> tuple[int, str]:
    if not period:
        return (0, "")
    m = re.match(r"^(\d{4})-(\d{2})$", period)
    if m:
        return (int(m.group(1)) * 100 + int(m.group(2)), period)
    m = re.match(r"^(\d{4})-Q([1-4])$", period)
    if m:
        return (int(m.group(1)) * 100 + int(m.group(2)) * 3, period)
    return (0, period)


def _brand_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    if "asus" in text or "asustek" in text or "華碩" in text:
        return "asus"
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", text)


def _normalize_record(row: dict[str, Any], source_file: str | None = None) -> dict[str, Any] | None:
    mapped: dict[str, Any] = {}
    for key, value in row.items():
        canonical = _FIELD_LOOKUP.get(_field_token(key))
        if canonical:
            mapped[canonical] = value

    if "brand" not in mapped and row.get("brand"):
        mapped["brand"] = row.get("brand")
    if "period" not in mapped and row.get("period"):
        mapped["period"] = row.get("period")

    period = _normalize_period(mapped.get("period"))
    brand = str(mapped.get("brand") or "").strip()
    if not period or not brand:
        return None

    share = _float_or_none(mapped.get("market_share"))
    if share is not None and 0 < share <= 1:
        share *= 100

    record = {
        "period": period,
        "brand": brand,
        "brand_key": _brand_key(brand),
        "shipments": _float_or_none(mapped.get("shipments")),
        "market_share": share,
        "yoy": _float_or_none(mapped.get("yoy")),
        "qoq": _float_or_none(mapped.get("qoq")),
    }
    if source_file:
        record["source_file"] = source_file
    return record


def _index_growth_by_period(growth: Any) -> dict[str, list]:
    """Convert DIGITIMES growth dict (qoq/yoy/mom) to {period: [values]} index.
    Accepts either a single dict (latest period only) or a list of dicts."""
    if isinstance(growth, list):
        result: dict[str, list] = {}
        for entry in growth:
            if isinstance(entry, dict) and entry.get("period"):
                result[entry["period"]] = entry.get("values") or []
        return result
    if isinstance(growth, dict) and growth.get("period"):
        return {growth["period"]: growth.get("values") or []}
    return {}


def _flatten_digitimes_section(section: dict, growth_qoq_key: str = "qoq") -> list[dict[str, Any]]:
    """Flatten DIGITIMES brand×period matrix (monthly_top5 / quarterly_top6) into flat rows."""
    rows: list[dict[str, Any]] = []
    brands = section.get("brands") or []
    shipments_list = section.get("shipments") or []
    if not brands or not shipments_list:
        return rows

    qoq_index = _index_growth_by_period(section.get(growth_qoq_key) or section.get("mom"))
    yoy_index = _index_growth_by_period(section.get("yoy"))

    for entry in shipments_list:
        period_raw = entry.get("period") if isinstance(entry, dict) else None
        if not period_raw:
            continue
        # Strip "(e)" estimate suffix; keep period clean for sorting
        period_clean = str(period_raw).replace("(e)", "").strip()
        values = entry.get("values") or []
        if len(values) != len(brands):
            continue
        period_total = sum(v for v in values if isinstance(v, (int, float)))

        for idx, brand in enumerate(brands):
            shipments = values[idx] if idx < len(values) else None
            row: dict[str, Any] = {"period": period_clean, "brand": brand}
            if isinstance(shipments, (int, float)):
                row["shipments"] = shipments
                if period_total:
                    row["market_share"] = round(shipments / period_total * 100, 2)

            qoq_vals = qoq_index.get(period_raw) or qoq_index.get(period_clean)
            if qoq_vals and idx < len(qoq_vals):
                row["qoq"] = qoq_vals[idx]

            yoy_vals = yoy_index.get(period_raw) or yoy_index.get(period_clean)
            if yoy_vals and idx < len(yoy_vals):
                row["yoy"] = yoy_vals[idx]

            rows.append(row)
    return rows


def _flatten_digitimes_rich(data: dict) -> list[dict[str, Any]]:
    """Convert DIGITIMES rich JSON (notebooks_api_*.json) into flat rows the war room understands.
    Prefers quarterly_top6 (industry standard, includes Apple). Falls back to monthly_top5."""
    qt6 = data.get("quarterly_top6")
    if isinstance(qt6, dict):
        rows = _flatten_digitimes_section(qt6, growth_qoq_key="qoq")
        if rows:
            return rows
    mt5 = data.get("monthly_top5")
    if isinstance(mt5, dict):
        return _flatten_digitimes_section(mt5, growth_qoq_key="mom")
    return []


def _iter_json_rows(data: Any) -> tuple[list[dict[str, Any]], str | None]:
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)], None
    if not isinstance(data, dict):
        return [], None

    # DIGITIMES rich format (notebooks_api_*.json from desktop pipeline)
    if "quarterly_top6" in data or "monthly_top5" in data:
        return _flatten_digitimes_rich(data), None

    default_period = _normalize_period(
        data.get("period") or data.get("report_period") or data.get("month") or data.get("期別")
    )
    for key in ("records", "rows", "data", "latest_rows"):
        value = data.get(key)
        if isinstance(value, list):
            return [r for r in value if isinstance(r, dict)], default_period

    snapshots = data.get("snapshots")
    if isinstance(snapshots, list) and snapshots:
        latest = snapshots[-1]
        if isinstance(latest, dict):
            rows, snap_period = _iter_json_rows(latest)
            return rows, snap_period or default_period

    if any(_FIELD_LOOKUP.get(_field_token(k)) for k in data):
        return [data], default_period
    return [], default_period


_CHART_DATA_CACHE: dict[str, Any] = {}


def _read_records_from_file(path: Path) -> tuple[list[dict[str, Any]], str | None]:
    try:
        source = str(path)
        records: list[dict[str, Any]] = []
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    rec = _normalize_record(row, source)
                    if rec:
                        records.append(rec)
            return records, None

        if path.suffix.lower() == ".json":
            with path.open("r", encoding="utf-8-sig") as f:
                raw = json.load(f)
            # Capture rich chart fields from DIGITIMES format for the war-room visualization
            if isinstance(raw, dict) and ("quarterly_top6" in raw or "monthly_top5" in raw):
                _CHART_DATA_CACHE["latest"] = {
                    "period_label": raw.get("period_label"),
                    "generated_at": raw.get("generated_at"),
                    "global_nb_shipments": raw.get("global_nb_shipments") or [],
                    "monthly_top5": raw.get("monthly_top5") or {},
                    "quarterly_top6": raw.get("quarterly_top6") or {},
                    "monthly_top5_line": (raw.get("charts") or {}).get("monthly_top5_line") or {},
                }
            rows, default_period = _iter_json_rows(raw)
            for row in rows:
                if default_period and not any(_FIELD_LOOKUP.get(_field_token(k)) == "period" for k in row):
                    row = {**row, "period": default_period}
                rec = _normalize_record(row, source)
                if rec:
                    records.append(rec)
            return records, None
    except Exception as exc:
        return [], f"{path.name}: {exc}"
    return [], None


def _candidate_record_files() -> list[Path]:
    files: list[Path] = []
    configured = os.environ.get(RECORDS_FILE_ENV, "").strip()
    if configured:
        files.append(Path(configured))

    if DATA_DIR.exists():
        blocked_terms = ("state", "storage", "cookie", "session", "auth")
        for suffix in ("*.csv", "*.json"):
            for path in DATA_DIR.glob(suffix):
                stem = path.stem.lower()
                if any(term in stem for term in blocked_terms):
                    continue
                files.append(path)

    seen: set[str] = set()
    unique: list[Path] = []
    for path in files:
        resolved = str(path)
        if resolved not in seen:
            unique.append(path)
            seen.add(resolved)
    return unique


def _load_file_records() -> tuple[list[dict[str, Any]], list[str], list[str]]:
    records: list[dict[str, Any]] = []
    loaded_files: list[str] = []
    errors: list[str] = []

    for path in _candidate_record_files():
        if not path.exists():
            errors.append(f"{path}: file not found")
            continue
        batch, error = _read_records_from_file(path)
        if error:
            errors.append(error)
        if batch:
            records.extend(batch)
            loaded_files.append(str(path))
    return records, loaded_files, errors


def _collect_records(state: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    state_records = []
    for row in state.get("records") or []:
        if isinstance(row, dict):
            rec = _normalize_record(row, "state")
            if rec:
                state_records.append(rec)

    file_records, loaded_files, errors = _load_file_records()
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in [*state_records, *file_records]:
        merged[(rec["period"], rec["brand_key"])] = rec

    records = list(merged.values())
    records.sort(key=lambda r: (_period_sort_key(r.get("period")), r.get("rank") or 999, r.get("brand") or ""))
    return records, {
        "state_record_count": len(state_records),
        "file_record_count": len(file_records),
        "loaded_files": loaded_files,
        "load_errors": errors[:6],
    }


def _periods(records: list[dict[str, Any]]) -> list[str]:
    values = sorted({r["period"] for r in records if r.get("period")}, key=_period_sort_key)
    return values


def _rank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = [dict(row) for row in rows]
    ranked.sort(key=lambda r: (r.get("market_share") is None, -(r.get("market_share") or 0), -(r.get("shipments") or 0)))
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return ranked


def _rows_for_period(records: list[dict[str, Any]], period: str | None) -> list[dict[str, Any]]:
    if not period:
        return []
    return _rank_rows([r for r in records if r.get("period") == period])


def _latest_rows(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    periods = _periods(records)
    if not periods:
        return []

    latest = periods[-1]
    previous = periods[-2] if len(periods) >= 2 else None
    rows = _rows_for_period(records, latest)
    previous_rows = {r["brand_key"]: r for r in _rows_for_period(records, previous)} if previous else {}

    for row in rows:
        prev = previous_rows.get(row["brand_key"])
        if not prev:
            row["share_delta"] = None
            row["rank_delta"] = None
            continue
        row["share_delta"] = (
            round(row["market_share"] - prev["market_share"], 2)
            if row.get("market_share") is not None and prev.get("market_share") is not None
            else None
        )
        row["rank_delta"] = prev.get("rank") - row.get("rank") if prev.get("rank") and row.get("rank") else None
    return rows


def _find_asus(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        if row.get("brand_key") == "asus":
            return row
    return None


def _build_alerts(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    asus = _find_asus(rows)

    for row in rows:
        brand = row.get("brand") or "Unknown"
        yoy = row.get("yoy")
        qoq = row.get("qoq")
        share_delta = row.get("share_delta")
        is_asus = row.get("brand_key") == "asus"

        if not is_asus and yoy is not None and yoy >= 8:
            alerts.append({"level": "watch", "brand": brand, "metric": "YoY", "message": f"{brand} 年增 {yoy:.1f}%，需檢視是否帶動市占壓力。"})
        elif not is_asus and yoy is not None and yoy <= -8:
            alerts.append({"level": "opportunity", "brand": brand, "metric": "YoY", "message": f"{brand} 年增 {yoy:.1f}%，可追蹤通路或產品線缺口。"})

        if not is_asus and qoq is not None and qoq >= 5:
            alerts.append({"level": "watch", "brand": brand, "metric": "QoQ", "message": f"{brand} 季/月增 {qoq:.1f}%，短期動能偏強。"})
        elif not is_asus and qoq is not None and qoq <= -5:
            alerts.append({"level": "opportunity", "brand": brand, "metric": "QoQ", "message": f"{brand} 季/月增 {qoq:.1f}%，短期動能轉弱。"})

        if not is_asus and share_delta is not None and share_delta >= 0.8:
            alerts.append({"level": "watch", "brand": brand, "metric": "Share", "message": f"{brand} 市占增加 {share_delta:.1f} 個百分點。"})

    if asus:
        if asus.get("rank") and asus["rank"] > 4:
            alerts.append({"level": "risk", "brand": asus.get("brand", "ASUS"), "metric": "Rank", "message": f"ASUS 最新排名為第 {asus['rank']}，已低於前四名門檻。"})
        if asus.get("share_delta") is not None and asus["share_delta"] <= -0.8:
            alerts.append({"level": "risk", "brand": asus.get("brand", "ASUS"), "metric": "Share", "message": f"ASUS 市占下降 {abs(asus['share_delta']):.1f} 個百分點。"})

    level_order = {"risk": 0, "watch": 1, "opportunity": 2}
    alerts.sort(key=lambda a: (level_order.get(a.get("level"), 9), a.get("brand") or ""))
    return alerts[:10]


def _pct(value: Any) -> str:
    numeric = _float_or_none(value)
    return f"{numeric:.1f}%" if numeric is not None else "--"


def _build_ai_brief(state: dict[str, Any], rows: list[dict[str, Any]], meta: dict[str, Any]) -> list[str]:
    if not rows:
        return [
            "尚未匯入 DIGITIMES 筆電品牌資料；戰情室目前停在 connector 與資料狀態監控。",
            f"可將授權下載後的 CSV/JSON 放在 {DATA_DIR.as_posix()}，或用 {RECORDS_FILE_ENV} 指向單一匯入檔。",
            "匯入後會自動計算品牌排名、市占變化、ASUS 位置與競品異動警示。",
        ]

    latest = rows[0].get("period")
    leader = rows[0]
    asus = _find_asus(rows)
    brief = [
        f"最新期別 {latest}，目前第一名為 {leader.get('brand')}（市占 {_pct(leader.get('market_share'))}）。"
    ]
    if asus:
        gap = (
            leader.get("market_share") - asus.get("market_share")
            if leader.get("market_share") is not None and asus.get("market_share") is not None
            else None
        )
        gap_text = f"，落後第一名 {gap:.1f} 個百分點" if gap is not None and asus is not leader else ""
        trend_text = ""
        if asus.get("share_delta") is not None:
            trend_text = f"，較前期 {'增加' if asus['share_delta'] >= 0 else '減少'} {abs(asus['share_delta']):.1f} 個百分點"
        brief.append(f"ASUS 排名第 {asus.get('rank')}，市占 {_pct(asus.get('market_share'))}{gap_text}{trend_text}。")
    else:
        brief.append("最新資料未辨識到 ASUS/華碩品牌列，請確認匯入欄位或品牌命名。")

    alerts = _build_alerts(rows)
    if alerts:
        brief.append(f"系統偵測到 {len(alerts)} 則競品/ASUS 異動，優先檢視紅色風險與橘色觀察項。")
    else:
        brief.append("目前沒有超過門檻的競品異動，仍建議對照產品線與通路庫存變化。")

    return brief


def _summary(records: list[dict[str, Any]], rows: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    periods = _periods(records)
    latest_period = periods[-1] if periods else None
    leader = rows[0] if rows else None
    asus = _find_asus(rows)
    top3_values = [r.get("market_share") for r in rows[:3] if r.get("market_share") is not None]
    top3_share = sum(top3_values) if top3_values else None
    asus_gap = (
        round((leader.get("market_share") or 0) - (asus.get("market_share") or 0), 2)
        if leader and asus and leader.get("market_share") is not None and asus.get("market_share") is not None
        else None
    )
    return {
        "latest_period": latest_period,
        "period_count": len(periods),
        "brand_count": len(rows),
        "record_count": len(records),
        "state_record_count": meta.get("state_record_count", 0),
        "file_record_count": meta.get("file_record_count", 0),
        "leader_brand": leader.get("brand") if leader else None,
        "leader_market_share": leader.get("market_share") if leader else None,
        "asus_rank": asus.get("rank") if asus else None,
        "asus_market_share": asus.get("market_share") if asus else None,
        "asus_share_gap": asus_gap,
        "asus_share_delta": asus.get("share_delta") if asus else None,
        "asus_rank_delta": asus.get("rank_delta") if asus else None,
        "top3_share": round(top3_share, 2) if top3_share is not None else None,
        "data_source": "file" if meta.get("file_record_count") else ("state" if meta.get("state_record_count") else "none"),
    }


def build_war_room_payload() -> dict[str, Any]:
    state = _read_state()
    records, meta = _collect_records(state)
    rows = _latest_rows(records)
    credentials = _credential_status()
    connector = state.get("connector") or {}

    if rows:
        status = "ready"
    elif credentials["has_storage_state"] or (credentials["has_username"] and credentials["has_password"]):
        status = "ready_for_probe"
    else:
        status = "not_configured"

    connector = {
        **connector,
        "status": status,
        "refresh_day": _refresh_day(),
        "next_scheduled_date": _next_monthly_run(),
        "credentials": credentials,
        "state_file": str(STATE_FILE),
        "records_file_env": RECORDS_FILE_ENV,
        "loaded_files": meta.get("loaded_files", []),
        "load_errors": meta.get("load_errors", []),
    }

    return {
        "ok": True,
        "generated_at": _iso_now(),
        "source": state.get("source") or {},
        "connector": connector,
        "summary": _summary(records, rows, meta),
        "periods": _periods(records),
        "ai_brief": _build_ai_brief(state, rows, meta),
        "leaders": rows[:6],
        "asus": _find_asus(rows),
        "alerts": _build_alerts(rows),
        "latest_rows": rows,
        "charts": _CHART_DATA_CACHE.get("latest") or {},
        "probe_checklist": [
            "確認 DIGITIMES 授權允許內部月更擷取與資料留存。",
            f"將匯出的 CSV/JSON 放入 {DATA_DIR.as_posix()}，或設定 {RECORDS_FILE_ENV}。",
            "欄位需包含 period、brand、market_share；shipments、yoy、qoq 可選。",
            "完成首次人工匯入後，再接 Playwright login/download probe。",
        ],
    }


def run_monthly_refresh(force: bool = False) -> dict[str, Any]:
    """Record a refresh attempt and return the current connector state.

    The authorized Playwright login/download implementation is intentionally
    not run yet. This endpoint verifies scheduling, credentials, and whether
    locally exported records are already readable.
    """
    state = _read_state()
    records, meta = _collect_records(state)
    credentials = _credential_status()
    connector = state.get("connector") or {}
    connector["last_attempted_at"] = _iso_now()
    connector["refresh_day"] = _refresh_day()
    connector["next_scheduled_date"] = _next_monthly_run()

    if not force:
        today = _now_tw().date()
        if today.isoformat() != connector["next_scheduled_date"]:
            connector["last_error"] = "not scheduled today"
            state["connector"] = connector
            _write_state(state)
            return {"ok": False, "message": "Monthly refresh is not scheduled for today.", "connector": connector}

    if records:
        connector["status"] = "ready"
        connector["last_success_at"] = _iso_now()
        connector["last_error"] = None
        connector["loaded_files"] = meta.get("loaded_files", [])
        state["connector"] = connector
        _write_state(state)
        return {
            "ok": True,
            "message": f"Loaded {len(records)} DIGITIMES competitor records from local state/files.",
            "connector": connector,
        }

    if not (credentials["has_storage_state"] or (credentials["has_username"] and credentials["has_password"])):
        connector["status"] = "not_configured"
        connector["last_error"] = "missing DIGITIMES credentials, storage state, or local records file"
        state["connector"] = connector
        _write_state(state)
        return {
            "ok": False,
            "message": "Set DIGITIMES credentials/storage state, or provide a local CSV/JSON records file.",
            "connector": connector,
        }

    connector["status"] = "ready_for_probe"
    connector["last_error"] = "login/download probe not implemented yet"
    state["connector"] = connector
    _write_state(state)
    return {
        "ok": False,
        "message": "Connector is configured. Next step: implement the authorized Playwright login/download probe.",
        "connector": connector,
    }
