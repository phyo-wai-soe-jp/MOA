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
STATUS_LABELS = {
    db.TABLE_STATUS_VACANT: "空席",
    db.TABLE_STATUS_IN_USE: "利用中",
    db.TABLE_STATUS_CHECKOUT_WAITING: "会計待ち",
    db.TABLE_STATUS_PAID: "会計済み",
    db.SESSION_STATUS_ACTIVE: "利用中",
    db.SESSION_STATUS_CHECKOUT_WAITING: "会計待ち",
    db.SESSION_STATUS_CLOSED: "会計済み",
    db.ORDER_STATUS_RECEIVED: "未確認",
    db.ORDER_STATUS_PREPARING: "調理中",
    db.ORDER_STATUS_SERVED: "提供済み",
    db.ORDER_STATUS_PAID: "会計済み",
    db.ORDER_STATUS_CANCELLED: "キャンセル",
}


class DecimalSafeJSONProvider(DefaultJSONProvider):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        if isinstance(obj, uuid.UUID):
            return str(obj)
        return super().default(obj)


app.json = DecimalSafeJSONProvider(app)

db.ensure_database()


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
    if sess and sess["status"] in (db.SESSION_STATUS_ACTIVE, db.SESSION_STATUS_CHECKOUT_WAITING):
        return sess, None
    table = db.get_or_create_table(DEFAULT_TABLE_LABEL)
    sess = db.get_or_create_active_session(table["id"])
    return sess, sess


def set_session_cookie(resp, sess):
    resp.set_cookie(TOKEN_COOKIE, sess["token"], max_age=60 * 60 * 6, httponly=True, samesite="Lax")
    return resp


def customer_num_from_request():
    raw = request.args.get("customers") or request.args.get("customer_num")
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def table_for_session(sess):
    return db.get_table(sess["table_id"]) if sess else None


def ordering_locked(sess):
    table = table_for_session(sess)
    if not sess or sess["status"] != db.SESSION_STATUS_ACTIVE:
        return True
    if table and table["status"] in (db.TABLE_STATUS_CHECKOUT_WAITING, db.TABLE_STATUS_PAID):
        return True
    return False


def session_state_payload(sess):
    table = table_for_session(sess)
    table_status = table["status"] if table else None
    return {
        "session_status": sess["status"] if sess else None,
        "session_status_label": STATUS_LABELS.get(sess["status"] if sess else None, ""),
        "table_status": table_status,
        "table_status_label": STATUS_LABELS.get(table_status, ""),
        "customer_num": table.get("customer_num") if table else None,
        "ordering_locked": ordering_locked(sess),
    }


# ---------- Customer entry points ----------

@app.get("/t/<label>")
def enter_table(label):
    table = db.get_or_create_table(label)
    sess = db.get_or_create_active_session(table["id"], customer_num_from_request())
    resp = make_response(redirect(url_for("index")))
    return set_session_cookie(resp, sess)


@app.get("/q/<qr_token>")
def enter_table_by_qr(qr_token):
    table = db.get_table_by_qr_token(qr_token)
    if not table:
        return "Invalid table QR token", 404
    sess = db.get_or_create_active_session(table["id"], customer_num_from_request())
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


def current_cart():
    cart = session.get("cart", [])
    cleaned = []

    for line in cart:
        choice_ids = [
            opt.get("choice_id")
            for opt in line.get("options", [])
            if opt.get("choice_id")
        ]
        rebuilt = build_cart_line(line.get("item_id"), line.get("qty", 1), choice_ids)
        if rebuilt:
            cleaned.append(rebuilt)

    if cleaned != cart:
        session["cart"] = cleaned
        session.modified = True

    return cleaned


@app.post("/api/cart/add")
def add_to_cart():
    sess, _ = require_table_session()
    if ordering_locked(sess):
        return jsonify({"error": "このテーブルは会計待ちのため追加注文できません。", **bill_payload(sess)}), 409

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
    return jsonify(cart_summary(current_cart()))


@app.get("/api/bill")
def get_bill():
    sess, _ = require_table_session()
    return jsonify(bill_payload(sess))


@app.get("/api/menu")
def api_menu():
    sess, _ = require_table_session()
    return jsonify({"menu": db.get_menu(), **session_state_payload(sess)})


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
    cart = current_cart()
    cs = cart_summary(cart)
    return {
        "orders": [order_for_api(o) for o in orders],
        "total": total,
        "count": count,
        "order_count": len(orders),
        "cart": cart,
        "cart_total": cs["total"],
        "cart_count": cs["count"],
        **session_state_payload(sess),
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
    if ordering_locked(sess):
        return "このテーブルは会計待ちのため追加注文できません。", 409

    cart = current_cart()

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
    if ordering_locked(sess):
        return jsonify({"error": "このテーブルは会計待ちのため追加注文できません。", **bill_payload(sess)}), 409

    cart = current_cart()
    if not cart:
        return jsonify({"error": "カートが空です。商品をもう一度追加してください。", **bill_payload(sess)}), 400

    try:
        db.create_order(sess["table_id"], sess["id"], cart_to_db_lines(cart))
    except Exception:
        app.logger.exception("Failed to create order")
        return jsonify({"error": "注文を送信できませんでした。ページを更新してもう一度お試しください。", **bill_payload(sess)}), 500

    session["cart"] = []
    session.modified = True
    return jsonify(bill_payload(sess))


@app.post("/api/order")
def create_order_api_compat():
    return create_order_api()


@app.get("/api/order/status")
def order_status_api():
    sess, _ = require_table_session()
    return jsonify(bill_payload(sess))


@app.post("/api/checkout")
def request_checkout_api():
    sess, _ = require_table_session()
    payload = bill_payload(sess)
    if payload["cart_count"] > 0:
        return jsonify({"error": "未送信の商品があります。先に注文を送信してください。", **payload}), 400
    if payload["order_count"] == 0:
        return jsonify({"error": "まだ注文がありません。", **payload}), 400

    db.request_checkout(sess["id"])
    refreshed = db.get_session_by_token(sess["token"])
    return jsonify(bill_payload(refreshed))


@app.post("/bill/checkout")
def checkout_bill():
    sess, _ = require_table_session()
    cart = current_cart()

    if cart and not ordering_locked(sess):
        db.create_order(sess["table_id"], sess["id"], cart_to_db_lines(cart))
        session["cart"] = []
        session.modified = True

    orders = db.get_orders_for_session(sess["id"])
    total = int(round(sum(float(o["total"]) for o in orders)))
    if orders:
        db.request_checkout(sess["id"])

    resp = make_response(
        render_template(
            "receipt.html",
            mode="bill_request",
            table=db_table_label(sess["table_id"]),
            orders=[order_for_receipt(o) for o in orders],
            total=total,
            bill_id=str(sess["id"])[:8].upper(),
        )
    )
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


def require_staff():
    staff = current_staff()
    if not staff:
        return None, redirect(url_for("admin_login_form"))
    return staff, None


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
    staff, redirect_resp = require_staff()
    if redirect_resp:
        return redirect_resp
    orders = db.get_all_orders()
    tables = db.get_table_states()
    grand_total = sum(float(o["total"]) for o in orders)
    return render_template(
        "admin.html",
        orders=orders,
        tables=tables,
        grand_total=grand_total,
        staff=staff,
        status_labels=STATUS_LABELS,
    )


@app.post("/admin/sessions/<session_id>/paid")
def admin_mark_session_paid(session_id):
    _, redirect_resp = require_staff()
    if redirect_resp:
        return redirect_resp
    db.complete_session_payment(session_id)
    return redirect(url_for("admin"))


@app.post("/admin/orders/<order_id>/accept")
def admin_accept_order(order_id):
    staff, redirect_resp = require_staff()
    if redirect_resp:
        return redirect_resp
    db.update_order_status(order_id, db.ORDER_STATUS_PREPARING, changed_by=staff["email"])
    return redirect(url_for("admin"))


@app.post("/admin/orders/<order_id>/served")
def admin_mark_order_served(order_id):
    staff, redirect_resp = require_staff()
    if redirect_resp:
        return redirect_resp
    db.update_order_status(order_id, db.ORDER_STATUS_SERVED, changed_by=staff["email"])
    return redirect(url_for("admin"))


@app.post("/admin/tables/<table_id>/reset")
def admin_reset_table(table_id):
    _, redirect_resp = require_staff()
    if redirect_resp:
        return redirect_resp
    db.reset_table(table_id)
    return redirect(url_for("admin"))


@app.post("/admin/reset-data")
def admin_reset_data():
    staff, redirect_resp = require_staff()
    if redirect_resp:
        return redirect_resp
    if int(staff.get("role") or 0) < 1:
        return "権限がありません", 403
    db.reset_runtime_data()
    session.pop("cart", None)
    return redirect(url_for("admin"))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
