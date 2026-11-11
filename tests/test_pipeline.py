"""Testa o pipeline inteiro sem rede, com duas loterias e Selic sinteticas.

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
    "megasena": {
        1: concurso(1, "11/03/1996", [("6 acertos", 0, 0.0), ("5 acertos", 10, 800.0), ("4 acertos", 100, 50.0)]),
        2: concurso(2, "15/05/2024", [("6 acertos", 1, 1000000.0), ("5 acertos", 50, 2000.0), ("4 acertos", 3000, 3000.0)]),
        3: concurso(3, "29/10/2026", [("6 acertos", 2, 500000.0), ("5 acertos", 0, 0.0), ("4 acertos", 0, 0.0)]),
    },
    "quina": {
        1: concurso(1, "13/03/1994", [("5 acertos", 1, 55000000.0)]),   # em CR$
        2: concurso(2, "01/03/2005", [("5 acertos", 2, 20.0)]),
        3: concurso(3, "15/05/2024", [("5 acertos", 1, 100000.0)]),
        4: concurso(4, "28/10/2026", [("5 acertos", 3, 1000.0)]),
    },
}


def instala_fakes(chamadas):
    update.LOTERIAS = {
        "megasena": {"nome": "Mega-Sena", "primeiro_concurso": date(1996, 3, 11)},
        "quina": {"nome": "Quina", "primeiro_concurso": date(1994, 3, 13),
                  "cruzeiro_real_ate": date(1994, 6, 30)},
    }
    update.fetch_ultimo = lambda slug: max(FAKE[slug])
    update.fetch_concurso = lambda slug, n: (chamadas["concursos"].append((slug, n)), FAKE[slug][n])[1]

    def fake_selic(ini, fim):
        chamadas["selic"].append((ini, fim))
        return pd.DataFrame({"data": pd.bdate_range(ini, fim), "valor": 0.05})
    update.fetch_selic = fake_selic


def main(destino=None):
    raiz = Path(destino) if destino else Path(tempfile.mkdtemp())
    raiz.mkdir(parents=True, exist_ok=True)
    shutil.copy(AQUI / "regras_ir.csv", raiz / "regras_ir.csv")
    if destino:
        shutil.copytree(AQUI / "docs", raiz / "docs", dirs_exist_ok=True, ignore=shutil.ignore_patterns("data"))
    chamadas = {"concursos": [], "selic": []}
    instala_fakes(chamadas)

    assert update.main(["listar"], root=raiz) == 0
    assert update.main(["tudo"], root=raiz, hoje=HOJE) == 0

    d = raiz / "docs" / "data"
    mega = pd.read_csv(d / "megasena" / "faixas.csv")
    quina = pd.read_csv(d / "quina" / "faixas.csv")
    meta_m = json.loads((d / "megasena" / "meta.json").read_text())
    meta_q = json.loads((d / "quina" / "meta.json").read_text())

    # Selic: fator do concurso 1 da Mega = 1,0005 ^ (dias uteis apos 11/03/1996 ate hoje)
    n = len(pd.bdate_range("1996-03-12", "2026-10-29"))
    f1 = mega[mega.concurso == 1].fator_selic.iloc[0]
    assert abs(f1 / 1.0005 ** n - 1) < 1e-7, (f1, 1.0005 ** n)
    # serie Selic cobre desde a Quina (1994)
    selic = pd.read_csv(raiz / "data" / "selic.csv", parse_dates=["data"])
    assert selic["data"].min() <= pd.Timestamp("1994-03-14")

    # IR Mega 1996 (R$ 11,10, base total): quina 10 x 800 x 30% + quadra 100 x 50 x 30%
    ir = mega.groupby("concurso").ir_nominal.sum()
    assert abs(ir[1] - (10 * 800 * 0.30 + 100 * 50 * 0.30)) < 1e-6
    # IR 2024 (limite 2259,20, base excedente)
    c2 = mega[mega.concurso == 2].set_index("faixa").ir_nominal
    assert abs(c2[1] - 0.30 * (1000000 - 2259.20)) < 1e-4
    assert c2[2] == 0
    assert abs(c2[3] - 3000 * 0.30 * (3000 - 2259.20)) < 1e-4
    assert abs(ir[3] - 2 * 0.30 * (500000 - 2428.80)) < 1e-4   # 2026, limite provisorio
    # Quina: 2005 e anterior a 2008 (R$ 11,10, base total): 2 x 20 x 30% = 12; 2026: 1000 < limite -> isento
    qir = quina.groupby("concurso").ir_nominal.sum()
    assert abs(qir[2] - 12.0) < 1e-9 and qir[4] == 0
    assert abs(qir[3] - 0.30 * (100000 - 2259.20)) < 1e-4
    # Quina 1994 em Cruzeiros Reais: 55.000.000 CR$ / 2750 = R$ 20.000 (IR 1994: 30% do total)
    q1 = quina[quina.concurso == 1].iloc[0]
    assert abs(q1.bruto_nominal - 20000.0) < 1e-6 and abs(q1.ir_nominal - 6000.0) < 1e-6

    # pendencias de regra de IR por loteria
    assert meta_m["regras_ir_pendentes"] == [{"de": "2025-05-01", "ate": None}]
    assert meta_q["regras_ir_pendentes"] == [{"de": "1994-03-13", "ate": "1996-03-10"},
                                             {"de": "2025-05-01", "ate": None}]
    assert meta_m["regras_ir_conferidas"] is False
    assert meta_m["concursos"] == 3 and meta_q["concursos"] == 4
    indice = json.loads((d / "loterias.json").read_text())
    assert [x["slug"] for x in indice] == ["megasena", "quina"]

    # coleta incremental: so os 3 ultimos de cada uma e so o dia novo da Selic
    chamadas["concursos"].clear(); chamadas["selic"].clear()
    assert update.main(["tudo"], root=raiz, hoje=date(2026, 10, 30)) == 0
    assert sorted(chamadas["concursos"]) == [("megasena", 1), ("megasena", 2), ("megasena", 3),
                                             ("quina", 2), ("quina", 3), ("quina", 4)]
    assert chamadas["selic"] == [(date(2026, 10, 30), date(2026, 10, 30))], chamadas["selic"]

    # isolamento: raw da Quina com buraco -> Quina falha (codigo 1), Mega continua publicada
    arq = raiz / "data" / "raw" / "quina.json"
    raw = json.loads(arq.read_text()); del raw["2"]; arq.write_text(json.dumps(raw))
    antes = (d / "quina" / "meta.json").read_text()
    assert update.main(["calcular"], root=raiz, hoje=date(2026, 10, 30)) == 1
    assert (d / "quina" / "meta.json").read_text() == antes          # nao sobrescreveu
    assert json.loads((d / "megasena" / "meta.json").read_text())["data_corrente"] == "2026-10-30"

    # coleta isolada: uma loteria que falha na rede nao impede as demais
    original = update.fetch_ultimo
    update.fetch_ultimo = lambda slug: (_ for _ in ()).throw(RuntimeError("rede")) if slug == "quina" else original(slug)
    assert update.main(["coletar"], root=raiz, hoje=HOJE) == 1
    update.fetch_ultimo = original
    assert update.main(["coletar", "xyz"], root=raiz) == 2

    # plano B: consulta do 'ultimo' falha (HTTP 500) -> acha o ultimo por sondagem
    def get_fake(url, tentativas=5):
        if url.endswith("/megasena"):
            raise RuntimeError("HTTP 500")
        n = int(url.rsplit("/", 1)[1])
        return FAKE["megasena"].get(n)          # None (404) alem do ultimo
    update_get, update._get_json = update._get_json, get_fake
    original_fu = update.fetch_ultimo
    try:
        import importlib; real = importlib.reload(update)   # restaura fetch_ultimo real
        real._get_json = get_fake
        assert real.fetch_ultimo("megasena") == 3
    finally:
        update.fetch_ultimo = original_fu; update._get_json = update_get
    print("OK: pipeline validado. Saida em", raiz)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
