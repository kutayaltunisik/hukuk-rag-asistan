"""Komut satırı arayüzü.

    ./.venv/bin/python -m src.cli sor "Kira artış sınırı nedir?"
    ./.venv/bin/python -m src.cli ara "zamanaşımı"      # yalnızca arama, LLM yok
    ./.venv/bin/python -m src.cli sohbet                # etkileşimli kip
    ./.venv/bin/python -m src.cli kontrol               # hızlı sağlık kontrolü
    ./.venv/bin/python -m src.cli madde 344             # maddeyi olduğu gibi göster
"""

from __future__ import annotations

import argparse
import json
import sys

from src import config, db


def _renk(metin: str, kod: str) -> str:
    """ANSI renk. Çıktı bir dosyaya yönlendirilmişse kaçış kodu üretilmez."""
    if not sys.stdout.isatty():
        return metin
    return f"\033[{kod}m{metin}\033[0m"


kalin = lambda s: _renk(s, "1")
mavi = lambda s: _renk(s, "36")
yesil = lambda s: _renk(s, "32")
sari = lambda s: _renk(s, "33")
kirmizi = lambda s: _renk(s, "31")
gri = lambda s: _renk(s, "90")


def _kaynaklari_yaz(parcalar, ayrintili: bool = False, baglam=None) -> None:
    """Arama sonuçlarını yazar; modele gönderilmeyenleri işaretler."""
    baglam_idleri = {p.id for p in baglam} if baglam is not None else None
    print(kalin("\nKAYNAKLAR"))
    for i, p in enumerate(parcalar, 1):
        etiket = f"{i}. {mavi(p.atif)}"
        if p.kenar_baslik:
            etiket += f" — {p.kenar_baslik}"
        if baglam_idleri is not None and p.id not in baglam_idleri:
            etiket += sari("  [bağlama alınmadı: zayıf eşleşme]")
        print(f"  {etiket}")
        print(gri(
            f"     skor {p.skor:.5f} | benzerlik {p.benzerlik:.4f} "
            f"| kaynak: {', '.join(p.kaynaklar)}"
        ))
        if p.konu_yolu:
            print(gri(f"     {p.konu_yolu}"))
        if ayrintili:
            for satir in p.icerik.splitlines():
                print(gri(f"     | {satir}"))


def komut_sor(args) -> int:
    from src.answer import HukukAsistani

    asistan = HukukAsistani()
    print(gri(f"[{type(asistan.saglayici).__name__} | {asistan.saglayici.chat_model}]"))
    print(kalin(f"\nSORU: {args.soru}"))

    arama, akis = asistan.cevapla_akisli(args.soru, top_k=args.top_k)
    baglam = arama.baglam_parcalari()
    _kaynaklari_yaz(arama.parcalar, args.ayrintili, baglam)
    if not arama.alakali:
        print(sari(
            f"\n[kapsam uyarısı] En yüksek benzerlik "
            f"{arama.en_yuksek_benzerlik:.4f}, alaka eşiğinin "
            f"({config.RELEVANCE_MIN}) altında. Soru korpusun kapsamı dışında "
            f"görünüyor."
        ))

    print(kalin("\nCEVAP"))
    parcalar = []
    for parca in akis:
        parcalar.append(parca)
        print(parca, end="", flush=True)
    print()

    cevap = asistan.cevabi_sonlandir(args.soru, arama, "".join(parcalar))
    if cevap.otomatik_atif:
        print(gri("\n(atıf güvenlik ağı dayanak satırını ekledi)"))
        print(cevap.metin)

    if cevap.dayanaksiz_atiflar:
        print(kirmizi(
            f"\n[UYARI] Bağlamda bulunmayan atıf(lar): "
            f"{', '.join(cevap.dayanaksiz_atiflar)} — bu cevaba güvenmeyin."
        ))
    elif cevap.atiflar:
        print(yesil(f"\n[✓] Atıflar doğrulandı: {', '.join(cevap.atiflar)}"))

    from src.prompts import FERAGATNAME

    print(gri(f"\n{FERAGATNAME}"))
    return 0


def komut_ara(args) -> int:
    from src.answer import HukukAsistani

    asistan = HukukAsistani()
    sonuc = asistan.ara(args.sorgu, top_k=args.top_k)
    print(kalin(f"SORGU: {args.sorgu}"))
    print(gri(
        f"vektör {sonuc.vektor_adet} | kelime {sonuc.fts_adet} | "
        f"madde-no {sonuc.madde_adet} | "
        f"en yüksek benzerlik {sonuc.en_yuksek_benzerlik:.4f} "
        f"({'kapsam içi' if sonuc.alakali else 'KAPSAM DIŞI'})"
    ))
    if sonuc.fts_sorgusu:
        print(gri(f"FTS ifadesi: {sonuc.fts_sorgusu}"))
    _kaynaklari_yaz(sonuc.parcalar, args.ayrintili, sonuc.baglam_parcalari())
    return 0


def komut_madde(args) -> int:
    conn = db.baglan()
    parcalar = db.madde_ile_getir(conn, args.numara, args.kanun)
    if not parcalar:
        print(kirmizi(f"m. {args.numara} bulunamadı."))
        return 1
    for p in parcalar:
        print(kalin(f"{p.atif} — {p.kenar_baslik}"))
        if p.konu_yolu:
            print(gri(p.konu_yolu))
        print()
        print(p.icerik)
        if p.not_etiketi:
            print(gri(f"\n({p.not_etiketi})"))
    return 0


def komut_sohbet(args) -> int:
    from src.answer import HukukAsistani
    from src.prompts import FERAGATNAME

    asistan = HukukAsistani()
    korpus = ", ".join(f"{k['kisaltma']} {k['kanun_no']}" for k in config.KANUNLAR)
    print(kalin("Hukuk RAG Asistanı — etkileşimli kip"))
    print(gri(f"Korpus: {korpus} | Model: {asistan.saglayici.chat_model}"))
    print(gri("Çıkmak için: çık / exit / Ctrl-D\n"))

    while True:
        try:
            soru = input(kalin("Soru> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not soru:
            continue
        if soru.lower() in {"çık", "cik", "exit", "quit", "q"}:
            return 0

        arama, akis = asistan.cevapla_akisli(soru, top_k=args.top_k)
        print(gri("  kaynaklar: " + ", ".join(p.atif for p in arama.parcalar)))
        print()
        for parca in akis:
            print(parca, end="", flush=True)
        print("\n" + gri(FERAGATNAME) + "\n")


def komut_teshis(args) -> int:
    print(kalin("KURULUM TEŞHİSİ\n"))
    conn = db.baglan()
    try:
        istatistik = db.istatistik(conn)
        print(kalin("Veritabanı"))
        for anahtar, deger in istatistik.items():
            print(f"  {anahtar:22} {deger}")
    except Exception as exc:
        print(kirmizi(f"  Veritabanı okunamadı: {exc}"))
        print(gri("  Çözüm: ./.venv/bin/python -m src.ingest"))

    print(kalin("\nSağlayıcı"))
    try:
        from src import providers

        saglayici = providers.saglayici_al()
        for anahtar, deger in saglayici.teshis().items():
            if isinstance(deger, (dict, list)):
                deger = json.dumps(deger, ensure_ascii=False)
            print(f"  {anahtar:22} {deger}")
        print(yesil("\n  Sağlayıcı hazır."))
    except Exception as exc:
        print(kirmizi(f"  Sağlayıcı hazır değil: {exc}"))
        return 1
    return 0


def komut_kontrol(args) -> int:
    """LLM'siz sağlık kontrolü."""
    kod = komut_teshis(args)
    print()
    print(kalin("Arama duman testi: 'kira artış sınırı'"))
    from types import SimpleNamespace

    ara_kod = komut_ara(SimpleNamespace(sorgu="kira artış sınırı", top_k=5, ayrintili=False))
    print()
    print(kalin("Madde 344 (kira artış tavanı)"))
    madde_kod = komut_madde(SimpleNamespace(numara="344", kanun=None))
    print()
    if kod or ara_kod or madde_kod:
        print(kirmizi("Kontrol başarısız."))
        return 1
    print(yesil("Kontrol geçti (LLM çağrılmadı)."))
    print(gri("Arayüz:  ./.venv/bin/streamlit run src/app_streamlit.py"))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="hukuk-rag",
        description="Yerel çalışan Türk Borçlar Hukuku RAG asistanı",
    )
    ap.add_argument("--top-k", type=int, default=config.TOP_K,
                    help="modele verilecek madde sayısı")
    ap.add_argument("--ayrintili", action="store_true",
                    help="kaynak maddelerin tam metnini de göster")
    alt = ap.add_subparsers(dest="komut", required=True)

    p = alt.add_parser("sor", help="tek soru sor ve atıflı cevap al")
    p.add_argument("soru")
    p.set_defaults(fn=komut_sor)

    p = alt.add_parser("ara", help="yalnızca arama yap (LLM çağrılmaz)")
    p.add_argument("sorgu")
    p.set_defaults(fn=komut_ara)

    p = alt.add_parser("madde", help="madde metnini olduğu gibi göster")
    p.add_argument("numara")
    p.add_argument("--kanun", default=None, help="kısaltma, örn. TBK")
    p.set_defaults(fn=komut_madde)

    p = alt.add_parser("sohbet", help="etkileşimli soru-cevap")
    p.set_defaults(fn=komut_sohbet)

    p = alt.add_parser("teshis", help="kurulum ve sağlayıcı kontrolü")
    p.set_defaults(fn=komut_teshis)

    p = alt.add_parser(
        "kontrol",
        help="hızlı sağlık kontrolü (LLM çağırmaz)",
    )
    p.set_defaults(fn=komut_kontrol)

    args = ap.parse_args(argv)
    try:
        return args.fn(args)
    except KeyboardInterrupt:
        print()
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
