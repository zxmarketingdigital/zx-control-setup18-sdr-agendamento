(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  SDR.views = SDR.views || {};
  const opaqueId = value => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(value || ''));
  const activeTakeover = lead => Date.parse(lead.takeover_until) > Date.now();
  const activity = lead => Math.max(Date.parse(lead.last_inbound_at) || 0, Date.parse(lead.last_outbound_at) || 0);
  const origins = new Map([['whatsapp', 'WhatsApp'], ['pagina', 'Página pública'], ['manual', 'Manual'], ['agente', 'Agente']]);
  const origin = value => origins.get(value) || value || 'Não informada';
  const loading = () => SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`;
  function closerBadge(closer) {
    if (!closer) return SDR.html`<span class="muted">Sem responsável</span>`;
    // Somente tokens da paleta podem chegar ao CSS; nunca interpolar uma cor do banco.
    const color = String(closer.color || '').toLowerCase();
    const tone = color === '#818cf8' ? 'indigo' : color === '#9ca3af' ? 'muted' : 'amber';
    return SDR.html`<span class="ld-closer"><span class="ld-dot ld-dot-${tone}" aria-hidden="true"></span>${closer.name || 'Responsável'}</span>`;
  }
  function valueText(value) {
    if (value == null || value === '') return 'Não informado';
    if (value === true) return 'Sim';
    if (value === false) return 'Não';
    if (Array.isArray(value)) return value.length ? value.map(valueText).join('; ') : 'Não informado';
    if (typeof value === 'object') return Object.entries(value).map(([key, entry]) => key + ': ' + valueText(entry)).join('; ') || 'Não informado';
    return String(value);
  }
  function qualification(lead, ctx) {
    const questions = new Map((Array.isArray(ctx.state.agent && ctx.state.agent.qualification_questions) ? ctx.state.agent.qualification_questions : []).map(q => [String(q.key), q.pergunta]));
    const entries = lead.qualification && typeof lead.qualification === 'object' && !Array.isArray(lead.qualification) ? Object.entries(lead.qualification) : [];
    return entries.length ? SDR.html`<dl class="ld-details">${entries.map(([key, value]) => SDR.html`<div><dt>${questions.get(key) || key}</dt><dd>${valueText(value)}</dd></div>`)}</dl>` : SDR.html`<div class="empty">O agente ainda não registrou respostas de qualificação para este contato.</div>`;
  }

  SDR.openLeadModal = async function (ctx, leadId) {
    const body = SDR.el('div', {class: 'ld-sheet stack'});
    let open = true, lead = null, busy = false, revision = 0, lastAction = null, release;
    const closed = new Promise(resolve => { release = resolve; });
    const dialog = ctx.modal({title: 'Ficha do lead', wide: true, body, actions: [{label: 'Fechar', kind: 'ghost', onClick: close => close()}], onClose() { open = false; revision++; release(); }});
    function notice(message, action) {
      const box = SDR.qs('.ld-action-error', body);
      if (!box) return;
      lastAction = action;
      SDR.mount(box, SDR.html`<div class="banner banner-error" role="alert"><p>${message}</p><button class="btn btn-ghost btn-sm" type="button" data-action="retry-action">Tentar de novo</button></div>`);
    }
    function draw() {
      const appointments = (Array.isArray(lead.appointments) ? lead.appointments : []).slice().sort((a, b) => (Date.parse(b.starts_at) || 0) - (Date.parse(a.starts_at) || 0));
      const objections = Array.isArray(lead.objections) ? lead.objections : [];
      SDR.mount(body, SDR.html`<div class="ld-action-error" aria-live="polite"></div>
        <section class="card stack" aria-label="Dados do contato"><div class="row ld-heading"><div><h3>${lead.name || 'Contato sem nome'}</h3><p class="mono muted">${SDR.fmt.phone(lead.phone)}</p></div>${SDR.stageBadge(lead.stage)}</div>
          <dl class="ld-details"><div><dt>E-mail</dt><dd>${lead.email || 'Não informado'}</dd></div><div><dt>Origem</dt><dd>${origin(lead.source)}</dd></div><div><dt>Score</dt><dd class="mono">${lead.score == null ? 'Não informado' : SDR.fmt.int(lead.score)}</dd></div><div><dt>Responsável</dt><dd>${closerBadge(lead.closer)}</dd></div><div><dt>Perfil</dt><dd>${lead.profile || 'Ainda não identificado'}</dd></div><div><dt>Motivo de desqualificação</dt><dd>${lead.disqualify_reason || 'Nenhum motivo registrado'}</dd></div></dl>
          <div class="field"><label class="label" for="ld-lead-stage">Corrigir estágio manualmente</label><select class="select" id="ld-lead-stage" data-action="stage">${SDR.STAGES.map(stage => SDR.html`<option value="${stage.key}" ${stage.key === lead.stage ? SDR.raw('selected') : SDR.raw('')}>${stage.label}</option>`)}</select></div>
          <label class="ld-switch"><input type="checkbox" role="switch" data-action="opt-out" ${lead.opt_out ? SDR.raw('checked') : SDR.raw('')}><span>Não enviar mensagens a este contato (opt-out)</span></label>
          ${lead.opt_out ? SDR.html`<div class="banner">Este contato pediu para não receber mensagens.</div>` : SDR.raw('')}
          <div class="row"><button class="btn btn-primary" type="button" data-action="conversation">Abrir conversa</button><button class="btn btn-ghost" type="button" data-action="takeover">${activeTakeover(lead) ? 'Devolver ao agente' : 'Assumir conversa'}</button></div>
          <p class="muted ld-control-note">${activeTakeover(lead) ? 'Dono no controle até ' + SDR.fmt.dateTime(lead.takeover_until, ctx.state.tz) : 'O agente está no controle da conversa.'}</p>
        </section>
        <section class="card"><h3>Qualificação</h3>${qualification(lead, ctx)}</section>
        <section class="card"><h3>Agendamentos <span class="mono muted">${appointments.length}</span></h3>${appointments.length ? SDR.html`<div class="ld-records">${appointments.map(appointment => SDR.html`<article class="ld-record"><div class="row">${SDR.statusBadge(appointment.status)}<time class="mono" datetime="${appointment.starts_at}">${SDR.fmt.dateTime(appointment.starts_at, ctx.state.tz)}</time></div><p>${closerBadge(appointment.closer || (ctx.state.closers || []).find(closer => closer.id === appointment.closer_id))}</p>${appointment.notes ? SDR.html`<p class="muted ld-prewrap">${appointment.notes}</p>` : SDR.raw('')}</article>`)}</div>` : SDR.html`<div class="empty">Este contato ainda não tem agendamentos registrados.</div>`}</section>
        <section class="card"><h3>Objeções <span class="mono muted">${objections.length}</span></h3>${objections.length ? SDR.html`<div class="ld-records">${objections.map(objection => SDR.html`<article class="ld-record"><div class="row"><span class="chip">${objection.category || 'Sem categoria'}</span><span class="badge ${objection.handled ? 'badge-green' : 'badge-muted'}">${objection.handled ? 'Tratada' : 'Pendente'}</span><time class="mono muted" datetime="${objection.created_at}">${SDR.fmt.dateTime(objection.created_at, ctx.state.tz)}</time></div><blockquote class="ld-quote">${objection.excerpt || 'Sem trecho registrado'}</blockquote></article>`)}</div>` : SDR.html`<div class="empty">Nenhuma objeção registrada para este contato.</div>`}</section>`);
    }
    async function load() {
      const current = ++revision;
      SDR.mount(body, loading());
      try {
        const result = await ctx.api.getLead(leadId);
        if (!open || current !== revision) return;
        if (!result) throw new Error('Este contato não foi encontrado.');
        lead = result; draw();
      } catch (error) {
        if (!open || current !== revision) return;
        SDR.mount(body, SDR.html`<div class="banner banner-error" role="alert"><p>${error.message || 'Não foi possível carregar a ficha.'}</p><button type="button" class="btn btn-ghost" data-action="retry">Tentar de novo</button></div>`);
      }
    }
    async function mutate(action) {
      if (busy || !open) return;
      busy = true;
      body.setAttribute('aria-busy', 'true');
      const controls = SDR.qsa('button, select, input', body);
      controls.forEach(control => { control.disabled = true; });
      try {
        const updated = await action();
        if (!open) return;
        lead = Object.assign({}, lead, updated || {}); draw(); ctx.toast('Contato atualizado.', 'ok');
      } catch (error) {
        if (!open) return;
        draw(); notice(error.message || 'Não foi possível atualizar o contato.', () => mutate(action));
        ctx.toast(error.message || 'Não foi possível atualizar o contato.', 'erro');
      } finally {
        busy = false; body.setAttribute('aria-busy', 'false');
        controls.forEach(control => { control.disabled = false; });
      }
    }
    body.addEventListener('click', event => {
      const button = event.target.closest('button[data-action]');
      if (!button || button.disabled) return;
      if (button.dataset.action === 'retry') { load(); return; }
      if (button.dataset.action === 'retry-action') { if (lastAction) lastAction(); return; }
      if (!lead || busy) return;
      if (button.dataset.action === 'takeover') { const next = !activeTakeover(lead); mutate(() => ctx.api.setTakeover(lead.id, next)); }
      if (button.dataset.action === 'conversation') {
        if (!opaqueId(lead.id)) { ctx.toast('O identificador deste contato é inválido.', 'erro'); return; }
        dialog.close(); ctx.navigate('conversas', {lead: lead.id});
      }
    });
    body.addEventListener('change', async event => {
      const control = event.target;
      if (!lead || busy) return;
      if (control.dataset.action === 'stage') {
        const stage = control.value;
        if (SDR.STAGES.some(item => item.key === stage)) await mutate(() => ctx.api.updateLead(lead.id, {stage}));
      } else if (control.dataset.action === 'opt-out') {
        const value = control.checked;
        if (!value) {
          try {
            if (!await ctx.confirm({title: 'Permitir mensagens novamente?', message: 'Confirme que este contato autorizou receber mensagens novamente.', confirmLabel: 'Permitir mensagens'})) { control.checked = true; return; }
          } catch (error) { control.checked = true; ctx.toast(error.message || 'Não foi possível confirmar a autorização.', 'erro'); return; }
          await mutate(() => ctx.api.reativarLead(lead.id));
          return;
        }
        await mutate(() => ctx.api.updateLead(lead.id, {opt_out: true}));
      }
    });
    await load();
    return Object.assign({}, dialog, {closed});
  };

  SDR.views.leads = {
    title: 'Leads',
    async render(root, ctx) {
      let disposed = false, rows = [], request = 0, sortKey = 'activity', descending = true, debounceTimer;
      SDR.mount(root, SDR.html`<div class="ld-view stack"><div class="card ld-filters"><div class="field"><label class="label" for="ld-search">Buscar contatos</label><input class="input" type="search" id="ld-search" placeholder="Nome, telefone ou e-mail" autocomplete="off"></div><div class="field"><label class="label" for="ld-stage-filter">Estágio</label><select id="ld-stage-filter" class="select"><option value="">Todos os estágios</option>${SDR.STAGES.map(stage => SDR.html`<option value="${stage.key}">${stage.label}</option>`)}</select></div></div><div class="row ld-toolbar"><p class="muted" id="ld-count" role="status" aria-live="polite"></p><div class="row" aria-label="Ordenar contatos"><button class="btn btn-ghost btn-sm" type="button" data-sort="activity">Última atividade ↓</button><button class="btn btn-ghost btn-sm" type="button" data-sort="score">Score</button></div></div><div class="ld-results"></div></div>`);
      const search = SDR.qs('#ld-search', root), stage = SDR.qs('#ld-stage-filter', root), results = SDR.qs('.ld-results', root), count = SDR.qs('#ld-count', root);
      function draw() {
        const sorted = rows.slice().sort((a, b) => {
          const value = item => sortKey === 'score' ? (item.score == null ? -1 : Number(item.score) || 0) : activity(item) || Date.parse(item.created_at) || 0;
          return (value(a) - value(b)) * (descending ? -1 : 1) || String(a.name || '').localeCompare(String(b.name || ''), 'pt-BR');
        });
        count.textContent = sorted.length + (sorted.length === 1 ? ' resultado' : ' resultados') + (rows.length >= 200 ? ' · Limite de 200; refine a busca.' : '');
        SDR.qsa('[data-sort]', root).forEach(button => { const selected = button.dataset.sort === sortKey; button.setAttribute('aria-pressed', String(selected)); button.textContent = (button.dataset.sort === 'score' ? 'Score' : 'Última atividade') + (selected ? descending ? ' ↓' : ' ↑' : ''); });
        if (!sorted.length) { SDR.mount(results, SDR.html`<div class="card empty"><h3>Nenhum contato encontrado</h3><p>${search.value.trim() || stage.value ? 'Tente outra busca ou escolha outro estágio.' : 'Os contatos aparecem aqui quando começam uma conversa ou fazem um agendamento.'}</p></div>`); return; }
        SDR.mount(results, SDR.html`<div class="table-wrap ld-table-wrap"><table class="table ld-table"><caption class="sr-only">Contatos encontrados. Use o nome para abrir a ficha.</caption><thead><tr><th scope="col">Contato</th><th scope="col">Origem</th><th scope="col">Estágio</th><th scope="col">Score</th><th scope="col">Perfil</th><th scope="col">Responsável</th><th scope="col">Última atividade</th></tr></thead><tbody>${sorted.map(lead => SDR.html`<tr data-lead-id="${lead.id}"><td data-label="Contato"><button class="ld-name" type="button" data-open="${lead.id}">${lead.name || 'Contato sem nome'}</button><span class="ld-phone mono muted">${SDR.fmt.phone(lead.phone)}</span></td><td data-label="Origem">${origin(lead.source)}</td><td data-label="Estágio">${SDR.stageBadge(lead.stage)}</td><td data-label="Score" class="mono">${lead.score == null ? '—' : SDR.fmt.int(lead.score)}</td><td data-label="Perfil" class="ld-profile">${lead.profile || 'Ainda não identificado'}</td><td data-label="Responsável">${closerBadge(lead.closer)}</td><td data-label="Última atividade"><span class="mono" title="${activity(lead) ? SDR.fmt.dateTime(activity(lead), ctx.state.tz) : 'Nenhuma mensagem registrada'}">${activity(lead) ? SDR.fmt.rel(activity(lead)) : 'Sem mensagens'}</span></td></tr>`)}</tbody></table></div>`);
      }
      async function load() {
        const current = ++request;
        results.setAttribute('aria-busy', 'true'); count.textContent = '';
        SDR.mount(results, loading());
        try {
          const data = await ctx.api.listLeads({search: search.value.trim(), stage: stage.value, limit: 200});
          if (disposed || current !== request) return;
          rows = Array.isArray(data) ? data : []; draw();
        } catch (error) {
          if (disposed || current !== request) return;
          count.textContent = 'Não foi possível carregar os contatos.';
          SDR.mount(results, SDR.html`<div class="banner banner-error" role="alert"><p>${error.message || 'Não foi possível carregar os contatos.'}</p><button type="button" class="btn btn-ghost" data-retry>Tentar de novo</button></div>`);
        } finally { if (!disposed && current === request) results.setAttribute('aria-busy', 'false'); }
      }
      const onSearch = () => { clearTimeout(debounceTimer); request++; debounceTimer = setTimeout(() => { if (!disposed) load(); }, 300); };
      const onStage = () => { clearTimeout(debounceTimer); load(); };
      const onClick = async event => {
        const button = event.target.closest('button');
        if (button && button.hasAttribute('data-retry')) { load(); return; }
        if (button && button.dataset.sort) { if (sortKey === button.dataset.sort) descending = !descending; else { sortKey = button.dataset.sort; descending = true; } draw(); return; }
        const row = event.target.closest('[data-lead-id]');
        if (!row || !root.contains(row)) return;
        const id = row.dataset.leadId;
        try { const modal = await SDR.openLeadModal(ctx, id); await modal.closed; if (!disposed) await load(); }
        catch (error) { if (!disposed) ctx.toast(error.message || 'Não foi possível abrir a ficha.', 'erro'); }
      };
      search.addEventListener('input', onSearch); stage.addEventListener('change', onStage); root.addEventListener('click', onClick);
      await load();
      return () => { disposed = true; request++; clearTimeout(debounceTimer); search.removeEventListener('input', onSearch); stage.removeEventListener('change', onStage); root.removeEventListener('click', onClick); };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
