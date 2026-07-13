import json
import os
import uuid
from decimal import Decimal

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, make_response
from flask.json.provider import DefaultJSONProvider
from werkzeug.security import check_password_hash, generate_password_hash

import db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "mobile-order-secret")

DEFAULT_TABLE_LABEL = "デフォルト"
TOKEN_COOKIE = "moa_token"


class DecimalSafeJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)


app.json = DecimalSafeJSONProvider(app)


def init_admin():
    email = os.environ.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD")
    if email and password:
        db.create_staff("Admin", email, generate_password_hash(password, method="pbkdf2:sha256"), role=1)


init_admin()


# ---------- Table / session resolution ----------

def current_session():
    token = request.cookies.get(TOKEN_COOKIE)
    if not token:
        return None
    return db.get_session_by_token(token)


def require_table_session():
    """Ensure the request has a valid table session; returns (session, new_session_or_None).
    If no session cookie is present, transparently starts one on the default table."""
    sess = current_session()
    if sess and sess["status"] == "active":
        return sess, None
    table = db.get_or_create_table(DEFAULT_TABLE_LABEL)
    sess = db.get_or_create_active_session(table["id"])
    return sess, sess


def set_session_cookie(resp, sess):
    resp.set_cookie(TOKEN_COOKIE, sess["token"], max_age=60 * 60 * 6, httponly=True, samesite="Lax")
    return resp


# ---------- Customer entry points ----------

@app.get("/t/<label>")
def enter_table(label):
    table = db.get_or_create_table(label)
    sess = db.get_or_create_active_session(table["id"])
    resp = make_response(redirect(url_for("index")))
    return set_session_cookie(resp, sess)


@app.get("/")
def index():
    sess, new_sess = require_table_session()
    menu = db.get_menu()
    resp = make_response(render_template("order.html", menu=menu))
    if new_sess:
        set_session_cookie(resp, new_sess)
    return resp


# ---------- Cart helpers ----------

def build_cart_line(item_id, qty, choice_ids):
    item = db.get_menu_item(item_id)
    if not item:
        return None

    unit_price = float(item["base_price"])
    options = []
    for choice_id in choice_ids or []:
        choice = db.get_choice(choice_id)
        if not choice:
            continue
        unit_price += float(choice["price_delta"])
        options.append(
            {
                "choice_id": str(choice["id"]),
                "group_name": choice["group_name"],
                "choice_name": choice["name"],
                "price_delta": float(choice["price_delta"]),
            }
        )

    line_key = json.dumps(
        {"item_id": str(item_id), "choice_ids": sorted(choice_ids or [])},
        sort_keys=True,
    )
    option_summary = " / ".join(o["choice_name"] for o in options)

    return {
        "key": line_key,
        "item_id": str(item_id),
        "name": item["name"],
        "base_price": int(round(float(item["base_price"]))),
        "price": int(round(unit_price)),
        "qty": qty,
        "options": options,
        "option_summary": option_summary,
    }


def cart_summary(cart):
    total = sum(c["price"] * c["qty"] for c in cart)
    count = sum(c["qty"] for c in cart)
    return {"cart": cart, "total": total, "count": count}


@app.post("/api/cart/add")
def add_to_cart():
    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")
    qty = max(int(data.get("qty", 1)), 1)
    choice_ids = data.get("choice_ids") or []

    line = build_cart_line(item_id, qty, choice_ids)
    if not line:
        return jsonify({"error": "Item not found"}), 404

    cart = session.get("cart", [])
    existing = next((c for c in cart if c["key"] == line["key"]), None)
    if existing:
        existing["qty"] += qty
    else:
        cart.append(line)
    session["cart"] = cart
    session.modified = True
    return jsonify(cart_summary(cart))


@app.post("/api/cart/remove")
def remove_from_cart():
    data = request.get_json(silent=True) or {}
    line_key = data.get("key")
    cart = [c for c in session.get("cart", []) if c["key"] != line_key]
    session["cart"] = cart
    session.modified = True
    return jsonify(cart_summary(cart))


@app.get("/api/cart")
def get_cart():
    return jsonify(cart_summary(session.get("cart", [])))


@app.get("/api/bill")
def get_bill():
    sess, _ = require_table_session()
    return jsonify(bill_payload(sess))


def order_for_api(order):
    items = []
    for item in order["items"]:
        qty = item["quantity"]
        options = item.get("options", [])
        items.append(
            {
                "name": item["item_name"],
                "qty": qty,
                "price": int(round(float(item["line_total"]) / qty)) if qty else 0,
                "option_summary": " / ".join(o["choice_name"] for o in options),
            }
        )
    return {
        "id": str(order["id"])[:8].upper(),
        "total": int(round(float(order["total"]))),
        "items": items,
    }


def bill_payload(sess):
    orders = db.get_orders_for_session(sess["id"])
    total = int(round(sum(float(o["total"]) for o in orders)))
    count = sum(sum(i["quantity"] for i in o["items"]) for o in orders)
    cart = session.get("cart", [])
    cs = cart_summary(cart)
    return {
        "orders": [order_for_api(o) for o in orders],
        "total": total,
        "count": count,
        "order_count": len(orders),
        "cart": cart,
        "cart_total": cs["total"],
        "cart_count": cs["count"],
    }


# ---------- Order submission ----------

def cart_to_db_lines(cart):
    lines = []
    for c in cart:
        lines.append(
            {
                "item_id": c["item_id"],
                "qty": c["qty"],
                "unit_price": c["price"],
                "base_price": c["base_price"],
                "options": c["options"],
            }
        )
    return lines


@app.post("/order")
def place_order():
    sess, _ = require_table_session()
    cart = session.get("cart", [])

    order = None
    if cart:
        order = db.create_order(sess["table_id"], sess["id"], cart_to_db_lines(cart))

    session["cart"] = []
    session.modified = True

    orders = db.get_orders_for_session(sess["id"])
    bill_total = int(round(sum(float(o["total"]) for o in orders)))
    bill_count = sum(sum(i["quantity"] for i in o["items"]) for o in orders)

    return render_template(
        "receipt.html",
        mode="order",
        table=db_table_label(sess["table_id"]),
        items=[{**line, "emoji": "📷"} for line in cart],
        total=int(round(float(order["total"]))) if order else 0,
        order_id=str(order["id"])[:8].upper() if order else None,
        bill={
            "total": bill_total,
            "order_count": len(orders),
            "count": bill_count,
        },
    )


@app.post("/api/orders")
def create_order_api():
    sess, _ = require_table_session()
    cart = session.get("cart", [])
    if not cart:
        return jsonify({"error": "Cart is empty", **bill_payload(sess)}), 400

    db.create_order(sess["table_id"], sess["id"], cart_to_db_lines(cart))
    session["cart"] = []
    session.modified = True
    return jsonify(bill_payload(sess))


@app.post("/bill/checkout")
def checkout_bill():
    sess, _ = require_table_session()
    cart = session.get("cart", [])

    if cart:
        db.create_order(sess["table_id"], sess["id"], cart_to_db_lines(cart))
        session["cart"] = []
        session.modified = True

    orders = db.get_orders_for_session(sess["id"])
    total = int(round(sum(float(o["total"]) for o in orders)))

    for o in orders:
        db.record_payment(o["id"], o["total"])
    db.close_session(sess["id"])

    resp = make_response(
        render_template(
            "receipt.html",
            mode="bill",
            table=db_table_label(sess["table_id"]),
            orders=[order_for_receipt(o) for o in orders],
            total=total,
            bill_id=str(sess["id"])[:8].upper(),
        )
    )
    resp.delete_cookie(TOKEN_COOKIE)
    return resp


def order_for_receipt(order):
    items = []
    for i in order["items"]:
        items.append(
            {
                "name": i["item_name"],
                "qty": i["quantity"],
                "price": float(i["base_price"]) if not i["options"] else float(i["line_total"]) / i["quantity"],
                "emoji": "📷",
                "option_summary": " / ".join(o["choice_name"] for o in i["options"]),
            }
        )
    return {"id": str(order["id"])[:8].upper(), "total": int(round(float(order["total"]))), "items": items}


def db_table_label(table_id):
    conn = db.get_conn()
    cur = db.dict_cursor(conn)
    cur.execute("SELECT label FROM tables WHERE id = %s", (table_id,))
    row = cur.fetchone()
    conn.close()
    return row["label"] if row else "?"


# ---------- Staff auth ----------

def current_staff():
    staff_id = session.get("staff_id")
    if not staff_id:
        return None
    conn = db.get_conn()
    cur = db.dict_cursor(conn)
    cur.execute("SELECT * FROM staff WHERE id = %s", (staff_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


@app.get("/admin/login")
def admin_login_form():
    return render_template("admin_login.html", error=None)


@app.post("/admin/login")
def admin_login():
    email = request.form.get("email", "")
    password = request.form.get("password", "")
    staff = db.get_staff_by_email(email)
    if not staff or not check_password_hash(staff["password_hash"], password):
        return render_template("admin_login.html", error="メールアドレスまたはパスワードが違います"), 401
    session["staff_id"] = str(staff["id"])
    return redirect(url_for("admin"))


@app.get("/admin/logout")
def admin_logout():
    session.pop("staff_id", None)
    return redirect(url_for("admin_login_form"))


@app.get("/admin")
def admin():
    staff = current_staff()
    if not staff:
        return redirect(url_for("admin_login_form"))
    orders = db.get_all_orders()
    grand_total = sum(float(o["total"]) for o in orders)
    return render_template("admin.html", orders=orders, grand_total=grand_total, staff=staff)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
