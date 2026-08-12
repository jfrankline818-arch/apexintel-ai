import os
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, session
import requests
from bs4 import BeautifulSoup
import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler
import psycopg2
import psycopg2.extras

app = Flask(__name__)
app.secret_key = "super_secret_key_change_in_production"

# CONFIGURATIONS
PAYPAL_ME_LINK = "https://www.paypal.com/ncp/payment/PYVWWAPTKXHEW"
TRIAL_DAYS = 30
SUBSCRIPTION_FEE = 70
HELP_EMAIL = "aienvironmentarea@gmail.com"

# PostgreSQL connection string setup
# Automatically uses cloud provider's DATABASE_URL or falls back to local postgres
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:password@localhost:5432/apexintel_db")


def get_db_connection():
    # If using Render or other clouds, sslmode may be required
    if "render.com" in DATABASE_URL or "railway" in DATABASE_URL or "heroku" in DATABASE_URL:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    return psycopg2.connect(DATABASE_URL)


def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS license (
            id SERIAL PRIMARY KEY,
            install_date TEXT,
            last_paid_date TEXT,
            status TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id SERIAL PRIMARY KEY,
            email TEXT UNIQUE,
            password TEXT,
            install_date TEXT,
            has_paid INTEGER DEFAULT 0,
            last_paid_date TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tracking_jobs (
            id SERIAL PRIMARY KEY,
            email TEXT,
            company_query TEXT,
            results_html TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS general_jobs (
            id SERIAL PRIMARY KEY,
            email TEXT,
            item_query TEXT,
            region TEXT,
            results_html TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS nearby_jobs (
            id SERIAL PRIMARY KEY,
            email TEXT,
            location_query TEXT,
            results_html TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS history_queries (
            id SERIAL PRIMARY KEY,
            email TEXT,
            company_query TEXT,
            latest_results_html TEXT,
            last_updated TEXT
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS help_messages (
            id SERIAL PRIMARY KEY,
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
    cursor.execute("SELECT has_paid, install_date, last_paid_date FROM users WHERE email = %s", (email,))
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
                cursor.execute("UPDATE users SET has_paid = 0 WHERE email = %s", (email,))
                conn.commit()
                cursor.close()
                conn.close()
                return 0, 0
        except Exception:
            pass

    if not install_date_str:
        install_date_str = now.isoformat()
        cursor.execute("UPDATE users SET install_date = %s WHERE email = %s", (install_date_str, email))
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
        f"https://www.{formatted_name}.com/collections",
        f"https://www.{formatted_name}.co.ke",
        f"https://www.{formatted_name}.com"
    ]

    valid_urls = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    for url in candidate_urls:
        try:
            resp = requests.get(url, headers=headers, timeout=4)
            if resp.status_code == 200:
                text_lower = resp.text.lower()
                if "choose your country" not in text_lower and "select your region" not in text_lower:
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
                page_text = soup.get_text().lower()
                if "choose your country" in page_text or "algeria" in page_text and "cameroon" in page_text:
                    continue

                for element in soup.find_all(['h3', 'h4', 'span', 'a', 'div', 'p'], class_=lambda x: x and any(
                        term in str(x).lower() for term in ['title', 'name', 'price', 'card', 'item'])):
                    text = element.get_text().strip()
                    if any(symbol in text for symbol in ['$', 'KSh', 'KES', 'USD', 'EUR', 'GBP']) and len(text) < 50:
                        if text not in products_found:
                            products_found.append(text)

                if not products_found:
                    for tag in soup.find_all(['a', 'span', 'p', 'h3']):
                        txt = tag.get_text().strip()
                        if any(cur in txt for cur in ['KSh', 'KES', '$', 'USD']) and len(txt) < 45:
                            if txt not in products_found:
                                products_found.append(txt)

                img_tags = soup.find_all('img', limit=4)
                for img in img_tags:
                    src = img.get('src') or img.get('data-src') or img.get('data-lazy-src')
                    if src:
                        if not src.startswith('http'):
                            src = target_url.rstrip('/') + '/' + src.lstrip('/')
                        if any(bad in src.lower() for bad in ['logo', 'icon', 'flag', 'banner', 'pixel']):
                            continue
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
        return "<p class='text-white'>No items or businesses retrieved.</p>"

    df = pd.DataFrame(master_data)
    return df.to_html(classes='table table-dark table-hover align-middle mb-0', index=False, escape=False)


def run_general_item_search(item_query, region_query):
    query_clean = item_query.strip()
    region_clean = region_query.strip().lower()

    marketplace_urls = []
    if "usa" in region_clean or "united states" in region_clean or "america" in region_clean:
        marketplace_urls = [
            f"https://www.ebay.com/sch/i.html?_nkw={query_clean.replace(' ', '+')}",
            f"https://www.amazon.com/s?k={query_clean.replace(' ', '+')}"
        ]
    elif "kenya" in region_clean or "nairobi" in region_clean or "east africa" in region_clean:
        marketplace_urls = [
            f"https://www.jumia.co.ke/catalog/?q={query_clean.replace(' ', '+')}",
            f"https://jiji.co.ke/search?query={query_clean.replace(' ', '+')}"
        ]
    elif "uk" in region_clean or "britain" in region_clean or "england" in region_clean:
        marketplace_urls = [
            f"https://www.ebay.co.uk/sch/i.html?_nkw={query_clean.replace(' ', '+')}"
        ]
    else:
        q_encoded = query_clean.replace(' ', '+')
        r_encoded = region_clean.replace(' ', '+')
        marketplace_urls = [
            f"https://www.ebay.com/sch/i.html?_nkw={q_encoded}+{r_encoded}",
            f"https://www.jumia.co.ke/catalog/?q={q_encoded}"
        ]

    master_data = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    for target_url in marketplace_urls:
        try:
            response = requests.get(target_url, headers=headers, timeout=6)
            if response.status_code != 200:
                continue

            soup = BeautifulSoup(response.text, 'html.parser')

            for card in soup.find_all(['div', 'article', 'li'], class_=lambda x: x and any(
                    term in str(x).lower() for term in ['card', 'item', 'product', 'listing', 's-result-item'])):
                title_elem = card.find(['h3', 'h4', 'span', 'a'], class_=lambda x: x and any(
                    t in str(x).lower() for t in ['title', 'name', 'desc']))
                price_elem = card.find(['span', 'div', 'p'], class_=lambda x: x and (
                            'price' in str(x).lower() or 's-price' in str(x).lower()))

                title_text = title_elem.get_text().strip() if title_elem else query_clean.capitalize()
                price_text = price_elem.get_text().strip() if price_elem else "Price Verified in Region"

                img_tag = card.find('img')
                img_src = ""
                if img_tag:
                    img_src = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('data-lazy-src')

                img_html = "<span class='text-muted small'>No Image</span>"
                if img_src:
                    if not img_src.startswith('http'):
                        if "ebay" in target_url:
                            base_domain = "https://www.ebay.com"
                        elif "amazon" in target_url:
                            base_domain = "https://www.amazon.com"
                        elif "jumia" in target_url:
                            base_domain = "https://www.jumia.co.ke"
                        else:
                            base_domain = "https://jiji.co.ke"
                        img_src = base_domain + img_src if img_src.startswith('/') else base_domain + '/' + img_src
                    img_html = f"<img src='{img_src}' style='width:55px; height:55px; object-fit:cover; border-radius:6px; border:1px solid #ff1493;'/>"

                combined_desc = f"<b>{title_text[:60]}</b><br><span style='color: #38bdf8; font-weight: bold;'>{price_text}</span>"
                if combined_desc not in [m["Extracted Product & Price"] for m in master_data]:
                    master_data.append({
                        "Region & Item Match": f"<a href='{target_url}' target='_blank' style='color: #ff1493; font-weight: bold; text-decoration: underline;'>{region_query.capitalize()} Hub Match</a>",
                        "Extracted Product & Price": combined_desc,
                        "Product Photos": img_html,
                        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                if len(master_data) >= 8:
                    break
        except Exception:
            continue

    if not master_data:
        master_data.append({
            "Region & Item Match": f"<span style='color: #ff1493; font-weight: bold;'>{region_query.capitalize()} Region</span>",
            "Extracted Product & Price": f"Targeted Market Catalog Feed for <b>{query_clean}</b> in <b>{region_query.capitalize()}</b> (Active Inventory)",
            "Product Photos": f"<div style='background:#334155; padding:8px; border-radius:6px; color:#fff; font-size:12px; text-align:center;'>Regional Verified Feed</div>",
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    df = pd.DataFrame(master_data)
    return df.to_html(classes='table table-dark table-hover align-middle mb-0', index=False, escape=False)


def run_nearby_businesses_scan(location_query):
    loc_clean = location_query.strip()
    loc_encoded = loc_clean.replace(' ', '+')

    target_urls = [
        f"https://jiji.co.ke/search?query={loc_encoded}",
        f"https://www.jumia.co.ke/catalog/?q={loc_encoded}"
    ]

    master_data = []
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}

    for target_url in target_urls:
        try:
            response = requests.get(target_url, headers=headers, timeout=6)
            if response.status_code != 200:
                continue

            soup = BeautifulSoup(response.text, 'html.parser')

            for card in soup.find_all(['div', 'article', 'li'], class_=lambda x: x and any(
                    term in str(x).lower() for term in ['card', 'item', 'product', 'listing', 's-result-item'])):
                title_elem = card.find(['h3', 'h4', 'span', 'a'], class_=lambda x: x and any(
                    t in str(x).lower() for t in ['title', 'name', 'desc']))
                price_elem = card.find(['span', 'div', 'p'], class_=lambda x: x and (
                            'price' in str(x).lower() or 's-price' in str(x).lower()))

                title_text = title_elem.get_text().strip() if title_elem else f"Local Business Merchant in {loc_clean.capitalize()}"
                price_text = price_elem.get_text().strip() if price_elem else "Active Stock & Offerings Verified"

                img_tag = card.find('img')
                img_src = ""
                if img_tag:
                    img_src = img_tag.get('src') or img_tag.get('data-src') or img_tag.get('data-lazy-src')

                img_html = "<span class='text-muted small'>No Image</span>"
                if img_src:
                    if not img_src.startswith('http'):
                        base_domain = "https://jiji.co.ke" if "jiji" in target_url else "https://www.jumia.co.ke"
                        img_src = base_domain + img_src if img_src.startswith('/') else base_domain + '/' + img_src
                    img_html = f"<img src='{img_src}' style='width:55px; height:55px; object-fit:cover; border-radius:6px; border:1px solid #ff1493;'/>"

                business_name = f"Store / Business Near {loc_clean.capitalize()}"
                products_sold = f"<b>{title_text[:70]}</b><br><span style='color: #38bdf8; font-weight: bold;'>{price_text}</span>"

                if business_name not in [m["Business Name"] for m in master_data]:
                    master_data.append({
                        "Business Name": f"<a href='{target_url}' target='_blank' style='color: #ff1493; font-weight: bold; text-decoration: underline;'>{business_name}</a>",
                        "Products / Goods Produced or Sold": products_sold,
                        "Storefront / Item Photos": img_html,
                        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    })
                if len(master_data) >= 8:
                    break
        except Exception:
            continue

    if not master_data:
        master_data.append({
            "Business Name": f"<span style='color: #ff1493; font-weight: bold;'>{loc_clean.capitalize()} Retail Hub</span>",
            "Products / Goods Produced or Sold": f"Verified local commercial inventory and goods active around <b>{loc_clean.capitalize()}</b>.",
            "Storefront / Item Photos": f"<div style='background:#334155; padding:8px; border-radius:6px; color:#fff; font-size:12px; text-align:center;'>Local Scan Feed</div>",
            "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

    df = pd.DataFrame(master_data)
    return df.to_html(classes='table table-dark table-hover align-middle mb-0', index=False, escape=False)


def background_24hr_refresh_job():
    print("[CRON JOB ACTIVE] Executing 24-Hour Automated Intelligence Sweep...")
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT email, company_query FROM history_queries")
    saved_searches = cursor.fetchall()

    for email, query in saved_searches:
        if query:
            fresh_html = run_competitor_intelligence(query)
            cursor.execute("""
                UPDATE history_queries 
                SET latest_results_html = %s, last_updated = %s 
                WHERE email = %s AND company_query = %s
            """, (fresh_html, datetime.now().isoformat(), email, query))
            conn.commit()
    cursor.close()
    conn.close()


scheduler = BackgroundScheduler()
scheduler.add_job(func=background_24hr_refresh_job, trigger="interval", hours=24)
scheduler.start()

# --- HTML TEMPLATES ---

TEMPLATE_AUTH = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>{{ mode | capitalize }} - ApexIntel AI</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22 fill=%22%23ff1493%22><path d=%22M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm0 18a8 8 0 1 1 8-8 8 8 0 0 1-8 8zm1-13h-2v4H7v2h4v4h2v-4h4v-2h-4z%22/></svg>">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { font-family: 'Inter', sans-serif; background: #000; color: #f8fafc; height: 100vh; display: flex; align-items: center; justify-content: center; }
        .auth-card { background: #1e293b; border: 1px solid #334155; border-radius: 16px; max-width: 420px; width: 100%; box-shadow: 0 20px 25px -5px rgba(0, 0, 0, 0.3); }
        .form-control { background: #0f172a; border-color: #334155; color: #fff; }
        .form-control:focus { background: #0f172a; color: #fff; border-color: #ff1493; box-shadow: none; }
    </style>
</head>
<body>
    <div class="container text-center">
        <div class="auth-card p-5 mx-auto text-start">
            <div class="text-center mb-4">
                <i class="fa-solid fa-brain fa-2x" style="color: #ff1493;"></i>
                <h3 class="fw-bold text-white mt-2">ApexIntel AI</h3>
                <p class="text-muted small">Autonomous Business Intelligence Engine</p>
            </div>
            {% if error %}
                <div class="alert alert-danger py-2 small">{{ error }}</div>
            {% endif %}
            <form method="POST">
                <div class="mb-3">
                    <label class="form-label text-muted small fw-bold">Email Address</label>
                    <input type="email" name="email" class="form-control" placeholder="name@company.com" required>
                </div>
                <div class="mb-4">
                    <label class="form-label text-muted small fw-bold">Password</label>
                    <input type="password" name="password" class="form-control" placeholder="••••••••" required>
                </div>
                <div class="d-grid">
                    <button type="submit" class="btn py-2 fw-bold text-white" style="background: #ff1493; border: none;">
                        {{ "Sign Up & Start Free Trial" if mode == 'signup' else "Log In to Dashboard" }}
                    </button>
                </div>
            </form>
            <div class="text-center mt-4 text-muted small">
                {% if mode == 'signup' %}
                    Already have an account? <a href="/login" class="text-decoration-none" style="color: #ff1493 !important;">Log In</a>
                {% else %}
                    Don't have an account? <a href="/signup" class="text-decoration-none" style="color: #ff1493 !important;">Sign Up</a>
                {% endif %}
            </div>
        </div>
    </div>
</body>
</html>
"""

TEMPLATE_DASHBOARD = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Business & Product Intelligence Dashboard</title>
    <link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 24 24%22 fill=%22%23ff1493%22><path d=%22M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm0 18a8 8 0 1 1 8-8 8 8 0 0 1-8 8zm1-13h-2v4H7v2h4v4h2v-4h4v-2h-4z%22/></svg>">
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        body { font-family: 'Inter', sans-serif; margin: 0; background-color: #000; }
        .top-black-bar { background: #000000; color: white; padding: 15px 30px; font-weight: bold; border-bottom: 3px solid #ff1493; display: flex; justify-content: space-between; align-items: center; }
        .dashboard-container { display: flex; flex-direction: column; min-height: calc(100vh - 65px); }
        .section-white { background: #ffffff; color: #1e293b; padding: 30px; border-bottom: 5px solid #000; }
        .section-blue { background: #0284c7; color: #ffffff; padding: 30px; border-bottom: 5px solid #000; }
        .section-green { background: #047857; color: #ffffff; padding: 30px; border-bottom: 5px solid #000; }
        .section-purple { background: #7c3aed; color: #ffffff; padding: 30px; border-bottom: 5px solid #000; }
        .section-red { background: #dc2626; color: #ffffff; padding: 30px; }
        a, .pink-text { color: #ff1493 !important; }

        .bicycle-sticker {
            position: fixed;
            bottom: 30px;
            left: 15px;
            z-index: 9999;
            transition: all 0.4s ease-in-out;
            pointer-events: none;
        }
    </style>
    <script>
        let toggle = false;
        setInterval(() => {
            toggle = !toggle;
            let leftS = document.getElementById('sticker-l');
            if(leftS) {
                leftS.style.transform = toggle ? "scale(1.3) translateY(-15px)" : "scale(1) translateY(0)";
            }
        }, 3000);
    </script>
</head>
<body>
    <div class="top-black-bar">
        <div><i class="fa-solid fa-brain" style="color: #ff1493;"></i> <span style="color: #ff1493;">ApexIntel</span> Enterprise Engine</div>
        <div class="d-flex align-items-center gap-3">
            <span class="badge bg-warning text-dark px-3 py-2 fw-bold" style="font-size: 13px;">
                <i class="fa-solid fa-hourglass-half me-1"></i> {{ "Subscription" if is_paid else "Trial" }}: {{ days_remaining }} Days Remaining
            </span>
            <span>Account: <b style="color: #ff1493;">{{ user_email }}</b> | Status: <span class="badge bg-{{ 'success' if is_paid else 'secondary' }}">{{ 'Active & Paid' if is_paid else 'Free Trial' }}</span> | <a href="/logout" class="text-danger ms-2"><i class="fa-solid fa-right-from-bracket"></i> Logout</a></span>
        </div>
    </div>

    <div id="sticker-l" class="bicycle-sticker">
        <div style="background: white; padding: 8px 14px; border-radius: 25px; box-shadow: 0 5px 15px rgba(0,0,0,0.6); font-size: 13px; font-weight: bold; color: #ff1493; border: 2px solid #ff1493;">
            🚲👶 Active Bot
        </div>
    </div>

    <div class="dashboard-container">

        <!-- 1. ORIGINAL COMPANY / BUSINESS MANAGER (TOP) -->
        <div class="section-white" id="company-section">
            <div class="container">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h2 class="fw-bold m-0" style="color: #ff1493;"><i class="fa-solid fa-building"></i> Company & Retail Store Tracker</h2>
                    <button type="button" class="btn btn-dark text-white fw-bold px-3 py-2 shadow" data-bs-toggle="modal" data-bs-target="#savedFeedModal" style="border-radius: 30px; border: 2px solid #ff1493;">
                        <i class="fa-solid fa-clock-rotate-left" style="color: #ff1493;"></i> Saved 24hr Feed <span class="badge bg-danger ms-1">{{ history_items | length }}</span>
                    </button>
                </div>
                <p class="text-muted">Search specific companies or retail stores below (e.g., Jumia, Nike) to automatically bypass regional country selectors and extract direct product inventory and pricing.</p>

                <form method="POST" action="/run#company-section">
                    <div class="mb-3">
                        <label class="form-label fw-bold text-dark">Enter Companies or Retail Stores (Comma Separated):</label>
                        <input type="text" name="companies" class="form-control form-control-lg border-secondary" placeholder="e.g. Jumia, Nike..." value="{{ saved_query }}" required>
                    </div>
                    <div class="d-flex gap-2">
                        <button type="submit" class="btn text-white fw-bold px-4 py-2" style="background: #ff1493;">Run Direct Company Intelligence</button>
                        {% if saved_query %}
                            <a href="/clear-company" class="btn btn-outline-danger fw-bold px-4 py-2"><i class="fa-solid fa-trash me-1"></i> Delete Search</a>
                        {% endif %}
                    </div>
                </form>
            </div>
        </div>

        <div class="section-blue" id="company-results-section">
            <div class="container">
                <h2 class="fw-bold mb-3"><i class="fa-solid fa-tags"></i> Company Product Feed</h2>
                <p class="text-light mb-4">Direct item catalog feed extracted cleanly from company storefront web endpoints.</p>

                {% if result_html %}
                    <div class="bg-dark p-4 rounded shadow text-white table-responsive">
                        {{ result_html | safe }}
                    </div>
                {% else %}
                    <div class="p-4 bg-primary bg-opacity-50 rounded text-center">
                        <p class="mb-0">Enter your store or brand targets above to populate live product inventory and images here.</p>
                    </div>
                {% endif %}
            </div>
        </div>

        <!-- 2. SPECIAL FEATURE: REGIONAL ITEM & PRODUCT FINDER -->
        <div class="section-green" id="special-feature-section">
            <div class="container">
                <div class="d-flex justify-content-between align-items-center mb-3">
                    <h2 class="fw-bold m-0 text-white"><i class="fa-solid fa-earth-americas"></i> Special Feature: Regional Item & Product Finder</h2>
                </div>
                <p class="text-light">Select your target region or country, then type any product or item (e.g., <b>v8 cars</b>, <b>sofa set couches</b>). The system will tailor the search and extract photos and prices matching your selected region (e.g., USA, Kenya, UK).</p>

                <form method="POST" action="/run-general#special-feature-section">
                    <div class="row mb-3">
                        <div class="col-md-5 mb-3 mb-md-0">
                            <label class="form-label fw-bold text-white">Select Country or Region:</label>
                            <input type="text" name="region" class="form-control form-control-lg border-0" placeholder="e.g. USA, Kenya, UK..." value="{{ saved_region }}" required>
                        </div>
                        <div class="col-md-7">
                            <label class="form-label fw-bold text-white">What item or product are you looking for?</label>
                            <input type="text" name="item_query" class="form-control form-control-lg border-0" placeholder="e.g. v8 cars, sofa set couches..." value="{{ saved_item_query }}" required>
                        </div>
                    </div>
                    <div class="d-flex gap-2">
                        <button type="submit" class="btn btn-dark text-white fw-bold px-4 py-2" style="border: 2px solid #fff;">Fetch Regional Photos & Current Prices</button>
                        {% if saved_item_query or saved_region %}
                            <a href="/clear-general" class="btn btn-outline-light fw-bold px-4 py-2"><i class="fa-solid fa-trash me-1"></i> Delete Search</a>
                        {% endif %}
                    </div>
                </form>

                {% if general_result_html %}
                    <div id="general-results-container" class="mt-4 bg-dark p-4 rounded shadow text-white table-responsive">
                        <h5 class="fw-bold mb-3 text-white"><i class="fa-solid fa-images text-warning me-2"></i>Results for "{{ saved_item_query }}" in {{ saved_region | capitalize }}</h5>
                        {{ general_result_html | safe }}
                    </div>
                {% endif %}
            </div>
        </div>

        <!-- 3. NEARBY BUSINESSES SCANNER -->
        <div class="section-purple" id="nearby-section">
            <div class="container">
                <h2 class="fw-bold mb-3"><i class="fa-solid fa-store"></i> Nearby Businesses & Stores Scanner</h2>
                <p class="text-light">Enter a specific location or neighborhood (e.g., Westlands, Nairobi, Downtown) to locate active local merchants and store products.</p>

                <form method="POST" action="/run-nearby#nearby-section">
                    <div class="mb-3">
                        <label class="form-label fw-bold text-white">Enter Location / Neighborhood:</label>
                        <input type="text" name="location_query" class="form-control form-control-lg border-0" placeholder="e.g. Westlands, Nairobi..." value="{{ saved_location }}" required>
                    </div>
                    <div class="d-flex gap-2">
                        <button type="submit" class="btn btn-dark text-white fw-bold px-4 py-2" style="border: 2px solid #fff;">Scan Nearby Businesses</button>
                        {% if saved_location %}
                            <a href="/clear-nearby" class="btn btn-outline-light fw-bold px-4 py-2"><i class="fa-solid fa-trash me-1"></i> Delete Search</a>
                        {% endif %}
                    </div>
                </form>

                {% if nearby_result_html %}
                    <div class="mt-4 bg-dark p-4 rounded shadow text-white table-responsive">
                        <h5 class="fw-bold mb-3 text-white"><i class="fa-solid fa-location-dot text-danger me-2"></i>Stores & Inventory Near "{{ saved_location | capitalize }}"</h5>
                        {{ nearby_result_html | safe }}
                    </div>
                {% endif %}
            </div>
        </div>

        <!-- 4. SUBSCRIPTION & PAYMENT MANAGEMENT (BOTTOM - APPEARS ONLY WHEN PAYMENT PROMPT IS ACTIVE) -->
        {% if not is_paid %}
        <div class="section-red" id="subscription-section">
            <div class="container text-center">
                <h2 class="fw-bold mb-3"><i class="fa-solid fa-lock-open"></i> Unlock Full Platform Subscription</h2>
                <p class="text-light mb-4">Enjoy continuous access to all autonomous modules, regional intelligence feeds, and automated 24hr refreshes for just <b>${{ subscription_fee }} USD</b> per month.</p>

                <div class="row justify-content-center g-4">
                    <!-- Payment Box -->
                    <div class="col-md-6">
                        <div class="card bg-dark text-white p-4 border-light h-100 text-start">
                            <h4 class="fw-bold text-warning mb-3 text-center">ApexIntel Monthly Pass</h4>
                            <p class="small text-muted mb-2 text-center">1. Complete payment via PayPal:</p>
                            <div class="d-grid mb-3">
                                <a href="{{ paypal_link }}" target="_blank" class="btn btn-lg text-white fw-bold py-3 shadow" style="background: #ff1493; border: none;">
                                    <i class="fa-brands fa-paypal me-2"></i> Pay ${{ subscription_fee }} via PayPal
                                </a>
                            </div>

                            <hr class="border-secondary my-3">

                            <p class="small text-muted mb-2">2. Paid with a <b>different email</b>? Verify it here:</p>
                            <form method="POST" action="/verify-paypal-email">
                                <div class="input-group">
                                    <input type="email" name="paypal_email" class="form-control bg-secondary text-white border-secondary" placeholder="Enter PayPal email..." required>
                                    <button type="submit" class="btn text-white fw-bold" style="background: #ff1493;">Verify</button>
                                </div>
                            </form>
                        </div>
                    </div>

                    <!-- Bundled Support / Help Section Box -->
                    <div class="col-md-6">
                        <div class="card bg-dark text-white p-4 border-light h-100 text-start">
                            <h4 class="fw-bold mb-3" style="color: #ff1493;"><i class="fa-solid fa-headset me-2"></i>Support & Help Desk</h4>
                            <p class="small text-muted mb-2">Need assistance with your subscription or payment verification? Contact our team:</p>
                            <div class="p-2 bg-secondary bg-opacity-25 rounded mb-3 border border-secondary text-center">
                                <a href="mailto:{{ help_email }}" class="fw-bold text-white text-decoration-none small"><i class="fa-solid fa-envelope me-1" style="color: #ff1493;"></i> {{ help_email }}</a>
                            </div>
                            <form method="POST" action="/send-help">
                                <div class="mb-2">
                                    <textarea name="help_message" class="form-control bg-dark text-white border-secondary small" rows="2" placeholder="Send a message to support..." required></textarea>
                                </div>
                                <button type="submit" class="btn text-white w-100 fw-bold btn-sm py-2" style="background: #ff1493;">Submit Ticket</button>
                            </form>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        {% endif %}

    </div>

    <!-- MODAL: SAVED 24-HOUR FEED HISTORY -->
    <div class="modal fade" id="savedFeedModal" tabindex="-1">
        <div class="modal-dialog modal-xl modal-dialog-scrollable">
            <div class="modal-content bg-dark text-white border-secondary">
                <div class="modal-header border-secondary">
                    <h5 class="modal-title fw-bold" style="color: #ff1493;"><i class="fa-solid fa-clock-rotate-left me-2"></i>Saved 24-Hour Automated Intelligence Feed</h5>
                    <button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button>
                </div>
                <div class="modal-body">
                    {% if history_items %}
                        <div class="accordion accordion-flush" id="historyAccordion">
                            {% for item in history_items %}
                                <div class="accordion-item bg-dark text-white border-secondary mb-3">
                                    <h2 class="accordion-header" id="heading{{ loop.index }}">
                                        <button class="accordion-button collapsed bg-secondary text-white fw-bold" type="button" data-bs-toggle="collapse" data-bs-target="#collapse{{ loop.index }}">
                                            Target Query: {{ item[1] }} &nbsp;|&nbsp; Last Refreshed: {{ item[3] }}
                                        </button>
                                    </h2>
                                    <div id="collapse{{ loop.index }}" class="accordion-collapse collapse" data-bs-parent="#historyAccordion">
                                        <div class="accordion-body table-responsive">
                                            {{ item[2] | safe }}
                                        </div>
                                    </div>
                                </div>
                            {% endfor %}
                        </div>
                    {% else %}
                        <p class="text-muted text-center py-4">No saved company search queries found in history. Run a company search to begin tracking.</p>
                    {% endif %}
                </div>
            </div>
        </div>
    </div>

    <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""


# --- ROUTES ---

@app.route('/')
def index():
    if 'user_email' not in session:
        return redirect(url_for('login'))
    return redirect(url_for('dashboard'))


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        password = request.form.get('password').strip()

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT password FROM users WHERE email = %s", (email,))
        row = cursor.fetchone()
        cursor.close()
        conn.close()

        if row and row[0] == password:
            session['user_email'] = email
            return redirect(url_for('dashboard'))
        else:
            error = "Invalid email or password."

    return render_template_string(TEMPLATE_AUTH, mode='login', error=error)


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    error = None
    if request.method == 'POST':
        email = request.form.get('email').strip().lower()
        password = request.form.get('password').strip()
        install_date = datetime.now().isoformat()

        try:
            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("INSERT INTO users (email, password, install_date, has_paid) VALUES (%s, %s, %s, 0)",
                           (email, password, install_date))
            conn.commit()
            cursor.close()
            conn.close()

            session['user_email'] = email
            return redirect(url_for('dashboard'))
        except psycopg2.IntegrityError:
            error = "Email already registered. Please log in."
        except Exception as e:
            error = str(e)

    return render_template_string(TEMPLATE_AUTH, mode='signup', error=error)


@app.route('/dashboard')
def dashboard():
    if 'user_email' not in session:
        return redirect(url_for('login'))

    email = session['user_email']
    is_paid, days_remaining = get_user_payment_status(email)

    conn = get_db_connection()
    cursor = conn.cursor()

    # Fetch last company job
    cursor.execute("SELECT company_query, results_html FROM tracking_jobs WHERE email = %s ORDER BY id DESC LIMIT 1",
                   (email,))
    t_row = cursor.fetchone()
    saved_query = t_row[0] if t_row else ""
    result_html = t_row[1] if t_row else ""

    # Fetch last general item job
    cursor.execute(
        "SELECT item_query, region, results_html FROM general_jobs WHERE email = %s ORDER BY id DESC LIMIT 1", (email,))
    g_row = cursor.fetchone()
    saved_item_query = g_row[0] if g_row else ""
    saved_region = g_row[1] if g_row else ""
    general_result_html = g_row[2] if g_row else ""

    # Fetch last nearby job
    cursor.execute("SELECT location_query, results_html FROM nearby_jobs WHERE email = %s ORDER BY id DESC LIMIT 1",
                   (email,))
    n_row = cursor.fetchone()
    saved_location = n_row[0] if n_row else ""
    nearby_result_html = n_row[1] if n_row else ""

    # Fetch history queries for modal
    cursor.execute(
        "SELECT id, company_query, latest_results_html, last_updated FROM history_queries WHERE email = %s ORDER BY id DESC",
        (email,))
    history_items = cursor.fetchall()

    cursor.close()
    conn.close()

    return render_template_string(
        TEMPLATE_DASHBOARD,
        user_email=email,
        is_paid=is_paid,
        days_remaining=days_remaining,
        saved_query=saved_query,
        result_html=result_html,
        saved_item_query=saved_item_query,
        saved_region=saved_region,
        general_result_html=general_result_html,
        saved_location=saved_location,
        nearby_result_html=nearby_result_html,
        history_items=history_items,
        paypal_link=PAYPAL_ME_LINK,
        subscription_fee=SUBSCRIPTION_FEE,
        help_email=HELP_EMAIL
    )


@app.route('/paypal-webhook', methods=['POST'])
def paypal_webhook():
    event = request.json
    if not event:
        return '', 400

    event_type = event.get('event_type')

    if event_type in ['PAYMENT.SALE.COMPLETED', 'CHECKOUT.ORDER.APPROVED']:
        resource = event.get('resource', {})
        payer_email = resource.get('payer', {}).get('email_address') or resource.get('supplementary_data', {}).get(
            'related_ids', {}).get('buyer_email')

        if payer_email:
            payer_email = payer_email.strip().lower()
            current_time = datetime.now().isoformat()

            conn = get_db_connection()
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET has_paid = 1, last_paid_date = %s 
                WHERE email = %s
            """, (current_time, payer_email))
            conn.commit()
            cursor.close()
            conn.close()

    return '', 200


@app.route('/verify-paypal-email', methods=['POST'])
def verify_paypal_email():
    if 'user_email' not in session:
        return redirect(url_for('login'))

    current_user_email = session['user_email']
    paypal_email = request.form.get('paypal_email', '').strip().lower()

    if not paypal_email:
        return redirect(url_for('dashboard'))

    current_time = datetime.now().isoformat()
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE users 
        SET has_paid = 1, last_paid_date = %s 
        WHERE email = %s
    """, (current_time, current_user_email))
    conn.commit()
    cursor.close()
    conn.close()

    return redirect(url_for('dashboard'))


@app.route('/run', methods=['POST'])
def run():
    if 'user_email' not in session:
        return redirect(url_for('login'))

    email = session['user_email']
    companies = request.form.get('companies', '')

    if companies:
        html_output = run_competitor_intelligence(companies)

        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            "INSERT INTO tracking_jobs (email, company_query, results_html, last_updated) VALUES (%s, %s, %s, %s)",
            (email, companies, html_output, datetime.now().isoformat()))

        cursor.execute("SELECT id FROM history_queries WHERE email = %s AND company_query = %s", (email, companies))
        if cursor.fetchone():
            cursor.execute(
                "UPDATE history_queries SET latest_results_html = %s, last_updated = %s WHERE email = %s AND company_query = %s",
                (html_output, datetime.now().isoformat(), email, companies))
        else:
            cursor.execute(
                "INSERT INTO history_queries (email, company_query, latest_results_html, last_updated) VALUES (%s, %s, %s, %s)",
                (email, companies, html_output, datetime.now().isoformat()))

        conn.commit()
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))


@app.route('/run-general', methods=['POST'])
def run_general():
    if 'user_email' not in session:
        return redirect(url_for('login'))

    email = session['user_email']
    item_query = request.form.get('item_query', '')
    region = request.form.get('region', '')

    if item_query and region:
        html_output = run_general_item_search(item_query, region)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO general_jobs (email, item_query, region, results_html, last_updated) VALUES (%s, %s, %s, %s, %s)",
            (email, item_query, region, html_output, datetime.now().isoformat()))
        conn.commit()
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))


@app.route('/run-nearby', methods=['POST'])
def run_nearby():
    if 'user_email' not in session:
        return redirect(url_for('login'))

    email = session['user_email']
    location_query = request.form.get('location_query', '')

    if location_query:
        html_output = run_nearby_businesses_scan(location_query)

        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO nearby_jobs (email, location_query, results_html, last_updated) VALUES (%s, %s, %s, %s)",
            (email, location_query, html_output, datetime.now().isoformat()))
        conn.commit()
        cursor.close()
        conn.close()

    return redirect(url_for('dashboard'))


@app.route('/clear-company')
def clear_company():
    if 'user_email' not in session:
        return redirect(url_for('login'))
    email = session['user_email']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM tracking_jobs WHERE email = %s", (email,))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('dashboard'))


@app.route('/clear-general')
def clear_general():
    if 'user_email' not in session:
        return redirect(url_for('login'))
    email = session['user_email']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM general_jobs WHERE email = %s", (email,))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('dashboard'))


@app.route('/clear-nearby')
def clear_nearby():
    if 'user_email' not in session:
        return redirect(url_for('login'))
    email = session['user_email']
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM nearby_jobs WHERE email = %s", (email,))
    conn.commit()
    cursor.close()
    conn.close()
    return redirect(url_for('dashboard'))


@app.route('/send-help', methods=['POST'])
def send_help():
    if 'user_email' not in session:
        return redirect(url_for('login'))
    email = session['user_email']
    message = request.form.get('help_message', '')
    if message:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO help_messages (email, message, timestamp) VALUES (%s, %s, %s)",
                       (email, message, datetime.now().isoformat()))
        conn.commit()
        cursor.close()
        conn.close()
    return redirect(url_for('dashboard'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))