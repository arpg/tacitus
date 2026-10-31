/* Tacitus web chat: the client. One file, no framework, no build step.
 *
 * The page reads the API under the path it was served from (the base
 * path, such as /chat), shows the channels, the threads of the open
 * channel and the open thread, and follows the server's event stream.
 * Text from the server reaches the page only as text nodes: the markdown
 * renderer builds every element itself and never parses HTML.
 */
'use strict';

const BASE = location.pathname.replace(/\/index\.html$/, '').replace(/\/+$/, '');
const enc = encodeURIComponent;
const EVENT_TYPES = ['message', 'update', 'reaction', 'card', 'status', 'file', 'channel'];
const CREATED = new Set(['message', 'card', 'file']); // the event types that post a message
const KIND_ORDER = { dm: 0, main: 1, sched: 2, room: 3 };
const NEW_THREAD = '+'; // never a thread key: every key opens with a letter or digit
const PAGE = 50;
const QUOTED_LABEL = 'Rest of the body: text that matches an earlier message of the thread';
const CANCEL_WORDS = ['cancel', 'keep', 'skip', 'dismiss', 'one by one'];
const RECIPIENT_LABELS = new Set(['to', 'cc', 'bcc', 'reply-to', 'from', 'attendees', 'optional']);

// Shortcode -> glyph, under the Slack name and the Zulip name of each emoji.
const EMOJI = Object.fromEntries(Object.entries({
  '🔒': 'lock locked', '🔓': 'unlock unlocked', '📧': 'email e-mail', '✉️': 'envelope',
  '📨': 'incoming_envelope mail_received', '📩': 'envelope_with_arrow mail_sent',
  '📥': 'inbox_tray inbox', '📤': 'outbox_tray outbox', '📬': 'mailbox', '📅': 'calendar date',
  '🗓️': 'spiral_calendar_pad spiral_calendar calendar_spiral', '⚠️': 'warning',
  '✅': 'white_check_mark check', '✔️': 'heavy_check_mark check_mark',
  '☑️': 'ballot_box_with_check checkbox', '❌': 'x cross_mark',
  '✖️': 'heavy_multiplication_x multiplication', '👀': 'eyes',
  '⏳': 'hourglass_flowing_sand time_ticking', '⌛': 'hourglass', '❓': 'question',
  '❔': 'grey_question', '❗': 'exclamation heavy_exclamation_mark', '❕': 'grey_exclamation',
  '⏸️': 'double_vertical_bar pause pause_button', '👍': '+1 thumbsup thumbs_up',
  '👎': '-1 thumbsdown thumbs_down', '✏️': 'pencil2 pencil', '📝': 'memo', '📎': 'paperclip',
  '🔗': 'link', '🔍': 'mag search', '🔔': 'bell', '📌': 'pushpin push_pin',
  '🤖': 'robot_face robot', '📋': 'clipboard', '📜': 'scroll', '🚧': 'construction',
  '🚨': 'rotating_light', '⚡': 'zap', '🔴': 'red_circle large_red_circle',
  '🟢': 'large_green_circle green_circle', '🟡': 'large_yellow_circle yellow_circle',
  '⚪': 'white_circle', '💬': 'speech_balloon', '💭': 'thought_balloon thought',
  '⏰': 'alarm_clock', '⏱️': 'stopwatch', '⏲️': 'timer_clock timer', '🕐': 'clock clock1',
  '⭐': 'star', '✨': 'sparkles', '🎉': 'tada', '🤔': 'thinking_face thinking',
  '✋': 'raised_hand', '👉': 'point_right', '➡️': 'arrow_right right', '⬆️': 'arrow_up',
  '⬇️': 'arrow_down', '🔄': 'arrows_counterclockwise counterclockwise', '🔁': 'repeat',
  '🆕': 'new', '🆘': 'sos', '💡': 'bulb', '🔥': 'fire', '🚀': 'rocket', '⚙️': 'gear',
  '🛠️': 'hammer_and_wrench', '📦': 'package', '💰': 'moneybag money', '💵': 'dollar',
  '📈': 'chart_with_upwards_trend chart', '📊': 'bar_chart', '📚': 'books', '🏠': 'house',
  '🏢': 'office', '✈️': 'airplane', '✍️': 'writing_hand writing',
  '📞': 'telephone_receiver landline', '📱': 'phone iphone', '🎥': 'movie_camera video_camera',
  '🤝': 'handshake', '👥': 'busts_in_silhouette silhouettes', '👤': 'bust_in_silhouette silhouette',
  '🔑': 'key', '🛡️': 'shield', '⛔': 'no_entry', '🚫': 'no_entry_sign prohibited',
  '🛑': 'stop_sign octagonal_sign', '🗑️': 'wastebasket', '📁': 'file_folder',
  '🗂️': 'card_index_dividers', '🗄️': 'file_cabinet', '📇': 'card_index',
  '📄': 'page_facing_up document', '📰': 'newspaper', '🏷️': 'label', '🧹': 'broom',
  '♻️': 'recycle', '👌': 'ok_hand', '🎯': 'dart', '🍂': 'leaves', '🌐': 'globe_with_meridians',
  '🎓': 'mortar_board', '☀️': 'sunny', '☕': 'coffee', '💤': 'zzz', '👋': 'wave', '🙏': 'pray',
  '👏': 'clap', '❤️': 'heart', 'ℹ️': 'information_source info', '🖼️': 'image frame_with_picture',
}).flatMap(([glyph, names]) => names.split(' ').map((n) => [n, glyph])));

const S = {
  me: null,
  channels: [],
  channel: '',          // the open channel id
  thread: '',           // the open thread key, NEW_THREAD, or ''
  threads: new Map(),   // channel id -> {list: [parent messages], more}
  msgs: new Map(),      // channel + thread key -> [messages], oldest first
  known: new Set(),     // channel + message id, for every message seen
  baseline: new Map(),  // channel id -> the server's unread count, until opened
  threadUnread: new Map(),
  nodes: new Map(),     // message id -> element, for the open thread
  lastSeq: 0,
  es: null,
  retry: 0,
  ready: false,
  queue: [],
  route: 0,
  sending: false,
  push: undefined,      // {reg, key, sub} once the toggle is ready
};

const $ = (id) => document.getElementById(id);
const tkey = (ch, tk) => ch + '\n' + tk;
const threadOf = (m) => m.thread_key || m.id;

/* -- DOM and HTTP helpers ------------------------------------------- */

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : String(v));
  }
  for (const kid of kids.flat()) {
    if (kid != null && kid !== false) el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

function api(path) { return BASE + '/api' + path; }

async function call(method, path, body) {
  const opt = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) {
    opt.body = JSON.stringify(body);
    opt.headers['Content-Type'] = 'application/json';
  }
  const res = await fetch(api(path), opt);
  let data = null;
  try { data = await res.json(); } catch { /* an empty or non-JSON body */ }
  if (res.status === 401) { showPair(); throw new Error('This browser is not paired.'); }
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}

function toast(text) {
  const el = $('toast');
  el.textContent = text;
  el.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { el.hidden = true; }, 6000);
}

/* -- Text: cleaning, inline markup, blocks -------------------------- */

// Characters that change how text displays without being seen: shown as
// a visible mark on cards, as the core's card_text.clean does.
const HIDDEN_RE = /[\p{Cc}\p{Cf}\p{Co}\p{Cs}\p{Cn}\u034F\u115F\u1160\u17B4\u17B5\u180B-\u180F\u2800\u3164\uFFA0\uFFF9-\uFFFB\u{E0100}-\u{E01EF}]/gu;

function clean(text, keepInert) {
  const s = String(text ?? '').replace(/\r\n|[\r\u2028\u2029\x85\x0b\x0c]/g, '\n');
  return s.replace(HIDDEN_RE, (ch, at, all) => {
    if (ch === '\t' || ch === '\n') return ch;
    if (keepInert && (ch === '\uE000' || ch === '\uE001')) return ch;
    if (ch === '\u200D' || ch === '\u200C') {
      const prev = [...all.slice(0, at)].pop() || '';
      const next = String.fromCodePoint(all.codePointAt(at + 1) || 32);
      const ok = ch === '\u200D'
        ? /[\p{So}\p{Sk}\uFE0F]/u.test(prev) && /[\p{So}\p{Sk}\uFE0F]/u.test(next)
        : prev > '\x7f' && next > '\x7f' && /\p{L}/u.test(prev) && /\p{L}/u.test(next);
      if (ok) return ch;
    }
    return `[U+${ch.codePointAt(0).toString(16).toUpperCase().padStart(4, '0')}]`;
  });
}

function oneLine(text, limit = 300, keepInert = false) {
  const flat = clean(text, keepInert).split(/\s+/).filter(Boolean).join(' ');
  if (!limit || flat.length <= limit) return flat;
  const keep = Math.max(1, limit - 32);
  let head = flat.slice(0, keep).trimEnd();
  if (head.lastIndexOf('\uE000') > head.lastIndexOf('\uE001')) head += '\uE001';
  return `${head} [cut: ${flat.length - keep} more characters]`;
}

const stripInert = (s) => String(s ?? '').replace(/[\uE000\uE001]/g, '');

const INLINE = {
  inert: /\uE000(?<inert>[^\uE000\uE001]*)\uE001/,
  code: /(?<tick>`+)(?<code>[^\n]+?)\k<tick>(?!`)/,
  mdlink: /\[(?<label>[^\]\n]+)\]\((?<href>[^()\s]+)\)/,
  angle: /<(?<aurl>(?:https?|mailto):[^|>\s]+)(?:\|(?<alabel>[^>\n]*))?>/,
  bold: /\*\*(?=\S)(?<bold>[^\n]*?\S)\*\*/,
  strike: /~~(?=\S)(?<strike>[^\n]*?\S)~~/,
  star: /(?<![\w*])\*(?=[^\s*])(?<star>[^*\n]*?[^\s*])\*(?![\w*])/,
  under: /(?<![\w_])_(?=[^\s_])(?<under>[^_\n]*?[^\s_])_(?![\w_])/,
  tilde: /(?<![\w~])~(?=[^\s~])(?<tilde>[^~\n]*?[^\s~])~(?![\w~])/,
  emoji: /:(?<emoji>[a-z0-9_+-]+):/,
  url: /(?<url>\bhttps?:\/\/[^\s<>]*[^\s<>.,;:!?'")\]*_~])/,
};
const join = (names) => names.map((n) => INLINE[n].source).join('|');
// Markdown is the text the core writes for Zulip; card text is Slack
// mrkdwn as the card builders write it (*bold*, _italic_, <url|label>).
const MD_SRC = join(['inert', 'code', 'mdlink', 'angle', 'bold', 'strike', 'star', 'emoji', 'url']);
const CARD_SRC = join(['inert', 'code', 'angle', 'bold', 'strike', 'star', 'under', 'tilde', 'emoji', 'url']);

function link(url, label) {
  if (!/^(https?:|mailto:)/i.test(url)) return null;
  return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, label || url);
}

function emojiNode(name) {
  const glyph = EMOJI[name];
  return glyph ? h('span', { class: 'emoji', title: `:${name}:`, role: 'img', 'aria-label': name }, glyph) : null;
}

// Appends `text` to `out` with inline markup; card mode reads Slack mrkdwn
// and shows a labelled link's target, as the Zulip card renderer does.
function inline(text, card, out) {
  const re = new RegExp(card ? CARD_SRC : MD_SRC, 'g');
  const wrap = (tag, inner) => inline(inner, card, h(tag));
  let last = 0;
  let m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.append(text.slice(last, m.index));
    last = re.lastIndex;
    const g = m.groups;
    if (g.inert !== undefined) out.append(h('code', { class: 'inert' }, g.inert));
    else if (g.code !== undefined) {
      const c = g.code;
      out.append(h('code', {}, /^ .*\S.* $/.test(c) ? c.slice(1, -1) : c));
    } else if (g.href !== undefined) out.append(link(g.href, g.label) || m[0]);
    else if (g.aurl !== undefined) {
      const label = (g.alabel || '').trim();
      if (card && label) out.append(label + ' (', link(g.aurl) || g.aurl, ')');
      else out.append(link(g.aurl, label) || m[0]);
    } else if (g.bold !== undefined) out.append(wrap('strong', g.bold));
    else if (g.strike !== undefined) out.append(wrap('s', g.strike));
    else if (g.star !== undefined) out.append(wrap(card ? 'strong' : 'em', g.star));
    else if (g.under !== undefined) out.append(wrap('em', g.under));
    else if (g.tilde !== undefined) out.append(wrap('s', g.tilde));
    else if (g.emoji !== undefined) {
      const node = emojiNode(g.emoji);
      if (node) out.append(node);
      else { // not an emoji: keep the text, and let its closing colon open the next one
        out.append(':' + g.emoji);
        last = re.lastIndex = re.lastIndex - 1;
      }
    } else if (g.url !== undefined) out.append(link(g.url) || g.url);
  }
  if (last < text.length) out.append(text.slice(last));
  return out;
}

const FENCE_RE = /^ {0,3}(`{3,}|~{3,})\s*(.*)$/;
const HEADING_RE = /^ {0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
const RULE_RE = /^ {0,3}([-*_])(\s*\1){2,}\s*$/;
const QUOTE_RE = /^ {0,3}>/;
const LIST_RE = /^(\s*)([-*+]|\d{1,9}[.)])\s+(.*)$/;
const TABLE_RE = /^\s*\|.*\|\s*$/;
const startsBlock = (ln) => FENCE_RE.test(ln) || HEADING_RE.test(ln) || RULE_RE.test(ln)
  || QUOTE_RE.test(ln) || LIST_RE.test(ln);

function list(lines, i, out) {
  const ordered = /\d/.test(LIST_RE.exec(lines[i])[2]);
  const el = h(ordered ? 'ol' : 'ul');
  const start = parseInt(LIST_RE.exec(lines[i])[2], 10);
  if (ordered && start !== 1) el.setAttribute('start', String(start));
  let li = null;
  for (; i < lines.length; i++) {
    const m = LIST_RE.exec(lines[i]);
    if (m && /\d/.test(m[2]) === ordered) {
      const depth = Math.min(3, Math.floor(m[1].replace(/\t/g, '  ').length / 2));
      li = h('li', depth ? { class: 'd' + depth } : {});
      el.append(inline(m[3], false, li));
    } else if (!m && li && /^\s+\S/.test(lines[i])) {
      li.append(h('br'));
      inline(lines[i].trim(), false, li);
    } else break;
  }
  out.append(el);
  return i;
}

function table(lines, i, out) {
  const cells = (ln) => ln.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((c) => c.trim());
  const head = h('tr', {}, cells(lines[i]).map((c) => inline(c, false, h('th'))));
  const body = h('tbody');
  for (i += 2; i < lines.length && TABLE_RE.test(lines[i]); i++) {
    body.append(h('tr', {}, cells(lines[i]).map((c) => inline(c, false, h('td')))));
  }
  out.append(h('div', { class: 'table-wrap' }, h('table', {}, h('thead', {}, head), body)));
  return i;
}

// Zulip-flavoured markdown to a fragment: fences (and spoiler and quote
// fences), headings, rules, quotes, lists, pipe tables and paragraphs.
function markdown(text) {
  const out = document.createDocumentFragment();
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  let i = 0;
  while (i < lines.length) {
    const ln = lines[i];
    let m;
    if (!ln.trim()) { i++; continue; }
    if ((m = FENCE_RE.exec(ln))) {
      const close = new RegExp(`^ {0,3}\\${m[1][0]}{${m[1].length},}\\s*$`);
      const body = [];
      for (i++; i < lines.length && !close.test(lines[i]); i++) body.push(lines[i]);
      i++;
      const info = m[2].trim();
      const src = body.join('\n');
      if (/^spoiler\b/.test(info)) {
        out.append(h('details', { class: 'spoiler' },
          h('summary', {}, info.slice(7).trim() || 'Hidden text'), markdown(src)));
      } else if (info === 'quote') out.append(h('blockquote', {}, markdown(src)));
      else out.append(h('pre', {}, h('code', {}, src)));
    } else if ((m = HEADING_RE.exec(ln))) {
      out.append(inline(m[2], false, h('h' + Math.min(6, m[1].length + 2))));
      i++;
    } else if (RULE_RE.test(ln)) {
      out.append(h('hr'));
      i++;
    } else if (QUOTE_RE.test(ln)) {
      const q = [];
      while (i < lines.length && QUOTE_RE.test(lines[i])) q.push(lines[i++].replace(/^ {0,3}> ?/, ''));
      out.append(h('blockquote', {}, markdown(q.join('\n'))));
    } else if (LIST_RE.test(ln)) {
      i = list(lines, i, out);
    } else if (TABLE_RE.test(ln) && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1] || '')) {
      i = table(lines, i, out);
    } else {
      const p = h('p');
      do {
        if (p.firstChild) p.append(h('br'));
        inline(lines[i++], false, p);
      } while (i < lines.length && lines[i].trim() && !startsBlock(lines[i]));
      out.append(p);
    }
  }
  return out;
}

// Card text (a title, a line, the footer, a status): cleaned, inert spans
// kept literal, each line inline only, so nothing in it opens a block.
function cardText(text, cls) {
  const box = h('div', { class: cls || 'card-line' });
  clean(text, true).split('\n').filter((ln) => ln.trim()).forEach((ln, k) => {
    if (k) box.append(h('br'));
    inline(ln, true, box);
  });
  return box;
}

function emojify(s) {
  return s.replace(/:([a-z0-9_+-]+):/g, (all, n) => EMOJI[n] || all);
}

function plainLine(text) {
  const line = String(text ?? '').split('\n').map((s) => s.trim()).find(Boolean) || '';
  return emojify(stripInert(line)
    .replace(/^(?:[>#]+\s*)+/, '')
    .replace(/\[([^\]]*)\]\([^)\s]*\)/g, '$1')
    .replace(/<[^<>|\s]+\|([^<>]*)>/g, '$1')
    .replace(/[*~`]|(?<!\w)_|_(?!\w)/g, ''));
}

function threadTitle(m) {
  if (!m) return '';
  let t = '';
  if (m.card && m.card.title) t = plainLine(String(m.card.title).replace(/^\s*:(?:lock|locked):\s*/, ''));
  if (!t) t = plainLine(m.text);
  if (!t && m.file) t = m.file.name || 'File';
  return (t.length > 90 ? t.slice(0, 88) + '...' : t) || '(no text)';
}

/* -- Rendering: times, sizes, files, cards, messages ---------------- */

function when(ts, short) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const now = new Date();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (d.toDateString() === now.toDateString()) return time;
  const opts = (now - d) < 6 * 864e5 ? { weekday: 'short' }
    : { month: 'short', day: 'numeric', ...(d.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) };
  const day = d.toLocaleDateString([], opts);
  return short ? day : `${day} ${time}`;
}

function size(n) {
  if (!(n >= 0)) return '';
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(n < 10240 ? 1 : 0)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function fileUrl(f) {
  if (f.id) return `${BASE}/api/files/${enc(f.id)}`;
  return /^https?:/i.test(f.url || '') ? f.url : null;
}

function renderFile(f) {
  const url = fileUrl(f);
  const box = h('div', { class: 'file' });
  if (url && /^image\//.test(f.mimetype || '')) {
    box.append(h('a', { href: url, target: '_blank', rel: 'noopener' },
      h('img', { src: url, alt: f.name || 'image', loading: 'lazy' })));
  }
  const label = [emojiNode('paperclip'), ' ', h('span', { class: 'file-name' }, f.name || 'file'),
    f.size != null ? h('span', { class: 'size' }, size(f.size)) : null];
  box.append(url ? h('a', { class: 'file-link', href: url, download: f.name || true }, label)
    : h('span', { class: 'file-link' }, label));
  return box;
}

const norm = (w) => String(w || '').toLowerCase().split(/\s+/).filter(Boolean).join(' ');

// The action a check selects and the one a cross selects, as cards.py
// picks them: the primary is styled primary (else the first action, unless
// the card has no default); the cancel carries a cancel word, else it is
// the first danger action that is not the primary.
function primaryId(card, withDefault) {
  const acts = card.actions || [];
  const p = acts.find((a) => a.style === 'primary');
  if (p) return p.id;
  return acts.length && withDefault ? acts[0].id : null;
}

function cancelId(card) {
  const acts = card.actions || [];
  const w = acts.find((a) => CANCEL_WORDS.includes(norm(a.word)));
  if (w) return w.id;
  const prim = primaryId(card, true);
  const d = acts.find((a) => a.style === 'danger' && a.id !== prim);
  return d ? d.id : null;
}

async function act(msg, body, box) {
  const controls = [...box.querySelectorAll('.card-actions button, .card-actions select')];
  const note = box.querySelector('.card-note');
  controls.forEach((c) => { c.disabled = true; });
  note.textContent = 'Sending...';
  try {
    await call('POST', `/cards/${enc(msg.channel)}/${enc(msg.id)}/actions`, body);
    note.textContent = 'Sent. The card updates when the bot answers.';
  } catch (e) {
    note.textContent = `Not sent: ${e.message}`;
  } finally {
    controls.forEach((c) => { c.disabled = false; });
  }
}

function renderCard(msg) {
  const c = msg.card || {};
  const box = h('div', { class: 'card', 'data-kind': c.kind || '' });
  const title = String(c.title || '').replace(/^\s*:(?:lock|locked):\s*/, '');
  const head = cardText(title || 'Confirm', 'card-title');
  head.prepend(emojiNode('lock'), ' ');
  box.append(head);
  if ((c.fields || []).length) {
    const dl = h('dl', { class: 'card-fields' });
    for (const f of c.fields) {
      const limit = RECIPIENT_LABELS.has(norm(f.label)) ? 2000 : 300;
      dl.append(h('dt', {}, oneLine(f.label, 60)),
        h('dd', {}, h('code', { class: 'inert' }, oneLine(stripInert(f.value), limit))));
    }
    box.append(dl);
  }
  for (const ln of c.lines || []) box.append(cardText(ln));
  if (c.body) box.append(h('div', { class: 'card-label' }, 'Body'), h('pre', { class: 'card-body' }, clean(c.body)));
  if (c.quoted) {
    box.append(h('details', { class: 'card-quoted' }, h('summary', {}, QUOTED_LABEL),
      h('pre', { class: 'card-body' }, clean(c.quoted))));
  }
  const row = h('div', { class: 'card-actions' });
  const selects = [c.select, ...(c.selects || [])].filter((s) => s && (s.options || []).length);
  for (const s of selects) {
    const sel = h('select', { 'aria-label': oneLine(s.placeholder || 'Choose', 80) },
      h('option', { value: '', disabled: true, selected: true }, oneLine(s.placeholder || 'Choose', 80)));
    for (const o of s.options) {
      const [label, value] = Array.isArray(o) ? o : [o.label, o.value];
      sel.append(h('option', { value: String(value ?? '') }, oneLine(stripInert(label), 80)));
    }
    sel.addEventListener('change', () => act(msg, { action_id: s.id, selected: sel.value }, box));
    row.append(sel);
  }
  const prim = primaryId(c, !c.no_default);
  const canc = cancelId(c);
  for (const a of c.actions || []) {
    const cls = a.id === prim ? 'btn primary' : a.id === canc ? 'btn plain' : 'btn';
    const btn = h('button', { type: 'button', class: cls + (a.style === 'danger' ? ' danger' : ''), 'data-action': a.id });
    inline(oneLine(a.label || a.word || a.id, 80, true), true, btn);
    btn.addEventListener('click', () => act(msg, { action_id: a.id, value: a.value ?? '' }, box));
    row.append(btn);
  }
  if (row.firstChild) box.append(row);
  box.append(h('div', { class: 'card-note', 'aria-live': 'polite' }));
  if (c.footer) box.append(cardText(c.footer, 'card-footer'));
  return box;
}

function renderMsg(m) {
  const own = m.author === 'owner';
  const el = h('article', { class: `msg ${own ? 'own' : 'bot'} kind-${m.kind || 'message'}` });
  const edited = m.kind === 'message' && m.updated_at && m.posted_at && m.updated_at - m.posted_at > 1;
  el.append(h('header', { class: 'meta' },
    h('span', { class: 'who' }, own ? 'You' : 'Tacitus'),
    h('time', { datetime: new Date((m.posted_at || 0) * 1000).toISOString() }, when(m.posted_at)),
    edited ? h('span', { class: 'edited' }, 'edited') : null));
  if (m.kind === 'card' && m.card) el.append(renderCard(m));
  else if (m.kind === 'status') el.append(cardText(m.text, 'card status'));
  else {
    if (m.text) el.append(h('div', { class: 'text' }, markdown(m.text)));
    if (m.file) el.append(renderFile(m.file));
  }
  if ((m.reactions || []).length) {
    el.append(h('div', { class: 'reactions' }, m.reactions.map((r) =>
      h('span', { class: 'chip', title: `:${r}:` }, emojiNode(r) || `:${r}:`))));
  }
  return el;
}

/* -- Channels and threads ------------------------------------------- */

const chan = (id) => S.channels.find((c) => c.id === id);
const channelName = (c) => (c ? c.name || (c.kind === 'dm' ? 'Direct messages' : c.id) : '');
const hashFor = (ch, tk) => '#/' + enc(ch) + (tk ? '/' + enc(tk) : '');

function unreadFor(ch) {
  let n = S.baseline.get(ch) || 0;
  for (const [k, v] of S.threadUnread) if (k.startsWith(ch + '\n')) n += v;
  return n;
}

function renderChannels() {
  const box = $('channel-list');
  const rows = [...S.channels].sort((a, b) => (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9)
    || channelName(a).localeCompare(channelName(b)));
  let total = 0;
  const item = (c) => {
    const n = unreadFor(c.id);
    if (!c.archived) total += n;
    return h('a', {
      class: 'chan' + (c.id === S.channel ? ' active' : '') + (n ? ' unread' : ''),
      href: hashFor(c.id), title: c.description || null,
    }, h('span', { class: 'c-icon', 'aria-hidden': 'true' }, c.kind === 'dm' ? '@' : '#'),
    h('span', { class: 'c-name' }, channelName(c)),
    n ? h('span', { class: 'badge', 'aria-label': `${n} unread` }, String(n)) : null);
  };
  const live = rows.filter((c) => !c.archived).map(item);
  const old = rows.filter((c) => c.archived);
  box.replaceChildren(...live);
  if (old.length) {
    box.append(h('details', { class: 'archived', open: old.some((c) => c.id === S.channel) },
      h('summary', {}, `Archived (${old.length})`), old.map(item)));
  }
  document.title = total ? `(${total}) Tacitus` : 'Tacitus';
  const others = total - unreadFor(S.channel);
  $('menu').dataset.unread = others > 0 ? String(others) : '';
}

function renderThreads() {
  const box = $('thread-list');
  const th = S.threads.get(S.channel);
  if (!th) { box.replaceChildren(h('p', { class: 'empty' }, 'Loading...')); return; }
  const sorted = [...th.list].sort((a, b) => (b.last_at || b.posted_at || 0) - (a.last_at || a.posted_at || 0));
  box.replaceChildren(...sorted.map((p) => {
    const tk = threadOf(p);
    const n = S.threadUnread.get(tkey(S.channel, tk)) || 0;
    const replies = p.reply_count || 0;
    return h('a', { class: 'thread-item' + (tk === S.thread ? ' active' : '') + (n ? ' unread' : ''), href: hashFor(S.channel, tk) },
      h('span', { class: 't-title' }, threadTitle(p)),
      h('span', { class: 't-meta' }, `${replies} ${replies === 1 ? 'reply' : 'replies'} · ${when(p.last_at || p.posted_at, true)}`),
      n ? h('span', { class: 'badge' }, String(n)) : null);
  }));
  if (!th.list.length) box.append(h('p', { class: 'empty' }, 'No threads yet. Write a message to start one.'));
  if (th.more) {
    box.append(h('button', { type: 'button', class: 'btn plain more', onclick: () => loadThreads(S.channel, true) },
      'Load older threads'));
  }
}

function remember(m) { S.known.add(tkey(m.channel, m.id)); }

async function loadThreads(ch, older) {
  const th = S.threads.get(ch) || { list: [], more: false };
  const oldest = older ? Math.min(...th.list.map((p) => p.seq || Infinity)) : 0;
  let q = `?limit=${PAGE}`;
  if (older && Number.isFinite(oldest)) q += `&before=${oldest}`;
  try {
    const rows = await call('GET', `/channels/${enc(ch)}/threads${q}`);
    const list = older ? th.list : th.list.filter((p) => !rows.some((r) => threadOf(r) === threadOf(p)));
    for (const r of rows) {
      remember(r);
      const i = list.findIndex((p) => threadOf(p) === threadOf(r));
      if (i >= 0) list[i] = r; else list.push(r);
    }
    S.threads.set(ch, { list, more: rows.length >= PAGE });
  } catch (e) {
    toast(`Could not load threads: ${e.message}`);
    if (!S.threads.has(ch)) S.threads.set(ch, th);
  }
  if (ch === S.channel) renderThreads();
}

function renderThread() {
  const box = $('messages');
  const list = S.msgs.get(tkey(S.channel, S.thread)) || [];
  S.nodes = new Map();
  box.replaceChildren(...list.map((m) => {
    const el = renderMsg(m);
    S.nodes.set(m.id, el);
    return el;
  }));
  $('thread-title').textContent = threadTitle(list[0]) || 'Thread';
}

const nearBottom = () => {
  const box = $('messages');
  return box.scrollHeight - box.scrollTop - box.clientHeight < 120;
};
const toBottom = () => { const box = $('messages'); box.scrollTop = box.scrollHeight; };

async function openThread(ch, tk, token) {
  const box = $('messages');
  S.nodes = new Map();
  const isNew = !tk || tk === NEW_THREAD;
  $('text').placeholder = isNew ? `Start a thread in ${channelName(chan(ch))}` : 'Reply';
  if (isNew) {
    $('thread-title').textContent = 'New thread';
    box.replaceChildren(h('p', { class: 'empty' }, 'Pick a thread, or write a message to start a new one.'));
    return;
  }
  const key = tkey(ch, tk);
  if (S.msgs.has(key)) { renderThread(); toBottom(); } else {
    S.msgs.set(key, []); // collects what the stream brings while the read runs
    $('thread-title').textContent = 'Thread';
    box.replaceChildren(h('p', { class: 'empty' }, 'Loading...'));
  }
  try {
    const rows = await call('GET', `/channels/${enc(ch)}/threads/${enc(tk)}`);
    rows.forEach(remember);
    // The read wins, except where the stream brought a newer copy or a
    // message the read did not see yet.
    const held = new Map(S.msgs.get(key).map((m) => [m.id, m]));
    const merged = rows.map((r) => {
      const mine = held.get(r.id);
      held.delete(r.id);
      return mine && (mine.updated_at || 0) > (r.updated_at || 0) ? mine : r;
    }).concat([...held.values()]).sort((a, b) => (a.seq || 0) - (b.seq || 0));
    S.msgs.set(key, merged);
    if (token !== S.route) return;
    renderThread();
    toBottom();
  } catch (e) {
    if (token === S.route) box.replaceChildren(h('p', { class: 'empty' }, `Could not load the thread: ${e.message}`));
  }
}

/* -- Routing -------------------------------------------------------- */

function parseHash() {
  const dec = (s) => { try { return decodeURIComponent(s || ''); } catch { return ''; } };
  const [ch, tk] = location.hash.replace(/^#\/?/, '').split('/');
  return { ch: dec(ch), tk: dec(tk) };
}

function defaultChannel() {
  const open = S.channels.filter((c) => !c.archived);
  return open.find((c) => c.kind === 'dm') || open.find((c) => c.kind === 'main') || open[0] || S.channels[0];
}

async function route() {
  if (!S.ready) return;
  const { ch, tk } = parseHash();
  if (!chan(ch)) {
    const def = defaultChannel();
    if (def) location.replace(hashFor(def.id));
    else $('thread-list').replaceChildren(h('p', { class: 'empty' }, 'No channels yet.'));
    return;
  }
  const token = ++S.route;
  const changed = ch !== S.channel;
  S.channel = ch;
  S.thread = tk;
  S.baseline.delete(ch);
  if (tk) S.threadUnread.delete(tkey(ch, tk));
  document.body.classList.remove('drawer');
  document.body.dataset.view = tk ? 'thread' : 'threads';
  $('channel-name').textContent = channelName(chan(ch));
  renderChannels();
  renderThreads();
  if (changed || !S.threads.has(ch)) loadThreads(ch);
  await openThread(ch, tk, token);
}

/* -- Live events ---------------------------------------------------- */

function setLive(state) {
  const el = $('live');
  el.className = 'live ' + state;
  el.title = { on: 'Live', off: 'Reconnecting', wait: 'Connecting' }[state];
}

function connect() {
  if (S.es) S.es.close();
  const es = new EventSource(api('/events') + (S.lastSeq ? `?since=${S.lastSeq}` : ''));
  S.es = es;
  setLive('wait');
  EVENT_TYPES.forEach((t) => es.addEventListener(t, (ev) => onEvent(t, ev)));
  es.addEventListener('open', () => { S.retry = 0; setLive('on'); });
  es.addEventListener('error', () => {
    setLive('off');
    // A dropped connection reconnects by itself with Last-Event-ID; a
    // refused one (an HTTP error) is closed, so it reconnects from here.
    if (es.readyState === EventSource.CLOSED) setTimeout(() => reconnect(es), Math.min(30000, 1000 * 2 ** S.retry++));
  });
  return es;
}

async function reconnect(es) {
  if (S.es !== es) return;
  try {
    await call('GET', '/me');
    connect();
  } catch {
    if ($('pair').hidden) setTimeout(() => reconnect(es), Math.min(30000, 1000 * 2 ** S.retry++));
  }
}

function onEvent(type, ev) {
  const seq = Number(ev.lastEventId) || 0;
  if (seq && seq <= S.lastSeq) return;
  if (seq) S.lastSeq = seq;
  let data;
  try { data = JSON.parse(ev.data); } catch { return; }
  if (!data || typeof data !== 'object') return;
  if (S.ready) apply(type, data); else S.queue.push([type, data]);
}

function apply(type, data) {
  if (type === 'channel') {
    const i = S.channels.findIndex((c) => c.id === data.id);
    if (i >= 0) S.channels[i] = { ...S.channels[i], ...data }; else S.channels.push(data);
    renderChannels();
    if (data.id === S.channel) $('channel-name').textContent = channelName(chan(data.id));
    return;
  }
  if (data.channel && data.id) ingest(data, CREATED.has(type));
}

// Takes one message (from the stream or a post's answer) into every view
// that holds it. It is idempotent on (channel, id): a message seen before
// is replaced when it is as new as the copy held, never counted twice. Only
// a message just posted counts as unread or as a reply; an edit of one never
// loaded here does not.
function ingest(m, created = true) {
  const tk = threadOf(m);
  const key = tkey(m.channel, tk);
  const fresh = !m.updated_at || !m.posted_at || m.updated_at - m.posted_at < 2;
  const isNew = created && fresh && !S.known.has(tkey(m.channel, m.id));
  remember(m);
  const msgs = S.msgs.get(key);
  let merged = m;
  if (msgs) {
    const i = msgs.findIndex((x) => x.id === m.id);
    if (i < 0) msgs.push(m);
    else if ((m.updated_at || 0) >= (msgs[i].updated_at || 0)) merged = msgs[i] = { ...msgs[i], ...m };
    else merged = msgs[i];
  }
  const th = S.threads.get(m.channel);
  if (th) {
    const p = th.list.find((x) => threadOf(x) === tk);
    if (!p) {
      th.list.push({ ...m, reply_count: 0, last_at: m.posted_at });
      fixParent(m.channel, tk);
    } else {
      if (p.id === m.id) Object.assign(p, m, { reply_count: m.reply_count ?? p.reply_count });
      else if (isNew) p.reply_count = (p.reply_count || 0) + 1;
      p.last_at = Math.max(p.last_at || 0, m.posted_at || 0);
    }
  }
  const viewing = m.channel === S.channel && tk === S.thread;
  if (isNew && m.author !== 'owner' && !(viewing && document.visibilityState === 'visible')) {
    S.threadUnread.set(key, (S.threadUnread.get(key) || 0) + 1);
  }
  if (viewing) {
    const stick = nearBottom();
    const el = renderMsg(merged);
    const old = S.nodes.get(m.id);
    if (old) old.replaceWith(el);
    else {
      if (!S.nodes.size) $('messages').replaceChildren();
      $('messages').append(el);
    }
    S.nodes.set(m.id, el);
    if (stick || (isNew && m.author === 'owner')) toBottom();
    if (msgs && msgs[0] === merged) $('thread-title').textContent = threadTitle(merged);
  }
  renderChannels();
  if (m.channel === S.channel) renderThreads();
}

// A message in a thread the list does not hold: read the thread so its
// parent, not the newest reply, names it.
async function fixParent(ch, tk) {
  try {
    const rows = await call('GET', `/channels/${enc(ch)}/threads/${enc(tk)}`);
    const th = S.threads.get(ch);
    const p = th && th.list.find((x) => threadOf(x) === tk);
    if (!rows.length || !p) return;
    rows.forEach(remember);
    Object.assign(p, rows[0], { reply_count: rows.length - 1, last_at: rows[rows.length - 1].posted_at });
    if (ch === S.channel) renderThreads();
  } catch { /* the list keeps the message it has */ }
}

/* -- Composer and uploads ------------------------------------------- */

function note(text) { $('composer-note').textContent = text || ''; }

function grow() {
  const ta = $('text');
  ta.style.height = 'auto';
  ta.style.height = Math.min(ta.scrollHeight, 200) + 'px';
}

// Where a post goes: the hash, which a click changes at once, while the
// route that follows it runs a moment later.
function target() {
  const { ch, tk } = parseHash();
  return chan(ch) ? { ch, tk: tk && tk !== NEW_THREAD ? tk : null } : { ch: S.channel, tk: null };
}

async function submit(build, text) {
  if (S.sending || !S.channel) return;
  const { ch, tk } = target();
  S.sending = true;
  $('composer').classList.add('busy');
  try {
    const m = await build(ch, tk);
    if ($('text').value === text) $('text').value = ''; // keep what was typed meanwhile
    grow();
    note('');
    if (m && m.id) ingest(m);
    if (!tk && m && m.thread_key && ch === S.channel) location.hash = hashFor(ch, m.thread_key);
  } catch (e) {
    note(`Not sent: ${e.message}`);
  } finally {
    S.sending = false;
    $('composer').classList.remove('busy');
  }
}

function send() {
  const text = $('text').value;
  if (!text.trim()) return;
  submit((ch, tk) => call('POST', `/channels/${enc(ch)}/messages`, tk ? { text, thread_key: tk } : { text }), text);
}

function upload(file) {
  const text = $('text').value;
  note(`Uploading ${file.name} (${size(file.size)})...`);
  submit((ch, tk) => {
    const fd = new FormData();
    fd.append('file', file, file.name);
    if (text.trim()) fd.append('text', text);
    if (tk) fd.append('thread_key', tk);
    return call('POST', `/channels/${enc(ch)}/files`, fd);
  }, text);
}

/* -- Notifications (Web Push) --------------------------------------- */

// The server's public key (base64url) as the bytes subscribe() takes.
function keyBytes(b64) {
  const s = b64.replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(s + '='.repeat((4 - s.length % 4) % 4)), (c) => c.charCodeAt(0));
}

const sameKey = (buf, key) => !buf || String(new Uint8Array(buf)) === String(keyBytes(key));

// The drawer's toggle. A subscription this browser already holds is posted
// again, so the server keeps it after a reset; one made with another key
// (the server's key was replaced) is dropped. iOS takes notifications only
// in the app added to the Home Screen (navigator.standalone is true there).
async function initPush() {
  const box = $('notify');
  if (!S.me.push || !('serviceWorker' in navigator)) return;
  $('drawer-foot').hidden = false;
  $('notify-hint').hidden = navigator.standalone !== false;
  box.disabled = true;
  if (!('PushManager' in window) || !('Notification' in window)) return;
  const reg = await navigator.serviceWorker.ready;
  const { key } = await call('GET', '/push/vapid');
  let sub = await reg.pushManager.getSubscription();
  if (sub && !sameKey(sub.options.applicationServerKey, key)) { await sub.unsubscribe(); sub = null; }
  if (sub && Notification.permission === 'granted') await call('POST', '/push/subscribe', sub.toJSON());
  S.push = { reg, key, sub };
  box.checked = Boolean(sub) && Notification.permission === 'granted';
  box.disabled = false;
  box.addEventListener('change', toggleNotify);
}

async function toggleNotify() {
  const box = $('notify');
  const on = box.checked;
  box.disabled = true;
  try {
    let { reg, key, sub } = S.push;
    if (on) {
      // subscribe() asks for the permission; Safari asks only straight from a tap.
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(key) });
      await call('POST', '/push/subscribe', sub.toJSON());
    } else if (sub) {
      await call('POST', '/push/unsubscribe', { endpoint: sub.endpoint });
      await sub.unsubscribe();
      sub = null;
    }
    S.push.sub = sub;
  } catch (e) {
    box.checked = !on;
    toast(Notification.permission === 'denied'
      ? 'Notifications are blocked for this site in the browser settings.' : `Notifications: ${e.message}`);
  } finally {
    box.disabled = false;
  }
}

/* -- Pairing and start-up ------------------------------------------- */

function showPair() {
  if (!$('pair').hidden) return;
  if (S.es) { S.es.close(); S.es = null; }
  $('pair').hidden = false;
  $('pair-token').focus();
}

async function pair(e) {
  e.preventDefault();
  const token = $('pair-token').value.trim();
  if (!token) return;
  $('pair-error').textContent = '';
  try {
    const res = await fetch(api('/pair'), {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }),
    });
    if (!res.ok) throw new Error(res.status === 401 || res.status === 400 ? 'That token does not match.' : `HTTP ${res.status}`);
    $('pair').hidden = true;
    $('pair-token').value = '';
    boot();
  } catch (err) {
    $('pair-error').textContent = err.message;
  }
}

async function boot() {
  try {
    S.me = await call('GET', '/me');
  } catch (e) {
    if ($('pair').hidden) toast(`Could not reach the bot: ${e.message}`);
    return;
  }
  const es = connect();
  // Wait for the stream before the first reads, so no event falls between them.
  await new Promise((resolve) => {
    es.addEventListener('open', resolve, { once: true });
    setTimeout(resolve, 3000);
  });
  try {
    const rows = await call('GET', '/channels');
    S.channels = rows || [];
    for (const c of S.channels) if (c.unread) S.baseline.set(c.id, Number(c.unread) || 0);
  } catch (e) {
    toast(`Could not load channels: ${e.message}`);
    return;
  }
  S.ready = true;
  S.queue.splice(0).forEach(([t, d]) => apply(t, d));
  renderChannels();
  route();
  if (S.push === undefined) { S.push = null; initPush().catch(() => {}); }
}

function init() {
  window.addEventListener('hashchange', route);
  $('menu').addEventListener('click', () => document.body.classList.toggle('drawer'));
  $('scrim').addEventListener('click', () => document.body.classList.remove('drawer'));
  // A tap on the open channel changes no hash, so the drawer closes here.
  $('channel-list').addEventListener('click', (e) => {
    if (e.target.closest('a.chan')) document.body.classList.remove('drawer');
  });
  $('back').addEventListener('click', () => { location.hash = hashFor(S.channel); });
  $('new-thread').addEventListener('click', () => { location.hash = hashFor(S.channel, NEW_THREAD); $('text').focus(); });
  $('composer').addEventListener('submit', (e) => { e.preventDefault(); send(); });
  $('text').addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
  });
  $('text').addEventListener('input', grow);
  $('attach').addEventListener('click', () => $('file').click());
  $('file').addEventListener('change', () => {
    const f = $('file').files[0];
    $('file').value = '';
    if (f) upload(f);
  });
  $('pair-form').addEventListener('submit', pair);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') document.body.classList.remove('drawer'); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && S.thread && S.threadUnread.delete(tkey(S.channel, S.thread))) {
      renderChannels();
      renderThreads();
    }
  });
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register(`${BASE}/sw.js`, { scope: `${BASE}/` }).catch(() => {});
  }
  boot();
}

init();
