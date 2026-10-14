"""
Media Monitoring MVP
- Input keyword + rentang tanggal
- Cari berita online (Google News RSS, edisi Indonesia)
- Ringkasan otomatis dengan Claude
"""

import html
import re
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


@st.cache_data(ttl=900, show_spinner=False)
def search_news(keyword: str, start: date, end: date, lang: str) -> list[dict]:
    hl, gl, ceid = ("id", "ID", "ID:id") if lang == "Indonesia" else ("en-US", "US", "US:en")
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(build_query(keyword, start, end))
        + f"&hl={hl}&gl={gl}&ceid={ceid}"
    )
    resp = requests.get(url, headers=HEADERS, timeout=20)
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)

    results, seen = [], set()
    for e in feed.entries:
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
            }
        )
    results.sort(key=lambda r: r["tanggal"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return results


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_fulltext(gnews_url: str) -> str:
    """Coba ambil isi artikel asli (best effort, boleh gagal)."""
    if not FULLTEXT_AVAILABLE:
        return ""
    try:
        decoded = gnewsdecoder(gnews_url, interval=1)
        real_url = decoded.get("decoded_url") if decoded.get("status") else None
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

try:
    secret_key = st.secrets.get("ANTHROPIC_API_KEY", "")
except Exception:  # belum ada file secrets
    secret_key = ""

with st.sidebar:
    st.header("Pengaturan")
    api_key = secret_key or st.text_input("Anthropic API key", type="password",
                                          help="Diperlukan untuk fitur ringkasan.")
    model = st.text_input("Model", value=DEFAULT_MODEL)
    lang = st.selectbox("Edisi berita", ["Indonesia", "Internasional (English)"])
    max_articles = st.slider("Maks. artikel diringkas", 5, 50, 25)
    use_fulltext = st.checkbox(
        "Ambil isi artikel lengkap (lebih akurat, lebih lambat)",
        value=False, disabled=not FULLTEXT_AVAILABLE,
    )

keyword = st.text_input("Keyword", placeholder='mis. "Bank Indonesia" suku bunga')
c1, c2 = st.columns(2)
today = date.today()
start = c1.date_input("Dari tanggal", value=today - timedelta(days=7), max_value=today)
end = c2.date_input("Sampai tanggal", value=today, max_value=today)

if st.button("🔍 Cari berita", type="primary", use_container_width=True):
    if not keyword.strip():
        st.warning("Isi keyword dulu.")
        st.stop()
    if start > end:
        st.warning("Tanggal awal harus sebelum tanggal akhir.")
        st.stop()
    with st.spinner("Mencari berita..."):
        try:
            st.session_state.articles = search_news(keyword.strip(), start, end, lang)
            st.session_state.meta = (keyword.strip(), start, end)
            st.session_state.summary = None
        except Exception as ex:
            st.error(f"Gagal mengambil berita: {ex}")
            st.stop()

articles = st.session_state.get("articles")
if articles is not None:
    kw, s, e = st.session_state.meta
    if not articles:
        st.info("Tidak ada berita ditemukan untuk keyword dan periode ini.")
        st.stop()

    m1, m2, m3 = st.columns(3)
    m1.metric("Jumlah berita", len(articles))
    m2.metric("Jumlah media", len({a["media"] for a in articles}))
    m3.metric("Periode", f"{(e - s).days + 1} hari")

    tab_sum, tab_list, tab_chart = st.tabs(["🧠 Ringkasan", "📄 Daftar berita", "📊 Statistik"])

    with tab_sum:
        if anthropic is None or not api_key:
            st.info("Masukkan Anthropic API key di sidebar untuk membuat ringkasan.")
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
            st.markdown(f"**{i}. [{a['judul']}]({a['link']})**  \n{a['media']} · {tgl}")
        df = pd.DataFrame(articles)
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
