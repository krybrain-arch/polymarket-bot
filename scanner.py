"""
Polymarket Bot - Market Scanner
Strategi: News Lag & Mispricing Detection
"""

import requests
import json
import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

# ─── KONFIGURASI ─────────────────────────────────────────────
GAMMA_API = "https://gamma-api.polymarket.com"

# Kriteria filter market
MIN_VOLUME       = 10_000   # Minimum volume USD
MIN_ODDS         = 0.55     # Odds minimum (jangan terlalu murah)
MAX_ODDS         = 0.82     # Odds maximum (jangan terlalu mahal)
MIN_DAYS_LEFT    = 3        # Minimum hari sebelum deadline
MAX_DAYS_LEFT    = 30       # Maximum hari sebelum deadline

# Keyword yang dihindari
BLACKLIST = ["weather", "temperature", "rain", "bitcoin price",
             "eth price", "btc price", "crypto price"]

# ─── FUNGSI AMBIL DATA ────────────────────────────────────────
def fetch_markets(limit=200):
    """Ambil daftar market dari Polymarket Gamma API."""
    try:
        url = f"{GAMMA_API}/markets"
        params = {
            "limit": limit,
            "active": "true",
            "closed": "false",
        }
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # Gamma API bisa return list langsung atau dict dengan key 'markets'
        if isinstance(data, list):
            return data
        return data.get("markets", [])
    except Exception as e:
        print(f"[ERROR] Gagal fetch markets: {e}")
        return []


def parse_days_left(end_date_str):
    """Hitung berapa hari lagi sampai deadline."""
    try:
        if not end_date_str:
            return None
        end_date = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = (end_date - now).days
        return delta
    except Exception:
        return None


def get_best_odds(market):
    """Ambil odds YES terbaik dari sebuah market."""
    try:
        tokens = market.get("tokens", [])
        for token in tokens:
            if token.get("outcome", "").upper() == "YES":
                price = float(token.get("price", 0))
                return price
        # Fallback: ambil price pertama
        if tokens:
            return float(tokens[0].get("price", 0))
    except Exception:
        pass
    return None


# ─── FUNGSI ANALISIS ──────────────────────────────────────────
def is_blacklisted(market):
    """Cek apakah market masuk kategori yang dihindari."""
    title = market.get("question", "").lower()
    return any(kw in title for kw in BLACKLIST)


def score_market(market):
    """
    Hitung skor peluang sebuah market (0–100).
    Semakin tinggi = semakin menarik.
    """
    score = 0
    notes = []

    volume = float(market.get("volume", 0) or 0)
    odds   = get_best_odds(market)
    days   = parse_days_left(market.get("endDate") or market.get("end_date_iso"))

    if odds is None or days is None:
        return 0, []

    # 1. Volume check
    if volume >= 50_000:
        score += 30
        notes.append(f"Volume tinggi ${volume:,.0f}")
    elif volume >= 10_000:
        score += 15
        notes.append(f"Volume cukup ${volume:,.0f}")
    else:
        return 0, []  # Langsung buang kalau volume kecil

    # 2. Odds check — zona mispricing
    if MIN_ODDS <= odds <= MAX_ODDS:
        score += 30
        notes.append(f"Odds di zona optimal ({odds:.2f})")
    else:
        return 0, []  # Diluar zona odds

    # 3. Waktu tersisa
    if MIN_DAYS_LEFT <= days <= MAX_DAYS_LEFT:
        score += 20
        notes.append(f"Deadline {days} hari lagi")
    else:
        return 0, []  # Terlalu jauh atau sudah mepet

    # 4. Bonus: odds sangat menarik (kemungkinan mispricing)
    if 0.60 <= odds <= 0.75:
        score += 20
        notes.append("⭐ Zona sweet-spot mispricing")

    return score, notes


# ─── FUNGSI EMAIL ─────────────────────────────────────────────
def send_email(opportunities):
    """Kirim email notifikasi kalau ada peluang bagus."""
    sender    = os.environ.get("EMAIL_SENDER")
    password  = os.environ.get("EMAIL_PASSWORD")
    recipient = os.environ.get("EMAIL_RECIPIENT", sender)

    if not sender or not password:
        print("[INFO] Email tidak dikonfigurasi, skip notifikasi.")
        return

    subject = f"🎯 Polymarket: {len(opportunities)} Peluang Ditemukan — {datetime.now().strftime('%d %b %Y %H:%M')} WIB"

    body = "=== POLYMARKET BOT — LAPORAN OTOMATIS ===\n\n"
    for i, opp in enumerate(opportunities, 1):
        body += f"#{i} {opp['question']}\n"
        body += f"   Odds YES : {opp['odds']:.2f}\n"
        body += f"   Volume   : ${opp['volume']:,.0f}\n"
        body += f"   Sisa     : {opp['days_left']} hari\n"
        body += f"   Skor     : {opp['score']}/100\n"
        body += f"   Catatan  : {', '.join(opp['notes'])}\n"
        body += f"   Link     : {opp['url']}\n\n"

    msg = MIMEMultipart()
    msg["From"]    = sender
    msg["To"]      = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.sendmail(sender, recipient, msg.as_string())
        print(f"[OK] Email terkirim ke {recipient}")
    except Exception as e:
        print(f"[ERROR] Gagal kirim email: {e}")


# ─── MAIN ─────────────────────────────────────────────────────
def main():
    print(f"\n{'='*50}")
    print(f"Polymarket Scanner — {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"{'='*50}")

    markets = fetch_markets(limit=200)
    print(f"[INFO] Total market diambil: {len(markets)}")

    opportunities = []

    for market in markets:
        if is_blacklisted(market):
            continue

        score, notes = score_market(market)

        if score >= 60:
            odds = get_best_odds(market)
            days = parse_days_left(market.get("endDate") or market.get("end_date_iso"))
            volume = float(market.get("volume", 0) or 0)
            slug = market.get("slug", "")
            url = f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"

            opportunities.append({
                "question" : market.get("question", "N/A"),
                "odds"     : odds,
                "volume"   : volume,
                "days_left": days,
                "score"    : score,
                "notes"    : notes,
                "url"      : url,
            })

    # Sort by score tertinggi
    opportunities.sort(key=lambda x: x["score"], reverse=True)

    print(f"\n[HASIL] Peluang ditemukan: {len(opportunities)}")
    print("-" * 50)

    if opportunities:
        for opp in opportunities[:10]:  # Tampilkan max 10
            print(f"\n📌 {opp['question']}")
            print(f"   Odds: {opp['odds']:.2f} | Volume: ${opp['volume']:,.0f} | Sisa: {opp['days_left']}h | Skor: {opp['score']}/100")
            print(f"   {', '.join(opp['notes'])}")
            print(f"   {opp['url']}")

        send_email(opportunities[:5])  # Email hanya 5 terbaik
    else:
        print("Tidak ada peluang yang memenuhi kriteria saat ini.")

    print(f"\n{'='*50}\n")


if __name__ == "__main__":
    main()
