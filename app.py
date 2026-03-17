import hmac
import hashlib
import json
import logging
import os
import random
import smtplib
import sqlite3
import string
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from functools import wraps
from pathlib import Path

from flask import Flask, request, jsonify, abort, render_template_string, session, redirect, url_for

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change_me_in_production")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Cấu hình ─────────────────────────────────────────────────────────────────
DB_PATH        = Path(__file__).parent / "licenses.db"
GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_PASS     = os.environ.get("GMAIL_APP_PASS", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

PAYOS_CLIENT_ID = os.environ.get("PAYOS_CLIENT_ID", "")
PAYOS_API_KEY   = os.environ.get("PAYOS_API_KEY", "")
PAYOS_CHECKSUM  = os.environ.get("PAYOS_CHECKSUM", "")

# Giá bán (VND)
PRICE_30D  = 99_000
PRICE_90D  = 249_000
PRICE_365D = 799_000

PRODUCT_NAME = "Auto CapCut Video Sync"
SUPPORT_URL  = "https://t.me/vankhaidev"   # ← thay link hỗ trợ của bạn
SHOP_URL     = "https://your-shop.com"     # ← thay link shop của bạn


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS licenses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            key         TEXT    UNIQUE NOT NULL,
            email       TEXT    NOT NULL,
            machine_id  TEXT    DEFAULT NULL,
            days        INTEGER NOT NULL DEFAULT 30,
            created_at  TEXT    NOT NULL,
            activated_at TEXT   DEFAULT NULL,
            expire_date TEXT    NOT NULL,
            active      INTEGER NOT NULL DEFAULT 1,
            order_id    TEXT    DEFAULT NULL,
            notes       TEXT    DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS orders (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            order_code  TEXT    UNIQUE NOT NULL,
            email       TEXT    NOT NULL,
            days        INTEGER NOT NULL,
            amount      INTEGER NOT NULL,
            status      TEXT    NOT NULL DEFAULT 'pending',
            created_at  TEXT    NOT NULL,
            paid_at     TEXT    DEFAULT NULL,
            key_sent    TEXT    DEFAULT NULL
        );
        """)
    log.info("Database khởi tạo xong: %s", DB_PATH)


# ── FIX: Gọi init_db() khi module load (gunicorn không chạy __main__)
init_db()


# ── Tạo key ───────────────────────────────────────────────────────────────────
def _gen_key() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        raw   = "".join(random.choices(chars, k=16))
        key   = f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"
        with get_db() as conn:
            row = conn.execute("SELECT id FROM licenses WHERE key=?", (key,)).fetchone()
        if not row:
            return key


def _create_license(email: str, days: int, order_id: str = None, notes: str = "") -> str:
    key         = _gen_key()
    now         = datetime.now()
    # Thời hạn tính từ lúc kích hoạt, không phải lúc tạo key
    expire_date = (now + timedelta(days=days)).strftime("%Y-%m-%d")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO licenses (key, email, days, created_at, expire_date, order_id, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (key, email, days, now.strftime("%Y-%m-%d %H:%M:%S"), expire_date, order_id, notes),
        )
    log.info("Tạo key '%s' cho %s (%d ngày)", key, email, days)
    return key


# ── Gửi email ─────────────────────────────────────────────────────────────────
def _send_key_email(to_email: str, key: str, days: int) -> bool:
    """Gửi key qua Gmail SMTP."""
    if not GMAIL_USER or not GMAIL_PASS:
        log.warning("Chưa cấu hình Gmail — bỏ qua gửi email.")
        return False

    subject = f"🎬 License Key {PRODUCT_NAME} của bạn"
    html_body = f"""
    <div style="font-family:'Segoe UI',Arial,sans-serif;max-width:560px;margin:0 auto;background:#fff;">
        <div style="background:#007BFF;padding:28px 32px;">
            <h1 style="color:white;margin:0;font-size:22px;">🎬 {PRODUCT_NAME}</h1>
            <p style="color:rgba(255,255,255,0.85);margin:6px 0 0;">Cảm ơn bạn đã tin dùng!</p>
        </div>
        <div style="padding:32px;">
            <p style="color:#1C1E21;font-size:15px;">Xin chào,</p>
            <p style="color:#606770;">Đây là License Key của bạn:</p>

            <div style="background:#F0F2F5;border:2px dashed #007BFF;border-radius:4px;padding:20px;text-align:center;margin:20px 0;">
                <span style="font-family:Consolas,monospace;font-size:26px;font-weight:bold;
                             color:#007BFF;letter-spacing:4px;">{key}</span>
                <p style="color:#606770;font-size:13px;margin:8px 0 0;">
                    Thời hạn: <b>{days} ngày</b>
                </p>
            </div>

            <h3 style="color:#1C1E21;">Cách kích hoạt:</h3>
            <ol style="color:#606770;line-height:1.8;">
                <li>Mở phần mềm <b>{PRODUCT_NAME}</b></li>
                <li>Màn hình kích hoạt sẽ hiện ra</li>
                <li>Nhập key ở trên vào ô License Key</li>
                <li>Nhấn <b>Kích hoạt</b></li>
            </ol>

            <div style="background:#FFF3CD;border-left:4px solid #FFC107;padding:12px 16px;margin:20px 0;">
                <b>⚠️ Lưu ý quan trọng:</b><br>
                Key này chỉ dùng được cho <b>1 máy tính</b>. 
                Sau khi kích hoạt trên máy này, key sẽ bị khóa với máy đó.<br>
                Nếu cần chuyển sang máy khác, vui lòng liên hệ hỗ trợ.
            </div>

            <p style="color:#606770;">
                Hỗ trợ: <a href="{SUPPORT_URL}" style="color:#007BFF;">{SUPPORT_URL}</a>
            </p>
        </div>
        <div style="background:#F0F2F5;padding:16px 32px;text-align:center;">
            <p style="color:#8D949E;font-size:12px;margin:0;">
                © {datetime.now().year} {PRODUCT_NAME} — Tự động tạo bởi hệ thống
            </p>
        </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = to_email
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(GMAIL_USER, GMAIL_PASS)
            smtp.sendmail(GMAIL_USER, to_email, msg.as_string())
        log.info("Đã gửi key đến %s", to_email)
        return True
    except Exception as e:
        log.error("Gửi email thất bại: %s", e)
        return False


# ═════════════════════════════════════════════════════════════════════════════
#  CLIENT API (tool gọi lên server)
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/api/activate", methods=["POST"])
def api_activate():
    """Kích hoạt key lần đầu / xác thực."""
    data       = request.get_json(silent=True) or {}
    key        = (data.get("key") or "").strip().upper()
    machine_id = (data.get("machine_id") or "").strip()

    if not key or not machine_id:
        return jsonify({"status": "error", "msg": "Thiếu key hoặc machine_id"}), 400

    with get_db() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE key=?", (key,)).fetchone()
        if not row:
            return jsonify({"status": "invalid"})
        if not row["active"]:
            return jsonify({"status": "invalid"})

        # Kiểm tra hết hạn
        expire_dt = datetime.strptime(row["expire_date"], "%Y-%m-%d")
        days_left = (expire_dt - datetime.now()).days
        if days_left < 0:
            return jsonify({"status": "expired", "expire": row["expire_date"]})

        # Gắn machine_id (lần đầu kích hoạt)
        if row["machine_id"] is None:
            conn.execute(
                "UPDATE licenses SET machine_id=?, activated_at=? WHERE key=?",
                (machine_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key),
            )
            log.info("Kích hoạt key '%s' cho machine '%s'", key, machine_id)
        elif row["machine_id"] != machine_id:
            return jsonify({"status": "wrong_machine"})

    return jsonify({
        "status":    "ok",
        "expire":    row["expire_date"],
        "days_left": days_left,
    })


@app.route("/api/check", methods=["POST"])
def api_check():
    """Kiểm tra license đang hoạt động (ping mỗi lần mở tool)."""
    data       = request.get_json(silent=True) or {}
    key        = (data.get("key") or "").strip().upper()
    machine_id = (data.get("machine_id") or "").strip()

    with get_db() as conn:
        row = conn.execute("SELECT * FROM licenses WHERE key=?", (key,)).fetchone()
        if not row or not row["active"]:
            return jsonify({"status": "invalid"})
        if row["machine_id"] and row["machine_id"] != machine_id:
            return jsonify({"status": "wrong_machine"})

        expire_dt = datetime.strptime(row["expire_date"], "%Y-%m-%d")
        days_left = (expire_dt - datetime.now()).days
        if days_left < 0:
            return jsonify({"status": "expired", "expire": row["expire_date"]})

    return jsonify({
        "status":    "ok",
        "expire":    row["expire_date"],
        "days_left": days_left,
    })


# ═════════════════════════════════════════════════════════════════════════════
#  PAYOS PAYMENT
# ═════════════════════════════════════════════════════════════════════════════

def _payos_checksum(data: dict) -> str:
    """Tính checksum PayOS theo tài liệu chính thức."""
    # Sort by key, nối thành chuỗi key=value&key=value
    sorted_str = "&".join(f"{k}={v}" for k, v in sorted(data.items()))
    return hmac.new(
        PAYOS_CHECKSUM.encode("utf-8"),
        sorted_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@app.route("/payment/create", methods=["POST"])
def payment_create():
    """
    Tạo link thanh toán PayOS.
    Body JSON: { "email": "...", "days": 30|90|365 }
    """
    import requests as req_lib
    import time

    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    days  = int(data.get("days", 30))

    if not email or "@" not in email:
        return jsonify({"error": "Email không hợp lệ"}), 400
    if days not in (30, 90, 365):
        return jsonify({"error": "Gói không hợp lệ"}), 400

    amount_map = {30: PRICE_30D, 90: PRICE_90D, 365: PRICE_365D}
    amount     = amount_map[days]
    order_code = int(time.time() * 1000) % 9_999_999  # PayOS yêu cầu số nguyên

    # Lưu order vào DB
    with get_db() as conn:
        conn.execute(
            "INSERT INTO orders (order_code, email, days, amount, status, created_at) VALUES (?,?,?,?,?,?)",
            (str(order_code), email, days, amount, "pending",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    # Gọi PayOS API
    payload = {
        "orderCode":     order_code,
        "amount":        amount,
        "description":   f"Key {days}d",   # tối đa 25 ký tự
        "buyerEmail":    email,
        "returnUrl":     f"{request.host_url}payment/success",
        "cancelUrl":     f"{request.host_url}payment/cancel",
    }
    checksum          = _payos_checksum(payload)
    payload["signature"] = checksum

    try:
        resp = req_lib.post(
            "https://api-merchant.payos.vn/v2/payment-requests",
            json=payload,
            headers={
                "x-client-id":  PAYOS_CLIENT_ID,
                "x-api-key":    PAYOS_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        result = resp.json()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if result.get("code") == "00":
        link = result["data"]["checkoutUrl"]
        return jsonify({"checkout_url": link, "order_code": order_code})

    return jsonify({"error": result.get("desc", "PayOS error")}), 400


@app.route("/payment/webhook", methods=["POST"])
def payment_webhook():
    """
    PayOS gọi vào đây sau khi thanh toán thành công.
    Tự động tạo key + gửi email cho khách.
    """
    data = request.get_json(silent=True) or {}
    log.info("PayOS webhook: %s", json.dumps(data, ensure_ascii=False))

    # Xác minh checksum
    received_sig = data.get("signature", "")
    check_data   = {k: v for k, v in data.items() if k != "signature"}
    expected_sig = _payos_checksum(check_data)
    if not hmac.compare_digest(received_sig, expected_sig):
        log.warning("Webhook checksum không hợp lệ!")
        return jsonify({"error": "invalid signature"}), 400

    order_code = str(data.get("orderCode", ""))
    status_pay = data.get("status", "")

    if status_pay != "PAID":
        return jsonify({"ok": True})

    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE order_code=?", (order_code,)
        ).fetchone()

        if not order:
            log.warning("Không tìm thấy order: %s", order_code)
            return jsonify({"ok": True})

        if order["status"] == "paid":
            return jsonify({"ok": True})   # đã xử lý rồi

        # Tạo key
        key = _create_license(
            email=order["email"],
            days=order["days"],
            order_id=order_code,
            notes=f"PayOS order {order_code}",
        )

        # Cập nhật order
        conn.execute(
            "UPDATE orders SET status='paid', paid_at=?, key_sent=? WHERE order_code=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key, order_code),
        )

    # Gửi email
    _send_key_email(order["email"], key, order["days"])

    return jsonify({"ok": True})


@app.route("/payment/success")
def payment_success():
    order_code = request.args.get("orderCode", "")
    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE order_code=?", (order_code,)
        ).fetchone()
    if order and order["status"] == "paid":
        return f"""
        <html><body style="font-family:Arial;text-align:center;padding:60px;background:#F0F2F5;">
        <h1 style="color:#28A745;">✅ Thanh toán thành công!</h1>
        <p>Key License đã được gửi đến email <b>{order['email']}</b></p>
        <p style="color:#606770;">Vui lòng kiểm tra hộp thư (kể cả thư mục Spam)</p>
        <a href="{SUPPORT_URL}" style="color:#007BFF;">Liên hệ hỗ trợ</a>
        </body></html>
        """
    return """
    <html><body style="font-family:Arial;text-align:center;padding:60px;">
    <h2>Đang xử lý thanh toán...</h2>
    <p>Vui lòng chờ vài giây rồi kiểm tra email.</p>
    </body></html>
    """


@app.route("/payment/cancel")
def payment_cancel():
    return """
    <html><body style="font-family:Arial;text-align:center;padding:60px;background:#F0F2F5;">
    <h2 style="color:#FD7E14;">Thanh toán bị huỷ</h2>
    <p>Bạn đã huỷ thanh toán. Không có khoản tiền nào bị trừ.</p>
    </body></html>
    """


# ═════════════════════════════════════════════════════════════════════════════
#  TRANG BÁN HÀNG (đơn giản)
# ═════════════════════════════════════════════════════════════════════════════

SHOP_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Auto CapCut Video Sync — Tự động hoá quy trình edit video</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Plus+Jakarta+Sans:wght@400;500;600&display=swap" rel="stylesheet">
<style>
:root {
  --bg:        #080810;
  --surface:   #10101C;
  --card:      #16162A;
  --border:    rgba(255,255,255,0.07);
  --gold:      #F5A623;
  --gold-dim:  rgba(245,166,35,0.15);
  --text:      #EDEAF4;
  --muted:     #7A7898;
  --red:       #FF4757;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  font-family:'Plus Jakarta Sans',sans-serif;
  background:var(--bg);
  color:var(--text);
  line-height:1.6;
  overflow-x:hidden;
}

/* ── Scrollbar ── */
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:#2A2A44;border-radius:3px}

/* ── NAV ── */
nav{
  position:fixed;top:0;left:0;right:0;z-index:100;
  display:flex;align-items:center;justify-content:space-between;
  padding:18px 5%;
  background:rgba(8,8,16,0.85);
  backdrop-filter:blur(12px);
  border-bottom:1px solid var(--border);
}
.logo{
  font-family:'Bebas Neue',sans-serif;
  font-size:22px;letter-spacing:2px;
  color:var(--text);
  display:flex;align-items:center;gap:10px;
}
.logo-badge{
  background:var(--gold);
  color:#080810;
  font-family:'Plus Jakarta Sans',sans-serif;
  font-size:10px;font-weight:600;
  padding:2px 8px;border-radius:3px;
  letter-spacing:1px;
}
.nav-link{
  color:var(--muted);font-size:14px;text-decoration:none;
  transition:color .2s;
}
.nav-link:hover{color:var(--text)}
.nav-right{display:flex;align-items:center;gap:20px}

/* ── HERO ── */
.hero{
  min-height:100vh;
  display:flex;flex-direction:column;
  align-items:center;justify-content:center;
  text-align:center;
  padding:120px 20px 80px;
  position:relative;
  overflow:hidden;
}
.hero-glow{
  position:absolute;
  width:600px;height:600px;
  background:radial-gradient(circle, rgba(245,166,35,0.08) 0%, transparent 70%);
  top:50%;left:50%;transform:translate(-50%,-60%);
  pointer-events:none;
}
.hero-glow2{
  position:absolute;
  width:400px;height:400px;
  background:radial-gradient(circle, rgba(100,80,255,0.06) 0%, transparent 70%);
  bottom:10%;right:5%;
  pointer-events:none;
}
.hero-tag{
  display:inline-flex;align-items:center;gap:8px;
  border:1px solid rgba(245,166,35,0.3);
  background:rgba(245,166,35,0.07);
  padding:6px 16px;border-radius:100px;
  font-size:13px;color:var(--gold);
  margin-bottom:28px;
  animation:fadeUp .8s ease both;
}
.hero-tag-dot{
  width:6px;height:6px;border-radius:50%;
  background:var(--gold);
  animation:pulse 2s ease infinite;
}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.5;transform:scale(.8)}}
h1{
  font-family:'Bebas Neue',sans-serif;
  font-size:clamp(56px,9vw,110px);
  line-height:1;letter-spacing:2px;
  animation:fadeUp .8s .1s ease both;
}
h1 em{
  font-style:normal;
  color:var(--gold);
  position:relative;
}
.hero-sub{
  max-width:540px;
  font-size:17px;color:var(--muted);
  margin:24px auto 40px;
  animation:fadeUp .8s .2s ease both;
}
.hero-cta{
  display:inline-flex;align-items:center;gap:10px;
  background:var(--gold);
  color:#080810;
  font-weight:600;font-size:16px;
  padding:16px 40px;border-radius:6px;
  border:none;cursor:pointer;
  text-decoration:none;
  transition:transform .2s,box-shadow .2s;
  animation:fadeUp .8s .3s ease both;
}
.hero-cta:hover{transform:translateY(-2px);box-shadow:0 12px 40px rgba(245,166,35,0.35)}
.hero-stats{
  display:flex;gap:48px;margin-top:64px;
  animation:fadeUp .8s .4s ease both;
}
.hero-stat-num{
  font-family:'Bebas Neue',sans-serif;
  font-size:38px;color:var(--text);
  letter-spacing:1px;
}
.hero-stat-label{font-size:13px;color:var(--muted)}

/* ── FEATURES ── */
.section{padding:80px 5%}
.section-label{
  font-size:12px;font-weight:600;letter-spacing:3px;
  color:var(--gold);text-transform:uppercase;
  margin-bottom:12px;
}
.section-title{
  font-family:'Bebas Neue',sans-serif;
  font-size:clamp(36px,5vw,56px);
  letter-spacing:1px;line-height:1.1;
  margin-bottom:16px;
}
.section-sub{font-size:16px;color:var(--muted);max-width:500px;line-height:1.7}
.features-grid{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
  gap:16px;margin-top:48px;
}
.feature-card{
  background:var(--card);
  border:1px solid var(--border);
  border-radius:12px;
  padding:28px;
  transition:border-color .2s,transform .2s;
  animation:fadeUp .6s ease both;
}
.feature-card:hover{
  border-color:rgba(245,166,35,0.25);
  transform:translateY(-3px);
}
.feature-icon{
  width:44px;height:44px;border-radius:10px;
  background:var(--gold-dim);
  display:flex;align-items:center;justify-content:center;
  font-size:22px;margin-bottom:18px;
}
.feature-title{font-size:16px;font-weight:600;margin-bottom:8px}
.feature-desc{font-size:14px;color:var(--muted);line-height:1.7}

/* ── PRICING ── */
.pricing-wrap{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
  gap:16px;margin-top:48px;max-width:960px;margin-left:auto;margin-right:auto;
}
.plan-card{
  background:var(--card);
  border:1px solid var(--border);
  border-radius:16px;
  padding:32px;
  position:relative;
  transition:transform .2s,border-color .2s;
}
.plan-card:hover{transform:translateY(-4px)}
.plan-card.popular{
  border-color:var(--gold);
  background:linear-gradient(160deg, #1C1A2E 0%, #16162A 60%);
}
.popular-badge{
  position:absolute;top:-13px;left:50%;transform:translateX(-50%);
  background:var(--gold);
  color:#080810;
  font-size:11px;font-weight:700;letter-spacing:2px;
  padding:4px 20px;border-radius:100px;
  white-space:nowrap;
  text-transform:uppercase;
}
.plan-name{font-size:14px;font-weight:600;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:12px}
.plan-price{
  font-family:'Bebas Neue',sans-serif;
  font-size:60px;letter-spacing:1px;color:var(--text);
  line-height:1;
}
.plan-price span{font-family:'Plus Jakarta Sans',sans-serif;font-size:18px;color:var(--muted);vertical-align:middle;margin-left:4px}
.plan-period{font-size:13px;color:var(--muted);margin-top:4px;margin-bottom:24px}
.plan-divider{height:1px;background:var(--border);margin:24px 0}
.plan-features{list-style:none;margin-bottom:32px}
.plan-features li{
  font-size:14px;color:var(--muted);
  padding:7px 0;
  display:flex;align-items:center;gap:10px;
}
.plan-features li::before{
  content:"";
  width:16px;height:16px;border-radius:50%;flex-shrink:0;
  background:var(--gold-dim);
  border:1px solid rgba(245,166,35,.4);
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'%3E%3Cpath d='M2 5l2 2 4-4' stroke='%23F5A623' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
  background-size:10px;background-position:center;background-repeat:no-repeat;
}
.plan-btn{
  width:100%;padding:14px;
  border-radius:8px;
  font-family:'Plus Jakarta Sans',sans-serif;
  font-size:15px;font-weight:600;
  cursor:pointer;border:none;
  transition:transform .15s,box-shadow .15s;
}
.plan-btn:hover{transform:translateY(-2px)}
.plan-btn.default{
  background:rgba(255,255,255,0.06);
  color:var(--text);border:1px solid var(--border);
}
.plan-btn.default:hover{background:rgba(255,255,255,0.1)}
.plan-btn.primary{
  background:var(--gold);color:#080810;
  box-shadow:0 8px 24px rgba(245,166,35,0.25);
}
.plan-btn.primary:hover{box-shadow:0 12px 32px rgba(245,166,35,0.4)}
.save-tag{
  display:inline-block;
  background:rgba(245,166,35,0.12);
  color:var(--gold);
  font-size:12px;font-weight:600;
  padding:3px 10px;border-radius:100px;
  margin-left:8px;
}

/* ── HOW IT WORKS ── */
.steps{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
  gap:0;margin-top:48px;
  position:relative;
}
.steps::before{
  content:"";
  position:absolute;top:28px;left:10%;right:10%;height:1px;
  background:linear-gradient(90deg, transparent, var(--border) 20%, var(--border) 80%, transparent);
}
.step{text-align:center;padding:0 20px;position:relative}
.step-num{
  width:56px;height:56px;border-radius:50%;
  background:var(--card);border:1px solid var(--border);
  display:flex;align-items:center;justify-content:center;
  font-family:'Bebas Neue',sans-serif;font-size:22px;color:var(--gold);
  margin:0 auto 20px;position:relative;z-index:1;
}
.step-title{font-size:15px;font-weight:600;margin-bottom:8px}
.step-desc{font-size:13px;color:var(--muted);line-height:1.6}

/* ── FAQ ── */
.faq-list{margin-top:40px;max-width:700px}
.faq-item{
  border-bottom:1px solid var(--border);
  overflow:hidden;
}
.faq-q{
  width:100%;background:none;border:none;
  color:var(--text);font-family:'Plus Jakarta Sans',sans-serif;
  font-size:15px;font-weight:500;
  padding:20px 0;text-align:left;cursor:pointer;
  display:flex;justify-content:space-between;align-items:center;
  gap:12px;
}
.faq-icon{
  width:20px;height:20px;border-radius:50%;
  border:1px solid var(--border);flex-shrink:0;
  display:flex;align-items:center;justify-content:center;
  font-size:14px;color:var(--muted);
  transition:transform .2s;
}
.faq-item.open .faq-icon{transform:rotate(45deg);border-color:var(--gold);color:var(--gold)}
.faq-a{
  font-size:14px;color:var(--muted);line-height:1.7;
  max-height:0;overflow:hidden;transition:max-height .3s ease, padding .3s;
  padding-bottom:0;
}
.faq-item.open .faq-a{max-height:200px;padding-bottom:20px}

/* ── FOOTER ── */
footer{
  border-top:1px solid var(--border);
  padding:40px 5%;
  display:flex;align-items:center;justify-content:space-between;
  flex-wrap:wrap;gap:16px;
}
.footer-copy{font-size:13px;color:var(--muted)}
.footer-links{display:flex;gap:20px}
.footer-links a{font-size:13px;color:var(--muted);text-decoration:none;transition:color .2s}
.footer-links a:hover{color:var(--text)}

/* ── MODAL ── */
.modal-overlay{
  display:none;position:fixed;inset:0;z-index:200;
  background:rgba(0,0,0,0.75);backdrop-filter:blur(4px);
  align-items:center;justify-content:center;padding:20px;
}
.modal-overlay.show{display:flex}
.modal{
  background:var(--card);
  border:1px solid rgba(255,255,255,0.1);
  border-radius:20px;
  padding:40px;width:100%;max-width:440px;
  animation:modalIn .25s ease;
}
@keyframes modalIn{from{transform:scale(.95) translateY(10px);opacity:0}to{transform:none;opacity:1}}
.modal-title{font-family:'Bebas Neue',sans-serif;font-size:30px;letter-spacing:1px;margin-bottom:4px}
.modal-sub{font-size:14px;color:var(--muted);margin-bottom:28px}
.modal label{display:block;font-size:12px;font-weight:600;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
.modal input{
  width:100%;padding:13px 16px;
  background:rgba(255,255,255,0.04);
  border:1px solid var(--border);border-radius:8px;
  color:var(--text);font-family:'Plus Jakarta Sans',sans-serif;font-size:15px;
  margin-bottom:16px;outline:none;
  transition:border-color .2s;
}
.modal input:focus{border-color:rgba(245,166,35,0.5)}
.modal input::placeholder{color:var(--muted)}
.modal-note{font-size:12px;color:var(--muted);line-height:1.6;margin-bottom:24px;padding:12px 14px;background:rgba(255,255,255,0.03);border-radius:8px;border-left:2px solid var(--gold)}
.btn-pay{
  width:100%;padding:16px;
  background:var(--gold);color:#080810;
  border:none;border-radius:8px;
  font-family:'Plus Jakarta Sans',sans-serif;font-size:16px;font-weight:700;
  cursor:pointer;transition:transform .15s,box-shadow .15s;
  display:flex;align-items:center;justify-content:center;gap:8px;
}
.btn-pay:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(245,166,35,0.35)}
.btn-pay:active{transform:scale(.98)}
.btn-cancel{
  width:100%;padding:12px;margin-top:10px;
  background:none;border:none;color:var(--muted);
  font-family:'Plus Jakarta Sans',sans-serif;font-size:14px;
  cursor:pointer;transition:color .2s;
}
.btn-cancel:hover{color:var(--text)}
#modal-msg{
  margin-top:12px;text-align:center;font-size:14px;font-weight:600;min-height:20px;
}
#modal-msg.err{color:var(--red)}
#modal-msg.ok{color:#4ade80}

/* ── ANIMATIONS ── */
@keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:none}}

/* ── RESPONSIVE ── */
@media(max-width:600px){
  .hero-stats{gap:28px}
  .steps::before{display:none}
  footer{flex-direction:column;text-align:center}
  .modal{padding:28px 20px}
}
</style>
</head>
<body>

<!-- NAV -->
<nav>
  <div class="logo">
    🎬 AutoCapCut
    <span class="logo-badge">v2.1</span>
  </div>
  <div class="nav-right">
    <a href="#pricing" class="nav-link">Bảng giá</a>
    <a href="https://t.me/vankhaidev" target="_blank" class="nav-link">Hỗ trợ</a>
  </div>
</nav>

<!-- HERO -->
<section class="hero">
  <div class="hero-glow"></div>
  <div class="hero-glow2"></div>
  <div class="hero-tag">
    <span class="hero-tag-dot"></span>
    Phiên bản 2.1 — Hỗ trợ Compound Clip
  </div>
  <h1>EDIT VIDEO<br><em>TỰ ĐỘNG HÓA</em></h1>
  <p class="hero-sub">
    Tự động ghép video, audio và subtitle vào CapCut Draft chỉ trong vài phút.
    Không cần server, không cần code.
  </p>
  <a href="#pricing" class="hero-cta">
    Mua License ngay
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M3 8h10M9 4l4 4-4 4" stroke="#080810" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
  </a>
  <div class="hero-stats">
    <div>
      <div class="hero-stat-num">5X</div>
      <div class="hero-stat-label">Nhanh hơn edit tay</div>
    </div>
    <div>
      <div class="hero-stat-num">1080P</div>
      <div class="hero-stat-label">Full HD output</div>
    </div>
    <div>
      <div class="hero-stat-num">SRT</div>
      <div class="hero-stat-label">Auto subtitle</div>
    </div>
  </div>
</section>

<!-- FEATURES -->
<section class="section" id="features">
  <div class="section-label">Tính năng</div>
  <div class="section-title">MỌI THỨ BẠN CẦN<br>ĐỂ EDIT NHANH HƠN</div>
  <p class="section-sub">Từ video gốc đến CapCut Draft hoàn chỉnh — tất cả tự động.</p>
  <div class="features-grid">
    <div class="feature-card" style="animation-delay:.0s">
      <div class="feature-icon">🎞️</div>
      <div class="feature-title">Auto-cắt clip theo SRT</div>
      <div class="feature-desc">Đọc file subtitle .srt, tự động cắt video đúng thời điểm, không cần kéo tay từng đoạn.</div>
    </div>
    <div class="feature-card" style="animation-delay:.1s">
      <div class="feature-icon">🔊</div>
      <div class="feature-title">Ghép audio thông minh</div>
      <div class="feature-desc">Điều chỉnh tốc độ video theo độ dài audio. Không bị lệch tiếng, không cần render lại.</div>
    </div>
    <div class="feature-card" style="animation-delay:.2s">
      <div class="feature-icon">⚡</div>
      <div class="feature-title">Compound Clip tự động</div>
      <div class="feature-desc">Gộp tất cả clip thành Compound Clip chỉ với một tham số. Video/Audio/Mixed đều hỗ trợ.</div>
    </div>
    <div class="feature-card" style="animation-delay:.3s">
      <div class="feature-icon">📱</div>
      <div class="feature-title">Ghi thẳng vào CapCut</div>
      <div class="feature-desc">Không cần server, không cần API. Draft xuất hiện ngay trong CapCut Projects của bạn.</div>
    </div>
  </div>
</section>

<!-- HOW IT WORKS -->
<section class="section" style="padding-top:0">
  <div class="section-label">Quy trình</div>
  <div class="section-title">CHỈ 3 BƯỚC<br>ĐỂ CÓ DRAFT HOÀN CHỈNH</div>
  <div class="steps">
    <div class="step">
      <div class="step-num">1</div>
      <div class="step-title">Chuẩn bị file</div>
      <div class="step-desc">Đặt video gốc, audio từng đoạn, và file subtitle .srt vào thư mục inputs</div>
    </div>
    <div class="step">
      <div class="step-num">2</div>
      <div class="step-title">Chạy lệnh</div>
      <div class="step-desc">Chạy <code style="background:rgba(255,255,255,.06);padding:2px 6px;border-radius:4px;font-size:12px">python main.py</code> và chờ vài giây</div>
    </div>
    <div class="step">
      <div class="step-num">3</div>
      <div class="step-title">Mở CapCut</div>
      <div class="step-desc">Draft hoàn chỉnh đã có trong CapCut Projects — chỉnh sửa thêm hoặc xuất ngay</div>
    </div>
  </div>
</section>

<!-- PRICING -->
<section class="section" id="pricing" style="padding-top:0">
  <div style="text-align:center">
    <div class="section-label" style="text-align:center">Bảng giá</div>
    <div class="section-title">CHỌN GÓI PHÙ HỢP</div>
    <p style="color:var(--muted);margin-top:8px">Một lần mua, dùng trên 1 máy tính. Không tính phí ẩn.</p>
  </div>
  <div class="pricing-wrap">

    <!-- 30 days -->
    <div class="plan-card">
      <div class="plan-name">Starter</div>
      <div class="plan-price">99K <span>VND</span></div>
      <div class="plan-period">Dùng 30 ngày</div>
      <div class="plan-divider"></div>
      <ul class="plan-features">
        <li>Dùng trên 1 máy tính</li>
        <li>Cập nhật miễn phí</li>
        <li>Hỗ trợ qua Telegram</li>
      </ul>
      <button class="plan-btn default" onclick="openModal(30, 99000)">Mua gói 30 ngày</button>
    </div>

    <!-- 90 days POPULAR -->
    <div class="plan-card popular">
      <div class="popular-badge">Phổ biến nhất</div>
      <div class="plan-name" style="color:var(--gold)">Creator</div>
      <div class="plan-price">249K <span>VND</span></div>
      <div class="plan-period">Dùng 90 ngày <span class="save-tag">Tiết kiệm 48K</span></div>
      <div class="plan-divider" style="background:rgba(245,166,35,0.15)"></div>
      <ul class="plan-features">
        <li>Dùng trên 1 máy tính</li>
        <li>Cập nhật miễn phí</li>
        <li>Hỗ trợ qua Telegram</li>
        <li>Ưu tiên hỗ trợ kỹ thuật</li>
      </ul>
      <button class="plan-btn primary" onclick="openModal(90, 249000)">Mua gói 90 ngày</button>
    </div>

    <!-- 365 days -->
    <div class="plan-card">
      <div class="plan-name">Pro</div>
      <div class="plan-price">799K <span>VND</span></div>
      <div class="plan-period">Dùng 365 ngày <span class="save-tag">Tiết kiệm 389K</span></div>
      <div class="plan-divider"></div>
      <ul class="plan-features">
        <li>Dùng trên 1 máy tính</li>
        <li>Cập nhật miễn phí</li>
        <li>Hỗ trợ qua Telegram</li>
        <li>Ưu tiên hỗ trợ kỹ thuật</li>
        <li>Truy cập tính năng beta</li>
      </ul>
      <button class="plan-btn default" onclick="openModal(365, 799000)">Mua gói 365 ngày</button>
    </div>

  </div>
</section>

<!-- FAQ -->
<section class="section" style="padding-top:0">
  <div class="section-label">FAQ</div>
  <div class="section-title">CÂU HỎI<br>THƯỜNG GẶP</div>
  <div class="faq-list">
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Phần mềm chạy trên hệ điều hành nào?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">Hiện tại hỗ trợ Windows 10/11 (64-bit), cần cài sẵn CapCut PC và FFmpeg. MacOS đang trong quá trình phát triển.</div>
    </div>
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Sau khi mua key được gửi về đâu?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">Key License sẽ được gửi tự động đến email bạn nhập khi thanh toán, thường trong vòng 1–2 phút sau khi thanh toán thành công. Nếu không thấy, hãy kiểm tra thư mục Spam.</div>
    </div>
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Tôi có thể dùng trên nhiều máy không?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">Mỗi license chỉ dùng được trên 1 máy. Nếu cần chuyển sang máy khác (thay máy, cài lại Windows), vui lòng liên hệ hỗ trợ qua Telegram.</div>
    </div>
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Có hỗ trợ hoàn tiền không?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">Chúng tôi hỗ trợ hoàn tiền trong 24 giờ đầu nếu phần mềm không chạy được trên máy bạn sau khi đã được hỗ trợ kỹ thuật. Liên hệ qua Telegram để được xử lý.</div>
    </div>
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Cần chuẩn bị gì trước khi dùng?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">Bạn cần: (1) CapCut PC đã cài và đã mở ít nhất 1 lần, (2) FFmpeg đã thêm vào PATH, (3) Python 3.10+, (4) Video gốc + file audio từng đoạn + file .srt. Hướng dẫn cài đặt chi tiết có trong file README sau khi mua.</div>
    </div>
  </div>
</section>

<!-- FOOTER -->
<footer>
  <div class="footer-copy">© 2026 Auto CapCut Video Sync — All rights reserved</div>
  <div class="footer-links">
    <a href="https://t.me/vankhaidev" target="_blank">Telegram hỗ trợ</a>
    <a href="#pricing">Bảng giá</a>
  </div>
</footer>

<!-- MODAL -->
<div class="modal-overlay" id="overlay">
  <div class="modal">
    <div class="modal-title" id="modal-title">MUA GÓI 90 NGÀY</div>
    <div class="modal-sub" id="modal-sub">249,000đ — thanh toán qua PayOS</div>
    <label>Email nhận key</label>
    <input type="email" id="email" placeholder="example@gmail.com" autocomplete="email">
    <label>Machine ID <span style="font-weight:400;text-transform:none;letter-spacing:0">(tuỳ chọn)</span></label>
    <input type="text" id="machine_id" placeholder="Lấy trong phần mềm khi mở lần đầu" style="font-family:monospace">
    <div class="modal-note">
      🔐 Key chỉ kích hoạt được trên <strong>1 máy tính</strong>. Machine ID có thể nhập sau khi nhận key nếu bạn chưa cài phần mềm.
    </div>
    <button class="btn-pay" id="pay-btn" onclick="submitPayment()">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none"><rect x="2" y="5" width="20" height="14" rx="2" stroke="#080810" stroke-width="2"/><path d="M2 10h20" stroke="#080810" stroke-width="2"/></svg>
      Thanh toán ngay
    </button>
    <button class="btn-cancel" onclick="closeModal()">Huỷ</button>
    <div id="modal-msg"></div>
  </div>
</div>

<script>
let _days = 90;

function openModal(days, price) {
  _days = days;
  const labels = {30:'STARTER — 30 NGÀY', 90:'CREATOR — 90 NGÀY', 365:'PRO — 365 NGÀY'};
  document.getElementById('modal-title').textContent = labels[days];
  document.getElementById('modal-sub').textContent =
    price.toLocaleString('vi') + 'đ — thanh toán qua PayOS';
  document.getElementById('modal-msg').textContent = '';
  document.getElementById('modal-msg').className = '';
  document.getElementById('email').value = '';
  document.getElementById('machine_id').value = '';
  document.getElementById('overlay').classList.add('show');
}
function closeModal() {
  document.getElementById('overlay').classList.remove('show');
}
document.getElementById('overlay').addEventListener('click', function(e){
  if(e.target === this) closeModal();
});

async function submitPayment() {
  const email = document.getElementById('email').value.trim();
  const msgEl = document.getElementById('modal-msg');
  const btn   = document.getElementById('pay-btn');
  if (!email || !email.includes('@')) {
    msgEl.className = 'err';
    msgEl.textContent = '⚠ Vui lòng nhập email hợp lệ';
    return;
  }
  btn.disabled = true;
  btn.textContent = '⏳ Đang tạo link thanh toán...';
  msgEl.textContent = '';
  try {
    const resp = await fetch('/payment/create', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({email, days: _days})
    });
    const data = await resp.json();
    if (data.checkout_url) {
      msgEl.className = 'ok';
      msgEl.textContent = '✓ Đang chuyển hướng...';
      window.location.href = data.checkout_url;
    } else {
      msgEl.className = 'err';
      msgEl.textContent = '✕ ' + (data.error || 'Lỗi tạo thanh toán');
      btn.disabled = false;
      btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><rect x="2" y="5" width="20" height="14" rx="2" stroke="#080810" stroke-width="2"/><path d="M2 10h20" stroke="#080810" stroke-width="2"/></svg> Thanh toán ngay';
    }
  } catch(e) {
    msgEl.className = 'err';
    msgEl.textContent = '✕ Lỗi kết nối server';
    btn.disabled = false;
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none"><rect x="2" y="5" width="20" height="14" rx="2" stroke="#080810" stroke-width="2"/><path d="M2 10h20" stroke="#080810" stroke-width="2"/></svg> Thanh toán ngay';
  }
}

function toggleFaq(btn) {
  const item = btn.closest('.faq-item');
  const isOpen = item.classList.contains('open');
  document.querySelectorAll('.faq-item.open').forEach(el => el.classList.remove('open'));
  if (!isOpen) item.classList.add('open');
}

// Scroll reveal
const obs = new IntersectionObserver(entries => {
  entries.forEach(e => { if(e.isIntersecting) e.target.style.opacity = 1; });
}, {threshold: 0.1});
document.querySelectorAll('.feature-card').forEach(el => {
  el.style.opacity = '0'; obs.observe(el);
});
</script>
</body>
</html>

"""

@app.route("/")
def shop_page():
    return SHOP_HTML


# ═════════════════════════════════════════════════════════════════════════════
#  ADMIN PANEL
# ═════════════════════════════════════════════════════════════════════════════

def _require_admin(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return redirect("/admin/login")
        return f(*args, **kwargs)
    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect("/admin")
    return """
    <html><body style="font-family:Arial;display:flex;justify-content:center;padding:80px;background:#F0F2F5;">
    <form method="POST" style="background:white;padding:40px;border:1px solid #CCD0D5;width:300px;">
    <h2 style="margin-bottom:20px;">Admin Login</h2>
    <input name="password" type="password" placeholder="Mật khẩu" 
           style="width:100%;padding:10px;margin-bottom:12px;border:1px solid #CCD0D5;">
    <button type="submit" style="width:100%;padding:10px;background:#007BFF;color:white;border:none;font-weight:bold;">
    Đăng nhập</button>
    </form></body></html>
    """


@app.route("/admin")
@_require_admin
def admin_dashboard():
    with get_db() as conn:
        licenses = conn.execute(
            "SELECT * FROM licenses ORDER BY created_at DESC LIMIT 100"
        ).fetchall()
        orders   = conn.execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        total    = conn.execute("SELECT COUNT(*) FROM licenses WHERE active=1").fetchone()[0]
        expired  = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE expire_date < date('now')"
        ).fetchone()[0]

    rows = "".join(
        f"<tr>"
        f"<td>{r['key']}</td>"
        f"<td>{r['email']}</td>"
        f"<td style='font-size:11px'>{r['machine_id'] or '—'}</td>"
        f"<td>{r['days']}d</td>"
        f"<td>{r['expire_date']}</td>"
        f"<td>{'✅' if r['active'] else '❌'}</td>"
        f"<td><a href='/admin/revoke/{r['key']}' onclick=\"return confirm('Revoke?')\">Revoke</a></td>"
        f"</tr>"
        for r in licenses
    )

    return f"""
    <html><head><title>Admin</title>
    <style>body{{font-family:Arial;background:#F0F2F5;}}
    table{{width:100%;border-collapse:collapse;background:white;}}
    th,td{{padding:8px 12px;border:1px solid #CCD0D5;font-size:13px;text-align:left;}}
    th{{background:#007BFF;color:white;}}
    tr:hover{{background:#F0F2F5;}}
    .stats{{display:flex;gap:16px;margin:20px 0;}}
    .stat{{background:white;border:1px solid #CCD0D5;padding:20px 28px;}}
    </style></head>
    <body style="padding:24px">
    <h2>🎬 Admin Panel — {PRODUCT_NAME}</h2>
    <div class="stats">
      <div class="stat"><b>{total}</b><br>Key active</div>
      <div class="stat"><b style="color:#DC3545">{expired}</b><br>Key hết hạn</div>
      <div class="stat"><b>{len(orders)}</b><br>Orders gần đây</div>
    </div>
    <h3>Tạo key thủ công</h3>
    <form action="/admin/create_key" method="POST" style="background:white;padding:16px;border:1px solid #CCD0D5;margin-bottom:20px;">
      <input name="email" placeholder="Email" required style="padding:8px;width:220px;border:1px solid #CCD0D5;">
      <select name="days" style="padding:8px;border:1px solid #CCD0D5;">
        <option value="30">30 ngày</option>
        <option value="90">90 ngày</option>
        <option value="365">365 ngày</option>
      </select>
      <input name="notes" placeholder="Ghi chú" style="padding:8px;width:200px;border:1px solid #CCD0D5;">
      <button type="submit" style="padding:8px 20px;background:#28A745;color:white;border:none;font-weight:bold;">
      Tạo & Gửi Email</button>
    </form>
    <h3>Danh sách License ({len(licenses)} gần nhất)</h3>
    <table><tr><th>Key</th><th>Email</th><th>Machine ID</th><th>Thời hạn</th>
    <th>Hết hạn</th><th>Active</th><th>Action</th></tr>{rows}</table>
    </body></html>
    """


@app.route("/admin/create_key", methods=["POST"])
@_require_admin
def admin_create_key():
    email = request.form.get("email", "").strip()
    days  = int(request.form.get("days", 30))
    notes = request.form.get("notes", "")
    if not email:
        return "Thiếu email", 400
    key = _create_license(email, days, notes=notes)
    _send_key_email(email, key, days)
    return redirect("/admin")


@app.route("/admin/revoke/<key>")
@_require_admin
def admin_revoke(key):
    with get_db() as conn:
        conn.execute("UPDATE licenses SET active=0 WHERE key=?", (key,))
    return redirect("/admin")


# ── Khởi động ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    log.info("Server khởi động tại port %d", port)
    app.run(host="0.0.0.0", port=port, debug=False)
