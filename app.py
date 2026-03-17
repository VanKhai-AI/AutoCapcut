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
                name         TEXT DEFAULT '',
                phone        TEXT DEFAULT '',
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
def _upsert_customer(email: str, name: str = "", phone: str = "",
                     source: str = "", notes: str = ""):
    """
    Tạo mới hoặc cập nhật bản ghi khách hàng theo email.
    Không ghi đè name/phone/notes nếu đã có và tham số truyền vào rỗng.
    """
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        existing = conn.execute(
            "SELECT * FROM customers WHERE email=?", (email,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO customers (email, name, phone, source, notes, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (email, name, phone, source, notes, now, now),
            )
            log.info("Tạo customer mới: %s", email)
        else:
            # Chỉ cập nhật các trường không rỗng
            updates, params = [], []
            if name  and not existing["name"]:
                updates.append("name=?");   params.append(name)
            if phone and not existing["phone"]:
                updates.append("phone=?");  params.append(phone)
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
      <input type="email" id="inp-email" placeholder="Email khách hàng" style="min-width:220px;flex:1;">
      <input type="text" id="inp-machine"
             placeholder="Machine ID (tuỳ chọn)"
             style="width:230px;font-family:Consolas,monospace;font-size:13px;">
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

  <!-- Tabs: Licenses / Orders -->
  <div class="panel">
    <div class="tab-btns">
      <button class="tab-btn active" onclick="switchTab('licenses', this)">
        Licenses ({{ licenses|length }})
      </button>
      <button class="tab-btn" onclick="switchTab('orders', this)">
        Orders ({{ orders|length }})
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
    msg.className = 'err'; msg.textContent = '❌ Machine ID sai định dạng (VD: A1B2-C3D4-E5F6-G7H8).'; return;
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
        : '🔓 Chờ kích hoạt (expire tính từ lúc KH)';
      msg.className = 'ok';
      msg.innerHTML = `✅ Key: <b class="key-mono">${data.key}</b>
        <button class="copy-btn" onclick="copyText('${data.key}')">copy</button>
        &nbsp;|&nbsp; ${data.email_sent ? '📧 Email đã gửi.' : '⚠️ Gửi email thất bại — copy key thủ công!'}
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
        active_count        = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE active=1"
        ).fetchone()[0]
        inactive_count      = conn.execute(
            "SELECT COUNT(*) FROM licenses WHERE active=0"
        ).fetchone()[0]
        orders_paid_count   = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='paid'"
        ).fetchone()[0]
        orders_pending_count = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE status='pending'"
        ).fetchone()[0]

    # Tính days_left cho mỗi license
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

    stats = {
        "active":         active_count,
        "expired":        inactive_count,
        "orders_paid":    orders_paid_count,
        "orders_pending": orders_pending_count,
    }
    return render_template_string(ADMIN_HTML,
                                  licenses=licenses,
                                  orders=orders,
                                  stats=stats)


@app.route("/admin/create_key", methods=["POST"])
@admin_required
def admin_create_key():
    data       = request.get_json(silent=True) or {}
    email      = (data.get("email") or "").strip().lower()
    days       = int(data.get("days", 30))
    note       = (data.get("note") or "").strip()
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
    :root{--bg:#080810;--card:#16162A;--border:rgba(255,255,255,0.07);
          --gold:#F5A623;--gold-dim:rgba(245,166,35,0.15);
          --text:#EDEAF4;--muted:#7A7898;--red:#FF4757;}
    *{box-sizing:border-box;margin:0;padding:0}
    html{scroll-behavior:smooth}
    body{font-family:'Plus Jakarta Sans',sans-serif;background:var(--bg);
         color:var(--text);line-height:1.6;overflow-x:hidden;}
    ::-webkit-scrollbar{width:6px}
    ::-webkit-scrollbar-track{background:var(--bg)}
    ::-webkit-scrollbar-thumb{background:#2A2A44;border-radius:3px}
    nav{position:fixed;top:0;left:0;right:0;z-index:100;
        display:flex;align-items:center;justify-content:space-between;
        padding:18px 5%;background:rgba(8,8,16,0.85);
        backdrop-filter:blur(12px);border-bottom:1px solid var(--border);}
    .logo{font-family:'Bebas Neue',sans-serif;font-size:22px;
          letter-spacing:2px;color:var(--text);display:flex;align-items:center;gap:10px;}
    .logo-badge{background:var(--gold);color:#080810;font-size:10px;
                font-weight:600;padding:2px 8px;border-radius:3px;letter-spacing:1px;}
    .nav-link{color:var(--muted);font-size:14px;text-decoration:none;transition:color .2s;}
    .nav-link:hover{color:var(--text)}
    .nav-right{display:flex;gap:20px}
    .hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;
          justify-content:center;text-align:center;padding:120px 20px 80px;
          position:relative;overflow:hidden;}
    .hero-glow{position:absolute;width:600px;height:600px;
               background:radial-gradient(circle,rgba(245,166,35,0.08) 0%,transparent 70%);
               top:50%;left:50%;transform:translate(-50%,-60%);pointer-events:none;}
    .hero-tag{display:inline-flex;align-items:center;gap:8px;
              border:1px solid rgba(245,166,35,0.3);background:rgba(245,166,35,0.07);
              padding:6px 16px;border-radius:100px;font-size:13px;color:var(--gold);
              margin-bottom:28px;animation:fadeUp .8s ease both;}
    .hero-tag-dot{width:6px;height:6px;border-radius:50%;background:var(--gold);
                  animation:pulse 2s ease infinite;}
    @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
    h1{font-family:'Bebas Neue',sans-serif;font-size:clamp(56px,9vw,110px);
       line-height:1;letter-spacing:2px;animation:fadeUp .8s .1s ease both;}
    h1 em{font-style:normal;color:var(--gold);}
    .hero-sub{max-width:540px;font-size:17px;color:var(--muted);
              margin:24px auto 40px;animation:fadeUp .8s .2s ease both;}
    .hero-cta{display:inline-flex;align-items:center;gap:10px;
              background:var(--gold);color:#080810;font-weight:600;font-size:16px;
              padding:16px 40px;border-radius:6px;border:none;cursor:pointer;
              text-decoration:none;transition:transform .2s,box-shadow .2s;
              animation:fadeUp .8s .3s ease both;}
    .hero-cta:hover{transform:translateY(-2px);box-shadow:0 12px 40px rgba(245,166,35,0.35)}
    .hero-stats{display:flex;gap:48px;margin-top:64px;animation:fadeUp .8s .4s ease both;}
    .hero-stat-num{font-family:'Bebas Neue',sans-serif;font-size:38px;letter-spacing:1px;}
    .hero-stat-label{font-size:13px;color:var(--muted)}
    .section{padding:80px 5%}
    .section-label{font-size:12px;font-weight:600;letter-spacing:3px;
                   color:var(--gold);text-transform:uppercase;margin-bottom:12px;}
    .section-title{font-family:'Bebas Neue',sans-serif;font-size:clamp(36px,5vw,56px);
                   letter-spacing:1px;line-height:1.1;margin-bottom:16px;}
    .section-sub{font-size:16px;color:var(--muted);max-width:500px;line-height:1.7}
    .features-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));
                   gap:16px;margin-top:48px;}
    .feature-card{background:var(--card);border:1px solid var(--border);border-radius:12px;
                  padding:28px;transition:border-color .2s,transform .2s;}
    .feature-card:hover{border-color:rgba(245,166,35,0.25);transform:translateY(-3px);}
    .feature-icon{width:44px;height:44px;border-radius:10px;background:var(--gold-dim);
                  display:flex;align-items:center;justify-content:center;
                  font-size:22px;margin-bottom:18px;}
    .feature-title{font-size:16px;font-weight:600;margin-bottom:8px}
    .feature-desc{font-size:14px;color:var(--muted);line-height:1.7}
    .pricing-wrap{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));
                  gap:16px;margin-top:48px;max-width:960px;margin-left:auto;margin-right:auto;}
    .plan-card{background:var(--card);border:1px solid var(--border);border-radius:16px;
               padding:32px;position:relative;transition:transform .2s,border-color .2s;}
    .plan-card:hover{transform:translateY(-4px)}
    .plan-card.popular{border-color:var(--gold);
                       background:linear-gradient(160deg,#1C1A2E 0%,#16162A 60%);}
    .popular-badge{position:absolute;top:-13px;left:50%;transform:translateX(-50%);
                   background:var(--gold);color:#080810;font-size:11px;font-weight:700;
                   letter-spacing:2px;padding:4px 20px;border-radius:100px;
                   white-space:nowrap;text-transform:uppercase;}
    .plan-name{font-size:14px;font-weight:600;color:var(--muted);letter-spacing:1px;
               text-transform:uppercase;margin-bottom:12px}
    .plan-price{font-family:'Bebas Neue',sans-serif;font-size:60px;letter-spacing:1px;
                line-height:1;}
    .plan-price span{font-family:'Plus Jakarta Sans',sans-serif;font-size:18px;
                     color:var(--muted);vertical-align:middle;margin-left:4px}
    .plan-period{font-size:13px;color:var(--muted);margin-top:4px;margin-bottom:24px}
    .plan-divider{height:1px;background:var(--border);margin:24px 0}
    .plan-features{list-style:none;margin-bottom:32px}
    .plan-features li{font-size:14px;color:var(--muted);padding:7px 0;
                      display:flex;align-items:center;gap:10px;}
    .plan-features li::before{content:"";width:16px;height:16px;border-radius:50%;flex-shrink:0;
      background:var(--gold-dim);border:1px solid rgba(245,166,35,.4);
      background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 10 10'%3E%3Cpath d='M2 5l2 2 4-4' stroke='%23F5A623' stroke-width='1.5' fill='none' stroke-linecap='round'/%3E%3C/svg%3E");
      background-size:10px;background-position:center;background-repeat:no-repeat;}
    .plan-btn{width:100%;padding:14px;border-radius:8px;
              font-family:'Plus Jakarta Sans',sans-serif;font-size:15px;font-weight:600;
              cursor:pointer;border:none;transition:transform .15s,box-shadow .15s;}
    .plan-btn:hover{transform:translateY(-2px)}
    .plan-btn.default{background:rgba(255,255,255,0.06);color:var(--text);
                      border:1px solid var(--border);}
    .plan-btn.default:hover{background:rgba(255,255,255,0.1)}
    .plan-btn.primary{background:var(--gold);color:#080810;
                      box-shadow:0 8px 24px rgba(245,166,35,0.25);}
    .plan-btn.primary:hover{box-shadow:0 12px 32px rgba(245,166,35,0.4)}
    .save-tag{display:inline-block;background:rgba(245,166,35,0.12);color:var(--gold);
              font-size:12px;font-weight:600;padding:3px 10px;border-radius:100px;margin-left:8px;}
    .steps{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));
           gap:0;margin-top:48px;position:relative;}
    .steps::before{content:"";position:absolute;top:28px;left:10%;right:10%;height:1px;
      background:linear-gradient(90deg,transparent,var(--border) 20%,var(--border) 80%,transparent);}
    .step{text-align:center;padding:0 20px;position:relative}
    .step-num{width:56px;height:56px;border-radius:50%;background:var(--card);
              border:1px solid var(--border);display:flex;align-items:center;
              justify-content:center;font-family:'Bebas Neue',sans-serif;
              font-size:22px;color:var(--gold);margin:0 auto 20px;
              position:relative;z-index:1;}
    .step-title{font-size:15px;font-weight:600;margin-bottom:8px}
    .step-desc{font-size:13px;color:var(--muted);line-height:1.6}
    .faq-list{margin-top:40px;max-width:700px}
    .faq-item{border-bottom:1px solid var(--border);overflow:hidden;}
    .faq-q{width:100%;background:none;border:none;color:var(--text);
           font-family:'Plus Jakarta Sans',sans-serif;font-size:15px;font-weight:500;
           padding:20px 0;text-align:left;cursor:pointer;
           display:flex;justify-content:space-between;align-items:center;gap:12px;}
    .faq-icon{width:20px;height:20px;border-radius:50%;border:1px solid var(--border);
              flex-shrink:0;display:flex;align-items:center;justify-content:center;
              font-size:14px;color:var(--muted);transition:transform .2s;}
    .faq-item.open .faq-icon{transform:rotate(45deg);border-color:var(--gold);color:var(--gold)}
    .faq-a{font-size:14px;color:var(--muted);line-height:1.7;
           max-height:0;overflow:hidden;transition:max-height .3s ease,padding .3s;padding-bottom:0;}
    .faq-item.open .faq-a{max-height:300px;padding-bottom:20px}
    footer{border-top:1px solid var(--border);padding:40px 5%;
           display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:16px;}
    .footer-copy{font-size:13px;color:var(--muted)}
    .footer-links a{font-size:13px;color:var(--muted);text-decoration:none;transition:color .2s}
    .footer-links a:hover{color:var(--text)}
    /* Modal */
    .modal-overlay{display:none;position:fixed;inset:0;z-index:200;
                   background:rgba(0,0,0,0.75);backdrop-filter:blur(4px);
                   align-items:center;justify-content:center;padding:20px;}
    .modal-overlay.show{display:flex}
    .modal{background:var(--card);border:1px solid rgba(255,255,255,0.1);
           border-radius:20px;padding:40px;width:100%;max-width:460px;
           animation:modalIn .25s ease;}
    @keyframes modalIn{from{transform:scale(.95) translateY(10px);opacity:0}to{transform:none;opacity:1}}
    .modal-title{font-family:'Bebas Neue',sans-serif;font-size:30px;letter-spacing:1px;margin-bottom:4px}
    .modal-sub{font-size:14px;color:var(--muted);margin-bottom:24px}
    .modal label{display:block;font-size:12px;font-weight:600;color:var(--muted);
                 letter-spacing:1px;text-transform:uppercase;margin-bottom:6px}
    .modal input{width:100%;padding:13px 16px;background:rgba(255,255,255,0.04);
                 border:1px solid var(--border);border-radius:8px;color:var(--text);
                 font-family:'Plus Jakarta Sans',sans-serif;font-size:15px;
                 margin-bottom:14px;outline:none;transition:border-color .2s;}
    .modal input:focus{border-color:rgba(245,166,35,0.5)}
    .modal input::placeholder{color:var(--muted)}
    .modal-note{font-size:12px;color:var(--muted);line-height:1.7;margin-bottom:20px;
                padding:12px 14px;background:rgba(255,255,255,0.03);
                border-radius:8px;border-left:2px solid var(--gold)}
    .btn-pay{width:100%;padding:16px;background:var(--gold);color:#080810;border:none;
             border-radius:8px;font-family:'Plus Jakarta Sans',sans-serif;
             font-size:16px;font-weight:700;cursor:pointer;
             transition:transform .15s,box-shadow .15s;
             display:flex;align-items:center;justify-content:center;gap:8px;}
    .btn-pay:hover{transform:translateY(-1px);box-shadow:0 8px 24px rgba(245,166,35,0.35)}
    .btn-cancel{width:100%;padding:12px;margin-top:10px;background:none;border:none;
                color:var(--muted);font-family:'Plus Jakarta Sans',sans-serif;
                font-size:14px;cursor:pointer;transition:color .2s;}
    .btn-cancel:hover{color:var(--text)}
    #modal-msg{margin-top:12px;text-align:center;font-size:14px;font-weight:600;min-height:20px;}
    #modal-msg.err{color:var(--red)}
    #modal-msg.ok{color:#4ade80}
    @keyframes fadeUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:none}}
    @media(max-width:600px){
      .hero-stats{gap:28px}.steps::before{display:none}
      footer{flex-direction:column;text-align:center}.modal{padding:28px 20px}
    }
  </style>
</head>
<body>

<nav>
  <div class="logo">🎬 AutoCapCut <span class="logo-badge">v2.1</span></div>
  <div class="nav-right">
    <a href="#pricing" class="nav-link">Bảng giá</a>
    <a href="{{ support_url }}" target="_blank" class="nav-link">Hỗ trợ</a>
  </div>
</nav>

<section class="hero">
  <div class="hero-glow"></div>
  <div class="hero-tag">
    <span class="hero-tag-dot"></span>
    Phiên bản 2.1 — Hỗ trợ Compound Clip
  </div>
  <h1>EDIT VIDEO<br><em>TỰ ĐỘNG HÓA</em></h1>
  <p class="hero-sub">Tự động ghép video, audio và subtitle vào CapCut Draft chỉ trong vài phút.</p>
  <a href="#pricing" class="hero-cta">
    Mua License ngay
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path d="M3 8h10M9 4l4 4-4 4" stroke="#080810" stroke-width="2"
            stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  </a>
  <div class="hero-stats">
    <div><div class="hero-stat-num">5X</div><div class="hero-stat-label">Nhanh hơn edit tay</div></div>
    <div><div class="hero-stat-num">1080P</div><div class="hero-stat-label">Full HD output</div></div>
    <div><div class="hero-stat-num">SRT</div><div class="hero-stat-label">Auto subtitle</div></div>
  </div>
</section>

<section class="section" id="features">
  <div class="section-label">Tính năng</div>
  <div class="section-title">MỌI THỨ BẠN CẦN<br>ĐỂ EDIT NHANH HƠN</div>
  <p class="section-sub">Từ video gốc đến CapCut Draft hoàn chỉnh — tất cả tự động.</p>
  <div class="features-grid">
    <div class="feature-card">
      <div class="feature-icon">🎞️</div>
      <div class="feature-title">Auto-cắt clip theo SRT</div>
      <div class="feature-desc">Đọc file subtitle .srt, tự động cắt video đúng thời điểm, không cần kéo tay từng đoạn.</div>
    </div>
    <div class="feature-card">
      <div class="feature-icon">🔊</div>
      <div class="feature-title">Ghép audio thông minh</div>
      <div class="feature-desc">Điều chỉnh tốc độ video theo độ dài audio. Không bị lệch tiếng, không cần render lại.</div>
    </div>
    <div class="feature-card">
      <div class="feature-icon">⚡</div>
      <div class="feature-title">Compound Clip tự động</div>
      <div class="feature-desc">Gộp tất cả clip thành Compound Clip chỉ với một tham số. Video/Audio/Mixed đều hỗ trợ.</div>
    </div>
    <div class="feature-card">
      <div class="feature-icon">📱</div>
      <div class="feature-title">Ghi thẳng vào CapCut</div>
      <div class="feature-desc">Không cần server, không cần API. Draft xuất hiện ngay trong CapCut Projects của bạn.</div>
    </div>
  </div>
</section>

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

<section class="section" id="pricing" style="padding-top:0">
  <div style="text-align:center">
    <div class="section-label">Bảng giá</div>
    <div class="section-title">CHỌN GÓI PHÙ HỢP</div>
    <p style="color:var(--muted);margin-top:8px">Một lần mua, dùng trên 1 máy tính. Không tính phí ẩn.</p>
  </div>
  <div class="pricing-wrap">
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
      <button class="plan-btn default" onclick="openModal(30,99000)">Mua gói 30 ngày</button>
    </div>
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
      <button class="plan-btn primary" onclick="openModal(90,249000)">Mua gói 90 ngày</button>
    </div>
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
      <button class="plan-btn default" onclick="openModal(365,799000)">Mua gói 365 ngày</button>
    </div>
  </div>
</section>

<section class="section" style="padding-top:0">
  <div class="section-label">FAQ</div>
  <div class="section-title">CÂU HỎI<br>THƯỜNG GẶP</div>
  <div class="faq-list">
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Phần mềm chạy trên hệ điều hành nào?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">Hiện tại hỗ trợ Windows 10/11 (64-bit), cần cài sẵn CapCut PC và FFmpeg.</div>
    </div>
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Machine ID là gì? Lấy ở đâu?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">
        Machine ID là mã định danh máy tính của bạn, dùng để khóa key với máy đó.
        Cách lấy: Mở phần mềm <b>Auto CapCut Video Sync</b> → màn hình kích hoạt sẽ hiển thị Machine ID ngay bên dưới ô nhập key.
        Copy dãy ký tự đó (định dạng XXXX-XXXX-XXXX-XXXX) rồi dán vào ô khi mua.
      </div>
    </div>
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Sau khi mua key được gửi về đâu?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">Key License gửi tự động đến email bạn nhập khi mua, thường trong vòng 1–2 phút. Nếu không thấy, hãy kiểm tra thư mục Spam.</div>
    </div>
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Thời hạn tính từ lúc nào?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">
        Thời hạn tính từ lần đầu tiên bạn nhập key vào phần mềm và bấm Kích hoạt — không phải từ lúc thanh toán.
        Bạn có thể mua trước, kích hoạt sau khi cần dùng.
      </div>
    </div>
    <div class="faq-item">
      <button class="faq-q" onclick="toggleFaq(this)">
        Tôi có thể dùng trên nhiều máy không?
        <span class="faq-icon">+</span>
      </button>
      <div class="faq-a">Mỗi license chỉ dùng được trên 1 máy. Nếu cần chuyển sang máy khác (thay máy, cài lại Windows), vui lòng liên hệ hỗ trợ qua Telegram.</div>
    </div>
  </div>
</section>

<footer>
  <div class="footer-copy">© 2026 Auto CapCut Video Sync</div>
  <div class="footer-links">
    <a href="{{ support_url }}" target="_blank">Hỗ trợ</a>
  </div>
</footer>

<!-- Modal thanh toán -->
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
      🖥️ <b>Lấy Machine ID:</b> Mở phần mềm → màn hình kích hoạt → copy dãy ký tự
      <b>XXXX-XXXX-XXXX-XXXX</b> hiển thị bên dưới ô nhập key.<br><br>
      ⏱️ <b>Thời hạn tính từ lần kích hoạt đầu tiên</b> — không phải từ lúc thanh toán.<br><br>
      🔒 Key bị khóa cứng với máy này sau khi kích hoạt.
    </div>

    <button class="btn-pay" id="btn-pay" onclick="submitPayment()">
      💳 Thanh toán ngay
    </button>
    <button class="btn-cancel" onclick="closeModal()">Huỷ</button>
    <div id="modal-msg"></div>
  </div>
</div>

<script>
let _days = 30;

function openModal(days, amount) {
  _days = days;
  const labels = {30:'30 ngày — 99.000₫', 90:'90 ngày — 249.000₫', 365:'365 ngày — 799.000₫'};
  document.getElementById('modal-title').textContent  = 'MUA ' + days + ' NGÀY';
  document.getElementById('modal-sub').textContent    = labels[days];
  document.getElementById('modal-msg').textContent    = '';
  document.getElementById('modal-msg').className      = '';
  document.getElementById('modal-email').value        = '';
  document.getElementById('modal-machine').value      = '';
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
    msg.textContent = '❌ Machine ID sai định dạng. Phải là XXXX-XXXX-XXXX-XXXX (chữ hoa + số).'; return;
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
</script>
</body>
</html>
"""


@app.route("/")
def shop():
    return render_template_string(SHOP_HTML, support_url=SUPPORT_URL)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
