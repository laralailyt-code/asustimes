-- ============================================================
-- ASUSTIMES Telegram Bot — Migration SQL
-- 在 Supabase SQL Editor 或 tools/run_migrations.py 執行
-- 設計成可重複執行（IF NOT EXISTS）
-- ============================================================

-- ── 1. Telegram 使用者主檔 ────────────────────────────────────
CREATE TABLE IF NOT EXISTS telegram_users (
    id            BIGSERIAL PRIMARY KEY,
    chat_id       BIGINT      NOT NULL UNIQUE,
    username      TEXT,
    first_name    TEXT,
    language_code TEXT,
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    blocked_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_users_chat_id  ON telegram_users(chat_id);
CREATE INDEX IF NOT EXISTS idx_users_active   ON telegram_users(is_active);


-- ── 2. 訂閱規則（一個使用者多筆，OR 邏輯）─────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
    id            BIGSERIAL PRIMARY KEY,
    user_id       BIGINT      NOT NULL REFERENCES telegram_users(id) ON DELETE CASCADE,
    type          TEXT        NOT NULL CHECK (type IN ('region','part','supplier','radius')),
    -- value: JSON, 內容依 type 不同：
    --   region:   {"region": "中國大陸"}                或 {"region": "華東"}
    --   part:     {"part_category": "BATTERY"}
    --   supplier: {"supplier_id": 123}                  (suppliers.id)
    --   radius:   {"lat": 24.76, "lng": 120.99, "km": 50, "label": "新竹 50km"}
    value         JSONB       NOT NULL,
    min_severity  TEXT        NOT NULL DEFAULT 'low' CHECK (min_severity IN ('low','medium','high')),
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    muted_until   TIMESTAMPTZ,             -- Inline 按鈕「靜音 24h」
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_subs_user        ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subs_active      ON subscriptions(is_active) WHERE is_active = TRUE;
CREATE INDEX IF NOT EXISTS idx_subs_type        ON subscriptions(type);


-- ── 3. 推播去重 + 稽核 ────────────────────────────────────────
CREATE TABLE IF NOT EXISTS notification_log (
    id              BIGSERIAL PRIMARY KEY,
    user_id         BIGINT      NOT NULL,
    event_id        TEXT        NOT NULL,
    subscription_id BIGINT      NOT NULL,
    sent_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status          TEXT        NOT NULL CHECK (status IN ('sent','failed','blocked','muted')),
    error_message   TEXT,
    UNIQUE(user_id, event_id)               -- 同事件對同人只推一次
);
CREATE INDEX IF NOT EXISTS idx_notif_event      ON notification_log(event_id);
CREATE INDEX IF NOT EXISTS idx_notif_user_sent  ON notification_log(user_id, sent_at DESC);


-- ── 4. 風險事件落地（為了去重 + 推播）─────────────────────────
-- 現有的事件來源都沒有持久化（USGS/NHC/GDACS proxy on-demand），
-- 這張表是為了「偵測新事件」與「事件 ID 穩定化」。
CREATE TABLE IF NOT EXISTS risk_events (
    id              TEXT PRIMARY KEY,         -- 事件 hash 或 source-id
    type            TEXT,                     -- disaster/geopolitical/strike/operational/war
    title           TEXT,
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    impact          TEXT,                     -- CRITICAL/HIGH/MED/LOW
    region          TEXT,
    occurred_at     TIMESTAMPTZ,              -- 事件發生時間（time 欄位轉換）
    supply_note     TEXT,                     -- 供應鏈影響說明（原 supply）
    source          TEXT,
    source_url      TEXT,
    raw_data        JSONB,                    -- 完整原始資料（除錯用）
    notified        BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_events_type      ON risk_events(type);
CREATE INDEX IF NOT EXISTS idx_events_region    ON risk_events(region);
CREATE INDEX IF NOT EXISTS idx_events_pending   ON risk_events(notified) WHERE notified = FALSE;
CREATE INDEX IF NOT EXISTS idx_events_geo       ON risk_events(lat, lng);


-- ── 5. 供應商主檔（從 suppliers.json 同步）────────────────────
-- 現有 suppliers.json 是地區聚合（22 筆，region+part_category+lat+lng），
-- 這張表保留同樣結構，未來 user 提供更詳細名單時可擴充欄位
CREATE TABLE IF NOT EXISTS suppliers (
    id              BIGSERIAL PRIMARY KEY,
    name            TEXT,                     -- 供應商名稱（目前空，未來填）
    region          TEXT NOT NULL,            -- 中國大陸/華東/台灣等（對應 _REGION_TO_CLUSTERS）
    country         TEXT,                     -- ISO 國碼或中文國名
    city            TEXT,
    lat             DOUBLE PRECISION,
    lng             DOUBLE PRECISION,
    part_categories TEXT[],                   -- ['BATTERY','IC','MEMORY',...]
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_supp_region      ON suppliers(region);
CREATE INDEX IF NOT EXISTS idx_supp_parts       ON suppliers USING GIN (part_categories);
CREATE INDEX IF NOT EXISTS idx_supp_geo         ON suppliers(lat, lng);


-- ── 6. 料件主檔（M2 之後使用，先建好 schema）─────────────────
CREATE TABLE IF NOT EXISTS parts (
    id              BIGSERIAL PRIMARY KEY,
    part_no         TEXT UNIQUE,
    category        TEXT,                     -- BATTERY/IC/MEMORY/...
    name            TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_parts_category   ON parts(category);


-- ── 7. 供應商 ↔ 料件 多對多關聯 ───────────────────────────────
CREATE TABLE IF NOT EXISTS supplier_parts (
    supplier_id BIGINT NOT NULL REFERENCES suppliers(id) ON DELETE CASCADE,
    part_id     BIGINT NOT NULL REFERENCES parts(id)     ON DELETE CASCADE,
    PRIMARY KEY (supplier_id, part_id)
);


-- ── 8. 訂閱精靈狀態（多步驟對話狀態暫存）──────────────────────
-- ConversationHandler 會用記憶體存狀態，但 worker restart 會掉
-- 用此表持久化，PTB 用 PicklePersistence 或自訂 Persistence 寫進這張
CREATE TABLE IF NOT EXISTS wizard_state (
    chat_id    BIGINT PRIMARY KEY,
    state      TEXT,                          -- 步驟編號或狀態名稱
    data       JSONB,                         -- 暫存的選擇
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
