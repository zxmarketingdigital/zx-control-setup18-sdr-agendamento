/**
 * Ponte opcional com o Google Agenda para o Agente SDR de Agendamento.
 *
 * Como usar:
 * 1. Acesse script.google.com com a conta Google dona da agenda e crie um projeto novo.
 * 2. Cole este arquivo inteiro e troque SEGREDO por uma senha longa e só sua.
 * 3. Implantar > Nova implantação > Tipo: App da Web.
 *    Executar como: Eu. Quem pode acessar: Qualquer pessoa.
 * 4. Copie a URL gerada e preencha em config/config.json:
 *    "google_calendar": { "apps_script_url": "<a URL>", "secret": "<o mesmo SEGREDO>" }
 *
 * O agendamento NUNCA depende do Google Agenda: se a ponte falhar, a call continua marcada no painel.
 */

var SEGREDO = 'TROQUE-ESTE-SEGREDO';
var AGENDA_ID = 'primary'; // 'primary' = agenda principal; ou o e-mail de uma agenda específica

function doPost(e) {
  // A implantação é pública ("Qualquer pessoa"): sem um segredo próprio e longo, ninguém pode usar a ponte.
  if (SEGREDO === 'TROQUE-ESTE-SEGREDO' || String(SEGREDO).length < 16) {
    return responder_({ ok: false, error: 'troque o segredo: edite SEGREDO no script (mínimo 16 caracteres) e reimplante' });
  }
  var body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return responder_({ ok: false, error: 'json_invalido' });
  }
  if (!body || body.secret !== SEGREDO) {
    return responder_({ ok: false, error: 'nao_autorizado' });
  }

  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(20000);
  } catch (err) {
    return responder_({ ok: false, error: 'ocupado' });
  }

  try {
    if (body.action === 'ping') {
      return responder_({ ok: true });
    }
    if (body.action === 'create') {
      return responder_(criar_(body));
    }
    if (body.action === 'cancel') {
      return responder_(cancelar_(body));
    }
    return responder_({ ok: false, error: 'acao_desconhecida' });
  } catch (err) {
    return responder_({ ok: false, error: String(err) });
  } finally {
    lock.releaseLock();
  }
}

// Mesma agenda para criar e cancelar (AGENDA_ID, com a agenda padrão como reserva).
function agenda_() {
  var agenda = CalendarApp.getCalendarById(AGENDA_ID === 'primary' ? Session.getActiveUser().getEmail() : AGENDA_ID);
  return agenda || CalendarApp.getDefaultCalendar();
}

function criar_(body) {
  var agenda = agenda_();
  var opcoes = { description: body.description || '' };
  if (body.guest_email) {
    opcoes.guests = body.guest_email;
    opcoes.sendInvites = true;
  }
  var evento = agenda.createEvent(body.title || 'Conversa agendada', new Date(body.start), new Date(body.end), opcoes);
  return { ok: true, id: evento.getId() };
}

function cancelar_(body) {
  if (!body.id) {
    return { ok: false, error: 'id_ausente' };
  }
  var agenda = agenda_();
  var evento = agenda.getEventById(body.id);
  if (!evento) {
    return { ok: true, ja_removido: true };
  }
  evento.deleteEvent();
  return { ok: true };
}

function responder_(objeto) {
  return ContentService.createTextOutput(JSON.stringify(objeto)).setMimeType(ContentService.MimeType.JSON);
}
