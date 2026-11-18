(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  const app = SDR.qs('#app');
  if (!app) return;
  SDR.views = SDR.views || {};
  const routes = [
    ['visao', 'Visão geral', 'M3 13h7V3H3zM14 21h7V11h-7zM3 21h7v-4H3zM14 7h7V3h-7z'],
    ['agenda', 'Agenda', 'M8 2v4m8-4v4M3 10h18M5 4h14a2 2 0 0 1 2 2v14H3V6a2 2 0 0 1 2-2Z'],
    ['disponibilidade', 'Disponibilidade', 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z'],
    ['conversas', 'Conversas', 'M21 11a8 8 0 0 1-8 8H7l-4 3V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2ZM7 8h10M7 12h7'],
    ['objecoes', 'Objeções', 'M12 8v5m0 3h.01M12 3 2 21h20Z'],
    ['leads', 'Leads', 'M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0ZM4 21v-2a8 8 0 0 1 16 0v2'],
    ['whatsapp', 'WhatsApp', 'M5 4h4l2 5-2.5 1.5a11 11 0 0 0 5 5L15 13l5 2v4a2 2 0 0 1-2 2A16 16 0 0 1 3 6a2 2 0 0 1 2-2Z'],
    ['config', 'Configuração', 'M4 6h16M4 12h16M4 18h16M8 3v6m8 0v6m-6 0v6']
  ];
  const icon = path => SDR.html`<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${path}"/></svg>`;
  const logo = SDR.html`<svg width="34" height="34" viewBox="0 0 36 36" fill="none" aria-hidden="true"><rect x="1" y="1" width="34" height="34" rx="10" stroke="currentColor"/><path d="M10 11h16L10 25h16M14 18h8" stroke="currentColor" stroke-width="2" stroke-linejoin="round"/></svg>`;
  const brand = () => SDR.html`<div class="pn-brand">${logo}<div><strong>ZX Control</strong><small>Agente SDR · Agendamento</small></div></div>`;
  let cleanup = null, routeRevision = 0, authRevision = 0, currentEmail = '', shell = null, unsubscribe = null, booting = false;
  const modals = [];
  function message(error) { return error && error.message || 'Não foi possível concluir. Tente de novo.'; }
  function loading(root) { SDR.mount(root, SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`); }
  function clearView() {
    routeRevision++;
    if (cleanup) { try { cleanup(); } catch (_) { /* Uma view não impede a troca de rota. */ } }
    cleanup = null;
    modals.slice().reverse().forEach(item => item.close());
  }
  SDR.toast = function (text, kind) {
    const region = SDR.qs('#pn-toasts');
    if (!region) return;
    const item = SDR.el('div', {class: 'banner pn-toast ' + (kind === 'erro' ? 'banner-error' : kind === 'ok' ? 'banner-success' : '')}, SDR.el('span', {}, text));
    const dismiss = SDR.el('button', {type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': 'Fechar aviso', onclick: () => item.remove()}, '×');
    item.appendChild(dismiss); region.appendChild(item);
    while (region.children.length > 4) region.firstElementChild.remove();
    setTimeout(() => item.remove(), 8000);
  };
  function modal(options) {
    const previous = global.document.activeElement;
    const id = 'pn-dialog-' + SDR.uuid();
    const backdrop = SDR.el('div', {class: 'pn-modal-backdrop'});
    const dialog = SDR.el('section', {class: 'pn-modal stack' + (options.wide ? ' pn-modal-wide' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-labelledby': id, tabindex: '-1'});
    const heading = SDR.el('div', {class: 'pn-modal-header row'}, SDR.el('h2', {id}, options.title || 'Detalhes'));
    const body = SDR.el('div', {class: 'pn-modal-body'}), footer = SDR.el('div', {class: 'pn-modal-actions row'});
    let closed = false, busy = false;
    const handle = {el: dialog, close};
    function close() {
      if (closed) return;
      closed = true;
      const index = modals.indexOf(handle); if (index >= 0) modals.splice(index, 1);
      backdrop.remove(); global.document.removeEventListener('keydown', keydown);
      if (modals.length) modals[modals.length - 1].el.removeAttribute('inert');
      else { app.removeAttribute('inert'); global.document.body.classList.remove('pn-modal-open'); }
      try { if (options.onClose) options.onClose(); } finally {
        if (previous && previous.isConnected) previous.focus();
      }
    }
    const closeButton = SDR.el('button', {type: 'button', class: 'btn btn-ghost btn-sm', 'aria-label': 'Fechar janela', onclick: () => { if (!busy) close(); }}, '×');
    heading.appendChild(closeButton);
    if (options.body && options.body.nodeType) body.appendChild(options.body); else SDR.mount(body, options.body || '');
    (options.actions || []).forEach(action => {
      const kind = ['primary', 'ghost', 'danger'].includes(action.kind) ? action.kind : 'ghost';
      const button = SDR.el('button', {type: 'button', class: 'btn btn-' + kind, onclick: async () => {
        if (busy || closed) return;
        busy = true; dialog.setAttribute('aria-busy', 'true');
        const buttons = SDR.qsa('.pn-modal-actions button', dialog); buttons.forEach(b => { b.disabled = true; }); closeButton.disabled = true;
        try { if (action.onClick) await action.onClick(close); else close(); }
        catch (error) {
          let alert = SDR.qs('.pn-action-error', body);
          if (!alert) { alert = SDR.el('div', {class: 'banner banner-error pn-action-error', role: 'alert'}); body.appendChild(alert); }
          alert.textContent = message(error);
          SDR.toast(message(error), 'erro');
          button.textContent = 'Tentar de novo';
        } finally {
          busy = false; dialog.removeAttribute('aria-busy'); buttons.forEach(b => { b.disabled = false; }); closeButton.disabled = false;
        }
      }}, action.label);
      footer.appendChild(button);
    });
    function keydown(event) {
      if (modals[modals.length - 1] !== handle) return;
      if (event.key === 'Escape') { event.preventDefault(); if (!busy) close(); }
      if (event.key !== 'Tab') return;
      const focusable = SDR.qsa('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]', dialog).filter(el => !el.hidden && !el.closest('[hidden]'));
      if (!focusable.length) { event.preventDefault(); dialog.focus(); return; }
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && (global.document.activeElement === first || global.document.activeElement === dialog)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (global.document.activeElement === last || !dialog.contains(global.document.activeElement))) { event.preventDefault(); first.focus(); }
    }
    dialog.append(heading, body, footer); backdrop.appendChild(dialog);
    if (modals.length) modals[modals.length - 1].el.setAttribute('inert', '');
    modals.push(handle); app.setAttribute('inert', ''); global.document.body.classList.add('pn-modal-open');
    SDR.qs('#pn-modals').appendChild(backdrop); global.document.addEventListener('keydown', keydown);
    backdrop.addEventListener('click', event => { if (event.target === backdrop && !busy) close(); });
    dialog.focus();
    return handle;
  }
  function confirm(options) {
    return new Promise(resolve => {
      let accepted = false;
      modal({title: options.title, body: SDR.html`<p>${options.message}</p>`, actions: [
        {label: 'Voltar', kind: 'ghost', onClick: close => close()},
        {label: options.confirmLabel || 'Confirmar', kind: options.danger ? 'danger' : 'primary', onClick: close => { accepted = true; close(); }}
      ], onClose: () => resolve(accepted)});
    });
  }
  const ctx = {
    api: SDR.api, state: {}, params: new URLSearchParams(), toast: SDR.toast, modal, confirm,
    async refreshState() {
      const [settings, agent, closers] = await Promise.all([ctx.api.getScheduleSettings(), ctx.api.getAgentConfig(), ctx.api.listClosers()]);
      if (!settings || !agent || !Array.isArray(closers)) throw new Error('Seu e-mail ainda não está autorizado neste painel. Peça para adicionar em panel_users.');
      const tz = settings.timezone || 'America/Sao_Paulo';
      try { new Intl.DateTimeFormat('pt-BR', {timeZone: tz}); } catch (_) { throw new Error('O fuso horário da agenda é inválido. Confira a configuração.'); }
      ctx.state = {tz, settings, agent, closers}; SDR.tz = tz;
      const zone = SDR.qs('#pn-zone'); if (zone) zone.textContent = tz;
    },
    navigate(route, params) {
      const name = String(route || 'visao').replace(/^#?\//, '').split('?')[0];
      const safeRoute = routes.some(r => r[0] === name) ? name : 'visao';
      const query = new URLSearchParams();
      // URLs do painel aceitam apenas identificadores opacos de contato.
      if (params && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(params.lead || ''))) query.set('lead', params.lead);
      const hash = '#/' + safeRoute + (query.toString() ? '?' + query.toString() : '');
      if (global.location.hash === hash) routeView(); else global.location.hash = hash;
    },
    async openLead(id) {
      if (typeof SDR.openLeadModal === 'function') {
        try { await SDR.openLeadModal(ctx, id); } catch (error) { SDR.toast(message(error), 'erro'); }
      } else SDR.toast('Tela indisponível. A ficha de leads ainda não foi carregada.', 'info');
    }
  };
  async function routeView() {
    if (!shell || !shell.isConnected) return;
    clearView(); const revision = routeRevision;
    const raw = global.location.hash.replace(/^#\/?/, ''), [requested, query] = raw.split('?');
    const name = requested || 'visao', route = routes.find(r => r[0] === name);
    const params = new URLSearchParams(query || '');
    ctx.params = new URLSearchParams();
    if (/^[0-9a-f-]{36}$/i.test(params.get('lead') || '')) ctx.params.set('lead', params.get('lead'));
    SDR.qsa('[data-pn-route]', shell).forEach(link => {
      if (link.dataset.pnRoute === name) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
    });
    const title = SDR.qs('#pn-title', shell), host = SDR.qs('#pn-view', shell);
    const view = route && SDR.views[name];
    title.textContent = view && view.title || route && route[1] || 'Tela indisponível';
    global.document.title = title.textContent + ' · ZX Control · Agente SDR';
    const section = SDR.el('section', {class: 'pn-view', 'aria-label': title.textContent});
    host.replaceChildren(section); loading(section); section.setAttribute('aria-busy', 'true');
    if (!view || typeof view.render !== 'function') {
      SDR.mount(section, SDR.html`<div class="empty card"><h2>Tela indisponível</h2><p>Os arquivos desta tela ainda não foram carregados.</p><button class="btn btn-ghost" type="button" data-retry>Tentar de novo</button></div>`);
      SDR.qs('[data-retry]', section).addEventListener('click', () => global.location.reload()); section.removeAttribute('aria-busy'); return;
    }
    try {
      // O contrato entrega uma section vazia; cada view controla seu loading.
      section.replaceChildren(); section.removeAttribute('aria-busy');
      const dispose = await view.render(section, ctx);
      if (revision !== routeRevision) { if (typeof dispose === 'function') dispose(); return; }
      cleanup = typeof dispose === 'function' ? dispose : null;
    } catch (error) {
      if (revision !== routeRevision) return;
      section.removeAttribute('aria-busy');
      SDR.mount(section, SDR.html`<div class="banner banner-error" role="alert"><p>${message(error)}</p><button type="button" class="btn btn-ghost" data-retry>Tentar de novo</button></div>`);
      SDR.qs('[data-retry]', section).addEventListener('click', routeView);
    }
  }
  async function signOut() {
    try { await ctx.api.signOut(); currentEmail = ''; showLogin(); }
    catch (error) { SDR.toast(message(error), 'erro'); }
  }
  function showShell(session) {
    clearView();
    SDR.mount(app, SDR.html`<div class="pn-shell"><aside class="pn-sidebar">${brand()}<span class="pn-nav-label">PAINEL DE OPERAÇÃO</span><nav class="pn-nav" aria-label="Navegação principal">${routes.map(([name,label,path]) => SDR.html`<a href="#/${name}" data-pn-route="${name}">${icon(path)}<span>${label}</span></a>`)}</nav><div class="pn-sidebar-bottom"><span class="muted">Desenvolvido por ZX LAB</span><a href="https://suporte.zxlab.com.br/hub" target="_blank" rel="noopener noreferrer">Suporte ZX LAB ↗</a></div></aside><div class="pn-workspace">${SDR.config.demo === true ? SDR.html`<div class="pn-demo banner"><strong>Modo demonstração</strong><span>— dados fictícios, nada é salvo</span></div>` : SDR.raw('')}<header class="pn-header"><div><span class="pn-eyebrow">SEU CENTRO DE OPERAÇÃO</span><h1 id="pn-title">Visão geral</h1></div><div class="pn-account"><span class="pn-session muted">${SDR.config.demo === true ? 'Ambiente de demonstração' : session.email}</span>${SDR.config.demo === true ? SDR.raw('') : SDR.html`<button type="button" id="pn-signout" class="btn btn-ghost btn-sm">Sair</button>`}</div></header><main id="conteudo" class="pn-content" tabindex="-1"><div class="pn-context row"><span class="pn-dot" aria-hidden="true"></span><span>Horários da agenda em</span><span class="mono" id="pn-zone">${ctx.state.tz}</span></div><div id="pn-view"></div></main></div></div>`);
    shell = SDR.qs('.pn-shell', app);
    const exit = SDR.qs('#pn-signout'); if (exit) exit.addEventListener('click', signOut);
    routeView();
  }
  function showLogin() {
    authRevision++; clearView(); shell = null; currentEmail = '';
    global.document.title = 'ZX Control · Agente SDR';
    SDR.mount(app, SDR.html`<main class="pn-login" id="conteudo" tabindex="-1"><div class="pn-login-card card stack">${brand()}<div><span class="pn-eyebrow">PAINEL DE OPERAÇÃO</span><h1>Bem-vindo ao seu painel.</h1><p class="muted">Acesso restrito ao dono do painel.</p></div><form id="pn-login" class="stack"><div class="field"><label class="label" for="pn-email">E-mail</label><input class="input" id="pn-email" type="email" autocomplete="username" required maxlength="254"></div><div class="field"><label class="label" for="pn-password">Senha</label><input class="input" id="pn-password" type="password" autocomplete="current-password" required></div><div id="pn-login-error" role="alert" hidden class="banner banner-error"></div><button class="btn btn-primary" type="submit">Entrar no painel</button></form><a class="pn-support" href="https://suporte.zxlab.com.br/hub" target="_blank" rel="noopener noreferrer">Suporte ZX LAB ↗</a></div></main>`);
    const form = SDR.qs('#pn-login');
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (form.dataset.busy) return;
      if (!form.reportValidity()) return;
      form.dataset.busy = 'true'; const button = SDR.qs('button', form), alert = SDR.qs('#pn-login-error');
      button.disabled = true; button.textContent = 'Entrando...'; alert.hidden = true;
      try {
        const session = await ctx.api.signIn(SDR.qs('#pn-email').value.trim(), SDR.qs('#pn-password').value);
        SDR.qs('#pn-password', form).value = '';
        if (!session) throw new Error('Não foi possível iniciar a sessão. Tente de novo.');
        await enter(session);
      } catch (error) { if (form.isConnected) { alert.textContent = message(error); alert.hidden = false; } }
      finally { delete form.dataset.busy; button.disabled = false; button.textContent = alert.hidden ? 'Entrar no painel' : 'Tentar de novo'; }
    });
  }
  function showBootError(error, denied) {
    clearView(); shell = null;
    SDR.mount(app, SDR.html`<main class="pn-start" id="conteudo" tabindex="-1"><div class="card stack">${brand()}<div class="banner banner-error" role="alert">${denied ? 'Seu e-mail ainda não está autorizado neste painel. Peça para adicionar em panel_users.' : message(error)}</div><div class="row"><button type="button" class="btn btn-primary" id="pn-retry">Tentar de novo</button>${currentEmail ? SDR.html`<button type="button" class="btn btn-ghost" id="pn-exit">Sair</button>` : SDR.raw('')}</div></div></main>`);
    SDR.qs('#pn-retry').addEventListener('click', boot);
    const exit = SDR.qs('#pn-exit'); if (exit) exit.addEventListener('click', signOut);
  }
  async function enter(session) {
    const revision = ++authRevision; currentEmail = session.email || '';
    clearView(); shell = null; loading(app);
    try {
      // O adapter retorna defaults em leitura vazia. A função usada pela RLS
      // distingue uma base nova de uma sessão sem acesso, inclusive com e-mail
      // cadastrado com capitalização diferente (conforme a migration existente).
      // Falha fechado: no modo live a checagem precisa rodar e devolver true; sem a função, nega.
      if (SDR.api.mode === 'live') {
        if (typeof SDR.api.isPanelUser !== 'function') throw new Error('Acesso não autorizado.');
        const allowed = await SDR.api.isPanelUser();
        if (allowed !== true) throw new Error('Acesso não autorizado.');
      }
      await ctx.refreshState();
      if (revision !== authRevision) return;
      showShell(session);
    } catch (error) {
      if (revision !== authRevision) return;
      showBootError(error, /autorizad|permissão|401|403/.test(message(error)));
    }
  }
  async function boot() {
    if (booting) return;
    booting = true;
    try {
      if (!SDR.requireConfigOrExplain(app)) return;
      loading(app);
      // initConnection oferece uma nova tentativa após falha do CDN/conexão.
      await SDR.initConnection(); ctx.api = SDR.api;
      const session = SDR.config.demo === true ? {email: 'demo@exemplo.com'} : await ctx.api.getSession();
      if (!unsubscribe && SDR.config.demo !== true) unsubscribe = ctx.api.onAuthChange(next => {
        // Não executar consultas dentro do callback síncrono do Supabase Auth.
        setTimeout(() => {
          if (!next) showLogin();
          else if (next.email !== currentEmail) enter(next);
        }, 0);
      });
      if (session) await enter(session); else showLogin();
    } catch (error) { showBootError(error, false); }
    finally { booting = false; }
  }
  global.addEventListener('hashchange', routeView);
  const skip = SDR.qs('a.skip');
  if (skip) skip.addEventListener('click', event => {
    event.preventDefault(); const content = SDR.qs('#conteudo'); if (content) content.focus();
  });
  global.addEventListener('pagehide', () => {
    authRevision++; clearView();
    if (unsubscribe) { unsubscribe(); unsubscribe = null; }
  });
  global.addEventListener('pageshow', event => { if (event.persisted) boot(); });
  SDR.beacon(); boot();
})(typeof window !== 'undefined' ? window : globalThis);
