"""Arama isabeti ve cevap kalitesi ölçümü.

    ./.venv/bin/python -m tests.evaluate --sadece-arama
    ./.venv/bin/python -m tests.evaluate --rapor reports/test_sonuclari.json
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config  # noqa: E402

EVAL_DOSYASI = Path(__file__).resolve().parent / "eval_set.yaml"


@dataclass
class SoruSonucu:
    id: str
    soru: str
    tur: str
    konu: str = ""
    beklenen_maddeler: list[str] = field(default_factory=list)
    getirilen_maddeler: list[str] = field(default_factory=list)
    ilk_isabet_sirasi: int | None = None      # 1 tabanlı; yoksa None
    hit1: bool = False
    hit3: bool = False
    hit5: bool = False
    rr: float = 0.0                            # 1 / ilk_isabet_sirasi
    en_yuksek_benzerlik: float = 0.0
    alakali_bulundu: bool = True               # alaka eşiğinin üstünde mi
    baglam_madde_sayisi: int = 0               # modele gerçekten verilen madde
    arama_ms: float = 0.0
    # --- tam kip alanları ---
    cevap: str = ""
    atiflar: list[str] = field(default_factory=list)
    dayanaksiz_atiflar: list[str] = field(default_factory=list)
    beklenen_madde_atif_yapildi: bool | None = None
    reddetti: bool | None = None               # kapsam dışı sorularda
    basarili: bool | None = None
    hata: str = ""                             # üretim hatası (varsa)
    uretim_ms: float = 0.0
    otomatik_atif: bool = False                # atıf güvenlik ağı ekledi


def _isabet_hesapla(sonuc: SoruSonucu, getirilen: list[str]) -> None:
    sonuc.getirilen_maddeler = getirilen
    beklenen = {m.upper() for m in sonuc.beklenen_maddeler}
    for sira, madde in enumerate(getirilen, start=1):
        if madde.upper() in beklenen:
            sonuc.ilk_isabet_sirasi = sira
            sonuc.rr = 1.0 / sira
            break
    s = sonuc.ilk_isabet_sirasi
    sonuc.hit1 = s == 1
    sonuc.hit3 = s is not None and s <= 3
    sonuc.hit5 = s is not None and s <= 5


def degerlendir(sadece_arama: bool = False, top_k: int = 5, sessiz: bool = False) -> dict:
    veri = yaml.safe_load(EVAL_DOSYASI.read_text(encoding="utf-8"))
    sorular = veri["sorular"]

    from src.answer import HukukAsistani

    asistan = HukukAsistani()
    sonuclar: list[SoruSonucu] = []

    for i, s in enumerate(sorular, start=1):
        sonuc = SoruSonucu(
            id=s["id"],
            soru=s["soru"],
            tur=s["tur"],
            konu=s.get("konu", ""),
            beklenen_maddeler=[str(m) for m in s.get("beklenen_maddeler", [])],
        )

        if sadece_arama:
            t0 = time.perf_counter()
            arama = asistan.ara(s["soru"], top_k=top_k)
            sonuc.arama_ms = (time.perf_counter() - t0) * 1000
            _isabet_hesapla(sonuc, [p.madde_no for p in arama.parcalar])
            sonuc.en_yuksek_benzerlik = round(arama.en_yuksek_benzerlik, 4)
            sonuc.alakali_bulundu = arama.alakali
            sonuc.baglam_madde_sayisi = len(arama.baglam_parcalari())
        else:
            # Kullanıcının gördüğü yol: HukukAsistani.cevapla (temizleme +
            # atıf güvenlik ağı dahil). Tek sorunun hatası koşuyu düşürmez.
            try:
                c = asistan.cevapla(s["soru"], top_k=top_k)
            except Exception as exc:
                sonuc.hata = f"{type(exc).__name__}: {exc}"
                sonuc.basarili = False
                sonuclar.append(sonuc)
                if not sessiz:
                    print(f"[{i:2}/{len(sorular)}] ! {sonuc.id} HATA: {sonuc.hata[:70]}")
                continue
            sonuc.arama_ms = c.arama_ms
            _isabet_hesapla(sonuc, [p.madde_no for p in c.parcalar])
            sonuc.en_yuksek_benzerlik = round(c.en_yuksek_benzerlik, 4)
            sonuc.alakali_bulundu = not c.kapsam_disi
            sonuc.baglam_madde_sayisi = len(c.baglam_parcalari)
            sonuc.uretim_ms = c.uretim_ms
            sonuc.cevap = c.metin
            sonuc.atiflar = c.atiflar
            sonuc.dayanaksiz_atiflar = c.dayanaksiz_atiflar
            sonuc.otomatik_atif = c.otomatik_atif

            if sonuc.tur == "kapsam_disi":
                sonuc.reddetti = c.bilmiyorum
                sonuc.basarili = c.bilmiyorum and not c.dayanaksiz_atiflar
            else:
                atifli_maddeler = {
                    a.split("m.")[-1].strip().upper() for a in c.atiflar
                }
                sonuc.beklenen_madde_atif_yapildi = bool(
                    atifli_maddeler & {m.upper() for m in sonuc.beklenen_maddeler}
                )
                sonuc.basarili = (
                    sonuc.beklenen_madde_atif_yapildi and not c.dayanaksiz_atiflar
                )

        sonuclar.append(sonuc)
        if not sessiz:
            isaret = "?" if sonuc.basarili is None else ("+" if sonuc.basarili else "-")
            konum = sonuc.ilk_isabet_sirasi or "-"
            print(
                f"[{i:2}/{len(sorular)}] {isaret} {sonuc.id} "
                f"(sıra {konum}) {sonuc.soru[:58]}"
            )

    return _ozetle(sonuclar, sadece_arama, top_k, asistan)


def _ozetle(sonuclar: list[SoruSonucu], sadece_arama: bool, top_k: int, asistan) -> dict:
    cevaplanabilir = [s for s in sonuclar if s.tur == "cevaplanabilir"]
    kapsam_disi = [s for s in sonuclar if s.tur == "kapsam_disi"]
    from src import config as _cfg

    def oran(sayi: int, toplam: int) -> float:
        return round(100.0 * sayi / toplam, 1) if toplam else 0.0

    ozet = {
        "olusturma": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kip": "sadece-arama" if sadece_arama else "tam",
        "top_k": top_k,
        "chat_modeli": getattr(asistan.saglayici, "chat_model", ""),
        "gomme_modeli": getattr(asistan.saglayici, "embed_model", ""),
        "saglayici": type(asistan.saglayici).__name__,
        "soru_sayisi": len(sonuclar),
        "ayarlar": {
            "aday_k": _cfg.CANDIDATE_K,
            "w_vektor": _cfg.W_VEKTOR,
            "w_fts": _cfg.W_FTS,
            "w_madde": _cfg.W_MADDE,
            "fts_df_limiti": _cfg.FTS_DF_LIMIT,
            "alaka_esigi": _cfg.RELEVANCE_MIN,
            "no_think": _cfg.NO_THINK,
        },
        "arama": {
            "cevaplanabilir_soru": len(cevaplanabilir),
            "hit@1": oran(sum(s.hit1 for s in cevaplanabilir), len(cevaplanabilir)),
            "hit@3": oran(sum(s.hit3 for s in cevaplanabilir), len(cevaplanabilir)),
            "hit@5": oran(sum(s.hit5 for s in cevaplanabilir), len(cevaplanabilir)),
            "MRR": round(
                statistics.mean([s.rr for s in cevaplanabilir]) if cevaplanabilir else 0.0,
                4,
            ),
            "kacirilan": [s.id for s in cevaplanabilir if s.ilk_isabet_sirasi is None],
            # Alaka eşiği kapsam dışı soruları doğru ayırabiliyor mu? Bu, LLM'e
            # hiç dokunmadan ölçülebilen bir kapsam kontrolüdür.
            "alaka_esigi_dogru_cevaplanabilir": oran(
                sum(s.alakali_bulundu for s in cevaplanabilir), len(cevaplanabilir)
            ),
            "alaka_esigi_dogru_kapsam_disi": oran(
                sum(not s.alakali_bulundu for s in kapsam_disi), len(kapsam_disi)
            ),
            "ortalama_ms": round(
                statistics.mean([s.arama_ms for s in sonuclar]) if sonuclar else 0.0, 1
            ),
            "medyan_ms": round(
                statistics.median([s.arama_ms for s in sonuclar]) if sonuclar else 0.0, 1
            ),
        },
    }

    if not sadece_arama:
        uretim = [s.uretim_ms for s in sonuclar if s.uretim_ms]
        ozet["cevap"] = {
            "dogru_atifli_cevap": oran(
                sum(bool(s.beklenen_madde_atif_yapildi) for s in cevaplanabilir),
                len(cevaplanabilir),
            ),
            "halusinasyon_atif_iceren_cevap": sum(
                1 for s in sonuclar if s.dayanaksiz_atiflar
            ),
            "kapsam_disi_soru": len(kapsam_disi),
            "kapsam_disi_ret_orani": oran(
                sum(bool(s.reddetti) for s in kapsam_disi), len(kapsam_disi)
            ),
            "genel_basari": oran(
                sum(bool(s.basarili) for s in sonuclar), len(sonuclar)
            ),
            "uretim_hatasi": [s.id for s in sonuclar if s.hata],
            "otomatik_atif_sayisi": sum(1 for s in cevaplanabilir if s.otomatik_atif),
            "uretim_ortalama_ms": round(statistics.mean(uretim), 1) if uretim else 0.0,
            "uretim_medyan_ms": round(statistics.median(uretim), 1) if uretim else 0.0,
        }

    return {"ozet": ozet, "sorular": [asdict(s) for s in sonuclar]}


def yazdir(rapor: dict) -> None:
    o = rapor["ozet"]
    print()
    print("=" * 66)
    print(f"DEĞERLENDİRME ÖZETİ  ({o['kip']} kip, top_k={o['top_k']})")
    print("=" * 66)
    print(f"Sağlayıcı  : {o['saglayici']}")
    print(f"Modeller   : chat={o['chat_modeli']}  gömme={o['gomme_modeli']}")
    print(f"Soru sayısı: {o['soru_sayisi']}")
    a = o["arama"]
    print()
    print("ARAMA (retrieval)")
    print(f"  cevaplanabilir soru : {a['cevaplanabilir_soru']}")
    print(f"  hit@1 / hit@3 / hit@5: {a['hit@1']}% / {a['hit@3']}% / {a['hit@5']}%")
    print(f"  MRR                 : {a['MRR']}")
    print(f"  kaçırılan           : {a['kacirilan'] or 'yok'}")
    print(f"  süre (ort/medyan)   : {a['ortalama_ms']} / {a['medyan_ms']} ms")
    print()
    print("ALAKA EŞİĞİ (LLM'e dokunmadan kapsam kontrolü)")
    print(f"  cevaplanabilir doğru sınıflandı : "
          f"{a['alaka_esigi_dogru_cevaplanabilir']}%")
    print(f"  kapsam dışı doğru sınıflandı    : "
          f"{a['alaka_esigi_dogru_kapsam_disi']}%")
    if c := o.get("cevap"):
        print()
        print("CEVAP (generation)")
        print(f"  doğru madde atıflı cevap : {c['dogru_atifli_cevap']}%")
        print(f"  halüsinasyon atıf içeren : {c['halusinasyon_atif_iceren_cevap']} cevap")
        print(f"  kapsam dışı ret oranı    : {c['kapsam_disi_ret_orani']}% "
              f"({c['kapsam_disi_soru']} soru)")
        print(f"  genel başarı             : {c['genel_basari']}%")
        print(f"  üretim hatası alan soru  : {c['uretim_hatasi'] or 'yok'}")
        print(f"  otomatik atıf (güvenlik) : {c.get('otomatik_atif_sayisi', 0)}")
        print(f"  üretim (ort/medyan)      : {c['uretim_ortalama_ms']} / "
              f"{c['uretim_medyan_ms']} ms")
    print("=" * 66)


def main() -> int:
    ap = argparse.ArgumentParser(description="Hukuk RAG asistanı değerlendirmesi")
    ap.add_argument("--sadece-arama", action="store_true",
                    help="LLM çağırmadan yalnızca arama isabetini ölç (hızlı)")
    ap.add_argument("--top-k", type=int, default=config.TOP_K)
    ap.add_argument("--rapor", type=Path, help="Sonuçları JSON olarak kaydet")
    ap.add_argument("--sessiz", action="store_true")
    args = ap.parse_args()

    rapor = degerlendir(
        sadece_arama=args.sadece_arama, top_k=args.top_k, sessiz=args.sessiz
    )
    yazdir(rapor)

    if args.rapor:
        args.rapor.parent.mkdir(parents=True, exist_ok=True)
        args.rapor.write_text(
            json.dumps(rapor, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\nRapor yazıldı: {args.rapor}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
