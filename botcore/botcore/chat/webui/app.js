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
const EVENT_TYPES = ['message', 'update', 'reaction', 'card', 'status', 'file', 'channel', 'thread_state', 'release'];
const CREATED = new Set(['message', 'card', 'file']); // the event types that post a message
const KIND_ORDER = { dm: 0, main: 1, sched: 2, digest: 3, room: 4 };
const NEW_THREAD = '+'; // never a thread key: every key opens with a letter or digit
const PAGE = 50;
const QUOTED_LABEL = 'Rest of the body: the earlier messages it quotes';
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
  states: new Map(),    // channel + thread key -> {state, by, reason, at, stale}
  needs: [],            // the Needs you list: thread rows of every channel
  needsView: false,     // the list pane shows the Needs you list
  olderOpen: false,     // its Older part is open
  filter: 'all',        // the thread filter chosen in this browser
  card: 0,              // the welcome's card on show
  welcomed: false,      // the welcome had its chance on this page
  updateReady: false,   // a new release is kept and waits for a reload
  looking: false,       // a look for a new release is under way
  lookedAt: 0,          // when the app last looked, coming back to the screen
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
    let gone = null; // the host has not answered since this timer was set
    es.addEventListener('open', () => { clearTimeout(gone); gone = null; sink.open(); });
    // A dropped connection reconnects by itself with Last-Event-ID; a
    // refused one (an HTTP error) is closed, so the app reconnects. A host
    // that stays silent (the phone left the owner's network) is given up
    // after a while when this browser holds a pairing: the reconnect then
    // picks the relay.
    es.addEventListener('error', () => {
      sink.down(es.readyState === EventSource.CLOSED);
      if (es.readyState === EventSource.CLOSED || gone) return;
      gone = setTimeout(async () => {
        gone = null;
        if (es.readyState === EventSource.OPEN || !(await loadPairing())) return;
        es.close();
        sink.down(true);
      }, HOST_GONE_MS);
    });
    return { close: () => { clearTimeout(gone); es.close(); } };
  },
};
let T = DIRECT;
let RELAY = null; // the relay transport of this browser's pairing, once made
const HOST_GONE_MS = 8000;

// One call on the transport in use. A direct call that fails on the
// network (the phone left the owner's network while the app was open)
// picks the transport again; when that is the relay now, the call goes
// again through it and the event stream moves over.
async function call(method, path, body) {
  let res;
  try {
    res = await T.api(method, path, body);
  } catch (e) {
    if (T.relay || !(e instanceof TypeError) || body instanceof FormData) throw e;
    await pickTransport();
    if (!T.relay) throw e;
    if (S.es) connect();
    res = await T.api(method, path, body);
  }
  let data = null;
  try { data = await res.json(); } catch { /* an empty or non-JSON body */ }
  if (res.status === 401 && !T.relay) { showPair(); throw new Error('This browser is not signed in.'); }
  if (T.relay) relayNote(res.status === 403 && data && data.pending ? pendingNote(T.rec) : '');
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
      else out.append(h('div', { class: 'code-box' }, h('pre', {}, h('code', {}, src)),
        h('button', { type: 'button', class: 'btn small code-copy', 'aria-label': 'Copy this block', onclick: () => copyText(src) }, 'Copy')));
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
  if (m.deleted) return 'You removed this message';
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
    note.textContent = 'Sent. The card updates when Tacitus answers.';
  } catch (e) {
    note.textContent = e.status === 403 && T.relay ? noCards() : `Not sent: ${e.message}`;
  } finally {
    controls.forEach((c) => { c.disabled = false; });
  }
}

const noCards = () => (T.rec && T.rec.hosted ? 'Approve this in Tacitus on your phone: this computer'
  : 'Approve this in Tacitus on your computer: this phone') + ' does not press card buttons unless you allow it there.';

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
  box.append(h('div', { class: 'card-note', 'aria-live': 'polite' }, locked ? noCards() : null));
  if (c.footer) box.append(cardText(c.footer, 'card-footer'));
  return box;
}

function renderMsg(m) {
  const own = m.author === 'owner';
  const el = h('article', { class: `msg ${own ? 'own' : 'bot'} kind-${m.kind || 'message'}${m.deleted ? ' removed' : ''}`, 'data-id': m.id });
  const edited = m.kind === 'message' && !m.deleted && m.updated_at && m.posted_at && m.updated_at - m.posted_at > 1;
  el.append(h('header', { class: 'meta' },
    h('span', { class: 'who' }, own ? 'You' : 'Tacitus'),
    h('time', { datetime: new Date((m.posted_at || 0) * 1000).toISOString() }, when(m.posted_at)),
    edited ? h('span', { class: 'edited' }, 'edited') : null, msgAct(m)));
  if (m.deleted) el.append(h('div', { class: 'text' }, 'You removed this message'));
  else if (m.kind === 'card' && m.card) el.append(renderCard(m));
  else if (m.kind === 'status') el.append(cardText(m.text, 'card status'));
  else {
    if (m.text) el.append(h('div', { class: 'text' }, markdown(m.text)));
    if (m.file) el.append(renderFile(m.file));
  }
  const chips = m.deleted ? null : reactionRow(m);
  if (chips) el.append(chips);
  return el;
}

/* -- Channels and threads ------------------------------------------- */

const chan = (id) => S.channels.find((c) => c.id === id);
const KIND_NAMES = { main: 'Ask Tacitus', dm: 'Direct messages', sched: 'Scheduling', digest: 'Mail digests' }; // a room: its name, no "ea-"
const channelName = (c) => (c ? KIND_NAMES[c.kind] || String(c.name || c.id).replace(/^ea-(?=.)/, '') : '');
const hashFor = (ch, tk) => '#/' + enc(ch) + (tk ? '/' + enc(tk) : '');

function unreadFor(ch) {
  let n = S.baseline.get(ch) || 0;
  for (const [k, v] of S.threadUnread) if (k.startsWith(ch + '\n')) n += v;
  return n;
}

function renderChannels() {
  const box = $('channel-list');
  // A channel with unread messages rises to the top; the rest keep their kind order, then their names.
  const rows = [...S.channels].sort((a, b) => (unreadFor(b.id) ? 1 : 0) - (unreadFor(a.id) ? 1 : 0)
    || (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) || channelName(a).localeCompare(channelName(b)));
  let total = 0;
  const item = (c) => {
    const n = unreadFor(c.id);
    if (!c.archived) total += n;
    return roomRow(c, h('a', {
      class: 'chan' + (c.id === S.channel && !S.needsView ? ' active' : '') + (n ? ' unread' : ''),
      href: hashFor(c.id), title: c.description || null,
    }, h('span', { class: 'c-icon', 'aria-hidden': 'true' }, c.kind === 'dm' ? '@' : '#'),
    h('span', { class: 'c-name' }, channelName(c)),
    n ? h('span', { class: 'badge', 'aria-label': `${n} unread` }, String(n)) : null));
  };
  const live = rows.filter((c) => !c.archived).map(item);
  const old = rows.filter((c) => c.archived);
  box.replaceChildren(...live);
  if (old.length) {
    box.append(h('details', { class: 'archived', open: old.some((c) => c.id === S.channel) },
      h('summary', {}, `Archived (${old.length})`), old.map(item)));
  }
  document.title = total ? `(${total}) Tacitus` : 'Tacitus';
  fitNav();
  const others = total - unreadFor(S.channel);
  $('menu').dataset.unread = others > 0 ? String(others) : '';
  // The Needs you entry above the channels, with the count of threads that need the owner this week.
  const need = needsRows().filter((r) => stateOf(r.channel, threadOf(r)) === 'needs_you' && !staleOf(r)).length;
  $('needs-link').hidden = false;
  $('needs-link').classList.toggle('active', S.needsView);
  $('needs-count').hidden = !need;
  $('needs-count').textContent = String(need);
  $('needs-count').setAttribute('aria-label', `${need} ${need === 1 ? 'conversation needs' : 'conversations need'} you`);
  $('new-room').hidden = !mainChannel();
}

// One thread's card: its title, its sticker and time, its channel (in the
// Needs you list) and replies, and its unread count.
function threadCard(p, ch, withChannel) {
  const tk = threadOf(p);
  const n = S.threadUnread.get(tkey(ch, tk)) || 0;
  const st = S.states.get(tkey(ch, tk)) || {};
  // An old one is muted with its age as the bot counts it (from the owner's tap, else the last message): "Needs you · 9 days".
  const word = STICKERS[st.state] && (st.stale ? `${STICKERS[st.state]} · ${ageDays(st.by === 'owner' ? st.at : p.last_at || p.posted_at)}` : STICKERS[st.state]);
  const replies = p.reply_count || 0;
  return h('a', {
    class: 'thread-item' + (ch === S.channel && tk === S.thread ? ' active' : '') + (n ? ' unread' : ''),
    href: hashFor(ch, tk), title: word ? st.reason || word : null, 'data-channel': ch, 'data-thread': tk,
  }, h('span', { class: 't-title' }, threadTitle(p)),
  word ? h('span', { class: `sticker st-${st.state}` + (st.stale ? ' stale' : ''), role: 'img', 'aria-label': st.reason ? `${word}: ${st.reason}` : word }, word) : null,
  h('time', { class: 't-time' }, when(p.last_at || p.posted_at, true)),
  h('span', { class: 't-meta' }, withChannel ? [h('span', { class: 't-chan' }, channelName(chan(ch)) || p.channel_name || ch), ' · '] : null,
    `${replies} ${replies === 1 ? 'reply' : 'replies'}`),
  n ? h('span', { class: 'badge', 'aria-label': `${n} unread` }, String(n)) : null);
}

function renderThreads() {
  const box = $('thread-list');
  if (S.search) return renderSearch();
  if (S.needsView) {
    const rows = needsRows();
    const fresh = rows.filter((r) => !staleOf(r)), old = rows.filter(staleOf);
    box.replaceChildren(...(old.length ? [h('h2', { class: 'needs-head' }, 'This week')] : []),
      ...fresh.map((r) => threadCard(r, r.channel, true)));
    if (!fresh.length) box.append(h('p', { class: 'empty' }, old.length ? 'Nothing new this week.' : 'Nothing needs you now.'));
    if (old.length) box.append(olderPart(old));
    return;
  }
  const th = S.threads.get(S.channel);
  if (!th || (th.loading && !th.list.length)) {
    box.replaceChildren(h('p', { class: 'empty' }, 'Loading...'));
    return;
  }
  const sorted = [...th.list].sort((a, b) => (b.last_at || b.posted_at || 0) - (a.last_at || a.posted_at || 0));
  const shown = S.filter === 'all' ? sorted : sorted.filter((p) => stateOf(S.channel, threadOf(p)) === S.filter);
  const chips = h('div', { class: 'filters', role: 'group', 'aria-label': 'Show conversations' }, FILTERS.map(([v, label]) =>
    h('button', { type: 'button', class: 'filter' + (v === S.filter ? ' on' : ''), 'aria-pressed': String(v === S.filter),
      'data-filter': v, onclick: () => setFilter(v) }, label)));
  box.replaceChildren(...(th.list.length ? [chips] : []), ...shown.map((p) => threadCard(p, S.channel)));
  if (!th.list.length) box.append(emptyNote((chan(S.channel) || {}).kind));
  else if (!shown.length) box.append(h('p', { class: 'empty' }, FILTERS.find(([v]) => v === S.filter)[2]));
  if (th.more) {
    box.append(h('button', { type: 'button', class: 'btn plain more', onclick: () => loadThreads(S.channel, true) },
      'Show older conversations'));
  }
}

function setFilter(v) {
  S.filter = v;
  try { localStorage.setItem(FILTER_KEY, v); } catch { /* kept for this visit */ }
  renderThreads();
  $('thread-list').querySelector('.filter.on').focus();
}

/* -- The state of a thread: stickers, Needs you, the override -------- */

// The sticker's words per state (`none` has no sticker); the filters, with
// what an empty list says under each.
const STICKERS = { needs_you: 'Needs you', working: 'Working', waiting: 'Waiting', done: 'Done' };
const FILTERS = [['all', 'All', ''], ['needs_you', 'Needs you', 'Nothing here needs you.'],
  ['waiting', 'Waiting', 'Nothing here is waiting.'], ['done', 'Done', 'Nothing here is done.']];
const FILTER_KEY = 'tacitus-thread-filter';
const NEEDS = 'needs'; // the Needs you route (#/needs): no channel id is this word
try { S.filter = localStorage.getItem(FILTER_KEY) || 'all'; } catch { /* no storage: All */ }
if (!FILTERS.some(([v]) => v === S.filter)) S.filter = 'all';

// A thread row carries state_by, state_reason, state_at; an event carries by, reason, at.
const rowState = (r) => ({ state: r.state, by: r.state_by ?? r.by, reason: r.state_reason ?? r.reason, at: r.state_at ?? r.at ?? 0,
  stale: Boolean(r.stale) });
const stateOf = (ch, tk) => (S.states.get(tkey(ch, tk)) || {}).state || 'none';
// A state the bot put on the stale shelf: a week or more with no change.
const staleOf = (r) => Boolean((S.states.get(tkey(r.channel, threadOf(r))) || {}).stale);
const ageDays = (at) => { const d = Math.max(1, Math.floor((Date.now() / 1000 - (at || 0)) / 86400)); return `${d} ${d === 1 ? 'day' : 'days'}`; };

// A row read from the bot keeps its state unless the one held is newer
// (an event the read did not see yet).
function noteState(ch, r) {
  if (!r || !r.state) return;
  const key = tkey(ch, threadOf(r));
  const s = rowState(r);
  const old = S.states.get(key);
  if (!old || (s.at || 0) >= (old.at || 0)) S.states.set(key, s);
}

// The Needs you list as the bot gave it, less the threads whose state has
// moved on since: needs you first, then the newest.
function needsRows() {
  const rank = (r) => ({ needs_you: 0, waiting: 1 })[stateOf(r.channel, threadOf(r))];
  return S.needs.filter((r) => rank(r) !== undefined)
    .sort((a, b) => rank(a) - rank(b) || (b.last_at || b.posted_at || 0) - (a.last_at || a.posted_at || 0));
}

// The list from the bot: on connect, on opening it, and after a state changes.
async function loadNeeds() {
  clearTimeout(S.needsTimer);
  try {
    const out = await call('GET', '/needs');
    S.needs = (out && out.threads) || [];
    S.needs.forEach((r) => noteState(r.channel, r));
  } catch { return; } // the entry keeps what it had
  if (S.ready) { renderChannels(); renderThreads(); }
}

function stateShown() {
  renderChannels();
  renderThreads();
  const btn = $('thread-state');
  btn.hidden = !S.thread || S.thread === NEW_THREAD;
  btn.textContent = stateOf(S.channel, S.thread) === 'done' ? 'Needs me' : 'Mark done';
}

// A `thread_state` event: the sticker, the filter and Needs you follow, and
// the list is read again, since a thread may have joined it.
function onState(d) {
  S.states.set(tkey(d.channel, d.thread_key), rowState(d));
  const p = (S.threads.get(d.channel) || { list: [] }).list.find((x) => threadOf(x) === d.thread_key);
  if (p && !S.needs.some((r) => r.channel === d.channel && threadOf(r) === d.thread_key)) S.needs.push({ ...p, channel: d.channel });
  clearTimeout(S.needsTimer);
  S.needsTimer = setTimeout(loadNeeds, 400);
  stateShown();
}

// Needs you's Older part: shut at first, its count on the button that opens
// it, and one tap to mark them all done (asked once).
function olderPart(rows) {
  const toggle = () => { S.olderOpen = !S.olderOpen; renderThreads(); $('older-toggle').focus(); };
  return h('section', { class: 'older', 'aria-label': 'Older' },
    h('div', { class: 'needs-head' },
      h('button', { type: 'button', id: 'older-toggle', class: 'older-toggle', 'aria-expanded': String(S.olderOpen), onclick: toggle },
        `Older (${rows.length})`),
      h('button', { type: 'button', id: 'older-done', class: 'btn plain small', onclick: () => markAllDone(rows) }, 'Mark all done')),
    S.olderOpen ? rows.map((r) => threadCard(r, r.channel, true)) : null);
}

async function markAllDone(rows) {
  const n = rows.length;
  if (!n || !confirm(`Mark all ${n} older ${n === 1 ? 'item' : 'items'} done? They leave Needs you. Nothing is sent or deleted.`)) return;
  const before = rows.map((r) => [r, S.states.get(tkey(r.channel, threadOf(r)))]);
  // Stamped now, so a list read before the bot answers each one keeps them done.
  for (const [r] of before) S.states.set(tkey(r.channel, threadOf(r)), { state: 'done', by: 'owner', at: Date.now() / 1000, reason: 'You marked it done.' });
  stateShown();
  let failed = 0, why = '';
  for (const [r, old] of before) { // one at a time: the bot answers each in turn
    const key = tkey(r.channel, threadOf(r));
    try {
      const row = await call('POST', `/threads/${enc(r.channel)}/${enc(threadOf(r))}/state`, { state: 'done' });
      if (row && row.state) S.states.set(key, rowState(row));
    } catch (e) {
      failed += 1; why = e.message;
      if (old) S.states.set(key, old); else S.states.delete(key);
    }
  }
  if (failed) toast(`${failed} not changed: ${why}`);
  stateShown();
}

// The owner's own state for a thread: done, or back to needing them. The
// sticker changes at once; the bot's answer and its event confirm it.
async function setState(ch, tk) {
  const key = tkey(ch, tk);
  const old = S.states.get(key);
  const state = old && old.state === 'done' ? 'needs_you' : 'done';
  S.states.set(key, { state, by: 'owner', at: old ? old.at : 0,
    reason: state === 'done' ? 'You marked it done.' : 'You marked it as needing you.' });
  stateShown();
  try {
    const row = await call('POST', `/threads/${enc(ch)}/${enc(tk)}/state`, { state });
    if (row && row.state) S.states.set(key, rowState(row));
  } catch (e) {
    if (old) S.states.set(key, old); else S.states.delete(key);
    toast(`Not changed: ${e.message}`);
  }
  stateShown();
}

// A long press (600 ms, held still) on a card on a touch screen does what
// the header's button does. The tap that ends it opens nothing, and the
// phone's own long-press menu stays shut.
function longPress(list) {
  let timer = 0, fired = 0, at = [0, 0];
  const stop = () => { clearTimeout(timer); timer = 0; };
  list.addEventListener('pointerdown', (e) => {
    const card = e.pointerType === 'touch' && e.target.closest('.thread-item');
    if (!card) return;
    at = [e.clientX, e.clientY];
    timer = setTimeout(() => { timer = 0; fired = Date.now(); setState(card.dataset.channel, card.dataset.thread); }, 600);
  });
  list.addEventListener('pointermove', (e) => { if (timer && Math.hypot(e.clientX - at[0], e.clientY - at[1]) > 10) stop(); });
  ['pointerup', 'pointercancel'].forEach((t) => list.addEventListener(t, stop));
  list.addEventListener('click', (e) => { if (Date.now() - fired < 800) e.preventDefault(); }, true);
  list.addEventListener('contextmenu', (e) => { if (timer || Date.now() - fired < 800) e.preventDefault(); });
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
      noteState(ch, r);
      const i = list.findIndex((p) => threadOf(p) === threadOf(r));
      if (i >= 0) list[i] = r; else list.push(r);
    }
    S.threads.set(ch, { list, more: rows.length >= PAGE });
  } catch (e) {
    toast(`Could not load conversations: ${e.message}`);
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
  $('thread-title').textContent = threadTitle(list[0]) || 'Conversation';
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
  $('text').placeholder = !isNew ? 'Reply' : (chan(ch) || {}).kind === 'main' ? 'Ask Tacitus' : `Message ${channelName(chan(ch))}`;
  if (isNew) {
    $('thread-title').textContent = 'New conversation';
    box.replaceChildren(h('p', { class: 'empty' }, tk ? 'Write your message below. It starts a new conversation.'
      : 'Pick a conversation, or write a message to start a new one.'));
    return;
  }
  const key = tkey(ch, tk);
  if (S.msgs.has(key)) { renderThread(); place(); } else {
    S.msgs.set(key, []); // collects what the stream brings while the read runs
    $('thread-title').textContent = 'Conversation';
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
    place();
  } catch (e) {
    if (token === S.route) box.replaceChildren(h('p', { class: 'empty' }, `Could not load the conversation: ${e.message}`));
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
  const needs = ch === NEEDS;
  if (!needs && !chan(ch)) {
    const def = defaultChannel();
    if (def) location.replace(hashFor(def.id));
    else $('thread-list').replaceChildren(h('p', { class: 'empty' }, 'Nothing here yet.'));
    return;
  }
  const token = ++S.route;
  // A thread opened from the Needs you list keeps the list beside it, and Back returns to it.
  S.needsView = needs || (Boolean(tk) && S.needsView);
  const changed = !needs && ch !== S.channel;
  if (!needs) S.channel = ch;
  else if (!chan(S.channel)) S.channel = (defaultChannel() || {}).id || '';
  S.thread = needs ? '' : tk;
  if (!needs) S.baseline.delete(ch);
  if (S.thread) S.threadUnread.delete(tkey(ch, tk));
  document.body.classList.remove('drawer');
  document.body.dataset.view = S.thread ? 'thread' : 'threads';
  $('channel-name').textContent = S.needsView ? 'Needs you' : channelName(chan(ch));
  $('new-thread').hidden = S.needsView;
  digestView();
  stateShown();
  if (needs) loadNeeds();
  else if (changed || !S.threads.has(ch)) loadThreads(ch);
  await openThread(S.channel, S.thread, token);
}

/* -- Live events ---------------------------------------------------- */

function setLive(state) {
  const el = $('live');
  el.className = 'live ' + state;
  el.title = { on: 'Live', off: 'Reconnecting', wait: 'Connecting' }[state];
  if ($('settings').open) connLine();
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
    open: () => { S.retry = 0; setLive('on'); opened(); loadNeeds(); if (S.ready) resync(); },
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

// The stream is back after a drop (a deploy restarts the bot): the open
// place is read again, so what the stream missed in between shows now.
function resync() {
  if (!S.channel || S.needsView) return;
  if (!S.threads.has(S.channel) || true) loadThreads(S.channel);
  if (S.thread && S.thread !== NEW_THREAD) openThread(S.channel, S.thread, S.route).catch(() => {});
}

async function reconnect(handle) {
  if (S.es !== handle) return;
  try {
    await pickTransport();
    const me = await call('GET', '/me');
    if (relayCopy() && !(await checkBundle())) {
      // the copy is being replaced or refused: look again, never stop here
      setTimeout(() => reconnect(handle), 30000);
      return;
    }
    connect();
    // A restart for a deploy drops the stream: the bot may sign another release now.
    if (!relayCopy()) lookAgain(me).catch(() => {});
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
  if (type === 'thread_state') {
    if (data.channel && data.thread_key) onState(data);
    return;
  }
  if (type === 'release') {
    if (data.bundle && S.me && data.bundle !== S.me.bundle) lookAgain().catch(() => {});
    return;
  }
  if (type === 'channel') {
    const i = S.channels.findIndex((c) => c.id === data.id);
    if (i >= 0) S.channels[i] = { ...S.channels[i], ...data }; else S.channels.push(data);
    renderChannels();
    // A channel event can follow rows stored with no event of their own (the
    // history the bot copies in when it starts): the open channel reads its
    // threads again. Another channel reads them when it is opened.
    if (data.id === S.channel) {
      if (!S.needsView) $('channel-name').textContent = channelName(chan(data.id));
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
  // The owner's own message (here, or mirrored from elsewhere) reads its thread, and its channel as the server counts.
  if (isNew && m.author === 'owner') { S.threadUnread.delete(key); S.baseline.delete(m.channel); }
  else if (isNew && !(viewing && document.visibilityState === 'visible')) S.threadUnread.set(key, (S.threadUnread.get(key) || 0) + 1);
  if (isNew && viewing && S.push && !S.push.sub) seenLater(`${m.channel}/${tk}`); // push off here: no worker says it
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
  if (m.channel === S.channel || S.needsView) renderThreads();
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

// A send that the host did not take (the connection refused while the bot
// restarts for a deploy, or a 502/503/504) goes again, with the same nonce
// so the host posts it once, for up to ~45 s. `step` says how long the
// composer has been trying.
const SEND_RETRY_S = [1, 2, 4, 8, 10, 10, 10];
function retriable(e) {
  return e instanceof TypeError || [502, 503, 504].includes(e && e.status);
}
async function sending(fn, step) {
  for (let i = 0; ; i++) {
    try {
      return await fn();
    } catch (e) {
      if (!retriable(e) || i >= SEND_RETRY_S.length) throw e;
      if (step) step(i);
      await new Promise((resolve) => { setTimeout(resolve, SEND_RETRY_S[i] * 1000); });
    }
  }
}
function sendNonce() {
  return (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
}

async function submit(build, text) {
  if (S.sending) return;
  if (!S.channel) { note('Not sent: the app is still connecting. Try again in a moment.'); return; }
  const { ch, tk } = target();
  S.sending = true;
  $('composer').classList.add('busy');
  try {
    const m = await sending(() => build(ch, tk), () => note('Tacitus is not answering (a restart?). Trying again.'));
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
  if (S.editing) return saveEdit();
  const text = $('text').value;
  if (!text.trim()) return;
  const n = sendNonce();
  submit((ch, tk) => call('POST', `/channels/${enc(ch)}/messages`, tk ? { text, thread_key: tk, nonce: n } : { text, nonce: n }), text);
}

function upload(file) {
  const text = $('text').value;
  if (T.relay && file.size > RELAY_MAX_BODY) {
    note('Away from home a file can be at most 10 MB. Send larger ones when you are on your own network.');
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

/* -- Owner actions: search, reactions, edit and delete -------------- */

// The owner's own reactions (the bot keeps them apart from its status
// marks) with the words a screen reader says, and how long the owner's
// message stays open to an edit or a removal (the bot's EDIT_WINDOW_S).
const MINE = [['+1', '👍', 'Thumbs up'], ['white_check_mark', '✅', 'Check mark'], ['heart', '❤️', 'Heart'],
  ['eyes', '👀', 'Eyes'], ['joy', '😂', 'Laughing']];
const EDIT_MS = 15 * 60 * 1000;
Object.assign(S, { search: null, hit: null, editing: null, strip: null });
const mineOf = (r) => MINE.find(([k]) => k === r);
const msgById = (id) => (S.msgs.get(tkey(S.channel, S.thread)) || []).find((x) => x.id === id);
// Search words as the bot's index splits them: letters and digits.
const wordsOf = (q) => String(q || '').match(/[\p{L}\p{N}]+/gu) || [];

// Under a message, one chip per emoji, the bot's marks and the owner's
// together, with the count; the owner's are highlighted, and a tap on one of
// the owner's five takes it away or adds it.
function reactionRow(m) {
  const bot = m.reactions || [], mine = new Set(m.owner_reactions || []);
  const names = [...new Set([...bot, ...mine])];
  if (!names.length) return null;
  return h('div', { class: 'reactions' }, names.map((r) => {
    const n = Number(bot.includes(r)) + Number(mine.has(r));
    const pill = h('span', { class: 'pill' }, emojiNode(r) || (mineOf(r) || [])[1] || `:${r}:`, h('span', { class: 'n' }, String(n)));
    return mineOf(r) ? h('button', { type: 'button', class: 'chip' + (mine.has(r) ? ' mine' : ''), title: `:${r}:`,
      'aria-pressed': String(mine.has(r)), 'aria-label': `${mineOf(r)[2]}, ${n}`, onclick: () => react(m, r) }, pill)
      : h('span', { class: 'chip', title: `:${r}:` }, pill);
  }));
}

// The pointer and keyboard way to the strip; a touch screen long-presses.
const msgAct = (m) => (m.deleted || m.kind === 'status' ? null
  : h('button', { type: 'button', class: 'msg-act', 'aria-label': 'Message options', 'aria-haspopup': 'true' }, '⋯'));

async function react(m, name) {
  closeStrip();
  try { ingest(await call('POST', `/messages/${enc(m.channel)}/${enc(m.id)}/react`, { emoji: name }), false); } catch (e) { toast(`Not changed: ${e.message}`); }
}

// A message's words as shown, without the Copy buttons of its code blocks.
function textOf(node) {
  if (!node) return '';
  const btns = [...node.querySelectorAll('.code-copy')];
  btns.forEach((b) => { b.hidden = true; });
  const text = node.innerText || '';
  btns.forEach((b) => { b.hidden = false; });
  return text;
}

// The strip over a message: the five reactions, then Edit and Delete on the
// owner's own words in their first 15 minutes, and Copy, since a long press
// on a phone selects no text.
function openStrip(el, keyboard) {
  const m = el && msgById(el.dataset.id);
  closeStrip();
  if (!m || m.deleted) return;
  const mine = new Set(m.owner_reactions || []);
  const own = m.author === 'owner' && m.kind === 'message' && Date.now() - m.posted_at * 1000 < EDIT_MS;
  const text = m.kind === 'message' || m.kind === 'file' ? textOf(el.querySelector('.text')) : '';
  const acts = [own && h('button', { type: 'button', class: 'btn small', onclick: () => startEdit(m) }, 'Edit'),
    own && h('button', { type: 'button', class: 'btn small danger', onclick: () => removeMsg(m) }, 'Delete'),
    text && h('button', { type: 'button', class: 'btn small', onclick: () => copyText(text) }, 'Copy')].filter(Boolean);
  const strip = h('div', { id: 'msg-strip', role: 'toolbar', 'aria-label': 'Message options' },
    h('div', { class: 'strip-row' }, MINE.map(([k, glyph, word]) => h('button', { type: 'button', class: 'strip-emoji',
      'aria-label': word, 'aria-pressed': String(mine.has(k)), onclick: () => react(m, k) }, glyph))),
    acts.length ? h('div', { class: 'strip-row' }, acts) : null);
  document.body.append(strip);
  // Above the message when there is room under the header, else below it, inside the window.
  const r = el.getBoundingClientRect(), s = strip.getBoundingClientRect();
  const floor = $('thread').querySelector('.bar').getBoundingClientRect().bottom + 8;
  const top = r.top - s.height - 8 >= floor ? r.top - s.height - 8 : Math.min(r.bottom + 8, innerHeight - s.height - 8);
  strip.style.top = `${Math.max(8, top)}px`;
  strip.style.left = `${Math.max(8, Math.min(m.author === 'owner' ? r.right - s.width : r.left, innerWidth - s.width - 8))}px`;
  S.strip = { el: strip, from: keyboard ? document.activeElement : null };
  if (keyboard) strip.querySelector('button').focus();
}

function closeStrip() {
  const s = S.strip;
  S.strip = null;
  if (!s) return;
  s.el.remove();
  if (s.from && s.from.isConnected) s.from.focus();
}

async function copyText(text) {
  closeStrip();
  try { await navigator.clipboard.writeText(text); toast('Copied.'); } catch { toast('This browser did not let the app copy.'); }
}

// Edit: the words go to the composer under an "Editing" line with Cancel;
// Send saves them, and what was typed before comes back.
function startEdit(m) {
  closeStrip();
  if (!S.editing) S.editing = { kept: $('text').value };
  Object.assign(S.editing, { ch: m.channel, tk: threadOf(m), id: m.id });
  $('text').value = m.text;
  document.querySelectorAll('.msg.editing').forEach((x) => x.classList.remove('editing'));
  (S.nodes.get(m.id) || h('i')).classList.add('editing');
  $('editing').hidden = false;
  $('attach').disabled = true;
  grow();
  $('text').focus();
}

function endEdit() {
  if (!S.editing) return;
  $('text').value = S.editing.kept;
  (S.nodes.get(S.editing.id) || h('i')).classList.remove('editing');
  S.editing = null;
  $('editing').hidden = true;
  $('attach').disabled = false;
  note('');
  grow();
}

async function saveEdit() {
  const { ch, id } = S.editing;
  const text = $('text').value;
  if (S.sending) return;
  if (!text.trim()) { note('Type the new words, or tap Cancel.'); return; }
  if (text === (msgById(id) || {}).text) { endEdit(); return; }
  S.sending = true;
  try { ingest(await call('PATCH', `/messages/${enc(ch)}/${enc(id)}`, { text }), false); endEdit(); } catch (e) { note(`Not changed: ${e.message}`); }
  S.sending = false;
}

async function removeMsg(m) {
  closeStrip();
  if (!confirm('Delete this message? This cannot be undone.')) return;
  if (S.editing && S.editing.id === m.id) endEdit();
  try { ingest(await call('DELETE', `/messages/${enc(m.channel)}/${enc(m.id)}`), false); } catch (e) { toast(`Not deleted: ${e.message}`); }
}

// Search: the field at the top of the menu opens it in the list's header,
// and the list shows the hits grouped by conversation, newest first. A hit
// opens its conversation at that message, marked; Back returns to the hits;
// the cross or Escape ends the search.
function openSearch() {
  S.search ||= { q: '', rows: null, busy: false, error: '', seq: 0 };
  document.body.classList.remove('drawer');
  document.body.classList.add('searching');
  $('search').hidden = false;
  $('search-q').focus();
  renderThreads();
}

function closeSearch() {
  if (!S.search) return;
  S.search = null;
  clearTimeout(S.searchTimer);
  document.body.classList.remove('searching');
  $('search').hidden = true;
  $('search-q').value = '';
  renderThreads();
}

async function runSearch(min = 2) {
  const s = S.search;
  if (!s) return;
  s.q = $('search-q').value.trim();
  const seq = ++s.seq;
  if (s.q.length < min) { Object.assign(s, { rows: null, busy: false, error: '' }); renderThreads(); return; }
  s.busy = true;
  renderThreads();
  let rows = [], error = '';
  try { rows = (await call('GET', `/search?q=${enc(s.q)}&limit=50`)).results || []; } catch (e) { error = e.message; }
  if (S.search !== s || seq !== s.seq) return; // a newer search is on its way
  Object.assign(s, { rows, error, busy: false });
  renderThreads();
}

function renderSearch() {
  const s = S.search, box = $('thread-list');
  const say = (text) => box.replaceChildren(h('p', { class: 'empty' }, text));
  if (s.error) return say(`Could not search: ${s.error}`);
  if (!s.rows) return say(s.busy ? 'Searching...' : 'Type a word or two to find it in any conversation.');
  if (!s.rows.length) return say(s.busy ? 'Searching...' : `Nothing found for "${s.q}".`);
  const words = wordsOf(s.q), groups = new Map();
  for (const r of s.rows) {
    const k = tkey(r.channel, r.thread_key);
    groups.set(k, [...(groups.get(k) || []), r]);
  }
  const go = (x) => () => {
    S.hit = { ch: x.channel, tk: x.thread_key, id: x.id, words };
    if (location.hash === hashFor(x.channel, x.thread_key)) showHit();
  };
  box.replaceChildren(...[...groups.values()].map((hits) => h('section', { class: 's-group' },
    h('a', { class: 's-head', href: hashFor(hits[0].channel, hits[0].thread_key), onclick: go(hits[0]) },
      h('span', { class: 's-title' }, threadTitle({ text: hits[0].thread_title })),
      h('span', { class: 's-chan' }, channelName(chan(hits[0].channel)) || hits[0].channel_name)),
    hits.map((x) => h('a', { class: 's-hit', href: hashFor(x.channel, x.thread_key), onclick: go(x) },
      h('span', { class: 's-meta' }, `${x.author === 'owner' ? 'You' : 'Tacitus'} · ${when(x.posted_at)}`),
      marked(plainLine(x.snippet), words, h('span', { class: 's-text' })))))));
}

// `text` into `box`, each place a search word starts a word in a <mark>.
function marked(text, words, box) {
  let last = 0;
  for (const m of words.length ? text.matchAll(new RegExp(`(?<![\\p{L}\\p{N}])(?:${words.join('|')})`, 'giu')) : []) {
    box.append(text.slice(last, m.index), h('mark', {}, m[0]));
    last = m.index + m[0].length;
  }
  box.append(text.slice(last));
  return box;
}

// The hit in the open conversation: ringed, its words marked, in the middle of the pane.
function showHit() {
  const hit = S.hit;
  const el = hit && hit.ch === S.channel && hit.tk === S.thread && S.nodes.get(hit.id);
  if (!el) return false;
  el.classList.add('hit');
  const re = new RegExp(`(?<![\\p{L}\\p{N}])(?:${hit.words.join('|') || '(?!)'})`, 'iu');
  for (const part of el.querySelectorAll('.text, .card')) {
    const walk = document.createTreeWalker(part, NodeFilter.SHOW_TEXT), found = [];
    while (walk.nextNode()) if (re.test(walk.currentNode.data) && !walk.currentNode.parentElement.closest('mark')) found.push(walk.currentNode);
    found.forEach((t) => t.replaceWith(marked(t.data, hit.words, document.createDocumentFragment())));
  }
  el.scrollIntoView({ block: 'center' });
  return true;
}
const place = () => { if (!showHit()) toBottom(); };

// A long press (500 ms, held still) on a message on a touch screen opens the
// strip; the tap that ends it opens nothing, and a link, a button or a code
// block keeps the phone's own press.
function ownerActions() {
  const box = $('messages');
  let timer = 0, fired = 0, at = [0, 0];
  const stop = () => { clearTimeout(timer); timer = 0; };
  box.addEventListener('pointerdown', (e) => {
    const el = e.pointerType === 'touch' && !e.target.closest('a, button, select, summary, input, pre') && e.target.closest('.msg[data-id]');
    if (!el) return;
    at = [e.clientX, e.clientY];
    timer = setTimeout(() => { timer = 0; fired = Date.now(); openStrip(el); }, 500);
  });
  box.addEventListener('pointermove', (e) => { if (timer && Math.hypot(e.clientX - at[0], e.clientY - at[1]) > 10) stop(); });
  ['pointerup', 'pointercancel'].forEach((t) => box.addEventListener(t, stop));
  box.addEventListener('click', (e) => { if (Date.now() - fired < 800) { e.preventDefault(); e.stopPropagation(); } }, true);
  box.addEventListener('contextmenu', (e) => { if (timer || Date.now() - fired < 800) e.preventDefault(); });
  box.addEventListener('click', (e) => { const b = e.target.closest('.msg-act'); if (b) openStrip(b.closest('.msg'), true); });
  box.addEventListener('scroll', () => { if (Date.now() - fired > 800) closeStrip(); }, { passive: true });
  document.addEventListener('pointerdown', (e) => { if (S.strip && !S.strip.el.contains(e.target)) closeStrip(); }, true);
  $('search-open').addEventListener('click', openSearch);
  $('search-close').addEventListener('click', () => { closeSearch(); $('search-open').focus(); });
  $('search-q').addEventListener('input', () => { clearTimeout(S.searchTimer); S.searchTimer = setTimeout(runSearch, 250); });
  $('search').addEventListener('submit', (e) => { // Enter searches one letter too; on a phone the keyboard goes, the hits show
    e.preventDefault();
    clearTimeout(S.searchTimer);
    runSearch(1);
    if (matchMedia('(pointer: coarse)').matches) $('search-q').blur();
  });
  $('edit-cancel').addEventListener('click', () => { endEdit(); $('text').focus(); });
  // A place picked in the menu ends the search; leaving a conversation ends its edit and its mark.
  $('channels').addEventListener('click', (e) => { if (e.target.closest('a.chan')) closeSearch(); });
  window.addEventListener('hashchange', () => {
    const { ch, tk } = parseHash();
    closeStrip();
    if (S.hit && (S.hit.ch !== ch || S.hit.tk !== tk)) S.hit = null;
    if (S.editing && (S.editing.ch !== ch || S.editing.tk !== tk)) endEdit();
  });
  document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape' || document.querySelector('dialog[open]')) return;
    if (S.strip) closeStrip();
    else if (S.editing && document.activeElement === $('text')) endEdit();
    else if (S.search) { closeSearch(); $('search-open').focus(); }
  });
}

/* -- Notifications (Web Push) --------------------------------------- */

// The server's public key (base64url) as the bytes subscribe() takes.
function keyBytes(b64) {
  const s = b64.replace(/-/g, '+').replace(/_/g, '/');
  return Uint8Array.from(atob(s + '='.repeat((4 - s.length % 4) % 4)), (c) => c.charCodeAt(0));
}

const sameKey = (buf, key) => !buf || String(new Uint8Array(buf)) === String(keyBytes(key));

// The switch on Settings' Notifications page. A subscription this browser already holds is posted
// again, so the server keeps it after a reset; one made with another key
// (the server's key was replaced) is dropped. iOS takes notifications only
// in the app added to the Home Screen; outside it the page and the install line say how.
// A subscription names this browser's relay device, if it has one: removing
// the device on the tailnet drops it.
const subBody = async (sub) => ({ ...sub.toJSON(), device: (await loadPairing())?.device || undefined });

async function initPush() {
  const box = $('notify');
  if (!S.me.push) return;
  const sw = 'serviceWorker' in navigator;
  // A browser that takes no push still tells the bot the thread it shows (watchSeen).
  if (!sw || !('PushManager' in window) || !('Notification' in window)) { S.push = {}; notifyState(); watchSeen(); return; }
  const reg = await navigator.serviceWorker.ready;
  const { key } = await call('GET', '/push/vapid');
  let sub = await reg.pushManager.getSubscription();
  if (sub && !sameKey(sub.options.applicationServerKey, key)) { await sub.unsubscribe(); sub = null; }
  if (sub && Notification.permission === 'granted') await call('POST', '/push/subscribe', await subBody(sub));
  S.push = { reg, key, sub };
  box.checked = Boolean(sub) && Notification.permission === 'granted';
  box.disabled = false;
  box.addEventListener('change', toggleNotify);
  notifyState();
  watchSeen();
}

// A thread this page shows clears its notifications everywhere, push on here or not: the page closes them
// here and tells the bot, which owes this browser no clear for it and clears the owner's other browsers.
// Opening the thread or bringing the page into view is the owner's look; a push or a message that arrives
// for the thread in view counts only while the page has the focus (a screen left on is not the owner
// reading), and else clears this browser alone. It waits SEEN_MS first: Firefox counts a push whose
// notification is gone in three seconds as silent. The worker says {shown: tag} for a push here; with push
// off here, the message on the stream says it (ingest).
const SEEN_MS = 5000;
const tagInView = () => (document.visibilityState === 'visible' && S.thread && S.thread !== NEW_THREAD ? `${S.channel}/${S.thread}` : '');
async function seenHere(tag, elsewhere = true) {
  if (!tag || !S.push) return;
  const { reg, sub } = S.push;
  if (reg) (await reg.getNotifications({ tag })).forEach((n) => n.close());
  if (sub || elsewhere) await call('POST', '/push/seen', { endpoint: sub ? sub.endpoint : undefined, tag, elsewhere });
}
const seenLater = (tag) => setTimeout(() => tagInView() === tag && seenHere(tag, document.hasFocus()).catch(() => {}), SEEN_MS);
// Notifications no clear reached here (Apple's push service takes none; one past its budget is skipped): the
// bot keeps their tags and the next push closes them; the page also asks for them when it starts or comes
// back into view, closes them, and the bot drops what it answered.
async function sweep() {
  const { reg, sub } = S.push || {};
  if (!sub || document.visibilityState !== 'visible') return;
  const tags = new Set(((await call('POST', '/push/unclosed', { endpoint: sub.endpoint })) || {}).tags || []);
  if (tags.size) (await reg.getNotifications()).forEach((n) => tags.has(n.tag) && n.close());
}
function watchSeen() {
  const seen = () => { seenHere(tagInView()).catch(() => {}); };
  const back = () => { sweep().catch(() => {}); seen(); };
  window.addEventListener('hashchange', seen);
  document.addEventListener('visibilitychange', back);
  navigator.serviceWorker?.addEventListener('message', (e) => {
    const tag = String((e.data && e.data.shown) || '');
    if (tag) seenLater(tag);
  });
  back();
}

// Whether this browser gets notifications, and why not, in one line each for the
// menu, the page and the drawer's foot ("Notifications off" where they could be on).
const here = () => (phoneHere() ? 'phone' : 'browser');
function notifyState() {
  $('notify-here').textContent = `Notifications on this ${here()}`;
  const p = S.push;
  if (!p) return; // the browser not asked yet: initPush calls again
  const why = !p.reg ? (navigator.standalone === false ? ['Only in the app on your Home Screen', 'On an iPhone or iPad, notifications '
    + 'come only in the app on your Home Screen. Tap Share, then Add to Home Screen, and open Tacitus from there.']
    : ['This browser does not show them', 'This browser does not show notifications.'])
    : Notification.permission === 'denied' ? ['Blocked in the browser settings', 'Notifications are blocked for this site in the browser settings.'] : null;
  const on = Boolean(p.sub) && !why;
  $('notify-why').textContent = why ? why[1] : '';
  $('notify-why').hidden = !why;
  $('notify-detail').textContent = why ? why[0] : `${on ? 'On' : 'Off'} for this ${here()}`;
  $('settings-hint').hidden = on || !p.reg;
}

// Every browser the bot pushes to, named by its push service's maker, each with Remove; this one's own is its switch.
const MAKERS = [['push.apple.com', 'An iPhone, iPad or Mac'], ['googleapis.com', 'Chrome or Android'], ['mozilla.com', 'Firefox'],
  ['windows.com', 'Microsoft Edge']];
const maker = (host) => (MAKERS.find(([end]) => host === end || host.endsWith(`.${end}`)) || ['', 'A browser'])[1];
async function listSubs() {
  const subs = await call('GET', '/push/subscriptions').catch(() => []);
  const mine = S.push && S.push.sub ? (await sha256Hex(S.push.sub.endpoint)).slice(0, 16) : '';
  $('subs-title').hidden = !subs.length;
  $('push-subs').replaceChildren(...subs.map((s) => h('li', null,
    h('span', null, `${s.id === mine ? `This ${here()}` : maker(s.host)}, since ${when(s.created_at, true)}`),
    s.id === mine ? null : h('button', { type: 'button', class: 'btn small', onclick: () => call('POST', '/push/revoke', { id: s.id })
      .then(listSubs, (e) => toast(`Notifications: ${e.message}`)) }, 'Remove'))));
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
    notifyState();
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

const footer = () => { $('drawer-foot').hidden = ['install', 'open-settings', 'update-note'].every((id) => $(id).hidden); };

// iOS shrinks the visual viewport for the keyboard, and from iOS 27 the window
// too (interactive-widget=resizes-content), the two out of step while the
// keyboard moves. The visual viewport is the one measure while a field has
// the focus: the app takes its height (--app-h) at the top, so the composer
// stays above the keyboard. The pin is set only around the keyboard; otherwise
// the stylesheet's own height rules, so a screen that changes size (a foldable
// opening, a window resized) is never held at the old height.
let lastSize = '';
let lastShort = '';
let lastEdit = 0; // when a text field last had the focus
let full = { w: 0, h: 0 }; // the window with no keyboard, per width (a fold changes the width)
let wasKeyboard = false;
let keyboardNotes = 0;
// The keyboard is up only while a text field has the focus: a screen that
// reports a short visual viewport for another reason (a foldable open in
// landscape) must not pin the app to that height.
const editing = () => { const el = document.activeElement; return Boolean(el) && (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT' || el.isContentEditable === true); };
function fitViewport() {
  const vv = window.visualViewport;
  const root = document.documentElement.style;
  const focused = editing();
  if (focused) lastEdit = Date.now();
  const recent = focused || Date.now() - lastEdit < 1000;
  // Only a plausible visual viewport counts: a foldable can report a negative or
  // huge height after a fold, and a stale one on the way to a new posture.
  const client = document.documentElement.clientHeight;
  const plausible = Boolean(vv) && Number.isFinite(vv.height) && vv.height > 0 && vv.height <= Math.max(window.innerHeight, client) && Math.abs(vv.scale - 1) <= 0.01;
  // The screen without the keyboard: the tallest window seen at this width, so a
  // window that shrinks with the keyboard still counts as one with a keyboard.
  if (full.w !== window.innerWidth) full = { w: window.innerWidth, h: 0 };
  if (!recent && (!plausible || vv.height >= window.innerHeight - 1)) full.h = Math.max(full.h, window.innerHeight);
  const screen = Math.max(full.h, window.innerHeight, client);
  const shorter = plausible && screen - vv.height > Math.max(40, screen * 0.1);
  // A field has the focus, or had it within the last second: the focus moves to
  // Send while the keyboard is still up.
  const keyboard = shorter && recent;
  if (keyboard) root.setProperty('--app-h', `${vv.height}px`);
  else root.removeProperty('--app-h');
  // The page itself never scrolls: back to the top once the app fits the visual
  // viewport, or once the keyboard is gone; not while the browser is still
  // bringing the focused field into view.
  if ((keyboard || !shorter) && (window.scrollY || (vv && vv.offsetTop))) window.scrollTo(0, 0);
  const size = `${window.innerWidth}x${window.innerHeight}`;
  const vvSize = vv ? `${Math.round(vv.width)}x${Math.round(vv.height)}` : '';
  if (lastSize && size !== lastSize && !keyboard) diagNote({ what: 'resize', from: lastSize, to: size, vv: vvSize });
  if (shorter && !keyboard && lastShort !== `${size}/${vvSize}`) { lastShort = `${size}/${vvSize}`; diagNote({ what: 'short-viewport', size, vv: vvSize }); }
  lastSize = size;
  // Each keyboard change, a few times a visit: where the composer landed against
  // the visual viewport, for the phone's own account of a keyboard that hid it.
  if (keyboard !== wasKeyboard && keyboardNotes < 6) {
    keyboardNotes++;
    diagNote({ what: 'keyboard', up: keyboard, size, vv: vvSize, client, offsetTop: Math.round((vv && vv.offsetTop) || 0),
      composer: Math.round($('composer').getBoundingClientRect().bottom), thread: S.thread === NEW_THREAD ? 'new' : S.thread ? 'open' : 'none' });
    if (!keyboard) flushDiag().catch(() => {});
  }
  wasKeyboard = keyboard;
}

/* -- The menu's width ----------------------------------------------- */

// Where the menu is a column (1000px and up), its edge is a handle: a drag
// (a mouse or a finger) or the arrow keys set the width, Home and End take
// it to either end, and a double-click or Enter gives back the menu's own
// width: its longest name on one line in bold, within NAV_FIT (a longer
// name wraps, and one cut short shows whole on hover). This browser keeps
// a width set; the stylesheet keeps the panes beside it wide enough to use.
const NAV_KEY = 'tacitus-menu-width';
const NAV = [180, 420], NAV_FIT = [240, 300];
const textWidth = document.createElement('canvas').getContext('2d');
const within = (w, [lo, hi]) => Math.round(Math.min(Math.max(w, lo), hi));
const navW = () => Math.round($('channels').getBoundingClientRect().width);
const rootStyle = document.documentElement.style;

// Sets the width and answers the width shown, which is the one kept.
function setNav(w) {
  rootStyle.setProperty('--nav-w', `${within(w, NAV)}px`);
  const shown = navW();
  rootStyle.setProperty('--nav-w', `${shown}px`);
  return shown;
}

function fitNav() {
  const names = [...$('channels').querySelectorAll('.c-name')].filter((el) => el.offsetWidth);
  if (!names.length) return;
  const st = getComputedStyle(names[0]), w = navW();
  textWidth.font = `700 ${st.fontSize} ${st.fontFamily}`;
  // Each name, and the room its row takes beside it (the icon, the padding, a room's dots) less a badge's.
  const need = Math.max(...names.map((el) => { const box = el.getBoundingClientRect(), tail = el.nextElementSibling;
    return textWidth.measureText(el.textContent).width + 2 + w - box.width - (tail && tail.offsetWidth ? tail.getBoundingClientRect().right - box.right : 0); }));
  rootStyle.setProperty('--nav-fit', `${within(need, NAV_FIT)}px`);
  cutNames();
}

const cutNames = () => $('channels').querySelectorAll('.c-name').forEach((el) => {
  if (el.scrollHeight > el.clientHeight + 1) el.title = el.textContent; else el.removeAttribute('title'); });

function navEdge() {
  const edge = $('nav-edge');
  const keep = (w) => { try { if (w) localStorage.setItem(NAV_KEY, String(w)); else localStorage.removeItem(NAV_KEY); } catch { /* for this visit */ } };
  try { const kept = Number(localStorage.getItem(NAV_KEY)); if (kept) rootStyle.setProperty('--nav-w', `${within(kept, NAV)}px`); } catch { /* its own width */ }
  const reset = () => { rootStyle.removeProperty('--nav-w'); keep(0); };
  new ResizeObserver(() => { if (!S.halted) { edge.setAttribute('aria-valuenow', String(navW())); cutNames(); } }).observe($('channels'));
  let drag = null;
  edge.addEventListener('pointerdown', (e) => {
    if (e.button) return;
    e.preventDefault();
    edge.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, w: navW(), to: 0 };
    document.body.classList.add('resizing');
  });
  edge.addEventListener('pointermove', (e) => { if (drag) drag.to = setNav(drag.w + e.clientX - drag.x); });
  ['pointerup', 'pointercancel', 'lostpointercapture'].forEach((t) => edge.addEventListener(t, () => {
    if (drag && drag.to) keep(drag.to);
    drag = null;
    document.body.classList.remove('resizing');
  }));
  edge.addEventListener('dblclick', reset);
  edge.addEventListener('keydown', (e) => {
    const w = navW(), step = e.shiftKey ? 64 : 16;
    const to = { ArrowLeft: w - step, ArrowRight: w + step, Home: NAV[0], End: NAV[1] }[e.key];
    if (e.key === 'Enter') reset();
    else if (to !== undefined) keep(setNav(to));
    else return;
    e.preventDefault();
  });
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

// An X25519 pair. An ephemeral one lives in this page alone. The device's
// own (`keep`) must outlive the page: its private half is kept as PKCS#8
// bytes in IndexedDB, since WebKit does not give an X25519 CryptoKey back
// from there, and the key the page computes with is a non-extractable
// import of those bytes (importPriv).
const importPriv = (bytes) => crypto.subtle.importKey('pkcs8', bytes, { name: 'X25519' }, false, ['deriveBits']);
async function newKeys(keep = false) {
  const pair = await crypto.subtle.generateKey({ name: 'X25519' }, keep, ['deriveBits']);
  const pub = bytesOf(await crypto.subtle.exportKey('raw', pair.publicKey));
  if (!keep) return { priv: pair.privateKey, pub };
  const privBytes = bytesOf(await crypto.subtle.exportKey('pkcs8', pair.privateKey));
  return { priv: await importPriv(privBytes), privBytes, pub };
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
  if (!frame || frame.k !== 'welcome' || frame.id !== p.id) throw new Error('the answer was not for this phone');
  const blob = b64d(frame.c);
  const keys = await sessionKeys(p, blob.subarray(0, 32));
  if (frame.sid !== keys.sid) throw new Error('the answer did not check out');
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
// A storage that cannot be read is remembered (S.pairingError): a phone
// whose setup is there but unreadable gets other words than one never set up.
const loadPairing = () => idb('readonly', (st) => st.get('pairing'))
  .then(async (rec) => {
    S.pairingError = '';
    if (rec && !rec.priv && rec.privBytes) rec.priv = await importPriv(rec.privBytes); // the kept bytes, usable
    return rec;
  })
  .catch((e) => { S.pairingError = (e && e.message) || String(e); return undefined; });
// A record with its key's bytes is kept without the CryptoKey; one from
// before (the CryptoKey alone, kept by browsers that give it back) stays as it is.
function savePairing(rec) {
  const kept = { ...rec };
  if (kept.privBytes) delete kept.priv;
  return idb('readwrite', (st) => st.put(kept, 'pairing'));
}

// On a phone the home-screen app and the browser keep separate storage, so
// each is its own device; the name says which one was set up.
const standalone = () => matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const phoneHere = () => matchMedia('(pointer: coarse)').matches;
function deviceName() {
  const ua = navigator.userAgent;
  const kind = ['iPhone', 'iPad', 'Android', 'Mac', 'Windows', 'Linux'].find((k) => ua.includes(k));
  return `${kind || 'Device'} ${standalone() ? 'home-screen app' : 'browser'}`;
}

// What this browser saw when it could not connect, kept for the bot
// (POST /diag on the next connection, logged there): a phone that fails
// off the owner's network can be read about without a cable.
const DIAG_KEY = 'tacitus-diag';
function diagNote(entry) {
  try {
    const kept = JSON.parse(localStorage.getItem(DIAG_KEY) || '[]');
    kept.push({ at: new Date().toISOString(), standalone: standalone(), ua: navigator.userAgent, ...entry });
    localStorage.setItem(DIAG_KEY, JSON.stringify(kept.slice(-20)));
  } catch { /* no storage: the note is lost */ }
}
async function flushDiag() {
  let kept = [];
  try { kept = JSON.parse(localStorage.getItem(DIAG_KEY) || '[]'); } catch { return; }
  if (!kept.length) return;
  await call('POST', '/diag', { entries: kept });
  try { localStorage.removeItem(DIAG_KEY); } catch { /* sent anyway */ }
}
// Whether this browser's IndexedDB keeps a plain value: 'ok', or the error.
const idbProbe = () => idb('readwrite', (st) => st.put(Date.now(), 'probe'))
  .then(() => idb('readonly', (st) => st.get('probe'))).then((v) => (v ? 'ok' : 'empty'))
  .catch((e) => `error: ${(e && e.message) || e}`);

// One line on this browser's own setup, for the phone dialog.
async function selfState() {
  const rec = await loadPairing();
  const what = phoneHere() ? 'phone' : 'browser';
  if (S.pairingError) return `This ${what} cannot read its saved setup (${S.pairingError}). Close the app fully and open it again.`;
  if (rec) return `This ${what} is set up${rec.code ? ` (code ${spaced(rec.code)})` : ''}.`;
  return `This ${what} is not set up yet.` + (phoneHere() && !standalone()
    ? ' The home-screen app keeps its own setup: set it up there if that is where you use Tacitus.' : '');
}

// What a relay error says. An err frame is not authenticated (the relay
// can forge one), so it is only a hint: the app reports it and tries
// again, and never drops its keys over one.
const RELAY_SAYS = {
  auth: 'Tacitus did not accept this device. If it was removed, set it up again under Settings, then Devices.',
  offline: 'Tacitus cannot be reached away from home right now.',
  timeout: 'Tacitus did not answer in time.',
  busy: 'The away-from-home connection is busy.',
  unsure: 'the connection broke off, and Tacitus may have it anyway. Look in the conversation before you send it again.',
};
const pendingNote = (rec) => (rec.hosted ? 'This computer waits for approval. On the phone or computer that gave you the code,'
  : 'This phone waits for approval. On your computer, open Settings, then Devices, in Tacitus and') + ` tap Approve next to code ${spaced(rec.code)}.`;

class RelayError extends Error {
  constructor(code, text) {
    super(RELAY_SAYS[code] || text || 'The away-from-home connection did not answer.');
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
// "Add a device") opened here: the fragment leaves the address bar first,
// then this browser makes its key pair, keeps it, and pairs through the
// relay. The new phone then waits for the owner's approval, by its code.
// `text` is an object for a computer set up with a code (setUp): the
// pairing opened from the sealed handoff, never in an address.
async function pairFromLink(kind, text, self = false) {
  history.replaceState(null, '', location.pathname + location.search);
  const old = await loadPairing();
  const hosted = typeof text === 'object';
  let saved = false;
  try {
    const p = hosted ? text : JSON.parse(TD.decode(b64d(text)));
    if (!p || p.v !== (kind === 'tr2' ? 2 : 1) || !/^[0-9a-f]{32}$/.test(p.p) || b64d(String(p.s)).length !== 32
      || (p.b !== undefined && !/^[0-9a-f]{64}$/.test(p.b))) {
      throw new Error('this is not a setup link');
    }
    if (p.a !== undefined && new URL(p.a).origin !== location.origin) throw new Error('this link opens the app elsewhere');
    if (!(p.x * 1000 > Date.now())) throw new Error('the link has expired; open a new one under Settings, then Devices');
    // On the bot's own origin the link gives only the pairing secret: the
    // relay, the install and the keys come from the bot, which answers
    // there. A link whose relay is this page's origin is for the relay's
    // copy of the app, where no bot answers: it needs the bot's hash (`b`).
    let at = p;
    if (hosted) { // the website's copy: like the relay's copy, it checks its code against `b`
      if (!p.b || p.a !== appHere()) throw new Error('the code is for Tacitus at another address');
      if (typeof verifyBundle !== 'function') return halt(UNCHECKED);
    } else if (new URL(p.r).origin !== location.origin) {
      const got = await directGet('/relay/status');
      const st = got && got.status === 200 ? await got.json().catch(() => null) : null;
      if (!st || !st.install_id) throw new Error('open the link while this phone is on your own network');
      if (st.install_id !== p.i || st.bot_public !== p.k) throw new Error('the link is for a different Tacitus');
      at = { r: st.relay_url, i: st.install_id, k: st.bot_public, c: st.client_key };
    } else if (!p.b) throw new Error('the link has no fingerprint of the app; open a new one under Settings, then Devices');
    else if (typeof verifyBundle !== 'function') return halt(UNCHECKED); // before it makes a key
    if (!safeRelay(at.r)) throw new Error('the link does not use a secure (https) connection');
    if (!/^[a-z2-7]{26}$/.test(at.i) || b64d(String(at.k)).length !== 32 || !/^[A-Za-z0-9_-]{43}$/.test(at.c)) {
      throw new Error('this is not a setup link');
    }
    if (old && !confirm('This browser is set up already. Replace that setup?')) return;
    const keys = await newKeys(true);
    const rec = { v: 1, relay: String(at.r).replace(/\/+$/, ''), install: at.i, clientKey: at.c,
      botPublic: b64d(at.k), priv: keys.priv, privBytes: keys.privBytes, pub: keys.pub, device: '', bundle: p.b || '', hosted };
    // Kept before the hello: a phone whose welcome is lost says a plain
    // hello next time, with the key the bot registered.
    await savePairing(rec);
    saved = true;
    const relay = relayTransport(rec);
    await relay.hello({ id: p.p, psk: b64d(p.s) });
    Object.assign(rec, { device: relay.session.device, code: relay.code });
    await savePairing(rec);
    const back = await loadPairing();
    if (!back || back.device !== rec.device) { // the setup must survive this page
      diagNote({ what: 'pairing-readback', got: back ? 'other' : 'none', pairingError: S.pairingError || '', idb: await idbProbe() });
      toast('This phone saved its setup but cannot read it back, so it may not work away from home.');
    }
    RELAY = relay;
    if (self) { // the owner set up the phone in hand: signed in here, so no code to compare
      await call('POST', '/relay/approve', { device: rec.device, cards: false });
      relayNote('This phone is set up. It works anywhere now; card buttons work when you are on your own network.');
    } else {
      relayNote(pendingNote(rec));
    }
    return true;
  } catch (e) {
    if (saved && old) await savePairing(old).catch(() => {}); // a failed pairing keeps the earlier one
    else if (saved && hosted) await idb('readwrite', (st) => st.delete('pairing')).catch(() => {}); // its code is spent: set up anew
    toast(`Setup failed: ${e.message}`);
    return false;
  }
}

/* -- A computer through the website's copy of the app ----------------- */

// The owner's device asks the bot for a handoff (POST /api/relay/handoff):
// a short code, and the pairing sealed under a key the code gives, left
// with the relay under a mailbox the code and that relay name
// (docs/webchat-relay.md). The computer types the code at the website's
// copy of the app, takes the sealed pairing once and pairs as from a link,
// with no secret in its address. Both come from one PBKDF2 block, so no
// relay can try codes fast, and each relay sees a mailbox of its own.
const CODE_ABC = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';
const HANDOFF = 'tacitus-handoff/1';
const appHere = () => `${location.origin}${BASE}/`;
const safeRelay = (u) => { try { const url = new URL(u); return url.protocol === 'https:'
  || (url.protocol === 'http:' && LOOPBACK_HOSTS.includes(url.hostname)); } catch { return false; } };

// The ten symbols of a typed code, or '' when it is not one, read as the bot reads it (ASCII only).
function handoffCode(text) {
  const s = String(text || '').toUpperCase().replace(/[ -]/g, '').replace(/O/g, '0').replace(/[IL]/g, '1');
  return /^[\x20-\x7e]*$/.test(String(text || '')) && s.length === 10 && [...s].every((c) => CODE_ABC.includes(c)) ? s : '';
}

async function handoffSecret(code, iterations = 600000) {
  const base = await crypto.subtle.importKey('raw', TE.encode(code), 'PBKDF2', false, ['deriveBits']);
  return bytesOf(await crypto.subtle.deriveBits({ name: 'PBKDF2', hash: 'SHA-256', salt: TE.encode(HANDOFF), iterations }, base, 256));
}
const hmac256 = async (key, text) => bytesOf(await crypto.subtle.sign('HMAC',
  await crypto.subtle.importKey('raw', key, { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']), TE.encode(text)));
// The mailbox at one relay, and the key (the same at each), from the code's secret.
const handoffKeys = async (secret, relay) => ({ key: await hmac256(secret, `${HANDOFF} key`),
  mailbox: hex((await hmac256(secret, `${HANDOFF} mailbox\0${relay.replace(/\/+$/, '')}`)).subarray(0, 16)) });

// The pairing in a sealed handoff (nonce, then the AES-GCM text); throws when it does not open.
async function openHandoff(keys, blob) {
  const raw = b64d(String(blob));
  const plain = await ungcm(await gcmKey(keys.key), raw.subarray(0, 12), raw.subarray(12), TE.encode(HANDOFF + keys.mailbox));
  return JSON.parse(TD.decode(plain));
}

// The relays to ask: the address's ?relay=, else the website's list
// (relays.json); null where neither is there (not the website's copy).
async function hostedRelays() {
  const given = new URLSearchParams(location.search).get('relay');
  let list = given ? [given] : null;
  if (!list) {
    const res = await fetch(`${BASE}/relays.json`, { cache: 'no-store', credentials: 'omit' }).catch(() => null);
    list = res && res.ok ? await res.json().catch(() => null) : null;
  }
  return Array.isArray(list) ? list.filter((u) => typeof u === 'string' && safeRelay(u)).map((u) => u.replace(/\/+$/, '')) : null;
}

function showSetup(relays) {
  S.relays = relays;
  $('setup').hidden = false;
  $('setup-code').focus();
}

const WRONG_CODE = 'That code did not work. Check it, or make a new one.';
async function setUp(e) {
  e.preventDefault();
  const say = (text, cls = 'error') => { $('setup-error').textContent = text; $('setup-error').className = cls; };
  const code = handoffCode($('setup-code').value);
  if (!code) return say('A code has ten letters and digits, like ABCDE-FGHJK.');
  if (!S.relays.length) return say('Open the whole address your other device shows, then type the code.');
  say('Checking the code...', '');
  $('setup-go').disabled = true;
  try {
    const secret = await handoffSecret(code);
    let p = null;
    for (const relay of S.relays) { // a relay that answers nothing that opens, or in time, does not end the search
      const keys = await handoffKeys(secret, relay);
      const res = await fetch(`${relay}/h/${keys.mailbox}`, { credentials: 'omit', cache: 'no-store', signal: AbortSignal.timeout(setUp.wait) }).catch(() => null);
      if ((p = res && res.ok ? await res.json().then((got) => openHandoff(keys, got.c)).catch(() => null) : null)) break;
    }
    if (!p || p.v !== 2) return say(WRONG_CODE);
    if (p.a !== appHere()) return say('That code is for Tacitus at another address. Make a new one and open the address it shows.');
    $('setup').hidden = true;
    if (await pairFromLink('tr2', p)) return boot();
    $('setup').hidden = false;
    say('Setup did not finish. Make a new code and try again.');
  } finally {
    $('setup-go').disabled = false;
  }
}
setUp.wait = 8000; // ms for each relay's answer, its body included

// "Add a device". This phone: one button pairs this browser in place and
// approves it, the owner being signed in here already. Another phone scans
// a QR code, then waits for Approve beside its code; a relay that serves the
// app adds the way for a phone that never joins the tailnet. A computer
// types a short code at the website's copy of the app (handoff).
const ownerRoutes = () => (S.me && S.me.owner_routes) || (T.relay ? 'none' : 'all');
async function addDevice(way) {
  const dlg = $('phone');
  if (!dlg.open) dlg.showModal();
  selfState().then((text) => { $('phone-self').textContent = text; });
  pairPhone.out = handoff.out = null;
  $('phone-link').value = ''; // nothing of an earlier opening shows while the bot answers
  $('phone-qr').width = 0;
  const st =await call('GET', '/relay/status').catch(() => ({})); // relayed, the bot may refuse it: owner_routes says
  const routes = ownerRoutes();
  const own = isSecureContext && Boolean(S.me.public_url) && new URL(S.me.public_url, location.href).origin === location.origin;
  const can = { here: routes === 'all' && own && Boolean(st.pair_own), phone: routes === 'all',
    computer: routes === 'handoff' ? Boolean(S.me.handoff) : routes === 'all' && Boolean(st.handoff) };
  Object.assign(addDevice, { st, can });
  Object.entries(can).forEach(([w, ok]) => { $(`way-${w}`).hidden = !ok; });
  $('way-here').textContent = phoneHere() ? 'This phone' : 'This computer';
  $('add-ways').hidden = Object.values(can).filter(Boolean).length < 2;
  showWay([way, phoneHere() && 'here', 'phone', 'computer'].find((w) => w && can[w]) || 'phone');
  clearInterval(addDevice.timer);
  addDevice.timer = setInterval(() => (dlg.open ? listPhones() : clearInterval(addDevice.timer)), 3000);
  listPhones();
}

function showWay(way) {
  for (const w of ['here', 'phone', 'computer']) {
    $(`add-${w}`).hidden = w !== way;
    $(`way-${w}`).setAttribute('aria-pressed', String(w === way));
  }
  if (way === 'computer') handoff();
  else if (!pairPhone.out) pairPhone();
}

async function pairPhone(via) {
  const st = addDevice.st || {};
  $('phone-link').value = '';
  $('phone-qr').width = 0;
  $('phone-here').hidden = true;
  $('phone-scan').hidden = true;
  $('phone-note').textContent = 'One moment...';
  const relayWay = Boolean(st.pair_relay && st.relay_app);
  $('phone-relay').hidden = via === 'relay' || !relayWay;
  if (via !== 'relay' && !addDevice.can.here) {
    $('phone-note').textContent = relayWay ? 'This address does not open on a phone. Use "Phone not on your network" instead.'
      : st.pair_own ? 'Open Tacitus at its address on your own network to set up a phone.'
      : 'Setting up a phone needs the signed app, which this copy of Tacitus does not have.';
    return;
  }
  try {
    const out = pairPhone.out = await call('POST', '/relay/pair', via === 'relay' ? { via } : {});
    QR.draw($('phone-qr'), out.qr, 4);
    $('phone-link').value = out.link;
    const frag = /#(tr[12])=([A-Za-z0-9_-]+)$/.exec(out.link);
    if (out.origin !== 'relay' && frag) {
      $('phone-here').hidden = false;
      $('phone-here').textContent = phoneHere() ? 'Set up this phone' : 'Set up this browser';
      $('phone-here').onclick = () => { $('phone').close(); pairFromLink(frag[1], frag[2], true); };
      $('phone-scan').hidden = false;
    }
    $('phone-note').textContent = out.origin === 'relay'
      ? `Scan this with the phone's camera. It opens Tacitus from anywhere; once you tap Approve below it can read and reply, and press buttons only if you allow it. The QR code works once, until ${when(out.expires)}. Never send this link through chat or mail.`
      : `When the other phone shows a six-digit code, tap Approve below if it is the same code. The QR code works once, until ${when(out.expires)}. Never send this link through chat or mail.`;
  } catch (e) {
    $('phone-note').textContent = `Could not start: ${e.message}`;
  }
}

// A computer: the bot makes the code (POST /api/relay/handoff); the address
// to open and the code show here, and the computer that took it waits below.
async function handoff() {
  if (handoff.out && handoff.out.expires * 1000 > Date.now()) return;
  $('handoff-code').textContent = $('handoff-open').textContent = '';
  $('handoff-note').textContent = 'One moment...';
  try {
    const out = handoff.out = await call('POST', '/relay/handoff', {});
    $('handoff-code').textContent = out.code;
    $('handoff-open').textContent = String(out.open || '').replace(/^https:\/\//, '');
    $('handoff-note').textContent = `The code works once, until ${when(out.expires)}. Never send this code through chat or mail.`;
  } catch (e) {
    $('handoff-note').textContent = `Could not start: ${e.message}`;
  }
}

// The computer that waits: the one that took the code shown here (its `pairing`), not yet approved.
function waiting(rows, mine) {
  const d = S.waiting = handoff.out && rows.find((r) => r.pending && r.pairing === handoff.out.pairing && mine(r));
  $('handoff-wait').hidden = !d;
  if (!d) return;
  $('handoff-ask').textContent = `The computer shows a six-digit code. Tap Approve if it is ${spaced(d.code)}.`;
  $('handoff-cards').checked = CARDS.has(d.id);
}

// The devices the bot lists, with Remove and Approve; relayed, Remove, Approve and the
// card choice only for a computer this phone added (`by`): the bot refuses the rest.
const WHERE = { own: '', hosted: ', opened with a code', relay: ', not on your network' };
async function listPhones() {
  const box = $('phone-devices');
  const me = T.relay ? T.rec.device : '';
  const mine = (d) => !me || (d.handoff === true && d.by === me);
  try {
    const rows = await call('GET', '/relay/devices');
    waiting(rows, mine);
    box.replaceChildren(...rows.map((d) => h('li', { 'data-device': d.id },
      h('span', {}, d.pending ? `${d.name}, code ${spaced(d.code)}, waits for approval` : `${d.name}, set up ${when(d.paired_at, true)}`
        + (d.origin === 'own' ? '' : `${WHERE[d.origin] ?? WHERE.relay}${d.bundle_verified ? '' : ', app not checked'}`)),
      d.origin === 'own' || !mine(d) ? null : h('label', {}, h('input', { type: 'checkbox', checked: d.pending ? CARDS.has(d.id) : d.cards_allowed,
        onchange: (e) => cardsFor(d, e.target.checked) }), d.origin === 'hosted' ? 'Allow Send and Create buttons on this computer'
        : 'Allow card buttons on this phone'),
      d.pending && mine(d) ? h('button', { type: 'button', class: 'btn small primary', onclick: () => phoneAct('approve', d) }, 'Approve') : null,
      !mine(d) || d.this_device ? null : h('button', { type: 'button', class: 'btn small plain', onclick: () => phoneAct('revoke', d) }, 'Remove'))));
    if (!rows.length) box.append(h('li', { class: 'empty' }, 'No device is set up yet.'));
  } catch (e) {
    box.replaceChildren(h('li', { class: 'error' }, e.message));
  }
}

// Card buttons on a relay-origin phone are the owner's choice: sent with Approve, or by approving again.
const CARDS = new Set();
const cardsFor = (d, on) => { CARDS[on ? 'add' : 'delete'](d.id); if (!d.pending) phoneAct('approve', d); };

async function phoneAct(verb, d) {
  if (verb === 'revoke' && !confirm(`Remove ${d.name}? It can no longer reach Tacitus away from home.`)) return;
  const cards = verb === 'approve' ? CARDS.has(d.id) : undefined; // revoke takes the device alone
  try { await call('POST', `/relay/${verb}`, { device: d.id, cards }); } catch (e) { toast(e.message); }
  listPhones();
}

/* -- Settings: which model does each task (docs/llm-lanes.md, 1.1) -- */

// One row per task: local, hosted or auto (the remote-LLM switches decide), what the task
// does, the model it runs on now and, where a floor holds a value, what the floor says.
// A task with a Codex route also picks its hosted model, Claude or Codex; under any
// other task a line says what Hosted means there. The local model's state heads the
// screen. Only the owner's own web chat saves.
const ROUTE_WORDS = { local: 'Local', hosted: 'Hosted', auto: 'Auto' };
const cap = (t) => (t ? t[0].toUpperCase() + t.slice(1) : t);

function hostedPick(data, r) {
  const names = data.hosted_names || {};
  return h('span', { class: 's-hosted' },
    h('span', { class: 's-sub', id: `hosted-${r.task}-name` }, 'Hosted model'),
    h('span', { class: 'seg', role: 'radiogroup', 'aria-labelledby': `hosted-${r.task}-name` },
      (data.hosted_values || []).map((m) => h('label', {},
        h('input', { type: 'radio', name: `hosted-${r.task}`, value: m, checked: r.hosted === m,
          disabled: !r.hosted_allowed.includes(m) }), names[m] || m))));
}

// A "?" beside a task's name opens what the task is, in plain words, under the row:
// a tap on a phone, a click on a computer; a second press closes it.
function helpFor(r) {
  if (!r.help) return [null, null];
  const note = h('p', { class: 's-help', id: `help-${r.task}`, hidden: true }, r.help);
  const btn = h('button', { type: 'button', class: 's-help-btn', 'aria-expanded': 'false',
    'aria-controls': note.id, 'aria-label': `What is "${r.label}"?`,
    onclick: () => { note.hidden = !note.hidden; btn.setAttribute('aria-expanded', String(!note.hidden)); } },
  h('span', { 'aria-hidden': 'true' }, '?'));
  return [btn, note];
}

function renderSettings(data) {
  const loc = data.local;
  $('settings-local').textContent = loc.configured ? `Local model ${loc.model}: `
    + `${loc.reachable === false ? 'not reachable' : loc.reachable ? 'reachable' : 'not checked yet'}. ${loc.detail}` : loc.detail;
  $('settings-local').className = loc.reachable === false ? 'error' : '';
  $('settings-rows').replaceChildren(...data.rows.map((r) => { const [ask, help] = helpFor(r); return h('li', { 'data-task': r.task },
    h('span', { class: 's-name' }, r.label, ask),
    h('span', { class: 'seg', role: 'radiogroup', 'aria-label': r.label }, data.values.map((v) => h('label', {},
      h('input', { type: 'radio', name: `route-${r.task}`, value: v, checked: r.value === v,
        disabled: !r.allowed.includes(v) }), ROUTE_WORDS[v] || v))),
    help,
    (r.hosted_allowed || []).length > 1 ? hostedPick(data, r) : null,
    h('p', {}, r.what),
    r.hosted_note ? h('p', { class: 's-note' }, r.hosted_note) : null,
    h('p', { class: 's-now' }, `Now on ${r.runs_on || r.effective}. ${cap(r.why)}.`),
    r.floor ? h('p', { class: 's-floor' }, r.floor) : null); }));
}

// The sections the bot serves: the routing table, and the notification switches where push works.
const settingsShown = () => {
  const pages = (S.me && S.me.settings) || [];
  return { notify: pages.includes('notify') && Boolean(S.me.push), routing: pages.includes('routing') };
};

/* -- Settings: the menu and its pages ------------------------------- */

// The drawer's one Settings entry opens a menu of what this browser can change:
// Notifications where the bot pushes, Devices where adding one works from here,
// Models where the bot serves the table and the copy is the owner's own, and the
// tour; under them, how this browser reaches Tacitus now. A page opens in place
// with Back, and Escape on a page goes back to the menu; Devices and the tour
// are dialogs of their own, so the menu closes for them.
const PAGES = { menu: ['settings-menu', 'Settings'], notify: ['notify-settings', 'Notifications'],
  routing: ['routing-settings', 'Models'] };

function openSettings(page = 'menu') {
  if (!$('settings').open) $('settings').showModal();
  settingsPage(page);
}

async function settingsPage(page) {
  const from = $('settings').dataset.page;
  for (const [name, [id]] of Object.entries(PAGES)) $(id).hidden = name !== page;
  $('settings-title').textContent = PAGES[page][1];
  $('settings-back').hidden = page === 'menu';
  $('settings').dataset.page = page;
  $('settings').scrollTop = 0;
  if (page === 'menu') {
    connLine();
    if (from && from !== 'menu') $(`go-${from}`).focus(); // back where Back came from
    return;
  }
  $('settings-back').focus();
  if (page === 'notify') {
    $('notify-note').textContent = '';
    notifyState();
    listSubs();
    if (!$('notify-kinds').hidden) {
      call('GET', '/settings/notify').then(renderNotify,
        (e) => { $('notify-note').textContent = `Could not read the settings: ${e.message}`; });
    }
    return;
  }
  $('settings-note').textContent = '';
  try { renderSettings(await call('GET', '/settings/routing')); } catch (e) { $('settings-note').textContent = `Could not read the settings: ${e.message}`; }
}

// The menu's last line: how this browser reaches Tacitus now, the live dot in words.
function connLine() {
  const dot = $('live').classList;
  $('settings-conn').textContent = dot.contains('off') ? 'Not connected right now. Trying again.'
    : dot.contains('wait') ? 'Connecting...'
    : T.relay ? `Connected away from home, through ${new URL(T.rec.relay).host}.` : 'Connected on your own network.';
}

function settingsMenu() {
  $('open-settings').addEventListener('click', () => openSettings());
  $('settings-back').addEventListener('click', () => settingsPage('menu'));
  $('go-notify').addEventListener('click', () => settingsPage('notify'));
  $('go-routing').addEventListener('click', () => settingsPage('routing'));
  $('settings').addEventListener('cancel', (e) => {
    if ($('settings').dataset.page !== 'menu') { e.preventDefault(); settingsPage('menu'); }
  });
  $('pair-phone').addEventListener('click', () => { $('settings').close(); addDevice(); });
  $('tour-again').addEventListener('click', () => { $('settings').close(); welcome(0); });
  $('settings-save').addEventListener('click', saveSettings);
  $('notify-rows').addEventListener('change', saveNotify);
}

async function saveSettings() {
  const tasks = {}, hosted = {};
  for (const el of $('settings-rows').querySelectorAll('input:checked:not(:disabled)')) {
    if (el.name.startsWith('hosted-')) hosted[el.name.slice(7)] = el.value;
    else tasks[el.name.slice(6)] = el.value;
  }
  $('settings-save').disabled = true;
  try {
    renderSettings(await call('POST', '/settings/routing', Object.keys(hosted).length ? { tasks, hosted } : { tasks }));
    $('settings-note').textContent = 'Saved. Each task uses its model from its next run.';
  } catch (e) {
    $('settings-note').textContent = `Not saved: ${e.message}`;
  } finally {
    $('settings-save').disabled = false;
  }
}

/* -- Settings: notifications (docs/webchat.md, Notifications) ------- */

// Three switches and the quiet-hours choice: the web block's notify_push, which the bot
// writes. Each change saves at once and the section redraws from the answer; a refusal
// stays on screen and the section shows what is saved.
function renderNotify(data) {
  for (const el of $('notify-rows').querySelectorAll('input')) {
    el.checked = el.type === 'radio' ? data.quiet === el.value : data[el.name] === true;
  }
}

async function saveNotify(e) {
  const el = e.target;
  if (!el.name || (el.type === 'radio' && !el.checked)) return;
  const inputs = [...$('notify-rows').querySelectorAll('input')];
  inputs.forEach((i) => { i.disabled = true; }); // one save at a time: each answer is the whole section
  $('notify-note').textContent = '';
  try {
    renderNotify(await call('POST', '/settings/notify', { [el.name]: el.type === 'radio' ? el.value : el.checked }));
    $('notify-note').textContent = 'Saved.';
  } catch (err) {
    $('notify-note').textContent = `Not saved: ${err.message}`;
    await call('GET', '/settings/notify').then(renderNotify, () => {});
  } finally {
    inputs.forEach((i) => { i.disabled = false; });
  }
}

/* -- A room's menu: rename, archive, bring back -------------------- */

// A room in the drawer has a menu: its dots button opens it with a mouse, a
// long press (600 ms, held still) with a finger; the fixed places have none.
// It renames the room in place, archives it after one plain question, or
// brings an archived one back. The answer redraws the drawer at once, and
// the bot's `channel` event does in every other open app.
function roomRow(c, link) {
  if (c.kind !== 'room') return link;
  return h('div', { class: 'chan-row', 'data-room': c.id }, link,
    h('button', { type: 'button', class: 'room-more', 'aria-label': `Room options: ${channelName(c)}`,
      'aria-haspopup': 'dialog', onclick: () => roomMenu(c.id) }));
}

function roomMenu(id, mode = 'menu') {
  const c = chan(id), dlg = $('room-menu');
  if (!c || c.kind !== 'room') return;
  const name = channelName(c);
  const cancel = h('button', { type: 'button', class: 'btn', onclick: () => dlg.close() }, 'Cancel');
  const item = (label, next, cls = '') => h('button', { type: 'button', class: `sheet-item ${cls}`, onclick: next }, label);
  const error = h('p', { class: 'error', id: 'room-menu-error', 'aria-live': 'polite' });
  const body = {
    rename: () => [h('label', { for: 'room-menu-name' }, 'New name'),
      h('input', { id: 'room-menu-name', value: name, maxlength: 60, autocomplete: 'off', enterkeyhint: 'done', 'aria-describedby': 'room-menu-error' }),
      error, h('div', { class: 'dialog-actions' }, cancel, h('button', { type: 'submit', class: 'btn primary' }, 'Save'))],
    archive: () => [h('p', {}, `Archive ${name}? Its conversations stay; the room moves under Archived.`),
      error, h('div', { class: 'dialog-actions' }, cancel, h('button', { type: 'submit', class: 'btn primary danger' }, 'Archive'))],
    menu: () => [...(c.archived ? [item('Bring back', () => roomDo(c, 'unarchive'))]
      : [item('Rename room', () => roomMenu(id, 'rename')), item('Archive room', () => roomMenu(id, 'archive'), 'danger')]), error, cancel],
  }[mode]();
  dlg.replaceChildren(h('form', { class: 'sheet', novalidate: true, 'data-mode': mode,
    onsubmit: (e) => { e.preventDefault(); roomDo(c, mode); } }, h('h2', { id: 'room-menu-title' }, name), body));
  if (!dlg.open) { roomMenu.at = Date.now(); dlg.showModal(); }
  const field = $('room-menu-name');
  if (field) { field.focus(); field.select(); }
}

async function roomDo(c, verb) {
  const dlg = $('room-menu');
  let body;
  if (verb === 'rename') {
    const typed = $('room-menu-name').value.trim().replace(/\s+/g, ' ');
    if (!typed) { $('room-menu-error').textContent = 'Type a name for the room.'; return; }
    if (typed === channelName(c)) { dlg.close(); return; }
    // The drawer shows a room without the bot's "ea-" mark; its new name keeps the mark.
    body = { name: /^ea-./.test(c.name || '') && !typed.startsWith('ea-') ? `ea-${typed}` : typed };
  }
  const buttons = [...dlg.querySelectorAll('button')];
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const row = verb === 'rename' ? await call('PATCH', `/channels/${enc(c.id)}`, body)
      : await call('POST', `/channels/${enc(c.id)}/${verb}`);
    if (row && row.id) apply('channel', row);
    dlg.close();
  } catch (e) {
    $('room-menu-error').textContent = `Not changed: ${e.message}`;
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// The menu's own dialog, a tap outside it to close it (not the end of the
// press that opened it), and the long press on the drawer's rooms.
function roomMenus() {
  const dlg = h('dialog', { id: 'room-menu', 'aria-labelledby': 'room-menu-title' });
  dlg.addEventListener('click', (e) => { if (e.target === dlg && Date.now() - roomMenu.at > 800) dlg.close(); });
  document.body.append(dlg);
  const list = $('channel-list');
  let timer = 0, fired = 0, at = [0, 0];
  const stop = () => { clearTimeout(timer); timer = 0; };
  list.addEventListener('pointerdown', (e) => {
    const row = e.pointerType === 'touch' && e.target.closest('.chan-row');
    if (!row) return;
    at = [e.clientX, e.clientY];
    timer = setTimeout(() => { timer = 0; fired = Date.now(); roomMenu(row.dataset.room); }, 600);
  });
  list.addEventListener('pointermove', (e) => { if (timer && Math.hypot(e.clientX - at[0], e.clientY - at[1]) > 10) stop(); });
  ['pointerup', 'pointercancel'].forEach((t) => list.addEventListener(t, stop));
  list.addEventListener('click', (e) => { if (Date.now() - fired < 800) { e.preventDefault(); e.stopPropagation(); } }, true);
  list.addEventListener('contextmenu', (e) => { if (e.target.closest('.chan-row') && (timer || Date.now() - fired < 800)) e.preventDefault(); });
}

/* -- Teaching: the welcome, the examples, New room, empty lists ------ */

// The welcome: three cards, shown once per browser after the channels
// load, again from "How Tacitus works". Next, a swipe or an arrow key
// moves; Skip, Done or Escape closes it, and closing it is what counts.
const WELCOME_KEY = 'tacitus-welcome-seen';
const mainChannel = () => S.channels.find((c) => c.kind === 'main' && !c.archived);

function welcome(at) {
  const cards = [...$('welcome-cards').children];
  S.card = Math.max(0, Math.min(cards.length - 1, at));
  cards.forEach((c, i) => c.classList.toggle('on', i === S.card));
  [...$('welcome-dots').children].forEach((d, i) => d.classList.toggle('on', i === S.card));
  $('welcome-dots').setAttribute('aria-label', `Card ${S.card + 1} of ${cards.length}`);
  $('welcome-next').textContent = S.card === cards.length - 1 ? 'Done' : 'Next';
  $('welcome-skip').hidden = S.card === cards.length - 1;
  if ($('welcome').open) return;
  document.body.classList.remove('drawer');
  $('welcome').showModal();
  $('welcome').focus(); // the dialog itself, not its first example: Enter taps nothing by surprise
}

function welcomeOnce() {
  let seen = '';
  try { seen = localStorage.getItem(WELCOME_KEY); } catch { /* no storage: shown each visit */ }
  if (!seen && !S.welcomed) welcome(0);
  S.welcomed = true;
}

// An example opens a new conversation in the EA channel with its words in
// the composer; a name in bold is selected, so typing replaces it. The view
// changes at once, so the focus opens a phone's keyboard within the tap.
function prefill(btn) {
  const main = mainChannel();
  if (!main) return;
  if ($('welcome').open) $('welcome').close();
  const text = btn.textContent;
  const slot = (btn.querySelector('b') || {}).textContent || '';
  const at = slot ? text.indexOf(slot) : text.length;
  location.hash = hashFor(main.id, NEW_THREAD);
  document.body.dataset.view = 'thread';
  document.body.classList.remove('drawer');
  $('text').value = text;
  grow();
  $('text').focus();
  $('text').setSelectionRange(at, at + slot.length);
}

const example = () => h('button', { type: 'button', class: 'example', onclick: (e) => prefill(e.currentTarget) },
  'Find me an hour with ', h('b', {}, 'Dana'), ' next week.');

// What an empty list says: what the place is for, and in the EA channel an example to tap.
function emptyNote(kind) {
  if (kind === 'main') return h('div', { class: 'empty' }, h('p', {}, 'Ask for something. For example:'), example());
  return h('p', { class: 'empty' }, {
    dm: 'Cards that need your tap land here.',
    room: 'This room is for one project. Ask about it here and Tacitus keeps track of it.',
    sched: 'Tacitus finds meeting times here.', digest: DIGEST_NOTE,
  }[kind] || 'Nothing here yet.');
}

// The digest channel takes no message: the bot posts there and reads no reply. Its threads
// show no composer and no New conversation, and a line says why.
const DIGEST_NOTE = 'Tacitus posts your mail digests here; it does not read replies.';
function digestView() {
  const digest = !S.needsView && (chan(S.channel) || {}).kind === 'digest';
  document.body.classList.toggle('digest', digest);
  $('composer-off').hidden = !digest;
}

// New room: the owner's own words to the bot, in a new conversation in the
// EA channel, which then opens; the bot answers there and the room arrives
// as a channel event.
function roomError(text) {
  $('room-error').textContent = text;
  $('room-name').setAttribute('aria-invalid', String(Boolean(text)));
}

async function openRoom(e) {
  e.preventDefault();
  const name = $('room-name').value.trim().replace(/\s+/g, ' ');
  const main = mainChannel();
  if (!name || !main) {
    roomError(main ? 'Type the name of the project.' : 'Tacitus has no place to ask for a room yet.');
    $('room-name').focus();
    return;
  }
  $('room-open').disabled = true;
  try {
    const m = await call('POST', `/channels/${enc(main.id)}/messages`, { text: `open a room for ${name}` });
    $('room').close();
    if (m && m.id) ingest(m);
    location.hash = hashFor(main.id, m && m.thread_key);
  } catch (err) {
    roomError(`Not sent: ${err.message}`);
  } finally {
    $('room-open').disabled = false;
  }
}

function teaching() {
  $('welcome-next').addEventListener('click', () => (S.card === $('welcome-cards').children.length - 1 ? $('welcome').close() : welcome(S.card + 1)));
  $('welcome').addEventListener('close', () => { try { localStorage.setItem(WELCOME_KEY, '1'); } catch { /* shown again next visit */ } });
  $('welcome').addEventListener('keydown', (e) => {
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') welcome(S.card + (e.key === 'ArrowRight' ? 1 : -1));
  });
  // A swipe across the cards: where the finger went down, and when it last
  // swiped, since the tap that ends a swipe over an example is no tap on it.
  let from = null, swiped = 0;
  $('welcome-cards').addEventListener('pointerdown', (e) => { from = [e.clientX, e.clientY]; });
  $('welcome-cards').addEventListener('pointerup', (e) => {
    const dx = from ? e.clientX - from[0] : 0;
    if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(e.clientY - from[1])) { swiped = Date.now(); welcome(S.card + (dx < 0 ? 1 : -1)); }
    from = null;
  });
  $('welcome-cards').addEventListener('click', (e) => { if (Date.now() - swiped < 500) e.stopPropagation(); }, true);
  for (const b of $('welcome').querySelectorAll('.example')) b.addEventListener('click', () => prefill(b));
  $('new-room').addEventListener('click', () => {
    $('room-name').value = '';
    roomError('');
    $('room').showModal();
  });
  $('room-form').addEventListener('submit', openRoom);
  $('room-cancel').addEventListener('click', () => $('room').close());
  $('room-name').addEventListener('input', () => roomError(''));
}

/* -- Code integrity (docs/webchat.md, Code integrity) ---------------- */

// The bot signs the app's files at start. The page checks the signed manifest
// (verifyBundle, bundle.js) and hands it to the service worker, which serves
// only matching files. The bot's own copy checks after start-up; the relay's
// before the chat runs, through the sealed channel, against its pairing's
// `bundle` (the QR's hash) first and the key that signed it (`signKey`) after,
// and takes each file its copy lacks from the bot (bring), never the relay.
// With neither, or no check that runs, it stops; checked, it attests. The
// website's copy (`hosted`) checks the same way, takes each file that
// differs from the bot and runs that (a reload): it never stops on a release.
const relayCopy = () => Boolean(T.relay && (T.rec.hosted || new URL(T.rec.relay).origin === location.origin));
const UNCHECKED = 'This copy of the app cannot check its code against your Tacitus, so it does not run. '
  + 'Set up this phone again once the away-from-home connection has the latest version.';

async function checkBundle() {
  const rec = T.relay ? T.rec : null;
  if (rec && !rec.bundle) return halt('This phone was set up without a fingerprint of the app. Set it up again.');
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
  const out = (await (rec ? bring(doc, v, true, rec.hosted) : toWorker({ bundle: doc }))) || (await checkFiles(v.files));
  if (out.refused) return refuseApp(out.refused, true);
  if (out.error || !out.ok) {
    if (rec) relayNote('This phone could not take the latest version of the app from Tacitus yet. Trying again.');
    throw new Error(out.error || 'the check of the app did not finish');
  }
  if (rec) await call('POST', '/relay/attest', { bundle: v.hash }); // the release its worker now keeps
  // The website's copy checks before its chat opens: the page runs the bot's release from a
  // reload. Once the chat is open, a release waits for the owner (appUpdated), as everywhere.
  if (out.updated && rec && rec.hosted && !S.ready) return location.reload();
  if (out.updated) appUpdated();
  return v;
}

// A release through the relay: the worker answers the files its copy lacks, the
// page fetches each from the bot (/api/app/<file>) and checks it against the
// manifest, and the worker checks each again and keeps all or none. `pin`: the
// relay's copy, whose first check this is too; `replace`: the website's copy
// (adopt in bundle.js). Null with no worker.
async function bring(doc, v, pin, replace = false) {
  const t = T;
  const out = await toWorker({ fromRelay: doc, pin, replace });
  if (!out || !out.want || !out.want.length) return out;
  const files = {};
  for (const path of out.want) {
    const res = await t.api('GET', `/app/${path}`);
    if (!res.ok) return { error: `${path}: HTTP ${res.status}` };
    const bytes = await res.arrayBuffer();
    if ((await sha256Hex(new Uint8Array(bytes))) !== v.files[path]) return { error: `${path} does not match the manifest` };
    files[path] = { type: res.headers.get('content-type') || '', bytes };
  }
  const last = (await toWorker({ fromRelay: doc, pin, replace, files })) || {};
  return last.want && last.want.length ? { error: `missing ${last.want.join(', ')}` } : last;
}

// The bot's own copy off the tailnet, after start-up: its worker cannot reach this origin.
async function updateOverRelay() {
  if (!S.me.bundle) return;
  const doc = await call('GET', '/bundle');
  const out = (await bring(doc, await verifyBundle(doc), false)) || {};
  if (out.error) throw new Error(out.error);
  if (out.updated) appUpdated();
}

// A new release is kept, and runs from the next start: the page does not
// reload in the middle of a conversation. The note above the composer stays
// until the owner taps Update (a reload, which runs the kept release) or the
// app starts again; the foot of the drawer says the same.
const UPDATE_LINE = 'A new version of Tacitus is ready.';
function appUpdated() {
  S.updateReady = true;
  $('app-update').hidden = false;
  $('update-note').textContent = UPDATE_LINE;
  $('update-note').hidden = false;
  footer();
}

// A release while the app is open. The bot says so on the stream when it signs
// the app again (`release`: a deploy that changed the app's files), a restart
// for a deploy drops the stream and the reconnect brings /me, and a phone that
// comes back to the screen asks /me (LOOK_MS apart). A /me that names another
// bundle than the one this page checked is taken as at start: checked and kept
// by the worker (checkBundle, or through the relay off the tailnet), and offered
// by appUpdated. A look that fails leaves S.me as it was, so the next one tries again.
const LOOK_MS = 60000;
async function lookAgain(me) {
  if (S.halted || !S.ready || S.updateReady || S.looking || !S.me) return;
  S.looking = true;
  try {
    const now = me || await call('GET', '/me');
    if (!now || !now.bundle || now.bundle === S.me.bundle) return;
    if (!T.relay || relayCopy()) await checkBundle();
    else await updateOverRelay();
    S.me = now;
  } finally {
    S.looking = false;
  }
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
  return { ok: true };
}

// A file that is not the signed one: the worker (`told` when it knows)
// drops its copy and moves each page to its refusal; this page stops here
// too, in case it cannot.
async function refuseApp(path, told) {
  halt(`This copy of the app is not the one your Tacitus signed: ${path} does not match it. Tacitus and the away-from-home `
    + 'connection run different versions, or the app was changed. Open the app on your own network, or tell whoever runs that connection.');
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
    if (!res.ok) throw new Error(res.status === 401 || res.status === 400 ? 'That code does not match.' : `HTTP ${res.status}`);
    $('pair').hidden = true;
    $('pair-token').value = '';
    boot();
  } catch (err) {
    $('pair-error').textContent = err.message;
  }
}

// What to say when the bot does not answer, in words a phone's owner can
// act on. Without a pairing in this
// browser's storage the app only knows its own address, which is reachable
// on the owner's network alone; on iOS the home-screen app and Safari keep
// separate storage, so a phone set up in one is not set up in the other.
async function unreachableNote(e) {
  if (T.relay) {
    const host = new URL(T.rec.relay).host;
    if (e instanceof RelayError) return `Could not reach Tacitus: ${e.message}`;
    if (!(e instanceof TypeError)) return ''; // the bot answered (a phone awaiting approval): the call said it
    // The call itself failed on the network. A plain GET on the relay tells
    // whether this phone reaches the relay at all, or only the call broke.
    if (S.cspBlocked) return `This copy of the app may not reach ${S.cspBlocked}. Open the app on your own network once to refresh it.`;
    const up = await fetch(`${T.rec.relay}/`, { mode: 'no-cors', cache: 'no-store', credentials: 'omit' }).then(() => true, () => false);
    return up
      ? `The away-from-home connection (${host}) answers, but the call to Tacitus failed (${e.message}). Trying again.`
      : `This phone cannot reach the away-from-home connection (${host}). Check that it is online (Wi-Fi or cellular), then try again.`;
  }
  if (!(await loadPairing())) {
    if (S.pairingError) return `This phone cannot read its saved setup (${S.pairingError}). Close the app fully and open it again.`;
    return 'Tacitus is reachable only on your own network from here. To use it anywhere, open this same app on that network '
      + 'and tap Settings in the menu, then Devices, then "Set up this phone".'
      + (phoneHere() && !standalone() ? ' The home-screen app and the browser each need their own setup.' : '');
  }
  return `Could not reach Tacitus: ${e.message}`;
}

// A call the page's own security policy refused: the note names it, since
// the browser reports such a call only as a failed load.
document.addEventListener('securitypolicyviolation', (e) => { S.cspBlocked = e.blockedURI || e.violatedDirective; });

async function boot() {
  if (S.halted) return;
  try {
    await pickTransport();
    S.me = await call('GET', '/me');
    flushDiag().catch(() => {}); // what this browser saw while it could not connect
    // A copy of the app from the relay runs the chat only once its code checks.
    if (relayCopy() && !(await checkBundle())) return;
  } catch (e) {
    // No bot behind this address and no pairing: the website's copy of the app, waiting for a code.
    const relays = !T.relay && e.status === 404 && !(await loadPairing()) && !S.pairingError ? await hostedRelays() : null;
    if (relays) return showSetup(relays);
    if ($('pair').hidden) {
      const why = await unreachableNote(e);
      if (why) toast(why);
      if (why && T.relay) relayNote(why); // the toast fades; the note stays while boot retries
      diagNote({ what: 'boot', why, relay: Boolean(T.relay), pairing: Boolean(await loadPairing()),
        pairingError: S.pairingError || '', idb: await idbProbe(), err: e.message });
    }
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
    toast(`Could not load the menu: ${e.message}`);
    return;
  }
  S.ready = true;
  S.queue.splice(0).forEach(([t, d]) => apply(t, d));
  renderChannels();
  route();
  if (S.push === undefined) { S.push = null; initPush().catch(() => {}); }
  // Adding a device happens where the API answers (the tailnet or this machine), and a computer from
  // the owner's own phone through the relay too, when the bot lets it (owner_routes, handoff).
  const routes = ownerRoutes(), shown = settingsShown();
  $('pair-phone').hidden = !(routes === 'handoff' ? Boolean(S.me.handoff) : routes === 'all' && Boolean(S.me.relay));
  $('go-notify').hidden = !S.me.push;
  $('notify-kinds').hidden = T.relay || !shown.notify; // the bot saves settings only from the owner's own copy
  $('go-routing').hidden = T.relay || !shown.routing;
  $('open-settings').hidden = false;
  footer();
  welcomeOnce();
  if (!T.relay) checkBundle().catch(() => {}); // the bot's own copy: checked meanwhile
  else if (!relayCopy()) { // the bot's own copy off the tailnet: a new release comes through the relay
    updateOverRelay().catch((e) => { diagNote({ what: 'update', err: e.message }); flushDiag().catch(() => {}); });
  }
}

async function init() {
  window.addEventListener('hashchange', () => S.halted || route()); // a stopped app renders nothing
  $('menu').addEventListener('click', () => document.body.classList.toggle('drawer'));
  $('scrim').addEventListener('click', () => document.body.classList.remove('drawer'));
  // A tap on the open channel changes no hash, so the drawer closes here.
  $('channels').addEventListener('click', (e) => {
    if (e.target.closest('a.chan')) document.body.classList.remove('drawer');
  });
  $('back').addEventListener('click', () => { location.hash = S.needsView ? hashFor(NEEDS) : hashFor(S.channel); });
  $('thread-state').addEventListener('click', () => setState(S.channel, S.thread));
  longPress($('thread-list'));
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
  navEdge();
  if (window.visualViewport) ['resize', 'scroll'].forEach((t) => visualViewport.addEventListener(t, fitViewport));
  // A fold or a rotation settles its sizes over a few hundred milliseconds: fit again after each.
  const refit = () => { fitViewport(); [250, 600].forEach((ms) => setTimeout(fitViewport, ms)); };
  ['resize', 'orientationchange', 'pageshow', 'focus'].forEach((t) => window.addEventListener(t, refit));
  ['focusin', 'focusout'].forEach((t) => document.addEventListener(t, () => setTimeout(fitViewport, 50)));
  // A window left at the keyboard's height after the field blurs (a home-screen
  // app on an older iOS): one forced layout brings it back, the thread's place kept.
  document.addEventListener('focusout', () => setTimeout(() => {
    const vv = window.visualViewport;
    if (editing() || full.w !== window.innerWidth || !full.h || window.innerHeight >= full.h - 4) return;
    if (vv && vv.height < window.innerHeight - 1) return; // the keyboard is still there
    const box = $('messages'), at = box.scrollTop;
    document.body.style.display = 'none';
    void document.body.offsetHeight;
    document.body.style.display = '';
    box.scrollTop = at;
    fitViewport();
  }, 600));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) fitViewport(); });
  fitViewport();
  // The thread holds its newest message in view while its pane shrinks (the keyboard opening).
  let stuck = true;
  $('messages').addEventListener('scroll', () => { stuck = nearBottom(); }, { passive: true });
  new ResizeObserver(() => { if (stuck && !S.halted) toBottom(); }).observe($('messages'));
  document.addEventListener('touchstart', () => {}, { passive: true }); // iOS applies :active only then
  $('phone-relay').addEventListener('click', () => pairPhone('relay'));
  ['here', 'phone', 'computer'].forEach((w) => $(`way-${w}`).addEventListener('click', () => showWay(w)));
  $('handoff-approve').addEventListener('click', () => phoneAct('approve', S.waiting));
  $('handoff-cards').addEventListener('change', (e) => { CARDS[e.target.checked ? 'add' : 'delete'](S.waiting.id); listPhones(); });
  $('setup-form').addEventListener('submit', setUp);
  settingsMenu();
  teaching();
  roomMenus();
  ownerActions();
  $('relay-note').addEventListener('click', () => relayNote(''));
  $('app-update-go').addEventListener('click', () => location.reload());
  // A phone back on the screen looks for a release made while it was away.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible' || Date.now() - S.lookedAt < LOOK_MS) return;
    S.lookedAt = Date.now();
    lookAgain().catch(() => {});
  });
  // Escape closes the drawer, unless it closes a dialog over it.
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !document.querySelector('dialog[open]')) document.body.classList.remove('drawer'); });
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
