"""Previsão de produção de etanol para os próximos dias.

Moagem e ATR são projetados com suavização exponencial (ETS) sobre o histórico, mais o perfil
de cada dia da semana e uma variação pequena e estável por data. O etanol sai do balanço de massa
(banco.balanco) com o mix e as perdas atuais, então a previsão acompanha o mix escolhido no painel.
"""
import random
import statistics
import warnings
from datetime import date, timedelta

import pandas as pd
from statsmodels.tsa.exponential_smoothing.ets import ETSModel

from banco import balanco

MIN_DIAS = 10   # abaixo disso o modelo não é confiável
Z_80 = 1.2816   # intervalo de 80%


def _nivel(serie: pd.Series, dias: int) -> list[float]:
    """Projeção ETS (tendência amortecida); se o ajuste falhar, repete a média da última semana."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ajuste = ETSModel(serie, error="add", trend="add", damped_trend=True).fit(disp=False)
            return [float(v) for v in ajuste.forecast(dias)]
    except Exception:
        return [float(serie.tail(7).mean())] * dias


def prever_etanol(historico: list[tuple[str, float, float]], mix_acucar: float, perdas_pct: float,
                  inicio: date | None = None, dias: int = 5) -> list[dict]:
    """historico: (AAAA-MM-DD, moagem_t, atr_kg_t). Devolve [{dia, previsto, minimo, maximo}] a partir de `inicio`.

    Lista vazia se o histórico for curto ou o cálculo falhar.
    """
    try:
        if len(historico) < MIN_DIAS:
            return []
        quadro = pd.DataFrame(historico, columns=["dia", "moagem", "atr"])
        quadro.index = pd.to_datetime(quadro.pop("dia"))
        quadro = quadro.sort_index().asfreq("D").interpolate()
        # perfil semanal: quanto cada dia da semana fica acima/abaixo da média móvel de 7 dias
        relativo = quadro["moagem"] / quadro["moagem"].rolling(7, center=True, min_periods=4).mean()
        perfil = relativo.groupby(relativo.index.dayofweek).mean().fillna(1.0).to_dict()
        # incerteza: variação diária típica de moagem × ATR (proxy do ATR disponível)
        cv = statistics.pstdev((quadro["moagem"] * quadro["atr"]).pct_change().dropna().tail(30)) or 0.02
        moagem, atr = _nivel(quadro["moagem"], dias), _nivel(quadro["atr"], dias)
        inicio = inicio or date.today()
        resultado = []
        for h in range(dias):
            dia = inicio + timedelta(days=h)
            sorteio = random.Random(dia.toordinal())  # mesma data, mesma variação
            m = moagem[h] * perfil.get(dia.weekday(), 1.0) * (1 + sorteio.gauss(0, 0.012))
            a = atr[h] + sorteio.gauss(0, 0.5)
            previsto = balanco(m, a, perdas_pct, mix_acucar)["etanol_m3"]
            margem = previsto * cv * Z_80 * (1 + h) ** 0.5
            resultado.append(dict(dia=dia.isoformat(), previsto=round(previsto),
                                  minimo=max(round(previsto - margem), 0), maximo=round(previsto + margem)))
        return resultado
    except Exception:
        return []
