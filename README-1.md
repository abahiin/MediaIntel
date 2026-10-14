# Media Monitoring — MVP

Aplikasi web sederhana untuk memantau pemberitaan online:

1. Masukkan **keyword**
2. Pilih **rentang tanggal**
3. Aplikasi mencari berita terkait (sumber: Google News, edisi Indonesia)
4. Klik **Buat ringkasan** → Claude merangkum isu utama, sentimen, media paling aktif, dan hal yang perlu diwaspadai

Fitur tambahan: unduh daftar berita (.csv), unduh ringkasan (.md), grafik jumlah berita per hari dan media teratas.

---

## Yang dibutuhkan

- Akun **GitHub** (gratis)
- Akun **Streamlit Community Cloud** (gratis, login pakai GitHub) — share.streamlit.io
- **Anthropic API key** untuk fitur ringkasan — console.anthropic.com → API Keys

## Cara online-kan (tanpa install apa pun, bisa dari laptop)

1. Buat repository baru di GitHub, misalnya `media-monitoring`.
2. Upload 3 file ini: `app.py`, `requirements.txt`, `README.md`.
3. Buka share.streamlit.io → **Create app** → pilih repository tadi, file utama `app.py` → **Deploy**.
4. Setelah jadi, buka **Settings → Secrets** di app tersebut, isi:
   ```
   ANTHROPIC_API_KEY = "sk-ant-..."
   ```
5. Selesai. Kamu dapat link (misalnya `xxx.streamlit.app`) yang bisa dibuka dari HP atau dibagikan ke tim.

### Opsional: tambah hasil Google Search (Serper.dev)

Google News hanya mencakup media yang terdaftar di Google News. Untuk ikut menangkap
situs lain (portal kecil, blog, rilis pers), daftar di serper.dev, salin API key-nya,
lalu tambahkan di Secrets:
```
SERPER_API_KEY = "xxxx"
```
Setiap pencarian memakai kredit Serper (atur jumlah halaman di sidebar).

## Cara jalankan di laptop sendiri

```bash
pip install -r requirements.txt
streamlit run app.py
```
Lalu masukkan API key di sidebar.

## Tips pencarian keyword

- Frasa persis pakai tanda kutip: `"Bank Indonesia"`
- Gabungan: `"OJK" asuransi`
- Kecualikan kata: `Garuda -Indonesia`
- Salah satu: `BI OR "Bank Indonesia"`

## Batasan MVP

- Google News RSS mengembalikan maksimal ±100 berita per pencarian. Untuk topik yang sangat ramai, persempit rentang tanggal.
- Secara default ringkasan dibuat dari judul + cuplikan berita. Centang **"Ambil isi artikel lengkap"** di sidebar untuk ringkasan yang lebih akurat (lebih lambat; sebagian situs bisa gagal diambil).
- Biaya ringkasan dihitung dari pemakaian Anthropic API (per kali klik "Buat ringkasan").

## Ide pengembangan berikutnya

- Simpan daftar keyword yang dipantau rutin
- Laporan otomatis harian/mingguan ke email
- Analisis sentimen per artikel
- Tambah sumber: portal berita spesifik, media sosial
