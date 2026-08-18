"""Günlük dil ile kanun dili arasında küçük bir sözlük.

Yalnızca kelime aramasına eklenir; gömme sorgusuna dokunulmaz.
Örnek: depozito → güvence, ihtiyaç → gereksinim.
"""

from __future__ import annotations

from src.turkish import normalize

# Anahtar: kullanıcının yazması muhtemel terim (normalleştirilmiş).
# Değer: kanun metninde geçen karşılık(lar).
HUKUK_ESANLAMLAR: dict[str, tuple[str, ...]] = {
    # --- kira hukuku ---
    "ihtiyac": ("gereksinim",),
    "ihtiyaci": ("gereksinim",),
    "depozito": ("guvence",),
    "teminat": ("guvence",),
    "tahliye": ("sona erdirme", "geri verme"),
    "cikarmak": ("sona erdirme", "fesih"),
    "cikarabilir": ("sona erdirme", "fesih"),
    "cikarma": ("sona erdirme", "fesih"),
    "zam": ("kira bedeli", "artis"),
    "zammi": ("kira bedeli", "artis"),
    "kontrat": ("sozlesme",),
    "emlakci": ("kiraya veren",),
    "evsahibi": ("kiraya veren",),
    # --- borçlar hukuku genel ---
    "gabin": ("asiri yararlanma",),
    "cezai": ("ceza kosulu",),
    "cezai sart": ("ceza kosulu",),
    "iptal": ("gecersiz", "hukumsuz"),
    "gecersizlik": ("hukumsuzluk",),
    "butlan": ("kesin hukumsuzluk",),
    "fesih": ("sona erdirme",),
    "vade": ("muaccel",),
    "vadesi": ("muaccel",),
    "gecikme faizi": ("temerrut faizi",),
    "borcun ifasi": ("ifa",),
    "odenmemesi": ("temerrut",),
    "odememe": ("temerrut",),
    # --- haksız fiil / sorumluluk ---
    "kaza": ("haksiz fiil", "zarar"),
    "isveren sorumlulugu": ("adam calistiran",),
    "patron": ("adam calistiran", "isveren"),
    "bina": ("yapi eseri",),
    "binadan": ("yapi eseri",),
    "hayvan": ("hayvan bulunduran",),
    # --- sözleşme türleri ---
    "kefil": ("kefalet",),
    "vekalet": ("vekil",),
    "avukatlik": ("vekalet",),
    "muteahhit": ("yuklenici",),
    "insaat": ("eser sozlesmesi", "yuklenici"),
    "tamir": ("eser sozlesmesi",),
    "isci": ("hizmet sozlesmesi", "iscinin"),
    "maas": ("ucret",),
    "issten cikarma": ("fesih",),
    # --- zamanaşımı ---
    "zaman asimi": ("zamanasimi",),
    "hak dusurucu": ("zamanasimi",),
}


def esanlamlari_ekle(sorgu: str) -> list[str]:
    """Sorgudan türeyen ek arama terimlerini döndürür.

    Hem tek kelimeler hem iki kelimelik kalıplar ("gecikme faizi") denenir;
    hukuk terimleri sık sık iki kelimeden oluşuyor.
    """
    norm = normalize(sorgu)
    kelimeler = norm.split()
    ekler: list[str] = []

    aranacaklar = list(kelimeler)
    aranacaklar += [
        f"{a} {b}" for a, b in zip(kelimeler, kelimeler[1:])
    ]

    for parca in aranacaklar:
        for karsilik in HUKUK_ESANLAMLAR.get(parca, ()):
            for kelime in karsilik.split():
                if kelime not in kelimeler and kelime not in ekler:
                    ekler.append(kelime)
    return ekler
