"""Banco SQLite da Usina Canavial: modelos, sensores, regras de alerta e carga inicial.

Usado por simulador.py (grava) e main.py (lê). Os JSONs de dados-exemplo/ são o
ponto de partida: viram os primeiros registros e o texto fixo das respostas.
"""
import json
from datetime import datetime
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


def ler_json(nome: str) -> list[dict]:
    return json.loads((PASTA_DADOS / f"{nome}.json").read_text(encoding="utf-8"))


def fmt_br(valor: float, casas: int) -> str:
    return f"{valor:.{casas}f}".replace(".", ",")


# ---------------------------------------------------------------- sensores

# Sensores que não variam (capacidade nominal, número de dornas).
FIXOS = {"Capacidade nominal", "Dornas ativas"}

# Grandezas com limite de alerta próximo do normal: variam menos.
BEM_COMPORTADAS = {"%", "°GL", "pH", "°Bx", "°C", "min"}

TEMP_DORNAS = {1: 31.4, 2: 31.8, 3: 33.1, 4: 31.6, 5: 32.0, 6: 31.5}


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
        ("Indicadores gerais", "Açúcar produzido", "t/dia", 2180, 0, 0.003),
        ("Indicadores gerais", "Faturamento", "R$ mi", 64.8, 1, 0.002),
        ("Utilidades", "Pressão de vapor", "bar", 42.0, 1, 0.006),
        # Indicadores do setor: o simulador recalcula a cada ciclo (ver Simulador.derivados).
        ("Fermentação", "Eficiência de fermentação", "%", 90.5, 1, 0.001),
        ("Indicadores gerais", "Perdas totais", "%", 13.4, 1, 0.001),
        ("Indicadores gerais", "Rendimento industrial", "L/t", 81.0, 1, 0.001),
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
        s.commit()
