"""mevzuat.gov.tr HTML'ini madde bazlı parçalara ayırır.

Parça birimi maddedir. Kaynak Word HTML'i olduğu için satır değil
blok (`h1`/`h2`/`p`) üzerinden gidilir.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import re
from dataclasses import dataclass, field

from src import config
from src.turkish import clean_whitespace, normalize

# --------------------------------------------------------------------------
# Desenler
# --------------------------------------------------------------------------
_ORDINAL = (
    r"(?:(?:ON|YİRMİ|OTUZ|KIRK|ELLİ|ALTMIŞ)\s*)?"
    r"(?:BİRİNCİ|İKİNCİ|ÜÇÜNCÜ|DÖRDÜNCÜ|BEŞİNCİ|ALTINCI|YEDİNCİ|SEKİZİNCİ"
    r"|DOKUZUNCU|ONUNCU|YİRMİNCİ|OTUZUNCU|KIRKINCI|ELLİNCİ|SON)"
)
_LEVELS = r"(KİTAP|KISIM|BÖLÜM|AYIRIM|AYRIM|FASIL)"

RE_STRUCT = re.compile(rf"^{_ORDINAL}\s+{_LEVELS}\b", re.UNICODE)
RE_MADDE = re.compile(
    r"^(GEÇİCİ\s+MADDE|EK\s+MADDE|MADDE)\s+(\d+)\s*"
    r"(?:/\s*([A-Za-zÇĞİÖŞÜçğıöşü]))?\s*[-–—]?\s*",
    re.UNICODE,
)

# Kenar başlığı / bent işareti: "A.", "I.", "1.", "a.", "aa."
RE_OUTLINE = re.compile(
    r"^(?P<marker>[A-ZÇĞİÖŞÜ]|[IVXLCDM]{1,5}|\d{1,2}|[a-zçğıöşü]{1,3})\s*[.)]\s+(?P<rest>\S.*)$",
    re.UNICODE,
)

# Ana metnin bittiği yer: değişiklik tarihleri tablosu (retrieval için gürültü)
RE_TABLO_BASI = re.compile(r"SAYILI KANUNA EK VE DEĞİŞİKLİK GETİREN", re.UNICODE)
# Kanuna işlenemeyen hükümler: hukuken anlamlı, ayrı etiketle alınır
RE_ISLENEMEYEN = re.compile(r"SAYILI KANUNA İŞLENEMEYEN HÜKÜMLER", re.UNICODE)
ISLENEMEYEN_ETIKET = "İŞLENEMEYEN HÜKÜM"
# Dipnot tanımları: "[1] 2/7/2018 tarihli ve ..." — korpusa alınmaz
RE_DIPNOT_TANIMI = re.compile(r"^\s*\[\d+\]\s")
# Metne karışan dipnot atıfları
RE_DIPNOT_ATFI = re.compile(r"\[\d+\]")

RE_META = {
    "kanun_no": re.compile(r"Kanun\s*Numaras[ıi]\s*:\s*(\d+)"),
    "kabul_tarihi": re.compile(r"Kabul\s*Tarihi\s*:\s*([\d/]+)"),
    "resmi_gazete": re.compile(
        r"Resm[îi]\s*Gazete\s*:\s*Tarih\s*:\s*([\d/]+)\s*Say[ıi]\s*:\s*(\d+)"
    ),
}

_ROMAN = ("I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
          "XI", "XII", "XIII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX")


# --------------------------------------------------------------------------
# Veri tipleri
# --------------------------------------------------------------------------
@dataclass
class Block:
    """HTML'deki tek bir blok öğe (<p> veya <hN>)."""

    tag: str
    centered: bool
    sinif: str           # Word paragraf sınıfı (MsoNormal, MsoTitle, h2 için boş)
    baslik_mi: bool      # başlık rolünde mi (etiket/sınıf/hizalamadan çıkarılır)
    text: str
    text_raw: str        # dipnot işaretleri temizlenmeden önceki hâli


@dataclass
class Madde:
    """Kanunun tek bir maddesi ve onu konumlandıran bağlam."""

    madde_no: str                      # "27", "2/A", "GEÇİCİ 1"
    madde_turu: str = "madde"          # madde | gecici | ek
    kenar_baslik: str = ""             # en özel kenar başlığı: "Kesin hükümsüzlük"
    konu_yolu: str = ""                # kenar başlığı hiyerarşisinin tamamı
    kitap: str = ""
    kisim: str = ""
    bolum: str = ""
    ayirim: str = ""
    fikralar: list[str] = field(default_factory=list)
    not_etiketi: str = ""              # "İŞLENEMEYEN HÜKÜM" gibi
    sira: int = 0

    @property
    def metin(self) -> str:
        return "\n".join(self.fikralar).strip()

    def baglam_yolu(self) -> str:
        parts = [p for p in (self.kitap, self.kisim, self.bolum, self.ayirim) if p]
        return " > ".join(parts)


@dataclass
class ParsedKanun:
    kanun_adi: str
    kanun_no: str
    kisaltma: str
    kabul_tarihi: str = ""
    resmi_gazete: str = ""
    maddeler: list[Madde] = field(default_factory=list)
    kaynak_url: str = ""
    sha256: str = ""


# --------------------------------------------------------------------------
# HTML -> blok listesi
# --------------------------------------------------------------------------
RE_BLOCK_OPEN = re.compile(r"(?is)<(h[1-6]|p)\b([^>]*)>")
RE_BLOCK_CLOSE_TAIL = re.compile(r"(?is)</(?:p|h[1-6])\s*>.*$")
RE_CLASS = re.compile(r"class=[\"']?([A-Za-z0-9_-]+)")

# Dipnot referansları: içeriğiyle birlikte atılır. Aksi halde kenar başlığına
# "II. Belirlenmesi 2" gibi dipnot numarası bulaşıyor ve atıf kirleniyor.
RE_DIPNOT_ISARETI = re.compile(
    r"(?is)<sup\b[^>]*>.*?</sup>"
    r"|<span[^>]*vertical-align:\s*super[^>]*>.*?</span>"
    r"|<span[^>]*class=[\"']?MsoFootnoteReference[^>]*>.*?</span>\s*(?:</span>)?"
    r"|<a[^>]*href=[\"']?#_ftn[^>]*>.*?</a>"
)

# Word paragraf sınıfları rolü belirler; kenar başlıklarının bir kısmı <h2>,
# bir kısmı "Stil..." ya da "MsoTitle" sınıflı düz <p>.
RE_STIL_SINIFI = re.compile(r"(?i)^stil")          # kesin başlık stili
RE_BELIRSIZ_BASLIK_SINIFI = re.compile(r"(?i)title|heading")
# Sayfa altı dipnot metni ve alt bilgi: korpusa hiç alınmaz.
RE_ATLANACAK_SINIF = re.compile(r"(?i)(footnotetext|footer)")


RE_KALIN = re.compile(r"(?is)<(?:b|strong)\b[^>]*>(.*?)</(?:b|strong)\s*>")


def _baslik_rolu_mu(
    tag: str, sinif: str, centered: bool, tamami_kalin: bool, text: str
) -> bool:
    """Blok başlık mı, madde gövdesi mi?

    Kaynakta kenar başlıkları üç ayrı biçimde işaretlenmiş; üçünü de yakalamak
    gerekiyor, aksi halde başlıklar madde metnine fıkra gibi karışıyor:

      1. <h2>A. Kesin hükümsüzlük</h2>            -> etiket kesin işaret
      2. <p class=MsoTitle>2. İkinci ...</p>       -> sınıf, ama güvenilmez
      3. <p class=MsoNormal><b>A. Ticari temsilci</b></p> -> tamamı kalın

    "MsoTitle" güvenilmez çünkü Word bu sınıfı bazı madde fıkraları için de
    kullanmış; yalnızca sınıfa güvenirsek TBK m. 27'nin ikinci fıkrası başlık
    sanılıp korpustan düşüyor. Bu yüzden belirsiz sınıflarda metnin biçimine de
    bakıyoruz: başlıklar kısadır ve cümle noktalamasıyla bitmez.

    Üçüncü biçimde ayırt edici olan, içeriğin TAMAMEN kalın olmasıdır: madde
    bloklarında yalnızca "MADDE 547-" kısmı kalındır, gerisi düz metindir.
    """
    if tag.startswith("h") or centered or RE_STIL_SINIFI.search(sinif):
        return True
    if tamami_kalin:
        return True
    if RE_BELIRSIZ_BASLIK_SINIFI.search(sinif):
        if re.search(r"[.!?;:]$", text):
            return False
        return bool(RE_OUTLINE.match(text)) and len(text) <= 120 or len(text) <= 60
    return False


def _strip_tags(fragment: str) -> str:
    fragment = RE_DIPNOT_ISARETI.sub(" ", fragment)
    fragment = re.sub(r"(?is)<[^>]+>", " ", fragment)
    fragment = html_mod.unescape(fragment).replace("\xa0", " ")
    # Blok içi satır sonları Word'ün sarmasıdır: tek boşluğa indir
    return re.sub(r"\s+", " ", fragment).strip()


def html_to_blocks(raw: bytes, encoding: str = config.MEVZUAT_ENCODING) -> list[Block]:
    text = raw.decode(encoding, errors="replace")
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)

    body = re.search(r"(?is)<body[^>]*>(.*)</body>", text)
    text = body.group(1) if body else text

    opens = list(RE_BLOCK_OPEN.finditer(text))
    blocks: list[Block] = []
    for i, m in enumerate(opens):
        end = opens[i + 1].start() if i + 1 < len(opens) else len(text)
        attrs = m.group(2)
        sinif = cm.group(1) if (cm := RE_CLASS.search(attrs)) else ""
        if RE_ATLANACAK_SINIF.search(sinif):
            continue

        inner = RE_BLOCK_CLOSE_TAIL.sub("", text[m.end():end])
        raw_text = _strip_tags(inner)
        if not raw_text:
            continue
        cleaned = clean_whitespace(RE_DIPNOT_ATFI.sub(" ", raw_text))
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned:
            continue

        # İçeriğin tamamı <b> içinde mi? Kenar başlıklarının bir kısmı yalnızca
        # bu şekilde işaretlenmiş (özel sınıf ya da <hN> etiketi olmadan).
        kalin_metin = _strip_tags(" ".join(RE_KALIN.findall(inner)))
        norm_kalin, norm_tam = normalize(kalin_metin), normalize(raw_text)
        tamami_kalin = bool(norm_kalin) and len(norm_kalin) >= 0.9 * len(norm_tam)

        tag = m.group(1).lower()
        blocks.append(
            Block(
                tag=tag,
                centered="center" in attrs.lower(),
                sinif=sinif,
                baslik_mi=_baslik_rolu_mu(
                    tag, sinif, "center" in attrs.lower(), tamami_kalin, cleaned
                ),
                text=cleaned,
                text_raw=raw_text,
            )
        )
    return blocks


# --------------------------------------------------------------------------
# Kenar başlığı hiyerarşisi
# --------------------------------------------------------------------------
class OutlineStack:
    """Kenar başlıklarının iç içe düzeyini takip eder.

    Kanun metni başlıkları "A. > I. > 1. > a." biçiminde iç içedir ve bu
    başlıklar birden fazla maddeyi kapsar. Yeni bir "2." başlığı geldiğinde
    üstteki "A." ve "I." hâlâ geçerlidir; sadece kendi düzeyi ve altı değişir.
    Bu yüzden düzeyleri bir yığında tutuyoruz.
    """

    LETTER, ROMAN, DIGIT, LOWER, LOWER2 = 1, 2, 3, 4, 5

    def __init__(self) -> None:
        self.levels: dict[int, str] = {}

    @staticmethod
    def _classify(marker: str) -> int:
        if marker.isdigit():
            return OutlineStack.DIGIT
        if len(marker) >= 2 and marker.islower():
            return OutlineStack.LOWER2
        if marker.islower():
            return OutlineStack.LOWER
        # Tek karakterli büyük harfler hem harf hem Roma sayısı olabilir
        # ("I.", "V.", "C."). Çok karakterli Roma sayıları ("II.", "IV.")
        # tereddütsüz Roma'dır; "I" ise bu metinlerde neredeyse her zaman Roma.
        if marker in _ROMAN and (len(marker) > 1 or marker == "I"):
            return OutlineStack.ROMAN
        return OutlineStack.LETTER

    def push(self, marker: str, text: str) -> None:
        level = self._classify(marker)
        self.levels[level] = text
        for deeper in list(self.levels):
            if deeper > level:
                del self.levels[deeper]

    def push_unmarked(self, text: str) -> None:
        """İşaretsiz başlık ("Yürürlük", "Yürütme") yığını sıfırlayıp tepeye yazılır.

        Bu başlıklar kanunun son hükümlerinde geçer ve bir üst hiyerarşiye bağlı
        değildir; önceki bölümün "G. > IV. > 2." zincirini korumak yanlış bağlam
        üretirdi.
        """
        self.levels = {self.LETTER: text}

    def reset(self) -> None:
        self.levels.clear()

    @property
    def en_ozel(self) -> str:
        return self.levels[max(self.levels)] if self.levels else ""

    @property
    def yol(self) -> str:
        return " > ".join(self.levels[k] for k in sorted(self.levels))


# --------------------------------------------------------------------------
# Ana ayrıştırıcı
# --------------------------------------------------------------------------
def parse_kanun(raw: bytes, kanun_meta: dict, kaynak_url: str = "") -> ParsedKanun:
    blocks = html_to_blocks(raw)
    plain = " ".join(b.text for b in blocks[:40])

    parsed = ParsedKanun(
        kanun_adi=kanun_meta["kanun_adi"],
        kanun_no=kanun_meta["kanun_no"],
        kisaltma=kanun_meta["kisaltma"],
        kaynak_url=kaynak_url,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    if m := RE_META["kabul_tarihi"].search(plain):
        parsed.kabul_tarihi = m.group(1)
    if m := RE_META["resmi_gazete"].search(plain):
        parsed.resmi_gazete = f"{m.group(1)} tarihli, {m.group(2)} sayılı Resmî Gazete"

    # Yapısal seviyeler yerel tutulur: modül seviyesinde saklansaydı aynı süreçte
    # ikinci bir kanun ayrıştırılırken önceki kanunun bölümü sızardı.
    levels: dict[str, str] = {"KİTAP": "", "KISIM": "", "BÖLÜM": "", "AYIRIM": ""}
    _ORDER = ("KİTAP", "KISIM", "BÖLÜM", "AYIRIM")
    _CANON = {"AYRIM": "AYIRIM", "FASIL": "BÖLÜM"}

    def set_level(level: str, value: str) -> None:
        # Üst seviye değişince alt seviyeler sıfırlanır; aksi halde önceki
        # bölümün AYIRIM'ı yeni bölüme taşınır ve atıf bağlamı yanlış olur.
        canon = _CANON.get(level, level)
        levels[canon] = value
        if canon in _ORDER:
            for lower in _ORDER[_ORDER.index(canon) + 1:]:
                levels[lower] = ""

    outline = OutlineStack()
    bekleyen_yapisal: tuple[str, str] | None = None   # (seviye, etiket)
    cur: Madde | None = None
    not_etiketi = ""
    sira = 0

    def finalize() -> None:
        nonlocal cur
        if cur is not None and cur.metin:
            parsed.maddeler.append(cur)
        cur = None

    for blk in blocks:
        text = blk.text

        # --- Ana metnin sonu ---
        if RE_TABLO_BASI.search(text) or RE_DIPNOT_TANIMI.match(blk.text_raw):
            break
        if RE_ISLENEMEYEN.search(text):
            finalize()
            outline.reset()
            not_etiketi = ISLENEMEYEN_ETIKET
            continue

        # --- Yapısal başlığın adı (bir önceki blok "ÜÇÜNCÜ BÖLÜM" idi) ---
        if bekleyen_yapisal is not None:
            seviye, etiket = bekleyen_yapisal
            bekleyen_yapisal = None
            # Ad gelmediyse (doğrudan madde başladıysa) etiketi tek başına yaz
            if not (RE_MADDE.match(text) or RE_STRUCT.match(text)):
                set_level(seviye, f"{etiket} {text}")
                outline.reset()
                continue
            set_level(seviye, etiket)
            outline.reset()

        # --- Yapısal başlık (KISIM / BÖLÜM / AYIRIM ...) ---
        if sm := RE_STRUCT.match(text):
            finalize()
            outline.reset()
            seviye = sm.group(1)
            etiket = text[: sm.end()].strip()
            inline_ad = text[sm.end():].strip(" -–—")
            if inline_ad:
                set_level(seviye, f"{etiket} {inline_ad}")
            else:
                bekleyen_yapisal = (seviye, etiket)
            continue

        # --- MADDE başlangıcı ---
        if mm := RE_MADDE.match(text):
            finalize()
            tur_kelime = re.sub(r"\s+", " ", mm.group(1)).upper()
            no = mm.group(2)
            if harf := mm.group(3):
                no = f"{no}/{harf.upper()}"
            if tur_kelime.startswith("GEÇİCİ"):
                madde_turu, madde_no = "gecici", f"GEÇİCİ {no}"
            elif tur_kelime.startswith("EK"):
                madde_turu, madde_no = "ek", f"EK {no}"
            else:
                madde_turu, madde_no = "madde", no

            sira += 1
            cur = Madde(
                madde_no=madde_no,
                madde_turu=madde_turu,
                kenar_baslik=outline.en_ozel,
                konu_yolu=outline.yol,
                kitap=levels["KİTAP"], kisim=levels["KISIM"],
                bolum=levels["BÖLÜM"], ayirim=levels["AYIRIM"],
                not_etiketi=not_etiketi,
                sira=sira,
            )
            if rest := text[mm.end():].strip():
                cur.fikralar.append(rest)
            continue

        # --- Kenar başlığı (<hN>, "MsoTitle"/"Stil..." sınıfı ya da ortalanmış) ---
        if blk.baslik_mi:
            if om := RE_OUTLINE.match(text):
                outline.push(om.group("marker"), text)
            else:
                outline.push_unmarked(text)
            continue

        # --- Gövde: her <p> bloğu bir fıkra ---
        if cur is not None:
            cur.fikralar.append(text)

    finalize()
    return parsed


# --------------------------------------------------------------------------
# Madde -> Chunk
# --------------------------------------------------------------------------
@dataclass
class Chunk:
    """Veritabanına yazılacak ve gömülecek (embed) en küçük birim."""

    kanun_adi: str
    kanun_no: str
    kisaltma: str
    madde_no: str
    madde_turu: str
    kenar_baslik: str
    konu_yolu: str
    kitap: str
    kisim: str
    bolum: str
    ayirim: str
    atif: str            # "TBK m. 27" — cevapta gösterilecek resmî atıf
    icerik: str          # saf madde metni (kullanıcıya gösterilen)
    gomme_metni: str     # bağlam başlığı + metin (embedding'e verilen)
    parca_no: int = 1
    parca_toplam: int = 1
    not_etiketi: str = ""
    sira: int = 0


def _atif_kur(kanun: ParsedKanun, m: Madde) -> str:
    """Maddenin resmî atfını üretir. İşlenemeyen hükümler ayrı etiketlenir."""
    if m.not_etiketi == ISLENEMEYEN_ETIKET:
        tur = "Geçici m." if m.madde_turu == "gecici" else "m."
        no = m.madde_no.replace("GEÇİCİ ", "").replace("EK ", "")
        return f"{kanun.kisaltma}'ya işlenemeyen hüküm — {tur} {no}"
    return f"{kanun.kisaltma} m. {m.madde_no}"


def _baglam_basligi(kanun: ParsedKanun, m: Madde, atif: str) -> str:
    """Parçanın başına eklenen kısa bağlam (kanun adı, kenar başlık)."""
    satirlar = [f"{kanun.kanun_adi} ({kanun.kanun_no}) — {atif}"]
    if m.kenar_baslik:
        satirlar.append(f"Kenar başlığı: {m.kenar_baslik}")
    if m.konu_yolu and m.konu_yolu != m.kenar_baslik:
        satirlar.append(f"Konu: {m.konu_yolu}")
    if yol := m.baglam_yolu():
        satirlar.append(f"Bağlam: {yol}")
    if m.not_etiketi:
        satirlar.append(f"Not: {m.not_etiketi}")
    return "\n".join(satirlar)


def _fikralari_bol(fikralar: list[str], limit: int, overlap: int) -> list[str]:
    """Uzun maddeyi fıkra sınırından böler; cümle ortasından asla bölmez."""
    parcalar: list[str] = []
    tampon: list[str] = []
    uzunluk = 0
    for f in fikralar:
        # Tek bir fıkra bile limitten uzunsa mecburen cümle sınırından böl
        if len(f) > limit:
            if tampon:
                parcalar.append("\n".join(tampon))
                tampon, uzunluk = [], 0
            alt: list[str] = []
            alt_uz = 0
            for c in re.split(r"(?<=[.!?])\s+", f):
                if alt and alt_uz + len(c) > limit:
                    parcalar.append(" ".join(alt))
                    alt, alt_uz = [], 0
                alt.append(c)
                alt_uz += len(c) + 1
            if alt:
                parcalar.append(" ".join(alt))
            continue
        if tampon and uzunluk + len(f) > limit:
            parcalar.append("\n".join(tampon))
            # Bağlamı korumak için son fıkranın kuyruğunu taşı
            kuyruk = tampon[-1][-overlap:] if overlap else ""
            tampon = [kuyruk] if kuyruk else []
            uzunluk = len(kuyruk)
        tampon.append(f)
        uzunluk += len(f) + 1
    if tampon:
        parcalar.append("\n".join(tampon))
    return [p.strip() for p in parcalar if p.strip()]


def maddeleri_parcala(
    kanun: ParsedKanun,
    limit: int = config.MAX_CHUNK_CHARS,
    overlap: int = config.CHUNK_OVERLAP_CHARS,
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for m in kanun.maddeler:
        atif = _atif_kur(kanun, m)
        baslik = _baglam_basligi(kanun, m, atif)
        parcalar = _fikralari_bol(m.fikralar, limit, overlap) or [m.metin]
        for i, p in enumerate(parcalar, start=1):
            chunks.append(
                Chunk(
                    kanun_adi=kanun.kanun_adi,
                    kanun_no=kanun.kanun_no,
                    kisaltma=kanun.kisaltma,
                    madde_no=m.madde_no,
                    madde_turu=m.madde_turu,
                    kenar_baslik=m.kenar_baslik,
                    konu_yolu=m.konu_yolu,
                    kitap=m.kitap, kisim=m.kisim, bolum=m.bolum, ayirim=m.ayirim,
                    atif=atif if len(parcalar) == 1 else f"{atif} ({i}/{len(parcalar)})",
                    icerik=p,
                    gomme_metni=f"{baslik}\n---\n{p}",
                    parca_no=i,
                    parca_toplam=len(parcalar),
                    not_etiketi=m.not_etiketi,
                    sira=m.sira,
                )
            )
    return chunks
