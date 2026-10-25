"""Previsão de produção de etanol com suavização exponencial (ETS)."""
import warnings
from datetime import date

import pandas as pd
from statsmodels.tsa.exponential_smoothing.ets import ETSModel

MIN_DIAS = 10  # abaixo disso o modelo não é confiável


def prever_etanol(historico: list[tuple[str, float]], dias: int = 5, confianca: float = 0.8) -> list[dict]:
    """historico: pares (AAAA-MM-DD, etanol_m3). Devolve [{dia, previsto, minimo, maximo}].

    Lista vazia se o histórico for curto ou o modelo falhar.
    """
    try:
        if len(historico) < MIN_DIAS:
            return []
        serie = pd.Series({pd.Timestamp(d): v for d, v in historico}).sort_index()
        serie = serie.asfreq("D").interpolate()  # dias faltando viram interpolação
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            ajuste = ETSModel(serie, error="add", trend="add", damped_trend=True).fit(disp=False)
            quadro = ajuste.get_prediction(start=len(serie), end=len(serie) + dias - 1) \
                .summary_frame(alpha=1 - confianca)
        resultado = []
        for dia, linha in quadro.iterrows():
            previsto, minimo, maximo = (round(float(linha[c])) for c in ("mean", "pi_lower", "pi_upper"))
            if None in (previsto, minimo, maximo) or not minimo <= previsto <= maximo:
                return []
            resultado.append(dict(dia=dia.date().isoformat(), previsto=previsto,
                                  minimo=max(minimo, 0), maximo=maximo))
        return resultado
    except Exception:
        return []
