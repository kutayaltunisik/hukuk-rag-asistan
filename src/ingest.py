"""Korpus kurma boru hattı: indir -> ayrıştır -> parçala -> göm -> SQLite'a yaz.

Bu betik projenin "bilgi tabanını" oluşturur. Bir kez çalıştırılır; sonrasında
sorgular yalnızca veritabanını ve yerel modeli kullanır.

Akış:
    1. mevzuat.gov.tr'den kanun metni indirilir (tek internet gerektiren adım)
    2. HTML madde bazlı parçalara ayrıştırılır (bkz. mevzuat/chunker.py)
    3. Her parça için Foundry Local ile gömme (embedding) üretilir
    4. Parçalar + gömmeler + tam metin indeksi SQLite'a yazılır
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from src import config, db
from src.mevzuat import chunker, fetch


@dataclass
class IngestSonucu:
    kanun_sayisi: int = 0
    madde_sayisi: int = 0
    parca_sayisi: int = 0
    gomme_sayisi: int = 0
    gomme_boyutu: int = 0
    gomme_modeli: str = ""
    saniye: float = 0.0
    uyarilar: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.uyarilar is None:
            self.uyarilar = []


def _madde_kapsami_dogrula(kanun: chunker.ParsedKanun) -> list[str]:
    """Eksik veya boş madde numaralarını uyarır."""
    uyarilar: list[str] = []
    numaralar = sorted(
        int(m.madde_no) for m in kanun.maddeler if m.madde_no.isdigit()
    )
    if not numaralar:
        return [f"{kanun.kisaltma}: hiç madde ayrıştırılamadı."]

    eksik = [n for n in range(1, numaralar[-1] + 1) if n not in set(numaralar)]
    if eksik:
        ozet = ", ".join(str(n) for n in eksik[:20])
        uyarilar.append(
            f"{kanun.kisaltma}: {len(eksik)} madde numarası atlanmış görünüyor "
            f"({ozet}{'...' if len(eksik) > 20 else ''})."
        )
    bos = [m.madde_no for m in kanun.maddeler if not m.metin.strip()]
    if bos:
        uyarilar.append(f"{kanun.kisaltma}: {len(bos)} madde metni boş: {bos[:10]}")
    return uyarilar


def korpusu_kur(
    saglayici=None,
    force_indir: bool = False,
    ilerleme=print,
) -> IngestSonucu:
    baslangic = time.perf_counter()
    sonuc = IngestSonucu()

    if saglayici is None:
        from src import providers

        saglayici = providers.saglayici_al()

    conn = db.baglan()
    db.sema_kur(conn)

    for kanun_meta in config.KANUNLAR:
        ilerleme(f"[1/4] {kanun_meta['kanun_adi']} indiriliyor...")
        yol, url = fetch.indir(kanun_meta, force=force_indir)

        ilerleme(f"[2/4] Ayrıştırılıyor: {yol.name}")
        parsed = chunker.parse_kanun(yol.read_bytes(), kanun_meta, url)
        sonuc.uyarilar.extend(_madde_kapsami_dogrula(parsed))
        chunks = chunker.maddeleri_parcala(parsed)
        ilerleme(
            f"      {len(parsed.maddeler)} madde -> {len(chunks)} parça "
            f"(en uzun {max((len(c.icerik) for c in chunks), default=0)} karakter)"
        )

        kanun_id = db.kanun_kaydet(conn, parsed, kanun_meta["slug"])
        parca_idleri = db.parcalari_kaydet(conn, kanun_id, chunks)
        conn.commit()

        ilerleme(f"[3/4] Gömmeler üretiliyor ({len(chunks)} parça)...")
        vektorler: list[list[float]] = []
        for bas in range(0, len(chunks), config.EMBED_BATCH):
            grup = chunks[bas : bas + config.EMBED_BATCH]
            vektorler.extend(saglayici.embed([c.gomme_metni for c in grup]))
            ilerleme(f"      gömme {len(vektorler)}/{len(chunks)}")

        model_adi = getattr(saglayici, "embed_model", "") or "bilinmiyor"
        db.gommeleri_kaydet(conn, parca_idleri, vektorler, model_adi)
        db.ustveri_yaz(conn, "gomme_modeli", model_adi)
        db.ustveri_yaz(conn, "gomme_boyutu", len(vektorler[0]) if vektorler else 0)
        conn.commit()

        sonuc.kanun_sayisi += 1
        sonuc.madde_sayisi += len(parsed.maddeler)
        sonuc.parca_sayisi += len(chunks)
        sonuc.gomme_sayisi += len(vektorler)
        sonuc.gomme_boyutu = len(vektorler[0]) if vektorler else 0
        sonuc.gomme_modeli = model_adi

    ilerleme("[4/4] Tamamlandı.")
    conn.close()
    sonuc.saniye = time.perf_counter() - baslangic
    return sonuc


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="python -m src.ingest",
        description="Kanun metnini indirir, maddelere ayırır, gömme üretip "
                    "SQLite'a yazar. İnternet yalnızca indirme adımında kullanılır.",
    )
    ap.add_argument(
        "--force-indir", action="store_true",
        help="önbellekteki HTML'i yok say ve mevzuat.gov.tr'den yeniden indir",
    )
    ap.add_argument(
        "--sessiz", action="store_true", help="ilerleme çıktısını bastır"
    )
    args = ap.parse_args(argv)

    sonuc = korpusu_kur(
        force_indir=args.force_indir,
        ilerleme=(lambda *_: None) if args.sessiz else print,
    )

    print()
    print(f"  kanun        : {sonuc.kanun_sayisi}")
    print(f"  madde        : {sonuc.madde_sayisi}")
    print(f"  parça        : {sonuc.parca_sayisi}")
    print(f"  gömme        : {sonuc.gomme_sayisi} x {sonuc.gomme_boyutu} boyut")
    print(f"  gömme modeli : {sonuc.gomme_modeli}")
    print(f"  süre         : {sonuc.saniye:.1f} saniye")
    print(f"  veritabanı   : {config.DB_PATH}")

    if sonuc.uyarilar:
        print("\n  UYARILAR:")
        for u in sonuc.uyarilar:
            print(f"    - {u}")
        # Eksik madde sessizce geçilmemeli: çağıran betik/CI fark etmeli.
        return 1

    print("\n  Sonraki adım: ./.venv/bin/python -m tests.verify_corpus")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
