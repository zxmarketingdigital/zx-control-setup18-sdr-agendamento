(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  const labels = {preco: 'Preço', tempo: 'Tempo', confianca: 'Confiança', concorrente: 'Concorrente', decisor: 'Decisor', 'vou pensar': 'Vou pensar', sem_orcamento: 'Sem orçamento'};
  const label = category => Object.prototype.hasOwnProperty.call(labels, category) ? labels[category] : category || 'Não informado';
  SDR.views = SDR.views || {};
  SDR.views.objecoes = {
    title: 'Objeções',
    async render(root, ctx) {
      let disposed = false, revision = 0, days = 30, category = '', stats = {}, excerpts = [], busy = false;
      let periodFrom = 0, periodTo = 0;
      const pending = new Set();
      SDR.mount(root, SDR.html`<div class="ob-view stack"><div class="row ob-toolbar"><div><h2>O que impede o próximo passo?</h2><p class="muted">Use os trechos reais para melhorar as respostas do agente.</p></div><div class="field"><label class="label" for="ob-period">Período</label><select class="select" id="ob-period"><option value="7">Últimos 7 dias</option><option value="30" selected>Últimos 30 dias</option><option value="90">Últimos 90 dias</option></select></div></div><div class="ob-content stack" aria-live="polite"></div></div>`);
      const content = SDR.qs('.ob-content', root), period = SDR.qs('#ob-period', root);
      function loading() { SDR.mount(content, SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`); }
      function showError(error) { SDR.mount(content, SDR.html`<div class="banner banner-error" role="alert"><p>${error.message || 'Não foi possível carregar as objeções.'}</p><button type="button" class="btn btn-ghost" data-ob-action="retry">Tentar de novo</button></div>`); }
      function bars(rows, reason) {
        const maximum = Math.max(1, ...rows.map(row => Number(row.count) || 0));
        if (!rows.length) return SDR.html`<div class="empty">${reason ? 'Nenhum motivo de desqualificação foi registrado neste período.' : 'Nenhuma objeção foi registrada neste período. As categorias aparecem quando houver conversas com objeções.'}</div>`;
        return SDR.html`<div class="ob-ranking">${rows.map(row => {
          const count = Math.max(0, Number(row.count) || 0), handled = Math.max(0, Math.min(count, Number(row.tratadas) || 0));
          const name = reason ? row.motivo || 'Não informado' : label(row.category);
          const body = SDR.html`<span class="row ob-bar-label"><strong>${name}</strong><span class="mono">${SDR.fmt.int(count)}</span></span><progress class="ob-bar" max="${maximum}" value="${count}" aria-label="${name}: ${count} registros"></progress>${reason ? SDR.raw('') : SDR.html`<span class="muted ob-caption"><span class="mono">${SDR.fmt.pct(count ? handled / count : 0)}</span> tratadas · ${SDR.fmt.int(handled)} de ${SDR.fmt.int(count)}</span>`}`;
          return reason ? SDR.html`<div class="ob-rank-item">${body}</div>` : SDR.html`<button class="ob-rank-item ob-category" type="button" data-ob-category="${row.category}" aria-pressed="${category === row.category}">${body}</button>`;
        })}</div>`;
      }
      function draw() {
        const rows = excerpts.filter(row => {
          const date = Date.parse(row.created_at);
          return date >= periodFrom && date < periodTo && (!category || row.category === category);
        });
        SDR.mount(content, SDR.html`<div class="grid ob-columns"><section class="card stack"><h3>Ranking de objeções</h3>${bars(Array.isArray(stats.objecoes) ? stats.objecoes : [], false)}<p class="muted ob-caption">Ajuste o playbook na tela Configuração com base nas categorias mais frequentes e nas respostas que ainda precisam de atenção.</p><button type="button" class="btn btn-ghost btn-sm" data-ob-action="config">Abrir Configuração</button></section><section class="card stack"><h3>Motivos de desqualificação</h3>${bars(Array.isArray(stats.desqualificacao) ? stats.desqualificacao : [], true)}</section></div><section class="card stack"><div class="row ob-toolbar"><div><h3>Trechos das conversas${category ? ' · ' + label(category) : ''}</h3><p class="muted ob-caption"><span class="mono">${SDR.fmt.int(rows.length)}</span> trechos carregados no período.</p></div>${category ? SDR.html`<button type="button" class="btn btn-ghost btn-sm" data-ob-action="clear">Todas as categorias</button>` : SDR.raw('')}</div>${excerpts.length >= 1000 ? SDR.html`<div class="banner">Mostrando apenas os 1000 trechos mais recentes do período; existem mais. O ranking considera o período completo. Escolha um período menor para ver os demais.</div>` : SDR.raw('')}${rows.length ? SDR.html`<div class="ob-excerpts">${rows.map(row => SDR.html`<article class="ob-excerpt"><div class="row ob-toolbar"><span class="badge badge-amber">${label(row.category)}</span><time class="mono muted" datetime="${row.created_at}">${SDR.fmt.dateTime(row.created_at, ctx.state.tz)}</time></div><blockquote>“${row.excerpt || 'Trecho não informado.'}”</blockquote><div class="row ob-toolbar"><button type="button" class="btn btn-ghost btn-sm ob-lead" data-ob-lead="${row.lead_id}">${row.lead && (row.lead.name || SDR.fmt.phone(row.lead.phone)) || 'Contato não disponível'}</button><label class="ob-switch"><input type="checkbox" role="switch" data-ob-handled="${row.id}" ${row.handled ? SDR.raw('checked') : SDR.raw('')} ${pending.has(String(row.id)) ? SDR.raw('disabled') : SDR.raw('')}> Tratada</label></div></article>`)}</div>` : SDR.html`<div class="empty">${category ? 'Não há trechos carregados desta categoria no período selecionado. Escolha outra categoria ou amplie o período.' : 'Ainda não há trechos de objeções neste período. Eles aparecerão aqui após serem registrados nas conversas.'}</div>`}</section>`);
      }
      async function load() {
        const ticket = ++revision;
        busy = true; loading(); period.disabled = true;
        const to = new Date(), from = SDR.tzTools.addDays(SDR.tzTools.startOfDay(to, ctx.state.tz), -(days - 1), ctx.state.tz);
        try {
          // Ranking pela RPC e trechos filtrados por período no servidor (teto do servidor: 1000).
          const results = await Promise.all([ctx.api.dashboardStats(from.toISOString(), to.toISOString()), ctx.api.listObjections({limit: 1000, from: from.toISOString(), to: to.toISOString()})]);
          if (disposed || ticket !== revision) return;
          stats = results[0] || {}; excerpts = results[1] || []; periodFrom = from.getTime(); periodTo = to.getTime();
          draw();
        } catch (error) { if (!disposed && ticket === revision) showError(error); }
        finally { if (!disposed && ticket === revision) { busy = false; period.disabled = false; } }
      }
      async function handleChange(event) {
        if (event.target === period) { days = Number(period.value); category = ''; await load(); return; }
        const input = event.target.closest('[data-ob-handled]');
        if (!input || busy) return;
        const id = input.dataset.obHandled, row = excerpts.find(item => String(item.id) === id);
        if (!row || pending.has(id)) return;
        const value = input.checked; pending.add(id); input.disabled = true;
        try {
          await ctx.api.setObjectionHandled(row.id, value);
          if (disposed) return;
          row.handled = value;
          pending.delete(id);
          ctx.toast(value ? 'Objeção marcada como tratada.' : 'Objeção marcada como pendente.', 'ok');
          // Reconsulta a fonte: outra alteração ou troca de período pode ter ocorrido
          // enquanto a gravação estava em andamento. Não soma em um ranking obsoleto.
          await load();
        } catch (error) { if (!disposed) { input.checked = row.handled; ctx.toast(error.message, 'erro'); } }
        finally { pending.delete(id); if (!disposed) input.disabled = false; }
      }
      function handleClick(event) {
        const button = event.target.closest('button'); if (!button || button.disabled) return;
        if (button.dataset.obCategory !== undefined) { category = category === button.dataset.obCategory ? '' : button.dataset.obCategory; draw(); }
        else if (button.dataset.obAction === 'clear') { category = ''; draw(); }
        else if (button.dataset.obAction === 'retry') load();
        else if (button.dataset.obAction === 'config') ctx.navigate('config');
        else if (button.dataset.obLead) Promise.resolve().then(() => ctx.openLead(button.dataset.obLead)).catch(error => { if (!disposed) ctx.toast(error.message, 'erro'); });
      }
      root.addEventListener('change', handleChange); root.addEventListener('click', handleClick);
      await load();
      return () => { disposed = true; revision++; root.removeEventListener('change', handleChange); root.removeEventListener('click', handleClick); };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
