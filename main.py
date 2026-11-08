"""API do painel da Usina Canavial (FastAPI).

As rotas /api/* devolvem o mesmo formato dos arquivos de dados-exemplo/, mas lendo o
banco SQLite que o simulador alimenta. A raiz (/) serve o painel 3D (index.html) e /2d o painel 2D.

    uvicorn main:app --reload
"""
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from banco import (CAPACIDADES, ETAPAS_3D, LIMITES_TEMP, RAIZ, REGRAS, SENSOR_GRAFICO, HistoricoSafra,
                   LeituraSensor, Ocorrencia, ProducaoDiaria, em_alarme, engine, fmt_br, ler_json, semear)
from previsao import prever_etanol

INICIO_SAFRA = date(2026, 4, 11)  # dia 190 da safra = 18/10/2026
DIAS_NO_GRAFICO = 30


@asynccontextmanager
async def ciclo_de_vida(_):
    semear()  # garante o banco mesmo se o simulador ainda não rodou
    yield


app = FastAPI(title="Painel Usina Canavial", lifespan=ciclo_de_vida)


def sessao():
    with Session(engine) as s:
        yield s


Sessao = Depends(sessao)


def leituras_atuais(s: Session) -> dict[tuple[str, str], float]:
    """Última leitura de cada sensor."""
    ultimos = select(func.max(LeituraSensor.id)).group_by(LeituraSensor.etapa, LeituraSensor.sensor)
    return {(r.etapa, r.sensor): r.valor
            for r in s.scalars(select(LeituraSensor).where(LeituraSensor.id.in_(ultimos)))}


def numero(modelo, valor: float):
    """Devolve int quando o dado de exemplo é inteiro, senão 1 casa decimal."""
    return round(valor) if isinstance(modelo, int) else round(valor, 1)


def dias_recentes(s: Session) -> list[ProducaoDiaria]:
    linhas = s.scalars(select(ProducaoDiaria).order_by(ProducaoDiaria.dia.desc()).limit(DIAS_NO_GRAFICO)).all()
    return linhas[::-1]


@app.get("/api/usina")
def usina(s: Session = Sessao):
    dados = ler_json("usina")
    ultimo = dias_recentes(s)[-1].dia
    agora = datetime.now()
    dados[0].update(
        atualizado_em=agora.strftime("%d/%m/%Y %H:%M"),
        dados_ate=date.fromisoformat(ultimo).strftime("%d/%m/%Y"),
        dia_safra=(agora.date() - INICIO_SAFRA).days + 1,
    )
    return dados


@app.get("/api/etapas")
def etapas(s: Session = Sessao):
    atuais = leituras_atuais(s)
    resposta = []
    for e in ler_json("etapas"):
        for i in range(1, 5):
            lido = atuais.get((e["etapa"], e[f"ind{i}"]))
            if lido is not None:  # indicador fixo (ex.: Capacidade nominal) não vem dos sensores
                e[f"val{i}"] = numero(e[f"val{i}"], lido)
        alertas = [r for r in REGRAS if r["etapa"] == e["etapa"]
                   and em_alarme(r, atuais[(r["etapa"], r["sensor"])])]
        e["status"] = "Atenção" if alertas else "Em operação"
        # A frase sobre o limite só aparece enquanto o problema existir.
        e["descricao"] = ". ".join([e["descricao"].split(". ")[0].rstrip(".")]
                                   + [a["detalhe"].rstrip(".") for a in alertas if a["detalhe"]]) + "."
        resposta.append(e)
    return resposta


def situacao(indicador: str, atingimento: float) -> str:
    if indicador == "moagem":
        return "Acima da capacidade" if atingimento > 100 else "Dentro da capacidade"
    if atingimento >= 104:
        return "Acima da meta"
    return "Dentro da meta" if atingimento >= 100 else "Abaixo da meta"


@app.get("/api/kpis")
def kpis(s: Session = Sessao):
    hoje, ontem = dias_recentes(s)[-2:][::-1]
    atuais = leituras_atuais(s)
    valores = {
        "moagem": (hoje.moagem_t, ontem.moagem_t),
        "atr": (hoje.atr_kg_t, ontem.atr_kg_t),
        "etanol": (hoje.etanol_m3, ontem.etanol_m3),
        "acucar": (hoje.acucar_t, ontem.acucar_t),
    }
    resposta = []
    for k in ler_json("kpis"):
        if k["indicador"] in valores:
            atual, anterior = valores[k["indicador"]]
            k["delta_pct"] = round((atual - anterior) / anterior * 100, 1)
        else:  # faturamento: sem histórico no banco, mantém a variação do exemplo
            atual = atuais[("Indicadores gerais", "Faturamento")]
        k["valor"] = numero(k["valor"], atual)
        k["atingimento_pct"] = round(atual / k["meta"] * 100, 1)
        k["situacao"] = situacao(k["indicador"], k["atingimento_pct"])
        resposta.append(k)
    return resposta


@app.get("/api/producao")
def producao(s: Session = Sessao):
    return [dict(dia=d.dia, moagem_t=round(d.moagem_t), etanol_m3=round(d.etanol_m3),
                 acucar_t=round(d.acucar_t), atr_kg_t=round(d.atr_kg_t, 1)) for d in dias_recentes(s)]


@app.get("/api/previsao")
def previsao(s: Session = Sessao):
    """Etanol dos próximos 5 dias (ETS, intervalo de 80%); [] se não der para prever."""
    linhas = s.execute(select(ProducaoDiaria.dia, ProducaoDiaria.etanol_m3).order_by(ProducaoDiaria.dia)).all()
    return prever_etanol([(d, v) for d, v in linhas])


@app.get("/api/mix")
def mix(s: Session = Sessao):
    atuais = leituras_atuais(s)
    itens = ler_json("mix")
    brutos = [atuais[("Mix de produtos", m["produto"])] for m in itens]
    total = sum(brutos)
    pcts = [round(b / total * 100, 1) for b in brutos]
    pcts[-1] = round(100 - sum(pcts[:-1]), 1)  # fecha em 100
    for m, p in zip(itens, pcts):
        m["percentual"] = p
    return itens


@app.get("/api/alertas")
def alertas(s: Session = Sessao):
    linhas = s.scalars(select(Ocorrencia).order_by(Ocorrencia.ts.desc(), Ocorrencia.id.desc()).limit(6))
    return [dict(id=f"a{o.id}", hora=o.ts.strftime("%H:%M"), nivel=o.nivel, area=o.area,
                 mensagem=o.mensagem) for o in linhas]


@app.get("/api/safra")
def safra(s: Session = Sessao):
    resposta = []
    for item in ler_json("safra"):
        h = s.scalars(select(HistoricoSafra).where(HistoricoSafra.indicador_id == item["id"])
                      .order_by(HistoricoSafra.id.desc())).first()
        item.update(acumulado=round(h.acumulado), meta=round(h.meta),
                    progresso_pct=round(h.acumulado / h.meta * 100, 1))
        resposta.append(item)
    return resposta


def alertas_ativos(atuais: dict) -> list[dict]:
    """Regras em alarme agora, calculadas sobre as últimas leituras do banco."""
    ativos = []
    for r in REGRAS:
        valor = atuais.get((r["etapa"], r["sensor"]))
        if valor is not None and em_alarme(r, valor):
            casas = 0 if r["sensor"] == "Pressão de vapor" else 1
            ativos.append(dict(id=r["id"], etapa=r["etapa"], area=r["area"], valor=valor,
                               mensagem=r["texto"].format(valor=fmt_br(valor, casas))))
    return ativos


@app.get("/api/cena")
def cena(s: Session = Sessao):
    """Dados das camadas do painel 3D: temperatura/limite, produção/capacidade e alertas ativos."""
    atuais = leituras_atuais(s)
    ativos = alertas_ativos(atuais)
    etapas_cena = {}
    for id_, nome in ETAPAS_3D.items():
        limite, sensor = LIMITES_TEMP[id_]
        if sensor:
            temp = atuais.get((nome, sensor))
        else:  # fermentação: dorna mais quente
            temps = [v for (e, n), v in atuais.items() if e == nome and n.startswith("Temperatura da dorna")]
            temp = max(temps) if temps else None
        sensores, capacidade = CAPACIDADES[id_]
        reais = [atuais.get((nome, x)) for x in sensores]
        producao = sum(reais) if None not in reais else None
        etapas_cena[id_] = dict(temperatura=temp, limite_temp=limite, producao=producao,
                                capacidade=capacidade,
                                alertas=[a for a in ativos if a["etapa"] == nome])
    return dict(etapas=etapas_cena, alertas=ativos,
                dornas={n.rsplit(" ", 1)[1]: v for (e, n), v in atuais.items()
                        if e == "Fermentação" and n.startswith("Temperatura da dorna")})


@app.get("/api/historico")
def historico(minutos: int = 5, s: Session = Sessao):
    """Série recente do sensor de gráfico de cada etapa (até 120 min)."""
    minutos = max(1, min(minutos, 120))
    desde = datetime.now() - timedelta(minutes=minutos)
    series = {}
    for id_, (etapa, sensor) in SENSOR_GRAFICO.items():
        linhas = s.execute(select(LeituraSensor.ts, LeituraSensor.valor)
                           .where(LeituraSensor.etapa == etapa, LeituraSensor.sensor == sensor,
                                  LeituraSensor.ts >= desde).order_by(LeituraSensor.ts)).all()
        series[id_] = [dict(ts=t.isoformat(timespec="seconds"), v=v) for t, v in linhas]
    return dict(minutos=minutos, series=series)


# ---- arquivos do painel (só img-*.js e dash-shim.js: a pasta também guarda o banco e o .git)

@app.get("/", include_in_schema=False)
def pagina():
    return FileResponse(RAIZ / "index.html")  # painel 3D


@app.get("/2d", include_in_schema=False)
def pagina_2d():
    return FileResponse(RAIZ / "painel2d.html")  # painel 2D antigo


@app.get("/{nome}.js", include_in_schema=False)
def imagem_js(nome: str):
    if not (nome.startswith("img-") or nome == "dash-shim") or "/" in nome or not (RAIZ / f"{nome}.js").is_file():
        raise HTTPException(404)
    return FileResponse(RAIZ / f"{nome}.js", media_type="text/javascript")
