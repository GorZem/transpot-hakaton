async function api(path, opts = {}) {
  const r = await fetch(path, {headers: {'Content-Type': 'application/json'}, ...opts});
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail; } catch (e) {}
    throw {status: r.status, detail};
  }
  const ct = r.headers.get('content-type') || '';
  return ct.includes('json') ? r.json() : r.text();
}
const post = (path, body) => api(path, {method: 'POST', body: JSON.stringify(body || {})});
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function toast(msg, kind = 'info') {
  let t = $('#toast');
  if (!t) { t = document.createElement('div'); t.id = 'toast'; document.body.appendChild(t); }
  t.textContent = msg; t.className = 'show ' + kind;
  clearTimeout(t._h); t._h = setTimeout(() => t.className = '', 3500);
}
const MODE_RU = {adaptive: 'Адаптивный', degraded: 'Деградированный', fixed: 'Фиксированный цикл', flashing: 'Жёлтый мигающий'};
const PHASE_RU = {
  veh_green: 'Зелёный ТС', veh_green_blink: 'Зелёный мигающий ТС', veh_yellow: 'Жёлтый ТС',
  all_red_to_ped: 'Всё красное', ped_green: 'Зелёный пешеходам', ped_green_blink: 'Зелёный мигающий пешеходам',
  all_red_to_veh: 'Всё красное (очистка)', flashing_yellow: 'Жёлтый мигающий',
};
const STATUS_RU = {ok: 'исправна', lost: 'нет сигнала', frozen: 'кадр завис', dark: 'нет изображения', error: 'ошибка', disabled: 'отключена', starting: 'запуск'};
