"""
Media Monitoring MVP
- Input keyword + rentang tanggal
- Cari berita online (Google News RSS, edisi Indonesia)
- Ringkasan otomatis dengan Claude
"""

import html
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser
import pandas as pd
import requests
import streamlit as st

try:
    import anthropic
except ImportError:  # app tetap jalan tanpa ringkasan
    anthropic = None

try:
    import trafilatura
    from googlenewsdecoder import gnewsdecoder
    FULLTEXT_AVAILABLE = True
except ImportError:
    FULLTEXT_AVAILABLE = False

st.set_page_config(page_title="Media Monitoring", page_icon="📰", layout="wide")

DEFAULT_MODEL = "claude-sonnet-5-5"
HEADERS = {"User-Agent": "Mozilla/5.0 (MediaMonitoringMVP)"}


# ---------------------------------------------------------------------------
# 1. Pencarian berita
# ---------------------------------------------------------------------------
def build_query(keyword: str, start: date, end: date) -> str:
    # Operator 'before' di Google News bersifat eksklusif, jadi +1 hari
    return f"{keyword} after:{start.isoformat()} before:{(end + timedelta(days=1)).isoformat()}"


def clean_html(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", text or "")).strip()


def fetch_feed_entries(keyword: str, day: date, lang: str) -> list:
    """Ambil berita untuk SATU hari (Google News membatasi ±100 hasil per pencarian)."""
    hl, gl, ceid = ("id", "ID", "ID:id") if lang == "Indonesia" else ("en-US", "US", "US:en")
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(build_query(keyword, day, day))
        + f"&hl={hl}&gl={gl}&ceid={ceid}"
    )
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()
            return feedparser.parse(resp.content).entries
        except Exception:
            if attempt == 2:
                return []
            time.sleep(1.5 * (attempt + 1))
    return []


@st.cache_data(ttl=900, show_spinner=False)
def search_news(keyword: str, start: date, end: date, lang: str) -> list[dict]:
    # Pecah per hari supaya tidak terpotong di 100 hasil. Mulai 1 hari lebih awal
    # karena Google memakai batas hari UTC, sedangkan kita memakai WIB.
    days = [start - timedelta(days=1) + timedelta(days=i) for i in range((end - start).days + 2)]
    entries = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for chunk in pool.map(lambda d: fetch_feed_entries(keyword, d, lang), days):
            entries.extend(chunk)

    results, seen = [], set()
    for e in entries:
        try:
            published = parsedate_to_datetime(e.get("published", ""))
        except Exception:
            published = None
        # Filter tanggal sekali lagi di sisi aplikasi
        if published:
            d = published.astimezone(timezone(timedelta(hours=7))).date()
            if d < start or d > end:
                continue

        source = e.get("source", {}).get("title", "") if e.get("source") else ""
        title = e.get("title", "")
        if source and title.endswith(f" - {source}"):
            title = title[: -len(f" - {source}")]
        key = title.lower().strip()
        if key in seen:
            continue
        seen.add(key)

        results.append(
            {
                "tanggal": published,
                "judul": title,
                "media": source,
                "cuplikan": clean_html(e.get("summary", "")),
                "link": e.get("link", ""),
                "sumber": "Google News",
            }
        )
    results.sort(key=lambda r: r["tanggal"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return results


# --- Sumber tambahan: Google Search via Serper.dev (opsional, berbayar per kredit) ---
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTHS.update({"mei": 5, "agu": 8, "okt": 10, "des": 12})


def parse_serper_date(text: str):
    """Serper memberi tanggal seperti 'Oct 12, 2026', '12 Okt 2026' atau '3 days ago'."""
    if not text:
        return None
    t = text.lower().replace(",", "").strip()
    now = datetime.now(timezone(timedelta(hours=7)))
    m = re.match(r"(\d+)\s+(minute|hour|day|week|menit|jam|hari|minggu)", t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        delta = {"minute": "minutes", "menit": "minutes", "hour": "hours", "jam": "hours",
                 "day": "days", "hari": "days", "week": "weeks", "minggu": "weeks"}[unit]
        return now - timedelta(**{delta: n})
    parts = t.split()
    try:
        if parts[0][:3] in MONTHS:  # Oct 12 2026
            return datetime(int(parts[2]), MONTHS[parts[0][:3]], int(parts[1]), tzinfo=now.tzinfo)
        if parts[1][:3] in MONTHS:  # 12 Okt 2026
            return datetime(int(parts[2]), MONTHS[parts[1][:3]], int(parts[0]), tzinfo=now.tzinfo)
    except (IndexError, ValueError):
        pass
    return None


@st.cache_data(ttl=900, show_spinner=False)
def search_serper(keyword: str, start: date, end: date, api_key: str, pages: int) -> list[dict]:
    tbs = f"cdr:1,cd_min:{start:%m/%d/%Y},cd_max:{end:%m/%d/%Y}"
    out = []
    for endpoint, label in (("news", "Google Search (News)"), ("search", "Google Search (Web)")):
        for page in range(1, pages + 1):
            resp = requests.post(
                f"https://google.serper.dev/{endpoint}",
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": keyword, "gl": "id", "hl": "id", "tbs": tbs, "num": 10, "page": page},
                timeout=30,
            )
            if resp.status_code in (401, 403):
                raise RuntimeError("API key Serper tidak valid atau kredit habis.")
            resp.raise_for_status()
            items = resp.json().get("news" if endpoint == "news" else "organic", [])
            for it in items:
                link = it.get("link", "")
                out.append({
                    "tanggal": parse_serper_date(it.get("date", "")),
                    "judul": it.get("title", ""),
                    "media": it.get("source") or re.sub(r"^https?://(www\.)?([^/]+).*", r"\2", link),
                    "cuplikan": it.get("snippet", ""),
                    "link": link,
                    "sumber": label,
                })
            if len(items) < 10:
                break
    return out


def is_gnews(link: str) -> bool:
    return "news.google.com" in (link or "")


def merge_results(*lists: list[dict]) -> list[dict]:
    """Gabung & buang duplikat. Jika berita yang sama punya link asli dari sumber lain
    (mis. Serper), link asli itu dipakai untuk versi Google News-nya."""
    merged, by_key, seen_links = [], {}, set()
    for lst in lists:
        for r in lst:
            key = re.sub(r"\W+", " ", r["judul"].lower()).strip()
            if not key or r["link"] in seen_links:
                continue
            if key in by_key:
                first = by_key[key]
                if is_gnews(first["link"]) and not first.get("link_asli") and not is_gnews(r["link"]):
                    first["link_asli"] = r["link"]
                continue
            r = dict(r)
            by_key[key] = r
            seen_links.add(r["link"])
            merged.append(r)
    merged.sort(key=lambda r: r["tanggal"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return merged


# --- Filter relevansi: pastikan berita benar-benar memuat keyword ---
def parse_keyword(q: str):
    """Aturan keyword:
      - koma ( , ), plus ( + ), titik koma ( ; ) atau OR  -> pemisah ALTERNATIF (salah satu cukup)
      - spasi di dalam satu alternatif                      -> semua kata harus ada (urutan bebas)
      - "tanda kutip"                                       -> frasa persis
      - kata*                                               -> awalan kata (invest* = investment, investments, ...)
      - -kata                                               -> buang berita yang memuat kata ini
    Hasil: (alts, excludes). alts = daftar alternatif, tiap alternatif = daftar term yang wajib ada.
    Contoh: 'IIM + Insight Invest* Management, "korporasi" Taspen -jasindo'
      -> alts [["iim"], ["insight", "invest*", "management"], ["korporasi", "taspen"]], excludes ["jasindo"]"""
    tokens = re.findall(r'-?"[^"]*"|[,;+|\n]|[^\s,;+|"]+', q)
    alts, current, excludes = [], [], []
    for tok in tokens:
        if tok in {",", ";", "+", "|", "\n"} or tok.upper() == "OR":
            if current:
                alts.append(current)
            current = []
            continue
        neg = tok.startswith("-") and len(tok) > 1
        term = (tok[1:] if neg else tok).strip('"').lower().strip()
        if not term or ":" in term:  # abaikan operator seperti site:
            continue
        (excludes if neg else current).append(term)
    if current:
        alts.append(current)
    return alts, excludes


def alt_to_query(alt: list[str], excludes: list[str]) -> str:
    """Satu alternatif -> query Google."""
    clean = [t.replace("*", "") for t in alt]  # Google tidak mengenal awalan*
    parts = [f'"{t}"' if " " in t else t for t in clean if t]
    parts += [f'-"{t}"' if " " in t else f"-{t}" for t in excludes]
    return " ".join(parts)


def term_pattern(term: str) -> re.Pattern:
    words = [w for w in re.split(r"[^\w*]+", term) if w.strip("*")]
    pats = [re.escape(w.rstrip("*")) + (r"\w*" if w.endswith("*") else "") for w in words]
    # kata dalam frasa boleh dipisah spasi/tanda baca; pakai batas kata
    return re.compile(r"(?<!\w)" + r"[\W_]+".join(pats) + r"(?!\w)", re.IGNORECASE)


def matches_keyword(text: str, alts, excludes) -> bool:
    if any(term_pattern(t).search(text) for t in excludes):
        return False
    return any(all(term_pattern(t).search(text) for t in alt) for alt in alts)


SOCIAL_DOMAINS = ("facebook.com", "instagram.com", "threads.com", "threads.net", "tiktok.com",
                  "twitter.com", "x.com", "youtube.com", "linkedin.com", "pinterest.com")


def is_social(link: str) -> bool:
    host = re.sub(r"^https?://(www\.|m\.)?([^/]+).*", r"\2", link or "").lower()
    return any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS)


def filter_relevant(articles, keyword, check_fulltext=False, max_check=60):
    clauses, excludes = parse_keyword(keyword)
    if not clauses:
        return articles, []
    keep, drop = [], []
    for a in articles:
        (keep if matches_keyword(f"{a['judul']} {a['cuplikan']}", clauses, excludes) else drop).append(a)
    # Opsional: cek isi artikel untuk yang tidak lolos di judul/cuplikan
    if check_fulltext and FULLTEXT_AVAILABLE and drop:
        to_check, rest = drop[:max_check], drop[max_check:]
        with ThreadPoolExecutor(max_workers=8) as pool:
            bodies = list(pool.map(lambda a: fetch_fulltext(a["link"]), to_check))
        drop = rest
        for a, body in zip(to_check, bodies):
            if body and matches_keyword(f"{a['judul']} {body}", clauses, excludes):
                keep.append(dict(a, isi=body))
            else:
                drop.append(a)
        keep.sort(key=lambda r: r["tanggal"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return keep, drop


@st.cache_resource(show_spinner=False)
def _link_cache() -> dict:
    return {}  # hanya menyimpan hasil yang BERHASIL, supaya yang gagal dicoba lagi


def resolve_url(link: str, cache: dict) -> str:
    """Cara 2: ubah link news.google.com dengan decoder (bisa diblokir Google di server cloud)."""
    if not is_gnews(link) or not FULLTEXT_AVAILABLE:
        return link
    return _decode_gnews(link, cache)


def _decode_gnews(link: str, cache: dict) -> str:
    if link in cache:
        return cache[link]
    for _ in range(2):
        try:
            decoded = gnewsdecoder(link, interval=1)
            if decoded.get("status") and decoded.get("decoded_url"):
                cache[link] = decoded["decoded_url"]
                return cache[link]
        except Exception:
            pass
        time.sleep(1)
    return link


def _norm(t: str) -> str:
    return re.sub(r"\W+", " ", (t or "").lower()).strip()


def serper_find_link(title: str, media: str, api_key: str, cache: dict) -> str:
    """Cara 3: cari judul berita di Google Search untuk menemukan link aslinya (1 kredit Serper)."""
    from difflib import SequenceMatcher
    ck = ("serper", title, media)
    if ck in cache:
        return cache[ck]
    try:
        r = requests.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                          json={"q": f'"{title}"', "gl": "id", "hl": "id", "num": 10}, timeout=30)
        items = r.json().get("organic", []) if r.status_code == 200 else []
    except Exception:
        return ""
    want = _norm(title)
    media_words = [w for w in _norm(media).replace(" com", "").split() if len(w) > 2]
    best, best_score = "", 0.0
    for it in items:
        raw = re.sub(r"\s*(\.\.\.|…)\s*$", "", it.get("title", ""))
        raw = re.sub(r"\s+[-|–]\s+[^-|–]{2,40}$", "", raw)  # buang " - NamaMedia" di akhir judul
        got = _norm(raw)
        n = min(len(got), len(want))
        score = SequenceMatcher(None, want[:n], got[:n]).ratio() if n >= 15 else 0
        link = it.get("link", "")
        if media_words and any(w in link.lower() for w in media_words):
            score += 0.1  # domain cocok dengan nama media
        if score > best_score:
            best, best_score = link, score
    if best_score >= 0.8:
        cache[ck] = best
        return best
    return ""


def resolve_all(articles: list[dict], serper_key: str = "") -> list[dict]:
    """Isi link_asli untuk tiap berita. Urutan: link asli dari Serper → decoder → cari judul via Serper."""
    cache = _link_cache()  # diambil di thread utama, dipakai bersama oleh thread pekerja

    def one(a):
        if a.get("link_asli") and not is_gnews(a["link_asli"]):
            return a["link_asli"]
        if not is_gnews(a["link"]):
            return a["link"]
        url = resolve_url(a["link"], cache)
        if is_gnews(url) and serper_key:
            url = serper_find_link(a["judul"], a.get("media", ""), serper_key, cache) or url
        return url
    with ThreadPoolExecutor(max_workers=5) as pool:
        urls = list(pool.map(one, articles))
    return [dict(a, link_asli=u) for a, u in zip(articles, urls)]


def build_wa_report(articles: list[dict], header: str = "", note: str = "") -> str:
    """Format laporan WhatsApp: '1. Judul' lalu link di baris berikutnya, dipisah baris kosong."""
    parts = []
    if header.strip():
        parts.append(header.strip())
    if note.strip():
        parts.append(note.strip())
    for i, a in enumerate(articles, 1):
        parts.append(f"{i}. {a['judul'].strip()}\n{a.get('link_asli') or a['link']}")
    return "\n\n".join(parts)


def build_csv(articles: list[dict]) -> bytes:
    rows = [{
        "No": i,
        "Tanggal": a["tanggal"].astimezone(timezone(timedelta(hours=7))).strftime("%d/%m/%Y %H:%M")
                   if a["tanggal"] else "",
        "Media": a["media"],
        "Judul Berita": a["judul"],
        "Link Berita": a.get("link_asli") or a["link"],
        "Cuplikan": a.get("cuplikan", ""),
        "Sumber Pencarian": a.get("sumber", ""),
    } for i, a in enumerate(articles, 1)]
    # utf-8-sig supaya huruf/tanda baca terbaca benar saat dibuka di Excel
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")


def copy_button(text: str, label: str):
    """Tombol salin ke clipboard (bekerja di HP dan laptop)."""
    import json
    payload = json.dumps(text).replace("</", "<\\/")
    page = f"""<meta charset="utf-8">
<button id="b" style="width:100%;padding:12px;font-size:16px;border:none;border-radius:8px;
  background:#25D366;color:#fff;font-weight:600;cursor:pointer;font-family:sans-serif">{html.escape(label)}</button>
<script>
const t = {payload};
const b = document.getElementById("b");
b.onclick = async () => {{
  let ok = false;
  try {{ await navigator.clipboard.writeText(t); ok = true; }} catch (e) {{}}
  if (!ok) {{
    const ta = document.createElement("textarea"); ta.value = t; document.body.appendChild(ta);
    ta.select(); try {{ ok = document.execCommand("copy"); }} catch (e) {{}} ta.remove();
  }}
  b.textContent = ok ? "✅ Tersalin, silakan tempel di WhatsApp" : "⚠️ Gagal, pakai ikon salin di kotak teks";
  setTimeout(() => b.textContent = {json.dumps(label)}, 3000);
}};
</script>"""
    if hasattr(st, "iframe"):          # Streamlit baru
        st.iframe(page, height=56)
    else:                              # Streamlit lama
        import streamlit.components.v1 as components
        components.html(page, height=56)


def analyst_note(keyword: str, articles: list[dict], api_key: str, model: str) -> str:
    """Catatan singkat dari AI: inti berita + perbedaan angka/fakta antar media."""
    client = anthropic.Anthropic(api_key=api_key)
    daftar = "\n".join(f"{i}. {a['judul']} ({a['media']})" for i, a in enumerate(articles, 1))
    prompt = f"""Kamu analis media monitoring. Berikut judul-judul berita hari ini untuk keyword "{keyword}":

{daftar}

Tulis catatan untuk klien, untuk ditaruh di atas daftar berita di WhatsApp:
- Baris pertama diawali "*Catatan:*" lalu 1-2 kalimat inti pemberitaan.
- Jika ada angka/fakta yang berbeda antar media (misal nominal berbeda), tambahkan satu baris
  "⚠️ Perbedaan data:" yang menyebut perbedaannya dan nomor berita [n]. Jika tidak ada, jangan tulis baris ini.
- Maksimal 4 baris. Bahasa Indonesia formal. Format WhatsApp (*tebal*), tanpa markdown lain.
- Hanya berdasarkan judul di atas, jangan menambah fakta."""
    msg = client.messages.create(model=model, max_tokens=400,
                                 messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fulltext(gnews_url: str) -> str:
    """Coba ambil isi artikel asli (best effort, boleh gagal)."""
    if not FULLTEXT_AVAILABLE:
        return ""
    try:
        if "news.google.com" in gnews_url:
            decoded = gnewsdecoder(gnews_url, interval=1)
            real_url = decoded.get("decoded_url") if decoded.get("status") else None
        else:
            real_url = gnews_url
        if not real_url:
            return ""
        downloaded = trafilatura.fetch_url(real_url)
        text = trafilatura.extract(downloaded) or ""
        return text[:4000]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 2. Ringkasan dengan Claude
# ---------------------------------------------------------------------------
def summarize(keyword, start, end, articles, api_key, model) -> str:
    client = anthropic.Anthropic(api_key=api_key)
    blocks = []
    for i, a in enumerate(articles, 1):
        tgl = a["tanggal"].strftime("%d %b %Y") if a["tanggal"] else "-"
        body = a.get("isi") or a["cuplikan"]
        blocks.append(f"[{i}] {tgl} | {a['media']}\nJudul: {a['judul']}\nIsi: {body}")

    prompt = f"""Kamu adalah analis media monitoring. Berikut daftar pemberitaan online
untuk keyword "{keyword}" periode {start:%d %b %Y} s.d. {end:%d %b %Y}.

{chr(10).join(blocks)}

Buat laporan ringkas dalam Bahasa Indonesia dengan format:
## Ringkasan Utama
3-5 kalimat tentang inti pemberitaan pada periode ini.
## Isu / Tema Utama
Poin-poin tema yang paling banyak diberitakan, sebutkan nomor artikel rujukan [n].
## Sentimen
Positif / netral / negatif secara umum, plus alasan singkat.
## Media Paling Aktif
## Hal yang Perlu Diwaspadai
Isu yang berpotensi negatif atau perlu ditindaklanjuti (tulis "Tidak ada" jika tidak ada).

Hanya gunakan informasi dari daftar di atas, jangan menambah fakta dari luar."""

    msg = client.messages.create(
        model=model,
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


# ---------------------------------------------------------------------------
# 3. Tampilan
# ---------------------------------------------------------------------------
st.title("📰 Media Monitoring")
st.caption("Cari pemberitaan online berdasarkan keyword dan tanggal, lalu dapatkan ringkasannya. · Versi 18-Okt-2026d (perbaikan link asli)")

def get_secret(name: str) -> str:
    try:
        return st.secrets.get(name, "")
    except Exception:  # belum ada file secrets
        return ""


secret_key = get_secret("ANTHROPIC_API_KEY")
serper_secret = get_secret("SERPER_API_KEY")

keyword = st.text_input(
    "Keyword", placeholder="mis. IIM, Insight Invest* Management, korporasi Taspen",
    help=("**Koma / +** = salah satu cukup (tiap nama dicari terpisah lalu digabung).  \n"
          "**Spasi** = semua kata harus ada.  \n"
          '**"tanda kutip"** = frasa persis.  \n'
          "**kata\\*** = awalan (invest\\* → investment, investments).  \n"
          "**-kata** = buang berita yang memuat kata ini."))
c1, c2 = st.columns(2)
today = date.today()
start = c1.date_input("Dari tanggal", value=today - timedelta(days=7), max_value=today)
end = c2.date_input("Sampai tanggal", value=today, max_value=today)

# Pengaturan di halaman utama (sidebar tersembunyi di HP)
with st.expander("⚙️ Pengaturan sumber & ringkasan", expanded=False):
    serper_key = serper_secret or st.text_input(
        "Serper API key (opsional)", type="password",
        help="Tambahkan hasil Google Search (web + tab News).")
    api_key = secret_key or st.text_input("Anthropic API key", type="password",
                                          help="Diperlukan untuk fitur ringkasan.")
    serper_pages = st.slider("Halaman Google Search", 1, 10, 5,
                             help="1 halaman = 10 hasil = 1 kredit Serper, untuk web dan news masing-masing.")
    lang = st.selectbox("Edisi Google News", ["Indonesia", "Internasional (English)"])
    max_articles = st.slider("Maks. artikel diringkas", 5, 50, 25)
    use_fulltext = st.checkbox(
        "Ambil isi artikel lengkap (lebih akurat, lebih lambat)",
        value=False, disabled=not FULLTEXT_AVAILABLE,
    )
    model = st.text_input("Model", value=DEFAULT_MODEL)

_alts, _exc = parse_keyword(keyword)
if len(_alts) > 1 or _exc:
    st.caption("Dicari sebagai: " + "  **atau**  ".join(
        " + ".join(f"`{t}`" for t in a) for a in _alts)
        + ("  ·  kecuali " + ", ".join(f"`{t}`" for t in _exc) if _exc else ""))

use_serper = st.toggle("Sertakan Google Search (Serper)", value=bool(serper_key), disabled=not serper_key)
include_social = st.toggle("Sertakan media sosial (Facebook, Instagram, TikTok, X, dll.)", value=False)
strict = st.toggle("Hanya berita yang memuat keyword", value=True,
                   help="Buang hasil yang keyword-nya tidak muncul di judul atau cuplikan.")
deep_check = st.toggle("Cek juga isi artikel (lebih lambat)", value=False,
                       disabled=not (strict and FULLTEXT_AVAILABLE),
                       help="Berita yang keyword-nya hanya ada di badan artikel tetap ikut. Maks. 60 artikel dicek.")
st.caption(
    ("✅ Serper aktif" if serper_key else "⚪ Serper belum diisi")
    + " · "
    + ("✅ Ringkasan aktif" if api_key else "⚪ Ringkasan belum aktif (API key Anthropic kosong)")
)

if st.button("🔍 Cari berita", type="primary", width="stretch"):
    if not keyword.strip():
        st.warning("Isi keyword dulu.")
        st.stop()
    if start > end:
        st.warning("Tanggal awal harus sebelum tanggal akhir.")
        st.stop()
    if (end - start).days > 92:
        st.warning("Rentang maksimal 3 bulan per pencarian, supaya tidak diblokir Google.")
        st.stop()
    alts, excludes = parse_keyword(keyword)
    if not alts:
        st.warning("Keyword tidak terbaca. Contoh: IIM, Insight Investments Management")
        st.stop()
    queries = [alt_to_query(a, excludes) for a in alts]
    gnews, serper = [], []
    for qi, q in enumerate(queries, 1):
        with st.spinner(f"Google News: '{q}' ({qi}/{len(queries)})..."):
            try:
                gnews += search_news(q, start, end, lang)
            except Exception as ex:
                st.error(f"Gagal mengambil Google News untuk '{q}': {ex}")
        if use_serper:
            with st.spinner(f"Google Search: '{q}' ({qi}/{len(queries)})..."):
                try:
                    serper += search_serper(q, start, end, serper_key, serper_pages)
                except Exception as ex:
                    st.error(f"Gagal mengambil Google Search untuk '{q}': {ex}")
    merged = merge_results(gnews, serper)
    social = [a for a in merged if is_social(a["link"])]
    if not include_social:
        merged = [a for a in merged if not is_social(a["link"])]
    filtered_out = []
    if strict:
        with st.spinner("Menyaring berita yang memuat keyword..."):
            merged, filtered_out = filter_relevant(merged, keyword.strip(), check_fulltext=deep_check)
    st.session_state.social_removed = 0 if include_social else len(social)
    st.session_state.articles = merged
    st.session_state.filtered_out = filtered_out
    st.session_state.meta = (keyword.strip(), start, end)
    st.session_state.summary = None
    st.session_state.wa_report = None
    st.session_state.csv_data = None

articles = st.session_state.get("articles")
if articles is not None:
    kw, s, e = st.session_state.meta
    filtered_out = st.session_state.get("filtered_out", [])
    if not articles:
        st.info("Tidak ada berita yang memuat keyword ini pada periode tersebut."
                + (f" ({len(filtered_out)} hasil Google disaring; matikan filter untuk melihatnya.)"
                   if filtered_out else ""))
        st.stop()
    if filtered_out:
        st.caption(f"🧹 {len(filtered_out)} hasil Google disaring karena tidak memuat keyword "
                   "(lihat di tab Daftar berita).")
    if st.session_state.get("social_removed"):
        st.caption(f"📵 {st.session_state.social_removed} hasil media sosial tidak ditampilkan.")

    m1, m2, m3 = st.columns(3)
    m1.metric("Jumlah berita", len(articles))
    m2.metric("Jumlah media", len({a["media"] for a in articles}))
    m3.metric("Periode", f"{(e - s).days + 1} hari")

    tab_wa, tab_sum, tab_list, tab_chart = st.tabs(
        ["💬 Laporan WhatsApp", "🧠 Ringkasan", "📄 Daftar berita", "📊 Statistik"])

    with tab_wa:
        st.caption("Centang berita yang masuk laporan, lalu klik Susun laporan. "
                   "Urutan mengikuti tabel (terbaru di atas).")
        table = pd.DataFrame({
            "Kirim": True,
            "Judul": [a["judul"] for a in articles],
            "Media": [a["media"] for a in articles],
            "Waktu": [a["tanggal"].astimezone(timezone(timedelta(hours=7))).strftime("%d/%m %H:%M")
                      if a["tanggal"] else "-" for a in articles],
        })
        edited = st.data_editor(
            table, hide_index=True, width="stretch",
            key=f"wa_{kw}_{s}_{e}_{len(articles)}",
            column_config={"Kirim": st.column_config.CheckboxColumn(width="small"),
                           "Judul": st.column_config.TextColumn(width="large")},
            disabled=["Media", "Waktu"],
        )
        header = st.text_input("Judul laporan (opsional)",
                               placeholder=f"mis. Media Monitoring {kw} – {date.today():%d/%m/%Y} Pagi")
        add_note = st.checkbox("Tambahkan catatan analis dari AI (inti berita + perbedaan data antar media)",
                               value=False, disabled=not (anthropic and api_key))

        if st.button("📝 Susun laporan", type="primary", width="stretch"):
            chosen = [dict(a, judul=edited.loc[i, "Judul"])  # judul boleh diedit reviewer
                      for i, a in enumerate(articles) if edited.loc[i, "Kirim"]]
            if not chosen:
                st.warning("Pilih minimal satu berita.")
            else:
                with st.spinner(f"Mengambil link asli {len(chosen)} berita..."):
                    chosen = resolve_all(chosen, serper_key)
                note = ""
                if add_note:
                    with st.spinner("AI menyiapkan catatan..."):
                        try:
                            note = analyst_note(kw, chosen, api_key, model)
                        except Exception as ex:
                            st.error(f"Catatan AI gagal dibuat: {ex}")
                st.session_state.wa_report = build_wa_report(chosen, header, note)
                st.session_state.wa_unresolved = sum("news.google.com" in c["link_asli"] for c in chosen)

        if st.session_state.get("wa_report"):
            report = st.session_state.wa_report
            if st.session_state.get("wa_unresolved"):
                st.warning(f"{st.session_state.wa_unresolved} link belum berhasil diubah ke link asli "
                           "(masih news.google.com). Link tetap bisa dibuka, atau klik Susun laporan lagi.")
            copy_button(report, "📋 Salin laporan")
            st.code(report, language=None, wrap_lines=True)
            st.caption("Klik Salin laporan, lalu tempel (paste) di chat WhatsApp klien. "
                       "Kalau tombol tidak berfungsi, pakai ikon salin di pojok kanan atas kotak teks.")

    with tab_sum:
        if anthropic is None or not api_key:
            st.info("Isi Anthropic API key (di Secrets atau di Pengaturan) untuk membuat ringkasan.")
        elif st.button("Buat ringkasan", width="stretch"):
            subset = [dict(a) for a in articles[:max_articles]]
            if use_fulltext:
                prog = st.progress(0.0, text="Mengambil isi artikel...")
                for i, a in enumerate(subset):
                    a["isi"] = fetch_fulltext(a["link"])
                    prog.progress((i + 1) / len(subset), text=f"Mengambil isi artikel {i+1}/{len(subset)}")
                prog.empty()
            with st.spinner("Claude sedang meringkas..."):
                try:
                    st.session_state.summary = summarize(kw, s, e, subset, api_key, model)
                except Exception as ex:
                    st.error(f"Gagal membuat ringkasan: {ex}")
        if st.session_state.get("summary"):
            st.markdown(st.session_state.summary)
            st.download_button("⬇️ Unduh ringkasan (.md)", st.session_state.summary,
                               file_name=f"ringkasan_{kw}_{s}_{e}.md")

    with tab_list:
        for i, a in enumerate(articles, 1):
            tgl = a["tanggal"].strftime("%d %b %Y %H:%M") if a["tanggal"] else "-"
            st.markdown(f"**{i}. [{a['judul']}]({a['link']})**  \n{a['media']} · {tgl} · _{a['sumber']}_")
        if filtered_out:
            with st.expander(f"🧹 Disaring — tidak memuat keyword ({len(filtered_out)})"):
                for a in filtered_out:
                    tgl = a["tanggal"].strftime("%d %b %Y") if a["tanggal"] else "-"
                    st.markdown(f"- [{a['judul']}]({a['link']}) · {a['media']} · {tgl}")
        st.divider()
        if st.button("📄 Siapkan CSV (dengan link asli berita)", width="stretch"):
            with st.spinner(f"Mengambil link asli {len(articles)} berita..."):
                resolved = resolve_all(articles, serper_key)
            st.session_state.csv_data = build_csv(resolved)
        if st.session_state.get("csv_data"):
            st.download_button("⬇️ Unduh daftar berita (.csv)", st.session_state.csv_data,
                               file_name=f"berita_{kw}_{s}_{e}.csv", mime="text/csv", width="stretch")

    with tab_chart:
        df = pd.DataFrame(articles).dropna(subset=["tanggal"])
        if not df.empty:
            df["hari"] = pd.to_datetime(df["tanggal"], utc=True).dt.tz_convert("Asia/Jakarta").dt.date
            st.subheader("Jumlah berita per hari")
            st.bar_chart(df.groupby("hari").size())
            st.subheader("Media teratas")
            st.bar_chart(df["media"].value_counts().head(10))
