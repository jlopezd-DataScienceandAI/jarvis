/**
 * Puente entre J.A.R.V.I.S. y Google Calendar.
 *
 * Jarvis envía aquí las tareas con fecha y este script las convierte en eventos del
 * calendario «Jarvis» de tu cuenta (lo crea si no existe). No toca ningún otro calendario.
 *
 * Instalación: ejecuta «jarvis calendar setup» en el PC y sigue los pasos que indica.
 * Necesita el servicio «Google Calendar API» (panel izquierdo, Servicios, +).
 */

var KEY = '__JARVIS_KEY__';
var CALENDAR_NAME = 'Jarvis';
var VERSION = 1;

function doGet() {
  return ContentService.createTextOutput('Puente de Jarvis activo.');
}

function doPost(e) {
  var reply;
  try {
    var request = JSON.parse(e.postData.contents);
    if (request.key !== KEY) {
      reply = {ok: false, error: 'bad_key'};
    } else if (typeof Calendar === 'undefined') {
      reply = {ok: false, error: 'no_service'};
    } else {
      // Una petición cada vez: dos a la vez podrían crear dos calendarios «Jarvis».
      var lock = LockService.getScriptLock();
      lock.waitLock(30000);
      try {
        reply = handle_(request);
      } finally {
        lock.releaseLock();
      }
    }
  } catch (err) {
    reply = {ok: false, error: message_(err)};
  }
  return ContentService.createTextOutput(JSON.stringify(reply)).setMimeType(ContentService.MimeType.JSON);
}

function handle_(request) {
  var calendarId = calendarId_();
  if (request.action === 'ping') {
    return {ok: true, version: VERSION, calendar: CALENDAR_NAME};
  }
  if (request.action !== 'apply') {
    return {ok: false, error: 'unknown_action'};
  }
  var done = {ok: true, version: VERSION, created: [], deleted: [], failed: {}};
  (request.delete || []).forEach(function (id) {
    try {
      remove_(calendarId, id);
      done.deleted.push(id);
    } catch (err) {
      done.failed[id] = message_(err);
    }
  });
  (request.create || []).forEach(function (item) {
    try {
      put_(calendarId, item);
      done.created.push(item.id);
    } catch (err) {
      done.failed[item.id] = message_(err);
    }
  });
  return done;
}

/** El calendario «Jarvis» del usuario; se crea la primera vez. */
function calendarId_() {
  var options = {minAccessRole: 'owner', maxResults: 250};
  do {
    var page = Calendar.CalendarList.list(options);
    var items = page.items || [];
    for (var i = 0; i < items.length; i++) {
      if (items[i].summary === CALENDAR_NAME) return items[i].id;
    }
    options.pageToken = page.nextPageToken;
  } while (options.pageToken);
  var created = Calendar.Calendars.insert({
    summary: CALENDAR_NAME,
    description: 'Avisos y tareas creados por J.A.R.V.I.S.',
    timeZone: Calendar.Calendars.get('primary').timeZone
  });
  return created.id;
}

function existing_(calendarId, id) {
  try {
    return Calendar.Events.get(calendarId, id);
  } catch (err) {
    return null;
  }
}

/** Crea el evento. Repetir la misma petición no lo duplica: el id lo fija Jarvis. */
function put_(calendarId, item) {
  var event = {
    id: item.id,
    summary: item.title,
    description: 'Creado por J.A.R.V.I.S.',
    start: item.start,
    end: item.end,
    status: 'confirmed',
    reminders: typeof item.reminder === 'number'
      ? {useDefault: false, overrides: [{method: 'popup', minutes: item.reminder}]}
      : {useDefault: true}
  };
  var found = existing_(calendarId, item.id);
  if (!found) {
    Calendar.Events.insert(event, calendarId);
  } else if (found.status === 'cancelled') {
    // Un evento borrado conserva su id: se recupera en lugar de crear otro.
    Calendar.Events.update(event, calendarId, item.id);
  }
}

/** Borra el evento. Si ya no existe (por ejemplo, lo borraste en el móvil), lo da por hecho. */
function remove_(calendarId, id) {
  try {
    Calendar.Events.remove(calendarId, id);
  } catch (err) {
    var found = existing_(calendarId, id);
    if (found && found.status !== 'cancelled') throw err;
  }
}

function message_(err) {
  return String((err && err.message) || err);
}
