"""Banco SQLite da Usina Canavial: modelos, sensores, regras de alerta e carga inicial.

Usado por simulador.py (grava) e main.py (lê). Os JSONs de dados-exemplo/ são o
ponto de partida: viram os primeiros registros e o texto fixo das respostas.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import DateTime, Float, Integer, String, create_engine, event, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

RAIZ = Path(__file__).parent
PASTA_DADOS = RAIZ / "dados-exemplo"
ARQUIVO_DB = RAIZ / "usina_canavial.db"

engine = create_engine(f"sqlite:///{ARQUIVO_DB}", connect_args={"timeout": 15})


@event.listens_for(engine, "connect")
def _modo_wal(conexao, _):
    # WAL deixa o simulador gravar enquanto a API lê.
    conexao.execute("PRAGMA journal_mode=WAL")


class Base(DeclarativeBase):
    pass


class LeituraSensor(Base):
    __tablename__ = "leituras_sensores"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    etapa: Mapped[str] = mapped_column(String(60))
    sensor: Mapped[str] = mapped_column(String(80))
    valor: Mapped[float] = mapped_column(Float)
    unidade: Mapped[str] = mapped_column(String(20))


class ProducaoDiaria(Base):
    __tablename__ = "producao_diaria"
    dia: Mapped[str] = mapped_column(String(10), primary_key=True)  # AAAA-MM-DD
    moagem_t: Mapped[float] = mapped_column(Float)
    etanol_m3: Mapped[float] = mapped_column(Float)
    acucar_t: Mapped[float] = mapped_column(Float)
    atr_kg_t: Mapped[float] = mapped_column(Float)


class Ocorrencia(Base):
    __tablename__ = "registro_ocorrencias"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    nivel: Mapped[str] = mapped_column(String(10))  # Atenção | Info | OK
    area: Mapped[str] = mapped_column(String(40))
    mensagem: Mapped[str] = mapped_column(String(200))
    prioridade: Mapped[str] = mapped_column(String(10), default="info")  # critico | atencao | info
    regra: Mapped[str | None] = mapped_column(String(30), default=None)  # id da regra que gerou
    reconhecido_em: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class HistoricoSafra(Base):
    __tablename__ = "historico_safra"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, index=True)
    indicador_id: Mapped[str] = mapped_column(String(20), index=True)
    indicador: Mapped[str] = mapped_column(String(60))
    acumulado: Mapped[float] = mapped_column(Float)
    meta: Mapped[float] = mapped_column(Float)
    unidade: Mapped[str] = mapped_column(String(10))


class Parametro(Base):
    """Parâmetros de operação ajustáveis pelo painel (ex.: mix açúcar/etanol)."""
    __tablename__ = "parametros"
    chave: Mapped[str] = mapped_column(String(30), primary_key=True)
    valor: Mapped[float] = mapped_column(Float)


# ---------------------------------------------------------------- balanço de massa

VERSAO_BALANCO = 2         # sobe quando a fórmula muda, para recalcular o histórico
MIX_PADRAO = 50.0          # % do ATR recuperado destinado ao açúcar
PERDAS_PLANO = 13.4        # % de perdas totais usada no planejamento (metas)
ATR_META = 138.0           # kg/t
FATOR_ACUCAR = 1.0495      # kg de ATR por kg de açúcar (Consecana)
FATOR_HIDRATADO = 1.6913   # kg de ATR por litro de etanol hidratado
FATOR_ANIDRO = 1.7651      # kg de ATR por litro de etanol anidro
PARTE_HIDRATADO = 0.6      # parcela do ATR do etanol que vira hidratado (o resto, anidro)
PARTE_VHP = 0.8            # parcela do açúcar que é VHP (o resto, cristal)
BAGACO_POR_CANA = 0.27     # t de bagaço por t de cana
VINHACA_POR_ETANOL = 12.0  # L de vinhaça por L de etanol


def balanco(moagem_t: float, atr_kg_t: float, perdas_pct: float, mix_acucar: float) -> dict:
    """Balanço de massa do dia: o ATR recuperado se divide entre açúcar e etanol pelo mix."""
    mix = min(max(mix_acucar, 0.0), 100.0)
    atr_total = moagem_t * atr_kg_t / 1000                      # t de ATR/dia
    # O ATR já desconta as perdas-padrão da indústria; só o que passa (ou falta) da referência muda o resultado.
    atr_rec = atr_total * (1 - perdas_pct / 100) / (1 - PERDAS_PLANO / 100)
    atr_acucar, atr_etanol = atr_rec * mix / 100, atr_rec * (1 - mix / 100)
    hidratado = atr_etanol * PARTE_HIDRATADO / FATOR_HIDRATADO  # t ATR / (kg/L) = m³
    anidro = atr_etanol * (1 - PARTE_HIDRATADO) / FATOR_ANIDRO
    etanol = hidratado + anidro
    return dict(mix_acucar=round(mix, 1), mix_etanol=round(100 - mix, 1), moagem_t=round(moagem_t),
                atr_kg_t=round(atr_kg_t, 1), perdas_pct=round(perdas_pct, 1),
                atr_total_t=round(atr_total, 1), atr_recuperado_t=round(atr_rec, 1),
                acucar_t=round(atr_acucar / FATOR_ACUCAR, 1),
                acucar_vhp_t=round(atr_acucar / FATOR_ACUCAR * PARTE_VHP, 1),
                acucar_cristal_t=round(atr_acucar / FATOR_ACUCAR * (1 - PARTE_VHP), 1),
                etanol_m3=round(etanol, 1), hidratado_m3=round(hidratado, 1), anidro_m3=round(anidro, 1),
                bagaco_t=round(moagem_t * BAGACO_POR_CANA, 1), vinhaca_m3=round(etanol * VINHACA_POR_ETANOL, 1))


# ---------------------------------------------------------------- cogeração

VAPOR_POR_BAGACO = 2.1      # t de vapor por t de bagaço com 50,4% de umidade (caldeira)
VAPOR_POR_MWH = 5.4         # t de vapor por MWh no turbogerador, a 42 bar
POTENCIA_MAX = 110.0        # MW, capacidade do turbogerador
CONSUMO_BASE = 27.0         # kWh/t de cana consumidos pela usina (mais 3 kWh/t com 100% de açúcar)
QUEIMA_PADRAO = 1.0         # fração média do bagaço produzido que vai para a caldeira
ESTOQUE_BAGACO = 15000.0    # t no pátio, valor inicial
ESTOQUE_BAGACO_MAX = 30000.0


def cogeracao(moagem_t: float, umidade_pct: float, pressao_bar: float, mix_acucar: float,
              queimado_t: float | None = None) -> dict:
    """Caldeira e turbogerador: bagaço queimado vira vapor e energia; a usina consome uma parte e exporta o resto."""
    bagaco = moagem_t * BAGACO_POR_CANA
    queimado = bagaco * QUEIMA_PADRAO if queimado_t is None else queimado_t
    vapor = queimado / 24 * VAPOR_POR_BAGACO * (100 - umidade_pct) / 50.4          # t/h
    gerada = min(vapor / VAPOR_POR_MWH * (max(pressao_bar, 1) / 42) ** 0.3, POTENCIA_MAX)
    consumida = moagem_t * (CONSUMO_BASE + 3 * mix_acucar / 100) / 24 / 1000        # MW
    exportada = max(gerada - consumida, 0.0)
    por_t = (lambda mw: mw * 24 * 1000 / moagem_t if moagem_t else 0.0)            # MW -> kWh/t de cana
    return dict(bagaco_produzido_t=round(bagaco, 1), bagaco_queimado_t=round(queimado, 1),
                bagaco_sobra_t=round(bagaco - queimado, 1), vapor_t_h=round(vapor, 1),
                gerada_mw=round(gerada, 1), consumida_mw=round(consumida, 1), exportada_mw=round(exportada, 1),
                capacidade_mw=POTENCIA_MAX, gerada_kwh_t=round(por_t(gerada), 1),
                consumida_kwh_t=round(por_t(consumida), 1), exportada_kwh_t=round(por_t(exportada), 1))


def mix_produtos(mix_acucar: float) -> dict[str, float]:
    """Participação (% do ATR) de cada produto do mix, na ordem de mix.json."""
    a = mix_acucar
    return {"Etanol hidratado": (100 - a) * PARTE_HIDRATADO, "Etanol anidro": (100 - a) * (1 - PARTE_HIDRATADO),
            "Açúcar VHP": a * PARTE_VHP, "Açúcar cristal": a * (1 - PARTE_VHP)}


def ler_mix(s: Session) -> float:
    p = s.get(Parametro, "mix_acucar")
    return p.valor if p else MIX_PADRAO


def ler_json(nome: str) -> list[dict]:
    return json.loads((PASTA_DADOS / f"{nome}.json").read_text(encoding="utf-8"))


def fmt_br(valor: float, casas: int) -> str:
    return f"{valor:.{casas}f}".replace(".", ",")


# ---------------------------------------------------------------- sensores

# Sensores que não variam (capacidade nominal, número de dornas).
FIXOS = {"Capacidade nominal", "Dornas ativas"}

# Grandezas com limite de alerta próximo do normal: variam menos.
BEM_COMPORTADAS = {"%", "°GL", "pH", "°Bx", "°C", "min"}

TEMP_DORNAS = {1: 31.4, 2: 31.8, 3: 33.1, 4: 31.6}  # 4 dornas, como na cena 3D


def sensores() -> list[dict]:
    """Lista de sensores: etapa, nome, unidade, valor inicial, alvo e ruído relativo.

    Os quatro indicadores de cada etapa vêm de etapas.json; os demais são extras
    usados pelos KPIs, pelo mix e pelas regras de alerta.
    """
    lista = []
    for e in ler_json("etapas"):
        for i in range(1, 5):
            valor = e[f"val{i}"]
            lista.append(dict(
                etapa=e["etapa"], sensor=e[f"ind{i}"], unidade=e[f"un{i}"], inicial=valor,
                alvo=valor, casas=0 if isinstance(valor, int) else 1,
                ruido=0 if e[f"ind{i}"] in FIXOS else (0.001 if e[f"un{i}"] in BEM_COMPORTADAS else 0.003),
            ))
    for n, temp in TEMP_DORNAS.items():
        lista.append(dict(
            etapa="Fermentação", sensor=f"Temperatura da dorna {n}", unidade="°C",
            inicial=temp, alvo=32.3 if n == 3 else temp, casas=1, ruido=0.0045,
        ))
    extras = [
        ("Indicadores gerais", "ATR", "kg/t", 141.2, 1, 0.002),
        ("Indicadores gerais", "Açúcar produzido", "t/dia", 1413, 0, 0.003),
        ("Indicadores gerais", "Faturamento", "R$ mi", 64.8, 1, 0.002),
        ("Utilidades", "Pressão de vapor", "bar", 42.0, 1, 0.006),
        # Cogeração: o simulador recalcula a cada ciclo (ver Simulador.cogerar).
        ("Utilidades", "Estoque de bagaço", "t", 15000, 0, 0.001),
        ("Utilidades", "Bagaço queimado", "t/dia", 5670, 0, 0.001),
        ("Utilidades", "Energia gerada", "MW", 91.9, 1, 0.001),
        ("Utilidades", "Energia consumida", "MW", 24.9, 1, 0.001),
        ("Utilidades", "Energia exportada", "MW", 67.0, 1, 0.001),
        # Indicadores do setor: o simulador recalcula a cada ciclo (ver Simulador.derivados).
        ("Fermentação", "Eficiência de fermentação", "%", 90.5, 1, 0.001),
        ("Indicadores gerais", "Perdas totais", "%", 13.4, 1, 0.001),
        ("Indicadores gerais", "Rendimento industrial", "L/t", 41.0, 1, 0.001),
        ("Recepção e moagem", "Fila de recepção", "caminhões", 5, 0, 0.001),
        # Temperaturas que faltavam para a camada de temperatura do painel 3D.
        ("Recepção e moagem", "Temperatura do caldo", "°C", 32.5, 1, 0.01),
        ("Destilação", "Temperatura da coluna", "°C", 78.0, 1, 0.004),
        ("Expedição", "Temperatura do tanque", "°C", 28.5, 1, 0.01),
    ]
    for etapa, nome, un, valor, casas, ruido in extras:
        lista.append(dict(etapa=etapa, sensor=nome, unidade=un, inicial=valor,
                          alvo=valor, casas=casas, ruido=ruido))
    for m in ler_json("mix"):
        lista.append(dict(etapa="Mix de produtos", sensor=m["produto"], unidade="%",
                          inicial=m["percentual"], alvo=m["percentual"], casas=1, ruido=0.01))
    return lista


# ---------------------------------------------------------------- limites do painel 3D

# id da etapa no painel -> nome da etapa no banco.
ETAPAS_3D = {"moagem": "Recepção e moagem", "caldo": "Tratamento do caldo", "fermentacao": "Fermentação",
             "destilacao": "Destilação", "expedicao": "Expedição"}

# Limite de temperatura (°C) de cada etapa e sensor de onde sai a leitura.
# Na fermentação vale a dorna mais quente.
LIMITES_TEMP = {
    "moagem": (40.0, "Temperatura do caldo"), "caldo": (90.0, "Temperatura"),
    "fermentacao": (33.0, None), "destilacao": (85.0, "Temperatura da coluna"),
    "expedicao": (40.0, "Temperatura do tanque"),
}

# Produção real (soma dos sensores) e capacidade nominal, na mesma unidade.
CAPACIDADES = {
    "moagem": (["Cana processada"], 24000),          # t/dia
    "caldo": (["Vazão"], 700),                       # m³/h
    "fermentacao": (["Volume em dornas"], 350000),   # L
    "destilacao": (["Hidratado", "Anidro"], 1800),   # m³/dia
    "expedicao": (["Etanol em tanque"], 22000),      # m³
}

# Sensor mostrado no gráfico de cada etapa.
SENSOR_GRAFICO = {
    "moagem": ("Indicadores gerais", "ATR"), "caldo": ("Tratamento do caldo", "Brix médio"),
    "fermentacao": ("Fermentação", "Temperatura da dorna 3"),
    "destilacao": ("Destilação", "Pureza do hidratado"), "expedicao": ("Expedição", "Etanol em tanque"),
}


# ---------------------------------------------------------------- regras

def _regra(id, etapa, sensor, area, tipo, limite, texto, texto_ok, histerese, detalhe="", prioridade="atencao"):
    return dict(id=id, etapa=etapa, sensor=sensor, area=area, tipo=tipo, limite=limite,
                texto=texto, texto_ok=texto_ok, histerese=histerese, detalhe=detalhe, prioridade=prioridade)


REGRAS = [
    *[
        _regra(f"dorna{n}", "Fermentação", f"Temperatura da dorna {n}", "Fermentação", "max", 33.0,
               f"Temperatura da dorna {n} acima do limite ({{valor}} °C)",
               f"Temperatura da dorna {n} normalizada ({{valor}} °C)", 0.3,
               detalhe=f"A dorna {n} está acima do limite de temperatura.")
        for n in TEMP_DORNAS
    ],
    _regra("ph_baixo", "Tratamento do caldo", "pH do caldo", "Tratamento", "min", 5.0,
           "pH do caldo abaixo da faixa ({valor})", "pH do caldo de volta à faixa ({valor})", 0.1,
           prioridade="critico"),
    _regra("ph_alto", "Tratamento do caldo", "pH do caldo", "Tratamento", "max", 5.8,
           "pH do caldo acima da faixa ({valor})", "pH do caldo de volta à faixa ({valor})", 0.1),
    _regra("extracao", "Recepção e moagem", "Extração", "Moagem", "min", 94.5,
           "Extração abaixo do esperado ({valor} %)", "Extração recuperada ({valor} %)", 0.3),
    _regra("umidade_bagaco", "Recepção e moagem", "Umidade do bagaço", "Moagem", "max", 52.0,
           "Umidade do bagaço alta ({valor} %)", "Umidade do bagaço normalizada ({valor} %)", 0.5),
    _regra("vapor", "Utilidades", "Pressão de vapor", "Caldeira", "min", 40.0,
           "Pressão de vapor baixa ({valor} bar)", "Pressão de vapor estabilizada em {valor} bar", 0.5,
           prioridade="critico"),
    _regra("pureza", "Destilação", "Pureza do hidratado", "Destilação", "min", 95.5,
           "Pureza do hidratado abaixo do limite ({valor} °GL)",
           "Pureza do hidratado normalizada ({valor} °GL)", 0.2, prioridade="critico"),
    _regra("ef_ferm", "Fermentação", "Eficiência de fermentação", "Fermentação", "min", 88.0,
           "Eficiência de fermentação baixa ({valor} %)", "Eficiência de fermentação recuperada ({valor} %)", 0.3),
    _regra("fila", "Expedição", "Fila média", "Expedição", "max", 60.0,
           "Fila de caminhões maior que a média no turno ({valor} min)",
           "Fila de caminhões normalizada ({valor} min)", 5.0),
]


def em_alarme(regra: dict, valor: float, ativo: bool = False) -> bool:
    """Compara com o limite; se já estava em alarme, só sai depois da histerese."""
    folga = regra["histerese"] if ativo else 0
    if regra["tipo"] == "max":
        return valor > regra["limite"] - folga
    return valor < regra["limite"] + folga


# ---------------------------------------------------------------- alarme por tendência

JANELA_TENDENCIA = timedelta(minutes=2)  # a inclinação sai dos últimos minutos de leituras
HORIZONTE_MIN = 10.0                     # só avisa se o limite for cruzado em até 10 min
MIN_PONTOS = 6
MIN_R2 = 0.8                             # descarta inclinação que é só ruído
INCLINACAO_MIN = 0.002                   # fração do limite por minuto abaixo da qual a série está "parada"

# Causa provável e ação sugerida de cada regra; valem para o alarme de limite e para o de tendência.
CAUSAS = {
    "ph_baixo": ("Excesso de ácido ou pouca cal na calagem do caldo",
                 "Conferir a dosagem de cal e corrigir o pH do caldo antes da decantação"),
    "ph_alto": ("Excesso de cal na calagem",
                "Reduzir a dosagem de cal e conferir a leitura do pHmetro"),
    "extracao": ("Moenda desregulada ou embuchamento; cana muito fibrosa",
                 "Ajustar pressão e rotação dos rolos, conferir a embebição e a preparação da cana"),
    "umidade_bagaco": ("Embebição excessiva ou pressão baixa nos rolos da última moenda",
                       "Reduzir a embebição e aumentar a pressão da última moenda"),
    "vapor": ("Pouco bagaço na caldeira ou demanda de vapor acima da geração",
              "Aumentar a alimentação de bagaço, conferir a queima e adiar cargas de vapor não essenciais"),
    "pureza": ("Refluxo baixo ou instabilidade de vapor na coluna",
               "Aumentar o refluxo da coluna e estabilizar o vapor de aquecimento"),
    "ef_ferm": ("Temperatura alta nas dornas ou levedura estressada",
                "Resfriar as dornas, conferir viabilidade da levedura e acidez do mosto"),
    "fila": ("Poucos pontos de carregamento ou atraso na liberação dos caminhões",
             "Abrir mais um ponto de carregamento e reprogramar os agendamentos"),
}
for _n in TEMP_DORNAS:
    CAUSAS[f"dorna{_n}"] = ("Fermentação acelerada ou pouca água de resfriamento no trocador da dorna",
                            f"Aumentar a vazão de água de resfriamento da dorna {_n} e conferir o trocador de calor")


def causa_acao(regra_id: str | None) -> dict:
    """Causa provável e ação sugerida de uma regra (aceita o prefixo tend_ do alarme por tendência)."""
    c = CAUSAS.get((regra_id or "").removeprefix("tend_"))
    return dict(causa=c[0], acao=c[1]) if c else {}


def inclinacao(pontos: list[tuple[float, float]]) -> tuple[float, float] | None:
    """Reta de mínimos quadrados de (segundos, valor): devolve (inclinação por minuto, R²)."""
    n = len(pontos)
    if n < MIN_PONTOS:
        return None
    mt, mv = sum(p[0] for p in pontos) / n, sum(p[1] for p in pontos) / n
    stt = sum((t - mt) ** 2 for t, _ in pontos)
    svv = sum((v - mv) ** 2 for _, v in pontos)
    if stt == 0 or svv == 0:
        return None
    stv = sum((t - mt) * (v - mv) for t, v in pontos)
    return stv / stt * 60, stv * stv / (stt * svv)


def tendencia(regra: dict, pontos: list[tuple[float, float]]) -> dict | None:
    """Prevê quando a série cruza o limite da regra, se continuar na inclinação dos últimos minutos."""
    reta = inclinacao(pontos)
    if not reta:
        return None
    por_min, r2 = reta
    atual = pontos[-1][1]
    if em_alarme(regra, atual) or r2 < MIN_R2:
        return None
    sentido = 1 if regra["tipo"] == "max" else -1
    ritmo = por_min * sentido  # positivo = aproximando-se do limite
    if ritmo < INCLINACAO_MIN * abs(regra["limite"]):
        return None
    minutos = abs(regra["limite"] - atual) / ritmo
    if minutos > HORIZONTE_MIN:
        return None
    return dict(minutos=round(minutos, 1), por_minuto=round(por_min, 2), r2=round(r2, 2))


def tendencias(s: Session, agora: datetime | None = None) -> dict[str, dict]:
    """Regras que vão passar do limite em breve, pela inclinação das leituras da janela recente."""
    agora = agora or datetime.now()
    desde = agora - JANELA_TENDENCIA
    series: dict[tuple[str, str], list[tuple[float, float]]] = {}
    for ts, e, n, v in s.execute(select(LeituraSensor.ts, LeituraSensor.etapa, LeituraSensor.sensor, LeituraSensor.valor)
                                 .where(LeituraSensor.ts >= desde).order_by(LeituraSensor.ts)):
        series.setdefault((e, n), []).append((ts.timestamp(), v))
    achadas = {}
    for r in REGRAS:
        t = tendencia(r, series.get((r["etapa"], r["sensor"]), []))
        if t:
            casas = 1 if r["sensor"] != "Pressão de vapor" else 0
            lado = "passar de" if r["tipo"] == "max" else "cair abaixo de"
            t["mensagem"] = (f"{r['sensor']} deve {lado} {fmt_br(r['limite'], casas)} em ~{fmt_br(t['minutos'], 0)} min "
                             f"({'+' if t['por_minuto'] > 0 else ''}{fmt_br(t['por_minuto'], 2)} por min)")
            achadas[r["id"]] = t
    return achadas


# ---------------------------------------------------------------- carga

INFO_SORTEIO = [
    ("Moagem", "Parada breve na moenda 2 para limpeza de facas"),
    ("Cogeração", "Exportação de energia acima da programação"),
    ("Expedição", "Troca de turno no carregamento de caminhões"),
    ("Destilação", "Ajuste de refluxo na coluna B"),
]


def migrar() -> None:
    """Acrescenta ao banco antigo as colunas de gestão de alarmes (create_all não altera tabelas)."""
    with engine.begin() as c:
        colunas = {r[1] for r in c.exec_driver_sql("PRAGMA table_info(registro_ocorrencias)")}
        if "prioridade" not in colunas:
            c.exec_driver_sql("ALTER TABLE registro_ocorrencias ADD COLUMN prioridade VARCHAR(10) DEFAULT 'info'")
            c.exec_driver_sql("UPDATE registro_ocorrencias SET prioridade='atencao' WHERE nivel='Atenção'")
        if "regra" not in colunas:
            c.exec_driver_sql("ALTER TABLE registro_ocorrencias ADD COLUMN regra VARCHAR(30)")
            c.exec_driver_sql("UPDATE registro_ocorrencias SET regra='dorna3' WHERE nivel='Atenção' "
                              "AND mensagem LIKE 'Temperatura da dorna 3%'")
        if "reconhecido_em" not in colunas:
            c.exec_driver_sql("ALTER TABLE registro_ocorrencias ADD COLUMN reconhecido_em DATETIME")


def semear() -> None:
    """Cria as tabelas e preenche as vazias com os JSONs de dados-exemplo."""
    Base.metadata.create_all(engine)
    migrar()
    agora = datetime.now()
    with Session(engine) as s:
        if not s.scalar(select(func.count()).select_from(LeituraSensor)):
            s.add_all(LeituraSensor(ts=agora, etapa=x["etapa"], sensor=x["sensor"],
                                    valor=x["inicial"], unidade=x["unidade"]) for x in sensores())
        if not s.scalar(select(func.count()).select_from(ProducaoDiaria)):
            s.add_all(ProducaoDiaria(**linha) for linha in ler_json("producao"))
        if not s.scalar(select(func.count()).select_from(Ocorrencia)):
            data = datetime.strptime(ler_json("usina")[0]["atualizado_em"], "%d/%m/%Y %H:%M")
            # Do mais antigo para o mais novo, para o id crescer com o tempo.
            for a in reversed(ler_json("alertas")):
                h, m = map(int, a["hora"].split(":"))
                s.add(Ocorrencia(ts=data.replace(hour=h, minute=m), nivel=a["nivel"],
                                 area=a["area"], mensagem=a["mensagem"]))
        if not s.scalar(select(func.count()).select_from(HistoricoSafra)):
            s.add_all(HistoricoSafra(
                ts=agora, indicador_id=x["id"], indicador=x["indicador"],
                acumulado=x["acumulado"], meta=x["meta"], unidade=x["unidade"],
            ) for x in ler_json("safra"))
        if s.get(Parametro, "mix_acucar") is None:
            s.add(Parametro(chave="mix_acucar", valor=MIX_PADRAO))
        versao = s.get(Parametro, "versao_balanco")
        if versao is None or versao.valor < VERSAO_BALANCO:
            # Balanço novo ou corrigido: recalcula etanol e açúcar do histórico pela moagem e ATR.
            if versao is None:
                s.add(Parametro(chave="versao_balanco", valor=VERSAO_BALANCO))
            else:
                versao.valor = VERSAO_BALANCO
            for d in s.scalars(select(ProducaoDiaria)):
                b = balanco(d.moagem_t, d.atr_kg_t, PERDAS_PLANO, MIX_PADRAO)
                d.etanol_m3, d.acucar_t = round(b["etanol_m3"]), round(b["acucar_t"])
        s.commit()
