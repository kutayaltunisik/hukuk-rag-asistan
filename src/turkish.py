"""Türkçe metin normalleştirme.

Neden gerekli: SQLite FTS5'in `unicode61` tokenizer'ı Türkçe'yi bilmez.
"İ" harfini küçültürken ASCII kuralı uygular, "ı/i" ayrımını ve
"ş/ç/ğ/ö/ü" harflerini beklediğimiz gibi eşlemez. Bu yüzden metni hem
indekslerken hem sorgularken AYNI fonksiyondan geçiriyoruz: böylece
"ZAMANAŞIMI", "zamanaşımı" ve "zamanasimi" aynı token'a düşer ve
kullanıcı Türkçe karakter yazmasa da sonuç bulur.
"""

from __future__ import annotations

import re
import unicodedata

# Türkçeye özgü harf eşlemesi. Küçültmeden ÖNCE uygulanır, çünkü
# str.lower() "I" -> "i" yaparak "ı" bilgisini kaybettirir.
_TR_MAP = str.maketrans(
    {
        "İ": "i", "I": "i", "ı": "i",
        "Ş": "s", "ş": "s",
        "Ç": "c", "ç": "c",
        "Ğ": "g", "ğ": "g",
        "Ö": "o", "ö": "o",
        "Ü": "u", "ü": "u",
        "Â": "a", "â": "a",
        "Î": "i", "î": "i",
        "Û": "u", "û": "u",
        "Ê": "e", "ê": "e",
        "'": " ", "'": " ", "’": " ", "‘": " ",
        "–": " ", "—": " ", "−": " ",
    }
)


def normalize(text: str) -> str:
    """Arama/karşılaştırma için metni sadeleştirir (ASCII, küçük harf)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(_TR_MAP).lower()
    # Kalan aksanları ayrıştırıp at (ör. kopyala-yapıştır kaynaklı harfler)
    text = "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if not unicodedata.combining(ch)
    )
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def clean_whitespace(text: str) -> str:
    """Görünmez/ikizlenmiş boşlukları tek boşluğa indirir, metni bozmaz."""
    text = text.replace("\xa0", " ").replace("\u200b", "")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# --------------------------------------------------------------------------
# Madde numarası çıkarımı
# --------------------------------------------------------------------------
# Kullanıcılar madde numarasını çok farklı yazar. Hepsini yakalamak
# retrieval kalitesi için kritik: "TBK 27" diyen biriyle
# "Türk Borçlar Kanunu'nun 27. maddesi" diyen biri aynı sonucu almalı.
_MADDE_PATTERNS = (
    re.compile(r"(?i)\bmadde\s*(\d+)\s*(?:/\s*([a-zçğıöşü]))?"),
    re.compile(r"(?i)\bm\s*\.?\s*(\d+)\s*(?:/\s*([a-zçğıöşü]))?"),
    re.compile(r"(?i)\b(\d+)\s*\.?\s*madde"),
    re.compile(r"(?i)\b(?:tbk|tck|tmk|hmk|cmk|kvkk|ttk)\s*[.:]?\s*(\d+)"),
)


def extract_madde_numbers(query: str) -> list[str]:
    """Sorgu metnindeki madde numaralarını sırayla döndürür ('27', '2/a')."""
    found: list[str] = []
    for pat in _MADDE_PATTERNS:
        for m in pat.finditer(query):
            num = m.group(1)
            suffix = m.lastindex and m.lastindex >= 2 and m.group(2) or None
            key = f"{num}/{suffix.lower()}" if suffix else num
            if key not in found:
                found.append(key)
    return found
