"""Arama ayarlarını değerlendirme seti üzerinde ızgara taramasıyla seçer.

Bu betik ürünün parçası değildir; ayar değerlerinin nasıl seçildiğini
belgelemek ve tekrar üretilebilir kılmak için vardır. Sorgu gömmeleri bir kez
hesaplanıp önbelleğe alınır; böylece yüzlerce parametre birleşimi LLM'e hiç
dokunmadan saniyeler içinde denenebilir.

Kullanım:
    ./.venv/bin/python -m tests.tune_retrieval
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

import numpy as np
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db  # noqa: E402
from src.answer import HukukAsistani  # noqa: E402

EVAL = Path(__file__).resolve().parent / "eval_set.yaml"


def main() -> int:
    veri = yaml.safe_load(EVAL.read_text(encoding="utf-8"))
    sorular = [s for s in veri["sorular"] if s["tur"] == "cevaplanabilir"]

    asistan = HukukAsistani()
    r = asistan.retriever

    # Sorgu gömmelerini bir kez üret; tarama boyunca yeniden kullanılır.
    print(f"{len(sorular)} sorgu gömmesi hesaplanıyor...", flush=True)
    gommeler: dict[str, np.ndarray] = {}
    for s in sorular:
        v = np.asarray(r.saglayici.embed_tek(s["soru"]), dtype=np.float32)
        gommeler[s["id"]] = v / (np.linalg.norm(v) or 1.0)
    print("hazır.\n", flush=True)

    def vektor_siralamasi(sid: str, k: int) -> list[int]:
        skorlar = r.matris @ gommeler[sid]
        return [r.parca_idleri[i] for i in np.argsort(-skorlar)[:k]]

    # Parça kimliği -> madde numarası eşlemesi (tek seferde)
    tum = db.parcalari_getir(r.conn, r.parca_idleri)
    madde_no = {pid: k.madde_no.upper() for pid, k in tum.items()}

    def olc(aday, w_v, w_f, w_m, df_lim, esanlam=True):
        rr, h1, h3, h5, kacan = [], 0, 0, 0, []
        for s in sorular:
            havuz: dict[int, float] = {}
            for sira, pid in enumerate(vektor_siralamasi(s["id"], aday), 1):
                havuz[pid] = havuz.get(pid, 0.0) + w_v / (60 + sira)
            fts_idler, _ = r.fts_ara(s["soru"], aday, df_limiti=df_lim, esanlam=esanlam)
            for sira, pid in enumerate(fts_idler, 1):
                havuz[pid] = havuz.get(pid, 0.0) + w_f / (60 + sira)
            madde_idler, _ = r.madde_ara(s["soru"])
            for sira, pid in enumerate(madde_idler, 1):
                havuz[pid] = havuz.get(pid, 0.0) + w_m / (60 + sira)

            ust = [pid for pid, _ in sorted(havuz.items(), key=lambda kv: -kv[1])[:5]]
            beklenen = {str(m).upper() for m in s["beklenen_maddeler"]}
            konum = next(
                (i for i, pid in enumerate(ust, 1) if madde_no.get(pid) in beklenen),
                None,
            )
            if konum is None:
                kacan.append(s["id"])
                rr.append(0.0)
            else:
                rr.append(1.0 / konum)
                h1 += konum == 1
                h3 += konum <= 3
                h5 += konum <= 5
        n = len(sorular)
        return (
            statistics.mean(rr),
            100 * h1 / n,
            100 * h3 / n,
            100 * h5 / n,
            kacan,
        )

    # Sıralama ölçütü: ÖNCE hit@5, SONRA MRR. Modele ilk 5 parça verildiği için
    # doğru madde ilk 5'te değilse cevap doğru olamaz; sıradaki iyileşme ikincil.
    sonuclar = []
    for aday in (8, 10, 12, 15, 20, 25, 40):
        for w_f in (0.2, 0.4, 0.6, 0.8, 1.0, 1.3, 1.6):
            for df_lim in (0.04, 0.06, 0.08, 0.12, 0.15):
                mrr, h1, h3, h5, kacan = olc(aday, 1.0, w_f, 3.0, df_lim)
                sonuclar.append((h5, mrr, h1, h3, aday, w_f, df_lim, kacan))

    sonuclar.sort(key=lambda x: (-x[0], -x[1]))
    print(f"{'hit@5':>6} {'MRR':>7} {'hit@1':>6} {'hit@3':>6} "
          f"{'aday':>5} {'w_fts':>6} {'df_lim':>7}  kaçan")
    print("-" * 78)
    for h5, mrr, h1, h3, aday, w_f, df_lim, kacan in sonuclar[:12]:
        print(f"{h5:6.1f} {mrr:7.4f} {h1:6.1f} {h3:6.1f} {aday:5} "
              f"{w_f:6.2f} {df_lim:7.2f}  {','.join(kacan) or '-'}")

    print("\n" + "=" * 78)
    print("BİLEŞEN KATKILARI (ablasyon)")
    print("=" * 78)
    # Son hâl: aday=12, w_fts=1.0, df_limiti=0.12 (yukarıdaki taramanın kazananı)
    for etiket, args, kw in [
        ("ilk sürüm (aday=25, eşit ağırlık, DF süzgeci ve eşanlam yok)",
         (25, 1.0, 1.0, 3.0, 1.0), {"esanlam": False}),
        ("+ aday havuzu 12'ye indirildi",
         (12, 1.0, 1.0, 3.0, 1.0), {"esanlam": False}),
        ("+ DF süzgeci (0.12)",
         (12, 1.0, 1.0, 3.0, 0.12), {"esanlam": False}),
        ("+ hukuk eşanlam sözlüğü  [SON HÂL]",
         (12, 1.0, 1.0, 3.0, 0.12), {"esanlam": True}),
        ("yalnızca vektör (kelime araması kapalı)",
         (12, 1.0, 0.0, 3.0, 0.12), {"esanlam": False}),
        ("yalnızca kelime araması (vektör kapalı)",
         (12, 0.0, 1.0, 3.0, 0.12), {"esanlam": True}),
    ]:
        mrr, h1, h3, h5, kacan = olc(*args, **kw)
        print(f"{etiket:52} hit@5={h5:5.1f}  MRR={mrr:.4f}  "
              f"hit@1={h1:5.1f}  kaçan={','.join(kacan) or '-'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
