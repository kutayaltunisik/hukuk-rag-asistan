"""Cevap sonrası temizleme ve tekrar dedektörünün birim testleri.

Girdiler UYDURMA DEĞİL: hepsi qwen3-4b'nin bu projede ürettiği gerçek
çıktılardan alındı. Bu yüzden test, gelecekte prompt veya model değişirse
temizlemenin hâlâ doğru davrandığını ölçen bir regresyon ağı işlevi görüyor.

Çalıştırmak için:
    ./.venv/bin/python -m tests.test_cevap_temizleme
"""

from __future__ import annotations

from src.answer import Cevap, _atif_guvenlik_agi, _degerlendir, cevabi_duzelt
from src.db import ParcaKaydi
from src.providers import tekrar_dongusu

# Model, cevabın önüne bağlam bloğunu taklit ederek koyuyor ve gövdeyi
# başka sözcüklerle yazıyor; ikinci blokta var olmayan bir hüküm uyduruyor.
TAKLIT = """[TBK m. 584] III. Eşin rızası
Eşlerden biri, bir ayrılık kararı olmadıkça, diğerinin yazılı rızasıyla kefil olabilir.

[TBK m. 585] IV. Eşin rızası
Eşlerden biri, bir ayrılık kararı olmadıkça, diğerinin yazılı rızasıyla kefil olabilir.

**Cevap:**
Evet, evli bir kişinin kefil olması için eşinin yazılı rızası gerekir [TBK m. 584]."""

SADE = "Genel zamanaşımı süresi on yıldır [TBK m. 146]."

# İstenen iki alanlı biçim: DAYANAK önce üretilir, sunumda sona taşınır.
IKI_ALAN = """DAYANAK: [TBK m. 344]
CEVAP: Kira artışı, bir önceki kira yılının tüketici fiyat endeksindeki oniki
aylık ortalamalara göre değişim oranını geçemez."""

# Üretim kesilmiş: cevap yarım kalmış ama dayanak önce yazıldığı için elimizde.
KESILMIS = """DAYANAK: [TBK m. 316]
CEVAP: Kiracı, kiralananı sözleşmeye uygun olarak özenle kullanmak ve"""

# Cevabın tamamı taklit bloğu: kırpınca hiçbir şey kalmıyor.
TAMAMI_TAKLIT = """[TBK m. 146] I. On yıllık zamanaşımı
Kanunda aksine bir hüküm bulunmadıkça, her alacak on yıllık zamanaşımına tabidir."""


def _esitle(ad: str, alinan, beklenen) -> bool:
    tamam = alinan == beklenen
    print(f"{'+' if tamam else '-'} {ad}")
    if not tamam:
        print(f"    beklenen: {beklenen!r}")
        print(f"    alınan  : {alinan!r}")
    return tamam


def main() -> int:
    sonuclar = [
        _esitle(
            "taklit blok ve süsleme başlığı atılır",
            cevabi_duzelt(TAKLIT),
            "Evet, evli bir kişinin kefil olması için eşinin yazılı rızası "
            "gerekir [TBK m. 584].",
        ),
        _esitle("sade cevap değişmeden geçer", cevabi_duzelt(SADE), SADE),
        _esitle(
            "iki alanlı biçimde dayanak sona taşınır",
            cevabi_duzelt(IKI_ALAN),
            "Kira artışı, bir önceki kira yılının tüketici fiyat endeksindeki "
            "oniki\naylık ortalamalara göre değişim oranını geçemez."
            "\n\nDAYANAK: [TBK m. 344]",
        ),
        _esitle(
            "cevap kesilse de dayanak korunur",
            cevabi_duzelt(KESILMIS),
            "Kiracı, kiralananı sözleşmeye uygun olarak özenle kullanmak ve"
            "\n\nDAYANAK: [TBK m. 316]",
        ),
        _esitle(
            "cevabın tamamı taklitse metin korunur",
            cevabi_duzelt(TAMAMI_TAKLIT),
            TAMAMI_TAKLIT,
        ),
        _esitle(
            "cümle içindeki atıf satır başı olsa da silinmez",
            cevabi_duzelt("[TBK m. 27] uyarınca sözleşme kesin hükümsüzdür."),
            "[TBK m. 27] uyarınca sözleşme kesin hükümsüzdür.",
        ),
        _esitle(
            "tekrar döngüsü yakalanır",
            tekrar_dongusu("bir borç muaccel olmasından önce veya sonra, " * 6),
            True,
        ),
        _esitle(
            "normal cevapta tekrar bulunmaz",
            tekrar_dongusu(TAKLIT),
            False,
        ),
        _esitle(
            "atıfsız alakalı cevaba güvenlik ağı dayanak ekler",
            "TBK m. 146" in _guvenlik_agi_atifi(),
            True,
        ),
        _esitle(
            "kapsam dışı cevapta güvenlik ağı sessiz kalır",
            _guvenlik_agi_kapsam_disi(),
            [],
        ),
    ]


def _ornek_parca(madde: str = "146") -> ParcaKaydi:
    return ParcaKaydi(
        id=1,
        atif=f"TBK m. {madde}",
        madde_no=madde,
        kenar_baslik="On yıllık zamanaşımı",
        konu_yolu="Genel Hükümler",
        bolum="",
        ayirim="",
        icerik="Kanunda aksine bir hüküm bulunmadıkça, her alacak on yıllık zamanaşımına tabidir.",
        kanun_adi="Türk Borçlar Kanunu",
        kisaltma="TBK",
        kanun_no="6098",
        benzerlik=0.7,
        kaynaklar=("vektor",),
    )


def _guvenlik_agi_atifi() -> str:
    c = Cevap(
        soru="Genel zamanaşımı süresi kaç yıldır?",
        metin="Genel zamanaşımı süresi on yıldır.",
        parcalar=[_ornek_parca()],
        baglam_parcalari=[_ornek_parca()],
    )
    _degerlendir(c)
    _atif_guvenlik_agi(c)
    return " ".join(c.atiflar)


def _guvenlik_agi_kapsam_disi() -> list:
    c = Cevap(
        soru="Hırsızlık cezası nedir?",
        metin="BAĞLAMDA YOK. Bu soru Türk Ceza Kanunu'na girer.",
        parcalar=[_ornek_parca()],
        baglam_parcalari=[_ornek_parca()],
        kapsam_disi=True,
    )
    _degerlendir(c)
    _atif_guvenlik_agi(c)
    return c.atiflar
    basarisiz = sonuclar.count(False)
    print(f"\n{len(sonuclar) - basarisiz}/{len(sonuclar)} test geçti")
    return 1 if basarisiz else 0


if __name__ == "__main__":
    raise SystemExit(main())
