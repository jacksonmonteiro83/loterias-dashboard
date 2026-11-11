# Histórico das Loterias da Caixa

Dashboard estático (GitHub Pages) com, para cada loteria da Caixa, os prêmios brutos pagos por faixa,
corrigidos pela Selic até hoje, o IR arrecadado e os totais por mês e por ano.
Loterias: Mega-Sena, Lotofácil, Quina, Lotomania, Dupla Sena, Timemania, Dia de Sorte, Super Sete e +Milionária.
Atualiza sozinho todo dia à meia-noite (Brasília).

## Como funciona

```
GitHub Actions (cron diário) — .github/workflows/atualizar.yml
  1. listar    → lê o catálogo de loterias de scripts/update.py
  2. coletar   → uma tarefa por loteria, em paralelo: baixa só os concursos novos → data/raw/<loteria>.json
  3. publicar  → baixa a Selic nova, recalcula tudo → docs/data/<loteria>/*.json, faz commit
GitHub Pages serve a pasta docs/
```

- **Falha isolada:** se uma loteria falhar, as outras seguem e são publicadas. O workflow fica vermelho
  e o log diz qual falhou. A loteria com problema continua mostrando os dados da última rodada boa.
- **Primeira rodada:** baixa o histórico inteiro (~25 mil concursos); cada loteria roda em paralelo e
  o progresso é gravado em blocos. Se uma tarefa cair no meio, rode o workflow de novo: ele continua de onde parou.
- Para desligar uma loteria, coloque `"ativa": False` na entrada dela em `LOTERIAS`.

## Publicar

1. **Settings → Pages**: *Deploy from a branch* → `main` → pasta `/docs`.
2. **Actions → Atualizar loterias → Run workflow**.

## Rodar local

```
pip install -r requirements.txt
python scripts/update.py tudo            # coleta + cálculo
python scripts/update.py coletar quina   # só uma loteria
python scripts/update.py calcular        # só recalcula (usa data/raw)
python -m http.server -d docs 8000       # abre http://localhost:8000
python tests/test_pipeline.py            # teste sem rede, dados sintéticos
```

## Método de cálculo

- **Bruto da faixa** = ganhadores × prêmio por ganhador (valor informado pela Caixa, tratado como bruto).
- **Quina antes de 07/1994:** a Caixa informa os prêmios em Cruzeiros Reais até 26/06/1994. O script divide
  por 2.750 (conversão para Real) nesses concursos (`cruzeiro_real_ate`).
- **Fator Selic** = produto de (1 + taxa_dia/100) dos dias **após** a data do concurso até hoje (SGS 11).
  Só Selic; não inclui IPCA.
- **IR** (`regras_ir.csv`), por ganhador, 30% retido na fonte, tributação exclusiva:
  - até 2007: sobre o prêmio inteiro, se maior que R$ 11,10 (RIR/1999, art. 676);
  - desde 01/01/2008: só sobre o que **excede** a primeira faixa da tabela mensal do IRPF
    (Lei 11.941/2009, art. 56; RIR/2018, art. 35, VII, "i").
  Calculado sobre o valor nominal e depois corrigido pela Selic.

## Pendências que mudam os números

O dashboard mostra um aviso amarelo, por loteria e período, enquanto houver linha com
`conferido` diferente de `sim` em `regras_ir.csv`:

- **A partir de 05/2025:** fontes divergem sobre o limite (R$ 2.428,80 desde 05/2025?) e a Lei 15.270/2025
  (isenção efetiva até R$ 5.000 em 2026) pode ter mudado o limite do art. 56. Confirmar na Receita/Caixa.
- **Antes de 03/1996 (Quina):** regime não verificado.
- **Dupla Sena:** os concursos recentes trazem 8 faixas (os dois sorteios), mas alguns períodos antigos
  trazem 4 ou 6; conferir se o histórico antigo está completo.
- **Super Sete:** a API da Caixa respondeu HTTP 500 na consulta do último concurso; o script usa uma
  busca por sondagem como plano B. Se a loteria seguir falhando, veja o log da tarefa `coletar (supersete)`.
- **Prêmio apurado, não necessariamente pago:** prêmios não resgatados em 90 dias não são descontados.
- O gráfico "Distribuição Estimada da Arrecadação" usa percentuais fixos, não a arrecadação real.
