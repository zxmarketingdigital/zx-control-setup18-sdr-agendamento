(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  SDR.views = SDR.views || {};
  const clone = value => JSON.parse(JSON.stringify(value));
  const asList = value => Array.isArray(value) ? clone(value) : [];
  const tones = ['Acolhedor e objetivo', 'Profissional e direto', 'Próximo e informal'];
  const tabs = [
    ['agente', 'Agente'], ['qualificacao', 'Qualificação'], ['playbook', 'Objeções e follow-up'],
    ['closers', 'Closers'], ['teste', 'Modo teste']
  ];
  const simulation = 'python -m motor simulate --phone +5511999990000 --text "oi"';
  const text = value => String(value == null ? '' : value);
  const emailValid = value => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);

  SDR.views.config = {
    title: 'Configuração do agente',
    async render(root, ctx) {
      let alive = true, tab = 'agente', busy = false, confirming = false, data = null;
      let drafts = {}, baselines = {}, errorMessage = '', retryAction = null, idCounter = 0;
      const frame = SDR.el('div', { class: 'cf-view stack' });
      const nav = SDR.el('div', { class: 'tabs cf-tabs', role: 'tablist', 'aria-label': 'Configuração do agente' });
      const content = SDR.el('div', { class: 'cf-content', role: 'tabpanel', id: 'cf-tabpanel' });
      const feedback = SDR.el('div', { class: 'cf-feedback', 'aria-live': 'polite' });
      const status = SDR.el('span', { class: 'muted cf-dirty', role: 'status', 'aria-live': 'polite' });
      const save = SDR.el('button', { type: 'submit', class: 'btn btn-primary', form: 'cf-form' }, 'Salvar');
      const footer = SDR.el('div', { class: 'cf-savebar row' }, status, save);
      frame.append(nav, feedback, content, footer);
      root.replaceChildren(frame);

      function notify(message, kind) { if (alive) ctx.toast(message, kind); }
      function dirty(key) { return JSON.stringify(drafts[key]) !== baselines[key]; }
      function hasChanges() { return Object.keys(drafts).some(dirty); }
      function updateStatus() {
        if (!alive || !data) return;
        status.textContent = dirty(tab) ? 'Alterações não salvas' : 'Tudo salvo nesta aba';
        save.disabled = busy || confirming || !dirty(tab);
        save.textContent = busy ? 'Salvando...' : 'Salvar';
        nav.querySelectorAll('button').forEach(button => { button.disabled = busy || confirming; });
        content.setAttribute('aria-busy', String(busy));
      }
      function showError(error, action) {
        if (!alive) return;
        errorMessage = error && error.message || 'Não foi possível concluir. Tente de novo.';
        retryAction = action;
        feedback.replaceChildren(SDR.el('div', { class: 'banner banner-error', role: 'alert' },
          SDR.el('p', {}, errorMessage),
          SDR.el('button', { type: 'button', class: 'btn btn-ghost btn-sm', onclick: () => { if (retryAction) retryAction(); } }, 'Tentar de novo')));
      }
      function clearError() { errorMessage = ''; retryAction = null; feedback.replaceChildren(); }
      function snapshot(key) { baselines[key] = JSON.stringify(drafts[key]); }
      function initDrafts(agent, closers) {
        data = agent;
        const toneParts = text(agent.tone).split('\n\n');
        const preset = tones.includes(toneParts[0]) ? toneParts.shift() : 'Personalizado';
        drafts = {
          agente: {
            agent_name: text(agent.agent_name), company: text(agent.company), tonePreset: preset,
            toneInstructions: toneParts.join('\n\n'), niche: text(agent.niche), offer: text(agent.offer),
            knowledge: text(agent.knowledge), owner_phone: text(agent.owner_phone),
            ai_provider: text(agent.ai_provider || 'gemini'), paused: !!agent.paused,
            followup_window_start: text(agent.followup_window_start || '08:00').slice(0, 5),
            followup_window_end: text(agent.followup_window_end || '20:00').slice(0, 5)
          },
          qualificacao: { qualification_questions: asList(agent.qualification_questions), disqualify_rules: asList(agent.disqualify_rules) },
          playbook: {
            objection_playbook: asList(agent.objection_playbook).map(row => ({ categoria: text(row.categoria), sinais: Array.isArray(row.sinais) ? row.sinais.join(', ') : text(row.sinais), resposta: text(row.resposta) })),
            followup_messages: asList(agent.followup_messages), confirm_template: text(agent.confirm_template),
            reminder_24h_template: text(agent.reminder_24h_template), reminder_2h_template: text(agent.reminder_2h_template)
          },
          closers: asList(closers).map(closerDraft),
          teste: { test_only: agent.test_only !== false, test_phones: asList(agent.test_phones) }
        };
        Object.keys(drafts).forEach(snapshot);
      }
      function closerDraft(row) {
        return { id: row.id || null, name: text(row.name), whatsapp: text(row.whatsapp), email: text(row.email), active: row.active !== false, weight: row.weight == null ? 1 : row.weight, color: /^#[0-9a-f]{6}$/i.test(row.color) ? row.color : primaryColor() };
      }
      function primaryColor() {
        const style = global.getComputedStyle ? global.getComputedStyle(global.document.documentElement).getPropertyValue('--primary').trim() : '';
        return /^#[0-9a-f]{6}$/i.test(style) ? style : '#D97706';
      }
      function field(label, object, key, options) {
        const opts = options || {}, id = 'cf-input-' + (++idCounter);
        const attrs = { id, name: key, class: opts.multiline ? 'textarea' : opts.choices ? 'select' : 'input' };
        if (opts.multiline) attrs.rows = opts.rows || 3;
        else if (!opts.choices) attrs.type = opts.type || 'text';
        if (opts.required) attrs.required = true;
        if (opts.min != null) attrs.min = opts.min;
        if (opts.max != null) attrs.max = opts.max;
        if (opts.step != null) attrs.step = opts.step;
        if (opts.placeholder) attrs.placeholder = opts.placeholder;
        if (opts.type === 'tel') attrs.autocomplete = 'tel';
        const input = SDR.el(opts.multiline ? 'textarea' : opts.choices ? 'select' : 'input', attrs);
        if (opts.choices) {
          const choices = opts.choices.slice();
          if (object[key] && !choices.some(choice => (Array.isArray(choice) ? choice[0] : choice) === object[key])) choices.push([text(object[key]), text(object[key])]);
          choices.forEach(choice => { const pair = Array.isArray(choice) ? choice : [choice, choice]; input.appendChild(SDR.el('option', { value: pair[0] }, pair[1])); });
        }
        input.value = text(object[key]);
        input.addEventListener(opts.choices ? 'change' : 'input', () => { object[key] = input.value; updateStatus(); });
        const wrapper = SDR.el('div', { class: 'field' }, SDR.el('label', { class: 'label', for: id }, label), input);
        if (opts.hint) {
          const hint = SDR.el('p', { id: id + '-hint', class: 'muted cf-hint' }, opts.hint);
          input.setAttribute('aria-describedby', hint.id); wrapper.appendChild(hint);
        }
        return wrapper;
      }
      function toggle(label, object, key, description, onChange) {
        const id = 'cf-input-' + (++idCounter);
        const input = SDR.el('input', { id, type: 'checkbox', role: 'switch', checked: !!object[key], class: 'cf-switch-input' });
        input.checked = !!object[key];
        input.addEventListener('change', async () => {
          const previous = !!object[key], next = input.checked;
          if (onChange) {
            input.disabled = true; confirming = true; updateStatus();
            try {
              const accepted = await onChange(next);
              if (!alive) return;
              object[key] = accepted ? next : previous;
            } catch (error) { object[key] = previous; notify(error.message || 'Não foi possível confirmar a alteração.', 'erro'); }
            finally { confirming = false; input.disabled = false; input.checked = !!object[key];
              const notice = content.querySelector('[data-cf-test-notice]');
              if (notice) notice.textContent = object[key] ? 'Modo teste ligado. As mudanças só entram em vigor ao salvar.' : 'Ao liberar, o agente responde a qualquer lead que mandar mensagem. Salve para aplicar.';
              updateStatus(); }
          } else { object[key] = next; updateStatus(); }
        });
        const wrapper = SDR.el('div', { class: 'cf-toggle' }, SDR.el('label', { class: 'row label', for: id }, input, SDR.el('span', {}, label)));
        if (description) {
          const hint = SDR.el('p', { id: id + '-hint', class: 'muted cf-hint' }, description);
          input.setAttribute('aria-describedby', hint.id); wrapper.appendChild(hint);
        }
        return wrapper;
      }
      function section(title, description, ...children) {
        return SDR.el('section', { class: 'card stack cf-section' }, SDR.el('header', {}, SDR.el('h3', {}, title), description ? SDR.el('p', { class: 'muted cf-hint' }, description) : null), ...children);
      }
      function makeButton(label, onClick, kind) {
        return SDR.el('button', { type: 'button', class: 'btn btn-sm btn-' + (kind || 'ghost'), onclick: event => { if (!busy && !confirming) return onClick(event); } }, label);
      }
      function editor(title, description, items, schema, newItem, reorder) {
        const list = SDR.el('div', { class: 'stack cf-editor-list' });
        if (!items.length) list.appendChild(SDR.el('div', { class: 'empty' }, 'Nenhum item cadastrado. Adicione o primeiro para orientar o agente.'));
        items.forEach((item, index) => {
          const actions = SDR.el('div', { class: 'row cf-editor-actions' });
          if (reorder) {
            const up = makeButton('Subir', () => { [items[index - 1], items[index]] = [items[index], items[index - 1]]; draw(); });
            const down = makeButton('Descer', () => { [items[index], items[index + 1]] = [items[index + 1], items[index]]; draw(); });
            up.disabled = index === 0; down.disabled = index === items.length - 1;
            up.setAttribute('aria-label', 'Subir item ' + (index + 1)); down.setAttribute('aria-label', 'Descer item ' + (index + 1));
            actions.append(up, down);
          }
          const remove = makeButton('Remover', () => { items.splice(index, 1); draw(); }, 'danger');
          remove.setAttribute('aria-label', 'Remover item ' + (index + 1) + ' de ' + title); actions.appendChild(remove);
          const fields = SDR.el('div', { class: 'grid cf-form-grid' });
          schema.forEach(spec => fields.appendChild(spec.check ? toggle(spec.label, item, spec.key) : field(spec.label, item, spec.key, spec)));
          list.appendChild(SDR.el('article', { class: 'cf-editor-item stack' }, SDR.el('div', { class: 'row cf-item-heading' }, SDR.el('strong', { class: 'mono' }, String(index + 1).padStart(2, '0')), actions), fields));
        });
        return section(title, description, list, makeButton('Adicionar', () => { items.push(clone(newItem)); draw(); }));
      }
      function drawAgent(form) {
        const draft = drafts.agente;
        const identity = SDR.el('div', { class: 'grid cf-form-grid' },
          field('Nome do agente', draft, 'agent_name', { required: true }), field('Empresa', draft, 'company'),
          field('Tom de voz', draft, 'tonePreset', { choices: tones.concat('Personalizado') }),
          field('Instruções de tom', draft, 'toneInstructions', { multiline: true, hint: 'Complemente o tom escolhido. No modo personalizado, descreva aqui como o agente deve conversar.' }),
          field('Nicho', draft, 'niche'), field('Oferta', draft, 'offer', { multiline: true }),
          field('WhatsApp do dono (E.164)', draft, 'owner_phone', { type: 'tel', placeholder: '+5511900000001', hint: 'Inclua + e o código do país. Deixe vazio se ainda não usar notificações.' }),
          field('Provedor de IA', draft, 'ai_provider', { choices: [['gemini', 'Gemini'], ['openai', 'OpenAI'], ['anthropic', 'Anthropic']] }));
        form.append(section('Identidade e atendimento', 'Defina como o agente representa seu negócio.', identity,
          SDR.el('div', { class: 'banner' }, 'A chave da IA fica só no computador, em config/config.json.')),
        section('Conhecimento', 'Escreva as informações que o agente pode usar. O conteúdo é salvo como Markdown.',
          field('Base de conhecimento', draft, 'knowledge', { multiline: true, rows: 12 })),
        section('Operação e follow-up', 'Os horários seguem o fuso do negócio: ' + (ctx.state.tz || SDR.tz) + '.',
          toggle('Pausar agente', draft, 'paused', 'Enquanto estiver pausado, o motor não deve iniciar respostas automáticas.'),
          SDR.el('div', { class: 'grid cf-form-grid' },
            field('Início da janela de follow-up', draft, 'followup_window_start', { type: 'time', required: true }),
            field('Fim da janela de follow-up', draft, 'followup_window_end', { type: 'time', required: true }))));
      }
      function drawQualification(form) {
        const draft = drafts.qualificacao;
        form.append(editor('Perguntas de qualificação', 'A ordem abaixo orienta a conversa. Use chaves únicas, sem espaços.', draft.qualification_questions, [
          { key: 'key', label: 'Chave', required: true }, { key: 'pergunta', label: 'Pergunta', multiline: true, required: true },
          { key: 'obrigatoria', label: 'Obrigatória', check: true }
        ], { key: '', pergunta: '', obrigatoria: false }, true),
        editor('Regras de desqualificação', 'Defina situações em que o atendimento não deve avançar.', draft.disqualify_rules, [
          { key: 'key', label: 'Chave', required: true }, { key: 'regra', label: 'Regra', multiline: true, required: true },
          { key: 'mensagem', label: 'Mensagem ao contato', multiline: true, required: true }
        ], { key: '', regra: '', mensagem: '' }, true));
      }
      function drawPlaybook(form) {
        const draft = drafts.playbook;
        form.append(editor('Playbook de objeções', 'Use o ranking da tela Objeções para ajustar os sinais e as respostas.', draft.objection_playbook, [
          { key: 'categoria', label: 'Categoria', required: true }, { key: 'sinais', label: 'Sinais separados por vírgula', required: true },
          { key: 'resposta', label: 'Resposta', multiline: true, required: true }
        ], { categoria: '', sinais: '', resposta: '' }, false),
        editor('Cadência de follow-up', 'Cada intervalo é contado em horas. Use valores positivos, em ordem crescente.', draft.followup_messages, [
          { key: 'apos_horas', label: 'Após quantas horas', type: 'number', min: 0.01, step: 'any', required: true },
          { key: 'texto', label: 'Mensagem', multiline: true, required: true }
        ], { apos_horas: 24, texto: '' }, true),
        section('Confirmações e lembretes', 'Variáveis disponíveis: {nome}, {data} e {hora}. Elas são substituídas pelo motor.',
          field('Confirmação do agendamento', draft, 'confirm_template', { multiline: true, required: true }),
          field('Lembrete de 24 horas', draft, 'reminder_24h_template', { multiline: true }),
          field('Lembrete de 2 horas', draft, 'reminder_2h_template', { multiline: true })));
      }
      function drawClosers(form) {
        const cards = SDR.el('div', { class: 'grid cf-closers' });
        if (!drafts.closers.length) cards.appendChild(SDR.el('div', { class: 'empty' }, 'Nenhum closer cadastrado. Adicione um responsável para distribuir os agendamentos.'));
        drafts.closers.forEach((closer, index) => {
          const remove = makeButton('Remover closer', () => removeCloser(index), 'danger');
          const fields = SDR.el('div', { class: 'grid cf-form-grid' },
            field('Nome', closer, 'name', { required: true }),
            field('WhatsApp (E.164)', closer, 'whatsapp', { type: 'tel', placeholder: '+5511900000001' }),
            field('E-mail', closer, 'email', { type: 'email', placeholder: 'contato@exemplo.com' }),
            field('Peso na distribuição', closer, 'weight', { type: 'number', min: 1, max: 10, step: 1, required: true }),
            field('Cor na agenda', closer, 'color', { type: 'color' }));
          cards.appendChild(section(closer.name || 'Novo closer', closer.id ? 'Responsável cadastrado' : 'Será cadastrado ao salvar.', fields,
            toggle('Ativo', closer, 'active', 'Desative para parar de receber novos horários e preservar o histórico.'), remove));
        });
        form.append(SDR.el('p', { class: 'muted cf-hint' }, 'O peso é usado quando a distribuição da agenda está configurada por peso. Exclusões são aplicadas após a confirmação.'), cards,
          makeButton('Adicionar closer', () => { drafts.closers.push(closerDraft({ active: true, weight: 1 })); draw(); }));
      }
      function drawTest(form) {
        const draft = drafts.teste;
        const phones = SDR.el('div', { class: 'stack cf-phone-list' });
        if (!draft.test_phones.length) phones.appendChild(SDR.el('div', { class: 'empty' }, 'Nenhum número autorizado. Com o modo teste ligado, adicione um número para testar o atendimento.'));
        draft.test_phones.forEach((phone, index) => phones.appendChild(SDR.el('div', { class: 'row cf-phone-row' },
          SDR.el('span', { class: 'mono' }, SDR.fmt.phone(phone)), makeButton('Remover', () => { draft.test_phones.splice(index, 1); draw(); }, 'danger'))));
        const phoneDraft = { phone: '' };
        const phoneField = field('Número autorizado (E.164)', phoneDraft, 'phone', { type: 'tel', placeholder: '+5511900000001' });
        const add = makeButton('Adicionar número', () => {
          const normalized = SDR.phone.toE164(phoneDraft.phone);
          if (!normalized) { showError(new Error('Informe um telefone internacional válido, começando com + e o código do país.'), () => add.click()); return; }
          if (draft.test_phones.includes(normalized)) { showError(new Error('Esse número já está na lista de testes.'), () => phoneField.querySelector('input').focus()); return; }
          draft.test_phones.push(normalized); draw();
        });
        const command = SDR.el('code', { class: 'mono', tabindex: '0' }, simulation);
        const copyButton = makeButton('Copiar comando', async () => {
          try {
            if (global.navigator.clipboard && global.navigator.clipboard.writeText) { await global.navigator.clipboard.writeText(simulation); notify('Comando copiado.', 'ok'); }
            else { const range = global.document.createRange(); range.selectNodeContents(command); const selection = global.getSelection(); selection.removeAllRanges(); selection.addRange(range); command.focus(); notify('Comando selecionado. Use Ctrl/Cmd+C para copiar.', 'info'); }
          } catch (_) { command.focus(); notify('Selecione o comando e use Ctrl/Cmd+C para copiar.', 'info'); }
        });
        form.append(section('Modo teste', 'Com o modo teste ligado, o agente responde somente aos números autorizados abaixo.',
          toggle('Responder somente aos números de teste', draft, 'test_only', 'Recomendado enquanto você configura e confere o comportamento do agente.', async next => next || ctx.confirm({
            title: 'Liberar atendimento para todos?', message: 'Ao liberar, o agente responde a qualquer lead que mandar mensagem', confirmLabel: 'Liberar atendimento', danger: true
          })),
          SDR.el('div', { class: 'banner', 'data-cf-test-notice': '' }, draft.test_only ? 'Modo teste ligado. As mudanças só entram em vigor ao salvar.' : 'Ao liberar, o agente responde a qualquer lead que mandar mensagem. Salve para aplicar.'),
          phones, SDR.el('div', { class: 'row cf-phone-add' }, phoneField, add)),
        section('Simular pelo terminal', 'Execute no terminal do seu computador. A simulação não é iniciada por este painel.',
          SDR.el('pre', { class: 'cf-command' }, command), copyButton));
      }
      function drawNav() {
        nav.replaceChildren();
        tabs.forEach(([key, label]) => {
          const selected = key === tab;
          nav.appendChild(SDR.el('button', {
            type: 'button', id: 'cf-tab-' + key, class: 'tab', role: 'tab',
            'aria-selected': String(selected), 'aria-controls': 'cf-tabpanel', tabindex: selected ? '0' : '-1',
            onclick: () => changeTab(key)
          }, label));
        });
        content.setAttribute('aria-labelledby', 'cf-tab-' + tab);
      }
      function draw() {
        if (!alive || !data) return;
        clearError(); drawNav();
        const form = SDR.el('form', { id: 'cf-form', class: 'stack', novalidate: true });
        form.addEventListener('submit', event => { event.preventDefault(); saveCurrent(); });
        content.replaceChildren(form);
        ({ agente: drawAgent, qualificacao: drawQualification, playbook: drawPlaybook, closers: drawClosers, teste: drawTest })[tab](form);
        updateStatus();
      }
      async function changeTab(next) {
        if (next === tab || busy || confirming || !data) return;
        confirming = true; updateStatus();
        try {
          if (dirty(tab) && !await ctx.confirm({ title: 'Alterações não salvas', message: 'Deseja descartar as alterações desta aba e continuar?', confirmLabel: 'Descartar alterações', danger: true })) return;
          if (!alive) return;
          if (dirty(tab)) drafts[tab] = JSON.parse(baselines[tab]);
          tab = next; draw();
          const selected = nav.querySelector('[aria-selected="true"]'); if (selected) selected.focus();
        } catch (error) { showError(error, () => changeTab(next)); }
        finally { confirming = false; updateStatus(); }
      }
      nav.addEventListener('keydown', event => {
        const index = tabs.findIndex(item => item[0] === tab);
        let next;
        if (event.key === 'ArrowRight') next = tabs[(index + 1) % tabs.length][0];
        else if (event.key === 'ArrowLeft') next = tabs[(index + tabs.length - 1) % tabs.length][0];
        else if (event.key === 'Home') next = tabs[0][0];
        else if (event.key === 'End') next = tabs[tabs.length - 1][0];
        if (next) { event.preventDefault(); changeTab(next); }
      });
      function required(value, message) { if (!text(value).trim()) throw new Error(message); return text(value).trim(); }
      function international(value, label) {
        if (!text(value).trim()) return '';
        const result = SDR.phone.toE164(value);
        if (!result) throw new Error(label + ': informe um número válido com + e o código do país.');
        return result;
      }
      function validateKeys(rows, kind) {
        const seen = new Set();
        rows.forEach(row => {
          required(row.key, 'Preencha a chave de cada ' + kind + '.');
          if (!/^[a-zA-Z][a-zA-Z0-9_]*$/.test(row.key)) throw new Error('Use chaves iniciadas por uma letra, com letras, números e sublinhado.');
          if (seen.has(row.key)) throw new Error('Cada ' + kind + ' precisa ter uma chave única.');
          seen.add(row.key);
        });
      }
      function patchFor(key) {
        const d = drafts[key];
        if (key === 'agente') {
          const name = required(d.agent_name, 'Informe o nome do agente.');
          if (!['gemini', 'openai', 'anthropic'].includes(d.ai_provider)) throw new Error('Escolha um provedor de IA válido.');
          if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(d.followup_window_start) || !/^([01]\d|2[0-3]):[0-5]\d$/.test(d.followup_window_end) || d.followup_window_start >= d.followup_window_end) throw new Error('A janela de follow-up precisa começar antes de terminar, no mesmo dia.');
          return {
            agent_name: name, company: d.company.trim(), tone: (d.tonePreset === 'Personalizado' ? d.toneInstructions : d.tonePreset + (d.toneInstructions.trim() ? '\n\n' + d.toneInstructions.trim() : '')).trim(),
            niche: d.niche.trim(), offer: d.offer.trim(), knowledge: d.knowledge, owner_phone: international(d.owner_phone, 'WhatsApp do dono'),
            ai_provider: d.ai_provider, paused: !!d.paused, followup_window_start: d.followup_window_start, followup_window_end: d.followup_window_end
          };
        }
        if (key === 'qualificacao') {
          validateKeys(d.qualification_questions, 'pergunta'); validateKeys(d.disqualify_rules, 'regra');
          return {
            qualification_questions: d.qualification_questions.map(row => ({ key: row.key, pergunta: required(row.pergunta, 'Preencha o texto de cada pergunta.'), obrigatoria: !!row.obrigatoria })),
            disqualify_rules: d.disqualify_rules.map(row => ({ key: row.key, regra: required(row.regra, 'Preencha cada regra de desqualificação.'), mensagem: required(row.mensagem, 'Preencha a mensagem de cada regra de desqualificação.') }))
          };
        }
        if (key === 'playbook') {
          let previous = 0;
          const followups = d.followup_messages.map(row => {
            const hours = Number(row.apos_horas);
            if (!Number.isFinite(hours) || hours <= previous) throw new Error('Os intervalos de follow-up devem ser positivos e estar em ordem crescente.');
            previous = hours; return { apos_horas: hours, texto: required(row.texto, 'Preencha a mensagem de cada follow-up.') };
          });
          return {
            objection_playbook: d.objection_playbook.map(row => {
              const signals = text(row.sinais).split(',').map(value => value.trim()).filter(Boolean);
              if (!signals.length) throw new Error('Informe pelo menos um sinal para cada objeção.');
              return { categoria: required(row.categoria, 'Informe a categoria de cada objeção.'), sinais: signals, resposta: required(row.resposta, 'Informe a resposta de cada objeção.') };
            }),
            followup_messages: followups, confirm_template: required(d.confirm_template, 'Preencha o texto de confirmação.'),
            reminder_24h_template: d.reminder_24h_template, reminder_2h_template: d.reminder_2h_template
          };
        }
        if (key === 'teste') return { test_only: !!d.test_only, test_phones: Array.from(new Set(d.test_phones.map(phone => international(phone, 'Número de teste')))) };
        return null;
      }
      function closerPayload(closer) {
        const weight = Number(closer.weight);
        if (!Number.isInteger(weight) || weight < 1 || weight > 10) throw new Error('O peso de cada closer deve ser um inteiro entre 1 e 10.');
        if (closer.email && !emailValid(closer.email.trim())) throw new Error('Confira o e-mail de cada closer.');
        if (!/^#[0-9a-f]{6}$/i.test(closer.color)) throw new Error('Escolha uma cor válida para cada closer.');
        const payload = { name: required(closer.name, 'Informe o nome de cada closer.'), whatsapp: international(closer.whatsapp, 'WhatsApp do closer'), email: closer.email.trim(), active: !!closer.active, weight, color: closer.color };
        if (closer.id) payload.id = closer.id;
        return payload;
      }
      async function refreshShared() {
        try { await ctx.refreshState(); }
        catch (_) { notify('Os dados foram salvos. Não foi possível atualizar o restante do painel; recarregue a página.', 'info'); }
      }
      async function saveClosers() {
        const previous = JSON.parse(baselines.closers);
        const payloads = drafts.closers.map(closerPayload); // Valida todos antes da primeira gravação.
        for (let i = 0; i < payloads.length; i++) {
          const old = previous.find(row => row.id && row.id === drafts.closers[i].id);
          if (old && JSON.stringify(old) === JSON.stringify(drafts.closers[i])) continue;
          const saved = await ctx.api.saveCloser(payloads[i]);
          // Os inputs mantêm referência ao rascunho. Preserve o objeto para que
          // uma segunda edição, sem trocar de aba, continue sendo detectada.
          Object.assign(drafts.closers[i], closerDraft(saved));
          // Guarda cada sucesso: se uma gravação posterior falhar, tentar de novo não duplica inserts.
          const index = previous.findIndex(row => saved.id && row.id === saved.id);
          if (index < 0) previous.push(clone(drafts.closers[i])); else previous[index] = clone(drafts.closers[i]);
          baselines.closers = JSON.stringify(previous);
          ctx.state.closers = previous.filter(row => row.id);
        }
        snapshot('closers');
      }
      async function saveCurrent() {
        if (!alive || busy || confirming || !data || !dirty(tab)) return;
        const current = tab;
        let patch;
        try { if (current !== 'closers') patch = patchFor(current); else drafts.closers.map(closerPayload); }
        catch (error) { showError(error, saveCurrent); notify(error.message, 'erro'); return; }
        busy = true; clearError(); updateStatus();
        const fields = content.querySelectorAll('input,select,textarea,button'); fields.forEach(input => { input.dataset.cfWasDisabled = String(input.disabled); input.disabled = true; });
        try {
          if (current === 'closers') await saveClosers();
          else {
            const saved = await ctx.api.saveAgentConfig(patch);
            data = Object.assign({}, data, saved || {}, patch);
            ctx.state.agent = Object.assign({}, ctx.state.agent, data);
            snapshot(current);
          }
          await refreshShared();
          notify(current === 'closers' ? 'Closers salvos.' : 'Configuração salva.', 'ok');
        } catch (error) { showError(error, saveCurrent); notify(error.message || 'Não foi possível salvar.', 'erro'); if (current === 'closers') await refreshShared(); }
        finally {
          busy = false;
          if (alive) { fields.forEach(input => { input.disabled = input.dataset.cfWasDisabled === 'true'; delete input.dataset.cfWasDisabled; }); updateStatus(); }
        }
      }
      async function removeCloser(index) {
        if (!alive || busy) return;
        const closer = drafts.closers[index];
        if (!closer) return;
        busy = true; updateStatus();
        const controls = Array.from(content.querySelectorAll('input,select,textarea,button')).map(input => ({ input, disabled: input.disabled }));
        controls.forEach(item => { item.input.disabled = true; });
        try {
          if (!await ctx.confirm({ title: 'Remover closer?', message: 'Deseja remover este responsável? Os vínculos e horários de disponibilidade podem ser excluídos. Para preservar o histórico, prefira desativar.', confirmLabel: 'Remover closer', danger: true })) return;
          if (!alive) return;
          if (closer.id) {
            await ctx.api.deleteCloser(closer.id);
            const saved = JSON.parse(baselines.closers).filter(row => row.id !== closer.id);
            baselines.closers = JSON.stringify(saved); ctx.state.closers = saved;
          }
          drafts.closers.splice(index, 1);
          if (closer.id) await refreshShared();
          notify('Closer removido.', 'ok'); draw();
        } catch (error) { showError(error, () => removeCloser(index)); notify(error.message || 'Não foi possível remover.', 'erro'); }
        finally { busy = false; controls.forEach(item => { item.input.disabled = item.disabled; }); updateStatus(); }
      }
      async function load() {
        if (!alive) return;
        clearError(); footer.hidden = true; nav.hidden = true;
        SDR.mount(content, SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`);
        try {
          const result = await Promise.all([ctx.api.getAgentConfig(), ctx.api.listClosers()]);
          if (!alive) return;
          initDrafts(result[0] || {}, result[1] || []);
          ctx.state.agent = result[0]; ctx.state.closers = result[1];
          footer.hidden = false; nav.hidden = false; draw();
        } catch (error) { if (alive) { content.replaceChildren(); showError(error, load); } }
      }
      function beforeUnload(event) { if (hasChanges()) { event.preventDefault(); event.returnValue = ''; } }
      global.addEventListener('beforeunload', beforeUnload);
      await load();
      return () => { alive = false; global.removeEventListener('beforeunload', beforeUnload); };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
