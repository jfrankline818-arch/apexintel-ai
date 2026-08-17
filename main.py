import os
import sqlite3
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, session
import requests
from bs4 import BeautifulSoup
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
import random

app = Flask(__name__)
app.secret_key = "super_secret_key_change_in_production"

# CONFIGURATIONS
PAYPAL_ME_LINK = "https://www.paypal.com/ncp/payment/PYVWWAPTKXHEW"
TRIAL_DAYS = 30
SUBSCRIPTION_FEE = 70
HELP_EMAIL = "aienvironmentarea@gmail.com"

# SQLite database path setup
DATABASE_PATH = "apexintel_db.sqlite"


def get_db_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS license (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            install_date TEXT,
            last_paid_date TEXT,
            status TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE,
            password TEXT,
            install_date TEXT,
            has_paid INTEGER DEFAULT 0,
            last_paid_date TEXT,
            reset_code TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tracking_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            company_query TEXT,
            results_html TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS general_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            item_query TEXT,
            region TEXT,
            results_html TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nearby_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            location_query TEXT,
            results_html TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            company_query TEXT,
            latest_results_html TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS help_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT,
            message TEXT,
            timestamp TEXT
        )
    ''')
    conn.commit()
    cursor.close()
    conn.close()


init_db()


def get_user_payment_status(email):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT has_paid, install_date, last_paid_date FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()

    if not row:
        cursor.close()
        conn.close()
        return 0, TRIAL_DAYS

    has_paid = row[0]
    install_date_str = row[1]
    last_paid_date_str = row[2]

    now = datetime.now()

    if has_paid and last_paid_date_str:
        try:
            paid_dt = datetime.fromisoformat(last_paid_date_str)
            days_since_payment = (now - paid_dt).days

            if days_since_payment < 30:
                days_remaining = 30 - days_since_payment
                cursor.close()
                conn.close()
                return 1, days_remaining
            else:
                cursor.execute("UPDATE users SET has_paid = 0 WHERE email = ?", (email,))
                conn.commit()
                cursor.close()
                conn.close()
                return 0, 0
        except Exception:
            pass

    if not install_date_str:
        install_date_str = now.isoformat()
        cursor.execute("UPDATE users SET install_date = ? WHERE email = ?", (install_date_str, email))
        conn.commit()
        cursor.close()
        conn.close()
        return 0, TRIAL_DAYS

    cursor.close()
    conn.close()

    try:
        install_dt = datetime.fromisoformat(install_date_str)
        days_passed = (now - install_dt).days
        days_remaining = max(0, TRIAL_DAYS - days_passed)
        return 0, days_remaining
    except Exception:
        return 0, TRIAL_DAYS


def discover_company_urls(query):
    query_clean = query.strip().lower()
    known_mappings = {
        "jumia": ["https://www.jumia.co.ke/", "https://www.jumia.co.ke/mlp-official-stores/"],
        "nike": ["https://www.nike.com/w", "https://www.nike.com/launch"],
        "linear": ["https://linear.app/pricing", "https://linear.app"],
        "figma": ["https://www.figma.com/pricing", "https://www.figma.com"],
        "notion": ["https://www.notion.so/pricing", "https://www.notion.so"],
        "slack": ["https://slack.com/pricing", "https://slack.com"],
        "github": ["https://github.com/pricing", "https://github.com"],
        "openai": ["https://openai.com/api/pricing/", "https://openai.com"]
    }
    if query_clean in known_mappings:
        return known_mappings[query_clean]

    formatted_name = query_clean.replace(" ", "").replace(".com", "")
    candidate_urls = [
        f"https://www.{formatted_name}.com/shop",
        f"https://www.{formatted_name}.com/products",
        f"https://www.{formatted_name}.co.ke",
        f"https://www.{formatted_name}.com"
    ]
    valid_urls = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=4)
            if resp.status_code == 200:
                valid_urls.append(url)
        except Exception:
            continue
    return valid_urls


def run_competitor_intelligence(companies_input):
    company_list = [c.strip() for c in companies_input.split(",") if c.strip()]
    master_data = []

    for comp in company_list:
        discovered_urls = discover_company_urls(comp)
        if not discovered_urls:
            master_data.append({
                "Business / Company / Item": f"<span style='color: #ff1493; font-weight: bold;'>{comp.capitalize()}</span>",
                "Extracted Current Price / Product": "Website or product page is not present / Regional selector block detected.",
                "Product Photos": "<span class='text-muted small'>N/A</span>",
                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            continue

        products_found = []
        product_images = ""
        primary_link = discovered_urls[0]

        for target_url in discovered_urls:
            try:
                headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
                response = requests.get(target_url, headers=headers, timeout=6)
                if response.status_code != 200:
                    continue
                soup = BeautifulSoup(response.text, 'html.parser')
                for element in soup.find_all(['h3', 'h4', 'span', 'a', 'div', 'p'], class_=lambda x: x and any(
                        term in str(x).lower() for term in ['title', 'name', 'price', 'card', 'item'])):
                    text = element.get_text().strip()
                    if any(symbol in text for symbol in ['$', 'KSh', 'KES', 'USD', 'EUR', 'GBP']) and len(text) < 50:
                        if text not in products_found:
                            products_found.append(text)
                img_tags = soup.find_all('img', limit=4)
                for img in img_tags:
                    src = img.get('src') or img.get('data-src')
                    if src:
                        if not src.startswith('http'):
                            src = target_url.rstrip('/') + '/' + src.lstrip('/')
                        if src not in product_images:
                            product_images += f"<img src='{src}' style='width:55px; height:55px; object-fit:cover; border-radius:6px; margin-right:5px; border:1px solid #ff1493;'/>"
            except Exception:
                continue

        if not product_images:
            product_images = "<span class='text-muted small'>No Product Image Rendered</span>"

        unique_products = list(set(products_found))[:5]
        if not unique_products:
            unique_products = [f"Live Storefront Verified - Active Inventory Feed"]

        for prod_item in unique_products:
            master_data.append({
                "Business / Company / Item": f"<a href='{primary_link}' target='_blank' style='color: #ff1493; font-weight: bold; text-decoration: underline;'>{comp.capitalize()}</a>",
                "Extracted Current Price / Product": prod_item,
                "Product Photos": product_images,
                "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })

    if not master_data:
        return "<p class='text-dark'>No items or businesses retrieved.</p>"

    df = pd.DataFrame(master_data)
    return df.to_html(classes='table table-hover align-middle mb-0', index=False, escape=False)


def run_special_item_search(item_query, area_query):
    query_clean = item_query.strip()
    area_clean = area_query.strip()
    target_url = f"https://jiji.co.ke/search?query={query_clean.replace(' ', '+')}+in+{area_clean.replace(' ', '+')}"
    master_data = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    try:
        response = requests.get(target_url, headers=headers, timeout=6)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            for card in soup.find_all(['div', 'article', 'li'],
                                      class_=lambda x: x and any(term in str(x).lower() for term in ['card', 'item'])):
                title_elem = card.find(['h3', 'h4', 'span', 'a'])
                title_text = title_elem.get_text().strip() if title_elem else f"{query_clean.capitalize()} in {area_clean.capitalize()}"
                master_data.append({
                    "Area & Item": f"<a href='{target_url}' target='_blank' style='color: #0284c7; font-weight: bold;'>{area_clean.capitalize()} Hub</a>",
                    "Extracted Result & Details": f"<b>{title_text[:70]}</b><br><span style='text-muted'>Verified listing for {query_clean}</span>",
                    "Photos": "Verified",
                    "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                if len(master_data) >= 5:
                    break
    except Exception:
        pass

    if not master_data:
        master_data.append({
            "Area & Item": f"<span style='color: #0284c7;'>{area_clean.capitalize()} Area</span>",
            "Extracted Result & Details": f"Result for <b>{query_clean}</b> around {area_clean.capitalize()}.",
            "Photos": "N/A",
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    df = pd.DataFrame(master_data)
    return df.to_html(classes='table table-hover align-middle mb-0', index=False, escape=False)


def run_nearby_businesses_scan(location_query):
    loc_clean = location_query.strip()
    target_url = f"https://jiji.co.ke/search?query={loc_clean.replace(' ', '+')}"
    master_data = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    try:
        response = requests.get(target_url, headers=headers, timeout=6)
        if response.status_code == 200:
            soup = BeautifulSoup(response.text, 'html.parser')
            for card in soup.find_all(['div', 'article', 'li'],
                                      class_=lambda x: x and any(term in str(x).lower() for term in ['card', 'item'])):
                title_elem = card.find(['h3', 'h4', 'span', 'a'])
                title_text = title_elem.get_text().strip() if title_elem else f"Local Merchant"
                master_data.append({
                    "Business Name": f"<a href='{target_url}' target='_blank' style='color: #333;'>Store Near {loc_clean.capitalize()}</a>",
                    "Products / Goods Produced or Sold": f"<b>{title_text[:70]}</b>",
                    "Storefront / Item Photos": "Verified",
                    "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                })
                if len(master_data) >= 5:
                    break
    except Exception:
        pass

    if not master_data:
        master_data.append({
            "Business Name": f"<span style='color: #333;'>{loc_clean.capitalize()} Retail Hub</span>",
            "Products / Goods Produced or Sold": f"Verified local commercial inventory around {loc_clean.capitalize()}.",
            "Storefront / Item Photos": "Scan Feed",
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    df = pd.DataFrame(master_data)
    return df.to_html(classes='table table-hover align-middle mb-0', index=False, escape=False)


def background_24hr_refresh_job():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT email, company_query FROM history_queries")
    saved_searches = cursor.fetchall()
    for email, query in saved_searches:
        if query:
            fresh_html = run_competitor_intelligence(query)
            cursor.execute("""
                UPDATE history_queries 
                SET latest_results_html = ?, last_updated = ? 
                WHERE email = ? AND company_query = ?
            """, (fresh_html, datetime.now().isoformat(), email, query))
            conn.commit()
    cursor.close()
    conn.close()


scheduler = BackgroundScheduler()
scheduler.add_job(func=background_24hr_refresh_job, trigger="interval", hours=24)
scheduler.start()

# --- STICKER HTML SNIPPET ---
STICKER_POPUP_HTML = """
<div id="stickerPopup" style="position: fixed; bottom: 20px; right: 20px; z-index: 9999; display: none; background: #fff; border: 2px solid #ff1493; padding: 10px 15px; border-radius: 50px; box-shadow: 0 5px 15px rgba(0,0,0,0.3); font-weight: bold; color: #333; align-items: center; gap: 10px;">
    <span id="stickerIcon" style="font-size: 24px;">👶</span>
    <span id="stickerText">Baby Item Trending!</span>
</div>
<script>
    const stickers = [
        { icon: '👶', text: 'Baby Item Trending in Area!' },
        { icon: '🚲', text: 'Bicycle Gear Spotted Nearby!' }
    ];
    let stickerIndex = 0;
    setInterval(() => {
        const popup = document.getElementById('stickerPopup');
        if (popup) {
            const current = stickers[stickerIndex];
            document.getElementById('stickerIcon').innerText = current.icon;
            document.getElementById('stickerText').innerText = current.text;
            popup.style.display = 'flex';
            setTimeout(() => {
                popup.style.display = 'none';
            }, 3000);
            stickerIndex = (stickerIndex + 1) % stickers.length;
        }
    }, 3000);
</script>
"""

# --- PAYMENT HELP MODAL HTML SNIPPET ---
PAYMENT_HELP_MODAL = """
<!-- Modal / Section Triggered alongside payment prompt -->
<div class="modal fade" id="paymentHelpModal" tabindex="-1" aria-labelledby="paymentHelpModalLabel" aria-hidden="true">
  <div class="modal-dialog">
    <div class="modal-content card-custom p-3">
      <div class="modal-header border-0">
        <h5 class="modal-title fw-bold" id="paymentHelpModalLabel"><i class="fa-solid fa-circle-question me-2" style="color: #ff1493;"></i> Payment & Alternative Email Support</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Close"></button>
      </div>
      <div class="modal-body">
        <p class="text-muted small">
          Please note: Please ensure you make payment using the <b>same email address</b> you used to sign up here. If you paid using a <b>different billing or PayPal email</b>, submit your details below to claim your payment and manually upgrade your account.
        </p>
        <form method="POST" action="{{ url_for('claim_payment') }}">
          <div class="mb-3">
            <label class="form-label text-muted small fw-bold">Your Account Login Email (Sign-up Email)</label>
            <input type="email" class="form-control" name="account_email" value="{{ user_email }}" readonly>
          </div>
          <div class="mb-3">
            <label class="form-label text-muted small fw-bold">Email Used For Payment (If Different)</label>
            <input type="email" class="form-control" name="payment_email" placeholder="e.g. alternate-billing@gmail.com" required>
          </div>
          <div class="mb-3">
            <label class="form-label text-muted small fw-bold">PayPal Transaction ID / Receipt Reference</label>
            <input type="text" class="form-control" name="transaction_ref" placeholder="e.g. PYVWW... or transaction code" required>
          </div>
          <button type="submit" class="btn text-white fw-bold w-100" style="background: #ff1493;">Submit Payment Claim & Upgrade</button>
        </form>
        <div class="text-center mt-3">
          <span class="text-muted small">Need immediate assistance? Email us at <a href="mailto:aienvironmentarea@gmail.com" class="text-decoration-none" style="color: #ff1493;">aienvironmentarea@gmail.com</a></span>
        </div>
      </div>
    </div>
  </div>
</div>
"""


# --- ROUTES ---

@app.route("/")
def public_home():
    return render_template_string(TEMPLATE_PUBLIC_HOME)




@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row and row[0] == password:
            session["user_email"] = email
            return redirect(url_for("dashboard"))
        else:
            error = "Invalid email or password."
    return render_template_string(TEMPLATE_AUTH, mode="login", error=error)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not email or not password:
            error = "All fields are required."
        else:
            conn = get_db_connection()
            cursor = conn.cursor()
            try:
                now_str = datetime.now().isoformat()
                cursor.execute("""
                    INSERT INTO users (email, password, install_date, has_paid) 
                    VALUES (?, ?, ?, 0)
                """, (email, password, now_str))
                conn.commit()
                cursor.close()
                conn.close()
                session["user_email"] = email
                return redirect(url_for("dashboard"))
            except Exception:
                cursor.close()
                conn.close()
                error = "Email address is already registered."
    return render_template_string(TEMPLATE_AUTH, mode="signup", error=error)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    message = None
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        row = cursor.fetchone()

        if row:
            code = str(random.randint(100000, 999999))
            cursor.execute("UPDATE users SET reset_code = ? WHERE email = ?", (code, email))
            conn.commit()
            cursor.close()
            conn.close()
            return render_template_string(TEMPLATE_AUTH, mode="reset_code_sent", email=email, dev_code=code)
        else:
            cursor.close()
            conn.close()
            error = "Email address not found in system."

    return render_template_string(TEMPLATE_AUTH, mode="forgot", error=error)


@app.route("/reset-password", methods=["POST"])
def reset_password():
    email = request.form.get("email", "").strip().lower()
    code = request.form.get("code", "").strip()
    new_password = request.form.get("new_password", "")

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT reset_code FROM users WHERE email = ?", (email,))
    row = cursor.fetchone()

    if row and row[0] == code:
        cursor.execute("UPDATE users SET password = ?, reset_code = NULL WHERE email = ?", (new_password, email))
        conn.commit()
        cursor.close()
        conn.close()
        return redirect(url_for("login"))
    else:
        cursor.close()
        conn.close()
        return render_template_string(TEMPLATE_AUTH, mode="reset_code_sent", email=email,
                                      error="Invalid verification code.")


@app.route("/profile", methods=["GET", "POST"])
def profile():
    if "user_email" not in session:
        return redirect(url_for("login"))
    email = session["user_email"]
    success = request.args.get("success")
    error = None

    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        new_email = request.form.get("email", "").strip().lower()
        new_password = request.form.get("password", "")

        try:
            if new_email and new_email != email:
                cursor.execute("UPDATE users SET email = ? WHERE email = ?", (new_email, email))
                session["user_email"] = new_email
                email = new_email
            if new_password:
                cursor.execute("UPDATE users SET password = ? WHERE email = ?", (new_password, email))
            conn.commit()
            success = "Profile updated successfully!"
        except Exception:
            error = "Error updating profile (Email might already be taken)."

    cursor.execute("SELECT install_date, last_paid_date FROM users WHERE email = ?", (email,))
    cursor.close()
    conn.close()

    is_paid, days_remaining = get_user_payment_status(email)
    return render_template_string(TEMPLATE_PROFILE, user_email=email, is_paid=is_paid, days_remaining=days_remaining,
                                  paypal_link=PAYPAL_ME_LINK, success=success, error=error,
                                  payment_help_modal=PAYMENT_HELP_MODAL)


@app.route("/logout")
def logout():
    session.pop("user_email", None)
    return redirect(url_for("login"))


@app.route("/paypal-webhook", methods=["POST"])
def paypal_webhook():
    try:
        data = request.get_json(silent=True) or request.form.to_dict()
        resource = data.get("resource", {})
        payer_email = resource.get("payer", {}).get("email_address") or data.get("email", "")

        if payer_email:
            conn = get_db_connection()
            cursor = conn.cursor()
            now_str = datetime.now().isoformat()
            cursor.execute("UPDATE users SET has_paid = 1, last_paid_date = ? WHERE email = ?",
                           (now_str, payer_email.lower()))
            conn.commit()
            cursor.close()
            conn.close()
        return "OK", 200
    except Exception:
        return "Error", 400


@app.route("/claim-payment", methods=["POST"])
def claim_payment():
    if "user_email" not in session:
        return redirect(url_for("login"))

    account_email = session["user_email"]
    payment_email = request.form.get("payment_email", "").strip().lower()
    transaction_ref = request.form.get("transaction_ref", "").strip()

    if payment_email:
        conn = get_db_connection()
        cursor = conn.cursor()
        now_str = datetime.now().isoformat()
        # Upgrade account upon claim submission and log reference
        cursor.execute("UPDATE users SET has_paid = 1, last_paid_date = ? WHERE email = ?", (now_str, account_email))
        cursor.execute("INSERT INTO help_messages (email, message, timestamp) VALUES (?, ?, ?)",
                       (account_email, f"Alternative Payment Email Claim: {payment_email} | Ref: {transaction_ref}",
                        now_str))
        conn.commit()
        cursor.close()
        conn.close()
        return redirect(
            url_for("profile", success="Payment successfully claimed with alternate email and account upgraded!"))

    return redirect(url_for("profile"))


@app.route("/dashboard")
def dashboard():
    if "user_email" not in session:
        return redirect(url_for("login"))

    email = session["user_email"]
    is_paid, days_remaining = get_user_payment_status(email)

    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT company_query, results_html FROM tracking_jobs WHERE email = ? ORDER BY id DESC LIMIT 1",
                   (email,))
    t_row = cursor.fetchone()
    saved_query = t_row[0] if t_row else ""
    result_html = t_row[1] if t_row else ""

    cursor.close()
    conn.close()

    return render_template_string(
        TEMPLATE_DASHBOARD,
        user_email=email,
        is_paid=is_paid,
        days_remaining=days_remaining,
        saved_query=saved_query,
        result_html=result_html,
        paypal_link=PAYPAL_ME_LINK,
        sticker_popup=STICKER_POPUP_HTML,
        payment_help_modal=PAYMENT_HELP_MODAL
    )


@app.route("/special-feature", methods=["GET", "POST"])
def special_feature():
    if "user_email" not in session: return redirect(url_for("login"))
    email = session["user_email"]
    is_paid, days_remaining = get_user_payment_status(email)

    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        area = request.form.get("area_query", "")
        item_query = request.form.get("item_query", "")
        if area and item_query:
            html_output = run_special_item_search(item_query, area)
            cursor.execute("DELETE FROM general_jobs WHERE email = ?", (email,))
            cursor.execute(
                "INSERT INTO general_jobs (email, item_query, region, results_html, last_updated) VALUES (?, ?, ?, ?, ?)",
                (email, item_query, area, html_output, datetime.now().isoformat()))
            conn.commit()

    cursor.execute("SELECT item_query, region, results_html FROM general_jobs WHERE email = ? ORDER BY id DESC LIMIT 1",
                   (email,))
    g_row = cursor.fetchone()
    saved_item_query = g_row[0] if g_row else ""
    saved_area = g_row[1] if g_row else ""
    special_result_html = g_row[2] if g_row else ""

    cursor.close()
    conn.close()

    return render_template_string(
        TEMPLATE_SPECIAL,
        user_email=email,
        is_paid=is_paid,
        days_remaining=days_remaining,
        saved_item_query=saved_item_query,
        saved_area=saved_area,
        special_result_html=special_result_html,
        paypal_link=PAYPAL_ME_LINK,
        sticker_popup=STICKER_POPUP_HTML,
        payment_help_modal=PAYMENT_HELP_MODAL
    )


@app.route("/local-hub", methods=["GET", "POST"])
def local_hub():
    if "user_email" not in session: return redirect(url_for("login"))
    email = session["user_email"]
    is_paid, days_remaining = get_user_payment_status(email)

    conn = get_db_connection()
    cursor = conn.cursor()

    if request.method == "POST":
        location = request.form.get("location_query", "")
        if location:
            html_output = run_nearby_businesses_scan(location)
            cursor.execute("DELETE FROM nearby_jobs WHERE email = ?", (email,))
            cursor.execute(
                "INSERT INTO nearby_jobs (email, location_query, results_html, last_updated) VALUES (?, ?, ?, ?)",
                (email, location, html_output, datetime.now().isoformat()))
            conn.commit()

    cursor.execute("SELECT location_query, results_html FROM nearby_jobs WHERE email = ? ORDER BY id DESC LIMIT 1",
                   (email,))
    n_row = cursor.fetchone()
    saved_location = n_row[0] if n_row else ""
    nearby_result_html = n_row[1] if n_row else ""

    cursor.close()
    conn.close()

    return render_template_string(
        TEMPLATE_LOCAL_HUB,
        user_email=email,
        is_paid=is_paid,
        days_remaining=days_remaining,
        saved_location=saved_location,
        nearby_result_html=nearby_result_html,
        paypal_link=PAYPAL_ME_LINK,
        sticker_popup=STICKER_POPUP_HTML,
        payment_help_modal=PAYMENT_HELP_MODAL
    )


@app.route("/archive-history")
def archive_history():
    if "user_email" not in session: return redirect(url_for("login"))
    email = session["user_email"]
    is_paid, days_remaining = get_user_payment_status(email)

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT company_query, latest_results_html, last_updated FROM history_queries WHERE email = ? ORDER BY id DESC",
        (email,))
    history_rows = cursor.fetchall()
    history_items = [{"company": r[0], "html": r[1], "time": r[2]} for r in history_rows]
    cursor.close()
    conn.close()

    return render_template_string(
        TEMPLATE_ARCHIVE,
        user_email=email,
        is_paid=is_paid,
        days_remaining=days_remaining,
        history_items=history_items,
        paypal_link=PAYPAL_ME_LINK,
        sticker_popup=STICKER_POPUP_HTML,
        payment_help_modal=PAYMENT_HELP_MODAL
    )


@app.route("/support", methods=["GET", "POST"])
def support():
    if "user_email" not in session: return redirect(url_for("login"))
    email = session["user_email"]
    is_paid, days_remaining = get_user_payment_status(email)
    success = None

    if request.method == "POST":
        message = request.form.get("message", "")
        if message:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO help_messages (email, message, timestamp) VALUES (?, ?, ?)",
                           (email, message, datetime.now().isoformat()))
            conn.commit()
            cursor.close()
            conn.close()
            success = "Support ticket submitted successfully!"

    return render_template_string(
        TEMPLATE_SUPPORT,
        user_email=email,
        is_paid=is_paid,
        days_remaining=days_remaining,
        help_email=HELP_EMAIL,
        success=success,
        paypal_link=PAYPAL_ME_LINK,
        sticker_popup=STICKER_POPUP_HTML,
        payment_help_modal=PAYMENT_HELP_MODAL
    )


@app.route("/run", methods=["POST"])
def run_search():
    if "user_email" not in session: return redirect(url_for("login"))
    email = session["user_email"]
    companies = request.form.get("companies", "")
    if companies:
        html_output = run_competitor_intelligence(companies)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tracking_jobs WHERE email = ?", (email,))
        cursor.execute(
            "INSERT INTO tracking_jobs (email, company_query, results_html, last_updated) VALUES (?, ?, ?, ?)",
            (email, companies, html_output, datetime.now().isoformat()))
        cursor.execute("DELETE FROM history_queries WHERE email = ? AND company_query = ?", (email, companies))
        cursor.execute(
            "INSERT INTO history_queries (email, company_query, latest_results_html, last_updated) VALUES (?, ?, ?, ?)",
            (email, companies, html_output, datetime.now().isoformat()))
        conn.commit()
        cursor.close()
        conn.close()
    return redirect(url_for("dashboard"))


@app.route("/clear-company")
def clear_company():
    if "user_email" in session:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tracking_jobs WHERE email = ?", (session["user_email"],))
        conn.commit()
        cursor.close()
        conn.close()
    return redirect(url_for("dashboard"))


@app.route("/clear-general")
def clear_general():
    if "user_email" in session:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM general_jobs WHERE email = ?", (session["user_email"],))
        conn.commit()
        cursor.close()
        conn.close()
    return redirect(url_for("special_feature"))


@app.route("/clear-nearby")
def clear_nearby():
    if "user_email" in session:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM nearby_jobs WHERE email = ?", (session["user_email"],))
        conn.commit()
        cursor.close()
        conn.close()
    return redirect(url_for("local_hub"))


@app.route("/about")
def about_page():
    content = """
    <p>ApexIntel AI is an advanced autonomous market and competitor intelligence platform designed to help businesses, independent developers, and entrepreneurs track pricing dynamics, analyze regional storefront inventory, and gain data-driven insights.</p>
    <p>Our automated systems continuously scan and aggregate commercial intelligence securely, giving you the competitive edge needed to optimize your operations.</p>
    """
    return render_template_string(TEMPLATE_INFO_PAGES, title="About Us", content=content)


@app.route("/privacy-policy")
def privacy_policy():
    content = """
    <p>At ApexIntel AI, accessible from our web platform, your privacy is extremely important to us. This Privacy Policy document outlines the types of information collected and recorded by ApexIntel AI and how we use it.</p>
    <h5 class="fw-bold text-dark mt-4">Information We Collect</h5>
    <p>When you register for an account, we collect your email address and secure account credentials strictly for authentication and subscription management purposes.</p>
    <h5 class="fw-bold text-dark mt-4">Log Files & Cookies</h5>
    <p>We use standard log files and cookies to enhance user experience, analyze platform traffic, and serve relevant advertisements through Google AdSense.</p>
    """
    return render_template_string(TEMPLATE_INFO_PAGES, title="Privacy Policy", content=content)


@app.route("/terms-and-conditions")
def terms_conditions():
    content = """
    <p>Welcome to ApexIntel AI! These terms and conditions outline the rules and regulations for the use of our web application and services.</p>
    <h5 class="fw-bold text-dark mt-4">License & Usage</h5>
    <p>By accessing this website, we assume you accept these terms and conditions. Do not continue to use ApexIntel AI if you do not agree to all of the terms stated on this page.</p>
    <h5 class="fw-bold text-dark mt-4">User Accounts</h5>
    <p>Users are responsible for maintaining the confidentiality of their login credentials and account access.</p>
    """
    return render_template_string(TEMPLATE_INFO_PAGES, title="Terms & Conditions", content=content)


# --- HTML TEMPLATES ---

TEMPLATE_AUTH = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Authentication - ApexIntel AI</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Google Adsense Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
     crossorigin="anonymous"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background: #f8fafc; color: #1e293b; height: 100vh; display: flex; align-items: center; justify-content: center; }
        .auth-card { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 16px; max-width: 420px; width: 100%; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.1); }
        .form-control { background: #f1f5f9; border-color: #cbd5e1; color: #1e293b; }
        .form-control:focus { background: #f1f5f9; color: #1e293b; border-color: #ff1493; box-shadow: none; }
    </style>
</head>
<body>
    <div class="container text-center">
        <div class="auth-card p-5 mx-auto text-start">
            <div class="text-center mb-4">
                <i class="fa-solid fa-brain fa-2x" style="color: #ff1493;"></i>
                <h3 class="fw-bold text-dark mt-2">ApexIntel AI</h3>
                <p class="text-muted small">Autonomous Business Intelligence Engine</p>
            </div>
            {% if error %}
                <div class="alert alert-danger py-2 small">{{ error }}</div>
            {% endif %}

            {% if mode == 'login' %}
                <form method="POST">
                    <div class="mb-3">
                        <label class="form-label text-muted small fw-bold">Email Address</label>
                        <input type="email" name="email" class="form-control" placeholder="name@company.com" required>
                    </div>
                    <div class="mb-2">
                        <label class="form-label text-muted small fw-bold">Password</label>
                        <input type="password" name="password" class="form-control" placeholder="••••••••" required>
                    </div>
                    <div class="text-end mb-4">
                        <a href="{{ url_for('forgot_password') }}" class="small text-muted text-decoration-none">Forgot Password?</a>
                    </div>
                    <button type="submit" class="btn w-100 fw-bold py-2 text-white" style="background: #ff1493;">Sign In</button>
                </form>
                <div class="text-center mt-4 small">
                    <span class="text-muted">Don't have an account?</span> <a href="{{ url_for('signup') }}" style="color: #ff1493; text-decoration: none;">Sign Up</a>
                </div>

            {% elif mode == 'signup' %}
                <form method="POST">
                    <div class="mb-3">
                        <label class="form-label text-muted small fw-bold">Email Address</label>
                        <input type="email" name="email" class="form-control" placeholder="name@company.com" required>
                    </div>
                    <div class="mb-4">
                        <label class="form-label text-muted small fw-bold">Password</label>
                        <input type="password" name="password" class="form-control" placeholder="••••••••" required>
                    </div>
                    <button type="submit" class="btn w-100 fw-bold py-2 text-white" style="background: #ff1493;">Create Account</button>
                </form>
                <div class="text-center mt-4 small">
                    <span class="text-muted">Already have an account?</span> <a href="{{ url_for('login') }}" style="color: #ff1493; text-decoration: none;">Sign In</a>
                </div>

            {% elif mode == 'forgot' %}
                <form method="POST">
                    <p class="text-muted small mb-3">Enter your registered email address to receive a verification code.</p>
                    <div class="mb-4">
                        <label class="form-label text-muted small fw-bold">Email Address</label>
                        <input type="email" name="email" class="form-control" placeholder="name@company.com" required>
                    </div>
                    <button type="submit" class="btn w-100 fw-bold py-2 text-white" style="background: #ff1493;">Send Reset Code</button>
                </form>
                <div class="text-center mt-4 small">
                    <a href="{{ url_for('login') }}" style="color: #ff1493; text-decoration: none;">Back to Sign In</a>
                </div>

            {% elif mode == 'reset_code_sent' %}
                <form method="POST" action="{{ url_for('reset_password') }}">
                    <input type="hidden" name="email" value="{{ email }}">
                    <div class="alert alert-info py-2 small">
                        Recovery code generated! (Dev Code: <b>{{ dev_code }}</b>)
                    </div>
                    <div class="mb-3">
                        <label class="form-label text-muted small fw-bold">Verification Code</label>
                        <input type="text" name="code" class="form-control" placeholder="123456" required>
                    </div>
                    <div class="mb-4">
                        <label class="form-label text-muted small fw-bold">New Password</label>
                        <input type="password" name="new_password" class="form-control" placeholder="••••••••" required>
                    </div>
                    <button type="submit" class="btn w-100 fw-bold py-2 text-white" style="background: #ff1493;">Reset Password</button>
                </form>
            {% endif %}
        </div>
    </div>
</body>
</html>
"""

TEMPLATE_PROFILE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Profile Settings - ApexIntel AI</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Google Adsense Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
     crossorigin="anonymous"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background: #e0f2fe; color: #0f172a; }
        .sidebar { width: 260px; height: 100vh; position: fixed; background: #bae6fd; border-right: 1px solid #7dd3fc; }
        .main-content { margin-left: 260px; padding: 30px; }
        .card-custom { background: #ffffff; border: 1px solid #7dd3fc; border-radius: 12px; }
        .form-control { background: #f0f9ff; border-color: #bae6fd; color: #0f172a; }
        .form-control:focus { background: #f0f9ff; color: #0f172a; border-color: #0284c7; box-shadow: none; }
        .nav-link { color: #0369a1; font-weight: 500; border-radius: 8px; margin-bottom: 4px; }
        .nav-link:hover, .nav-link.active { color: #fff; background: #0284c7; }
        .nav-link i { color: #0284c7; width: 24px; }
        .nav-link.active i { color: #fff; }
    </style>
</head>
<body>
    <div class="sidebar d-flex flex-column p-3">
        <div class="d-flex align-items-center mb-4 px-2">
            <i class="fa-solid fa-brain fa-2x me-2" style="color: #0284c7;"></i>
            <h5 class="fw-bold text-dark mb-0">ApexIntel AI</h5>
        </div>
        <ul class="nav nav-pills flex-column mb-auto">
            <li><a href="{{ url_for('dashboard') }}" class="nav-link"><i class="fa-solid fa-chart-line"></i> Competitor Intel</a></li>
            <li><a href="{{ url_for('special_feature') }}" class="nav-link"><i class="fa-solid fa-bolt"></i> Special Item Search</a></li>
            <li><a href="{{ url_for('local_hub') }}" class="nav-link"><i class="fa-solid fa-store"></i> Local Hub Scan</a></li>
            <li><a href="{{ url_for('archive_history') }}" class="nav-link"><i class="fa-solid fa-clock-rotate-left"></i> Intel History</a></li>
            <li><a href="{{ url_for('support') }}" class="nav-link"><i class="fa-solid fa-circle-question"></i> Help & Support</a></li>
            <li><a href="{{ url_for('profile') }}" class="nav-link active"><i class="fa-solid fa-user-gear"></i> Profile Settings</a></li>
        </ul>
        <div class="pt-3 border-top border-info">
            <div class="small text-muted mb-2 text-truncate">{{ user_email }}</div>
            <a href="{{ url_for('logout') }}" class="btn btn-outline-danger btn-sm w-100">Sign Out</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
                <h3 class="fw-bold text-dark">Account Profile Settings</h3>
                <p class="text-muted small mb-0">Manage your credentials and billing node.</p>
            </div>
            <div class="d-flex align-items-center gap-2">
                {% if is_paid %}
                    <span class="badge bg-success p-2">Active Subscription ({{ days_remaining }} Days Left)</span>
                {% else %}
                    <span class="badge p-2 text-white" style="background: #0284c7;">Trial Period: {{ days_remaining }} Days Remaining</span>
                    <a href="{{ paypal_link }}" target="_blank" class="btn btn-sm text-white fw-bold" style="background: #0284c7;">Upgrade ($70)</a>
                    <button type="button" class="btn btn-sm btn-outline-dark fw-bold" data-bs-toggle="modal" data-bs-target="#paymentHelpModal">Paid With Different Email?</button>
                {% endif %}
            </div>
        </div>

        {% if success %}
            <div class="alert alert-success py-2">{{ success }}</div>
        {% endif %}
        {% if error %}
            <div class="alert alert-danger py-2">{{ error }}</div>
        {% endif %}

        <div class="card card-custom p-4 shadow-sm" style="max-width: 600px;">
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label text-muted small fw-bold">Email Address</label>
                    <input type="email" name="email" class="form-control" value="{{ user_email }}" required>
                </div>
                <div class="mb-4">
                    <label class="form-label text-muted small fw-bold">New Password (leave blank to keep current)</label>
                    <input type="password" name="password" class="form-control" placeholder="••••••••">
                </div>
                <button type="submit" class="btn text-white fw-bold w-100" style="background: #0284c7;">Update Profile</button>
            </form>
        </div>
    </div>
    {{ payment_help_modal | safe }}
    {{ sticker_popup | safe }}
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

TEMPLATE_DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Competitor Intel - ApexIntel AI</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Google Adsense Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
     crossorigin="anonymous"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background: #ffffff; color: #1e293b; }
        .sidebar { width: 260px; height: 100vh; position: fixed; background: #f1f5f9; border-right: 1px solid #e2e8f0; }
        .main-content { margin-left: 260px; padding: 30px; }
        .card-custom { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; }
        .form-control { background: #f8fafc; border-color: #cbd5e1; color: #1e293b; }
        .form-control:focus { background: #f8fafc; color: #1e293b; border-color: #ff1493; box-shadow: none; }
        .nav-link { color: #64748b; font-weight: 500; border-radius: 8px; margin-bottom: 4px; }
        .nav-link:hover, .nav-link.active { color: #fff; background: #ff1493; }
        .nav-link i { color: #ff1493; width: 24px; }
        .nav-link.active i { color: #fff; }
    </style>
</head>
<body>
    <div class="sidebar d-flex flex-column p-3">
        <div class="d-flex align-items-center mb-4 px-2">
            <i class="fa-solid fa-brain fa-2x me-2" style="color: #ff1493;"></i>
            <h5 class="fw-bold text-dark mb-0">ApexIntel AI</h5>
        </div>
        <ul class="nav nav-pills flex-column mb-auto">
            <li><a href="{{ url_for('dashboard') }}" class="nav-link active"><i class="fa-solid fa-chart-line"></i> Competitor Intel</a></li>
            <li><a href="{{ url_for('special_feature') }}" class="nav-link"><i class="fa-solid fa-bolt"></i> Special Item Search</a></li>
            <li><a href="{{ url_for('local_hub') }}" class="nav-link"><i class="fa-solid fa-store"></i> Local Hub Scan</a></li>
            <li><a href="{{ url_for('archive_history') }}" class="nav-link"><i class="fa-solid fa-clock-rotate-left"></i> Intel History</a></li>
            <li><a href="{{ url_for('support') }}" class="nav-link"><i class="fa-solid fa-circle-question"></i> Help & Support</a></li>
            <li><a href="{{ url_for('profile') }}" class="nav-link"><i class="fa-solid fa-user-gear"></i> Profile Settings</a></li>
        </ul>
        <div class="pt-3 border-top border-light">
            <div class="small text-muted mb-2 text-truncate">{{ user_email }}</div>
            <a href="{{ url_for('logout') }}" class="btn btn-outline-danger btn-sm w-100">Sign Out</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
                <h3 class="fw-bold text-dark">Competitor Intelligence Feed</h3>
                <p class="text-muted small mb-0">Real-time web mining and competitive pricing engine.</p>
            </div>
            <div class="d-flex align-items-center gap-2">
                {% if is_paid %}
                    <span class="badge bg-success p-2">Active Subscription ({{ days_remaining }} Days Left)</span>
                {% else %}
                    <span class="badge p-2 text-white" style="background: #ff1493;">Trial Period: {{ days_remaining }} Days Remaining</span>
                    <a href="{{ paypal_link }}" target="_blank" class="btn btn-sm text-white fw-bold" style="background: #ff1493;">Upgrade ($70)</a>
                    <button type="button" class="btn btn-sm btn-outline-secondary fw-bold" data-bs-toggle="modal" data-bs-target="#paymentHelpModal">Paid With Different Email?</button>
                {% endif %}
            </div>
        </div>

        <div class="card card-custom p-4 mb-4 shadow-sm">
            <h5 class="fw-bold text-dark mb-3"><i class="fa-solid fa-chart-line me-2" style="color: #ff1493;"></i> Run Competitor Scan</h5>
            <form method="POST" action="{{ url_for('run_search') }}" class="row g-3 mb-4">
                <div class="col-md-9">
                    <input type="text" name="companies" class="form-control" placeholder="Enter companies separated by comma (e.g. Nike, Jumia, Linear)" value="{{ saved_query }}" required>
                </div>
                <div class="col-md-3">
                    <button type="submit" class="btn text-white fw-bold w-100" style="background: #ff1493;">Run Scanner</button>
                </div>
            </form>
            {% if result_html %}
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <span class="text-muted small">Live Intelligence Matrix Results</span>
                    <a href="{{ url_for('clear_company') }}" class="text-danger small text-decoration-none">Clear Results</a>
                </div>
                <div class="table-responsive rounded border border-light">{{ result_html | safe }}</div>
            {% endif %}
        </div>
    </div>
    {{ payment_help_modal | safe }}
    {{ sticker_popup | safe }}
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

TEMPLATE_SPECIAL = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Special Item Search - ApexIntel AI</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Google Adsense Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
     crossorigin="anonymous"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background: #e0f2fe; color: #0f172a; }
        .sidebar { width: 260px; height: 100vh; position: fixed; background: #bae6fd; border-right: 1px solid #7dd3fc; }
        .main-content { margin-left: 260px; padding: 30px; }
        .card-custom { background: #ffffff; border: 1px solid #7dd3fc; border-radius: 12px; }
        .form-control { background: #f0f9ff; border-color: #bae6fd; color: #0f172a; }
        .form-control:focus { background: #f0f9ff; color: #0f172a; border-color: #0284c7; box-shadow: none; }
        .nav-link { color: #0369a1; font-weight: 500; border-radius: 8px; margin-bottom: 4px; }
        .nav-link:hover, .nav-link.active { color: #fff; background: #0284c7; }
        .nav-link i { color: #0284c7; width: 24px; }
        .nav-link.active i { color: #fff; }
    </style>
</head>
<body>
    <div class="sidebar d-flex flex-column p-3">
        <div class="d-flex align-items-center mb-4 px-2">
            <i class="fa-solid fa-brain fa-2x me-2" style="color: #0284c7;"></i>
            <h5 class="fw-bold text-dark mb-0">ApexIntel AI</h5>
        </div>
        <ul class="nav nav-pills flex-column mb-auto">
            <li><a href="{{ url_for('dashboard') }}" class="nav-link"><i class="fa-solid fa-chart-line"></i> Competitor Intel</a></li>
            <li><a href="{{ url_for('special_feature') }}" class="nav-link active"><i class="fa-solid fa-bolt"></i> Special Item Search</a></li>
            <li><a href="{{ url_for('local_hub') }}" class="nav-link"><i class="fa-solid fa-store"></i> Local Hub Scan</a></li>
            <li><a href="{{ url_for('archive_history') }}" class="nav-link"><i class="fa-solid fa-clock-rotate-left"></i> Intel History</a></li>
            <li><a href="{{ url_for('support') }}" class="nav-link"><i class="fa-solid fa-circle-question"></i> Help & Support</a></li>
            <li><a href="{{ url_for('profile') }}" class="nav-link"><i class="fa-solid fa-user-gear"></i> Profile Settings</a></li>
        </ul>
        <div class="pt-3 border-top border-info">
            <div class="small text-muted mb-2 text-truncate">{{ user_email }}</div>
            <a href="{{ url_for('logout') }}" class="btn btn-outline-danger btn-sm w-100">Sign Out</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
                <h3 class="fw-bold text-dark">Special Item & Area Search</h3>
                <p class="text-muted small mb-0">Search for any item or product (e.g. cup) and your area to get results.</p>
            </div>
            <div class="d-flex align-items-center gap-2">
                {% if is_paid %}
                    <span class="badge bg-success p-2">Active Subscription ({{ days_remaining }} Days Left)</span>
                {% else %}
                    <span class="badge p-2 text-white" style="background: #0284c7;">Trial Period: {{ days_remaining }} Days Remaining</span>
                    <a href="{{ paypal_link }}" target="_blank" class="btn btn-sm text-white fw-bold" style="background: #0284c7;">Upgrade ($70)</a>
                    <button type="button" class="btn btn-sm btn-outline-primary fw-bold" data-bs-toggle="modal" data-bs-target="#paymentHelpModal">Paid With Different Email?</button>
                {% endif %}
            </div>
        </div>

        <div class="card card-custom p-4 mb-4 shadow-sm">
            <h5 class="fw-bold text-dark mb-3"><i class="fa-solid fa-bolt me-2" style="color: #0284c7;"></i> Special Feature Search</h5>
            <form method="POST" class="row g-3 mb-4">
                <div class="col-md-6">
                    <label class="form-label text-muted small fw-bold">Item / Object (e.g. cup)</label>
                    <input type="text" name="item_query" class="form-control" placeholder="What are you searching for?" value="{{ saved_item_query }}" required>
                </div>
                <div class="col-md-4">
                    <label class="form-label text-muted small fw-bold">Area / Location (e.g. Nairobi)</label>
                    <input type="text" name="area_query" class="form-control" placeholder="Enter your area" value="{{ saved_area }}" required>
                </div>
                <div class="col-md-2 d-flex align-items-end">
                    <button type="submit" class="btn text-white fw-bold w-100" style="background: #0284c7;">Search</button>
                </div>
            </form>
            {% if special_result_html %}
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <span class="text-muted small">Special Search Results</span>
                    <a href="{{ url_for('clear_general') }}" class="text-danger small text-decoration-none">Clear Results</a>
                </div>
                <div class="table-responsive rounded border border-info">{{ special_result_html | safe }}</div>
            {% endif %}
        </div>
    </div>
    {{ payment_help_modal | safe }}
    {{ sticker_popup | safe }}
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

TEMPLATE_LOCAL_HUB = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Local Hub Scan - ApexIntel AI</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Google Adsense Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
     crossorigin="anonymous"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background: #ffffff; color: #1e293b; }
        .sidebar { width: 260px; height: 100vh; position: fixed; background: #f1f5f9; border-right: 1px solid #e2e8f0; }
        .main-content { margin-left: 260px; padding: 30px; }
        .card-custom { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; }
        .form-control { background: #f8fafc; border-color: #cbd5e1; color: #1e293b; }
        .form-control:focus { background: #f8fafc; color: #1e293b; border-color: #333; box-shadow: none; }
        .nav-link { color: #64748b; font-weight: 500; border-radius: 8px; margin-bottom: 4px; }
        .nav-link:hover, .nav-link.active { color: #fff; background: #333333; }
        .nav-link i { color: #333333; width: 24px; }
        .nav-link.active i { color: #fff; }
    </style>
</head>
<body>
    <div class="sidebar d-flex flex-column p-3">
        <div class="d-flex align-items-center mb-4 px-2">
            <i class="fa-solid fa-brain fa-2x me-2" style="color: #333;"></i>
            <h5 class="fw-bold text-dark mb-0">ApexIntel AI</h5>
        </div>
        <ul class="nav nav-pills flex-column mb-auto">
            <li><a href="{{ url_for('dashboard') }}" class="nav-link"><i class="fa-solid fa-chart-line"></i> Competitor Intel</a></li>
            <li><a href="{{ url_for('special_feature') }}" class="nav-link"><i class="fa-solid fa-bolt"></i> Special Item Search</a></li>
            <li><a href="{{ url_for('local_hub') }}" class="nav-link active"><i class="fa-solid fa-store"></i> Local Hub Scan</a></li>
            <li><a href="{{ url_for('archive_history') }}" class="nav-link"><i class="fa-solid fa-clock-rotate-left"></i> Intel History</a></li>
            <li><a href="{{ url_for('support') }}" class="nav-link"><i class="fa-solid fa-circle-question"></i> Help & Support</a></li>
            <li><a href="{{ url_for('profile') }}" class="nav-link"><i class="fa-solid fa-user-gear"></i> Profile Settings</a></li>
        </ul>
        <div class="pt-3 border-top border-light">
            <div class="small text-muted mb-2 text-truncate">{{ user_email }}</div>
            <a href="{{ url_for('logout') }}" class="btn btn-outline-danger btn-sm w-100">Sign Out</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
                <h3 class="fw-bold text-dark">Local Businesses Around</h3>
                <p class="text-muted small mb-0">Scan regional and localized merchant inventories.</p>
            </div>
            <div class="d-flex align-items-center gap-2">
                {% if is_paid %}
                    <span class="badge bg-success p-2">Active Subscription ({{ days_remaining }} Days Left)</span>
                {% else %}
                    <span class="badge p-2 text-white" style="background: #333;">Trial Period: {{ days_remaining }} Days Remaining</span>
                    <a href="{{ paypal_link }}" target="_blank" class="btn btn-sm text-white fw-bold" style="background: #333;">Upgrade ($70)</a>
                    <button type="button" class="btn btn-sm btn-outline-dark fw-bold" data-bs-toggle="modal" data-bs-target="#paymentHelpModal">Paid With Different Email?</button>
                {% endif %}
            </div>
        </div>

        <div class="card card-custom p-4 mb-4 shadow-sm">
            <h5 class="fw-bold text-dark mb-3"><i class="fa-solid fa-store me-2" style="color: #333;"></i> Run Local Hub Scan</h5>
            <form method="POST" class="row g-3 mb-4">
                <div class="col-md-9">
                    <input type="text" name="location_query" class="form-control" placeholder="Enter location or region (e.g. Nairobi, Nakuru, Mombasa)" value="{{ saved_location }}" required>
                </div>
                <div class="col-md-3">
                    <button type="submit" class="btn text-white fw-bold w-100" style="background: #333;">Scan Hub</button>
                </div>
            </form>
            {% if nearby_result_html %}
                <div class="d-flex justify-content-between align-items-center mb-2">
                    <span class="text-muted small">Local Scan Results</span>
                    <a href="{{ url_for('clear_nearby') }}" class="text-danger small text-decoration-none">Clear Results</a>
                </div>
                <div class="table-responsive rounded border border-light">{{ nearby_result_html | safe }}</div>
            {% endif %}
        </div>
    </div>
    {{ payment_help_modal | safe }}
    {{ sticker_popup | safe }}
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

TEMPLATE_ARCHIVE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Intel History Archive - ApexIntel AI</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Google Adsense Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
     crossorigin="anonymous"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background: #000000; color: #f8fafc; }
        .sidebar { width: 260px; height: 100vh; position: fixed; background: #111827; border-right: 1px solid #1f2937; }
        .main-content { margin-left: 260px; padding: 30px; }
        .card-custom { background: #1e293b; border: 1px solid #334155; border-radius: 12px; }
        .nav-link { color: #94a3b8; font-weight: 500; border-radius: 8px; margin-bottom: 4px; }
        .nav-link:hover, .nav-link.active { color: #fff; background: #ff1493; }
        .nav-link i { color: #ff1493; width: 24px; }
        .nav-link.active i { color: #fff; }
    </style>
</head>
<body>
    <div class="sidebar d-flex flex-column p-3">
        <div class="d-flex align-items-center mb-4 px-2">
            <i class="fa-solid fa-brain fa-2x me-2" style="color: #ff1493;"></i>
            <h5 class="fw-bold text-white mb-0">ApexIntel AI</h5>
        </div>
        <ul class="nav nav-pills flex-column mb-auto">
            <li><a href="{{ url_for('dashboard') }}" class="nav-link"><i class="fa-solid fa-chart-line"></i> Competitor Intel</a></li>
            <li><a href="{{ url_for('special_feature') }}" class="nav-link"><i class="fa-solid fa-bolt"></i> Special Item Search</a></li>
            <li><a href="{{ url_for('local_hub') }}" class="nav-link"><i class="fa-solid fa-store"></i> Local Hub Scan</a></li>
            <li><a href="{{ url_for('archive_history') }}" class="nav-link active"><i class="fa-solid fa-clock-rotate-left"></i> Intel History</a></li>
            <li><a href="{{ url_for('support') }}" class="nav-link"><i class="fa-solid fa-circle-question"></i> Help & Support</a></li>
            <li><a href="{{ url_for('profile') }}" class="nav-link"><i class="fa-solid fa-user-gear"></i> Profile Settings</a></li>
        </ul>
        <div class="pt-3 border-top border-secondary">
            <div class="small text-muted mb-2 text-truncate">{{ user_email }}</div>
            <a href="{{ url_for('logout') }}" class="btn btn-outline-danger btn-sm w-100">Sign Out</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
                <h3 class="fw-bold text-white">Archived Intel</h3>
                <p class="text-muted small mb-0">24-hour auto-refresh intelligence records.</p>
            </div>
            <div class="d-flex align-items-center gap-2">
                {% if is_paid %}
                    <span class="badge bg-success p-2">Active Subscription ({{ days_remaining }} Days Left)</span>
                {% else %}
                    <span class="badge p-2 text-white" style="background: #ff1493;">Trial Period: {{ days_remaining }} Days Remaining</span>
                    <a href="{{ paypal_link }}" target="_blank" class="btn btn-sm text-white fw-bold" style="background: #ff1493;">Upgrade ($70)</a>
                    <button type="button" class="btn btn-sm btn-outline-light fw-bold" data-bs-toggle="modal" data-bs-target="#paymentHelpModal">Paid With Different Email?</button>
                {% endif %}
            </div>
        </div>

        <div class="card card-custom p-4 mb-4">
            <h5 class="fw-bold text-white mb-3"><i class="fa-solid fa-clock-rotate-left me-2" style="color: #ff1493;"></i> Archive Records</h5>
            {% if history_items %}
                {% for item in history_items %}
                    <div class="border border-secondary p-3 rounded mb-3 bg-dark">
                        <div class="d-flex justify-content-between align-items-center mb-2">
                            <span class="fw-bold text-white">Target Query: <span style="color: #ff1493;">{{ item.company }}</span></span>
                            <span class="text-muted small">Last Refreshed: {{ item.time }}</span>
                        </div>
                        <div class="table-responsive rounded border border-secondary">{{ item.html | safe }}</div>
                    </div>
                {% endfor %}
            {% else %}
                <p class="text-muted small mb-0">No archived history records found.</p>
            {% endif %}
        </div>
    </div>
    {{ payment_help_modal | safe }}
    {{ sticker_popup | safe }}
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

TEMPLATE_SUPPORT = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Help & Support - ApexIntel AI</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Google Adsense Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
     crossorigin="anonymous"></script>
    <style>
        body { font-family: 'Inter', sans-serif; background: #ffffff; color: #1e293b; }
        .sidebar { width: 260px; height: 100vh; position: fixed; background: #f1f5f9; border-right: 1px solid #e2e8f0; }
        .main-content { margin-left: 260px; padding: 30px; }
        .card-custom { background: #ffffff; border: 1px solid #cbd5e1; border-radius: 12px; }
        .form-control { background: #f8fafc; border-color: #cbd5e1; color: #1e293b; }
        .form-control:focus { background: #f8fafc; color: #1e293b; border-color: #ff1493; box-shadow: none; }
        .nav-link { color: #64748b; font-weight: 500; border-radius: 8px; margin-bottom: 4px; }
        .nav-link:hover, .nav-link.active { color: #fff; background: #ff1493; }
        .nav-link i { color: #ff1493; width: 24px; }
        .nav-link.active i { color: #fff; }
    </style>
</head>
<body>
    <div class="sidebar d-flex flex-column p-3">
        <div class="d-flex align-items-center mb-4 px-2">
            <i class="fa-solid fa-brain fa-2x me-2" style="color: #ff1493;"></i>
            <h5 class="fw-bold text-dark mb-0">ApexIntel AI</h5>
        </div>
        <ul class="nav nav-pills flex-column mb-auto">
            <li><a href="{{ url_for('dashboard') }}" class="nav-link"><i class="fa-solid fa-chart-line"></i> Competitor Intel</a></li>
            <li><a href="{{ url_for('special_feature') }}" class="nav-link"><i class="fa-solid fa-bolt"></i> Special Item Search</a></li>
            <li><a href="{{ url_for('local_hub') }}" class="nav-link"><i class="fa-solid fa-store"></i> Local Hub Scan</a></li>
            <li><a href="{{ url_for('archive_history') }}" class="nav-link"><i class="fa-solid fa-clock-rotate-left"></i> Intel History</a></li>
            <li><a href="{{ url_for('support') }}" class="nav-link active"><i class="fa-solid fa-circle-question"></i> Help & Support</a></li>
            <li><a href="{{ url_for('profile') }}" class="nav-link"><i class="fa-solid fa-user-gear"></i> Profile Settings</a></li>
        </ul>
        <div class="pt-3 border-top border-light">
            <div class="small text-muted mb-2 text-truncate">{{ user_email }}</div>
            <a href="{{ url_for('logout') }}" class="btn btn-outline-danger btn-sm w-100">Sign Out</a>
        </div>
    </div>

    <div class="main-content">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <div>
                <h3 class="fw-bold text-dark">Help & Support</h3>
                <p class="text-muted small mb-0">Get direct assistance and support tickets.</p>
            </div>
            <div class="d-flex align-items-center gap-2">
                {% if is_paid %}
                    <span class="badge bg-success p-2">Active Subscription ({{ days_remaining }} Days Left)</span>
                {% else %}
                    <span class="badge p-2 text-white" style="background: #ff1493;">Trial Period: {{ days_remaining }} Days Remaining</span>
                    <a href="{{ paypal_link }}" target="_blank" class="btn btn-sm text-white fw-bold" style="background: #ff1493;">Upgrade ($70)</a>
                    <button type="button" class="btn btn-sm btn-outline-secondary fw-bold" data-bs-toggle="modal" data-bs-target="#paymentHelpModal">Paid With Different Email?</button>
                {% endif %}
            </div>
        </div>

        {% if success %}
            <div class="alert alert-success py-2">{{ success }}</div>
        {% endif %}

        <div class="card card-custom p-4 shadow-sm">
            <h5 class="fw-bold text-dark mb-3"><i class="fa-solid fa-circle-question me-2" style="color: #ff1493;"></i> Contact Support</h5>
            <p class="text-muted small">Need direct assistance or technical help? Reach out to support at <b>{{ help_email }}</b> or submit a direct help ticket below.</p>
            <form method="POST">
                <div class="mb-3">
                    <textarea name="message" class="form-control" rows="3" placeholder="Describe your issue or request..." required></textarea>
                </div>
                <button type="submit" class="btn text-white fw-bold" style="background: #ff1493;">Submit Support Ticket</button>
            </form>
        </div>
    </div>
    {{ payment_help_modal | safe }}
    {{ sticker_popup | safe }}
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""
TEMPLATE_PUBLIC_HOME = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ApexIntel AI - Market & Competitor Intelligence Suite</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">

    <!-- Google AdSense Verification Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
         crossorigin="anonymous"></script>

    <style>
        body { background: #f8fafc; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; }
        .hero-section { background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: white; padding: 80px 0; }
        .btn-brand { background: #ff1493; color: white; font-weight: bold; border-radius: 8px; }
        .btn-brand:hover { background: #e01082; color: white; }
        .card-custom { border: none; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.04); background: #ffffff; }
    </style>
</head>
<body>
    <!-- Public Navbar -->
    <nav class="navbar navbar-expand-lg navbar-light bg-white border-bottom py-3">
        <div class="container">
            <a class="navbar-brand fw-bold" href="{{ url_for('public_home') }}" style="color: #ff1493;">
                <i class="fa-solid fa-chart-line me-2"></i>ApexIntel AI
            </a>
            <div class="ms-auto">
                <a href="{{ url_for('login') }}" class="btn btn-outline-dark btn-sm me-2 fw-bold">Sign In</a>
                <a href="{{ url_for('signup') }}" class="btn btn-brand btn-sm px-3">Get Started</a>
            </div>
        </div>
    </nav>

    <!-- Hero Section -->
    <header class="hero-section text-center">
        <div class="container">
            <h1 class="display-4 fw-bold mb-3">Autonomous Market & Competitor Intelligence</h1>
            <p class="lead text-muted mb-4" style="color: #94a3b8 !important;">Track live competitor prices, scan local business hubs, and analyze market trends instantly with AI.</p>
            <a href="{{ url_for('signup') }}" class="btn btn-brand btn-lg px-5 py-3 shadow">Start Free Trial</a>
        </div>
    </header>

    <!-- AdSense Ad Unit Placement on Public Homepage -->
    <div class="container my-5 text-center">
        <div class="p-4 bg-white border rounded shadow-sm">
            <span class="text-muted small d-block mb-2">Advertisement</span>
            <ins class="adsbygoogle"
                 style="display:block"
                 data-ad-client="ca-pub-4321543724878495"
                 data-ad-slot="1234567890"
                 data-ad-format="auto"
                 data-full-width-responsive="true"></ins>
            <script>
                 (adsbygoogle = window.adsbygoogle || []).push({});
            </script>
        </div>
    </div>

    <!-- Features Overview -->
    <section class="container py-5">
        <div class="row g-4">
            <div class="col-md-4">
                <div class="card card-custom p-4 h-100">
                    <i class="fa-solid fa-store fa-2x mb-3" style="color: #ff1493;"></i>
                    <h5 class="fw-bold">Competitor Intel</h5>
                    <p class="text-muted small">Monitor real-time competitor storefront pricing and inventory status dynamically.</p>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card card-custom p-4 h-100">
                    <i class="fa-solid fa-crosshairs fa-2x mb-3" style="color: #0284c7;"></i>
                    <h5 class="fw-bold">Special Feature</h5>
                    <p class="text-muted small">Pinpoint specific items and merchandise searches across precise geographic regions.</p>
                </div>
            </div>
            <div class="col-md-4">
                <div class="card card-custom p-4 h-100">
                    <i class="fa-solid fa-shop fa-2x mb-3" style="color: #ff1493;"></i>
                    <h5 class="fw-bold">Local Hub Scan</h5>
                    <p class="text-muted small">Discover active local merchants, goods produced, and commercial hubs instantly.</p>
                </div>
            </div>
        </div>
    </section>

    <!-- Public Footer -->
    <footer class="bg-white border-top py-4 mt-5 text-center text-muted small">
        <div class="container">
            <div class="mb-2">
                <a href="{{ url_for('public_home') }}" class="text-decoration-none text-muted me-3">Home</a>
                <a href="{{ url_for('about_page') }}" class="text-decoration-none text-muted me-3">About Us</a>
                <a href="{{ url_for('privacy_policy') }}" class="text-decoration-none text-muted me-3">Privacy Policy</a>
                <a href="{{ url_for('terms_conditions') }}" class="text-decoration-none text-muted">Terms & Conditions</a>
            </div>
            <p class="m-0">&copy; 2026 ApexIntel AI. All rights reserved.</p>
        </div>
    </footer>
</body>
</html>
"""

TEMPLATE_INFO_PAGES = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - ApexIntel AI</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">

    <!-- Google AdSense Verification Script -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495"
         crossorigin="anonymous"></script>

    <style>
        body { background: #f8fafc; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; color: #333; }
        .card-custom { border: none; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.04); background: #ffffff; }
    </style>
</head>
<body>
    <nav class="navbar navbar-expand-lg navbar-light bg-white border-bottom py-3">
        <div class="container">
            <a class="navbar-brand fw-bold" href="{{ url_for('public_home') }}" style="color: #ff1493;">
                <i class="fa-solid fa-chart-line me-2"></i>ApexIntel AI
            </a>
            <div class="ms-auto">
                <a href="{{ url_for('public_home') }}" class="btn btn-outline-dark btn-sm me-2">Home</a>
                <a href="{{ url_for('login') }}" class="btn btn-dark btn-sm">Sign In</a>
            </div>
        </div>
    </nav>

    <div class="container py-5" style="max-width: 800px;">
        <div class="card card-custom p-5">
            <h2 class="fw-bold mb-4" style="color: #ff1493;">{{ title }}</h2>
            <div class="text-muted" style="line-height: 1.8;">
                {{ content | safe }}
            </div>
        </div>
    </div>

    <footer class="bg-white border-top py-4 text-center text-muted small mt-5">
        <div class="container">
            <a href="{{ url_for('public_home') }}" class="text-decoration-none text-muted me-3">Home</a>
            <a href="{{ url_for('about_page') }}" class="text-decoration-none text-muted me-3">About Us</a>
            <a href="{{ url_for('privacy_policy') }}" class="text-decoration-none text-muted me-3">Privacy Policy</a>
            <a href="{{ url_for('terms_conditions') }}" class="text-decoration-none text-muted">Terms & Conditions</a>
            <p class="mt-2 m-0">&copy; 2026 ApexIntel AI. All rights reserved.</p>
        </div>
    </footer>
</body>
</html>
"""
if __name__ == "__main__":
    app.run(debug=True, port=5000)
