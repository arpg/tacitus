/* Tacitus web chat: the client. One file, no framework, no build step.
 *
 * The page reads the API under the path it was served from (the base
 * path, such as /chat), shows the channels, the threads of the open
 * channel and the open thread, and follows the server's event stream.
 * Text from the server reaches the page only as text nodes: the markdown
 * renderer builds every element itself and never parses HTML.
 *
 * Off the tailnet a paired phone reaches the bot through the relay
 * (docs/webchat-relay.md): the same calls, sealed end to end with
 * WebCrypto (X25519, HKDF, AES-GCM), behind the one transport interface.
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

// Two ways to the bot behind one interface (docs/webchat-relay.md, For the
// web app): api(method, path, body) answers a Response, and events(since,
// sink) opens the event stream and calls sink.open(), sink.event(type, id,
// data) and sink.down(closed). DIRECT is fetch and EventSource on the base
// path; relayTransport (below) seals each call for the bot.
const apiUrl = (path) => BASE + '/api' + path;

const DIRECT = {
  relay: false,
  api(method, path, body) {
    const opt = { method, credentials: 'same-origin', headers: { Accept: 'application/json' } };
    if (body instanceof FormData) opt.body = body;
    else if (body !== undefined) {
      opt.body = JSON.stringify(body);
      opt.headers['Content-Type'] = 'application/json';
    }
    return fetch(apiUrl(path), opt);
  },
  events(since, sink) {
    const es = new EventSource(apiUrl('/events') + (since ? `?since=${since}` : ''));
    EVENT_TYPES.forEach((t) => es.addEventListener(t, (ev) => sink.event(t, ev.lastEventId, ev.data)));
    es.addEventListener('open', () => sink.open());
    // A dropped connection reconnects by itself with Last-Event-ID; a
    // refused one (an HTTP error) is closed, so the app reconnects.
    es.addEventListener('error', () => sink.down(es.readyState === EventSource.CLOSED));
    return { close: () => es.close() };
  },
};
let T = DIRECT;
let RELAY = null; // the relay transport of this browser's pairing, once made

async function call(method, path, body) {
  const res = await T.api(method, path, body);
  let data = null;
  try { data = await res.json(); } catch { /* an empty or non-JSON body */ }
  if (res.status === 401 && !T.relay) { showPair(); throw new Error('This browser is not paired.'); }
  if (T.relay) relayNote(res.status === 403 && data && data.pending ? pendingNote(T.rec.code) : '');
  if (!res.ok) throw Object.assign(new Error((data && data.error) || `HTTP ${res.status}`), { status: res.status });
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
  // A Zulip mention or stream link, @**Name|id** or #**stream**, shows its name.
  mention: /(?<sigil>[@#])_?\*\*(?<mention>[^*|\n]+?)(?:\|\d+)?\*\*/,
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
const MD_SRC = join(['inert', 'code', 'mdlink', 'angle', 'mention', 'bold', 'strike', 'star', 'emoji', 'url']);
const CARD_SRC = join(['inert', 'code', 'angle', 'bold', 'strike', 'star', 'under', 'tilde', 'emoji', 'url']);

// A permalink into this app (the base path and a #/ route) opens in it, also
// installed; any other link opens in the browser, outside the app's window.
function link(url, label) {
  const own = (url.startsWith(location.origin + '/') ? url.slice(location.origin.length) : url).startsWith(BASE + '/#/');
  if (!own && !/^(https?:|mailto:)/i.test(url)) return null;
  return h('a', own ? { href: url } : { href: url, target: '_blank', rel: 'noopener noreferrer' }, label || url);
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
    } else if (g.mention !== undefined) out.append(h('span', { class: 'mention' }, g.sigil + g.mention));
    else if (g.bold !== undefined) out.append(wrap('strong', g.bold));
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
    .replace(/([@#])_?\*\*([^*|\n]+?)(?:\|\d+)?\*\*/g, '$1$2')
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
  const time = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
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
  const viaRelay = Boolean(T.relay && f.id); // the bytes come through the transport
  const href = viaRelay ? '#' : url;
  const box = h('div', { class: 'file' });
  if (url && /^image\//.test(f.mimetype || '')) {
    box.append(h('a', { href, target: '_blank', rel: 'noopener' },
      h('img', { src: viaRelay ? null : url, alt: f.name || 'image', loading: 'lazy' })));
  }
  const label = [emojiNode('paperclip'), ' ', h('span', { class: 'file-name' }, f.name || 'file'),
    f.size != null ? h('span', { class: 'size' }, size(f.size)) : null];
  box.append(url ? h('a', { class: 'file-link', href, download: f.name || true }, label)
    : h('span', { class: 'file-link' }, label));
  if (viaRelay) relayFile(f, box);
  return box;
}

// Through the relay a stored file has no address the phone can reach: an
// image shows from a blob, and a link takes the blob's address on a tap.
// A blob page has this page's origin, so only a raster image keeps its
// type; anything else (SVG, HTML) is plain bytes, and every link downloads.
const RASTER = /^image\/(png|jpeg|gif|webp)$/;
function relayFile(f, box) {
  let got = null;
  const blob = () => (got ||= T.api('GET', `/files/${enc(f.id)}`).then(async (res) => {
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const type = String(res.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    return URL.createObjectURL(new Blob([await res.arrayBuffer()], { type: RASTER.test(type) ? type : 'application/octet-stream' }));
  }).catch((e) => { got = null; throw e; }));
  const img = box.querySelector('img');
  if (img) blob().then((u) => { img.src = u; img.parentElement.href = u; }).catch(() => {});
  for (const a of box.querySelectorAll('a')) {
    a.setAttribute('download', f.name || 'file');
    a.removeAttribute('target');
    a.addEventListener('click', async (e) => {
      if (a.href.startsWith('blob:')) return;
      e.preventDefault();
      try { a.href = await blob(); a.click(); } catch (err) { toast(`Could not fetch the file: ${err.message}`); }
    });
  }
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
    note.textContent = e.status === 403 && T.relay ? NO_CARDS : `Not sent: ${e.message}`;
  } finally {
    controls.forEach((c) => { c.disabled = false; });
  }
}

const NO_CARDS = 'Approve this in the web chat on your computer: this phone does not press card buttons unless you allow it there.';

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
  // A phone the bot does not let confirm cards (paired on the relay's own
  // origin) sees the buttons but cannot press them.
  const locked = Boolean(row.firstChild && S.me && S.me.card_actions === false);
  if (locked) row.querySelectorAll('button, select').forEach((c) => { c.disabled = true; });
  box.append(h('div', { class: 'card-note', 'aria-live': 'polite' }, locked ? NO_CARDS : null));
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
  if (!th || (th.loading && !th.list.length)) {
    box.replaceChildren(h('p', { class: 'empty' }, 'Loading...'));
    return;
  }
  const sorted = [...th.list].sort((a, b) => (b.last_at || b.posted_at || 0) - (a.last_at || a.posted_at || 0));
  box.replaceChildren(...sorted.map((p) => {
    const tk = threadOf(p);
    const n = S.threadUnread.get(tkey(S.channel, tk)) || 0;
    const replies = p.reply_count || 0;
    return h('a', { class: 'thread-item' + (tk === S.thread ? ' active' : '') + (n ? ' unread' : ''), href: hashFor(S.channel, tk) },
      h('span', { class: 't-title' }, threadTitle(p)),
      h('span', { class: 't-meta' }, `${replies} ${replies === 1 ? 'reply' : 'replies'} · ${when(p.last_at || p.posted_at, true)}`),
      n ? h('span', { class: 'badge', 'aria-label': `${n} unread` }, String(n)) : null);
  }));
  if (!th.list.length) box.append(h('p', { class: 'empty' }, 'No threads yet. Start one with New thread.'));
  if (th.more) {
    box.append(h('button', { type: 'button', class: 'btn plain more', onclick: () => loadThreads(S.channel, true) },
      'Load older threads'));
  }
}

function remember(m) { S.known.add(tkey(m.channel, m.id)); }

async function loadThreads(ch, older) {
  // A channel read for the first time gets its list at once, marked as
  // loading: a message the stream brings during the read lands in it, and
  // the merge below keeps it (the read may predate it).
  if (!S.threads.has(ch)) S.threads.set(ch, { list: [], more: false, loading: true });
  const th = S.threads.get(ch);
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
    th.loading = false;
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
    box.replaceChildren(h('p', { class: 'empty' }, tk ? 'Write the first message of the new thread below.'
      : 'Pick a thread, or write a message to start a new one.'));
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

// The event stream on the transport in use; `ready` settles once it opens.
// A stream that ends for good reconnects from here, with ?since= (or the
// relay's `open` frame) naming the last event seen.
function connect() {
  if (S.es) S.es.close();
  setLive('wait');
  let opened;
  const handle = { ready: new Promise((resolve) => { opened = resolve; }) };
  const es = T.events(S.lastSeq, {
    open: () => { S.retry = 0; setLive('on'); opened(); },
    event: onEvent,
    down: (closed) => {
      setLive('off');
      if (closed) setTimeout(() => reconnect(handle), Math.min(30000, 1000 * 2 ** S.retry++));
    },
  });
  handle.close = es.close;
  S.es = handle;
  return handle;
}

async function reconnect(handle) {
  if (S.es !== handle) return;
  try {
    await pickTransport();
    await call('GET', '/me');
    if (relayCopy() && !(await checkBundle())) return;
    connect();
  } catch {
    if ($('pair').hidden) setTimeout(() => reconnect(handle), Math.min(30000, 1000 * 2 ** S.retry++));
  }
}

function onEvent(type, id, raw) {
  const seq = Number(id) || 0;
  if (seq && seq <= S.lastSeq) return;
  if (seq) S.lastSeq = seq;
  let data;
  try { data = JSON.parse(raw); } catch { return; }
  if (!data || typeof data !== 'object') return;
  if (S.ready) apply(type, data); else S.queue.push([type, data]);
}

function apply(type, data) {
  if (type === 'channel') {
    const i = S.channels.findIndex((c) => c.id === data.id);
    if (i >= 0) S.channels[i] = { ...S.channels[i], ...data }; else S.channels.push(data);
    renderChannels();
    // A channel event can follow rows stored with no event of their own (the
    // history the bot copies in when it starts): the open channel reads its
    // threads again. Another channel reads them when it is opened.
    if (data.id === S.channel) {
      $('channel-name').textContent = channelName(chan(data.id));
      loadThreads(data.id);
    }
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
  if (T.relay && file.size > RELAY_MAX_BODY) {
    note('Through the relay a file can be at most 10 MB. Send larger ones on the tailnet.');
    return;
  }
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
// in the app added to the Home Screen; outside it the install line says how.
// A subscription names this browser's relay device, if it has one: removing
// the device on the tailnet drops it.
const subBody = async (sub) => ({ ...sub.toJSON(), device: (await loadPairing())?.device || undefined });

async function initPush() {
  const box = $('notify');
  if (!S.me.push || !('serviceWorker' in navigator)) return;
  $('notify-row').hidden = false;
  footer();
  box.disabled = true;
  if (!('PushManager' in window) || !('Notification' in window)) return;
  const reg = await navigator.serviceWorker.ready;
  const { key } = await call('GET', '/push/vapid');
  let sub = await reg.pushManager.getSubscription();
  if (sub && !sameKey(sub.options.applicationServerKey, key)) { await sub.unsubscribe(); sub = null; }
  if (sub && Notification.permission === 'granted') await call('POST', '/push/subscribe', await subBody(sub));
  S.push = { reg, key, sub };
  box.checked = Boolean(sub) && Notification.permission === 'granted';
  box.disabled = false;
  box.addEventListener('change', toggleNotify);
  listSubs();
}

// Every browser the bot pushes to, each with a Revoke button (this one's own is the toggle).
async function listSubs() {
  const subs = await call('GET', '/push/subscriptions').catch(() => []);
  $('push-subs').replaceChildren(...subs.map((s) => h('li', null, `${s.host}, ${when(s.created_at, true)} `,
    h('button', { type: 'button', class: 'btn small', onclick: () => call('POST', '/push/revoke', { id: s.id })
      .then(listSubs, (e) => toast(`Notifications: ${e.message}`)) }, 'Revoke'))));
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
      await call('POST', '/push/subscribe', await subBody(sub));
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
    listSubs();
  }
}

/* -- Installing the app, and the phone's viewport -------------------- */

// Outside the installed app a line at the drawer's foot says how to install
// it: on iOS (navigator.standalone false) Share, then Add to Home Screen; in
// Chrome its own prompt. Hidden, it stays so 30 days, in this browser only.
const INSTALL_KEY = 'tacitus-install-hidden';
let installPrompt = null;
let installHidden = 0;
try { installHidden = Number(localStorage.getItem(INSTALL_KEY)) || 0; } catch { /* no storage: shown each visit */ }

function installHint(prompt) {
  if (prompt !== undefined) installPrompt = prompt;
  const ios = navigator.standalone === false;
  const installed = navigator.standalone ?? !matchMedia('(display-mode: browser)').matches;
  $('install-text').textContent = installPrompt ? 'Install Tacitus as an app.'
    : 'Tap Share, then Add to Home Screen, so notifications work.';
  $('install-btn').hidden = !installPrompt;
  $('install').hidden = installed || !(installPrompt || ios) || Date.now() - installHidden < 30 * 864e5;
  footer();
}

const footer = () => { $('drawer-foot').hidden = $('notify-row').hidden && $('install').hidden && $('pair-phone').hidden && $('open-settings').hidden; };

// iOS shrinks only the visual viewport for the keyboard: the app takes its
// height (--app-h) at the top, so the composer stays above the keyboard.
function fitViewport() {
  const vv = window.visualViewport;
  const root = document.documentElement.style;
  if (Math.abs(vv.scale - 1) > 0.01) { root.removeProperty('--app-h'); return; } // zoomed in
  root.setProperty('--app-h', `${vv.height}px`);
  if (window.scrollY || vv.offsetTop) window.scrollTo(0, 0);
}

/* -- The relay: crypto (relay_crypto.py is the reference) ------------ */

// Byte for byte as relay_crypto.py, which tests/fixtures/relay_vectors.json
// pins: X25519 for every agreement, HKDF-SHA256, AES-256-GCM, a 256-byte
// padding, and frames bound to their session, kind and id.
const TE = new TextEncoder();
const TD = new TextDecoder();
const PROTO = 'tacitus-relay/1';
const RELAY_PAD = 256;
const RELAY_WINDOW = 4096;
const RELAY_MAX_BODY = 10 * 1024 * 1024;
const ZERO_NONCE = new Uint8Array(12);
const LOOPBACK_HOSTS = ['127.0.0.1', 'localhost', '[::1]'];

function b64e(bytes) {
  let s = '';
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

function b64d(text) {
  if (typeof text !== 'string' || !/^[A-Za-z0-9_-]*$/.test(text)) throw new Error('not base64url');
  const s = atob(text.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (text.length % 4)) % 4));
  return Uint8Array.from(s, (c) => c.charCodeAt(0));
}

function cat(...parts) {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  parts.reduce((at, p) => { out.set(p, at); return at + p.length; }, 0);
  return out;
}

const bytesOf = (buf) => new Uint8Array(buf);
const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
const unhex = (s) => Uint8Array.from(String(s).match(/../g) || [], (x) => parseInt(x, 16));
const newId = () => hex(crypto.getRandomValues(new Uint8Array(16)));
const u32 = (b) => new DataView(b.buffer, b.byteOffset, b.byteLength).getUint32(0);
const frameAad = (sid, kind, id) => TE.encode(`${PROTO}|${sid}|${kind}|${id}`);

const sha256 = async (...parts) => bytesOf(await crypto.subtle.digest('SHA-256', cat(...parts)));

async function hkdf(ikm, salt, info, length = 32) {
  const key = await crypto.subtle.importKey('raw', ikm, 'HKDF', false, ['deriveBits']);
  return bytesOf(await crypto.subtle.deriveBits({ name: 'HKDF', hash: 'SHA-256', salt, info: TE.encode(info) },
    key, length * 8));
}

async function dh(priv, pub) {
  const key = await crypto.subtle.importKey('raw', pub, { name: 'X25519' }, true, []);
  return bytesOf(await crypto.subtle.deriveBits({ name: 'X25519', public: key }, priv, 256));
}

const gcmKey = (raw) => crypto.subtle.importKey('raw', raw, 'AES-GCM', false, ['encrypt', 'decrypt']);
const gcm = async (key, iv, data, aad) => bytesOf(await crypto.subtle.encrypt(
  { name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }, key, data));
const ungcm = async (key, iv, data, aad) => bytesOf(await crypto.subtle.decrypt(
  { name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }, key, data));

// A sealed plaintext: its length in 4 bytes, the data, then zeros up to a
// multiple of 256 bytes, so the relay sees sizes only that coarsely.
function pad(data) {
  const out = new Uint8Array(Math.ceil((data.length + 4) / RELAY_PAD) * RELAY_PAD);
  new DataView(out.buffer).setUint32(0, data.length);
  out.set(data, 4);
  return out;
}

function unpad(data) {
  if (data.length < 4 || u32(data) > data.length - 4) throw new Error('bad padding');
  return data.subarray(4, 4 + u32(data));
}

// A frame's plaintext: a 4-byte length, a JSON head, then the raw body.
function pack(meta, body = new Uint8Array(0)) {
  const head = TE.encode(JSON.stringify(meta));
  const out = cat(new Uint8Array(4), head, body);
  new DataView(out.buffer).setUint32(0, head.length);
  return out;
}

function unpack(data) {
  if (data.length < 4 || u32(data) > data.length - 4) throw new Error('bad plaintext');
  const meta = JSON.parse(TD.decode(data.subarray(4, 4 + u32(data))));
  if (!meta || typeof meta !== 'object' || Array.isArray(meta)) throw new Error('bad plaintext');
  return [meta, data.subarray(4 + u32(data))];
}

// A direction tag and an 8-byte counter.
function nonce(dir, n) {
  const out = new Uint8Array(12);
  out.set(TE.encode(dir));
  const v = new DataView(out.buffer);
  v.setUint32(4, Math.floor(n / 2 ** 32));
  v.setUint32(8, n % 2 ** 32);
  return out;
}

async function newKeys() {
  const pair = await crypto.subtle.generateKey({ name: 'X25519' }, false, ['deriveBits']);
  return { priv: pair.privateKey, pub: bytesOf(await crypto.subtle.exportKey('raw', pair.publicKey)) };
}

// The phone's hello (docs/webchat-relay.md, Handshake and sessions): its
// ephemeral key in the clear, its static key sealed under es, then the
// clock and its name sealed under es and ss; when it pairs, the pairing
// secret goes into both keys. `fixed` (the test vectors) gives the
// ephemeral pair, the payload and the frame id.
async function relayHello(rec, pairing, fixed = {}) {
  const eph = fixed.eph || await newKeys();
  const psk = pairing ? pairing.psk : new Uint8Array(0);
  const header = pairing ? cat(Uint8Array.of(1, 1), unhex(pairing.id)) : Uint8Array.of(1, 0);
  const zero = Uint8Array.of(0);
  let h = await sha256(TE.encode(PROTO), zero, TE.encode(rec.install), zero, rec.botPublic, header, eph.pub);
  const es = await dh(eph.priv, rec.botPublic);
  const cS = await gcm(await gcmKey(await hkdf(cat(es, psk), h, `${PROTO} static`)), ZERO_NONCE, rec.pub, h);
  h = await sha256(h, cS);
  const ss = await dh(rec.priv, rec.botPublic);
  const payload = fixed.payload
    || TE.encode(JSON.stringify({ name: deviceName(), t: Math.floor(Date.now() / 1000) }));
  const cP = await gcm(await gcmKey(await hkdf(cat(es, ss, psk), h, `${PROTO} hello`)), ZERO_NONCE, pad(payload), h);
  h = await sha256(h, cP);
  const id = fixed.id || newId();
  return { frame: { id, k: 'hello', c: b64e(cat(header, eph.pub, cS, cP)) }, pending: { id, eph, rec, psk, es, ss, h } };
}

// The keys a welcome's ephemeral key gives: c2b, b2c and the session id.
async function sessionKeys(p, eb) {
  const ee = await dh(p.eph.priv, eb);
  const se = await dh(p.rec.priv, eb);
  const okm = await hkdf(cat(p.es, p.ss, ee, se, p.psk), await sha256(p.h, eb), `${PROTO} session`, 80);
  return { c2b: okm.slice(0, 32), b2c: okm.slice(32, 64), sid: hex(okm.slice(64, 80)) };
}

// Only the holder of the bot's private key makes a welcome that opens here.
async function relayFinish(p, frame) {
  if (!frame || frame.k !== 'welcome' || frame.id !== p.id) throw new Error('not the welcome for this hello');
  const blob = b64d(frame.c);
  const keys = await sessionKeys(p, blob.subarray(0, 32));
  if (frame.sid !== keys.sid) throw new Error('the welcome names another session');
  const s = { sid: keys.sid, send: await gcmKey(keys.c2b), recv: await gcmKey(keys.b2c), ctr: 0, top: -1, seen: new Set() };
  s.device = String(unpack(await openRaw(s, 'welcome', frame.id, frame.n, blob.subarray(32)))[0].device || '');
  return s;
}

async function sealFrame(s, kind, id, data) {
  if (s.ctr >= 2 ** 48) throw new RelayError('session'); // spent: hello again
  const n = s.ctr++;
  const c = await gcm(s.send, nonce('c2b\0', n), pad(data), frameAad(s.sid, kind, id));
  return { id, k: kind, sid: s.sid, n, c: b64e(c) };
}

// Each counter opens once, within RELAY_WINDOW of the newest; a frame that
// fails to open does not spend its counter.
const fresh = (s, n) => n > s.top || (s.top - n < RELAY_WINDOW && !s.seen.has(n));

async function openRaw(s, kind, id, n, ct) {
  if (!Number.isSafeInteger(n) || n < 0 || n >= 2 ** 48 || !/^[A-Za-z0-9_-]{1,64}$/.test(String(id))) {
    throw new Error('bad frame');
  }
  if (!fresh(s, n)) throw new Error('replayed frame');
  const plain = await ungcm(s.recv, nonce('b2c\0', n), ct, frameAad(s.sid, kind, id));
  if (!fresh(s, n)) throw new Error('replayed frame');
  s.seen.add(n);
  if (n > s.top) {
    s.top = n;
    if (s.seen.size > 2 * RELAY_WINDOW) for (const k of s.seen) if (s.top - k >= RELAY_WINDOW) s.seen.delete(k);
  }
  return unpad(plain);
}

// A frame of the kind and id this side waits for (the answer to its
// request, an event of its stream), and nothing else.
function openFrame(s, frame, kind, id) {
  if (!frame || frame.k !== kind || frame.id !== id || frame.sid !== s.sid) throw new Error('bad frame');
  return openRaw(s, frame.k, frame.id, frame.n, b64d(frame.c));
}

// The pairing code: six digits of the hello's transcript hash, which the
// bot shows beside the device on the tailnet (relay_crypto.pairing_code).
async function pairCode(h) {
  return String(u32(await sha256(TE.encode(`${PROTO} code`), Uint8Array.of(0), h)) % 1e6).padStart(6, '0');
}
const spaced = (code) => String(code || '').replace(/^(\d{3})/, '$1 ');

/* -- The relay: this browser's pairing and the transport ------------- */

// The pairing lives in IndexedDB: the relay, the install, the bot's public
// key and this browser's key pair, whose private half WebCrypto made
// non-extractable. Web Storage holds none of it.
function idb(mode, fn) {
  return new Promise((resolve, reject) => {
    const open = indexedDB.open('tacitus-relay', 1);
    open.onupgradeneeded = () => open.result.createObjectStore('kv');
    open.onerror = () => reject(open.error);
    open.onsuccess = () => {
      const db = open.result;
      const tx = db.transaction('kv', mode);
      const req = fn(tx.objectStore('kv'));
      tx.oncomplete = () => { db.close(); resolve(req.result); };
      tx.onerror = tx.onabort = () => { db.close(); reject(tx.error); };
    };
  });
}
const loadPairing = () => idb('readonly', (st) => st.get('pairing')).catch(() => undefined);
const savePairing = (rec) => idb('readwrite', (st) => st.put(rec, 'pairing'));

function deviceName() {
  const ua = navigator.userAgent;
  const kind = ['iPhone', 'iPad', 'Android', 'Mac', 'Windows', 'Linux'].find((k) => ua.includes(k));
  return `${kind || 'Browser'} web app`;
}

// What a relay error says. An err frame is not authenticated (the relay
// can forge one), so it is only a hint: the app reports it and tries
// again, and never drops its keys over one.
const RELAY_SAYS = {
  auth: 'The bot did not accept this phone. If it was removed, pair it again under "Pair a phone".',
  offline: 'The bot cannot be reached through the relay now.',
  timeout: 'The bot did not answer in time.',
  busy: 'The relay is busy.',
  unsure: 'the relay broke off, and the bot may have it anyway. Look in the thread before you send it again.',
};
const pendingNote = (code) => `This phone waits for approval. In the web chat, under "Pair a phone", approve the phone with code ${spaced(code)}.`;

class RelayError extends Error {
  constructor(code, text) {
    super(RELAY_SAYS[code] || text || 'The relay did not answer.');
    this.code = code;
  }
}

function relayNote(text) {
  $('relay-note').textContent = text || '';
  $('relay-note').hidden = !text;
}

function relayTransport(rec) {
  const base = `${rec.relay}/r/${rec.install}`;
  const headers = { 'Content-Type': 'application/json', 'X-Relay-Key': rec.clientKey };
  const self = { relay: true, rec, session: null, helloing: null };

  // One frame to the relay; its answer frame. The key rides in a header.
  async function post(route, frame) {
    const res = await fetch(`${base}/${route}`, {
      method: 'POST', credentials: 'omit', cache: 'no-store', headers, body: JSON.stringify(frame),
    });
    let out = null;
    try { out = await res.json(); } catch { /* not JSON */ }
    if (out && out.k === 'err') {
      if (out.e === 'auth') relayNote(RELAY_SAYS.auth);
      throw new RelayError(out.e);
    }
    if (!res.ok || !out) throw new RelayError(res.status === 503 ? 'offline' : '', out && out.error);
    return out;
  }

  self.hello = async (pairing) => {
    const { frame, pending } = await relayHello(rec, pairing);
    if (pairing) self.code = await pairCode(pending.h);
    self.session = await relayFinish(pending, await post('c', frame));
    if (!pairing) relayNote('');
    return self.session;
  };
  const session = () => (self.session ? Promise.resolve(self.session)
    : (self.helloing ||= self.hello(null).finally(() => { self.helloing = null; })));
  const forget = (s) => { if (self.session === s) self.session = null; };

  self.api = async (method, path, body) => {
    let ctype = '';
    let data = new Uint8Array(0);
    if (body instanceof FormData) {
      const r = new Response(body);
      ctype = r.headers.get('content-type');
      data = bytesOf(await r.arrayBuffer());
    } else if (body !== undefined) {
      ctype = 'application/json';
      data = TE.encode(JSON.stringify(body));
    }
    const h = { accept: 'application/json', ...(ctype ? { 'content-type': ctype } : {}) };
    const plain = pack({ m: method, p: '/api' + path, h }, data);
    for (let attempt = 0; ; attempt++) {
      const s = await session();
      const id = newId();
      try {
        const [meta, bytes] = unpack(await openFrame(s, await post('c', await sealFrame(s, 'req', id, plain)), 'res', id));
        const status = meta.s >= 200 && meta.s <= 599 ? meta.s : 502;
        return new Response([204, 205, 304].includes(status) ? null : bytes, { status, headers: meta.h || {} });
      } catch (e) {
        if (!['session', 'replay', 'bad'].includes(e.code)) throw e;
        // A bot that restarted has forgotten the session: hello again. Only
        // a GET goes twice; an err frame is not sealed (the relay can forge
        // one after the bot ran a POST), so a POST is not sent again.
        forget(s);
        if (attempt || method !== 'GET') throw new RelayError('unsure');
      }
    }
  };

  // The stream opens with a one-time ticket for its sealed `open` frame,
  // so its URL carries neither the key nor the frame. `since` resumes
  // after the last event seen; EventSource's own retry would replay the
  // same frame, so the app reconnects with a fresh one instead.
  self.events = (since, sink) => {
    const ctl = new AbortController();
    (async () => {
      const s = await session();
      try {
        const opener = await sealFrame(s, 'open', newId(), pack(since ? { since } : {}));
        const { ticket } = await post('sse', opener);
        const res = await fetch(`${base}/sse?t=${enc(ticket)}`, { credentials: 'omit', cache: 'no-store', signal: ctl.signal });
        if (!res.ok || !res.body) throw new RelayError(res.status === 503 ? 'offline' : '');
        // Open once the bot's first frame comes (its stream's `retry`): the
        // relay answers before the bot has its own stream, and an event in
        // between would be lost.
        let opened = false;
        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
        let buf = '';
        for (;;) {
          const { value, done } = await reader.read();
          if (done) return;
          buf += value;
          for (let at = buf.indexOf('\n'); at >= 0; at = buf.indexOf('\n')) {
            const line = buf.slice(0, at);
            buf = buf.slice(at + 1);
            if (!line.startsWith('data: ')) continue;
            const fr = JSON.parse(line.slice(6));
            if (fr.k === 'err') throw new RelayError(fr.e);
            if (fr.k === 'end') return;
            if (fr.k !== 'sse') continue;
            const [ev] = unpack(await openFrame(s, fr, 'sse', opener.id));
            if (!opened) { opened = true; sink.open(); }
            if (ev.event || ev.data) sink.event(String(ev.event || 'message'), String(ev.id ?? ''), String(ev.data ?? ''));
          }
        }
      } catch (e) {
        if (['session', 'replay', 'bad'].includes(e.code)) forget(s);
        if (e.code === 'auth') relayNote(RELAY_SAYS.auth);
        throw e;
      }
    })().catch(() => {}).finally(() => { if (!ctl.signal.aborted) sink.down(true); });
    return { close: () => ctl.abort() };
  };
  return self;
}

// The relay carries the chat when this origin's API does not answer (off
// the tailnet, or on the relay's own copy of the app) and this browser
// holds a pairing. Without a pairing nothing changes: the API, and the
// token form on a 401.
async function pickTransport() {
  const rec = await loadPairing();
  if (!rec) { T = DIRECT; return; }
  const res = await directGet('/me');
  if (res && res.status === 200) { T = DIRECT; return; }
  if (!RELAY || b64e(RELAY.rec.pub) !== b64e(rec.pub)) RELAY = relayTransport(rec);
  T = RELAY;
}

// A GET on this origin's API, given up after four seconds (a host off the
// tailnet may not answer at all); null without an answer.
async function directGet(path) {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), 4000);
  try {
    return await fetch(apiUrl(path), { credentials: 'same-origin', cache: 'no-store', signal: ctl.signal });
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// A pairing link (`<app>#tr2=<payload>`, or `#tr1=` from before, made by
// "Pair a phone") opened here: the fragment leaves the address bar first,
// then this browser makes its key pair, keeps it, and pairs through the
// relay. The new phone then waits for the owner's approval, by its code.
async function pairFromLink(kind, text) {
  history.replaceState(null, '', location.pathname + location.search);
  const old = await loadPairing();
  let saved = false;
  try {
    const p = JSON.parse(TD.decode(b64d(text)));
    if (!p || p.v !== (kind === 'tr2' ? 2 : 1) || !/^[0-9a-f]{32}$/.test(p.p) || b64d(String(p.s)).length !== 32
      || (p.b !== undefined && !/^[0-9a-f]{64}$/.test(p.b))) {
      throw new Error('this is not a pairing link');
    }
    if (p.a !== undefined && new URL(p.a).origin !== location.origin) throw new Error('this link opens the app elsewhere');
    if (!(p.x * 1000 > Date.now())) throw new Error('the link has expired; open a new one under "Pair a phone"');
    // On the bot's own origin the link gives only the pairing secret: the
    // relay, the install and the keys come from the bot, which answers
    // there. A link whose relay is this page's origin is for the relay's
    // copy of the app, where no bot answers: it needs the bot's hash (`b`).
    let at = p;
    if (new URL(p.r).origin !== location.origin) {
      const got = await directGet('/relay/status');
      const st = got && got.status === 200 ? await got.json().catch(() => null) : null;
      if (!st || !st.install_id) throw new Error('open the link while this phone is on the tailnet');
      if (st.install_id !== p.i || st.bot_public !== p.k) throw new Error('the link is for another install');
      at = { r: st.relay_url, i: st.install_id, k: st.bot_public, c: st.client_key };
    } else if (!p.b) throw new Error('the link has no hash of the bot\'s code; open a new one under "Pair a phone"');
    else if (typeof verifyBundle !== 'function') return halt(UNCHECKED); // before it makes a key
    const url = new URL(at.r);
    if (url.protocol !== 'https:' && !(url.protocol === 'http:' && LOOPBACK_HOSTS.includes(url.hostname))) {
      throw new Error('the relay must use https');
    }
    if (!/^[a-z2-7]{26}$/.test(at.i) || b64d(String(at.k)).length !== 32 || !/^[A-Za-z0-9_-]{43}$/.test(at.c)) {
      throw new Error('this is not a pairing link');
    }
    if (old && !confirm('This browser is paired already. Replace that pairing?')) return;
    const keys = await newKeys();
    const rec = { v: 1, relay: String(at.r).replace(/\/+$/, ''), install: at.i, clientKey: at.c,
      botPublic: b64d(at.k), priv: keys.priv, pub: keys.pub, device: '', bundle: p.b || '' };
    // Kept before the hello: a phone whose welcome is lost says a plain
    // hello next time, with the key the bot registered.
    await savePairing(rec);
    saved = true;
    const relay = relayTransport(rec);
    await relay.hello({ id: p.p, psk: b64d(p.s) });
    Object.assign(rec, { device: relay.session.device, code: relay.code });
    await savePairing(rec);
    RELAY = relay;
    relayNote(pendingNote(rec.code));
  } catch (e) {
    if (saved && old) await savePairing(old).catch(() => {}); // a failed pairing keeps the earlier one
    toast(`Pairing failed: ${e.message}`);
  }
}

// "Pair a phone": a one-time link as a QR code, and the phones paired so
// far; a new one waits for Approve, beside its code. A phone opens only the
// bot's own address (public_url, a secure context); elsewhere (loopback, or
// plain http on a LAN) the dialog offers the relay's copy of the app.
async function pairPhone(via) {
  const dlg = $('phone');
  $('phone-link').value = '';
  $('phone-qr').width = 0;
  $('phone-note').textContent = 'Opening a pairing...';
  if (!dlg.open) dlg.showModal();
  const st = await call('GET', '/relay/status').catch(() => ({}));
  const here = isSecureContext && Boolean(S.me.public_url) && new URL(S.me.public_url, location.href).origin === location.origin;
  $('phone-relay').hidden = via === 'relay' || !st.pair_relay;
  if (via !== 'relay' && !(here && st.pair_own)) {
    $('phone-note').textContent = `A phone cannot open this address. ${st.pair_relay ? 'Pair through the relay: the phone loads the app '
      + 'from the relay and checks its code against this bot\'s.' : st.pair_own ? 'Open the web chat on its tailnet address.'
      : 'Pairing through the relay needs the signed app bundle, which this build does not have.'}`;
  } else {
    try {
      const out = await call('POST', '/relay/pair', via === 'relay' ? { via } : {});
      QR.draw($('phone-qr'), out.qr, 4);
      $('phone-link').value = out.link;
      $('phone-note').textContent = `${out.origin === 'relay' ? 'Scan it with the phone\'s camera; it opens the app from the relay '
        + 'and checks its code against this bot\'s. Once approved it can read and chat, and press card buttons only if you allow it below. '
        : 'Scan it with the phone\'s camera on the tailnet. '}It works once, until ${when(out.expires)}. Approve the phone below `
        + 'only when its code is the one the phone shows. Never send this link through chat or mail.';
    } catch (e) {
      $('phone-note').textContent = `Could not open a pairing: ${e.message}`;
    }
  }
  clearInterval(pairPhone.timer);
  pairPhone.timer = setInterval(() => (dlg.open ? listPhones() : clearInterval(pairPhone.timer)), 3000);
  listPhones();
}

async function listPhones() {
  const box = $('phone-devices');
  try {
    const rows = await call('GET', '/relay/devices');
    box.replaceChildren(...rows.map((d) => h('li', { 'data-device': d.id },
      h('span', {}, d.pending ? `${d.name}, code ${spaced(d.code)}, waits for approval` : `${d.name}, paired ${when(d.paired_at, true)}`
        + (d.origin === 'own' ? '' : `, through the relay, ${d.bundle_verified ? `verified copy of ${d.release || 'this release'}` : 'code not verified'}`)),
      d.origin === 'own' ? null : h('label', {}, h('input', { type: 'checkbox', checked: d.pending ? CARDS.has(d.id) : d.cards_allowed,
        onchange: (e) => cardsFor(d, e.target.checked) }), 'Allow card buttons on this phone'),
      d.pending ? h('button', { type: 'button', class: 'btn small primary', onclick: () => phoneAct('approve', d) }, 'Approve') : null,
      h('button', { type: 'button', class: 'btn small plain', onclick: () => phoneAct('revoke', d) }, 'Remove'))));
    if (!rows.length) box.append(h('li', { class: 'empty' }, 'No phone is paired yet.'));
  } catch (e) {
    box.replaceChildren(h('li', { class: 'error' }, e.message));
  }
}

// Card buttons on a relay-origin phone are the owner's choice: sent with Approve, or by approving again.
const CARDS = new Set();
const cardsFor = (d, on) => { CARDS[on ? 'add' : 'delete'](d.id); if (!d.pending) phoneAct('approve', d); };

async function phoneAct(verb, d) {
  if (verb === 'revoke' && !confirm(`Remove ${d.name}? It can no longer reach the bot through the relay.`)) return;
  const cards = verb === 'approve' ? CARDS.has(d.id) : undefined; // revoke takes the device alone
  try { await call('POST', `/relay/${verb}`, { device: d.id, cards }); } catch (e) { toast(e.message); }
  listPhones();
}

/* -- Settings: which model does each task (docs/llm-lanes.md, 1.1) -- */

// One row per task: local, hosted or auto (the remote-LLM switches decide), what the task
// does, the model it runs on now and, where a floor holds a value, what the floor says.
// The local model's state heads the screen. Only the owner's own web chat saves.
const ROUTE_WORDS = { local: 'Local', hosted: 'Hosted', auto: 'Auto' };

function renderSettings(data) {
  const loc = data.local;
  $('settings-local').textContent = loc.configured ? `Local model ${loc.model}: `
    + `${loc.reachable === false ? 'not reachable' : loc.reachable ? 'reachable' : 'not checked yet'}. ${loc.detail}` : loc.detail;
  $('settings-local').className = loc.reachable === false ? 'error' : '';
  $('settings-rows').replaceChildren(...data.rows.map((r) => h('li', { 'data-task': r.task },
    h('span', { class: 's-name' }, r.label),
    h('span', { class: 'seg', role: 'radiogroup', 'aria-label': r.label }, data.values.map((v) => h('label', {},
      h('input', { type: 'radio', name: `route-${r.task}`, value: v, checked: r.value === v,
        disabled: !r.allowed.includes(v) }), ROUTE_WORDS[v] || v))),
    h('p', {}, r.what),
    h('p', { class: 's-now' }, `Now ${r.effective}: ${r.why}.`),
    r.floor ? h('p', { class: 's-floor' }, r.floor) : null)));
}

async function openSettings() {
  $('settings-note').textContent = '';
  if (!$('settings').open) $('settings').showModal();
  try { renderSettings(await call('GET', '/settings/routing')); } catch (e) { $('settings-note').textContent = `Could not read the settings: ${e.message}`; }
}

async function saveSettings() {
  const tasks = {};
  for (const el of $('settings-rows').querySelectorAll('input:checked:not(:disabled)')) tasks[el.name.slice(6)] = el.value;
  $('settings-save').disabled = true;
  try {
    renderSettings(await call('POST', '/settings/routing', { tasks }));
    $('settings-note').textContent = 'Saved. Each task uses its model from its next run.';
  } catch (e) {
    $('settings-note').textContent = `Not saved: ${e.message}`;
  } finally {
    $('settings-save').disabled = false;
  }
}

/* -- Code integrity (docs/webchat.md, Code integrity) ---------------- */

// The bot signs the app's files at start. The page checks the signed manifest
// (verifyBundle, bundle.js) and hands it to the service worker, which serves
// only matching files. The bot's own copy checks after start-up; the relay's
// before the chat runs, through the sealed channel, against its pairing's
// `bundle` (the QR's hash) first and the key that signed it (`signKey`) after.
// With neither, or no check that runs, it stops; checked, it attests.
const relayCopy = () => Boolean(T.relay && new URL(T.rec.relay).origin === location.origin);
const UNCHECKED = 'Code verification unavailable: this copy of the app from the relay cannot check its code against the bot\'s. '
  + 'Pair this phone again once the relay serves the current release.';

async function checkBundle() {
  const rec = T.relay ? T.rec : null;
  if (rec && !rec.bundle) return halt('This phone was paired without the hash of the bot\'s code. Pair it again.');
  let doc;
  if (rec) doc = await call('GET', '/bundle');
  else if (!S.me.bundle) return null; // a bot that signs nothing (no cryptography package)
  else {
    const res = await fetch(`${BASE}/manifest.sig.json`, { credentials: 'same-origin', cache: 'no-store' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    doc = await res.json();
  }
  let v;
  try {
    v = await verifyBundle(doc, rec && !rec.signKey ? rec.bundle || null : null, rec ? rec.signKey || null : null);
  } catch (e) {
    if (e instanceof BundleError) return refuseApp('manifest.sig.json');
    if (rec) return halt(`${UNCHECKED} (${e.message})`); // no check ran: no chat either
    throw e;
  }
  if (rec && !rec.signKey) await savePairing(Object.assign(rec, { signKey: v.key }));
  const out = (await toWorker({ bundle: doc, pin: Boolean(rec) })) || (await checkFiles(v.files));
  if (out.refused) return refuseApp(out.refused, true);
  if (out.error) throw new Error(out.error);
  if (out.updated) toast('A new version of the app is ready. Close the app and open it again.');
  if (rec) await call('POST', '/relay/attest', { bundle: v.hash });
  return v;
}

// The service worker's answer, or null without one (none yet, or blocked).
async function toWorker(msg) {
  if (!('serviceWorker' in navigator)) return null;
  const reg = await Promise.race([navigator.serviceWorker.ready, new Promise((r) => { setTimeout(r, 5000); })]);
  if (!reg || !reg.active) return null;
  return new Promise((resolve) => {
    const ch = new MessageChannel();
    ch.port1.onmessage = (e) => resolve(e.data || {});
    setTimeout(() => resolve({ error: 'the service worker did not answer' }), 60000);
    reg.active.postMessage(msg, [ch.port2]);
  });
}

// Without a service worker the page checks the files itself, fetched fresh.
async function checkFiles(files) {
  for (const [path, want] of Object.entries(files)) {
    const res = await fetch(`${BASE}/${path === 'index.html' ? '' : path}`, { cache: 'no-store' }).catch(() => null);
    if (res && res.ok && (await sha256Hex(new Uint8Array(await res.arrayBuffer()))) !== want) return { refused: path };
  }
  return {};
}

// A file that is not the signed one: the worker (`told` when it knows)
// drops its copy and moves each page to its refusal; this page stops here
// too, in case it cannot.
async function refuseApp(path, told) {
  halt(`This copy of the app is not the code your bot signed: ${path} does not match it. The relay and the bot run `
    + 'different releases, or the app was changed. Open the app on the tailnet, or tell the person who runs the relay.');
  if (!told) await toWorker({ refuse: path }).catch(() => {});
  return null;
}

// The app stops on a notice, and nothing runs past it (boot and reconnect).
function halt(text) {
  if (S.es) { S.es.close(); S.es = null; }
  S.ready = false;
  S.halted = true;
  document.body.replaceChildren(h('main', { class: 'refusal', role: 'alert' }, h('h1', {}, 'Tacitus stopped'), h('p', {}, text)));
  return null;
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
    const res = await fetch(apiUrl('/pair'), {
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
  if (S.halted) return;
  try {
    await pickTransport();
    S.me = await call('GET', '/me');
    // A copy of the app from the relay runs the chat only once its code checks.
    if (relayCopy() && !(await checkBundle())) return;
  } catch (e) {
    if ($('pair').hidden) toast(`Could not reach the bot: ${e.message}`);
    if (T.relay) setTimeout(boot, Math.min(30000, 1000 * 2 ** S.retry++));
    return;
  }
  const es = connect();
  // Wait for the stream before the first reads, so no event falls between them.
  await Promise.race([es.ready, new Promise((resolve) => { setTimeout(resolve, 3000); })]);
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
  // Pairing a phone happens where the API answers: the tailnet or this machine.
  const phones = Boolean(S.me.relay) && !T.relay;
  $('pair-phone').hidden = !phones;
  $('open-settings').hidden = T.relay || !(S.me.settings || []).includes('routing');
  footer();
  if (!T.relay) checkBundle().catch(() => {}); // the bot's own copy: checked meanwhile
}

async function init() {
  window.addEventListener('hashchange', () => S.halted || route()); // a stopped app renders nothing
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
  window.addEventListener('beforeinstallprompt', (e) => { e.preventDefault(); installHint(e); });
  window.addEventListener('appinstalled', () => installHint(null));
  $('install-btn').addEventListener('click', () => { const p = installPrompt; installHint(null); p?.prompt(); });
  $('install-close').addEventListener('click', () => {
    installHidden = Date.now();
    try { localStorage.setItem(INSTALL_KEY, String(installHidden)); } catch { /* hidden for this visit */ }
    installHint();
  });
  installHint();
  if (window.visualViewport) ['resize', 'scroll'].forEach((t) => visualViewport.addEventListener(t, fitViewport));
  // The thread holds its newest message in view while its pane shrinks (the keyboard opening).
  let stuck = true;
  $('messages').addEventListener('scroll', () => { stuck = nearBottom(); }, { passive: true });
  new ResizeObserver(() => { if (stuck && !S.halted) toBottom(); }).observe($('messages'));
  document.addEventListener('touchstart', () => {}, { passive: true }); // iOS applies :active only then
  $('pair-phone').addEventListener('click', () => pairPhone());
  $('phone-relay').addEventListener('click', () => pairPhone('relay'));
  $('open-settings').addEventListener('click', openSettings);
  $('settings-save').addEventListener('click', saveSettings);
  $('relay-note').addEventListener('click', () => relayNote(''));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') document.body.classList.remove('drawer'); });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !S.halted && S.thread && S.threadUnread.delete(tkey(S.channel, S.thread))) {
      renderChannels();
      renderThreads();
    }
  });
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register(`${BASE}/sw.js`, { scope: `${BASE}/` }).catch(() => {});
    // sw.js names a tapped notification's url when it cannot navigate this window.
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (e.data && e.data.refused) refuseApp(String(e.data.refused), true);
      const url = String((e.data && e.data.open) || '');
      if (url.startsWith(`${location.origin}${BASE}/#/`)) location.hash = url.slice(url.indexOf('#'));
    });
  }
  const pairing = /^#(tr[12])=([A-Za-z0-9_-]+)$/.exec(location.hash);
  if (pairing) await pairFromLink(pairing[1], pairing[2]);
  boot();
}

init();
