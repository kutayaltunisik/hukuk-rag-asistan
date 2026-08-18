"""Proje ayarları. Ortam değişkeniyle ezilebilir."""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Dizinler
# --------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
DATA_RAW = ROOT / "data" / "raw"
DATA_PROCESSED = ROOT / "data" / "processed"
DB_DIR = ROOT / "db"
DB_PATH = Path(os.getenv("HUKUK_DB_PATH", DB_DIR / "hukuk.db"))
REPORTS_DIR = ROOT / "reports"

for _d in (DATA_RAW, DATA_PROCESSED, DB_DIR, REPORTS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Korpus: hangi mevzuat indirilecek
# --------------------------------------------------------------------------
# mevzuat.gov.tr metin adresleri "<MevzuatTur>.<Tertip>.<MevzuatNo>.htm" desenini
# izler. Kanunlar için Tur=1, güncel tertip 5'tir.
MEVZUAT_URL_TEMPLATE = "https://www.mevzuat.gov.tr/MevzuatMetin/{tur}.{tertip}.{no}.htm"

# Kaynak HTML Word'den üretildiği için Windows-1254 kodludur.
MEVZUAT_ENCODING = "windows-1254"

# Odak alanı: Borçlar Hukuku. Yeni kanun eklemek için listeye satır eklemek yeterli.
KANUNLAR: list[dict] = [
    {
        "slug": "tbk_6098",
        "kanun_adi": "Türk Borçlar Kanunu",
        "kanun_no": "6098",
        "kisaltma": "TBK",
        "tur": 1,
        "tertip": 5,
    },
]


# --------------------------------------------------------------------------
# Modeller (Foundry Local katalog adları)
# --------------------------------------------------------------------------
# Katalog adları sürümle değişebiliyor; sırayla denenir.
# 8 GB RAM için birincil sohbet modeli qwen3-4b.
CHAT_MODEL_CANDIDATES: list[str] = [
    c.strip()
    for c in os.getenv(
        "HUKUK_CHAT_MODEL",
        "qwen3-4b,qwen3-1.7b,qwen3.5-2b-text,qwen3.5-2b,qwen2.5-7b,phi-3.5-mini",
    ).split(",")
    if c.strip()
]

# Qwen3 düşünme modu açıkken cevap çok uzuyor; `/no_think` öntanımlı.
NO_THINK = os.getenv("HUKUK_NO_THINK", "1") not in ("0", "false")

# Boş `<think></think>` blokları cevaptan ayıklanır.
STRIP_THINK_BLOCKS = os.getenv("HUKUK_STRIP_THINK", "1") not in ("0", "false")

# sdk ana yol; http sıcaklık/token limiti için; dev yedek.
PROVIDER_ORDER: list[str] = [
    p.strip().lower()
    for p in os.getenv("HUKUK_PROVIDER", "sdk,http,dev").split(",")
    if p.strip()
]

# SDK, CLI'nin indirdiği modelleri bu dizin verilmezse bulamıyor.
MODEL_CACHE_DIR = os.getenv(
    "HUKUK_MODEL_CACHE_DIR", str(Path.home() / ".foundry" / "cache" / "models")
)

EMBEDDING_MODEL_CANDIDATES: list[str] = [
    c.strip()
    for c in os.getenv(
        "HUKUK_EMBEDDING_MODEL",
        "qwen3-embedding-0.6b,qwen3-embedding-8b",
    ).split(",")
    if c.strip()
]

# Foundry Local servisi. Boşsa SDK ile / port taramasıyla otomatik bulunur.
FOUNDRY_ENDPOINT = os.getenv("HUKUK_FOUNDRY_ENDPOINT", "").strip()
FOUNDRY_PROBE_PORTS = (5273, 5272, 5274, 5275, 8080, 62399)
FOUNDRY_TIMEOUT = float(os.getenv("HUKUK_FOUNDRY_TIMEOUT", "300"))

# Foundry yoksa test için yedek sağlayıcı.
ALLOW_DEV_FALLBACK = os.getenv("HUKUK_ALLOW_DEV_FALLBACK", "1") not in ("0", "false")


# --------------------------------------------------------------------------
# Parçalama (chunking)
# --------------------------------------------------------------------------
# Hukuk metninin doğal birimi maddedir; bu yüzden madde bazlı bölüyoruz.
# Çok uzun maddeler fıkra sınırından ikiye bölünür (embedding penceresi için).
MAX_CHUNK_CHARS = int(os.getenv("HUKUK_MAX_CHUNK_CHARS", "2400"))
# Bölünen parçalar arasında bağlamı korumak için taşınan karakter miktarı.
CHUNK_OVERLAP_CHARS = int(os.getenv("HUKUK_CHUNK_OVERLAP_CHARS", "200"))


# --------------------------------------------------------------------------
# Arama (retrieval)
# --------------------------------------------------------------------------
TOP_K = int(os.getenv("HUKUK_TOP_K", "5"))          # modele verilecek parça sayısı
RRF_K = 60
EMBED_BATCH = int(os.getenv("HUKUK_EMBED_BATCH", "16"))

# tests/tune_retrieval.py ile seçildi.
CANDIDATE_K = int(os.getenv("HUKUK_CANDIDATE_K", "12"))

# RRF ağırlıkları. Madde numarası en güçlü sinyal.
W_VEKTOR = float(os.getenv("HUKUK_W_VEKTOR", "1.0"))
W_FTS = float(os.getenv("HUKUK_W_FTS", "1.0"))
W_MADDE = float(os.getenv("HUKUK_W_MADDE", "3.0"))

# Çok sık geçen terimler ("sözleşme", "zarar") kelime aramasından atılır.
FTS_DF_LIMIT = float(os.getenv("HUKUK_FTS_DF_LIMIT", "0.12"))

# Tepe benzerlik bunun altındaysa soru kapsam dışı sayılır.
RELEVANCE_MIN = float(os.getenv("HUKUK_RELEVANCE_MIN", "0.42"))

# Bağlama yalnızca tepe skorun bu oranı ve üzerindeki maddeler girer.
BAGLAM_ORAN_ESIGI = float(os.getenv("HUKUK_BAGLAM_ORAN_ESIGI", "0.70"))
BAGLAM_MIN_MADDE = int(os.getenv("HUKUK_BAGLAM_MIN_MADDE", "2"))


# --------------------------------------------------------------------------
# Üretim (generation)
# --------------------------------------------------------------------------
TEMPERATURE = float(os.getenv("HUKUK_TEMPERATURE", "0.1"))
MAX_TOKENS = int(os.getenv("HUKUK_MAX_TOKENS", "400"))

# SDK max_tokens almıyor; akış tüketici tarafında kesilir.
CHAT_KARAKTER_BUTCESI = int(os.getenv("HUKUK_CHAT_KARAKTER_BUTCESI", "700"))
CHAT_SURE_SINIRI = float(os.getenv("HUKUK_CHAT_SURE_SINIRI", "60"))
