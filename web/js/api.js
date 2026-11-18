(function (global) {
  'use strict';
  const SDR = global.SDR;
  if (!SDR) return;
  const LIB = 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/dist/umd/supabase.js';
  const LIB_SRI = 'sha384-0w2KAL2YHP6wKOkUDzkCDGgVvfmHnj02DHeQ6XcHOgTfFsGyonKOpShMH1x6nk9o';
  SDR.api = SDR.config.demo === true ? SDR.apiDemo : SDR.apiLive;
  function loadLibrary() {
    if (global.supabase && typeof global.supabase.createClient === 'function') return Promise.resolve();
    return new Promise((resolve, reject) => {
      if (!global.document) { reject(new Error('Não foi possível carregar a conexão com o banco. Tente de novo.')); return; }
      let script = global.document.querySelector('script[data-sdr-supabase]');
      if (!script) { script = global.document.createElement('script'); script.src = LIB; script.integrity = LIB_SRI; script.crossOrigin = 'anonymous'; script.async = true; script.dataset.sdrSupabase = 'true'; }
      script.addEventListener('load', () => {
        if (global.supabase && typeof global.supabase.createClient === 'function') resolve();
        else { script.remove(); reject(new Error('Não foi possível iniciar a conexão com o banco. Tente de novo.')); }
      }, { once: true });
      script.addEventListener('error', () => { script.remove(); reject(new Error('Não foi possível carregar a conexão com o banco. Verifique sua internet e tente de novo.')); }, { once: true });
      if (!script.isConnected) global.document.head.appendChild(script);
    });
  }
  // O painel pode chamar SDR.api imediatamente; o adapter live aguarda SDR.ready.
  // Chamadas subsequentes de initConnection permitem repetir o carregamento após erro de rede.
  SDR.initConnection = function () {
    const ready = Promise.resolve().then(async () => {
      if (!SDR.requireConfigOrExplain(null)) throw new Error(SDR.configProblem === 'chave' ? 'Configuração inválida: chave errada, use a anon/publishable, nunca a service_role.' : 'Configuração não encontrada. Rode o INICIAR SETUP ou crie web/config.js a partir de config.example.js.');
      if (!SDR.api) throw new Error('Não foi possível carregar os arquivos da aplicação. Atualize a página.');
      if (SDR.config.demo === true) { if (SDR.api.init) SDR.api.init(); return SDR.api; }
      await loadLibrary();
      if (!SDR.client) SDR.client = global.supabase.createClient(SDR.config.supabaseUrl, SDR.config.supabaseAnonKey, { auth: { detectSessionInUrl: false } });
      // Anônimo independente: nunca herda o login do painel nem grava sessão.
      if (!SDR.publicClient) SDR.publicClient = global.supabase.createClient(SDR.config.supabaseUrl, SDR.config.supabaseAnonKey, { auth: { persistSession: false, autoRefreshToken: false, detectSessionInUrl: false, storageKey: 'sdr-public-anon' } });
      SDR.api.init(SDR.client, SDR.publicClient);
      return SDR.api;
    }).catch(error => { throw new Error(error && /^(Configuração|Não foi possível)/.test(error.message) ? error.message : 'Não foi possível iniciar a conexão com o banco. Confira web/config.js e tente de novo.'); });
    SDR.ready = ready;
    // Evita rejeição não tratada em páginas que exibem primeiro o aviso de configuração.
    ready.catch(() => {});
    return ready;
  };
  SDR.initConnection();
})(typeof window !== 'undefined' ? window : globalThis);
