-- Mobile Order App — full schema matching the drawSQL ERD
CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- for gen_random_uuid()

-- ---------- Menu ----------
CREATE TABLE IF NOT EXISTS menu_categories (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    sort_order INT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS menu_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category_id UUID REFERENCES menu_categories(id),
    name VARCHAR(255) NOT NULL,
    name_ja VARCHAR(255),
    description TEXT,
    base_price DECIMAL(10, 2) NOT NULL,
    image_url TEXT,
    is_available BOOLEAN DEFAULT TRUE,
    sort_order INT DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS option_groups (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    item_id UUID REFERENCES menu_items(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    selection_type VARCHAR(10) NOT NULL DEFAULT 'single', -- 'single' | 'multi'
    is_required BOOLEAN DEFAULT TRUE,
    sort_order INT DEFAULT 0
);

CREATE TABLE IF NOT EXISTS option_choices (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    group_id UUID REFERENCES option_groups(id) ON DELETE CASCADE,
    name VARCHAR(255) NOT NULL,
    price_delta DECIMAL(10, 2) DEFAULT 0,
    is_default BOOLEAN DEFAULT FALSE,
    sort_order INT DEFAULT 0
);

-- ---------- Tables / QR / Sessions ----------
CREATE TABLE IF NOT EXISTS tables (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label VARCHAR(50) UNIQUE NOT NULL,
    seats INT,
    qr_token VARCHAR(128),
    customer_num INT DEFAULT 0,
    status VARCHAR(20) DEFAULT 'vacant',
    is_active BOOLEAN DEFAULT TRUE
);

ALTER TABLE tables ADD COLUMN IF NOT EXISTS qr_token VARCHAR(128);
ALTER TABLE tables ADD COLUMN IF NOT EXISTS customer_num INT DEFAULT 0;
ALTER TABLE tables ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'vacant';
UPDATE tables SET customer_num = 0 WHERE customer_num IS NULL;
UPDATE tables SET status = 'vacant' WHERE status IS NULL;

CREATE TABLE IF NOT EXISTS qr_codes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    table_id UUID REFERENCES tables(id),
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    table_id UUID REFERENCES tables(id),
    qr_code_id UUID REFERENCES qr_codes(id),
    token VARCHAR(128) UNIQUE NOT NULL,
    guest_name VARCHAR(255),
    status VARCHAR(20) DEFAULT 'active', -- active | closed
    started_at TIMESTAMPTZ DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL,
    ended_at TIMESTAMPTZ
);

-- ---------- Staff ----------
CREATE TABLE IF NOT EXISTS staff (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role INT DEFAULT 0, -- 0 = staff, 1 = manager
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- ---------- Orders ----------
CREATE TABLE IF NOT EXISTS orders (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    table_id UUID REFERENCES tables(id),
    session_id UUID REFERENCES sessions(id),
    order_number SERIAL,
    status INT DEFAULT 0, -- 0 received, 1 preparing, 2 served, 3 paid, 4 cancelled
    subtotal DECIMAL(10, 2) NOT NULL,
    tax DECIMAL(10, 2) DEFAULT 0,
    service_charge DECIMAL(10, 2) DEFAULT 0,
    total DECIMAL(10, 2) NOT NULL,
    notes TEXT,
    created_at TIMESTAMPTZ DEFAULT now(),
    updated_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS order_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID REFERENCES orders(id) ON DELETE CASCADE,
    item_id UUID REFERENCES menu_items(id),
    quantity INT NOT NULL,
    base_price DECIMAL(10, 2) NOT NULL,
    line_total DECIMAL(10, 2) NOT NULL,
    special_instructions TEXT
);

CREATE TABLE IF NOT EXISTS order_item_options (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_item_id UUID REFERENCES order_items(id) ON DELETE CASCADE,
    choice_id UUID REFERENCES option_choices(id),
    group_name VARCHAR(255),
    choice_name VARCHAR(255),
    price_delta DECIMAL(10, 2) DEFAULT 0
);

CREATE TABLE IF NOT EXISTS order_status_history (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID REFERENCES orders(id) ON DELETE CASCADE,
    status INT NOT NULL,
    changed_at TIMESTAMPTZ DEFAULT now(),
    changed_by VARCHAR(255)
);

CREATE TABLE IF NOT EXISTS payments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id UUID REFERENCES orders(id),
    amount DECIMAL(10, 2) NOT NULL,
    method VARCHAR(50),
    status INT DEFAULT 0, -- 0 pending, 1 paid, 2 refunded
    provider_ref VARCHAR(255),
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_id);
CREATE INDEX IF NOT EXISTS idx_orders_table ON orders(table_id);
CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tables_qr_token ON tables(qr_token) WHERE qr_token IS NOT NULL;
