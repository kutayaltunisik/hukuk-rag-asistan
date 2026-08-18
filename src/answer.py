"""Soru → arama → prompt → yerel model → atıf kontrolü."""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Iterator

from src import config, db, prompts, retrieve

# Cevap içindeki atıfları yakalar: [TBK m. 27], [TBK m. GEÇİCİ 1], [TBK m. 2/A]
RE_CEVAP_ATFI = re.compile(
    r"\[\s*([A-ZÇĞİÖŞÜ]{2,6})\s*m\.?\s*((?:GEÇİCİ|EK)?\s*\d+(?:/[A-Za-zÇĞİÖŞÜçğıöşü])?)\s*[^\]]*\]",
    re.UNICODE,
)


@dataclass
class Cevap:
    soru: str
    metin: str
    parcalar: list[db.ParcaKaydi] = field(default_factory=list)   # aramanın döndürdüğü
    baglam_parcalari: list[db.ParcaKaydi] = field(default_factory=list)  # modele gideni
    atiflar: list[str] = field(default_factory=list)          # cevapta geçen atıflar
    dayanaksiz_atiflar: list[str] = field(default_factory=list)  # bağlamda olmayanlar
    bilmiyorum: bool = False
    kapsam_disi: bool = False          # arama alaka eşiğinin altında kaldı
    en_yuksek_benzerlik: float = 0.0
    arama_ms: float = 0.0
    uretim_ms: float = 0.0
    toplam_ms: float = 0.0
    chat_modeli: str = ""
    gomme_modeli: str = ""
    arama_ozeti: str = ""
    otomatik_atif: bool = False  # atıf modelden gelmedi, güvenlik ağı ekledi

    @property
    def guvenilir(self) -> bool:
        """Cevap, bağlamda gerçekten bulunan maddelere mi dayanıyor?

        Kapsam dışı bir soruda güvenilir olmanın koşulu cevap vermek DEĞİL,
        cevap vermediğini söylemektir.
        """
        if self.dayanaksiz_atiflar:
            return False
        if self.kapsam_disi:
            return self.bilmiyorum
        return self.bilmiyorum or bool(self.atiflar)

    def kaynak_listesi(self) -> list[str]:
        return [f"{p.atif} — {p.kenar_baslik}".strip(" —") for p in self.parcalar]


def _atiflari_ayikla(metin: str) -> list[str]:
    bulunan: list[str] = []
    for kisaltma, no in RE_CEVAP_ATFI.findall(metin):
        atif = f"{kisaltma.upper()} m. {re.sub(r'\s+', ' ', no).strip().upper()}"
        if atif not in bulunan:
            bulunan.append(atif)
    return bulunan


# Bağlam bloğunun başlık satırı: "[TBK m. 584] III. Eşin rızası", "[TBK m. 146]"
#
# Kenar başlıklar TBK'da neredeyse istisnasız numaralıdır ("I.", "2.", "A)");
# desen bu numarayı ZORUNLU tutar. Böylece "[TBK m. 27] uyarınca sözleşme
# hükümsüzdür" gibi GERÇEK bir cevap cümlesi yanlışlıkla başlık sanılmaz.
# Satır sonunda cümle noktalaması bulunmaması da aranır: cümleler noktayla
# biter, başlıklar bitmez.
RE_BAGLAM_BASLIGI = re.compile(
    r"^\[[^\]]{3,60}\]"
    r"(?:\s*(?:[IVXLC]+|\d+|[A-ZÇĞİÖŞÜ])\s*[.)]\s*[^\n]{0,60})?"
    r"\s*$"
)
# İstenen çıktı biçiminin etiketleri (bkz. prompts.SISTEM_PROMPTU).
RE_CEVAP_ETIKETI = re.compile(r"^\s*\**\s*cevap\s*\**\s*:\s*\**\s*", re.IGNORECASE | re.MULTILINE)
RE_DAYANAK_SATIRI = re.compile(
    r"^[ \t]*\**[ \t]*dayanak[ \t]*\**[ \t]*:[ \t]*\**(.*)$\n?",
    re.IGNORECASE | re.MULTILINE,
)
# Modelin kendiliğinden eklediği süsleme başlıkları: "**Özetle:**", "Sonuç:"
RE_SUS_BASLIGI = re.compile(
    r"^\s*(\*\*)?\s*(özet|özetle|sonuç|yanıt)\s*(\*\*)?\s*:\s*(\*\*)?\s*",
    re.IGNORECASE,
)


def cevabi_duzelt(metin: str) -> str:
    """Cevabın başındaki kopyalanmış bağlam bloğunu ve süsleme başlığını atar."""
    dayanak = ""
    if m := RE_DAYANAK_SATIRI.search(metin):
        dayanak = m.group(1).strip()
        metin = metin[: m.start()] + metin[m.end():]
    if etiket := RE_CEVAP_ETIKETI.search(metin):
        metin = metin[etiket.end():]
    satirlar = metin.split("\n")
    i = 0
    blok_icinde = False
    while i < len(satirlar):
        s = satirlar[i].strip()
        if not s:
            blok_icinde = False
            i += 1
            continue
        if RE_BAGLAM_BASLIGI.match(s) and not s.endswith((".", "!", "?")):
            blok_icinde = True
            i += 1
            continue
        if blok_icinde:
            i += 1
            continue
        break
    kalan = "\n".join(satirlar[i:]).strip() or metin.strip()
    kalan = RE_SUS_BASLIGI.sub("", kalan, count=1).strip()
    if dayanak and dayanak not in kalan:
        kalan = f"{kalan}\n\nDAYANAK: {dayanak}".strip()
    return kalan


def _atif_kumesi(parcalar: list[db.ParcaKaydi]) -> set[str]:
    """Bağlamdaki maddelerin atıf kümesi (parça eki olmadan)."""
    return {
        f"{p.kisaltma.upper()} m. {p.madde_no.upper()}"
        for p in parcalar
    }


def _degerlendir(cevap: Cevap) -> None:
    """Atıfları ayıklar ve modele GERÇEKTEN GÖSTERİLEN maddelerle karşılaştırır.

    Karşılaştırma tabanı `baglam_parcalari`dir, `parcalar` değil: model yalnızca
    kendisine gönderilen maddeleri görmüştür. Arama tarafından bulunup bağlam
    süzgeciyle çıkarılmış bir maddeye atıf yapılması, modelin o hükmü
    ezberinden ürettiği anlamına gelir ve halüsinasyon sayılmalıdır.
    """
    cevap.atiflar = _atiflari_ayikla(cevap.metin)
    gosterilen = cevap.baglam_parcalari or cevap.parcalar
    baglam = _atif_kumesi(gosterilen)
    cevap.dayanaksiz_atiflar = [a for a in cevap.atiflar if a not in baglam]
    cevap.bilmiyorum = prompts.BILMIYORUM_ISARETI.lower() in cevap.metin.lower()


def _atif_guvenlik_agi(cevap: Cevap) -> None:
    """Atıfsız ama alakalı bir cevabın başına bağlamdaki ilk maddeyi ekler.

    Ölçümde (34 soru) içerik olarak doğru, atıfsız 7 cevap görüldü: model
    "on yıldır" dedi ama [TBK m. 146] yazmadı. Küçük modeller talimatı
    dinlemiyor; atıfı programatik eklemek biçim kuralını bozmadan içeriği
    korur. Yalnızca modele GERÇEKTEN gösterilen ilk madde kullanılır —
    uydurma atıf üretmez. Kapsam dışı / "bilmiyorum" cevaplarına dokunulmaz.
    """
    if cevap.kapsam_disi or cevap.bilmiyorum or cevap.atiflar:
        return
    kaynak = cevap.baglam_parcalari or cevap.parcalar
    if not kaynak:
        return
    etiketler = [f"[{p.atif}]" for p in kaynak]
    if any(e in cevap.metin for e in etiketler):
        return
    # Tek maddeye kilitlenmek yanlış atıf üretir: "on yıldır" hem m. 146
    # (genel süre) hem m. 156 (kesilmeden sonraki süre) içinde geçer.
    # Modele gösterilen maddelerin hepsi gerçek kaynaktır.
    cevap.metin = f"{cevap.metin.rstrip()}\n\nDAYANAK: {', '.join(etiketler)}"
    cevap.otomatik_atif = True
    cevap.atiflar = _atiflari_ayikla(cevap.metin)
    gosterilen = cevap.baglam_parcalari or cevap.parcalar
    baglam = _atif_kumesi(gosterilen)
    cevap.dayanaksiz_atiflar = [a for a in cevap.atiflar if a not in baglam]


class HukukAsistani:
    """Uygulamanın giriş kapısı. CLI ve Streamlit aynı sınıfı kullanır."""

    def __init__(self, conn: sqlite3.Connection | None = None, saglayici=None) -> None:
        from src import providers

        self.conn = conn or db.baglan()
        self.saglayici = saglayici or providers.saglayici_al()
        self.retriever = retrieve.Retriever(self.conn, self.saglayici)

    # ------------------------------------------------------------------ arama
    def ara(self, soru: str, top_k: int = config.TOP_K) -> retrieve.AramaSonucu:
        return self.retriever.ara(soru, top_k=top_k)

    # ------------------------------------------------------------------ cevap
    def cevapla(self, soru: str, top_k: int = config.TOP_K) -> Cevap:
        t0 = time.perf_counter()
        arama = self.ara(soru, top_k=top_k)
        t1 = time.perf_counter()

        baglam = arama.baglam_parcalari()
        mesajlar = prompts.mesajlari_kur(soru, baglam, alakali=arama.alakali)
        metin = cevabi_duzelt(self.saglayici.chat(mesajlar))
        t2 = time.perf_counter()

        cevap = Cevap(
            soru=soru,
            metin=metin,
            parcalar=arama.parcalar,
            baglam_parcalari=baglam,
            kapsam_disi=not arama.alakali,
            en_yuksek_benzerlik=arama.en_yuksek_benzerlik,
            arama_ms=(t1 - t0) * 1000,
            uretim_ms=(t2 - t1) * 1000,
            toplam_ms=(t2 - t0) * 1000,
            chat_modeli=getattr(self.saglayici, "chat_model", ""),
            gomme_modeli=getattr(self.saglayici, "embed_model", ""),
            arama_ozeti=(
                f"vektör {arama.vektor_adet}, kelime {arama.fts_adet}, "
                f"madde-no {arama.madde_adet}"
            ),
        )
        _degerlendir(cevap)
        _atif_guvenlik_agi(cevap)
        return cevap

    def cevabi_sonlandir(
        self,
        soru: str,
        arama: retrieve.AramaSonucu,
        ham_metin: str,
        arama_ms: float = 0.0,
        uretim_ms: float = 0.0,
    ) -> Cevap:
        """Akışlı üretim bittikten sonra temizleme + atıf doğrulaması.

        CLI ve Streamlit ham token'ları ekrana basar; kullanıcının gördüğü
        resmî cevap bu fonksiyondan geçer (`cevapla()` ile aynı son işlem).
        """
        baglam = arama.baglam_parcalari()
        metin = cevabi_duzelt(self.saglayici._temizle(ham_metin))
        cevap = Cevap(
            soru=soru,
            metin=metin,
            parcalar=arama.parcalar,
            baglam_parcalari=baglam,
            kapsam_disi=not arama.alakali,
            en_yuksek_benzerlik=arama.en_yuksek_benzerlik,
            arama_ms=arama_ms,
            uretim_ms=uretim_ms,
            toplam_ms=arama_ms + uretim_ms,
            chat_modeli=getattr(self.saglayici, "chat_model", ""),
            gomme_modeli=getattr(self.saglayici, "embed_model", ""),
            arama_ozeti=(
                f"vektör {arama.vektor_adet}, kelime {arama.fts_adet}, "
                f"madde-no {arama.madde_adet}"
            ),
        )
        _degerlendir(cevap)
        _atif_guvenlik_agi(cevap)
        return cevap

    # -------------------------------------------------------- akışlı cevap
    def cevapla_akisli(
        self, soru: str, top_k: int = config.TOP_K
    ) -> tuple[retrieve.AramaSonucu, Iterator[str]]:
        """Arama sonucunu hemen, cevabı parça parça döndürür.

        Streamlit için: kullanıcı kaynak maddeleri model yazmayı bitirmeden
        görebiliyor, böylece bekleme algısı belirgin biçimde azalıyor.
        """
        arama = self.ara(soru, top_k=top_k)
        mesajlar = prompts.mesajlari_kur(
            soru, arama.baglam_parcalari(), alakali=arama.alakali
        )
        return arama, self.saglayici.chat_streaming(mesajlar)
