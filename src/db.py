"""SQLite: madde parçaları, gömme vektörleri (BLOB) ve FTS5 tam metin indeksi."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import numpy as np

from src import config
from src.turkish import normalize

SEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS kanunlar (
    id              INTEGER PRIMARY KEY,
    slug            TEXT UNIQUE NOT NULL,
    kanun_adi       TEXT NOT NULL,
    kanun_no        TEXT NOT NULL,
    kisaltma        TEXT NOT NULL,
    kabul_tarihi    TEXT,
    resmi_gazete    TEXT,
    kaynak_url      TEXT,
    sha256          TEXT,          -- kaynak değişti mi kontrolü için
    islenme_zamani  TEXT
);

CREATE TABLE IF NOT EXISTS parcalar (
    id            INTEGER PRIMARY KEY,
    kanun_id      INTEGER NOT NULL REFERENCES kanunlar(id) ON DELETE CASCADE,
    madde_no      TEXT NOT NULL,
    madde_turu    TEXT NOT NULL,
    kenar_baslik  TEXT,
    konu_yolu     TEXT,
    kitap         TEXT,
    kisim         TEXT,
    bolum         TEXT,
    ayirim        TEXT,
    atif          TEXT NOT NULL,   -- "TBK m. 27"
    icerik        TEXT NOT NULL,   -- kullanıcıya gösterilen saf madde metni
    gomme_metni   TEXT NOT NULL,   -- bağlam başlığı + metin (embed edilen)
    parca_no      INTEGER DEFAULT 1,
    parca_toplam  INTEGER DEFAULT 1,
    not_etiketi   TEXT,
    sira          INTEGER,
    icerik_norm   TEXT             -- Türkçe normalize (FTS ve eşleşme için)
);

CREATE INDEX IF NOT EXISTS ix_parcalar_madde ON parcalar(kanun_id, madde_no);
CREATE INDEX IF NOT EXISTS ix_parcalar_sira  ON parcalar(kanun_id, sira);

CREATE TABLE IF NOT EXISTS gommeler (
    parca_id  INTEGER PRIMARY KEY REFERENCES parcalar(id) ON DELETE CASCADE,
    model     TEXT NOT NULL,
    boyut     INTEGER NOT NULL,
    vektor    BLOB NOT NULL        -- float32 dizisi
);

-- Tam metin indeksi. İçeriği ayrı tutulan bağımsız bir FTS tablosu; rowid
-- değerleri parcalar.id ile birebir aynı tutulur.
CREATE VIRTUAL TABLE IF NOT EXISTS parcalar_fts USING fts5(
    icerik_norm,
    baslik_norm,
    atif_norm
);

CREATE TABLE IF NOT EXISTS ustveri (
    anahtar TEXT PRIMARY KEY,
    deger   TEXT
);
"""


# --------------------------------------------------------------------------
# Bağlantı
# --------------------------------------------------------------------------
def baglan(yol: Path | str = config.DB_PATH) -> sqlite3.Connection:
    # Streamlit her rerun'da farklı thread kullanır; bağlantı cache'de kalır.
    conn = sqlite3.connect(str(yol), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def sema_kur(conn: sqlite3.Connection) -> None:
    conn.executescript(SEMA)
    conn.commit()


def ustveri_yaz(conn: sqlite3.Connection, anahtar: str, deger) -> None:
    conn.execute(
        "INSERT INTO ustveri(anahtar, deger) VALUES(?, ?) "
        "ON CONFLICT(anahtar) DO UPDATE SET deger = excluded.deger",
        (anahtar, json.dumps(deger, ensure_ascii=False)),
    )


def ustveri_oku(conn: sqlite3.Connection, anahtar: str, varsayilan=None):
    row = conn.execute(
        "SELECT deger FROM ustveri WHERE anahtar = ?", (anahtar,)
    ).fetchone()
    return json.loads(row["deger"]) if row else varsayilan


# --------------------------------------------------------------------------
# Vektör dönüşümü
# --------------------------------------------------------------------------
def vektor_to_blob(vec: Sequence[float]) -> bytes:
    return np.asarray(vec, dtype=np.float32).tobytes()


def blob_to_vektor(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# --------------------------------------------------------------------------
# Yazma
# --------------------------------------------------------------------------
def kanun_kaydet(conn: sqlite3.Connection, kanun, slug: str) -> int:
    """Kanunu (yeniden) kaydeder ve eski parçalarını temizler.

    Aynı kanun tekrar işlendiğinde parçalar ON DELETE CASCADE ile silinir;
    böylece ingest her zaman sıfırdan tutarlı bir sonuç üretir.

    DİKKAT — FTS tablosu CASCADE'e dahil DEĞİLDİR. `parcalar_fts` bağımsız bir
    FTS5 sanal tablosudur; yabancı anahtarı ve tetikleyicisi yoktur, dolayısıyla
    `parcalar` satırları silindiğinde FTS satırları yerinde kalır. SQLite
    silinen rowid'leri yeniden kullandığı için ikinci ingest denemesinde
    `sqlite3.IntegrityError: constraint failed` alınır (bu hata gerçekten
    yaşandı). Bu yüzden FTS satırları elle, kanun silinmeden ÖNCE temizlenir:
    silme işleminden sonra hangi parçanın bu kanuna ait olduğu bilinemez.
    """
    conn.execute(
        "DELETE FROM parcalar_fts WHERE rowid IN ("
        "  SELECT p.id FROM parcalar p JOIN kanunlar k ON k.id = p.kanun_id"
        "  WHERE k.slug = ?)",
        (slug,),
    )
    conn.execute("DELETE FROM kanunlar WHERE slug = ?", (slug,))
    cur = conn.execute(
        "INSERT INTO kanunlar(slug, kanun_adi, kanun_no, kisaltma, kabul_tarihi,"
        " resmi_gazete, kaynak_url, sha256, islenme_zamani)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (
            slug, kanun.kanun_adi, kanun.kanun_no, kanun.kisaltma,
            kanun.kabul_tarihi, kanun.resmi_gazete, kanun.kaynak_url,
            kanun.sha256, datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )
    return int(cur.lastrowid)


def parcalari_kaydet(conn: sqlite3.Connection, kanun_id: int, chunks: Iterable) -> list[int]:
    """Parçaları yazar, FTS indeksini de aynı işlemde günceller."""
    idler: list[int] = []
    for c in chunks:
        icerik_norm = normalize(c.icerik)
        cur = conn.execute(
            "INSERT INTO parcalar(kanun_id, madde_no, madde_turu, kenar_baslik,"
            " konu_yolu, kitap, kisim, bolum, ayirim, atif, icerik, gomme_metni,"
            " parca_no, parca_toplam, not_etiketi, sira, icerik_norm)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                kanun_id, c.madde_no, c.madde_turu, c.kenar_baslik, c.konu_yolu,
                c.kitap, c.kisim, c.bolum, c.ayirim, c.atif, c.icerik,
                c.gomme_metni, c.parca_no, c.parca_toplam, c.not_etiketi,
                c.sira, icerik_norm,
            ),
        )
        pid = int(cur.lastrowid)
        idler.append(pid)

        # FTS satırı: madde numarası ve kısaltma da aranabilir olsun ki
        # "TBK 344" veya "madde 344" sorguları doğrudan isabet etsin.
        baslik_norm = normalize(f"{c.kenar_baslik} {c.konu_yolu} {c.bolum} {c.ayirim}")
        atif_norm = normalize(
            f"{c.kisaltma} {c.kanun_no} madde {c.madde_no} m {c.madde_no} {c.atif}"
        )
        conn.execute(
            "INSERT INTO parcalar_fts(rowid, icerik_norm, baslik_norm, atif_norm)"
            " VALUES(?,?,?,?)",
            (pid, icerik_norm, baslik_norm, atif_norm),
        )
    return idler


def gommeleri_kaydet(
    conn: sqlite3.Connection,
    parca_idleri: Sequence[int],
    vektorler: Sequence[Sequence[float]],
    model: str,
) -> None:
    if len(parca_idleri) != len(vektorler):
        raise ValueError(
            f"Parça sayısı ({len(parca_idleri)}) ile vektör sayısı "
            f"({len(vektorler)}) uyuşmuyor."
        )
    conn.executemany(
        "INSERT INTO gommeler(parca_id, model, boyut, vektor) VALUES(?,?,?,?)"
        " ON CONFLICT(parca_id) DO UPDATE SET model=excluded.model,"
        " boyut=excluded.boyut, vektor=excluded.vektor",
        [
            (pid, model, len(vec), vektor_to_blob(vec))
            for pid, vec in zip(parca_idleri, vektorler)
        ],
    )


def fts_yeniden_kur(conn: sqlite3.Connection) -> None:
    """FTS tablosunu parcalar tablosundan yeniden üretir (onarım amaçlı)."""
    conn.execute("DELETE FROM parcalar_fts")
    for row in conn.execute(
        "SELECT p.id, p.icerik_norm, p.kenar_baslik, p.konu_yolu, p.bolum,"
        " p.ayirim, p.madde_no, p.atif, k.kisaltma, k.kanun_no"
        " FROM parcalar p JOIN kanunlar k ON k.id = p.kanun_id"
    ).fetchall():
        conn.execute(
            "INSERT INTO parcalar_fts(rowid, icerik_norm, baslik_norm, atif_norm)"
            " VALUES(?,?,?,?)",
            (
                row["id"],
                row["icerik_norm"],
                normalize(
                    f"{row['kenar_baslik']} {row['konu_yolu']} "
                    f"{row['bolum']} {row['ayirim']}"
                ),
                normalize(
                    f"{row['kisaltma']} {row['kanun_no']} madde {row['madde_no']} "
                    f"m {row['madde_no']} {row['atif']}"
                ),
            ),
        )
    conn.commit()


# --------------------------------------------------------------------------
# Okuma
# --------------------------------------------------------------------------
@dataclass
class ParcaKaydi:
    """Aramadan dönen parça (skor bilgisiyle birlikte)."""

    id: int
    atif: str
    madde_no: str
    kenar_baslik: str
    konu_yolu: str
    bolum: str
    ayirim: str
    icerik: str
    kanun_adi: str
    kisaltma: str
    kanun_no: str
    not_etiketi: str = ""
    skor: float = 0.0                 # RRF birleşik skoru (sıralama için)
    benzerlik: float = 0.0            # ham kosinüs benzerliği (bağlam süzgeci için)
    kaynaklar: tuple[str, ...] = ()   # hangi arama getirdi: vektor / fts / madde

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ParcaKaydi":
        return cls(
            id=row["id"], atif=row["atif"], madde_no=row["madde_no"],
            kenar_baslik=row["kenar_baslik"] or "", konu_yolu=row["konu_yolu"] or "",
            bolum=row["bolum"] or "", ayirim=row["ayirim"] or "",
            icerik=row["icerik"], kanun_adi=row["kanun_adi"],
            kisaltma=row["kisaltma"], kanun_no=row["kanun_no"],
            not_etiketi=row["not_etiketi"] or "",
        )


_PARCA_SELECT = """
SELECT p.id, p.madde_no, p.kenar_baslik, p.konu_yolu, p.bolum, p.ayirim,
       p.atif, p.icerik, p.not_etiketi,
       k.kanun_adi, k.kisaltma, k.kanun_no
FROM parcalar p JOIN kanunlar k ON k.id = p.kanun_id
"""


def parcalari_getir(conn: sqlite3.Connection, idler: Sequence[int]) -> dict[int, ParcaKaydi]:
    if not idler:
        return {}
    yer = ",".join("?" * len(idler))
    rows = conn.execute(f"{_PARCA_SELECT} WHERE p.id IN ({yer})", tuple(idler)).fetchall()
    return {int(r["id"]): ParcaKaydi.from_row(r) for r in rows}


def madde_ile_getir(
    conn: sqlite3.Connection, madde_no: str, kisaltma: str | None = None
) -> list[ParcaKaydi]:
    """Madde numarasıyla doğrudan erişim ("TBK 344" tipi sorgular için)."""
    sql = f"{_PARCA_SELECT} WHERE upper(p.madde_no) = upper(?)"
    params: list = [madde_no]
    if kisaltma:
        sql += " AND upper(k.kisaltma) = upper(?)"
        params.append(kisaltma)
    sql += " ORDER BY p.parca_no"
    return [ParcaKaydi.from_row(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def tum_gommeler(conn: sqlite3.Connection) -> tuple[np.ndarray, list[int], str]:
    """Tüm gömmeleri tek matriste döndürür: (matris, parca_idleri, model_adi).

    Bu ölçekte (binlerce satır) belleğe almak kabul edilebilir ve kosinüs
    benzerliğini tek matris çarpımıyla hesaplamayı sağlar.
    """
    rows = conn.execute(
        "SELECT parca_id, model, vektor FROM gommeler ORDER BY parca_id"
    ).fetchall()
    if not rows:
        return np.zeros((0, 0), dtype=np.float32), [], ""
    idler = [int(r["parca_id"]) for r in rows]
    matris = np.vstack([blob_to_vektor(r["vektor"]) for r in rows])
    return matris, idler, rows[0]["model"]


def istatistik(conn: sqlite3.Connection) -> dict:
    def tek(sql: str):
        return conn.execute(sql).fetchone()[0]

    return {
        "kanun_sayisi": tek("SELECT COUNT(*) FROM kanunlar"),
        "parca_sayisi": tek("SELECT COUNT(*) FROM parcalar"),
        "madde_sayisi": tek("SELECT COUNT(DISTINCT kanun_id || '#' || madde_no) FROM parcalar"),
        "gomme_sayisi": tek("SELECT COUNT(*) FROM gommeler"),
        "fts_sayisi": tek("SELECT COUNT(*) FROM parcalar_fts"),
        "gomme_modeli": ustveri_oku(conn, "gomme_modeli", ""),
        "gomme_boyutu": ustveri_oku(conn, "gomme_boyutu", 0),
    }
