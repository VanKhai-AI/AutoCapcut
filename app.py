"""
app.py — License Server cho Auto CapCut Video Sync
Email: Resend HTTP API (không bị Railway block)
Fix: expire_date tính từ lúc KÍCH HOẠT, không phải lúc tạo key
"""

import hashlib
import hmac
import json
import logging
import os
import random
import re
import sqlite3
import string
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import requests as req_lib
from flask import (Flask, jsonify, redirect, render_template_string,
                   request, session, url_for)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change_me_in_production")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

# ── Cấu hình ─────────────────────────────────────────────────────────────────
DB_PATH        = Path(__file__).parent / "licenses.db"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")

# Resend API — gửi email qua HTTP (Railway không block)
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "re_YTQ6GNnE_kK7TuURhfKSSCt88kDLf1tYC")
EMAIL_FROM     = os.environ.get("EMAIL_FROM", "noreply@vankhaiaistudio.com")

# PayOS
PAYOS_CLIENT_ID = os.environ.get("PAYOS_CLIENT_ID", "")
PAYOS_API_KEY   = os.environ.get("PAYOS_API_KEY",   "")
PAYOS_CHECKSUM  = os.environ.get("PAYOS_CHECKSUM",  "")

# Thông tin sản phẩm
PRICE_30D    = 99_000
PRICE_90D    = 249_000
PRICE_365D   = 799_000
PRODUCT_NAME = "Auto CapCut Video Sync"
SUPPORT_URL  = os.environ.get("SUPPORT_URL", "https://t.me/VanKhaiAI")
SHOP_URL     = os.environ.get("SHOP_URL", "https://autocapcut-production.up.railway.app")


# ── Database ──────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS licenses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                key          TEXT UNIQUE NOT NULL,
                email        TEXT NOT NULL,
                machine_id   TEXT DEFAULT NULL,
                days         INTEGER NOT NULL DEFAULT 30,
                created_at   TEXT NOT NULL,
                activated_at TEXT DEFAULT NULL,
                expire_date  TEXT DEFAULT NULL,
                active       INTEGER NOT NULL DEFAULT 1,
                order_id     TEXT DEFAULT NULL,
                notes        TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS orders (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                order_code  TEXT UNIQUE NOT NULL,
                email       TEXT NOT NULL,
                machine_id  TEXT DEFAULT NULL,
                days        INTEGER NOT NULL,
                amount      INTEGER NOT NULL,
                status      TEXT NOT NULL DEFAULT 'pending',
                created_at  TEXT NOT NULL,
                paid_at     TEXT DEFAULT NULL,
                key_sent    TEXT DEFAULT NULL
            );
            CREATE TABLE IF NOT EXISTS customers (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                email        TEXT UNIQUE NOT NULL,
                source       TEXT DEFAULT '',
                notes        TEXT DEFAULT '',
                created_at   TEXT NOT NULL,
                updated_at   TEXT NOT NULL
            );
        """)
        # Migration an toàn cho DB cũ
        for col_sql in [
            "ALTER TABLE orders ADD COLUMN machine_id TEXT DEFAULT NULL",
            "ALTER TABLE licenses ADD COLUMN activated_at TEXT DEFAULT NULL",
        ]:
            try:
                conn.execute(col_sql)
            except Exception:
                pass
    log.info("Database khởi tạo xong: %s", DB_PATH)


init_db()


# ── Tự động tạo/cập nhật Customer khi có email mới ───────────────────────────
def _upsert_customer(email: str, source: str = "", notes: str = ""):
    """Tạo mới hoặc cập nhật bản ghi khách hàng theo email."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM customers WHERE email=?", (email,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO customers (email, source, notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?)",
                (email, source, notes, now, now),
            )
            log.info("Tạo customer mới: %s", email)
        else:
            updates, params = [], []
            if source and not existing["source"]:
                updates.append("source=?"); params.append(source)
            if notes:
                updates.append("notes=?");  params.append(notes)
            updates.append("updated_at=?"); params.append(now)
            params.append(email)
            conn.execute(
                f"UPDATE customers SET {', '.join(updates)} WHERE email=?", params
            )
def _gen_key() -> str:
    chars = string.ascii_uppercase + string.digits
    while True:
        raw = "".join(random.choices(chars, k=16))
        key = f"{raw[0:4]}-{raw[4:8]}-{raw[8:12]}-{raw[12:16]}"
        with get_db() as conn:
            if not conn.execute(
                "SELECT id FROM licenses WHERE key=?", (key,)
            ).fetchone():
                return key


def _create_license(email: str, days: int,
                    machine_id: str = None,
                    order_id: str = None,
                    notes: str = "") -> str:
    """
    Tạo license key.
    expire_date = NULL — sẽ được tính khi kích hoạt lần đầu.
    Nếu machine_id được cung cấp ngay (pre-lock), tính expire luôn.
    """
    key = _gen_key()
    now = datetime.now()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Nếu pre-lock (biết machine_id ngay) → tính expire từ bây giờ
    if machine_id:
        expire_date  = (now + timedelta(days=days)).strftime("%Y-%m-%d")
        activated_at = now_str
    else:
        expire_date  = None   # sẽ tính lúc kích hoạt
        activated_at = None

    with get_db() as conn:
        conn.execute(
            "INSERT INTO licenses "
            "(key, email, machine_id, days, created_at, activated_at, expire_date, order_id, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (key, email, machine_id, days, now_str,
             activated_at, expire_date, order_id, notes),
        )
    # Tự động tạo/cập nhật bản ghi khách hàng
    _upsert_customer(email, source="license")
    log.info("Tạo key '%s' cho %s (%d ngày)%s",
             key, email, days,
             f" — pre-lock machine={machine_id}" if machine_id else "")
    return key


# ── Gửi email qua Resend API ──────────────────────────────────────────────────
def _build_email_html(key: str, days: int) -> str:
    return f"""<!DOCTYPE html>
<html lang="vi">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#F0F2F5;">
<div style="font-family:'Segoe UI',Arial,sans-serif;max-width:560px;
            margin:40px auto;background:#fff;border-radius:10px;
            overflow:hidden;box-shadow:0 4px 24px rgba(0,0,0,0.08);">
  <div style="background:#007BFF;padding:32px;">
    <h1 style="color:#fff;margin:0;font-size:22px;">🎬 {PRODUCT_NAME}</h1>
    <p style="color:rgba(255,255,255,0.85);margin:8px 0 0;font-size:14px;">
      Cảm ơn bạn đã tin dùng!
    </p>
  </div>
  <div style="padding:32px;">
    <p style="color:#1C1E21;font-size:15px;margin-top:0;">Xin chào,</p>
    <p style="color:#606770;font-size:14px;">Đây là License Key của bạn:</p>
    <div style="background:#F0F2F5;border:2px dashed #007BFF;border-radius:8px;
                padding:24px;text-align:center;margin:20px 0;">
      <span style="font-family:Consolas,monospace;font-size:28px;font-weight:bold;
                   color:#007BFF;letter-spacing:6px;">{key}</span>
      <p style="color:#606770;font-size:13px;margin:10px 0 0;">
        Thời hạn: <b>{days} ngày kể từ lần kích hoạt đầu tiên</b>
      </p>
    </div>
    <h3 style="color:#1C1E21;font-size:15px;margin-bottom:8px;">Cách kích hoạt:</h3>
    <ol style="color:#606770;font-size:14px;line-height:2;padding-left:20px;margin:0 0 20px;">
      <li>Mở phần mềm <b>{PRODUCT_NAME}</b></li>
      <li>Màn hình kích hoạt sẽ hiện ra</li>
      <li>Nhập key ở trên vào ô License Key</li>
      <li>Nhấn <b>Kích hoạt</b></li>
    </ol>
    <div style="background:#FFF3CD;border-left:4px solid #FFC107;
                padding:14px 16px;border-radius:4px;margin-bottom:20px;">
      <b style="color:#856404;">⚠️ Lưu ý quan trọng:</b><br>
      <span style="color:#856404;font-size:13px;">
        Key này chỉ dùng được cho <b>1 máy tính</b>.
        Sau khi kích hoạt, key sẽ bị khóa với máy đó.<br>
        Nếu cần chuyển sang máy khác, vui lòng liên hệ hỗ trợ.
      </span>
    </div>
    <p style="color:#606770;font-size:14px;margin:0;">
      Hỗ trợ: <a href="{SUPPORT_URL}" style="color:#007BFF;">{SUPPORT_URL}</a>
    </p>
  </div>
  <div style="background:#F0F2F5;padding:16px 32px;text-align:center;">
    <p style="color:#8D949E;font-size:12px;margin:0;">
      © {datetime.now().year} {PRODUCT_NAME} — Tự động tạo bởi hệ thống
    </p>
  </div>
</div>
</body>
</html>"""


def _send_key_email(to_email: str, key: str, days: int) -> bool:
    """Gửi license key qua Resend HTTP API."""
    if not RESEND_API_KEY:
        log.warning("Chưa cấu hình RESEND_API_KEY.")
        return False
    try:
        resp = req_lib.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            json={
                "from":    EMAIL_FROM,
                "to":      [to_email],
                "subject": f"🎬 License Key {PRODUCT_NAME} của bạn",
                "html":    _build_email_html(key, days),
            },
            timeout=10,
        )
        if resp.status_code in (200, 201):
            log.info("Đã gửi email đến %s", to_email)
            return True
        log.error("Resend lỗi %s: %s", resp.status_code, resp.text)
        return False
    except Exception as e:
        log.error("Gửi email thất bại: %s", e)
        return False


# ── Admin auth ────────────────────────────────────────────────────────────────
def admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return wrapped


# ── Admin Login ───────────────────────────────────────────────────────────────
ADMIN_LOGIN_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Admin Login</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#F0F2F5;display:flex;align-items:center;
         justify-content:center;min-height:100vh;
         font-family:'Segoe UI',Arial,sans-serif;}
    .card{background:#fff;border-radius:10px;padding:40px 36px;
          width:100%;max-width:380px;
          box-shadow:0 4px 24px rgba(0,0,0,0.08);}
    h2{color:#1C1E21;margin-bottom:24px;font-size:20px;}
    label{display:block;font-size:13px;color:#606770;margin-bottom:6px;}
    input{width:100%;padding:10px 14px;border:1px solid #ddd;
          border-radius:6px;font-size:15px;outline:none;
          transition:border-color .2s;}
    input:focus{border-color:#007BFF;}
    button{width:100%;margin-top:18px;padding:12px;
           background:#007BFF;color:#fff;border:none;
           border-radius:6px;font-size:15px;font-weight:600;
           cursor:pointer;transition:background .2s;}
    button:hover{background:#0069D9;}
    .err{color:#dc3545;font-size:13px;margin-top:12px;text-align:center;}
  </style>
</head>
<body>
  <div class="card">
    <h2>🔐 Admin Login</h2>
    <form method="POST">
      <label>Mật khẩu</label>
      <input type="password" name="password" placeholder="Nhập mật khẩu" autofocus>
      <button type="submit">Đăng nhập</button>
      {% if error %}<p class="err">{{ error }}</p>{% endif %}
    </form>
  </div>
</body>
</html>
"""


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_panel"))
        error = "Sai mật khẩu!"
    return render_template_string(ADMIN_LOGIN_HTML, error=error)


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("admin_login"))


# ── Admin Panel ───────────────────────────────────────────────────────────────
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Admin Panel — Auto CapCut Video Sync</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#F0F2F5;font-family:'Segoe UI',Arial,sans-serif;color:#1C1E21;}
    .topbar{background:#fff;padding:14px 24px;
            border-bottom:1px solid #e4e6eb;
            display:flex;align-items:center;justify-content:space-between;}
    .topbar h1{font-size:18px;}
    .topbar a{color:#606770;font-size:13px;text-decoration:none;}
    .topbar a:hover{color:#007BFF;}
    .wrap{max-width:1200px;margin:28px auto;padding:0 16px;}
    .stats{display:flex;gap:16px;margin-bottom:24px;flex-wrap:wrap;}
    .stat-card{background:#fff;border-radius:8px;padding:20px 24px;
               border:1px solid #e4e6eb;min-width:140px;}
    .stat-num{font-size:28px;font-weight:700;color:#007BFF;}
    .stat-num.red{color:#dc3545;}
    .stat-label{font-size:13px;color:#606770;margin-top:4px;}
    .panel{background:#fff;border-radius:8px;border:1px solid #e4e6eb;
           padding:24px;margin-bottom:24px;}
    .panel h2{font-size:16px;margin-bottom:16px;padding-bottom:12px;
              border-bottom:1px solid #e4e6eb;}
    .form-row{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;}
    .form-row input,.form-row select{
      padding:9px 12px;border:1px solid #ddd;border-radius:6px;
      font-size:14px;outline:none;transition:border-color .2s;}
    .form-row input:focus,.form-row select:focus{border-color:#007BFF;}
    .form-hint{font-size:12px;color:#888;margin-top:8px;}
    .btn{padding:9px 20px;border:none;border-radius:6px;
         font-size:14px;font-weight:600;cursor:pointer;transition:background .2s;}
    .btn-primary{background:#007BFF;color:#fff;}
    .btn-primary:hover{background:#0069D9;}
    .btn-sm{padding:4px 12px;font-size:12px;border:none;border-radius:4px;
            cursor:pointer;font-weight:600;}
    .btn-danger{background:#dc3545;color:#fff;}
    .btn-danger:hover{background:#c82333;}
    .btn-warn{background:#fd7e14;color:#fff;}
    .btn-warn:hover{background:#e96b02;}
    table{width:100%;border-collapse:collapse;font-size:13px;}
    th{background:#F0F2F5;padding:10px 12px;text-align:left;
       font-weight:600;color:#606770;white-space:nowrap;}
    td{padding:10px 12px;border-bottom:1px solid #e4e6eb;vertical-align:middle;}
    tr:last-child td{border-bottom:none;}
    tr:hover td{background:#f7f8fa;}
    .badge{display:inline-block;padding:3px 10px;border-radius:100px;font-size:11px;font-weight:600;}
    .badge-ok{background:#d4edda;color:#155724;}
    .badge-no{background:#f8d7da;color:#721c24;}
    .badge-pending{background:#fff3cd;color:#856404;}
    .key-mono{font-family:Consolas,monospace;color:#007BFF;letter-spacing:1px;}
    #msg{padding:10px 14px;border-radius:6px;margin-top:14px;font-size:13px;
         font-weight:600;display:none;}
    #msg.ok{background:#d4edda;color:#155724;display:block;}
    #msg.err{background:#f8d7da;color:#721c24;display:block;}
    .copy-btn{background:none;border:1px solid #ddd;border-radius:4px;
              padding:2px 8px;font-size:11px;cursor:pointer;color:#606770;}
    .copy-btn:hover{border-color:#007BFF;color:#007BFF;}
    .tab-btns{display:flex;gap:8px;margin-bottom:16px;}
    .tab-btn{padding:7px 20px;border:1px solid #ddd;border-radius:6px;
             background:#fff;cursor:pointer;font-size:13px;font-weight:600;color:#606770;}
    .tab-btn.active{background:#007BFF;color:#fff;border-color:#007BFF;}
    .tab-content{display:none;}
    .tab-content.active{display:block;}
  </style>
</head>
<body>

<div class="topbar">
  <h1>🎬 Admin Panel — Auto CapCut Video Sync</h1>
  <a href="/admin/logout">Đăng xuất</a>
</div>

<div class="wrap">

  <!-- Stats -->
  <div class="stats">
    <div class="stat-card">
      <div class="stat-num">{{ stats.customers }}</div>
      <div class="stat-label">Khách hàng</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{{ stats.active }}</div>
      <div class="stat-label">Key đang active</div>
    </div>
    <div class="stat-card">
      <div class="stat-num red">{{ stats.expired }}</div>
      <div class="stat-label">Key hết hạn / revoked</div>
    </div>
    <div class="stat-card">
      <div class="stat-num">{{ stats.orders_paid }}</div>
      <div class="stat-label">Orders đã thanh toán</div>
    </div>
    <div class="stat-card">
      <div class="stat-num red">{{ stats.orders_pending }}</div>
      <div class="stat-label">Orders chờ xử lý</div>
    </div>
  </div>

  <!-- Tạo key thủ công -->
  <div class="panel">
    <h2>Tạo key thủ công</h2>
    <div class="form-row">
      <input type="email" id="inp-email" placeholder="Email khách hàng *" style="min-width:220px;flex:1;">
      <input type="text"  id="inp-machine"
             placeholder="Machine ID (tuỳ chọn)"
             style="width:210px;font-family:Consolas,monospace;font-size:13px;">
      <select id="inp-days">
        <option value="30">30 ngày</option>
        <option value="90">90 ngày</option>
        <option value="365">365 ngày</option>
      </select>
      <input type="text" id="inp-note" placeholder="Ghi chú" style="width:150px;">
      <button class="btn btn-primary" onclick="createKey()">Tạo & Gửi Email</button>
    </div>
    <div class="form-hint">
      💡 <b>Machine ID:</b> Để trống → khách kích hoạt tự do lần đầu (expire tính từ lúc kích hoạt).
      Nhập Machine ID → key bị khóa cứng ngay (expire tính từ bây giờ).
    </div>
    <div id="msg"></div>
  </div>

  <!-- Tabs: Licenses / Orders / Khách hàng -->
  <div class="panel">
    <div class="tab-btns">
      <button class="tab-btn active" onclick="switchTab('licenses', this)">
        🔑 Licenses ({{ licenses|length }})
      </button>
      <button class="tab-btn" onclick="switchTab('orders', this)">
        🧾 Orders ({{ orders|length }})
      </button>
      <button class="tab-btn" onclick="switchTab('customers', this)">
        👥 Khách hàng ({{ customers|length }})
      </button>
    </div>

    <!-- Tab Licenses -->
    <div id="tab-licenses" class="tab-content active">
      <table>
        <thead>
          <tr>
            <th>Key</th><th>Email</th><th>Machine ID</th>
            <th>Ngày tạo</th><th>Kích hoạt</th><th>Hết hạn</th>
            <th>Còn lại</th><th>Status</th><th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% for lic in licenses %}
          <tr>
            <td>
              <span class="key-mono">{{ lic.key }}</span>
              <button class="copy-btn" onclick="copyText('{{ lic.key }}')">copy</button>
            </td>
            <td>{{ lic.email }}</td>
            <td style="font-family:Consolas,monospace;font-size:11px;color:#606770;">
              {{ lic.machine_id or '—' }}
            </td>
            <td style="color:#606770;font-size:12px;">{{ lic.created_at[:10] }}</td>
            <td style="color:#606770;font-size:12px;">
              {{ lic.activated_at[:10] if lic.activated_at else '—' }}
            </td>
            <td style="font-size:12px;">
              {{ lic.expire_date or '(chờ kích hoạt)' }}
            </td>
            <td style="font-size:12px;">
              {% if lic.expire_date %}
                {{ lic.days_left }}d
              {% else %}
                {{ lic.days }}d (sau KH)
              {% endif %}
            </td>
            <td>
              {% if not lic.active %}
                <span class="badge badge-no">Revoked</span>
              {% elif lic.expire_date and lic.days_left < 0 %}
                <span class="badge badge-no">Hết hạn</span>
              {% elif not lic.expire_date %}
                <span class="badge badge-pending">Chờ KH</span>
              {% else %}
                <span class="badge badge-ok">Active</span>
              {% endif %}
            </td>
            <td style="display:flex;gap:6px;flex-wrap:wrap;">
              {% if lic.active %}
              <button class="btn-sm btn-danger"
                onclick="revokeKey('{{ lic.key }}', this)">Revoke</button>
              {% endif %}
              {% if lic.machine_id %}
              <button class="btn-sm btn-warn"
                onclick="resetMachine('{{ lic.key }}', this)">Reset máy</button>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <!-- Tab Orders -->
    <div id="tab-orders" class="tab-content">
      <table>
        <thead>
          <tr>
            <th>Order Code</th><th>Email</th><th>Machine ID</th>
            <th>Gói</th><th>Tiền</th><th>Ngày tạo</th>
            <th>Thanh toán</th><th>Key đã gửi</th><th>Status</th>
          </tr>
        </thead>
        <tbody>
          {% for o in orders %}
          <tr>
            <td style="font-family:Consolas,monospace;font-size:12px;">{{ o.order_code }}</td>
            <td>{{ o.email }}</td>
            <td style="font-family:Consolas,monospace;font-size:11px;color:#606770;">
              {{ o.machine_id or '—' }}
            </td>
            <td>{{ o.days }}d</td>
            <td>{{ "{:,.0f}".format(o.amount) }}₫</td>
            <td style="color:#606770;font-size:12px;">{{ o.created_at[:16] }}</td>
            <td style="color:#606770;font-size:12px;">{{ o.paid_at[:16] if o.paid_at else '—' }}</td>
            <td>
              {% if o.key_sent %}
              <span class="key-mono" style="font-size:11px;">{{ o.key_sent }}</span>
              {% else %}—{% endif %}
            </td>
            <td>
              {% if o.status == 'paid' %}
                <span class="badge badge-ok">Đã TT</span>
              {% else %}
                <span class="badge badge-pending">Pending</span>
              {% endif %}
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>

    <!-- Tab Khách hàng -->
    <div id="tab-customers" class="tab-content">

      <!-- Thanh tìm kiếm -->
      <div style="display:flex;gap:10px;margin-bottom:16px;align-items:center;">
        <input type="text" id="cust-search" placeholder="🔍  Tìm theo email, tên, SĐT..."
               oninput="filterCustomers()"
               style="flex:1;padding:9px 14px;border:1px solid #ddd;border-radius:6px;
                      font-size:14px;outline:none;">
        <span id="cust-count" style="font-size:12px;color:#606770;white-space:nowrap;">
          {{ customers|length }} khách
        </span>
      </div>

      <table id="cust-table">
        <thead>
          <tr>
            <th>Email</th><th>Nguồn</th><th>Keys</th>
            <th>Đơn hàng</th><th>Ngày tạo</th><th>Ghi chú</th><th>Action</th>
          </tr>
        </thead>
        <tbody>
          {% for c in customers %}
          <tr data-search="{{ c.email|lower }}">
            <td><b>{{ c.email }}</b></td>
            <td>
              <span class="badge {{ 'badge-ok' if c.source == 'payos' else 'badge-pending' }}">
                {{ c.source or 'manual' }}
              </span>
            </td>
            <td style="text-align:center;font-weight:bold;color:#007BFF;">{{ c.key_count }}</td>
            <td style="text-align:center;font-weight:bold;color:#28a745;">{{ c.order_count }}</td>
            <td style="color:#606770;font-size:12px;">{{ c.created_at[:10] }}</td>
            <td>
              <span class="cust-notes-view-{{ c.id }}"
                    style="color:#606770;font-size:12px;max-width:200px;
                           display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
                {{ c.notes or '—' }}
              </span>
              <input class="cust-notes-edit-{{ c.id }}" type="text" value="{{ c.notes }}"
                     style="display:none;padding:4px 8px;border:1px solid #007BFF;
                            border-radius:4px;font-size:12px;width:180px;">
            </td>
            <td style="display:flex;gap:6px;">
              <button class="btn-sm btn-primary"
                id="cust-edit-btn-{{ c.id }}"
                onclick="editCustomer({{ c.id }}, this)">Sửa</button>
              <button class="btn-sm" id="cust-save-btn-{{ c.id }}"
                style="display:none;background:#28a745;color:#fff;"
                onclick="saveCustomer({{ c.id }}, '{{ c.email }}', this)">Lưu</button>
            </td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
    </div>
  </div>

<script>
function switchTab(name, btn) {
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + name).classList.add('active');
  btn.classList.add('active');
}

async function createKey() {
  const email     = document.getElementById('inp-email').value.trim();
  const machineId = document.getElementById('inp-machine').value.trim().toUpperCase();
  const days      = document.getElementById('inp-days').value;
  const note      = document.getElementById('inp-note').value.trim();
  const msg       = document.getElementById('msg');
  msg.className = ''; msg.textContent = '';

  if (!email || !email.includes('@')) {
    msg.className = 'err'; msg.textContent = '❌ Vui lòng nhập email hợp lệ.'; return;
  }
  if (machineId && !/^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(machineId)) {
    msg.className = 'err'; msg.textContent = '❌ Machine ID sai định dạng.'; return;
  }
  msg.className = 'ok'; msg.textContent = 'Đang tạo key...';

  try {
    const res  = await fetch('/admin/create_key', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ email, days: parseInt(days), note, machine_id: machineId || null })
    });
    const data = await res.json();
    if (data.status === 'ok') {
      const lockInfo = data.machine_locked
        ? `🔒 Khóa với: <b>${data.machine_locked}</b>`
        : '🔓 Chờ kích hoạt';
      msg.className = 'ok';
      msg.innerHTML = `✅ Key: <b class="key-mono">${data.key}</b>
        <button class="copy-btn" onclick="copyText('${data.key}')">copy</button>
        &nbsp;|&nbsp; ${data.email_sent ? '📧 Email đã gửi.' : '⚠️ Gửi email thất bại — copy key!'}
        &nbsp;|&nbsp; ${lockInfo}`;
      ['inp-email','inp-machine','inp-note'].forEach(id => document.getElementById(id).value = '');
      setTimeout(() => location.reload(), 4000);
    } else {
      msg.className = 'err';
      msg.textContent = '❌ ' + (data.msg || JSON.stringify(data));
    }
  } catch(e) {
    msg.className = 'err'; msg.textContent = '❌ Lỗi kết nối: ' + e;
  }
}

async function revokeKey(key, btn) {
  if (!confirm('Revoke key ' + key + '?')) return;
  btn.disabled = true; btn.textContent = '...';
  const res  = await fetch('/admin/revoke_key', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ key })
  });
  const data = await res.json();
  if (data.status === 'ok') location.reload();
  else { alert('Lỗi: ' + data.msg); btn.disabled = false; btn.textContent = 'Revoke'; }
}

async function resetMachine(key, btn) {
  if (!confirm('Reset machine cho key ' + key + '?\\nKey sẽ có thể kích hoạt trên máy mới.')) return;
  btn.disabled = true; btn.textContent = '...';
  const res  = await fetch('/admin/reset_machine', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({ key })
  });
  const data = await res.json();
  if (data.status === 'ok') location.reload();
  else { alert('Lỗi: ' + data.msg); btn.disabled = false; btn.textContent = 'Reset máy'; }
}

function filterCustomers() {
  const q   = document.getElementById('cust-search').value.toLowerCase();
  const rows = document.querySelectorAll('#cust-table tbody tr');
  let visible = 0;
  rows.forEach(row => {
    const match = row.dataset.search.includes(q);
    row.style.display = match ? '' : 'none';
    if (match) visible++;
  });
  document.getElementById('cust-count').textContent = visible + ' khách';
}

function editCustomer(id, btn) {
  document.querySelector(`.cust-notes-view-${id}`).style.display = 'none';
  document.querySelector(`.cust-notes-edit-${id}`).style.display = 'inline-block';
  btn.style.display = 'none';
  document.getElementById(`cust-save-btn-${id}`).style.display = 'inline-block';
}

async function saveCustomer(id, email, btn) {
  const notes = document.querySelector(`.cust-notes-edit-${id}`).value.trim();
  btn.textContent = '...'; btn.disabled = true;
  try {
    const res  = await fetch('/admin/update_customer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ email, notes })
    });
    const data = await res.json();
    if (data.status === 'ok') {
      document.querySelector(`.cust-notes-view-${id}`).textContent = notes || '—';
      document.querySelector(`.cust-notes-view-${id}`).style.display = '';
      document.querySelector(`.cust-notes-edit-${id}`).style.display = 'none';
      btn.style.display = 'none';
      document.getElementById(`cust-edit-btn-${id}`).style.display = 'inline-block';
    } else {
      alert('Lỗi: ' + data.msg);
    }
  } catch(e) { alert('Lỗi kết nối: ' + e); }
  btn.textContent = 'Lưu'; btn.disabled = false;
}

function copyText(text) {
  navigator.clipboard.writeText(text).then(() => {
    if (event && event.target) {
      const t = event.target; const prev = t.textContent;
      t.textContent = 'copied!';
      setTimeout(() => t.textContent = prev, 1500);
    }
  });
}
</script>
</body>
</html>
"""


@app.route("/admin")
@app.route("/admin/")
@admin_required
def admin_panel():
    with get_db() as conn:
        raw_licenses = conn.execute(
            "SELECT * FROM licenses ORDER BY id DESC LIMIT 200"
        ).fetchall()
        orders = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT 200"
        ).fetchall()
        # Customers kèm số key và số đơn hàng
        raw_customers = conn.execute("""
            SELECT c.*,
                   COUNT(DISTINCT l.id) AS key_count,
                   COUNT(DISTINCT o.id) AS order_count
            FROM customers c
            LEFT JOIN licenses l ON l.email = c.email
            LEFT JOIN orders   o ON o.email = c.email AND o.status = 'paid'
            GROUP BY c.id
            ORDER BY c.created_at DESC
            LIMIT 500
        """).fetchall()

        active_count         = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE active=1"
        ).fetchone()[0]
        inactive_count       = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE active=0"
        ).fetchone()[0]
        orders_paid_count    = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='paid'"
        ).fetchone()[0]
        orders_pending_count = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        ).fetchone()[0]
        customers_count      = conn.execute(
            "SELECT COUNT(*) FROM customers"
        ).fetchone()[0]

    # Tính days_left cho licenses
    licenses = []
    for lic in raw_licenses:
        d = dict(lic)
        if d.get("expire_date"):
            try:
                dt = datetime.strptime(d["expire_date"], "%Y-%m-%d")
                d["days_left"] = (dt - datetime.now()).days
            except Exception:
                d["days_left"] = 0
        else:
            d["days_left"] = d.get("days", 0)
        licenses.append(d)

    customers = [dict(c) for c in raw_customers]

    stats = {
        "customers":      customers_count,
        "active":         active_count,
        "expired":        inactive_count,
        "orders_paid":    orders_paid_count,
        "orders_pending": orders_pending_count,
    }
    return render_template_string(ADMIN_HTML,
                                  licenses=licenses,
                                  orders=orders,
                                  customers=customers,
                                  stats=stats)


@app.route("/admin/create_key", methods=["POST"])
@admin_required
def admin_create_key():
    data       = request.get_json(silent=True) or {}
    email      = (data.get("email")      or "").strip().lower()
    days       = int(data.get("days", 30))
    note       = (data.get("note")       or "").strip()
    machine_id = (data.get("machine_id") or "").strip().upper() or None

    if not email or "@" not in email:
        return jsonify({"status": "error", "msg": "Email không hợp lệ"}), 400
    if days not in (30, 90, 365):
        return jsonify({"status": "error", "msg": "Số ngày không hợp lệ"}), 400
    if machine_id and not re.fullmatch(
            r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", machine_id):
        return jsonify({"status": "error",
                        "msg": "Machine ID sai định dạng (XXXX-XXXX-XXXX-XXXX)"}), 400

    key = _create_license(
        email=email, days=days,
        machine_id=machine_id,
        notes=note or "Admin tạo thủ công",
    )
    # Lưu thông tin khách hàng
    _upsert_customer(email, source="admin")

    email_sent = False
    try:
        email_sent = _send_key_email(email, key, days)
    except Exception as e:
        log.error("Gửi email thất bại: %s", e)

    return jsonify({
        "status":         "ok",
        "key":            key,
        "email_sent":     email_sent,
        "machine_locked": machine_id,
    })


@app.route("/admin/revoke_key", methods=["POST"])
@admin_required
def admin_revoke_key():
    key = (request.get_json(silent=True) or {}).get("key", "").strip().upper()
    if not key:
        return jsonify({"status": "error", "msg": "Thiếu key"}), 400
    with get_db() as conn:
        conn.execute("UPDATE licenses SET active=0 WHERE key=?", (key,))
    log.info("Revoked key: %s", key)
    return jsonify({"status": "ok"})


@app.route("/admin/reset_machine", methods=["POST"])
@admin_required
def admin_reset_machine():
    """Unlock machine — cho phép khách kích hoạt trên máy mới."""
    key = (request.get_json(silent=True) or {}).get("key", "").strip().upper()
    if not key:
        return jsonify({"status": "error", "msg": "Thiếu key"}), 400
    with get_db() as conn:
        conn.execute(
            "UPDATE licenses SET machine_id=NULL, activated_at=NULL, expire_date=NULL WHERE key=?",
            (key,)
        )
    log.info("Reset machine cho key: %s", key)
    return jsonify({"status": "ok"})


@app.route("/admin/update_customer", methods=["POST"])
@admin_required
def admin_update_customer():
    data  = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    notes = (data.get("notes") or "").strip()
    if not email:
        return jsonify({"status": "error", "msg": "Thiếu email"}), 400
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM customers WHERE email=?", (email,)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE customers SET notes=?, updated_at=? WHERE email=?",
                (notes, now, email)
            )
        else:
            conn.execute(
                "INSERT INTO customers (email, notes, source, created_at, updated_at) "
                "VALUES (?,?,'manual',?,?)",
                (email, notes, now, now)
            )
    log.info("Cập nhật ghi chú customer: %s", email)
    return jsonify({"status": "ok"})


# ═════════════════════════════════════════════════════════════════════════════
# CLIENT API
# ═════════════════════════════════════════════════════════════════════════════

@app.route("/api/activate", methods=["POST"])
def api_activate():
    """
    Kích hoạt key lần đầu.
    - Lần đầu: gán machine_id + tính expire_date từ bây giờ
    - Lần sau: kiểm tra machine_id khớp
    """
    data       = request.get_json(silent=True) or {}
    key        = (data.get("key") or "").strip().upper()
    machine_id = (data.get("machine_id") or "").strip()

    if not key or not machine_id:
        return jsonify({"status": "error", "msg": "Thiếu key hoặc machine_id"}), 400

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE key=?", (key,)
        ).fetchone()

        if not row:
            return jsonify({"status": "invalid"})
        if not row["active"]:
            return jsonify({"status": "invalid"})

        # Kiểm tra hoặc gán machine_id
        if row["machine_id"] is None:
            # Kích hoạt lần đầu → tính expire từ bây giờ
            expire_date  = (datetime.now() + timedelta(days=row["days"])).strftime("%Y-%m-%d")
            activated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE licenses SET machine_id=?, activated_at=?, expire_date=? WHERE key=?",
                (machine_id, activated_at, expire_date, key),
            )
            log.info("Kích hoạt key '%s' — machine '%s' — expire %s",
                     key, machine_id, expire_date)
        elif row["machine_id"] != machine_id:
            return jsonify({"status": "wrong_machine"})
        else:
            expire_date = row["expire_date"]

    # Kiểm tra hết hạn
    try:
        expire_dt = datetime.strptime(expire_date, "%Y-%m-%d")
        days_left = (expire_dt - datetime.now()).days
    except Exception:
        days_left = 0

    if days_left < 0:
        return jsonify({"status": "expired", "expire": expire_date})

    return jsonify({
        "status":     "ok",
        "expire":     expire_date,
        "days_left":  days_left,
        "days_total": row["days"],
        "email":      row["email"],
    })


@app.route("/api/check", methods=["POST"])
def api_check():
    """Ping mỗi lần mở tool để xác nhận license còn hạn."""
    data       = request.get_json(silent=True) or {}
    key        = (data.get("key") or "").strip().upper()
    machine_id = (data.get("machine_id") or "").strip()

    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM licenses WHERE key=?", (key,)
        ).fetchone()

    if not row or not row["active"]:
        return jsonify({"status": "invalid"})
    if row["machine_id"] and row["machine_id"] != machine_id:
        return jsonify({"status": "wrong_machine"})
    if not row["expire_date"]:
        # Chưa kích hoạt lần nào
        return jsonify({"status": "ok", "expire": None,
                        "days_left": row["days"], "days_total": row["days"],
                        "email": row["email"]})

    try:
        expire_dt = datetime.strptime(row["expire_date"], "%Y-%m-%d")
        days_left = (expire_dt - datetime.now()).days
    except Exception:
        days_left = 0

    if days_left < 0:
        return jsonify({"status": "expired", "expire": row["expire_date"]})

    return jsonify({
        "status":     "ok",
        "expire":     row["expire_date"],
        "days_left":  days_left,
        "days_total": row["days"],
        "email":      row["email"],
    })


# ═════════════════════════════════════════════════════════════════════════════
# PAYOS PAYMENT
# ═════════════════════════════════════════════════════════════════════════════

def _payos_checksum(data: dict) -> str:
    sorted_str = "&".join(f"{k}={v}" for k, v in sorted(data.items()))
    return hmac.new(
        PAYOS_CHECKSUM.encode("utf-8"),
        sorted_str.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@app.route("/payment/create", methods=["POST"])
def payment_create():
    import time
    data       = request.get_json(silent=True) or {}
    email      = (data.get("email") or "").strip().lower()
    days       = int(data.get("days", 30))
    machine_id = (data.get("machine_id") or "").strip().upper()

    if not email or "@" not in email:
        return jsonify({"error": "Email không hợp lệ"}), 400
    if days not in (30, 90, 365):
        return jsonify({"error": "Gói không hợp lệ"}), 400
    if not machine_id:
        return jsonify({"error": "Vui lòng nhập Machine ID"}), 400
    if not re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", machine_id):
        return jsonify({"error": "Machine ID không đúng định dạng"}), 400

    amount_map = {30: PRICE_30D, 90: PRICE_90D, 365: PRICE_365D}
    amount     = amount_map[days]
    order_code = int(time.time() * 1000) % 9_999_999

    with get_db() as conn:
        conn.execute(
            "INSERT INTO orders "
            "(order_code, email, machine_id, days, amount, status, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (str(order_code), email, machine_id, days, amount, "pending",
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )

    payload = {
        "orderCode":   order_code,
        "amount":      amount,
        "description": f"Key {days}d",
        "buyerEmail":  email,
        "returnUrl":   f"{request.host_url}payment/success",
        "cancelUrl":   f"{request.host_url}payment/cancel",
    }
    payload["signature"] = _payos_checksum(payload)

    try:
        resp   = req_lib.post(
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
        return jsonify({
            "checkout_url": result["data"]["checkoutUrl"],
            "order_code":   order_code,
        })
    return jsonify({"error": result.get("desc", "PayOS error")}), 400


@app.route("/payment/webhook", methods=["POST"])
def payment_webhook():
    data = request.get_json(silent=True) or {}
    log.info("PayOS webhook: %s", json.dumps(data, ensure_ascii=False))

    received_sig = data.get("signature", "")
    check_data   = {k: v for k, v in data.items() if k != "signature"}
    if PAYOS_CHECKSUM and not hmac.compare_digest(
            received_sig, _payos_checksum(check_data)):
        log.warning("Webhook checksum không hợp lệ!")
        return jsonify({"error": "invalid signature"}), 400

    order_code = str(data.get("orderCode", ""))
    if data.get("status") != "PAID":
        return jsonify({"ok": True})

    with get_db() as conn:
        order = conn.execute(
            "SELECT * FROM orders WHERE order_code=?", (order_code,)
        ).fetchone()
        if not order:
            log.warning("Không tìm thấy order: %s", order_code)
            return jsonify({"ok": True})
        if order["status"] == "paid":
            return jsonify({"ok": True})

        machine_id = order["machine_id"] or None
        key = _create_license(
            email=order["email"],
            days=order["days"],
            machine_id=machine_id,
            order_id=order_code,
            notes=f"PayOS order {order_code}",
        )
        conn.execute(
            "UPDATE orders SET status='paid', paid_at=?, key_sent=? WHERE order_code=?",
            (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), key, order_code),
        )

    _send_key_email(order["email"], key, order["days"])
    # Cập nhật thông tin khách hàng từ order
    _upsert_customer(order["email"], source="payos")
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
        </body></html>"""
    return """
    <html><body style="font-family:Arial;text-align:center;padding:60px;">
      <h2>Đang xử lý thanh toán...</h2>
      <p>Vui lòng chờ vài giây rồi kiểm tra email.</p>
    </body></html>"""


@app.route("/payment/cancel")
def payment_cancel():
    return """
    <html><body style="font-family:Arial;text-align:center;padding:60px;background:#F0F2F5;">
      <h2 style="color:#FD7E14;">Thanh toán bị huỷ</h2>
      <p>Bạn đã huỷ thanh toán. Không có khoản tiền nào bị trừ.</p>
    </body></html>"""


# ═════════════════════════════════════════════════════════════════════════════
# TRANG BÁN HÀNG
# ═════════════════════════════════════════════════════════════════════════════

DOWNLOAD_URL = os.environ.get("DOWNLOAD_URL", "https://drive.google.com/uc?export=download&id=YOUR_FILE_ID")
APP_VERSION  = os.environ.get("APP_VERSION",  "2.1.0")
APP_SIZE     = os.environ.get("APP_SIZE",      "45 MB")

SHOP_HTML = """
<!DOCTYPE html>
<html lang="vi">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Auto CapCut Video Sync — Tự động ghép video, audio, phụ đề vào CapCut</title>
  <meta name="description" content="Công cụ tự động ghép video, audio và phụ đề .srt vào CapCut Draft chỉ trong vài giây. Tiết kiệm hàng giờ edit tay.">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root{
      --bg:#07070F;--surface:#0D0D1C;--card:#12122A;
      --border:rgba(255,255,255,0.06);--border2:rgba(255,255,255,0.1);
      --gold:#F5A623;--gold2:#FFD166;--gold-dim:rgba(245,166,35,0.12);
      --blue:#4F8EF7;--green:#2ECC71;
      --text:#EDEAF4;--muted:#7A7898;--muted2:#5A5878;
    }
    *{box-sizing:border-box;margin:0;padding:0}
    html{scroll-behavior:smooth}
    body{font-family:'Plus Jakarta Sans',sans-serif;background:var(--bg);color:var(--text);line-height:1.6;overflow-x:hidden;}
    ::-webkit-scrollbar{width:5px}
    ::-webkit-scrollbar-track{background:var(--bg)}
    ::-webkit-scrollbar-thumb{background:#2A2A44;border-radius:3px}

    /* ── NAV ── */
    nav{position:fixed;top:0;left:0;right:0;z-index:100;
        display:flex;align-items:center;justify-content:space-between;
        padding:16px 5%;background:rgba(7,7,15,0.9);
        backdrop-filter:blur(16px);border-bottom:1px solid var(--border);}
    .logo{font-family:'Bebas Neue',sans-serif;font-size:20px;letter-spacing:2px;
          color:var(--text);display:flex;align-items:center;gap:10px;text-decoration:none;}
    .logo-badge{background:var(--gold);color:#07070F;font-size:9px;font-weight:700;
                padding:2px 7px;border-radius:3px;letter-spacing:1px;font-family:'Plus Jakarta Sans',sans-serif;}
    .nav-links{display:flex;align-items:center;gap:28px;}
    .nav-link{color:var(--muted);font-size:13px;text-decoration:none;transition:color .2s;}
    .nav-link:hover{color:var(--text)}
    .nav-dl-btn{background:var(--gold);color:#07070F;font-size:13px;font-weight:700;
                padding:8px 18px;border-radius:6px;text-decoration:none;
                transition:transform .15s,box-shadow .15s;}
    .nav-dl-btn:hover{transform:translateY(-1px);box-shadow:0 6px 20px rgba(245,166,35,.35)}

    /* ── HERO ── */
    .hero{min-height:100vh;display:flex;align-items:center;justify-content:center;
          padding:120px 5% 80px;position:relative;overflow:hidden;}
    .hero-glow{position:absolute;width:800px;height:800px;border-radius:50%;
               background:radial-gradient(circle,rgba(79,142,247,0.07) 0%,transparent 65%);
               top:-200px;right:-200px;pointer-events:none;}
    .hero-glow2{position:absolute;width:500px;height:500px;border-radius:50%;
                background:radial-gradient(circle,rgba(245,166,35,0.06) 0%,transparent 65%);
                bottom:-100px;left:-100px;pointer-events:none;}
    .hero-inner{display:grid;grid-template-columns:1fr 1fr;gap:64px;
                align-items:center;max-width:1200px;width:100%;margin:0 auto;}
    .hero-left{}
    .hero-tag{display:inline-flex;align-items:center;gap:8px;
              border:1px solid rgba(79,142,247,.3);background:rgba(79,142,247,.07);
              padding:5px 14px;border-radius:100px;font-size:12px;color:var(--blue);
              margin-bottom:24px;font-weight:600;letter-spacing:.5px;}
    .hero-tag-dot{width:6px;height:6px;border-radius:50%;background:var(--blue);
                  animation:pulse 2s ease infinite;}
    @keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.3;transform:scale(.7)}}
    .hero h1{font-family:'Bebas Neue',sans-serif;font-size:clamp(52px,5.5vw,80px);
             line-height:1.05;letter-spacing:1px;margin-bottom:20px;}
    .hero h1 em{font-style:normal;
                background:linear-gradient(135deg,var(--gold),var(--gold2));
                -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
    .hero-desc{font-size:16px;color:var(--muted);line-height:1.8;margin-bottom:36px;max-width:480px;}
    .hero-desc strong{color:var(--text);}
    .hero-btns{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:48px;}
    .btn-gold{display:inline-flex;align-items:center;gap:8px;background:var(--gold);
              color:#07070F;font-weight:700;font-size:15px;padding:14px 28px;
              border-radius:8px;text-decoration:none;border:none;cursor:pointer;
              transition:transform .15s,box-shadow .15s;}
    .btn-gold:hover{transform:translateY(-2px);box-shadow:0 10px 32px rgba(245,166,35,.35)}
    .btn-outline{display:inline-flex;align-items:center;gap:8px;
                 background:transparent;color:var(--text);font-weight:600;font-size:15px;
                 padding:14px 28px;border-radius:8px;text-decoration:none;
                 border:1px solid var(--border2);transition:border-color .2s,background .2s;}
    .btn-outline:hover{border-color:var(--gold);background:var(--gold-dim)}
    .hero-trust{display:flex;align-items:center;gap:16px;flex-wrap:wrap;}
    .trust-item{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted);}
    .trust-dot{width:5px;height:5px;border-radius:50%;background:var(--green);}

    /* Hero screenshot */
    .hero-right{position:relative;}
    .screenshot-wrap{position:relative;border-radius:12px;overflow:hidden;
                     border:1px solid var(--border2);
                     box-shadow:0 40px 80px rgba(0,0,0,.6),0 0 0 1px rgba(255,255,255,.05);}
    .screenshot-wrap img{width:100%;display:block;}
    .screenshot-badge{position:absolute;top:16px;right:16px;
                      background:rgba(46,204,113,.15);border:1px solid rgba(46,204,113,.4);
                      color:var(--green);font-size:11px;font-weight:700;
                      padding:4px 12px;border-radius:100px;letter-spacing:1px;}
    .float-card{position:absolute;background:var(--card);border:1px solid var(--border2);
                border-radius:10px;padding:12px 16px;
                box-shadow:0 8px 24px rgba(0,0,0,.4);}
    .float-card-1{bottom:-20px;left:-24px;}
    .float-card-2{top:-16px;right:-16px;}
    .fc-num{font-family:'Bebas Neue',sans-serif;font-size:28px;color:var(--gold);letter-spacing:1px;}
    .fc-lbl{font-size:11px;color:var(--muted);margin-top:2px;}

    /* ── COUNTER STRIP ── */
    .counter-strip{background:var(--surface);border-top:1px solid var(--border);
                   border-bottom:1px solid var(--border);padding:32px 5%;}
    .counter-inner{display:flex;justify-content:center;gap:80px;
                   max-width:1200px;margin:0 auto;flex-wrap:wrap;}
    .counter-item{text-align:center;}
    .counter-num{font-family:'Bebas Neue',sans-serif;font-size:48px;letter-spacing:2px;
                 background:linear-gradient(135deg,var(--gold),var(--gold2));
                 -webkit-background-clip:text;-webkit-text-fill-color:transparent;}
    .counter-label{font-size:13px;color:var(--muted);margin-top:4px;}

    /* ── SECTION BASE ── */
    .section{padding:80px 5%;}
    .section-inner{max-width:1200px;margin:0 auto;}
    .section-label{font-size:11px;font-weight:700;letter-spacing:3px;
                   color:var(--gold);text-transform:uppercase;margin-bottom:12px;}
    .section-title{font-family:'Bebas Neue',sans-serif;font-size:clamp(34px,4vw,52px);
                   letter-spacing:1px;line-height:1.1;margin-bottom:16px;}
    .section-sub{font-size:15px;color:var(--muted);max-width:520px;line-height:1.8;}

    /* ── FEATURES ── */
    .features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
                   gap:16px;margin-top:48px;}
    .feature-card{background:var(--card);border:1px solid var(--border);border-radius:14px;
                  padding:28px;transition:border-color .25s,transform .25s;}
    .feature-card:hover{border-color:rgba(245,166,35,.2);transform:translateY(-4px);}
    .feature-icon{width:48px;height:48px;border-radius:12px;background:var(--gold-dim);
                  border:1px solid rgba(245,166,35,.2);display:flex;align-items:center;
                  justify-content:center;font-size:22px;margin-bottom:20px;}
    .feature-title{font-size:15px;font-weight:700;margin-bottom:8px;}
    .feature-desc{font-size:13px;color:var(--muted);line-height:1.7;}

    /* ── SCREENSHOT SECTION ── */
    .screenshots-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:48px;}
    .shot-card{border-radius:12px;overflow:hidden;border:1px solid var(--border2);
               background:var(--card);transition:transform .2s,box-shadow .2s;}
    .shot-card:hover{transform:translateY(-4px);box-shadow:0 20px 48px rgba(0,0,0,.5);}
    .shot-card img{width:100%;display:block;}
    .shot-caption{padding:14px 16px;font-size:13px;color:var(--muted);}
    .shot-caption strong{color:var(--text);}

    /* ── STEPS ── */
    .steps-wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));
                gap:0;margin-top:48px;position:relative;}
    .steps-wrap::before{content:"";position:absolute;top:28px;left:8%;right:8%;height:1px;
      background:linear-gradient(90deg,transparent,var(--border) 20%,var(--border) 80%,transparent);}
    .step{text-align:center;padding:0 20px;position:relative;}
    .step-num{width:56px;height:56px;border-radius:50%;
              background:linear-gradient(135deg,rgba(79,142,247,.2),rgba(79,142,247,.05));
              border:1px solid rgba(79,142,247,.3);
              display:flex;align-items:center;justify-content:center;
              font-family:'Bebas Neue',sans-serif;font-size:22px;color:var(--blue);
              margin:0 auto 20px;position:relative;z-index:1;}
    .step-title{font-size:15px;font-weight:600;margin-bottom:8px;}
    .step-desc{font-size:13px;color:var(--muted);line-height:1.6;}
    .step-desc code{background:rgba(255,255,255,.06);padding:1px 6px;
                    border-radius:4px;font-size:12px;}

    /* ── DOWNLOAD SECTION ── */
    .download-section{background:var(--surface);border-top:1px solid var(--border);
                      border-bottom:1px solid var(--border);}
    .dl-inner{max-width:1000px;margin:0 auto;padding:80px 5%;
              display:grid;grid-template-columns:1fr auto;gap:48px;align-items:center;}
    .dl-left h2{font-family:'Bebas Neue',sans-serif;font-size:clamp(36px,4vw,52px);
                letter-spacing:1px;margin-bottom:12px;}
    .dl-left h2 em{font-style:normal;color:var(--gold);}
    .dl-left p{font-size:15px;color:var(--muted);line-height:1.7;max-width:500px;}
    .dl-meta{display:flex;gap:20px;margin-top:20px;flex-wrap:wrap;}
    .dl-meta-item{display:flex;align-items:center;gap:6px;font-size:13px;color:var(--muted);}
    .dl-meta-item svg{opacity:.5;}
    .dl-right{text-align:center;}
    .dl-btn{display:inline-flex;flex-direction:column;align-items:center;gap:4px;
            background:linear-gradient(135deg,var(--gold),#E8941A);
            color:#07070F;font-weight:700;font-size:16px;
            padding:20px 48px;border-radius:12px;text-decoration:none;
            box-shadow:0 8px 32px rgba(245,166,35,.35);
            transition:transform .15s,box-shadow .15s;}
    .dl-btn:hover{transform:translateY(-3px);box-shadow:0 16px 48px rgba(245,166,35,.45)}
    .dl-btn-sub{font-size:11px;font-weight:500;opacity:.7;margin-top:2px;}
    .dl-note{font-size:11px;color:var(--muted2);margin-top:10px;}
    .req-list{display:flex;flex-direction:column;gap:8px;margin-top:24px;}
    .req-item{display:flex;align-items:center;gap:10px;font-size:13px;color:var(--muted);}
    .req-icon{width:28px;height:28px;border-radius:6px;background:var(--card);
              border:1px solid var(--border);display:flex;align-items:center;
              justify-content:center;font-size:14px;flex-shrink:0;}

    /* ── TESTIMONIALS ── */
    .testi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
                gap:16px;margin-top:48px;}
    .testi-card{background:var(--card);border:1px solid var(--border);border-radius:14px;padding:24px;}
    .testi-stars{color:var(--gold);font-size:14px;margin-bottom:14px;letter-spacing:2px;}
    .testi-text{font-size:14px;color:var(--text);line-height:1.7;margin-bottom:16px;}
    .testi-author{display:flex;align-items:center;gap:10px;}
    .testi-avatar{width:36px;height:36px;border-radius:50%;
                  display:flex;align-items:center;justify-content:center;
                  font-size:15px;font-weight:700;color:var(--bg);}
    .testi-name{font-size:13px;font-weight:600;}
    .testi-role{font-size:11px;color:var(--muted);}

    /* ── PRICING ── */
    .pricing-wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
                  gap:16px;margin-top:48px;max-width:1000px;margin-left:auto;margin-right:auto;}
    .plan-card{background:var(--card);border:1px solid var(--border);border-radius:16px;
               padding:32px;position:relative;transition:transform .2s,border-color .2s;}
    .plan-card:hover{transform:translateY(-4px);}
    .plan-card.popular{border-color:var(--gold);
                       background:linear-gradient(160deg,#1C1A2E 0%,#12122A 60%);}
    .popular-badge{position:absolute;top:-13px;left:50%;transform:translateX(-50%);
                   background:var(--gold);color:#07070F;font-size:10px;font-weight:700;
                   letter-spacing:2px;padding:4px 18px;border-radius:100px;
                   white-space:nowrap;text-transform:uppercase;}
    .plan-name{font-size:12px;font-weight:700;color:var(--muted);letter-spacing:2px;
               text-transform:uppercase;margin-bottom:12px;}
    .plan-price{font-family:'Bebas Neue',sans-serif;font-size:56px;letter-spacing:1px;line-height:1;}
    .plan-price span{font-family:'Plus Jakarta Sans',sans-serif;font-size:16px;
                     color:var(--muted);vertical-align:middle;margin-left:4px;}
    .plan-period{font-size:13px;color:var(--muted);margin-top:4px;margin-bottom:24px;}
    .save-tag{background:rgba(245,166,35,.12);color:var(--gold);font-size:11px;
              font-weight:600;padding:2px 10px;border-radius:100px;margin-left:8px;}
    .plan-divider{height:1px;background:var(--border);margin:20px 0;}
    .plan-features{list-style:none;margin-bottom:28px;}
    .plan-features li{font-size:13px;color:var(--muted);padding:6px 0;
                      display:flex;align-items:center;gap:10px;}
    .plan-features li::before{content:"";width:16px;height:16px;border-radius:50%;flex-shrink:0;
      background:var(--gold-dim);border:1px solid rgba(245,166,35,.3);
      background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'%3E%3Cpath d='M2 5l2 2 4-4' stroke='%23F5A623' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
      background-size:10px;background-position:center;background-repeat:no-repeat;}
    .plan-btn{width:100%;padding:14px;border-radius:8px;
              font-family:'Plus Jakarta Sans',sans-serif;font-size:14px;font-weight:700;
              cursor:pointer;border:none;transition:transform .15s,box-shadow .15s;}
    .plan-btn:hover{transform:translateY(-2px);}
    .plan-btn.default{background:rgba(255,255,255,.05);color:var(--text);border:1px solid var(--border2);}
    .plan-btn.default:hover{background:rgba(255,255,255,.09);}
    .plan-btn.primary{background:var(--gold);color:#07070F;
                      box-shadow:0 6px 20px rgba(245,166,35,.25);}
    .plan-btn.primary:hover{box-shadow:0 12px 32px rgba(245,166,35,.4);}

    /* ── FAQ ── */
    .faq-list{margin-top:40px;max-width:720px;}
    .faq-item{border-bottom:1px solid var(--border);overflow:hidden;}
    .faq-q{width:100%;background:none;border:none;color:var(--text);
           font-family:'Plus Jakarta Sans',sans-serif;font-size:15px;font-weight:500;
           padding:20px 0;text-align:left;cursor:pointer;
           display:flex;justify-content:space-between;align-items:center;gap:12px;}
    .faq-icon{width:22px;height:22px;border-radius:50%;border:1px solid var(--border2);
              flex-shrink:0;display:flex;align-items:center;justify-content:center;
              font-size:14px;color:var(--muted);transition:transform .2s,border-color .2s;}
    .faq-item.open .faq-icon{transform:rotate(45deg);border-color:var(--gold);color:var(--gold);}
    .faq-a{font-size:14px;color:var(--muted);line-height:1.8;
           max-height:0;overflow:hidden;transition:max-height .3s ease,padding .3s;padding-bottom:0;}
    .faq-item.open .faq-a{max-height:300px;padding-bottom:20px;}

    /* ── FOOTER ── */
    footer{border-top:1px solid var(--border);padding:40px 5%;}
    .footer-inner{max-width:1200px;margin:0 auto;
                  display:flex;align-items:center;justify-content:space-between;
                  flex-wrap:wrap;gap:16px;}
    .footer-copy{font-size:13px;color:var(--muted2);}
    .footer-links{display:flex;gap:24px;}
    .footer-links a{font-size:13px;color:var(--muted);text-decoration:none;transition:color .2s;}
    .footer-links a:hover{color:var(--text);}

    /* ── MODAL ── */
    .modal-overlay{display:none;position:fixed;inset:0;z-index:200;
                   background:rgba(0,0,0,.8);backdrop-filter:blur(6px);
                   align-items:center;justify-content:center;padding:20px;}
    .modal-overlay.show{display:flex;}
    .modal{background:var(--card);border:1px solid var(--border2);
           border-radius:20px;padding:40px;width:100%;max-width:460px;
           animation:modalIn .2s ease;}
    @keyframes modalIn{from{transform:scale(.95) translateY(12px);opacity:0}to{transform:none;opacity:1}}
    .modal-title{font-family:'Bebas Neue',sans-serif;font-size:28px;letter-spacing:1px;margin-bottom:4px;}
    .modal-sub{font-size:14px;color:var(--muted);margin-bottom:24px;}
    .modal label{display:block;font-size:11px;font-weight:700;color:var(--muted);
                 letter-spacing:1.5px;text-transform:uppercase;margin-bottom:6px;}
    .modal input{width:100%;padding:13px 16px;background:rgba(255,255,255,.04);
                 border:1px solid var(--border2);border-radius:8px;color:var(--text);
                 font-family:'Plus Jakarta Sans',sans-serif;font-size:14px;
                 margin-bottom:14px;outline:none;transition:border-color .2s;}
    .modal input:focus{border-color:rgba(245,166,35,.6);}
    .modal input::placeholder{color:var(--muted2);}
    .modal-note{font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:20px;
                padding:12px 14px;background:rgba(255,255,255,.03);
                border-radius:8px;border-left:2px solid var(--gold);}
    .btn-pay{width:100%;padding:16px;background:var(--gold);color:#07070F;border:none;
             border-radius:8px;font-family:'Plus Jakarta Sans',sans-serif;
             font-size:15px;font-weight:700;cursor:pointer;
             display:flex;align-items:center;justify-content:center;gap:8px;
             transition:transform .15s,box-shadow .15s;}
    .btn-pay:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(245,166,35,.35);}
    .btn-cancel{width:100%;padding:12px;margin-top:10px;background:none;border:none;
                color:var(--muted);font-family:'Plus Jakarta Sans',sans-serif;
                font-size:13px;cursor:pointer;transition:color .2s;}
    .btn-cancel:hover{color:var(--text);}
    #modal-msg{margin-top:12px;text-align:center;font-size:12px;font-weight:600;min-height:20px;}
    #modal-msg.err{color:#FF4757;}
    #modal-msg.ok{color:var(--green);}

    @keyframes fadeUp{from{opacity:0;transform:translateY(24px)}to{opacity:1;transform:none}}
    .anim{animation:fadeUp .7s ease both;}

    @media(max-width:900px){
      .hero-inner{grid-template-columns:1fr;}
      .hero-right{display:none;}
      .screenshots-grid{grid-template-columns:1fr;}
      .dl-inner{grid-template-columns:1fr;text-align:center;}
      .dl-left p{margin:0 auto;}
      .dl-meta{justify-content:center;}
    }
    @media(max-width:600px){
      .hero-btns{flex-direction:column;}
      .counter-inner{gap:40px;}
      footer .footer-inner{flex-direction:column;text-align:center;}
      .modal{padding:28px 20px;}
      .steps-wrap::before{display:none;}
    }
  </style>
</head>
<body>

<!-- ── NAV ── -->
<nav>
  <a href="/" class="logo">🎬 AutoCapCut <span class="logo-badge">v2.1</span></a>
  <div class="nav-links">
    <a href="#features" class="nav-link">Tính năng</a>
    <a href="#pricing"  class="nav-link">Bảng giá</a>
    <a href="#download" class="nav-link">Tải xuống</a>
    <a href="{{ support_url }}" target="_blank" class="nav-link">Hỗ trợ</a>
    <a href="#download" class="nav-dl-btn">⬇ Tải miễn phí</a>
  </div>
</nav>

<!-- ── HERO ── -->
<section class="hero">
  <div class="hero-glow"></div>
  <div class="hero-glow2"></div>
  <div class="hero-inner">
    <div class="hero-left anim">
      <div class="hero-tag">
        <span class="hero-tag-dot"></span>
        Phiên bản 2.1 — Mới nhất
      </div>
      <h1>TỰ ĐỘNG HOÁ<br>QUY TRÌNH <em>EDIT VIDEO</em><br>TRÊN CAPCUT</h1>
      <p class="hero-desc">
        Chỉ cần <strong>video gốc + audio + file .srt</strong> — phần mềm tự động cắt, ghép,
        điều chỉnh tốc độ và tạo Draft hoàn chỉnh trong CapCut.
        Không cần kéo tay từng đoạn, <strong>tiết kiệm hàng giờ mỗi ngày.</strong>
      </p>
      <div class="hero-btns">
        <a href="#download" class="btn-gold">
          ⬇&nbsp; Tải xuống miễn phí
        </a>
        <a href="#pricing" class="btn-outline">
          💳&nbsp; Xem bảng giá
        </a>
      </div>
      <div class="hero-trust">
        <div class="trust-item"><span class="trust-dot"></span>Windows 10/11</div>
        <div class="trust-item"><span class="trust-dot"></span>CapCut 3.9 → 7.3</div>
        <div class="trust-item"><span class="trust-dot"></span>Không cần code</div>
        <div class="trust-item"><span class="trust-dot"></span>1 lần setup, dùng mãi</div>
      </div>
    </div>
    <div class="hero-right anim" style="animation-delay:.15s">
      <div class="screenshot-wrap">
        <img src="https://i.imgur.com/placeholder_gui.png"
             onerror="this.style.display='none';this.parentNode.style.background='var(--card)';this.parentNode.style.minHeight='420px';"
             alt="Giao diện Auto CapCut Video Sync">
        <div class="screenshot-badge">● LIVE</div>
      </div>
      <div class="float-card float-card-1">
        <div class="fc-num" id="cnt-users">500+</div>
        <div class="fc-lbl">Người dùng</div>
      </div>
      <div class="float-card float-card-2">
        <div class="fc-num">5X</div>
        <div class="fc-lbl">Nhanh hơn edit tay</div>
      </div>
    </div>
  </div>
</section>

<!-- ── COUNTER STRIP ── -->
<div class="counter-strip">
  <div class="counter-inner">
    <div class="counter-item">
      <div class="counter-num" data-target="500">0</div>
      <div class="counter-label">Người dùng tin tưởng</div>
    </div>
    <div class="counter-item">
      <div class="counter-num" data-target="50000">0</div>
      <div class="counter-label">Clips đã xử lý</div>
    </div>
    <div class="counter-item">
      <div class="counter-num" data-target="5">0</div>
      <div class="counter-label">Lần nhanh hơn edit tay</div>
    </div>
    <div class="counter-item">
      <div class="counter-num" data-target="14">0</div>
      <div class="counter-label">Phiên bản CapCut hỗ trợ</div>
    </div>
  </div>
</div>

<!-- ── FEATURES ── -->
<section class="section" id="features">
  <div class="section-inner">
    <div class="section-label">Tính năng</div>
    <div class="section-title">MỌI THỨ BẠN CẦN<br>ĐỂ EDIT NHANH HƠN</div>
    <p class="section-sub">Từ video gốc đến CapCut Draft hoàn chỉnh — tất cả tự động, không cần động tay.</p>
    <div class="features-grid">
      <div class="feature-card">
        <div class="feature-icon">🎞️</div>
        <div class="feature-title">Auto cắt clip theo SRT</div>
        <div class="feature-desc">Đọc file phụ đề .srt, tự động cắt video đúng từng mốc thời gian. Không cần kéo thanh timeline thủ công từng đoạn.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🔊</div>
        <div class="feature-title">Ghép audio thông minh</div>
        <div class="feature-desc">Tự động điều chỉnh tốc độ video khớp với độ dài audio. Không bị lệch tiếng, không cần render lại từng clip.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon">⚡</div>
        <div class="feature-title">Compound Clip tự động</div>
        <div class="feature-desc">Gộp tất cả clip thành Compound Clip chỉ với 1 tham số. Hỗ trợ Video, Audio và Mixed compound.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon">📂</div>
        <div class="feature-title">Ghi thẳng vào CapCut</div>
        <div class="feature-desc">Draft xuất hiện ngay trong CapCut Projects, không cần copy hay import thêm bước nào.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon">🔧</div>
        <div class="feature-title">Hỗ trợ 14 phiên bản CapCut</div>
        <div class="feature-desc">Từ CapCut 3.9 đến 7.3, tương thích tất cả phiên bản phổ biến đang dùng trên thị trường.</div>
      </div>
      <div class="feature-card">
        <div class="feature-icon">📝</div>
        <div class="feature-title">Auto subtitle</div>
        <div class="feature-desc">Tự động thêm text segment vào timeline theo nội dung file SRT. Chỉnh sửa font, màu sắc sau trong CapCut.</div>
      </div>
    </div>
  </div>
</section>

<!-- ── SCREENSHOTS ── -->
<section class="section" style="padding-top:0" id="screenshots">
  <div class="section-inner">
    <div class="section-label">Giao diện</div>
    <div class="section-title">TRỰC QUAN,<br>DỄ SỬ DỤNG</div>
    <p class="section-sub">Thiết kế giao diện gọn gàng — mọi thao tác trong tầm tay, không cần đọc hướng dẫn dài.</p>
    <div class="screenshots-grid">
      <div class="shot-card">
        <div style="background:var(--surface);min-height:280px;display:flex;align-items:center;
                    justify-content:center;font-size:13px;color:var(--muted2);padding:40px;text-align:center;">
          📸 Screenshot tab Tệp Draft<br>
          <small style="font-size:11px;margin-top:8px;display:block;">
            (Thay bằng ảnh thực tế của phần mềm)
          </small>
        </div>
        <div class="shot-caption"><strong>Tab Tệp Draft</strong> — Quản lý video, audio và SRT đầu vào</div>
      </div>
      <div class="shot-card">
        <div style="background:var(--surface);min-height:280px;display:flex;align-items:center;
                    justify-content:center;font-size:13px;color:var(--muted2);padding:40px;text-align:center;">
          📸 Screenshot tab Cấu hình<br>
          <small style="font-size:11px;margin-top:8px;display:block;">
            (Thay bằng ảnh thực tế của phần mềm)
          </small>
        </div>
        <div class="shot-caption"><strong>Tab Cấu hình</strong> — Chọn phiên bản CapCut, thư mục Draft</div>
      </div>
    </div>
  </div>
</section>

<!-- ── HOW IT WORKS ── -->
<section class="section" style="padding-top:0">
  <div class="section-inner">
    <div class="section-label">Quy trình</div>
    <div class="section-title">CHỈ 3 BƯỚC<br>ĐỂ CÓ DRAFT HOÀN CHỈNH</div>
    <div class="steps-wrap">
      <div class="step">
        <div class="step-num">1</div>
        <div class="step-title">Chuẩn bị file</div>
        <div class="step-desc">Đặt video gốc, thư mục audio từng đoạn và file phụ đề <code>.srt</code> vào thư mục <code>inputs/</code></div>
      </div>
      <div class="step">
        <div class="step-num">2</div>
        <div class="step-title">Nhấn Bắt đầu</div>
        <div class="step-desc">Mở phần mềm → chọn file → nhấn <code>BẮT ĐẦU CHẠY</code> và chờ vài giây</div>
      </div>
      <div class="step">
        <div class="step-num">3</div>
        <div class="step-title">Mở CapCut</div>
        <div class="step-desc">Draft hoàn chỉnh xuất hiện ngay trong CapCut Projects — chỉnh sửa thêm hoặc xuất ngay</div>
      </div>
    </div>
  </div>
</section>

<!-- ── DOWNLOAD ── -->
<section class="download-section" id="download">
  <div class="dl-inner">
    <div class="dl-left">
      <div class="section-label" style="margin-bottom:8px;">Tải xuống</div>
      <h2>DÙNG THỬ <em>MIỄN PHÍ</em><br>NGAY HÔM NAY</h2>
      <p>Tải về, cài đặt trong 1 phút. Dùng thử không cần key — nhập key để mở khoá toàn bộ tính năng sau khi mua license.</p>
      <div class="dl-meta">
        <div class="dl-meta-item">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/></svg>
          Windows 10/11 (64-bit)
        </div>
        <div class="dl-meta-item">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/></svg>
          Phiên bản {{ app_version }}
        </div>
        <div class="dl-meta-item">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/></svg>
          {{ app_size }}
        </div>
      </div>
      <div class="req-list" style="margin-top:24px;">
        <div class="req-item"><div class="req-icon">⚡</div>FFmpeg đã cài trong PATH</div>
        <div class="req-item"><div class="req-icon">🎬</div>CapCut PC đã cài đặt</div>
        <div class="req-item"><div class="req-icon">🔑</div>License key (mua bên dưới)</div>
      </div>
    </div>
    <div class="dl-right">
      <a href="{{ download_url }}" target="_blank" class="dl-btn">
        <span>⬇&nbsp; Tải xuống</span>
        <span class="dl-btn-sub">Google Drive · {{ app_size }}</span>
      </a>
      <div class="dl-note">Miễn phí tải · Cần license để dùng đầy đủ</div>
      <div style="margin-top:20px;font-size:12px;color:var(--muted2);">
        Sau khi tải: giải nén → chạy<br>
        <code style="background:var(--card);padding:2px 8px;border-radius:4px;
                     font-size:11px;">AutoCapCutVideoSync.exe</code>
      </div>
    </div>
  </div>
</section>

<!-- ── TESTIMONIALS ── -->
<section class="section" id="reviews">
  <div class="section-inner">
    <div class="section-label">Đánh giá</div>
    <div class="section-title">KHÁCH HÀNG<br>NÓI GÌ VỀ CHÚNG TÔI?</div>
    <div class="testi-grid">
      <div class="testi-card">
        <div class="testi-stars">★★★★★</div>
        <div class="testi-text">"Trước đây mất 3-4 tiếng để ghép 50 clip, giờ chạy xong trong 5 phút. Tool này thật sự thay đổi quy trình làm việc của mình."</div>
        <div class="testi-author">
          <div class="testi-avatar" style="background:linear-gradient(135deg,#667eea,#764ba2);">T</div>
          <div>
            <div class="testi-name">Tuấn Anh</div>
            <div class="testi-role">Content Creator · TikTok 500K followers</div>
          </div>
        </div>
      </div>
      <div class="testi-card">
        <div class="testi-stars">★★★★★</div>
        <div class="testi-text">"Cài đặt cực đơn giản, chạy ổn định. Mình dùng cho kênh review phim, mỗi ngày xử lý cả trăm clips không bao giờ lỗi."</div>
        <div class="testi-author">
          <div class="testi-avatar" style="background:linear-gradient(135deg,#f093fb,#f5576c);">M</div>
          <div>
            <div class="testi-name">Minh Hằng</div>
            <div class="testi-role">Video Editor · Agency</div>
          </div>
        </div>
      </div>
      <div class="testi-card">
        <div class="testi-stars">★★★★☆</div>
        <div class="testi-text">"Compound Clip tự động là tính năng mình chờ đợi mãi. Tiết kiệm rất nhiều thời gian sau khi tool ghép xong timeline."</div>
        <div class="testi-author">
          <div class="testi-avatar" style="background:linear-gradient(135deg,#4facfe,#00f2fe);">H</div>
          <div>
            <div class="testi-name">Hoàng Nam</div>
            <div class="testi-role">Freelance Editor</div>
          </div>
        </div>
      </div>
    </div>
  </div>
</section>

<!-- ── PRICING ── -->
<section class="section" id="pricing" style="padding-top:0">
  <div class="section-inner">
    <div style="text-align:center;margin-bottom:0;">
      <div class="section-label" style="text-align:center;">Bảng giá</div>
      <div class="section-title" style="text-align:center;">CHỌN GÓI<br>PHÙ HỢP VỚI BẠN</div>
      <p style="color:var(--muted);font-size:14px;margin-top:8px;">
        Mua 1 lần · Dùng trên 1 máy · Thời hạn tính từ lần kích hoạt đầu tiên
      </p>
    </div>
    <div class="pricing-wrap">
      <div class="plan-card">
        <div class="plan-name">Starter</div>
        <div class="plan-price">99K <span>VND</span></div>
        <div class="plan-period">30 ngày</div>
        <div class="plan-divider"></div>
        <ul class="plan-features">
          <li>Tất cả tính năng đầy đủ</li>
          <li>1 máy tính</li>
          <li>Cập nhật trong thời hạn</li>
          <li>Hỗ trợ qua Telegram</li>
        </ul>
        <button class="plan-btn default" onclick="openModal(30,99000)">Mua gói 30 ngày</button>
      </div>
      <div class="plan-card popular">
        <div class="popular-badge">Phổ biến nhất</div>
        <div class="plan-name" style="color:var(--gold);">Creator</div>
        <div class="plan-price">249K <span>VND</span></div>
        <div class="plan-period">90 ngày <span class="save-tag">Tiết kiệm 48K</span></div>
        <div class="plan-divider" style="background:rgba(245,166,35,.15);"></div>
        <ul class="plan-features">
          <li>Tất cả tính năng đầy đủ</li>
          <li>1 máy tính</li>
          <li>Cập nhật trong thời hạn</li>
          <li>Hỗ trợ ưu tiên</li>
        </ul>
        <button class="plan-btn primary" onclick="openModal(90,249000)">Mua gói 90 ngày</button>
      </div>
      <div class="plan-card">
        <div class="plan-name">Pro</div>
        <div class="plan-price">799K <span>VND</span></div>
        <div class="plan-period">365 ngày <span class="save-tag">Tiết kiệm 389K</span></div>
        <div class="plan-divider"></div>
        <ul class="plan-features">
          <li>Tất cả tính năng đầy đủ</li>
          <li>1 máy tính</li>
          <li>Cập nhật trong thời hạn</li>
          <li>Hỗ trợ ưu tiên</li>
          <li>Truy cập tính năng beta</li>
        </ul>
        <button class="plan-btn default" onclick="openModal(365,799000)">Mua gói 365 ngày</button>
      </div>
    </div>
  </div>
</section>

<!-- ── FAQ ── -->
<section class="section" style="padding-top:0">
  <div class="section-inner">
    <div class="section-label">FAQ</div>
    <div class="section-title">CÂU HỎI<br>THƯỜNG GẶP</div>
    <div class="faq-list">
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">
          Phần mềm chạy trên hệ điều hành nào?
          <span class="faq-icon">+</span>
        </button>
        <div class="faq-a">Hiện tại hỗ trợ Windows 10/11 (64-bit). Cần cài sẵn CapCut PC phiên bản từ 3.9 đến 7.3 và FFmpeg. macOS đang trong quá trình phát triển.</div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">
          Machine ID là gì? Lấy ở đâu?
          <span class="faq-icon">+</span>
        </button>
        <div class="faq-a">Machine ID là mã định danh máy tính của bạn, dùng để khóa key với đúng máy đó. Cách lấy: mở phần mềm → màn hình kích hoạt → copy dãy ký tự XXXX-XXXX-XXXX-XXXX hiển thị bên dưới ô nhập key, rồi dán vào khi mua.</div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">
          Thời hạn tính từ lúc nào?
          <span class="faq-icon">+</span>
        </button>
        <div class="faq-a">Thời hạn tính từ lần đầu tiên bạn nhập key vào phần mềm và bấm Kích hoạt — không phải từ lúc thanh toán. Bạn có thể mua trước, kích hoạt khi cần dùng.</div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">
          Sau khi mua nhận key như thế nào?
          <span class="faq-icon">+</span>
        </button>
        <div class="faq-a">Key License được gửi tự động về email bạn nhập khi thanh toán, thường trong vòng 1–2 phút. Nếu không thấy, kiểm tra thư mục Spam.</div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">
          Tôi có thể dùng trên nhiều máy không?
          <span class="faq-icon">+</span>
        </button>
        <div class="faq-a">Mỗi license chỉ dùng được trên 1 máy. Nếu cần chuyển sang máy khác (thay máy, cài lại Windows), liên hệ hỗ trợ qua Telegram để được reset miễn phí.</div>
      </div>
      <div class="faq-item">
        <button class="faq-q" onclick="toggleFaq(this)">
          Cài FFmpeg như thế nào?
          <span class="faq-icon">+</span>
        </button>
        <div class="faq-a">Tải FFmpeg tại ffmpeg.org → giải nén → thêm vào PATH của Windows. Hoặc liên hệ hỗ trợ để được hướng dẫn chi tiết qua Telegram.</div>
      </div>
    </div>
  </div>
</section>

<!-- ── FOOTER ── -->
<footer>
  <div class="footer-inner">
    <div>
      <div class="footer-copy">© 2026 Auto CapCut Video Sync by Văn Khải</div>
      <div style="font-size:12px;color:var(--muted2);margin-top:4px;">
        Phần mềm hỗ trợ content creator Việt Nam tự động hoá edit video
      </div>
    </div>
    <div class="footer-links">
      <a href="#download">Tải xuống</a>
      <a href="#pricing">Bảng giá</a>
      <a href="{{ support_url }}" target="_blank">Hỗ trợ</a>
    </div>
  </div>
</footer>

<!-- ── MODAL ── -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-title" id="modal-title">MUA LICENSE</div>
    <div class="modal-sub" id="modal-sub">Nhập thông tin để nhận key kích hoạt</div>

    <label>Email nhận key</label>
    <input type="email" id="modal-email" placeholder="example@gmail.com">

    <label>Machine ID <span style="color:var(--gold)">*</span></label>
    <input type="text" id="modal-machine"
           placeholder="XXXX-XXXX-XXXX-XXXX"
           style="font-family:Consolas,monospace;font-size:14px;letter-spacing:2px;"
           oninput="this.value=this.value.toUpperCase()">

    <div class="modal-note">
      🖥️ <b>Lấy Machine ID:</b> Mở phần mềm → màn hình kích hoạt → copy dãy ký tự bên dưới.<br><br>
      ⏱️ <b>Thời hạn tính từ lần kích hoạt đầu tiên</b> — không phải từ lúc thanh toán.<br><br>
      🔒 Key bị khóa cứng với máy này sau khi kích hoạt.
    </div>

    <button class="btn-pay" id="btn-pay" onclick="submitPayment()">
      💳&nbsp; Thanh toán ngay
    </button>
    <button class="btn-cancel" onclick="closeModal()">Huỷ</button>
    <div id="modal-msg"></div>
  </div>
</div>

<script>
/* ── Counter animation ── */
function animateCounters() {
  document.querySelectorAll('.counter-num[data-target]').forEach(el => {
    const target = parseInt(el.dataset.target);
    const dur    = 2000;
    const step   = 16;
    const inc    = target / (dur / step);
    let cur = 0;
    const t = setInterval(() => {
      cur = Math.min(cur + inc, target);
      el.textContent = target >= 1000
        ? Math.floor(cur).toLocaleString('vi-VN') + '+'
        : Math.floor(cur) + (target > 5 ? '+' : 'X');
      if (cur >= target) clearInterval(t);
    }, step);
  });
}
const observer = new IntersectionObserver(entries => {
  entries.forEach(e => { if (e.isIntersecting) { animateCounters(); observer.disconnect(); }});
}, {threshold: 0.3});
observer.observe(document.querySelector('.counter-strip'));

/* ── Modal ── */
let _days = 30;
function openModal(days, amount) {
  _days = days;
  const labels = {30:'30 ngày — 99.000₫', 90:'90 ngày — 249.000₫', 365:'365 ngày — 799.000₫'};
  document.getElementById('modal-title').textContent = 'MUA ' + days + ' NGÀY';
  document.getElementById('modal-sub').textContent   = labels[days];
  document.getElementById('modal-msg').textContent   = '';
  document.getElementById('modal-msg').className     = '';
  document.getElementById('modal-email').value       = '';
  document.getElementById('modal-machine').value     = '';
  document.getElementById('modal').classList.add('show');
}
function closeModal() {
  document.getElementById('modal').classList.remove('show');
}
function isValidMachineId(v) {
  return /^[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}$/.test(v.trim());
}
async function submitPayment() {
  const email     = document.getElementById('modal-email').value.trim();
  const machineId = document.getElementById('modal-machine').value.trim().toUpperCase();
  const msg       = document.getElementById('modal-msg');
  const btn       = document.getElementById('btn-pay');
  msg.className = ''; msg.textContent = '';
  if (!email || !email.includes('@')) {
    msg.className = 'err'; msg.textContent = '❌ Vui lòng nhập email hợp lệ.'; return;
  }
  if (!machineId) {
    msg.className = 'err'; msg.textContent = '❌ Vui lòng nhập Machine ID.'; return;
  }
  if (!isValidMachineId(machineId)) {
    msg.className = 'err';
    msg.textContent = '❌ Machine ID sai định dạng (XXXX-XXXX-XXXX-XXXX).'; return;
  }
  btn.textContent = 'Đang xử lý...'; btn.disabled = true;
  try {
    const res  = await fetch('/payment/create', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ email, days: _days, machine_id: machineId })
    });
    const data = await res.json();
    if (data.checkout_url) {
      window.location.href = data.checkout_url;
    } else {
      msg.className = 'err';
      msg.textContent = '❌ ' + (data.error || 'Không tạo được link thanh toán');
      btn.textContent = '💳 Thanh toán ngay'; btn.disabled = false;
    }
  } catch(e) {
    msg.className = 'err'; msg.textContent = '❌ Lỗi kết nối: ' + e;
    btn.textContent = '💳 Thanh toán ngay'; btn.disabled = false;
  }
}
function toggleFaq(btn) {
  btn.closest('.faq-item').classList.toggle('open');
}
document.getElementById('modal').addEventListener('click', function(e) {
  if (e.target === this) closeModal();
});

/* ── Scroll fade-in ── */
const fadeObs = new IntersectionObserver(entries => {
  entries.forEach(e => {
    if (e.isIntersecting) { e.target.style.opacity='1'; e.target.style.transform='none'; }
  });
}, {threshold:0.1});
document.querySelectorAll('.feature-card,.testi-card,.plan-card,.shot-card').forEach(el => {
  el.style.opacity = '0';
  el.style.transform = 'translateY(20px)';
  el.style.transition = 'opacity .6s ease, transform .6s ease';
  fadeObs.observe(el);
});
</script>
</body>
</html>
"""


@app.route("/")
def shop():
    return render_template_string(
        SHOP_HTML,
        support_url=SUPPORT_URL,
        download_url=DOWNLOAD_URL,
        app_version=APP_VERSION,
        app_size=APP_SIZE,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
