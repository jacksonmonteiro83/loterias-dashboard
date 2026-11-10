#!/usr/bin/env python3
"""
Atualiza o historico das loterias da Caixa e gera os dados do dashboard.

Fluxo (idempotente, pode rodar todo dia):
  1. Baixa so os concursos novos da Caixa  -> data/raw/<loteria>.json
  2. Baixa so os dias novos da Selic (SGS 11) -> data/selic.csv
  3. Recalcula bruto, correcao pela Selic e IR -> docs/data/<loteria>/*.json|csv
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

# CATALOGO COMPLETO DAS LOTERIAS DA CAIXA
LOTERIAS = {
    "megasena":  {"nome": "Mega-Sena",  "primeiro_concurso": date(1996, 3, 11)},
    "lotofacil": {"nome": "Lotofácil",  "primeiro_concurso": date(2003, 9, 29)},
    "quina":     {"nome": "Quina",      "primeiro_concurso": date(1994, 3, 13)},
    "lotomania": {"nome": "Lotomania",  "primeiro_concurso": date(1999, 10, 2)},
    "duplasena": {"nome": "Dupla Sena", "primeiro_concurso": date(2001, 11, 6)},
    "timemania": {"nome": "Timemania",  "primeiro_concurso": date(2008, 3, 1)},
    "diadesorte":{"nome": "Dia de Sorte","primeiro_concurso": date(2018, 5, 19)},
    "supersete": {"nome": "Super Sete", "primeiro_concurso": date(2020, 10, 2)},
}

CAIXA = "https://servicebus2.caixa.gov.br/portaldeloterias/api"
SGS = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.11/dados"
HEADERS = {"User-Agent": "Mozilla/5.0 (historico-loterias)", "Accept": "application/json"}
REBAIXAR_ULTIMOS = 3  # rebaixa os ultimos N concursos a cada rodada (pega correcoes)


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


def fetch_ultimo(slug):
    res = _get_json(f"{CAIXA}/{slug}")
    if not res or "numero" not in res:
        raise RuntimeError(f"Nao foi possivel obter o ultimo concurso de {slug}")
    return int(res["numero"])


def fetch_concurso(slug, n):
    return _get_json(f"{CAIXA}/{slug}/{n}")


def fetch_selic(ini, fim):
    """Selic diaria (% a.d.) entre ini e fim, em janelas de ate 10 anos (limite do BCB)."""
    partes = []
    while ini <= fim:
        fim_janela = min(date(ini.year + 9, 12, 31), fim)
        url = f"{SGS}?formato=json&dataInicial={ini:%d/%m/%Y}&dataFinal={fim_janela:%d/%m/%Y}"
        try:
            j = _get_json(url)
            if j:
                partes.append(pd.DataFrame(j))
        except Exception as e:
            print(f"Aviso Selic ({ini} a {fim_janela}): {e}", file=sys.stderr)
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


def atualizar_raw(slug, raw_dir):
    arq = raw_dir / f"{slug}.json"
    raw = {}
    if arq.exists():
        try:
            raw = {int(k): v for k, v in json.loads(arq.read_text("utf-8")).items()}
        except Exception:
            raw = {}

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

    if faltam:
        with ThreadPoolExecutor(max_workers=4) as ex:
            for c in ex.map(baixa, faltam):
                raw[c["numero"]] = c
        raw_dir.mkdir(parents=True, exist_ok=True)
        arq.write_text(json.dumps({str(k): raw[k] for k in sorted(raw)}, ensure_ascii=False,
                                  separators=(",", ":")), encoding="utf-8")
    return raw


def atualizar_selic(csv, primeiro, hoje):
    atual = pd.read_csv(csv, parse_dates=["data"]) if csv.exists() else None
    ini = (atual["data"].max().date() + timedelta(days=1)) if atual is not None else primeiro
    if ini <= hoje:
        novo = fetch_selic(ini, hoje)
        if not novo.empty:
            atual = pd.concat([atual, novo], ignore_index=True) if atual is not None else novo
    if atual is None or atual.empty:
        raise RuntimeError("Serie da Selic vazia")
    atual = atual.drop_duplicates("data").sort_values("data").reset_index(drop=True)
    csv.parent.mkdir(parents=True, exist_ok=True)
    atual.to_csv(csv, index=False)
    return atual


# ------------------------------------------------------------------ calculo
def tabela_faixas(raw, hoje):
    linhas = []
    for c in raw.values():
        if date.fromisoformat(c["data"]) > hoje:
            continue
        for f in c["faixas"]:
            linhas.append({"concurso": c["numero"], "data": pd.Timestamp(c["data"]),
                           "faixa": f["faixa"], "descricao": f["descricao"],
                           "ganhadores": f["ganhadores"], "premio_por_ganhador": f["premio"]})
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
    r = regras.sort_values("vigencia_inicio").copy()
    r["vigencia_inicio"] = r["vigencia_inicio"].astype("datetime64[ns]")
    df = df.sort_values("data").copy()
    df = pd.merge_asof(df, r[["vigencia_inicio", "limite_isencao", "aliquota"]],
                       left_on="data", right_on="vigencia_inicio", direction="backward")
    if df["aliquota"].isna().any():
        print("AVISO: ha concursos anteriores a primeira regra de IR (IR=0 neles).", file=sys.stderr)
    tributa = df["premio_por_ganhador"] > df["limite_isencao"]
    df["ir_nominal"] = np.where(tributa, df["bruto_nominal"] * df["aliquota"], 0.0)
    df["ir_nominal"] = df["ir_nominal"].fillna(0.0)
    return df.drop(columns=["vigencia_inicio", "limite_isencao", "aliquota"])


VALORES = ["bruto_nominal", "bruto_corrigido", "ir_nominal", "ir_corrigido"]


def calcular(raw, selic, regras, hoje):
    f = tabela_faixas(raw, hoje)
    if f.empty:
        return f, pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
        
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


def gravar(slug, nome, saida, f, pc, por_ano, por_mes, por_ano_faixa, meta):
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


def main(argv=None, root=ROOT, hoje=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--so-calcular", action="store_true",
                    help="nao baixa nada; recalcula a partir de data/")
    a = ap.parse_args(argv)
    hoje = hoje or datetime.now(ZoneInfo("America/Sao_Paulo")).date()

    regras = pd.read_csv(root / "regras_ir.csv", parse_dates=["vigencia_inicio"])
    regras_ok = bool((regras["conferido"] == "sim").all())
    if not regras_ok:
        print("AVISO: regras_ir.csv tem linhas nao conferidas (conferido != sim).", file=sys.stderr)

    saida = root / "docs" / "data"
    indice = []
    
    # Execução resiliente por modalidade
    for slug, cfg in LOTERIAS.items():
        try:
            if a.so_calcular:
                raw = {int(k): v for k, v in json.loads(
                    (root / "data" / "raw" / f"{slug}.json").read_text("utf-8")).items()}
                selic = pd.read_csv(root / "data" / "selic.csv", parse_dates=["data"])
            else:
                raw = atualizar_raw(slug, root / "data" / "raw")
                selic = atualizar_selic(root / "data" / "selic.csv", cfg["primeiro_concurso"], hoje)
            
            f, pc, por_ano, por_mes, por_ano_faixa = calcular(raw, selic, regras, hoje)
            
            if pc.empty:
                continue

            meta = {
                "loteria": slug, "nome": cfg["nome"],
                "atualizado_em": datetime.now(ZoneInfo("America/Sao_Paulo")).isoformat(timespec="seconds"),
                "data_corrente": hoje.isoformat(),
                "concursos": int(pc["concurso"].nunique()),
                "ultimo_concurso": int(pc["concurso"].max()),
                "data_ultimo_concurso": pc["data"].max().strftime("%Y-%m-%d"),
                "selic_ate": selic["data"].max().strftime("%Y-%m-%d"),
                "regras_ir_conferidas": regras_ok,
                "totais": {c: round(float(pc[c].sum()), 2) for c in VALORES},
            }
            gravar(slug, cfg["nome"], saida, f, pc, por_ano, por_mes, por_ano_faixa, meta)
            indice.append({"slug": slug, "nome": cfg["nome"]})
            print(f"[{slug}] Sucesso: {meta['concursos']} concursos processados.", file=sys.stderr)
        except Exception as err:
            print(f"ERRO ao processar [{slug}]: {err}", file=sys.stderr)

    # Grava o indice de loterias disponiveis
    if indice:
        (saida / "loterias.json").write_text(json.dumps(indice, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
