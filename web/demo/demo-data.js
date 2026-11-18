(function (root) {
  'use strict';
  // Todos os contatos e resultados abaixo são fictícios. As datas acompanham o dia atual.
  const tz = 'America/Sao_Paulo';
  const now = new Date();
  const uid = (kind, n) => '00000000-0000-4000-8000-' + String(kind * 1000 + n).padStart(12, '0');
  function at(offset, hour, minute) {
    const values = {};
    new Intl.DateTimeFormat('en-CA', {timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit'}).formatToParts(now).forEach(p => { values[p.type] = p.value; });
    const date = new Date(Date.UTC(+values.year, +values.month - 1, +values.day + offset));
    if (root.SDR && root.SDR.tzTools) return root.SDR.tzTools.zonedToUtc(date.getUTCFullYear(), date.getUTCMonth() + 1, date.getUTCDate(), hour, minute || 0, tz).toISOString();
    return new Date(Date.UTC(date.getUTCFullYear(), date.getUTCMonth(), date.getUTCDate(), hour + 3, minute || 0)).toISOString();
  }
  function businessOffset(offset) {
    let day = offset;
    while ([0, 6].includes(new Date(at(day, 12)).getUTCDay())) day++;
    return day;
  }
  const names = ['Beatriz Lima', 'Bruno Costa', 'Clara Gomes', 'Diego Martins', 'Elisa Rocha', 'Felipe Sousa', 'Gabriela Dias', 'Hugo Barros', 'Inês Alves', 'João Ribeiro', 'Lara Mendes', 'Lucas Teixeira', 'Luísa Cardoso', 'Mariana Lopes', 'Mateus Pinto', 'Nina Azevedo', 'Otávio Melo', 'Patrícia Neves', 'Pedro Reis', 'Renata Nunes', 'Rita Fonseca', 'Sofia Duarte', 'Tiago Leite', 'Valentina Cruz', 'Vera Matos', 'Davi Moreira', 'Yasmin Freitas', 'Zélia Carvalho'];
  const stages = ['novo', 'respondeu', 'qualificado', 'desqualificado', 'horarios_ofertados', 'agendado', 'confirmado', 'compareceu', 'no_show', 'reagendado', 'venda', 'perdido'];
  const closers = [
    {id: uid(1, 1), name: 'Alice Martins', whatsapp: '+5511900000091', email: 'alice@exemplo.com', active: true, weight: 2, color: '#D97706'},
    {id: uid(1, 2), name: 'Daniel Oliveira', whatsapp: '+5511900000092', email: 'daniel@exemplo.com', active: true, weight: 1, color: '#818CF8'},
    {id: uid(1, 3), name: 'Helena Duarte', whatsapp: '+5511900000093', email: 'helena@exemplo.com', active: true, weight: 1, color: '#9CA3AF'}
  ];
  const leads = names.map((name, i) => ({
    id: uid(2, i + 1), name, phone: i >= 26 ? '+3519123400' + String(i).padStart(2, '0') : '+55119000000' + String(i + 1).padStart(2, '0'),
    email: 'contato' + (i + 1) + '@exemplo.com', source: i % 3 === 0 ? 'pagina' : 'whatsapp', stage: stages[i % stages.length],
    profile: i % 2 ? 'Busca um atendimento com horário flexível.' : 'Quer conhecer as opções de atendimento.',
    qualification: {objetivo: i % 2 ? 'Organizar os próximos cuidados.' : 'Entender como funciona o atendimento.', disponibilidade: 'Durante a semana', prioridade: i % 3 ? 'Este mês' : 'Nas próximas semanas'},
    score: 30 + (i * 7 % 70), disqualify_reason: i % stages.length === 3 ? 'Fora da região de atendimento' : null,
    takeover_until: i === 1 ? new Date(now.getTime() + 18 * 3600000).toISOString() : null,
    closer_id: closers[i % 3].id, opt_out: i === 11, followup_step: i % 3,
    last_inbound_at: i < 8 ? new Date(now.getTime() - (i + 1) * 27 * 60000).toISOString() : null,
    last_outbound_at: i < 8 ? new Date(now.getTime() - (i + 1) * 27 * 60000 + 60000).toISOString() : null,
    created_at: at(-20 + Math.floor(i / 2), 8), updated_at: new Date(now.getTime() - i * 3600000).toISOString()
  }));
  const appointments = Array.from({length: 16}, (_, i) => {
    const past = i < 8;
    const offset = past ? -10 + i : businessOffset(i < 12 ? 1 + Math.floor((i - 8) / 2) : 7 + Math.floor((i - 12) / 2));
    const status = past ? ['feito', 'no_show', 'venda', 'feito', 'venda', 'cancelado', 'reagendado', 'no_show'][i] : i % 2 ? 'confirmado' : 'agendado';
    const start = at(offset, i % 2 ? 15 : 10, i % 3 === 0 ? 30 : 0);
    const lead = leads[i + 4];
    lead.closer_id = closers[i % 3].id;
    if (status !== 'cancelado') lead.stage = ({feito: 'compareceu', venda: 'venda', no_show: 'no_show', reagendado: 'reagendado'})[status] || status;
    // Conserva exemplos de todos os estágios mesmo após associar os agendamentos.
    return {id: uid(3, i + 1), lead_id: lead.id, closer_id: closers[i % 3].id, starts_at: start, ends_at: new Date(Date.parse(start) + 30 * 60000).toISOString(), status,
      sale_value: status === 'venda' ? (i === 2 ? 850 : 1200) : null, source: ['agente', 'pagina', 'manual'][i % 3], notes: past ? 'Registro fictício de atendimento.' : 'Conversa inicial de demonstração.', created_at: at(offset - 2, 9), updated_at: at(offset - 2, 9)};
  });
  stages.forEach((stage, i) => { if (!leads.some(lead => lead.stage === stage)) leads[27 - i].stage = stage; });
  const messages = [];
  const exchanges = [
    ['Olá! Gostaria de saber como funciona.', 'Olá! Posso ajudar. Qual período é melhor para você?'],
    ['Tenho uma dúvida sobre os horários.', 'Claro, estou verificando as opções com você.'],
    ['Prefiro conversar durante a tarde.', 'Temos opções à tarde. Quer escolher um horário?'],
    ['Vocês atendem na minha região?', 'Vamos conferir a região antes de agendar.'],
    ['Estou comparando algumas opções.', 'Podemos esclarecer suas dúvidas em uma conversa inicial.'],
    ['Já escolhi meu horário.', 'Seu agendamento está registrado.'],
    ['Obrigada pela atenção.', 'Será um prazer receber você.'],
    ['Posso reagendar minha conversa?', 'Sim. Vamos encontrar um novo horário disponível.']
  ];
  exchanges.forEach((pair, i) => pair.forEach((body, j) => messages.push({id: messages.length + 1, lead_id: leads[i].id, direction: j ? 'out' : 'in', author: j ? (i === 1 ? 'dono' : 'agente') : 'lead', body, media_type: 'text', wa_id: null, created_at: new Date(now.getTime() - (i + 1) * 27 * 60000 + j * 60000).toISOString()})));
  const categories = ['preco', 'tempo', 'confianca', 'concorrente', 'decisor', 'vou pensar', 'sem_orcamento'];
  const excerpts = ['Preciso entender o investimento primeiro.', 'Minha agenda está apertada esta semana.', 'Quero conhecer melhor o atendimento.', 'Também estou olhando outra clínica.', 'Preciso conversar com minha família.', 'Vou pensar com calma antes de decidir.', 'Neste momento não tenho orçamento.'];
  const objections = Array.from({length: 20}, (_, i) => ({id: i + 1, lead_id: leads[i % leads.length].id, category: categories[i % 7], excerpt: excerpts[i % 7], handled: i % 3 !== 0, created_at: at(-Math.floor(i / 3), 11)}));
  root.SDR_DEMO_DATA = {
    scheduleSettings: {id: 1, public_slug: 'demo', public_enabled: true, call_minutes: 30, buffer_minutes: 10, slot_step_minutes: 30, min_notice_hours: 2, days_ahead: 14, timezone: tz, distribution: 'rodizio'},
    agentConfig: {id: 1, agent_name: 'Ana', company: 'Clínica Exemplo', tone: 'Acolhedor e objetivo', niche: 'saúde', offer: 'Conversa inicial para conhecer o atendimento', knowledge: '# Atendimento\nExplique as etapas com clareza e confirme a disponibilidade.\nA conversa inicial acontece no horário escolhido.',
      qualification_questions: [{key: 'objetivo', pergunta: 'O que você busca no atendimento?', obrigatoria: true}, {key: 'disponibilidade', pergunta: 'Qual período funciona melhor para você?', obrigatoria: true}, {key: 'prioridade', pergunta: 'Quando pretende começar?', obrigatoria: false}],
      disqualify_rules: [{key: 'regiao', regra: 'Fora da região atendida', mensagem: 'No momento, nossa região de atendimento é limitada.'}, {key: 'urgencia', regra: 'Solicitação de emergência', mensagem: 'Procure o serviço de urgência da sua região.'}],
      objection_playbook: categories.map((categoria, i) => ({categoria, sinais: [excerpts[i]], resposta: 'Entendo sua dúvida. Podemos conversar sobre as opções antes de você decidir.'})),
      followup_messages: [{apos_horas: 24, texto: 'Olá! Ficou alguma dúvida sobre o atendimento?'}, {apos_horas: 72, texto: 'Se quiser conversar, podemos encontrar um horário para você.'}],
      confirm_template: 'Olá {nome}! Sua conversa está confirmada para {data} às {hora}.', reminder_24h_template: 'Olá {nome}! Lembrete da sua conversa amanhã, às {hora}.', reminder_2h_template: 'Sua conversa será às {hora}. Até breve!', owner_phone: '', test_only: true, test_phones: [], ai_provider: 'gemini', paused: false, followup_window_start: '08:00', followup_window_end: '20:00'},
    closers, rules: closers.flatMap((closer, index) => [1, 2, 3, 4, 5].flatMap(weekday => [{id: uid(4, index * 10 + weekday * 2), closer_id: closer.id, weekday, start_time: index === 1 && weekday === 3 ? '10:00:00' : '09:00:00', end_time: '12:00:00'}, {id: uid(4, index * 10 + weekday * 2 + 1), closer_id: closer.id, weekday, start_time: '14:00:00', end_time: index === 2 && weekday === 5 ? '17:00:00' : '18:00:00'}])),
    blocks: [{id: uid(5, 1), closer_id: null, starts_at: at(businessOffset(10), 0), ends_at: at(businessOffset(10) + 1, 0), reason: 'Folga de demonstração'}],
    // Número fictício e QR de demonstração (padrão quadriculado, não é legível por celular).
    whatsapp: {demo_number: '5511900000000', demo_qr: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAMgAAADIAQAAAACFI5MzAAAAgklEQVR42u2XSQ7AMAgD+f+nqbrJQJreB2ElF+YU2bKC2S13nSwa8VPmQs8ES67rkmlKJuG0IME5NnmTKOdqRlGkWb+lF1p4M54UQUnuiOQfkvgWQcnkbfI2eZu8Td4YeVt/QnXfY5Fl01MUmaRuemoKNPlwDk1+nGORTWFQSa9+OwBTvVtcQhk4ggAAAABJRU5ErkJggg=='},
    leads, appointments, messages, objections,
    outbox: [0, 1, 2].map((i) => ({id: i + 1, lead_id: leads[i].id, phone: leads[i].phone, body: ['Seu horário está reservado.', 'Podemos esclarecer suas dúvidas.', 'Escolha um horário disponível.'][i], status: ['enviado', 'pendente', 'erro'][i], attempts: i === 2 ? 1 : 0, last_error: i === 2 ? 'Falha simulada na demonstração.' : null, wa_key_id: null, created_by: 'painel', created_at: at(0, 8), sent_at: i === 0 ? at(0, 8, 1) : null})),
    funnel_events: leads.flatMap((lead, i) => {
      const paths = {
        novo: ['novo'], respondeu: ['novo', 'respondeu'], qualificado: ['novo', 'respondeu', 'qualificado'],
        desqualificado: ['novo', 'respondeu', 'desqualificado'], perdido: ['novo', 'respondeu', 'perdido']
      };
      const qualified = ['novo', 'respondeu', 'qualificado', 'horarios_ofertados'];
      let path = paths[lead.stage];
      if (!path) {
        path = qualified.slice();
        if (lead.stage !== 'horarios_ofertados') path.push('agendado');
        if (['confirmado', 'compareceu', 'no_show', 'venda'].includes(lead.stage)) path.push('confirmado');
        if (['compareceu', 'venda'].includes(lead.stage)) path.push('compareceu');
        if (!path.includes(lead.stage)) path.push(lead.stage);
      }
      const start = Date.parse(lead.created_at), end = Date.parse(lead.updated_at);
      return path.map((stage, j) => ({id: i * 20 + j + 1, lead_id: lead.id, stage, created_at: new Date(start + (end - start) * (path.length === 1 ? 0 : j / (path.length - 1))).toISOString()}));
    })
  };
}(typeof window !== 'undefined' ? window : globalThis));
