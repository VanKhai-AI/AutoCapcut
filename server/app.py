"""
server/app.py — License Server cho Auto CapCut Video Sync
Stack: Flask + SQLite + PayOS (VN) + Gmail SMTP
Deploy: Railway.app hoặc Render.com (miễn phí)

Cài đặt:
    pip install flask payos requests

Biến môi trường cần set trên Railway/Render:
    SECRET_KEY        — Khóa bí mật Flask
    GMAIL_USER        — Gmail dùng để gửi key
    GMAIL_APP_PASS    — App Password của Gmail (https://myaccount.google.com/apppasswords)
    PAYOS_CLIENT_ID   — Lấy từ dashboard.payos.vn
    PAYOS_API_KEY     — Lấy từ dashboard.payos.vn
    PAYOS_CHECKSUM    — Lấy từ dashboard.payos.vn
    ADMIN_PASSWORD    — Mật khẩu vào trang /admin
"""

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
<title>Mua License — Auto CapCut Video Sync</title>
<style>
  * { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:'Segoe UI',Arial,sans-serif; background:#F0F2F5; color:#1C1E21; }
  .hero { background:#007BFF; color:white; padding:60px 20px; text-align:center; }
  .hero h1 { font-size:32px; margin-bottom:12px; }
  .hero p  { font-size:16px; opacity:.85; }
  .plans { display:flex; gap:24px; justify-content:center; flex-wrap:wrap; padding:48px 20px; max-width:900px; margin:0 auto; }
  .plan  { background:white; border:1px solid #CCD0D5; border-radius:4px; padding:32px 24px; width:240px; text-align:center; }
  .plan.popular { border:2px solid #007BFF; position:relative; }
  .plan.popular::before { content:"Phổ biến nhất"; background:#007BFF; color:white; font-size:12px;
     font-weight:bold; padding:4px 12px; position:absolute; top:-14px; left:50%; transform:translateX(-50%); }
  .plan h2 { font-size:18px; margin-bottom:8px; }
  .plan .price { font-size:32px; font-weight:bold; color:#007BFF; margin:12px 0; }
  .plan .price span { font-size:14px; color:#606770; }
  .plan ul { list-style:none; text-align:left; margin:16px 0; color:#606770; font-size:14px; line-height:2; }
  .plan ul li::before { content:"✓  "; color:#28A745; }
  .plan button { background:#007BFF; color:white; border:none; padding:12px 24px; font-size:15px;
    font-weight:bold; cursor:pointer; width:100%; margin-top:12px; }
  .plan button:hover { background:#0056B3; }
  .form-overlay { display:none; position:fixed; inset:0; background:rgba(0,0,0,.5); z-index:100; align-items:center; justify-content:center; }
  .form-overlay.show { display:flex; }
  .form-box { background:white; padding:32px; width:380px; border-radius:4px; }
  .form-box h3 { margin-bottom:16px; }
  .form-box input { width:100%; padding:10px; border:1px solid #CCD0D5; font-size:14px; margin-bottom:12px; }
  .form-box button { width:100%; background:#007BFF; color:white; border:none; padding:12px; font-size:15px; font-weight:bold; cursor:pointer; }
  .form-box .cancel { background:#E4E6EB; color:#1C1E21; margin-top:8px; }
  #msg { margin-top:12px; text-align:center; font-weight:bold; }
</style>
</head>
<body>
<div class="hero">
  <h1>🎬 Auto CapCut Video Sync</h1>
  <p>Tự động tạo draft CapCut từ video + audio + SRT — tiết kiệm hàng giờ edit</p>
</div>

<div class="plans">
  <div class="plan">
    <h2>30 Ngày</h2>
    <div class="price">99K <span>VND</span></div>
    <ul>
      <li>Dùng trên 1 máy</li>
      <li>Cập nhật miễn phí</li>
      <li>Hỗ trợ Telegram</li>
    </ul>
    <button onclick="openForm(30, 99000)">Mua ngay</button>
  </div>

  <div class="plan popular">
    <h2>90 Ngày</h2>
    <div class="price">249K <span>VND</span></div>
    <ul>
      <li>Dùng trên 1 máy</li>
      <li>Cập nhật miễn phí</li>
      <li>Hỗ trợ Telegram</li>
      <li>Tiết kiệm 48K</li>
    </ul>
    <button onclick="openForm(90, 249000)">Mua ngay</button>
  </div>

  <div class="plan">
    <h2>365 Ngày</h2>
    <div class="price">799K <span>VND</span></div>
    <ul>
      <li>Dùng trên 1 máy</li>
      <li>Cập nhật miễn phí</li>
      <li>Hỗ trợ Telegram</li>
      <li>Tiết kiệm 389K</li>
    </ul>
    <button onclick="openForm(365, 799000)">Mua ngay</button>
  </div>
</div>

<div class="form-overlay" id="overlay">
  <div class="form-box">
    <h3 id="form-title">Nhập thông tin</h3>
    <input type="email" id="email" placeholder="Email của bạn (để nhận key)" required>
    <input type="text" id="machine_id" placeholder="Machine ID (tuỳ chọn)" style="font-family:monospace">
    <p style="font-size:12px;color:#606770;margin-bottom:12px;">
      Machine ID lấy trong phần mềm khi mở lần đầu.<br>
      Nếu chưa có thể bỏ trống, nhập sau khi nhận key.
    </p>
    <button onclick="submitPayment()">💳 Thanh toán ngay</button>
    <button class="cancel" onclick="closeForm()">Huỷ</button>
    <div id="msg"></div>
  </div>
</div>

<script>
let selectedDays = 30;
function openForm(days, price) {
  selectedDays = days;
  document.getElementById('form-title').textContent = `Mua gói ${days} ngày — ${price.toLocaleString('vi')}đ`;
  document.getElementById('overlay').classList.add('show');
}
function closeForm() { document.getElementById('overlay').classList.remove('show'); }
async function submitPayment() {
  const email = document.getElementById('email').value.trim();
  if (!email || !email.includes('@')) { document.getElementById('msg').textContent = '⚠️ Vui lòng nhập email hợp lệ'; return; }
  document.getElementById('msg').textContent = '⏳ Đang tạo link thanh toán...';
  const resp = await fetch('/payment/create', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({email, days: selectedDays})
  });
  const data = await resp.json();
  if (data.checkout_url) { window.location.href = data.checkout_url; }
  else { document.getElementById('msg').textContent = '❌ ' + (data.error || 'Lỗi tạo thanh toán'); }
}
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
