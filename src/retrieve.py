"""Hibrit arama: vektör + FTS5 + madde numarası, RRF ile birleştirilir."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

import numpy as np

from src import config, db
from src.esanlam import esanlamlari_ekle
from src.turkish import extract_madde_numbers, normalize

# Türkçe durak kelimeler. FTS sorgusundan atılır: "nedir", "nasıl" gibi
# kelimeler her maddede geçmediği hâlde skoru bulandırır ve gereksiz aday getirir.
DURAK_KELIMELER = {
    "ve", "veya", "ile", "ama", "fakat", "ancak", "ise", "de", "da", "ki",
    "bir", "bu", "su", "o", "the", "icin", "gibi", "kadar", "gore", "daha",
    "cok", "az", "her", "hic", "ne", "nedir", "nasil", "neden", "nicin",
    "kim", "kimdir", "hangi", "hangisi", "mi", "mu", "mi", "midir", "var",
    "yok", "olur", "olmaz", "mi", "olarak", "sonra", "once", "en", "tum",
    "butun", "bazi", "ben", "sen", "biz", "siz", "onlar", "beni", "bana",
    "yapmak", "etmek", "olmak", "diye", "eger", "ya", "hem", "ayni",
}


@dataclass
class AramaSonucu:
    parcalar: list[db.ParcaKaydi]
    vektor_adet: int = 0
    fts_adet: int = 0
    madde_adet: int = 0
    fts_sorgusu: str = ""
    bulunan_madde_numaralari: tuple[str, ...] = ()
    en_yuksek_benzerlik: float = 0.0

    def baglam_parcalari(
        self,
        oran_esigi: float | None = None,
        en_az: int | None = None,
    ) -> list[db.ParcaKaydi]:
        """Modele GÖNDERİLECEK maddeler (arayüzde gösterilenlerin alt kümesi).

        Tepe benzerliğin belirli bir oranının altında kalan zayıf eşleşmeler
        çıkarılır; küçük modellerde bu dolgu maddeleri yanlış cevaba yol
        açıyor (bkz. config.BAGLAM_ORAN_ESIGI). Madde numarasıyla açıkça
        istenen maddeler her hâlükârda korunur.
        """
        oran = config.BAGLAM_ORAN_ESIGI if oran_esigi is None else oran_esigi
        alt_sinir = config.BAGLAM_MIN_MADDE if en_az is None else en_az
        if not self.parcalar or self.en_yuksek_benzerlik <= 0:
            return self.parcalar

        # Kullanıcı madde numarasını açıkça yazdıysa ("TBK 344 ne diyor?")
        # bağlam yalnızca o maddedir. Numara sorularında anlamsal benzerlik
        # yanıltıcıdır: sorguda ayırt edici kelime olmadığı için konuyla
        # alakasız maddeler istenen maddeden yüksek benzerlik alabiliyor
        # (ölçümde m. 344 sorulduğunda m. 645 ve m. 518 daha yüksek çıktı).
        # Bunları bağlama koymak modeli yanlış maddeden cevap üretmeye itiyor.
        if numarali := [p for p in self.parcalar if "madde-no" in p.kaynaklar]:
            return numarali

        esik = self.en_yuksek_benzerlik * oran
        secili = [
            p for p in self.parcalar
            if p.benzerlik >= esik or "madde-no" in p.kaynaklar
        ]
        # Süzgeç fazla agresif davranırsa modeli bağlamsız bırakmamak için
        # sıralamadaki ilk maddelerle tamamlanır.
        if len(secili) < alt_sinir:
            secili = self.parcalar[:alt_sinir]
        return secili

    @property
    def alakali(self) -> bool:
        """Korpusta soruyla yeterince ilgili bir hüküm var mı?

        Soruda madde numarası açıkça yazılmışsa (örn. "TBK 344 ne diyor?")
        benzerliğe bakılmaz: kullanıcı maddeyi kimliğiyle istemiştir ve o madde
        korpusta bulunmuştur.
        """
        if self.madde_adet:
            return True
        return self.en_yuksek_benzerlik >= config.RELEVANCE_MIN


# --------------------------------------------------------------------------
# FTS sorgusu kurma
# --------------------------------------------------------------------------
def _terimlestir(sorgu: str) -> list[str]:
    """Soruyu FTS5 terimlerine çevirir (önek araması).

    Türkçe eklemeli bir dil olduğu için ("zamanaşımı" / "zamanaşımına" /
    "zamanaşımının") tam eşleşme aramak isabeti düşürür. Bu yüzden uzun
    kelimelerde sonu kırpıp önek (prefix) araması yapıyoruz: kaba ama bu ölçekte
    etkili bir gövdeleme (stemming) yerine geçiyor.
    """
    terimler: list[str] = []
    for tok in normalize(sorgu).split():
        if len(tok) < 2 or tok in DURAK_KELIMELER:
            continue
        if tok.isdigit():
            terimler.append(tok)              # madde numarası: tam eşleşme
        elif len(tok) >= 6:
            kok = tok[: max(5, int(len(tok) * 0.8))]
            terimler.append(f"{kok}*")
        else:
            terimler.append(f"{tok}*")
    return list(dict.fromkeys(terimler))


def fts_sorgusu_kur(sorgu: str) -> str:
    """Süzme yapmadan ham FTS ifadesi (geriye dönük uyumluluk ve teşhis için)."""
    return " OR ".join(_terimlestir(sorgu))


# --------------------------------------------------------------------------
# Arama motoru
# --------------------------------------------------------------------------
class Retriever:
    """Hibrit arama motoru.

    Gömme matrisi bir kez belleğe alınır (652 x 1024 float32 ≈ 2,7 MB), böylece
    her sorguda tek matris çarpımıyla tüm korpusa karşı benzerlik hesaplanır.
    """

    def __init__(self, conn: sqlite3.Connection, saglayici) -> None:
        self.conn = conn
        self.saglayici = saglayici
        matris, idler, model = db.tum_gommeler(conn)
        self.gomme_modeli = model
        self.parca_idleri = idler
        if matris.size:
            # Kosinüs benzerliğini tek nokta çarpımına indirmek için önceden
            # birim uzunluğa normalleştiriyoruz.
            normlar = np.linalg.norm(matris, axis=1, keepdims=True)
            normlar[normlar == 0] = 1.0
            self.matris = (matris / normlar).astype(np.float32)
        else:
            self.matris = matris

        self.parca_sayisi = (
            self.conn.execute("SELECT count(*) FROM parcalar").fetchone()[0] or 1
        )
        self._df_onbellegi: dict[str, int] = {}
        # Parça kimliğinden gömme matrisindeki satıra eşleme (benzerlik okumak için)
        self._satir = {pid: i for i, pid in enumerate(self.parca_idleri)}

    # -------------------------------------------------- terim ayırt ediciliği
    def _belge_frekansi(self, terim: str) -> int:
        """Terimin kaç parçada geçtiğini sayar (sonuç önbelleğe alınır).

        Sayım FTS5'in kendisine sorulur; böylece ölçülen frekans, aramada
        fiilen kullanılacak önek eşleşmesiyle birebir aynı olur. Python
        tarafında ayrı bir sayaç tutulsaydı iki mantık zamanla ayrışabilirdi.
        """
        if terim in self._df_onbellegi:
            return self._df_onbellegi[terim]
        try:
            (sayi,) = self.conn.execute(
                "SELECT count(*) FROM parcalar_fts WHERE parcalar_fts MATCH ?",
                (terim,),
            ).fetchone()
        except sqlite3.OperationalError:
            sayi = 0
        self._df_onbellegi[terim] = int(sayi)
        return int(sayi)

    def _ayirt_edici_terimler(
        self, sorgu: str, df_limiti: float | None = None, esanlam: bool = True
    ) -> list[str]:
        """Yalnızca korpusta seyrek geçen (bilgi taşıyan) terimleri döndürür.

        Hepsi sınırın üstünde kalırsa boş liste dönmek yerine en seyrek üç terim
        korunur: kelime aramasını tamamen kapatmak, tam terim içeren sorularda
        (örneğin "takas") isabeti düşürüyor.
        """
        limit = config.FTS_DF_LIMIT if df_limiti is None else df_limiti
        esik = max(1, int(self.parca_sayisi * limit))
        terimler = _terimlestir(sorgu)
        if esanlam:
            # Kanun dilindeki karşılıklar da aranır ("ihtiyaç" -> "gereksinim").
            terimler += [
                t for t in _terimlestir(" ".join(esanlamlari_ekle(sorgu)))
                if t not in terimler
            ]
        if not terimler:
            return []
        frekanslar = [(t, self._belge_frekansi(t)) for t in terimler]
        ayirt_edici = [t for t, df in frekanslar if 0 < df <= esik]
        if ayirt_edici:
            return ayirt_edici
        bulunanlar = [(t, df) for t, df in frekanslar if df > 0]
        bulunanlar.sort(key=lambda td: td[1])
        return [t for t, _ in bulunanlar[:3]]

    # ------------------------------------------------------------ bileşenler
    def _benzerlikler(self, sorgu: str) -> np.ndarray:
        """Sorguyu gömüp tüm korpusa karşı kosinüs benzerliğini döndürür.

        Gömme matrisi kurucuda birim uzunluğa getirildiği için kosinüs
        benzerliği tek matris çarpımına iniyor (652 x 1024, ~2,7 MB).
        """
        if not self.matris.size:
            return np.empty(0, dtype=np.float32)
        q = np.asarray(self.saglayici.embed_tek(sorgu), dtype=np.float32)
        if q.shape[0] != self.matris.shape[1]:
            raise ValueError(
                f"Sorgu gömme boyutu ({q.shape[0]}) veritabanındakiyle "
                f"({self.matris.shape[1]}) uyuşmuyor. Farklı bir embedding "
                f"modeliyle ingest yapılmış olabilir; ingest'i tekrar çalıştır."
            )
        q /= np.linalg.norm(q) or 1.0
        return self.matris @ q

    def vektor_ara(self, sorgu: str, k: int) -> list[int]:
        return self._ust_k(self._benzerlikler(sorgu), k)

    def _ust_k(self, skorlar: np.ndarray, k: int) -> list[int]:
        if not skorlar.size:
            return []
        return [self.parca_idleri[i] for i in np.argsort(-skorlar)[:k]]

    def fts_ara(
        self,
        sorgu: str,
        k: int,
        df_limiti: float | None = None,
        esanlam: bool = True,
    ) -> tuple[list[int], str]:
        ifade = " OR ".join(self._ayirt_edici_terimler(sorgu, df_limiti, esanlam))
        if not ifade:
            return [], ""
        try:
            rows = self.conn.execute(
                "SELECT rowid FROM parcalar_fts WHERE parcalar_fts MATCH ?"
                " ORDER BY rank LIMIT ?",
                (ifade, k),
            ).fetchall()
        except sqlite3.OperationalError:
            # Bozuk MATCH ifadesi sorguyu tamamen düşürmesin
            return [], ifade
        return [int(r["rowid"]) for r in rows], ifade

    def madde_ara(self, sorgu: str) -> tuple[list[int], tuple[str, ...]]:
        """Soruda madde numarası varsa o maddeyi doğrudan getirir.

        "TBK 344 ne diyor?" gibi sorularda bu, isabeti garantiye alır: arama
        skorlarına güvenmek yerine maddeye kimliğiyle erişiyoruz.
        """
        numaralar = extract_madde_numbers(sorgu)
        if not numaralar:
            return [], ()
        kisaltma = None
        if km := re.search(r"(?i)\b(tbk|tck|tmk|hmk|cmk|kvkk|ttk)\b", sorgu):
            kisaltma = km.group(1).upper()
        idler: list[int] = []
        for no in numaralar:
            for p in db.madde_ile_getir(self.conn, no, kisaltma):
                idler.append(p.id)
        return idler, tuple(numaralar)

    # ------------------------------------------------------------------ RRF
    @staticmethod
    def _rrf_ekle(
        havuz: dict[int, float],
        kaynaklar: dict[int, set[str]],
        siralama: list[int],
        etiket: str,
        agirlik: float = 1.0,
        k: int = config.RRF_K,
    ) -> None:
        for sira, pid in enumerate(siralama, start=1):
            havuz[pid] = havuz.get(pid, 0.0) + agirlik / (k + sira)
            kaynaklar.setdefault(pid, set()).add(etiket)

    def ara(
        self,
        sorgu: str,
        top_k: int = config.TOP_K,
        aday: int | None = None,
        w_vektor: float | None = None,
        w_fts: float | None = None,
        w_madde: float | None = None,
        df_limiti: float | None = None,
    ) -> AramaSonucu:
        """Hibrit arama. Ağırlıklar ızgara taraması yapılabilsin diye parametrik."""
        aday = aday or config.CANDIDATE_K
        havuz: dict[int, float] = {}
        kaynaklar: dict[int, set[str]] = {}

        # Benzerlikler bir kez hesaplanır: hem sıralama hem alaka eşiği için.
        benzerlikler = self._benzerlikler(sorgu)
        en_yuksek = float(benzerlikler.max()) if benzerlikler.size else 0.0
        vektor_idler = self._ust_k(benzerlikler, aday)
        self._rrf_ekle(
            havuz, kaynaklar, vektor_idler, "vektör",
            agirlik=config.W_VEKTOR if w_vektor is None else w_vektor,
        )

        fts_idler, ifade = self.fts_ara(sorgu, aday, df_limiti=df_limiti)
        self._rrf_ekle(
            havuz, kaynaklar, fts_idler, "kelime",
            agirlik=config.W_FTS if w_fts is None else w_fts,
        )

        # Doğrudan madde eşleşmesine yüksek ağırlık: kullanıcı numarayı açıkça
        # yazdıysa o maddenin ilk sırada olmaması kullanıcı hatası gibi görünür.
        madde_idler, numaralar = self.madde_ara(sorgu)
        self._rrf_ekle(
            havuz, kaynaklar, madde_idler, "madde-no",
            agirlik=config.W_MADDE if w_madde is None else w_madde,
        )

        sirali = sorted(havuz.items(), key=lambda kv: -kv[1])[:top_k]
        kayitlar = db.parcalari_getir(self.conn, [pid for pid, _ in sirali])

        sonuc: list[db.ParcaKaydi] = []
        for pid, skor in sirali:
            if kayit := kayitlar.get(pid):
                kayit.skor = round(skor, 6)
                kayit.kaynaklar = tuple(sorted(kaynaklar.get(pid, set())))
                satir = self._satir.get(pid)
                kayit.benzerlik = (
                    round(float(benzerlikler[satir]), 4)
                    if satir is not None and benzerlikler.size
                    else 0.0
                )
                sonuc.append(kayit)

        return AramaSonucu(
            parcalar=sonuc,
            vektor_adet=len(vektor_idler),
            fts_adet=len(fts_idler),
            madde_adet=len(madde_idler),
            fts_sorgusu=ifade,
            bulunan_madde_numaralari=numaralar,
            en_yuksek_benzerlik=en_yuksek,
        )
