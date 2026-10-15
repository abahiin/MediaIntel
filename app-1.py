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


def merge_results(*lists: list[dict]) -> list[dict]:
    merged, seen = [], set()
    for lst in lists:
        for r in lst:
            key = re.sub(r"\W+", " ", r["judul"].lower()).strip()
            if not key or key in seen or r["link"] in seen:
                continue
            seen.update({key, r["link"]})
            merged.append(r)
    merged.sort(key=lambda r: r["tanggal"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return merged


# --- Filter relevansi: pastikan berita benar-benar memuat keyword ---
def parse_keyword(q: str):
    """Ubah keyword gaya Google jadi aturan.
    Hasil: (clauses, excludes). clauses = daftar syarat AND, tiap syarat = daftar alternatif (OR).
    Contoh: '"OJK" asuransi OR reasuransi -kripto'
      -> clauses [["ojk"], ["asuransi", "reasuransi"]], excludes ["kripto"]"""
    tokens = re.findall(r'-?"[^"]+"|\S+', q)
    clauses, excludes, join_next = [], [], False
    for tok in tokens:
        if tok.upper() == "OR" or tok == "|":
            join_next = bool(clauses)
            continue
        neg = tok.startswith("-") and len(tok) > 1
        term = tok[1:] if neg else tok
        term = term.strip('"').lower().strip()
        if not term or ":" in term:  # abaikan operator seperti site:
            continue
        if neg:
            excludes.append(term)
        elif join_next:
            clauses[-1].append(term)
        else:
            clauses.append([term])
        join_next = False
    return clauses, excludes


def term_pattern(term: str) -> re.Pattern:
    words = [re.escape(w) for w in re.split(r"\W+", term) if w]
    # kata dalam frasa boleh dipisah spasi/tanda baca; pakai batas kata
    return re.compile(r"(?<!\w)" + r"[\W_]+".join(words) + r"(?!\w)", re.IGNORECASE)


def matches_keyword(text: str, clauses, excludes) -> bool:
    if any(term_pattern(t).search(text) for t in excludes):
        return False
    return all(any(term_pattern(t).search(text) for t in alts) for alts in clauses)


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
st.caption("Cari pemberitaan online berdasarkan keyword dan tanggal, lalu dapatkan ringkasannya.")

def get_secret(name: str) -> str:
    try:
        return st.secrets.get(name, "")
    except Exception:  # belum ada file secrets
        return ""


secret_key = get_secret("ANTHROPIC_API_KEY")
serper_secret = get_secret("SERPER_API_KEY")

keyword = st.text_input("Keyword", placeholder='mis. "Bank Indonesia" suku bunga')
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

use_serper = st.toggle("Sertakan Google Search (Serper)", value=bool(serper_key), disabled=not serper_key)
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

if st.button("🔍 Cari berita", type="primary", use_container_width=True):
    if not keyword.strip():
        st.warning("Isi keyword dulu.")
        st.stop()
    if start > end:
        st.warning("Tanggal awal harus sebelum tanggal akhir.")
        st.stop()
    if (end - start).days > 92:
        st.warning("Rentang maksimal 3 bulan per pencarian, supaya tidak diblokir Google.")
        st.stop()
    with st.spinner(f"Mencari berita hari per hari ({(end - start).days + 1} hari)..."):
        try:
            gnews = search_news(keyword.strip(), start, end, lang)
        except Exception as ex:
            gnews = []
            st.error(f"Gagal mengambil Google News: {ex}")
    serper = []
    if use_serper:
        with st.spinner("Menambahkan hasil Google Search..."):
            try:
                serper = search_serper(keyword.strip(), start, end, serper_key, serper_pages)
            except Exception as ex:
                st.error(f"Gagal mengambil Google Search: {ex}")
    merged = merge_results(gnews, serper)
    filtered_out = []
    if strict:
        with st.spinner("Menyaring berita yang memuat keyword..."):
            merged, filtered_out = filter_relevant(merged, keyword.strip(), check_fulltext=deep_check)
    st.session_state.articles = merged
    st.session_state.filtered_out = filtered_out
    st.session_state.meta = (keyword.strip(), start, end)
    st.session_state.summary = None

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

    m1, m2, m3 = st.columns(3)
    m1.metric("Jumlah berita", len(articles))
    m2.metric("Jumlah media", len({a["media"] for a in articles}))
    m3.metric("Periode", f"{(e - s).days + 1} hari")

    tab_sum, tab_list, tab_chart = st.tabs(["🧠 Ringkasan", "📄 Daftar berita", "📊 Statistik"])

    with tab_sum:
        if anthropic is None or not api_key:
            st.info("Isi Anthropic API key (di Secrets atau di Pengaturan) untuk membuat ringkasan.")
        elif st.button("Buat ringkasan", use_container_width=True):
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
        df = pd.DataFrame(articles).drop(columns=["isi"], errors="ignore")
        df["tanggal"] = df["tanggal"].astype(str)
        st.download_button("⬇️ Unduh daftar berita (.csv)", df.to_csv(index=False).encode("utf-8"),
                           file_name=f"berita_{kw}_{s}_{e}.csv", mime="text/csv")

    with tab_chart:
        df = pd.DataFrame(articles).dropna(subset=["tanggal"])
        if not df.empty:
            df["hari"] = pd.to_datetime(df["tanggal"], utc=True).dt.tz_convert("Asia/Jakarta").dt.date
            st.subheader("Jumlah berita per hari")
            st.bar_chart(df.groupby("hari").size())
            st.subheader("Media teratas")
            st.bar_chart(df["media"].value_counts().head(10))
