(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  SDR.views = SDR.views || {};
  SDR.views.visao = {
    title: 'Visão geral',
    async render(root, ctx) {
      let disposed = false, revision = 0;
      const tz = ctx.state.tz || SDR.tz, today = SDR.tzTools.startOfDay(new Date(), tz);
      const dateKey = value => { const p = SDR.tzTools.parts(value, tz); return [p.y, String(p.m).padStart(2, '0'), String(p.d).padStart(2, '0')].join('-'); };
      const parseDay = value => {
        if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error('Informe as duas datas do período.');
        const [y, m, d] = value.split('-').map(Number), day = SDR.tzTools.startOfDay(SDR.tzTools.zonedToUtc(y, m, d, 12, 0, tz), tz);
        if (dateKey(day) !== value) throw new Error('Informe uma data válida.');
        return day;
      };
      SDR.mount(root, SDR.html`<div class="stack vg-view"><div id="vg-wa-alert" aria-live="polite"></div><form class="card vg-filters" id="vg-filters"><div class="field"><label class="label" for="vg-period">Período</label><select class="select" id="vg-period"><option value="today">Hoje</option><option value="7">7 dias</option><option value="30" selected>30 dias</option><option value="month">Este mês</option><option value="custom">Personalizado</option></select></div><div class="field"><label class="label" for="vg-closer">Responsável</label><select class="select" id="vg-closer"><option value="">Todos os responsáveis</option>${ctx.state.closers.map(c => SDR.html`<option value="${c.id}">${c.name}${c.active ? '' : ' (inativo)'}</option>`)}</select></div><div id="vg-custom" class="vg-dates" hidden><div class="field"><label class="label" for="vg-from">De</label><input type="date" class="input mono" id="vg-from" value="${dateKey(SDR.tzTools.addDays(today, -29, tz))}"></div><div class="field"><label class="label" for="vg-to">Até</label><input type="date" class="input mono" id="vg-to" value="${dateKey(today)}"></div></div><button class="btn btn-primary" type="submit">Atualizar</button></form><div id="vg-results" class="stack" aria-live="polite"></div></div>`);
      const results = SDR.qs('#vg-results', root), period = SDR.qs('#vg-period', root), closer = SDR.qs('#vg-closer', root), form = SDR.qs('#vg-filters', root);
      function range() {
        const end = SDR.tzTools.startOfDay(SDR.tzTools.addDays(today, 1, tz), tz);
        if (period.value === 'custom') {
          const from = parseDay(SDR.qs('#vg-from', root).value), last = parseDay(SDR.qs('#vg-to', root).value);
          if (from > last) throw new Error('A data inicial deve ser anterior ou igual à data final.');
          return {from, to: SDR.tzTools.startOfDay(SDR.tzTools.addDays(last, 1, tz), tz)};
        }
        if (period.value === 'month') { const p = SDR.tzTools.parts(today, tz); return {from: SDR.tzTools.startOfDay(SDR.tzTools.zonedToUtc(p.y, p.m, 1, 12, 0, tz), tz), to: end}; }
        return {from: period.value === 'today' ? today : SDR.tzTools.addDays(today, 1 - Number(period.value), tz), to: end};
      }
      const count = value => Math.max(0, Number(value) || 0);
      function kpi(label, value, detail, index) {
        return SDR.html`<article class="card kpi vg-kpi"><span class="vg-kpi-index mono" aria-hidden="true">${String(index + 1).padStart(2, '0')}</span><span class="kpi-label">${label}</span><strong class="kpi-value">${value}</strong><small class="muted">${detail}</small></article>`;
      }
      function draw(stats, appointments, selected) {
        const steps = [['leads','Leads'], ['respondeu','Responderam'], ['qualificados','Qualificados'], ['agendados','Agendados'], ['comparecimentos','Comparecimentos'], ['vendas','Vendas']];
        const max = Math.max(1, ...steps.map(([key]) => count(stats[key])));
        const meaningful = steps.some(([key]) => count(stats[key]) > 0);
        const funnel = Array.isArray(stats.funil) ? stats.funil : [];
        const upcoming = appointments.filter(a => ['agendado', 'confirmado'].includes(a.status) && new Date(a.starts_at) >= new Date()).sort((a, b) => new Date(a.starts_at) - new Date(b.starts_at));
        SDR.mount(results, SDR.html`<div class="vg-range muted">Período: <span class="mono">${SDR.fmt.date(selected.from, tz)} – ${SDR.fmt.date(SDR.tzTools.addDays(selected.to, -1, tz), tz)}</span></div><div class="vg-kpis">${[
          ['Leads', SDR.fmt.int(stats.leads), 'Novos contatos no período'],
          ['Taxa de qualificação', SDR.fmt.pct(stats.taxa_qualificacao), 'Qualificados / novos leads'],
          ['Taxa de agendamento', SDR.fmt.pct(stats.taxa_agendamento), 'Agendados / qualificados'],
          ['Comparecimento', SDR.fmt.pct(stats.taxa_comparecimento), 'Presenças / presenças e no-shows'],
          ['Vendas', SDR.fmt.int(stats.vendas), 'Conversas com resultado de venda'],
          ['Valor em vendas', SDR.fmt.money(stats.valor_vendas), 'Valor registrado na agenda']
        ].map((args, i) => kpi(...args, i))}</div>${!meaningful ? SDR.html`<div class="empty card"><svg width="48" height="48" viewBox="0 0 48 48" fill="none" stroke="var(--primary-light)" stroke-width="2" aria-hidden="true"><path d="M8 38V24h7v14m6 0V16h7v22m6 0V8h7v30M5 42h38"/></svg><h2>O próximo passo começa com uma conversa.</h2><p>Assim que o agente começar a conversar, os números aparecem aqui.</p><p>Você também pode escolher outro período nos filtros.</p></div>` : SDR.raw('')}<div class="vg-columns"><article class="card stack"><div class="vg-section-head row"><div><span class="pn-eyebrow">DA CONVERSA AO RESULTADO</span><h2>Funil de conversão</h2></div><span class="badge badge-amber">${steps.length} etapas</span></div><div class="vg-funnel">${steps.map(([key, label], i) => {
          const previous = i ? count(stats[steps[i - 1][0]]) : 0;
          const rate = i ? previous ? SDR.fmt.pct(count(stats[key]) / previous) + ' da etapa anterior' : 'Sem base na etapa anterior' : 'Entrada do período';
          const width = Math.min(100, Math.max(0, count(stats[key]) / max * 100));
          return SDR.html`<div class="vg-step"><div class="row vg-step-label"><span>${label}</span><strong class="mono">${SDR.fmt.int(stats[key])}</strong></div><div class="vg-track"><div class="vg-bar" style="width:${width}%"></div></div><span class="muted vg-rate">${rate}</span></div>`;
        })}</div><p class="muted vg-note">Contagens de leads, eventos e agendamentos usam suas respectivas datas no período. As taxas entre etapas podem superar 100% quando há contatos de períodos anteriores.</p></article><article class="card stack"><div class="vg-section-head row"><div><span class="pn-eyebrow">PRÓXIMOS 7 DIAS</span><h2>Próximas calls</h2></div><button class="btn btn-ghost btn-sm" type="button" id="vg-agenda">Ver agenda</button></div>${upcoming.length ? SDR.html`<div class="vg-upcoming">${upcoming.map(a => SDR.html`<div class="vg-call"><div class="vg-call-time mono"><strong>${SDR.fmt.time(a.starts_at, tz)}</strong><small>${SDR.fmt.dayLabel(a.starts_at, tz)}</small></div><div class="vg-call-info"><strong>${a.lead && a.lead.name || 'Contato sem nome'}</strong><small class="muted">${a.closer && a.closer.name || 'Responsável não informado'}</small>${SDR.statusBadge(a.status)}</div></div>`)}</div>` : SDR.html`<div class="empty"><h3>Sua agenda tem espaço.</h3><p>Nenhuma conversa agendada para os próximos sete dias com este filtro.</p></div>`}</article></div><article class="card stack"><div class="vg-section-head"><span class="pn-eyebrow">TODOS OS ESTÁGIOS</span><h2>Movimentação do funil</h2></div>${funnel.length ? SDR.html`<div class="vg-stages">${funnel.map(item => SDR.html`<div class="vg-stage">${SDR.stageBadge(item.stage)}<strong class="mono">${SDR.fmt.int(item.count)}</strong></div>`)}</div>` : SDR.html`<div class="empty">Ainda não há movimentações de estágio neste período.</div>`}</article>`);
        SDR.qs('#vg-agenda', results).addEventListener('click', () => ctx.navigate('agenda'));
      }
      async function load() {
        const current = ++revision;
        results.setAttribute('aria-busy', 'true');
        SDR.mount(results, SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`);
        try {
          const selected = range(), now = new Date(), until = SDR.tzTools.addDays(now, 7, tz);
          const [stats, appointments] = await Promise.all([ctx.api.dashboardStats(selected.from.toISOString(), selected.to.toISOString(), closer.value || undefined), ctx.api.listAppointments(now.toISOString(), until.toISOString(), closer.value || undefined)]);
          if (disposed || current !== revision) return;
          draw(stats || {}, appointments || [], selected);
        } catch (error) {
          if (disposed || current !== revision) return;
          SDR.mount(results, SDR.html`<div class="banner banner-error" role="alert"><p>${error.message || 'Não foi possível carregar os indicadores.'}</p><button type="button" class="btn btn-ghost" id="vg-retry">Tentar de novo</button></div>`);
          SDR.qs('#vg-retry', results).addEventListener('click', load);
        } finally { if (!disposed && current === revision) results.removeAttribute('aria-busy'); }
      }
      // Aviso de WhatsApp desconectado: consulta extra, silenciosa; falha ou tabela ausente não afeta a visão.
      async function loadWhatsappAlert() {
        const box = SDR.qs('#vg-wa-alert', root);
        if (!box || !ctx.api || typeof ctx.api.getWhatsappConnection !== 'function') return;
        try {
          const wa = await ctx.api.getWhatsappConnection();
          if (disposed) return;
          const down = wa && wa.state === 'close' && typeof SDR.whatsappMotorAlive === 'function' && SDR.whatsappMotorAlive(wa);
          SDR.mount(box, down ? SDR.html`<div class="banner banner-error vg-wa-alert" role="alert"><strong>O WhatsApp do atendimento está desconectado.</strong> <a href="#/whatsapp">Reconectar</a></div>` : SDR.raw(''));
        } catch (_) { /* sem aviso quando não foi possível conferir */ }
      }
      function submit(event) { event.preventDefault(); load(); }
      function changePeriod() { SDR.qs('#vg-custom', root).hidden = period.value !== 'custom'; if (period.value !== 'custom') load(); }
      form.addEventListener('submit', submit); period.addEventListener('change', changePeriod); closer.addEventListener('change', load);
      load(); loadWhatsappAlert();
      return () => { disposed = true; revision++; form.removeEventListener('submit', submit); period.removeEventListener('change', changePeriod); closer.removeEventListener('change', load); };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
