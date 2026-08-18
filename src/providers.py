"""Foundry Local: SDK (ana yol), HTTP ve geliştirme yedeği.

SDK `model_cache_dir` ister; Qwen3 için `/no_think` kullanılır.
`complete_chat` temperature/max_tokens almaz — gerekirse HTTP yolu.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable

from src import config

RE_THINK = re.compile(r"(?is)<think>.*?</think>\s*")
# Sorgulama komutları (`server status`, `cache location`) asılabildiği için kısa
# tutulur; bu komutlar hızlı cevap vermezse zaten güvenilmezdir.
CLI_TIMEOUT = 10.0
# Model yükleme MEŞRU olarak uzun sürer: ölçümde sohbet modeli 22-28 saniyede
# yüklendi. Buraya da 10 saniye verilirse yükleme her seferinde yarıda kesilir
# ve "model yüklü değil" hatası hiç düzelmez.
MODEL_LOAD_TIMEOUT = 180.0


class ModelYokHatasi(RuntimeError):
    """İstenen modellerin hiçbiri indirilmemiş."""


class ServisYokHatasi(RuntimeError):
    """Foundry Local bulunamadı ya da kullanılamıyor."""


# --------------------------------------------------------------------------
# Ortak arayüz
# --------------------------------------------------------------------------
@runtime_checkable
class Saglayici(Protocol):
    """answer.py'nin gördüğü sözleşme."""

    embed_model: str
    chat_model: str

    def embed(self, metinler: list[str]) -> list[list[float]]: ...
    def embed_tek(self, metin: str) -> list[float]: ...
    def chat(self, mesajlar: list[dict]) -> str: ...
    def chat_streaming(self, mesajlar: list[dict]) -> Iterator[str]: ...
    def teshis(self) -> dict: ...


def tekrar_dongusu(metin: str, pencere: int = 48, esik: int = 3) -> bool:
    """Aynı ifadenin döngüde tekrarlanıp tekrarlanmadığını kontrol eder."""
    kuyruk = metin[-pencere:]
    if len(kuyruk) < pencere:
        return False
    return metin.count(kuyruk) >= esik


class TemelSaglayici:
    """Ortak yardımcılar: tek metin gömme, akıştan tam cevap, /no_think eklemesi."""

    embed_model: str = ""
    chat_model: str = ""

    def embed(self, metinler: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError

    def embed_tek(self, metin: str) -> list[float]:
        return self.embed([metin])[0]

    def chat_streaming(self, mesajlar: list[dict]) -> Iterator[str]:
        yield self.chat(mesajlar)

    def chat(self, mesajlar: list[dict]) -> str:
        return self._temizle("".join(self.chat_streaming(mesajlar)))

    # ------------------------------------------------------------ yardımcılar
    @staticmethod
    def _no_think_ekle(mesajlar: list[dict]) -> list[dict]:
        """Her kullanıcı mesajının sonuna `/no_think` ekler."""
        if not config.NO_THINK:
            return mesajlar
        kopya = [dict(m) for m in mesajlar]
        for m in kopya:
            if m.get("role") == "user" and "/no_think" not in m.get("content", ""):
                m["content"] = f"{m['content']}\n/no_think"
        return kopya

    @staticmethod
    def _temizle(metin: str) -> str:
        if config.STRIP_THINK_BLOCKS:
            metin = RE_THINK.sub("", metin)
        return metin.strip()

    def teshis(self) -> dict:
        return {
            "saglayici": type(self).__name__,
            "chat_modeli": self.chat_model,
            "embed_modeli": self.embed_model,
        }


# --------------------------------------------------------------------------
# Yardımcı: model önbellek dizini ve katalog
# --------------------------------------------------------------------------
def _foundry_cli() -> str | None:
    if yol := shutil.which("foundry"):
        return yol
    varsayilan = Path("/usr/local/bin/foundry")
    return str(varsayilan) if varsayilan.exists() else None


def _cli_calistir(argumanlar: list[str], zaman_asimi: float = CLI_TIMEOUT) -> str | None:
    """CLI'yi çalıştırır; her hatayı yutar ve None döner.

    CLI'nin asılabildiği görüldüğü (`foundry model list` bir denemede 6 dakika
    yanıt vermedi) için zaman aşımı zorunludur.
    """
    cli = _foundry_cli()
    if not cli:
        return None
    try:
        sonuc = subprocess.run(
            [cli, *argumanlar], capture_output=True, text=True, timeout=zaman_asimi
        )
        return sonuc.stdout
    except (subprocess.SubprocessError, OSError):
        return None


def onbellek_dizini() -> str:
    """Model önbellek dizinini bulur.

    Önce `foundry cache location` denenir; CLI asılır veya yoksa
    yapılandırmadaki varsayılana (~/.foundry/cache/models) düşülür.
    """
    if cikti := _cli_calistir(["cache", "location"]):
        for satir in cikti.splitlines():
            aday = satir.strip().strip('"')
            # Çıktı "Location  /path" biçiminde de gelebiliyor
            if m := re.search(r"(/[^\s\"]+)", aday):
                yol = Path(m.group(1))
                if yol.exists():
                    return str(yol)
    return config.MODEL_CACHE_DIR


def indirilmis_modeller(manager) -> dict[str, dict]:
    """Gerçekten İNDİRİLMİŞ modelleri döndürür: {alias: bilgi}.

    Kaynak olarak SDK'nın `catalog.get_cached_models()` metodu kullanılır; tek
    güvenilir kaynak budur. Önbellek dizinindeki `foundry.modelinfo.json`
    dosyası bu iş için UYGUN DEĞİLDİR: o dosya indirilmiş modelleri değil
    KATALOĞUN TAMAMINI (130+ kayıt) listeler. Ona bakarak model seçilirse
    indirilmemiş bir model seçilir ve `load()` çağrısı ya koca bir indirme
    başlatır ya da hata verir.
    """
    try:
        varyantlar = manager.catalog.get_cached_models() or []
    except Exception:
        return {}

    harita: dict[str, dict] = {}
    for v in varyantlar:
        bilgi = {
            "id": getattr(v, "id", ""),
            "alias": getattr(v, "alias", ""),
            "capabilities": getattr(v, "capabilities", ""),
            "context_length": getattr(v, "context_length", 0),
            "is_loaded": getattr(v, "is_loaded", False),
        }
        for anahtar in (bilgi["alias"], bilgi["id"]):
            if anahtar:
                harita.setdefault(str(anahtar), bilgi)
    return harita


# --------------------------------------------------------------------------
# 1) SDK sağlayıcısı — ANA YOL
# --------------------------------------------------------------------------
# SDK'nın initialize() metodu statik ve süreç genelinde tek seferliktir; ayrıca
# model yükleme 6-28 saniye sürdüğü için yüklenen modelleri süreç ömrü boyunca
# saklıyoruz. Modül düzeyindeki bu önbellek, birden fazla Retriever/CLI çağrısı
# olsa bile modelin bir kez yüklenmesini garanti eder.
_sdk_manager = None
_sdk_modeller: dict[str, object] = {}
_sdk_istemciler: dict[str, object] = {}


def _sdk_manager_al():
    global _sdk_manager
    if _sdk_manager is not None:
        return _sdk_manager
    try:
        from foundry_local_sdk import Configuration, FoundryLocalManager  # type: ignore
    except ImportError as exc:
        raise ServisYokHatasi(
            "foundry-local-sdk kurulu değil. Kurmak için:\n"
            "  ./.venv/bin/pip install foundry-local-sdk"
        ) from exc

    cache = onbellek_dizini()
    try:
        FoundryLocalManager.initialize(
            Configuration(app_name="hukuk_rag", model_cache_dir=cache)
        )
        _sdk_manager = FoundryLocalManager.instance
    except Exception as exc:  # SDK kendi istisna tipini kullanıyor
        raise ServisYokHatasi(f"Foundry Local SDK başlatılamadı: {exc}") from exc
    if _sdk_manager is None:
        raise ServisYokHatasi("Foundry Local SDK başlatıldı ama instance alınamadı.")
    return _sdk_manager


@dataclass
class FoundrySdkProvider(TemelSaglayici):
    """foundry-local-sdk üzerinden embedding ve sohbet.

    `complete_chat` temperature/max_tokens almaz. Sistem mesajı modele
    ulaşmıyor; ilk kullanıcı turuna katlanır (`_sistemi_katla`).
    """

    chat_model: str = ""
    embed_model: str = ""

    def __post_init__(self) -> None:
        self.manager = _sdk_manager_al()
        if not self.embed_model:
            self.embed_model = self._model_sec(config.EMBEDDING_MODEL_CANDIDATES, "Embedding")
        if not self.chat_model:
            self.chat_model = self._model_sec(config.CHAT_MODEL_CANDIDATES, "Sohbet")

    # ------------------------------------------------------------ model seçimi
    def _model_sec(self, adaylar: list[str], tur: str) -> str:
        """Aday listesinden İNDİRİLMİŞ ilk modeli seçer.

        Katalogda var olmak yetmez: indirilmemiş bir model seçilirse `load()`
        gigabaytlarca indirme başlatır. Bu yüzden yalnızca indirilmiş modellere
        bakılır ve hiçbiri yoksa kullanıcıya indirme komutu söylenir.
        """
        indirilmis = indirilmis_modeller(self.manager)
        for aday in adaylar:
            if aday in indirilmis:
                return aday
        raise ModelYokHatasi(
            f"{tur} modeli indirilmemiş. Denenenler: {', '.join(adaylar)}\n"
            f"İndirilmiş olanlar: "
            f"{', '.join(sorted(a for a in indirilmis if ':' not in a)) or '(yok)'}\n"
            f"İndirmek için: foundry model download {adaylar[0]}"
        )

    def _model_yukle(self, alias: str):
        """Modeli (bir kez) yükler ve süreç ömrü boyunca saklar."""
        if alias in _sdk_modeller:
            return _sdk_modeller[alias]
        model = self.manager.catalog.get_model(alias)
        if model is None:
            raise ModelYokHatasi(f"'{alias}' katalogda bulunamadı.")
        model.load()
        _sdk_modeller[alias] = model
        return model

    def _embedding_istemcisi(self):
        if "embed" not in _sdk_istemciler:
            model = self._model_yukle(self.embed_model)
            _sdk_istemciler["embed"] = model.get_embedding_client()
        return _sdk_istemciler["embed"]

    def _chat_istemcisi(self):
        if "chat" not in _sdk_istemciler:
            model = self._model_yukle(self.chat_model)
            _sdk_istemciler["chat"] = model.get_chat_client()
        return _sdk_istemciler["chat"]

    # ---------------------------------------------------------------- embedding
    def embed(self, metinler: list[str]) -> list[list[float]]:
        if not metinler:
            return []
        cevap = self._embedding_istemcisi().generate_embeddings(metinler)
        sirali = sorted(cevap.data, key=lambda d: getattr(d, "index", 0))
        return [list(d.embedding) for d in sirali]

    # --------------------------------------------------------------------- chat
    @staticmethod
    def _sistemi_katla(mesajlar: list[dict]) -> list[dict]:
        """`system` mesajlarını ilk kullanıcı turunun başına taşır.

        SDK'nın sohbet istemcisi sistem rolünü modele iletmiyor (bkz. sınıf
        docstring'i). Talimatlar kaybolduğunda model, bağlam bloğunun biçimini
        sürdürüp var olmayan maddeler üretiyor: bir denemede TBK m. 585-593
        arasını uydurup her birine kısa bir "hüküm" yazdı. Talimatları kullanıcı
        turuna katlamak bu davranışı ortadan kaldırıyor.
        """
        sistemler = [m["content"] for m in mesajlar if m.get("role") == "system"]
        if not sistemler:
            return mesajlar
        kalan = [dict(m) for m in mesajlar if m.get("role") != "system"]
        onek = "\n\n".join(sistemler)
        for m in kalan:
            if m.get("role") == "user":
                m["content"] = f"{onek}\n\n---\n\n{m['content']}"
                return kalan
        return [{"role": "user", "content": onek}, *kalan]

    def chat_streaming(self, mesajlar: list[dict]) -> Iterator[str]:
        """Akışı üretir ve BÜTÇE DOLUNCA KESER.

        Kesme burada bir zarafet değil, zorunluluk: SDK `max_tokens` almadığı
        için modelin tekrar döngüsüne girmesini durduracak başka bir kaldıraç
        yok. Bütçesiz hâlde ölçülen sonuç, üretimin ~5 dakika sürüp servisin iç
        iptaline (`Operation was cancelled`) takılması ve cevabın tamamen
        kaybedilmesiydi. Kısalmış cevap, kayıp cevaptan iyidir.

        Üretici `close()` ile kapatılır; aksi hâlde servis arkada üretmeye devam
        eder ve sıradaki isteği yavaşlatır.
        """
        istemci = self._chat_istemcisi()
        hazir = self._no_think_ekle(self._sistemi_katla(mesajlar))
        akis = istemci.complete_streaming_chat(hazir)
        baslangic = time.monotonic()
        uretilen = 0
        tampon: list[str] = []
        try:
            for parca in akis:
                if not getattr(parca, "choices", None):
                    continue
                if icerik := getattr(parca.choices[0].delta, "content", None):
                    uretilen += len(icerik)
                    tampon.append(icerik)
                    yield icerik
                if (
                    uretilen >= config.CHAT_KARAKTER_BUTCESI
                    or time.monotonic() - baslangic > config.CHAT_SURE_SINIRI
                    or tekrar_dongusu("".join(tampon))
                ):
                    break
        finally:
            if kapat := getattr(akis, "close", None):
                try:
                    kapat()
                except Exception:
                    pass

    def chat(self, mesajlar: list[dict]) -> str:
        """Akışı toplayıp tam cevabı döndürür; iptal hâlinde bir kez yeniler.

        Bütçe kesmesi iptalleri büyük ölçüde ortadan kaldırdı; yine de üretim
        başlamadan (ön dolum sırasında) iptal gelebildiği için tek bir yeniden
        deneme korunuyor.
        """
        son_hata: Exception | None = None
        for deneme in range(2):
            uretilen: list[str] = []
            try:
                for parca in self.chat_streaming(mesajlar):
                    uretilen.append(parca)
                return self._temizle("".join(uretilen))
            except Exception as exc:   # SDK kendi istisna tipini kullanıyor
                son_hata = exc
                if "cancel" not in str(exc).lower():
                    raise
                # İptal, üretimin ortasında gelmişse eldeki metin çöpe atılmaz:
                # atıflarıyla birlikte kullanılabilir bir cevap olabilir. Kesik
                # olduğu açıkça işaretlenir; kullanıcı tam cevap sanmasın.
                kismi = self._temizle("".join(uretilen))
                if len(kismi) >= 120:
                    return f"{kismi}\n\n[uyarı: cevap üretim sırasında kesildi]"
                if deneme == 0:
                    time.sleep(2)
        raise RuntimeError(
            f"Model iki denemede de cevap üretemedi (istek iptal edildi): {son_hata}"
        ) from son_hata

    def teshis(self) -> dict:
        indirilmis = indirilmis_modeller(self.manager)
        d = super().teshis()
        d.update(
            {
                "onbellek_dizini": onbellek_dizini(),
                "indirilmis_modeller": {
                    a: b["id"] for a, b in sorted(indirilmis.items()) if ":" not in a
                },
                "not": "SDK yolu sıcaklık/token limiti geçirmeyi desteklemez.",
            }
        )
        return d


# --------------------------------------------------------------------------
# 2) HTTP sağlayıcısı — ikincil yol
# --------------------------------------------------------------------------
def _http_adres_cli() -> str | None:
    """`foundry server status` çıktısından servis adresini okur."""
    if cikti := _cli_calistir(["server", "status"]):
        if m := re.search(r"https?://[\d.]+:\d+", cikti):
            return m.group(0)
    return None


def _http_adres_tara() -> str | None:
    for port in config.FOUNDRY_PROBE_PORTS:
        adres = f"http://127.0.0.1:{port}"
        try:
            with urllib.request.urlopen(f"{adres}/v1/models", timeout=2):
                return adres
        except Exception:
            continue
    return None


def http_adres_bul() -> str:
    if config.FOUNDRY_ENDPOINT:
        return config.FOUNDRY_ENDPOINT.rstrip("/").removesuffix("/v1")
    for strateji in (_http_adres_cli, _http_adres_tara):
        if adres := strateji():
            return adres
    raise ServisYokHatasi(
        "Foundry Local HTTP servisi bulunamadı.\n"
        "  1) Kurulu mu?    foundry --version\n"
        "  2) Çalışıyor mu? foundry server start\n"
        "  3) Adresi elle ver: HUKUK_FOUNDRY_ENDPOINT=http://127.0.0.1:PORT"
    )


@dataclass
class FoundryHttpProvider(TemelSaglayici):
    """Foundry Local'in OpenAI uyumlu /v1 servisi üzerinden çalışır.

    NE ZAMAN TERCİH EDİLİR: SDK yolu `temperature` ve `max_tokens`
    geçirmeye izin vermiyor. Hukuk cevabında belirlenimcilik için sıcaklığı
    düşük sabitlemek istediğimizde bu yol kullanılır (HUKUK_PROVIDER=http).

    Kısıt: servisin portu her başlatmada değişir; adres CLI çıktısından ya da
    port taramasıyla bulunur.
    """

    adres: str = ""
    chat_model: str = ""
    embed_model: str = ""
    zaman_asimi: float = config.FOUNDRY_TIMEOUT
    _yuklenenler: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.adres = (self.adres or http_adres_bul()).rstrip("/")

    # ---------------------------------------------------------------- HTTP
    def _istek(self, yol: str, govde: dict | None = None, zaman_asimi: float | None = None) -> dict:
        veri = json.dumps(govde).encode("utf-8") if govde is not None else None
        req = urllib.request.Request(
            f"{self.adres}{yol}",
            data=veri,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST" if veri else "GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=zaman_asimi or self.zaman_asimi) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            govde_metni = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Foundry Local hatası ({exc.code}): {govde_metni[:400]}"
            ) from exc

    def katalog(self) -> dict[str, str]:
        """Servisin sunduğu modeller: {takma_ad: varyant_kimliği}.

        Foundry kimlikleri varyantlaştırıyor ("qwen3-4b" ->
        "qwen3-4b-generic-gpu"); takma adla eşleme bu yüzden gerekli.
        """
        veri = self._istek("/v1/models", zaman_asimi=30)
        harita: dict[str, str] = {}
        for m in veri.get("data", []):
            kimlik = m.get("id", "")
            harita.setdefault(m.get("parent") or kimlik, kimlik)
            harita.setdefault(kimlik, kimlik)
        return harita

    def _model_sec(self, adaylar: list[str], tur: str) -> str:
        harita = self.katalog()
        for aday in adaylar:
            if aday in harita:
                return aday
        raise ModelYokHatasi(
            f"{tur} modeli bulunamadı. Denenenler: {', '.join(adaylar)}\n"
            f"Serviste olanlar: {', '.join(sorted(harita)) or '(yok)'}\n"
            f"İndirmek için: foundry model download {adaylar[0]}"
        )

    def chat_modeli(self) -> str:
        if not self.chat_model:
            self.chat_model = self._model_sec(config.CHAT_MODEL_CANDIDATES, "Sohbet")
        return self.chat_model

    def embed_modeli(self) -> str:
        if not self.embed_model:
            self.embed_model = self._model_sec(config.EMBEDDING_MODEL_CANDIDATES, "Embedding")
        return self.embed_model

    def _yukleyip_yeniden_dene(self, model: str, cagri):
        """İlk çağrı 'model yüklü değil' derse modeli yükleyip bir kez yeniler.

        Modelleri peşin yüklemek yerine bu yol seçildi: model zaten yüklüyse
        gereksiz CLI çağrısı ve gecikme olmuyor.
        """
        try:
            return cagri()
        except RuntimeError as exc:
            if "not loaded" not in str(exc).lower():
                raise
            if model in self._yuklenenler:
                raise
            _cli_calistir(["model", "load", model], zaman_asimi=MODEL_LOAD_TIMEOUT)
            self._yuklenenler.add(model)
            return cagri()

    def embed(self, metinler: list[str]) -> list[list[float]]:
        if not metinler:
            return []
        model = self.embed_modeli()

        def cagri():
            veri = self._istek("/v1/embeddings", {"model": model, "input": metinler})
            sirali = sorted(veri["data"], key=lambda d: d.get("index", 0))
            return [d["embedding"] for d in sirali]

        return self._yukleyip_yeniden_dene(model, cagri)

    def chat(
        self,
        mesajlar: list[dict],
        sicaklik: float = config.TEMPERATURE,
        max_token: int = config.MAX_TOKENS,
    ) -> str:
        model = self.chat_modeli()

        def cagri():
            veri = self._istek(
                "/v1/chat/completions",
                {
                    "model": model,
                    "messages": self._no_think_ekle(mesajlar),
                    "temperature": sicaklik,
                    "max_tokens": max_token,
                    "stream": False,
                },
            )
            return veri["choices"][0]["message"]["content"] or ""

        return self._temizle(self._yukleyip_yeniden_dene(model, cagri))

    def teshis(self) -> dict:
        d = super().teshis()
        d.update(
            {
                "adres": self.adres,
                "servisteki_modeller": sorted(
                    k for k in self.katalog() if not k.endswith(("-gpu", "-cpu"))
                ),
                "chat_modeli": self.chat_modeli(),
                "embed_modeli": self.embed_modeli(),
                "not": "Sıcaklık ve token limiti bu yolda geçirilebilir.",
            }
        )
        return d


# --------------------------------------------------------------------------
# 3) Yedek sağlayıcı
# --------------------------------------------------------------------------
@dataclass
class DevProvider(TemelSaglayici):
    """Foundry yokken boru hattını çalıştırmak için yedek. Ölçüm için değil."""

    boyut: int = 512
    chat_model: str = "dev-fallback"
    embed_model: str = "dev-hash-3gram"

    def embed(self, metinler: list[str]) -> list[list[float]]:
        import hashlib
        import math

        from src.turkish import normalize

        cikti: list[list[float]] = []
        for metin in metinler:
            norm = normalize(metin)
            vec = [0.0] * self.boyut
            for i in range(max(len(norm) - 2, 1)):
                gram = norm[i : i + 3]
                h = int.from_bytes(
                    hashlib.blake2b(gram.encode(), digest_size=4).digest(), "big"
                )
                vec[h % self.boyut] += 1.0
            uzunluk = math.sqrt(sum(v * v for v in vec)) or 1.0
            cikti.append([v / uzunluk for v in vec])
        return cikti

    def chat(self, mesajlar: list[dict]) -> str:
        kullanici = next(
            (m["content"] for m in reversed(mesajlar) if m.get("role") == "user"), ""
        )
        return (
            "[YEDEK SAĞLAYICI — Foundry Local bulunamadı, metin üretimi yapılmadı]\n\n"
            "Aşağıda soruyla en ilgili bulunan mevzuat hükümleri ham hâliyle "
            "listelenmiştir; yorum ve özet içermez.\n\n" + kullanici
        )


# --------------------------------------------------------------------------
# Seçim
# --------------------------------------------------------------------------
_SAGLAYICILAR = {
    "sdk": FoundrySdkProvider,
    "http": FoundryHttpProvider,
    "dev": DevProvider,
}


def saglayici_al(sira: list[str] | None = None, sessiz: bool = False):
    """Yapılandırmadaki sıraya göre ilk çalışan sağlayıcıyı döndürür."""
    sira = sira or config.PROVIDER_ORDER
    hatalar: list[str] = []
    for ad in sira:
        sinif = _SAGLAYICILAR.get(ad)
        if sinif is None:
            hatalar.append(f"{ad}: bilinmeyen sağlayıcı")
            continue
        if ad == "dev" and not config.ALLOW_DEV_FALLBACK:
            continue
        try:
            saglayici = sinif()
            if ad == "http":
                saglayici.katalog()   # servis gerçekten cevap veriyor mu
            return saglayici
        except Exception as exc:
            hatalar.append(f"{ad}: {exc}")
            if not sessiz:
                print(f"[uyarı] '{ad}' sağlayıcısı kullanılamadı: {exc}")
    raise ServisYokHatasi(
        "Hiçbir sağlayıcı kullanılamadı:\n  " + "\n  ".join(hatalar)
    )


def bekle_hazir(saniye: float = 60.0) -> bool:
    """Sağlayıcı hazır olana kadar bekler (model yüklemesi sonrası için)."""
    bitis = time.time() + saniye
    while time.time() < bitis:
        try:
            saglayici_al(sessiz=True)
            return True
        except Exception:
            time.sleep(2)
    return False
