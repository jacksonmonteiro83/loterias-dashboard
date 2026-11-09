"""Testa o pipeline inteiro sem rede, com concursos e Selic sinteticos.

Uso:  python tests/test_pipeline.py [pasta_de_saida]
Sem argumento usa uma pasta temporaria. Com argumento, deixa os arquivos la
(util para abrir o dashboard e ver como fica).
"""
import json
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path

import pandas as pd

AQUI = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(AQUI / "scripts"))
import update  # noqa: E402

HOJE = date(2026, 10, 29)


def concurso(n, dia, faixas):
    return {"numero": n, "dataApuracao": dia, "listaDezenas": ["01", "02", "03", "04", "05", "06"],
            "acumulado": False, "valorArrecadado": 1e6, "localSorteio": "teste",
            "listaRateioPremio": [{"faixa": i + 1, "descricaoFaixa": d, "numeroDeGanhadores": g, "valorPremio": p}
                                  for i, (d, g, p) in enumerate(faixas)]}


FAKE = {
    1: concurso(1, "11/03/1996", [("6 acertos", 0, 0.0), ("5 acertos", 10, 800.0), ("4 acertos", 100, 50.0)]),
    2: concurso(2, "15/05/2024", [("6 acertos", 1, 1000000.0), ("5 acertos", 50, 2000.0), ("4 acertos", 3000, 3000.0)]),
    3: concurso(3, "29/10/2026", [("6 acertos", 2, 500000.0), ("5 acertos", 0, 0.0), ("4 acertos", 0, 0.0)]),
}


def main(destino=None):
    raiz = Path(destino) if destino else Path(tempfile.mkdtemp())
    raiz.mkdir(parents=True, exist_ok=True)
    shutil.copy(AQUI / "regras_ir.csv", raiz / "regras_ir.csv")
    if destino:
        shutil.copytree(AQUI / "docs", raiz / "docs", dirs_exist_ok=True, ignore=shutil.ignore_patterns("data"))

    chamadas = {"concursos": [], "selic": []}
    update.fetch_ultimo = lambda slug: max(FAKE)
    update.fetch_concurso = lambda slug, n: (chamadas["concursos"].append(n), FAKE[n])[1]

    def fake_selic(ini, fim):
        chamadas["selic"].append((ini, fim))
        d = pd.bdate_range(ini, fim)
        return pd.DataFrame({"data": d, "valor": 0.05})
    update.fetch_selic = fake_selic

    update.LOTERIAS = {"megasena": {"nome": "Mega-Sena", "primeiro_concurso": date(1996, 3, 11)}}
    update.main([], root=raiz, hoje=HOJE)

    d = raiz / "docs" / "data" / "megasena"
    faixas = pd.read_csv(d / "faixas.csv")
    meta = json.loads((d / "meta.json").read_text())

    # fator Selic do concurso 1 = 1,0005 ^ (dias uteis apos 11/03/1996 ate hoje)
    n = len(pd.bdate_range("1996-03-12", "2026-10-29"))
    f1 = faixas[faixas.concurso == 1].fator_selic.iloc[0]
    assert abs(f1 / 1.0005 ** n - 1) < 1e-7, (f1, 1.0005 ** n)  # CSV guarda 6 casas
    assert abs(faixas[faixas.concurso == 3].fator_selic.iloc[0] - 1) < 1e-12
    # IR: 1996 isento (800 e 50 < 900); 2024: limite 2259,20 -> sena e quadra tributam, quina nao
    ir = faixas.groupby("concurso").ir_nominal.sum()
    assert ir[1] == 0
    assert ir[2] == 300000.0 + 3000 * 3000 * 0.30
    assert ir[3] == 300000.0
    assert faixas[(faixas.concurso == 2) & (faixas.faixa == 2)].ir_nominal.iloc[0] == 0
    assert meta["concursos"] == 3 and meta["ultimo_concurso"] == 3
    assert meta["regras_ir_conferidas"] is False
    ano = json.loads((d / "resumo_ano.json").read_text())
    assert abs(sum(r["ir_nominal"] for r in ano) - meta["totais"]["ir_nominal"]) < 0.01
    assert [r["ano"] for r in ano] == [1996, 2024, 2026]

    # 2a rodada (incremental): so os 3 ultimos concursos sao rebaixados; Selic so o dia novo
    chamadas["concursos"].clear(); chamadas["selic"].clear()
    update.main([], root=raiz, hoje=date(2026, 10, 30))
    assert sorted(chamadas["concursos"]) == [1, 2, 3]  # so ha 3 concursos, todos sao "ultimos"
    assert chamadas["selic"] == [(date(2026, 10, 30), date(2026, 10, 30))], chamadas["selic"]
    meta2 = json.loads((d / "meta.json").read_text())
    assert meta2["selic_ate"] == "2026-10-30"
    print("OK: pipeline validado. Saida em", raiz)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
