(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  SDR.views = SDR.views || {};
  const DAYS = ['Dom', 'Seg', 'Ter', 'Qua', 'Qui', 'Sex', 'Sáb'];
  const TABS = [['horarios', 'Horários de atendimento'], ['parametros', 'Parâmetros da agenda'], ['bloqueios', 'Bloqueios e feriados'], ['publica', 'Página pública']];
  const ZONES = ['America/Sao_Paulo', 'America/Fortaleza', 'America/Recife', 'America/Bahia', 'America/Belem', 'America/Manaus', 'America/Cuiaba', 'America/Campo_Grande', 'America/Porto_Velho', 'America/Boa_Vista', 'America/Rio_Branco', 'America/Noronha', 'Europe/Lisbon', 'Atlantic/Madeira', 'Atlantic/Azores'];
  const selected = value => value ? SDR.raw('selected') : SDR.raw('');
  const checked = value => value ? SDR.raw('checked') : SDR.raw('');
  const minutes = value => { const match = /^([01]\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?$/.exec(String(value || '')); return match ? +match[1] * 60 + +match[2] : NaN; };
  const shortTime = value => String(value || '').slice(0, 5);
  const sameSpan = (a, b) => minutes(a.start_time) === minutes(b.start_time) && minutes(a.end_time) === minutes(b.end_time);
  const overlap = (a, b) => minutes(a.start_time) < minutes(b.end_time) && minutes(a.end_time) > minutes(b.start_time);
  function validateSpan(rule, others) {
    if (!Number.isFinite(minutes(rule.start_time)) || !Number.isFinite(minutes(rule.end_time)) || minutes(rule.start_time) >= minutes(rule.end_time)) throw new Error('O horário de início deve ser anterior ao horário de fim.');
    if (others.some(row => (!rule.id || row.id !== rule.id) && row.closer_id === rule.closer_id && +row.weekday === +rule.weekday && overlap(rule, row))) throw new Error('Esta faixa se sobrepõe a outra do mesmo dia. Ajuste os horários antes de salvar.');
  }
  function wallToISO(value, tz) {
    const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(String(value || ''));
    if (!match) throw new Error('Informe a data e o horário completos.');
    const [, y, m, d, hh, mm] = match.map(Number);
    if (y < 1900 || y > 9999 || m < 1 || m > 12 || d < 1 || d > 31 || hh > 23 || mm > 59) throw new Error('Informe uma data e um horário válidos.');
    const date = SDR.tzTools.zonedToUtc(y, m, d, hh, mm, tz), p = SDR.tzTools.parts(date, tz);
    if (p.y !== y || p.m !== m || p.d !== d || p.hh !== hh || p.mm !== mm) throw new Error('Esta data ou horário não existe no fuso da agenda.');
    return date.toISOString();
  }
  SDR.views.disponibilidade = {
    title: 'Disponibilidade',
    async render(root, ctx) {
      let alive = true, revision = 0, busy = false, retryAction = null;
      let rules = [], blocks = [], activeTab = 'horarios';
      let settings = Object.assign({}, ctx.state.settings);
      const closers = () => (ctx.state.closers || []).filter(row => row.active);
      const q = selector => SDR.qs(selector, root);
      const message = error => error && error.message ? error.message : 'Não foi possível concluir a operação. Tente de novo.';
      const tz = () => settings.timezone || ctx.state.tz || SDR.tz;
      const byCloser = id => (ctx.state.closers || []).find(row => row.id === id);
      const closerName = id => !id ? 'Todos os responsáveis' : (byCloser(id) || {}).name || 'Responsável removido';

      function notice(error, retry) {
        if (!alive) return;
        retryAction = retry || null;
        const box = q('#dp-notice');
        if (!box) return;
        box.hidden = !error;
        SDR.mount(box, error ? SDR.html`<div class="row"><span>${message(error)}</span>${retry ? SDR.html`<button type="button" class="btn btn-ghost btn-sm" data-dp-action="retry-operation">Tentar de novo</button>` : SDR.raw('')}</div>` : SDR.raw(''));
      }
      function loading() {
        root.setAttribute('aria-busy', 'true');
        SDR.mount(root, SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`);
      }
      function closerOptions(value, all) {
        return SDR.html`${all ? SDR.html`<option value="">Todos os responsáveis</option>` : SDR.raw('')}${closers().map(row => SDR.html`<option value="${row.id}" ${selected(row.id === value)}>${row.name}</option>`)}`;
      }
      function rulePanel() {
        return SDR.html`<div class="stack"><div class="dp-head"><div><h2>Horários de atendimento</h2><p class="muted">Defina as faixas em que cada responsável pode receber calls. Fuso: <span class="mono">${tz()}</span>.</p></div><span class="badge badge-amber mono">${rules.length} faixas</span></div>${closers().length ? closers().map(closer => SDR.html`<section class="card stack dp-closer"><div class="row dp-head"><h3>${closer.name}</h3><button type="button" class="btn btn-ghost btn-sm" data-dp-action="copy-monday" data-closer="${closer.id}">Copiar seg para seg-sex</button></div><div class="dp-rule-grid">${DAYS.map((day, weekday) => {
          const dayRules = rules.filter(row => row.closer_id === closer.id && +row.weekday === weekday).sort((a, b) => a.start_time.localeCompare(b.start_time));
          return SDR.html`<section class="dp-day" aria-label="${day}, ${closer.name}"><h4>${day}</h4><div class="stack">${dayRules.length ? dayRules.map(rule => SDR.html`<div class="dp-rule-line"><span class="mono">${shortTime(rule.start_time)}–${shortTime(rule.end_time)}</span><button type="button" class="btn btn-ghost btn-sm" data-dp-action="delete-rule" data-id="${rule.id}" aria-label="Remover faixa ${shortTime(rule.start_time)} a ${shortTime(rule.end_time)} de ${day}, ${closer.name}">Remover</button></div>`) : SDR.html`<p class="muted dp-closed">Sem atendimento.</p>`}</div><button type="button" class="btn btn-ghost btn-sm" data-dp-action="show-rule" data-closer="${closer.id}" data-day="${weekday}">+ faixa</button><form class="stack dp-add-rule" data-dp-form="rule" data-closer="${closer.id}" data-day="${weekday}" hidden><div class="field"><label class="label" for="dp-start-${closer.id}-${weekday}">Início</label><input class="input mono" id="dp-start-${closer.id}-${weekday}" name="start" type="time" required></div><div class="field"><label class="label" for="dp-end-${closer.id}-${weekday}">Fim</label><input class="input mono" id="dp-end-${closer.id}-${weekday}" name="end" type="time" required></div><button class="btn btn-primary btn-sm" type="submit">Salvar faixa</button></form></section>`;
        })}</div></section>`) : SDR.html`<div class="card empty"><h3>Nenhum responsável ativo</h3><p>Cadastre ou ative um responsável na configuração do agente para definir os horários de atendimento.</p><button class="btn btn-ghost" type="button" data-dp-action="config">Abrir configuração</button></div>`}<p class="muted">O atalho de segunda replica apenas as faixas ausentes nos dias úteis. As faixas existentes são preservadas; horários sobrepostos precisam ser ajustados antes.</p></div>`;
      }
      function numberField(name, label, min, max, value) {
        return SDR.html`<div class="field"><label class="label" for="dp-${name}">${label}</label><input class="input mono" id="dp-${name}" name="${name}" type="number" min="${min}" max="${max}" step="1" required value="${value}"></div>`;
      }
      function parametersPanel() {
        const zones = Array.from(new Set(ZONES.concat(tz())));
        return SDR.html`<section class="card stack"><div class="dp-head"><div><h2>Parâmetros da agenda</h2><p class="muted">A duração, os intervalos e o fuso valem para toda a agenda.</p></div><span class="badge badge-amber mono">${settings.call_minutes} min / call</span></div><form class="stack" data-dp-form="parameters"><div class="dp-fields grid">${numberField('call_minutes', 'Duração da call (minutos)', 1, 1440, settings.call_minutes)}${numberField('buffer_minutes', 'Intervalo entre calls (minutos)', 0, 1440, settings.buffer_minutes)}${numberField('slot_step_minutes', 'Passo entre horários ofertados (minutos)', 1, 1440, settings.slot_step_minutes)}${numberField('min_notice_hours', 'Antecedência mínima (horas)', 0, 8760, settings.min_notice_hours)}${numberField('days_ahead', 'Dias disponíveis à frente', 1, 366, settings.days_ahead)}${numberField('public_max_per_hour', 'Página pública: máximo de agendamentos por hora', 1, 1000, settings.public_max_per_hour == null ? 20 : settings.public_max_per_hour)}${numberField('public_max_per_ip_day', 'Página pública: máximo por visitante por dia', 1, 100, settings.public_max_per_ip_day == null ? 3 : settings.public_max_per_ip_day)}${numberField('public_max_per_phone_day', 'Página pública: máximo de tentativas por WhatsApp por dia', 1, 100, settings.public_max_per_phone_day == null ? 3 : settings.public_max_per_phone_day)}<div class="field"><label class="label" for="dp-timezone">Fuso horário do negócio</label><select class="select mono" id="dp-timezone" name="timezone">${zones.map(zone => SDR.html`<option value="${zone}" ${selected(zone === tz())}>${zone}</option>`)}</select></div><div class="field"><label class="label" for="dp-distribution">Distribuição das calls</label><select class="select" id="dp-distribution" name="distribution" aria-describedby="dp-distribution-help"><option value="rodizio" ${selected(settings.distribution === 'rodizio')}>Rodízio</option><option value="peso" ${selected(settings.distribution === 'peso')}>Por peso</option></select><p class="muted" id="dp-distribution-help">Rodízio: equilibra o número de calls entre responsáveis. Por peso: distribui proporcionalmente ao peso definido para cada responsável.</p></div></div><div><button type="submit" class="btn btn-primary">Salvar parâmetros</button></div></form></section>`;
      }
      function blocksPanel() {
        const future = blocks.filter(row => new Date(row.ends_at).getTime() > Date.now());
        return SDR.html`<section class="card stack"><div class="dp-head"><div><h2>Bloqueios e feriados</h2><p class="muted">Os horários abaixo usam o fuso <span class="mono">${tz()}</span>. Bloqueios em andamento também aparecem.</p></div><span class="badge badge-muted mono">${future.length} bloqueios</span></div>${future.length ? SDR.html`<div class="stack">${future.map(block => SDR.html`<div class="dp-block row"><div><strong>${block.reason || 'Bloqueio sem motivo informado'}</strong><p class="muted">${closerName(block.closer_id)}</p><p class="mono">${SDR.fmt.dateTime(block.starts_at, tz())} até ${SDR.fmt.dateTime(block.ends_at, tz())}</p></div><button type="button" class="btn btn-ghost btn-sm" data-dp-action="delete-block" data-id="${block.id}">Remover</button></div>`)}</div>` : SDR.html`<div class="empty"><h3>Nenhum bloqueio futuro</h3><p>Use o formulário para reservar folgas, feriados ou períodos sem atendimento.</p></div>`}<form class="stack" data-dp-form="block"><h3>Adicionar bloqueio</h3><div class="dp-fields grid"><div class="field"><label class="label" for="dp-block-closer">Responsável</label><select class="select" id="dp-block-closer" name="closer">${closerOptions('', true)}</select></div><div class="field"><label class="label" for="dp-block-reason">Motivo</label><input class="input" id="dp-block-reason" name="reason" maxlength="500" placeholder="Ex.: feriado local"></div><div class="field"><label class="label" for="dp-block-start">Data e hora de início</label><input class="input mono" id="dp-block-start" name="start" type="datetime-local" required></div><div class="field"><label class="label" for="dp-block-end">Data e hora de fim</label><input class="input mono" id="dp-block-end" name="end" type="datetime-local" required></div></div><div><button type="submit" class="btn btn-primary">Adicionar bloqueio</button></div></form></section>`;
      }
      function publicURL() {
        const slug = String(settings.public_slug || '');
        if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug)) return '';
        return global.location.origin + '/agendar/' + slug;
      }
      function publicPanel() {
        const url = publicURL();
        return SDR.html`<section class="card stack"><div class="dp-head"><div><h2>Página pública de agendamento</h2><p class="muted">Compartilhe o link em sua bio, nos anúncios ou em uma landing page.</p></div><span class="badge ${settings.public_enabled ? 'badge-green' : 'badge-muted'}">${settings.public_enabled ? 'Página ativa' : 'Página desativada'}</span></div><form class="stack" data-dp-form="public"><label class="row dp-checkbox" for="dp-public-enabled"><input type="checkbox" id="dp-public-enabled" name="enabled" role="switch" ${checked(settings.public_enabled)}><span>Ativar página pública de agendamento</span></label><div class="field"><label class="label" for="dp-public-slug">Identificador do link</label><input class="input mono" id="dp-public-slug" name="slug" maxlength="100" pattern="[a-z0-9]+(-[a-z0-9]+)*" required value="${settings.public_slug || ''}" aria-describedby="dp-slug-help"><p class="muted" id="dp-slug-help">Use letras minúsculas sem acento, números e hífens entre palavras. O identificador deve ser único; essa verificação acontece ao salvar.</p></div><div><button type="submit" class="btn btn-primary">Salvar página pública</button></div></form>${url ? SDR.html`<div class="field"><label class="label" for="dp-public-link">Link salvo</label><div class="row dp-link"><input id="dp-public-link" class="input mono" readonly value="${url}"><button class="btn btn-ghost" type="button" data-dp-action="copy-link">Copiar link</button></div>${!settings.public_enabled ? SDR.html`<p class="muted">Ative e salve a página para disponibilizar horários neste link.</p>` : SDR.raw('')}</div>` : SDR.html`<div class="empty"><p>Salve um identificador válido para gerar o link de agendamento.</p></div>`}</section>`;
      }
      function draw() {
        if (!alive) return;
        root.setAttribute('aria-busy', 'false');
        SDR.mount(root, SDR.html`<div class="stack dp-view"><div class="tabs dp-tabs" role="tablist" aria-label="Configurações de disponibilidade">${TABS.map(([key, label]) => SDR.html`<button class="tab" type="button" id="dp-tab-${key}" role="tab" aria-selected="${key === activeTab}" aria-controls="dp-panel" tabindex="${key === activeTab ? 0 : -1}" data-dp-tab="${key}">${label}</button>`)}</div><div id="dp-notice" class="banner banner-error" role="alert" hidden></div><section class="dp-content" id="dp-panel" role="tabpanel" tabindex="0" aria-labelledby="dp-tab-${activeTab}">${activeTab === 'horarios' ? rulePanel() : activeTab === 'parametros' ? parametersPanel() : activeTab === 'bloqueios' ? blocksPanel() : publicPanel()}</section></div>`);
      }
      async function load() {
        const token = ++revision;
        loading();
        try {
          const data = await Promise.all([ctx.api.listRules(), ctx.api.listBlocks()]);
          if (!alive || token !== revision) return;
          rules = data[0] || []; blocks = data[1] || [];
          settings = Object.assign({}, ctx.state.settings);
          retryAction = null;
          draw();
        } catch (error) {
          if (!alive || token !== revision) return;
          root.setAttribute('aria-busy', 'false');
          SDR.mount(root, SDR.html`<div class="banner banner-error" role="alert"><p>${message(error)}</p><button type="button" class="btn btn-ghost" data-dp-action="load">Tentar de novo</button></div>`);
        }
      }
      async function run(action, retry) {
        if (!alive || busy) return;
        busy = true;
        notice(null);
        root.setAttribute('aria-busy', 'true');
        const controls = SDR.qsa('button,input,select', root).map(el => [el, el.disabled]);
        controls.forEach(([el]) => { el.disabled = true; });
        try {
          await action();
        } catch (error) {
          const refreshRetry = error && error.dpRefreshAction ? () => run(() => refreshAfterWrite(error.dpRefreshAction, error.dpRefreshAction)) : () => load();
          notice(error, error && error.dpRefreshOnly ? refreshRetry : retry);
        } finally {
          busy = false;
          if (alive) {
            root.setAttribute('aria-busy', 'false');
            controls.forEach(([el, disabled]) => { if (el.isConnected) el.disabled = disabled; });
          }
        }
      }
      async function reloadRules() {
        const rows = await ctx.api.listRules();
        if (alive) rules = rows || [];
        return rows || [];
      }
      async function refreshAfterWrite(action, retry) {
        try { return await action(); }
        catch (error) {
          const failure = new Error('A alteração foi salva, mas a tela não pôde ser atualizada. ' + message(error));
          failure.dpRefreshOnly = true;
          failure.dpRefreshAction = retry || null;
          throw failure;
        }
      }
      async function refreshSettings() {
        await ctx.refreshState();
        if (alive) { settings = Object.assign({}, ctx.state.settings); draw(); }
      }
      async function copyMonday(closerId) {
        const current = await reloadRules();
        if (!alive) return;
        const monday = current.filter(row => row.closer_id === closerId && +row.weekday === 1);
        if (!monday.length) throw new Error('Cadastre pelo menos uma faixa na segunda-feira antes de copiar.');
        const additions = [];
        for (let day = 2; day <= 5; day++) {
          monday.forEach(source => {
            const next = {closer_id: closerId, weekday: day, start_time: source.start_time, end_time: source.end_time};
            const existing = current.concat(additions).filter(row => row.closer_id === closerId && +row.weekday === day);
            if (existing.some(row => sameSpan(row, next))) return;
            try { validateSpan(next, current.concat(additions)); }
            catch (_) { throw new Error('Há uma faixa sobreposta em ' + DAYS[day] + '. Ajuste esse dia antes de copiar a segunda-feira.'); }
            additions.push(next);
          });
        }
        if (!additions.length) { ctx.toast('As faixas de segunda já estão nos dias úteis.', 'info'); return; }
        let saved = 0;
        try {
          for (const rule of additions) {
            if (!alive) return;
            await ctx.api.saveRule(rule); saved++;
          }
        } catch (error) {
          let refreshed = true;
          try { await reloadRules(); } catch (_) { refreshed = false; }
          if (alive) draw();
          throw new Error(message(error) + (saved ? ' ' + saved + ' faixa(s) já foram salvas e serão preservadas.' : '') + (refreshed ? ' Tente de novo para copiar somente as faixas que faltam.' : ' Atualize a tela antes de continuar.'));
        }
        await refreshAfterWrite(reloadRules);
        if (alive) { draw(); ctx.toast('Faixas de segunda copiadas para os dias úteis.', 'ok'); }
      }
      function value(form, name) { const field = form.elements.namedItem(name); return field ? field.value : ''; }
      async function submit(form) {
        const type = form.dataset.dpForm;
        const again = () => { if (form.isConnected) form.requestSubmit(); };
        if (type === 'rule') {
          const rule = {closer_id: form.dataset.closer, weekday: Number(form.dataset.day), start_time: value(form, 'start'), end_time: value(form, 'end')};
          await run(async () => {
            const fresh = await reloadRules();
            if (!alive) return;
            validateSpan(rule, fresh);
            await ctx.api.saveRule(rule);
            await refreshAfterWrite(reloadRules);
            if (alive) { draw(); ctx.toast('Faixa de atendimento salva.', 'ok'); }
          }, again);
        } else if (type === 'parameters') {
          const patch = {timezone: value(form, 'timezone'), distribution: value(form, 'distribution')};
          const bounds = {call_minutes: [1, 1440], buffer_minutes: [0, 1440], slot_step_minutes: [1, 1440], min_notice_hours: [0, 8760], days_ahead: [1, 366], public_max_per_hour: [1, 1000], public_max_per_ip_day: [1, 100], public_max_per_phone_day: [1, 100]};
          Object.keys(bounds).forEach(key => { patch[key] = Number(value(form, key)); });
          await run(async () => {
            Object.entries(bounds).forEach(([key, range]) => { if (!Number.isInteger(patch[key]) || patch[key] < range[0] || patch[key] > range[1]) throw new Error('Confira os valores numéricos da agenda. Use números inteiros dentro dos limites indicados.'); });
            try { new Intl.DateTimeFormat('pt-BR', {timeZone: patch.timezone}); } catch (_) { throw new Error('Escolha um fuso horário válido.'); }
            if (!['rodizio', 'peso'].includes(patch.distribution)) throw new Error('Escolha uma distribuição válida.');
            await ctx.api.saveScheduleSettings(patch);
            await refreshAfterWrite(refreshSettings, refreshSettings);
            if (alive) ctx.toast('Parâmetros da agenda salvos.', 'ok');
          }, again);
        } else if (type === 'block') {
          const start = value(form, 'start'), end = value(form, 'end');
          const patch = {closer_id: value(form, 'closer') || null, reason: value(form, 'reason').trim() || null};
          await run(async () => {
            patch.starts_at = wallToISO(start, tz()); patch.ends_at = wallToISO(end, tz());
            if (new Date(patch.starts_at) >= new Date(patch.ends_at)) throw new Error('O fim do bloqueio deve ser depois do início.');
            await ctx.api.saveBlock(patch);
            const rows = await refreshAfterWrite(() => ctx.api.listBlocks());
            if (alive) { blocks = rows || []; draw(); ctx.toast('Bloqueio adicionado.', 'ok'); }
          }, again);
        } else if (type === 'public') {
          const slug = value(form, 'slug').trim(), enabled = form.elements.namedItem('enabled').checked;
          await run(async () => {
            if (!/^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(slug) || slug.length > 100) throw new Error('Use letras minúsculas sem acento, números e hífens entre palavras no identificador.');
            await ctx.api.saveScheduleSettings({public_slug: slug, public_enabled: enabled});
            await refreshAfterWrite(refreshSettings, refreshSettings);
            if (alive) ctx.toast('Página pública salva.', 'ok');
          }, again);
        }
      }
      async function onClick(event) {
        const button = event.target.closest('button');
        if (!button || !root.contains(button) || button.disabled || busy) return;
        if (button.dataset.dpTab) {
          activeTab = button.dataset.dpTab; draw();
          const tab = q('#dp-tab-' + activeTab); if (tab) tab.focus();
          return;
        }
        const action = button.dataset.dpAction;
        if (action === 'load') { await load(); return; }
        if (action === 'retry-operation') { if (retryAction) await retryAction(); return; }
        if (action === 'config') { ctx.navigate('config'); return; }
        if (action === 'show-rule') {
          const form = button.parentElement.querySelector('form');
          form.hidden = !form.hidden;
          button.setAttribute('aria-expanded', String(!form.hidden));
          if (!form.hidden) form.elements.namedItem('start').focus();
          return;
        }
        if (action === 'copy-monday') {
          const id = button.dataset.closer;
          await run(() => copyMonday(id), () => run(() => copyMonday(id), () => load()));
        } else if (action === 'delete-rule' || action === 'delete-block') {
          const id = button.dataset.id;
          await run(async () => {
            const yes = await ctx.confirm({title: action === 'delete-rule' ? 'Remover faixa de atendimento?' : 'Remover bloqueio?', message: action === 'delete-rule' ? 'Novas calls deixarão de ser ofertadas nesta faixa. Agendamentos existentes serão preservados.' : 'O período voltará a aceitar calls quando houver uma faixa de atendimento disponível.', confirmLabel: 'Remover', danger: true});
            if (!yes || !alive) return;
            if (action === 'delete-rule') { await ctx.api.deleteRule(id); await refreshAfterWrite(reloadRules); }
            else { await ctx.api.deleteBlock(id); const rows = await refreshAfterWrite(() => ctx.api.listBlocks()); if (alive) blocks = rows || []; }
            if (alive) { draw(); ctx.toast(action === 'delete-rule' ? 'Faixa removida.' : 'Bloqueio removido.', 'ok'); }
          }, () => { if (button.isConnected) button.click(); });
        } else if (action === 'copy-link') {
          const link = q('#dp-public-link');
          if (!link) return;
          try {
            if (!global.navigator.clipboard || !global.navigator.clipboard.writeText) throw new Error('clipboard');
            await global.navigator.clipboard.writeText(link.value);
            if (alive) ctx.toast('Link copiado.', 'ok');
          } catch (_) {
            if (!alive) return;
            link.focus(); link.select();
            ctx.toast('Link selecionado. Use Ctrl+C ou Command+C para copiar; no celular, toque e segure o campo.', 'info');
          }
        }
      }
      function onSubmit(event) {
        const form = event.target.closest('form[data-dp-form]');
        if (!form || !root.contains(form)) return;
        event.preventDefault();
        if (busy || !alive || !form.reportValidity()) return;
        submit(form).catch(error => notice(error, () => form.requestSubmit()));
      }
      function onKeyDown(event) {
        const tab = event.target.closest('[data-dp-tab]');
        if (!tab || busy || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
        event.preventDefault();
        const current = TABS.findIndex(([key]) => key === activeTab);
        const next = event.key === 'Home' ? 0 : event.key === 'End' ? TABS.length - 1 : (current + (event.key === 'ArrowRight' ? 1 : -1) + TABS.length) % TABS.length;
        activeTab = TABS[next][0]; draw(); q('#dp-tab-' + activeTab).focus();
      }
      root.addEventListener('click', onClick);
      root.addEventListener('submit', onSubmit);
      root.addEventListener('keydown', onKeyDown);
      // Retorna o cleanup imediatamente; a navegação pode cancelar o carregamento pendente.
      load();
      return function () {
        alive = false; revision++; retryAction = null;
        root.removeEventListener('click', onClick);
        root.removeEventListener('submit', onSubmit);
        root.removeEventListener('keydown', onKeyDown);
      };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
