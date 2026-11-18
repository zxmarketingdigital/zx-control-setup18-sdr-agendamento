// Testes da fatia web (node puro, sem navegador): node tests/web/test_web.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const web = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'web');
function load(files, extra) {
  const ctx = Object.assign({ console, URL, Intl, Date, JSON, Math, Promise, setTimeout, clearTimeout, SDR_CONFIG: { demo: false } }, extra || {});
  ctx.globalThis = ctx; ctx.window = ctx;
  vm.createContext(ctx);
  files.forEach(f => vm.runInContext(fs.readFileSync(path.join(web, f), 'utf8'), ctx, { filename: f }));
  return ctx;
}
let failed = 0;
async function test(name, fn) {
  try { await fn(); console.log('ok   ' + name); } catch (e) { failed++; console.log('FAIL ' + name + '\n' + (e.stack || e)); }
}

// Cliente falso: registra cada chamada encadeada e devolve 'rows' por tabela.
function fakeClient(tables) {
  const log = [];
  const client = { log, from(table) {
    const calls = { table, ops: [] };
    log.push(calls);
    const b = new Proxy({}, { get(_, op) {
      if (op === 'then') return resolve => { const rows = tables[table]; const data = typeof rows === 'function' ? rows(calls) : rows; resolve({ data, error: null }); };
      return (...args) => { calls.ops.push([op, ...args]); return b; };
    } });
    return b;
  } };
  return client;
}
const has = (calls, op, ...args) => calls.ops.some(o => o[0] === op && args.every((a, i) => JSON.stringify(o[i + 1]) === JSON.stringify(a)));

const common = ['js/common.js'];

await test('toE164: número já com o código do país não é prefixado de novo', () => {
  const { SDR } = load(common);
  assert.equal(SDR.phone.toE164('5511999990000', '55'), '+5511999990000');
  assert.equal(SDR.phone.toE164('551133334444', '55'), '+551133334444');
  assert.equal(SDR.phone.toE164('11999990000', '55'), '+5511999990000');
  assert.equal(SDR.phone.toE164('(11) 99999-0000', '55'), '+5511999990000');
  assert.equal(SDR.phone.toE164('55999990000', '55'), '+5555999990000'); // DDD 55, sem código
  assert.equal(SDR.phone.toE164('5555999990000', '55'), '+5555999990000');
  assert.equal(SDR.phone.toE164('551199990000', '55'), '+551199990000'); // 12 dígitos fixo com DDI
  assert.equal(SDR.phone.toE164('999990000', '55'), null); // 9 dígitos: falta DDD
  assert.equal(SDR.phone.toE164('551199999000012', '55'), null);
  assert.equal(SDR.phone.toE164('0011999990000', '55'), '+11999990000');
  // r8: sem '+', dígitos que já começam com o DDI do país escolhido são ambíguos => inválidos (nunca '+351351...')
  assert.equal(SDR.phone.toE164('3912345678', '39'), null);
  assert.equal(SDR.phone.toE164('351912345678', '351'), null);
  assert.equal(SDR.phone.toE164('12025550123', '1'), null);
  assert.equal(SDR.phone.toE164('+351912345678', '351'), '+351912345678');
  assert.equal(SDR.phone.toE164('+12025550123', '1'), '+12025550123');
  assert.equal(SDR.phone.toE164('2025550123', '1'), '+12025550123');
  assert.equal(SDR.phone.hasDialPrefix('351912345678', '351'), true);
  assert.equal(SDR.phone.hasDialPrefix('+351912345678', '351'), false);
  assert.equal(SDR.phone.toE164('912345678', '351'), '+351912345678');
  assert.equal(SDR.phone.toE164('+5511999990000', '351'), '+5511999990000');
});

const liveFiles = ['js/common.js', 'js/api-live.js'];
await test('listConversations: limitada no servidor, sem consultar mensagens', async () => {
  const { SDR } = load(liveFiles);
  const mk = (id, inb, out) => ({ id, last_inbound_at: inb, last_outbound_at: out });
  const client = fakeClient({ leads: calls => has(calls, 'not', 'last_inbound_at', 'is', null)
    ? [mk('a', '2026-01-02T00:00:00Z', null), mk('b', '2026-01-01T00:00:00Z', '2026-01-05T00:00:00Z')]
    : [mk('b', '2026-01-01T00:00:00Z', '2026-01-05T00:00:00Z'), mk('c', null, '2026-01-03T00:00:00Z')] });
  SDR.apiLive.init(client, client);
  const rows = await SDR.apiLive.listConversations({ limit: 3 });
  assert.equal(rows.map(r => r.id).join(','), 'b,c,a');
  assert.equal(client.log.some(c => c.table === 'messages'), false);
  assert.ok(client.log.every(c => has(c, 'limit', 3)));
  const none = await SDR.apiLive.listConversations({ limit: 1 });
  assert.equal(none.length, 1);
});

await test('listObjections: período vai ao servidor e limite é capado em 1000', async () => {
  const { SDR } = load(liveFiles);
  const client = fakeClient({ objections: [] });
  SDR.apiLive.init(client, client);
  await SDR.apiLive.listObjections({ limit: 10000, from: '2026-01-01T00:00:00Z', to: '2026-02-01T00:00:00Z' });
  const call = client.log[0];
  assert.ok(has(call, 'gte', 'created_at', '2026-01-01T00:00:00Z'));
  assert.ok(has(call, 'lte', 'created_at', '2026-02-01T00:00:00Z'));
  assert.ok(has(call, 'limit', 1000));
});

await test('status do agendamento: RPC atômica set_appointment_status, sem update manual em leads/appointments', async () => {
  const { SDR } = load(liveFiles);
  const client = rpcClient({ ok: true, appointment_id: 'p1', status: 'venda', lead_stage: 'venda' }, { appointments: { id: 'p1', lead_id: 'l1', status: 'venda', lead: { id: 'l1', stage: 'venda' } } });
  SDR.apiLive.init(client, client);
  const appt = await SDR.apiLive.setAppointmentStatus('p1', 'venda', { sale_value: 1500 });
  assert.equal(JSON.stringify(client.rpcs[0]), JSON.stringify(['set_appointment_status', { p_appointment: 'p1', p_status: 'venda', p_sale_value: 1500 }]));
  assert.equal(appt.lead.stage, 'venda');
  assert.ok(!client.log.some(c => c.ops.some(o => o[0] === 'update')), 'nenhuma escrita fora da RPC');
  const bad = rpcClient({ ok: false, error: 'agendamento_inexistente' });
  SDR.apiLive.init(bad, bad);
  await assert.rejects(() => SDR.apiLive.setAppointmentStatus('x', 'feito'), /não encontrado/);
});

await test('status do agendamento: releitura que falha depois da RPC ok NÃO lança erro', async () => {
  const { SDR } = load(liveFiles);
  const client = rpcClient({ ok: true, appointment_id: 'p1', status: 'no_show', lead_stage: 'no_show' });
  client.from = () => { throw new Error('network fetch failed'); };
  SDR.apiLive.init(client, client);
  const appt = await SDR.apiLive.setAppointmentStatus('p1', 'no_show');
  assert.equal(appt.status, 'no_show'); assert.equal(appt.lead.stage, 'no_show');
});

await test('demo: cancelar volta o lead para qualificado e objeções filtram período', async () => {
  const ctx = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']);
  const api = ctx.SDR.apiDemo; api.init();
  const to = new Date(Date.now() + 90 * 86400000).toISOString(), from = new Date(Date.now() + 80 * 86400000).toISOString();
  assert.equal((await api.listObjections({ from, to })).length, 0);
  const all = await api.listObjections({ limit: 5000 });
  assert.ok(all.length > 0);
  const appts = await api.listAppointments(new Date(Date.now() - 400 * 86400000).toISOString(), new Date(Date.now() + 400 * 86400000).toISOString());
  const target = appts.find(a => ['agendado', 'confirmado'].includes(a.status));
  assert.ok(target);
  const result = await api.setAppointmentStatus(target.id, 'cancelado');
  assert.equal(result.lead.stage === 'qualificado' || (await api.getLead(target.lead_id)).stage === 'qualificado', true);
});

await test('safeUrl: esquema com TAB/LF/espaço/controle não burla; só http, https, mailto, tel e relativos', () => {
  const { SDR } = load(common);
  ['javascript:alert(1)', 'java\tscript:alert(1)', 'java\nscript:alert(1)', ' \u0001javascript:x', 'JaVaScRiPt:x', 'data:text/html,x', 'vbscript:x', 'ja&#x76;ascript:x'.replace('&#x76;', 'v'), 'java\u0000script:x', 'file:///etc/passwd']
    .forEach(v => assert.equal(SDR.safeUrl(v), false, JSON.stringify(v)));
  ['https://a.com', 'HTTP://a.com', 'mailto:a@b.co', 'tel:+5511999990000', '/agendar/x', 'agenda.html?x=1', '#topo'].forEach(v => assert.equal(SDR.safeUrl(v), true, v));
});

await test('requireConfigOrExplain: demo:true com supabaseUrl é recusado', () => {
  const ok = load(common, { SDR_CONFIG: { demo: true } });
  assert.equal(ok.SDR.requireConfigOrExplain(null), true);
  const bad = load(common, { SDR_CONFIG: { demo: true, supabaseUrl: 'https://x.supabase.co', supabaseAnonKey: 'k' } });
  assert.equal(bad.SDR.requireConfigOrExplain(null), false);
});

function rpcClient(rpcResult, tables) {
  const base = fakeClient(tables || {});
  base.rpcs = [];
  base.rpc = (name, args) => { base.rpcs.push([name, args]); return Promise.resolve({ data: typeof rpcResult === 'function' ? rpcResult(name, args) : rpcResult, error: null }); };
  return base;
}

await test('reagendar: usa o RPC reschedule_slot e não toca appointments no cliente', async () => {
  const { SDR } = load(liveFiles);
  const client = rpcClient({ ok: true, appointment_id: 'n1', old_appointment_id: 'p1', starts_at: '2026-01-01T10:00:00Z' });
  SDR.apiLive.init(client, client);
  const r = await SDR.apiLive.rescheduleAppointment('p1', '2026-01-01T10:00:00Z', 'c1');
  assert.equal(r.old_appointment_id, 'p1');
  assert.equal(JSON.stringify(client.rpcs[0]), JSON.stringify(['reschedule_slot', { p_appointment: 'p1', p_starts_at: '2026-01-01T10:00:00Z', p_closer: 'c1' }]));
  assert.equal(client.log.length, 0, 'sem update/select manual');
  const fail = rpcClient({ ok: false, error: 'slot_indisponivel' });
  SDR.apiLive.init(fail, fail);
  await assert.rejects(() => SDR.apiLive.rescheduleAppointment('p1', 'x'), /ocupado/);
  assert.equal(fail.log.length, 0);
});

await test('envio manual: test_only bloqueia fora de test_phones; permitido leva client_key; 23505 é sucesso', async () => {
  const { SDR } = load(liveFiles);
  let inserted = null, insertError = null;
  const client = fakeClient({ agent_config: { id: 1, test_only: true, test_phones: ['+5511999990000'] }, outbox: calls => { const ins = calls.ops.find(o => o[0] === 'insert'); if (ins) inserted = ins[1]; return { id: 1 }; } });
  SDR.apiLive.init(client, client);
  await assert.rejects(() => SDR.apiLive.sendMessage('l1', '+5511888880000', 'oi'), /modo teste/);
  assert.equal(inserted, null);
  await SDR.apiLive.sendMessage('l1', '+5511999990000', 'oi', 'k-1');
  assert.equal(inserted.client_key, 'k-1'); assert.equal(inserted.created_by, 'painel');
  // duplicata
  const dup = fakeClient({ agent_config: { id: 1, test_only: false }, outbox: null });
  const orig = dup.from.bind(dup);
  dup.from = table => { const b = orig(table); if (table !== 'outbox') return b; const px = new Proxy({}, { get(_, op) { if (op === 'then') return resolve => resolve({ data: null, error: { code: '23505', message: 'dup' } }); return () => px; } }); return px; };
  SDR.apiLive.init(dup, dup);
  const r = await SDR.apiLive.sendMessage('l1', '+5511888880000', 'oi', 'k-2');
  assert.equal(r.duplicate, true);
});

await test('excluir: zero linhas afetadas (RLS) vira erro', async () => {
  const { SDR } = load(liveFiles);
  const none = fakeClient({ blocks: [] });
  SDR.apiLive.init(none, none);
  await assert.rejects(() => SDR.apiLive.deleteBlock('b1'), /não foi removido/);
  const one = fakeClient({ blocks: [{ id: 'b1' }] });
  SDR.apiLive.init(one, one);
  await SDR.apiLive.deleteBlock('b1');
});

await test('demo: reagendar é atômico, envio respeita test_only, página pública limita e saneia nome', async () => {
  const ctx = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']);
  const api = ctx.SDR.apiDemo; api.init();
  const from = new Date().toISOString(), to = new Date(Date.now() + 14 * 86400000).toISOString();
  const appts = await api.listAppointments(new Date(Date.now() - 400 * 86400000).toISOString(), new Date(Date.now() + 400 * 86400000).toISOString());
  const target = appts.find(a => ['agendado', 'confirmado'].includes(a.status) && new Date(a.starts_at) > new Date());
  if (target) {
    await assert.rejects(() => api.rescheduleAppointment(target.id, '2000-01-01T00:00:00Z'));
    const after = (await api.listAppointments(new Date(Date.now() - 400 * 86400000).toISOString(), new Date(Date.now() + 400 * 86400000).toISOString())).find(a => a.id === target.id);
    assert.equal(after.status, target.status, 'falha não deixa o antigo reagendado');
  }
  const lead = (await api.listLeads({})).find(l => l.phone);
  await assert.rejects(() => api.sendMessage(lead.id, '+5511000000001', 'oi'), /modo teste/);
  const settings = await api.getScheduleSettings();
  assert.equal(settings.public_max_per_hour, 20); assert.equal(settings.public_max_per_ip_day, 3);
  await api.saveScheduleSettings({ public_max_per_hour: 1 });
  const slots = await api.publicSlots(settings.public_slug, from, to);
  const base = { slug: settings.public_slug, email: 'a@b.co', hp: '' };
  const r1 = await api.publicBook(Object.assign({ name: 'Ana http://spam.com www.x.com', whatsapp: '+5511911110001', startsAt: slots[0].starts_at }, base));
  assert.equal(r1.ok, true);
  const msg = (await api.listOutbox(null, 1000)).find(o => o.created_by === 'pagina');
  assert.ok(!/http|www/.test(msg.body), msg.body);
  const r2 = await api.publicBook(Object.assign({ name: 'Bia', whatsapp: '+5511911110002', startsAt: slots[1].starts_at }, base));
  assert.equal(r2.error, 'limite_atingido');
});

await test('envio: busca na outbox por client_key e marca erro de validação', async () => {
  const { SDR } = load(liveFiles);
  const client = fakeClient({ outbox: calls => has(calls, 'eq', 'client_key', 'k-9') ? [{ id: 5, client_key: 'k-9' }] : [] });
  SDR.apiLive.init(client, client);
  assert.equal((await SDR.apiLive.findOutboxByKey('l1', 'k-9')).id, 5);
  const none = fakeClient({ outbox: [] });
  SDR.apiLive.init(none, none);
  assert.equal(await SDR.apiLive.findOutboxByKey('l1', 'k-9'), null);
  const blocked = fakeClient({ agent_config: { id: 1, test_only: true, test_phones: [] } });
  SDR.apiLive.init(blocked, blocked);
  await assert.rejects(() => SDR.apiLive.sendMessage('l1', '+5511888880000', 'oi'), e => e.sdrValidation === true);
});

await test('demo página pública: limite por telefone conta TENTATIVAS e confirmação não leva o nome', async () => {
  const ctx = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']);
  const api = ctx.SDR.apiDemo; api.init();
  const settings = await api.getScheduleSettings();
  const slots = await api.publicSlots(settings.public_slug, new Date().toISOString(), new Date(Date.now() + 14 * 86400000).toISOString());
  const base = { slug: settings.public_slug, email: 'a@b.co', hp: '' };
  const phone = '+5511922220001';
  const r1 = await api.publicBook(Object.assign({ name: 'Maria', whatsapp: phone, startsAt: slots[0].starts_at }, base));
  assert.equal(r1.ok, true);
  const msg = (await api.listOutbox(null, 1000)).find(o => o.created_by === 'pagina' && o.phone === phone);
  assert.ok(!/Maria/.test(msg.body), 'nome do lead fora da mensagem: ' + msg.body);
  const second = await api.publicBook(Object.assign({ name: 'Maria', whatsapp: phone, startsAt: slots[1].starts_at }, base));
  assert.equal(second.error, 'ja_agendado');
  const third = await api.publicBook(Object.assign({ name: 'Maria', whatsapp: phone, startsAt: slots[2].starts_at }, base));
  assert.equal(third.error, 'ja_agendado');
  const fourth = await api.publicBook(Object.assign({ name: 'Maria', whatsapp: phone, startsAt: slots[3].starts_at }, base));
  assert.equal(fourth.error, 'limite_atingido', 'a 4a tentativa do mesmo telefone no dia é barrada mesmo sem sucesso');
});

await test('demo página pública: opt_out recusa; desqualificado agenda sem WhatsApp e mantém estágio', async () => {
  const ctx = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']);
  const api = ctx.SDR.apiDemo; api.init();
  const settings = await api.getScheduleSettings();
  const slots = await api.publicSlots(settings.public_slug, new Date().toISOString(), new Date(Date.now() + 14 * 86400000).toISOString());
  const base = { slug: settings.public_slug, email: 'a@b.co', hp: '' };
  const leads = await api.listLeads({});
  const free = leads.filter(l => l.phone && !['agendado', 'confirmado'].includes(l.stage));
  const optOut = free[0], dq = free[1];
  await api.updateLead(optOut.id, { opt_out: true });
  const r = await api.publicBook(Object.assign({ name: 'Zed', whatsapp: optOut.phone, startsAt: slots[0].starts_at }, base));
  assert.equal(r.error, 'indisponivel');
  const before = (await api.listOutbox(null, 1000)).length;
  await api.updateLead(dq.id, { stage: 'desqualificado' });
  const r2 = await api.publicBook(Object.assign({ name: 'Zed', whatsapp: dq.phone, startsAt: slots[1].starts_at }, base));
  assert.equal(r2.ok, true);
  assert.equal((await api.getLead(dq.id)).stage, 'desqualificado');
  assert.equal((await api.listOutbox(null, 1000)).length, before, 'nada enfileirado');
});

// ---- Harness de DOM mínimo para renderizar views sem navegador ----
function stubEl(preset) {
  const el = Object.assign({ innerHTML: '', value: '', hidden: false, disabled: false, textContent: '', scrollTop: 0, scrollHeight: 0, clientHeight: 0, checked: false, dataset: {}, handlers: {}, children: {}, isConnected: true }, preset || {});
  el.classList = { add() {}, remove() {}, toggle() {} };
  el.addEventListener = (type, fn) => { el.handlers[type] = fn; };
  el.removeEventListener = () => {};
  el.setAttribute = () => {}; el.removeAttribute = () => {}; el.focus = () => {}; el.select = () => {};
  el.contains = () => true;
  el.querySelector = sel => (el.children[sel] = el.children[sel] || stubEl());
  el.querySelectorAll = () => [];
  return el;
}
function viewCtx(files, config) {
  const doc = stubEl(); doc.hidden = false; doc.activeElement = null; doc.body = stubEl();
  const ctx = load(files, { document: doc, setInterval: () => 0, clearInterval() {}, SDR_CONFIG: Object.assign({ demo: true }, config || {}) });
  return ctx;
}
const tick = (n = 10) => new Promise(r => { let i = 0; const go = () => (++i >= n ? r() : setTimeout(go, 0)); go(); });

await test('api-live: valor de venda vazio vira null (nunca 0), venda exige valor > 0, texto inválido é recusado', async () => {
  const { SDR } = load(liveFiles);
  const calls = [];
  const client = { from() { return { select() { return this; }, eq() { return this; }, single: async () => ({ data: { id: 'a' }, error: null }) }; }, rpc: async (name, args) => { calls.push(args); return { data: { ok: true, appointment_id: 'a', status: args.p_status }, error: null }; } };
  SDR.apiLive.init(client, client);
  await SDR.apiLive.setAppointmentStatus('a', 'feito', { sale_value: '   ' });
  assert.equal(calls[0].p_sale_value, null);
  await SDR.apiLive.setAppointmentStatus('a', 'feito', { sale_value: '' });
  assert.equal(calls[1].p_sale_value, null);
  await assert.rejects(() => SDR.apiLive.setAppointmentStatus('a', 'venda', { sale_value: '' }), /valor da venda/);
  await assert.rejects(() => SDR.apiLive.setAppointmentStatus('a', 'venda', { sale_value: 0 }), /valor da venda/);
  await assert.rejects(() => SDR.apiLive.setAppointmentStatus('a', 'venda'), /valor da venda/);
  await assert.rejects(() => SDR.apiLive.setAppointmentStatus('a', 'feito', { sale_value: '12abc' }), /valor de venda válido/);
  await SDR.apiLive.setAppointmentStatus('a', 'venda', { sale_value: '1500,50' });
  assert.equal(calls[2].p_sale_value, 1500.5);
  assert.equal(calls.length, 3);
});

await test('agendar.js: ja_agendado no retry após falha de rede NUNCA mostra "confirmado"', async () => {
  const doc = stubEl(); doc.hidden = false; doc.activeElement = null; doc.body = stubEl();
  const root = stubEl(); const nodes = {};
  root.querySelector = sel => (nodes[sel] = nodes[sel] || stubEl());
  doc.querySelector = sel => sel === '#agendamento' ? root : root.querySelector(sel);
  const ctx = load(['js/common.js'], { document: doc, setInterval: () => 0, clearInterval() {}, location: { pathname: '/agendar/minha-pagina' }, SDR_CONFIG: { demo: false } });
  const SDR = ctx.SDR;
  const slot = new Date(Date.now() + 3 * 86400000).toISOString();
  let n = 0;
  SDR.ready = Promise.resolve(); SDR.beacon = () => {}; SDR.requireConfigOrExplain = () => true;
  SDR.api = { publicSlots: async () => [{ starts_at: slot, ends_at: new Date(Date.parse(slot) + 1800000).toISOString() }], publicBook: async () => { if (++n === 1) throw new Error('rede caiu'); return { ok: false, error: 'ja_agendado' }; } };
  vm.runInContext(fs.readFileSync(path.join(web, 'js/agendar.js'), 'utf8'), ctx, { filename: 'js/agendar.js' });
  await tick(20);
  const form = { id: 'booking-form' };
  nodes['#name'] = stubEl({ value: 'Maria Silva' }); nodes['#email'] = stubEl({ value: 'maria@example.com' }); nodes['#phone'] = stubEl({ value: '11911110001' }); nodes['#country'] = stubEl({ value: 'BR' }); nodes['#website'] = stubEl({ value: '' }); nodes['#confirm'] = stubEl();
  const submit = () => root.handlers.submit({ target: form, preventDefault() {} });
  const clickBtn = dataset => root.handlers.click({ target: { closest: () => ({ dataset, id: dataset.id, disabled: false }) } });
  clickBtn({ slot }); clickBtn({ id: 'continue' });
  await submit(); await tick(10);
  await submit(); await tick(10);
  assert.equal(n, 2, 'duas tentativas chegaram ao publicBook');
  assert.ok(!/Agendamento confirmado/.test(root.innerHTML), 'não exibe confirmado');
  assert.ok(/já tem um horário marcado/.test(root.innerHTML) && /WhatsApp/.test(root.innerHTML));
});

await test('Visão geral: taxa 0.5 renderiza 50% (fração, nunca 5.000%/10.000%) e nulo vira traço', async () => {
  const ctx = viewCtx(['js/common.js', 'js/views/visao.js']);
  const mk = stats => { const root = stubEl(); root.children['#vg-period'] = stubEl({ value: '30' }); const results = stubEl(); root.children['#vg-results'] = results;
    const c = { state: { tz: 'America/Sao_Paulo', closers: [] }, navigate() {}, api: { dashboardStats: async () => stats, listAppointments: async () => [] } };
    return ctx.SDR.views.visao.render(root, c).then(async () => { await tick(); return results.innerHTML; }); };
  const html = await mk({ leads: 10, qualificados: 4, agendados: 2, taxa_qualificacao: 0.4, taxa_agendamento: 0.5, taxa_comparecimento: 1 });
  assert.ok(html.includes('50%'), 'taxa_agendamento 0.5 => 50%');
  assert.ok(html.includes('100%') && html.includes('40%'));
  assert.ok(!/5\.000|10\.000|4\.000/.test(html), 'sem milhares de percentual');
  const nul = await mk({ leads: 0, taxa_qualificacao: null, taxa_agendamento: null, taxa_comparecimento: null });
  assert.ok(!/>0%</.test(nul), 'nulo não vira 0%');
  assert.ok(nul.includes('—'));
});

await test('demo: taxa_agendamento = agendados/qualificados em fração; sem base vira null', async () => {
  const ctx = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']);
  const api = ctx.SDR.apiDemo; api.init();
  const st = await api.dashboardStats(new Date(Date.now() - 400 * 86400000).toISOString(), new Date(Date.now() + 400 * 86400000).toISOString());
  assert.ok(st.qualificados > 0);
  assert.equal(st.taxa_agendamento, Math.round(st.agendados / st.qualificados * 1000) / 1000);
  const empty = await api.dashboardStats('2000-01-01T00:00:00Z', '2000-01-02T00:00:00Z');
  assert.equal(empty.taxa_agendamento, null);
  assert.equal(ctx.SDR.fmt.pct(null), '—');
  assert.equal(ctx.SDR.fmt.pct(0), '0%');
});

await test('agenda: lead_ja_agendado do banco vira texto amigável; demo também recusa segunda call ativa', async () => {
  const { SDR } = load(liveFiles);
  const client = { from() { throw new Error('não deve ler'); }, rpc: async () => ({ data: { ok: false, error: 'lead_ja_agendado' }, error: null }) };
  SDR.apiLive.init(client, client);
  await assert.rejects(() => SDR.apiLive.setAppointmentStatus('x', 'agendado'), e => /já tem um agendamento ativo/.test(e.message) && !/lead_ja_agendado/.test(e.message));
  const ctx = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']);
  const api = ctx.SDR.apiDemo; api.init();
  const appts = await api.listAppointments(new Date(Date.now() - 400 * 86400000).toISOString(), new Date(Date.now() + 400 * 86400000).toISOString());
  const active = appts.find(a => ['agendado', 'confirmado'].includes(a.status));
  const slots = await api.availableSlots(new Date().toISOString(), new Date(Date.now() + 14 * 86400000).toISOString());
  // cria uma segunda call para o mesmo lead via cancelamento + novo agendamento, depois tenta reativar a cancelada
  await api.setAppointmentStatus(active.id, 'cancelado');
  const booked = await api.manualBook(active.lead_id, slots.find(s => s.starts_at).starts_at, null, '');
  assert.equal(booked.ok, true);
  await assert.rejects(() => api.setAppointmentStatus(active.id, 'agendado'), /já tem um agendamento ativo/);
});

await test('listAppointments: pagina com range até esgotar (sem corte silencioso em 1000)', async () => {
  const { SDR } = load(liveFiles);
  const pages = [1000, 1000, 5, 0];
  const seen = [];
  const client = fakeClient({ appointments: calls => { const r = calls.ops.find(o => o[0] === 'range'); seen.push([r[1], r[2]]); return Array.from({ length: pages[seen.length - 1] }, (_, i) => ({ id: seen.length + '-' + i })); } });
  SDR.apiLive.init(client, client);
  const rows = await SDR.apiLive.listAppointments('2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z');
  assert.equal(rows.length, 2005);
  assert.deepEqual(seen, [[0, 999], [1000, 1999], [2000, 2999], [2005, 3004]]);
});

await test('r19 listAppointments: teto do servidor menor que a página (500) não corta em silêncio', async () => {
  const { SDR } = load(liveFiles);
  const TOTAL = 600, CAP = 500;
  const client = fakeClient({ appointments: calls => { const r = calls.ops.find(o => o[0] === 'range'); const from = r[1]; const n = Math.max(0, Math.min(CAP, TOTAL - from, r[2] - r[1] + 1)); return Array.from({ length: n }, (_, i) => ({ id: 'a' + (from + i) })); } });
  SDR.apiLive.init(client, client);
  const rows = await SDR.apiLive.listAppointments('2026-01-01T00:00:00Z', '2026-02-01T00:00:00Z');
  assert.equal(rows.length, 600);
  assert.equal(new Set(rows.map(r => r.id)).size, 600);
});

await test('r19 tz: startOfDay/startOfWeek quando 00:00 não existe (Santiago, 2026-09-06)', async () => {
  const { SDR } = load(['js/common.js']);
  const tz = 'America/Santiago';
  const noon = SDR.tzTools.zonedToUtc(2026, 9, 6, 12, 0, tz);
  let day;
  assert.doesNotThrow(() => { day = SDR.tzTools.startOfDay(noon, tz); });
  const p = SDR.tzTools.parts(day, tz);
  assert.deepEqual([p.y, p.m, p.d, p.hh, p.mm], [2026, 9, 6, 1, 0]);
  assert.doesNotThrow(() => SDR.tzTools.startOfWeek(SDR.tzTools.zonedToUtc(2026, 9, 6, 12, 0, tz), tz));
  const monday = SDR.tzTools.startOfWeek(SDR.tzTools.zonedToUtc(2026, 9, 8, 12, 0, tz), tz);
  const q = SDR.tzTools.parts(monday, tz);
  assert.deepEqual([q.d, q.hh], [7, 0]);
  const sp = SDR.tzTools.startOfDay(new Date('2026-05-10T15:00:00Z'), 'America/Sao_Paulo');
  assert.equal(sp.toISOString(), '2026-05-10T03:00:00.000Z');
});

const U1 = '11111111-1111-4111-8111-111111111111';
function conversasHarness(api, config) {
  const ctx = viewCtx(['js/common.js', 'js/views/conversas.js'], Object.assign({ sendConfirmDelaysMs: [0, 0, 0] }, config));
  const root = stubEl(), toasts = [];
  const lead = { id: U1, name: 'Ana', phone: '+5511999990000', stage: 'respondeu', takeover_until: new Date(Date.now() + 3600000).toISOString(), opt_out: false };
  const c = { state: { tz: 'America/Sao_Paulo' }, toast: (m, k) => toasts.push([m, k]), confirm: async () => true, openLead() {}, params: { get: () => U1 },
    api: Object.assign({ mode: 'live', listConversations: async () => [], listMessages: async () => [], listOutbox: async () => [], getLead: async () => lead, setTakeover: async () => ({}) }, api) };
  return ctx.SDR.views.conversas.render(root, c).then(async cleanup => {
    await tick(20);
    const input = root.children['.cv-message-input'], status = root.children['.cv-send-status'], composer = root.children['.cv-composer'];
    return { cleanup, root, doc: ctx.document, click: dataset => root.handlers.click({ target: { closest: () => ({ dataset, disabled: false }) } }), input, status, composer, send: async text => { input.value = text; await composer.handlers.submit({ preventDefault() {} }); await tick(5); }, toasts, lead };
  });
}

await test('conversas: falha ambígua do insert NUNCA vira "não foi enviada"; mantém takeover e orienta conferir', async () => {
  const calls = [];
  const h = await conversasHarness({ sendMessage: async () => { throw new Error('Não foi possível conectar.'); }, findOutboxByKey: async () => null, setTakeover: async (id, on) => { calls.push(on); } });
  await h.send('oi');
  assert.ok(/Não confirmamos o envio/.test(h.status.innerHTML), h.status.innerHTML);
  assert.ok(!/não foi enviada/i.test(h.status.innerHTML));
  assert.ok(!calls.includes(false), 'takeover não foi devolvido ao agente');
  h.cleanup();
});

await test('conversas: "Descartar e escrever outra" limpa o campo e o rascunho (sem reenvio duplicado)', async () => {
  const h = await conversasHarness({ sendMessage: async () => { throw new Error('timeout'); }, findOutboxByKey: async () => null });
  await h.send('Olá, segue o link');
  assert.ok(/Não confirmamos o envio/.test(h.status.innerHTML));
  h.input.value = 'Olá, segue o link';
  await h.click({ cvAction: 'discard-send' }); await tick(5);
  assert.equal(h.input.value, '');
  h.cleanup();
});

await test('agendar.js: DDI digitado sem + mostra a mensagem própria no formulário e na preview', () => {
  const agendar = fs.readFileSync(path.join(web, 'js/agendar.js'), 'utf8');
  assert.ok(/Esse número parece já ter o código do país\. Digite só o número local ou comece com \+\./.test(agendar));
  assert.ok((agendar.match(/DIAL_MSG/g) || []).length >= 3);
});

await test('conversas: a linha aparece na 2a consulta com backoff => tratada como enfileirada', async () => {
  let n = 0;
  const h = await conversasHarness({ sendMessage: async () => { throw new Error('timeout'); }, findOutboxByKey: async () => (++n >= 2 ? { id: 1, status: 'pendente' } : null) });
  await h.send('oi');
  assert.equal(n, 2);
  assert.ok(/foi enviada para a fila/.test(h.status.innerHTML) && /Não envie de novo/.test(h.status.innerHTML), h.status.innerHTML);
  h.cleanup();
});

await test('conversas: setTakeover ambíguo relê o lead e, se já assumido, segue com o envio', async () => {
  const lead = { id: U1, name: 'Ana', phone: '+5511999990000', stage: 'respondeu', takeover_until: null, opt_out: false };
  let reads = 0, sent = 0;
  const h = await conversasHarness({ getLead: async () => { reads++; return reads >= 3 ? Object.assign({}, lead, { takeover_until: new Date(Date.now() + 3600000).toISOString() }) : lead; },
    setTakeover: async () => { throw new Error('rede'); }, sendMessage: async () => { sent++; return { id: 1 }; } });
  await h.send('oi');
  assert.equal(sent, 1, 'a gravação do takeover tinha acontecido: envia');
  h.cleanup();
});

await test('conversas: prévia = autor + 80 caracteres sem quebra de linha', async () => {
  const long = 'linha um\nlinha dois ' + 'x'.repeat(200);
  const ctx = viewCtx(['js/common.js', 'js/views/conversas.js']);
  const root = stubEl();
  const c = { state: { tz: 'America/Sao_Paulo' }, toast() {}, confirm: async () => true, openLead() {}, params: { get: () => null },
    api: { mode: 'live', listMessages: async () => [], listOutbox: async () => [], getLead: async () => ({}), listConversations: async () => [
      { id: U1, name: 'Ana', stage: 'novo', last_inbound_at: new Date().toISOString(), lastMessage: { body: long, author: 'agente', created_at: new Date().toISOString() } },
      { id: '22222222-2222-4222-8222-222222222222', name: 'Bia', stage: 'novo', last_inbound_at: new Date().toISOString(), lastMessage: { body: 'oi', author: 'dono', created_at: new Date().toISOString() } }] } };
  const cleanup = await ctx.SDR.views.conversas.render(root, c); await tick(20);
  const html = root.children['.cv-list'].innerHTML;
  assert.ok(html.includes('Agente: linha um linha dois '), html.slice(0, 600));
  assert.ok(html.includes('Você: oi'));
  const preview = /cv-preview">([^<]*)</.exec(html)[1];
  assert.ok(Array.from(preview).length <= 'Agente: '.length + 80 && preview.endsWith('…') && !preview.includes('\n'));
  cleanup();
});

await test('disponibilidade: retry de releitura pós-escrita redesenha a tela sem a faixa removida', async () => {
  const ctx = viewCtx(['js/common.js', 'js/views/disponibilidade.js']);
  const root = stubEl();
  const block = { id: 'b1', closer_id: null, reason: 'Feriado Teste', starts_at: new Date(Date.now() + 86400000).toISOString(), ends_at: new Date(Date.now() + 2 * 86400000).toISOString() };
  let reads = 0;
  const c = { state: { tz: 'America/Sao_Paulo', settings: { timezone: 'America/Sao_Paulo' }, closers: [] }, toast() {}, navigate() {}, confirm: async () => true,
    api: { listRules: async () => [], listBlocks: async () => { reads++; if (reads === 1) return [block]; if (reads === 2) throw new Error('rede'); return []; }, deleteBlock: async () => {} } };
  const cleanup = ctx.SDR.views.disponibilidade.render(root, c);
  await tick(20);
  const click = async dataset => root.handlers.click({ target: { closest: () => ({ dataset, disabled: false, isConnected: true, click() {} }) } });
  await click({ dpTab: 'bloqueios' });
  assert.ok(root.innerHTML.includes('Feriado Teste'));
  await click({ dpAction: 'delete-block', id: 'b1' }); await tick(10);
  assert.ok(root.children['#dp-notice'].innerHTML.includes('Tentar de novo'), 'oferece nova tentativa');
  await click({ dpAction: 'retry-operation' }); await tick(10);
  assert.equal(reads, 3);
  assert.ok(!root.innerHTML.includes('Feriado Teste'), 'a tela foi redesenhada sem o bloqueio removido');
  (await cleanup)();
});

const b64u = obj => Buffer.from(JSON.stringify(obj)).toString('base64url');
const jwt = role => 'eyJhbGciOiJIUzI1NiJ9.' + b64u({ iss: 'supabase', ref: 'abc', role }) + '.assinatura_falsa';
await test('requireConfigOrExplain: service_role, sb_secret_ e JWT ilegível são recusados; anon e publishable passam', () => {
  const cfg = key => load(common, { SDR_CONFIG: { demo: false, supabaseUrl: 'https://x.supabase.co', supabaseAnonKey: key } }).SDR;
  assert.equal(cfg(jwt('anon')).requireConfigOrExplain(null), true);
  assert.equal(cfg(jwt('authenticated')).requireConfigOrExplain(null), true);
  assert.equal(cfg('sb_publishable_abc123').requireConfigOrExplain(null), true);
  ['service_role', 'SERVICE_ROLE'].forEach(role => assert.equal(cfg(jwt(role)).requireConfigOrExplain(null), false, role));
  ['sb_secret_abc123', 'abc', 'a.b.c', jwt('anon').replace(/\.[^.]*\./, '.@@@.'), 'eyJhbGciOiJIUzI1NiJ9.' + b64u({ ref: 'x' }) + '.s'].forEach(key => assert.equal(cfg(key).requireConfigOrExplain(null), false, key));
  const bad = cfg(jwt('service_role')), root = stubEl();
  assert.equal(bad.requireConfigOrExplain(root), false);
  assert.equal(bad.configProblem, 'chave');
  assert.ok(/service_role/.test(root.innerHTML) && /anon\/publishable/.test(root.innerHTML), root.innerHTML);
});

await test('initConnection: chave service_role nunca cria cliente Supabase', async () => {
  let created = 0;
  const ctx = load(['js/common.js', 'js/api-live.js', 'js/api.js'], { SDR_CONFIG: { demo: false, supabaseUrl: 'https://x.supabase.co', supabaseAnonKey: jwt('service_role') }, supabase: { createClient() { created++; return {}; } }, document: undefined });
  await assert.rejects(() => ctx.SDR.ready, /chave errada/);
  assert.equal(created, 0);
});

await test('fuso inválido nunca vira UTC em silêncio: mostra traço', () => {
  const { SDR } = load(common);
  assert.equal(SDR.fmt.time('2026-01-01T15:00:00Z', 'Foo/Bar'), '—');
  assert.equal(SDR.fmt.time('2026-01-01T15:00:00Z', 'America/Sao_Paulo'), '12:00');
});

await test('agendar.js: tela não promete WhatsApp de forma absoluta e SDR.initConnection existe', () => {
  const agendar = fs.readFileSync(path.join(web, 'js/agendar.js'), 'utf8');
  assert.ok(!/Você vai receber a confirmação no WhatsApp/.test(agendar));
  assert.ok(/Se o atendimento pelo WhatsApp estiver ativo, a confirmação chega por lá/.test(agendar));
  assert.ok(!/enviamos|também vai/.test(agendar));
  const apiSrc = fs.readFileSync(path.join(web, 'js/api.js'), 'utf8');
  assert.ok(/sha384-[A-Za-z0-9+\/]{64}/.test(apiSrc) && /crossOrigin = 'anonymous'/.test(apiSrc));
  assert.ok(/SDR\.initConnection = function/.test(fs.readFileSync(path.join(web, 'js/api.js'), 'utf8')));
});

await test('conversas: envio incerto de A não vaza o texto para o composer de B', async () => {
  const U2 = '22222222-2222-4222-8222-222222222222';
  const h = await conversasHarness({ sendMessage: async () => { await tick(25); throw new Error('timeout'); }, findOutboxByKey: async () => null,
    getLead: async id => ({ id, name: 'X', phone: '+5511999990000', stage: 'respondeu', takeover_until: new Date(Date.now() + 3600000).toISOString(), opt_out: false }) }, { sendConfirmDelaysMs: [60, 60] });
  h.input.value = 'texto de A';
  const sending = h.composer.handlers.submit({ preventDefault() {} });
  await tick(8);
  h.click({ cvLead: U2 }); await tick(5);
  await sending; await tick(5);
  assert.notEqual(h.input.value, 'texto de A', 'texto de A não pode aparecer no composer de B');
  h.cleanup();
});

await test('conversas: envio incerto trava o texto e o retry reusa a MESMA client_key com o texto original', async () => {
  const sent = [];
  const h = await conversasHarness({ sendMessage: async (id, phone, body, key) => { sent.push([body, key]); if (sent.length === 1) throw new Error('timeout'); return { id: 1 }; }, findOutboxByKey: async () => null });
  await h.send('Oi, o horário é 15h');
  assert.ok(/Não confirmamos o envio/.test(h.status.innerHTML) && /Descartar e escrever outra/.test(h.status.innerHTML), h.status.innerHTML);
  assert.equal(h.input.disabled, true, 'texto travado');
  h.input.value = 'Oi, o horário é 15h30';
  await h.click({ cvAction: 'retry-send' }); await tick(10);
  assert.equal(sent.length, 2);
  assert.equal(sent[1][1], sent[0][1], 'mesma client_key');
  assert.equal(sent[1][0], 'Oi, o horário é 15h', 'texto original, não o editado');
  h.cleanup();
});

await test('conversas: retry de envio incerto consulta a chave anterior e, se gravou, NÃO envia de novo', async () => {
  let sends = 0, finds = 0;
  const h = await conversasHarness({ sendMessage: async () => { sends++; throw new Error('timeout'); }, findOutboxByKey: async () => (++finds > 3 ? { id: 1, status: 'pendente' } : null) });
  await h.send('oi');
  assert.equal(sends, 1);
  await h.click({ cvAction: 'retry-send' }); await tick(10);
  assert.equal(sends, 1, 'achou a linha: sem segundo envio');
  assert.ok(/já estava na fila/.test(h.status.innerHTML), h.status.innerHTML);
  assert.equal(h.input.disabled, false);
  h.cleanup();
});

await test('conversas: "descartar e escrever outra" destrava o texto e gera chave nova', async () => {
  const keys = [];
  const h = await conversasHarness({ sendMessage: async (id, phone, body, key) => { keys.push(key); if (keys.length === 1) throw new Error('timeout'); return { id: 1 }; }, findOutboxByKey: async () => null });
  await h.send('primeira');
  assert.equal(h.input.disabled, true);
  await h.click({ cvAction: 'discard-send' }); await tick(10);
  assert.equal(h.input.disabled, false);
  await h.send('segunda');
  assert.equal(keys.length, 2);
  assert.notEqual(keys[1], keys[0], 'chave nova só após descarte explícito');
  h.cleanup();
});

await test('conversas: falha pontual do polling mantém o contato e o formulário', async () => {
  let fail = false;
  const lead = { id: U1, name: 'Ana', phone: '+5511999990000', stage: 'respondeu', takeover_until: new Date(Date.now() + 3600000).toISOString(), opt_out: false };
  const h = await conversasHarness({ getLead: async () => { if (fail) throw new Error('rede'); return lead; } });
  assert.equal(h.composer.hidden, false);
  fail = true;
  await h.doc.handlers.visibilitychange(); await tick(20);
  assert.equal(h.composer.hidden, false, 'formulário continua visível');
  assert.equal(h.input.disabled, false);
  h.cleanup();
});

// ---- WhatsApp: conexão pelo painel ----
function waHarness(row, extra) {
  const doc = stubEl(); doc.hidden = false; doc.activeElement = null; doc.body = stubEl();
  const timers = new Map(); let next = 1;
  const fakeSet = (fn, ms) => { const id = next++; timers.set(id, { fn, ms }); return id; };
  const fakeClear = id => { timers.delete(id); };
  const ctx = load(['js/common.js', 'js/views/whatsapp.js'], { document: doc, setTimeout: fakeSet, clearTimeout: fakeClear, SDR_CONFIG: { demo: true } });
  const root = stubEl(), body = stubEl(); root.children['#wa-body'] = body;
  const requests = [];
  const api = { mode: 'live', getWhatsappConnection: async () => (typeof row === 'function' ? row() : row), requestWhatsappCommand: async c => { requests.push(c); return { ok: true, command_id: 'x' }; } };
  const c = Object.assign({ api, confirm: async () => true, navigate() {}, toast() {} }, extra || {});
  return { ctx, root, body, timers, requests, c, render: () => ctx.SDR.views.whatsapp.render(root, c) };
}
const waRow = over => Object.assign({ id: 1, state: 'close', owner_number: null, qr_base64: null, pairing_code: null, qr_expires_at: null, command: null, command_status: null, command_error: null, checked_age_seconds: 5, qr_valid: false, qr_remaining_seconds: null }, over || {});
const PNG_OK = 'data:image/png;base64,iVBORw0KGgo=';

await test('whatsapp: a view existe, está na lista de rotas e carregada no index', () => {
  const painel = fs.readFileSync(path.join(web, 'js/painel.js'), 'utf8');
  assert.ok(/\['whatsapp', 'WhatsApp',/.test(painel), 'rota whatsapp no painel.js');
  assert.ok(/js\/views\/whatsapp\.js/.test(fs.readFileSync(path.join(web, 'index.html'), 'utf8')), 'script no index.html');
  const h = waHarness(waRow());
  assert.equal(typeof h.ctx.SDR.views.whatsapp.render, 'function');
  assert.equal(h.ctx.SDR.views.whatsapp.title, 'WhatsApp');
  assert.ok(!/seu Mac/i.test(fs.readFileSync(path.join(web, 'js/views/whatsapp.js'), 'utf8')));
});

await test('whatsapp: QR que não é data URL PNG base64 nunca vira <img>', async () => {
  const { SDR } = waHarness(waRow()).ctx;
  [PNG_OK, 'data:image/png;base64,AAAA'].forEach(v => assert.equal(SDR.safeQrSrc(v), v));
  ['data:image/svg+xml;base64,PHN2Zz4=', 'data:image/png;base64,AA"onerror="x', 'data:image/png;base64,AA AA', 'data:image/png;base64,AAA<script>', 'javascript:alert(1)', 'https://x.example/qr.png', 'data:image/png;base64,', '', null, 42]
    .forEach(v => assert.equal(SDR.safeQrSrc(v), null, String(v)));
  for (const bad of ['data:image/svg+xml;base64,PHN2Zz4=', 'data:image/png;base64,AA"onerror="alert(1)']) {
    const h = waHarness(waRow({ command_status: 'aguardando_leitura', qr_base64: bad, qr_valid: true, qr_remaining_seconds: 30 }));
    const cleanup = await h.render();
    assert.ok(!/<img/.test(h.body.innerHTML), 'não renderiza imagem inválida');
    assert.ok(/QR recebido não é válido/.test(h.body.innerHTML));
    cleanup();
  }
  const good = waHarness(waRow({ command_status: 'aguardando_leitura', qr_base64: PNG_OK, pairing_code: 'ABCD-1234', qr_valid: true, qr_remaining_seconds: 30 }));
  const cleanup = await good.render();
  assert.ok(good.body.innerHTML.includes('src="' + PNG_OK + '"'));
  assert.ok(/Aparelhos conectados/.test(good.body.innerHTML) && /ABCD-1234/.test(good.body.innerHTML));
  cleanup();
});

await test('whatsapp: command_error é escapado e motor parado desabilita os botões', async () => {
  const evil = '<img src=x onerror=alert(1)>';
  const h = waHarness(waRow({ command_status: 'erro', command_error: evil }));
  const cleanup = await h.render();
  assert.ok(!h.body.innerHTML.includes(evil) && h.body.innerHTML.includes('&lt;img src=x'), 'erro escapado');
  assert.ok(/Gerar QR novo/.test(h.body.innerHTML));
  cleanup();
  for (const checked_age_seconds of [null, undefined, -5, 600, 'abc', NaN]) {
    const stale = waHarness(waRow({ checked_age_seconds }));
    const done = await stale.render();
    assert.ok(/O motor não está respondendo/.test(stale.body.innerHTML) && /\/sdr-agendamento/.test(stale.body.innerHTML));
    assert.equal((stale.body.innerHTML.match(/ disabled/g) || []).length, 2, 'os dois botões desabilitados');
    done();
  }
  const open = waHarness(waRow({ state: 'open', owner_number: '5511900000000' }));
  const done = await open.render();
  assert.ok(/Conectado/.test(open.body.innerHTML) && /\+55 11 90000-0000/.test(open.body.innerHTML));
  assert.ok(!/ disabled/.test(open.body.innerHTML));
  done();
});

await test('whatsapp: polling de 3 s com comando em andamento, 15 s sem, e é limpo no dispose', async () => {
  let row = waRow({ command_status: 'pendente' });
  const h = waHarness(() => row);
  const cleanup = await h.render();
  assert.equal([...h.timers.values()].filter(t => t.ms === 3000).length, 1);
  assert.ok(/Pedindo o QR ao motor/.test(h.body.innerHTML));
  row = waRow({ state: 'open', command_status: 'concluido', owner_number: '5511900000000' });
  const [id, t] = [...h.timers.entries()][0]; h.timers.delete(id); await t.fn(); await tick(5);
  assert.equal([...h.timers.values()].filter(t2 => t2.ms === 15000).length, 1, 'sem comando: 15 s');
  assert.equal(h.timers.size, 1);
  cleanup();
  assert.equal(h.timers.size, 0, 'nenhum timer vivo depois do dispose');
  // com QR na tela, o contador de 1 s também é limpo
  row = waRow({ command_status: 'aguardando_leitura', qr_base64: PNG_OK, qr_valid: true, qr_remaining_seconds: 30 });
  const h2 = waHarness(() => row);
  const c2 = await h2.render();
  assert.ok([...h2.timers.values()].some(t3 => t3.ms === 1000));
  c2();
  assert.equal(h2.timers.size, 0);
});

await test('whatsapp: Trocar número pede confirmação; Reconectar chama connect; erro da RPC vira aviso', async () => {
  const h = waHarness(waRow({ state: 'open', owner_number: '5511900000000' }));
  const confirms = [];
  h.c.confirm = async o => { confirms.push(o.message); return false; };
  const cleanup = await h.render();
  const click = action => h.root.handlers.click({ target: { closest: () => ({ dataset: { waAction: action }, disabled: false }) } });
  await click('logout'); await tick(5);
  assert.ok(/desconecta o número atual do agente/.test(confirms[0]) && h.requests.length === 0, 'recusou: nada pedido');
  h.c.confirm = async () => true;
  await click('logout'); await tick(5);
  assert.deepEqual(h.requests, ['logout']);
  h.c.api.requestWhatsappCommand = async () => ({ ok: false, error: 'comando_em_andamento' });
  await click('connect'); await tick(5);
  assert.ok(/Já tem um pedido em andamento/.test(h.body.innerHTML));
  cleanup();
});

await test('demo: connect/logout percorrem pendente → aguardando_leitura → open sem sair da página', async () => {
  const ctx = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']);
  const api = ctx.SDR.apiDemo; api.init(); api.waTiming = { qrMs: 40, openMs: 160 };
  const first = await api.getWhatsappConnection();
  assert.equal(first.state, 'open'); assert.equal(first.owner_number, '5511900000000'); assert.ok(first.checked_at);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (const command of ['connect', 'logout']) {
    const r = await api.requestWhatsappCommand(command);
    assert.equal(r.ok, true); assert.ok(r.command_id);
    assert.equal(JSON.stringify(await api.requestWhatsappCommand(command)), JSON.stringify({ ok: false, error: 'comando_em_andamento' }));
    assert.ok(['pendente', 'em_andamento'].includes((await api.getWhatsappConnection()).command_status));
    if (command === 'logout') assert.equal((await api.getWhatsappConnection()).owner_number, null);
    await sleep(70);
    const mid = await api.getWhatsappConnection();
    assert.equal(mid.command_status, 'aguardando_leitura');
    assert.ok(/^data:image\/png;base64,[A-Za-z0-9+/=]+$/.test(mid.qr_base64));
    await sleep(120);
    const end = await api.getWhatsappConnection();
    assert.equal(end.state, 'open'); assert.equal(end.command_status, 'concluido'); assert.equal(end.qr_base64, null);
  }
  assert.equal(JSON.stringify(await api.requestWhatsappCommand('apagar')), JSON.stringify({ ok: false, error: 'comando_invalido' }));
});

await test('api-live: getWhatsappConnection lê a linha 1 e requestWhatsappCommand usa a RPC', async () => {
  const { SDR } = load(liveFiles);
  const rpcs = [];
  const client = { rpc: async (name, args) => { rpcs.push([name, args]); return name === 'whatsapp_status' ? { data: { id: 1, state: 'open', checked_age_seconds: 4 }, error: null } : { data: { ok: true, command_id: 'c1' }, error: null }; } };
  SDR.apiLive.init(client, client);
  const row = await SDR.apiLive.getWhatsappConnection();
  assert.equal(row.state, 'open'); assert.equal(row.checked_age_seconds, 4); assert.equal(rpcs[0][0], 'whatsapp_status');
  const r = await SDR.apiLive.requestWhatsappCommand('connect');
  assert.equal(r.ok, true); assert.equal(JSON.stringify(rpcs[1]), JSON.stringify(['request_whatsapp_command', { p_command: 'connect' }]));
  await assert.rejects(() => SDR.apiLive.requestWhatsappCommand('apagar'), /inválido/);
  const denied = { from() { return {}; }, rpc: async () => ({ data: null, error: { code: '42501', message: 'sem_permissao' } }) };
  SDR.apiLive.init(denied, denied);
  await assert.rejects(() => SDR.apiLive.requestWhatsappCommand('logout'), /sessão não tem permissão/);
  const missing = { rpc: async () => ({ data: null, error: { code: '42883', message: 'x' } }) };
  SDR.apiLive.init(missing, missing);
  await assert.rejects(() => SDR.apiLive.getWhatsappConnection(), /migrations/);
});

await test('Visão geral: banner do WhatsApp só com motor vivo e estado diferente de open; falha não quebra', async () => {
  const ctx = viewCtx(['js/common.js', 'js/views/whatsapp.js', 'js/views/visao.js']);
  const mk = async wa => { const root = stubEl(); root.children['#vg-period'] = stubEl({ value: '30' }); root.children['#vg-results'] = stubEl(); const box = stubEl(); root.children['#vg-wa-alert'] = box;
    const c = { state: { tz: 'America/Sao_Paulo', closers: [] }, navigate() {}, api: { dashboardStats: async () => ({}), listAppointments: async () => [], getWhatsappConnection: typeof wa === 'function' ? wa : async () => wa } };
    await ctx.SDR.views.visao.render(root, c); await tick(); return box.innerHTML; };
  const fresh = 5;
  assert.ok(/desconectado/.test(await mk({ state: 'close', checked_age_seconds: fresh })) && /#\/whatsapp/.test(await mk({ state: 'close', checked_age_seconds: fresh })));
  assert.equal(await mk({ state: 'open', checked_age_seconds: fresh }), '');
  assert.equal(await mk({ state: 'connecting', checked_age_seconds: fresh }), '', 'connecting sem banner de erro');
  assert.equal(await mk({ state: 'desconhecido', checked_age_seconds: fresh }), '', 'desconhecido sem banner de erro');
  assert.equal(await mk({ state: 'close', checked_age_seconds: 600 }), '');
  assert.equal(await mk({ state: 'close', checked_age_seconds: -30 }), '', 'idade negativa = motor parado');
  assert.equal(await mk({ state: 'close', checked_at: new Date().toISOString() }), '', 'sem idade do servidor = parado');
  assert.equal(await mk(() => { throw new Error('sem tabela'); }), '');
});

await test('whatsapp: motor vivo só pela idade do servidor; relógio do navegador não entra', () => {
  const { SDR } = waHarness(waRow()).ctx;
  assert.equal(SDR.whatsappMotorAlive({ checked_age_seconds: 0 }), true);
  assert.equal(SDR.whatsappMotorAlive({ checked_age_seconds: 180 }), true);
  assert.equal(SDR.whatsappMotorAlive({ checked_age_seconds: 181 }), false);
  assert.equal(SDR.whatsappMotorAlive({ checked_at: new Date(Date.now() + 3600000).toISOString() }), false);
  assert.equal(SDR.whatsappMotorAlive(null), false);
});

await test('whatsapp: QR só aparece com qr_valid true; contador parte de qr_remaining_seconds', async () => {
  const off = waHarness(waRow({ command_status: 'aguardando_leitura', qr_base64: PNG_OK, qr_valid: false }));
  const c1 = await off.render();
  assert.ok(!/<img/.test(off.body.innerHTML) && /Renovando o QR/.test(off.body.innerHTML)); c1();
  const on = waHarness(waRow({ command_status: 'aguardando_leitura', qr_base64: PNG_OK, qr_valid: true, qr_remaining_seconds: 25 }));
  const c2 = await on.render();
  assert.ok(/<img/.test(on.body.innerHTML) && /mais (25|24) s/.test(on.body.innerHTML)); c2();
});

await test('whatsapp: poll sem mudança não remonta a tela; mudança remonta; aria-live só no status', async () => {
  let row = waRow({ command_status: 'pendente' });
  const h = waHarness(() => Object.assign({}, row));
  let mounts = 0; const orig = h.ctx.SDR.mount; h.ctx.SDR.mount = (...a) => { mounts++; return orig(...a); };
  const cleanup = await h.render(); const first = mounts;
  assert.ok(!/id="wa-body"[^>]*aria-live/.test(h.root.innerHTML || ''), 'corpo sem aria-live');
  for (let i = 0; i < 2; i++) { const [id, t] = [...h.timers.entries()][0]; h.timers.delete(id); await t.fn(); await tick(5); }
  assert.equal(mounts, first, 'nenhuma remontagem sem mudança');
  row = waRow({ command_status: 'erro', command_error: 'x' });
  const [id, t] = [...h.timers.entries()][0]; h.timers.delete(id); await t.fn(); await tick(5);
  assert.ok(mounts > first, 'remonta quando o estado muda');
  cleanup();
});

await test('api-live/painel: isPanelUser explícito, falha fechado e demo devolve true', async () => {
  const { SDR } = load(liveFiles);
  SDR.apiLive.init({ rpc: async () => ({ data: true, error: null }) }, {});
  assert.equal(await SDR.apiLive.isPanelUser(), true);
  SDR.apiLive.init({ rpc: async () => ({ data: false, error: null }) }, {});
  assert.equal(await SDR.apiLive.isPanelUser(), false);
  SDR.apiLive.init({ rpc: async () => ({ data: null, error: { code: '42501', message: 'x' } }) }, {});
  assert.equal(await SDR.apiLive.isPanelUser(), false);
  SDR.apiLive.init({ rpc: async () => ({ data: null, error: { code: '500', message: 'x' } }) }, {});
  await assert.rejects(() => SDR.apiLive.isPanelUser(), /verificar a autoriza/);
  const demo = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']); demo.SDR.apiDemo.init();
  assert.equal(await demo.SDR.apiDemo.isPanelUser(), true);
  const dq = await demo.SDR.apiDemo.getWhatsappConnection();
  assert.equal(dq.checked_age_seconds, 0); assert.ok('qr_valid' in dq && 'qr_remaining_seconds' in dq);
  const painel = fs.readFileSync(path.join(web, 'js/painel.js'), 'utf8');
  assert.ok(/isPanelUser/.test(painel) && !/SDR\.client\.rpc\('is_panel_user'\)/.test(painel), 'painel usa a checagem explícita');
});

await test('whatsapp: Trocar número não promete QR automático', () => {
  const src = fs.readFileSync(path.join(web, 'js/views/whatsapp.js'), 'utf8');
  assert.ok(!/um QR novo será gerado|gera um QR para o número novo/.test(src), 'sem promessa de QR automático');
  assert.ok(/Clique em Reconectar \(ler QR Code\) para ler o QR do número novo/.test(src), 'aviso manda reconectar');
  assert.ok(/depois, clique em Reconectar/.test(src), 'descrição da ação manda reconectar');
});

await test('r18 updateLead: whitelist, opt_out só true, campos desconhecidos descartados (live e demo)', async () => {
  const ctx = load(liveFiles);
  const warns = []; ctx.console = { warn: m => warns.push(m), log() {}, error() {} };
  const patches = [];
  const client = { from() { const b = new Proxy({}, { get(_, op) {
    if (op === 'then') return resolve => resolve({ data: { id: U1 }, error: null });
    return (...args) => { if (op === 'update') patches.push(args[0]); return b; };
  } }); return b; } };
  const { SDR } = ctx; SDR.apiLive.init(client, client);
  const api = SDR.apiLive;
  await api.updateLead(U1, { stage: 'qualificado', opt_out: false, hack: 1, takeover_until: null });
  assert.equal(JSON.stringify(patches[0]), JSON.stringify({ stage: 'qualificado', takeover_until: null }), 'opt_out:false e campo desconhecido não chegam ao update');
  await api.updateLead(U1, { opt_out: true });
  assert.equal(JSON.stringify(patches[1]), JSON.stringify({ opt_out: true }));
  await assert.rejects(() => api.updateLead(U1, { opt_out: false, name: 'x' }));
  assert.equal(patches.length, 2, 'patch vazio depois do filtro não vai ao banco');
  const demo = load(['js/common.js', 'demo/demo-data.js', 'js/api-demo.js']);
  const d = demo.SDR.apiDemo; d.init();
  const lead = (await d.listLeads({})).find(l => l.phone);
  await d.updateLead(lead.id, { opt_out: true });
  await d.updateLead(lead.id, { opt_out: false, stage: 'respondeu' });
  assert.equal((await d.getLead(lead.id)).opt_out, true, 'demo também não reverte opt_out');
  await api.reativarLead(U1);
  assert.equal(JSON.stringify(patches[2]), JSON.stringify({ opt_out: false }), 'reativação explícita envia só opt_out:false');
  await d.reativarLead(lead.id);
  assert.equal((await d.getLead(lead.id)).opt_out, false, 'demo reativa pelo caminho explícito');
});

await test('r18 leads: desmarcar opt-out pede confirmação e usa reativarLead', async () => {
  const src = fs.readFileSync(new URL('../../web/js/views/leads.js', import.meta.url), 'utf8');
  assert.match(src, /Permitir mensagens novamente\?/);
  assert.match(src, /ctx\.api\.reativarLead\(lead\.id\)/);
  assert.doesNotMatch(src, /updateLead\(lead\.id, \{opt_out: value\}\)/);
});

await test('r18 whatsapp: logout expirado não oferece QR novo', async () => {
  const h = waHarness(waRow({ state: 'open', owner_number: '5511900000000', command: 'logout', command_status: 'expirado' }));
  const cleanup = await h.render();
  assert.ok(/expirou sem ser executado; o número atual continua conectado/.test(h.body.innerHTML));
  assert.ok(!/Gerar QR novo/.test(h.body.innerHTML) && !/O QR expirou/.test(h.body.innerHTML));
  cleanup();
  const q = waHarness(waRow({ command: 'connect', command_status: 'expirado' }));
  const c2 = await q.render();
  assert.ok(/O QR expirou/.test(q.body.innerHTML) && /Gerar QR novo/.test(q.body.innerHTML));
  c2();
});

await test('r18 whatsapp: poll falhando envelhece o motor e avisa depois de 2 falhas', async () => {
  const realNow = Date.now; let t = realNow.call(Date); Date.now = () => t;
  try {
    let fail = false;
    const h = waHarness(() => { if (fail) throw new Error('401'); return waRow({ state: 'open', owner_number: '5511900000000', checked_age_seconds: 20 }); });
    const cleanup = await h.render();
    assert.ok(/Conectado/.test(h.body.innerHTML) && !/O motor não está respondendo/.test(h.body.innerHTML));
    fail = true;
    const poll = async () => { const [id, tm] = [...h.timers.entries()].find(([, x]) => x.ms === 15000); h.timers.delete(id); await tm.fn(); await tick(5); };
    t += 100000; await poll();
    assert.ok(!/Não foi possível atualizar o status agora/.test(h.body.innerHTML), 'uma falha só não avisa');
    t += 100000; await poll();
    assert.ok(/Não foi possível atualizar o status agora/.test(h.body.innerHTML), 'duas falhas avisam');
    assert.ok(/O motor não está respondendo/.test(h.body.innerHTML), '200 s locais: motor parado');
    cleanup();
  } finally { Date.now = realNow; }
});

await test('r20 whatsapp: logout só anuncia o pedido; a conclusão só aparece com concluido + close', async () => {
  let current = waRow({ state: 'open', owner_number: '5511900000000' });
  const h = waHarness(() => current);
  const cleanup = await h.render();
  const click = action => h.root.handlers.click({ target: { closest: () => ({ dataset: { waAction: action }, disabled: false }) } });
  current = waRow({ state: 'open', owner_number: '5511900000000', command: 'logout', command_status: 'pendente' });
  await click('logout'); await tick(5);
  assert.ok(/Pedido de desconexão enviado\. O motor vai desconectar o número em instantes\./.test(h.body.innerHTML), 'aviso de pedido');
  assert.ok(!/O número atual foi desconectado/.test(h.body.innerHTML), 'não afirma desconexão antes da hora');
  current = waRow({ state: 'open', owner_number: '5511900000000', command: 'logout', command_status: 'em_andamento' });
  const poll = async () => { const [id, tm] = [...h.timers.entries()].find(([, x]) => x.ms === 3000); h.timers.delete(id); await tm.fn(); await tick(5); };
  await poll();
  assert.ok(!/O número atual foi desconectado/.test(h.body.innerHTML));
  current = waRow({ state: 'close', owner_number: null, command: 'logout', command_status: 'concluido' });
  const [id, tm] = [...h.timers.entries()].find(([, x]) => x.ms === 3000); h.timers.delete(id); await tm.fn(); await tick(5);
  assert.ok(/O número atual foi desconectado/.test(h.body.innerHTML) && /Reconectar \(ler QR Code\)/.test(h.body.innerHTML), 'conclusão só depois de concluido + close');
  assert.ok(!/Pedido de desconexão enviado/.test(h.body.innerHTML));
  cleanup();
  const open = waHarness(waRow({ state: 'open', owner_number: '5511900000000', command: 'logout', command_status: 'concluido' }));
  const c2 = await open.render();
  assert.ok(!/O número atual foi desconectado/.test(open.body.innerHTML), 'concluido mas ainda open: não afirma');
  c2();
});

await test('r20 visao: Personalizado e Este mês usam o primeiro instante existente do dia (Santiago)', async () => {
  const tz = 'America/Santiago';
  const ctx = viewCtx(['js/common.js', 'js/views/visao.js']);
  const calls = [];
  const mk = async (mode, from, to) => {
    const root = stubEl(); root.children['#vg-period'] = stubEl({ value: mode });
    root.children['#vg-from'] = stubEl({ value: from }); root.children['#vg-to'] = stubEl({ value: to });
    root.children['#vg-results'] = stubEl();
    const c = { state: { tz, closers: [] }, navigate() {}, api: { dashboardStats: async (a, b) => { calls.push([a, b]); return { leads: 0 }; }, listAppointments: async () => [] } };
    await ctx.SDR.views.visao.render(root, c); await tick(); return calls[calls.length - 1];
  };
  const [from, to] = await mk('custom', '2026-09-06', '2026-09-06');
  assert.equal(from, ctx.SDR.tzTools.zonedToUtc(2026, 9, 6, 1, 0, tz).toISOString(), 'De = 01:00 (00:00 não existe em 2026-09-06)');
  assert.equal(to, ctx.SDR.tzTools.zonedToUtc(2026, 9, 7, 0, 0, tz).toISOString());
  const [mFrom] = await mk('month', '', '');
  const p = ctx.SDR.tzTools.parts(new Date(mFrom), tz);
  assert.equal(p.d, 1, 'Este mês começa no dia 1');
  assert.ok(/startOfDay/.test(fs.readFileSync(path.join(web, 'js/views/visao.js'), 'utf8').split("period.value === 'month'")[1].slice(0, 200)), 'ramo month usa startOfDay');
});

await test('r21 requireConfigOrExplain: http só em localhost; http remoto recusa com banner próprio', () => {
  const cfg = url => load(common, { SDR_CONFIG: { demo: false, supabaseUrl: url, supabaseAnonKey: 'sb_publishable_abc123' } }).SDR;
  assert.equal(cfg('https://x.supabase.co').requireConfigOrExplain(null), true);
  ['http://localhost:54321', 'http://127.0.0.1:54321', 'http://[::1]:54321'].forEach(u => assert.equal(cfg(u).requireConfigOrExplain(null), true, u));
  const bad = cfg('http://x.supabase.co'), root = stubEl();
  assert.equal(bad.requireConfigOrExplain(root), false);
  assert.equal(bad.configProblem, 'http');
  assert.ok(/sem https/.test(root.innerHTML) && /sem criptografia/.test(root.innerHTML), root.innerHTML);
  assert.equal(cfg('http://localhost.evil.com').requireConfigOrExplain(null), false);
  assert.equal(cfg('ftp://x.supabase.co').requireConfigOrExplain(null), false);
  assert.equal(load(common, { SDR_CONFIG: { demo: true } }).SDR.requireConfigOrExplain(null), true);
});

process.exit(failed ? 1 : 0);
