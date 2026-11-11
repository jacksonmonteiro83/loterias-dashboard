#!/usr/bin/env python3
"""
Historico das loterias da Caixa: coleta, correcao pela Selic e IR arrecadado.

Comandos:
  listar            imprime em JSON os slugs das loterias ativas (usado pelo workflow)
  coletar [slug]    baixa so os concursos novos da Caixa -> data/raw/<slug>.json
  calcular          atualiza a Selic e recalcula tudo a partir de data/raw -> docs/data/
  tudo              coletar (todas) + calcular   (padrao; uso local)

Cada loteria e isolada: se uma falhar, as outras continuam e o comando termina com
codigo de saida 1 no final, listando quais falharam.
"""
import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent.parent

# Catalogo. "primeiro_concurso" define a data inicial da serie Selic (usa-se a menor de todas).
# "ativa": False desliga uma loteria sem apagar nada.
LOTERIAS = {
    "megasena":       {"nome": "Mega-Sena",     "primeiro_concurso": date(1996, 3, 11)},
    "lotofacil":      {"nome": "Lotofácil",     "primeiro_concurso": date(2003, 9, 29)},
    # Quina: ate o concurso de 26/06/1994 a Caixa informa os premios em Cruzeiros Reais (CR$);
    # a partir de 07/07/1994, em Reais. Confirmado nos dados reais (concursos 26 e 30).
    "quina":          {"nome": "Quina",         "primeiro_concurso": date(1994, 3, 13),
                       "cruzeiro_real_ate": date(1994, 6, 30)},
    "lotomania":      {"nome": "Lotomania",     "primeiro_concurso": date(1999, 10, 2)},
    "duplasena":      {"nome": "Dupla Sena",    "primeiro_concurso": date(2001, 11, 6)},
    "timemania":      {"nome": "Timemania",     "primeiro_concurso": date(2008, 3, 1)},
    "diadesorte":     {"nome": "Dia de Sorte",  "primeiro_concurso": date(2018, 5, 19)},
    # Desligada: o backend da Caixa para /supersete esta quebrado (HTTP 500, "404 Not Found" do
    # SharePoint interno), para qualquer cliente. Religar com True quando a URL
    # https://servicebus2.caixa.gov.br/portaldeloterias/api/supersete voltar a responder JSON.
    "supersete":      {"nome": "Super Sete",    "primeiro_concurso": date(2020, 10, 2),
                       "ativa": False},
    "maismilionaria": {"nome": "+Milionária",   "primeiro_concurso": date(2022, 5, 28)},
}
# Conversao Cruzeiro Real -> Real (1 R$ = 2.750 CR$) para concursos ate `cruzeiro_real_ate`.
# Como o indice Selic e continuo, dividir o valor em CR$ por 2750 e depois aplicar o fator
# da data do concurso ate hoje equivale a converter na data da troca da moeda.
CRUZEIRO_REAL_POR_REAL = 2750.0

CAIXA = "https://servicebus2.caixa.gov.br/portaldeloterias/api"
SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.11/dados"
HEADERS = {"User-Agent": "Mozilla/5.0 (historico-loterias)", "Accept": "application/json"}
REBAIXAR_ULTIMOS = 3   # rebaixa os ultimos N concursos a cada rodada (pega correcoes)
BLOCO = 200            # grava o progresso a cada BLOCO concursos baixados


def ativas():
    return {k: v for k, v in LOTERIAS.items() if v.get("ativa", True)}


# ------------------------------------------------------------------ rede
def _get_json(url, tentativas=5):
    ultimo_erro = None
    for i in range(tentativas):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 404:
                return None
            ultimo_erro = f"HTTP {r.status_code}"
        except (requests.RequestException, ValueError) as e:
            ultimo_erro = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"Falha ao baixar {url}: {ultimo_erro}")


def _existe(slug, n):
    try:
        c = _get_json(f"{CAIXA}/{slug}/{n}", tentativas=2)
    except RuntimeError:
        return False
    return isinstance(c, dict) and "numero" in c


def ultimo_por_sondagem(slug):
    """Plano B quando a consulta do 'ultimo concurso' falha (ex.: HTTP 500 na Super Sete):
    acha o maior numero existente por busca exponencial + binaria (concursos sao 1..N)."""
    if not _existe(slug, 1):
        raise RuntimeError(f"Concurso 1 de {slug} indisponivel")
    ok, passo = 1, 1
    while _existe(slug, ok + passo):
        ok += passo
        passo *= 2
    lo, hi = ok, ok + passo          # lo existe, hi nao
    while hi - lo > 1:
        meio = (lo + hi) // 2
        lo, hi = (meio, hi) if _existe(slug, meio) else (lo, meio)
    return lo


def fetch_ultimo(slug):
    try:
        res = _get_json(f"{CAIXA}/{slug}")
        if isinstance(res, dict) and "numero" in res:
            return int(res["numero"])
    except RuntimeError as e:
        print(f"[{slug}] consulta do ultimo concurso falhou ({e}); sondando...", file=sys.stderr)
    return ultimo_por_sondagem(slug)


def fetch_concurso(slug, n):
    return _get_json(f"{CAIXA}/{slug}/{n}")


def fetch_selic(ini, fim):
    """Selic diaria (% a.d.) entre ini e fim, em janelas de ate 10 anos (limite do BCB).
    Falha alto: uma janela perdida deixaria um buraco permanente na serie."""
    partes = []
    while ini <= fim:
        fim_janela = min(date(ini.year + 9, 12, 31), fim)
        url = f"{SGS}?formato=json&dataInicial={ini:%d/%m/%Y}&dataFinal={fim_janela:%d/%m/%Y}"
        j = _get_json(url)
        if j:
            partes.append(pd.DataFrame(j))
        ini = date(fim_janela.year + 1, 1, 1)
    if not partes:
        return pd.DataFrame(columns=["data", "valor"])
    s = pd.concat(partes, ignore_index=True)
    s["data"] = pd.to_datetime(s["data"], format="%d/%m/%Y")
    s["valor"] = s["valor"].astype(float)
    return s


# ------------------------------------------------------------------ coleta
def enxugar(c):
    """Guarda so o necessario de cada concurso."""
    return {
        "numero": int(c["numero"]),
        "data": datetime.strptime(c["dataApuracao"], "%d/%m/%Y").date().isoformat(),
        "dezenas": c.get("listaDezenas") or c.get("dezenasSorteadasOrdemSorteio") or [],
        "acumulado": bool(c.get("acumulado", False)),
        "arrecadado": float(c.get("valorArrecadado") or 0),
        "local": c.get("localSorteio") or c.get("nomeMunicipioUFSorteio") or "",
        "faixas": [
            {
                "faixa": int(f["faixa"]),
                "descricao": f["descricaoFaixa"],
                "ganhadores": int(f["numeroDeGanhadores"]),
                "premio": float(f["valorPremio"]),
            }
            for f in (c.get("listaRateioPremio") or [])
        ],
    }


def ler_raw(arq):
    if not arq.exists():
        return {}
    return {int(k): v for k, v in json.loads(arq.read_text("utf-8")).items()}


def gravar_raw(arq, raw):
    arq.parent.mkdir(parents=True, exist_ok=True)
    arq.write_text(json.dumps({str(k): raw[k] for k in sorted(raw)}, ensure_ascii=False,
                              separators=(",", ":")), encoding="utf-8")


def atualizar_raw(slug, raw_dir):
    arq = raw_dir / f"{slug}.json"
    raw = ler_raw(arq)
    ultimo = fetch_ultimo(slug)
    faltam = set(range(1, ultimo + 1)) - set(raw)
    faltam |= set(range(max(1, ultimo - REBAIXAR_ULTIMOS + 1), ultimo + 1))
    faltam = sorted(faltam)
    print(f"[{slug}] ultimo concurso: {ultimo}; a baixar: {len(faltam)}", file=sys.stderr)

    def baixa(n):
        c = fetch_concurso(slug, n)
        if c is None:
            raise RuntimeError(f"Concurso {n} de {slug} nao encontrado")
        return enxugar(c)

    # em blocos, gravando o progresso: se cair no meio, o que ja veio nao se perde
    with ThreadPoolExecutor(max_workers=4) as ex:
        for i in range(0, len(faltam), BLOCO):
            for c in ex.map(baixa, faltam[i:i + BLOCO]):
                raw[c["numero"]] = c
            gravar_raw(arq, raw)
            print(f"[{slug}] {min(i + BLOCO, len(faltam))}/{len(faltam)}", file=sys.stderr)
    return raw


def atualizar_selic(csv, primeiro, hoje):
    """Mantem data/selic.csv: acrescenta dias novos e, se preciso, historico anterior."""
    atual = pd.read_csv(csv, parse_dates=["data"]) if csv.exists() else None
    partes = [atual] if atual is not None else []
    if atual is None:
        ini = primeiro
    else:
        ini = atual["data"].max().date() + timedelta(days=1)
        # a serie ja guardada comeca depois do necessario (ex.: entrou a Quina, de 1994)
        if atual["data"].min().date() > primeiro + timedelta(days=7):
            antes = fetch_selic(primeiro, atual["data"].min().date() - timedelta(days=1))
            if not antes.empty:
                partes.append(antes)
    if ini <= hoje:
        novo = fetch_selic(ini, hoje)
        if not novo.empty:
            partes.append(novo)
    if not partes:
        raise RuntimeError("Serie da Selic vazia")
    s = pd.concat(partes, ignore_index=True)
    s["data"] = pd.to_datetime(s["data"]).astype("datetime64[ns]")
    s = s.drop_duplicates("data").sort_values("data").reset_index(drop=True)
    csv.parent.mkdir(parents=True, exist_ok=True)
    s.to_csv(csv, index=False)
    return s


# ------------------------------------------------------------------ calculo
def tabela_faixas(raw, hoje, cfg=None):
    cfg = cfg or {}
    cr_ate = cfg.get("cruzeiro_real_ate")
    linhas = []
    for c in raw.values():
        d = date.fromisoformat(c["data"])
        if d > hoje:
            continue
        conv = CRUZEIRO_REAL_POR_REAL if (cr_ate and d <= cr_ate) else 1.0
        for f in c["faixas"]:
            linhas.append({"concurso": c["numero"], "data": pd.Timestamp(c["data"]),
                           "faixa": f["faixa"], "descricao": f["descricao"],
                           "ganhadores": f["ganhadores"],
                           "premio_por_ganhador": f["premio"] / conv})
    df = pd.DataFrame(linhas)
    if df.empty:
        return df
    df["data"] = df["data"].astype("datetime64[ns]")
    df["bruto_nominal"] = df["ganhadores"] * df["premio_por_ganhador"]
    return df


def fator_selic(datas, selic, hoje):
    """Produto de (1 + taxa_dia) dos dias APOS a data do concurso ate `hoje`."""
    s = selic[selic["data"] <= pd.Timestamp(hoje)].sort_values("data")
    indice = np.cumprod(1 + s["valor"].to_numpy() / 100)
    pos = np.searchsorted(s["data"].to_numpy(dtype="datetime64[ns]"),
                          datas.to_numpy(dtype="datetime64[ns]"), side="right") - 1
    pos = np.clip(pos, 0, None)
    return indice[-1] / indice[pos]


def aplicar_ir(df, regras):
    """IR por ganhador. Se o premio passa do limite vigente na data da regra:
       base 'total'     -> aliquota sobre o premio inteiro (regime anterior a 2008)
       base 'excedente' -> aliquota so sobre o que excede o limite (Lei 11.941/2009, art. 56)"""
    r = regras.sort_values("vigencia_inicio").copy()
    r["vigencia_inicio"] = r["vigencia_inicio"].astype("datetime64[ns]")
    if "base" not in r.columns:
        r["base"] = "total"
    df = df.sort_values("data").copy()
    df = pd.merge_asof(df, r[["vigencia_inicio", "limite_isencao", "aliquota", "base"]],
                       left_on="data", right_on="vigencia_inicio", direction="backward")
    if df["aliquota"].isna().any():
        print("AVISO: ha concursos anteriores a primeira regra de IR (IR=0 neles).", file=sys.stderr)
    premio, limite = df["premio_por_ganhador"], df["limite_isencao"]
    base_ir = np.where(df["base"] == "excedente", premio - limite, premio)
    por_ganhador = np.where(premio > limite, df["aliquota"] * base_ir, 0.0)
    df["ir_nominal"] = pd.Series(por_ganhador, index=df.index).fillna(0.0) * df["ganhadores"]
    return df.drop(columns=["vigencia_inicio", "limite_isencao", "aliquota", "base"])


def pendencias(regras, d_ini, d_fim):
    """Periodos de regra de IR nao conferida que cruzam o intervalo de datas da loteria."""
    r = regras.sort_values("vigencia_inicio").reset_index(drop=True)
    out = []
    for i, row in r.iterrows():
        if row["conferido"] == "sim":
            continue
        ini = row["vigencia_inicio"]
        fim = r.loc[i + 1, "vigencia_inicio"] - pd.Timedelta(days=1) if i + 1 < len(r) else None
        if ini > d_fim or (fim is not None and fim < d_ini):
            continue
        if out and out[-1]["ate"] is not None and pd.Timestamp(out[-1]["ate"]) + pd.Timedelta(days=1) == ini:
            out[-1]["ate"] = fim.strftime("%Y-%m-%d") if fim is not None else None
        else:
            out.append({"de": ini.strftime("%Y-%m-%d"),
                        "ate": fim.strftime("%Y-%m-%d") if fim is not None else None})
    return out


VALORES = ["bruto_nominal", "bruto_corrigido", "ir_nominal", "ir_corrigido"]


def calcular(raw, selic, regras, hoje, cfg=None):
    f = tabela_faixas(raw, hoje, cfg)
    if f.empty:
        raise RuntimeError("Sem faixas de premiacao para calcular")
    f["fator_selic"] = fator_selic(f["data"], selic, hoje)
    f = aplicar_ir(f, regras)
    f["bruto_corrigido"] = f["bruto_nominal"] * f["fator_selic"]
    f["ir_corrigido"] = f["ir_nominal"] * f["fator_selic"]
    f = f.sort_values(["concurso", "faixa"]).reset_index(drop=True)

    pc = f.groupby(["concurso", "data"], as_index=False)[VALORES].sum()
    pc["ano"] = pc["data"].dt.year
    pc["mes"] = pc["data"].dt.strftime("%Y-%m")
    agg = {"concursos": ("concurso", "count"), **{c: (c, "sum") for c in VALORES}}
    por_ano = pc.groupby("ano", as_index=False).agg(**agg)
    por_mes = pc.groupby("mes", as_index=False).agg(**agg)

    f["ano"] = f["data"].dt.year
    nomes = f.sort_values("data").groupby("faixa")["descricao"].last()
    por_ano_faixa = f.groupby(["ano", "faixa"], as_index=False).agg(
        ganhadores=("ganhadores", "sum"), **{c: (c, "sum") for c in VALORES})
    por_ano_faixa["descricao"] = por_ano_faixa["faixa"].map(nomes)
    return f, pc, por_ano, por_mes, por_ano_faixa


# ------------------------------------------------------------------ saida
def _records(df):
    d = df.copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].dt.strftime("%Y-%m-%d")
        elif d[c].dtype.kind == "f":
            d[c] = d[c].round(2)
    return json.loads(d.to_json(orient="records", force_ascii=False))


def gravar(slug, saida, f, pc, por_ano, por_mes, por_ano_faixa, meta):
    d = saida / slug
    d.mkdir(parents=True, exist_ok=True)
    comp = {"separators": (",", ":"), "ensure_ascii": False}
    for nome_arq, df in [("resumo_ano", por_ano), ("resumo_mes", por_mes),
                         ("resumo_ano_faixa", por_ano_faixa),
                         ("concursos", pc.drop(columns=["ano", "mes"]))]:
        (d / f"{nome_arq}.json").write_text(json.dumps(_records(df), **comp), encoding="utf-8")
    csv = f.drop(columns=["ano"], errors="ignore").copy()
    cols_f = csv.select_dtypes("float").columns
    csv[cols_f] = csv[cols_f].round(6)
    csv.to_csv(d / "faixas.csv", index=False, date_format="%Y-%m-%d")
    (d / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")


def raw_integro(raw):
    """Concursos devem ser 1..N sem buracos; buraco = download interrompido."""
    return bool(raw) and sorted(raw) == list(range(1, max(raw) + 1))


# ------------------------------------------------------------------ comandos
def cmd_coletar(slugs, root):
    falhas = []
    for slug in slugs:
        try:
            atualizar_raw(slug, root / "data" / "raw")
        except Exception as e:  # isola a loteria: as demais continuam
            print(f"ERRO [{slug}] coleta: {e}", file=sys.stderr)
            falhas.append(slug)
    return falhas


def cmd_calcular(root, hoje):
    regras = pd.read_csv(root / "regras_ir.csv", parse_dates=["vigencia_inicio"])
    saida = root / "docs" / "data"
    cat = ativas()
    selic = atualizar_selic(root / "data" / "selic.csv",
                            min(c["primeiro_concurso"] for c in cat.values()), hoje)
    agora = datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds")
    falhas = []
    for slug, cfg in cat.items():
        try:
            raw = ler_raw(root / "data" / "raw" / f"{slug}.json")
            if not raw_integro(raw):
                raise RuntimeError("dados ausentes ou incompletos em data/raw (rode a coleta)")
            f, pc, por_ano, por_mes, por_ano_faixa = calcular(raw, selic, regras, hoje, cfg)
            pend = pendencias(regras, pc["data"].min(), pc["data"].max())
            meta = {
                "loteria": slug, "nome": cfg["nome"], "atualizado_em": agora,
                "data_corrente": hoje.isoformat(),
                "concursos": int(pc["concurso"].nunique()),
                "ultimo_concurso": int(pc["concurso"].max()),
                "data_ultimo_concurso": pc["data"].max().strftime("%Y-%m-%d"),
                "selic_ate": selic["data"].max().strftime("%Y-%m-%d"),
                "regras_ir_conferidas": not pend,
                "regras_ir_pendentes": pend,
                "totais": {c: round(float(pc[c].sum()), 2) for c in VALORES},
            }
            gravar(slug, saida, f, pc, por_ano, por_mes, por_ano_faixa, meta)
            print(f"[{slug}] {meta['concursos']} concursos; selic ate {meta['selic_ate']}", file=sys.stderr)
        except Exception as e:
            print(f"ERRO [{slug}] calculo: {e}", file=sys.stderr)
            falhas.append(slug)
    # o dashboard so lista loterias que tem dados publicados
    publicadas = [{"slug": s, "nome": c["nome"]} for s, c in cat.items()
                  if (saida / s / "meta.json").exists()]
    saida.mkdir(parents=True, exist_ok=True)
    (saida / "loterias.json").write_text(json.dumps(publicadas, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    return falhas


def main(argv=None, root=ROOT, hoje=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("listar")
    c = sub.add_parser("coletar")
    c.add_argument("slug", nargs="?")
    sub.add_parser("calcular")
    sub.add_parser("tudo")
    a = ap.parse_args(argv)
    cmd = a.cmd or "tudo"
    hoje = hoje or datetime.now(ZoneInfo("America/Sao_Paulo")).date()

    if cmd == "listar":
        print(json.dumps(list(ativas())))
        return 0
    falhas = []
    if cmd in ("coletar", "tudo"):
        slugs = [a.slug] if getattr(a, "slug", None) else list(ativas())
        desconhecidas = [s for s in slugs if s not in ativas()]
        if desconhecidas:
            print(f"Loteria desconhecida/inativa: {desconhecidas}", file=sys.stderr)
            return 2
        falhas += cmd_coletar(slugs, root)
    if cmd in ("calcular", "tudo"):
        falhas += cmd_calcular(root, hoje)
    if falhas:
        print(f"FALHARAM: {sorted(set(falhas))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
