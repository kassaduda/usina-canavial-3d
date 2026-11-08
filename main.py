"""API do painel da Usina Canavial (FastAPI).

As rotas /api/* devolvem o mesmo formato dos arquivos de dados-exemplo/, mas lendo o
banco SQLite que o simulador alimenta. A raiz (/) serve o painel 3D (index.html) e /2d o painel 2D.

    uvicorn main:app
"""
import asyncio
import re
import time
import unicodedata
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta

from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from banco import (CAPACIDADES, ETAPAS_3D, LIMITES_TEMP, RAIZ, REGRAS, SENSOR_GRAFICO, TEMP_DORNAS, HistoricoSafra,
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


_cache_prev = {"t": -1e9, "dados": []}


@app.get("/api/previsao")
def previsao(s: Session = Sessao):
    """Etanol dos próximos 5 dias (ETS, intervalo de 80%); [] se não der para prever."""
    if time.monotonic() - _cache_prev["t"] > 60:  # o ajuste do modelo é caro: refaz a cada minuto
        linhas = s.execute(select(ProducaoDiaria.dia, ProducaoDiaria.etanol_m3).order_by(ProducaoDiaria.dia)).all()
        _cache_prev.update(t=time.monotonic(), dados=prever_etanol([(d, v) for d, v in linhas]))
    return _cache_prev["dados"]


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


def ocorrencia(o: Ocorrencia) -> dict:
    """Alarme com prioridade e dados de reconhecimento (campos extras além do formato antigo)."""
    ack = o.reconhecido_em
    return dict(id=f"a{o.id}", hora=o.ts.strftime("%H:%M"), nivel=o.nivel, area=o.area, mensagem=o.mensagem,
                prioridade=o.prioridade or "info", ts=o.ts.isoformat(timespec="seconds"),
                reconhecido=ack is not None,
                reconhecido_em=ack.isoformat(timespec="seconds") if ack else None,
                segundos_ate_reconhecer=round((ack - o.ts).total_seconds()) if ack else None)


@app.get("/api/alertas")
def alertas(s: Session = Sessao):
    linhas = s.scalars(select(Ocorrencia).order_by(Ocorrencia.ts.desc(), Ocorrencia.id.desc()).limit(6))
    return [ocorrencia(o) for o in linhas]


@app.get("/api/alarmes")
def alarmes(limite: int = 30, s: Session = Sessao):
    """Histórico de alarmes (mais recentes primeiro) com prioridade e tempo até o reconhecimento."""
    linhas = s.scalars(select(Ocorrencia).order_by(Ocorrencia.ts.desc(), Ocorrencia.id.desc())
                       .limit(max(1, min(limite, 200))))
    return [ocorrencia(o) for o in linhas]


@app.post("/api/alertas/{alerta_id}/reconhecer")
def reconhecer(alerta_id: str, s: Session = Sessao):
    try:
        o = s.get(Ocorrencia, int(alerta_id.lstrip("a")))
    except ValueError:
        o = None
    if o is None:
        raise HTTPException(404, "Alarme não encontrado")
    if o.reconhecido_em is None:  # reconhecer de novo não muda o tempo já registrado
        o.reconhecido_em = datetime.now()
        s.commit()
    return ocorrencia(o)


@app.get("/api/indicadores")
def indicadores(s: Session = Sessao):
    """Indicadores do setor sucroenergético calculados pelo simulador."""
    atuais = leituras_atuais(s)
    itens = [("rendimento", "Rendimento industrial", "Indicadores gerais", "Rendimento industrial", "L/t"),
             ("ef_fermentacao", "Eficiência de fermentação", "Fermentação", "Eficiência de fermentação", "%"),
             ("ef_extracao", "Eficiência de extração", "Recepção e moagem", "Extração", "%"),
             ("perdas", "Perdas totais", "Indicadores gerais", "Perdas totais", "%")]
    return [dict(id=i, nome=n, valor=atuais[(e, sn)], unidade=u) for i, n, e, sn, u in itens if (e, sn) in atuais]


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
                               prioridade=r["prioridade"],
                               mensagem=r["texto"].format(valor=fmt_br(valor, casas))))
    return ativos


NOMES_DORNAS = {f"Temperatura da dorna {n}" for n in TEMP_DORNAS}  # ignora dornas antigas do banco


def montar_cena(atuais: dict, ativos: list[dict]) -> dict:
    """Camadas do painel 3D a partir de um conjunto de leituras (agora ou de um instante do passado)."""
    etapas_cena = {}
    for id_, nome in ETAPAS_3D.items():
        limite, sensor = LIMITES_TEMP[id_]
        if sensor:
            temp = atuais.get((nome, sensor))
        else:  # fermentação: dorna mais quente
            temps = [v for (e, n), v in atuais.items() if e == nome and n in NOMES_DORNAS]
            temp = max(temps) if temps else None
        sensores, capacidade = CAPACIDADES[id_]
        reais = [atuais.get((nome, x)) for x in sensores]
        producao = sum(reais) if None not in reais else None
        etapas_cena[id_] = dict(temperatura=temp, limite_temp=limite, producao=producao,
                                capacidade=capacidade,
                                alertas=[a for a in ativos if a["etapa"] == nome])
    return dict(etapas=etapas_cena, alertas=ativos,
                dornas={n.rsplit(" ", 1)[1]: v for (e, n), v in atuais.items()
                        if e == "Fermentação" and n in NOMES_DORNAS},
                fila_recepcao=atuais.get(("Recepção e moagem", "Fila de recepção")))


@app.get("/api/cena")
def cena(s: Session = Sessao):
    """Dados das camadas do painel 3D: temperatura/limite, produção/capacidade e alertas ativos."""
    atuais = leituras_atuais(s)
    ativos = alertas_ativos(atuais)
    for a in ativos:  # liga o alarme ativo ao registro no banco (para reconhecer)
        o = s.scalars(select(Ocorrencia).where(Ocorrencia.regra == a["id"], Ocorrencia.nivel == "Atenção")
                      .order_by(Ocorrencia.id.desc())).first()
        a.update(alarme_id=f"a{o.id}" if o else None, reconhecido=bool(o and o.reconhecido_em),
                 segundos_ate_reconhecer=ocorrencia(o)["segundos_ate_reconhecer"] if o else None)
    return montar_cena(atuais, ativos)


@app.get("/api/historico")
def historico(minutos: int = 5, detalhe: bool = False, s: Session = Sessao):
    """Série recente do sensor de gráfico de cada etapa (até 120 min).

    Com detalhe=true devolve também um instantâneo por minuto (leituras + camadas), usado
    pela linha do tempo "voltar no tempo".
    """
    minutos = max(1, min(minutos, 120))
    desde = datetime.now() - timedelta(minutes=minutos)
    series = {}
    for id_, (etapa, sensor) in SENSOR_GRAFICO.items():
        linhas = s.execute(select(LeituraSensor.ts, LeituraSensor.valor)
                           .where(LeituraSensor.etapa == etapa, LeituraSensor.sensor == sensor,
                                  LeituraSensor.ts >= desde).order_by(LeituraSensor.ts)).all()
        series[id_] = [dict(ts=t.isoformat(timespec="seconds"), v=v) for t, v in linhas]
    resposta = dict(minutos=minutos, series=series)
    if detalhe:
        base = leituras_atuais(s)  # sensores fixos (capacidade, dornas ativas) não têm série
        por_minuto: dict[datetime, dict] = {}
        for t, e, n, v in s.execute(select(LeituraSensor.ts, LeituraSensor.etapa, LeituraSensor.sensor,
                                           LeituraSensor.valor).where(LeituraSensor.ts >= desde)
                                    .order_by(LeituraSensor.ts)):
            por_minuto.setdefault(t.replace(second=0, microsecond=0), {})[(e, n)] = v
        resposta["instantaneos"] = [
            dict(ts=m.isoformat(timespec="minutes"), v={f"{e}|{n}": v for (e, n), v in atuais.items()},
                 cena=montar_cena(atuais, alertas_ativos(atuais)))
            for m, parcial in sorted(por_minuto.items()) for atuais in [base | parcial]]
    return resposta


# ---- pergunte à usina (regras e palavras-chave, sem serviços externos)

class Pergunta(BaseModel):
    pergunta: str = Field(max_length=300)


def sem_acento(t: str) -> str:
    return unicodedata.normalize("NFD", t.lower()).encode("ascii", "ignore").decode()


def num(v, casas: int | None = None) -> str:
    casas = (0 if float(v).is_integer() else 1) if casas is None else casas
    return f"{v:,.{casas}f}".replace(",", "X").replace(".", ",").replace("X", ".")


PALAVRAS_ETAPA = {"moagem": r"moagem|moenda|recepcao|cana", "caldo": r"caldo|tratamento|ph|brix",
                  "fermentacao": r"fermenta|dorna|levedura", "destilacao": r"destila|coluna|pureza|hidratado|anidro",
                  "expedicao": r"expedi|estoque|tanque|caminh|fila"}
ID_DA_ETAPA = {nome: id_ for id_, nome in ETAPAS_3D.items()}


def tem(texto: str, padrao: str) -> bool:
    return re.search(rf"\b(?:{padrao})", texto) is not None


@app.post("/api/perguntar")
def perguntar(p: Pergunta, s: Session = Sessao):
    q = sem_acento(p.pergunta)
    if tem(q, r"atencao|alerta|alarme|problema|critic|anormal"):
        ativos = alertas_ativos(leituras_atuais(s))
        if not ativos:
            return dict(resposta="Nenhuma etapa está em atenção agora. Tudo dentro dos limites.", etapa=None)
        return dict(resposta="Em atenção agora: " + "; ".join(f"{a['area']} ({a['mensagem']})" for a in ativos) + ".",
                    etapa=ID_DA_ETAPA.get(ativos[0]["etapa"]))
    if tem(q, r"previs|prever|amanha|proximos dias|projec"):
        prev = previsao(s)
        if not prev:
            return dict(resposta="Ainda não há dados suficientes para prever o etanol.", etapa="expedicao")
        dias = ", ".join(f"{date.fromisoformat(x['dia']):%d/%m}: {num(x['previsto'])} m³" for x in prev)
        return dict(resposta=f"Previsão de etanol para os próximos {len(prev)} dias (intervalo de 80%): {dias}.",
                    etapa="expedicao")
    if tem(q, r"rendimento|eficiencia|perda"):
        ind = {i["id"]: i["valor"] for i in indicadores(s)}
        return dict(resposta=f"Rendimento industrial de {num(ind.get('rendimento', 0), 1)} L de etanol por tonelada de cana; "
                             f"eficiência de extração {num(ind.get('ef_extracao', 0), 1)} %, de fermentação "
                             f"{num(ind.get('ef_fermentacao', 0), 1)} %; perdas totais {num(ind.get('perdas', 0), 1)} %.",
                    etapa="fermentacao" if tem(q, r"fermenta") else "destilacao")
    for id_, padrao in PALAVRAS_ETAPA.items():
        if tem(q, padrao):
            e = next(x for x in etapas(s) if x["etapa"] == ETAPAS_3D[id_])
            vals = "; ".join(f"{e[f'ind{i}']}: {num(e[f'val{i}'])} {e[f'un{i}']}" for i in range(1, 5))
            return dict(resposta=f"{e['etapa']} está em {e['status'].lower()}. {e['descricao']} {vals}.", etapa=id_)
    if tem(q, r"produ|hoje|moeu|quanto"):
        d = dias_recentes(s)[-1]
        return dict(resposta=f"Hoje a usina processou {num(d.moagem_t)} t de cana, produziu {num(d.etanol_m3)} m³ de "
                             f"etanol e {num(d.acucar_t)} t de açúcar.", etapa="moagem")
    return dict(resposta="Não entendi. Tente: “o que está em atenção?”, “produção de hoje”, “previsão de etanol” "
                         "ou o estado de uma etapa (“como está a fermentação?”).", etapa=None)


# ---- WebSocket: mesmo conteúdo da consulta, enviado a cada 10 s

def pacote(s: Session) -> dict:
    return dict(etapas=etapas(s), kpis=kpis(s), cena=cena(s), historico=historico(5, False, s),
                indicadores=indicadores(s), alarmes=alarmes(20, s), previsao=previsao(s))


@app.websocket("/ws")
async def ws(socket: WebSocket):
    await socket.accept()
    try:
        while True:
            with Session(engine) as s:
                dados = pacote(s)
            await socket.send_json(dados)
            await asyncio.sleep(10)
    except (WebSocketDisconnect, RuntimeError):
        pass


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
