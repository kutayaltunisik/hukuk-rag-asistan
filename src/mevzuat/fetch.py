"""mevzuat.gov.tr'den kanun metnini indirir ve yerel diske önbelleğe alır.

Bu, projedeki İNTERNET GEREKTİREN TEK adımdır. Bir kez çalıştırıldıktan sonra
metinler `data/raw/` altında durur ve sistemin tamamı çevrimdışı çalışır.

Neden PDF veya DOC değil de HTML: aynı kanunun `.htm` sürümü Word'ün "filtered
HTML" çıktısıdır ve paragraf sınırlarını `<p>` etiketiyle korur. PDF'te bu bilgi
kaybolur, sayfa başlıkları/numaraları metne karışır; `.doc` ise eski ikili biçim
olduğu için ek araç gerektirir.
"""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from src import config

# mevzuat.gov.tr betik istemcilerini reddedebiliyor; normal tarayıcı başlığı veriyoruz.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "tr-TR,tr;q=0.9",
}


def kanun_url(kanun: dict) -> str:
    return config.MEVZUAT_URL_TEMPLATE.format(
        tur=kanun["tur"], tertip=kanun["tertip"], no=kanun["kanun_no"]
    )


def indir(kanun: dict, force: bool = False) -> tuple[Path, str]:
    """Kanunu indirir (veya önbellekten okur). (dosya_yolu, url) döndürür."""
    url = kanun_url(kanun)
    hedef = config.DATA_RAW / f"{kanun['slug']}.htm"

    if hedef.exists() and not force:
        return hedef, url

    req = urllib.request.Request(url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            veri = resp.read()
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"{kanun['kanun_adi']} indirilemedi ({url}). İnternet bağlantısını "
            f"kontrol et. Hata: {exc}"
        ) from exc

    # Site hata durumunda 200 ile küçük bir HTML hata sayfası döndürüyor;
    # bunu sessizce korpusa almamak için boyut ve içerik kontrolü yapıyoruz.
    if len(veri) < 50_000 or b"MADDE" not in veri.upper():
        raise RuntimeError(
            f"{url} beklenen kanun metnini döndürmedi ({len(veri)} bayt). "
            "Adres kalıbı (tur/tertip/no) değişmiş olabilir."
        )

    hedef.write_bytes(veri)
    return hedef, url


def tum_kanunlari_indir(force: bool = False) -> list[tuple[dict, Path, str]]:
    sonuc = []
    for kanun in config.KANUNLAR:
        yol, url = indir(kanun, force=force)
        sonuc.append((kanun, yol, url))
    return sonuc
