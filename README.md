# Histórico das Loterias da Caixa

Dashboard estático (GitHub Pages) que mostra, para cada loteria, os prêmios brutos pagos por faixa,
corrigidos pela Selic até hoje, o IR arrecadado e os totais por mês e por ano.
Hoje: **Mega-Sena**. Atualiza sozinho todo dia à meia-noite (Brasília).

## Como funciona

```
GitHub Actions (cron diário)
  └─ scripts/update.py
       ├─ baixa só os concursos novos da Caixa      → data/raw/megasena.json
       ├─ baixa só os dias novos da Selic (BCB 11)  → data/selic.csv
       └─ recalcula tudo                             → docs/data/megasena/*.json, faixas.csv
  └─ commit automático  → GitHub Pages serve docs/
```

Todo dia os valores corrigidos mudam (a Selic acumula mais um dia), então sempre há commit.

## Publicar (uma vez só)

1. Crie um repositório no GitHub e envie esta pasta.
2. **Settings → Pages**: *Deploy from a branch* → `main` → pasta `/docs`.
3. **Settings → Actions → General → Workflow permissions**: *Read and write permissions*.
4. **Actions → Atualizar loterias → Run workflow**. A primeira rodada baixa todos os concursos
   (alguns minutos). Depois o site fica em `https://<usuario>.github.io/<repositorio>/`.

Se o GitHub pausar o agendamento por inatividade do repositório, reative em Actions.

## Rodar local

```
pip install -r requirements.txt
python scripts/update.py                 # baixa e calcula
python -m http.server -d docs 8000       # abre http://localhost:8000
python tests/test_pipeline.py            # teste sem rede, dados sintéticos
```

## Método de cálculo

- **Bruto da faixa** = ganhadores × prêmio por ganhador (valor informado pela Caixa; tratado como bruto,
  antes do IR).
- **Fator Selic** = produto de (1 + taxa_dia/100) dos dias **após** a data do concurso até hoje (SGS 11).
- **IR** (`regras_ir.csv`): se o prêmio por ganhador for maior que o limite de isenção vigente na data,
  incide a alíquota sobre o prêmio inteiro daquele ganhador. Calculado sobre o valor nominal e depois
  corrigido pela Selic (as duas versões aparecem no dashboard).

## Pendências que mudam os números

- **`regras_ir.csv` não está conferido.** Alíquota de 30% retida na fonte pela Caixa, sobre o valor
  total do prêmio, com tributação exclusiva: confirmado em reportagens. **Limite de isenção por
  período: não confirmado** (os valores do arquivo são de memória). Enquanto houver linha com
  `conferido` diferente de `sim`, o dashboard mostra um aviso. Corrija o arquivo e troque para `sim`.
- **Valor bruto vs. líquido:** uma das reportagens consultadas trata o prêmio divulgado como líquido
  (70% do bruto). Confira um concurso conhecido contra o valor que o ganhador recebeu.
- **Prêmio apurado, não necessariamente pago:** prêmios não resgatados em 90 dias não são descontados.

## Adicionar outra loteria

Em `scripts/update.py`, acrescente a entrada em `LOTERIAS` (slug da API da Caixa, como `quina` ou
`lotofacil`). O dashboard cria a aba sozinho. Confirme se a regra de IR é a mesma para ela.
