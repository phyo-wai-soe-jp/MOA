"""Seed menu_categories / menu_items / option_groups / option_choices.

The menu data follows menu.csv and the option tables supplied for the MOA menu.
Usage: python migrate_menu.py
"""
import csv
import os

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5432/moa_test"
)

CATEGORY_ORDER = ["定食", "丼もの", "単品・おつまみ", "盛り合わせ", "期間限定", "ドリンク", "酒類"]

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


def main():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    menu_path = os.path.join(os.path.dirname(__file__), "menu.csv")
    with open(menu_path, newline="", encoding="utf-8") as f:
        menu_rows = list(csv.DictReader(f))
    expected_names = [row["name"].strip() for row in menu_rows]

    # Apply schema (safe to re-run: CREATE TABLE/EXTENSION IF NOT EXISTS)
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, encoding="utf-8") as f:
        cur.execute(f.read())
    print("Schema applied")

    cur.execute("SELECT name FROM menu_items ORDER BY sort_order")
    current_names = [row["name"] for row in cur.fetchall()]
    if current_names == expected_names:
        print("Menu already seeded, skipping.")
        return

    if current_names:
        print("Existing menu differs from menu.csv; clearing old menu and order data.")
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

    # categories
    category_ids = {}
    for i, name in enumerate(CATEGORY_ORDER):
        cur.execute(
            "INSERT INTO menu_categories (name, sort_order) VALUES (%s, %s) RETURNING id",
            (name, i),
        )
        category_ids[name] = cur.fetchone()["id"]
    print(f"Inserted {len(category_ids)} categories")

    # items from csv
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
    print(f"Inserted {len(item_ids)} menu items")

    # option groups + choices, attached to specific items
    group_total = 0
    choice_total = 0
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
            group_total += 1
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
                choice_total += 1
    print(f"Inserted {group_total} option groups, {choice_total} option choices")


if __name__ == "__main__":
    main()
