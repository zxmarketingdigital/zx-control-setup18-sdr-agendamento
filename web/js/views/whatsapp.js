(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  const FAST_POLL_MS = 3000, SLOW_POLL_MS = 15000, MOTOR_STALE_SECONDS = 180;
  const QR_PATTERN = /^data:image\/png;base64,[A-Za-z0-9+/=]+$/;
  const IN_PROGRESS = ['pendente', 'em_andamento', 'aguardando_leitura'];
  const COMMAND_ERRORS = {
    comando_em_andamento: 'Já tem um pedido em andamento, aguarde alguns segundos.',
    sem_permissao: 'Sua sessão não tem permissão para isso. Entre de novo com a conta do dono do painel.',
    comando_invalido: 'Comando inválido. Atualize a página e tente de novo.'
  };
  // Só um PNG em data URL vira imagem; qualquer outra coisa (inclusive javascript: ou SVG) nunca chega ao src.
  SDR.safeQrSrc = value => typeof value === 'string' && value.length <= 200000 && QR_PATTERN.test(value) ? value : null;
  // A idade do último sinal do motor é calculada pelo BANCO (checked_age_seconds): nunca se compara o relógio
  // do navegador com o do computador do motor. Ausente, negativa ou não numérica = motor parado.
  SDR.whatsappMotorAlive = function (row, localSeconds) {
    if (!row) return false;
    const base = row.checked_age_seconds;
    const age = typeof base === 'number' && Number.isFinite(base) && base >= 0 ? base + (localSeconds > 0 ? localSeconds : 0) : base;
    return typeof age === 'number' && Number.isFinite(age) && age >= 0 && age <= MOTOR_STALE_SECONDS;
  };
  const STATES = {
    open: ['Conectado', 'green'], close: ['Desconectado', 'red'],
    connecting: ['Conectando…', 'amber'], desconhecido: ['Aguardando o motor', 'muted']
  };
  SDR.views = SDR.views || {};
  SDR.views.whatsapp = {
    title: 'WhatsApp',
    async render(root, ctx) {
      let failStreak = 0, disposed = false, revision = 0, timer = null, countdownTimer = null, row = null, rowReadAt = 0, lastSig = null, loadError = '', sending = false, notice = null;
      SDR.mount(root, SDR.html`<div class="wa-view stack"><div class="row wa-toolbar"><div><h2>WhatsApp do atendimento</h2><p class="muted">Conecte, reconecte ou troque o número que o agente usa para conversar. O motor do agente roda no seu computador e faz a conexão.</p></div></div><div class="wa-body stack" id="wa-body"></div></div>`);
      const body = SDR.qs('#wa-body', root);
      const inProgress = () => !!row && IN_PROGRESS.includes(row.command_status);
      // Idade do motor = idade medida pelo banco na última leitura + segundos locais desde então.
      function localSeconds() { return rowReadAt ? Math.max(0, (Date.now() - rowReadAt) / 1000) : 0; }
      function motorAge() {
        const base = row && row.checked_age_seconds;
        return typeof base === 'number' && Number.isFinite(base) && base >= 0 ? base + localSeconds() : base;
      }
      const isAlive = () => SDR.whatsappMotorAlive(row, localSeconds());
      function secondsLeft() {
        const base = row ? row.qr_remaining_seconds : null;
        if (typeof base !== 'number' || !Number.isFinite(base)) return null;
        return Math.max(0, Math.ceil(base - (Date.now() - rowReadAt) / 1000));
      }
      function countdownText() {
        const left = secondsLeft();
        if (left == null) return 'O QR é renovado sozinho.';
        return left > 0 ? 'Este QR vale por mais ' + left + ' s. Ele é renovado sozinho.' : 'Renovando o QR…';
      }
      function statusCard(alive) {
        const info = STATES[row.state] || STATES.desconhecido;
        const number = row.state === 'open' && row.owner_number ? SDR.html`<p>Número conectado: <strong class="mono">${SDR.fmt.phone('+' + String(row.owner_number).replace(/\D/g, ''))}</strong></p>` : SDR.raw('');
        const hint = row.state === 'close' ? 'O agente não consegue enviar nem receber mensagens até você reconectar.' : row.state === 'desconhecido' ? 'O motor ainda não informou o estado da conexão.' : row.state === 'connecting' ? 'A conexão está sendo estabelecida.' : 'O agente está atendendo por este número.';
        const age = motorAge();
        const checked = typeof age === 'number' && Number.isFinite(age) && age >= 0 ? SDR.html`<small class="muted">Última conferência do motor: ${age < 60 ? 'agora há pouco' : 'há ' + Math.round(age / 60) + ' min'}</small>` : SDR.raw('');
        return SDR.html`<section class="card stack wa-status"><div class="row"><span class="wa-dot wa-dot-${alive ? info[1] : 'muted'}" aria-hidden="true"></span><h3>Estado da conexão</h3><span class="badge badge-${alive ? info[1] : 'muted'}">${alive ? info[0] : 'Sem resposta do motor'}</span></div>${alive ? number : SDR.raw('')}<p class="muted" aria-live="polite">${alive ? hint : 'O estado abaixo pode estar desatualizado.'}</p>${checked}</section>`;
      }
      function motorWarning() {
        return SDR.html`<div class="banner banner-error wa-motor" role="alert"><strong>O motor não está respondendo.</strong><p>O agente roda no seu computador: confira se ele está ligado e se o serviço está ativo (skill /sdr-agendamento). Enquanto isso, os botões ficam desativados.</p></div>`;
      }
      function commandArea() {
        const status = row.command_status;
        if (['pendente', 'em_andamento'].includes(status)) return SDR.html`<section class="card stack wa-command" role="status"><div class="row"><span class="spinner" aria-hidden="true"></span><strong>Pedindo o QR ao motor…</strong></div><p class="muted">O motor confere o pedido a cada ciclo. Isso costuma levar alguns segundos.</p></section>`;
        if (status === 'aguardando_leitura') {
          if (row.qr_valid !== true) return SDR.html`<section class="card stack wa-command" role="status"><div class="row"><span class="spinner" aria-hidden="true"></span><strong>Renovando o QR…</strong></div><p class="muted">O motor gera um QR novo sozinho. Aguarde alguns segundos.</p></section>`;
          const src = SDR.safeQrSrc(row.qr_base64);
          if (!src) return SDR.html`<section class="card stack wa-command"><div class="banner banner-error" role="alert"><strong>O QR recebido não é válido.</strong><p>Gere um QR novo com o botão abaixo.</p></div></section>`;
          const demo = ctx.api && ctx.api.mode === 'demo';
          const code = row.pairing_code ? SDR.html`<p>Se preferir, use o código de pareamento: <strong class="mono wa-code">${row.pairing_code}</strong></p>` : SDR.raw('');
          return SDR.html`<section class="card stack wa-command"><h3>Leia o QR Code com o celular</h3><div class="wa-qr-wrap"><img class="wa-qr" src="${src}" alt="${demo ? 'QR de demonstração' : 'QR Code para conectar o WhatsApp'}" width="232" height="232"></div>${demo ? SDR.html`<p class="muted"><strong>QR de demonstração</strong> — não é legível; na instalação real aparece o QR verdadeiro.</p>` : SDR.raw('')}<ol class="wa-steps"><li>No celular, abra o WhatsApp.</li><li>Toque em Aparelhos conectados e depois em Conectar aparelho.</li><li>Aponte a câmera para o QR acima.</li></ol>${code}<p class="muted" data-wa-countdown>${countdownText()}</p></section>`;
        }
        if (status === 'expirado' && row.command === 'logout') return SDR.html`<section class="card stack wa-command"><div class="banner" role="status"><strong>O pedido expirou.</strong><p>O pedido para trocar o número expirou sem ser executado; o número atual continua conectado.</p></div></section>`;
        if (status === 'expirado') return SDR.html`<section class="card stack wa-command"><div class="banner" role="status"><strong>O QR expirou.</strong><p>Ninguém leu o QR a tempo. Gere um novo com o botão abaixo.</p></div></section>`;
        if (status === 'erro') return SDR.html`<section class="card stack wa-command"><div class="banner banner-error" role="alert"><strong>Não foi possível concluir.</strong><p>${row.command_error || 'O motor não conseguiu concluir o pedido.'}</p></div></section>`;
        if (status === 'concluido' && row.command === 'logout' && row.state === 'close') return SDR.html`<section class="card stack wa-command"><div class="banner banner-success" role="status"><strong>O número atual foi desconectado.</strong><p>Clique em Reconectar (ler QR Code) para ler o QR do número novo.</p></div></section>`;
        if (status === 'concluido' && row.state === 'open') return SDR.html`<section class="card stack wa-command"><div class="banner banner-success" role="status"><strong>WhatsApp conectado.</strong></div></section>`;
        return SDR.raw('');
      }
      // Assinatura do que o usuário enxerga: o poll só remonta a tela quando algo relevante mudou.
      function signature() {
        const qr = typeof row?.qr_base64 === 'string' ? row.qr_base64 : '';
        return JSON.stringify([!!row, loadError, sending, notice && notice.text, row && [row.state, row.command, row.command_status, row.command_error, row.owner_number, row.qr_valid === true, qr.length, qr.slice(-24), row.pairing_code, isAlive(), failStreak >= 2]]);
      }
      function draw() {
        if (disposed) return;
        lastSig = signature();
        if (loadError) {
          SDR.mount(body, SDR.html`<div class="banner banner-error" role="alert"><p>${loadError}</p><button type="button" class="btn btn-ghost" data-wa-action="reload">Tentar de novo</button></div>`);
          return;
        }
        if (!row) {
          SDR.mount(body, SDR.html`<div class="banner" role="status"><strong>Recurso ainda não ativado no banco.</strong><p>A tabela do WhatsApp não foi encontrada. Peça ao Claude Code para aplicar as migrations (etapa_banco.py migrar) e atualize esta página.</p></div>`);
          return;
        }
        const alive = isAlive(), busy = sending || inProgress() && row.command_status !== 'aguardando_leitura';
        const retry = (row.command_status === 'expirado' && row.command !== 'logout') || row.command_status === 'erro' || row.command_status === 'aguardando_leitura' && row.qr_valid === true && !SDR.safeQrSrc(row.qr_base64);
        const disabled = !alive || busy ? SDR.raw(' disabled') : SDR.raw('');
        const why = !alive ? SDR.html`<p class="muted wa-why">Os botões voltam quando o motor responder.</p>` : SDR.raw('');
        const noticeBlock = notice ? SDR.html`<div class="banner ${notice.kind === 'erro' ? 'banner-error' : ''}" role="${notice.kind === 'erro' ? 'alert' : 'status'}">${notice.text}</div>` : SDR.raw('');
        SDR.mount(body, SDR.html`${alive ? SDR.raw('') : motorWarning()}${failStreak >= 2 ? SDR.html`<div class="banner" role="status">Não foi possível atualizar o status agora. O que aparece abaixo pode estar desatualizado.</div>` : SDR.raw('')}${statusCard(alive)}${noticeBlock}${alive ? commandArea() : SDR.raw('')}<section class="card stack wa-actions"><h3>O que você quer fazer?</h3><div class="row"><button type="button" class="btn btn-primary" data-wa-action="connect"${disabled}>${retry ? 'Gerar QR novo' : 'Reconectar (ler QR Code)'}</button><button type="button" class="btn btn-ghost" data-wa-action="logout"${disabled}>Trocar número</button></div><p class="muted">Reconectar serve quando o WhatsApp caiu. Trocar número só desconecta o atual: depois, clique em Reconectar (ler QR Code) para ler o QR do número novo.</p>${why}</section>`);
      }
      function stopTimers() {
        if (timer != null) { global.clearTimeout(timer); timer = null; }
        if (countdownTimer != null) { global.clearTimeout(countdownTimer); countdownTimer = null; }
      }
      function schedule() {
        if (disposed) return;
        if (timer != null) global.clearTimeout(timer);
        timer = global.setTimeout(() => { timer = null; load(true); }, inProgress() ? FAST_POLL_MS : SLOW_POLL_MS);
        if (countdownTimer != null) global.clearTimeout(countdownTimer);
        countdownTimer = null;
        if (row && row.command_status === 'aguardando_leitura') tickCountdown();
      }
      function tickCountdown() {
        if (disposed) return;
        countdownTimer = global.setTimeout(() => {
          countdownTimer = null;
          if (disposed || !row || row.command_status !== 'aguardando_leitura') return;
          const node = SDR.qs('[data-wa-countdown]', body);
          if (node) node.textContent = countdownText();
          tickCountdown();
        }, 1000);
      }
      async function load(quiet) {
        const ticket = ++revision;
        try {
          const next = await ctx.api.getWhatsappConnection();
          if (disposed || ticket !== revision) return;
          row = next || null; rowReadAt = Date.now(); loadError = ''; failStreak = 0;
          if (notice && notice.pending && row && !['pendente', 'em_andamento'].includes(row.command_status)) notice = null;
        } catch (error) {
          if (disposed || ticket !== revision) return;
          failStreak++;
          // Falha pontual no polling mantém a tela atual; só a primeira carga mostra o erro.
          if (!quiet || !row) loadError = error && error.message || 'Não foi possível carregar o estado do WhatsApp.';
        }
        if (signature() !== lastSig) draw();
        else { const node = SDR.qs('[data-wa-countdown]', body); if (node) node.textContent = countdownText(); }
        schedule();
      }
      async function request(command) {
        if (sending || disposed) return;
        sending = true; notice = null; draw();
        try {
          const result = await ctx.api.requestWhatsappCommand(command);
          if (disposed) return;
          if (!result || !result.ok) notice = {kind: 'erro', text: COMMAND_ERRORS[result && result.error] || 'Não foi possível enviar o pedido ao motor. Tente de novo.'};
          else { notice = {kind: 'info', text: command === 'logout' ? 'Pedido de desconexão enviado. O motor vai desconectar o número em instantes.' : 'Pedido enviado ao motor.', pending: true}; }
        } catch (error) {
          if (disposed) return;
          notice = {kind: 'erro', text: error && error.message || 'Não foi possível enviar o pedido ao motor. Tente de novo.'};
        } finally { sending = false; }
        if (disposed) return;
        await load(false);
      }
      async function handleClick(event) {
        const button = event.target && event.target.closest ? event.target.closest('button') : null;
        if (!button || button.disabled) return;
        const action = button.dataset && button.dataset.waAction;
        if (action === 'reload') { loadError = ''; await load(false); }
        else if (action === 'connect') await request('connect');
        else if (action === 'logout') {
          const ok = await ctx.confirm({title: 'Trocar o número do atendimento', message: 'Isso desconecta o número atual do agente. Depois você lê o QR Code com o WhatsApp do número novo. Continuar?', confirmLabel: 'Desconectar e trocar', danger: true});
          if (ok && !disposed) await request('logout');
        }
      }
      root.addEventListener('click', handleClick);
      await load(false);
      return () => { disposed = true; revision++; stopTimers(); root.removeEventListener('click', handleClick); };
    }
  };
})(typeof window !== 'undefined' ? window : globalThis);
