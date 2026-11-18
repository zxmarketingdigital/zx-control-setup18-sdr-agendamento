(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  const root = SDR.qs('#agendamento');
  if (!root) return;
  const browserTz = SDR.tzTools.browserTz();
  const state = { step: 1, days: [], day: '', selected: '', slots: [], sending: false, error: '', errors: {}, retryOf: null, fields: { name: '', email: '', phone: '', country: 'BR', website: '' }, revision: 0 };
  const icons = {
    clock: SDR.raw('<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg>'),
    arrow: SDR.raw('<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="m9 5 7 7-7 7M4 12h12"/></svg>'),
    check: SDR.raw('<svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m5 12 4 4L19 6"/></svg>')
  };
  function slugFromPath() {
    try {
      const segments = global.location.pathname.split('/').filter(Boolean), index = segments.indexOf('agendar');
      let tail = index >= 0 ? segments.slice(index + 1) : [];
      if (tail[tail.length - 1] === 'index.html') tail.pop();
      const slug = tail.length ? decodeURIComponent(tail[tail.length - 1]).trim().toLowerCase() : '';
      return slug || (SDR.config.demo === true ? 'demo' : '');
    } catch (_) { return ''; }
  }
  const slug = slugFromPath();
  function dateKey(value) { const p = SDR.tzTools.parts(value, browserTz); return [p.y, String(p.m).padStart(2,'0'), String(p.d).padStart(2,'0')].join('-'); }
  function longDate(value) { return new Intl.DateTimeFormat('pt-BR', { timeZone: browserTz, weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' }).format(new Date(value)); }
  function timezoneText(value) {
    let offset = '';
    try { const part = new Intl.DateTimeFormat('pt-BR', { timeZone: browserTz, timeZoneName: 'shortOffset' }).formatToParts(new Date(value || Date.now())).find(p => p.type === 'timeZoneName'); offset = part ? part.value.replace('−', '-') : ''; } catch (_) { /* O identificador IANA continua visível. */ }
    return browserTz + (offset ? ' (' + offset + ')' : '');
  }
  function stepHeader(n, text) { return SDR.html`<div class="booking-step"><span class="step-number">${String(n).padStart(2,'0')}</span><span>${text}</span><span class="step-line"></span><span class="step-dots" aria-label="Etapa ${n} de 3">${[1,2,3].map(i => SDR.html`<span class="step-dot ${i <= n ? 'is-active' : ''}"></span>`)}</span></div>`; }
  function alert() { return state.error ? SDR.html`<div class="banner banner-error booking-alert" role="alert">${state.error}</div>` : SDR.raw(''); }
  function focus(selector) { const el = SDR.qs(selector,root); if (el) el.focus({ preventScroll: true }); }
  function renderTimes() {
    const day = state.days.find(d => d.key === state.day);
    if (!day) return SDR.raw('');
    const selectedDay = longDate(day.slots[0].starts_at);
    return SDR.html`<p class="slot-label" id="slot-label">Horários disponíveis</p><div class="slot-list" role="group" aria-labelledby="slot-label">${day.slots.map(slot => {
      const time = SDR.fmt.time(slot.starts_at,browserTz);
      // Inclui offset para distinguir horários repetidos em transições de fuso.
      return SDR.html`<button type="button" class="slot-button" data-slot="${slot.starts_at}" aria-pressed="${state.selected === slot.starts_at}" aria-label="${selectedDay}, ${time}, ${timezoneText(slot.starts_at)}">${time}</button>`;
    })}</div>`;
  }
  function renderPick() {
    const day = state.days.find(d => d.key === state.day) || state.days[0];
    state.day = day.key;
    const month = new Intl.DateTimeFormat('pt-BR', { timeZone: browserTz, month: 'long', year: 'numeric' }).format(new Date(day.slots[0].starts_at));
    SDR.mount(root,SDR.html`${stepHeader(1,'Dia e horário')}${alert()}<h2 id="step-title" tabindex="-1">Quando podemos conversar?</h2><p class="timezone-note">Horários no seu fuso: ${timezoneText(day.slots[0].starts_at)}</p><p class="month-label">${month}</p><div class="day-nav"><button type="button" class="btn btn-ghost day-arrow" data-scroll="-1" aria-label="Ver dias anteriores">‹</button><div class="day-list" role="group" aria-label="Dias com horários livres">${state.days.map(d => {
      const start = d.slots[0].starts_at, p = SDR.tzTools.parts(start,browserTz);
      return SDR.html`<button type="button" class="day-button" data-day="${d.key}" aria-pressed="${state.day === d.key}" aria-label="${longDate(start)}; ${d.slots.length} horários disponíveis"><span class="day-week">${SDR.fmt.weekdayShort(start,browserTz)}</span><span class="day-number">${String(p.d).padStart(2,'0')}</span><span class="day-month">${new Intl.DateTimeFormat('pt-BR',{timeZone:browserTz,month:'short'}).format(new Date(start)).replace('.','')}</span></button>`;
    })}</div><button type="button" class="btn btn-ghost day-arrow" data-scroll="1" aria-label="Ver próximos dias">›</button></div><div id="time-options">${renderTimes()}</div><button type="button" class="btn btn-primary booking-action" id="continue" ${state.selected ? SDR.raw('') : SDR.raw('disabled')}>Continuar ${icons.arrow}</button><p class="booking-caption" id="selection-caption" aria-live="polite">${state.selected ? 'Selecionado: ' + SDR.fmt.dateTime(state.selected,browserTz) : 'Selecione um dia e um horário para continuar.'}</p>`);
  }
  function errorField(key) { return SDR.html`<p class="field-error" id="error-${key}" ${state.errors[key] ? SDR.raw('') : SDR.raw('hidden')}>${state.errors[key] || ''}</p>`; }
  function renderForm() {
    const f = state.fields;
    SDR.mount(root,SDR.html`${stepHeader(2,'Seus dados')}<button type="button" class="btn btn-ghost back-button" id="back">← Alterar horário</button>${alert()}<h2 id="step-title" tabindex="-1">Só falta confirmar seus dados.</h2><p class="muted field-hint">Vamos usá-los para confirmar seu agendamento.</p><div class="selected-summary">${icons.clock}<div><strong>${longDate(state.selected)}</strong><span class="mono">${SDR.fmt.time(state.selected,browserTz)}</span><br><small>${timezoneText(state.selected)}</small></div></div><form id="booking-form" class="booking-form" novalidate>
      <div class="field"><label class="label" for="name">Nome completo</label><input class="input" id="name" name="name" autocomplete="name" required minlength="2" maxlength="120" value="${f.name}" aria-describedby="error-name" aria-invalid="${!!state.errors.name}">${errorField('name')}</div>
      <div class="field"><label class="label" for="email">E-mail</label><input class="input" id="email" name="email" type="email" autocomplete="email" required maxlength="254" placeholder="voce@exemplo.com" value="${f.email}" aria-describedby="error-email" aria-invalid="${!!state.errors.email}">${errorField('email')}</div>
      <div class="field"><label class="label" for="phone">WhatsApp</label><div class="phone-fields"><label class="sr-only" for="country">País do WhatsApp</label><select id="country" name="country" class="select">${SDR.phone.COUNTRIES.map(c => SDR.html`<option value="${c.iso}" ${f.country === c.iso ? SDR.raw('selected') : SDR.raw('')}>${c.flag} +${c.dial} ${c.nome}</option>`)}</select><input class="input" type="tel" inputmode="tel" autocomplete="tel" id="phone" name="phone" required maxlength="40" placeholder="Seu número" value="${f.phone}" aria-describedby="phone-hint error-phone" aria-invalid="${!!state.errors.phone}"></div><p class="field-hint" id="phone-hint">Você pode colar o número internacional com +.</p><p class="field-hint mono" id="phone-preview" aria-live="polite"></p>${errorField('phone')}</div>
      <div class="honeypot" aria-hidden="true"><label for="website">Website</label><input type="text" id="website" name="website" tabindex="-1" autocomplete="off" value="${f.website}"></div>
      <button class="btn btn-primary booking-action" id="confirm" type="submit">${state.error ? 'Tentar de novo' : 'Confirmar agendamento'} ${icons.arrow}</button><p class="booking-caption">Seus dados são usados somente para o atendimento.</p></form>`);
    updatePhonePreview();
  }
  function renderSuccess(startsAt) {
    state.step = 3;
    SDR.mount(root,SDR.html`${stepHeader(3,'Tudo certo')}<div class="booking-success"><div class="success-icon" aria-hidden="true">${icons.check}</div><h2 id="step-title" tabindex="-1">Agendamento confirmado!</h2><p class="muted">Seu horário está reservado. Até lá!</p><div class="success-date"><strong>${longDate(startsAt)}</strong><span class="mono">${SDR.fmt.time(startsAt,browserTz)}</span><small>Horários no seu fuso: ${timezoneText(startsAt)}</small></div><p>Se o atendimento pelo WhatsApp estiver ativo, a confirmação chega por lá. Em caso de dúvida, fale com quem enviou este link.</p>${SDR.config.demo === true ? SDR.html`<p class="field-hint">Nesta demonstração, a reserva foi registrada somente na memória desta aba. Nenhuma mensagem real é enviada.</p>` : SDR.raw('')}</div>`);
    focus('#step-title');
  }
  function render() { root.setAttribute('aria-busy','false'); if (state.step === 1) renderPick(); else renderForm(); }
  async function load(message) {
    const revision = ++state.revision;
    state.step = 1; state.selected = ''; state.error = message || '';
    root.setAttribute('aria-busy','true');
    SDR.mount(root,SDR.html`<div class="empty" role="status"><span class="spinner" aria-hidden="true"></span><p>Carregando...</p></div>`);
    try {
      await SDR.ready;
      const now = new Date(), to = new Date(now.getTime() + 30 * 86400000);
      const rows = await SDR.api.publicSlots(slug,now.toISOString(),to.toISOString());
      if (revision !== state.revision) return;
      const unique = new Map();
      (rows || []).forEach(slot => { const start = new Date(slot.starts_at), end = new Date(slot.ends_at); if (Number.isFinite(start.getTime()) && Number.isFinite(end.getTime()) && start >= now && end > start) unique.set(start.toISOString(),{starts_at:start.toISOString(),ends_at:end.toISOString()}); });
      state.slots = Array.from(unique.values()).sort((a,b) => a.starts_at.localeCompare(b.starts_at));
      const groups = new Map();
      state.slots.forEach(slot => { const key = dateKey(slot.starts_at); if (!groups.has(key)) groups.set(key,{key,slots:[]}); groups.get(key).slots.push(slot); });
      state.days = Array.from(groups.values());
      root.setAttribute('aria-busy','false');
      if (!state.days.length) { SDR.mount(root,SDR.html`${alert()}<div class="empty"><div class="booking-icon" aria-hidden="true">${icons.clock}</div><h2 id="step-title" tabindex="-1">Nenhum horário disponível agora</h2><p>A agenda pode estar completa ou o link pode estar desativado. Tente novamente mais tarde ou fale com quem enviou este link.</p><button type="button" class="btn btn-ghost" id="retry">Tentar de novo</button></div>`); return; }
      if (!groups.has(state.day)) state.day = state.days[0].key;
      render();
      if (message) focus('#step-title');
    } catch (error) {
      if (revision !== state.revision) return;
      root.setAttribute('aria-busy','false');
      SDR.mount(root,SDR.html`<div class="empty" role="alert"><h2>Não foi possível carregar a agenda.</h2><p>${error.message || 'Verifique sua conexão e tente de novo.'}</p><button class="btn btn-ghost" type="button" id="retry">Tentar de novo</button></div>`);
    }
  }
  function readFields() {
    ['name','email','phone','country','website'].forEach(key => { const input = SDR.qs('#'+key,root); if (input) state.fields[key] = input.value; });
  }
  const DIAL_MSG = 'Esse número parece já ter o código do país. Digite só o número local ou comece com +.';
  function validate() {
    const f = state.fields;
    state.errors = {};
    if (f.name.trim().length < 2 || f.name.trim().length > 120) state.errors.name = 'Informe seu nome com 2 a 120 caracteres.';
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(f.email.trim()) || f.email.trim().length > 254) state.errors.email = 'Informe um e-mail válido.';
    const country = SDR.phone.COUNTRIES.find(c => c.iso === f.country) || SDR.phone.COUNTRIES[0];
    const phone = SDR.phone.toE164(f.phone,country.dial);
    if (!phone) state.errors.phone = SDR.phone.hasDialPrefix(f.phone,country.dial) ? DIAL_MSG : 'Informe um WhatsApp válido e confira o país selecionado.';
    Object.keys({name:1,email:1,phone:1}).forEach(key => {
      const input = SDR.qs('#'+key,root), error = SDR.qs('#error-'+key,root);
      input.setAttribute('aria-invalid',String(!!state.errors[key])); error.textContent = state.errors[key] || ''; error.hidden = !state.errors[key];
    });
    const first = Object.keys(state.errors)[0];
    if (first) { focus('#'+first); return null; }
    return phone;
  }
  function updatePhonePreview() {
    const phone = SDR.qs('#phone',root), preview = SDR.qs('#phone-preview',root), select = SDR.qs('#country',root);
    if (!phone || !preview) return;
    const country = SDR.phone.COUNTRIES.find(c => c.iso === (select && select.value)) || SDR.phone.COUNTRIES[0];
    const value = phone.value.trim() ? SDR.phone.toE164(phone.value,country.dial) : null;
    preview.textContent = value ? 'Vamos enviar para ' + SDR.fmt.phone(value) : (phone.value.trim() && SDR.phone.hasDialPrefix(phone.value,country.dial) ? DIAL_MSG : '');
  }
  root.addEventListener('input',event => { if (event.target.id === 'phone') updatePhonePreview(); });
  root.addEventListener('change',event => { if (event.target.id === 'country') updatePhonePreview(); });
  root.addEventListener('click',event => {
    const button = event.target.closest('button');
    if (!button || button.disabled || state.sending) return;
    if (button.dataset.scroll) { const strip = SDR.qs('.day-list',root); strip.scrollBy({left: Number(button.dataset.scroll)*200,behavior: global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'}); return; }
    if (button.dataset.day) {
      const strip = SDR.qs('.day-list',root), scroll = strip.scrollLeft;
      state.day = button.dataset.day; state.selected = ''; renderPick();
      SDR.qs('.day-list',root).scrollLeft = scroll; focus('[data-day="'+state.day+'"]'); return;
    }
    if (button.dataset.slot) {
      if (!state.slots.some(s => s.starts_at === button.dataset.slot)) return;
      state.selected = button.dataset.slot;
      SDR.qsa('[data-slot]',root).forEach(b => b.setAttribute('aria-pressed',String(b.dataset.slot === state.selected)));
      SDR.qs('#continue',root).disabled = false;
      SDR.qs('#selection-caption',root).textContent = 'Selecionado: '+SDR.fmt.dateTime(state.selected,browserTz); return;
    }
    if (button.id === 'continue' && state.selected) { state.step = 2; state.error = ''; render(); focus('#name'); }
    else if (button.id === 'back') { readFields(); state.step = 1; state.error = ''; render(); focus('#step-title'); }
    else if (button.id === 'retry') { SDR.initConnection(); load(); }
  });
  root.addEventListener('submit',async event => {
    if (event.target.id !== 'booking-form') return;
    event.preventDefault();
    if (state.sending) return;
    readFields(); const phone = validate(); if (!phone) return;
    state.sending = true;
    const button = SDR.qs('#confirm',root); button.disabled = true; button.textContent = 'Confirmando...'; root.setAttribute('aria-busy','true');
    try {
      const result = await SDR.api.publicBook({slug,name:state.fields.name.trim(),email:state.fields.email.trim(),whatsapp:phone,startsAt:state.selected,hp:state.fields.website});
      if (!result || !result.ok) {
        const code = result && result.error;
        // Tentativa anterior sem resposta (rede caiu): ja_agendado só diz que o WhatsApp tem call ativa, não QUAL horário.
        // A RPC pública não expõe o horário existente, então nunca se mostra "confirmado" por inferência.
        if (code === 'ja_agendado' && state.retryOf && state.retryOf.phone === phone) { state.retryOf = null; SDR.mount(root,SDR.html`<div class="empty" role="status"><h2 id="step-title" tabindex="-1">Você já tem um horário marcado</h2><p>Se o atendimento pelo WhatsApp estiver ativo, a confirmação chega por lá. Em caso de dúvida, fale com quem enviou este link. Para alterar o horário, fale com quem enviou este link.</p></div>`); focus('#step-title'); return; }
        if (code === 'slot_indisponivel') { await load('Esse horário acabou de ser ocupado. Escolha outro dia ou horário.'); return; }
        if (code === 'pagina_desativada') { SDR.mount(root,SDR.html`<div class="empty" role="alert"><h2>Agendamento indisponível</h2><p>Esta página foi desativada. Fale com quem enviou o link para combinar um horário.</p><button type="button" class="btn btn-ghost" id="retry">Tentar de novo</button></div>`); return; }
        if (code === 'limite_atingido') { state.error = result.scope === 'phone' || result.limit === 'phone' ? 'Este WhatsApp já fez muitas tentativas hoje. Tente de novo amanhã ou fale com quem enviou este link.' : 'Recebemos muitos agendamentos em pouco tempo. Aguarde alguns minutos e tente de novo, ou fale com quem enviou este link.'; renderForm(); focus('#step-title'); return; }
        state.error = code === 'indisponivel' ? 'Não foi possível agendar por esta página. Fale com quem enviou este link para combinar um horário.' : code === 'ja_agendado' ? 'Já existe um agendamento ativo para este WhatsApp. Fale com o atendimento para alterar o horário.' : code === 'email_invalido' ? 'Confira o e-mail: use um endereço no formato nome@dominio.com.' : code === 'dados_invalidos' ? 'Confira seu nome, e-mail e WhatsApp e tente de novo.' : 'Não foi possível confirmar. Tente de novo.';
        renderForm(); focus('#step-title'); return;
      }
      renderSuccess(result.starts_at || state.selected);
    } catch (error) {
      state.retryOf = {phone, startsAt: state.selected};
      state.error = error.message || 'Não foi possível confirmar o agendamento. Tente de novo.';
      renderForm(); focus('#step-title');
    } finally { state.sending = false; root.setAttribute('aria-busy','false'); if (button.isConnected) { button.disabled = false; button.textContent = 'Confirmar agendamento'; } }
  });
  SDR.beacon();
  if (SDR.config.demo === true) SDR.qs('#demo-banner').hidden = false;
  if (!SDR.requireConfigOrExplain(root)) { root.setAttribute('aria-busy','false'); return; }
  if (!slug) { root.setAttribute('aria-busy','false'); SDR.mount(root,SDR.html`<div class="empty" role="alert"><h2>Link de agendamento inválido.</h2><p>Confira o endereço ou peça um novo link a quem convidou você.</p></div>`); return; }
  load();
})(typeof window !== 'undefined' ? window : globalThis);
