(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  SDR.views = SDR.views || {};
  const opaqueId = value => /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(String(value || ''));
  const controlled = lead => !!lead && Date.parse(lead.takeover_until) > Date.now();
  const displayName = lead => lead.name || SDR.fmt.phone(lead.phone) || 'Contato sem nome';
  const activity = lead => Math.max(Date.parse(lead.last_inbound_at) || 0, Date.parse(lead.last_outbound_at) || 0, Date.parse(lead.lastMessage && lead.lastMessage.created_at) || 0);
  const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
  // Esperas (ms) entre as consultas que confirmam um envio de resultado incerto; configurável em SDR_CONFIG.sendConfirmDelaysMs.
  const confirmDelays = () => { const custom = SDR.config && SDR.config.sendConfirmDelaysMs; return Array.isArray(custom) && custom.length && custom.every(n => Number.isFinite(n) && n >= 0) ? custom : [600, 1500, 3000]; };
  const PREVIEW_MAX = 80;
  function previewText(item) {
    const msg = item.lastMessage;
    if (!msg) return activity(item) ? 'Abra para ver as mensagens' : 'Ainda sem mensagens';
    const flat = String(msg.body || 'Mensagem de mídia').replace(/\s+/g, ' ').trim() || 'Mensagem de mídia';
    const chars = Array.from(flat), text = chars.length > PREVIEW_MAX ? chars.slice(0, PREVIEW_MAX - 1).join('') + '…' : flat;
    const who = msg.author === 'agente' ? 'Agente: ' : ['dono', 'painel'].includes(msg.author) ? 'Você: ' : '';
    return who + text;
  }
  const loading = () => SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`;
  const errorView = (message, retry, extra) => SDR.html`<div class="banner banner-error" role="alert"><p>${message || 'Não foi possível atualizar. Tente de novo.'}</p><button class="btn btn-ghost btn-sm" type="button" data-cv-action="${retry}">Tentar de novo</button>${extra ? SDR.html` <button class="btn btn-ghost btn-sm" type="button" data-cv-action="${extra.action}">${extra.label}</button>` : SDR.raw('')}</div>`;
  const backIcon = SDR.raw('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m12 5-7 7 7 7M5 12h14"/></svg>');

  SDR.views.conversas = {
    title: 'Conversas',
    async render(root, ctx) {
      let disposed = false, selected = '', lead = null, onlyTakeover = false;
      let listVersion = 0, threadVersion = 0, polling = false, sending = false, changingTakeover = false;
      let conversations = [], messageSignature = '', outboxSignature = '';
      const drafts = new Map(), scrolls = new Map(), sendKeys = new Map();
      SDR.mount(root, SDR.html`<div class="cv-layout"><aside class="card cv-sidebar" aria-label="Lista de conversas"><div class="cv-list-header"><h2>Conversas</h2><span class="muted cv-poll-note">Atualização a cada 8 segundos</span><label class="cv-filter"><input type="checkbox" data-cv-filter> Só as que assumi</label></div><div class="cv-list-status" aria-live="polite"></div><div class="cv-list"></div></aside><section class="card cv-thread" aria-label="Conversa selecionada"><div class="cv-thread-status" aria-live="polite"></div><header class="cv-thread-header"></header><div class="cv-messages" role="log" aria-label="Histórico de mensagens" aria-live="polite" aria-relevant="additions text" tabindex="0"></div><form class="cv-composer" hidden><div class="cv-contact-warning"></div><label class="label" for="cv-message">Mensagem</label><textarea id="cv-message" class="textarea cv-message-input" rows="3" maxlength="10000" placeholder="Escreva sua mensagem..."></textarea><div class="cv-send-row"><small class="muted">Ctrl ou Cmd + Enter para enviar</small><button type="submit" class="btn btn-primary cv-send">Enviar</button></div><p class="muted cv-queue-help">A mensagem entra na fila; o agente envia em até alguns segundos. O envio depende de o motor estar em execução.</p><div class="cv-send-status" aria-live="polite"></div></form><section class="cv-outbox" aria-label="Fila de envio" hidden><h3>Fila de envio</h3><div class="cv-outbox-content"></div></section></section></div>`);
      const q = selector => SDR.qs(selector, root);
      const layout = q('.cv-layout'), list = q('.cv-list'), listStatus = q('.cv-list-status');
      const header = q('.cv-thread-header'), messages = q('.cv-messages'), threadStatus = q('.cv-thread-status');
      const composer = q('.cv-composer'), input = q('.cv-message-input'), sendButton = q('.cv-send');
      const outbox = q('.cv-outbox'), outboxContent = q('.cv-outbox-content'), sendStatus = q('.cv-send-status');
      const notice = (message, kind) => { if (!disposed) ctx.toast(message, kind); };
      function renderList() {
        if (disposed) return;
        const previousScroll = list.scrollTop;
        const active = global.document.activeElement;
        const focusedId = active && active.dataset ? active.dataset.cvLead : null;
        SDR.mount(list, conversations.length ? SDR.html`${conversations.map(item => SDR.html`<button class="cv-conversation ${item.id === selected ? 'is-active' : ''}" type="button" data-cv-lead="${item.id}" aria-pressed="${item.id === selected}"><span class="cv-avatar" aria-hidden="true">${Array.from(displayName(item))[0].toLocaleUpperCase('pt-BR')}</span><span class="cv-conversation-info"><span class="cv-conversation-name">${displayName(item)}</span><span class="cv-preview">${previewText(item)}</span><span class="cv-conversation-badges">${SDR.stageBadge(item.stage)}${controlled(item) ? SDR.html`<span class="badge badge-amber">Dono no controle</span>` : SDR.raw('')}</span></span><time class="cv-list-time mono" datetime="${new Date(activity(item) || Date.now()).toISOString()}">${activity(item) ? SDR.fmt.rel(activity(item)) : '—'}</time></button>`)}` : SDR.html`<div class="empty"><h3>${onlyTakeover ? 'Nenhuma conversa assumida' : 'Nenhuma conversa por aqui'}</h3><p>${onlyTakeover ? 'Assuma uma conversa para encontrá-la neste filtro.' : 'As conversas aparecem quando um contato troca mensagens com o atendimento.'}</p></div>`);
        list.scrollTop = previousScroll;
        if (focusedId) {
          const target = SDR.qsa('[data-cv-lead]', list).find(button => button.dataset.cvLead === focusedId);
          if (target) target.focus({preventScroll: true});
        }
      }
      function renderHeader() {
        if (!lead || disposed) return;
        const active = controlled(lead);
        SDR.mount(header, SDR.html`<button class="btn btn-ghost btn-sm cv-back" type="button" data-cv-action="back">${backIcon} Voltar</button><div class="cv-thread-identity"><h2 tabindex="-1" class="cv-thread-title">${displayName(lead)}</h2><span class="mono muted">${SDR.fmt.phone(lead.phone)}</span><div class="row">${SDR.stageBadge(lead.stage)}${active ? SDR.html`<span class="badge badge-amber">Dono no controle até ${SDR.fmt.time(lead.takeover_until, ctx.state.tz)}</span>` : SDR.html`<span class="badge badge-muted">Agente no controle</span>`}</div></div><div class="cv-thread-actions"><button class="btn btn-ghost btn-sm" type="button" data-cv-action="takeover" ${sending || changingTakeover ? SDR.raw('disabled') : SDR.raw('')}>${changingTakeover ? 'Atualizando...' : active ? 'Devolver ao agente' : 'Assumir'}</button><button class="btn btn-ghost btn-sm" type="button" data-cv-action="lead">Ver lead</button></div>`);
        SDR.mount(q('.cv-contact-warning'), lead.opt_out ? SDR.html`<div class="banner banner-error" role="alert">Este contato pediu para não receber mensagens.</div>` : SDR.raw(''));
        updateComposer();
      }
      function updateComposer() {
        // Envio incerto: o texto fica travado até confirmar, reenviar com a MESMA chave ou descartar de propósito.
        const held = sendKeys.get(selected);
        input.disabled = !lead || !!lead.opt_out || sending || !!(held && held.unknown);
        sendButton.disabled = !lead || !!lead.opt_out || sending || changingTakeover || !input.value.trim();
        sendButton.textContent = sending ? 'Enviando...' : 'Enviar';
      }
      function renderMessages(rows, fresh) {
        const signature = JSON.stringify(rows);
        if (!fresh && signature === messageSignature) return;
        messageSignature = signature;
        const position = messages.scrollTop;
        const atBottom = messages.scrollHeight - messages.clientHeight - position < 70;
        SDR.mount(messages, rows.length ? SDR.html`${rows.map(row => {
          const author = ['lead', 'agente', 'dono', 'sistema'].includes(row.author) ? row.author : 'sistema';
          const labels = {lead: 'Contato', agente: 'Agente', dono: 'Dono', sistema: 'Sistema'};
          return SDR.html`<article class="cv-bubble cv-bubble-${author}"><div class="cv-bubble-meta"><strong>${labels[author]}</strong><time class="mono" datetime="${row.created_at || ''}">${SDR.fmt.dateTime(row.created_at, ctx.state.tz)}</time></div>${row.body ? SDR.html`<p>${row.body}</p>` : SDR.html`<span class="chip">${({audio: 'Áudio', image: 'Imagem', imagem: 'Imagem', video: 'Vídeo', document: 'Documento', sticker: 'Figurinha'})[row.media_type] || 'Mídia'}</span>`}</article>`;
        })}` : SDR.html`<div class="empty"><h3>Esta conversa ainda não tem mensagens</h3><p>O histórico aparecerá aqui assim que houver atividade.</p></div>`);
        if (fresh) messages.scrollTop = scrolls.has(selected) ? scrolls.get(selected) : messages.scrollHeight;
        else messages.scrollTop = atBottom ? messages.scrollHeight : position;
      }
      function renderOutbox(rows, fresh) {
        const signature = JSON.stringify(rows);
        if (!fresh && signature === outboxSignature) return;
        outboxSignature = signature;
        const statuses = {pendente: ['Pendente', 'amber'], enviando: ['Enviando', 'amber'], enviado: ['Enviado', 'green'], erro: ['Erro', 'red']};
        SDR.mount(outboxContent, rows.length ? SDR.html`<ul class="cv-queue">${rows.map(row => {
          const status = statuses[row.status] || ['Status indisponível', 'muted'];
          return SDR.html`<li class="cv-queue-item"><div class="row"><span class="badge badge-${status[1]}">${status[0]}</span><time class="mono muted">${SDR.fmt.dateTime(row.created_at, ctx.state.tz)}</time></div><p>${row.body || 'Mensagem sem texto'}</p>${row.status === 'erro' && row.last_error ? SDR.html`<p class="field-error">${row.last_error}</p>` : SDR.raw('')}</li>`;
        })}</ul>` : SDR.html`<div class="empty">Nenhuma mensagem na fila deste contato.</div>`);
      }
      async function loadList(initial) {
        const version = ++listVersion;
        if (initial) SDR.mount(list, loading());
        try {
          const rows = await ctx.api.listConversations({onlyTakeover});
          if (disposed || version !== listVersion) return;
          conversations = rows || []; SDR.mount(listStatus, conversations.length >= 50 ? SDR.html`<p class="muted">Mostrando as 50 conversas mais recentes.</p>` : SDR.raw('')); renderList();
        } catch (error) {
          if (!disposed && version === listVersion) {
            if (initial) SDR.mount(list, SDR.raw(''));
            SDR.mount(listStatus, errorView(error.message, 'retry-list'));
          }
        }
      }
      async function loadThread(initial) {
        if (!selected || disposed) return;
        const id = selected, version = ++threadVersion;
        if (initial) {
          lead = null; composer.hidden = true; outbox.hidden = true;
          SDR.mount(header, SDR.html`<button class="btn btn-ghost btn-sm cv-back" type="button" data-cv-action="back">${backIcon} Voltar</button>`);
          SDR.mount(messages, loading()); SDR.mount(threadStatus, SDR.raw(''));
        }
        // Cada região tem um erro próprio: uma falha na fila não apaga o histórico.
        const results = await Promise.allSettled([ctx.api.getLead(id), ctx.api.listMessages(id, 200), ctx.api.listOutbox(id, 100)]);
        if (disposed || version !== threadVersion || selected !== id) return;
        const [leadResult, messageResult, queueResult] = results;
        if (leadResult.status === 'fulfilled') {
          lead = leadResult.value; composer.hidden = false; outbox.hidden = false; renderHeader();
        } else {
          // Falha pontual no polling não derruba o formulário: mantém o contato já carregado e só avisa no status.
          if (initial || !lead) { lead = null; updateComposer(); composer.hidden = true; }
        }
        if (messageResult.status === 'fulfilled') renderMessages(messageResult.value || [], initial);
        else if (initial) SDR.mount(messages, SDR.raw(''));
        const failure = leadResult.status === 'rejected' ? leadResult.reason : messageResult.status === 'rejected' ? messageResult.reason : null;
        SDR.mount(threadStatus, failure ? errorView(failure.message, 'retry-thread') : SDR.raw(''));
        if (queueResult.status === 'fulfilled') renderOutbox(queueResult.value || [], initial);
        else { outbox.hidden = false; outboxSignature = ''; SDR.mount(outboxContent, errorView(queueResult.reason.message, 'retry-thread')); }
      }
      function saveCurrentPosition() {
        if (selected) { drafts.set(selected, input.value); scrolls.set(selected, messages.scrollTop); }
      }
      function selectConversation(id) {
        if (!opaqueId(id) || disposed) return;
        saveCurrentPosition(); selected = id; input.value = drafts.get(id) || '';
        messageSignature = ''; outboxSignature = ''; SDR.mount(sendStatus, SDR.raw(''));
        layout.classList.add('cv-has-selection'); renderList();
        loadThread(true).then(() => {
          if (!disposed && selected === id) { const title = q('.cv-thread-title'); if (title) title.focus({preventScroll: true}); }
        }).catch(error => { if (!disposed) SDR.mount(threadStatus, errorView(error.message, 'retry-thread')); });
      }
      async function refresh() {
        if (disposed || global.document.hidden || polling || sending || changingTakeover) return;
        polling = true;
        try { await Promise.all([loadList(false), loadThread(false)]); }
        finally { polling = false; }
      }
      async function toggleTakeover() {
        if (!lead || changingTakeover || sending) return;
        const id = selected, on = !controlled(lead);
        changingTakeover = true; renderHeader();
        try {
          await ctx.api.setTakeover(id, on);
          notice(on ? 'Você assumiu a conversa por 24 horas.' : 'Conversa devolvida ao agente.', 'ok');
          if (!disposed) await Promise.all([loadList(false), selected === id ? loadThread(false) : Promise.resolve()]);
        } catch (error) {
          // Resposta perdida pode esconder uma gravação feita: relê o contato antes de mostrar erro.
          let applied = false;
          try { const fresh = await ctx.api.getLead(id); applied = controlled(fresh) === on; } catch (_) { /* segue com o erro original */ }
          if (applied) {
            notice(on ? 'Você assumiu a conversa por 24 horas.' : 'Conversa devolvida ao agente.', 'ok');
            if (!disposed) await Promise.all([loadList(false), selected === id ? loadThread(false) : Promise.resolve()]);
          } else notice(error.message, 'erro');
        }
        finally { changingTakeover = false; if (!disposed) renderHeader(); }
      }
      async function send(event) {
        if (event) event.preventDefault();
        if (disposed || sending || changingTakeover || !selected || !lead || lead.opt_out) return;
        const held = sendKeys.get(selected);
        // Retry de envio incerto reenvia o texto ORIGINAL com a mesma client_key; texto novo só depois de descartar.
        const id = selected, body = held && held.unknown ? held.body : input.value.trim();
        if (!body) return;
        sending = true; updateComposer(); renderHeader(); SDR.mount(sendStatus, SDR.raw(''));
        let queued = false, tookOver = false, reachedSend = false;
        // Chave de idempotência por tentativa: reenvio do mesmo texto reaproveita a chave, então o banco ignora a duplicata.
        let attempt = sendKeys.get(id);
        if (!attempt || attempt.body !== body) { attempt = {key: SDR.uuid(), body}; sendKeys.set(id, attempt); }
        try {
          if (attempt.unknown) {
            let found = null;
            try { found = await ctx.api.findOutboxByKey(id, attempt.key); } catch (_) { /* sem resposta: reenvia com a mesma chave, o banco ignora duplicata */ }
            if (found) {
              queued = true; drafts.delete(id); sendKeys.delete(id);
              if (!disposed && selected === id) { input.value = ''; SDR.mount(sendStatus, SDR.html`<span class="badge badge-green">Mensagem já estava na fila</span>`); }
              notice('A mensagem já estava na fila. Não envie de novo.', 'ok');
              return;
            }
          }
          let current = await ctx.api.getLead(id);
          if (disposed) return;
          if (current.opt_out) throw new Error('Este contato pediu para não receber mensagens. O envio foi bloqueado.');
          if (!controlled(current)) {
            const accepted = await ctx.confirm({title: 'Assumir e enviar a mensagem?', message: 'O agente está no controle. Ao confirmar, você assume por 24 horas para evitar respostas simultâneas e coloca esta mensagem na fila.', confirmLabel: 'Assumir e enviar'});
            if (!accepted || disposed) return;
            try { await ctx.api.setTakeover(id, true); tookOver = true; }
            catch (takeError) {
              // Resposta perdida pode esconder uma gravação feita: relê o contato antes de desistir.
              let reread = null;
              try { reread = await ctx.api.getLead(id); } catch (_) { tookOver = true; /* incerto: o ramo de falha tenta devolver ao agente */ }
              if (reread && controlled(reread)) tookOver = true;
              else throw takeError;
            }
          }
          // Revalida após o diálogo: o opt-out pode ter mudado enquanto ele estava aberto.
          current = await ctx.api.getLead(id);
          if (disposed) return;
          if (current.opt_out) throw new Error('Este contato pediu para não receber mensagens. O envio foi bloqueado.');
          reachedSend = true;
          await ctx.api.sendMessage(id, current.phone, body, attempt.key);
          queued = true; drafts.delete(id); sendKeys.delete(id);
          if (!disposed && selected === id) { input.value = ''; SDR.mount(sendStatus, SDR.html`<span class="badge badge-green">Mensagem colocada na fila</span>`); }
          notice(ctx.api.mode === 'demo' ? 'Mensagem registrada na demonstração. Nenhum envio real é feito.' : 'Mensagem colocada na fila de envio.', 'ok');
        } catch (error) {
          let message = error.message;
          // Falha depois de a requisição sair (rede, timeout, 5xx) é ambígua: o insert pode ter sido gravado.
          // Só uma consulta que ENCONTRA a linha prova o envio; nunca se afirma "não foi enviada" sem prova.
          let state = 'absent';
          if (reachedSend && error.sdrValidation) attempt.unknown = false;
          if (reachedSend && !error.sdrValidation) {
            state = 'unknown'; attempt.unknown = true; drafts.set(id, attempt.body); if (!disposed && selected === id) input.value = attempt.body;
            if (!disposed && selected === id) SDR.mount(sendStatus, SDR.html`<span class="badge badge-amber">Enviando, confirmando...</span>`);
            const delays = confirmDelays();
            for (let i = 0; i < delays.length && state === 'unknown'; i++) {
              await wait(delays[i]);
              try { if (await ctx.api.findOutboxByKey(id, attempt.key)) state = 'queued'; } catch (_) { /* tenta de novo */ }
            }
          }
          if (state === 'queued') {
            queued = true; drafts.delete(id); sendKeys.delete(id);
            message = 'A mensagem foi enviada para a fila, mas a confirmação não chegou.' + (tookOver ? ' Você continua no controle da conversa.' : '') + ' Não envie de novo.';
            if (!disposed && selected === id) input.value = '';
          } else if (state === 'unknown') {
            message = 'Não confirmamos o envio. Confira a conversa e a fila de envio antes de reenviar. Tentar de novo reenvia o mesmo texto sem duplicar.' + (tookOver ? ' Você continua no controle da conversa; devolva ao agente só depois de conferir.' : '');
          } else if (tookOver) {
            try { await ctx.api.setTakeover(id, false); message += ' A mensagem não foi enviada e a conversa foi devolvida ao agente.'; }
            catch (_) { message += ' A mensagem não foi enviada. Atenção: você ainda está no controle desta conversa. Use "Devolver ao agente" se não for responder.'; }
          }
          if (!disposed && selected === id) SDR.mount(sendStatus, errorView(message, 'retry-send', state === 'unknown' ? {action: 'discard-send', label: 'Descartar e escrever outra'} : null));
          notice(message, 'erro');
        } finally {
          sending = false;
          if (!disposed) {
            // Uma falha de atualização depois do insert nunca oferece um novo envio automático.
            await Promise.all([loadList(false), loadThread(false)]);
            updateComposer(); renderHeader();
            if (selected === id && !queued && lead && !lead.opt_out) input.focus({preventScroll: true});
          }
        }
      }
      async function discardSend() {
        const id = selected, held = sendKeys.get(id);
        if (!id || !held || !held.unknown || sending) return;
        const accepted = await ctx.confirm({title: 'Descartar e escrever outra?', message: 'A mensagem anterior pode já ter sido enviada. Confira a fila de envio antes de descartar. Ao confirmar, você poderá escrever um texto novo.', confirmLabel: 'Descartar e escrever outra'});
        if (!accepted || disposed || selected !== id) return;
        sendKeys.delete(id); drafts.delete(id); input.value = ''; SDR.mount(sendStatus, SDR.raw('')); updateComposer(); input.focus({preventScroll: true});
      }
      function onClick(event) {
        const button = event.target.closest('button');
        if (!button || !root.contains(button) || button.disabled || disposed) return;
        if (button.dataset.cvLead) { selectConversation(button.dataset.cvLead); return; }
        const action = button.dataset.cvAction;
        if (action === 'retry-list') loadList(true);
        else if (action === 'retry-thread') loadThread(false);
        else if (action === 'retry-send') send();
        else if (action === 'discard-send') discardSend();
        else if (action === 'back') {
          saveCurrentPosition(); selected = ''; lead = null; threadVersion++;
          layout.classList.remove('cv-has-selection'); composer.hidden = true; outbox.hidden = true;
          SDR.mount(header, SDR.raw('')); SDR.mount(messages, SDR.html`<div class="empty"><h3>Escolha uma conversa</h3><p>Selecione um contato para acompanhar o histórico e a fila de envio.</p></div>`); SDR.mount(threadStatus, SDR.raw('')); renderList();
          q('[data-cv-filter]').focus({preventScroll: true});
        } else if (action === 'takeover') toggleTakeover();
        else if (action === 'lead' && selected) { Promise.resolve().then(() => ctx.openLead(selected)).catch(error => notice(error.message, 'erro')); }
      }
      function onInput() { if (selected) drafts.set(selected, input.value); updateComposer(); }
      function onKey(event) { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); send(); } }
      function onFilter() { onlyTakeover = q('[data-cv-filter]').checked; loadList(true); }
      function onVisibility() { if (!global.document.hidden) refresh(); }
      root.addEventListener('click', onClick); composer.addEventListener('submit', send);
      input.addEventListener('input', onInput); input.addEventListener('keydown', onKey);
      q('[data-cv-filter]').addEventListener('change', onFilter); global.document.addEventListener('visibilitychange', onVisibility);
      SDR.mount(messages, SDR.html`<div class="empty"><h3>Escolha uma conversa</h3><p>Selecione um contato para acompanhar o histórico e a fila de envio.</p></div>`);
      loadList(true);
      const initialId = ctx.params && ctx.params.get('lead');
      if (initialId && opaqueId(initialId)) selectConversation(initialId);
      else if (initialId) SDR.mount(threadStatus, SDR.html`<div class="banner" role="alert">O identificador da conversa é inválido. Escolha um contato na lista.</div>`);
      const interval = global.setInterval(refresh, 8000);
      return function cleanup() {
        disposed = true; listVersion++; threadVersion++; global.clearInterval(interval);
        root.removeEventListener('click', onClick); composer.removeEventListener('submit', send);
        input.removeEventListener('input', onInput); input.removeEventListener('keydown', onKey);
        q('[data-cv-filter]').removeEventListener('change', onFilter); global.document.removeEventListener('visibilitychange', onVisibility);
      };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
