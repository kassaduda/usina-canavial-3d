# Usina Canavial — Painel de Operação em Tempo Real

Painel web que acompanha uma usina de cana-de-açúcar, da moagem à expedição, com indicadores, gráficos e alertas.
Os dados vêm de um simulador que alimenta um banco SQLite, expostos por uma API FastAPI e consumidos pelo painel.

![Painel geral da usina](docs/painel.png)
*Visão geral: fluxo do processo, KPIs, produção por dia e alertas.*

![Previsão de etanol](docs/previsao.png)
*Previsão de etanol para os próximos 5 dias, com intervalo de confiança de 80%.*

![Documentação interativa da API](docs/api-docs.png)
*Documentação interativa da API (Swagger), gerada pelo FastAPI.*

> **Aviso:** todos os dados são **fictícios**, criados apenas para demonstração de portfólio. "Usina Canavial" é uma marca fictícia.

## Funcionalidades

- **Fluxo do processo:** cinco etapas (recepção e moagem, tratamento do caldo, fermentação, destilação e expedição), cada uma com quatro indicadores atualizados a partir das leituras dos sensores e um status ("Em operação" ou "Atenção").
- **KPIs** de moagem, ATR, etanol, açúcar e faturamento, com variação em relação ao dia anterior e situação frente à meta.
- **Produção diária:** gráficos de moagem, etanol e açúcar (duas escalas) e ATR, com seleção de período e tabela ordenável.
- **Previsão de etanol** para os próximos 5 dias, com intervalo de confiança de 80%, usando suavização exponencial (ETS).
  Como os dados são simulados, a previsão demonstra a técnica e não a precisão do modelo.
- **Mix de ATR** e **progresso da safra** frente à meta.
- **Alertas e eventos** gerados por regras sobre os sensores (limites de temperatura, pH e outros), mais avisos informativos aleatórios.
- **Atualização automática:** o painel consulta a API a cada 10 segundos.
- **Simulador** que grava novas leituras a cada 10 segundos, com pequenas variações em torno de valores-alvo.

## Tecnologias

- **Python**
- **FastAPI** (API REST) e **Uvicorn** (servidor)
- **SQLAlchemy** e **SQLite** (persistência)
- **statsmodels** e **pandas** (previsão por ETS)
- **D3.js** (gráficos do painel)

## Arquitetura

```
┌────────────┐  grava leituras  ┌────────────┐  consulta   ┌────────────┐  fetch /api/*  ┌────────────┐
│ simulador  │ ───────────────► │   banco    │ ──────────► │    API     │ ─────────────► │   painel   │
│  (Python)  │   a cada 10 s    │  (SQLite)  │ SQLAlchemy  │ (FastAPI)  │     JSON       │ (HTML + D3)│
└────────────┘                  └────────────┘             └────────────┘                └────────────┘
```

Arquivos principais:

| Arquivo | Papel |
| --- | --- |
| `simulador.py` | Gera leituras, histórico de safra e alertas |
| `banco.py` | Modelos, regras de alarme e criação do banco |
| `main.py` | Rotas `/api/*` e entrega do `index.html` |
| `previsao.py` | Previsão de etanol (ETS) |
| `index.html` | Painel (HTML, CSS e JavaScript com D3.js) |
| `dados-exemplo/` | JSONs de partida e formato de referência das rotas |

## Como rodar

Pré-requisito: Python 3.10 ou superior.

> **Atenção:** o D3.js é carregado por internet (CDN), então o painel precisa de conexão para exibir os gráficos.
> O banco `usina_canavial.db` é criado automaticamente na primeira execução.

**1. Criar o ambiente e instalar as bibliotecas**

```bash
python -m venv .venv

# Windows (PowerShell)
.venv\Scripts\Activate.ps1
# Linux / macOS
source .venv/bin/activate

pip install -r requirements.txt
```

**2. Terminal 1 — iniciar o simulador**

```bash
python simulador.py
```

Opções úteis: `--intervalo 2` (ciclo mais rápido) e `--ciclos 5` (faz 5 ciclos e sai).

**3. Terminal 2 — iniciar a API**

```bash
python -m uvicorn main:app --reload
```

Abra <http://127.0.0.1:8000> para ver o painel. A documentação interativa da API fica em <http://127.0.0.1:8000/docs>.

Lembre de ativar o ambiente virtual também no segundo terminal.

## Glossário

- **ATR (Açúcares Totais Recuperáveis):** quantidade de açúcar da cana que pode ser recuperada no processo, em kg por tonelada de cana. Quanto maior, melhor a qualidade da cana.
- **Brix:** percentual de sólidos solúveis (principalmente açúcares) no caldo. É um indicador da concentração do caldo.
- **Moagem:** etapa em que a cana é esmagada para extrair o caldo. Costuma ser medida em toneladas de cana por dia.
- **Dornas:** tanques onde o caldo é fermentado para produzir etanol.

## Decisões técnicas

- **FastAPI:** gera a documentação interativa (`/docs`) sem esforço extra e tem uma sintaxe enxuta para as rotas, o que deixa a API fácil de ler e de testar.
- **SQLite, com possibilidade de PostgreSQL:** o SQLite não exige instalar nem configurar um servidor, o que facilita rodar o projeto. Como o acesso é feito via SQLAlchemy, trocar para PostgreSQL deve exigir poucas mudanças.
- **ETS (suavização exponencial):** é um modelo simples e rápido para séries diárias, que entrega previsão com intervalo de confiança sem exigir muito histórico.
- **Atualização a cada 10 segundos:** consultar a API em intervalos fixos é simples e suficiente para o ritmo do simulador. Um canal em tempo real (WebSocket ou SSE) só se justificaria com mais usuários ou dados mais rápidos.

## Próximos passos

- Atualização por WebSocket ou SSE no lugar da consulta a cada 10 segundos.
- Testes automatizados para as rotas e para as regras de alarme.
- Filtro de histórico por intervalo de datas e exportação dos dados em CSV.
- Notificações de alertas críticos (e-mail ou mensagem).
- Troca do SQLite por PostgreSQL e empacotamento com Docker.
- Autenticação e perfis de acesso.

## Créditos

Fotos das etapas do processo: [Pexels](https://www.pexels.com), usadas conforme a licença do site.

## Autora

Maria Eduarda Kassa — [LinkedIn](https://www.linkedin.com/in/maria-eduarda-kassa)

## Licença

Distribuído sob a licença MIT. Veja o arquivo [LICENSE](LICENSE).
