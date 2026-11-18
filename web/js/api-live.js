(function (root) {
  'use strict';

  const SDR = root.SDR = root.SDR || {};
  let client = null;
  let publicClient = null;
  const scheduleDefaults = {
    id: 1, call_minutes: 30, buffer_minutes: 10, slot_step_minutes: 30,
    min_notice_hours: 2, days_ahead: 14, timezone: 'America/Sao_Paulo',
    public_slug: null, public_enabled: false, distribution: 'rodizio',
    public_max_per_hour: 20, public_max_per_ip_day: 3, public_max_per_phone_day: 3
  };
  const agentDefaults = {
    id: 1, agent_name: 'Ana', company: '', tone: '', niche: '', offer: '', knowledge: '',
    qualification_questions: [], disqualify_rules: [], objection_playbook: [], followup_messages: [],
    confirm_template: 'Olá {nome}! Sua conversa está confirmada para {data} às {hora}.',
    reminder_24h_template: '', reminder_2h_template: '', owner_phone: '',
    test_only: true, test_phones: [], ai_provider: 'gemini', paused: false,
    followup_window_start: '08:00:00', followup_window_end: '20:00:00'
  };
  const appointmentSelect = '*,lead:leads(id,name,phone,email,stage),closer:closers(id,name,color)';
  const leadSelect = '*,closer:closers(id,name,color)';
  const businessMessages = {
    slot_indisponivel: 'Esse horário acabou de ser ocupado. Escolha outro.',
    lead_ja_agendado: 'Este contato já tem um agendamento ativo. Reagende a conversa existente.',
    fora_da_agenda: 'Esse horário está fora da disponibilidade. Escolha outro.',
    lead_inexistente: 'Contato não encontrado. Atualize a lista e tente de novo.',
    agendamento_inexistente: 'Agendamento não encontrado. Atualize a agenda.',
    agendamento_inativo: 'Este agendamento não está mais ativo. Atualize a agenda.',
    status_invalido: 'Status de agendamento inválido.'
  };

  function friendly(message) {
    const error = new Error(message);
    error.sdrFriendly = true;
    return error;
  }

  function validation(message) {
    const error = friendly(message);
    error.sdrValidation = true;
    return error;
  }

  function translate(error, fallback) {
    if (error && error.sdrFriendly) return error;
    const code = String(error && error.code || '');
    const message = String(error && error.message || '').toLowerCase();
    if (code === 'invalid_credentials' || message.includes('invalid login credentials')) {
      return friendly('E-mail ou senha inválidos. Confira os dados e tente de novo.');
    }
    if (code === 'email_not_confirmed') return friendly('Confirme seu e-mail antes de entrar.');
    if (code === 'over_request_rate_limit' || code === 'over_email_send_rate_limit' || message.includes('rate limit')) {
      return friendly('Muitas tentativas em pouco tempo. Aguarde um momento e tente de novo.');
    }
    if (code === '42501' || code === 'PGRST301' || message.includes('permission denied')) {
      return friendly('Você não tem permissão para acessar esses dados. Entre com uma conta autorizada.');
    }
    if (code === '23505') { const dup = friendly('Já existe um registro com esses dados. Confira antes de salvar.'); dup.sdrDuplicateKey = true; return dup; }
    if (code === '23P01') return friendly(businessMessages.slot_indisponivel);
    if (code === '23503') return friendly('Este registro está vinculado a outros dados. Confira os vínculos e tente de novo.');
    if (message.includes('fetch') || message.includes('network') || message.includes('failed to load')) {
      return friendly('Não foi possível conectar. Confira sua conexão e tente de novo.');
    }
    return friendly(fallback || 'Não foi possível concluir a operação. Tente de novo.');
  }

  async function getClient(isPublic) {
    if (SDR.ready) await SDR.ready;
    const selected = isPublic ? publicClient : client;
    if (!selected) throw friendly('A conexão ainda não está disponível. Confira a configuração e tente de novo.');
    return selected;
  }

  async function query(build, fallback, isPublic) {
    try {
      const selected = await getClient(isPublic);
      const result = await build(selected);
      if (result.error) throw result.error;
      return result.data;
    } catch (error) {
      throw translate(error, fallback);
    }
  }

  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function limit(value, defaultValue) {
    const parsed = Number(value);
    // O servidor devolve no máximo 1000 linhas por consulta: pedir mais só esconderia o corte.
    return Number.isFinite(parsed) && parsed > 0 ? Math.min(Math.floor(parsed), 1000) : defaultValue;
  }
  function requireBooking(result) {
    if (!result || !result.ok) {
      throw friendly(businessMessages[result && result.error] || 'Não foi possível agendar. Atualize os horários e tente de novo.');
    }
    return result;
  }
  async function save(table, obj, message) {
    return query(c => {
      const action = obj.id ? c.from(table).update(obj).eq('id', obj.id) : c.from(table).insert(obj);
      return action.select('*').single();
    }, message);
  }
  async function remove(table, id, message) {
    // Sem .select() o PostgREST responde sucesso mesmo quando a RLS bloqueia (0 linhas apagadas).
    const rows = await query(c => c.from(table).delete().eq('id', id).select('id'), message);
    if (!Array.isArray(rows) || rows.length !== 1) throw friendly('Não foi possível excluir: o item não foi removido. Atualize a página e confira suas permissões.');
  }
  async function book(leadId, startsAt, closerId, notes) {
    const result = await query(c => c.rpc('book_slot', {
      p_lead: leadId, p_starts_at: startsAt, p_source: 'manual',
      p_closer: closerId || null, p_notes: notes || null
    }), 'Não foi possível agendar. Tente de novo.');
    return requireBooking(result);
  }

  const api = {
    mode: 'live',
    init(privateConnection, anonymousConnection) {
      client = privateConnection;
      publicClient = anonymousConnection;
      return api;
    },

    async getSession() {
      const data = await query(c => c.auth.getSession(), 'Não foi possível verificar sua sessão. Tente de novo.');
      return data && data.session && data.session.user ? { email: data.session.user.email } : null;
    },
    async signIn(email, password) {
      const data = await query(c => c.auth.signInWithPassword({ email, password }), 'Não foi possível entrar. Confira seus dados e tente de novo.');
      return data && data.user ? { email: data.user.email } : null;
    },
    async signOut() {
      await query(c => c.auth.signOut(), 'Não foi possível sair. Tente de novo.');
    },
    onAuthChange(callback) {
      let cancelled = false;
      let subscription = null;
      getClient(false).then(c => {
        if (cancelled) return;
        const result = c.auth.onAuthStateChange((_event, session) => {
          if (!cancelled) callback(session && session.user ? { email: session.user.email } : null);
        });
        subscription = result.data.subscription;
        if (cancelled && subscription) subscription.unsubscribe();
      }).catch(() => { /* A falha de inicialização é exibida pela tela que aguarda SDR.ready. */ });
      return () => { cancelled = true; if (subscription) subscription.unsubscribe(); };
    },

    async getScheduleSettings() {
      const row = await query(c => c.from('schedule_settings').select('*').eq('id', 1).maybeSingle(), 'Não foi possível carregar a disponibilidade. Tente de novo.');
      return Object.assign(clone(scheduleDefaults), row || {});
    },
    async saveScheduleSettings(patch) {
      return query(c => c.from('schedule_settings').upsert(Object.assign({}, patch, { id: 1 })).select('*').single(), 'Não foi possível salvar a disponibilidade. Tente de novo.');
    },
    async getAgentConfig() {
      const row = await query(c => c.from('agent_config').select('*').eq('id', 1).maybeSingle(), 'Não foi possível carregar a configuração do agente. Tente de novo.');
      return Object.assign(clone(agentDefaults), row || {});
    },
    async saveAgentConfig(patch) {
      return query(c => c.from('agent_config').upsert(Object.assign({}, patch, { id: 1 })).select('*').single(), 'Não foi possível salvar a configuração do agente. Tente de novo.');
    },
    async listClosers() {
      return (await query(c => c.from('closers').select('*').order('name'), 'Não foi possível carregar os responsáveis. Tente de novo.')) || [];
    },
    async saveCloser(obj) { return save('closers', obj, 'Não foi possível salvar o responsável. Tente de novo.'); },
    async deleteCloser(id) { return remove('closers', id, 'Não foi possível excluir o responsável. Tente de novo.'); },
    async listRules(closerId) {
      return (await query(c => {
        let request = c.from('availability_rules').select('*');
        if (closerId) request = request.eq('closer_id', closerId);
        return request.order('weekday').order('start_time');
      }, 'Não foi possível carregar as regras de horário. Tente de novo.')) || [];
    },
    async saveRule(obj) { return save('availability_rules', obj, 'Não foi possível salvar a regra de horário. Tente de novo.'); },
    async deleteRule(id) { return remove('availability_rules', id, 'Não foi possível excluir a regra de horário. Tente de novo.'); },
    async listBlocks() {
      const recent = new Date(Date.now() - 30 * 86400000).toISOString();
      return (await query(c => c.from('blocks').select('*').gte('ends_at', recent).order('starts_at'), 'Não foi possível carregar os bloqueios. Tente de novo.')) || [];
    },
    async saveBlock(obj) { return save('blocks', obj, 'Não foi possível salvar o bloqueio. Tente de novo.'); },
    async deleteBlock(id) { return remove('blocks', id, 'Não foi possível excluir o bloqueio. Tente de novo.'); },

    async listAppointments(fromISO, toISO, closerId) {
      // O servidor corta em 1000 linhas por consulta sem erro: pagina com range até esgotar.
      const PAGE = 1000, all = [];
      // O teto do servidor (max-rows) pode ser menor que PAGE: avança pelas linhas recebidas e para só com página vazia.
      for (let start = 0; ;) {
        const page = (await query(c => {
          let request = c.from('appointments').select(appointmentSelect).gte('starts_at', fromISO).lt('starts_at', toISO);
          if (closerId) request = request.eq('closer_id', closerId);
          return request.order('starts_at').order('id').range(start, start + PAGE - 1);
        }, 'Não foi possível carregar a agenda. Tente de novo.')) || [];
        if (!page.length) break;
        all.push(...page);
        start += page.length;
      }
      return all;
    },
    async setAppointmentStatus(id, status, extra) {
      const allowed = ['agendado', 'confirmado', 'feito', 'no_show', 'reagendado', 'cancelado', 'venda'];
      if (!allowed.includes(status)) throw friendly('Status de agendamento inválido.');
      let saleValue = null;
      if (extra && extra.sale_value !== undefined && extra.sale_value !== null) {
        const raw = typeof extra.sale_value === 'string' ? extra.sale_value.trim() : extra.sale_value;
        if (raw !== '') {
          if (typeof raw === 'string' && !/^\d+([.,]\d{1,2})?$/.test(raw)) throw friendly('Informe um valor de venda válido.');
          saleValue = typeof raw === 'string' ? Number(raw.replace(',', '.')) : Number(raw);
          if (!Number.isFinite(saleValue) || saleValue < 0) throw friendly('Informe um valor de venda válido.');
        }
      }
      // Vazio vira null (o banco preserva o valor atual); venda só com valor > 0.
      if (status === 'venda' && !(saleValue > 0)) throw friendly('Informe o valor da venda (maior que zero).');
      // Transição atômica no banco: status, valor, estágio do lead e evento de funil numa transação só.
      const result = await query(c => c.rpc('set_appointment_status', { p_appointment: id, p_status: status, p_sale_value: saleValue }), 'Não foi possível atualizar o agendamento. Tente de novo.');
      if (!result || !result.ok) {
        throw friendly(businessMessages[result && result.error] || 'Não foi possível atualizar o agendamento. Atualize a agenda e tente de novo.');
      }
      // A escrita principal já foi confirmada: se só a releitura falhar, não devolve erro (o operador repetiria a ação).
      try {
        const fresh = await query(c => c.from('appointments').select(appointmentSelect).eq('id', id).single(), 'Não foi possível recarregar o agendamento.');
        if (fresh) return fresh;
      } catch (_) { /* cai no retorno mínimo abaixo */ }
      return { id: result.appointment_id || id, status: result.status || status, lead: { stage: result.lead_stage }, reloaded: false };
    },
    async availableSlots(fromISO, toISO, closerId) {
      return (await query(c => c.rpc('available_slots', { p_from: fromISO, p_to: toISO, p_closer: closerId || null }), 'Não foi possível consultar os horários. Tente de novo.')) || [];
    },
    async rescheduleAppointment(id, newStartsAtISO, closerId) {
      // Atômico no banco: se o novo horário falhar, o antigo continua como estava (sem rollback no navegador).
      const result = await query(c => c.rpc('reschedule_slot', {
        p_appointment: id, p_starts_at: newStartsAtISO, p_closer: closerId || null
      }), 'Não foi possível reagendar. Atualize a agenda e confira o agendamento.');
      return requireBooking(result);
    },
    async manualBook(leadId, startsAtISO, closerId, notes) { return book(leadId, startsAtISO, closerId, notes); },
    async dashboardStats(fromISO, toISO, closerId) {
      return query(c => c.rpc('dashboard_stats', { p_from: fromISO, p_to: toISO, p_closer: closerId || null }), 'Não foi possível carregar os indicadores. Tente de novo.');
    },

    async listLeads(options) {
      const opts = options || {};
      return (await query(c => {
        let request = c.from('leads').select(leadSelect);
        if (opts.stage) request = request.eq('stage', opts.stage);
        if (opts.search && String(opts.search).trim()) {
          // Valores entre aspas protegem vírgulas e parênteses da gramática do PostgREST.
          const search = String(opts.search).trim().replace(/\\/g, '\\\\').replace(/"/g, '\\"').replace(/[%_*]/g, '\\$&');
          const pattern = '"*' + search + '*"';
          request = request.or('name.ilike.' + pattern + ',phone.ilike.' + pattern + ',email.ilike.' + pattern);
        }
        return request.order('updated_at', { ascending: false }).limit(limit(opts.limit, 200));
      }, 'Não foi possível carregar os contatos. Tente de novo.')) || [];
    },
    async getLead(id) {
      return query(c => c.from('leads').select(leadSelect + ',appointments(*,closer:closers(id,name,color)),objections(*)').eq('id', id).single(), 'Não foi possível carregar este contato. Atualize a lista e tente de novo.');
    },
    async updateLead(id, patch) {
      // Só os campos que o painel de fato edita; opt_out nunca volta a false por aqui.
      const safe = SDR.sanitizeLeadPatch(patch);
      if (!Object.keys(safe).length) throw validation('Nada para atualizar neste contato.');
      return query(c => c.from('leads').update(safe).eq('id', id).select(leadSelect).single(), 'Não foi possível atualizar o contato. Tente de novo.');
    },
    async reativarLead(id) {
      // Ação explícita e confirmada pelo dono: o único caminho que devolve opt_out a false.
      return query(c => c.from('leads').update({ opt_out: false }).eq('id', id).select(leadSelect).single(), 'Não foi possível permitir mensagens para este contato. Tente de novo.');
    },

    async listConversations(options) {
      const opts = options || {}, size = limit(opts.limit, 50);
      // Sem GREATEST no PostgREST: pega os N mais recentes por entrada e por saída,
      // junta e reordena pela atividade mais recente. Mensagens só são lidas por lead aberto.
      const fetchBy = column => query(c => {
        let request = c.from('leads').select(leadSelect).not(column, 'is', null);
        if (opts.onlyTakeover) request = request.gt('takeover_until', new Date().toISOString());
        return request.order(column, { ascending: false }).limit(size);
      }, 'Não foi possível carregar as conversas. Tente de novo.');
      const [byInbound, byOutbound] = await Promise.all([fetchBy('last_inbound_at'), fetchBy('last_outbound_at')]);
      const unique = new Map();
      (byInbound || []).concat(byOutbound || []).forEach(lead => unique.set(lead.id, lead));
      const activity = lead => Math.max(Date.parse(lead.last_inbound_at) || 0, Date.parse(lead.last_outbound_at) || 0);
      return Array.from(unique.values()).sort((a, b) => activity(b) - activity(a)).slice(0, size)
        .map(lead => Object.assign({}, lead, { lastMessage: null }));
    },
    async listMessages(leadId, count) {
      const rows = (await query(c => c.from('messages').select('*').eq('lead_id', leadId).order('created_at', { ascending: false }).order('id', { ascending: false }).limit(limit(count, 200)), 'Não foi possível carregar as mensagens. Tente de novo.')) || [];
      return rows.reverse();
    },
    async setTakeover(leadId, on) {
      return api.updateLead(leadId, { takeover_until: on ? new Date(Date.now() + 24 * 3600000).toISOString() : null });
    },
    async sendMessage(leadId, phone, body, clientKey) {
      if (!String(body || '').trim()) throw validation('Escreva uma mensagem antes de enviar.');
      // test_only vale também para o painel: fora dos números de teste, nada entra na fila.
      const agent = await api.getAgentConfig();
      if (agent.test_only !== false) {
        const allowed = Array.isArray(agent.test_phones) ? agent.test_phones : [];
        if (!allowed.includes(phone)) throw validation('O modo teste está ligado e este número não está na lista de testes. Libere a produção ou inclua o número nos testes antes de enviar.');
      }
      const key = clientKey || SDR.uuid();
      try {
        return await query(c => c.from('outbox').insert({ lead_id: leadId, phone, body, created_by: 'painel', client_key: key }).select('*').single(), 'Não foi possível colocar a mensagem na fila. Tente de novo.');
      } catch (error) {
        // 23505 na client_key: o insert anterior já foi gravado; é o mesmo envio, não uma falha.
        if (error && error.sdrDuplicateKey) return { duplicate: true, client_key: key };
        throw error;
      }
    },
    async findOutboxByKey(leadId, clientKey) {
      // Descobre se o insert de um envio foi gravado (resposta perdida): devolve a linha ou null.
      const rows = await query(c => c.from('outbox').select('id,status,client_key').eq('lead_id', leadId).eq('client_key', clientKey).limit(1), 'Não foi possível conferir a fila de mensagens.');
      return Array.isArray(rows) && rows.length ? rows[0] : null;
    },
    async listOutbox(leadId, count) {
      return (await query(c => {
        let request = c.from('outbox').select('*');
        if (leadId) request = request.eq('lead_id', leadId);
        return request.order('created_at', { ascending: false }).limit(limit(count, 100));
      }, 'Não foi possível carregar a fila de mensagens. Tente de novo.')) || [];
    },
    async listObjections(options) {
      const opts = options || {};
      return (await query(c => {
        let request = c.from('objections').select('*,lead:leads(name,phone)');
        if (opts.category) request = request.eq('category', opts.category);
        if (opts.from) request = request.gte('created_at', opts.from);
        if (opts.to) request = request.lte('created_at', opts.to);
        return request.order('created_at', { ascending: false }).limit(limit(opts.limit, 100));
      }, 'Não foi possível carregar as objeções. Tente de novo.')) || [];
    },
    async setObjectionHandled(id, handled) {
      return query(c => c.from('objections').update({ handled: Boolean(handled) }).eq('id', id).select('*,lead:leads(name,phone)').single(), 'Não foi possível atualizar a objeção. Tente de novo.');
    },

    async getWhatsappConnection() {
      // Linha única (id = 1) escrita pelo motor. A tabela só existe depois da migration do WhatsApp.
      try {
        const selected = await getClient(false);
        // Idades e validade do QR vêm calculadas pelo relógio do BANCO (função whatsapp_status).
        const result = await selected.rpc('whatsapp_status');
        if (result.error) throw result.error;
        const data = result.data;
        return data && typeof data === 'object' && data.id != null ? data : null;
      } catch (error) {
        const code = String(error && error.code || '');
        if (code === 'PGRST205' || code === '42P01' || code === '42883' || code === 'PGRST202') throw friendly('O WhatsApp pelo painel ainda não está disponível no seu banco. Peça ao Claude Code para aplicar as migrations (etapa_banco.py migrar).');
        throw translate(error, 'Não foi possível carregar o estado do WhatsApp. Tente de novo.');
      }
    },
    // Checagem explícita de acesso do painel (função usada pela RLS). Qualquer falha em rodar = não autorizado.
    async isPanelUser() {
      const selected = await getClient(false);
      const check = await selected.rpc('is_panel_user');
      if (check.error) {
        const code = String(check.error.code || check.status || '');
        if (['401', '403', '42501', 'PGRST301'].includes(code) || [401, 403].includes(Number(check.status || check.error.status))) return false;
        throw friendly('Não foi possível verificar a autorização. Tente de novo.');
      }
      return check.data === true;
    },
    async requestWhatsappCommand(command) {
      if (!['connect', 'logout'].includes(command)) throw validation('Comando inválido.');
      try {
        const selected = await getClient(false);
        const result = await selected.rpc('request_whatsapp_command', { p_command: command });
        if (result.error) throw result.error;
        const data = result.data;
        if (!data || typeof data.ok !== 'boolean') throw friendly('Não foi possível enviar o pedido ao motor. Tente de novo.');
        return data;
      } catch (error) {
        const text = String(error && error.message || '');
        if (String(error && error.code || '') === '42501' || text.includes('sem_permissao')) throw friendly('Sua sessão não tem permissão para isso. Entre de novo com a conta do dono do painel.');
        throw translate(error, 'Não foi possível enviar o pedido ao motor. Tente de novo.');
      }
    },

    async publicSlots(slug, fromISO, toISO) {
      return (await query(c => c.rpc('public_slots', { p_slug: slug, p_from: fromISO, p_to: toISO }), 'Não foi possível carregar os horários. Tente de novo.', true)) || [];
    },
    async publicBook(details) {
      const result = await query(c => c.rpc('public_book', {
        p_slug: details.slug, p_name: details.name, p_email: details.email,
        p_whatsapp: details.whatsapp, p_starts_at: details.startsAt, p_hp: details.hp || ''
      }), 'Não foi possível confirmar o agendamento. Confira sua conexão e tente de novo.', true);
      if (result && typeof result.ok === 'boolean') return result;
      throw friendly('Não foi possível confirmar o agendamento. Tente de novo.');
    }
  };

  SDR.apiLive = api;
})(typeof window !== 'undefined' ? window : globalThis);
