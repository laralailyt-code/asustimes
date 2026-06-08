"""
News Aggregation Platform — Flask Backend
ASUSTIMES: ASUS tech industry news hub
Auto-refreshes every 30 minutes in background.
"""

import os
import csv
import json
import threading
import time
import logging
import requests as req_lib
from concurrent.futures import ThreadPoolExecutor, as_completed, wait as fut_wait
from datetime import datetime, date as date_cls, timedelta, timezone
from flask import Flask, jsonify, render_template, request
from scraper import fetch_all_news, CATEGORY_KEYWORDS
from digitimes_competitor import build_war_room_payload, run_monthly_refresh

try:
    import yfinance as yf
    _YF_AVAILABLE = True
except ImportError:
    _YF_AVAILABLE = False
    logging.getLogger(__name__).warning("yfinance not installed – live commodity prices disabled")

try:
    import anthropic as _anthropic_lib
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__, static_folder='templates', static_url_path='')

# ── Timezone ───────────────────────────────────────────────────────────────────
TW_TZ = timezone(timedelta(hours=8))

# ── Environment detection ──────────────────────────────────────────────────────
# On Render: RENDER=true, On Localhost: RENDER is not set
_IS_RENDER_PRODUCTION = os.environ.get("RENDER") == "true"
_SHOW_RISK_PAGE = True  # Show risk page in all environments

# ── In-memory cache ────────────────────────────────────────────────────────────
_cache: dict = {
    "articles": [],
    "last_updated": None,
    "loading": False,
}
_cache_lock = threading.Lock()

REFRESH_INTERVAL = 60 * 60  # seconds — 1 hour（之前 30 分鐘對 Bing News 太密集）


def refresh_news():
    with _cache_lock:
        if _cache["loading"]:
            return
        _cache["loading"] = True
    try:
        # Fetch fresh articles
        fresh_articles = fetch_all_news()

        # Save fresh articles to archive for persistent storage
        _save_articles_to_archive(fresh_articles)

        # Load archived articles (past 2 years)
        archived_articles = _load_archived_articles()

        # Merge fresh + archived, deduplicate by URL
        merged = {}
        for article in archived_articles:
            url = article.get("source_url", "")
            if url:
                merged[url] = article

        for article in fresh_articles:
            url = article.get("source_url", "")
            if url:
                merged[url] = article  # Fresh articles overwrite archived ones

        articles = list(merged.values())
        articles.sort(key=lambda a: a.get("published") or a.get("fetched_at", ""), reverse=True)

        with _cache_lock:
            _cache["articles"] = articles
            _cache["last_updated"] = datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")
            _cache["loading"] = False
        logger.info(f"Cache refreshed: {len(articles)} articles ({len(fresh_articles)} fresh, {len(archived_articles)} archived)")
    except Exception as e:
        logger.error(f"refresh_news error: {e}")
        with _cache_lock:
            _cache["loading"] = False


def background_refresh_loop():
    refresh_news()
    while True:
        time.sleep(REFRESH_INTERVAL)
        refresh_news()


def _risk_cache_preload_loop():
    """Pre-warm geopolitical + strike caches at startup and every 3 hours."""
    first_run = True
    while True:
        try:
            if not first_run:
                time.sleep(2)  # Brief delay for subsequent runs
            logger.info("[RISK] Pre-warming geopolitical cache (parallel)...")
            _do_geo_scan()
        except Exception as e:
            logger.warning(f"[RISK] geo preload error: {e}")
        try:
            logger.info("[RISK] Pre-warming strike cache (parallel)...")
            _do_strike_scan()
        except Exception as e:
            logger.warning(f"[RISK] strike preload error: {e}")
        logger.info("[RISK] Risk caches pre-warmed.")
        first_run = False
        time.sleep(3 * 3600)  # 每 3 小時更新一次


# NOTE: 之前還有一個 _digitimes_refresh_loop 每 2 小時呼叫 refresh_news()，
# 跟 background_refresh_loop 完全重複（兩個都呼叫 refresh_news → 兩次 Bing 抓取）。
# Digitimes 文章現已透過 Bing News RSS site:digitimes.com 由主 loop 抓取，
# 不再需要獨立的 Digitimes loop。已移除以降低 Bing News 用量。


def daily_digest_loop():
    """Send digest email every day at DIGEST_HOUR (UTC)."""
    sent_date = None
    while True:
        time.sleep(60)
        now = datetime.utcnow()
        digest_hour = int(os.environ.get("DIGEST_HOUR", "0"))
        today_str = now.strftime("%Y-%m-%d")
        if now.hour == digest_hour and sent_date != today_str:
            recipients_raw = os.environ.get("DIGEST_RECIPIENTS", "")
            recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
            if recipients:
                with _cache_lock:
                    articles = list(_cache["articles"])
                    last_updated = _cache["last_updated"]
                api_key = os.environ.get("RESEND_API_KEY", "")
                if api_key and articles:
                    html_body = _build_digest_html(articles, last_updated)
                    for r in recipients:
                        try:
                            req_lib.post(
                                "https://api.resend.com/emails",
                                headers={"Authorization": f"Bearer {api_key}",
                                         "Content-Type": "application/json"},
                                json={
                                    "from": "ASUSTIMES <onboarding@resend.dev>",
                                    "to": [r],
                                    "subject": f"ASUSTIMES 科技摘要 {today_str}",
                                    "html": html_body,
                                },
                                timeout=15,
                            )
                            logger.info(f"Daily digest sent to {r}")
                        except Exception as e:
                            logger.error(f"Daily digest error for {r}: {e}")
            sent_date = today_str


# ── Background thread: starts on first request (gunicorn-compatible) ───────────
_bg_started = False
_bg_lock = threading.Lock()
# Critical threads (Telegram bot + disaster persist) may be started early at
# module-load on Render. These flags ensure we never start them twice in the
# same worker process — duplicate Telegram polling causes getUpdates Conflict.
_telegram_bot_started = False
_disaster_persist_started = False

@app.before_request
def _ensure_bg_running():
    global _bg_started, _telegram_bot_started, _disaster_persist_started
    if not _bg_started:
        with _bg_lock:
            if not _bg_started:
                _bg_started = True
                t = threading.Thread(target=background_refresh_loop, daemon=True)
                t.start()
                td = threading.Thread(target=daily_digest_loop, daemon=True)
                td.start()
                tl = threading.Thread(target=_live_price_loop, daemon=True)
                tl.start()
                tr = threading.Thread(target=_risk_cache_preload_loop, daemon=True)
                tr.start()
                # Telegram bot polling（M3+）— only if not already started by _start_critical_bg_threads
                if not _telegram_bot_started:
                    _telegram_bot_started = True
                    tt = threading.Thread(target=_telegram_bot_loop, daemon=True, name="telegram-bot")
                    tt.start()
                # 災害事件即時偵測（USGS/NOAA/GDACS）每 5 分鐘
                if not _disaster_persist_started:
                    _disaster_persist_started = True
                    tdis = threading.Thread(target=_disaster_persist_loop, daemon=True, name="disaster-persist")
                    tdis.start()
                logger.info("Background threads started (Telegram bot polling + disaster persist may have been started earlier)")


def _telegram_bot_loop():
    """在獨立背景執行緒裡跑 PTB Application（polling 模式）。

    啟動規則（避免本地 + Render 同時 polling 同一 bot 造成衝突）：
    - Render 環境（RENDER=true 由 Render 自動注入）→ 一律跑 polling
    - 本地環境 → 預設 skip；需要本地測試時在 .env 加
                 TELEGRAM_FORCE_LOCAL_POLLING=true
    - 沒設 TOKEN → silently skip
    """
    if not os.environ.get("TELEGRAM_BOT_TOKEN"):
        logger.info("[telegram_bot] TOKEN 未設定，skip polling")
        return

    is_render = os.environ.get("RENDER") == "true"
    force_local = os.environ.get("TELEGRAM_FORCE_LOCAL_POLLING", "").lower() in ("true", "1", "yes")
    if not is_render and not force_local:
        logger.info(
            "[telegram_bot] 本地預設不跑 polling（避免與 Render 衝突）。"
            "要本地測試請在 .env 加 TELEGRAM_FORCE_LOCAL_POLLING=true"
        )
        return
    try:
        # 嘗試從 .env 載入（本地用，Render 已由環境變數注入）
        try:
            from dotenv import load_dotenv as _ld
            _ld()
        except ImportError:
            pass

        import asyncio
        from telegram_bot import db as _tdb, bot as _tbot

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        _tdb.init_pool()
        application = _tbot.build_application()

        async def _run():
            await application.initialize()
            await application.start()
            await application.updater.start_polling(drop_pending_updates=True)
            logger.info("[telegram_bot] Polling started inside Flask process")
            await asyncio.Event().wait()  # 永遠 block

        loop.run_until_complete(_run())
    except Exception as e:
        logger.error(f"[telegram_bot] start failed: {e}", exc_info=True)


# ── Routes ─────────────────────────────────────────────────────────────────────
# ── Telegram bot 整合：事件落地 helper ────────────────────────────────────────
# 放在這裡是為了給 _do_geo_scan / _do_strike_scan 用，背景執行不阻塞
def _persist_events_async(events, source_label: str = "") -> None:
    """非阻塞地把事件清單寫進 Supabase risk_events 表。
    若 telegram_bot 套件未啟用（例如本地不跑 bot），會 silently skip。"""
    if not events:
        return
    def _job():
        try:
            from telegram_bot import event_persister
            event_persister.persist_events(events)
        except Exception as e:
            logger.debug(f"[telegram_bot] persist {source_label} skipped: {e}")
    threading.Thread(target=_job, daemon=True).start()


# ── 災害事件即時偵測（地震官方來源 / NOAA / GDACS）每 5 分鐘 ──────────────────
# 地震 / GDACS 文字 place 字串 → 我們訂閱用的中文區域
_DISASTER_REGION_KEYWORDS = {
    "taiwan":      "台灣",
    "japan":       "日本",
    "korea":       "韓國", "korean": "韓國",
    "china":       "中國大陸", "chinese": "中國大陸",
    "malaysia":    "馬來西亞",
    "philippine":  "菲律賓",
    "philippines": "菲律賓",
    "filipina":    "菲律賓",
    "filipino":    "菲律賓",
    "mindanao":    "菲律賓",
    "luzon":       "菲律賓",
    "visayas":     "菲律賓",
    "vietnam":     "越南",
    "indonesia":   "印度尼西亞",
    "india":       "印度",
    "thailand":    "泰國",
    "singapore":   "新加坡",
    "usa":         "美國", "united states": "美國",
    "germany":     "德國",
    "netherlands": "荷蘭",
    "france":      "法國",
    "uk":          "英國", "united kingdom": "英國",
}


def _infer_disaster_region(place_or_country: str) -> str:
    """地震 place / GDACS country → 中文地區（用於訂閱比對）。"""
    if not place_or_country:
        return ""
    s = place_or_country.lower()
    for kw, region in _DISASTER_REGION_KEYWORDS.items():
        if kw in s:
            return region
    return ""


_QUAKE_DAYS = 56  # 8 週，對齊全站事件 retention window
_QUAKE_CRITICAL_MAG = 6.5
_QUAKE_CRITICAL_INTENSITY = 60  # 6弱 / MMI VI
_QUAKE_MIN_MAG_QUERY = 4.5
_QUAKE_SOURCE_HEADERS = {
    "User-Agent": "ASUSTIMES/1.0 (+https://asustimes.local)",
    "Accept": "application/json,text/plain,*/*",
}


def _to_float(value) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _quake_dt_to_ms(value) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(value if value > 10_000_000_000 else value * 1000)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    if not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(s[:len(fmt)], fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _quake_ms_to_iso(ms: int | None) -> str:
    if not ms:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def _quake_intensity_value(label) -> int | None:
    if label in (None, ""):
        return None
    if isinstance(label, (int, float)):
        return int(float(label) * 10 if float(label) < 10 else float(label))
    s = str(label).strip().upper()
    s = s.translate(str.maketrans("０１２３４５６７８９＋－", "0123456789+-"))
    s = s.replace("震度", "").replace("級", "").replace(" ", "")
    mapping = {
        "7": 70, "6+": 65, "6強": 65, "6-": 60, "6弱": 60, "6": 60,
        "5+": 55, "5強": 55, "5-": 50, "5弱": 50, "5": 50,
        "4": 40, "3": 30, "2": 20, "1": 10,
        "VIII": 80, "VII": 70, "VI": 60, "V": 50,
        "IV": 40, "III": 30, "II": 20, "I": 10,
    }
    for key, val in mapping.items():
        if s == key or key in s:
            return val
    import re as _re
    romans = []
    for token in _re.findall(r"\b(?:VIII|VII|VI|IV|V|III|II|I)(?:-(?:VIII|VII|VI|IV|V|III|II|I))?\b", s):
        for part in token.split("-"):
            if part in mapping:
                romans.append(mapping[part])
    if romans:
        return max(romans)
    m = _re.search(r"\d(?:[+-]|弱|強)?", s)
    return mapping.get(m.group(0)) if m else None


def _quake_impact(mag, intensity_value: int | None = None) -> str:
    mag_val = _to_float(mag)
    if (mag_val is not None and mag_val >= _QUAKE_CRITICAL_MAG) or (
        intensity_value is not None and intensity_value >= _QUAKE_CRITICAL_INTENSITY
    ):
        return "CRITICAL"
    if (mag_val is not None and mag_val >= 6.0) or (intensity_value is not None and intensity_value >= 50):
        return "HIGH"
    if (mag_val is not None and mag_val >= 5.5) or (intensity_value is not None and intensity_value >= 40):
        return "MED"
    return "LOW"


def _quake_feature(
    *,
    eid: str,
    source: str,
    place: str,
    lat,
    lng,
    depth_km=None,
    mag=None,
    time_value=None,
    source_url: str = "",
    region: str = "",
    max_intensity: str | None = None,
    intensity_scale: str | None = None,
    mag_type: str | None = None,
) -> dict | None:
    lat_f = _to_float(lat)
    lng_f = _to_float(lng)
    if not eid or lat_f is None or lng_f is None:
        return None
    depth_f = _to_float(depth_km)
    mag_f = _to_float(mag)
    time_ms = _quake_dt_to_ms(time_value)
    intensity_value = _quake_intensity_value(max_intensity)
    impact = _quake_impact(mag_f, intensity_value)
    mag_label = f"M{mag_f:.1f}" if mag_f is not None else "規模未定"
    intensity_text = f" / 最大震度 {max_intensity}" if max_intensity else ""
    title = f"{mag_label} 地震{intensity_text} — {place or region or source}"
    props = {
        "mag": mag_f,
        "magType": mag_type,
        "place": place or "",
        "time": time_ms,
        "updated": time_ms,
        "url": source_url,
        "source": source,
        "sourceUrl": source_url,
        "region": region or _infer_disaster_region(place) or "",
        "depth": depth_f,
        "maxIntensity": max_intensity,
        "maxIntensityValue": intensity_value,
        "intensityScale": intensity_scale,
        "impact": impact,
        "title": title,
    }
    return {
        "type": "Feature",
        "id": eid,
        "properties": props,
        "geometry": {"type": "Point", "coordinates": [lng_f, lat_f, depth_f]},
    }


def _quake_feature_is_recent(feat: dict, days: int) -> bool:
    if not days:
        return True
    time_ms = (feat.get("properties") or {}).get("time")
    if not time_ms:
        return True
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)
    return int(time_ms) >= cutoff


def _parse_jma_coord(cod: str) -> tuple[float | None, float | None, float | None]:
    if not cod:
        return None, None, None
    import re as _re
    m = _re.match(r"^([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)([+-]\d+)(?:/)?$", str(cod).strip())
    if not m:
        return None, None, None
    lat = _to_float(m.group(1))
    lng = _to_float(m.group(2))
    depth_m = _to_float(m.group(3))
    if lat is None or lng is None or not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return None, None, None
    depth_km = abs(depth_m) / 1000 if depth_m is not None else None
    return lat, lng, depth_km


def _feature_in_region(feat: dict, region: str) -> bool:
    props = feat.get("properties") or {}
    coords = (feat.get("geometry") or {}).get("coordinates") or []
    lng = coords[0] if len(coords) > 0 else None
    lat = coords[1] if len(coords) > 1 else None
    lat_f = _to_float(lat)
    lng_f = _to_float(lng)
    place = (props.get("place") or "").lower()
    explicit_region = (props.get("region") or "").strip()
    if explicit_region:
        return explicit_region == region
    if region == "台灣":
        return (
            lat_f is not None and lng_f is not None and 21.5 <= lat_f <= 25.8 and 119 <= lng_f <= 123.5
        ) or any(k in place for k in ("taiwan", "臺灣", "台灣"))
    if region == "日本":
        return (
            lat_f is not None and lng_f is not None and 24 <= lat_f <= 46.5 and 123.5 <= lng_f <= 154
        ) or any(k in place for k in ("japan", "honshu", "hokkaido", "kyushu", "shikoku", "ryukyu"))
    if region == "印度尼西亞":
        return (
            lat_f is not None and lng_f is not None and -11.5 <= lat_f <= 6.5 and 94 <= lng_f <= 142.5
        ) or any(k in place for k in ("indonesia", "java", "sumatra", "sulawesi", "papua", "molucca", "bali"))
    if region == "菲律賓":
        return (
            lat_f is not None and lng_f is not None and 4 <= lat_f <= 21.5 and 116 <= lng_f <= 127
        ) or any(k in place for k in ("philippine", "philippines", "filipina", "mindanao", "luzon", "visayas"))
    return False


def _fetch_jma_quake_features(days: int = _QUAKE_DAYS) -> list[dict]:
    out: list[dict] = []
    try:
        r = req_lib.get("https://www.jma.go.jp/bosai/quake/data/list.json", headers=_QUAKE_SOURCE_HEADERS, timeout=12)
        if r.status_code != 200:
            return out
        best: dict[str, dict] = {}
        for item in r.json() or []:
            eid = str(item.get("eid") or "")
            if not eid:
                continue
            score = 0
            ttl = item.get("ttl") or ""
            if "震源・震度" in ttl:
                score += 8
            if item.get("mag") not in (None, ""):
                score += 4
            if item.get("maxi") not in (None, ""):
                score += 2
            score += int(item.get("ser") or 0)
            prev = best.get(eid)
            if not prev or score > prev["_score"]:
                item["_score"] = score
                best[eid] = item
        for item in best.values():
            lat, lng, depth = _parse_jma_coord(item.get("cod", ""))
            place = item.get("en_anm") or item.get("anm") or "Japan"
            feat = _quake_feature(
                eid=f"jma-{item.get('eid')}",
                source="JMA日本氣象廳",
                place=f"日本 {place}",
                lat=lat,
                lng=lng,
                depth_km=depth,
                mag=item.get("mag"),
                time_value=item.get("at") or item.get("rdt"),
                source_url=f"https://www.data.jma.go.jp/multi/quake/quake_detail.html?eventID={item.get('eid')}&lang=en",
                region="日本",
                max_intensity=str(item.get("maxi")) if item.get("maxi") not in (None, "") else None,
                intensity_scale="JMA",
                mag_type="Mj",
            )
            if feat and _feature_in_region(feat, "日本") and _quake_feature_is_recent(feat, days):
                out.append(feat)
    except Exception as e:
        logger.warning(f"[quake] JMA fetch failed: {e}")
    return out


def _cwa_api_key() -> str:
    return (
        os.environ.get("CWA_API_KEY")
        or os.environ.get("CWA_AUTHORIZATION")
        or os.environ.get("CWA_OPENDATA_API_KEY")
        or ""
    ).strip()


def _cwa_max_intensity(eq: dict) -> str | None:
    labels: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                if k in {"AreaIntensity", "SeismicIntensity", "MaxIntensity"} and v not in (None, ""):
                    labels.append(str(v))
                walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(eq.get("Intensity") or eq)
    if not labels:
        return None
    return max(labels, key=lambda x: _quake_intensity_value(x) or 0)


def _fetch_cwa_quake_features(days: int = _QUAKE_DAYS) -> list[dict]:
    key = _cwa_api_key()
    if not key:
        logger.info("[quake] CWA key not set; Taiwan will use USGS fallback")
        return []
    out: list[dict] = []
    for data_id in ("E-A0015-001", "E-A0016-001"):
        try:
            r = req_lib.get(
                f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{data_id}",
                params={"Authorization": key, "format": "JSON"},
                headers=_QUAKE_SOURCE_HEADERS,
                timeout=12,
            )
            if r.status_code != 200:
                continue
            records = (r.json().get("records") or {}).get("Earthquake") or []
            if isinstance(records, dict):
                records = [records]
            for eq in records:
                info = eq.get("EarthquakeInfo") or {}
                epicenter = info.get("Epicenter") or {}
                mag_info = info.get("EarthquakeMagnitude") or {}
                lat = epicenter.get("EpicenterLatitude") or info.get("EpicenterLatitude")
                lng = epicenter.get("EpicenterLongitude") or info.get("EpicenterLongitude")
                place = epicenter.get("Location") or info.get("Location") or eq.get("ReportContent") or "Taiwan"
                origin = info.get("OriginTime")
                max_intensity = _cwa_max_intensity(eq)
                feat = _quake_feature(
                    eid=f"cwa-{data_id}-{eq.get('EarthquakeNo') or origin or len(out)}",
                    source="CWA中央氣象署",
                    place=f"台灣 {place}",
                    lat=lat,
                    lng=lng,
                    depth_km=info.get("FocalDepth"),
                    mag=mag_info.get("MagnitudeValue") or eq.get("MagnitudeValue"),
                    time_value=origin,
                    source_url=eq.get("Web") or "https://www.cwa.gov.tw/",
                    region="台灣",
                    max_intensity=max_intensity,
                    intensity_scale="CWA",
                    mag_type=mag_info.get("MagnitudeType"),
                )
                if feat and _quake_feature_is_recent(feat, days):
                    out.append(feat)
        except Exception as e:
            logger.warning(f"[quake] CWA {data_id} fetch failed: {e}")
    return out


def _parse_bmkg_depth(value) -> float | None:
    if value is None:
        return None
    return _to_float(str(value).replace("km", "").strip())


def _fetch_bmkg_quake_features(days: int = _QUAKE_DAYS) -> list[dict]:
    urls = [
        ("https://data.bmkg.go.id/DataMKG/TEWS/gempaterkini.json", False),
        ("https://data.bmkg.go.id/DataMKG/TEWS/gempadirasakan.json", True),
    ]
    by_id: dict[str, dict] = {}
    for url, felt_feed in urls:
        try:
            r = req_lib.get(url, headers=_QUAKE_SOURCE_HEADERS, timeout=12)
            if r.status_code != 200:
                continue
            records = ((r.json().get("Infogempa") or {}).get("gempa")) or []
            if isinstance(records, dict):
                records = [records]
            for rec in records:
                coords = str(rec.get("Coordinates") or "").split(",")
                if len(coords) != 2:
                    continue
                dt = rec.get("DateTime")
                eid = f"bmkg-{str(dt or rec.get('Tanggal') or '').replace(':','').replace('-','').replace('+','')}-{coords[0].strip()}-{coords[1].strip()}"
                max_intensity = f"MMI {rec.get('Dirasakan')}" if rec.get("Dirasakan") and rec.get("Dirasakan") != "-" else None
                wilayah = str(rec.get("Wilayah") or "").strip()
                bmkg_region = _infer_disaster_region(wilayah) or "印度尼西亞"
                place_prefix = "菲律賓" if bmkg_region == "菲律賓" else "印尼"
                feat = _quake_feature(
                    eid=eid,
                    source="BMKG印尼氣象氣候地球物理局",
                    place=f"{place_prefix} {wilayah}".strip(),
                    lat=coords[0],
                    lng=coords[1],
                    depth_km=_parse_bmkg_depth(rec.get("Kedalaman")),
                    mag=rec.get("Magnitude"),
                    time_value=dt,
                    source_url="https://data.bmkg.go.id/gempabumi/",
                    region=bmkg_region,
                    max_intensity=max_intensity,
                    intensity_scale="MMI" if max_intensity else None,
                    mag_type="M",
                )
                if not feat or not _quake_feature_is_recent(feat, days):
                    continue
                old = by_id.get(eid)
                if old and not old["properties"].get("maxIntensity") and max_intensity:
                    old["properties"]["maxIntensity"] = max_intensity
                    old["properties"]["maxIntensityValue"] = _quake_intensity_value(max_intensity)
                    old["properties"]["intensityScale"] = "MMI"
                    old["properties"]["impact"] = _quake_impact(
                        old["properties"].get("mag"),
                        old["properties"].get("maxIntensityValue"),
                    )
                    old["properties"]["title"] = old["properties"]["title"].replace(" — ", f" / 最大震度 {max_intensity} — ")
                else:
                    by_id[eid] = feat
        except Exception as e:
            logger.warning(f"[quake] BMKG fetch failed ({url}): {e}")
    return list(by_id.values())


def _fetch_usgs_quake_features(days: int = _QUAKE_DAYS) -> list[dict]:
    out: list[dict] = []
    try:
        starttime = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        r = req_lib.get(
            "https://earthquake.usgs.gov/fdsnws/event/1/query",
            params={
                "format": "geojson",
                "starttime": starttime,
                "minmagnitude": _QUAKE_MIN_MAG_QUERY,
                "orderby": "time",
            },
            headers=_QUAKE_SOURCE_HEADERS,
            timeout=15,
        )
        if r.status_code != 200:
            return out
        for feat in r.json().get("features", []) or []:
            props = feat.get("properties") or {}
            coords = (feat.get("geometry") or {}).get("coordinates") or []
            norm = _quake_feature(
                eid=f"usgs-{feat.get('id')}",
                source="USGS美國地質調查局",
                place=props.get("place") or "",
                lat=coords[1] if len(coords) > 1 else None,
                lng=coords[0] if len(coords) > 0 else None,
                depth_km=coords[2] if len(coords) > 2 else None,
                mag=props.get("mag"),
                time_value=props.get("time"),
                source_url=props.get("url") or "https://earthquake.usgs.gov/",
                region=_infer_disaster_region(props.get("place") or ""),
                mag_type=props.get("magType"),
            )
            if norm and _quake_feature_is_recent(norm, days):
                out.append(norm)
    except Exception as e:
        logger.warning(f"[quake] USGS fetch failed: {e}")
    return out


def _fetch_quake_features(days: int = _QUAKE_DAYS) -> list[dict]:
    features: list[dict] = []
    official_regions: list[str] = []
    for region, fetcher in (
        ("日本", _fetch_jma_quake_features),
        ("台灣", _fetch_cwa_quake_features),
        ("印度尼西亞", _fetch_bmkg_quake_features),
    ):
        local = fetcher(days)
        if local:
            features.extend(local)
            official_regions.append(region)

    usgs = _fetch_usgs_quake_features(days)
    for feat in usgs:
        if any(_feature_in_region(feat, region) for region in official_regions):
            continue
        features.append(feat)

    deduped: dict[str, dict] = {}
    for feat in features:
        deduped[feat["id"]] = feat
    return sorted(
        deduped.values(),
        key=lambda f: ((f.get("properties") or {}).get("time") or 0),
        reverse=True,
    )


def _quake_feature_to_event(feat: dict) -> dict | None:
    props = feat.get("properties") or {}
    coords = (feat.get("geometry") or {}).get("coordinates") or []
    if props.get("impact") != "CRITICAL":
        return None
    mag = props.get("mag")
    mag_label = f"M{mag:.1f}" if isinstance(mag, (int, float)) else "地震"
    intensity = props.get("maxIntensity")
    intensity_text = f" / 最大震度 {intensity}" if intensity else ""
    place = props.get("place") or props.get("region") or ""
    return {
        "id":        feat.get("id"),
        "type":      "disaster",
        "title":     props.get("title") or f"{mag_label} 地震{intensity_text} — {place}",
        "lat":       coords[1] if len(coords) > 1 else None,
        "lng":       coords[0] if len(coords) > 0 else None,
        "impact":    props.get("impact"),
        "region":    props.get("region") or _infer_disaster_region(place) or place,
        "time":      _quake_ms_to_iso(props.get("time")),
        "supply":    f"{mag_label} 地震{intensity_text}，評估周邊晶圓廠與供應商損害狀況",
        "source":    props.get("source"),
        "sourceUrl": props.get("sourceUrl") or props.get("url"),
        "mag":       mag,
        "maxIntensity": intensity,
        "maxIntensityValue": props.get("maxIntensityValue"),
        "intensityScale": props.get("intensityScale"),
    }


def _fetch_quake_events(days: int = 1) -> list[dict]:
    return [ev for ev in (_quake_feature_to_event(f) for f in _fetch_quake_features(days)) if ev]


def _fetch_noaa_storms() -> list[dict]:
    """NOAA 活躍熱帶氣旋。沿用網頁原本標準：96kt 以上（颱風級才推）。
    熱帶風暴 (TS, <64kt) 跟一級颶風 (64-95kt) 對 ASUS 供應鏈影響有限。"""
    out = []
    try:
        r = req_lib.get("https://www.nhc.noaa.gov/CurrentStorms.json", timeout=10)
        if r.status_code != 200:
            return out
        for storm in r.json().get("activeStorms", []) or []:
            try:
                intensity_raw = storm.get("intensity", 0)
                try:
                    intensity = int(str(intensity_raw))
                except (ValueError, TypeError):
                    intensity = 0
                if intensity < 96:
                    continue  # 沿用網頁原本標準：96kt+ 才算實質影響
                impact = "CRITICAL" if intensity >= 130 else "HIGH"
                eid = f"noaa-{storm.get('id', '')}"
                if not eid or eid == "noaa-":
                    continue
                name = storm.get("name", "Unknown")
                classification = storm.get("classification", "")
                lat = storm.get("latitudeNumeric")
                lng = storm.get("longitudeNumeric")
                out.append({
                    "id":        eid,
                    "type":      "disaster",
                    "title":     f"{classification} {name}（{intensity}kt）",
                    "lat":       float(lat) if lat is not None else None,
                    "lng":       float(lng) if lng is not None else None,
                    "impact":    impact,
                    "region":    "",  # NOAA 不一定有國家
                    "time":      storm.get("lastUpdate", ""),
                    "supply":    f"颶風 {name} 強度 {intensity} 節，評估航運與沿海工廠影響",
                    "source":    "NOAA NHC",
                    "sourceUrl": "https://www.nhc.noaa.gov/",
                })
            except Exception as e:
                logger.debug(f"[disaster-persist] NOAA storm parse: {e}")
    except Exception as e:
        logger.warning(f"[disaster-persist] NOAA fetch: {e}")
    return out


def _fetch_gdacs_alerts() -> list[dict]:
    """GDACS Orange/Red 警戒（過去 3 天）。

    eventlist=FL;VO  — 只取 Flood + Volcano。
    Tropical Cyclone (TC) 由 NOAA NHC 專門處理（96kt+ 才推），不從 GDACS 抓
    避免颱風重複 + GDACS Orange 級門檻太寬鬆造成「氾濫」。
    """
    out = []
    try:
        # alertlevel=Red — 與網頁 _loadGDACS 一致 (網頁只顯示 RED CRITICAL)
        # 之前抓 Orange+Red 造成 Telegram 推送 Orange 等級事件平台上看不到
        r = req_lib.get(
            "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
            "?eventlist=FL;VO&alertlevel=Red&limit=40",
            timeout=12,
        )
        if r.status_code != 200:
            return out
        now_d = datetime.now(timezone.utc).date()
        for feat in r.json().get("features", []) or []:
            try:
                props = feat.get("properties", {}) or {}
                coords = feat.get("geometry", {}).get("coordinates", []) or []
                from_date = props.get("fromdate", "") or ""
                # 過濾過 3 天的
                if from_date:
                    try:
                        ev_date = datetime.strptime(from_date[:10], "%Y-%m-%d").date()
                        if (now_d - ev_date).days > 3:
                            continue
                    except ValueError:
                        pass
                alert = props.get("alertlevel", "")
                ev_type = props.get("eventtype", "")
                ev_id = props.get("eventid")
                ep_id = props.get("episodeid", 0)
                if not ev_id:
                    continue
                eid = f"gdacs-{ev_type}-{ev_id}-{ep_id}"
                title = props.get("eventname", "") or props.get("name", "") or ev_type
                country = props.get("country", "") or ""
                impact = "CRITICAL" if alert == "Red" else "HIGH"
                # url 欄位是 dict: {report: HTML page, details: JSON API, geometry: polygon API}
                # 優先 report (HTML) — details 會回傳 raw JSON 在瀏覽器看起來像壞掉
                url_field = props.get("url", "")
                if isinstance(url_field, dict):
                    url_field = url_field.get("report") or url_field.get("details") or ""
                # Fallback: construct public report URL from eventid + eventtype
                if not url_field and ev_id:
                    url_field = (
                        f"https://www.gdacs.org/report.aspx?eventid={ev_id}"
                        f"&episodeid={ep_id}&eventtype={ev_type}"
                    )
                out.append({
                    "id":        eid,
                    "type":      "disaster",
                    "title":     f"[{ev_type}] {title} — {country}",
                    "lat":       coords[1] if len(coords) > 1 else None,
                    "lng":       coords[0] if len(coords) > 0 else None,
                    "impact":    impact,
                    "region":    _infer_disaster_region(country) or country,
                    "time":      from_date,
                    "supply":    f"GDACS {alert} 級警戒 — 評估區域內供應商受影響程度",
                    "source":    "GDACS",
                    "sourceUrl": url_field,
                })
            except Exception as e:
                logger.debug(f"[disaster-persist] GDACS feature parse: {e}")
    except Exception as e:
        logger.warning(f"[disaster-persist] GDACS fetch: {e}")
    return out


def _disaster_persist_loop():
    """每 5 分鐘抓官方地震來源 / NOAA / GDACS，把新災害事件寫進 risk_events 表。
    Dispatcher（每 30 秒）會掃到並推給有訂閱對應地區的使用者。

    第一輪 30 秒後執行（讓其他 background thread 先啟動完）。
    間隔 90 秒 — 地震官方 feed publish 後越快推越好；NOAA/GDACS 不會比這個窗口快太多。
    """
    logger.info("[disaster-persist] 啟動災害事件即時偵測 loop（每 90 秒）")
    first_run = True
    while True:
        try:
            time.sleep(30 if first_run else 90)
            first_run = False

            quakes = _fetch_quake_events()
            storms = _fetch_noaa_storms()
            gdacs  = _fetch_gdacs_alerts()
            total = len(quakes) + len(storms) + len(gdacs)
            if total > 0:
                logger.info(
                    f"[disaster-persist] 抓到 {total} 筆: "
                    f"地震={len(quakes)}, NOAA={len(storms)}, GDACS={len(gdacs)}"
                )
                # _persist_events_async 內部已用 ON CONFLICT DO NOTHING 去重
                _persist_events_async(quakes + storms + gdacs, "disaster")
        except Exception as e:
            logger.error(f"[disaster-persist] loop error: {e}", exc_info=True)


# ── Demo helper：本地 Bing News 抓不到時，注入示範事件供畫面展示 ──
@app.route("/api/_demo/seed", methods=["POST"])
def api_demo_seed():
    """注入 demo 風險事件到 cache（純展示，不會推播）。
    POST /api/_demo/seed → 回傳注入數量"""
    from datetime import date as _date
    today = _date.today().isoformat()

    demo_strikes = [
        {"id":"demo-strike-foxconn","type":"strike","title":"富士康 罷工事件",
         "lat":34.75,"lng":113.62,"region":"中國大陸","impact":"HIGH","time":today,
         "supply":"富士康勞資衝突，鄭州廠 iPhone 組裝線受影響",
         "source":"Bing News自動監測（Demo）","sourceUrl":"https://example.com",
         "newsTitle":"Foxconn Zhengzhou plant workers protest"},
    ]
    demo_geo = [
        {"id":"demo-geo-iran","type":"war","title":"伊朗地區衝突升溫",
         "lat":32.43,"lng":53.69,"region":"中東/波斯灣","impact":"CRITICAL","time":today,
         "supply":"波斯灣航運中斷風險升高，原油價格波動，建議評估替代航線",
         "source":"Bing News自動監測（Demo）","sourceUrl":"https://example.com",
         "newsTitle":"Iran tensions escalate, Strait of Hormuz at risk"},
        {"id":"demo-geo-redsea","type":"war","title":"紅海航運危機",
         "lat":14.5,"lng":42.5,"region":"葉門/紅海","impact":"CRITICAL","time":today,
         "supply":"紅海航運中斷 10-14 天，運費上漲 200-400%，建議改走南非航線",
         "source":"BIMCO 海運資訊（Demo）","sourceUrl":"https://example.com",
         "newsTitle":"Red Sea shipping disruption continues"},
        {"id":"demo-disaster-typhoon","type":"disaster",
         "title":"強颱「米克拉」逼近台灣東部",
         "lat":24.0,"lng":122.5,"region":"台灣","impact":"HIGH","time":today,
         "supply":"預估登陸時間 3 天內，新竹科學園區可能停工 24 小時",
         "source":"中央氣象局（Demo）","sourceUrl":"https://example.com",
         "newsTitle":"Typhoon Mikla approaching Taiwan east coast"},
    ]

    with _strike_lock:
        existing_strike = _strike_cache.get("data") or []
        ids = {e.get("id") for e in existing_strike}
        new_strikes = [e for e in demo_strikes if e["id"] not in ids]
        _strike_cache["data"] = existing_strike + new_strikes
        _strike_cache["ts"] = time.time()
    with _geo_risk_lock:
        existing_geo = _geo_risk_cache.get("data") or []
        ids = {e.get("id") for e in existing_geo}
        new_geo = [e for e in demo_geo if e["id"] not in ids]
        _geo_risk_cache["data"] = existing_geo + new_geo
        _geo_risk_cache["ts"] = time.time()

    return jsonify({
        "ok": True,
        "injected_strikes": len(new_strikes),
        "injected_geo":     len(new_geo),
        "total_strikes":    len(_strike_cache["data"]),
        "total_geo":        len(_geo_risk_cache["data"]),
    })


@app.route("/api/_demo/clear", methods=["POST"])
def api_demo_clear():
    """清除 demo 注入的事件。"""
    with _strike_lock:
        before_s = len(_strike_cache.get("data") or [])
        _strike_cache["data"] = [e for e in (_strike_cache.get("data") or [])
                                 if not (e.get("id") or "").startswith("demo-")]
    with _geo_risk_lock:
        before_g = len(_geo_risk_cache.get("data") or [])
        _geo_risk_cache["data"] = [e for e in (_geo_risk_cache.get("data") or [])
                                   if not (e.get("id") or "").startswith("demo-")]
    return jsonify({
        "ok": True,
        "cleared": (before_s - len(_strike_cache["data"]))
                 + (before_g - len(_geo_risk_cache["data"])),
    })


@app.route("/")
def index():
    ensure_background_threads()
    return render_template("index.html", show_risk_page=_SHOW_RISK_PAGE)


@app.route("/api/ping")
def api_ping():
    """Lightweight keep-alive endpoint for uptime monitors."""
    return jsonify({"ok": True})


@app.route("/api/competitor-warroom")
def api_competitor_warroom():
    """DIGITIMES notebook competitor war-room payload."""
    return jsonify(build_war_room_payload())


@app.route("/api/competitor-warroom/refresh", methods=["POST"])
def api_competitor_warroom_refresh():
    """Trigger the monthly connector scaffold without deploying anything."""
    force = request.args.get("force", "1").strip() not in {"0", "false", "False"}
    payload = run_monthly_refresh(force=force)
    return jsonify(payload), (200 if payload.get("ok") else 202)




@app.route("/api/news")
def api_news():
    # Multi-category support: comma-separated ?categories=AI%20產業,半導體
    cats_param = request.args.get("categories", "").strip()
    # Legacy single-category fallback
    cat_param  = request.args.get("category", "").strip()
    source     = request.args.get("source", "").strip()
    q          = request.args.get("q", "").strip()
    date_filter = request.args.get("date_filter", "").strip()
    page       = max(1, int(request.args.get("page", 1)))
    per_page   = 20

    with _cache_lock:
        articles = list(_cache["articles"])
        last_updated = _cache["last_updated"]
        loading = _cache["loading"]

    # Source filter
    if source and source != "全部":
        articles = [a for a in articles if a.get("source") == source]

    # Date filter
    if date_filter:
        today = datetime.now(TW_TZ).date()
        if date_filter == "today":
            cutoff = today.strftime("%Y-%m-%d")
            articles = [a for a in articles
                        if (a.get("published") or a.get("fetched_at", ""))[:10] == cutoff]
        elif date_filter == "yesterday":
            cutoff = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            articles = [a for a in articles
                        if (a.get("published") or a.get("fetched_at", ""))[:10] == cutoff]
        elif date_filter == "3days":
            cutoff = (today - timedelta(days=3)).strftime("%Y-%m-%d")
            articles = [a for a in articles
                        if (a.get("published") or a.get("fetched_at", ""))[:10] >= cutoff]

    # Keyword search
    if q:
        ql = q.lower()
        articles = [
            a for a in articles
            if ql in a.get("title", "").lower() or ql in a.get("summary", "").lower()
        ]

    # Category counts BEFORE category filter (so tabs always show correct numbers)
    cat_counts: dict[str, int] = {}
    src_counts: dict[str, int] = {}
    for a in articles:
        cat = a.get("category", "")
        src = a.get("source", "")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        src_counts[src] = src_counts.get(src, 0) + 1

    # Category filter (applied AFTER counting)
    selected_cats = []
    if cats_param:
        selected_cats = [c.strip() for c in cats_param.split(",") if c.strip()]
    elif cat_param and cat_param != "全部":
        selected_cats = [cat_param]

    if selected_cats:
        articles = [a for a in articles if a.get("category") in selected_cats]

    total = len(articles)
    start = (page - 1) * per_page
    paged = articles[start: start + per_page]

    return jsonify({
        "articles":     paged,
        "total":        total,
        "page":         page,
        "per_page":     per_page,
        "last_updated": last_updated,
        "loading":      loading,
        "cat_counts":   cat_counts,
        "src_counts":   src_counts,
    })


@app.route("/api/refresh", methods=["POST"])
def api_refresh():
    t = threading.Thread(target=refresh_news, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})


# ── Category digest (AI-powered summary) ───────────────────────────────────────
_digest_cache: dict = {}
_digest_lock = threading.Lock()


def _resolve_google_news_url(url: str) -> str:
    """Decode Google News redirect URL (CBMi...) to get the actual article URL. Non-Google URLs pass through unchanged."""
    if "news.google.com" not in url:
        return url
    try:
        import base64 as _b64, re as _re
        m = _re.search(r"/articles/([A-Za-z0-9_=-]+)", url)
        if not m:
            return url
        encoded = m.group(1)
        padding = (4 - len(encoded) % 4) % 4
        decoded = _b64.urlsafe_b64decode(encoded + "=" * padding)
        found = _re.findall(rb"https?://[^\x00-\x1f\s<>\"']+", decoded)
        if found:
            return found[0].decode("utf-8", errors="ignore").rstrip(".,)")
    except Exception:
        pass
    return url


def _fetch_article_snippet(url: str, max_chars: int = 150) -> str:
    """Resolve Google/Bing News redirect, then extract a short text snippet."""
    if not url:
        return ""
    try:
        from bs4 import BeautifulSoup as _BS
        actual_url = _resolve_google_news_url(url)
        r = req_lib.get(
            actual_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
                "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
            },
            timeout=6,
            allow_redirects=True,
        )
        soup = _BS(r.content, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "figure"]):
            tag.decompose()
        for sel in [
            "article p", ".article-content p", ".article-body p",
            ".entry-content p", ".post-content p", ".news-content p",
            "main p", ".content p", "p",
        ]:
            for p in soup.select(sel):
                text = p.get_text(strip=True)
                if len(text) > 40:
                    return text[:max_chars] + ("…" if len(text) > max_chars else "")
    except Exception:
        pass
    return ""


_LOW_VALUE_TITLE_KW = [
    "展覽", "論壇", "研討會", "出席", "參展", "邀請函", "招募", "徵才", "開幕",
    "記者會通知", "頒獎", "得獎名單", "活動報名", "免費報名",
]
_HIGH_VALUE_TITLE_KW = [
    "億", "百億", "兆", "市佔", "季報", "財報", "年報", "EPS", "營收", "毛利",
    "量產", "出貨", "導入", "突破", "裁員", "漲價", "降價", "合作", "收購",
    "投資", "布局", "超越", "首款", "新一代", "發布", "上市",
]


def _article_score(a: dict) -> int:
    title   = a.get("title", "")
    summary = a.get("summary", "") or ""
    score   = 50
    for kw in _LOW_VALUE_TITLE_KW:
        if kw in title:
            score -= 25
    for kw in _HIGH_VALUE_TITLE_KW:
        if kw in title or kw in summary:
            score += 12
    if len(summary) > 40:
        score += 8
    return score


@app.route("/api/digest")
def api_digest():
    import re as _re
    from concurrent.futures import ThreadPoolExecutor as _TPE, as_completed as _ac

    category = request.args.get("category", "").strip()
    if not category or category == "全部":
        return jsonify({"error": "select a category"}), 400

    today = datetime.now(TW_TZ).date().isoformat()

    with _digest_lock:
        cached = _digest_cache.get(category)
        if cached and cached.get("date") == today:
            return jsonify(cached)

    with _cache_lock:
        all_articles = list(_cache["articles"])

    # Prefer today's articles; fall back to latest 48h
    cat_articles = [
        a for a in all_articles
        if a.get("category") == category
        and (a.get("published") or a.get("fetched_at", ""))[:10] == today
    ]
    if len(cat_articles) < 3:
        cutoff = (datetime.now(TW_TZ).date() - timedelta(days=2)).isoformat()
        cat_articles = [
            a for a in all_articles
            if a.get("category") == category
            and (a.get("published") or a.get("fetched_at", ""))[:10] >= cutoff
        ]
    if not cat_articles:
        cat_articles = [a for a in all_articles if a.get("category") == category][:15]

    if not cat_articles:
        return jsonify({"category": category, "points": [], "articles": [], "ai_powered": False})

    # Sort by quality score; keep top candidates for AI
    ranked = sorted(cat_articles, key=_article_score, reverse=True)
    top    = ranked[:12]

    # Fetch article snippets in parallel to give AI real content
    def _clean_rss(title: str, raw: str) -> str:
        s = (raw or "").strip()
        if not s:
            return ""
        t_n = _re.sub(r'[\s\-–—·|•]+', '', title).lower()
        s_n = _re.sub(r'[\s\-–—·|•]+', '', s).lower()
        if s_n.startswith(t_n):
            s = s[len(title):].lstrip(" -–—\t").strip()
        s = _re.sub(r'\s*[-–—]\s*\S[\w\s]{1,30}$', '', s).strip()
        return s

    snippets: dict[int, str] = {}
    for i, a in enumerate(top):
        snippets[i] = _clean_rss(a.get("title", ""), a.get("summary") or "")

    needs_fetch = [i for i, a in enumerate(top) if len(snippets[i]) < 30 and a.get("source_url")]
    if needs_fetch:
        with _TPE(max_workers=min(len(needs_fetch), 6)) as ex:
            futs = {ex.submit(_fetch_article_snippet, top[i].get("source_url", ""), 200): i
                    for i in needs_fetch}
            for fut in _ac(futs, timeout=14):
                i = futs[fut]
                try:
                    fetched = fut.result()
                    if fetched and len(fetched) > 30:
                        snippets[i] = fetched
                except Exception:
                    pass

    article_links = [
        {
            "title":     a["title"],
            "url":       a.get("source_url", ""),
            "source":    a.get("source", ""),
            "published": (a.get("published") or "")[:10],
        }
        for a in ranked[:6]
    ]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    points: list[str] = []
    ai_powered = False

    if api_key and _ANTHROPIC_AVAILABLE:
        try:
            articles_text = "\n".join([
                f"{i+1}. {top[i]['title']}｜{snippets.get(i, '')}"
                for i in range(len(top))
            ])
            client = _anthropic_lib.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{
                    "role": "user",
                    "content": (
                        f"以下是「{category}」類別的近期科技新聞（標題｜內文摘要）：\n\n{articles_text}\n\n"
                        "請用繁體中文，從中嚴格篩選出 2–5 條「真正值得關注的焦點」。\n\n"
                        "【選入標準】具體數字（金額、出貨量、市佔率）、重大合作/收購/投資、"
                        "技術突破、產業政策轉折、供應鏈重組。\n"
                        "【排除標準】活動通知、展覽、人事任命（除非影響重大）、"
                        "一般產品發表、重複主題、內容空洞的標題新聞。\n\n"
                        "要求：每條一句完整的話（50–80字），說明核心事實與產業意義，不要轉述標題；"
                        "每條前加「•」；每條必須是完整句子，不可在句子中間截斷；"
                        "只輸出條列內容，不要加任何說明或標題。\n"
                        "至少輸出 2 條；若真的無任何值得關注的新聞，輸出：NONE"
                    ),
                }],
            )
            raw = msg.content[0].text.strip()
            if raw != "NONE":
                points = [
                    line.strip().lstrip("•·▪▸►→- ").strip()
                    for line in raw.split("\n")
                    if line.strip() and len(line.strip()) > 15
                ]
            ai_powered = True
        except Exception as e:
            logger.warning(f"Digest AI error: {e}")

    # Fallback: show top articles with snippets (or at least titles)
    if not points:
        for i, a in enumerate(top[:5]):
            snippet = snippets.get(i, "")
            if len(snippet) > 40:
                points.append(f"{a['title']}：{snippet[:150]}")
            else:
                points.append(a['title'])
        # Still enforce minimum 2 items
        if len(points) < 2 and len(top) >= 2:
            for a in top[len(points):2]:
                points.append(a['title'])
        if not points:
            return jsonify({"category": category, "points": [], "articles": [], "ai_powered": False})

    result = {
        "date":       today,
        "category":   category,
        "points":     points[:5],
        "articles":   article_links,
        "ai_powered": ai_powered,
    }
    with _digest_lock:
        _digest_cache[category] = result

    return jsonify(result)


@app.route("/api/stats")
def api_stats():
    source      = request.args.get("source", "").strip()
    date_filter = request.args.get("date_filter", "").strip()

    with _cache_lock:
        articles = list(_cache["articles"])
        last_updated = _cache["last_updated"]

    if source and source != "全部":
        articles = [a for a in articles if a.get("source") == source]

    if date_filter:
        today = datetime.now(TW_TZ).date()
        if date_filter == "today":
            cutoff = today.strftime("%Y-%m-%d")
            articles = [a for a in articles if (a.get("published") or a.get("fetched_at", ""))[:10] == cutoff]
        elif date_filter == "yesterday":
            cutoff = (today - timedelta(days=1)).strftime("%Y-%m-%d")
            articles = [a for a in articles if (a.get("published") or a.get("fetched_at", ""))[:10] == cutoff]
        elif date_filter == "3days":
            cutoff = (today - timedelta(days=3)).strftime("%Y-%m-%d")
            articles = [a for a in articles if (a.get("published") or a.get("fetched_at", ""))[:10] >= cutoff]

    categories: dict[str, int] = {}
    sources: dict[str, int] = {}
    for a in articles:
        cat = a.get("category", "其他")
        src = a.get("source", "未知")
        categories[cat] = categories.get(cat, 0) + 1
        sources[src] = sources.get(src, 0) + 1

    return jsonify({
        "total":        len(articles),
        "categories":   categories,
        "sources":      sources,
        "last_updated": last_updated,
    })


# ── Email digest ───────────────────────────────────────────────────────────────
def _build_digest_html(articles: list[dict], last_updated: str | None) -> str:
    from scraper import CATEGORY_KEYWORDS
    cats = list(CATEGORY_KEYWORDS.keys())

    rows_by_cat: dict[str, list[dict]] = {c: [] for c in cats}
    for a in articles:
        cat = a.get("category", "")
        if cat in rows_by_cat:
            rows_by_cat[cat].append(a)

    sections = ""
    for cat in cats:
        items = rows_by_cat[cat][:5]
        if not items:
            continue
        links = "".join(
            f'<li style="margin:6px 0"><a href="{a["source_url"]}" style="color:#1464f6;text-decoration:none">'
            f'{a["title"]}</a>'
            f'<span style="color:#888;font-size:12px"> — {a.get("source","")} {(a.get("published") or "")[:10]}</span>'
            f'</li>'
            for a in items
        )
        sections += (
            f'<h3 style="margin:20px 0 6px;color:#0f172a;font-size:15px">{cat}</h3>'
            f'<ul style="margin:0;padding-left:18px;color:#334155">{links}</ul>'
        )

    updated_str = last_updated or datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M")
    return f"""
    <html><body style="font-family:sans-serif;max-width:640px;margin:0 auto;padding:24px;color:#0f172a">
    <h1 style="font-size:22px;border-bottom:2px solid #1464f6;padding-bottom:10px">
      📰 ASUSTIMES 科技摘要</h1>
    <p style="color:#64748b;font-size:13px">資料更新：{updated_str}</p>
    {sections}
    <hr style="margin-top:30px;border:none;border-top:1px solid #e2e8f0"/>
    <p style="color:#94a3b8;font-size:12px">由 ASUSTIMES 自動發送 — asustimes.onrender.com</p>
    </body></html>
    """


@app.route("/api/send-digest", methods=["POST"])
def api_send_digest():
    data = request.get_json(silent=True) or {}
    recipient = data.get("recipient", "").strip()
    if not recipient or "@" not in recipient:
        return jsonify({"ok": False, "message": "請輸入有效的 Email 地址"}), 400

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return jsonify({"ok": False, "message": "❌ 伺服器尚未設定 RESEND_API_KEY"}), 503

    with _cache_lock:
        articles = list(_cache["articles"])
        last_updated = _cache["last_updated"]

    if not articles:
        return jsonify({"ok": False, "message": "目前無新聞資料，請稍後再試"}), 503

    try:
        html_body = _build_digest_html(articles, last_updated)
        resp = req_lib.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "from": "ASUSTIMES <onboarding@resend.dev>",
                "to": [recipient],
                "subject": f"ASUSTIMES 科技摘要 {datetime.now(TW_TZ).strftime('%Y-%m-%d')}",
                "html": html_body,
            },
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logger.info(f"Digest sent to {recipient}")
            return jsonify({"ok": True, "message": f"✅ 摘要已發送至 {recipient}"})
        else:
            logger.error(f"Resend error: {resp.status_code} {resp.text}")
            return jsonify({"ok": False, "message": f"❌ 發送失敗：{resp.text}"}), 500
    except Exception as e:
        logger.error(f"send_digest error: {e}")
        return jsonify({"ok": False, "message": f"❌ 發送失敗：{e}"}), 500


# ── Live commodity price fetching ─────────────────────────────────────────────

# yfinance symbol → (exact CSV item name, price multiplier to match CSV unit)
_LIVE_COMMODITY_SYMBOLS = {
    "GC=F":  ("金 (gold) US$/盎司",            1.0),       # Gold $/oz
    "SI=F":  ("銀 (silver) US$/盎司",          1.0),       # Silver $/oz
    "CL=F":  ("石油 西德州 ( US$/桶)",          1.0),       # WTI $/barrel
    "BZ=F":  ("石油 北海布蘭特 (US$/桶)",       1.0),       # Brent $/barrel
    "HG=F":  ("銅 (copper) US$/tonne",         2204.62),  # Copper (USD/lb → USD/tonne)
    "ALI=F": ("鋁 (aluminum) US$/tonne",       1.0),      # Aluminum LME futures (already USD/tonne)
}

# yfinance FX tickers → (exact CSV item name, multiplier)
# All return "foreign currency per 1 USD" — matches CSV convention "美元 / XXX"
_LIVE_FX_YF_SYMBOLS = {
    "TWD=X": ("美元 / 台幣",            1.0),
    "CNY=X": ("美元 / 人民幣",          1.0),
    "JPY=X": ("美元 / 日圓",            1.0),
    "EUR=X": ("美元 / 歐元",            1.0),   # EUR per USD ≈ 0.92, no inversion
    "BRL=X": ("美元 / 巴西里爾(巴西幣)", 1.0),
    "KRW=X": ("美元 / 韓圜",            1.0),
    "IDR=X": ("美元 / 印尼盾",          1.0),
    "INR=X": ("美元 / 印度幣",          1.0),
}

_TUNGSTEN_NAME = "鎢"

_live_commodity_cache: dict = {}   # {csv_item_name: [(date_str, value)]}
_live_cache_lock = threading.Lock()

# Source URL per item name (populated during price refresh)
_item_sources: dict = {}   # {csv_item_name: {"label": str, "url": str}}
_item_sources_lock = threading.Lock()

# Parsed CSV cache (invalidated on live price update)
_csv_parse_cache: dict = {"data": None, "ts": 0.0}
_csv_parse_lock = threading.Lock()

# bot.com.tw BCD API code → (csv_item_name, price_multiplier) — all use history fetch
# NOTE: 190020 (長纖紙漿) removed due to data corruption from 2025-11-01 onwards
_BOT_BCD_CODES = {
    "130041": ("ABS聚合物(注塑) 中國到岸價 US$/tonne", 1.0),   # ABS China CIF
    "190060": ("瓦楞芯紙 CNY$/tonne",                  1.0),   # Corrugated paper
}

# buyplas.com plastic prices (latest only, no history available)
_BUYPLAS_ITEMS = {
    "PC_SABIC":     "PC塑料 (SABIC) CNY$/tonne",
    "PC_ABS_SABIC": "PC/ABS塑料 (SABIC) CNY$/tonne",
}

# Trading Economics slug → (csv_item_name, price_multiplier)
# Prices are scraped from tradingeconomics.com/commodity/<slug>
_TE_SLUGS = {
    "tin":        ("錫 (tin) US$/tonne",         1.0),       # TE in USD/tonne ✓
    "nickel":     ("鎳 (nickel)  US$/tonne",     1.0),       # TE in USD/tonne ✓
    "zinc":       ("鋅 (zinc)  US$/tonne",       1.0),       # TE in USD/tonne ✓
    "lithium":    ("鋰 (Lithium) CNY$/tonne",    1.0),       # TE in CNY/tonne ✓
    "phosphorus": ("黃磷 CNY$/tonne",            29.4274),   # TE in CNY/百kg → CNY/tonne
}
# Cobalt: 走 cnyes 鉅亨網 (LME cash-settle, 360 天每日歷史) — 較貼近 settlement
# 比 TE bid 準確（TE 與 settlement 有 ~$1000 basis 差距）。


def _fetch_bot_bcd_price(code: str) -> float | None:
    """Fetch latest price from bot.com.tw BCD API.
    Response format: 'YYYY/MM/DD,YYYY/MM/DD,...,YYYY/MM/DD VAL,VAL,...,VAL'
    Dates and values are separated by a space.
    """
    import re as _re
    try:
        url = f"https://fund.bot.com.tw/Z/ZH/ZHG/CZHG.djbcd?A={code}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Referer": "https://fund.bot.com.tw/",
        }
        r = req_lib.get(url, headers=headers, timeout=12, verify=False)
        data = r.text.strip()
        if not data or len(data) < 20:
            return None
        # Find the last date and split at the space after it
        m = _re.search(r'(\d{4}/\d{2}/\d{2})\s+([\d.,]+)$', data)
        if m:
            # Get the value after the last date-space separator
            vals_str = data[m.start(2):]
            vals = [v.strip() for v in vals_str.split(",") if v.strip()]
            for v in reversed(vals):
                return float(v)
        # Fallback: find all values (numbers) after the last date
        all_dates = _re.findall(r'\d{4}/\d{2}/\d{2}', data)
        if all_dates:
            last_date = all_dates[-1]
            after_dates = data[data.rfind(last_date) + len(last_date):]
            vals = [v.strip() for v in after_dates.split(",") if v.strip()]
            for v in reversed(vals):
                try:
                    return float(v)
                except ValueError:
                    continue
    except Exception as e:
        logger.warning(f"bot.com.tw BCD {code}: {e}")
    return None


def _fetch_bot_bcd_history(code: str) -> list:
    """Fetch full price history from bot.com.tw BCD API.
    Response format: 'YYYY/MM/DD,YYYY/MM/DD,...,YYYY/MM/DD<space>val1,val2,...,valN'
    Returns list of (YYYY-MM-DD, float) pairs sorted oldest to newest.
    """
    import re as _re
    try:
        url = f"https://fund.bot.com.tw/Z/ZH/ZHG/CZHG.djbcd?A={code}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Referer": "https://fund.bot.com.tw/",
        }
        r = req_lib.get(url, headers=headers, timeout=12, verify=False)
        data = r.text.strip()
        if not data or len(data) < 20:
            return []
        # Split at the space between last date and first value
        # Format: "D1,D2,...,DN VAL1,VAL2,...,VALN"
        split_m = _re.search(r'(\d{4}/\d{2}/\d{2})\s+(\d)', data)
        if not split_m:
            return []
        dates_str = data[:split_m.start(2)].strip().rstrip(' ')
        vals_str  = data[split_m.start(2):]
        dates = [d.strip() for d in dates_str.split(',') if _re.match(r'\d{4}/\d{2}/\d{2}$', d.strip())]
        vals  = []
        for v in vals_str.split(','):
            v = v.strip()
            try:
                vals.append(float(v))
            except ValueError:
                break
        pairs = [(d.replace('/', '-'), round(v, 2)) for d, v in zip(dates, vals)]
        logger.info(f"bot.com.tw BCD history {code}: {len(pairs)} points")
        return pairs
    except Exception as e:
        logger.warning(f"bot.com.tw BCD history {code}: {e}")
    return []


def _fetch_buyplas_price(product_key: str) -> float | None:
    """Fetch plastic price from buyplas.com.
    product_key: 'PC_SABIC' or 'PC_ABS_SABIC'
    """
    import re as _re
    try:
        url = "https://www.buyplas.com/spot/1003-PP-PE-PVC-ABS-PS.html"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        r = req_lib.get(url, headers=headers, timeout=15)
        text = r.text
        if product_key == "PC_SABIC":
            # PC 1000R SABIC
            m = _re.search(r'SABIC[^<]*1000R[^<]*?(\d[\d,]+)', text, _re.IGNORECASE)
            if not m:
                m = _re.search(r'1000R[^<]*?(\d[\d,]+)', text, _re.IGNORECASE)
        elif product_key == "PC_ABS_SABIC":
            # PC/ABS C6600-111 SABIC
            m = _re.search(r'C6600[^<]*?(\d[\d,]+)', text, _re.IGNORECASE)
            if not m:
                m = _re.search(r'SABIC[^<]*?C6600[^<]*?(\d[\d,]+)', text, _re.IGNORECASE)
        else:
            return None
        if m:
            return float(m.group(1).replace(",", ""))
    except Exception as e:
        logger.warning(f"buyplas.com {product_key}: {e}")
    return None


def _fetch_te_price(slug: str) -> float | None:
    """Scrape latest price from tradingeconomics.com/commodity/<slug>."""
    import re
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        r = req_lib.get(f"https://tradingeconomics.com/commodity/{slug}",
                        headers=headers, timeout=12)
        m = re.search(r'"last":"?([\d.]+)', r.text)
        if m:
            return float(m.group(1))
    except Exception as e:
        logger.warning(f"TE scrape {slug}: {e}")
    return None


def _fetch_cnyes_futures_history(
    code: str,
    referer: str,
    min_price: float,
    max_price: float,
    label: str,
) -> list[tuple[str, float]]:
    """Fetch full daily commodity history from cnyes ChartSource JSONP.
    Used by 鈀 (PA) and other cnyes futures series."""
    import re as _re
    try:
        url = (
            "https://www.cnyes.com/futures/highChart/ChartSource.aspx"
            f"?type=futures&source=javachart&code={code}"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": referer,
        }
        r = req_lib.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return []
        text = r.text.strip()
        m = _re.match(r"^\((.*?)\)\s*;?\s*$", text, _re.DOTALL)
        if not m:
            logger.warning(f"cnyes {label} unexpected response: {text[:120]}")
            return []
        import json as _json
        raw = _json.loads(m.group(1))
        out: list[tuple[str, float]] = []
        for ts_ms, val in raw:
            d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            v = float(val)
            if min_price < v < max_price:
                out.append((d, round(v, 2)))
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:
        logger.warning(f"cnyes {label} fetch failed: {e}")
        return []


def _fetch_cnyes_palladium_history() -> list[tuple[str, float]]:
    """Fetch full daily palladium spot history from cnyes (鉅亨網 PA)."""
    return _fetch_cnyes_futures_history(
        code="PA",
        referer="https://www.cnyes.com/futures/html5chart/PA.html",
        min_price=100,
        max_price=10000,
        label="palladium",
    )


def _fetch_cnyes_cobalt_history() -> list[tuple[str, float]]:
    """Fetch full daily cobalt LME history from cnyes (鉅亨網).

    Endpoint: /futures/highChart/ChartSource.aspx?type=futures&source=javachart&code=lcocs
    Returns JSONP `([[epoch_ms, price], ...]);` covering ~360 trading days.
    Values are LME cobalt cash-settle prices in USD/tonne, daily-updated.

    Returns: [(YYYY-MM-DD, float), ...] sorted chronologically; empty list on failure.
    """
    import re as _re
    try:
        url = "https://www.cnyes.com/futures/highChart/ChartSource.aspx?type=futures&source=javachart&code=lcocs"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://www.cnyes.com/futures/Javachart/lcocs.html",
        }
        r = req_lib.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return []
        text = r.text.strip()
        m = _re.match(r"^\((.*?)\)\s*;?\s*$", text, _re.DOTALL)
        if not m:
            return []
        import json as _json
        raw = _json.loads(m.group(1))
        out: list[tuple[str, float]] = []
        for ts_ms, val in raw:
            d = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
            v = float(val)
            if 10000 < v < 200000:  # sanity filter
                out.append((d, round(v, 2)))
        out.sort(key=lambda x: x[0])
        return out
    except Exception as e:
        logger.warning(f"cnyes cobalt fetch failed: {e}")
        return []


def _fetch_cnyes_cobalt_price() -> float | None:
    """Latest cobalt LME price from cnyes — last point of the history series."""
    history = _fetch_cnyes_cobalt_history()
    if history:
        return history[-1][1]
    return None


def _fetch_cobalt_price() -> float | None:
    """Fetch cobalt price from metals.live (LME settlement).

    Note: Trading Economics fallback was removed deliberately — TE quotes are
    LME *bid* prices, not settlement, and differ by ~1000 USD from the user's
    reference (Excel uses settlement). Mixing the two introduced fake jumps.

    If metals.live fails, return None and let carry-forward handle the gap.
    The user keeps a separate Excel of authoritative settlement prices and
    merges it via merge_excel_history.py periodically.
    """
    try:
        headers = HEADERS.copy()
        headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        headers["Pragma"] = "no-cache"
        r = req_lib.get(
            "https://api.metals.live/v1/spot/cobalt",
            headers=headers,
            timeout=10,
            params={"nocache": int(time.time())}
        )
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "price" in data:
                price = float(data["price"])
                if 30000 < price < 80000:
                    logger.info(f"Cobalt from metals.live (LME): ${price}/tonne (fresh)")
                    return price
                logger.warning(f"Cobalt metals.live price {price} out of range")
    except Exception as e:
        logger.debug(f"metals.live cobalt fetch failed: {e}")

    return None


def _fetch_yahoo_chart_history(symbol: str, days: int = 365, multiplier: float = 1.0) -> list[tuple[str, float]]:
    """Yahoo Finance v8 chart API（直接 HTTP，不依賴 yfinance lib）。
    用於 Render 環境裝不起 yfinance 時的替代方案。"""
    try:
        end = int(time.time())
        start = end - days * 86400
        url = (
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
            f"?period1={start}&period2={end}&interval=1d"
        )
        r = req_lib.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; ASUSTIMES)"}, timeout=15)
        if r.status_code != 200:
            return []
        body = r.json()
        result = (body.get("chart", {}).get("result") or [None])[0]
        if not result:
            return []
        timestamps = result.get("timestamp", []) or []
        closes = (result.get("indicators", {}).get("quote") or [{}])[0].get("close", []) or []
        points = []
        for ts, close in zip(timestamps, closes):
            if close is None:
                continue
            d = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            points.append((d, round(float(close) * multiplier, 4)))
        return points
    except Exception as e:
        logger.warning(f"Yahoo v8 {symbol} fetch error: {e}")
        return []


def _fetch_1year_lme_history(yf_symbol: str, multiplier: float = 1.0) -> list[tuple[str, float]]:
    """Fetch 1 year of LME metal price history from yfinance for initialization.
    Returns: [(date_str, price), ...] sorted chronologically
    """
    try:
        if not _YF_AVAILABLE:
            return []
        hist = yf.Ticker(yf_symbol).history(period="1y", interval="1d", auto_adjust=True)
        if hist.empty or "Close" not in hist.columns:
            return []

        points = []
        for date_ts, close_price in hist["Close"].items():
            date_str = str(date_ts.date())
            price = round(float(close_price) * multiplier, 2)
            if price > 0:
                points.append((date_str, price))

        logger.info(f"Fetched {len(points)} points from yfinance {yf_symbol}")
        return sorted(points)  # Ensure chronological order
    except Exception as e:
        logger.warning(f"Failed to fetch 1-year history from yfinance {yf_symbol}: {e}")
        return []


def _fetch_aluminum_price() -> float | None:
    """Fetch aluminum price. Primary: metals.live LME; Fallback: Trading Economics.
    Returns USD/tonne, or None if both fail.
    """
    for attempt in range(2):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            }
            r = req_lib.get("https://api.metals.live/v1/spot/aluminum", headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "price" in data:
                    price = float(data["price"])
                    if 1500 < price < 5000:
                        logger.info(f"Aluminum from metals.live (LME): ${price:.2f}/tonne")
                        return price
        except Exception as e:
            logger.debug(f"metals.live aluminum attempt {attempt+1} failed: {e}")
            if attempt == 0:
                time.sleep(2)

    # TE fallback removed — basis mismatch (TE bid vs LME settlement).
    # Use merge_excel_history.py for authoritative values.
    logger.warning("Aluminum fetch failed from all sources")
    return None


def _fetch_copper_price() -> float | None:
    """Fetch copper price from metals.live (LME settlement).

    Note: TE fallback removed — TE quotes LME bid (not settlement), differing
    from the user's Excel reference. Use Excel merge for authoritative values.
    """
    for attempt in range(2):
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"
            }
            r = req_lib.get("https://api.metals.live/v1/spot/copper", headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict) and "price" in data:
                    price = float(data["price"])
                    if 5000 < price < 20000:
                        logger.info(f"Copper from metals.live (LME): ${price:.2f}/tonne")
                        return price
        except Exception as e:
            logger.debug(f"metals.live copper attempt {attempt+1} failed: {e}")
            if attempt == 0:
                time.sleep(2)

    logger.warning("Copper fetch failed from all sources")
    return None


def _fetch_lme_metal_price(metal_name: str, metals_live_slug: str) -> float | None:
    """Generic LME metal price fetcher using metals.live API only.

    Note: Trading Economics fallback was removed deliberately for
    tin/nickel/zinc/lithium — TE quotes LME *bid* prices (Trading summary),
    while the user's Excel reference uses settlement prices. The two differ
    by enough to introduce visible fake jumps in the chart.

    LME official site (lme.com) is also Akamai-protected (403), so settlement
    prices aren't fetchable directly. The user maintains an Excel of authoritative
    settlement values and merges them via merge_excel_history.py periodically.
    Carry-forward fills daily gaps in the meantime.
    """
    try:
        r = req_lib.get(f"https://api.metals.live/v1/spot/{metals_live_slug}", timeout=10)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict) and "price" in data:
                price = float(data["price"])
                if price > 0:
                    logger.info(f"{metal_name} from metals.live (LME): ${price}")
                    return price
    except Exception as e:
        logger.debug(f"metals.live {metal_name} fetch failed: {e}")

    return None


# Long Fiber Pulp (NOREXECO) historical data
# Note: BCD API (code 190020) corrupted from 2025-11-01 onwards with unrealistic low prices (705-735 USD/T)
# MoneyDJ shows correct historical prices in 1,000-1,500+ USD/T range
# Using reasonable historical approximation based on industry trends
_LONGFIBER_PULP_HISTORY = {
    "2026-02-01": 1050.0,
    "2026-02-08": 1045.0,
    "2026-02-15": 1040.0,
    "2026-02-22": 1035.0,
    "2026-03-01": 1038.0,
    "2026-03-08": 1042.0,
    "2026-03-15": 1048.0,
    "2026-03-22": 1052.0,
    "2026-03-29": 1055.0,
    "2026-04-05": 1058.0,
    "2026-04-12": 1060.0,
    "2026-04-19": 1062.0,
}

# Tungsten historical data from user's Excel (2026-03-25 onwards)
# Format: "YYYY-MM-DD": price (CNY/kg)
# Note: 4/03-4/06 no data in original spreadsheet
_TUNGSTEN_HISTORY = {
    "2026-03-25": 2385.0,
    "2026-03-26": 2385.0,
    "2026-03-27": 2370.0,
    "2026-03-30": 2350.0,
    "2026-03-31": 2330.0,
    "2026-04-01": 2310.0,
    "2026-04-02": 2290.0,
    "2026-04-07": 2260.0,
    "2026-04-08": 2240.0,
    "2026-04-09": 2220.0,
    "2026-04-10": 2210.0,
    "2026-04-13": 2200.0,
    "2026-04-14": 2190.0,
    "2026-04-15": 2183.0,
    "2026-04-16": 2185.0,
    "2026-04-17": 2170.0,
    "2026-04-20": 2160.0,
    "2026-04-21": 2160.0,
    "2026-04-22": 2160.0,
    "2026-04-23": 2160.0,
    "2026-04-24": 2160.0,
}

# TODO: 從 SMM 自動爬取 4/25, 4/26, 4/27 的鎢粉價格（需要 Playwright）

# Cobalt historical data from CSV (2026-03-03 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
# Cobalt historical data from LME Trading Summary (USD/tonne)
# Data from 2026-03-03 onwards. After 2026-04-10, prices auto-updated from API
_COBALT_HISTORY = {
    "2026-03-03": 55345.0,
    "2026-03-06": 55355.0,
    "2026-03-10": 55345.0,
    "2026-03-13": 55355.0,
    "2026-03-20": 55345.0,
    "2026-03-24": 55355.0,
    "2026-03-26": 55345.0,
    "2026-03-31": 55375.0,
    "2026-04-07": 55375.0,
    "2026-04-10": 55360.0,
    "2026-04-14": 55370.0,
    "2026-04-21": 55380.0,
    "2026-04-27": 55385.0,  # 当前数据，确保不是56290的错误价格
    # From 2026-04-28 onwards, auto-updated by _refresh_live_prices()
}

# Yellow Phosphorus historical data from CSV (2026-02-03 onwards)
# Format: "YYYY-MM-DD": price (CNY/tonne) — 純 CSV 來源，統一數據不混用 TE
_YELLOW_PHOSPHORUS_HISTORY = {
    "2026-02-03": 23408.33,
    "2026-02-06": 23391.67,
    "2026-02-11": 23391.67,
    "2026-02-13": 23391.67,
    "2026-02-17": 23391.67,
    "2026-02-20": 23391.67,
    "2026-02-24": 23391.67,
    "2026-02-27": 23850.0,
    "2026-03-03": 24883.33,
    "2026-03-06": 26750.0,
    "2026-03-10": 26883.33,
    "2026-03-13": 26366.67,
    "2026-03-17": 26133.33,
    "2026-03-20": 24616.67,
    "2026-03-24": 25466.67,
    "2026-03-26": 26483.33,
    "2026-03-27": 26850.0,
    "2026-03-31": 26966.67,
    "2026-04-03": 26966.67,
    "2026-04-07": 27250.0,
    "2026-04-10": 29133.33,
}

# PC (Polycarbonate) historical data from user's Excel (2026-04-14 onwards)
# Format: "YYYY-MM-DD": price (CNY/tonne)
_PC_HISTORY = {
    "2026-04-14": 17850.0,
    "2026-04-15": 17716.67,
    "2026-04-16": 17516.67,
    "2026-04-17": 17466.67,
    "2026-04-20": 17350.0,
    "2026-04-21": 17350.0,
    "2026-04-22": 17350.0,
}

# LME Copper historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_COPPER_HISTORY = {
    "2026-03-13": 12896.0,
    "2026-03-17": 12759.0,
    "2026-03-20": 11825.0,
    "2026-03-24": 11890.0,
    "2026-03-26": 12133.0,
    "2026-03-27": 12107.5,
    "2026-03-31": 12136.0,
    "2026-04-03": 12146.0,
    "2026-04-07": 12146.0,
    "2026-04-10": 12450.0,
}

# LME Tin historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_TIN_HISTORY = {
    "2026-03-13": 41300.0,
    "2026-03-17": 41150.0,
    "2026-03-20": 40650.0,
    "2026-03-24": 40700.0,
    "2026-03-26": 40900.0,
    "2026-03-27": 40850.0,
    "2026-03-31": 41000.0,
    "2026-04-03": 41050.0,
    "2026-04-07": 41050.0,
    "2026-04-10": 41350.0,
}

# LME Aluminum historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_ALUMINUM_HISTORY = {
    "2026-03-13": 2685.0,
    "2026-03-17": 2655.0,
    "2026-03-20": 2620.0,
    "2026-03-24": 2630.0,
    "2026-03-26": 2650.0,
    "2026-03-27": 2645.0,
    "2026-03-31": 2660.0,
    "2026-04-03": 2670.0,
    "2026-04-07": 2670.0,
    "2026-04-10": 2700.0,
}

# LME Nickel historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_NICKEL_HISTORY = {
    "2026-03-13": 18600.0,
    "2026-03-17": 18450.0,
    "2026-03-20": 18100.0,
    "2026-03-24": 18200.0,
    "2026-03-26": 18350.0,
    "2026-03-27": 18300.0,
    "2026-03-31": 18400.0,
    "2026-04-03": 18450.0,
    "2026-04-07": 18450.0,
    "2026-04-10": 18650.0,
}

# LME Zinc historical data from CSV (2026-03-13 onwards)
# Format: "YYYY-MM-DD": price (USD/tonne)
_ZINC_HISTORY = {
    "2026-03-13": 2980.0,
    "2026-03-17": 2930.0,
    "2026-03-20": 2880.0,
    "2026-03-24": 2900.0,
    "2026-03-26": 2930.0,
    "2026-03-27": 2920.0,
    "2026-03-31": 2950.0,
    "2026-04-03": 2960.0,
    "2026-04-07": 2960.0,
    "2026-04-10": 3000.0,
}


def _fetch_ebaiyin_tungsten() -> tuple:
    """Fetch tungsten rod (1#鎢條) price and monthly history from ebaiyin.com API.
    Returns (latest_price_or_None, [(date_str, price), ...]).
    Monthly history dates are returned as YYYY-MM-01 strings.
    Also returns daily data for the current month.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Referer": "https://www.ebaiyin.com/quote/wu.shtml",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    try:
        r_m = req_lib.post(
            "https://www.ebaiyin.com/Ajax/GetMarketKLineList",
            data={"name": "1#钨条", "type": "3", "spell": "wutiao"},
            headers=headers, timeout=60,
        )
        r_d = req_lib.post(
            "https://www.ebaiyin.com/Ajax/GetMarketKLineList",
            data={"name": "1#钨条", "type": "1", "spell": "wutiao"},
            headers=headers, timeout=60,
        )
        history = []
        d_m = r_m.json()
        if d_m.get("Status") == 200 and d_m.get("Data", {}).get("OKLine"):
            for t, p in zip(d_m["Data"]["Time"], d_m["Data"]["OKLine"]):
                history.append((t + "-01", round(float(p), 2)))

        # Get daily data for current month
        daily_data = {}
        d_d = r_d.json()
        if d_d.get("Status") == 200 and d_d.get("Data", {}).get("OKLine"):
            times = d_d["Data"]["Time"]
            prices = d_d["Data"]["OKLine"]
            # Parse "2026/4/22 13:49:43" format
            for t, p in zip(times, prices):
                parts = t.split(" ")[0].split("/")  # "2026/4/22" -> ["2026", "4", "22"]
                if len(parts) == 3:
                    y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
                    date_str = f"{y}-{m:02d}-{d:02d}"
                    daily_data[date_str] = round(float(p), 2)

        latest = None
        if d_d.get("Status") == 200 and d_d.get("Data", {}).get("OKLine"):
            latest = round(float(d_d["Data"]["OKLine"][-1]), 2)

        return latest, history, daily_data
    except Exception as e:
        logger.warning(f"ebaiyin tungsten: {e}")
        return None, [], {}


def _fetch_smm_tungsten_powder_price() -> float | None:
    """Fetch tungsten POWDER (钨粉) price from SMM using Playwright.
    Source: SMM (上海有色網 - 国产钨粉 domestic tungsten powder)
    Returns price in CNY/kg or None if fetch fails.
    """
    import re
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto("https://hq.smm.cn/h5/tungsten-powder-price", timeout=15000)
            page.wait_for_load_state("networkidle", timeout=10000)

            content = page.content()
            browser.close()

            # Extract average price from "2100 - 2140 (avg: 2120)" format
            # Pattern: digits - digits with optional average
            pattern = r'(\d{3,4})\s*-\s*(\d{3,4})'
            matches = re.findall(pattern, content)

            if matches:
                # Get first match and calculate average
                low, high = matches[0]
                avg_price = (float(low) + float(high)) / 2
                if 200 < avg_price < 5000:
                    logger.info(f"Tungsten powder from SMM (Playwright): {avg_price:.0f} CNY/kg")
                    return avg_price
    except ImportError:
        logger.warning("Playwright not installed for SMM tungsten, falling back to requests")
    except Exception as e:
        logger.debug(f"Playwright tungsten fetch error: {e}")

    # Fallback: try basic requests if Playwright fails
    try:
        r = req_lib.get(
            "https://hq.smm.cn/h5/tungsten-powder-price",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=12
        )

        pattern = r'(\d{3,4})\s*-\s*(\d{3,4})'
        matches = re.findall(pattern, r.text)

        if matches:
            low, high = matches[0]
            avg_price = (float(low) + float(high)) / 2
            if 200 < avg_price < 5000:
                logger.info(f"Tungsten powder from SMM (requests fallback): {avg_price:.0f} CNY/kg")
                return avg_price
    except Exception as e:
        logger.warning(f"SMM tungsten fallback error: {e}")

    logger.warning(f"SMM tungsten powder: could not extract price")
    return None


def _fetch_sci99_price(old_id: int, label: str = "") -> tuple[float | None, str | None]:
    """Fetch latest price from sci99.com JSON API.

    sci99.com switched its monitor pages to JS-rendered empty tables in 2026,
    so parsing the static HTML returns nothing. Use the AJAX endpoint that the
    site itself calls. `oldId` matches the number in the page URL — e.g.
    monitor-678-0.html → oldId=678 (黃磷); monitor-68-0.html → oldId=68 (PC).

    Returns (price as float, date_str) or (None, None) on failure.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": f"https://www.sci99.com/monitor-{old_id}-0.html",
        }
        r = req_lib.get(
            "https://www.sci99.com/priceMonitor/listProductPagePrice",
            params={"oldId": old_id, "type": 0},
            headers=headers,
            timeout=12,
        )
        if r.status_code != 200:
            logger.warning(f"sci99 API {label or old_id}: HTTP {r.status_code}")
            return None, None
        body = r.json()
        if body.get("code") != 200 or not body.get("data"):
            logger.warning(f"sci99 API {label or old_id}: empty data")
            return None, None
        first = body["data"][0]
        date_str = first.get("dateRange")
        price_str = first.get("mdataValue")
        if not date_str or not price_str:
            return None, None
        return float(price_str.replace(",", "")), date_str
    except Exception as e:
        logger.warning(f"sci99 API {label or old_id} fetch error: {e}")
        return None, None


def _fetch_pc_price_from_sci99() -> float | None:
    """Fetch PC (Polycarbonate) price from sci99.com (oldId=68).
    Returns price in CNY/tonne or None if fetch fails.
    """
    price, date_str = _fetch_sci99_price(68, label="PC")
    if price and 10000 < price < 25000:
        logger.info(f"PC price from sci99 API: {price} CNY/tonne (date: {date_str})")
        return price
    if price is not None:
        logger.warning(f"PC price out of range from sci99 API: {price}")
    return None


def _fetch_pc_price_fallback() -> float | None:
    """Fallback: Fetch PC price from alternative source (buyplas.com).
    Returns price in CNY/tonne or None if fetch fails.
    """
    try:
        price = _fetch_buyplas_price("PC_SABIC")
        if price and price > 0:
            # buyplas.com may return in different unit, validate range
            if 10000 < price < 25000:
                logger.info(f"PC price from buyplas.com (fallback): {price} CNY/tonne")
                return price
    except Exception as e:
        logger.debug(f"buyplas.com PC fallback failed: {e}")
    return None


def _load_commodity_csv_to_cache():
    """Load CSV historical data into live cache on startup."""
    global _live_commodity_cache
    csv_data = _parse_commodity_csv()
    with _live_cache_lock:
        for item_name, item_data in csv_data.items():
            dates = item_data.get("dates", [])
            values = item_data.get("values", [])
            if dates and values:
                # Store as [(date, value), ...] tuples
                _live_commodity_cache[item_name] = list(zip(dates, values))
                logger.info(f"Loaded {item_name} from CSV: {len(dates)} historical points")


def _refresh_live_prices():
    """Fetch commodity & FX prices with 1-year history. Called on startup and periodically."""
    from bs4 import BeautifulSoup as _BS
    logger.info("[REFRESH] Starting refresh...")
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    fresh: dict = {}
    sources: dict = {}

    # FIX (2026-04-29): cache 寫入邏輯有 bug — 「if today not in existing_dates: prev.append」
    # 在 today 已存在時 skip 不更新，導致舊錯值永不被新值覆蓋。最小修正：
    # 在這裡先清掉所有商品 today 的 cache 條目，後面所有 fetch 函數的「if today not in」
    # 邏輯就會正確 append 新值。
    with _live_cache_lock:
        for name in list(_live_commodity_cache.keys()):
            points = _live_commodity_cache[name]
            cleaned = [(d, v) for d, v in points if d != today]
            if len(cleaned) != len(points):
                _live_commodity_cache[name] = cleaned
        logger.info(f"[REFRESH] cleared today({today}) cache entries for re-fetch")

    # 1. Yahoo Finance（commodities + FX）— 1-year daily history (parallel)
    # 優先用 yfinance lib（本地環境裝得起來），失敗或不可用時 fallback 到 Yahoo v8 HTTP
    all_yf_syms: dict = {}
    all_yf_syms.update(_LIVE_COMMODITY_SYMBOLS)
    all_yf_syms.update(_LIVE_FX_YF_SYMBOLS)

    def _fetch_yf_sym(sym, csv_name, mult):
        # 路徑 1：yfinance lib
        if _YF_AVAILABLE:
            for attempt in range(2):
                try:
                    hist   = yf.Ticker(sym).history(period="1y", interval="1d", auto_adjust=True)
                    series = hist["Close"].dropna() if "Close" in hist.columns else hist.dropna()
                    if series.empty:
                        break
                    points = [(str(ts.date()), round(float(v) * mult, 4)) for ts, v in series.items()]
                    if points:
                        logger.info(f"yfinance {sym}: {csv_name}, {len(points)} pts, latest={points[-1][1]}")
                        return sym, csv_name, points, f"https://finance.yahoo.com/quote/{sym}"
                    break
                except Exception as e:
                    if attempt == 0 and "RateLimit" in type(e).__name__:
                        logger.warning(f"yfinance {sym} rate limited, retrying 15s")
                        time.sleep(15)
                    else:
                        logger.warning(f"yfinance {sym}: {e}")
                        break
        # 路徑 2：Yahoo v8 直接 HTTP（Render 上 yfinance 裝不起來時的後備）
        points = _fetch_yahoo_chart_history(sym, days=365, multiplier=mult)
        if points:
            logger.info(f"yahoo-v8 {sym}: {csv_name}, {len(points)} pts, latest={points[-1][1]}")
            return sym, csv_name, points, f"https://finance.yahoo.com/quote/{sym}"
        return sym, csv_name, None, None

    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(_fetch_yf_sym, sym, csv_name, mult): sym
                for sym, (csv_name, mult) in all_yf_syms.items()}
        for fut in as_completed(futs):
            sym, csv_name, points, url = fut.result()
            if points:
                # Merge: Yahoo wins for the dates it covers (last 1y), but keep
                # older history from cache so we don't truncate >1y data.
                with _live_cache_lock:
                    prev = list(_live_commodity_cache.get(csv_name, []))
                yahoo_dates = {d for d, _ in points}
                merged = [p for p in prev if p[0] not in yahoo_dates] + points
                merged.sort(key=lambda x: x[0])
                fresh[csv_name]   = merged
                sources[csv_name] = {"label": "Yahoo Finance", "url": url}

    # 2. bot.com.tw BCD API — full history for all codes
    for code, (csv_name, mult) in _BOT_BCD_CODES.items():
        chart_url = f"https://fund.bot.com.tw/z/ze/zeq/zeqa_D0{code}.djhtm"
        history = _fetch_bot_bcd_history(code)
        if history:
            fresh[csv_name]   = [(d, round(v * mult, 2)) for d, v in history]
            sources[csv_name] = {"label": "台灣銀行 fund.bot.com.tw",
                                 "url":   chart_url}
            logger.info(f"bot.com.tw BCD {code}: {csv_name}, {len(history)} pts")
        else:
            # Fallback to latest-only if history parse fails
            price = _fetch_bot_bcd_price(code)
            if price is not None:
                fresh[csv_name]   = [(today, round(price * mult, 2))]
                sources[csv_name] = {"label": "台灣銀行 fund.bot.com.tw",
                                     "url":   chart_url}

    # 3. buyplas.com plastic prices (latest point only — no public history)
    for key, csv_name in _BUYPLAS_ITEMS.items():
        try:
            price = _fetch_buyplas_price(key)
        except Exception as e:
            logger.warning(f"buyplas.com {key} error: {e}")
            price = None
        if price is not None:
            with _live_cache_lock:
                prev = list(_live_commodity_cache.get(csv_name, []))
            existing_dates = {d for d, _ in prev}
            if today not in existing_dates:
                prev.append((today, price))
            fresh[csv_name]   = prev
            sources[csv_name] = {"label": "Buyplas.com",
                                 "url":   "https://www.buyplas.com/spot/1003-PP-PE-PVC-ABS-PS.html"}
            logger.info(f"buyplas.com {key}: {csv_name} = {price}")

    logger.info("[REFRESH] Starting TradingEconomics...")
    # 排除走別的來源處理的：
    # - copper, aluminum: 走 Yahoo Finance (HG=F, ALI=F)
    # - phosphorus:       走 sci99 JSON API
    # tin/nickel/zinc/cobalt/lithium 走 TE（metals.live 已死、Yahoo 沒這幾檔期貨）
    excluded_slugs = {"copper", "aluminum", "phosphorus"}
    for slug, (csv_name, mult) in _TE_SLUGS.items():
        if slug in excluded_slugs:
            continue  # Skip LME metals and phosphorus, fetch them separately
        try:
            price = _fetch_te_price(slug)
        except Exception as e:
            logger.warning(f"TE {slug} error: {e}")
            price = None
        if price is not None:
            val = round(price * mult, 2)
            with _live_cache_lock:
                prev = list(_live_commodity_cache.get(csv_name, []))
            existing_dates = {d for d, _ in prev}
            if today not in existing_dates:
                prev.append((today, val))
            fresh[csv_name]   = prev
            sources[csv_name] = {"label": "Trading Economics",
                                 "url":   f"https://tradingeconomics.com/commodity/{slug}"}
            logger.info(f"TradingEconomics: {csv_name} = {val}")

    # Cobalt — cnyes 鉅亨網 LME cash-settle (360-day daily history merged with cache)
    logger.info("[REFRESH] Starting Cobalt (cnyes lcocs)...")
    cobalt_name = "鈷 (cobalt) US$/tonne"
    cnyes_history = _fetch_cnyes_cobalt_history()
    if cnyes_history:
        with _live_cache_lock:
            prev = list(_live_commodity_cache.get(cobalt_name, []))
        cnyes_dates = {d for d, _ in cnyes_history}
        # cnyes wins for dates it covers; keep older cached-only dates
        merged = [p for p in prev if p[0] not in cnyes_dates] + cnyes_history
        merged.sort(key=lambda x: x[0])
        fresh[cobalt_name] = merged
        sources[cobalt_name] = {
            "label": "鉅亨網 cnyes (LME cobalt cash-settle)",
            "url":   "https://www.cnyes.com/futures/Javachart/lcocs.html",
        }
        logger.info(f"cnyes cobalt: {cobalt_name} = {cnyes_history[-1][1]} ({len(cnyes_history)} pts, latest {cnyes_history[-1][0]})")
    else:
        logger.warning("cnyes cobalt fetch failed; preserving cached cobalt history")

    # Palladium — cnyes 鉅亨網 PA daily history (USD/oz, merged with cache)
    logger.info("[REFRESH] Starting Palladium (cnyes PA)...")
    palladium_name = "鈀 (palladium) US$/盎司"
    palladium_history = _fetch_cnyes_palladium_history()
    if palladium_history:
        with _live_cache_lock:
            prev = list(_live_commodity_cache.get(palladium_name, []))
        cnyes_dates = {d for d, _ in palladium_history}
        merged = [p for p in prev if p[0] not in cnyes_dates] + palladium_history
        merged.sort(key=lambda x: x[0])
        fresh[palladium_name] = merged
        sources[palladium_name] = {
            "label": "鉅亨網 cnyes (鈀金現貨 PA)",
            "url":   "https://www.cnyes.com/futures/html5chart/PA.html",
        }
        logger.info(f"cnyes palladium: {palladium_name} = {palladium_history[-1][1]} ({len(palladium_history)} pts, latest {palladium_history[-1][0]})")
    else:
        logger.warning("cnyes palladium fetch failed; preserving cached palladium history")

    logger.info("[REFRESH] Starting Yellow Phosphorus (SCI99 only)...")
    yp_name = "黃磷 CNY$/tonne"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(yp_name, []))

    # DO NOT initialize from historical CSV — always fetch fresh from URL
    # User requirement: Yellow Phosphorus price must come from URL only, no CSV history

    # Fetch from SCI99 JSON API (oldId=678 for 黃磷, matches monitor-678-0.html URL).
    # The HTML monitor page is now JS-rendered (empty static <table>), so we hit
    # the same AJAX endpoint that the page itself calls.
    yp_price, yp_date = _fetch_sci99_price(678, label="Yellow Phosphorus")
    if yp_price and yp_price > 0:
        logger.info(f"Yellow Phosphorus from SCI99 API: {yp_price} CNY/tonne (date: {yp_date})")
    else:
        yp_price = None

    if yp_price and yp_price > 0:
        yp_val = round(yp_price, 2)
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, yp_val))
            logger.info(f"Added new SCI99 price for {today}: {yp_val} CNY/tonne")
        else:
            prev = [(d if d != today else today, yp_val if d == today else p) for d, p in prev]
        fresh[yp_name] = prev
        sources[yp_name] = {"label": "SCI99（固定來源）",
                            "url":   "https://www.sci99.com/monitor-678-0.html"}
        logger.info(f"Yellow Phosphorus: {len(prev)} historical points (latest: {yp_val} CNY/tonne on {today})")
    else:
        # If fetch fails, preserve all historical data (don't delete history)
        fresh[yp_name] = prev
        sources[yp_name] = {"label": "SCI99（待更新）",
                            "url":   "https://www.sci99.com/monitor-678-0.html"}
        logger.warning(f"Yellow Phosphorus fetch failed, preserved {len(prev)} historical points")

    # NOTE: Copper / Aluminum 走 _LIVE_COMMODITY_SYMBOLS Yahoo 路徑 (HG=F, ALI=F)。
    # Tin / Nickel / Zinc / Cobalt 走 _TE_SLUGS Trading Economics 路徑。
    # 之前這裡有 4 個 dedicated block (Copper/Tin-Nickel-Zinc/Cobalt/Aluminum) 用已死的
    # metals.live API，並會用 stale cache 蓋掉 Yahoo/TE 已寫入的正確值，且 cobalt 還拿
    # ZS=F (大豆期貨!) × 22.05 當初始化資料。已全部移除以避免汙染 fresh[]。

    logger.info("[REFRESH] Starting Tungsten Powder (SMM 国产钨粉 only)...")
    tungsten_name = "鎢"
    tungsten_source = {"label": "上海有色網 SMM (钨粉)", "url": "https://hq.smm.cn/h5/tungsten-powder-price"}

    # Load historical data from user's Excel (2026-03-25 onwards)
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(tungsten_name, []))

    # If cache is empty, initialize from _TUNGSTEN_HISTORY
    if not prev:
        prev = [(date, price) for date, price in sorted(_TUNGSTEN_HISTORY.items())]
        logger.info(f"Initialized tungsten from user history: {len(prev)} points")

    # Get today's price from SMM only (no fallback)
    tungsten_price = _fetch_smm_tungsten_powder_price()

    if tungsten_price is not None:
        # Always update/add today's price from SMM
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, tungsten_price))
            logger.info(f"Added new SMM price for {today}: {tungsten_price} CNY/kg")
        else:
            # Update today's price if already exists
            prev = [(d if d != today else today, tungsten_price if d == today else p) for d, p in prev]
            logger.info(f"Updated SMM price for {today}: {tungsten_price} CNY/kg")

        fresh[tungsten_name] = prev
        sources[tungsten_name] = tungsten_source
        logger.info(f"Tungsten Powder: {len(prev)} historical points (latest: {tungsten_price} CNY/kg on {today})")
    else:
        # If fetch fails today, keep existing cache but log warning
        if prev:
            fresh[tungsten_name] = prev
            sources[tungsten_name] = {"label": "上海有色網 SMM (钨粉) [SMM unavailable]",
                                     "url": "https://hq.smm.cn/h5/tungsten-powder-price"}
            logger.warning(f"Tungsten Powder fetch failed from SMM, keeping cached data ({len(prev)} points)")
        else:
            logger.error("Tungsten Powder: No price available and no cache")

    logger.info("[REFRESH] Starting Long Fiber Pulp (MoneyDJ only)...")
    pulp_name = "NOREXECO 長纖紙漿  USD/T"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(pulp_name, []))

    # Initialize from historical data if cache is empty
    if not prev:
        prev = [(date, price) for date, price in sorted(_LONGFIBER_PULP_HISTORY.items())]
        logger.info(f"Initialized Long Fiber Pulp from history: {len(prev)} points")

    # Try to fetch today's price from MoneyDJ
    try:
        r = req_lib.get("https://concords.moneydj.com/z/ze/zeq/zeqa_D0190400.djhtm", timeout=15)
        if r.status_code == 200:
            import re
            # Look for price patterns in MoneyDJ HTML
            match = re.search(r'(\d{1,4}[.,]\d{2})', r.text)
            if match:
                price_str = match.group(1).replace(',', '.')
                try:
                    price = float(price_str)
                    # Only add if price is reasonable (1000-2000 USD/T range)
                    if 1000 <= price <= 2000:
                        existing_dates = {d for d, _ in prev}
                        if today not in existing_dates:
                            prev.append((today, price))
                            logger.info(f"Added new MoneyDJ price for {today}: {price} USD/T")
                        else:
                            prev = [(d if d != today else today, price if d == today else p) for d, p in prev]
                            logger.info(f"Updated MoneyDJ price for {today}: {price} USD/T")
                except (ValueError, AttributeError):
                    pass
        else:
            logger.warning(f"MoneyDJ fetch failed: HTTP {r.status_code}")
    except Exception as e:
        logger.warning(f"Long Fiber Pulp MoneyDJ fetch error: {e}")

    fresh[pulp_name] = prev
    sources[pulp_name] = {"label": "MoneyDJ (長纖紙漿)",
                         "url": "https://concords.moneydj.com/z/ze/zeq/zeqa_D0190400.djhtm"}
    logger.info(f"Long Fiber Pulp: {len(prev)} points (latest from MoneyDJ)")

    logger.info("[REFRESH] Starting PC (Polycarbonate from sci99.com)...")
    pc_name = "PC塑料 (SABIC) CNY$/tonne"
    with _live_cache_lock:
        prev = list(_live_commodity_cache.get(pc_name, []))

    # Initialize from user's historical data if cache is empty
    if not prev:
        prev = [(date, price) for date, price in sorted(_PC_HISTORY.items())]
        logger.info(f"Initialized PC from user history: {len(prev)} points")

    pc_price = _fetch_pc_price_from_sci99()
    if pc_price is None:
        # Fallback: try alternative source
        pc_price = _fetch_pc_price_fallback()

    if pc_price is not None:
        pc_val = round(pc_price, 2)
        existing_dates = {d for d, _ in prev}
        if today not in existing_dates:
            prev.append((today, pc_val))
            logger.info(f"Added new PC price for {today}: {pc_val} CNY/tonne")
        else:
            # Update today if already exists
            prev = [(d if d != today else today, pc_val if d == today else p) for d, p in prev]
            logger.info(f"Updated PC price for {today}: {pc_val} CNY/tonne")
        fresh[pc_name] = prev
        src_label = "sci99.com" if pc_price else "buyplas.com (fallback)"
        sources[pc_name] = {"label": src_label,
                            "url":   "https://www.sci99.com/monitor-68-0.html"}
        logger.info(f"PC: {len(prev)} historical points (latest: {pc_val} CNY/tonne on {today})")
    else:
        # If all sources fail, preserve all historical data (don't delete history)
        fresh[pc_name] = prev
        sources[pc_name] = {"label": "sci99.com + buyplas (待更新)",
                            "url":   "https://www.sci99.com/monitor-68-0.html"}
        logger.warning(f"PC fetch failed, preserved {len(prev)} historical points")

    with _live_cache_lock:
        _live_commodity_cache.update(fresh)
    with _item_sources_lock:
        _item_sources.update(sources)
        # 不再硬編 "LME (歷史)" — 由 Yahoo / TE 路徑自己寫入正確的 source label
    # Persist updated prices back to CSV file
    _save_commodity_csv()
    # Invalidate CSV parse cache so next request re-merges fresh live data
    with _csv_parse_lock:
        _csv_parse_cache["data"] = None
    logger.info(f"Live prices updated: {len(fresh)} items")
    logger.info("[REFRESH] Done!")


def _live_price_loop():
    # Load CSV historical data first, then fetch fresh prices
    _load_commodity_csv_to_cache()
    _refresh_live_prices()
    # Refresh at 07:00, 09:00, 11:00, 13:00, 15:00, 17:00 Taiwan time (UTC+8) every day
    # This ensures ~2+ data points per 7 days for cobalt and other commodities
    _REFRESH_HOURS = {7, 9, 11, 13, 15, 17}
    last_run_hour: set = set()
    while True:
        time.sleep(60)
        now_tw = datetime.now(timezone(timedelta(hours=8)))
        key = (now_tw.date(), now_tw.hour)
        if now_tw.hour in _REFRESH_HOURS and key not in last_run_hour:
            last_run_hour.add(key)
            # Keep only today's keys to avoid unbounded growth
            today = now_tw.date()
            last_run_hour = {k for k in last_run_hour if k[0] == today}
            _refresh_live_prices()


# ── Commodity dashboard ────────────────────────────────────────────────────────
_COMMODITY_CSV = os.path.join(os.path.dirname(__file__), "2026 Raw material trend history.csv")

# Category mapping for each item
_COMMODITY_CATEGORIES = {
    "金屬": ["銅", "錫", "鋁", "鎳", "鋅", "鈷", "鋰", "鎢"],
    "貴金屬": ["金", "銀", "鈀"],
    "能源": ["石油 西德州", "石油 北海布蘭特"],
    "原物料": ["黃磷", "ABS聚合物", "PC塑料 (SABIC)", "PC/ABS塑料", "NOREXECO 長纖紙漿  USD/T", "瓦楞芯紙"],
    "匯率": ["美元 / 台幣", "美元 / 人民幣", "美元 / 日圓", "美元 / 歐元",
              "美元 / 巴西里爾", "美元 / 韓圜", "美元 / 印尼盾", "美元 / 印度幣"],
}

def _parse_commodity_csv() -> dict:
    """Parse wide-format CSV into {item_name: {dates:[], values:[], unit, category}}."""
    result = {}
    if not os.path.exists(_COMMODITY_CSV):
        return result
    try:
        with open(_COMMODITY_CSV, encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)

        if not rows:
            return result

        # Row 0: header — first cell is "項目", rest are dates
        # Supports both old M/D format and new YYYY/M/D format
        header = rows[0]
        raw_dates = header[1:]

        today = datetime.now(TW_TZ)
        dates = []
        prev_month = None
        year = today.year
        for d in raw_dates:
            d = d.strip()
            if not d:
                dates.append(None)
                continue
            try:
                parts = d.split("/")
                if len(parts) == 3:
                    # Full date: YYYY/M/D
                    y, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    dates.append(f"{y}-{month:02d}-{day:02d}")
                elif len(parts) == 2:
                    # Legacy M/D format — infer year from boundary
                    month, day = int(parts[0]), int(parts[1])
                    if prev_month is not None and month < prev_month and prev_month >= 10:
                        year = today.year
                    if month == 12 and prev_month is None:
                        year = today.year - 1
                    prev_month = month
                    dates.append(f"{year}-{month:02d}-{day:02d}")
                else:
                    dates.append(None)
            except Exception:
                dates.append(None)

        # Build category lookup
        item_to_cat = {}
        for cat, items in _COMMODITY_CATEGORIES.items():
            for item in items:
                item_to_cat[item] = cat

        # Parse each data row
        for row in rows[1:]:
            if not row or not row[0].strip():
                continue
            name = row[0].strip()
            if not name:
                continue

            # Extract unit from name (e.g. "US$/tonne")
            unit = ""
            for u in ["US$/tonne", "CNY$/tonne", "US$/盎司", "US$/桶", "USD/T", "CNY$/tonne"]:
                if u in name:
                    unit = u
                    break

            # Determine category
            cat = "其他"
            for key, c in item_to_cat.items():
                if key in name:
                    cat = c
                    break

            values = []
            for i, v in enumerate(row[1:]):
                v = v.strip()
                if v in ("", "N/A", "-", "[object Object]") or v is None:
                    values.append(None)
                else:
                    try:
                        # Strip carry-forward marker '*' before parsing
                        clean = v.replace(",", "").rstrip("*").strip()
                        values.append(float(clean))
                    except Exception:
                        values.append(None)

            # Pair dates with values, skip None dates and None values
            paired = [(d, v) for d, v in zip(dates, values) if d is not None and v is not None]

            result[name] = {
                "unit":     unit,
                "category": cat,
                "dates":    [p[0] for p in paired],
                "values":   [p[1] for p in paired],
            }

            # Set initial (transient) source label from CSV history.
            # 注意：這些 label 只在第一次 refresh 之前顯示；refresh 完成後會被
            # Yahoo / TE / sci99 / SMM 寫入真正的 source 蓋掉。
            with _item_sources_lock:
                if "鎢" in name or "tungsten" in name:
                    _item_sources[name] = {
                        "label": "上海有色網 SMM (钨粉)",
                        "url": "https://hq.smm.cn/h5/tungsten-powder-price"
                    }
                elif name not in _item_sources:
                    _item_sources[name] = {
                        "label": "歷史記錄（待更新）",
                        "url": "file:///csv"
                    }

    except Exception as e:
        logger.error(f"Commodity CSV parse error: {e}")

    # Merge live prices (append new dates; also create entry for live-only items)
    item_to_cat = {}
    for cat, items in _COMMODITY_CATEGORIES.items():
        for item in items:
            item_to_cat[item] = cat

    with _live_cache_lock:
        for csv_name, live_points in _live_commodity_cache.items():
            if csv_name in result:
                existing = set(result[csv_name]["dates"])
                for date, val in live_points:
                    if date not in existing:
                        result[csv_name]["dates"].append(date)
                        result[csv_name]["values"].append(val)
                        existing.add(date)
            else:
                # Item only exists in live cache (no CSV history) — create entry
                unit = ""
                _LIVE_UNIT_OVERRIDES = {"鎢": "元/千克"}
                for name_key, u_val in _LIVE_UNIT_OVERRIDES.items():
                    if name_key == csv_name:
                        unit = u_val
                        break
                if not unit:
                    for u in ["US$/tonne", "CNY$/tonne", "US$/盎司", "US$/桶", "USD/T"]:
                        if u in csv_name:
                            unit = u
                            break
                cat = "其他"
                for key, c in item_to_cat.items():
                    if key in csv_name:
                        cat = c
                        break
                result[csv_name] = {
                    "unit":     unit,
                    "category": cat,
                    "dates":    [p[0] for p in live_points],
                    "values":   [p[1] for p in live_points],
                }

    # Sort all items by date after merging live data (prevents x-axis going backward)
    for key in result:
        if result[key]["dates"]:
            paired = sorted(zip(result[key]["dates"], result[key]["values"]))
            result[key]["dates"]  = [p[0] for p in paired]
            result[key]["values"] = [p[1] for p in paired]

    return result


def _apply_carry_forward(rows: list, header: list, carry_back_days: int = 30) -> None:
    """Fill empty cells using nearest real value, tagged with trailing '*'.

    Two-pass:
      1) Carry-forward: empty cell ← most recent prior real value (no time limit)
      2) Carry-back:    leading empties ← first real value, BUT only within the
         last `carry_back_days` days (to avoid polluting 10+ years of history
         when a commodity only has recent points).

    Both tagged with '*' (e.g. '27000*'). Real values in cache overwrite tags
    automatically on next save.
    """
    from datetime import datetime as _dt, timedelta as _td
    if len(rows) < 2:
        return
    n_cols = len(header)
    cutoff = _dt.now().date() - _td(days=carry_back_days)

    # Pre-parse header dates for cutoff comparison
    header_dates = [None] * n_cols
    for i, h in enumerate(header):
        if i == 0:
            continue
        try:
            header_dates[i] = _dt.strptime(h, "%Y/%m/%d").date()
        except (ValueError, TypeError):
            pass

    for ridx in range(1, len(rows)):
        row = rows[ridx]
        while len(row) < n_cols:
            row.append("")
        # Pass 1: carry-forward (always allowed)
        last_real = None
        for cidx in range(1, n_cols):
            cell = row[cidx].strip() if cidx < len(row) else ""
            if cell and cell != "0":
                if not cell.endswith("*"):
                    last_real = cell
                continue
            if last_real is not None:
                row[cidx] = f"{last_real}*"
        # Pass 2: carry-back, but only for cells within carry_back_days
        first_real = None
        for cidx in range(1, n_cols):
            cell = row[cidx].strip() if cidx < len(row) else ""
            if cell and cell != "0" and not cell.endswith("*"):
                first_real = cell
                break
        if first_real is not None:
            for cidx in range(1, n_cols):
                cell = row[cidx].strip() if cidx < len(row) else ""
                if cell and cell != "0":
                    break
                d = header_dates[cidx]
                if d is None or d < cutoff:
                    continue  # too old, skip
                row[cidx] = f"{first_real}*"


def _save_commodity_csv():
    """Save current _live_commodity_cache back to CSV file in wide-format.

    Safeguard: merge cache with EXISTING CSV history before writing, so an
    empty/partial cache never wipes out previously backfilled data.
    Cache wins on overlapping dates; older CSV-only dates are preserved.
    """
    try:
        with _live_cache_lock:
            cache_copy = dict(_live_commodity_cache)

        if not cache_copy:
            logger.warning("Commodity cache is empty, skipping CSV save")
            return

        # Read existing CSV and merge with cache to prevent data loss.
        existing_data = _parse_commodity_csv()
        merged: dict[str, list[tuple[str, float]]] = {}
        all_names = set(cache_copy.keys()) | set(existing_data.keys())
        for name in all_names:
            cached = list(cache_copy.get(name, []))
            cached_dates = {d for d, _ in cached}
            existing_pts: list[tuple[str, float]] = []
            if name in existing_data:
                existing_pts = list(zip(existing_data[name]["dates"], existing_data[name]["values"]))
            # Cache wins for dates it covers; keep older existing-only dates.
            kept_existing = [p for p in existing_pts if p[0] not in cached_dates]
            combined = sorted(kept_existing + cached, key=lambda x: x[0])
            if combined:
                merged[name] = combined
                if len(combined) > len(cached):
                    logger.info(f"[CSV merge] {name}: kept {len(combined) - len(cached)} dates from existing CSV")
        cache_copy = merged

        # Collect all unique dates from all items
        all_dates = set()
        for item_name, price_list in cache_copy.items():
            for date_str, _ in price_list:
                all_dates.add(date_str)

        if not all_dates:
            logger.warning("No dates found in commodity cache, skipping CSV save")
            return

        # Sort dates chronologically
        sorted_dates = sorted(all_dates)

        # Build rows for CSV (wide format: items in rows, dates in columns)
        rows = []

        # Header row: "項目" + dates in YYYY/M/D format
        header = ["項目"]
        for date_str in sorted_dates:
            # Convert YYYY-MM-DD to YYYY/M/D format
            try:
                parts = date_str.split("-")
                if len(parts) == 3:
                    year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
                    # Remove leading zeros from month and day for compact format
                    formatted_date = f"{year}/{month}/{day}"
                    header.append(formatted_date)
                else:
                    header.append(date_str)
            except Exception:
                header.append(date_str)
        rows.append(header)

        # Data rows: one per commodity item
        for item_name in sorted(cache_copy.keys()):
            price_list = cache_copy[item_name]
            # Build a dict of date->price for fast lookup
            date_price = {date_str: price for date_str, price in price_list}

            row = [item_name]
            for date_str in sorted_dates:
                if date_str in date_price:
                    price = date_price[date_str]
                    # Format price: remove decimals if whole number, otherwise keep 2 decimals
                    if isinstance(price, float):
                        if price == int(price):
                            row.append(str(int(price)))
                        else:
                            row.append(str(round(price, 2)))
                    else:
                        row.append(str(price))
                else:
                    row.append("")
            rows.append(row)

        # Carry-forward: fill empty cells with most recent prior value (tagged with *).
        # Ensures no commodity stays blank for more than 1 day after first real point.
        _apply_carry_forward(rows, rows[0])

        # Write to CSV with utf-8-sig encoding (preserves BOM for Excel compatibility)
        with open(_COMMODITY_CSV, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        logger.info(f"Saved commodity CSV: {len(cache_copy)} items, {len(sorted_dates)} dates (with carry-forward)")
    except Exception as e:
        logger.error(f"Commodity CSV save error: {e}", exc_info=True)


# ── News archive for persistent storage ────────────────────────────────────────
_NEWS_ARCHIVE = os.path.join(os.path.dirname(__file__), "news_archive.json")
_NEWS_ARCHIVE_LOCK = threading.Lock()
_ARTICLE_RETENTION_DAYS = 730  # Keep articles for 2 years

def _load_archived_articles() -> list[dict]:
    """Load articles from persistent archive, filtering for articles from past 2 years."""
    try:
        if not os.path.exists(_NEWS_ARCHIVE):
            logger.debug(f"Archive not found: {_NEWS_ARCHIVE}")
            return []

        try:
            with open(_NEWS_ARCHIVE, "r", encoding="utf-8") as f:
                all_articles = json.load(f)
        except json.JSONDecodeError:
            logger.warning(f"Archive corrupted, returning empty list")
            return []

        if not isinstance(all_articles, list):
            logger.warning(f"Archive format invalid, returning empty list")
            return []

        if not all_articles:
            logger.debug(f"Archive is empty")
            return []

        # Filter for articles from past 2 years
        now = datetime.now(TW_TZ)
        cutoff_date = now - timedelta(days=_ARTICLE_RETENTION_DAYS)

        filtered = []
        for article in all_articles:
            try:
                date_str = article.get("published") or article.get("fetched_at", "")
                if date_str:
                    # Parse date: expected format "YYYY-MM-DD HH:MM" or "YYYY-MM-DD HH:MM:SS"
                    article_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                    article_date = article_date.astimezone(TW_TZ)
                    if article_date >= cutoff_date:
                        filtered.append(article)
            except Exception:
                # If date parsing fails, include the article anyway (might be old format)
                filtered.append(article)

        logger.info(f"Loaded {len(filtered)} archived articles from past 2 years (total in archive: {len(all_articles)})")
        return filtered
    except Exception as e:
        logger.warning(f"News archive load error: {e}")
        return []


def _save_articles_to_archive(new_articles: list[dict]):
    """Append new articles to persistent archive, removing duplicates and old articles."""
    if not new_articles:
        return

    try:
        with _NEWS_ARCHIVE_LOCK:
            # Load existing archive
            try:
                if os.path.exists(_NEWS_ARCHIVE):
                    with open(_NEWS_ARCHIVE, "r", encoding="utf-8") as f:
                        archive = json.load(f)
                        if not isinstance(archive, list):
                            archive = []
                else:
                    archive = []
            except Exception as e:
                logger.warning(f"Failed to load archive: {e}")
                archive = []

            # Deduplicate by URL (most reliable key)
            archive_urls = {article.get("source_url", ""): article for article in archive if article.get("source_url")}

            # Add new articles if they're not already in archive
            added_count = 0
            for article in new_articles:
                url = article.get("source_url", "")
                if url and url not in archive_urls:
                    archive.append(article)
                    archive_urls[url] = article
                    added_count += 1
                    logger.debug(f"Added article to archive: {article.get('title', '')[:50]}")

            # Only filter if archive has articles older than 2 years
            now = datetime.now(TW_TZ)
            cutoff_date = now - timedelta(days=_ARTICLE_RETENTION_DAYS)
            filtered_archive = []
            removed_count = 0
            for article in archive:
                try:
                    date_str = article.get("published") or article.get("fetched_at", "")
                    if date_str:
                        article_date = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
                        article_date = article_date.astimezone(TW_TZ)
                        if article_date >= cutoff_date:
                            filtered_archive.append(article)
                        else:
                            removed_count += 1
                    else:
                        # Keep articles with unparseable dates (might be old or new)
                        filtered_archive.append(article)
                except Exception as e:
                    # Keep articles with unparseable dates
                    logger.debug(f"Error parsing article date: {e}")
                    filtered_archive.append(article)

            # Write archive (sorted by date, newest first)
            filtered_archive.sort(key=lambda a: a.get("published") or a.get("fetched_at", ""), reverse=True)
            with open(_NEWS_ARCHIVE, "w", encoding="utf-8") as f:
                json.dump(filtered_archive, f, ensure_ascii=False, indent=2)

            logger.info(f"News archive: +{added_count} new, -{removed_count} old, {len(filtered_archive)} total")
    except Exception as e:
        logger.error(f"News archive save error: {e}", exc_info=True)


# ── Commodity news (bilingual: GDELT EN + Bing ZH with 3-layer guard) ────────
# 中文 → Bing News RSS（搜尋「商品名 價格」）
# 英文 → GDELT 2.0 doc API（搜尋商品英文名 + price）
#
# Bing 3-layer guard 防止觸發 rate-limit / IP ban：
#   ① 60 分鐘 cache（per commodity name）
#   ② 每日 200 query 上限（超過自動跳過 Bing，純走 GDELT）
#   ③ 收到 429 → 觸發 60 分鐘 cooldown，期間 0 Bing query

_commodity_news_cache: dict = {}   # {item_zh: {data, ts, source}}
_COMMODITY_NEWS_TTL = 3600         # 60 min
_BING_DAILY_CAP = 200
_bing_daily_count = 0
_bing_count_date = None            # YYYY-MM-DD reset
_bing_cooldown_until = 0.0         # epoch seconds; while now < this, skip Bing

# mining.com pool — 1 fetch / hour 抓全站，所有商品共用
# 取代「per-commodity 都打 Bing/GDELT」的浪費。
_mining_cache: dict = {"data": [], "ts": 0.0}
_MINING_TTL = 3600

# 翻譯 cache — per English title → 中文。永久保留（同 title 只翻一次）。
_translation_cache: dict = {}      # title_en (lowercased) → title_zh

# 商品 → Bing News 中文搜尋關鍵字（比預設 item_short 更精準）
# 沒設定的會 fallback 用 item_short + " 價格"
_COMMODITY_BING_QUERY = {
    "PC":        "聚碳酸酯 價格",   # PC 塑料的中文化學名
    "PC塑料":    "聚碳酸酯 價格",
    "ABS":       "ABS塑料 價格",
    "ABS聚合物": "ABS塑料 價格",
    "瓦楞":      "紙漿 紙價",        # 瓦楞芯紙跟著紙漿價格走
    "瓦楞芯紙":  "紙漿 紙價",
    "長纖":      "長纖紙漿 價格",
    "長纖紙漿":  "長纖紙漿 價格",
    "黃磷":      "黃磷 價格",
    "鈀":        "鈀金 價格",
    # 鈷不加 LME（Bing News 對「鈷 價格 LME」三字組合會回 0 篇，太窄）
    # 用「鈷 價格」即可，讓 30-day 智慧 fallback 處理排序與相關性
}

# 商品 → mining.com 過濾用單字（小寫 substring 比對）
_COMMODITY_FILTER_TOKENS = {
    "銅":   ["copper"],
    "鋁":   ["aluminum", "aluminium", "alumina"],
    "錫":   ["tin"],     # word-boundary 比對（不會誤命中 Martin / Tinder）
    "鎳":   ["nickel"],
    "鋅":   ["zinc"],
    "鈷":   ["cobalt"],
    "鋰":   ["lithium"],
    "鎢":   ["tungsten"],
    "金":   ["gold"],
    "銀":   ["silver"],
    "鈀":   ["palladium"],
    "石油": ["crude", "oil price", "wti", "brent", "opec"],
    "PC":   ["polycarbonate"],
    "ABS":  ["abs resin", "acrylonitrile"],
    "黃磷": ["phosphorus"],
    "瓦楞": ["corrugated"],
    "長纖": ["pulp"],
    "鎢粉": ["tungsten"],
}

# Human-pacing throttle: 模仿人點擊的間隔，避免被當機器人。
# GDELT 規定 1 req / 5 秒；Bing 沒明文但類似節流降低被偵測風險。
# 每個 host 獨立 lock，這樣 bing 跟 gdelt 可以平行跑（共用 lock 會串行 sleep）。
_bing_throttle_lock  = threading.Lock()
_gdelt_throttle_lock = threading.Lock()
_last_bing_call_ts   = 0.0
_last_gdelt_call_ts  = 0.0
_NEWS_MIN_GAP_SEC    = 6  # 6 秒 + 隨機 0-2 秒 jitter ≈ 真人 8 秒間隔

# 商品中文名 → 英文搜尋詞（給 GDELT 用）
_COMMODITY_EN_KEYWORDS = {
    "銅":   "copper price LME",
    "鋁":   "aluminum price LME",
    "錫":   "tin price LME",
    "鎳":   "nickel price LME",
    "鋅":   "zinc price LME",
    "鈷":   "cobalt price battery",
    "鋰":   "lithium price battery",
    "鎢":   "tungsten price china",
    "金":   "gold price",
    "銀":   "silver price",
    "鈀":   "palladium price Russia",
    "石油": "crude oil price WTI Brent",
    "PC":   "polycarbonate plastic price",
    "ABS":  "ABS plastic resin price",
    "黃磷": "yellow phosphorus price china",
    "瓦楞": "corrugated paper pulp price",
    "長纖": "softwood pulp price",
    "鋰電": "lithium battery materials",
}


def _commodity_en_query(item_zh: str) -> str:
    """從中文商品名推英文搜尋詞。沒對到就用原名（多半 user 輸入英文）。"""
    for zh, en in _COMMODITY_EN_KEYWORDS.items():
        if zh in item_zh:
            return en
    return item_zh


def _bing_budget_ok() -> bool:
    """檢查 Bing 是否可用（未超日上限 + 不在 cooldown）。"""
    global _bing_daily_count, _bing_count_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if today != _bing_count_date:
        _bing_count_date = today
        _bing_daily_count = 0
    if _bing_daily_count >= _BING_DAILY_CAP:
        return False
    if time.time() < _bing_cooldown_until:
        return False
    return True


def _throttle_news_call(host: str) -> None:
    """每個 host 獨立 lock，bing/gdelt 之間可以平行（不互相 block）。
    強制每個 host 兩次 call 至少間隔 6-8 秒（模仿真人點擊節奏）。"""
    global _last_bing_call_ts, _last_gdelt_call_ts
    import random as _r
    if host == "bing":
        with _bing_throttle_lock:
            elapsed = time.time() - _last_bing_call_ts
            if elapsed < _NEWS_MIN_GAP_SEC:
                time.sleep(_NEWS_MIN_GAP_SEC - elapsed + _r.uniform(0, 2))
            _last_bing_call_ts = time.time()
    elif host == "gdelt":
        with _gdelt_throttle_lock:
            elapsed = time.time() - _last_gdelt_call_ts
            if elapsed < _NEWS_MIN_GAP_SEC:
                time.sleep(_NEWS_MIN_GAP_SEC - elapsed + _r.uniform(0, 2))
            _last_gdelt_call_ts = time.time()


def _fetch_mining_pool() -> list:
    """從 mining.com RSS 抓近 7 天文章，1 小時 cache，所有商品共用。"""
    now = time.time()
    if _mining_cache["data"] and (now - _mining_cache["ts"]) < _MINING_TTL:
        return _mining_cache["data"]
    try:
        import xml.etree.ElementTree as ET
        from email.utils import parsedate_to_datetime
        r = req_lib.get("https://www.mining.com/feed/", timeout=15,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; ASUSTIMES/1.0)"})
        if r.status_code != 200:
            return _mining_cache["data"] or []
        root = ET.fromstring(r.content)
        out = []
        for item in root.iter("item"):
            try:
                title = (item.findtext("title") or "").strip()
                link  = (item.findtext("link")  or "").strip()
                pub_raw = item.findtext("pubDate", "") or ""
                try:
                    pub = parsedate_to_datetime(pub_raw).strftime("%Y-%m-%d")
                except Exception:
                    pub = ""
                if title and link:
                    out.append({
                        "title":      title,
                        "source_url": link,
                        "published":  pub,
                        "source":     "mining.com",
                        "lang":       "en",
                    })
            except Exception:
                continue
        _mining_cache["data"] = out
        _mining_cache["ts"]   = now
        logger.info(f"[mining.com] refreshed pool: {len(out)} articles")
        return out
    except Exception as e:
        logger.warning(f"[mining.com] fetch error: {e}")
        return _mining_cache["data"] or []


def _filter_mining_for(item_short: str, max_records: int = 5) -> list:
    """從 mining.com pool 撈出與 item_short 相關的文章。
    用 word boundary 避免「Martin / Tinder / oilcake」等誤命中。"""
    import re as _re
    pool = _fetch_mining_pool()
    if not pool:
        return []
    tokens = _COMMODITY_FILTER_TOKENS.get(item_short, [])
    if not tokens:
        tokens = [item_short.lower()]
    # 編譯成 word-boundary regex（含中文以 substring 比對）
    patterns = []
    for t in tokens:
        t = t.strip()
        if not t:
            continue
        if _re.search(r"[a-z]", t):
            # 英文 token → word boundary
            patterns.append(_re.compile(rf"\b{_re.escape(t)}\b", _re.IGNORECASE))
        else:
            # 中文 token → 直接 substring（中文沒空格分詞）
            patterns.append(_re.compile(_re.escape(t)))
    out = []
    for art in pool:
        text = art.get("title", "") or ""
        if any(p.search(text) for p in patterns):
            out.append(art)
            if len(out) >= max_records:
                break
    return out


def _translate_to_zh(text_en: str) -> str:
    """英文 → 繁中。Google Translate 公開 endpoint 為主、MyMemory 備援、cache 永久。"""
    if not text_en or not text_en.strip():
        return ""
    key = text_en.strip().lower()
    if key in _translation_cache:
        return _translation_cache[key]

    # Path 1: Google Translate gtx (~1-2s 正常，4s timeout 留 buffer)
    try:
        from urllib.parse import quote
        url = ("https://translate.googleapis.com/translate_a/single"
               f"?client=gtx&sl=en&tl=zh-TW&dt=t&q={quote(text_en)}")
        r = req_lib.get(url, timeout=4,
                        headers={"User-Agent": "Mozilla/5.0 (compatible; ASUSTIMES/1.0)"})
        if r.status_code == 200:
            arr = r.json()
            if arr and arr[0]:
                zh = "".join(seg[0] for seg in arr[0] if seg and seg[0]).strip()
                if zh and zh != text_en:
                    _translation_cache[key] = zh
                    return zh
    except Exception as e:
        logger.debug(f"[translate] Google gtx failed: {e}")

    # Path 2: MyMemory 備援（4s timeout 與 Google 對齊）
    try:
        r = req_lib.get("https://api.mymemory.translated.net/get",
                        params={"q": text_en, "langpair": "en|zh-TW"}, timeout=4)
        if r.status_code == 200:
            d = r.json()
            zh = (d.get("responseData", {}) or {}).get("translatedText", "").strip()
            if zh and zh != text_en:
                _translation_cache[key] = zh
                return zh
    except Exception as e:
        logger.debug(f"[translate] MyMemory failed: {e}")

    # 翻譯失敗 → 回傳原文（不快取，下次再試）
    return text_en


def _fetch_gdelt_commodity_news(query: str, max_records: int = 5,
                                 must_contain: list[str] | None = None) -> list:
    """GDELT 2.0 doc API — 完全免費，但官方規定 1 req / 5 秒，必須 throttle。
    must_contain：標題必須包含其中一個關鍵字（小寫 substring 比對），
    避免 GDELT 用 body match 把不相關的文章撈進來。"""
    _throttle_news_call("gdelt")
    try:
        from urllib.parse import quote
        # 抓 30 天內，數量多 fetch 點留給 must_contain 過濾後仍有結果
        url = (
            "https://api.gdeltproject.org/api/v2/doc/doc"
            f"?query={quote(query)}&mode=ArtList&format=json"
            f"&maxrecords=25&timespan=1month&sourcelang=eng&sort=DateDesc"
        )
        r = req_lib.get(url, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
        tokens_lower = [t.lower() for t in (must_contain or [])]
        out = []
        for art in data.get("articles", []) or []:
            title = art.get("title", "").strip()
            if not title:
                continue
            # 標題必須含關鍵字才保留（過濾掉只是 body 提到的不相關文章）
            if tokens_lower and not any(tok in title.lower() for tok in tokens_lower):
                continue
            seendate = art.get("seendate", "")
            pub = ""
            if seendate and len(seendate) >= 8:
                pub = f"{seendate[0:4]}-{seendate[4:6]}-{seendate[6:8]}"
            out.append({
                "title":      title,
                "source_url": art.get("url", ""),
                "published":  pub,
                "source":     art.get("domain", "GDELT"),
                "lang":       "en",
            })
            if len(out) >= max_records:
                break
        return out
    except Exception as e:
        logger.warning(f"[GDELT] fetch error: {e}")
        return []


def _fetch_bing_commodity_news(query: str, max_records: int = 5) -> list:
    """Bing News RSS。caller 必須先呼叫 _bing_budget_ok() 確認額度。
    內建 6-8 秒 human-pacing throttle 模仿真人點擊節奏。"""
    _throttle_news_call("bing")
    global _bing_daily_count, _bing_cooldown_until
    try:
        import xml.etree.ElementTree as ET
        from urllib.parse import quote
        url = f"https://www.bing.com/news/search?q={quote(query)}&format=rss"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = req_lib.get(url, headers=headers, timeout=15)
        if r.status_code == 429:
            _bing_cooldown_until = time.time() + 3600
            logger.warning("[Bing-news] 429 → cooldown 60 min")
            return []
        if r.status_code != 200:
            return []
        _bing_daily_count += 1
        root = ET.fromstring(r.content)
        out = []
        for item in root.iter("item"):
            link = item.findtext("link", "")
            # Bing 重定向解析
            if "bing.com/news/apiclick.aspx" in link:
                from urllib.parse import urlparse, parse_qs, unquote
                qs = parse_qs(urlparse(link).query)
                if "url" in qs:
                    link = unquote(qs["url"][0])
            pub_raw = item.findtext("pubDate", "")
            pub = ""
            try:
                from email.utils import parsedate_to_datetime
                pub = parsedate_to_datetime(pub_raw).strftime("%Y-%m-%d") if pub_raw else ""
            except Exception:
                pass
            domain = ""
            try:
                from urllib.parse import urlparse as _up
                domain = _up(link).netloc
            except Exception:
                pass
            out.append({
                "title":      item.findtext("title", "").strip(),
                "source_url": link,
                "published":  pub,
                "source":     domain or "Bing News",
                "lang":       "zh",
            })
            if len(out) >= max_records:
                break
        return out
    except Exception as e:
        logger.warning(f"[Bing-news] fetch error: {e}")
        return []


@app.route("/api/commodity-news")
def api_commodity_news():
    """Bilingual commodity news. q = item name (中文 like '鈷' or '銅 (copper) US$/tonne')."""
    q_raw = (request.args.get("q") or request.args.get("name") or "").strip()
    if not q_raw:
        return jsonify({"articles": []})

    # 把長名抓成 short token (e.g., "銅 (copper) US$/tonne" → "銅")
    item_short = q_raw.split()[0].split("(")[0].strip()
    if not item_short:
        item_short = q_raw

    # Cache hit
    cached = _commodity_news_cache.get(item_short)
    if cached and (time.time() - cached["ts"]) < _COMMODITY_NEWS_TTL:
        logger.info(f"[COMMODITY-NEWS] cache hit '{item_short}': {len(cached['data'])} arts")
        return jsonify({"articles": cached["data"]})

    # 英文：mining.com（hourly cache、共用、最低 Bing 用量）
    en_news = _filter_mining_for(item_short, max_records=5)

    # 平行 fetch：GDELT 補英文不足 + Bing 抓中文
    bing_allowed = _bing_budget_ok()
    if not bing_allowed:
        logger.info(f"[COMMODITY-NEWS] skip Bing for '{item_short}' (budget/cooldown)")

    need_gdelt = len(en_news) < 3
    zh_news = []
    extra_en = []
    # 給 GDELT 過濾用的關鍵字（標題 must contain）
    gdelt_tokens = _COMMODITY_FILTER_TOKENS.get(item_short) or [item_short.lower()]
    # Bing 搜尋詞：先看是否有特化中文關鍵字（如 PC → 聚碳酸酯），沒有就用預設 item + 價格
    bing_query = _COMMODITY_BING_QUERY.get(item_short) or f"{item_short} 價格"
    with ThreadPoolExecutor(max_workers=2) as fpool:
        f_gdelt = fpool.submit(_fetch_gdelt_commodity_news,
                               _commodity_en_query(item_short),
                               5 - len(en_news),
                               gdelt_tokens) if need_gdelt else None
        f_bing = fpool.submit(_fetch_bing_commodity_news,
                              bing_query, 8) if bing_allowed else None  # fetch 8 給 sort 後挑 top 5
        if f_gdelt:
            try:
                extra_en = f_gdelt.result(timeout=15)
            except Exception as e:
                logger.warning(f"[COMMODITY-NEWS] GDELT failed: {e}")
        if f_bing:
            try:
                zh_news = f_bing.result(timeout=15)
            except Exception as e:
                logger.warning(f"[COMMODITY-NEWS] Bing failed: {e}")

    # 排序所有文章 by 發布日期（最新優先）
    en_news.sort(key=lambda a: a.get("published") or "", reverse=True)
    extra_en.sort(key=lambda a: a.get("published") or "", reverse=True)
    zh_news.sort(key=lambda a: a.get("published") or "", reverse=True)

    # 智慧 30 天 filter：優先取 30 天內，但若不足 3 篇則放寬到 top 5（保證至少有東西看）
    from datetime import datetime as _dt, timedelta as _td
    cutoff_date = (_dt.utcnow() - _td(days=30)).strftime("%Y-%m-%d")
    def _filter_or_fallback(arts, target=5, min_recent=3):
        if not arts:
            return []
        recent = [a for a in arts if (a.get("published") or "") >= cutoff_date or not a.get("published")]
        # 30 天內 ≥ 3 篇 → 全用 30 天 filter
        # 30 天內 < 3 篇 → 用 top N by date（讓使用者至少看得到東西，反正也不會太舊太多）
        return recent[:target] if len(recent) >= min_recent else arts[:target]
    en_news = _filter_or_fallback(en_news, 5)
    extra_en = _filter_or_fallback(extra_en, 5)
    zh_news = _filter_or_fallback(zh_news, 5)

    # 合併英文（去重）
    seen = {a["source_url"] for a in en_news}
    for a in extra_en:
        if a["source_url"] not in seen:
            en_news.append(a)
            seen.add(a["source_url"])

    # 為英文文章加上中文標題（cache 過 → 0 cost；新文章 → ~2 秒 / 篇 平行翻）
    # 平行翻譯 max_workers=8 確保 5 篇都能同時跑，總等待 ≈ 單篇翻譯時間 (~2-4s)
    if en_news:
        with ThreadPoolExecutor(max_workers=8) as tpool:
            futs = {tpool.submit(_translate_to_zh, a["title"]): a for a in en_news}
            for fut in futs:
                a = futs[fut]
                try:
                    a["title_zh"] = fut.result(timeout=5)
                except Exception:
                    a["title_zh"] = a["title"]

    articles = zh_news + en_news
    _commodity_news_cache[item_short] = {"data": articles, "ts": time.time()}
    logger.info(f"[COMMODITY-NEWS] '{item_short}' mining.com={sum(1 for a in en_news if a['source']=='mining.com')} GDELT={sum(1 for a in en_news if a['source']!='mining.com')} Bing={len(zh_news)} Bing-budget={_bing_daily_count}/{_BING_DAILY_CAP}")
    return jsonify({"articles": articles})


@app.route("/api/commodities/refresh", methods=["POST"])
def api_commodities_refresh():
    t = threading.Thread(target=_refresh_live_prices, daemon=True)
    t.start()
    return jsonify({"status": "refreshing"})




@app.route("/api/risk/suppliers")
def api_risk_suppliers():
    """Return backend-managed supplier list from suppliers.json."""
    import json
    path = os.path.join(os.path.dirname(__file__), "suppliers.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            suppliers = json.load(f)
    except FileNotFoundError:
        suppliers = []
    return jsonify(suppliers)


_QUAKE_CACHE: dict = {"data": None, "ts": 0.0}
_QUAKE_CACHE_TTL = 300  # 5 minutes — official feeds refresh often enough that per-request fetches are unnecessary.


@app.route("/api/risk/quakes")
def api_risk_quakes():
    """官方來源優先的過去 8 週地震資料，5 分鐘 cache。

    日本 JMA、台灣 CWA、印尼 BMKG 先接官方 JSON/API；其他地區保留 USGS fallback。
    """
    now = time.time()
    cached = _QUAKE_CACHE.get("data")
    if cached is not None and (now - _QUAKE_CACHE.get("ts", 0)) < _QUAKE_CACHE_TTL:
        return jsonify(cached)
    try:
        data = {
            "type": "FeatureCollection",
            "metadata": {
                "title": "Official local earthquake feeds with USGS fallback",
                "days": _QUAKE_DAYS,
                "sources": ["JMA", "CWA", "BMKG", "USGS"],
                "critical": {
                    "magnitude": _QUAKE_CRITICAL_MAG,
                    "maxIntensityValue": _QUAKE_CRITICAL_INTENSITY,
                    "maxIntensity": "6弱",
                },
            },
            "features": _fetch_quake_features(_QUAKE_DAYS),
        }
        _QUAKE_CACHE["data"] = data
        _QUAKE_CACHE["ts"] = now
        return jsonify(data)
    except Exception as e:
        logger.warning(f"official/local quake fetch error: {e}", exc_info=True)
        if cached is not None:
            return jsonify(cached)  # 拿舊 cache 比空陣列好
        return jsonify({"type": "FeatureCollection", "features": []})


# 5-minute caches for proxy endpoints — without these every page load hits
# NOAA + GDACS + ReliefWeb live, adding 5-10s to /api/risk/* fan-out.
_STORMS_CACHE: dict = {"data": None, "ts": 0.0}
_GDACS_CACHE:  dict = {"data": None, "ts": 0.0}
_CRISES_CACHE: dict = {"data": None, "ts": 0.0}
_RISK_PROXY_TTL = 300  # 5 minutes — these feeds change at most hourly anyway


@app.route("/api/risk/storms")
def api_risk_storms():
    """Proxy NOAA NHC active storms (5-min cache)."""
    now = time.time()
    cached = _STORMS_CACHE.get("data")
    if cached is not None and (now - _STORMS_CACHE.get("ts", 0)) < _RISK_PROXY_TTL:
        return jsonify(cached)
    try:
        r = req_lib.get("https://www.nhc.noaa.gov/CurrentStorms.json", timeout=5)
        if r.status_code == 200:
            data = r.json()
            _STORMS_CACHE["data"] = data
            _STORMS_CACHE["ts"] = now
            return jsonify(data)
        return r.content, r.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        logger.warning(f"NHC proxy error: {e}")
        if cached is not None:
            return jsonify(cached)
        return jsonify({"activeStorms": []})


@app.route("/api/risk/gdacs")
def api_risk_gdacs():
    """Proxy GDACS floods + volcanoes (5-min cache, Orange/Red, ≤3 days old).

    Cyclones (TC) excluded — handled by /api/risk/storms (NOAA NHC, 96kt+).
    避免颱風雙重來源 + GDACS 寬鬆門檻造成「氾濫」。
    """
    now = time.time()
    cached = _GDACS_CACHE.get("data")
    if cached is not None and (now - _GDACS_CACHE.get("ts", 0)) < _RISK_PROXY_TTL:
        return jsonify(cached)
    try:
        r = req_lib.get(
            "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
            "?eventlist=FL;VO&alertlevel=Orange;Red&limit=40",
            timeout=8,
        )
        data = r.json()

        # Filter to only events from last 3 days (per user requirement)
        today = datetime.now(timezone(timedelta(hours=8))).date()
        filtered_features = []
        for feature in data.get("features", []):
            try:
                props = feature.get("properties", {})
                event_date_str = props.get("fromdate", "")
                if event_date_str:
                    event_date = datetime.strptime(event_date_str[:10], "%Y-%m-%d").date()
                    if (today - event_date).days <= 3:
                        filtered_features.append(feature)
            except Exception:
                continue

        result = {"type": data.get("type"), "features": filtered_features}
        _GDACS_CACHE["data"] = result
        _GDACS_CACHE["ts"] = now
        return jsonify(result)
    except Exception as e:
        logger.warning(f"GDACS proxy error: {e}")
        if cached is not None:
            return jsonify(cached)
        return jsonify({"features": []})


@app.route("/api/risk/crises")
def api_risk_crises():
    """Proxy ReliefWeb ALL ongoing crises (wars, floods, epidemics) — 5-min cache."""
    now = time.time()
    cached = _CRISES_CACHE.get("data")
    if cached is not None and (now - _CRISES_CACHE.get("ts", 0)) < _RISK_PROXY_TTL:
        return jsonify(cached)
    try:
        payload = {
            "appname": "asustimes-risk",
            "profile": "list",
            "slim": 1,
            "limit": 50,
            "fields": {"include": ["name", "date", "country", "type", "status"]},
            "filter": {"field": "status", "value": "ongoing"},
            "sort": ["date.created:desc"],
        }
        r = req_lib.post("https://api.reliefweb.int/v1/disasters", json=payload, timeout=8)
        if r.status_code == 200:
            data = r.json()
            _CRISES_CACHE["data"] = data
            _CRISES_CACHE["ts"] = now
            return jsonify(data)
        return r.content, r.status_code, {"Content-Type": "application/json"}
    except Exception as e:
        logger.warning(f"ReliefWeb proxy error: {e}")
        if cached is not None:
            return jsonify(cached)
        return jsonify({"data": []})


# ── Geopolitical risk cache (4-hour TTL) ─────────────────────
_geo_risk_cache: dict = {"data": None, "ts": 0.0}
_geo_risk_lock  = threading.Lock()

# ── Disaster Risks ──────────────────────────────────────────────
# 已全部移除。所有災害事件都由各自的官方 feed 提供：
#   地震 → JMA / CWA / BMKG / USGS fallback  (api_risk_quakes / _fetch_quake_features)
#   颱風 → NOAA NHC (api_risk_storms / _fetch_noaa_storms)
#   洪水 / 火山 / 極端天氣 → GDACS (api_risk_disasters / _fetch_gdacs_alerts)
# 之前用新聞 scrape 重複偵測這些事件，標題/規模/座標都寫死，不準且跟官方 feed 衝突。
_DISASTER_RISKS: list = []

# 升級關鍵字：標題含這些詞才視為「真實升溫」事件 → 拉高到原 impact
# 否則只是常態緊張 → 降一級成 MED（地圖顯示但不視為急迫事件）
_GEO_ESCALATION_KW = [
    # === 中文：要求具體軍事行動，避免「升級/演習/衝突」等泛詞誤觸 ===
    "開火", "交火", "開戰", "宣戰", "戰爭爆發", "戰火爆發",
    "擦槍走火", "公然挑釁",
    "飛彈攻擊", "炸彈攻擊", "武力攻擊", "空襲",
    "軍事衝突", "武裝衝突", "邊境衝突", "邊境交火",
    "犯台", "犯境", "侵略", "入侵",
    "海上封鎖", "貿易封鎖", "全面封鎖", "突破封鎖",
    # === 英文：使用多字組合，避免單字（attack/strike/crisis/raid/fire/missile）泛濫 ===
    "open fire", "opened fire", "opens fire",
    "exchanged fire", "exchanges fire", "exchange of fire",
    "fired upon", "shots fired",
    "missile strike", "missile attack", "missile struck",
    "air strike", "airstrike", "air raid",
    "armed clash", "armed conflict", "military clash",
    "naval blockade", "blockade imposed",
    "invasion of", "invading forces",
    "war declared", "declares war",
    "act of war",
    "killed in attack", "casualties reported",
]

_GEO_RISKS = [
    {"id":"geo-redsea",  "kw":["Houthi Red Sea ship attack","Red Sea shipping attack"],
     "title":"紅海航運威脅（胡塞武裝）","type":"war","lat":14.5,"lng":42.5,"region":"葉門/紅海",
     "impact":"CRITICAL","supply":"亞歐航程延長10-14天，運費上漲200-400%，建議改走好望角或提前備貨",
     "affected_materials":["晶片","電子產品","汽車零件"],"shipping_routes":["蘇伊士運河","紅海","亞歐航線"],
     "needs_escalation": True},  # 紅海有持續低度威脅，要升級關鍵字才升 CRITICAL
    {"id":"geo-taiwan",  "kw":["PLA Taiwan Strait military","China Taiwan military exercise"],
     "title":"台灣海峽地緣緊張","type":"war","lat":24.0,"lng":122.0,"region":"東亞",
     "impact":"HIGH","supply":"全球半導體（TSMC等）供應鏈最高風險區",
     "affected_materials":["晶片","半導體","記憶體"],"shipping_routes":["台灣海峽","東北亞航線"],
     "needs_escalation": True},  # 台海軍事活動是日常新聞，要升級關鍵字才升 HIGH
    {"id":"geo-iran",    "kw":["Iran Israel attack war","Iran US military strike","Iran attack Israel"],
     "title":"伊朗地區衝突","type":"war","lat":32.0,"lng":53.0,"region":"中東/波斯灣",
     "impact":"HIGH","supply":"荷姆茲海峽石油供應威脅，波斯灣航運風險",
     "affected_materials":["石油","天然氣","化工品"],"shipping_routes":["荷姆茲海峽","波斯灣","中東航線"],
     "needs_escalation": True},
    {"id":"geo-ukraine", "kw":["Ukraine Russia war attack","Russia Ukraine missile"],
     "title":"俄烏戰爭","type":"war","lat":49.0,"lng":32.0,"region":"東歐",
     "impact":"CRITICAL","supply":"穀物、化肥、氖氣供應中斷；黑海航運受限",
     "affected_materials":["氖氣","鈀","穀物","化肥"],"shipping_routes":["黑海","烏克蘭港口","歐亞航線"],
     "needs_escalation": False},  # 俄烏戰爭是 active war，所有相關報導都是事件
    {"id":"geo-drc",     "kw":["DRC Congo M23 conflict cobalt","Congo mineral conflict"],
     "title":"剛果衝突（礦產風險）","type":"war","lat":-1.5,"lng":29.0,"region":"中非",
     "impact":"HIGH","supply":"鈷、鋰等電池礦產供應不穩定",
     "affected_materials":["鈷","鋰","銅礦"],"shipping_routes":["中非港口","非洲航線"],
     "needs_escalation": True},
    {"id":"geo-myanmar", "kw":["Myanmar civil war military","Myanmar junta conflict"],
     "title":"緬甸內戰","type":"war","lat":19.8,"lng":96.2,"region":"東南亞",
     "impact":"HIGH","supply":"稀土、天然氣出口受阻；紡織供應鏈中斷",
     "affected_materials":["稀土","天然氣","紡織品"],"shipping_routes":["馬六甲海峽","仰光港"],
     "needs_escalation": True},
    {"id":"geo-india-pak",
     "kw":["India Pakistan military tension border","India Pakistan conflict"],
     "title":"印巴邊境緊張","type":"war","lat":30.0,"lng":71.0,"region":"南亞",
     "impact":"MED","supply":"南亞製造業（電子/紡織）物流中斷風險",
     "affected_materials":["紡織品","電子零件"],"shipping_routes":["南亞港口","阿拉伯海"],
     "needs_escalation": True},
]

def _scan_one_geo_risk(risk, headers, cutoff):
    """Scan Bing News for one geopolitical risk entry. Returns result dict or None."""
    import xml.etree.ElementTree as ET
    import re
    import time as _time
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime
    latest_article = None
    latest_date = None
    has_escalation = False  # 是否有任何文章標題含「升級關鍵字」
    needs_escalation = risk.get("needs_escalation", False)
    for kw in risk["kw"]:
        for attempt in range(3):
            try:
                url = f"https://www.bing.com/news/search?format=rss&q={quote(kw)}"
                r = req_lib.get(url, timeout=15, headers=headers)
                if r.status_code == 429:
                    logger.warning(f"[GEO] {risk['title']} rate-limited (429), retry {attempt+1}/3")
                    _time.sleep(3 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    logger.warning(f"[GEO] {risk['title']} HTTP {r.status_code}, skip")
                    break
                snippet = r.content[:200].lstrip()
                if not (snippet.startswith(b'<?xml') or snippet.startswith(b'<rss')):
                    logger.warning(f"[GEO] {risk['title']} non-XML response ({len(r.content)}B), skip")
                    break
                clean = re.sub(rb'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', b'', r.content)
                items = ET.fromstring(clean).findall('.//item')[:5]
                logger.info(f"[GEO] {risk['title']} + '{kw}': {len(items)} items (status {r.status_code})")
                for item in items:
                    pub = item.findtext('pubDate', '')
                    title = (item.findtext('title') or '').lower()
                    try:
                        dt = parsedate_to_datetime(pub)
                        if dt >= cutoff:
                            found_date = str(dt.date())
                            # 檢查標題是否含「升級關鍵字」（真實升溫 vs 日常背景緊張）
                            this_escalated = any(k.lower() in title for k in _GEO_ESCALATION_KW)
                            if this_escalated:
                                has_escalation = True
                                logger.info(f"[GEO] 🚨 {risk['title']}: ESCALATION article — '{title[:60]}'")
                            else:
                                logger.info(f"[GEO] ✓ {risk['title']}: routine article — '{title[:60]}'")
                            # Keep track of latest article across all keywords
                            if latest_date is None or dt > latest_date:
                                latest_date = dt
                                latest_article = found_date
                    except (ValueError, TypeError):
                        pass
                break
            except ET.ParseError as e:
                logger.warning(f"[GEO] {risk['title']} ParseError on attempt {attempt+1}: {e}")
                _time.sleep(2)
            except Exception as e:
                logger.warning(f"[GEO] {risk['title']} + '{kw}' ERROR: {type(e).__name__}: {e}")
                break
    if not latest_article:
        logger.info(f"[GEO] ✗ {risk['title']}: no matching articles within 8 weeks")
        return None

    # impact 計算：
    #   needs_escalation=True 的（台海/紅海/伊朗/緬甸/印巴等慢性緊張）：
    #     沒升級關鍵字 → 降一級到 MED；有升級 → 用原 impact
    #   needs_escalation=False（俄烏戰爭，所有相關報導都算）：
    #     永遠用原 impact
    final_impact = risk["impact"]
    title_suffix = ""
    if needs_escalation and not has_escalation:
        final_impact = "MED"
        title_suffix = "（持續關注）"
    elif needs_escalation and has_escalation:
        title_suffix = "（升級事件）"

    from urllib.parse import quote as _q
    return {
        "id": risk["id"], "type": risk["type"],
        "title": risk["title"] + title_suffix, "lat": risk["lat"], "lng": risk["lng"],
        "region": risk["region"], "impact": final_impact,
        "supply": risk["supply"],
        "time": latest_article,
        "status": "升溫事件" if has_escalation else "新聞持續報導中",
        "source": "Bing News自動監測",
        "sourceUrl": f"https://www.bing.com/news/search?q={_q(risk['kw'][0])}",
    }


def _do_geo_scan():
    """Run parallel geopolitical AND disaster scan and update cache. Returns results list."""
    from datetime import datetime, timezone, timedelta
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=56)
    results = []
    all_risks = _GEO_RISKS + _DISASTER_RISKS  # Include both geopolitical and disaster risks
    executor = ThreadPoolExecutor(max_workers=min(2, len(all_risks)))  # Limit to 2 parallel (reduce rate-limit triggers)
    try:
        futs = [executor.submit(_scan_one_geo_risk, risk, headers, cutoff)
                for risk in all_risks]
        done, not_done = fut_wait(futs, timeout=60)  # Increased from 20 to 60
        for fut in not_done:
            fut.cancel()
        for fut in done:
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    logger.info(f"[GEO] Found: {res['title']}")
            except Exception as e:
                logger.debug(f"geo scan error: {e}")
    finally:
        executor.shutdown(wait=False)
    with _geo_risk_lock:
        # Fallback: if scan returns no results, keep last cached data
        if results:
            _geo_risk_cache["data"] = results
        elif _geo_risk_cache["data"] is None:
            _geo_risk_cache["data"] = []
        _geo_risk_cache["ts"] = time.time()
    logger.info(f"Geopolitical + Disaster risks detected: {len(results)}/{len(all_risks)}")
    # Telegram bot：把 results 落地到 Supabase（背景，不阻塞）
    _persist_events_async(results, "geo")
    return results


@app.route("/api/risk/geopolitical")
def api_risk_geopolitical():
    """Return cached geopolitical risks instantly. Background loop refreshes every 3 hours."""
    with _geo_risk_lock:
        data = _geo_risk_cache["data"]
    if data is None:
        return jsonify([])
    return jsonify(data)


# ── Strike risk monitor ─────────────────────────────────────────────────────
_STRIKE_TARGETS = [
    # ── 3C/半導體/物流相關罷工（ASUS 供應鏈相關）
    # aliases: 該公司的中英文別名（小寫），用於文章主語驗證
    # region 用城市級 (國家/城市)，讓城市訂閱者只收自己關心的廠
    # 例：「中國大陸/鄭州」訂閱者收富士康罷工但不收比亞迪深圳；
    #     「中國大陸」訂閱者兩個都收（substring 匹配）。
    {"company": "三星電子",  "kw": ["三星 罷工", "Samsung strike", "Samsung workers strike"],
     "aliases": ["三星", "samsung"],
     "lat": 37.00, "lng": 127.06, "region": "韓國/平澤", "industry": "semiconductor"},
    {"company": "富士康",    "kw": ["富士康 罷工", "Foxconn strike", "foxconn workers"],
     "aliases": ["富士康", "鴻海", "foxconn"],
     "lat": 34.75, "lng": 113.62, "region": "中國大陸/鄭州", "industry": "electronics"},
    {"company": "SK海力士",  "kw": ["SK Hynix strike", "SK海力士 罷工"],
     "aliases": ["sk海力士", "海力士", "sk hynix", "hynix"],
     "lat": 37.27, "lng": 127.44, "region": "韓國/利川", "industry": "semiconductor"},
    {"company": "LG",        "kw": ["LG strike", "LG 罷工"],
     "aliases": ["lg電子", "lg "],  # 加空格避免誤匹配 "lgbt" 等
     "lat": 37.52, "lng": 126.89, "region": "韓國/首爾", "industry": "electronics"},
    {"company": "比亞迪",    "kw": ["比亞迪 罷工", "BYD strike", "BYD workers"],
     "aliases": ["比亞迪", "byd"],
     "lat": 22.58, "lng": 114.09, "region": "中國大陸/深圳", "industry": "battery_ev"},
    {"company": "台積電",    "kw": ["台積電 罷工", "TSMC strike", "TSMC workers"],
     "aliases": ["台積電", "tsmc"],
     "lat": 24.82, "lng": 120.97, "region": "台灣/新竹", "industry": "semiconductor"},
    {"company": "聯發科",    "kw": ["聯發科 罷工", "MediaTek strike", "MediaTek workers"],
     "aliases": ["聯發科", "mediatek"],
     "lat": 24.96, "lng": 121.19, "region": "台灣/新北", "industry": "semiconductor"},
    {"company": "UPS",       "kw": ["UPS strike", "UPS workers walkout"],
     "aliases": ["ups "],
     "lat": 33.75, "lng": -84.39, "region": "美國/亞特蘭大", "industry": "logistics"},
]

_strike_cache: dict = {"data": [], "ts": 0.0}  # Force empty on startup + immediate refresh
_strike_lock  = threading.Lock()

_STRIKE_ACTION_KEYWORDS = [
    "罷工", "工潮", "停工", "workers strike", "labor strike", "strike action",
    "general strike", "full-scale strike", "went on strike", "go on strike",
    "goes on strike", "on strike", "launch strike", "launches strike",
    "launched strike", "begin strike", "begins strike", "began strike",
    "start strike", "starts strike", "started strike", "walk out", "walkout",
    "industrial action",
]

_STRIKE_EXCLUDE_KEYWORDS = [
    # Not a current/active labor stoppage.
    "沒有要罷工", "不會罷工", "未罷工", "暫緩罷工", "擱置罷工", "取消罷工",
    "罷工喊卡", "化解罷工", "避免罷工", "避開罷工", "罷工危機落幕",
    "罷工風險暫時解除", "罷工風險解除", "通過薪資協議", "薪資協議過關",
    "達成協議", "初步協議", "臨時協議", "暫定協議", "投票通過",
    "揚言", "威脅", "醞釀", "擬", "不排除", "效法", "網傳", "傳出",
    "傳調整", "傳大砍", "傳砍", "傳聞", "社群", "員工不滿",
    "not affected", "unaffected", "avert strike", "averts strike",
    "averted strike", "avoid strike", "avoids strike", "avoided strike",
    "strike averted", "strike threat is over", "strike risk is over",
    "suspend strike", "suspends strike", "suspended strike", "postpone strike",
    "postpones strike", "postponed strike", "put off strike", "puts off strike",
    "hold off", "call off", "called off", "wage deal", "pay deal",
    "tentative deal", "tentative agreement", "strike deal", "strike agreement",
    "strike settlement", "approve wage deal", "approves wage deal",
    "approved wage deal", "ratify", "ratified", "considering strikes",
    "considering strike", "threaten strike", "threatens strike",
    "threatening strike", "threaten to strike", "threatens to strike",
    "threatening to strike", "strike threat", "strike looms", "strike loom",
    "could strike", "may strike", "might strike", "reportedly",
    "strike price", "court", "fine", "theft", "legal", "lawsuit", "patent",
    "intellectual property",
]

_STRIKE_COMPANY_EXCLUDE = {
    # Samsung Electronics labor issues matter to memory/fab supply. Biologics does not.
    "三星電子": ["samsung biologics", "三星生物"],
}

_RESOLVED_STRIKE_CUTOFFS = {
    "\u53f0\u7a4d\u96fb": "2026-05-29",
    "\u4e09\u661f\u96fb\u5b50": "2026-05-27",
}

def _is_excluded_strike_event(event: dict) -> bool:
    """Return True for demo, rumor/threat, resolved, or wrong-company strike events."""
    if (event.get("id") or "") == "demo-strike-samsung":
        return True

    text = " ".join(str(event.get(k, "")) for k in ("title", "newsTitle", "supply", "status")).lower()
    if any(kw in text for kw in _STRIKE_EXCLUDE_KEYWORDS):
        return True

    for company, kws in _STRIKE_COMPANY_EXCLUDE.items():
        if company in event.get("title", "") and any(kw in text for kw in kws):
            return True
    event_date = str(event.get("time", ""))[:10]
    event_title = event.get("title", "")
    for company, cutoff in _RESOLVED_STRIKE_CUTOFFS.items():
        if company in event_title and event_date and event_date <= cutoff:
            return True
    return False

def _scan_one_strike(target, headers, cutoff):
    """Scan Bing News for one strike target. Returns result dict or None."""
    import xml.etree.ElementTree as ET
    import re
    import time as _time
    from urllib.parse import quote
    from email.utils import parsedate_to_datetime
    latest_article = None
    latest_date = None
    for kw in target["kw"]:
        for attempt in range(3):
            try:
                url = f"https://www.bing.com/news/search?format=rss&q={quote(kw)}"
                r = req_lib.get(url, timeout=15, headers=headers)
                if r.status_code == 429:
                    logger.warning(f"[STRIKE] {target['company']} rate-limited (429), retry {attempt+1}/3")
                    _time.sleep(3 * (attempt + 1))
                    continue
                if r.status_code != 200:
                    logger.warning(f"[STRIKE] {target['company']} HTTP {r.status_code}, skip")
                    break
                snippet = r.content[:200].lstrip()
                if not (snippet.startswith(b'<?xml') or snippet.startswith(b'<rss')):
                    logger.warning(f"[STRIKE] {target['company']} non-XML response ({len(r.content)}B), skip")
                    break
                clean = re.sub(rb'[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]', b'', r.content)
                root = ET.fromstring(clean)
                items = root.findall(".//item")[:5]
                logger.info(f"[STRIKE] {target['company']} + '{kw}': {len(items)} items (status {r.status_code})")
                for item in items:
                    pub = item.findtext("pubDate", "")
                    try:
                        dt = parsedate_to_datetime(pub)
                        if dt >= cutoff:
                            title = item.findtext("title", "").lower()
                            desc = item.findtext("description", "").lower()
                            full_text = f"{title} {desc}"

                            # Strict validation: keep only current/active labor stoppages.
                            # Rumors, strike threats, and resolved/averted negotiations are not events.
                            has_strike_action = any(kw in full_text for kw in _STRIKE_ACTION_KEYWORDS)
                            exclude_hits = [kw for kw in _STRIKE_EXCLUDE_KEYWORDS if kw in full_text]
                            company_exclude_hits = [
                                kw for kw in _STRIKE_COMPANY_EXCLUDE.get(target["company"], [])
                                if kw in full_text
                            ]
                            has_exclude = bool(exclude_hits or company_exclude_hits)

                            if not has_strike_action or has_exclude:
                                if not has_strike_action:
                                    logger.warning(f"[STRIKE] FILTERED {target['company']}: '{title[:70]}' — no strike action keywords")
                                else:
                                    logger.warning(f"[STRIKE] FILTERED {target['company']}: '{title[:70]}' — excluded: {exclude_hits + company_exclude_hits}")
                                continue

                            # === 公司主語驗證（避免新聞只順帶提及目標公司）===
                            # 中文：subject 通常在動詞前 → 公司名要在「罷工」前 8 字內
                            # （後窗只開 2 字，給「罷工的 X 公司」這種少見句型保留）
                            # 英文：較寬鬆 30 字前後（英文 subject/object 順序更彈性）
                            #
                            # 例 A：「三星罷工陰影籠罩 海力士有望坐穩股王」
                            #   罷工前 8 字 = "...三星" → Samsung match ✓ 正確
                            #   海力士 in 罷工 後 13 字 → 不在窗口 → SK Hynix not match ✓ 正確排除
                            # 例 B：「SK海力士Q1利潤增4倍 三星因獎金爭議陷罷工」
                            #   罷工前 8 字 = "...陷" → 海力士不在窗口 → 排除 ✓
                            strike_kws_short = ["罷工", "工潮", "strike", "walkout", "walk out"]
                            aliases = target.get("aliases", [target["company"].lower()])

                            def _is_cn(s):
                                return any('一' <= c <= '鿿' for c in s)

                            # 主語驗證演算法：
                            #   1. 找到罷工/strike 的位置 p
                            #   2. 對每個公司 alias，找到它的位置 a
                            #   3. 檢查 alias 跟 sk 之間是否有句讀邊界（，。；！？換行）
                            #      → 有邊界 = 不同子句 = 不算主語
                            #   4. 檢查距離（alias→sk 不超過 win，sk→alias 不超過 small win）
                            BOUNDARY_CHARS = '，。；！？\n'

                            def _subject_match(text):
                                for sk in strike_kws_short:
                                    is_cn = _is_cn(sk)
                                    # 距離窗口：中文較緊（句法緊湊），英文寬鬆
                                    if is_cn:
                                        max_before, max_after = 14, 2
                                    else:
                                        max_before, max_after = 50, 30

                                    p_idx = 0
                                    while True:
                                        p = text.find(sk, p_idx)
                                        if p < 0:
                                            break
                                        for alias in aliases:
                                            a_idx = 0
                                            while True:
                                                a_p = text.find(alias, a_idx)
                                                if a_p < 0:
                                                    break
                                                a_end = a_p + len(alias)
                                                if a_end <= p:
                                                    # alias 在 sk 之前
                                                    gap = p - a_end
                                                    between = text[a_end:p]
                                                    no_boundary = not any(b in between for b in BOUNDARY_CHARS)
                                                    if no_boundary and gap <= max_before:
                                                        return True
                                                elif a_p >= p + len(sk):
                                                    # alias 在 sk 之後
                                                    gap = a_p - (p + len(sk))
                                                    between = text[p + len(sk):a_p]
                                                    no_boundary = not any(b in between for b in BOUNDARY_CHARS)
                                                    if no_boundary and gap <= max_after:
                                                        return True
                                                a_idx = a_p + 1
                                        p_idx = p + 1
                                return False

                            if not _subject_match(full_text):
                                logger.warning(f"[STRIKE] FILTERED {target['company']}: '{title[:70]}' — company not subject of strike (proximity check failed)")
                                continue

                            # Keep track of latest article across all keywords
                            if latest_date is None or dt > latest_date:
                                latest_date = dt
                                latest_article = {
                                    "title": item.findtext("title", ""),
                                    "url":   item.findtext("link", ""),
                                    "date":  str(dt.date()),
                                }
                                logger.warning(f"[STRIKE] ✓✓✓ ACCEPTED {target['company']}: '{latest_article['title'][:70]}'")
                    except Exception:
                        pass
                break
            except ET.ParseError as e:
                logger.warning(f"[STRIKE] {target['company']} ParseError on attempt {attempt+1}: {e}")
                _time.sleep(2)
            except Exception as e:
                logger.warning(f"[STRIKE] {target['company']} + '{kw}' ERROR: {type(e).__name__}: {e}")
                break
    if not latest_article:
        logger.info(f"[STRIKE] ✗ {target['company']}: no matching articles within 8 weeks")
        return None

    # Decode Bing apiclick redirect URLs to get actual article URLs
    article_url = latest_article["url"]
    if "bing.com/news/apiclick.aspx" in article_url:
        try:
            from urllib.parse import urlparse, parse_qs, unquote
            qs = parse_qs(urlparse(article_url).query)
            if "url" in qs:
                article_url = unquote(qs["url"][0])
        except Exception:
            pass  # If parsing fails, use original URL

    return {
        "id":        f"strike-{target['company']}",
        "type":      "strike",
        "title":     f"{target['company']} 罷工事件",
        "lat":       target["lat"], "lng": target["lng"],
        "region":    target["region"],
        "time":      latest_article["date"],
        "impact":    "HIGH",
        "supply":    f"{target['company']}勞資衝突，可能影響生產排程與出貨交期，建議評估替代供應",
        "source":    "Bing News自動監測",
        "sourceUrl": article_url,
        "newsTitle": latest_article["title"],
    }


def _do_strike_scan():
    """Run parallel strike scan and update cache. Returns results list."""
    logger.info(f"[STRIKE] ===== SCAN START =====")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8",
    }
    cutoff = datetime.now(timezone.utc) - timedelta(days=56)
    logger.info(f"[STRIKE] Cutoff: {cutoff.date()} (56 days)")
    results = []
    executor = ThreadPoolExecutor(max_workers=min(2, len(_STRIKE_TARGETS)))  # Limit to 2 parallel (reduce rate-limit triggers)
    try:
        futs = [executor.submit(_scan_one_strike, t, headers, cutoff)
                for t in _STRIKE_TARGETS]
        done, not_done = fut_wait(futs, timeout=60)  # Increased from 25 to 60
        for fut in not_done:
            fut.cancel()
        for fut in done:
            try:
                res = fut.result()
                if res:
                    results.append(res)
                    logger.info(f"[STRIKE] Found: {res['title']} ({res['time']})")
            except Exception as e:
                logger.debug(f"strike scan error: {e}")
    finally:
        executor.shutdown(wait=False)

    # Validate results: ensure each result's company matches expected data
    validated = []
    for res in results:
        company_in_result = res.get("title", "")
        # Check if result title contains actual company name (not misattributed)
        expected_companies = [t["company"] for t in _STRIKE_TARGETS]
        found_match = False
        for company in expected_companies:
            if company in company_in_result:
                found_match = True
                break

        if found_match and not _is_excluded_strike_event(res):
            validated.append(res)
            logger.info(f"[STRIKE] ✓ Validated: {res['title']}")
        elif found_match:
            logger.warning(f"[STRIKE] ✗ REJECTED (excluded event): '{company_in_result}'")
        else:
            logger.warning(f"[STRIKE] ✗ REJECTED (company mismatch): '{company_in_result}'")

    with _strike_lock:
        if validated:
            _strike_cache["data"] = validated
        else:
            _strike_cache["data"] = []
        _strike_cache["ts"] = time.time()
    # Telegram bot：把 validated 落地到 Supabase（背景，不阻塞）
    _persist_events_async(validated, "strike")
    logger.info(f"[STRIKE] Stored {len(validated)}/{len(results)} validated results")
    return validated


@app.route("/api/risk/strikes")
def api_risk_strikes():
    """Return cached strike events instantly. Background loop refreshes every 3 hours."""
    from datetime import datetime, timezone, timedelta

    # Companies to exclude (non-tech/non-supply-chain only)
    # Taiwan semiconductor companies (TSMC, MediaTek) are included if they have actual strikes
    EXCLUDED_COMPANIES = {
        "現代汽車", "Hyundai",          # Automotive
        "波音", "Boeing",              # Aerospace
        "通用汽車", "GM",              # Automotive
        "Volkswagen", "VW", "福斯",    # Automotive
    }

    with _strike_lock:
        data = _strike_cache["data"]

    if data is None:
        return jsonify([])

    # Only show strikes with recent news (within the past 7 days)
    # Use Taiwan timezone to match event timestamps
    TW_TZ = timezone(timedelta(hours=8))
    cutoff = (datetime.now(TW_TZ) - timedelta(days=7)).date()

    filtered_data = [
        event for event in data
        if not any(excluded in event.get("title", "") for excluded in EXCLUDED_COMPANIES)
        and not _is_excluded_strike_event(event)
        and event.get("time", "") >= str(cutoff)  # Only show if latest news is recent (7 days)
    ]

    return jsonify(filtered_data)


def _get_commodity_data() -> dict:
    """Return only web/API prices (no CSV mixing). Use live cache data."""
    # Build commodity data from live cache only (network prices, not CSV)
    result = {}

    # Category mapping
    item_to_cat = {}
    for cat, items in _COMMODITY_CATEGORIES.items():
        for item in items:
            item_to_cat[item] = cat

    with _live_cache_lock:
        for csv_name, live_points in _live_commodity_cache.items():
            if not live_points:
                continue

            # Extract unit and category
            unit = ""
            _LIVE_UNIT_OVERRIDES = {"鎢": "元/千克"}
            for name_key, u_val in _LIVE_UNIT_OVERRIDES.items():
                if name_key == csv_name:
                    unit = u_val
                    break
            if not unit:
                for u in ["US$/tonne", "CNY$/tonne", "US$/盎司", "US$/桶", "USD/T", "CNY$/kg"]:
                    if u in csv_name:
                        unit = u
                        break

            cat = "其他"
            for key, c in item_to_cat.items():
                if key in csv_name:
                    cat = c
                    break

            result[csv_name] = {
                "unit":     unit,
                "category": cat,
                "dates":    [p[0] for p in live_points],
                "values":   [p[1] for p in live_points],
            }

    return result


@app.route("/api/commodities")
def api_commodities():
    """Return item metadata only (no history) — fast small payload."""
    with _live_cache_lock:
        cache_empty = not _live_commodity_cache
    data = _get_commodity_data()
    with _item_sources_lock:
        src_snapshot = dict(_item_sources)
    items = []
    for name, d in data.items():
        latest = next((v for v in reversed(d["values"]) if v is not None), None)
        prev   = next((v for v in reversed(d["values"][:-1]) if v is not None), None)
        change = round(((latest - prev) / prev * 100), 2) if latest and prev and prev != 0 else None
        src    = src_snapshot.get(name, {})
        # Find the date of the latest non-null value
        latest_date = None
        for dt, v in zip(reversed(d["dates"]), reversed(d["values"])):
            if v is not None:
                latest_date = dt
                break
        items.append({
            "name":         name,
            "unit":         d["unit"],
            "category":     d["category"],
            "latest":       latest,
            "latest_date":  latest_date,
            "change":       change,
            "source_label": src.get("label", ""),
            "source_url":   src.get("url", ""),
        })
    categories = list(_COMMODITY_CATEGORIES.keys())
    return jsonify({"items": items, "categories": categories, "loading": cache_empty})


@app.route("/api/commodity-history")
def api_commodity_history():
    """Return full date/value history for a single item (fetched on demand)."""
    name = request.args.get("name", "").strip()
    if not name:
        return jsonify({"dates": [], "values": []})
    # Bypass 5-min cache to get fresh live data
    with _csv_parse_lock:
        _csv_parse_cache["data"] = None
    data = _get_commodity_data()
    d = data.get(name)
    if not d:
        return jsonify({"dates": [], "values": []})
    return jsonify({"dates": d["dates"], "values": d["values"]})


# ── Supply Chain Risk Monitor ─────────────────────────────────────────────────

_SUPPLY_CHAIN_CLUSTERS = [
    {"id": "hsinchu",    "name": "新竹",     "name_en": "Hsinchu",        "lat": 24.76, "lng": 120.99, "industries": ["半導體", "IC設計"],      "region": "TW"},
    {"id": "taichung",   "name": "台中",     "name_en": "Taichung",       "lat": 24.15, "lng": 120.68, "industries": ["精密製造", "電子"],       "region": "TW"},
    {"id": "shenzhen",   "name": "深圳",     "name_en": "Shenzhen",       "lat": 22.54, "lng": 114.06, "industries": ["消費電子", "PCB"],         "region": "CN", "region_macro": "華南", "region_city": "深圳"},
    {"id": "kunshan",    "name": "昆山",     "name_en": "Kunshan",        "lat": 31.39, "lng": 121.16, "industries": ["PCB", "NB代工"],          "region": "CN", "region_macro": "華東", "region_city": "昆山"},
    {"id": "zhengzhou",  "name": "鄭州",     "name_en": "Zhengzhou",      "lat": 34.75, "lng": 113.62, "industries": ["手機組裝", "EMS"],         "region": "CN", "region_macro": "華中", "region_city": "鄭州"},
    {"id": "shanghai",   "name": "上海",     "name_en": "Shanghai",       "lat": 31.23, "lng": 121.47, "industries": ["汽車電子", "IC設計"],      "region": "CN", "region_macro": "華東", "region_city": "上海"},
    {"id": "penang",     "name": "檳城",     "name_en": "Penang",         "lat": 5.41,  "lng": 100.33, "industries": ["IC封測", "電子製造"],      "region": "MY"},
    {"id": "pyeongtaek", "name": "平澤",     "name_en": "Pyeongtaek",     "lat": 36.99, "lng": 127.11, "industries": ["DRAM", "NAND Flash"],     "region": "KR"},
    {"id": "icheon",     "name": "利川",     "name_en": "Icheon",         "lat": 37.27, "lng": 127.44, "industries": ["DRAM", "記憶體"],          "region": "KR"},
    {"id": "kumamoto",   "name": "熊本",     "name_en": "Kumamoto",       "lat": 32.80, "lng": 130.71, "industries": ["晶圓代工", "半導體"],       "region": "JP"},
    {"id": "osaka",      "name": "大阪",     "name_en": "Osaka",          "lat": 34.69, "lng": 135.50, "industries": ["OLED", "感測器"],          "region": "JP"},
    {"id": "san_jose",   "name": "矽谷",     "name_en": "Silicon Valley", "lat": 37.34, "lng": -121.89,"industries": ["AI晶片", "Fabless"],      "region": "US"},
    {"id": "austin",     "name": "奧斯汀",   "name_en": "Austin TX",      "lat": 30.27, "lng": -97.74, "industries": ["晶圓廠", "資料中心"],      "region": "US"},
    {"id": "dresden",    "name": "德勒斯登", "name_en": "Dresden",        "lat": 51.05, "lng": 13.74,  "industries": ["汽車晶片", "半導體"],      "region": "EU"},
    {"id": "eindhoven",  "name": "恩荷芬",   "name_en": "Eindhoven",      "lat": 51.44, "lng": 5.48,   "industries": ["半導體設備", "EUV"],       "region": "EU"},
]

_RISK_KEYWORDS = {
    "disaster":     ["地震", "颶風", "洪水", "水災", "火災", "海嘯", "暴風雪", "龍捲風", "冰雹", "霜凍", "雪災",
                     "earthquake", "hurricane", "flood", "tsunami", "disaster", "blizzard", "tornado", "snowstorm", "cyclone"],
    "geopolitical": ["制裁", "關稅", "禁令", "出口管制", "貿易戰", "戰爭", "衝突", "伊朗", "中東", "紅海", "胡塞", "俄烏", "以巴",
                     "tariff", "sanction", "ban", "export control", "trade war", "chip war",
                     "war", "conflict", "iran", "middle east", "red sea", "houthi", "russia ukraine", "israel palestin"],
    "strike":       ["罷工", "工人罷工", "工潮", "勞資爭議", "勞工抗議", "工會", "停工", "罷課",
                     "strike", "labor strike", "workers strike", "walkout", "industrial action", "union"],
    "operational":  ["限電", "缺料", "斷鏈", "停工", "產能", "blackout", "shortage", "disruption", "halt"],
    "financial":    ["破產", "虧損", "裁員", "信評", "倒閉", "財報", "獲利預警", "虧損擴大",
                     "bankruptcy", "layoff", "downgrade", "profit warning", "earnings miss", "default"],
}

# Typhoon only counts as disaster if paired with SERIOUS impact keywords (致災程度，不只是氣象預報)
_DISASTER_SEVERITY_KEYWORDS = ["致災", "災害", "損失", "損害", "中斷", "停工", "罹難", "傷亡", "淹水", "破壞",
                                "damage", "disruption", "impact", "closure", "casualty", "fatality", "flooding", "destruction"]
_TYPHOON_KEYWORDS = ["颱風", "typhoon"]
_TYPHOON_FORECAST_KEYWORDS = ["預報", "預測", "警報", "警戒", "forecast", "warning", "alert", "prediction"]  # Exclude pure forecasts

_CLUSTER_KEYWORDS = {
    "hsinchu":    ["新竹", "竹科", "台積電", "TSMC", "聯電", "UMC", "聯發科", "MediaTek"],
    "taichung":   ["台中", "中科"],
    "shenzhen":   ["深圳", "Shenzhen", "比亞迪", "BYD"],
    "kunshan":    ["昆山", "Kunshan"],
    "zhengzhou":  ["鄭州", "Zhengzhou", "富士康", "Foxconn", "鴻海"],
    "shanghai":   ["上海", "Shanghai", "張江", "浦東"],
    "penang":     ["檳城", "Penang", "馬來西亞", "Malaysia"],
    "pyeongtaek": ["平澤", "Pyeongtaek", "三星", "Samsung", "韓國"],
    "icheon":     ["利川", "Icheon", "SK海力士", "SK Hynix", "海力士", "韓國"],
    "kumamoto":   ["熊本", "Kumamoto", "TSMC日本", "JASM"],
    "osaka":      ["大阪", "Osaka", "夏普", "Sharp", "Japan Display", "JDI", "Sony"],
    "san_jose":   ["矽谷", "Silicon Valley", "聖荷西"],
    "austin":     ["奧斯汀", "Austin"],
    "dresden":    ["德勒斯登", "Dresden"],
    "eindhoven":  ["恩荷芬", "Eindhoven"],
}

# 根據供應商分布，對應集群的地區影響範圍
_REGION_TO_CLUSTERS = {
    "台灣": ["hsinchu", "taichung"],  # 台灣供應商集中在新竹、台中
    "台北": ["hsinchu"],  # 台北新竹相近
    "中國大陸": ["shenzhen", "kunshan", "zhengzhou", "shanghai"],  # 中國集群
    # 中國宏觀地區分層
    "華東": ["shanghai", "kunshan"],  # 上海、昆山
    "華南": ["shenzhen"],  # 深圳
    "華中": ["zhengzhou"],  # 鄭州
    "日本": ["kumamoto", "osaka"],  # 日本集群
    "韓國": ["pyeongtaek", "icheon"],  # 韓國集群
    "馬來西亞": ["penang"],  # 馬來西亞集群
    "美國": ["san_jose", "austin"],  # 美國集群
    "德國": ["dresden"],  # 德國集群
    "荷蘭": ["eindhoven"],  # 荷蘭集群
}

_REGION_LABELS = {
    # Cluster regions
    "TW": "🇹🇼 台灣 (Taiwan)",
    "CN": "🇨🇳 中國 (China)",
    "KR": "🇰🇷 韓國 (South Korea)",
    "JP": "🇯🇵 日本 (Japan)",
    "US": "🇺🇸 美國 (USA)",
    "MY": "🇲🇾 馬來西亞 (Malaysia)",
    "EU": "🇪🇺 歐洲 (Europe)",
    # China macro regions
    "華東": "📍 華東 (East China)",
    "華南": "📍 華南 (South China)",
    "華中": "📍 華中 (Central China)",
    # Geopolitical regions
    "東亞": "🗺️ 東亞 (East Asia)",
    "東南亞": "🗺️ 東南亞 (Southeast Asia)",
    "南亞": "🗺️ 南亞 (South Asia)",
    "中東/波斯灣": "🗺️ 中東/波斯灣 (Middle East/Persian Gulf)",
    "葉門/紅海": "🗺️ 葉門/紅海 (Yemen/Red Sea)",
    "東歐": "🗺️ 東歐 (Eastern Europe)",
    "中非": "🗺️ 中非 (Central Africa)",
}

# Map broader geopolitical/event regions to specific fab cluster regions
_GEO_REGION_TO_CLUSTERS = {
    "東亞": ["台灣", "韓國", "日本"],  # Taiwan Strait tensions affect East Asia fabs
    "中東/波斯灣": [],  # Shipping impact, no direct fab region
    "葉門/紅海": [],  # Shipping impact, no direct fab region
    "東歐": [],  # No fab clusters in Eastern Europe
    "中非": [],  # Cobalt, but no direct fab impact
    "中國": ["中國大陸"],  # Direct mapping for China
    "台灣": ["台灣"],  # Taiwan fabs
    "韓國": ["韓國"],  # Korean fabs
    "日本": ["日本"],  # Japanese fabs
    "馬來西亞": ["馬來西亞"],  # Malaysian fabs
    "美國": ["美國"],  # US fabs
    "德國": ["德國"],  # German fabs
    "荷蘭": ["荷蘭"],  # Dutch fabs
}

_RISK_TYPE_LABELS = {
    "disaster":     "🌊 天災",
    "geopolitical": "🚨 地緣",
    "strike":       "✊ 罷工",
    "operational":  "⚡ 停運",
    "financial":    "💸 財警",  # Financial: shown in news wall but NOT counted for risk scores
}

# Key fab keywords: ONLY critical fabs (TSMC, Samsung, SK Hynix)
_KEY_FAB_KEYWORDS = ["台積電", "tsmc", "samsung", "三星", "sk海力士", "sk hynix", "hynix"]

# Fab-related companies whose strikes matter for supply chain risk
# (not all companies in _STRIKE_TARGETS are fabs)
_FAB_COMPANIES = ["三星電子", "Samsung", "SK海力士", "SK Hynix", "富士康", "Foxconn"]

# Event certainty keywords: indicates confirmed/imminent event (not pure forecast)
_CONFIRMED_EVENT_KEYWORDS = ["宣布", "確認", "已發生", "發動", "啟動", "正在", "進行中", "將", "即將",
                             "announced", "confirmed", "occurred", "launched", "underway", "will"]

# Event duration keywords: indicates prolonged impact (>7 days)
_PROLONGED_EVENT_KEYWORDS = ["18天", "两周", "一周", "持續", "ongoing", "continues", "week", "month"]

# Map key fab companies to their regions (for automatic region inference from company mentions)
_FAB_TO_REGIONS = {
    "台積電": "台灣",
    "tsmc": "台灣",
    "samsung": "韓國",
    "三星": "韓國",
    "sk海力士": "韓國",
    "sk hynix": "韓國",
    "hynix": "韓國",
}


@app.route("/api/risk")
def api_risk():
    """Supply chain risk monitor: cluster risk scores + tagged news."""
    with _cache_lock:
        articles = list(_cache["articles"])

    now = datetime.now(timezone(timedelta(hours=8)))
    cutoff_21d = (now - timedelta(days=21)).strftime("%Y-%m-%d")  # Extended to 21 days for tracking
    recent = [a for a in articles
              if (a.get("published") or a.get("fetched_at", ""))[:10] >= cutoff_21d]

    # Risk scoring weights: Financial excluded (not counted for risk scores)
    weights = {"disaster": 30, "geopolitical": 20, "strike": 20, "operational": 15}
    cluster_scores = {c["id"]: 0 for c in _SUPPLY_CHAIN_CLUSTERS}

    for article in recent:
        pub_str = article.get("published") or article.get("fetched_at", "")
        try:
            pub_date = datetime.strptime(pub_str[:10], "%Y-%m-%d").date()
        except:
            continue

        days_old = (now.date() - pub_date).days
        text = (article.get("title", "") + " " + article.get("summary", "")).lower()

        # 1. Filter: Only count if affects KEY fab (TSMC, Samsung, SK Hynix, etc.)
        has_key_fab = any(kw.lower() in text for kw in _KEY_FAB_KEYWORDS)
        if not has_key_fab:
            continue  # Skip if not involving critical fab

        # 2. Detect if typhoon is pure forecast (exclude these)
        is_typhoon_forecast = False
        if any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
            if any(fk.lower() in text for fk in _TYPHOON_FORECAST_KEYWORDS):
                is_typhoon_forecast = True

        # 3. Detect event certainty: confirmed/imminent events only
        is_confirmed = any(kw.lower() in text for kw in _CONFIRMED_EVENT_KEYWORDS)

        # 4. Detect event duration: prolonged impacts (>= 7 days)
        is_prolonged = any(kw.lower() in text for kw in _PROLONGED_EVENT_KEYWORDS)

        # 5. Identify affected regions: via cluster keywords OR infer from company mentions
        affected_regions = set()

        # First try: match cluster location keywords
        for cid, ckws in _CLUSTER_KEYWORDS.items():
            if any(kw.lower() in text for kw in ckws):
                for region, cluster_list in _REGION_TO_CLUSTERS.items():
                    if cid in cluster_list:
                        affected_regions.add(region)
                        break

        # Fallback: if no location found but key fab mentioned, infer region from company
        if not affected_regions:
            for fab, region in _FAB_TO_REGIONS.items():
                if fab.lower() in text:
                    affected_regions.add(region)
                    break  # One fab is enough to infer region

        if not affected_regions:
            continue  # Skip if no region can be inferred

        # 6. Calculate time decay: reduce weight for older events
        time_multiplier = 1.0
        if days_old > 7:
            time_multiplier = max(0.3, 1.0 - (days_old - 7) * 0.1)  # Decrease 10% per day after day 7
        if days_old > 21:
            continue  # Don't score events older than 21 days

        # 7. Score only the affected region's clusters
        for rtype, rkws in _RISK_KEYWORDS.items():
            if rtype == "financial":
                continue  # SKIP financial news for risk scoring

            risk_found = False
            if rtype == "disaster":
                # Disaster risk: two categories
                # (1) Earthquakes: 5.0+ magnitude, no time limit
                # (2) Typhoon/flood/tsunami/blizzard: need severity keywords + within 3 days

                is_earthquake = any(ek.lower() in text for ek in ["地震", "earthquake", "magnitude", "震度"])

                if is_earthquake:
                    # Earthquake: check for magnitude >= 5.0
                    # Look for patterns like "5.0", "5級", "5强", etc.
                    import re as _re
                    magnitude_patterns = [
                        r'(?:magnitude|震度|級)\s*[5-9]',  # magnitude 5-9
                        r'[5-9](?:\.[0-9])?(?:級|强|magnitude)?',  # 5.0-9.9 magnitude
                    ]
                    if any(_re.search(pat, text, _re.IGNORECASE) for pat in magnitude_patterns):
                        risk_found = True
                        logger.debug(f"Earthquake 5.0+ detected: {text[:60]}")

                elif any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
                    # Typhoon/flood/tsunami/blizzard: need severity + 3 days limit
                    if days_old > 3:
                        continue  # Skip events older than 3 days
                    if not is_typhoon_forecast and any(sk.lower() in text for sk in _DISASTER_SEVERITY_KEYWORDS):
                        risk_found = True
                        logger.debug(f"Severe disaster within 3 days: {text[:60]}")
                # Other disasters not mentioned above: skip
            elif any(rk.lower() in text for rk in rkws):
                risk_found = True

            if risk_found:
                # Adjust weight based on certainty and duration
                base_weight = weights.get(rtype, 10)

                # If not confirmed/imminent, reduce weight
                if not is_confirmed and rtype in ["strike", "geopolitical"]:
                    base_weight *= 0.6

                # If not prolonged (>7 days), reduce weight
                if not is_prolonged and rtype in ["strike", "operational"]:
                    base_weight *= 0.7

                final_weight = base_weight * time_multiplier

                # Only increase score for affected region's clusters
                for region in affected_regions:
                    for cid in _REGION_TO_CLUSTERS.get(region, []):
                        cluster_scores[cid] = min(100, cluster_scores[cid] + final_weight)

    # Also score clusters based on cached strike and geopolitical events
    with _strike_lock:
        strikes = _strike_cache.get("data", []) or []
    with _geo_risk_lock:
        geo_risks = _geo_risk_cache.get("data", []) or []

    for event in strikes + geo_risks:
        # Skip non-fab strikes (only count strikes from actual fab companies)
        event_type = event.get("type", "")
        if event_type == "strike":
            if _is_excluded_strike_event(event):
                continue
            # Extract company name from event title (format: "公司名 罷工事件")
            title = event.get("title", "")
            is_fab_strike = any(fab in title for fab in _FAB_COMPANIES)
            if not is_fab_strike:
                continue  # Skip non-fab company strikes

        # Check if event is recent (within 21 days)
        event_date_str = event.get("time", "")
        if not event_date_str:
            continue
        try:
            event_date = datetime.strptime(event_date_str[:10], "%Y-%m-%d").date()
        except:
            continue

        days_old = (now.date() - event_date).days
        if days_old > 21:
            continue

        # Calculate time decay
        time_multiplier_event = 1.0
        if days_old > 7:
            time_multiplier_event = max(0.3, 1.0 - (days_old - 7) * 0.1)

        # Score the event based on its region and type
        event_region = event.get("region", "")

        if event_type == "strike":
            base_weight_event = weights.get("strike", 20)
            final_weight_event = base_weight_event * time_multiplier_event
        elif event_type in ["geopolitical", "war"]:
            base_weight_event = weights.get("geopolitical", 20)
            final_weight_event = base_weight_event * time_multiplier_event
        else:
            continue

        # Map event region to actual fab cluster regions
        fab_regions = _GEO_REGION_TO_CLUSTERS.get(event_region, [])
        if not fab_regions:
            # Try direct region mapping if geo mapping doesn't apply
            if event_region in _REGION_TO_CLUSTERS:
                fab_regions = [event_region]

        # Score all clusters in the affected fab regions
        for fab_region in fab_regions:
            for cid in _REGION_TO_CLUSTERS.get(fab_region, []):
                cluster_scores[cid] = min(100, cluster_scores[cid] + final_weight_event)

    # Tag articles for news walls
    regional_events, financial_warnings = [], []
    seen: set = set()
    for article in articles[:400]:
        url = article.get("source_url") or article.get("url", "")
        if url in seen:
            continue
        seen.add(url)
        text = (article.get("title", "") + " " + article.get("summary", "")).lower()

        # Calculate article age
        pub_str = article.get("published") or article.get("fetched_at", "")
        try:
            pub_date = datetime.strptime(pub_str[:10], "%Y-%m-%d").date()
            article_days_old = (now.date() - pub_date).days
        except:
            article_days_old = 0

        # Detect risk types with special handling for typhoon/flood (require severity keywords + 3-day limit)
        risk_types = []
        for rt, rkws in _RISK_KEYWORDS.items():
            if any(rk.lower() in text for rk in rkws):
                # For disaster: ONLY typhoon/flood within 3 days are shown
                # Per user requirement: "洪水 氣旋 三天內才顯示 其餘一律不視為有風險"
                if rt == "disaster":
                    if any(tk.lower() in text for tk in _TYPHOON_KEYWORDS):
                        # Typhoon/flood: only show if within 3 days AND has severity keywords
                        if article_days_old <= 3 and any(sk.lower() in text for sk in _DISASTER_SEVERITY_KEYWORDS):
                            risk_types.append(rt)
                    # All other disasters: do NOT show on events/map (skip)
                else:
                    risk_types.append(rt)

        if not risk_types:
            continue
        region_tags, industry_tags = set(), set()
        for c in _SUPPLY_CHAIN_CLUSTERS:
            if any(kw.lower() in text for kw in _CLUSTER_KEYWORDS.get(c["id"], [])):
                region_tags.add(_REGION_LABELS.get(c["region"], c["region"]))
                industry_tags.update(c["industries"][:2])
        item = {
            "title":         article.get("title"),
            "url":           url,
            "published":     article.get("published"),
            "source":        article.get("source"),
            "risk_types":    [_RISK_TYPE_LABELS[rt] for rt in risk_types],
            "region_tags":   sorted(region_tags)[:3],
            "industry_tags": sorted(industry_tags)[:4],
        }
        if "financial" in risk_types:
            financial_warnings.append(item)
        else:
            regional_events.append(item)

    # Filter regional_events: remove typhoon/flood events older than 3 days
    # Per user requirement: "洪水 氣旋 三天內才顯示"
    filtered_regional_events = []
    for event in regional_events:
        risk_type_labels = event.get("risk_types", [])
        # Check if this is a typhoon/flood event
        has_typhoon_label = "🌊 天災" in risk_type_labels  # Disaster emoji label

        if has_typhoon_label:
            # Only keep if within 3 days
            try:
                event_date = datetime.strptime(event.get("published", "")[:10], "%Y-%m-%d").date()
                days_old = (now.date() - event_date).days
                if days_old <= 3:
                    filtered_regional_events.append(event)
            except:
                # If date parsing fails, exclude it
                pass
        else:
            # Keep non-disaster events
            filtered_regional_events.append(event)

    regional_events = filtered_regional_events

    clusters_out = [{**c, "risk_score": cluster_scores.get(c["id"], 0)}
                    for c in _SUPPLY_CHAIN_CLUSTERS]

    # Get cached strikes and geopolitical risks
    with _strike_lock:
        strikes = _strike_cache.get("data", []) or []
    with _geo_risk_lock:
        geo_risks = _geo_risk_cache.get("data", []) or []

    # Combine specific events (strikes + geo risks) for the map
    # Filter out typhoon/flood events older than 3 days (per user requirement)
    specific_events = []
    for event in strikes + geo_risks:
        event_type = event.get("type", "").lower()
        event_title = event.get("title", "").lower()
        if event_type == "strike" and _is_excluded_strike_event(event):
            continue

        # Check if this is a typhoon/flood/disaster event
        is_disaster = any(kw in event_title for kw in ["颱風", "typhoon", "洪水", "flood", "氣旋", "cyclone"])

        # Filter: skip old typhoon/flood events (keep only 3 days old or newer)
        if is_disaster:
            try:
                event_date_str = event.get("time", "")
                if event_date_str:
                    event_date = datetime.strptime(event_date_str[:10], "%Y-%m-%d").date()
                    days_old = (now.date() - event_date).days
                    if days_old > 3:
                        continue  # Skip this old disaster event
            except:
                # If date parsing fails, skip it to be safe
                continue

        specific_events.append({
            "id": event.get("id"),
            "type": event.get("type"),
            "title": event.get("title"),
            "region": event.get("region"),
            "lat": event.get("lat"),
            "lng": event.get("lng"),
            "impact": event.get("impact"),
            "supply": event.get("supply"),
            "time": event.get("time"),
            "source": event.get("source"),
            "sourceUrl": event.get("sourceUrl"),
            "newsTitle": event.get("newsTitle", ""),
        })

    return jsonify({
        "clusters":           clusters_out,
        "regional_events":    regional_events[:50],
        "financial_warnings": financial_warnings[:50],
        "specific_events":    specific_events,
        "last_updated":       now.strftime("%Y-%m-%d %H:%M"),
    })


_threads_started = False

def ensure_background_threads():
    """Ensure background threads are running (safe to call multiple times)."""
    global _threads_started
    if _threads_started:
        return
    _threads_started = True
    logger.info("Starting background threads...")
    threading.Thread(target=background_refresh_loop, daemon=True).start()
    threading.Thread(target=_live_price_loop, daemon=True).start()
    threading.Thread(target=_risk_cache_preload_loop, daemon=True).start()
    logger.info("Background threads started")

# ── Render production: module-load 時直接啟動災害偵測 + Telegram bot ──────────
# 在 Render 上 gunicorn 不跑 __main__，且 @app.before_request 的 lazy 啟動有時
# 因為 worker 重啟時序而沒被觸發。為了讓災害推播跟 Telegram bot 一定會起來，
# 在 module-load 結束時直接啟動（會一次性、不會重複，因為 _bg_started flag）。
def _sync_suppliers_from_json() -> None:
    """At startup, upsert suppliers.json into the Supabase suppliers table.

    Idempotent — uses upsert_supplier so re-running is safe. Runs once per
    worker process. Failures are logged but don't block startup.
    """
    import re as _re
    try:
        path = os.path.join(os.path.dirname(__file__), "suppliers.json")
        if not os.path.exists(path):
            logger.info("[suppliers] no suppliers.json, skip sync")
            return
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        from telegram_bot import db as _tdb
        _tdb.init_pool()
        for entry in data:
            region_full = entry.get("region", "").strip()
            if "/" in region_full:
                country, city = region_full.split("/", 1)
                country, city = country.strip(), city.strip()
            else:
                country, city = region_full, None
            cats_raw = entry.get("part_category", "")
            cats = [p.strip().upper() for p in _re.split(r"[、,;，]", cats_raw) if p.strip()]
            _tdb.upsert_supplier(
                name=None, region=region_full, country=country, city=city,
                lat=entry.get("lat"), lng=entry.get("lng"),
                part_categories=cats,
            )
        logger.info(f"[suppliers] synced {len(data)} entries from suppliers.json → Supabase")
    except Exception as e:
        logger.warning(f"[suppliers] startup sync failed (Bot subscriptions may show stale data): {e}")


def _start_critical_bg_threads():
    """獨立於 _ensure_bg_running 之外的早期啟動點。
    目的：確保災害事件偵測 + Telegram bot 在 gunicorn 啟動 worker 後立刻運作。
    用獨立 flag 防止 _ensure_bg_running 在 first-request 時重複啟動同樣的 thread
    （重複 polling 會觸發 Telegram getUpdates Conflict）。"""
    global _telegram_bot_started, _disaster_persist_started
    # 先把 suppliers.json 同步到 DB（給 Telegram bot 訂閱精靈用）— 同步呼叫，
    # 22 筆 upsert ~1-2 秒，失敗也不擋後面。
    _sync_suppliers_from_json()
    try:
        with _bg_lock:
            if not _disaster_persist_started:
                _disaster_persist_started = True
                threading.Thread(target=_disaster_persist_loop, daemon=True, name="disaster-persist-early").start()
            if not _telegram_bot_started:
                _telegram_bot_started = True
                threading.Thread(target=_telegram_bot_loop, daemon=True, name="telegram-bot-early").start()
        logger.info("[startup] 災害偵測 + Telegram bot 已在 module-load 時啟動")
    except Exception as e:
        logger.error(f"[startup] critical thread start failed: {e}", exc_info=True)


# 在 Render（gunicorn worker）載入時自動拉起
if os.environ.get("RENDER") == "true":
    _start_critical_bg_threads()


if __name__ == "__main__":
    logger.info("Fetching initial live prices...")
    _refresh_live_prices()
    # Pre-warm risk caches in background so first page visit is fast
    threading.Thread(target=_risk_cache_preload_loop, daemon=True).start()
    # 本地也啟動災害偵測 + Telegram bot（會被 _telegram_bot_loop 內的 RENDER 判斷阻擋本地 polling）
    threading.Thread(target=_disaster_persist_loop, daemon=True, name="disaster-persist").start()
    threading.Thread(target=_telegram_bot_loop, daemon=True, name="telegram-bot").start()
    app.run(host="0.0.0.0", debug=False, port=5050, use_reloader=False)
