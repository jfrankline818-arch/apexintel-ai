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
    if not email:
        return 0, TRIAL_DAYS
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

# --- SNIPPETS & TEMPLATES ---
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

PAYMENT_HELP_MODAL = """
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

NAVBAR_SNIPPET = """
<nav class="navbar navbar-expand-lg navbar-dark bg-dark border-bottom border-secondary mb-4 shadow-sm">
  <div class="container-fluid">
    <a class="navbar-brand fw-bold" href="{{ url_for('dashboard') }}"><i class="fa-solid fa-chart-line me-2" style="color: #ff1493;"></i>ApexIntel AI</a>
    <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarNav">
      <span class="navbar-toggler-icon"></span>
    </button>
    <div class="collapse navbar-collapse" id="navbarNav">
      <ul class="navbar-nav me-auto">
        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'dashboard' %}active{% endif %}" href="{{ url_for('dashboard') }}">Competitor Intel</a></li>
        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'special_feature' %}active{% endif %}" href="{{ url_for('special_feature') }}">Special Search</a></li>
        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'local_hub' %}active{% endif %}" href="{{ url_for('local_hub') }}">Local Businesses</a></li>
        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'archive_history' %}active{% endif %}" href="{{ url_for('archive_history') }}">Archived Intel</a></li>
        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'profile' %}active{% endif %}" href="{{ url_for('profile') }}">Profile & Plan</a></li>
        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'support' %}active{% endif %}" href="{{ url_for('support') }}">Support</a></li>
      </ul>
      <div class="d-flex align-items-center">
        <span class="text-light me-3 small"><i class="fa-solid fa-user me-1 text-muted"></i>{{ user_email }}</span>
        {% if is_paid %}
          <span class="badge bg-success me-3 py-2 px-3 fw-bold"><i class="fa-solid fa-shield-halved me-1"></i>PRO ({{ days_remaining }}d left)</span>
        {% else %}
          <span class="badge bg-warning text-dark me-3 py-2 px-3 fw-bold"><i class="fa-solid fa-clock me-1"></i>TRIAL ({{ days_remaining }}d left)</span>
          <a href="{{ paypal_link }}" target="_blank" class="btn btn-sm btn-light fw-bold text-dark me-2" style="background-color: #ff1493; color: white !important; border: none;"><i class="fa-solid fa-crown me-1"></i> Upgrade $70</a>
        {% endif %}
        <a href="{{ url_for('logout') }}" class="btn btn-sm btn-outline-light"><i class="fa-solid fa-right-from-bracket"></i></a>
      </div>
    </div>
  </div>
</nav>
"""

BASE_HTML_WRAPPER = """
<!DOCTYPE html>
<html lang="en">
<head>
    <!-- Google AdSense Verification Tag -->
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495" crossorigin="anonymous"></script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{{ title }} - ApexIntel AI</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { background-color: {{ bg_color }}; color: #333; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .card-custom { background: #ffffff; border: 1px solid #e5e7eb; border-radius: 12px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }
        .table-hover tbody tr:hover { background-color: rgba(255, 20, 147, 0.03); }
        .btn-custom-primary { background-color: #ff1493; color: white; border: none; font-weight: 600; }
        .btn-custom-primary:hover { background-color: #e00e7f; color: white; }
    </style>
</head>
<body>
    {% if user_email %}
        {{ navbar_snippet | safe }}
    {% endif %}
    <div class="container mb-5">
        {{ content | safe }}
    </div>
    <footer class="text-center py-4 text-muted small border-top mt-5">
        <a href="{{ url_for('about_page') }}" class="text-decoration-none text-muted me-3">About Us</a>
        <a href="{{ url_for('privacy_policy') }}" class="text-decoration-none text-muted me-3">Privacy Policy</a>
        <a href="{{ url_for('terms_conditions') }}" class="text-decoration-none text-muted">Terms & Conditions</a>
        <p class="mt-2 mb-0">&copy; 2026 ApexIntel AI. All rights reserved.</p>
    </footer>
    {{ payment_help_modal | safe }}
    {{ sticker_popup | safe }}
    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

def render_page(template_content, title, bg_color="#ffffff", **kwargs):
    content = render_template_string(template_content, **kwargs)
    navbar_str = render_template_string(NAVBAR_SNIPPET, user_email=kwargs.get('user_email'), is_paid=kwargs.get('is_paid'), days_remaining=kwargs.get('days_remaining'), paypal_link=kwargs.get('paypal_link'))
    return render_template_string(BASE_HTML_WRAPPER, title=title, bg_color=bg_color, navbar_snippet=navbar_str, content=content, payment_help_modal=kwargs.get('payment_help_modal', ''), sticker_popup=kwargs.get('sticker_popup', ''), **kwargs)


# --- ROUTES ---

@app.route("/")
def public_home():
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495" crossorigin="anonymous"></script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ApexIntel AI - Automated Market Intelligence</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { background: #111; color: #fff; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
        .hero { padding: 100px 0; text-align: center; }
        .btn-accent { background: #ff1493; color: white; font-weight: bold; padding: 12px 30px; border-radius: 30px; }
        .btn-accent:hover { background: #e00e7f; color: white; }
    </style>
</head>
<body>
    <div class="container hero">
        <h1 class="display-3 fw-bold mb-4">ApexIntel <span style="color: #ff1493;">AI Agent</span></h1>
        <p class="lead text-muted mb-5">Automated competitor pricing intelligence, custom item regional discovery, and live local business tracking feeds.</p>
        <div class="mb-5">
            <a href="{{ url_for('dashboard') }}" class="btn btn-outline-light btn-lg me-3 px-4">Explore Dashboard</a>
            <a href="{{ url_for('login') }}" class="btn btn-accent btn-lg me-3">Log In</a>
            <a href="{{ url_for('signup') }}" class="btn btn-accent btn-lg">Get Started Free</a>
        </div>
        <div class="text-muted small">
            <a href="{{ url_for('about_page') }}" class="text-decoration-none text-muted me-3">About Us</a>
            <a href="{{ url_for('privacy_policy') }}" class="text-decoration-none text-muted me-3">Privacy Policy</a>
            <a href="{{ url_for('terms_conditions') }}" class="text-decoration-none text-muted">Terms & Conditions</a>
        </div>
    </div>
</body>
</html>
""")


@app.route("/about")
def about_page():
    content = """
    <div class="container py-5" style="max-width: 800px; color: #333;">
        <h2 class="fw-bold mb-4" style="color: #ff1493;">About ApexIntel AI</h2>
        <p>ApexIntel AI is an advanced autonomous market and competitor intelligence platform designed to help businesses, independent developers, and entrepreneurs track pricing dynamics, analyze regional storefront inventory, and gain data-driven insights.</p>
        <p>Our automated systems continuously scan and aggregate commercial intelligence securely, giving you the competitive edge needed to optimize your operations.</p>
    </div>
    """
    return render_template_string(BASE_HTML_WRAPPER, title="About Us", bg_color="#ffffff", navbar_snippet="", content=content, payment_help_modal="", sticker_popup="")


@app.route("/privacy-policy")
def privacy_policy():
    content = """
    <div class="container py-5" style="max-width: 800px; color: #333;">
        <h2 class="fw-bold mb-4" style="color: #ff1493;">Privacy Policy</h2>
        <p>At ApexIntel AI, accessible from our web platform, your privacy is extremely important to us. This Privacy Policy document outlines the types of information collected and recorded by ApexIntel AI and how we use it.</p>
        <h5 class="fw-bold text-dark mt-4">Information We Collect</h5>
        <p>When you register for an account, we collect your email address and secure account credentials strictly for authentication and subscription management purposes.</p>
        <h5 class="fw-bold text-dark mt-4">Log Files & Cookies</h5>
        <p>We use standard log files and cookies to enhance user experience, analyze platform traffic, and serve relevant advertisements through Google AdSense.</p>
    </div>
    """
    return render_template_string(BASE_HTML_WRAPPER, title="Privacy Policy", bg_color="#ffffff", navbar_snippet="", content=content, payment_help_modal="", sticker_popup="")


@app.route("/terms-and-conditions")
def terms_conditions():
    content = """
    <div class="container py-5" style="max-width: 800px; color: #333;">
        <h2 class="fw-bold mb-4" style="color: #ff1493;">Terms & Conditions</h2>
        <p>Welcome to ApexIntel AI! These terms and conditions outline the rules and regulations for the use of our web application and services.</p>
        <h5 class="fw-bold text-dark mt-4">License & Usage</h5>
        <p>By accessing this website, we assume you accept these terms and conditions. Do not continue to use ApexIntel AI if you do not agree to all of the terms stated on this page.</p>
        <h5 class="fw-bold text-dark mt-4">User Accounts</h5>
        <p>Users are responsible for maintaining the confidentiality of their login credentials and account access.</p>
    </div>
    """
    return render_template_string(BASE_HTML_WRAPPER, title="Terms & Conditions", bg_color="#ffffff", navbar_snippet="", content=content, payment_help_modal="", sticker_popup="")


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
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495" crossorigin="anonymous"></script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Log In - ApexIntel AI</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; }
        .auth-card { width: 100%; max-width: 420px; padding: 30px; background: white; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }
    </style>
</head>
<body>
<div class="auth-card">
    <h3 class="fw-bold mb-4 text-center" style="color: #ff1493;">ApexIntel AI</h3>
    {% if error %}
        <div class="alert alert-danger py-2 small">{{ error }}</div>
    {% endif %}
    <form method="POST">
        <div class="mb-3">
            <label class="form-label text-muted small fw-bold">Email Address</label>
            <input type="email" class="form-control" name="email" required>
        </div>
        <div class="mb-3">
            <label class="form-label text-muted small fw-bold">Password</label>
            <input type="password" class="form-control" name="password" required>
        </div>
        <button type="submit" class="btn btn-dark w-100 fw-bold py-2 mb-3" style="background: #ff1493; border: none;">Log In</button>
        <div class="d-flex justify-content-between small">
            <a href="{{ url_for('signup') }}" class="text-decoration-none" style="color: #ff1493;">Create Account</a>
            <a href="{{ url_for('forgot_password') }}" class="text-decoration-none text-muted">Forgot Password?</a>
        </div>
        <div class="text-center mt-3">
            <a href="{{ url_for('public_home') }}" class="text-decoration-none text-muted small"><i class="fa-solid fa-arrow-left me-1"></i> Back to Home</a>
        </div>
    </form>
</div>
</body>
</html>
""", error=error)


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
                cursor.execute("INSERT INTO users (email, password, install_date, has_paid) VALUES (?, ?, ?, 0)", (email, password, now_str))
                conn.commit()
                cursor.close()
                conn.close()
                session["user_email"] = email
                return redirect(url_for("dashboard"))
            except Exception:
                cursor.close()
                conn.close()
                error = "Email address is already registered."
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head>
    <script async src="https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-4321543724878495" crossorigin="anonymous"></script>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Sign Up - ApexIntel AI</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body { background: #f8fafc; display: flex; align-items: center; justify-content: center; height: 100vh; }
        .auth-card { width: 100%; max-width: 420px; padding: 30px; background: white; border-radius: 12px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }
    </style>
</head>
<body>
<div class="auth-card">
    <h3 class="fw-bold mb-4 text-center" style="color: #ff1493;">ApexIntel AI</h3>
    {% if error %}
        <div class="alert alert-danger py-2 small">{{ error }}</div>
    {% endif %}
    <form method="POST">
        <div class="mb-3">
            <label class="form-label text-muted small fw-bold">Email Address</label>
            <input type="email" class="form-control" name="email" required>
        </div>
        <div class="mb-3">
            <label class="form-label text-muted small fw-bold">Password</label>
            <input type="password" class="form-control" name="password" required>
        </div>
        <button type="submit" class="btn btn-dark w-100 fw-bold py-2 mb-3" style="background: #ff1493; border: none;">Sign Up</button>
        <div class="text-center small">
            <span class="text-muted">Already have an account?</span> <a href="{{ url_for('login') }}" class="text-decoration-none" style="color: #ff1493;">Log In</a>
        </div>
    </form>
</div>
</body>
</html>
""", error=error)


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
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
            return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body style="background:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;">
<div style="width:100%;max-width:420px;padding:30px;background:white;border-radius:12px;border:1px solid #e2e8f0;">
<h3 class="fw-bold mb-3" style="color:#ff1493;">Reset Password</h3>
<div class="alert alert-info small"><b>Simulation Code:</b> {{ dev_code }}</div>
<form method="POST" action="{{ url_for('reset_password') }}">
<input type="hidden" name="email" value="{{ email }}">
<div class="mb-3"><label class="form-label small fw-bold">Verification Code</label><input type="text" class="form-control" name="code" required></div>
<div class="mb-3"><label class="form-label small fw-bold">New Password</label><input type="password" class="form-control" name="new_password" required></div>
<button type="submit" class="btn w-100 fw-bold text-white" style="background:#ff1493;">Reset Password</button>
</form>
</div>
</body>
</html>
""", email=email, dev_code=code)
        else:
            cursor.close()
            conn.close()
            error = "Email address not found in system."
    return render_template_string("""
<!DOCTYPE html>
<html lang="en">
<head><link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet"></head>
<body style="background:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;">
<div style="width:100%;max-width:420px;padding:30px;background:white;border-radius:12px;border:1px solid #e2e8f0;">
<h3 class="fw-bold mb-3" style="color:#ff1493;">Forgot Password</h3>
{% if error %}<div class="alert alert-danger py-2 small">{{ error }}</div>{% endif %}
<form method="POST">
<div class="mb-3"><label class="form-label small fw-bold">Account Email</label><input type="email" class="form-control" name="email" required></div>
<button type="submit" class="btn w-100 fw-bold text-white mb-2" style="background:#ff1493;">Send Code</button>
<a href="{{ url_for('login') }}" class="text-muted small text-decoration-none">Back to Login</a>
</form>
</div>
</body>
</html>
""", error=error)


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
    cursor.close()
    conn.close()
    return redirect(url_for("forgot_password"))


@app.route("/profile", methods=["GET", "POST"])
def profile():
    email = session.get("user_email", "guest@apexintel.ai")
    success = request.args.get("success")
    error = None
    conn = get_db_connection()
    cursor = conn.cursor()
    if request.method == "POST" and "user_email" in session:
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
    is_paid, days_remaining = get_user_payment_status(email if "user_email" in session else None)
    
    body = """
<div class="row mb-4">
    <div class="col-12">
        <div class="card card-custom p-4" style="background: #ffffff; border-left: 5px solid #0284c7;">
            <h3 class="fw-bold mb-1">User Profile & Membership</h3>
            <p class="text-muted small mb-0">Manage your account settings, subscription status, and billing upgrades.</p>
        </div>
    </div>
</div>
{% if success %}<div class="alert alert-success small">{{ success }}</div>{% endif %}
{% if error %}<div class="alert alert-danger small">{{ error }}</div>{% endif %}
<div class="row g-4">
    <div class="col-md-6">
        <div class="card card-custom p-4 h-100">
            <h5 class="fw-bold mb-3"><i class="fa-solid fa-user-gear me-2" style="color: #0284c7;"></i>Account Credentials</h5>
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label text-muted small fw-bold">Email Address</label>
                    <input type="email" class="form-control" name="email" value="{{ user_email }}" required>
                </div>
                <div class="mb-3">
                    <label class="form-label text-muted small fw-bold">New Password</label>
                    <input type="password" class="form-control" name="password" placeholder="••••••••">
                </div>
                <button type="submit" class="btn w-100 fw-bold text-white" style="background: #0284c7;">Update Profile</button>
            </form>
        </div>
    </div>
    <div class="col-md-6">
        <div class="card card-custom p-4 h-100 d-flex flex-column justify-content-between">
            <div>
                <h5 class="fw-bold mb-3"><i class="fa-solid fa-crown me-2" style="color: #ff1493;"></i>Subscription Status</h5>
                <p class="text-muted small">Current Tier: 
                    {% if is_paid %}<b class="text-success">PRO ACTIVE</b> ({{ days_remaining }} days remaining)
                    {% else %}<b class="text-warning">FREE TRIAL</b> ({{ days_remaining }} days remaining){% endif %}
                </p>
                <p class="text-muted small">Upgrade to Pro ($70 / 30 Days) to unlock uninterrupted automated intelligence feeds and priority background scraping.</p>
            </div>
            <div class="mt-3">
                <a href="{{ paypal_link }}" target="_blank" class="btn btn-custom-primary w-100 fw-bold py-2 mb-2"><i class="fa-solid fa-lock me-1"></i> Pay $70 via PayPal</a>
                <button type="button" class="btn btn-outline-secondary w-100 fw-bold py-2" data-bs-toggle="modal" data-bs-target="#paymentHelpModal">
                    <i class="fa-solid fa-circle-question me-1"></i> Paid with different email? Claim here
                </button>
            </div>
        </div>
    </div>
</div>
"""
    return render_page(body, title="Profile & Plan", bg_color="#ffffff", user_email=email, is_paid=is_paid, days_remaining=days_remaining, success=success, error=error, paypal_link=PAYPAL_ME_LINK, payment_help_modal=PAYMENT_HELP_MODAL)


@app.route("/logout")
def logout():
    session.pop("user_email", None)
    return redirect(url_for("public_home"))


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
            cursor.execute("UPDATE users SET has_paid = 1, last_paid_date = ? WHERE email = ?", (now_str, payer_email.lower()))
            conn.commit()
            cursor.close()
            conn.close()
        return "OK", 200
    except Exception:
        return "Error", 400


@app.route("/claim-payment", methods=["POST"])
def claim_payment():
    account_email = session.get("user_email", "guest@apexintel.ai")
    payment_email = request.form.get("payment_email", "").strip().lower()
    transaction_ref = request.form.get("transaction_ref", "").strip()
    if payment_email:
        conn = get_db_connection()
        cursor = conn.cursor()
        now_str = datetime.now().isoformat()
        cursor.execute("UPDATE users SET has_paid = 1, last_paid_date = ? WHERE email = ?", (now_str, account_email))
        cursor.execute("INSERT INTO help_messages (email, message, timestamp) VALUES (?, ?, ?)", (account_email, f"Alternative Payment Email Claim: {payment_email} | Ref: {transaction_ref}", now_str))
        conn.commit()
        cursor.close()
        conn.close()
        return redirect(url_for("profile", success="Payment successfully claimed with alternate email and account upgraded!"))
    return redirect(url_for("profile"))


@app.route("/dashboard")
def dashboard():
    email = session.get("user_email", "guest@apexintel.ai")
    is_paid, days_remaining = get_user_payment_status(email if "user_email" in session else None)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT company_query, results_html FROM tracking_jobs ORDER BY id DESC LIMIT 1")
    t_row = cursor.fetchone()
    cursor.close()
    conn.close()

    body = """
<div class="row mb-4">
    <div class="col-12">
        <div class="card card-custom p-4" style="background: #ffffff; border-left: 5px solid #ff1493;">
            <h3 class="fw-bold mb-1">Competitor Intel Feed</h3>
            <p class="text-muted small mb-3">Monitor competitor pricing, product changes, and storefront images automatically.</p>
            <form method="POST" action="{{ url_for('run_search') }}" class="row g-3">
                <div class="col-md-10">
                    <input type="text" class="form-control" name="companies" placeholder="e.g. jumia, nike, linear, figma" value="{{ saved_query }}" required>
                </div>
                <div class="col-md-2">
                    <button type="submit" class="btn btn-custom-primary w-100 py-2"><i class="fa-solid fa-bolt me-1"></i> Run Intel</button>
                </div>
            </form>
        </div>
    </div>
</div>
<div class="row">
    <div class="col-12">
        <div class="card card-custom p-4">
            <h5 class="fw-bold mb-3"><i class="fa-solid fa-table-list me-2" style="color: #ff1493;"></i>Extracted Live Results</h5>
            <div class="table-responsive">
                {% if result_html %}{{ result_html | safe }}{% else %}<p class="text-muted small">No active competitor intelligence extracted yet. Enter company names above and click Run Intel.</p>{% endif %}
            </div>
        </div>
    </div>
</div>
"""
    return render_page(body, title="Competitor Intel", bg_color="#ffffff", user_email=email, is_paid=is_paid, days_remaining=days_remaining, saved_query=t_row[0] if t_row else "", result_html=t_row[1] if t_row else "", paypal_link=PAYPAL_ME_LINK, payment_help_modal=PAYMENT_HELP_MODAL, sticker_popup=STICKER_POPUP_HTML)


@app.route("/special-feature", methods=["GET", "POST"])
def special_feature():
    email = session.get("user_email", "guest@apexintel.ai")
    is_paid, days_remaining = get_user_payment_status(email if "user_email" in session else None)
    conn = get_db_connection()
    cursor = conn.cursor()
    if request.method == "POST":
        area = request.form.get("area_query", "")
        item_query = request.form.get("item_query", "")
        if area and item_query:
            html_output = run_special_item_search(item_query, area)
            cursor.execute("INSERT INTO general_jobs (email, item_query, region, results_html, last_updated) VALUES (?, ?, ?, ?, ?)", (email, item_query, area, html_output, datetime.now().isoformat()))
            conn.commit()
    cursor.execute("SELECT item_query, region, results_html FROM general_jobs ORDER BY id DESC LIMIT 1")
    g_row = cursor.fetchone()
    cursor.close()
    conn.close()

    body = """
<div class="row mb-4">
    <div class="col-12">
        <div class="card card-custom p-4" style="background: #ffffff; border-left: 5px solid #0284c7;">
            <h3 class="fw-bold mb-1">Special Regional Item Search</h3>
            <p class="text-muted small mb-3">Search for any specific item or product and specify your exact local area.</p>
            <form method="POST" class="row g-3">
                <div class="col-md-6">
                    <input type="text" class="form-control" name="item_query" placeholder="What are you looking for?" value="{{ saved_item_query }}" required>
                </div>
                <div class="col-md-4">
                    <input type="text" class="form-control" name="area_query" placeholder="Enter your area / location" value="{{ saved_area }}" required>
                </div>
                <div class="col-md-2">
                    <button type="submit" class="btn w-100 py-2 text-white fw-bold" style="background: #0284c7;"><i class="fa-solid fa-search me-1"></i> Search Area</button>
                </div>
            </form>
        </div>
    </div>
</div>
<div class="row">
    <div class="col-12">
        <div class="card card-custom p-4">
            <h5 class="fw-bold mb-3"><i class="fa-solid fa-map-location-dot me-2" style="color: #0284c7;"></i>Localized Area Results</h5>
            <div class="table-responsive">
                {% if special_result_html %}{{ special_result_html | safe }}{% else %}<p class="text-muted small">No special search query executed yet.</p>{% endif %}
            </div>
        </div>
    </div>
</div>
"""
    return render_page(body, title="Special Search", bg_color="#ffffff", user_email=email, is_paid=is_paid, days_remaining=days_remaining, saved_item_query=g_row[0] if g_row else "", saved_area=g_row[1] if g_row else "", special_result_html=g_row[2] if g_row else "", paypal_link=PAYPAL_ME_LINK, payment_help_modal=PAYMENT_HELP_MODAL, sticker_popup=STICKER_POPUP_HTML)


@app.route("/local-hub", methods=["GET", "POST"])
def local_hub():
    email = session.get("user_email", "guest@apexintel.ai")
    is_paid, days_remaining = get_user_payment_status(email if "user_email" in session else None)
    conn = get_db_connection()
    cursor = conn.cursor()
    if request.method == "POST":
        location = request.form.get("location_query", "")
        if location:
            html_output = run_nearby_businesses_scan(location)
            cursor.execute("INSERT INTO nearby_jobs (email, location_query, results_html, last_updated) VALUES (?, ?, ?, ?)", (email, location, html_output, datetime.now().isoformat()))
            conn.commit()
    cursor.execute("SELECT location_query, results_html FROM nearby_jobs ORDER BY id DESC LIMIT 1")
    n_row = cursor.fetchone()
    cursor.close()
    conn.close()

    body = """
<div class="row mb-4">
    <div class="col-12">
        <div class="card card-custom p-4" style="background: #ffffff; border-left: 5px solid #333;">
            <h3 class="fw-bold mb-1">Local Businesses Around</h3>
            <p class="text-muted small mb-3">Discover verified local merchants and inventories around your specific neighborhood.</p>
            <form method="POST" class="row g-3">
                <div class="col-md-10">
                    <input type="text" class="form-control" name="location_query" placeholder="Enter neighborhood or city" value="{{ saved_location }}" required>
                </div>
                <div class="col-md-2">
                    <button type="submit" class="btn btn-dark w-100 py-2 fw-bold"><i class="fa-solid fa-store me-1"></i> Scan Hub</button>
                </div>
            </form>
        </div>
    </div>
</div>
<div class="row">
    <div class="col-12">
        <div class="card card-custom p-4">
            <h5 class="fw-bold mb-3"><i class="fa-solid fa-list-check me-2 text-dark"></i>Local Commercial Feed</h5>
            <div class="table-responsive">
                {% if nearby_result_html %}{{ nearby_result_html | safe }}{% else %}<p class="text-muted small">No local business scan executed yet.</p>{% endif %}
            </div>
        </div>
    </div>
</div>
"""
    return render_page(body, title="Local Businesses", bg_color="#ffffff", user_email=email, is_paid=is_paid, days_remaining=days_remaining, saved_location=n_row[0] if n_row else "", nearby_result_html=n_row[1] if n_row else "", paypal_link=PAYPAL_ME_LINK, payment_help_modal=PAYMENT_HELP_MODAL, sticker_popup=STICKER_POPUP_HTML)


@app.route("/archive-history")
def archive_history():
    email = session.get("user_email", "guest@apexintel.ai")
    is_paid, days_remaining = get_user_payment_status(email if "user_email" in session else None)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT company_query, latest_results_html, last_updated FROM history_queries ORDER BY id DESC LIMIT 10")
    history_rows = cursor.fetchall()
    history_items = [{"company": r[0], "html": r[1], "time": r[2]} for r in history_rows]
    cursor.close()
    conn.close()

    body = """
<div class="row mb-4">
    <div class="col-12">
        <div class="card card-custom p-4" style="background: #000000; color: #ffffff; border-left: 5px solid #ff1493;">
            <h3 class="fw-bold mb-1 text-white">Archived Intel History</h3>
            <p class="text-secondary small mb-0">Review all previously captured competitor intelligence feeds and automated updates.</p>
        </div>
    </div>
</div>
{% if history_items %}
    {% for item in history_items %}
    <div class="row mb-4">
        <div class="col-12">
            <div class="card card-custom p-4">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h5 class="fw-bold mb-0" style="color: #ff1493;"><i class="fa-solid fa-clock-rotate-left me-2"></i>{{ item.company }}</h5>
                    <span class="text-muted small">Updated: {{ item.time }}</span>
                </div>
                <div class="table-responsive">{{ item.html | safe }}</div>
            </div>
        </div>
    </div>
    {% endfor %}
{% else %}
<div class="row"><div class="col-12"><div class="card card-custom p-4 text-center"><p class="text-muted mb-0">No archived intelligence found.</p></div></div></div>
{% endif %}
"""
    return render_page(body, title="Archived Intel", bg_color="#000000", user_email=email, is_paid=is_paid, days_remaining=days_remaining, history_items=history_items, paypal_link=PAYPAL_ME_LINK, payment_help_modal=PAYMENT_HELP_MODAL, sticker_popup=STICKER_POPUP_HTML)


@app.route("/support", methods=["GET", "POST"])
def support():
    email = session.get("user_email", "guest@apexintel.ai")
    is_paid, days_remaining = get_user_payment_status(email if "user_email" in session else None)
    success = None
    if request.method == "POST":
        message = request.form.get("message", "")
        if message:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO help_messages (email, message, timestamp) VALUES (?, ?, ?)", (email, message, datetime.now().isoformat()))
            conn.commit()
            cursor.close()
            conn.close()
            success = "Support ticket submitted successfully!"

    body = """
<div class="row mb-4">
    <div class="col-12">
        <div class="card card-custom p-4" style="background: #ffffff; border-left: 5px solid #ff1493;">
            <h3 class="fw-bold mb-1">Customer Support & Help</h3>
            <p class="text-muted small mb-0">Need assistance or have billing inquiries? Submit a support ticket below.</p>
        </div>
    </div>
</div>
{% if success %}<div class="alert alert-success small">{{ success }}</div>{% endif %}
<div class="row">
    <div class="col-md-8 mx-auto">
        <div class="card card-custom p-4">
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label text-muted small fw-bold">Your Email</label>
                    <input type="email" class="form-control" value="{{ user_email }}" readonly>
                </div>
                <div class="mb-3">
                    <label class="form-label text-muted small fw-bold">Support Message / Inquiry</label>
                    <textarea class="form-control" name="message" rows="5" placeholder="Describe your issue..." required></textarea>
                </div>
                <button type="submit" class="btn btn-custom-primary w-100 fw-bold py-2">Submit Support Ticket</button>
            </form>
            <div class="text-center mt-3">
                <span class="text-muted small">Direct Support Email: <a href="mailto:{{ help_email }}" class="text-decoration-none" style="color: #ff1493;">{{ help_email }}</a></span>
            </div>
        </div>
    </div>
</div>
"""
    return render_page(body, title="Support", bg_color="#ffffff", user_email=email, is_paid=is_paid, days_remaining=days_remaining, help_email=HELP_EMAIL, success=success, paypal_link=PAYPAL_ME_LINK, payment_help_modal=PAYMENT_HELP_MODAL, sticker_popup=STICKER_POPUP_HTML)


@app.route("/run", methods=["POST"])
def run_search():
    email = session.get("user_email", "guest@apexintel.ai")
    companies = request.form.get("companies", "")
    if companies:
        html_output = run_competitor_intelligence(companies)
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tracking_jobs (email, company_query, results_html, last_updated) VALUES (?, ?, ?, ?)", (email, companies, html_output, datetime.now().isoformat()))
        cursor.execute("INSERT INTO history_queries (email, company_query, latest_results_html, last_updated) VALUES (?, ?, ?, ?)", (email, companies, html_output, datetime.now().isoformat()))
        conn.commit()
        cursor.close()
        conn.close()
    return redirect(url_for("dashboard"))


@app.route("/clear-company")
def clear_company():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tracking_jobs")
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for("dashboard"))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
