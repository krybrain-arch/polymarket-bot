"""
╔══════════════════════════════════════════════════════════╗
║          POLYMARKET BOT — Market Scanner v2.0            ║
║  Strategi : News Lag & Mispricing Detection              ║
║  Dibuat   : Claude (Anthropic) untuk krybrain-arch       ║
║  Repo     : github.com/krybrain-arch/polymarket-bot      ║
╠══════════════════════════════════════════════════════════╣
║  CARA KERJA:                                             ║
║  1. Ambil data langsung dari Polymarket API (realtime)   ║
║  2. Saring pasar berdasarkan kriteria ketat              ║
║  3. Hitung skor 0–100 untuk setiap peluang               ║
║  4. Kirim email notifikasi untuk peluang terbaik         ║
║                                                          ║
║  KEAMANAN:                                               ║
║  - Kredensial HANYA dibaca dari GitHub Secrets           ║
║  - Tidak ada data sensitif yang ditulis ke log           ║
║  - Semua request HTTP pakai HTTPS + timeout + retry      ║
║  - Tidak ada penulisan file ke disk                      ║
╚══════════════════════════════════════════════════════════╝
"""

# ─────────────────────────────────────────────────────────
# IMPORTS (semua dari standard library + requests)
# ─────────────────────────────────────────────────────────
import requests
import smtplib
import os
import json
import time
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────
# KONFIGURASI — ubah di sini jika perlu adjustment
# ─────────────────────────────────────────────────────────

# API Polymarket (Gamma)
GAMMA_API        = "https://gamma-api.polymarket.com"
MAX_PAGES        = 5        # Halaman yang diambil: 5 × 100 = maks 500 market
PAGE_SIZE        = 100      # Batas per halaman (batas resmi API Polymarket)
REQUEST_TIMEOUT  = 20       # Detik sebelum satu request dibatalkan
RETRY_COUNT      = 3        # Berapa kali coba ulang jika request gagal
RETRY_DELAY      = 2        # Detik jeda antar percobaan ulang
PAGE_DELAY       = 0.8      # Detik jeda antar halaman (menghindari rate-limit)

# Filter minimum — market tidak akan dianalisa jika di bawah ini
MIN_VOLUME       = 10_000   # Volume total minimum (USD)
MIN_LIQUIDITY    = 1_500    # Liquidity order-book minimum (USD)
MIN_ODDS_YES     = 0.52     # Odds YES minimum (di bawah ini terlalu murah)
MAX_ODDS_YES     = 0.85     # Odds YES maksimum (di atas ini terlalu mahal)
MIN_DAYS_LEFT    = 3        # Minimum hari tersisa sampai deadline
MAX_DAYS_LEFT    = 30       # Maksimum hari tersisa sampai deadline

# Skor minimum untuk masuk laporan email
SCORE_THRESHOLD  = 60       # 0–100, semakin tinggi semakin selektif


# ─────────────────────────────────────────────────────────
# BLACKLIST — kategori yang dihindari (sulit diprediksi)
# ─────────────────────────────────────────────────────────
BLACKLIST_KEYWORDS = [
    # Cuaca dan bencana alam (acak, tidak bisa dianalisa)
    "weather", "temperature", "rain", "snow", "hurricane",
    "tornado", "earthquake", "flood", "storm",

    # Harga crypto spesifik (terlalu volatile dan tidak terprediksi)
    "bitcoin price", "btc price", "eth price", "ethereum price",
    "solana price", "dogecoin price", "crypto price",
    "will reach $", "will hit $", "above $", "below $",
    "price above", "price below", "price target",

    # Skor pertandingan yang eksak
    "exact score", "final score will be", "score will be",

    # Random / tidak terstruktur
    "who will tweet", "twitter followers", "youtube views",
    "instagram followers",
]


# ─────────────────────────────────────────────────────────
# HIGH-VALUE KEYWORDS — kategori lebih mudah diprediksi
# (ada data publik, berita, polling, jadwal resmi, dll.)
# ─────────────────────────────────────────────────────────
HIGH_VALUE_KEYWORDS = [
    # Politik (ada polling, berita, data historis)
    "election", "president", "congress", "senate", "parliament",
    "vote", "referendum", "will win", "polling",
    "primary", "candidate", "governor",

    # Ekonomi (data resmi dirilis secara terjadwal)
    "fed rate", "interest rate", "federal reserve",
    "inflation", "gdp", "unemployment", "jobs report",
    "cpi ", "ppi ", "nonfarm",

    # Olahraga kompetitif (ada statistik dan sejarah)
    "championship", "world cup", "super bowl",
    "playoff", "finals", "stanley cup",
    "nba finals", "world series", "fa cup",

    # Bisnis & korporasi (ada laporan, berita resmi)
    "ipo", "merger", "acquisition", "bankrupt",
    "earnings", "quarterly results", "ceo resign",
    "revenue", "profit",

    # Hukum & regulasi (ada jadwal sidang, voting resmi)
    "supreme court", "federal court", "regulation",
    "approved by", "rejected by", "ban on",
    "legislation", "signed into law",

    # Geopolitik (ada mediasi, deadline, negosiasi)
    "ceasefire", "peace deal", "treaty", "sanctions",
    "nato", "un resolution",

    # Penghargaan kompetitif (ada jadwal tetap)
    "oscar", "grammy", "emmy", "award",
    "nobel prize", "golden globe",
]


# ══════════════════════════════════════════════════════════
# BAGIAN 1 — UTILITAS UMUM
# ══════════════════════════════════════════════════════════

def safe_float(value, default=0.0):
    """
    Konversi nilai ke float tanpa crash.
    Menangani: string, None, integer, dan format tidak valid.
    """
    try:
        if value is None:
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def parse_json_field(value):
    """
    Polymarket API kadang kirim list sebagai string JSON (misalnya: '["Yes","No"]').
    Fungsi ini menangani dua kasus: sudah list, atau masih string JSON.
    """
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            result = json.loads(value)
            return result if isinstance(result, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


# ══════════════════════════════════════════════════════════
# BAGIAN 2 — FETCH DATA DARI API
# ══════════════════════════════════════════════════════════

def fetch_one_page(offset):
    """
    Ambil satu halaman market dari Polymarket API.
    Otomatis retry jika gagal (sampai RETRY_COUNT kali).
    Return: list market, atau list kosong jika semua percobaan gagal.
    """
    url    = f"{GAMMA_API}/markets"
    params = {
        "limit"     : PAGE_SIZE,
        "offset"    : offset,
        "active"    : "true",
        "closed"    : "false",
        "order"     : "volume",    # Urutkan dari volume terbesar
        "ascending" : "false",     # Volume terbesar = paling relevan
    }

    for attempt in range(1, RETRY_COUNT + 1):
        try:
            resp = requests.get(
                url,
                params  = params,
                timeout = REQUEST_TIMEOUT,
                headers = {"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

            # API bisa return list langsung, atau dict dengan key "markets"
            markets = data if isinstance(data, list) else data.get("markets", [])
            return markets if isinstance(markets, list) else []

        except requests.exceptions.Timeout:
            print(f"  [WARN] Timeout (offset={offset}), percobaan {attempt}/{RETRY_COUNT}")
        except requests.exceptions.HTTPError as exc:
            code = getattr(exc.response, "status_code", "?")
            print(f"  [WARN] HTTP {code} (offset={offset}), percobaan {attempt}/{RETRY_COUNT}")
            # Untuk error permanen (bukan server error), berhenti retry
            if str(code) in ("400", "401", "403", "404"):
                return []
        except requests.exceptions.ConnectionError:
            print(f"  [WARN] Koneksi gagal (offset={offset}), percobaan {attempt}/{RETRY_COUNT}")
        except (json.JSONDecodeError, ValueError):
            print(f"  [WARN] Respons bukan JSON (offset={offset}), percobaan {attempt}/{RETRY_COUNT}")
        except Exception as exc:
            # Log nama error saja, TIDAK log isi response (bisa ada data sensitif)
            print(f"  [WARN] Error: {type(exc).__name__} (offset={offset}), percobaan {attempt}/{RETRY_COUNT}")

        # Jeda sebelum retry berikutnya
        if attempt < RETRY_COUNT:
            time.sleep(RETRY_DELAY)

    return []


def fetch_all_markets():
    """
    Ambil semua market aktif dengan pagination otomatis.
    Mengambil hingga MAX_PAGES halaman (maks 500 market).

    Berhenti lebih awal jika:
      - Halaman kosong (tidak ada market lagi)
      - Halaman tidak penuh (artinya sudah halaman terakhir)
    """
    all_markets = []
    seen_ids    = set()   # Set untuk mencegah duplikat

    for page_idx in range(MAX_PAGES):
        offset     = page_idx * PAGE_SIZE
        page_label = f"Halaman {page_idx + 1}/{MAX_PAGES}"

        page = fetch_one_page(offset)

        if not page:
            print(f"  [{page_label}] Kosong — paginasi selesai.")
            break

        # Deduplikasi: skip market yang sudah ada
        added = 0
        for market in page:
            # Coba beberapa field sebagai ID unik
            mid = (market.get("id")
                   or market.get("conditionId")
                   or market.get("slug"))
            if mid and mid not in seen_ids:
                seen_ids.add(mid)
                all_markets.append(market)
                added += 1

        print(f"  [{page_label}] +{added} market baru (total sejauh ini: {len(all_markets)})")

        # Jika halaman tidak penuh, tidak ada halaman berikutnya
        if len(page) < PAGE_SIZE:
            print(f"  [{page_label}] Tidak penuh ({len(page)}/{PAGE_SIZE}) — paginasi selesai.")
            break

        # Jeda sopan antar halaman (menghindari rate-limit)
        if page_idx < MAX_PAGES - 1:
            time.sleep(PAGE_DELAY)

    return all_markets


# ══════════════════════════════════════════════════════════
# BAGIAN 3 — EKSTRAKSI DATA DARI SATU MARKET
# ══════════════════════════════════════════════════════════

def get_volume(market):
    """
    Ambil total trading volume.
    Dicoba beberapa nama field karena API kadang tidak konsisten.
    """
    for field in ("volume", "volumeNum", "volume24hr", "volumeFormatted"):
        val = market.get(field)
        result = safe_float(val)
        if result > 0:
            return result
    return 0.0


def get_liquidity(market):
    """
    Ambil liquidity (kedalaman order book).
    Dari log nyata 30 Mei 2026: field 'liquidity' tersedia langsung.
    """
    for field in ("liquidity", "liquidityNum", "liquidityFormatted"):
        val = market.get(field)
        result = safe_float(val)
        if result > 0:
            return result
    return 0.0


def get_end_date(market):
    """Ambil tanggal deadline dari berbagai kemungkinan nama field."""
    for field in ("endDate", "end_date_iso", "endDateIso", "endDateTimestamp"):
        val = market.get(field)
        if val:
            return val
    return None


def parse_days_left(end_date_str):
    """
    Hitung sisa hari dari string tanggal ISO 8601 atau timestamp Unix.
    Return integer, atau None jika parsing gagal.
    """
    try:
        if not end_date_str:
            return None

        # Handle timestamp integer (Unix seconds)
        if isinstance(end_date_str, (int, float)):
            end_dt = datetime.fromtimestamp(end_date_str, tz=timezone.utc)
        else:
            # Handle string ISO 8601 (dengan atau tanpa "Z" di akhir)
            clean = str(end_date_str).replace("Z", "+00:00")
            end_dt = datetime.fromisoformat(clean)

        now  = datetime.now(timezone.utc)
        days = (end_dt.date() - now.date()).days
        return days

    except Exception:
        return None


def get_yes_odds(market):
    """
    Ambil odds untuk outcome YES.

    Berdasarkan log NYATA (30 Mei 2026), API Polymarket mengirim:
      - 'outcomes'     → '["Yes", "No"]'  (string JSON)
      - 'outcomePrices'→ '["0.65", "0.35"]'  (string JSON)

    Fungsi ini menangani kedua format: string JSON maupun list langsung.
    Juga mendukung format lama 'tokens' sebagai fallback.

    Return: float odds YES (0.0–1.0), atau None jika tidak bisa dibaca.
    """
    try:
        # ── Format utama: outcomes + outcomePrices ──
        raw_outcomes = market.get("outcomes")
        raw_prices   = market.get("outcomePrices")

        if raw_outcomes is not None and raw_prices is not None:
            outcomes = parse_json_field(raw_outcomes)
            prices   = parse_json_field(raw_prices)

            if outcomes and prices and len(outcomes) == len(prices):
                # Cari outcome "YES" / "Yes" secara case-insensitive
                for outcome, price in zip(outcomes, prices):
                    if str(outcome).strip().upper() == "YES":
                        val = safe_float(price)
                        return val if 0 < val < 1 else None

                # Fallback: ambil harga pertama (konvensi Polymarket: YES = index 0)
                val = safe_float(prices[0])
                return val if 0 < val < 1 else None

        # ── Format lama / alternatif: tokens ──
        raw_tokens = market.get("tokens", [])
        tokens = (parse_json_field(raw_tokens)
                  if isinstance(raw_tokens, str)
                  else raw_tokens)

        if isinstance(tokens, list) and tokens:
            for token in tokens:
                if isinstance(token, dict):
                    if token.get("outcome", "").strip().upper() == "YES":
                        val = safe_float(token.get("price", 0))
                        return val if 0 < val < 1 else None
            # Fallback: token pertama
            if isinstance(tokens[0], dict):
                val = safe_float(tokens[0].get("price", 0))
                return val if 0 < val < 1 else None

    except Exception as exc:
        # Hanya log nama errornya, TIDAK log isi market (bisa banyak data)
        print(f"  [DEBUG] get_yes_odds error: {type(exc).__name__}")

    return None


# ══════════════════════════════════════════════════════════
# BAGIAN 4 — FILTER DAN SCORING
# ══════════════════════════════════════════════════════════

def is_blacklisted(market):
    """
    Cek apakah market masuk kategori yang dihindari.
    Memeriksa question + description.
    """
    question    = str(market.get("question", "")).lower()
    description = str(market.get("description", "")).lower()
    combined    = question + " " + description
    return any(kw in combined for kw in BLACKLIST_KEYWORDS)


def get_category_bonus(market):
    """
    Hitung bonus skor berdasarkan kategori market.
    Kategori bernilai tinggi = ada data publik untuk riset.

    Return: (poin_bonus, keterangan_string)
    """
    question    = str(market.get("question", "")).lower()
    description = str(market.get("description", "")).lower()
    combined    = question + " " + description

    matched = [kw for kw in HIGH_VALUE_KEYWORDS if kw in combined]

    if len(matched) >= 2:
        label = f"{matched[0].strip()}, {matched[1].strip()}"
        return 15, f"Kategori premium ({label})"
    elif len(matched) == 1:
        return 8, f"Kategori bernilai ({matched[0].strip()})"

    return 0, None


def score_market(market):
    """
    Hitung skor peluang untuk satu market (skala 0–100).

    ┌─────────────────────────────────────────────────────┐
    │ Komponen Skor           │ Max  │ Alasan             │
    ├─────────────────────────┼──────┼────────────────────┤
    │ 1. Volume               │  25  │ Ukuran pasar nyata │
    │ 2. Posisi Odds YES       │  25  │ Zona mispricing    │
    │ 3. Sisa Waktu           │  20  │ Jendela waktu ideal│
    │ 4. Kedalaman Liquidity  │  15  │ Likuiditas cukup   │
    │ 5. Kategori Market      │  15  │ Mudah diprediksi   │
    │                         │ 100  │ Total              │
    └─────────────────────────┴──────┴────────────────────┘

    Return: (score_int, [daftar_catatan_string])
    Jika tidak lolos pra-filter: return (0, [])
    """
    score = 0
    notes = []

    # Ambil semua data yang dibutuhkan
    volume    = get_volume(market)
    liquidity = get_liquidity(market)
    odds      = get_yes_odds(market)
    days      = parse_days_left(get_end_date(market))

    # ── Pra-filter: skip market yang tidak memenuhi syarat dasar ──
    if odds is None or days is None:
        return 0, []                          # Data tidak lengkap
    if not (MIN_ODDS_YES <= odds <= MAX_ODDS_YES):
        return 0, []                          # Di luar range odds
    if not (MIN_DAYS_LEFT <= days <= MAX_DAYS_LEFT):
        return 0, []                          # Di luar jendela waktu
    if volume < MIN_VOLUME:
        return 0, []                          # Volume terlalu kecil
    if liquidity < MIN_LIQUIDITY:
        return 0, []                          # Liquidity terlalu tipis

    # ── 1. Volume (max 25 poin) ──────────────────────────────────
    if volume >= 500_000:
        score += 25
        notes.append(f"Volume sangat tinggi (${volume:,.0f})")
    elif volume >= 100_000:
        score += 20
        notes.append(f"Volume tinggi (${volume:,.0f})")
    elif volume >= 50_000:
        score += 14
        notes.append(f"Volume kuat (${volume:,.0f})")
    elif volume >= 10_000:
        score += 7
        notes.append(f"Volume cukup (${volume:,.0f})")

    # ── 2. Posisi Odds (max 25 poin) ────────────────────────────
    # Zona sweet-spot: 0.62–0.72 adalah di mana mispricing paling sering terjadi
    # Bukan terlalu yakin (>0.85) dan bukan 50-50 (<0.52)
    if 0.62 <= odds <= 0.72:
        score += 25
        notes.append(f"⭐ Odds sweet-spot ({odds:.2f}) — zona mispricing optimal")
    elif 0.57 <= odds <= 0.80:
        score += 17
        notes.append(f"Odds zona baik ({odds:.2f})")
    else:
        score += 9
        notes.append(f"Odds zona marginal ({odds:.2f})")

    # ── 3. Sisa Waktu (max 20 poin) ─────────────────────────────
    # Ideal: 7–21 hari — cukup jauh untuk analisa, cukup dekat untuk resolve
    if 7 <= days <= 21:
        score += 20
        notes.append(f"⏰ Deadline ideal ({days} hari lagi)")
    elif 4 <= days <= 28:
        score += 12
        notes.append(f"Deadline {days} hari lagi")
    else:
        score += 5
        notes.append(f"Deadline {days} hari lagi")

    # ── 4. Kedalaman Liquidity (max 15 poin) ────────────────────
    # Liquidity tinggi = order book dalam = bisa masuk dan keluar posisi
    if liquidity >= 50_000:
        score += 15
        notes.append(f"Liquidity sangat dalam (${liquidity:,.0f})")
    elif liquidity >= 20_000:
        score += 11
        notes.append(f"Liquidity dalam (${liquidity:,.0f})")
    elif liquidity >= 5_000:
        score += 7
        notes.append(f"Liquidity cukup (${liquidity:,.0f})")
    elif liquidity >= 1_500:
        score += 3
        notes.append(f"Liquidity tipis (${liquidity:,.0f})")

    # ── 5. Bonus Kategori (max 15 poin) ─────────────────────────
    cat_bonus, cat_note = get_category_bonus(market)
    if cat_bonus > 0 and cat_note:
        score += cat_bonus
        notes.append(cat_note)

    # Cap di 100
    return min(score, 100), notes


# ══════════════════════════════════════════════════════════
# BAGIAN 5 — FORMAT DAN KIRIM EMAIL
# ══════════════════════════════════════════════════════════

def format_email_body(opportunities):
    """
    Format isi email sebagai teks bersih dan mudah dibaca.
    Termasuk disclaimer risiko di bagian bawah.
    """
    now_str   = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M")
    sep_thick = "=" * 58
    sep_thin  = "─" * 58

    lines = [
        sep_thick,
        "  POLYMARKET BOT v2.0  —  LAPORAN OTOMATIS",
        f"  Waktu    : {now_str} UTC",
        f"  Peluang  : {len(opportunities)} market lolos kriteria",
        sep_thick,
        "",
    ]

    for i, opp in enumerate(opportunities, 1):
        # Potong judul yang terlalu panjang agar email tidak berantakan
        question = opp["question"]
        if len(question) > 90:
            question = question[:87] + "..."

        action = "BUY YES"

        lines += [
            sep_thin,
            f"  #{i}  {question}",
            "",
            f"  Odds YES   :  {opp['odds']:.2f}  ({opp['odds'] * 100:.0f}%)",
            f"  Volume     :  ${opp['volume']:>13,.0f}",
            f"  Liquidity  :  ${opp['liquidity']:>13,.0f}",
            f"  Sisa waktu :  {opp['days_left']} hari",
            f"  Skor Bot   :  {opp['score']}/100",
            f"  Sinyal     :  {' | '.join(opp['notes'][:3])}",
            f"  Aksi saran :  {action}",
            "",
            f"  🔗 {opp['url']}",
            "",
        ]

    lines += [
        sep_thick,
        "  ⚠  DISCLAIMER — BACA SEBELUM TRADE",
        "",
        "  Bot ini hanya alat analisis data otomatis.",
        "  BUKAN saran investasi atau keuangan.",
        "  Selalu lakukan riset manual sebelum memasang posisi.",
        "  Jangan gunakan modal yang tidak siap kamu hilang.",
        "  Past performance tidak menjamin hasil di masa depan.",
        sep_thick,
    ]

    return "\n".join(lines)


def send_email_notification(opportunities):
    """
    Kirim email notifikasi ke EMAIL_RECIPIENT.

    Keamanan:
      - Kredensial HANYA dibaca dari env vars (GitHub Secrets)
      - Tidak ada password yang dicetak ke log, dalam kondisi apapun
      - Coba SSL port 465 dulu, fallback ke STARTTLS port 587
      - Timeout 30 detik per koneksi
    """
    # Baca dari GitHub Secrets (env vars) — JANGAN hardcode di sini
    sender    = os.environ.get("EMAIL_SENDER", "").strip()
    password  = os.environ.get("EMAIL_PASSWORD", "").strip()
    recipient = os.environ.get("EMAIL_RECIPIENT", sender).strip()

    # Validasi (tanpa mencetak nilai password ke log)
    if not sender or not password:
        print("  [INFO] EMAIL_SENDER / EMAIL_PASSWORD tidak ada di Secrets — skip notifikasi.")
        return
    if not recipient:
        print("  [INFO] EMAIL_RECIPIENT tidak dikonfigurasi — skip notifikasi.")
        return

    # Buat pesan email
    timestamp = datetime.now(timezone.utc).strftime("%d %b %Y %H:%M")
    subject   = f"[Polymarket Bot] {len(opportunities)} Peluang Ditemukan — {timestamp} UTC"
    body      = format_email_body(opportunities)

    msg           = MIMEMultipart()
    msg["From"]   = sender
    msg["To"]     = recipient
    msg["Subject"]= subject
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg_bytes = msg.as_string()

    sent = False

    # ── Percobaan 1: SSL port 465 (lebih aman, koneksi terenkripsi dari awal) ──
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as srv:
            srv.login(sender, password)
            srv.sendmail(sender, recipient, msg_bytes)
        print(f"  [OK] Email terkirim ke {recipient}  (SSL :465)")
        sent = True
    except smtplib.SMTPAuthenticationError:
        # Jika password salah, tidak perlu coba lagi
        print("  [ERROR] Autentikasi Gmail gagal.")
        print("  [ERROR] Pastikan GitHub Secret EMAIL_PASSWORD berisi App Password yang valid.")
        return
    except Exception as exc:
        print(f"  [WARN] Port 465 gagal ({type(exc).__name__}) — mencoba port 587...")

    # ── Percobaan 2: STARTTLS port 587 (fallback) ──
    if not sent:
        try:
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as srv:
                srv.ehlo()
                srv.starttls()
                srv.login(sender, password)
                srv.sendmail(sender, recipient, msg_bytes)
            print(f"  [OK] Email terkirim ke {recipient}  (STARTTLS :587)")
        except smtplib.SMTPAuthenticationError:
            print("  [ERROR] Autentikasi Gmail gagal pada port 587.")
            print("  [ERROR] Pastikan GitHub Secret EMAIL_PASSWORD berisi App Password yang valid.")
        except Exception as exc:
            print(f"  [ERROR] Gagal kirim email: {type(exc).__name__}")


# ══════════════════════════════════════════════════════════
# BAGIAN 6 — PROGRAM UTAMA
# ══════════════════════════════════════════════════════════

def main():
    start_ts  = time.time()
    sep_thick = "=" * 58

    # ── Header ───────────────────────────────────────────
    print(f"\n{sep_thick}")
    print(f"  Polymarket Scanner v2.0")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(sep_thick)

    # ════════════════════════════════════════════
    # STEP 1: Ambil data dari Polymarket API
    # ════════════════════════════════════════════
    print("\n[STEP 1] Mengambil data pasar dari Polymarket API...")
    markets = fetch_all_markets()

    if not markets:
        print("\n[ERROR] Tidak ada market yang berhasil diambil.")
        print("[ERROR] Periksa: koneksi GitHub Actions, atau Polymarket API sedang down.")
        sys.exit(1)

    print(f"\n[INFO] Total market berhasil diambil : {len(markets)}")

    # Tampilkan field API yang tersedia (berguna untuk debugging)
    if markets:
        print(f"[DEBUG] Field API tersedia          : {list(markets[0].keys())[:15]}")

    # ════════════════════════════════════════════
    # STEP 2: Filter dan scoring
    # ════════════════════════════════════════════
    print("\n[STEP 2] Menganalisa setiap market...")

    opportunities      = []
    count_blacklisted  = 0
    count_no_data      = 0
    count_below_score  = 0

    for market in markets:
        # Lewati kategori yang di-blacklist
        if is_blacklisted(market):
            count_blacklisted += 1
            continue

        # Hitung skor
        score, notes = score_market(market)

        # score == 0, notes == [] artinya data tidak lengkap atau gagal pra-filter
        if score == 0 and not notes:
            count_no_data += 1
            continue

        # Di bawah threshold
        if score < SCORE_THRESHOLD:
            count_below_score += 1
            continue

        # Lolos semua filter — tambahkan ke daftar peluang
        opportunities.append({
            "question" : market.get("question", "N/A"),
            "odds"     : get_yes_odds(market),
            "volume"   : get_volume(market),
            "liquidity": get_liquidity(market),
            "days_left": parse_days_left(get_end_date(market)),
            "score"    : score,
            "notes"    : notes,
            "url"      : (
                f"https://polymarket.com/event/{market['slug']}"
                if market.get("slug") else "https://polymarket.com"
            ),
        })

    # Urutkan dari skor tertinggi ke terendah
    opportunities.sort(key=lambda x: x["score"], reverse=True)

    # ════════════════════════════════════════════
    # STEP 3: Tampilkan ringkasan hasil
    # ════════════════════════════════════════════
    print(f"\n{sep_thick}")
    print(f"  RINGKASAN HASIL")
    print(f"  {'─' * 50}")
    print(f"  Market diambil dari API     : {len(markets)}")
    print(f"  Dilewati (blacklist)        : {count_blacklisted}")
    print(f"  Dilewati (data tidak lengkap): {count_no_data}")
    print(f"  Di bawah skor {SCORE_THRESHOLD}           : {count_below_score}")
    print(f"  ─────────────────────────────────────────")
    print(f"  Peluang ditemukan  ✓        : {len(opportunities)}")
    print(sep_thick)

    # ════════════════════════════════════════════
    # STEP 4: Tampilkan dan kirim notifikasi
    # ════════════════════════════════════════════
    if opportunities:
        # Tampilkan top 10 di log GitHub Actions
        print(f"\n[HASIL] TOP {min(10, len(opportunities))} PELUANG:\n")
        for opp in opportunities[:10]:
            # Potong judul yang terlalu panjang di log
            q_short = opp["question"][:70] + ("..." if len(opp["question"]) > 70 else "")
            print(f"  📌  {q_short}")
            print(f"      Odds : {opp['odds']:.2f}  "
                  f"Volume: ${opp['volume']:,.0f}  "
                  f"Liq: ${opp['liquidity']:,.0f}  "
                  f"Sisa: {opp['days_left']}h  "
                  f"Skor: {opp['score']}/100")
            print(f"      {' | '.join(opp['notes'][:3])}")
            print(f"      {opp['url']}\n")

        # Kirim top 8 via email
        print("[STEP 4] Mengirim email notifikasi...")
        send_email_notification(opportunities[:8])

    else:
        # Tidak ada peluang — tampilkan debug untuk diagnosa
        print("\n[INFO] Tidak ada peluang yang memenuhi semua kriteria saat ini.")
        print("[INFO] Ini normal jika pasar sedang sepi atau semua harga sudah efisien.")
        print("\n[DEBUG] Sampel 5 market (tanpa filter) untuk diagnosa:\n")
        for m in markets[:5]:
            q     = str(m.get("question", "N/A"))[:65]
            odds  = get_yes_odds(m)
            vol   = get_volume(m)
            liq   = get_liquidity(m)
            days  = parse_days_left(get_end_date(m))
            bl    = " ← BLACKLIST" if is_blacklisted(m) else ""
            print(f"  Q    : {q}")
            print(f"  Data : odds={odds}  vol=${vol:,.0f}  liq=${liq:,.0f}  days={days}{bl}\n")

    # ── Footer ───────────────────────────────────────────
    elapsed = time.time() - start_ts
    print(f"[INFO] Total waktu eksekusi: {elapsed:.1f} detik")
    print(f"{sep_thick}\n")


# ─────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
