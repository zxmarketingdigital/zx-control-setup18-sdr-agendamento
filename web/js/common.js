(function (global) {
  'use strict';
  const SDR = global.SDR = global.SDR || {};
  SDR.config = Object.assign({ supabaseUrl: '', supabaseAnonKey: '', cfBeaconToken: '', demo: false }, global.SDR_CONFIG || {});
  SDR.configMissing = global.SDR_CONFIG === undefined;
  SDR.tz = 'America/Sao_Paulo';
  SDR.esc = function (value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c]);
  };
  // Campos do lead que o painel pode alterar (leads.js: stage e opt_out; conversas.js: takeover_until).
  // opt_out só aceita true: reativar um contato que pediu para sair nunca é feito pelo painel.
  SDR.LEAD_EDITABLE = ['stage', 'opt_out', 'takeover_until'];
  SDR.sanitizeLeadPatch = function (patch) {
    const out = {};
    Object.keys(patch || {}).forEach(key => {
      if (!SDR.LEAD_EDITABLE.includes(key)) { if (global.console) global.console.warn('Campo de contato ignorado: ' + key); return; }
      if (key === 'opt_out' && patch[key] !== true) { if (global.console) global.console.warn('opt_out só pode ser ligado pelo painel.'); return; }
      out[key] = patch[key];
    });
    return out;
  };
  SDR.raw = function (html) { return { __raw: true, html: String(html == null ? '' : html), toString() { return this.html; } }; };
  const interpolate = value => Array.isArray(value) ? value.map(interpolate).join('') : value && value.__raw === true ? String(value.html) : SDR.esc(value);
  SDR.html = function (strings, ...values) {
    return SDR.raw(strings.reduce((html, part, i) => html + part + (i < values.length ? interpolate(values[i]) : ''), ''));
  };
  SDR.mount = function (el, value) { if (el) el.innerHTML = value && value.__raw === true ? String(value.html) : SDR.esc(value); return el; };
  SDR.qs = (selector, root) => (root || global.document).querySelector(selector);
  SDR.qsa = (selector, root) => Array.from((root || global.document).querySelectorAll(selector));
  // Lista de permissão: o navegador ignora TAB/LF/espaços e controles dentro do esquema,
  // então eles saem antes da checagem. Só http, https, mailto, tel e caminhos relativos passam.
  SDR.safeUrl = function (value) {
    const text = String(value == null ? '' : value).replace(/[\u0000-\u0020\u007f-\u009f\u2028\u2029\u200b-\u200f\ufeff]/g, '');
    const scheme = text.match(/^([a-z][a-z0-9+.-]*):/i);
    if (scheme) return /^(https?|mailto|tel)$/i.test(scheme[1]);
    return true;
  };
  SDR.el = function (tag, attrs, ...children) {
    const node = global.document.createElement(tag);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value == null || value === false) return;
      if (/^on/i.test(key)) { if (typeof value === 'function') node.addEventListener(key.slice(2).toLowerCase(), value); return; }
      if (key === 'dataset') { Object.entries(value).forEach(([k, v]) => { node.dataset[k] = String(v); }); return; }
      if (key === 'textContent') { node.textContent = String(value); return; }
      if (key === 'className') key = 'class';
      // Refuse active URL schemes even when a caller passes an unsafe database value.
      if (/^(href|src|action|formaction)$/i.test(key) && !SDR.safeUrl(value)) return;
      if (value === true) { node.setAttribute(key, ''); if (key in node) node[key] = true; }
      else node.setAttribute(key, String(value));
    });
    const append = child => {
      if (Array.isArray(child)) child.forEach(append);
      else if (child != null && child !== false) node.appendChild(child && child.nodeType ? child : global.document.createTextNode(String(child)));
    };
    children.forEach(append);
    return node;
  };

  const dateValue = value => value instanceof Date ? value : new Date(value);
  function dateFormat(value, tz, options) {
    const date = dateValue(value);
    if (!Number.isFinite(date.getTime())) return '—';
    try { return new Intl.DateTimeFormat('pt-BR', Object.assign({ timeZone: tz || SDR.tz }, options)).format(date); }
    // Fuso inválido nunca vira UTC em silêncio (deslocaria todos os horários): mostra traço; o painel barra o fuso inválido ao carregar.
    catch (_) { return '—'; }
  }
  function parts(value, tz) {
    const date = dateValue(value);
    if (!Number.isFinite(date.getTime())) throw new Error('Data inválida.');
    const values = {};
    new Intl.DateTimeFormat('en-GB', { timeZone: tz || SDR.tz, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23' }).formatToParts(date).forEach(p => { if (p.type !== 'literal') values[p.type] = Number(p.value); });
    const result = { y: values.year, m: values.month, d: values.day, hh: values.hour, mm: values.minute, ss: values.second };
    result.weekday = new Date(Date.UTC(result.y, result.m - 1, result.d)).getUTCDay();
    return result;
  }
  function zonedToUtc(y, m, d, hh, mm, tz) {
    const target = Date.UTC(y, m - 1, d, hh || 0, mm || 0);
    let guess = target;
    // Corrige o offset com Intl, inclusive em fusos com horário de verão.
    for (let i = 0; i < 6; i++) {
      const p = parts(new Date(guess), tz);
      const delta = target - Date.UTC(p.y, p.m - 1, p.d, p.hh, p.mm, p.ss);
      if (delta === 0) return new Date(guess);
      guess += delta;
    }
    // Horário de parede inexistente na transição de verão não pode virar outro slot.
    const p = parts(new Date(guess), tz);
    const normalized = new Date(target);
    if (p.y !== normalized.getUTCFullYear() || p.m !== normalized.getUTCMonth() + 1 || p.d !== normalized.getUTCDate() || p.hh !== normalized.getUTCHours() || p.mm !== normalized.getUTCMinutes()) throw new Error('Este horário não existe no fuso escolhido.');
    return new Date(guess);
  }
  // Primeiro instante que existe no dia: se 00:00 foi pulado pelo horário de verão (ex.: Santiago), usa o primeiro horário válido.
  function firstInstantOfDay(y, m, d, tz) {
    for (let minute = 0; minute < 360; minute++) {
      try { return zonedToUtc(y, m, d, Math.floor(minute / 60), minute % 60, tz); } catch (_) { /* horário inexistente: tenta o minuto seguinte */ }
    }
    return zonedToUtc(y, m, d, 0, 0, tz);
  }
  SDR.tzTools = {
    parts, zonedToUtc,
    startOfDay(value, tz) { const p = parts(value, tz); return firstInstantOfDay(p.y, p.m, p.d, tz); },
    addDays(value, days, tz) {
      const p = parts(value, tz);
      const calendar = new Date(Date.UTC(p.y, p.m - 1, p.d + Number(days), p.hh, p.mm, p.ss));
      const result = zonedToUtc(calendar.getUTCFullYear(), calendar.getUTCMonth() + 1, calendar.getUTCDate(), calendar.getUTCHours(), calendar.getUTCMinutes(), tz);
      return new Date(result.getTime() + p.ss * 1000 + dateValue(value).getUTCMilliseconds());
    },
    startOfWeek(value, tz) { const p = parts(value, tz); const day = new Date(Date.UTC(p.y, p.m - 1, p.d - (p.weekday + 6) % 7)); return firstInstantOfDay(day.getUTCFullYear(), day.getUTCMonth() + 1, day.getUTCDate(), tz); },
    browserTz() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'America/Sao_Paulo'; } catch (_) { return 'America/Sao_Paulo'; } }
  };
  SDR.fmt = {
    date: (d, tz) => dateFormat(d, tz, { day: '2-digit', month: '2-digit', year: 'numeric' }),
    time: (d, tz) => dateFormat(d, tz, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }),
    dateTime(d, tz) { return this.date(d, tz) + ' às ' + this.time(d, tz); },
    weekdayShort: (d, tz) => dateFormat(d, tz, { weekday: 'short' }).replace(/\.$/, ''),
    dayLabel(d, tz) { return this.weekdayShort(d, tz) + ', ' + dateFormat(d, tz, { day: '2-digit', month: '2-digit' }); },
    money: n => new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL' }).format(Number(n) || 0),
    // Recebe FRAÇÃO (0..1), nunca pontos percentuais: Intl multiplica por 100. Sem valor (divisão por zero no banco) mostra traço.
    pct: n => n == null || n === '' || !Number.isFinite(Number(n)) ? '—' : new Intl.NumberFormat('pt-BR', { style: 'percent', maximumFractionDigits: 1 }).format(Number(n)),
    int: n => new Intl.NumberFormat('pt-BR', { maximumFractionDigits: 0 }).format(Number(n) || 0),
    rel(value) {
      const date = dateValue(value);
      if (!Number.isFinite(date.getTime())) return '—';
      const diff = Date.now() - date.getTime(), abs = Math.abs(diff);
      if (abs < 60000) return diff < 0 ? 'em instantes' : 'agora';
      const n = Math.floor(abs / (abs < 3600000 ? 60000 : abs < 86400000 ? 3600000 : 86400000));
      if (diff >= 0 && n === 1 && abs >= 86400000 && abs < 172800000) return 'ontem';
      return (diff < 0 ? 'em ' : 'há ') + n + (abs < 3600000 ? ' min' : abs < 86400000 ? ' h' : n === 1 ? ' dia' : ' dias');
    },
    phone(value) {
      const phone = String(value || '');
      const br = phone.match(/^\+55(\d{2})(\d{4,5})(\d{4})$/);
      if (br) return '+55 ' + br[1] + ' ' + br[2] + '-' + br[3];
      const pt = phone.match(/^\+351(\d{3})(\d{3})(\d{3})$/);
      if (pt) return '+351 ' + pt.slice(1).join(' ');
      if (!SDR.phone.valid(phone)) return phone;
      const match = SDR.phone.COUNTRIES.slice().sort((a, b) => b.dial.length - a.dial.length).find(c => phone.slice(1).startsWith(c.dial));
      const dial = match ? match.dial : phone.slice(1, 2), national = phone.slice(1 + dial.length);
      return '+' + dial + ' ' + (national.match(/.{1,3}/g) || []).join(' ');
    }
  };
  SDR.phone = {
    COUNTRIES: [
      ['BR', 'Brasil', '55'], ['PT', 'Portugal', '351'], ['US', 'Estados Unidos', '1'], ['AR', 'Argentina', '54'], ['CL', 'Chile', '56'], ['CO', 'Colômbia', '57'], ['MX', 'México', '52'], ['PY', 'Paraguai', '595'], ['UY', 'Uruguai', '598'], ['PE', 'Peru', '51'], ['ES', 'Espanha', '34'], ['FR', 'França', '33'], ['IT', 'Itália', '39'], ['DE', 'Alemanha', '49'], ['GB', 'Reino Unido', '44'], ['AO', 'Angola', '244'], ['MZ', 'Moçambique', '258'], ['CV', 'Cabo Verde', '238'], ['CA', 'Canadá', '1']
    ].map(([iso, nome, dial]) => ({ iso, nome, dial, flag: iso })),
    valid: value => /^\+[1-9][0-9]{7,14}$/.test(String(value || '')),
    // Sem '+', fora do Brasil: dígitos que já começam com o DDI do país escolhido (ex.: Portugal '351912345678') são ambíguos
    // e viram inválidos, para nunca gerar '+351351...'. Com '+' ou '00' vale o que veio.
    hasDialPrefix(raw, dial) {
      const text = String(raw == null ? '' : raw).trim(), digits = text.replace(/\D/g, ''), prefix = String(dial == null ? '' : dial).replace(/\D/g, '');
      if (!prefix || prefix === '55' || text.startsWith('+') || /^00[1-9]/.test(digits)) return false;
      return digits.replace(/^0+/, '').startsWith(prefix);
    },
    toE164(raw, dial) {
      const text = String(raw == null ? '' : raw).trim(), digits = text.replace(/\D/g, '');
      const prefix = String(dial == null ? '' : dial).replace(/\D/g, '');
      if (!digits || (!text.startsWith('+') && !/^[1-9][0-9]{0,2}$/.test(prefix))) return null;
      // Com '+' (ou '00') vale o que veio. Sem '+', o número é NACIONAL do país escolhido: só se aceita DDI já digitado
      // no Brasil quando o tamanho é inequívoco (12-13 dígitos começando com 55); DDD 55 de 10-11 dígitos recebe +55.
      let value;
      if (text.startsWith('+')) value = '+' + digits;
      else if (/^00[1-9]/.test(digits)) value = '+' + digits.slice(2);
      else {
        const bare = digits.replace(/^0+/, '');
        if (prefix === '55') {
          if (bare.length >= 10 && bare.length <= 11) value = '+55' + bare;
          else if (bare.length >= 12 && bare.length <= 13 && bare.startsWith('55')) value = '+' + bare;
          else return null;
        } else if (this.hasDialPrefix(text, prefix)) return null;
        else value = '+' + prefix + bare;
      }
      return this.valid(value) ? value : null;
    }
  };
  SDR.uuid = function () {
    if (global.crypto && typeof global.crypto.randomUUID === 'function') return global.crypto.randomUUID();
    const bytes = new Uint8Array(16);
    if (global.crypto && global.crypto.getRandomValues) global.crypto.getRandomValues(bytes);
    else for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(Math.random() * 256);
    bytes[6] = bytes[6] & 15 | 64; bytes[8] = bytes[8] & 63 | 128;
    return Array.from(bytes, (b, i) => ([4, 6, 8, 10].includes(i) ? '-' : '') + b.toString(16).padStart(2, '0')).join('');
  };
  SDR.debounce = function (fn, ms) { let timer; return function (...args) { clearTimeout(timer); timer = setTimeout(() => fn.apply(this, args), ms); }; };
  SDR.toast = function (message, kind) {
    if (!global.document || !global.document.body) return;
    let region = global.document.getElementById('sdr-toast');
    if (!region) { region = SDR.el('div', { id: 'sdr-toast', class: 'sdr-toast', role: 'status', 'aria-live': 'polite', 'aria-atomic': 'true' }); global.document.body.appendChild(region); }
    region.className = 'sdr-toast banner ' + (kind === 'erro' ? 'banner-error' : kind === 'ok' ? 'banner-success' : '');
    region.textContent = String(message); region.hidden = false;
    clearTimeout(SDR.toast.timer); SDR.toast.timer = setTimeout(() => { region.hidden = true; }, 6000);
  };
  SDR.beacon = function () {
    const token = typeof SDR.config.cfBeaconToken === 'string' ? SDR.config.cfBeaconToken.trim() : '';
    if (!token || !global.document || global.document.querySelector('[data-sdr-beacon]')) return;
    const script = global.document.createElement('script');
    script.defer = true; script.src = 'https://static.cloudflareinsights.com/beacon.min.js';
    script.setAttribute('data-cf-beacon', JSON.stringify({ token })); script.setAttribute('data-sdr-beacon', 'true'); global.document.head.appendChild(script);
  };
  // Decodifica base64url sem depender de atob (o painel e os testes rodam em ambientes diferentes).
  function base64urlText(segment) {
    const table = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
    const clean = String(segment || '').replace(/=+$/, '');
    if (!clean || /[^A-Za-z0-9_-]/.test(clean) || clean.length % 4 === 1) return null;
    let bits = 0, acc = 0, out = '';
    for (const ch of clean) {
      acc = acc << 6 | table.indexOf(ch); bits += 6;
      if (bits >= 8) { bits -= 8; out += String.fromCharCode(acc >> bits & 255); acc &= (1 << bits) - 1; }
    }
    try { return decodeURIComponent(out.split('').map(c => '%' + c.charCodeAt(0).toString(16).padStart(2, '0')).join('')); } catch (_) { return out; }
  }
  // Só a chave pública entra no navegador: service_role, sb_secret_ e JWT ilegível são recusados.
  SDR.keyProblem = function (key) {
    const text = typeof key === 'string' ? key.trim() : '';
    if (!text) return 'vazia';
    if (/^sb_publishable_/i.test(text)) return null;
    if (/^sb_secret_/i.test(text)) return 'secreta';
    const segments = text.split('.');
    if (segments.length !== 3) return 'ilegivel';
    let payload = null;
    try { payload = JSON.parse(base64urlText(segments[1])); } catch (_) { payload = null; }
    if (!payload || typeof payload !== 'object') return 'ilegivel';
    if (String(payload.role).toLowerCase() === 'service_role') return 'secreta';
    return payload.role === 'anon' || payload.role === 'authenticated' ? null : 'ilegivel';
  };
  SDR.requireConfigOrExplain = function (root) {
    // Demo só vale sem backend configurado: config.js com demo:true e Supabase junto vira erro, não gravação só na aba.
    const demoWithBackend = SDR.config.demo === true && String(SDR.config.supabaseUrl || '').trim() !== '';
    let valid = !SDR.configMissing && SDR.config.demo === true && !demoWithBackend;
    let wrongKey = false, insecure = false;
    SDR.configProblem = '';
    if (!SDR.configMissing && !valid && !demoWithBackend) {
      try {
        const url = new URL(SDR.config.supabaseUrl);
        const local = ['localhost', '127.0.0.1', '[::1]', '::1'].includes(url.hostname);
        const secure = url.protocol === 'https:' || (url.protocol === 'http:' && local);
        if (url.protocol === 'http:' && !local) insecure = true;
        valid = secure && typeof SDR.config.supabaseAnonKey === 'string' && SDR.config.supabaseAnonKey.trim().length > 0;
        if (valid && SDR.keyProblem(SDR.config.supabaseAnonKey)) { valid = false; wrongKey = true; }
      } catch (_) { valid = false; }
    }
    if (wrongKey) SDR.configProblem = 'chave';
    else if (insecure) SDR.configProblem = 'http';
    if (!valid && root && wrongKey) SDR.mount(root, SDR.html`<div class="banner banner-error" role="alert"><strong>Chave errada: use a anon/publishable, nunca a service_role.</strong><p>A chave em web/config.js é secreta ou ilegível. Troque por a chave pública (anon ou publishable) do seu projeto Supabase. A página não conecta ao banco com esta chave.</p></div>`);
    else if (!valid && root && insecure) SDR.mount(root, SDR.html`<div class="banner banner-error" role="alert"><strong>Endereço do Supabase sem https.</strong><p>Use o endereço https://... do seu projeto em web/config.js; sem https a senha trafega sem criptografia.</p></div>`);
    else if (!valid && root && demoWithBackend) SDR.mount(root, SDR.html`<div class="banner banner-error" role="alert"><strong>Modo demonstração com banco configurado.</strong><p>Remova supabaseUrl ou ponha demo: false em web/config.js antes de publicar.</p></div>`);
    else if (!valid && root) SDR.mount(root, SDR.html`<div class="banner" role="alert"><strong>Configuração não encontrada.</strong><p>Rode o INICIAR SETUP ou crie web/config.js a partir de config.example.js.</p></div>`);
    return valid;
  };
  SDR.STAGES = [
    ['novo', 'Novo', 'muted'], ['respondeu', 'Respondeu', 'indigo'], ['qualificado', 'Qualificado', 'amber'], ['desqualificado', 'Desqualificado', 'red'], ['horarios_ofertados', 'Horários ofertados', 'amber'], ['agendado', 'Agendado', 'amber'], ['confirmado', 'Confirmado', 'green'], ['compareceu', 'Compareceu', 'green'], ['no_show', 'No-show', 'red'], ['reagendado', 'Reagendado', 'indigo'], ['venda', 'Venda', 'green'], ['perdido', 'Perdido', 'red']
  ].map(([key, label, tone]) => ({ key, label, tone }));
  SDR.APPT_STATUS = [['agendado', 'Agendado', 'amber'], ['confirmado', 'Confirmado', 'green'], ['feito', 'Feito', 'green'], ['no_show', 'No-show', 'red'], ['reagendado', 'Reagendado', 'indigo'], ['cancelado', 'Cancelado', 'muted'], ['venda', 'Venda', 'green']].map(([key, label, tone]) => ({ key, label, tone }));
  function badge(list, key) { const item = list.find(s => s.key === key) || { label: 'Não informado', tone: 'muted' }; return SDR.html`<span class="badge badge-${item.tone}">${item.label}</span>`; }
  SDR.stageBadge = key => badge(SDR.STAGES, key);
  SDR.statusBadge = key => badge(SDR.APPT_STATUS, key);
})(typeof window !== 'undefined' ? window : globalThis);
