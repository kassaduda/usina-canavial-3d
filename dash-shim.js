/* Substitui o objeto `dash` que existia só no ambiente do Claude.
   dash.data(id)    -> { status, data } com o JSON de /api/<id>
   dash.onData(fn)  -> fn() roda quando algum dado chega ou muda
   dash.setLink(id) -> guarda a seção atual no #hash da URL
   status: 'loading' | 'ok' | 'error' (HTTP) | 'connect' (sem resposta da API) */
(function () {
  const INTERVALO_MS = 10000;
  const fontes = {};   // id -> { status, data }
  const textos = {};   // id -> JSON da última resposta, para detectar mudança
  const ouvintes = [];
  let agendada = false;

  async function buscar(id) {
    try {
      const r = await fetch('/api/' + id, { cache: 'no-store' });
      if (!r.ok) return fontes[id].status === 'ok' ? false : trocar(id, 'error', [], null);
      const texto = await r.text();
      if (texto === textos[id]) return false;
      return trocar(id, 'ok', JSON.parse(texto), texto);
    } catch (e) {
      // Falha de rede: mantém os últimos dados bons, se houver.
      return fontes[id].status === 'ok' ? false : trocar(id, 'connect', [], null);
    }
  }

  function trocar(id, status, data, texto) {
    if (fontes[id].status === status && status !== 'ok') return false;
    fontes[id] = { status, data };
    textos[id] = texto;
    return true;
  }

  async function rodada() {
    agendada = false;
    if (document.hidden) return;
    const mudou = await Promise.all(Object.keys(fontes).map(buscar));
    if (mudou.some(Boolean)) ouvintes.forEach((fn) => fn());
  }

  function agendar() {
    if (agendada) return;
    agendada = true;
    setTimeout(rodada, 0);
  }

  setInterval(rodada, INTERVALO_MS);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) rodada(); });

  window.dash = {
    data(id) {
      if (!fontes[id]) { fontes[id] = { status: 'loading', data: [] }; agendar(); }
      return fontes[id];
    },
    onData(fn) { ouvintes.push(fn); },
    setLink(id) { try { history.replaceState(null, '', '#' + id); } catch (e) { /* sem hash */ } }
  };
})();
