"""Simulador da Usina Canavial.

Cria o banco usina_canavial.db (se preciso), parte dos JSONs de dados-exemplo/ e,
a cada 10 segundos, grava novas leituras com pequenas variações e gera alertas
por regra (ver REGRAS em banco.py).

    python simulador.py                  # roda até Ctrl+C
    python simulador.py --intervalo 2    # ciclo mais rápido, para testar
    python simulador.py --ciclos 5       # faz 5 ciclos e sai
"""
import argparse
import random
import time
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from banco import (INFO_SORTEIO, REGRAS, HistoricoSafra, LeituraSensor, Ocorrencia, ProducaoDiaria,
                   em_alarme, engine, fmt_br, semear, sensores)

REVERSAO = 0.1           # quanto o valor volta ao alvo a cada ciclo
CICLOS_POR_SAFRA = 6     # um registro de historico_safra por minuto
RETENCAO = timedelta(hours=24)
CHANCE_INFO = 0.003      # chance de um aviso "Info" por ciclo


class Simulador:
    def __init__(self) -> None:
        semear()
        self.specs = {(x["etapa"], x["sensor"]): x for x in sensores()}
        self.valores = {k: x["inicial"] for k, x in self.specs.items()}
        self.em_alarme: dict[str, bool] = {}
        self.ciclo = 0
        self.ultimo = time.monotonic()
        with Session(engine) as s:
            ultimos = select(func.max(LeituraSensor.id)).group_by(LeituraSensor.etapa, LeituraSensor.sensor)
            for r in s.scalars(select(LeituraSensor).where(LeituraSensor.id.in_(ultimos))):
                if (r.etapa, r.sensor) in self.valores:
                    self.valores[(r.etapa, r.sensor)] = r.valor
            self.safra = {}
            for id_ in ("cana", "etanol", "acucar"):
                linha = s.scalars(select(HistoricoSafra).where(HistoricoSafra.indicador_id == id_)
                                  .order_by(HistoricoSafra.id.desc())).first()
                self.safra[id_] = dict(linha=linha, acumulado=linha.acumulado)
                s.expunge(linha)
        # Estado inicial das regras, sem gerar alerta: os alertas de exemplo já cobrem esse caso.
        for r in REGRAS:
            self.em_alarme[r["id"]] = em_alarme(r, self.valores[(r["etapa"], r["sensor"])])
        self.podar()

    def v(self, etapa: str, sensor: str) -> float:
        return self.valores[(etapa, sensor)]

    def variar(self) -> None:
        for chave, x in self.specs.items():
            if not x["ruido"]:
                continue
            novo = self.valores[chave] + REVERSAO * (x["alvo"] - self.valores[chave]) \
                + random.gauss(0, x["ruido"] * x["alvo"])
            self.valores[chave] = round(max(novo, 0), x["casas"])

    def gravar_leituras(self, s: Session, agora: datetime) -> None:
        s.add_all(LeituraSensor(ts=agora, etapa=e, sensor=n, valor=self.valores[(e, n)], unidade=x["unidade"])
                  for (e, n), x in self.specs.items() if x["ruido"])

    def atualizar_producao(self, s: Session) -> None:
        # O último dia da tabela é o dia em andamento: acompanha os sensores.
        dia = s.scalars(select(ProducaoDiaria).order_by(ProducaoDiaria.dia.desc())).first()
        dia.moagem_t = self.v("Recepção e moagem", "Cana processada")
        dia.etanol_m3 = self.v("Destilação", "Hidratado") + self.v("Destilação", "Anidro")
        dia.acucar_t = self.v("Indicadores gerais", "Açúcar produzido")
        dia.atr_kg_t = self.v("Indicadores gerais", "ATR")

    def avaliar_regras(self, s: Session, agora: datetime) -> None:
        for r in REGRAS:
            chave = (r["etapa"], r["sensor"])
            valor = self.valores[chave]
            ativo = self.em_alarme[r["id"]]
            novo = em_alarme(r, valor, ativo)
            if novo == ativo:
                continue
            self.em_alarme[r["id"]] = novo
            texto = (r["texto"] if novo else r["texto_ok"]).format(valor=fmt_br(valor, self.specs[chave]["casas"]))
            s.add(Ocorrencia(ts=agora, nivel="Atenção" if novo else "OK", area=r["area"], mensagem=texto))
        if random.random() < CHANCE_INFO:
            area, texto = random.choice(INFO_SORTEIO)
            s.add(Ocorrencia(ts=agora, nivel="Info", area=area, mensagem=texto))

    def acumular_safra(self, s: Session, agora: datetime, dt: float) -> None:
        taxas = {  # por dia
            "cana": self.v("Recepção e moagem", "Cana processada"),
            "etanol": self.v("Destilação", "Hidratado") + self.v("Destilação", "Anidro"),
            "acucar": self.v("Indicadores gerais", "Açúcar produzido"),
        }
        for id_, estado in self.safra.items():
            estado["acumulado"] += taxas[id_] * dt / 86400
        if self.ciclo % CICLOS_POR_SAFRA == 0:
            for id_, estado in self.safra.items():
                base = estado["linha"]
                s.add(HistoricoSafra(ts=agora, indicador_id=id_, indicador=base.indicador,
                                     acumulado=round(estado["acumulado"], 1), meta=base.meta,
                                     unidade=base.unidade))

    def podar(self) -> None:
        with Session(engine) as s:
            s.execute(delete(LeituraSensor).where(LeituraSensor.ts < datetime.now() - RETENCAO))
            s.commit()

    def passo(self) -> None:
        self.ciclo += 1
        agora, mono = datetime.now(), time.monotonic()
        dt, self.ultimo = mono - self.ultimo, mono
        self.variar()
        with Session(engine) as s:
            self.gravar_leituras(s, agora)
            self.atualizar_producao(s)
            self.avaliar_regras(s, agora)
            self.acumular_safra(s, agora, dt)
            s.commit()
        if self.ciclo % 360 == 0:
            self.podar()
        print(f"[{agora:%H:%M:%S}] ciclo {self.ciclo}: "
              f"cana {self.v('Recepção e moagem', 'Cana processada'):.0f} t/dia, "
              f"dorna 3 {self.v('Fermentação', 'Temperatura da dorna 3'):.1f} °C", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--intervalo", type=float, default=10, help="segundos entre ciclos (padrão: 10)")
    p.add_argument("--ciclos", type=int, default=0, help="parar após N ciclos (padrão: sem fim)")
    args = p.parse_args()

    sim = Simulador()
    print(f"Banco pronto. Gravando a cada {args.intervalo:g} s. Ctrl+C para parar.", flush=True)
    proximo = time.monotonic()
    try:
        while True:
            proximo += args.intervalo
            sim.passo()
            if args.ciclos and sim.ciclo >= args.ciclos:
                break
            time.sleep(max(0, proximo - time.monotonic()))
    except KeyboardInterrupt:
        print("\nSimulador encerrado.")


if __name__ == "__main__":
    main()
