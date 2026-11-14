-- Ledger schema. All inventory math happens here or in plain Python, never in the LLM.
-- Idempotent: safe to run repeatedly.

CREATE TABLE IF NOT EXISTS items (
    id            SERIAL PRIMARY KEY,
    name          TEXT UNIQUE NOT NULL,          -- matches grab zone names, e.g. 'pepperoni'
    unit          TEXT NOT NULL,                 -- 'lb', 'each', 'oz'
    unit_cost     NUMERIC(10,2) NOT NULL,        -- $ per unit
    par           NUMERIC(10,2) NOT NULL,        -- reorder below this
    qty_per_label JSONB NOT NULL DEFAULT '{}'    -- {"pinch":0.05,"handful":0.25,"whole_container":5}
);

-- Menu item -> ingredient quantity per unit sold
CREATE TABLE IF NOT EXISTS recipes (
    menu_item TEXT NOT NULL,
    item_id   INT NOT NULL REFERENCES items(id),
    qty       NUMERIC(10,3) NOT NULL,
    PRIMARY KEY (menu_item, item_id)
);

CREATE TABLE IF NOT EXISTS pos_sales (
    id        SERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL,
    menu_item TEXT NOT NULL,
    qty       INT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'synthetic'  -- 'synthetic' | 'register_export'
);

CREATE TABLE IF NOT EXISTS purchases (
    id        SERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL,
    item_id   INT NOT NULL REFERENCES items(id),
    qty       NUMERIC(10,2) NOT NULL,
    unit_cost NUMERIC(10,2) NOT NULL,
    supplier  TEXT,
    source    TEXT NOT NULL DEFAULT 'synthetic'  -- 'synthetic' | 'invoice' | 'agent_order'
);

-- Physical counts (manual or camera-estimated). Latest count anchors on-hand stock.
CREATE TABLE IF NOT EXISTS counts (
    id      SERIAL PRIMARY KEY,
    ts      TIMESTAMPTZ NOT NULL,
    item_id INT NOT NULL REFERENCES items(id),
    qty     NUMERIC(10,2) NOT NULL,
    source  TEXT NOT NULL DEFAULT 'manual'
);

-- One row per grab package (see docs/architecture.md, "The handoff: the grab package")
CREATE TABLE IF NOT EXISTS grab_events (
    grab_id     TEXT PRIMARY KEY,
    zone        TEXT NOT NULL,
    camera_id   TEXT,
    source      TEXT,                            -- 'webcam' | 'iphone' | 'fake'
    t_start     TIMESTAMPTZ NOT NULL,
    t_end       TIMESTAMPTZ NOT NULL,
    item_id     INT REFERENCES items(id),        -- NULL when d1 said 'none'
    action      TEXT,                            -- 'took' | 'put_back' | 'touched'
    qty_label   TEXT,                            -- 'pinch' | 'handful' | 'whole_container'
    qty         NUMERIC(10,3),                   -- qty_label converted via items.qty_per_label
    confidence  NUMERIC(5,4),                    -- min probability across the deciding d1 answers
    status      TEXT NOT NULL DEFAULT 'flagged', -- 'logged' | 'flagged' | 'confirmed'
    frames      JSONB NOT NULL,                  -- {"before": path, "during": [paths], "after": path}
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw d1 answers per grab, kept for the trace view and for debugging
CREATE TABLE IF NOT EXISTS d1_answers (
    id          SERIAL PRIMARY KEY,
    grab_id     TEXT NOT NULL REFERENCES grab_events(grab_id) ON DELETE CASCADE,
    question_id TEXT NOT NULL,
    answer      TEXT NOT NULL,
    probs       JSONB NOT NULL,                  -- {"took":0.88,"put_back":0.07,"touched":0.05}
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS orders (
    id         SERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    item_id    INT NOT NULL REFERENCES items(id),
    qty        NUMERIC(10,2) NOT NULL,
    supplier   TEXT,
    status     TEXT NOT NULL DEFAULT 'draft',    -- 'draft' | 'approved' | 'rejected'
    chat_id    TEXT
);

-- Every message the agent tried to send, with its d1 guard result
CREATE TABLE IF NOT EXISTS messages (
    id        SERIAL PRIMARY KEY,
    ts        TIMESTAMPTZ NOT NULL DEFAULT now(),
    chat_id   TEXT,
    text      TEXT NOT NULL,
    d1_score  NUMERIC(5,4),
    decision  TEXT NOT NULL,                     -- 'sent' | 'softened' | 'blocked'
    claims    JSONB NOT NULL DEFAULT '[]'        -- [{"claim":..., "score":...}]
);

CREATE INDEX IF NOT EXISTS grab_events_t_start ON grab_events (t_start);
CREATE INDEX IF NOT EXISTS pos_sales_ts ON pos_sales (ts);
