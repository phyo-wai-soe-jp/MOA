import csv
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/moa_test")

SESSION_LIFETIME_HOURS = 3

CATEGORY_ORDER = ["定食", "丼もの", "単品・おつまみ", "盛り合わせ", "期間限定"]

OPTION_GROUPS_BY_MENU_ID = {
    1: ["ご飯の量", "唐揚げの個数", "ソース追加"],
    2: ["ご飯の量", "唐揚げの個数", "ソース追加"],
    7: ["ソース追加"],
    10: ["ソース追加"],
}

OPTION_GROUPS = {
    "ご飯の量": [
        {"name": "小盛り（少なめ）", "price_delta": -50, "is_default": False},
        {"name": "普通", "price_delta": 0, "is_default": True},
        {"name": "大盛り", "price_delta": 100, "is_default": False},
        {"name": "特盛り（ガッツリ！）", "price_delta": 200, "is_default": False},
    ],
    "唐揚げの個数": [
        {"name": "3個（少なめ）", "price_delta": -100, "is_default": False},
        {"name": "4個（標準）", "price_delta": 0, "is_default": True},
        {"name": "6個（満腹）", "price_delta": 250, "is_default": False},
    ],
    "ソース追加": [
        {"name": "なし（プレーン）", "price_delta": 0, "is_default": True},
        {"name": "自慢のマヨネーズ", "price_delta": 0, "is_default": False},
        {"name": "特製タルタルソース", "price_delta": 50, "is_default": False},
        {"name": "ねぎ塩だれ（さっぱり）", "price_delta": 60, "is_default": False},
        {"name": "ハニーマスタード", "price_delta": 70, "is_default": False},
        {"name": "激辛レッドソース", "price_delta": 80, "is_default": False},
        {"name": "レモン（追加用1個）", "price_delta": 200, "is_default": False},
    ],
}


def get_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


# ---------- Schema / seed ----------

def ensure_database():
    base_dir = os.path.dirname(__file__)
    menu_path = os.path.join(base_dir, "menu.csv")
    schema_path = os.path.join(base_dir, "schema.sql")

    with open(menu_path, newline="", encoding="utf-8") as f:
        menu_rows = list(csv.DictReader(f))
    expected_names = [row["name"].strip() for row in menu_rows]

    conn = get_conn()
    cur = dict_cursor(conn)
    with open(schema_path, encoding="utf-8") as f:
        cur.execute(f.read())

    cur.execute("SELECT name FROM menu_items ORDER BY sort_order")
    current_names = [row["name"] for row in cur.fetchall()]
    if current_names == expected_names:
        conn.close()
        return

    cur.execute("DELETE FROM payments")
    cur.execute("DELETE FROM order_status_history")
    cur.execute("DELETE FROM order_item_options")
    cur.execute("DELETE FROM order_items")
    cur.execute("DELETE FROM orders")
    cur.execute("DELETE FROM sessions")
    cur.execute("DELETE FROM qr_codes")
    cur.execute("DELETE FROM option_choices")
    cur.execute("DELETE FROM option_groups")
    cur.execute("DELETE FROM menu_items")
    cur.execute("DELETE FROM menu_categories")

    category_ids = {}
    for i, name in enumerate(CATEGORY_ORDER):
        cur.execute(
            "INSERT INTO menu_categories (name, sort_order) VALUES (%s, %s) RETURNING id",
            (name, i),
        )
        category_ids[name] = cur.fetchone()["id"]

    item_ids = {}
    for i, row in enumerate(menu_rows):
        menu_id = int(row["menu_id"])
        cur.execute(
            "INSERT INTO menu_items (category_id, name, base_price, image_url, description, sort_order) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (
                category_ids[row["category"].strip()],
                row["name"].strip(),
                int(row["base_price"]),
                row["image_url"].strip() or None,
                row.get("description", "").strip() or None,
                i + 1,
            ),
        )
        item_ids[menu_id] = cur.fetchone()["id"]

    for menu_id, group_names in OPTION_GROUPS_BY_MENU_ID.items():
        item_id = item_ids.get(menu_id)
        if not item_id:
            continue
        for gi, group_name in enumerate(group_names):
            cur.execute(
                "INSERT INTO option_groups (item_id, name, selection_type, is_required, sort_order) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (item_id, group_name, "single", True, gi),
            )
            group_id = cur.fetchone()["id"]
            for ci, choice in enumerate(OPTION_GROUPS[group_name]):
                cur.execute(
                    "INSERT INTO option_choices (group_id, name, price_delta, is_default, sort_order) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        group_id,
                        choice["name"],
                        choice["price_delta"],
                        choice["is_default"],
                        ci,
                    ),
                )

    conn.close()


# ---------- Menu ----------

def get_menu():
    """Returns categories in order, each with its items, each item with its option groups/choices."""
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM menu_categories ORDER BY sort_order, name")
    categories = cur.fetchall()

    cur.execute(
        "SELECT * FROM menu_items WHERE is_available = TRUE ORDER BY sort_order, name"
    )
    items = cur.fetchall()

    cur.execute("SELECT * FROM option_groups ORDER BY sort_order")
    groups = cur.fetchall()

    cur.execute("SELECT * FROM option_choices ORDER BY sort_order")
    choices = cur.fetchall()
    conn.close()

    choices_by_group = {}
    for c in choices:
        choices_by_group.setdefault(str(c["group_id"]), []).append(c)

    groups_by_item = {}
    for g in groups:
        g = dict(g)
        g["choices"] = choices_by_group.get(str(g["id"]), [])
        groups_by_item.setdefault(str(g["item_id"]), []).append(g)

    items_by_category = {}
    for item in items:
        item = dict(item)
        item["option_groups"] = groups_by_item.get(str(item["id"]), [])
        item["customizable"] = len(item["option_groups"]) > 0
        items_by_category.setdefault(str(item["category_id"]), []).append(item)

    result = []
    for cat in categories:
        cat = dict(cat)
        cat["items"] = items_by_category.get(str(cat["id"]), [])
        if cat["items"]:
            result.append(cat)
    return result


def get_menu_item(item_id):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM menu_items WHERE id = %s", (item_id,))
    item = cur.fetchone()
    conn.close()
    return dict(item) if item else None


def get_choice(choice_id):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute(
        "SELECT oc.*, og.name AS group_name FROM option_choices oc "
        "JOIN option_groups og ON og.id = oc.group_id WHERE oc.id = %s",
        (choice_id,),
    )
    choice = cur.fetchone()
    conn.close()
    return dict(choice) if choice else None


# ---------- Tables / QR / Sessions ----------

def get_or_create_table(label):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM tables WHERE label = %s", (label,))
    row = cur.fetchone()
    if row:
        conn.close()
        return dict(row)
    cur.execute(
        "INSERT INTO tables (label) VALUES (%s) RETURNING *", (label,)
    )
    row = cur.fetchone()
    conn.close()
    return dict(row)


def get_or_create_active_session(table_id):
    """Reuse an active, unexpired session for this table, or start a new one."""
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute(
        "SELECT * FROM sessions WHERE table_id = %s AND status = 'active' "
        "AND expires_at > now() ORDER BY started_at DESC LIMIT 1",
        (table_id,),
    )
    row = cur.fetchone()
    if row:
        conn.close()
        return dict(row)

    token = uuid.uuid4().hex
    expires_at = datetime.now(timezone.utc) + timedelta(hours=SESSION_LIFETIME_HOURS)
    cur.execute(
        "INSERT INTO sessions (table_id, token, expires_at) VALUES (%s, %s, %s) RETURNING *",
        (table_id, token, expires_at),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row)


def get_session_by_token(token):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM sessions WHERE token = %s", (token,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def close_session(session_id):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "UPDATE sessions SET status = 'closed', ended_at = now() WHERE id = %s",
        (session_id,),
    )
    conn.close()


# ---------- Orders ----------

def create_order(table_id, session_id, cart, tax_rate=0.0, service_rate=0.0):
    """cart: list of {item_id, qty, unit_price, options: [{group_name, choice_name, choice_id, price_delta}]}"""
    subtotal = sum(c["unit_price"] * c["qty"] for c in cart)
    tax = round(subtotal * tax_rate, 2)
    service_charge = round(subtotal * service_rate, 2)
    total = subtotal + tax + service_charge

    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute(
        "INSERT INTO orders (table_id, session_id, subtotal, tax, service_charge, total) "
        "VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
        (table_id, session_id, subtotal, tax, service_charge, total),
    )
    order = dict(cur.fetchone())

    for line in cart:
        line_total = line["unit_price"] * line["qty"]
        cur.execute(
            "INSERT INTO order_items (order_id, item_id, quantity, base_price, line_total) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id",
            (order["id"], line["item_id"], line["qty"], line["base_price"], line_total),
        )
        order_item_id = cur.fetchone()["id"]
        for opt in line.get("options", []):
            cur.execute(
                "INSERT INTO order_item_options (order_item_id, choice_id, group_name, choice_name, price_delta) "
                "VALUES (%s, %s, %s, %s, %s)",
                (order_item_id, opt.get("choice_id"), opt["group_name"], opt["choice_name"], opt["price_delta"]),
            )

    cur.execute(
        "INSERT INTO order_status_history (order_id, status, changed_by) VALUES (%s, %s, %s)",
        (order["id"], 0, "system"),
    )
    conn.close()
    return get_order(order["id"])


def get_order(order_id):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        conn.close()
        return None
    order = dict(order)

    cur.execute(
        "SELECT oi.*, mi.name AS item_name FROM order_items oi "
        "JOIN menu_items mi ON mi.id = oi.item_id WHERE oi.order_id = %s",
        (order_id,),
    )
    items = [dict(i) for i in cur.fetchall()]
    for item in items:
        cur.execute(
            "SELECT * FROM order_item_options WHERE order_item_id = %s", (item["id"],)
        )
        item["options"] = [dict(o) for o in cur.fetchall()]
    order["items"] = items
    conn.close()
    return order


def get_orders_for_session(session_id):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute(
        "SELECT id FROM orders WHERE session_id = %s ORDER BY created_at", (session_id,)
    )
    ids = [row["id"] for row in cur.fetchall()]
    conn.close()
    return [get_order(i) for i in ids]


def get_all_orders(limit=200):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute(
        "SELECT o.*, t.label AS table_label FROM orders o "
        "LEFT JOIN tables t ON t.id = o.table_id "
        "ORDER BY o.created_at DESC LIMIT %s",
        (limit,),
    )
    orders = [dict(o) for o in cur.fetchall()]
    conn.close()
    result = []
    for o in orders:
        full = get_order(o["id"])
        full["table_label"] = o["table_label"]
        result.append(full)
    return result


def record_payment(order_id, amount, method="cash"):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO payments (order_id, amount, method, status) VALUES (%s, %s, %s, %s)",
        (order_id, amount, method, 1),
    )
    cur.execute("UPDATE orders SET status = 3, updated_at = now() WHERE id = %s", (order_id,))
    cur.execute(
        "INSERT INTO order_status_history (order_id, status, changed_by) VALUES (%s, %s, %s)",
        (order_id, 3, "system"),
    )
    conn.close()


# ---------- Staff ----------

def get_staff_by_email(email):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute("SELECT * FROM staff WHERE email = %s AND is_active = TRUE", (email,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def create_staff(name, email, password_hash, role=0):
    conn = get_conn()
    cur = dict_cursor(conn)
    cur.execute(
        "INSERT INTO staff (name, email, password_hash, role) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (email) DO NOTHING RETURNING *",
        (name, email, password_hash, role),
    )
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None
