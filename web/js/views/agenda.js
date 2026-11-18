(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  SDR.views = SDR.views || {};
  const stamp = value => new Date(value).getTime();
  const button = (label, onClick, kind) => SDR.el('button', {type: 'button', class: 'btn btn-' + (kind || 'ghost'), onClick}, label);
  function dateKey(date, tz) {
    const p = SDR.tzTools.parts(date, tz);
    return p.y + '-' + String(p.m).padStart(2, '0') + '-' + String(p.d).padStart(2, '0');
  }
  function wallDay(value, tz) {
    const bits = String(value).split('-').map(Number);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error('Escolha uma data válida.');
    const day = SDR.tzTools.zonedToUtc(bits[0], bits[1], bits[2], 0, 0, tz);
    if (dateKey(day, tz) !== value) throw new Error('Escolha uma data válida.');
    return day;
  }
  function colorToken(color) {
    const key = String(color || '').toLowerCase();
    if (key === '#818cf8') return 'var(--indigo)';
    if (key === '#9ca3af' || key === '#6b7280') return 'var(--text-secondary)';
    return 'var(--primary-light)';
  }
  function loading(target) { SDR.mount(target, SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`); }
  function failure(target, error, retry) {
    SDR.mount(target, SDR.html`<div class="banner banner-error" role="alert"><p>${error.message || 'Não foi possível carregar. Tente de novo.'}</p></div>`);
    target.firstElementChild.appendChild(button('Tentar de novo', retry));
  }
  function labeled(label, control) {
    const id = 'ag-' + SDR.uuid(); control.id = id;
    return SDR.el('div', {class: 'field'}, SDR.el('label', {class: 'label', for: id}, label), control);
  }
  function closerSelect(closers, selected, allText) {
    const select = SDR.el('select', {class: 'select'}, SDR.el('option', {value: ''}, allText || 'Todos os responsáveis'), closers.map(c => SDR.el('option', {value: c.id}, c.name)));
    select.value = selected || ''; return select;
  }
  SDR.views.agenda = {
    title: 'Agenda',
    async render(root, ctx) {
      let disposed = false, revision = 0, view = 'semana', anchor = new Date(), closerId = '';
      let appointments = [], rules = [], blocks = [];
      const ownedModals = new Set();
      const tz = () => ctx.state.tz || SDR.tz;
      const closers = () => ctx.state.closers || [];
      const findCloser = id => closers().find(c => c.id === id);
      function modal(options) {
        let instance;
        const original = options.onClose;
        instance = ctx.modal(Object.assign({}, options, {onClose() { ownedModals.delete(instance); if (original) original(); }}));
        ownedModals.add(instance); return instance;
      }
      const toolbar = SDR.el('div', {class: 'ag-toolbar row'});
      const content = SDR.el('div', {class: 'ag-content'});
      const period = SDR.el('strong', {class: 'ag-period mono', 'aria-live': 'polite'});
      const tabs = SDR.el('div', {class: 'tabs', role: 'group', 'aria-label': 'Exibição da agenda'});
      ['semana', 'dia'].forEach(key => tabs.appendChild(SDR.el('button', {type: 'button', class: 'tab', 'aria-pressed': key === view, dataset: {mode: key}, onClick() { view = key; load(); }}, key === 'semana' ? 'Semana' : 'Dia')));
      const picker = closerSelect(closers(), '');
      picker.addEventListener('change', () => { closerId = picker.value; load(); });
      toolbar.append(tabs, button('Anterior', () => { anchor = SDR.tzTools.addDays(anchor, view === 'semana' ? -7 : -1, tz()); load(); }), button('Hoje', () => { anchor = new Date(); load(); }), button('Próximo', () => { anchor = SDR.tzTools.addDays(anchor, view === 'semana' ? 7 : 1, tz()); load(); }), labeled('Responsável', picker), button('+ Agendar manualmente', () => openBooking(null), 'primary'));
      const legend = SDR.el('div', {class: 'ag-closers row', 'aria-label': 'Cores dos responsáveis'}, closers().map(c => {
        const dot = SDR.el('span', {class: 'ag-dot', 'aria-hidden': 'true'}); dot.style.background = colorToken(c.color);
        return SDR.el('span', {class: 'ag-closer'}, dot, c.name);
      }));
      root.append(SDR.el('div', {class: 'stack'}, toolbar, SDR.el('div', {class: 'row'}, period, SDR.el('span', {class: 'muted'}, 'Fuso: ' + tz())), legend, content));
      function range() {
        const from = view === 'semana' ? SDR.tzTools.startOfWeek(anchor, tz()) : SDR.tzTools.startOfDay(anchor, tz());
        return {from, to: SDR.tzTools.addDays(from, view === 'semana' ? 7 : 1, tz())};
      }
      async function load() {
        const token = ++revision, current = range();
        period.textContent = view === 'semana' ? SDR.fmt.date(current.from, tz()) + ' — ' + SDR.fmt.date(SDR.tzTools.addDays(current.to, -1, tz()), tz()) : SDR.fmt.date(current.from, tz());
        SDR.qsa('[data-mode]', tabs).forEach(tab => { tab.setAttribute('aria-pressed', String(tab.dataset.mode === view)); tab.classList.toggle('active', tab.dataset.mode === view); });
        loading(content); content.setAttribute('aria-busy', 'true');
        try {
          const rows = await Promise.all([ctx.api.listAppointments(current.from.toISOString(), current.to.toISOString(), closerId || undefined), ctx.api.listRules(closerId || undefined), ctx.api.listBlocks()]);
          if (disposed || token !== revision) return;
          appointments = rows[0] || []; rules = rows[1] || [];
          blocks = (rows[2] || []).filter(b => (!closerId || !b.closer_id || b.closer_id === closerId) && stamp(b.starts_at) < +current.to && stamp(b.ends_at) > +current.from);
          draw(current);
        } catch (error) { if (!disposed && token === revision) failure(content, error, load); }
        finally { if (!disposed && token === revision) content.setAttribute('aria-busy', 'false'); }
      }
      function eventButton(row, className) {
        const c = row.closer || findCloser(row.closer_id), lead = row.lead;
        const item = SDR.el('button', {type: 'button', class: className, onClick: () => detail(row), 'aria-label': (lead && lead.name || 'Contato sem nome') + ', ' + SDR.fmt.dateTime(row.starts_at, tz())});
        item.style.setProperty('--ag-color', colorToken(c && c.color));
        SDR.mount(item, SDR.html`<span class="mono">${SDR.fmt.time(row.starts_at, tz())}–${SDR.fmt.time(row.ends_at, tz())}</span><strong>${lead && lead.name || 'Contato sem nome'}</strong><span class="ag-event-closer">${c && c.name || 'Responsável não informado'}</span>${SDR.statusBadge(row.status)}`);
        return item;
      }
      function dayRows(day) {
        const end = SDR.tzTools.addDays(day, 1, tz());
        return {appointments: appointments.filter(a => stamp(a.starts_at) >= +day && stamp(a.starts_at) < +end), blocks: blocks.filter(b => stamp(b.starts_at) < +end && stamp(b.ends_at) > +day)};
      }
      function listDay(day) {
        const entries = dayRows(day), list = SDR.el('section', {class: 'ag-day-list'}, SDR.el('h3', {}, SDR.fmt.dayLabel(day, tz())));
        const combined = entries.appointments.map(row => ({row, block: false})).concat(entries.blocks.map(row => ({row, block: true}))).sort((a, b) => stamp(a.row.starts_at) - stamp(b.row.starts_at));
        if (!combined.length) list.appendChild(SDR.el('p', {class: 'empty'}, 'Nenhum agendamento ou bloqueio neste dia.'));
        combined.forEach(({row, block}) => {
          if (!block) list.appendChild(eventButton(row, 'ag-list-item'));
          else {
            const c = findCloser(row.closer_id);
            list.appendChild(SDR.el('div', {class: 'ag-list-block'}, SDR.el('strong', {}, 'Bloqueio · ' + (row.reason || 'Indisponível')), SDR.el('span', {class: 'mono'}, SDR.fmt.dateTime(row.starts_at, tz()) + ' — ' + SDR.fmt.dateTime(row.ends_at, tz())), SDR.el('small', {class: 'muted'}, c ? c.name : 'Todos os responsáveis')));
          }
        }); return list;
      }
      function minuteAt(value, day) {
        if (stamp(value) <= +day) return 0;
        if (stamp(value) >= +SDR.tzTools.addDays(day, 1, tz())) return 1440;
        const p = SDR.tzTools.parts(value, tz()); return p.hh * 60 + p.mm;
      }
      function draw(current) {
        SDR.mount(content, SDR.raw(''));
        if (!appointments.length && !blocks.length) content.appendChild(SDR.el('div', {class: 'empty'}, SDR.el('strong', {}, 'Sua agenda está livre neste período.'), SDR.el('p', {}, 'Os agendamentos aparecerão aqui. Você também pode reservar um horário manualmente.')));
        const days = Array.from({length: view === 'semana' ? 7 : 1}, (_, i) => SDR.tzTools.addDays(current.from, i, tz()));
        if (view === 'dia') { content.appendChild(listDay(days[0])); return; }
        const bounds = rules.flatMap(r => [Number(r.start_time.slice(0, 2)) * 60 + Number(r.start_time.slice(3, 5)), Number(r.end_time.slice(0, 2)) * 60 + Number(r.end_time.slice(3, 5))]);
        appointments.forEach(a => { const p = SDR.tzTools.parts(a.starts_at, tz()), q = SDR.tzTools.parts(a.ends_at, tz()); bounds.push(p.hh * 60 + p.mm, q.hh * 60 + q.mm); });
        const start = Math.max(0, Math.floor((bounds.length ? Math.min(...bounds) : 480) / 60) * 60), end = Math.min(1440, Math.max(start + 60, Math.ceil((bounds.length ? Math.max(...bounds) : 1200) / 60) * 60));
        const span = end - start, height = span * 1.65;
        const grid = SDR.el('div', {class: 'ag-week', 'aria-label': 'Agenda semanal'});
        grid.appendChild(SDR.el('div', {class: 'ag-corner', 'aria-hidden': 'true'}, 'Hora'));
        days.forEach(day => grid.appendChild(SDR.el('div', {class: 'ag-week-head'}, SDR.fmt.dayLabel(day, tz()))));
        const axis = SDR.el('div', {class: 'ag-hours', 'aria-hidden': 'true'}); axis.style.height = height + 'px';
        for (let m = start; m <= end; m += 60) { const label = SDR.el('span', {class: 'ag-hour-label mono'}, String(Math.floor(m / 60)).padStart(2, '0') + ':00'); label.style.top = ((m - start) / span * 100) + '%'; axis.appendChild(label); }
        grid.appendChild(axis);
        days.forEach(day => {
          const column = SDR.el('div', {class: 'ag-day-column'}); column.style.height = height + 'px';
          for (let m = start; m < end; m += 60) { const line = SDR.el('div', {class: 'ag-hour-line', 'aria-hidden': 'true'}); line.style.top = ((m - start) / span * 100) + '%'; column.appendChild(line); }
          const rows = dayRows(day);
          rows.blocks.forEach(b => {
            const lo = Math.max(start, minuteAt(b.starts_at, day)), hi = Math.min(end, minuteAt(b.ends_at, day));
            if (hi <= lo) return;
            const c = findCloser(b.closer_id), item = SDR.el('div', {class: 'ag-block', title: (b.reason || 'Indisponível') + ' · ' + (c ? c.name : 'Todos os responsáveis')}, SDR.el('span', {}, 'Bloqueio'), SDR.el('small', {}, b.reason || 'Indisponível'));
            item.style.top = ((lo - start) / span * 100) + '%'; item.style.height = ((hi - lo) / span * 100) + '%'; column.appendChild(item);
          });
          // Cada grupo de intervalos que se cruzam recebe colunas independentes.
          const events = rows.appointments.map(row => ({row, lo: Math.max(start, minuteAt(row.starts_at, day)), hi: Math.min(end, minuteAt(row.ends_at, day))})).filter(e => e.hi > e.lo).sort((a, b) => a.lo - b.lo || a.hi - b.hi);
          const groups = []; let group = [], groupEnd = -1;
          events.forEach(event => { if (event.lo >= groupEnd && group.length) { groups.push(group); group = []; groupEnd = -1; } group.push(event); groupEnd = Math.max(groupEnd, event.hi); });
          if (group.length) groups.push(group);
          groups.forEach(items => {
            const lanes = [];
            items.forEach(item => { let lane = lanes.findIndex(until => until <= item.lo); if (lane < 0) lane = lanes.length; lanes[lane] = item.hi; item.lane = lane; });
            items.forEach(item => { const element = eventButton(item.row, 'ag-event'); element.style.top = ((item.lo - start) / span * 100) + '%'; element.style.height = ((item.hi - item.lo) / span * 100) + '%'; element.style.left = (item.lane / lanes.length * 100) + '%'; element.style.width = (100 / lanes.length) + '%'; column.appendChild(element); });
          }); grid.appendChild(column);
        });
        content.append(grid, SDR.el('div', {class: 'ag-mobile-list stack'}, days.map(listDay)));
      }
      function detail(row) {
        let busy = false, closed = false;
        const body = SDR.el('div', {class: 'stack'}), errors = SDR.el('div', {class: 'ag-modal-error', role: 'alert'}), actions = SDR.el('div', {class: 'ag-detail-actions row'});
        const c = row.closer || findCloser(row.closer_id), lead = row.lead || {};
        SDR.mount(body, SDR.html`<div class="ag-detail-grid"><div><span class="muted">Contato</span><strong>${lead.name || 'Contato sem nome'}</strong><span>${SDR.fmt.phone(lead.phone)}</span></div><div><span class="muted">Responsável</span><strong>${c && c.name || 'Não informado'}</strong></div><div><span class="muted">Data e horário</span><span class="mono">${SDR.fmt.dateTime(row.starts_at, tz())}–${SDR.fmt.time(row.ends_at, tz())}</span></div><div><span class="muted">Origem</span><span>${({agente: 'Agente', pagina: 'Página pública', manual: 'Manual'})[row.source] || 'Não informada'}</span></div><div><span class="muted">Status atual</span>${SDR.statusBadge(row.status)}</div>${row.sale_value ? SDR.html`<div><span class="muted">Valor da venda</span><span class="mono">${SDR.fmt.money(row.sale_value)}</span></div>` : SDR.raw('')}</div><div><span class="muted">Notas</span><p class="ag-notes">${row.notes || 'Nenhuma nota registrada.'}</p></div>`);
        const sale = SDR.el('div', {class: 'field', hidden: true});
        const value = SDR.el('input', {type: 'number', class: 'input mono', min: '.01', step: '.01', inputmode: 'decimal', value: row.sale_value || ''});
        sale.append(labeled('Valor da venda (R$)', value), button('Salvar venda', () => {
          const amount = Number(value.value);
          if (!Number.isFinite(amount) || amount <= 0) { showError(new Error('Informe um valor de venda maior que zero.')); value.focus(); return; }
          mutate('venda', {sale_value: amount});
        }, 'primary'));
        body.append(button('Ver lead', () => { if (!busy) ctx.openLead(row.lead_id); }), errors, actions, sale);
        const dialog = modal({title: 'Detalhes do agendamento', body, actions: [], wide: true, onClose() { closed = true; }});
        function showError(error) {
          if (closed) return;
          SDR.mount(errors, SDR.html`<div class="banner banner-error">${error.message || 'Não foi possível atualizar. Tente de novo.'}</div>`);
        }
        async function mutate(status, extra) {
          if (busy || closed || disposed) return;
          busy = true; SDR.mount(errors, SDR.raw('')); SDR.qsa('button', body).forEach(b => { b.disabled = true; });
          try { await ctx.api.setAppointmentStatus(row.id, status, extra); if (disposed) return; ctx.toast('Agendamento atualizado.', 'ok'); dialog.close(); await load(); }
          catch (error) { showError(error); errors.appendChild(button('Tentar de novo', () => mutate(status, extra))); }
          finally { busy = false; if (!closed) SDR.qsa('button', body).forEach(b => { b.disabled = false; }); }
        }
        [['Confirmado', 'confirmado'], ['Feito (compareceu)', 'feito'], ['No-show', 'no_show']].forEach(([label, status]) => actions.appendChild(button(label, () => mutate(status))));
        actions.append(button('Venda', () => { sale.hidden = false; value.focus(); }, 'primary'), button('Reagendar', () => { if (busy) return; dialog.close(); openBooking(row); }), button('Cancelar', async () => {
          if (busy) return; busy = true;
          try { const ok = await ctx.confirm({title: 'Cancelar agendamento?', message: 'O horário será liberado na agenda.', confirmLabel: 'Cancelar agendamento', danger: true}); busy = false; if (ok && !closed && !disposed) await mutate('cancelado'); }
          catch (error) { busy = false; showError(error); }
        }, 'danger'));
      }
      function openBooking(previous) {
        let closed = false, busy = false, autoPicked = false, slotsRevision = 0, leadsRevision = 0, slots = [], selected = null, leadId = previous && previous.lead_id || '', searchTimer;
        const body = SDR.el('div', {class: 'stack ag-form'}), errorBox = SDR.el('div', {role: 'alert'}), slotBox = SDR.el('div', {'aria-live': 'polite'});
        const minDate = dateKey(new Date(), tz()), lastDate = dateKey(SDR.tzTools.addDays(new Date(), 13, tz()), tz());
        const day = SDR.el('input', {type: 'date', class: 'input mono', min: minDate, max: lastDate, value: minDate});
        const owner = closerSelect(closers().filter(c => c.active), previous && previous.closer_id, 'Todos · distribuição automática');
        const notes = SDR.el('textarea', {class: 'textarea', maxlength: 4000, rows: 3}, previous && previous.notes || '');
        if (!previous) {
          const search = SDR.el('input', {class: 'input', type: 'search', placeholder: 'Buscar por nome, telefone ou e-mail', autocomplete: 'off'});
          const results = SDR.el('div', {class: 'ag-lead-results', 'aria-live': 'polite'});
          body.append(labeled('Contato para agendar', search), results);
          async function loadLeads() {
            const token = ++leadsRevision, term = search.value; loading(results);
            try {
              const leads = await ctx.api.listLeads({search: term, limit: 200});
              if (closed || disposed || token !== leadsRevision) return;
              SDR.mount(results, SDR.raw(''));
              if (!leads.length) { results.appendChild(SDR.el('p', {class: 'empty'}, 'Nenhum contato encontrado. Cadastre um contato na tela Leads.')); return; }
              leads.forEach(lead => {
                const item = SDR.el('button', {type: 'button', class: 'ag-lead-option', 'aria-pressed': leadId === lead.id, onClick() {
                  leadId = lead.id; SDR.qsa('.ag-lead-option', results).forEach(b => b.setAttribute('aria-pressed', String(b === item))); selection.textContent = 'Contato selecionado: ' + (lead.name || 'Sem nome');
                }}, SDR.el('strong', {}, lead.name || 'Sem nome'), SDR.el('span', {class: 'mono muted'}, SDR.fmt.phone(lead.phone)));
                results.appendChild(item);
              });
            } catch (error) { if (!closed && !disposed && token === leadsRevision) failure(results, error, loadLeads); }
          }
          search.addEventListener('input', () => { leadId = ''; selection.textContent = 'Selecione um contato nos resultados.'; ++leadsRevision; clearTimeout(searchTimer); searchTimer = setTimeout(loadLeads, 250); });
          const selection = SDR.el('p', {class: 'muted', 'aria-live': 'polite'}, 'Selecione um contato nos resultados.'); body.appendChild(selection);
          loadLeads();
        } else body.appendChild(SDR.el('p', {class: 'banner'}, 'O horário anterior será substituído somente se a nova reserva for concluída.'));
        body.append(SDR.el('div', {class: 'grid grid-2'}, labeled('Data (próximos 14 dias)', day), labeled('Responsável', owner)), SDR.el('p', {class: 'muted'}, 'Horários no fuso ' + tz() + '.'), slotBox);
        if (!previous) body.appendChild(labeled('Notas (opcional)', notes));
        body.appendChild(errorBox);
        const dialog = modal({title: previous ? 'Reagendar conversa' : 'Agendar manualmente', body, wide: true, actions: [{label: previous ? 'Confirmar reagendamento' : 'Confirmar agendamento', kind: 'primary', onClick: save}], onClose() { closed = true; ++slotsRevision; ++leadsRevision; clearTimeout(searchTimer); }});
        function showError(error) { if (!closed) SDR.mount(errorBox, SDR.html`<div class="banner banner-error">${error.message || 'Não foi possível agendar. Tente de novo.'}</div>`); }
        async function loadSlots() {
          const token = ++slotsRevision; selected = null; loading(slotBox); slotBox.setAttribute('aria-busy', 'true');
          try {
            if (day.value < minDate || day.value > lastDate) throw new Error('Escolha uma data nos próximos 14 dias.');
            const from = wallDay(day.value, tz()), to = SDR.tzTools.addDays(from, 1, tz());
            slots = await ctx.api.availableSlots(from.toISOString(), to.toISOString(), owner.value || undefined);
            if (closed || disposed || token !== slotsRevision) return;
            SDR.mount(slotBox, SDR.raw(''));
            if (!slots.length && !autoPicked) {
              // Primeira abertura: pula para o primeiro dia que tem horário livre.
              autoPicked = true;
              const wide = await ctx.api.availableSlots(from.toISOString(), SDR.tzTools.addDays(wallDay(lastDate, tz()), 1, tz()).toISOString(), owner.value || undefined);
              if (closed || disposed || token !== slotsRevision) return;
              if (wide.length) { day.value = dateKey(new Date(wide[0].starts_at), tz()); return loadSlots(); }
            }
            if (!slots.length) { slotBox.appendChild(SDR.el('p', {class: 'empty'}, 'Nenhum horário livre neste dia. Escolha outra data ou responsável.')); return; }
            const list = SDR.el('div', {class: 'ag-slot-list', role: 'group', 'aria-label': 'Horários disponíveis'});
            slots.forEach(slot => {
              const c = findCloser(slot.closer_id);
              const item = SDR.el('button', {type: 'button', class: 'btn btn-ghost ag-slot', 'aria-pressed': false, onClick() {
                selected = slot; SDR.qsa('button', list).forEach(b => { b.setAttribute('aria-pressed', String(b === item)); b.classList.toggle('active', b === item); });
              }}, SDR.el('span', {class: 'mono'}, SDR.fmt.time(slot.starts_at, tz())), SDR.el('small', {}, c ? c.name : 'Disponível'));
              list.appendChild(item);
            }); slotBox.appendChild(list);
          } catch (error) { if (!closed && !disposed && token === slotsRevision) failure(slotBox, error, loadSlots); }
          finally { if (!closed && token === slotsRevision) slotBox.setAttribute('aria-busy', 'false'); }
        }
        day.addEventListener('change', loadSlots); owner.addEventListener('change', loadSlots);
        async function save(close) {
          if (busy || closed || disposed) return;
          if (!leadId) { showError(new Error('Selecione um contato para agendar.')); return; }
          if (!selected || !slots.includes(selected)) { showError(new Error('Selecione um horário disponível.')); return; }
          busy = true; SDR.mount(errorBox, SDR.raw('')); SDR.qsa('button, input, select, textarea', body).forEach(input => { input.disabled = true; });
          try {
            const result = previous ? await ctx.api.rescheduleAppointment(previous.id, selected.starts_at, selected.closer_id || owner.value || undefined) : await ctx.api.manualBook(leadId, selected.starts_at, selected.closer_id || owner.value || undefined, notes.value.trim() || undefined);
            if (result && result.ok === false) throw new Error('Não foi possível reservar esse horário. Atualize a disponibilidade e tente de novo.');
            if (!disposed) { ctx.toast(previous ? 'Conversa reagendada.' : 'Agendamento criado.', 'ok'); close(); await load(); }
          } catch (error) { showError(error); if (!closed && !disposed) await loadSlots(); }
          finally { busy = false; if (!closed) SDR.qsa('button, input, select, textarea', body).forEach(input => { input.disabled = false; }); }
        }
        loadSlots();
      }
      await load();
      return () => { disposed = true; ++revision; ownedModals.forEach(dialog => dialog.close()); ownedModals.clear(); };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
