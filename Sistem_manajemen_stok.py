from supabase import create_client, Client
import hashlib
import difflib
import io
import json
import os
import random
import re
import secrets
import smtplib
from email.mime.text import MIMEText
from datetime import datetime, timedelta, timezone
import numpy as np
import pandas as pd
import streamlit as st

try:
    from PIL import Image as PILImage
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# --- 0. KONFIGURASI NAMA FILE PENYIMPANAN PERMANEN ---
# Path selalu mengacu ke folder tempat file .py ini berada,
# supaya tidak bentrok dengan project lain meskipun dijalankan
# dari CMD dengan working directory yang berbeda-beda.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "products_db.json")
IMG_DIR = os.path.join(BASE_DIR, "product_images")
USERS_FILE = os.path.join(BASE_DIR, "users_db.json")
REMEMBER_FILE = os.path.join(BASE_DIR, "remembered_user.json")
SESSIONS_FILE = os.path.join(BASE_DIR, "active_sessions.json")
os.makedirs(IMG_DIR, exist_ok=True)

# --- KONFIGURASI EMAIL PENGIRIM (untuk fitur Lupa Password) ---
# WAJIB DIISI supaya fitur kirim kode OTP ke email berfungsi.
# Kalau pakai Gmail: aktifkan 2-Step Verification lalu buat
# "App Password" di myaccount.google.com/apppasswords (JANGAN
# pakai password akun Gmail biasa).
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
SENDER_EMAIL = "SENDER_EMAIL"
SENDER_APP_PASSWORD = "SENDER_APP_PASSWORD"


def save_uploaded_image(barcode, uploaded_file):
    """Upload gambar produk ke Supabase Storage."""

    if uploaded_file is None:
        return None

    try:
        file_bytes = uploaded_file.getbuffer()

        # Validasi dan kompres gambar seperti sebelumnya
        if HAS_PIL:
            try:
                img_obj = PILImage.open(io.BytesIO(file_bytes))
                img_obj.verify()

                img_obj = PILImage.open(io.BytesIO(file_bytes))
                img_obj.load()

                MAX_SISI = 1000

                if max(img_obj.size) > MAX_SISI:
                    img_obj.thumbnail(
                        (MAX_SISI, MAX_SISI),
                        PILImage.LANCZOS
                    )

                buffer_keluaran = io.BytesIO()

                if img_obj.mode in ("RGBA", "P"):
                    img_obj = img_obj.convert("RGB")

                img_obj.save(
                    buffer_keluaran,
                    format="JPEG",
                    quality=80,
                    optimize=True
                )

                file_bytes = buffer_keluaran.getvalue()
                ext = ".jpg"
                content_type = "image/jpeg"

            except Exception:
                st.error(
                    "❌ File yang diupload bukan gambar yang valid "
                    "atau rusak."
                )
                return None

        else:
            ext = (
                os.path.splitext(uploaded_file.name)[1].lower()
                or ".jpg"
            )
            content_type = (
                uploaded_file.type
                or "image/jpeg"
            )

        safe_barcode = re.sub(
            r"[^A-Za-z0-9_-]",
            "_",
            str(barcode)
        )

        filename = f"{safe_barcode}{ext}"

        # Upload ke bucket Supabase
        supabase.storage \
            .from_("product-images") \
            .upload(
                path=filename,
                file=file_bytes,
                file_options={
                    "content-type": content_type,
                    "upsert": "true",
                    "cache-control": "3600",
                },
            )

        # Ambil URL publik
        public_url = (
            supabase.storage
            .from_("product-images")
            .get_public_url(filename)
        )

        return public_url

    except Exception as e:
        st.error(f"❌ Gagal mengupload foto ke Supabase: {e}")
        return None

def resolve_image_path(image_value):
    """Mengembalikan URL Supabase atau path lokal foto lama."""

    if not image_value:
        return None

    if not isinstance(image_value, str):
        return None

    image_value = image_value.strip()

    if not image_value:
        return None

    # Foto baru yang sudah berada di Supabase Storage
    if image_value.startswith("http://") or image_value.startswith("https://"):
        return image_value

    # Foto lama yang masih tersimpan secara lokal
    filename = os.path.basename(
        image_value.replace("\\", "/")
    )

    kandidat_di_img_dir = os.path.join(
        IMG_DIR,
        filename
    )

    if os.path.exists(kandidat_di_img_dir):
        return kandidat_di_img_dir

    # Fallback ke path lama jika masih valid
    if os.path.exists(image_value):
        return image_value

    return None

def delete_image_file(image_value):
    """Menghapus foto dari Supabase Storage atau file lokal lama."""

    if not image_value:
        return

    try:
        # Jika foto berasal dari Supabase Storage
        if isinstance(image_value, str) and (
            image_value.startswith("http://")
            or image_value.startswith("https://")
        ):
            filename = image_value.split("/")[-1]

            (
                supabase
                .storage
                .from_("product-images")
                .remove([filename])
            )

            return

        # Jika masih foto lama yang tersimpan lokal
        resolved = resolve_image_path(image_value)

        if resolved and os.path.exists(resolved):
            try:
                os.remove(resolved)
            except Exception:
                pass

    except Exception as e:
        st.warning(f"Foto gagal dihapus dari storage: {e}")


def cari_kandidat_nama_di_filename(filename, products_data):
    """Mencari SEMUA barang yang namanya cocok dengan sebuah nama file
    foto (dipakai bareng oleh guess_product_for_filename, dan oleh
    tampilan buat kasih tau user ada barang nama sama merek apa aja)."""
    base = os.path.splitext(filename)[0]
    base_norm = re.sub(r"[_\-]+", " ", base).strip().lower()
    if not base_norm:
        return []
    kandidat = []
    for p in products_data:
        name_norm = re.sub(r"[_\-]+", " ", p["name"]).strip().lower()
        if name_norm and (name_norm in base_norm or base_norm in name_norm):
            kandidat.append(p)
    return kandidat


def guess_product_for_filename(filename, products_data):
    """Menebak barang mana yang cocok untuk sebuah nama file foto, dengan
    mencocokkan nama file (tanpa ekstensi) ke nama barang. Dipakai supaya
    upload banyak foto sekaligus nggak perlu dicocokkan manual satu-satu
    kalau nama filenya sudah mirip nama barangnya.

    Kalau ada BEBERAPA barang dengan nama sama tapi merek beda (misal
    3x "Map Sneilhekter" dari Buffalo/Carinex/TriJaya), fungsi ini juga
    coba cocokkan mereknya dari nama file (misal file
    "Map Sneilhekter Carinex.png" harus kecocok ke yang Carinex, bukan
    asal ke yang pertama ketemu). Kalau merek di nama file nggak jelas,
    mendingan nggak nebak sama sekali (return None, biar defaultnya di
    dropdown "Lewati") daripada nebak ke barang yang salah."""
    base = os.path.splitext(filename)[0]
    base_norm = re.sub(r"[_\-]+", " ", base).strip().lower()
    if not base_norm:
        return None

    kandidat = cari_kandidat_nama_di_filename(filename, products_data)

    if not kandidat:
        # Kalau tidak ada yang persis, coba kemiripan teks (typo/singkatan dikit)
        names_norm = [re.sub(r"[_\-]+", " ", p["name"]).strip().lower() for p in products_data]
        close = difflib.get_close_matches(base_norm, names_norm, n=1, cutoff=0.6)
        if close:
            idx = names_norm.index(close[0])
            return products_data[idx]["barcode"]
        return None

    if len(kandidat) == 1:
        return kandidat[0]["barcode"]

    # Nama-nya sama di beberapa barang — coba bedain pakai merek yang
    # ada di nama file
    for p in kandidat:
        merek_norm = re.sub(r"[_\-]+", " ", str(p.get("merek", ""))).strip().lower()
        if merek_norm and merek_norm in base_norm:
            return p["barcode"]

    # Merek di nama file nggak jelas/nggak ketemu -> jangan asal nebak
    # salah satu, biar user yang pilih manual di dropdown
    return None


def get_pagination_slice(df, page_key, page_size=15):
    """Menghitung potongan halaman yang lagi aktif dari tabel panjang,
    supaya Streamlit gak harus render ratusan foto + form sekaligus
    (itu yang bikin lag/lama loading kalau barangnya udah 76-150+).
    Mengembalikan (df_halaman_ini, halaman_sekarang, total_halaman)."""
    total = len(df)
    if total == 0:
        return df, 1, 1

    total_pages = max(1, -(-total // page_size))  # pembulatan ke atas

    if page_key not in st.session_state:
        st.session_state[page_key] = 1
    if st.session_state[page_key] > total_pages:
        st.session_state[page_key] = total_pages
    if st.session_state[page_key] < 1:
        st.session_state[page_key] = 1

    halaman_sekarang = st.session_state[page_key]
    start = (halaman_sekarang - 1) * page_size
    end = start + page_size
    return df.iloc[start:end], halaman_sekarang, total_pages


def render_pagination_controls(page_key, halaman_sekarang, total_pages, total_items, posisi):
    """Menampilkan tombol Sebelumnya/Selanjutnya. Dipanggil dua kali per
    daftar (atas & bawah) supaya abis scroll ke bawah lihat-lihat barang,
    bisa langsung pindah halaman tanpa scroll balik ke atas lagi."""
    if total_pages <= 1:
        return

    c_prev, c_mid, c_next = st.columns([1, 2, 1])
    with c_prev:
        if st.button(
            "⬅️ Sebelumnya",
            key=f"{page_key}_prev_{posisi}",
            disabled=halaman_sekarang <= 1,
            use_container_width=True,
        ):
            st.session_state[page_key] -= 1
            st.rerun()
    with c_mid:
        st.markdown(
            f"<div style='text-align:center; padding-top:0.4rem;'>"
            f"Halaman {halaman_sekarang} dari {total_pages} "
            f"({total_items} barang)</div>",
            unsafe_allow_html=True,
        )
    with c_next:
        if st.button(
            "Selanjutnya ➡️",
            key=f"{page_key}_next_{posisi}",
            disabled=halaman_sekarang >= total_pages,
            use_container_width=True,
        ):
            st.session_state[page_key] += 1
            st.rerun()


def reset_page_if_search_changed(search_value, search_state_key, page_key):
    """Balik ke halaman 1 setiap kali kata kuncinya berubah, supaya gak
    nyangkut di halaman kosong kalau hasil pencarian jadi lebih sedikit."""
    prev_value = st.session_state.get(search_state_key)
    if prev_value != search_value:
        st.session_state[page_key] = 1
    st.session_state[search_state_key] = search_value


# =========================================================
# --- SISTEM AUTENTIKASI (LOGIN, INGAT USERNAME, LUPA PASSWORD) ---
# =========================================================


def load_sessions():
    """Mengambil semua session dari Supabase."""

    try:
        response = (
            supabase
            .table("sessions")
            .select("*")
            .execute()
        )

        sessions = {}

        for row in response.data or []:
            sessions[row["token"]] = {
                "username": row["username"],
                "expiry": row["expires_at"],
            }

        return sessions

    except Exception as e:
        st.error(f"Gagal mengambil session dari Supabase: {e}")
        return {}


def save_sessions(sessions):
    """Menyimpan session ke Supabase."""

    try:
        for token, session in sessions.items():

            data = {
                "token": token,
                "username": session["username"],
                "expires_at": session["expiry"],
            }

            (
                supabase
                .table("sessions")
                .upsert(data)
                .execute()
            )

        return True

    except Exception as e:
        st.error(f"Gagal menyimpan session ke Supabase: {e}")
        return False


def create_session(username):
    """Membuat token sesi baru yang berlaku selama 7 hari."""

    token = secrets.token_hex(24)

    expires_at = (
        datetime.now(timezone.utc) + timedelta(days=7)
    ).isoformat()

    data = {
        "token": token,
        "username": username,
        "expires_at": expires_at,
    }

    try:
        (
            supabase
            .table("sessions")
            .insert(data)
            .execute()
        )

        return token

    except Exception as e:
        st.error(f"Gagal membuat session: {e}")
        return None


def validate_session(token):
    """Mengecek apakah token session masih berlaku."""

    if not token:
        return None

    try:
        response = (
            supabase
            .table("sessions")
            .select("*")
            .eq("token", token)
            .limit(1)
            .execute()
        )

        if not response.data:
            return None

        entry = response.data[0]

        expires_at = datetime.fromisoformat(
            entry["expires_at"].replace("Z", "+00:00")
        )

        now = datetime.now(timezone.utc)

        if now > expires_at:
            (
                supabase
                .table("sessions")
                .delete()
                .eq("token", token)
                .execute()
            )

            return None

        return entry["username"]

    except Exception as e:
        st.error(f"Gagal memvalidasi session: {e}")
        return None


def delete_session(token):
    """Menghapus session dari Supabase."""

    if not token:
        return

    try:
        (
            supabase
            .table("sessions")
            .delete()
            .eq("token", token)
            .execute()
        )

    except Exception as e:
        st.error(f"Gagal menghapus session: {e}")

    except Exception as e:
        st.error(f"Gagal menghapus session: {e}")


def get_query_param(key):
    """Wrapper supaya kompatibel dengan versi Streamlit lama & baru."""
    try:
        return st.query_params.get(key)
    except Exception:
        try:
            params = st.experimental_get_query_params()
            vals = params.get(key)
            return vals[0] if vals else None
        except Exception:
            return None


def set_query_param(key, value):
    try:
        st.query_params[key] = value
    except Exception:
        try:
            st.experimental_set_query_params(**{key: value})
        except Exception:
            pass


def clear_query_params():
    try:
        st.query_params.clear()
    except Exception:
        try:
            st.experimental_set_query_params()
        except Exception:
            pass


def hash_password(password):
    """Mengubah password menjadi hash SHA-256 supaya tidak disimpan mentah."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()

def load_users():
    """Mengambil daftar akun dari Supabase."""

    try:
        response = (
            supabase
            .table("users")
            .select("*")
            .order("id")
            .execute()
        )

        data = response.data if response.data else []

        return data

    except Exception as e:
        st.error(f"Gagal mengambil data pengguna dari Supabase: {e}")
        return []


def save_users(users):
    """Menyimpan perubahan data pengguna ke Supabase."""

    try:
        # Ambil user yang saat ini ada di Supabase
        existing_response = (
            supabase
            .table("users")
            .select("id, username, email")
            .execute()
        )

        existing_users = existing_response.data or []

        existing_ids = {
            user["id"]
            for user in existing_users
            if user.get("id") is not None
        }

        current_ids = {
            user["id"]
            for user in users
            if user.get("id") is not None
        }

        # Hapus akun yang sudah dihapus dari aplikasi
        ids_to_delete = existing_ids - current_ids

        for user_id in ids_to_delete:
            (
                supabase
                .table("users")
                .delete()
                .eq("id", user_id)
                .execute()
            )

        # Insert / update akun
        for user in users:
            data = {
                "username": user["username"],
                "password_hash": user["password_hash"],
                "email": user.get("email", ""),
            }

            if user.get("id") is not None:
                data["id"] = user["id"]

            (
                supabase
                .table("users")
                .upsert(data)
                .execute()
            )

        return True

    except Exception as e:
        st.error(f"Gagal menyimpan pengguna ke Supabase: {e}")
        return False


def find_user(username):
    for u in st.session_state["users_data"]:
        if u["username"].lower() == username.strip().lower():
            return u
    return None


def find_user_by_email(email):
    for u in st.session_state["users_data"]:
        if u.get("email", "").strip().lower() == email.strip().lower():
            return u
    return None


def load_remembered_username():
    if os.path.exists(REMEMBER_FILE):
        try:
            with open(REMEMBER_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("username", "")
        except Exception:
            return ""
    return ""


def save_remembered_username(username):
    with open(REMEMBER_FILE, "w", encoding="utf-8") as f:
        json.dump({"username": username}, f)


def clear_remembered_username():
    if os.path.exists(REMEMBER_FILE):
        try:
            os.remove(REMEMBER_FILE)
        except Exception:
            pass


def send_otp_email(to_email, otp_code):
    """Mengirim kode OTP 6 digit ke email pengguna lewat SMTP.
    Mengembalikan (True, pesan) jika berhasil, (False, pesan_error) jika gagal."""
    if "isi_email_pengirim" in SENDER_EMAIL or "isi_app_password" in SENDER_APP_PASSWORD:
        return False, (
            "Konfigurasi email pengirim belum diisi oleh admin aplikasi "
            "(lihat SENDER_EMAIL & SENDER_APP_PASSWORD di kode)."
        )

    try:
        subject = "Kode Verifikasi Reset Password - Stok Barang"
        body = (
            f"Halo,\n\nKode verifikasi (OTP) untuk reset password kamu adalah:\n\n"
            f"    {otp_code}\n\n"
            f"Kode ini berlaku selama 10 menit. Jangan berikan kode ini ke siapa pun.\n\n"
            f"Kalau kamu tidak merasa meminta reset password, abaikan email ini."
        )
        msg = MIMEText(body)
        msg["Subject"] = subject
        msg["From"] = SENDER_EMAIL
        msg["To"] = to_email

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_APP_PASSWORD)
            server.sendmail(SENDER_EMAIL, [to_email], msg.as_string())

        return True, "Kode OTP berhasil dikirim ke email kamu."
    except Exception as e:
        return False, f"Gagal mengirim email: {e}"


def render_register_form():
    """Form pendaftaran akun baru. Hanya dipanggil setelah user login."""
    st.subheader("📝 Tambah Akun Baru")
    st.caption(
        "Daftarkan akun baru untuk kasir/pegawai lain. Email wajib diisi dan "
        "harus valid, karena dipakai untuk fitur reset password."
    )
    with st.form("form_register"):
        new_username = st.text_input("Username baru")
        new_email = st.text_input("Email")
        new_password = st.text_input("Password", type="password")
        new_password_confirm = st.text_input("Konfirmasi Password", type="password")
        submit_register = st.form_submit_button(
            "Daftarkan Akun", use_container_width=True
        )

        if submit_register:
            if not new_username.strip() or not new_email.strip() or not new_password:
                st.warning("Semua kolom wajib diisi.")
            elif "@" not in new_email or "." not in new_email:
                st.warning("Format email tidak valid.")
            elif new_password != new_password_confirm:
                st.warning("Konfirmasi password tidak cocok.")
            elif len(new_password) < 6:
                st.warning("Password minimal 6 karakter.")
            elif find_user(new_username):
                st.error("Username sudah dipakai, coba nama lain.")
            elif find_user_by_email(new_email):
                st.error("Email sudah terdaftar di akun lain.")
            else:
                st.session_state["users_data"].append(
                    {
                        "username": new_username.strip(),
                        "password_hash": hash_password(new_password),
                        "email": new_email.strip(),
                    }
                )
                save_users(st.session_state["users_data"])
                st.success(f"Akun **{new_username.strip()}** berhasil dibuat!")

    st.divider()
    st.caption("📋 Daftar Akun Terdaftar")

    current_username = st.session_state.get("current_user")
    total_akun = len(st.session_state["users_data"])

    for u in st.session_state["users_data"]:
        c_u1, c_u2 = st.columns([4, 1])
        with c_u1:
            label = f"**{u['username']}** &nbsp; ({u.get('email', '-')})"
            if u["username"] == current_username:
                label += "  &nbsp; 🟢 *sedang login*"
            st.markdown(label)
        with c_u2:
            is_self = u["username"] == current_username
            is_last_account = total_akun <= 1
            if is_self:
                st.caption("Akun aktif")
            elif is_last_account:
                st.caption("Min. 1 akun")
            else:
                with st.popover("🗑️ Hapus", use_container_width=True):
                    st.warning(
                        f"Yakin mau hapus akun **{u['username']}**? "
                        "Tindakan ini tidak bisa dibatalkan."
                    )
                    if st.button(
                        "Ya, Hapus Akun Ini",
                        type="primary",
                        key=f"del_user_{u['username']}",
                    ):
                        st.session_state["users_data"] = [
                            x
                            for x in st.session_state["users_data"]
                            if x["username"] != u["username"]
                        ]
                        save_users(st.session_state["users_data"])
                        st.success(f"Akun **{u['username']}** berhasil dihapus.")
                        st.rerun()


def render_login_page():
    """Menampilkan halaman Login / Daftar / Lupa Password.
    Menghentikan eksekusi app utama selama user belum login."""

    st.markdown(
        "<h1 style='text-align:center;'>📦 Stok Barang </h1>",
        unsafe_allow_html=True,
    )

    login_tab, forgot_tab = st.tabs(["🔐 Login", "❓ Lupa Password"])

    # ------------------- TAB LOGIN -------------------
    with login_tab:
        remembered = load_remembered_username()
        with st.form("form_login"):
            username_input = st.text_input("Username", value=remembered)
            password_input = st.text_input("Password", type="password")
            remember_me = st.checkbox("Ingat username saya", value=bool(remembered))
            submit_login = st.form_submit_button("Login", use_container_width=True)

            if submit_login:
                user = find_user(username_input)
                if user and user["password_hash"] == hash_password(password_input):
                    token = create_session(user["username"])
                    st.session_state["logged_in"] = True
                    st.session_state["current_user"] = user["username"]
                    st.session_state["session_token"] = token
                    set_query_param("token", token)

                    if remember_me:
                        save_remembered_username(user["username"])
                    else:
                        clear_remembered_username()

                    st.success(f"Selamat datang, {user['username']}!")
                    st.rerun()
                else:
                    st.error("Username atau password salah.")

    # ------------------- TAB LUPA PASSWORD -------------------
    with forgot_tab:
        if "reset_stage" not in st.session_state:
            st.session_state["reset_stage"] = "request"

        # Tahap 1: minta email, kirim OTP
        if st.session_state["reset_stage"] == "request":
            with st.form("form_forgot_request"):
                reset_username = st.text_input("Username")
                reset_email = st.text_input("Email terdaftar")
                submit_reset_request = st.form_submit_button(
                    "Kirim Kode OTP", use_container_width=True
                )

                if submit_reset_request:
                    user = find_user(reset_username)
                    if not user:
                        st.error("Username tidak ditemukan.")
                    elif user.get("email", "").strip().lower() != reset_email.strip().lower():
                        st.error("Email tidak cocok dengan akun ini.")
                    else:
                        otp_code = f"{random.randint(0, 999999):06d}"
                        ok, msg = send_otp_email(reset_email, otp_code)
                        if ok:
                            st.session_state["reset_otp"] = otp_code
                            st.session_state["reset_otp_expiry"] = (
                                datetime.now() + timedelta(minutes=10)
                            )
                            st.session_state["reset_username_target"] = user["username"]
                            st.session_state["reset_stage"] = "verify"
                            st.success(msg)
                            st.rerun()
                        else:
                            st.error(msg)

        # Tahap 2: verifikasi OTP
        elif st.session_state["reset_stage"] == "verify":
            st.info(
                f"Kode OTP telah dikirim ke email untuk akun "
                f"**{st.session_state['reset_username_target']}**. "
                f"Masukkan kode 6 digit di bawah ini. "
                f"**(Pastikan Cek Folder Email SPAM!)**"
            )
            with st.form("form_forgot_verify"):
                otp_input = st.text_input("Kode OTP")
                col_v1, col_v2 = st.columns(2)
                with col_v1:
                    submit_verify = st.form_submit_button(
                        "Verifikasi", use_container_width=True
                    )
                with col_v2:
                    cancel_verify = st.form_submit_button(
                        "Batal", use_container_width=True
                    )

                if cancel_verify:
                    st.session_state["reset_stage"] = "request"
                    st.rerun()

                if submit_verify:
                    if datetime.now() > st.session_state.get(
                        "reset_otp_expiry", datetime.now()
                    ):
                        st.error("Kode OTP sudah kedaluwarsa. Silakan minta kode baru.")
                        st.session_state["reset_stage"] = "request"
                    elif otp_input.strip() == st.session_state.get("reset_otp"):
                        st.session_state["reset_stage"] = "new_password"
                        st.rerun()
                    else:
                        st.error("Kode OTP salah.")

        # Tahap 3: set password baru
        elif st.session_state["reset_stage"] == "new_password":
            st.success("Verifikasi berhasil! Silakan buat password baru.")
            with st.form("form_forgot_newpass"):
                new_pass = st.text_input("Password Baru", type="password")
                new_pass_confirm = st.text_input(
                    "Konfirmasi Password Baru", type="password"
                )
                submit_newpass = st.form_submit_button(
                    "Simpan Password Baru", use_container_width=True
                )

                if submit_newpass:
                    if len(new_pass) < 6:
                        st.warning("Password minimal 6 karakter.")
                    elif new_pass != new_pass_confirm:
                        st.warning("Konfirmasi password tidak cocok.")
                    else:
                        target_username = st.session_state["reset_username_target"]
                        user = find_user(target_username)
                        user["password_hash"] = hash_password(new_pass)
                        save_users(st.session_state["users_data"])

                        for key in [
                            "reset_stage",
                            "reset_otp",
                            "reset_otp_expiry",
                            "reset_username_target",
                        ]:
                            st.session_state.pop(key, None)

                        st.success(
                            "Password berhasil diubah! Silakan login dengan password baru."
                        )


def load_data_from_supabase():
    """Mengambil seluruh data produk dari Supabase."""
    try:
        response = (
            supabase
            .table("products")
            .select("*")
            .order("id")
            .execute()
        )

        data = response.data if response.data else []

        return data

    except Exception as e:
        st.error(f"Gagal mengambil data produk dari Supabase: {e}")
        return []


def save_data_to_supabase(data):
    """Menyimpan perubahan seluruh data produk ke Supabase."""

    try:
        # Ambil ID produk yang sekarang ada di Supabase
        existing = (
            supabase
            .table("products")
            .select("id")
            .execute()
        )

        existing_ids = {row["id"] for row in existing.data}

        current_ids = {
            item["id"]
            for item in data
            if item.get("id") is not None
        }

        # Hapus produk yang sudah tidak ada di session_state
        ids_to_delete = existing_ids - current_ids

        if ids_to_delete:
            for product_id in ids_to_delete:
                supabase \
                    .table("products") \
                    .delete() \
                    .eq("id", product_id) \
                    .execute()

        # Simpan/update produk
        for item in data:
            product = {
                "id": item.get("id"),
                "barcode": item.get("barcode"),
                "name": item.get("name"),
                "merek": item.get("merek", ""),
                "category": item.get("category", ""),
                "unit": item.get("unit", "Unit"),
                "stock": int(item.get("stock", 0)),
                "price": float(item.get("price", 0)),
                "expiry_date": item.get("expiry_date") or None,
                "image": item.get("image"),
            }

            (
                supabase
                .table("products")
                .upsert(product)
                .execute()
            )

        return True

    except Exception as e:
        st.error(f"Gagal menyimpan data ke Supabase: {e}")
        return False

# --- 1. DETEKSI & IMPORT PUSTAKA UTAMA ---
HAS_ZXING = False
HAS_PYZBAR = False
HAS_OPENCV = False

try:
    import zxingcpp

    HAS_ZXING = True
except ImportError:
    HAS_ZXING = False

try:
    from pyzbar.pyzbar import decode as decode_pyzbar

    HAS_PYZBAR = True
except ImportError:
    HAS_PYZBAR = False

try:
    import cv2

    HAS_OPENCV = True
except ImportError:
    HAS_OPENCV = False


# --- 2. PARSER DATA QRIS (EMVCo Standar) ---
def parse_qris_payload(qr_data):
    if not isinstance(qr_data, str) or not qr_data.startswith("000201"):
        return None

    details = {
        "is_qris": True,
        "merchant_name": "Tidak Diketahui",
        "merchant_city": "Tidak Diketahui",
        "nmid": "-",
        "amount": None,
    }

    try:
        i = 0
        length = len(qr_data)
        while i < length:
            tag = qr_data[i : i + 2]
            val_len = int(qr_data[i + 2 : i + 4])
            val = qr_data[i + 4 : i + 4 + val_len]

            if tag == "59":
                details["merchant_name"] = val
            elif tag == "60":
                details["merchant_city"] = val
            elif tag == "54":
                details["amount"] = float(val)
            elif tag in ["26", "51"]:
                sub_i = 0
                sub_len = len(val)
                while sub_i < sub_len:
                    sub_tag = val[sub_i : sub_i + 2]
                    sub_val_len = int(val[sub_i + 2 : sub_i + 4])
                    sub_val = val[sub_i + 4 : sub_i + 4 + sub_val_len]
                    if sub_tag == "02":
                        details["nmid"] = sub_val
                    sub_i += 4 + sub_val_len

            i += 4 + val_len
        return details
    except Exception:
        return {"is_qris": True, "merchant_name": "QRIS Valid", "nmid": "-"}


# --- 3. KONFIGURASI HALAMAN & RESPONSIVE UI UNTUK HP ---
st.set_page_config(
    page_title="Inventaris Barang",
    page_icon="📦",
    layout="wide",
)

SUPABASE_URL = st.secrets["SUPABASE_URL"]
SUPABASE_KEY = st.secrets["SUPABASE_KEY"]

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


try:
    test = supabase.table("products").select("id").limit(1).execute()
    st.success("✅ Berhasil terhubung ke Supabase!")
except Exception as e:
    st.error(f"❌ Gagal terhubung ke Supabase: {e}")

# --- TAMPILAN: SIMPEL & BERSIH ---
# Semua warna diatur langsung di sini lewat CSS (satu file ini aja,
# gak perlu file config terpisah). Tinggal ganti kode warnanya
# (yang format #XXXXXX) kalau nanti mau disesuaikan lagi.
#
# CATATAN PENTING soal tombol "System / Light / Dark" di menu titik tiga
# pojok kanan atas: itu FITUR BAWAAN STREAMLIT SENDIRI, bukan sesuatu
# yang kita buat, dan gak bisa dihilangkan dari kodingan. Kalau CSS kita
# cuma ngatur sebagian elemen (kayak sebelumnya), pas orang pilih "Dark"
# di menu itu, elemen yang gak kesentuh CSS kita bakal ganti ke warna
# gelap bawaan Streamlit sementara elemen lain tetap kita paksa terang —
# hasilnya belang/aneh kayak di screenshot kamu. Makanya di bawah ini
# hampir semua jenis elemen (termasuk input, dropdown, kotak upload,
# expander, tab) dipaksa pakai palet kita sendiri pakai !important, jadi
# TAMPILANNYA TETAP SAMA & KONSISTEN walau orang lain pencet System/
# Light/Dark sekalipun.
WARNA_LATAR = "#E1DBC9"        # warna latar belakang halaman (krem lebih gelap/pekat lagi)
WARNA_LATAR_KARTU = "#F1EEE2"  # warna kotak metric/kartu/input (krem lembut, bukan putih)
WARNA_GARIS = "#B8B0A0"        # warna garis pinggir kotak (dipertegas biar keliatan)
WARNA_TEKS = "#262624"         # warna teks utama
WARNA_TEKS_REDUP = "#6B7280"   # warna teks sekunder/caption
WARNA_UTAMA = "#16A34A"        # warna tombol/aksen utama (hijau)
WARNA_UTAMA_HOVER = "#15803D"  # warna tombol saat di-hover
WARNA_HEADER_BG = "#1B4332"        # warna pita hijau tua di judul paling atas
WARNA_HEADER_TEKS = "#F3EFE0"      # warna teks di dalam pita hijau (krem terang)
WARNA_HEADER_KARTU = "#2D6A4F"     # warna kartu metric di dalam pita hijau
WARNA_HEADER_KARTU_GARIS = "#40916C"  # warna garis kartu di dalam pita hijau

st.markdown(
    f"""
<style>
    /* PENTING: banyak kontrol bawaan browser (bulatan radio, checkbox,
       kalender tanggal) itu dirender NATIVE oleh browser sendiri
       berdasarkan properti CSS "color-scheme", BUKAN dari warna
       background yang kita atur di div-nya. Kalau ini nggak di-set,
       kontrol itu tetap ikut gelap pas mode Dark dipilih walau semua
       CSS lain di bawah ini udah dipaksa terang. Baris ini kunci utama
       biar radio/checkbox nggak item sendiri lagi. */
    html, body, .stApp {{
        color-scheme: light !important;
    }}

    /* Latar belakang halaman & warna teks dasar — dipaksa (!important)
       supaya gak ketiban tema gelap bawaan Streamlit kalau ada yang
       pencet "Dark" di menu titik tiga */
    .stApp, [data-testid="stAppViewContainer"], [data-testid="stMain"],
    [data-testid="stHeader"], [data-testid="stBottomBlockContainer"] {{
        background-color: {WARNA_LATAR} !important;
        color: {WARNA_TEKS} !important;
    }}
    [data-testid="stHeader"] {{
        background-color: transparent !important;
    }}

    h1, h2, h3, h4, h5, h6, p, span, label, li,
    [data-testid="stMarkdownContainer"] {{
        color: {WARNA_TEKS} !important;
    }}
    [data-testid="stCaptionContainer"], small {{
        color: {WARNA_TEKS_REDUP} !important;
    }}

    /* Pita hijau di judul paling atas ("Stok Barang" + kartu ringkasan),
       terpisah dari warna krem di badan halaman */
    .st-key-header_hijau {{
        background-color: {WARNA_HEADER_BG} !important;
        padding: 1.25rem 1.25rem 1rem 1.25rem;
        border-radius: 14px;
        margin-bottom: 0.5rem;
    }}
    .st-key-header_hijau h1, .st-key-header_hijau h2,
    .st-key-header_hijau h3, .st-key-header_hijau p,
    .st-key-header_hijau span, .st-key-header_hijau label {{
        color: {WARNA_HEADER_TEKS} !important;
    }}
    .st-key-header_hijau div[data-testid="stMetric"] {{
        background-color: {WARNA_HEADER_KARTU} !important;
        border: 1px solid {WARNA_HEADER_KARTU_GARIS} !important;
    }}
    .st-key-header_hijau [data-testid="stMetricValue"],
    .st-key-header_hijau [data-testid="stMetricLabel"] {{
        color: {WARNA_HEADER_TEKS} !important;
    }}

    /* Sidebar dibikin senada sama latar utama, biar gak keliatan
       kotak putih terang yang norak sendiri */
    section[data-testid="stSidebar"] {{
        background-color: {WARNA_LATAR} !important;
        border-right: 1px solid {WARNA_GARIS};
    }}

    /* Kartu metric (misal "Jenis Barang", "Nilai Total Stok") */
    body div[data-testid="stMetric"] {{
        background-color: {WARNA_LATAR_KARTU} !important;
        border: 2px solid {WARNA_GARIS} !important;
        border-radius: 12px;
        padding: 0.9rem 1rem;
        box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    }}
    [data-testid="stMetricValue"], [data-testid="stMetricLabel"] {{
        color: {WARNA_TEKS} !important;
    }}
    /* Angka Jumlah Harga sempat kepotong jadi "Rp 55,..." kalau kotaknya
       sempit — ini bikin dia boleh turun baris / nggak dipotong titik3,
       daripada mepet dan kepotong */
    [data-testid="stMetricValue"] {{
        white-space: normal !important;
        overflow: visible !important;
        text-overflow: unset !important;
        word-break: break-word;
    }}

    /* Kotak/kartu barang (st.container(border=True)), expander, popover.
       Border-nya sengaja ditebelin (2px) & dikasih awalan "body" biar
       gak gampang keilangan pas mode Dark dipilih */
    body div[data-testid="stVerticalBlockBorderWrapper"],
    body div[data-testid="stExpander"] details,
    body div[data-testid="stPopoverBody"] {{
        background-color: {WARNA_LATAR_KARTU} !important;
        border: 2px solid {WARNA_GARIS} !important;
        border-radius: 12px !important;
    }}

    /* Input teks, angka, dropdown, textarea — selector diulang dengan
       beberapa cara (data-testid, data-baseweb, awalan "body") biar
       makin sulit dikalahkan CSS bawaan Streamlit yang kadang lebih
       spesifik */
    body div[data-testid="stTextInput"] input,
    body div[data-testid="stNumberInput"] input,
    body div[data-testid="stTextArea"] textarea,
    body div[data-testid="stDateInput"] input,
    body div[data-testid="stSelectbox"] div[data-baseweb="select"],
    body div[data-testid="stSelectbox"] div[data-baseweb="select"] * {{
        background-color: {WARNA_LATAR_KARTU} !important;
        color: {WARNA_TEKS} !important;
    }}
    body div[data-testid="stTextInput"] input,
    body div[data-testid="stNumberInput"] input,
    body div[data-testid="stTextArea"] textarea,
    body div[data-testid="stDateInput"] input,
    body div[data-testid="stSelectbox"] div[data-baseweb="select"] > div {{
        border: 1.5px solid {WARNA_GARIS} !important;
        border-radius: 8px !important;
    }}

    /* Placeholder (teks contoh abu-abu di kotak kosong, misal "Contoh:
       Spidol Whiteboard") — ini elemen TERPISAH dari teks yang diketik,
       jadi harus di-set warnanya sendiri. Kalau nggak, pas mode Dark
       dipilih placeholder bisa ikutan putih/terang dan jadi nggak
       kelihatan di atas kotak yang sudah kita bikin terang juga. */
    body input::placeholder, body textarea::placeholder {{
        color: {WARNA_TEKS_REDUP} !important;
        opacity: 1 !important;
    }}

    /* Kotak upload file (Foto Produk, Import Excel, Upload Banyak Foto).
       Dicari pakai "mengandung kata FileUploader" (bukan nama persis)
       biar tetap kena walau nama testid detailnya beda-beda tergantung
       versi Streamlit — HANYA warna latar kotaknya, teks & tombol di
       dalamnya sengaja dibiarkan ikut aturan umum di bagian lain (biar
       gak numpuk/bentrok jadi tulisan malah hilang kayak sebelumnya). */
    body [data-testid*="FileUploader"] {{
        background-color: {WARNA_LATAR_KARTU} !important;
    }}
    body [data-testid="stFileUploaderDropzone"] {{
        border: 2px dashed {WARNA_GARIS} !important;
        border-radius: 8px !important;
    }}

    /* Daftar pilihan dropdown yang muncul pas kotak Kategori/Satuan
       diklik (ini elemen terpisah, nongol di atas semua) */
    ul[data-baseweb="menu"], ul[data-baseweb="menu"] *, li[role="option"] {{
        background-color: {WARNA_LATAR_KARTU} !important;
        color: {WARNA_TEKS} !important;
    }}
    li[role="option"]:hover, li[aria-selected="true"] {{
        background-color: {WARNA_LATAR} !important;
    }}

    /* Tombol pilihan bulat (radio), misal "Foto Barcode / Ketik Manual".
       Dicoba pakai beberapa cara sekaligus (data-baseweb, role ARIA)
       karena bulatannya itu dirender pakai beberapa lapis elemen yang
       namanya bisa beda-beda tergantung versi Streamlit. */
    div[data-baseweb="radio"] > div:first-child,
    div[data-testid="stRadio"] label > div:first-child,
    [role="radio"] {{
        background-color: {WARNA_LATAR_KARTU} !important;
        border: 1.5px solid {WARNA_TEKS_REDUP} !important;
    }}
    [role="radio"][aria-checked="true"],
    div[data-baseweb="radio"] input:checked + div {{
        background-color: {WARNA_UTAMA} !important;
        border-color: {WARNA_UTAMA} !important;
    }}

    /* Reset dasar untuk SEMUA tombol kecil bawaan widget (tombol +/-
       di kotak angka, "Browse files" di kotak upload, ikon
       tampilkan/sembunyikan password) supaya default-nya juga terang,
       sebelum tombol aksi utama (Simpan/Login/dst di bawah) menang
       jadi hijau karena lebih spesifik targetnya */
    button {{
        background-color: {WARNA_LATAR_KARTU} !important;
        color: {WARNA_TEKS} !important;
        border: 1px solid {WARNA_GARIS} !important;
    }}
    button svg {{
        fill: {WARNA_TEKS} !important;
    }}

    /* Tab menu (Scan / Cek Stok / Tambah & Edit / Kelola Akun) — dibuat
       transparan lagi, biar gak keikut jadi kotak putih gara-gara reset
       tombol umum di atas */
    button[data-baseweb="tab"] {{
        background-color: transparent !important;
        border: none !important;
        color: {WARNA_TEKS_REDUP} !important;
    }}
    button[data-baseweb="tab"][aria-selected="true"] {{
        color: {WARNA_UTAMA} !important;
    }}
    div[data-baseweb="tab-highlight"] {{
        background-color: {WARNA_UTAMA} !important;
    }}

    /* Semua jenis tombol: biasa, submit form, dan download */
    div[data-testid="stButton"] button,
    div[data-testid="stFormSubmitButton"] button,
    div[data-testid="stDownloadButton"] button {{
        background-color: {WARNA_UTAMA} !important;
        color: #FFFFFF !important;
        border: none !important;
        border-radius: 8px !important;
    }}
    div[data-testid="stButton"] button:hover,
    div[data-testid="stFormSubmitButton"] button:hover,
    div[data-testid="stDownloadButton"] button:hover {{
        background-color: {WARNA_UTAMA_HOVER} !important;
        color: #FFFFFF !important;
    }}
    div[data-testid="stButton"] button:disabled,
    div[data-testid="stFormSubmitButton"] button:disabled {{
        background-color: {WARNA_GARIS} !important;
        color: {WARNA_TEKS_REDUP} !important;
    }}

    /* Kotak kamera scan barcode */
    div[data-testid="stCameraInput"] {{
        border-radius: 12px;
        border: 1px solid {WARNA_GARIS};
    }}

    /* Tabel/dataframe */
    div[data-testid="stDataFrame"] {{
        border: 1px solid {WARNA_GARIS};
        border-radius: 10px;
    }}

    /* Tombol full-width otomatis di layar HP, biar gampang dipencet jari */
    @media (max-width: 640px) {{
        div[data-testid="stButton"] button,
        div[data-testid="stFormSubmitButton"] button,
        div[data-testid="stDownloadButton"] button {{
            width: 100%;
        }}
    }}
</style>
""",
    unsafe_allow_html=True,
)


# --- 3.5 GERBANG LOGIN ---
if "users_data" not in st.session_state:
    st.session_state["users_data"] = load_users()

if "logged_in" not in st.session_state:
    st.session_state["logged_in"] = False

    # Coba auto-login pakai token sesi dari URL, supaya login
    # tidak hilang meskipun halaman di-refresh (F5).
    url_token = get_query_param("token")
    auto_username = validate_session(url_token)
    if auto_username:
        st.session_state["logged_in"] = True
        st.session_state["current_user"] = auto_username
        st.session_state["session_token"] = url_token

if not st.session_state["logged_in"]:
    render_login_page()
    st.stop()

with st.sidebar:
    st.markdown(f"👤 Login sebagai **{st.session_state['current_user']}**")
    if st.button("🚪 Logout", use_container_width=True):
        delete_session(st.session_state.get("session_token"))
        clear_query_params()
        st.session_state["logged_in"] = False
        st.session_state["current_user"] = None
        st.session_state["session_token"] = None
        st.rerun()


if "products_data" not in st.session_state:
    st.session_state["products_data"] = load_data_from_supabase()


# --- 5. HELPER FUNCTIONS ---
DAFTAR_SATUAN = ["Unit", "Buah", "Lembar", "Rim", "Botol", "Pack", "Lusin", "Dus"]


def get_products():
    if not st.session_state["products_data"]:
        return pd.DataFrame(
            columns=[
                "id",
                "barcode",
                "name",
                "merek",
                "category",
                "unit",
                "stock",
                "price",
                "total_harga",
                "expiry_date",
                "image",
            ]
        )

    df = pd.DataFrame(st.session_state["products_data"])
    if "image" not in df.columns:
        df["image"] = None
    if "merek" not in df.columns:
        df["merek"] = ""
    if "unit" not in df.columns:
        df["unit"] = "Unit"
    df["merek"] = df["merek"].fillna("")
    df["unit"] = df["unit"].fillna("Unit")
    # Jumlah harga = harga satuan x stok, otomatis ikut naik/turun saat stok berubah
    df["total_harga"] = df["price"] * df["stock"]
    return df


def get_existing_categories():
    categories = list(
        set(
            p["category"]
            for p in st.session_state["products_data"]
            if p.get("category")
        )
    )
    default_cats = [
        "Alat Tulis Kantor",
        "Alat Kebersihan",
        "Elektronik",
        "Buku & Modul",
        "Perabotan",
        "Umum",
    ]
    for cat in default_cats:
        if cat not in categories:
            categories.append(cat)
    return sorted(categories)


def get_product_by_barcode(barcode_code):
    for p in st.session_state["products_data"]:
        if p["barcode"] == barcode_code:
            return (
                p["id"],
                p["barcode"],
                p["name"],
                p.get("merek", ""),
                p["category"],
                p.get("unit", "Unit"),
                p["stock"],
                p["price"],
                p["expiry_date"],
            )
    return None


def find_products_by_name(name_query, exclude_barcode=None):
    """Mencari barang dengan nama mirip (dipakai untuk mencegah data double
    saat barang ditambahkan manual maupun lewat scan barcode baru)."""
    if not name_query or not name_query.strip():
        return []
    q = name_query.strip().lower()
    hasil = []
    for p in st.session_state["products_data"]:
        if exclude_barcode and p["barcode"] == exclude_barcode:
            continue
        if q in p["name"].strip().lower():
            hasil.append(p)
    return hasil


def save_product_to_db(
    barcode, name, merek, category, unit, stock, price, expiry_date, image_path=None
):
    for p in st.session_state["products_data"]:
        if p["barcode"] == barcode:
            p["name"] = name
            p["merek"] = merek
            p["category"] = category
            p["unit"] = unit
            p["stock"] = stock
            p["price"] = price
            p["expiry_date"] = expiry_date
            if image_path:
                p["image"] = image_path
            save_data_to_supabase(st.session_state["products_data"])
            return

    new_id = len(st.session_state["products_data"]) + 1
    st.session_state["products_data"].append(
        {
            "id": new_id,
            "barcode": barcode,
            "name": name,
            "merek": merek,
            "category": category,
            "unit": unit,
            "stock": stock,
            "price": price,
            "expiry_date": expiry_date,
            "image": image_path,
        }
    )
    save_data_to_supabase(st.session_state["products_data"])


def attach_barcode_to_product(old_barcode, new_barcode):
    """Menempelkan barcode hasil scan ke barang lama yang sebelumnya
    ditambahkan manual (tanpa barcode), supaya tidak tercatat double."""
    for p in st.session_state["products_data"]:
        if p["barcode"] == old_barcode:
            p["barcode"] = new_barcode
            save_data_to_supabase(st.session_state["products_data"])
            return True
    return False


# --- IMPORT BANYAK BARANG SEKALIGUS DARI EXCEL/CSV ---
IMPORT_KOLOM_WAJIB = {
    "nama barang": "name",
    "nama": "name",
    "merek": "merek",
    "kategori": "category",
    "satuan": "unit",
    "stok": "stock",
    "volume": "stock",
    "harga satuan": "price",
    "harga": "price",
}


def parse_import_file(uploaded_file):
    """Membaca file Excel/CSV yang diupload user dan mengubahnya jadi
    DataFrame dengan kolom baku (name, merek, category, unit, stock, price).
    Nama kolom di file boleh fleksibel (huruf besar/kecil, 'Stok' atau
    'Volume', dll) asal salah satu nama yang dikenal di IMPORT_KOLOM_WAJIB.
    Mengembalikan (df_bersih, pesan_error)."""
    try:
        if uploaded_file.name.lower().endswith(".csv"):
            df_raw = pd.read_csv(uploaded_file)
        else:
            df_raw = pd.read_excel(uploaded_file)
    except Exception as e:
        return None, f"Gagal membaca file: {e}"

    rename_map = {}
    for col in df_raw.columns:
        key = str(col).strip().lower()
        if key in IMPORT_KOLOM_WAJIB:
            rename_map[col] = IMPORT_KOLOM_WAJIB[key]
    df_raw = df_raw.rename(columns=rename_map)

    kolom_dibutuhkan = ["name", "stock", "price"]
    kolom_hilang = [k for k in kolom_dibutuhkan if k not in df_raw.columns]
    if kolom_hilang:
        return None, (
            "Kolom wajib tidak ditemukan di file: "
            f"{', '.join(kolom_hilang)}. Pastikan ada kolom 'Nama Barang', "
            "'Stok', dan 'Harga Satuan'."
        )

    for opsional, isi_default in [
        ("merek", ""), ("category", "Umum"), ("unit", "Unit")
    ]:
        if opsional not in df_raw.columns:
            df_raw[opsional] = isi_default

    df_raw = df_raw[df_raw["name"].notna() & (df_raw["name"].astype(str).str.strip() != "")]
    df_raw["merek"] = df_raw["merek"].fillna("").astype(str).str.strip()
    df_raw["merek"] = df_raw["merek"].replace("-", "")
    df_raw["category"] = df_raw["category"].fillna("Umum").astype(str).str.strip()
    df_raw["unit"] = df_raw["unit"].fillna("Unit").astype(str).str.strip().str.title()
    df_raw["stock"] = pd.to_numeric(df_raw["stock"], errors="coerce").fillna(0).astype(int)
    df_raw["price"] = pd.to_numeric(df_raw["price"], errors="coerce").fillna(0).astype(float)

    return df_raw[["name", "merek", "category", "unit", "stock", "price"]], None


def bulk_save_products(df_import):
    """Menyimpan banyak barang sekaligus dari hasil parse_import_file ke
    database. Barang hasil import tidak punya barcode fisik, jadi tiap
    baris dikasih kode internal unik (sama seperti tambah manual satuan)."""
    for _, row in df_import.iterrows():
        internal_code = f"MANUAL-{secrets.token_hex(6)}"
        save_product_to_db(
            internal_code,
            str(row["name"]).strip(),
            str(row["merek"]).strip(),
            str(row["category"]).strip(),
            str(row["unit"]).strip(),
            int(row["stock"]),
            float(row["price"]),
            "",
            None,
        )


def update_product_in_db(barcode, new_stock, new_price, image_path=None, remove_image=False):
    """Memperbarui stok, harga satuan, dan (opsional) gambar produk
    berdasarkan barcode. Set remove_image=True untuk menghapus foto
    yang tersimpan (dipakai fitur Hapus Foto)."""
    for p in st.session_state["products_data"]:
        if p["barcode"] == barcode:
            p["stock"] = new_stock
            p["price"] = new_price
            if remove_image:
                p["image"] = None
            elif image_path:
                p["image"] = image_path
            save_data_to_supabase(st.session_state["products_data"])
            return True
    return False


def update_product_details_in_db(barcode, name, merek, category, unit):
    """Memperbarui nama, merek, kategori, dan satuan barang (bukan stok/harga)."""
    for p in st.session_state["products_data"]:
        if p["barcode"] == barcode:
            p["name"] = name
            p["merek"] = merek
            p["category"] = category
            p["unit"] = unit
            save_data_to_supabase(st.session_state["products_data"])
            return True
    return False


def delete_products_from_db(barcodes_to_delete):
    """Menghapus beberapa produk berdasarkan daftar barcode yang dipilih."""
    st.session_state["products_data"] = [
        p
        for p in st.session_state["products_data"]
        if p["barcode"] not in barcodes_to_delete
    ]
    save_data_to_supabase(st.session_state["products_data"])


def clear_all_db_data():
    st.session_state["products_data"] = []
    save_data_to_supabase(st.session_state["products_data"])


# --- 6. PARSER KADALUARSA OTOMATIS ---
def parse_expiry_from_barcode(barcode_str):
    if not barcode_str:
        return None

    clean_code = re.sub(r"[^\d]", "", str(barcode_str))

    gs1_match = re.search(
        r"17(\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])", clean_code
    )
    if gs1_match:
        yy, mm, dd = gs1_match.groups()
        year = 2000 + int(yy)
        try:
            return datetime(year, int(mm), int(dd)).date()
        except ValueError:
            pass

    date_match_1 = re.search(
        r"(202[4-9]|203[0-9])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])", clean_code
    )
    if date_match_1:
        yyyy, mm, dd = date_match_1.groups()
        try:
            return datetime(int(yyyy), int(mm), int(dd)).date()
        except ValueError:
            pass

    return None


# --- 7. DECODER ENGINE ---
def decode_image_unified(img):
    if HAS_ZXING:
        try:
            results = zxingcpp.read_barcodes(img)
            if results:
                return results[0].text
        except Exception:
            pass

    if HAS_PYZBAR:
        try:
            barcodes = decode_pyzbar(img)
            if barcodes:
                return barcodes[0].data.decode("utf-8")
        except Exception:
            pass

    return None


# --- 8. PEMBACAAN OTOMATIS ---
def auto_recover_and_scan(cv_img):
    if cv_img is None:
        return None

    code = decode_image_unified(cv_img)
    if code:
        return code

    if HAS_OPENCV:
        gray = (
            cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
            if len(cv_img.shape) == 3
            else cv_img
        )

        variants = []

        clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
        variants.append(clahe.apply(gray))

        inv_gamma = 1.0 / 0.5
        table = np.array([
            ((i / 255.0) ** inv_gamma) * 255 for i in np.arange(0, 256)
        ]).astype("uint8")
        variants.append(cv2.LUT(gray, table))

        gaussian_blur = cv2.GaussianBlur(gray, (0, 0), 3)
        sharpened = cv2.addWeighted(gray, 1.5, gaussian_blur, -0.5, 0)
        variants.append(sharpened)

        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        variants.append(thresh)

        for var in variants:
            code = decode_image_unified(var)
            if code:
                return code

            resized = cv2.resize(
                var,
                (var.shape[1] * 2, var.shape[0] * 2),
                interpolation=cv2.INTER_CUBIC,
            )
            code = decode_image_unified(resized)
            if code:
                return code

    return None


# --- 9. TAMPILAN APLIKASI KASIR ---
# Judul & ringkasan dibungkus st.container(key=...) supaya CSS bisa kasih
# warna beda (pita hijau) khusus buat bagian ini doang, terpisah dari
# warna krem di bagian bawahnya. Ini cuma pembungkus tampilan — isi &
# urutannya sama persis kayak sebelumnya, nggak ada logika yang berubah.
with st.container(key="header_hijau"):
    st.title("📦 Stok Barang")

    # --- Ringkasan singkat di atas semua menu, selalu terlihat ---
    _df_ringkasan = get_products()
    _col_r1, _col_r2 = st.columns(2)
    _col_r1.metric("📦 Jenis Barang", f"{len(_df_ringkasan)}")
    _col_r2.metric(
        "💰 Nilai Total Stok",
        f"Rp {_df_ringkasan['total_harga'].sum():,.0f}" if not _df_ringkasan.empty else "Rp 0",
    )

st.markdown("---")

tab1, tab2, tab3, tab4 = st.tabs([
    "🔍 Scan Barcode / QRIS",
    "📊 Cek Stok Barang",
    "⚙️ Tambah & Edit Produk",
    "👥 Kelola Akun",
])

if "last_scanned_code" not in st.session_state:
    st.session_state["last_scanned_code"] = None

# ----------------- TAB 1: SCANNER -----------------
with tab1:
    st.subheader("Pilih Cara Scan:")

    scan_mode = st.radio(
        "",
        [
            "📸 Foto Barcode (Kamera HP)",
            "⌨️ Ketik Manual / Alat Scan Gun",
        ],
        horizontal=True,
    )

    st.markdown("---")

    if scan_mode == "📸 Foto Barcode (Kamera HP)":
        # Fitur Pemilihan Kamera Depan / Belakang untuk HP
        col_cam1, col_cam2 = st.columns([2, 1])
        with col_cam1:
            st.caption(
                "📷 **Tips HP**: Untuk scan barang fisik, gunakan Kamera Belakang. "
                "Jika di browser HP terbuka kamera depan, klik icon switch kamera di dalam area foto."
            )
        with col_cam2:
            camera_side = st.selectbox(
                "📱 Pilihan Kamera HP",
                options=["Belakang (Environment)", "Depan (User)"],
                index=0,
                key="camera_selection"
            )

        # Pengaturan parameter kamera berdasarkan pilihan HP
        # "environment" = Kamera Belakang | "user" = Kamera Depan (Selfie)
        camera_facing_mode = "environment" if "Belakang" in camera_side else "user"

        # Tampilan kamera input Streamlit
        img_file = st.camera_input(
            "Ambil Foto Barcode / QRIS",
            key=f"camera_input_{camera_facing_mode}",
        )

        if img_file:
            raw_bytes = np.frombuffer(img_file.getvalue(), dtype=np.uint8)
            cv_img = (
                cv2.imdecode(raw_bytes, cv2.IMREAD_COLOR)
                if HAS_OPENCV
                else None
            )

            if cv_img is not None:
                with st.spinner("🔍 Membaca kode..."):
                    detected_code = auto_recover_and_scan(cv_img)

                if detected_code:
                    st.session_state["last_scanned_code"] = detected_code
                else:
                    st.error(
                        "❌ Barcode tidak terbaca. Silakan coba ambil foto"
                        " ulang dengan pencahayaan yang cukup."
                    )

    elif scan_mode == "⌨️ Ketik Manual / Alat Scan Gun":
        manual_code = st.text_input(
            "Masukkan Angka Barcode:",
            key="admin_barcode_input",
            placeholder="Contoh: 899123456789",
        )
        if manual_code.strip():
            st.session_state["last_scanned_code"] = manual_code.strip()

    scanned_code = st.session_state.get("last_scanned_code")

    if scanned_code:
        st.markdown("---")
        qris_info = parse_qris_payload(scanned_code)

        if qris_info:
            st.success("💳 **Pembayaran QRIS Terbaca!**")
            with st.container(border=True):
                col_q1, col_q2, col_q3 = st.columns(3)
                col_q1.metric(
                    "🏪 Nama Toko / Merchant", qris_info["merchant_name"]
                )
                col_q2.metric("📍 Kota", qris_info["merchant_city"])
                col_q3.metric("🆔 ID QRIS", qris_info["nmid"])

                if qris_info["amount"]:
                    st.info(f"💰 Nominal: Rp {qris_info['amount']:,.0f}")

            if st.button("🔄 Scan Barang Lain", key="btn_reset_qris"):
                st.session_state["last_scanned_code"] = None
                st.rerun()

        else:
            prod_data = get_product_by_barcode(scanned_code)

            if prod_data:
                p_id, p_barcode, p_name, p_merek, p_cat, p_unit, p_stock, p_price, p_exp = (
                    prod_data
                )

                st.success(f"✅ **Barang Ditemukan! (Kode: {p_barcode})**")

                col_res1, col_res2, col_res3, col_res4 = st.columns(4)
                col_res1.metric("📦 Nama Barang", p_name + (f" ({p_merek})" if p_merek else ""))
                col_res2.metric("🏷️ Kategori", p_cat)
                col_res3.metric("📊 Sisa Stok", f"{p_stock} {p_unit}")
                col_res4.metric("💰 Harga Satuan", f"Rp {p_price:,.0f}")

                if p_exp:
                    exp_date_obj = datetime.strptime(p_exp, "%Y-%m-%d").date()
                    sisa_hari = (exp_date_obj - datetime.now().date()).days
                    if sisa_hari < 0:
                        st.error(
                            f"🚨 Perhatian: Barang **{p_name}** sudah lewat"
                            f" tanggal kadaluarsa ({p_exp})!"
                        )
                    elif sisa_hari <= 7:
                        st.warning(
                            f"⚠️ Perhatian: Barang **{p_name}** mendekati"
                            f" kadaluarsa ({p_exp})!"
                        )

                if st.button("🔄 Scan Barang Lain", key="btn_reset_scan"):
                    st.session_state["last_scanned_code"] = None
                    st.rerun()

            else:
                st.info(f"✨ **Barang Baru! Kode Barcode: `{scanned_code}`**")

                # --- Cek dulu: jangan-jangan barang ini sudah pernah ditambahkan
                # manual (tanpa barcode) sebelumnya, supaya tidak tercatat double.
                name_check = st.text_input(
                    "Ketik dulu nama barangnya untuk dicek",
                    placeholder="Contoh: Spidol Whiteboard",
                    key="scan_name_check",
                )
                kandidat_sama = find_products_by_name(name_check)

                if kandidat_sama:
                    st.warning(
                        "⚠️ Ditemukan barang dengan nama mirip di data. Kalau ini "
                        "barang yang sama, tempelkan barcode ini ke barang lama "
                        "supaya tidak double, jangan tambah baru."
                    )
                    for kandidat in kandidat_sama:
                        c_k1, c_k2 = st.columns([3, 1])
                        judul_kandidat = kandidat["name"]
                        if kandidat.get("merek"):
                            judul_kandidat += f" ({kandidat['merek']})"
                        c_k1.write(
                            f"**{judul_kandidat}** — {kandidat['category']} — "
                            f"Stok: {kandidat['stock']} {kandidat.get('unit', 'Unit')} "
                            f"— Barcode lama: `{kandidat['barcode']}`"
                        )
                        if c_k2.button(
                            "🔗 Ini barang yang sama",
                            key=f"attach_{kandidat['barcode']}",
                        ):
                            attach_barcode_to_product(kandidat["barcode"], scanned_code)
                            st.success(
                                f"Barcode ditempelkan ke **{kandidat['name']}**, "
                                "tidak ada data double."
                            )
                            st.session_state["last_scanned_code"] = None
                            st.rerun()
                    st.markdown("---")

                st.write("Kalau memang barang baru, lengkapi datanya di bawah ini:")

                auto_parsed_exp = parse_expiry_from_barcode(scanned_code)
                existing_cats = get_existing_categories()
                cat_options = existing_cats + ["➕ Kategori Baru..."]

                with st.form(key="form_add_scanned_product", clear_on_submit=True):
                    col_f1, col_f2 = st.columns(2)

                    with col_f1:
                        new_name = st.text_input(
                            "Nama Barang *",
                            value=name_check,
                            placeholder="Contoh: Spidol Whiteboard",
                        )
                        new_merek = st.text_input(
                            "Merek (opsional)", placeholder="Contoh: Snowman"
                        )
                        selected_cat_option = st.selectbox(
                            "Kategori", cat_options
                        )

                        if selected_cat_option == "➕ Kategori Baru...":
                            custom_cat = st.text_input("Nama Kategori Baru *")
                        else:
                            custom_cat = None

                        new_image_file = st.file_uploader(
                            "🖼️ Foto Produk (opsional)",
                            type=["jpg", "jpeg", "png", "webp"],
                            key="new_product_image",
                        )
                        if new_image_file:
                            st.image(new_image_file, width=150)

                    with col_f2:
                        new_unit = st.selectbox("Satuan", DAFTAR_SATUAN)
                        new_stock = st.number_input(
                            "Jumlah Stok *", min_value=0, value=10, step=1
                        )
                        new_price = st.number_input(
                            "Harga Satuan (Rp) *",
                            min_value=0.0,
                            value=5000.0,
                            step=500.0,
                        )
                        st.caption(
                            f"💰 Jumlah Harga: Rp {new_stock * new_price:,.0f}"
                        )

                        new_expiry = st.date_input(
                            "Tanggal Kadaluarsa (opsional)",
                            value=auto_parsed_exp if auto_parsed_exp else None,
                        )

                    submit_btn = st.form_submit_button("💾 Simpan Barang")

                    if submit_btn:
                        final_category = (
                            custom_cat.strip()
                            if selected_cat_option == "➕ Kategori Baru..."
                            and custom_cat
                            else selected_cat_option
                        )

                        if not new_name.strip():
                            st.error("Nama Barang tidak boleh kosong!")
                        elif selected_cat_option == (
                            "➕ Kategori Baru..."
                        ) and not (custom_cat and custom_cat.strip()):
                            st.error("Nama Kategori Baru tidak boleh kosong!")
                        else:
                            exp_str = (
                                new_expiry.strftime("%Y-%m-%d")
                                if new_expiry
                                else ""
                            )
                            saved_image_path = save_uploaded_image(
                                scanned_code, new_image_file
                            )
                            save_product_to_db(
                                scanned_code,
                                new_name.strip(),
                                new_merek.strip(),
                                final_category,
                                new_unit,
                                int(new_stock),
                                float(new_price),
                                exp_str,
                                saved_image_path,
                            )
                            st.success(f"🎉 **{new_name}** berhasil disimpan!")
                            st.session_state["last_scanned_code"] = None
                            st.rerun()

# ----------------- TAB 2: STOK BARANG -----------------
with tab2:
    st.header("📊 Cek Stok Barang")

    df_products = get_products()

    if df_products.empty:
        st.info(
            "Belum ada barang tersimpan. Silakan scan atau tambah barang dulu."
        )
    else:
        search_stok = st.text_input(
            "🔎 Cari Nama Barang",
            placeholder="Ketik nama barang yang dicari...",
            key="search_cek_stok",
        )

        if search_stok.strip():
            df_view = df_products[
                df_products["name"].str.lower().str.contains(
                    search_stok.strip().lower(), na=False
                )
            ]
        else:
            df_view = df_products

        st.caption(f"Menampilkan {len(df_view)} dari {len(df_products)} barang")

        if df_view.empty:
            st.warning("Tidak ada barang yang cocok dengan pencarian.")

        reset_page_if_search_changed(search_stok, "_prev_search_stok", "stok_page")
        df_view_halaman, halaman_stok, total_halaman_stok = get_pagination_slice(
            df_view, "stok_page", page_size=15
        )
        render_pagination_controls(
            "stok_page", halaman_stok, total_halaman_stok, len(df_view), "atas"
        )

        # Tampilkan barang halaman ini beserta gambarnya
        for idx, row in df_view_halaman.iterrows():
            with st.container(border=True):
                c_img, c_info = st.columns([1, 3])
                with c_img:
                    resolved_img = resolve_image_path(row.get("image"))
                    if resolved_img:
                        st.image(resolved_img, use_container_width=True)
                    else:
                        st.caption("🖼️ Tidak ada foto")
                with c_info:
                    judul = row["name"]
                    if row.get("merek"):
                        judul += f" — {row['merek']}"
                    st.markdown(f"**{judul}**")
                    st.caption(f"Kategori: {row['category']}")
                    c_a, c_b, c_c = st.columns(3)
                    c_a.metric("Stok", f"{int(row['stock'])} {row['unit']}")
                    c_b.metric("Harga Satuan", f"Rp {row['price']:,.0f}")
                    c_c.metric("Jumlah Harga", f"Rp {row['total_harga']:,.0f}")

        render_pagination_controls(
            "stok_page", halaman_stok, total_halaman_stok, len(df_view), "bawah"
        )

        st.markdown("---")

        # --- Grafik stok, ditaruh di bawah daftar barang ---
        if not df_view.empty:
            st.subheader("Grafik Stok Saat Ini")

            # PENTING: kalau di-set_index pakai "name" doang, barang dengan
            # nama sama tapi merek beda (misal 3x "Map Sneilhekter" dari
            # Buffalo/Carinex/TriJaya) bakal punya index yang sama persis,
            # dan Altair/Vega-Lite nge-stack ketiganya jadi 1 batang gabungan
            # — kelihatannya cuma 1 barang padahal itu jumlah 3 barang
            # ditumpuk, dan tooltip cuma nunjukin salah satu datanya. Supaya
            # tiap barang tetap dapat batang sendiri-sendiri, label grafiknya
            # digabung dengan merek, dan kalau nama+merek masih sama juga,
            # ditambah nomor urut biar tetap unik.
            df_chart = df_view.copy()
            df_chart["chart_label"] = df_chart["name"] + df_chart["merek"].apply(
                lambda m: f" — {m}" if m else ""
            )
            dup_mask = df_chart["chart_label"].duplicated(keep=False)
            if dup_mask.any():
                nomor_urut = df_chart.groupby("chart_label").cumcount() + 1
                df_chart.loc[dup_mask, "chart_label"] = (
                    df_chart.loc[dup_mask, "chart_label"]
                    + " #"
                    + nomor_urut[dup_mask].astype(str)
                )

            chart_data = df_chart.set_index("chart_label")[["stock"]]
            chart_data.columns = ["Jumlah Stok"]
            st.bar_chart(chart_data, color="#22c55e")

            st.markdown("---")

        df_display = df_view[[
            "name", "merek", "category", "unit", "stock", "price", "total_harga",
        ]].copy()
        df_display.columns = [
            "Nama Barang", "Merek", "Kategori", "Satuan", "Stok",
            "Harga Satuan", "Jumlah Harga",
        ]

        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df_display.to_excel(writer, index=False, sheet_name="Stok_Barang")
        excel_data = output.getvalue()

        st.download_button(
            label="📥 Download Laporan Stok (Excel)",
            data=excel_data,
            file_name=f'Laporan_Stok_{datetime.now().strftime("%Y%m%d")}.xlsx',
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )


# ----------------- TAB 3: TAMBAH & EDIT PRODUK -----------------
with tab3:
    st.header("➕ Tambah Barang Baru (Manual)")
    st.caption(
        "Tambah barang tanpa perlu scan barcode. Kalau nanti barang ini "
        "di-scan barcode-nya, tempelkan barcode-nya lewat Tab 🔍 Scan supaya "
        "tidak tercatat double."
    )

    if "manual_add_versi" not in st.session_state:
        st.session_state["manual_add_versi"] = 0

    manual_name_check = st.text_input(
        "Ketik nama barang untuk dicek dulu",
        placeholder="Contoh: Spidol Whiteboard",
        key=f"manual_name_check_{st.session_state['manual_add_versi']}",
    )
    kandidat_manual = find_products_by_name(manual_name_check)
    if kandidat_manual:
        st.warning(
            "⚠️ Sudah ada barang dengan nama mirip. Kalau ini barang yang sama, "
            "edit stoknya saja di daftar bawah, jangan tambah baru:"
        )
        for kandidat in kandidat_manual:
            judul_kandidat = kandidat["name"]
            if kandidat.get("merek"):
                judul_kandidat += f" — Merek: {kandidat['merek']}"
            st.write(
                f"- **{judul_kandidat}** ({kandidat['category']}) — "
                f"Stok: {kandidat['stock']} {kandidat.get('unit', 'Unit')}"
            )

    with st.form(key="form_add_manual_product", clear_on_submit=True):
        col_m1, col_m2 = st.columns(2)

        with col_m1:
            manual_name = st.text_input(
                "Nama Barang *", value=manual_name_check
            )
            manual_merek = st.text_input(
                "Merek (opsional)", placeholder="Contoh: Snowman"
            )
            manual_cat_options = get_existing_categories() + ["➕ Kategori Baru..."]
            manual_selected_cat = st.selectbox(
                "Kategori", manual_cat_options, key="manual_cat_select"
            )
            if manual_selected_cat == "➕ Kategori Baru...":
                manual_custom_cat = st.text_input(
                    "Nama Kategori Baru *", key="manual_custom_cat"
                )
            else:
                manual_custom_cat = None
            manual_image_file = st.file_uploader(
                "🖼️ Foto Produk (opsional)",
                type=["jpg", "jpeg", "png", "webp"],
                key="manual_product_image",
            )

        with col_m2:
            manual_unit = st.selectbox("Satuan", DAFTAR_SATUAN, key="manual_unit")
            manual_stock = st.number_input(
                "Jumlah Stok *", min_value=0, value=1, step=1, key="manual_stock"
            )
            manual_price = st.number_input(
                "Harga Satuan (Rp) *",
                min_value=0.0,
                value=0.0,
                step=500.0,
                key="manual_price",
            )
            st.caption(f"💰 Jumlah Harga: Rp {manual_stock * manual_price:,.0f}")
            manual_expiry = st.date_input(
                "Tanggal Kadaluarsa (opsional)",
                value=None,
                key="manual_expiry",
            )

        manual_submit = st.form_submit_button("💾 Simpan Barang Baru")

        if manual_submit:
            manual_final_cat = (
                manual_custom_cat.strip()
                if manual_selected_cat == "➕ Kategori Baru..." and manual_custom_cat
                else manual_selected_cat
            )
            if not manual_name.strip():
                st.error("Nama Barang tidak boleh kosong!")
            elif manual_selected_cat == "➕ Kategori Baru..." and not (
                manual_custom_cat and manual_custom_cat.strip()
            ):
                st.error("Nama Kategori Baru tidak boleh kosong!")
            else:
                # Barang manual tidak punya barcode fisik, jadi dibuatkan kode
                # internal unik supaya tetap kompatibel dengan sistem penyimpanan.
                internal_code = f"MANUAL-{secrets.token_hex(6)}"
                manual_exp_str = (
                    manual_expiry.strftime("%Y-%m-%d") if manual_expiry else ""
                )
                saved_manual_image = save_uploaded_image(
                    internal_code, manual_image_file
                )
                save_product_to_db(
                    internal_code,
                    manual_name.strip(),
                    manual_merek.strip(),
                    manual_final_cat,
                    manual_unit,
                    int(manual_stock),
                    float(manual_price),
                    manual_exp_str,
                    saved_manual_image,
                )
                st.session_state["manual_add_versi"] += 1
                st.success(f"🎉 **{manual_name}** berhasil disimpan!")
                st.rerun()

    st.markdown("---")
    st.header("📥 Import Banyak Barang dari Excel")
    st.caption(
        "Buat yang tiap bulan input ulang data laporan"
        ", gak perlu diketik satu-satu. Cukup buat file Excel/CSV "
        "dengan kolom: **Nama Barang**, **Merek** (opsional), **Kategori** "
        "(opsional), **Satuan**, **Stok**, **Harga Satuan** — lalu upload di "
        "sini. Urutan kolom bebas, yang penting nama headernya.\n\n"
        "Klik Tombol **'Download Template Excel'** untuk download template nya"
    )

    template_df = pd.DataFrame([
        {
            "Nama Barang": "Contoh: Map Sneilhekter",
            "Merek": "Buffalo",
            "Kategori": "Alat Tulis Kantor",
            "Satuan": "Lembar",
            "Stok": 50,
            "Harga Satuan": 1110,
        }
    ])
    template_output = io.BytesIO()
    with pd.ExcelWriter(template_output, engine="openpyxl") as writer:
        template_df.to_excel(writer, index=False, sheet_name="Template")
    st.download_button(
        "📄 Download Template Excel",
        data=template_output.getvalue(),
        file_name="template_import_barang.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    if "bulk_import_versi" not in st.session_state:
        st.session_state["bulk_import_versi"] = 0

    import_file = st.file_uploader(
        "Upload file Excel / CSV berisi daftar barang",
        type=["xlsx", "xls", "csv"],
        key=f"bulk_import_file_{st.session_state['bulk_import_versi']}",
    )

    if import_file:
        df_import, err_msg = parse_import_file(import_file)
        if err_msg:
            st.error(err_msg)
        elif df_import.empty:
            st.warning("Tidak ada baris barang yang bisa dibaca dari file ini.")
        else:
            st.success(f"✅ {len(df_import)} barang siap diimport, cek dulu di bawah:")
            st.dataframe(df_import, use_container_width=True, hide_index=True)

            nama_double = []
            for nm in df_import["name"]:
                if find_products_by_name(nm):
                    nama_double.append(nm)
            if nama_double:
                st.warning(
                    "⚠️ Beberapa nama barang sudah ada di data sekarang, "
                    "kalau diimport akan jadi entri terpisah (double): "
                    + ", ".join(sorted(set(nama_double)))
                )

            if st.button("💾 Import Semua Barang Ini", type="primary"):
                bulk_save_products(df_import)
                # Naikin versi supaya kotak upload balik kosong lagi
                # (bukan cuma pesan sukses doang) — biar jelas kelihatan
                # kalau prosesnya udah beres, gak keliatan kayak belum
                # ke-submit dan bikin orang mencet dua kali jadi double.
                st.session_state["bulk_import_versi"] += 1
                st.success(f"🎉 {len(df_import)} barang berhasil diimport!")
                st.rerun()

    st.markdown("---")
    st.header("🖼️ Upload Banyak Foto Sekaligus")
    st.caption(
        "Ada banyak foto barang yang mau diupload bareng-bareng? "
        " Upload semuanya di sini sekaligus. "
        "Sistem otomatis nebak foto ini punya barang yang mana berdasarkan "
        "nama file-nya (misal file `Map Sneilhekter.jpg` otomatis kecocok "
        "ke barang 'Map Sneilhekter'). Kalau tebakannya salah/kosong, "
        "tinggal pilih manual di dropdown-nya sebelum disimpan."
    )

    if "bulk_foto_versi" not in st.session_state:
        st.session_state["bulk_foto_versi"] = 0

    bulk_images = st.file_uploader(
        "Upload beberapa foto sekaligus",
        type=["jpg", "jpeg", "png", "webp"],
        accept_multiple_files=True,
        key=f"bulk_image_upload_{st.session_state['bulk_foto_versi']}",
    )

    if "bulk_foto_tersimpan" not in st.session_state:
        st.session_state["bulk_foto_tersimpan"] = set()

    # Pesan hasil simpan sebelumnya (kalau ada) ditampilkan sekali di sini.
    # Khusus buat bagian upload foto ini doang (beda dari mekanisme
    # notifikasi umum yang sempat dicoba lalu dibatalkan), jadi nggak
    # nyenggol bagian lain aplikasi sama sekali.
    pesan_bulk_foto = st.session_state.pop("bulk_foto_pesan", None)
    if pesan_bulk_foto:
        if pesan_bulk_foto.get("konflik_list"):
            st.warning("⚠️ Ada barang dipilih lebih dari 1 foto, ini dilewati dulu:")
            for baris in pesan_bulk_foto["konflik_list"]:
                st.write(baris)
        st.success(pesan_bulk_foto["sukses"])

    if bulk_images:
        all_products = st.session_state["products_data"]
        LEWATI_LABEL = "— Lewati, jangan diupload —"
        product_labels = [LEWATI_LABEL] + [
            f"{p['name']}" + (f" ({p['merek']})" if p.get("merek") else "")
            + f"  ·  {p['category']}"
            for p in all_products
        ]
        label_to_barcode = {
            label: p["barcode"] for label, p in zip(product_labels[1:], all_products)
        }

        def id_nama_ukuran(f):
            # Pengenal berdasarkan NAMA+UKURAN file — dipakai buat nge-cek
            # "foto ini udah kesimpen belum". Sengaja BUKAN berdasarkan
            # posisi/urutan, karena kalau user hapus salah satu file dari
            # kotak upload, urutan file lain ikut geser — kalau
            # pengenalnya ikut posisi, foto yang udah beres bisa keliru
            # dianggap "belum kesimpen" lagi gara-gara geser urutan itu.
            return f"{f.name}_{f.size}"

        # Foto yang udah berhasil kesimpen di percobaan sebelumnya (dari
        # simpan sebagian karena ada konflik) nggak ditampilin lagi di
        # sini — biar yang kelihatan di daftar cuma yang masih perlu
        # dibenerin. (Kotak upload bawaan di atas tetap nampilin semua
        # file apa adanya — itu murni komponen bawaan, di luar kendali
        # kita, jadi dibiarkan saja apa adanya.)
        sudah_tersimpan = st.session_state["bulk_foto_tersimpan"]
        sisa_images = [
            f for f in bulk_images if id_nama_ukuran(f) not in sudah_tersimpan
        ]

        if not sisa_images:
            # Semua foto di batch ini sudah kesimpen, tinggal beres-beres
            st.session_state["bulk_foto_versi"] += 1
            st.session_state["bulk_foto_tersimpan"] = set()
            st.rerun()

        def id_foto(idx, f):
            # Pengenal unik per foto buat KEY WIDGET doang (beda dari
            # id_nama_ukuran di atas) — supaya kalau kebetulan ada 2 foto
            # dengan nama file yang PERSIS SAMA (kejadian nyata: user
            # pilih file yang sama 2x), key widget-nya tetap unik dan
            # nggak bikin aplikasi error/crash. Ini cuma perlu unik
            # SEKALI RENDER, jadi aman dipakai walau posisinya geser
            # antar render.
            return f"{idx}_{f.name}_{f.size}"

        pilihan_final = {}
        konfirmasi_ganti = {}
        for idx, img_file in enumerate(sisa_images):
            fid = id_foto(idx, img_file)
            c_bi1, c_bi2 = st.columns([1, 3])
            with c_bi1:
                st.image(img_file, width=100)
            with c_bi2:
                guessed_barcode = guess_product_for_filename(img_file.name, all_products)
                default_idx = 0
                if guessed_barcode:
                    for i, p in enumerate(all_products):
                        if p["barcode"] == guessed_barcode:
                            default_idx = i + 1
                            break
                elif not guessed_barcode:
                    # Kalau nggak ketebak, kasih tau KENAPA — biar user
                    # nggak bingung, terutama kalau ternyata ada beberapa
                    # barang nama sama tapi merek beda
                    kandidat_nama = cari_kandidat_nama_di_filename(
                        img_file.name, all_products
                    )
                    if len(kandidat_nama) > 1:
                        daftar_merek = ", ".join(
                            sorted(
                                {
                                    (p.get("merek") or "").strip() or "(tanpa merek)"
                                    for p in kandidat_nama
                                }
                            )
                        )
                        st.caption(
                            f"⚠️ Ada {len(kandidat_nama)} barang bernama "
                            f"\"{kandidat_nama[0]['name']}\" — mereknya: "
                            f"{daftar_merek}. Nama file nggak nyebut merek "
                            "yang mana, pilih manual di bawah ini:"
                        )
                chosen_label = st.selectbox(
                    f"Foto **{img_file.name}** ini punya barang:",
                    product_labels,
                    index=default_idx,
                    key=f"bulk_match_{fid}",
                )
                barcode_terpilih = label_to_barcode.get(chosen_label)
                pilihan_final[fid] = (img_file, barcode_terpilih)

                # Kalau barang yang dipilih SUDAH punya foto, jangan
                # langsung timpa diam-diam — minta konfirmasi dulu, di
                # baris yang sama (bukan di ringkasan terpisah).
                konfirmasi_ganti[fid] = True
                if barcode_terpilih:
                    target_produk = next(
                        (p for p in all_products if p["barcode"] == barcode_terpilih),
                        None,
                    )
                    if target_produk and resolve_image_path(target_produk.get("image")):
                        konfirmasi_ganti[fid] = st.checkbox(
                            "⚠️ Barang ini sudah ada foto — centang untuk mengganti",
                            key=f"overwrite_confirm_{fid}",
                        )
            st.markdown("---")

        if st.button("💾 Simpan Semua Foto Sekaligus", type="primary"):
            # Konflik dicek & ditampilin DI SINI SAJA (setelah tombol
            # diklik) — bukan terus-menerus muncul selagi masih milih
            # dropdown, biar nggak berisik pas lagi milih-milih.
            barcode_ke_file = {}
            for fid, (img_file, bc) in pilihan_final.items():
                if bc:
                    barcode_ke_file.setdefault(bc, []).append(img_file.name)
            konflik = {bc: fn for bc, fn in barcode_ke_file.items() if len(fn) > 1}

            jumlah_tersimpan = 0
            jumlah_belum_konfirmasi = 0
            for fid, (img_file, barcode) in pilihan_final.items():
                if not barcode or barcode in konflik:
                    continue
                if not konfirmasi_ganti.get(fid, True):
                    # Belum dicentang konfirmasi ganti foto -> lewati,
                    # tetap tinggal di daftar (warning-nya udah ada di
                    # baris fotonya sendiri, nggak perlu diulang di sini)
                    jumlah_belum_konfirmasi += 1
                    continue
                saved_path = save_uploaded_image(barcode, img_file)
                for p in st.session_state["products_data"]:
                    if p["barcode"] == barcode:
                        p["image"] = saved_path
                        break
                jumlah_tersimpan += 1
                # Tandai foto ini "sudah beres" (pakai nama+ukuran, BUKAN
                # posisi) biar beneran hilang dari daftar pas halaman
                # di-render ulang, dan tetap kena walau posisi file lain
                # di kotak upload geser gara-gara ada yang di-X manual.
                st.session_state["bulk_foto_tersimpan"].add(id_nama_ukuran(img_file))
            save_data_to_supabase(st.session_state["products_data"])

            ada_yang_dilewati = bool(konflik) or jumlah_belum_konfirmasi > 0

            pesan_hasil = {
                "konflik_list": [],
                "sukses": f"🎉 {jumlah_tersimpan} foto berhasil disimpan!",
            }
            for bc, fnames in konflik.items():
                produk = next((p for p in all_products if p["barcode"] == bc), None)
                nama_barang = produk["name"] if produk else bc
                if produk and produk.get("merek"):
                    nama_barang += f" ({produk['merek']})"
                pesan_hasil["konflik_list"].append(f"- **{nama_barang}**: {', '.join(fnames)}")

            if ada_yang_dilewati:
                pesan_hasil["sukses"] = (
                    f"🎉 {jumlah_tersimpan} foto berhasil disimpan. Sisanya di "
                    "bawah ini masih perlu diperbaiki/dikonfirmasi, lalu klik "
                    "Simpan lagi."
                )
            else:
                # Semua bersih -> baru boleh reset (kotak upload jadi
                # kosong lagi, siap buat batch berikutnya)
                st.session_state["bulk_foto_versi"] += 1
                st.session_state["bulk_foto_tersimpan"] = set()

            st.session_state["bulk_foto_pesan"] = pesan_hasil
            st.rerun()

    st.markdown("---")
    st.header("⚙️ Daftar Semua Barang")

    df_products_all = get_products()
    if df_products_all.empty:
        st.info("Belum ada data barang.")
    else:
        search_query = st.text_input(
            "🔎 Cari Barang",
            placeholder="Ketik nama, kategori, atau barcode barang...",
            key="search_kelola_data",
        )

        if search_query.strip():
            q = search_query.strip().lower()
            mask = (
                df_products_all["name"].str.lower().str.contains(q, na=False)
                | df_products_all["category"].str.lower().str.contains(q, na=False)
                | df_products_all["barcode"].astype(str).str.lower().str.contains(q, na=False)
            )
            df_filtered = df_products_all[mask]
        else:
            df_filtered = df_products_all

        st.caption(f"Menampilkan {len(df_filtered)} dari {len(df_products_all)} barang")

        if df_filtered.empty:
            st.warning("Tidak ada barang yang cocok dengan pencarian.")

        reset_page_if_search_changed(search_query, "_prev_search_kelola", "edit_page")
        df_filtered_halaman, halaman_edit, total_halaman_edit = get_pagination_slice(
            df_filtered, "edit_page", page_size=15
        )
        render_pagination_controls(
            "edit_page", halaman_edit, total_halaman_edit, len(df_filtered), "atas"
        )

        for idx, row in df_filtered_halaman.iterrows():
            with st.container(border=True):
                with st.form(key=f"edit_form_{row['barcode']}"):
                    c_img, c_info, c_stok, c_price, c_total, c_btn = st.columns(
                        [1.2, 2, 1.5, 1.5, 2.8, 1.3]
                    )

                    with c_img:
                        existing_image = row.get("image")
                        resolved_edit_img = resolve_image_path(existing_image)
                        if resolved_edit_img:
                            try:
                                st.image(resolved_edit_img, width=80)
                            except Exception:
                                st.caption("⚠️ Foto rusak/tidak terbaca")
                        else:
                            st.caption("🖼️ Belum ada foto")
                        new_edit_image = st.file_uploader(
                            "Ganti Foto",
                            type=["jpg", "jpeg", "png", "webp"],
                            key=f"image_input_{row['barcode']}",
                            label_visibility="collapsed",
                        )
                        hapus_foto = False
                        if resolved_edit_img:
                            hapus_foto = st.checkbox(
                                "🗑️ Hapus foto ini",
                                key=f"hapus_foto_{row['barcode']}",
                            )

                    with c_info:
                        judul_edit = row["name"]
                        if row.get("merek"):
                            judul_edit += f" ({row['merek']})"
                        st.markdown(f"**{judul_edit}**")
                        st.caption(f"{row['category']} · Satuan: {row['unit']}")
                        if not str(row["barcode"]).startswith("MANUAL-"):
                            st.caption(f"Barcode: `{row['barcode']}`")

                    with c_stok:
                        edit_stock = st.number_input(
                            f"Stok ({row['unit']})",
                            min_value=0,
                            value=int(row["stock"]),
                            step=1,
                            key=f"stock_input_{row['barcode']}",
                        )

                    with c_price:
                        edit_price = st.number_input(
                            "Harga Satuan (Rp)",
                            min_value=0.0,
                            value=float(row["price"]),
                            step=500.0,
                            key=f"price_input_{row['barcode']}",
                        )

                    with c_total:
                        st.write("")
                        st.metric("Jumlah Harga", f"Rp {edit_stock * edit_price:,.0f}")

                    with c_btn:
                        st.write("")
                        st.write("")
                        save_btn = st.form_submit_button("💾 Simpan")

                    if save_btn:
                        if hapus_foto and not new_edit_image:
                            # Hapus foto: buang file lamanya dari disk,
                            # kosongin field image di database
                            delete_image_file(existing_image)
                            update_product_in_db(
                                row["barcode"],
                                int(edit_stock),
                                float(edit_price),
                                remove_image=True,
                            )
                        else:
                            updated_image_path = None
                            if new_edit_image:
                                # Ganti foto: hapus dulu foto lama biar
                                # gak numpuk sampah file yang gak
                                # kepakai lagi di folder product_images
                                delete_image_file(existing_image)
                                updated_image_path = save_uploaded_image(
                                    row["barcode"], new_edit_image
                                )
                            update_product_in_db(
                                row["barcode"],
                                int(edit_stock),
                                float(edit_price),
                                updated_image_path,
                            )
                        st.success(
                            f"Perubahan pada **{row['name']}** berhasil disimpan!"
                        )
                        st.rerun()

                with st.popover("✏️ Edit Detail (Nama / Merek / Kategori / Satuan)"):
                    with st.form(key=f"edit_detail_form_{row['barcode']}"):
                        detail_name = st.text_input(
                            "Nama Barang",
                            value=row["name"],
                            key=f"detail_name_{row['barcode']}",
                        )
                        detail_merek = st.text_input(
                            "Merek (opsional)",
                            value=row.get("merek", ""),
                            key=f"detail_merek_{row['barcode']}",
                        )

                        detail_cat_options = get_existing_categories() + ["➕ Kategori Baru..."]
                        current_cat = row["category"]
                        cat_index = (
                            detail_cat_options.index(current_cat)
                            if current_cat in detail_cat_options
                            else len(detail_cat_options) - 1
                        )
                        detail_cat_select = st.selectbox(
                            "Kategori",
                            detail_cat_options,
                            index=cat_index,
                            key=f"detail_cat_{row['barcode']}",
                        )
                        if detail_cat_select == "➕ Kategori Baru...":
                            detail_cat_custom = st.text_input(
                                "Nama Kategori Baru *",
                                value=current_cat if current_cat not in detail_cat_options else "",
                                key=f"detail_cat_custom_{row['barcode']}",
                            )
                        else:
                            detail_cat_custom = None

                        unit_index = (
                            DAFTAR_SATUAN.index(row["unit"])
                            if row["unit"] in DAFTAR_SATUAN
                            else 0
                        )
                        detail_unit = st.selectbox(
                            "Satuan",
                            DAFTAR_SATUAN,
                            index=unit_index,
                            key=f"detail_unit_{row['barcode']}",
                        )

                        detail_save = st.form_submit_button("💾 Simpan Detail")

                        if detail_save:
                            final_detail_cat = (
                                detail_cat_custom.strip()
                                if detail_cat_select == "➕ Kategori Baru..."
                                and detail_cat_custom
                                else detail_cat_select
                            )
                            if not detail_name.strip():
                                st.error("Nama Barang tidak boleh kosong!")
                            elif detail_cat_select == "➕ Kategori Baru..." and not (
                                detail_cat_custom and detail_cat_custom.strip()
                            ):
                                st.error("Nama Kategori Baru tidak boleh kosong!")
                            else:
                                update_product_details_in_db(
                                    row["barcode"],
                                    detail_name.strip(),
                                    detail_merek.strip(),
                                    final_detail_cat,
                                    detail_unit,
                                )
                                st.success("Detail barang berhasil diperbarui!")
                                st.rerun()


        render_pagination_controls(
            "edit_page", halaman_edit, total_halaman_edit, len(df_filtered), "bawah"
        )

        st.markdown("---")

        # --- FITUR HAPUS PRODUK ---
        st.subheader("🗑️ Opsi Penghapusan Data Barang")

        delete_option = st.radio(
            "Pilih Metode Penghapusan:",
            ["Pilih Barang Tertentu", "Hapus Semua Barang"],
            horizontal=True,
        )

        if delete_option == "Pilih Barang Tertentu":
            product_options = {
                f"{p['name']} (Barcode: {p['barcode']})": p["barcode"]
                for p in st.session_state["products_data"]
            }

            selected_items = st.multiselect(
                "Pilih satu atau beberapa barang yang ingin dihapus:",
                options=list(product_options.keys()),
                placeholder="Pilih barang...",
            )

            if selected_items:
                barcodes_to_del = [
                    product_options[item] for item in selected_items
                ]
                with st.popover("🗑️ Hapus Barang Terpilih"):
                    st.warning(
                        f"Apakah Anda yakin ingin menghapus {len(barcodes_to_del)} barang yang dipilih?"
                    )
                    if st.button("Ya, Hapus Barang Terpilih", type="primary"):
                        delete_products_from_db(barcodes_to_del)
                        st.success("Barang terpilih berhasil dihapus.")
                        st.rerun()

        elif delete_option == "Hapus Semua Barang":
            with st.popover("🗑️ Hapus Semua Data Barang"):
                st.warning(
                    "Apakah Anda yakin ingin menghapus SELURUH data barang?"
                )
                if st.button("Ya, Hapus Semua Data", type="primary"):
                    clear_all_db_data()
                    st.success("Seluruh data barang berhasil dikosongkan.")
                    st.rerun()


# ----------------- TAB 4: KELOLA AKUN -----------------
with tab4:
    render_register_form()