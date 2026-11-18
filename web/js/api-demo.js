(function (root) {
  'use strict';
  const SDR = root.SDR;
  let state;
  const listeners = new Set();
  const copy = value => value == null ? value : JSON.parse(JSON.stringify(value));
  const active = appointment => ['agendado', 'confirmado'].includes(appointment.status);
  const nowISO = () => new Date().toISOString();
  const time = value => new Date(value).getTime();
  const inRange = (value, from, to) => time(value) >= time(from) && time(value) < time(to);
  const overlap = (a, b, c, d) => time(a) < time(d) && time(b) > time(c);
  const settingsDefaults = {id: 1, call_minutes: 30, buffer_minutes: 10, slot_step_minutes: 30, min_notice_hours: 2, days_ahead: 14, timezone: 'America/Sao_Paulo', public_slug: 'demo', public_enabled: true, distribution: 'rodizio', public_max_per_hour: 20, public_max_per_ip_day: 3, public_max_per_phone_day: 3};
  function data() {
    if (!state) {
      if (!root.SDR_DEMO_DATA) throw new Error('Os dados de demonstração não foram carregados. Tente atualizar a página.');
      state = copy(root.SDR_DEMO_DATA);
      ['closers', 'rules', 'blocks', 'leads', 'appointments', 'messages', 'objections', 'outbox', 'funnel_events'].forEach(key => { if (!Array.isArray(state[key])) state[key] = []; });
      state.scheduleSettings = Object.assign({}, settingsDefaults, state.scheduleSettings || {});
      state.agentConfig = Object.assign({id: 1, agent_name: 'Ana', company: '', qualification_questions: [], disqualify_rules: [], objection_playbook: [], followup_messages: [], test_only: true, test_phones: [], paused: false}, state.agentConfig || {});
    }
    return state;
  }
  function find(table, id, message) {
    const row = data()[table].find(item => String(item.id) === String(id));
    if (!row) throw new Error(message || 'Esse registro não foi encontrado. Atualize a página.');
    return row;
  }
  const nextId = table => data()[table].reduce((n, item) => Math.max(n, +item.id || 0), 0) + 1;
  function closerFor(id) {
    const row = data().closers.find(item => item.id === id);
    return row ? {id: row.id, name: row.name, color: row.color} : null;
  }
  function leadFor(id) {
    const lead = data().leads.find(item => item.id === id);
    return lead ? {id: lead.id, name: lead.name, phone: lead.phone, email: lead.email, stage: lead.stage} : null;
  }
  const embedLead = lead => Object.assign({}, lead, {closer: closerFor(lead.closer_id)});
  const embedAppointment = row => Object.assign({}, row, {lead: leadFor(row.lead_id), closer: closerFor(row.closer_id)});
  function updateStage(lead, stage) {
    if (lead.stage !== stage) {
      lead.stage = stage;
      data().funnel_events.push({id: nextId('funnel_events'), lead_id: lead.id, stage, created_at: nowISO()});
    }
    lead.updated_at = nowISO();
  }
  function save(table, obj) {
    if (obj.id) {
      const row = find(table, obj.id);
      Object.assign(row, copy(obj), {updated_at: nowISO()});
      return copy(row);
    }
    const row = Object.assign({id: SDR.uuid(), created_at: nowISO()}, copy(obj));
    data()[table].push(row);
    return copy(row);
  }
  function remove(table, id) {
    find(table, id);
    data()[table] = data()[table].filter(row => String(row.id) !== String(id));
  }
  function validateDates(start, end) {
    if (!Number.isFinite(time(start)) || !Number.isFinite(time(end)) || time(start) >= time(end)) throw new Error('Informe um intervalo de datas válido.');
  }
  function validateSettings(patch) {
    const numbers = {call_minutes: [1, 1440], buffer_minutes: [0, 1440], slot_step_minutes: [1, 1440], min_notice_hours: [0, 8760], days_ahead: [1, 366], public_max_per_hour: [1, 1000], public_max_per_ip_day: [1, 100], public_max_per_phone_day: [1, 100]};
    Object.keys(numbers).forEach(key => {
      if (patch[key] !== undefined && (!Number.isFinite(+patch[key]) || +patch[key] < numbers[key][0] || +patch[key] > numbers[key][1])) throw new Error('Confira os valores de duração, intervalo e antecedência.');
    });
    if (patch.timezone) { try { new Intl.DateTimeFormat('pt-BR', {timeZone: patch.timezone}); } catch (_) { throw new Error('Escolha um fuso horário válido.'); } }
    if (patch.distribution && !['rodizio', 'peso'].includes(patch.distribution)) throw new Error('Escolha uma distribuição válida.');
    if (patch.public_enabled && !String(patch.public_slug || data().scheduleSettings.public_slug || '').trim()) throw new Error('Informe o identificador do link público.');
  }
  function slots(from, to, closerId) {
    validateDates(from, to);
    const d = data(), settings = d.scheduleSettings, tz = settings.timezone;
    const minute = 60000, duration = +settings.call_minutes * minute, buffer = +settings.buffer_minutes * minute;
    const step = Math.max(1, +settings.slot_step_minutes);
    const earliest = Date.now() + +settings.min_notice_hours * 3600000;
    const latest = Date.now() + +settings.days_ahead * 86400000;
    const rangeStart = Math.max(time(from), earliest), rangeEnd = Math.min(time(to), latest + 1);
    if (rangeStart >= rangeEnd) return [];
    const result = [], seen = new Set();
    const closers = d.closers.filter(row => row.active && (!closerId || row.id === closerId));
    for (let day = SDR.tzTools.startOfDay(new Date(rangeStart), tz); day.getTime() < rangeEnd; day = SDR.tzTools.addDays(day, 1, tz)) {
      const p = SDR.tzTools.parts(day, tz);
      closers.forEach(closer => {
        d.rules.filter(rule => rule.closer_id === closer.id && +rule.weekday === p.weekday).forEach(rule => {
          const startBits = rule.start_time.split(':').map(Number), endBits = rule.end_time.split(':').map(Number);
          const end = SDR.tzTools.zonedToUtc(p.y, p.m, p.d, endBits[0], endBits[1], tz).getTime();
          for (let wall = startBits[0] * 60 + startBits[1]; wall < endBits[0] * 60 + endBits[1]; wall += step) {
            const start = SDR.tzTools.zonedToUtc(p.y, p.m, p.d, Math.floor(wall / 60), wall % 60, tz).getTime();
            const finish = start + duration;
            if (start < rangeStart || start >= rangeEnd || finish > end) continue;
            const blocked = d.blocks.some(block => (!block.closer_id || block.closer_id === closer.id) && overlap(start, finish, block.starts_at, block.ends_at));
            const occupied = d.appointments.some(row => row.closer_id === closer.id && active(row) && overlap(start, finish, time(row.starts_at) - buffer, time(row.ends_at) + buffer));
            const key = start + ':' + closer.id;
            if (!blocked && !occupied && !seen.has(key)) {
              result.push({starts_at: new Date(start).toISOString(), ends_at: new Date(finish).toISOString(), closer_id: closer.id}); seen.add(key);
            }
          }
        });
      });
    }
    return result.sort((a, b) => time(a.starts_at) - time(b.starts_at) || a.closer_id.localeCompare(b.closer_id));
  }
  const bookingErrors = {slot_indisponivel: 'Esse horário acabou de ser ocupado. Escolha outro.', lead_ja_agendado: 'Este contato já tem um agendamento ativo.', fora_da_agenda: 'Esse horário está fora da agenda disponível.', lead_inexistente: 'O contato não foi encontrado. Atualize a lista de leads.'};
  function book(leadId, startsAt, closerId, notes, source) {
    const d = data(), lead = d.leads.find(item => item.id === leadId);
    if (!lead) return {ok: false, error: 'lead_inexistente'};
    if (d.appointments.some(row => row.lead_id === leadId && active(row) && time(row.starts_at) > Date.now())) return {ok: false, error: 'lead_ja_agendado'};
    if (!Number.isFinite(time(startsAt))) return {ok: false, error: 'fora_da_agenda'};
    const candidates = slots(new Date(time(startsAt)).toISOString(), new Date(time(startsAt) + 1).toISOString(), closerId).filter(slot => time(slot.starts_at) === time(startsAt));
    if (!candidates.length) return {ok: false, error: 'slot_indisponivel'};
    const cutoff = Date.now() - 30 * 86400000;
    function load(id) {
      const closer = d.closers.find(row => row.id === id);
      const count = d.appointments.filter(row => row.closer_id === id && time(row.created_at) >= cutoff).length;
      return count / (d.scheduleSettings.distribution === 'peso' ? Math.max(1, +closer.weight) : 1);
    }
    candidates.sort((a, b) => load(a.closer_id) - load(b.closer_id) || closerFor(a.closer_id).name.localeCompare(closerFor(b.closer_id).name, 'pt-BR'));
    const selected = candidates[0];
    const appointment = Object.assign({id: SDR.uuid(), lead_id: leadId, status: 'agendado', sale_value: null, source: source || 'manual', notes: notes || null, created_at: nowISO(), updated_at: nowISO()}, selected);
    d.appointments.push(appointment);
    lead.closer_id = selected.closer_id;
    updateStage(lead, 'agendado');
    return {ok: true, appointment_id: appointment.id, closer_id: selected.closer_id, closer_name: closerFor(selected.closer_id).name, starts_at: selected.starts_at, ends_at: selected.ends_at};
  }
  function assertBook(result) { if (!result.ok) throw new Error(bookingErrors[result.error] || 'Não foi possível agendar. Escolha outro horário.'); return copy(result); }
  function outboxRow(leadId, phone, body, createdBy) {
    const row = {id: nextId('outbox'), lead_id: leadId, phone, body, status: 'pendente', attempts: 0, last_error: null, wa_key_id: null, created_by: createdBy, created_at: nowISO(), sent_at: null};
    data().outbox.push(row); return row;
  }
  function whatsappAdvance() {
    const d = data();
    if (!d.whatsappConnection) {
      const demo = d.whatsapp || {};
      d.whatsappConnection = {id: 1, state: 'open', owner_number: demo.demo_number || '5511900000000', qr_base64: null, pairing_code: null, qr_expires_at: null, command: null, command_id: null, command_status: null, command_error: null, command_requested_at: null, checked_at: nowISO(), updated_at: nowISO()};
    }
    const wa = d.whatsappConnection, timing = api.waTiming, now = Date.now();
    wa.checked_at = nowISO();
    if (wa.command && ['pendente', 'em_andamento', 'aguardando_leitura'].includes(wa.command_status)) {
      const elapsed = now - time(wa.command_requested_at);
      if (elapsed >= timing.openMs) {
        Object.assign(wa, {state: 'open', owner_number: (d.whatsapp || {}).demo_number || '5511900000000', qr_base64: null, pairing_code: null, qr_expires_at: null, command_status: 'concluido'});
      } else if (elapsed >= timing.qrMs) {
        if (wa.command_status !== 'aguardando_leitura') Object.assign(wa, {state: 'connecting', qr_base64: (d.whatsapp || {}).demo_qr || null, pairing_code: 'DEMO-0000', qr_expires_at: new Date(time(wa.command_requested_at) + timing.openMs).toISOString(), command_status: 'aguardando_leitura'});
      } else if (wa.command_status === 'pendente') wa.command_status = 'em_andamento';
    }
    return wa;
  }
  const api = {
    mode: 'demo',
    init() { data(); return api; },
    async getSession() { return {email: 'demo@exemplo.com'}; },
    async signIn() { const session = {email: 'demo@exemplo.com'}; listeners.forEach(cb => cb(copy(session))); return session; },
    async signOut() { listeners.forEach(cb => cb(null)); },
    onAuthChange(cb) { listeners.add(cb); return () => listeners.delete(cb); },
    async getScheduleSettings() { return copy(data().scheduleSettings); },
    async saveScheduleSettings(patch) { validateSettings(patch); Object.assign(data().scheduleSettings, copy(patch), {id: 1, updated_at: nowISO()}); return copy(data().scheduleSettings); },
    async getAgentConfig() { return copy(data().agentConfig); },
    async saveAgentConfig(patch) { Object.assign(data().agentConfig, copy(patch), {id: 1, updated_at: nowISO()}); return copy(data().agentConfig); },
    async listClosers() { return copy(data().closers.slice().sort((a, b) => a.name.localeCompare(b.name, 'pt-BR'))); },
    async saveCloser(obj) {
      if (!String(obj.name || '').trim()) throw new Error('Informe o nome do responsável.');
      if (obj.weight != null && (!Number.isInteger(+obj.weight) || +obj.weight < 1 || +obj.weight > 10)) throw new Error('O peso do responsável deve ser um número inteiro entre 1 e 10.');
      return save('closers', Object.assign(obj.id ? {} : {active: true, weight: 1, color: '#D97706'}, obj));
    },
    async deleteCloser(id) {
      if (data().appointments.some(row => row.closer_id === id)) throw new Error('Este responsável possui agendamentos. Desative-o para preservar o histórico.');
      remove('closers', id); data().rules = data().rules.filter(row => row.closer_id !== id); data().blocks = data().blocks.filter(row => row.closer_id !== id); data().leads.forEach(row => { if (row.closer_id === id) row.closer_id = null; });
    },
    async listRules(closerId) { return copy(data().rules.filter(row => !closerId || row.closer_id === closerId).sort((a, b) => a.weekday - b.weekday || a.start_time.localeCompare(b.start_time))); },
    async saveRule(obj) {
      find('closers', obj.closer_id);
      const validTime = value => /^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$/.test(value || '');
      if (!Number.isInteger(+obj.weekday) || +obj.weekday < 0 || +obj.weekday > 6 || !validTime(obj.start_time) || !validTime(obj.end_time) || obj.start_time.slice(0, 5) >= obj.end_time.slice(0, 5)) throw new Error('Confira o dia da semana e os horários da disponibilidade.');
      return save('rules', Object.assign({}, obj, {weekday: +obj.weekday, start_time: obj.start_time.slice(0, 5) + ':00', end_time: obj.end_time.slice(0, 5) + ':00'}));
    },
    async deleteRule(id) { remove('rules', id); },
    async listBlocks() { return copy(data().blocks.filter(row => time(row.ends_at) >= Date.now() - 30 * 86400000).sort((a, b) => time(a.starts_at) - time(b.starts_at))); },
    async saveBlock(obj) { validateDates(obj.starts_at, obj.ends_at); if (obj.closer_id) find('closers', obj.closer_id); return save('blocks', Object.assign({closer_id: null}, obj)); },
    async deleteBlock(id) { remove('blocks', id); },
    async listAppointments(from, to, closerId) { validateDates(from, to); return copy(data().appointments.filter(row => inRange(row.starts_at, from, to) && (!closerId || row.closer_id === closerId)).sort((a, b) => time(a.starts_at) - time(b.starts_at)).map(embedAppointment)); },
    async setAppointmentStatus(id, status, extra) {
      if (!['agendado', 'confirmado', 'feito', 'no_show', 'reagendado', 'cancelado', 'venda'].includes(status)) throw new Error('Escolha um status válido.');
      const row = find('appointments', id), lead = find('leads', row.lead_id);
      if (extra && typeof extra.sale_value === 'string' && extra.sale_value.trim() === '') extra = Object.assign({}, extra, {sale_value: null});
      if (extra && extra.sale_value != null && (!Number.isFinite(+extra.sale_value) || +extra.sale_value < 0)) throw new Error('Informe um valor de venda válido.');
      if (status === 'venda' && !(extra && +extra.sale_value > 0)) throw new Error('Informe o valor da venda (maior que zero).');
      if (active({status}) && data().appointments.some(other => other.id !== id && other.closer_id === row.closer_id && active(other) && overlap(row.starts_at, row.ends_at, other.starts_at, other.ends_at))) throw new Error('Esse horário já está ocupado por outro agendamento.');
      if (active({status}) && data().appointments.some(other => other.id !== id && other.lead_id === row.lead_id && active(other))) throw new Error('Este contato já tem um agendamento ativo. Reagende a conversa existente.');
      row.status = status; row.updated_at = nowISO();
      if (extra && extra.sale_value != null) row.sale_value = +extra.sale_value;
      const stage = {agendado: 'agendado', confirmado: 'confirmado', feito: 'compareceu', no_show: 'no_show', reagendado: 'reagendado', venda: 'venda', cancelado: 'qualificado'}[status];
      if (stage) updateStage(lead, stage);
      return copy(embedAppointment(row));
    },
    async availableSlots(from, to, closerId) { return copy(slots(from, to, closerId)); },
    async manualBook(leadId, startsAt, closerId, notes) { return assertBook(book(leadId, startsAt, closerId, notes, 'manual')); },
    async rescheduleAppointment(id, startsAt, closerId) {
      // Atômico como o reschedule_slot do banco: se o novo horário falha, o antigo fica como estava.
      const row = data().appointments.find(item => String(item.id) === String(id));
      if (!row) throw new Error('Agendamento não encontrado. Atualize a agenda.');
      if (!active(row)) throw new Error('Este agendamento não está mais ativo. Atualize a agenda.');
      const oldStatus = row.status, oldStage = leadFor(row.lead_id) && find('leads', row.lead_id).stage;
      row.status = 'reagendado';
      const note = 'reagendado de ' + SDR.fmt.dateTime(row.starts_at, data().scheduleSettings.timezone);
      const result = book(row.lead_id, startsAt, closerId || null, row.notes ? row.notes + ' · ' + note : note, row.source);
      if (!result.ok) { row.status = oldStatus; if (oldStage) find('leads', row.lead_id).stage = oldStage; throw new Error(bookingErrors[result.error] || 'Não foi possível reagendar. Escolha outro horário.'); }
      row.updated_at = nowISO();
      return Object.assign(copy(result), {old_appointment_id: row.id});
    },
    async listLeads(options) {
      const opts = options || {}, term = String(opts.search || '').toLocaleLowerCase('pt-BR');
      return copy(data().leads.filter(row => (!opts.stage || row.stage === opts.stage) && (!term || [row.name, row.phone, row.email].some(value => String(value || '').toLocaleLowerCase('pt-BR').includes(term)))).sort((a, b) => time(b.updated_at) - time(a.updated_at)).slice(0, opts.limit == null ? 200 : opts.limit).map(embedLead));
    },
    async getLead(id) { const lead = find('leads', id, 'Esse contato não foi encontrado.'); return copy(Object.assign(embedLead(lead), {appointments: data().appointments.filter(row => row.lead_id === id).sort((a, b) => time(b.starts_at) - time(a.starts_at)).map(embedAppointment), objections: data().objections.filter(row => row.lead_id === id)})); },
    async updateLead(id, patch) {
      const row = find('leads', id), payload = copy(SDR.sanitizeLeadPatch(patch));
      if (payload.stage && !['novo', 'respondeu', 'qualificado', 'desqualificado', 'horarios_ofertados', 'agendado', 'confirmado', 'compareceu', 'no_show', 'reagendado', 'venda', 'perdido'].includes(payload.stage)) throw new Error('Escolha um estágio válido para o contato.');
      if (payload.phone !== undefined && (!SDR.phone.valid(payload.phone) || data().leads.some(lead => lead.id !== id && lead.phone === payload.phone))) throw new Error('Informe um WhatsApp válido que ainda não esteja cadastrado.');
      if (payload.closer_id) find('closers', payload.closer_id);
      if (payload.stage) updateStage(row, payload.stage);
      Object.assign(row, payload, {updated_at: nowISO()}); return copy(embedLead(row));
    },
    async reativarLead(id) { const row = find('leads', id); Object.assign(row, {opt_out: false, updated_at: nowISO()}); return copy(embedLead(row)); },
    async listConversations(options) {
      return copy(data().leads.filter(lead => (lead.last_inbound_at || lead.last_outbound_at || data().messages.some(row => row.lead_id === lead.id)) && (!(options || {}).onlyTakeover || time(lead.takeover_until) > Date.now())).map(lead => {
        const last = data().messages.filter(row => row.lead_id === lead.id).sort((a, b) => time(b.created_at) - time(a.created_at))[0];
        return Object.assign(embedLead(lead), {lastMessage: last ? {body: last.body, direction: last.direction, author: last.author, created_at: last.created_at} : null});
      }).sort((a, b) => Math.max(time(b.last_inbound_at) || 0, time(b.last_outbound_at) || 0, b.lastMessage ? time(b.lastMessage.created_at) : 0) - Math.max(time(a.last_inbound_at) || 0, time(a.last_outbound_at) || 0, a.lastMessage ? time(a.lastMessage.created_at) : 0)));
    },
    async listMessages(leadId, limit) { return copy(data().messages.filter(row => row.lead_id === leadId).sort((a, b) => time(b.created_at) - time(a.created_at)).slice(0, limit == null ? 200 : limit).reverse()); },
    async setTakeover(leadId, on) { return api.updateLead(leadId, {takeover_until: on ? new Date(Date.now() + 24 * 3600000).toISOString() : null}); },
    async sendMessage(leadId, phone, body, clientKey) {
      const lead = find('leads', leadId);
      const reject = message => Object.assign(new Error(message), {sdrValidation: true});
      if (!SDR.phone.valid(phone) || !String(body || '').trim()) throw reject('Informe um WhatsApp válido e escreva a mensagem.');
      const agent = data().agentConfig;
      if (agent.test_only !== false && !(Array.isArray(agent.test_phones) ? agent.test_phones : []).includes(phone)) throw reject('O modo teste está ligado e este número não está na lista de testes. Libere a produção ou inclua o número nos testes antes de enviar.');
      const key = clientKey || SDR.uuid(), dup = data().outbox.find(item => item.client_key === key);
      if (dup) return {duplicate: true, client_key: key};
      const row = outboxRow(leadId, phone, String(body).trim(), 'painel'); row.client_key = key;
      data().messages.push({id: nextId('messages'), lead_id: leadId, direction: 'out', author: 'dono', body: row.body, media_type: 'text', wa_id: null, created_at: row.created_at});
      lead.last_outbound_at = row.created_at; lead.updated_at = row.created_at;
      return copy(row);
    },
    async findOutboxByKey(leadId, clientKey) { const row = data().outbox.find(item => item.lead_id === leadId && item.client_key === clientKey); return row ? copy(row) : null; },
    async listOutbox(leadId, limit) { return copy(data().outbox.filter(row => !leadId || row.lead_id === leadId).sort((a, b) => time(b.created_at) - time(a.created_at)).slice(0, limit == null ? 100 : limit)); },
    async listObjections(options) { const opts = options || {}; return copy(data().objections.filter(row => (!opts.category || row.category === opts.category) && (!opts.from || time(row.created_at) >= time(opts.from)) && (!opts.to || time(row.created_at) <= time(opts.to))).sort((a, b) => time(b.created_at) - time(a.created_at)).slice(0, Math.min(opts.limit == null ? 100 : opts.limit, 1000)).map(row => { const lead = leadFor(row.lead_id); return Object.assign({}, row, {lead: lead ? {name: lead.name, phone: lead.phone} : null}); })); },
    async setObjectionHandled(id, handled) { const row = find('objections', id); row.handled = !!handled; return copy(row); },
    async dashboardStats(from, to, closerId) {
      validateDates(from, to);
      const d = data(), included = lead => !closerId || lead.closer_id === closerId;
      const leads = d.leads.filter(lead => inRange(lead.created_at, from, to) && included(lead));
      const ids = new Set(leads.map(lead => lead.id));
      const events = d.funnel_events.filter(event => inRange(event.created_at, from, to) && d.leads.some(lead => lead.id === event.lead_id && included(lead)));
      const countStage = stage => new Set(events.filter(event => event.stage === stage).map(event => event.lead_id)).size;
      const appointments = d.appointments.filter(row => inRange(row.starts_at, from, to) && (!closerId || row.closer_id === closerId));
      const attended = appointments.filter(row => ['feito', 'venda'].includes(row.status)).length;
      const noShows = appointments.filter(row => row.status === 'no_show').length;
      const sales = appointments.filter(row => row.status === 'venda');
      const qualified = countStage('qualificado'), booked = new Set(appointments.map(row => row.lead_id)).size;
      const objections = d.objections.filter(row => inRange(row.created_at, from, to) && d.leads.some(lead => lead.id === row.lead_id && included(lead)));
      const categories = [...new Set(objections.map(row => row.category))];
      const reasons = [...new Set(leads.filter(row => row.stage === 'desqualificado').map(row => row.disqualify_reason || 'Não informado'))];
      const safeRate = (n, total) => total ? Math.round(n / total * 1000) / 1000 : null;
      const keys = ['novo', 'respondeu', 'qualificado', 'desqualificado', 'horarios_ofertados', 'agendado', 'confirmado', 'compareceu', 'no_show', 'reagendado', 'venda', 'perdido'];
      return {leads: ids.size, respondeu: countStage('respondeu'), qualificados: qualified, desqualificados: countStage('desqualificado'), agendados: booked, comparecimentos: attended, no_shows: noShows, vendas: sales.length, valor_vendas: sales.reduce((sum, row) => sum + (+row.sale_value || 0), 0), taxa_qualificacao: safeRate(qualified, ids.size), taxa_agendamento: safeRate(booked, qualified), taxa_comparecimento: safeRate(attended, attended + noShows), funil: keys.map(stage => ({stage, count: countStage(stage)})), objecoes: categories.map(category => ({category, count: objections.filter(row => row.category === category).length, tratadas: objections.filter(row => row.category === category && row.handled).length})).sort((a, b) => b.count - a.count), desqualificacao: reasons.map(motivo => ({motivo, count: leads.filter(row => row.stage === 'desqualificado' && (row.disqualify_reason || 'Não informado') === motivo).length})).sort((a, b) => b.count - a.count)};
    },
    // Simulação do WhatsApp: pedido -> QR de demonstração (~2 s) -> conectado (~10 s). Nada sai da página.
    waTiming: {qrMs: 2000, openMs: 10000},
    async isPanelUser() { return true; },
    async getWhatsappConnection() {
      // Mesmo formato do live: idades e validade calculadas "pelo servidor" (aqui, a própria demo).
      const wa = copy(whatsappAdvance()), now = Date.now();
      const left = wa.qr_expires_at ? (time(wa.qr_expires_at) - now) / 1000 : null;
      return Object.assign(wa, {
        server_now: nowISO(), checked_age_seconds: 0,
        command_age_seconds: wa.command_requested_at ? Math.max(0, (now - time(wa.command_requested_at)) / 1000) : null,
        qr_valid: !!wa.qr_base64 && left != null && left > 0 && wa.command_status === 'aguardando_leitura',
        qr_remaining_seconds: left == null ? null : Math.max(0, left)
      });
    },
    async requestWhatsappCommand(command) {
      if (!['connect', 'logout'].includes(command)) return {ok: false, error: 'comando_invalido'};
      const wa = whatsappAdvance();
      if (['pendente', 'em_andamento'].includes(wa.command_status)) return {ok: false, error: 'comando_em_andamento'};
      const id = SDR.uuid();
      Object.assign(wa, {command, command_id: id, command_status: 'pendente', command_error: null, command_requested_at: nowISO(), updated_at: nowISO()});
      if (command === 'logout') Object.assign(wa, {state: 'close', owner_number: null, qr_base64: null, pairing_code: null, qr_expires_at: null});
      return {ok: true, command_id: id};
    },

    async publicSlots(slug, from, to) {
      const settings = data().scheduleSettings;
      if (!settings.public_enabled || slug !== settings.public_slug) return [];
      const seen = new Set();
      return slots(from, to).filter(row => { if (seen.has(row.starts_at)) return false; seen.add(row.starts_at); return true; }).map(row => ({starts_at: row.starts_at, ends_at: row.ends_at}));
    },
    async publicBook(options) {
      const opts = options || {}, d = data(), settings = d.scheduleSettings;
      if (!settings.public_enabled || opts.slug !== settings.public_slug) return {ok: false, error: 'pagina_desativada'};
      if (String(opts.hp || '').length) return {ok: true};
      const name = String(opts.name || '').replace(/[\r\n{}]+/g, ' ').replace(/\S*(?::\/\/|www|\.[a-z]{2,})\S*/gi, ' ').replace(/\s+/g, ' ').trim().slice(0, 60), email = String(opts.email || '').trim(), phone = String(opts.whatsapp || '').trim();
      if (name.length < 2 || name.length > 60 || !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email) || !SDR.phone.valid(phone) || !Number.isFinite(time(opts.startsAt))) return {ok: false, error: 'dados_invalidos'};
      // Conta TENTATIVAS (não só sucessos): global/hora e por telefone/dia. IP não existe na demonstração.
      const attempts = d.public_attempts || (d.public_attempts = []);
      const now = Date.now();
      const hourCount = attempts.filter(row => now - row.at < 3600000).length;
      const phoneCount = attempts.filter(row => row.phone === phone && now - row.at < 86400000).length;
      attempts.push({at: now, phone});
      if (hourCount >= (+settings.public_max_per_hour || 20) || phoneCount >= (+settings.public_max_per_phone_day || 3)) return {ok: false, error: 'limite_atingido'};
      let lead = d.leads.find(row => row.phone === phone);
      if (lead && lead.opt_out) return {ok: false, error: 'indisponivel'};
      const priorStage = lead && lead.stage, priorCloser = lead && lead.closer_id;
      if (!lead) {
        lead = {id: SDR.uuid(), name, email, phone, source: 'pagina', stage: 'novo', profile: null, qualification: {}, score: null, disqualify_reason: null, takeover_until: null, closer_id: null, opt_out: false, followup_step: 0, last_inbound_at: null, last_outbound_at: null, created_at: nowISO(), updated_at: nowISO()};
        d.leads.push(lead); d.funnel_events.push({id: nextId('funnel_events'), lead_id: lead.id, stage: 'novo', created_at: nowISO()});
      } else { if (name) lead.name = name; if (email) lead.email = email; lead.source = 'pagina'; lead.updated_at = nowISO(); }
      const result = book(lead.id, opts.startsAt, null, null, 'pagina');
      if (!result.ok) return {ok: false, error: result.error === 'lead_ja_agendado' ? 'ja_agendado' : 'slot_indisponivel'};
      // Estágio terminal ou já avançado não regride (venda, compareceu, desqualificado, perdido); o responsável existente é mantido.
      const terminal = priorStage && ['venda', 'compareceu', 'desqualificado', 'perdido'].includes(priorStage);
      if (terminal) lead.stage = priorStage;
      if (priorCloser) lead.closer_id = priorCloser;
      // Desqualificado/perdido agenda, mas não enfileira WhatsApp. A confirmação nunca leva texto livre do lead.
      if (!['desqualificado', 'perdido'].includes(priorStage)) {
        const company = String(d.agentConfig.company || '').replace(/[\r\n{}]+/g, ' ').trim().slice(0, 60);
        const body = 'Olá! Sua conversa' + (company ? ' com ' + company : '') + ' está confirmada para ' + SDR.fmt.date(result.starts_at, settings.timezone) + ' às ' + SDR.fmt.time(result.starts_at, settings.timezone) + '.';
        outboxRow(lead.id, phone, body, 'pagina');
      }
      return {ok: true, starts_at: result.starts_at};
    }
  };
  SDR.apiDemo = api;
}(typeof window !== 'undefined' ? window : globalThis));
