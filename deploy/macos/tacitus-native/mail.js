// mail.js: the Mail.app half of tacitus-native (docs/macos-native-accounts.md).
//
// The helper runs it as `osascript -l JavaScript mail.js` with one request
// on stdin, {"verb": ..., "args": {...}}, and reads the answer from stdout:
// {"ok": true, "result": {...}} or {"ok": false, "error": {"code", "message"}}.
// The request and every value read from mail are data only: nothing here
// builds script source from them, and no verb runs a shell.
//
// Mail has no public framework; JavaScript for Automation drives its
// scripting dictionary through the Automation permission the helper holds.
// Status: written on Linux against the dictionary as documented, not yet
// run on a Mac. Lines marked VERIFY rely on dictionary behaviour that the
// Mac test list in the design document checks first.

ObjC.import('Foundation');

const Mail = Application('Mail');

function readStdin() {
  const data = $.NSFileHandle.fileHandleWithStandardInput.readDataToEndOfFile;
  return $.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding).js;
}

function fail(code, message) {
  const e = new Error(message);
  e.helperCode = code;
  return e;
}

function safe(read, fallback) {
  try {
    const value = read();
    return value === undefined || value === null ? fallback : value;
  } catch (e) {
    return fallback;
  }
}

function iso(date) {
  return date ? new Date(date).toISOString() : '';
}

function bare(messageId) {
  return String(messageId || '').trim().replace(/^</, '').replace(/>$/, '').trim();
}

// ── accounts and mailboxes ──────────────────────────────────────────

function account(name) {
  const hits = Mail.accounts.whose({name: name})();
  if (hits.length === 0) throw fail('not-found', 'no Mail account named ' + name);
  return hits[0];
}

function mailbox(acct, name) {
  // VERIFY: a nested mailbox is named by its path ("Parent/Child").
  const box = acct.mailboxes.byName(name);
  try {
    box.name();
  } catch (e) {
    throw fail('not-found', 'no mailbox ' + name + ' in ' + acct.name());
  }
  return box;
}

function boxesOf(list) {
  return (list || []).map(b => ({account: b.account, mailbox: b.mailbox,
                                 box: mailbox(account(b.account), b.mailbox)}));
}

function mailboxNames(box, prefix, depth, out) {
  safe(() => box.mailboxes(), []).forEach(child => {
    const name = prefix ? prefix + '/' + child.name() : child.name();
    out.push(name);
    if (depth > 0) mailboxNames(child, name, depth - 1, out);
  });
  return out;
}

// ── message records ─────────────────────────────────────────────────

function headerMap(raw) {
  // Unfold continuation lines, then keep the threading headers.
  const text = String(raw || '').replace(/\r?\n[ \t]+/g, ' ');
  const out = {'in-reply-to': '', 'references': ''};
  text.split(/\r?\n/).forEach(line => {
    const i = line.indexOf(':');
    if (i <= 0) return;
    const name = line.slice(0, i).trim().toLowerCase();
    if (name in out && !out[name]) out[name] = line.slice(i + 1).trim();
  });
  return out;
}

function address(r) {
  return {name: safe(() => r.name(), ''), address: safe(() => r.address(), '')};
}

function record(m, acctName, boxName, full) {
  const rec = {
    account: acctName,
    mailbox: boxName,
    local_id: safe(() => m.id(), 0),
    message_id: bare(safe(() => m.messageId(), '')),
    subject: safe(() => m.subject(), ''),
    sender: safe(() => m.sender(), ''),
    to: safe(() => m.toRecipients().map(address), []),
    cc: safe(() => m.ccRecipients().map(address), []),
    date_received: iso(safe(() => m.dateReceived(), null)),
    date_sent: iso(safe(() => m.dateSent(), null)),
    read: safe(() => m.readStatus(), false),
    flagged: safe(() => m.flaggedStatus(), false),
    headers: headerMap(safe(() => m.allHeaders(), '')),
    attachments: safe(() => m.mailAttachments().map(a => ({
      name: safe(() => a.name(), ''),
      mime: safe(() => a.mimeType(), ''),
      size: safe(() => a.fileSize(), 0),
      downloaded: safe(() => a.downloaded(), false),
    })), []),
  };
  if (full) rec.content = String(safe(() => m.content(), ''));
  return rec;
}

// The newest `top` of a collection. Ids and dates come in one Apple event
// each for the whole collection; only the kept messages are read one by one.
function newest(collection, top) {
  const ids = safe(() => collection.id(), []);
  const dates = safe(() => collection.dateReceived(), []);
  const order = ids.map((id, i) => ({id: id, t: dates[i] ? new Date(dates[i]).getTime() : 0}))
    .sort((a, b) => b.t - a.t);
  const kept = top ? order.slice(0, top) : order;
  return {ids: kept.map(o => o.id), more: top ? order.length > top : false};
}

function since(args) {
  return args.since ? new Date(args.since) : null;
}

function byIds(box, ids) {
  // VERIFY: Mail's message element takes the by-id reference form.
  return ids.map(id => box.messages.byId(id));
}

// ── verbs ───────────────────────────────────────────────────────────

function mailStatus(args) {
  const running = Mail.running();
  if (!running) return {running: false, accounts: []};
  return {
    running: true,
    accounts: Mail.accounts().map(a => ({
      name: a.name(),
      enabled: safe(() => a.enabled(), true),
      addresses: safe(() => a.emailAddresses(), []),
      kind: String(safe(() => a.accountType(), 'unknown')),
    })),
  };
}

function listMailboxes(args) {
  return {mailboxes: mailboxNames(account(args.account), '', 3, []).sort()};
}

function listMessages(args) {
  const acct = account(args.account);
  const box = mailbox(acct, args.mailbox);
  const from = since(args);
  const collection = from ? box.messages.whose({dateReceived: {_greaterThan: from}}) : box.messages;
  const pick = newest(collection, args.top || 0);
  // The body costs one read per message, so it comes only when asked for.
  const withBody = !!args.preview;
  return {
    messages: byIds(box, pick.ids).map(m => record(m, args.account, args.mailbox, withBody)),
    has_more: pick.more,
  };
}

function searchMessages(args) {
  const q = String(args.query || '');
  if (!q) throw fail('bad-request', 'search-messages needs a query');
  const from = since(args);
  const hits = [];
  boxesOf(args.mailboxes).forEach(b => {
    const any = [{subject: {_contains: q}}, {sender: {_contains: q}}];
    if (args.body) any.push({content: {_contains: q}});
    let test = {_or: any};
    if (from) test = {_and: [{dateReceived: {_greaterThan: from}}, test]};
    const collection = b.box.messages.whose(test);
    const ids = safe(() => collection.id(), []);
    const dates = safe(() => collection.dateReceived(), []);
    ids.forEach((id, i) => hits.push({b: b, id: id, t: dates[i] ? new Date(dates[i]).getTime() : 0}));
  });
  hits.sort((x, y) => y.t - x.t);
  const top = args.top || 25;
  return {
    messages: hits.slice(0, top).map(h =>
      record(h.b.box.messages.byId(h.id), h.b.account, h.b.mailbox, !!args.preview)),
    has_more: hits.length > top,
  };
}

function findIn(list, messageId) {
  const id = bare(messageId);
  for (const b of boxesOf(list)) {
    const found = b.box.messages.whose({messageId: id})();
    if (found.length > 0) return {b: b, m: found[0]};
  }
  return null;
}

function getMessage(args) {
  const hit = findIn(args.mailboxes, args.message_id);
  if (!hit) throw fail('not-found', 'no mailbox named holds that message');
  return {message: record(hit.m, hit.b.account, hit.b.mailbox, args.full !== false)};
}

// Every message whose headers name the thread's root Message-ID (its own
// Message-ID, In-Reply-To or References). The Python side keeps the ones
// whose own root is that id.
function findThread(args) {
  const root = bare(args.root);
  if (!root) throw fail('bad-request', 'find-thread needs a root');
  const from = since(args);
  const out = [];
  boxesOf(args.mailboxes).forEach(b => {
    let test = {allHeaders: {_contains: root}};
    if (from) test = {_and: [{dateReceived: {_greaterThan: from}}, test]};
    b.box.messages.whose(test)().forEach(m => out.push(record(m, b.account, b.mailbox, true)));
  });
  return {messages: out};
}

function quoteOf(orig) {
  const who = safe(() => orig.sender(), '');
  const when = iso(safe(() => orig.dateReceived(), null));
  const lines = String(safe(() => orig.content(), '')).split(/\r?\n/).map(l => '> ' + l);
  return 'On ' + when + ', ' + who + ' wrote:\n' + lines.join('\n');
}

function setRecipients(msg, kind, list) {
  // VERIFY: deleting the recipients a reply starts with, then adding ours.
  const current = kind === 'to' ? safe(() => msg.toRecipients(), []) : safe(() => msg.ccRecipients(), []);
  current.forEach(r => Mail.delete(r));
  (list || []).forEach(r => {
    const props = {address: r.address, name: r.name || ''};
    if (kind === 'to') msg.toRecipients.push(Mail.ToRecipient(props));
    else msg.ccRecipients.push(Mail.CcRecipient(props));
  });
}

// An outgoing message with the request's content. A reply is made with
// Mail's own reply command, so it carries In-Reply-To and References; its
// recipients and body are then set to exactly what the EA's draft says.
function compose(args) {
  let msg;
  if (args.reply_to) {
    const hit = findIn([args.reply_to], args.reply_to.message_id);
    if (!hit) throw fail('not-found', 'the message this replies to is gone');
    // VERIFY: reply returns the outgoing message with openingWindow false,
    // and its content can be set.
    msg = Mail.reply(hit.m, {openingWindow: false, replyToAll: !!args.reply_all});
    msg.content = String(args.body || '') + '\n\n' + quoteOf(hit.m);
  } else {
    msg = Mail.OutgoingMessage({subject: String(args.subject || ''),
                                content: String(args.body || ''), visible: false});
    Mail.outgoingMessages.push(msg);
  }
  if (args.reply_to && args.subject) msg.subject = String(args.subject);
  if (args.sender) msg.sender = String(args.sender);
  setRecipients(msg, 'to', args.to);
  setRecipients(msg, 'cc', args.cc);
  (args.attachments || []).forEach(path => {
    // VERIFY: the attachment element of an outgoing message's content.
    msg.content.attachments.push(Mail.Attachment({fileName: Path(path)}));
  });
  return msg;
}

function saveDraft(args) {
  const acct = account(args.account);
  const drafts = mailbox(acct, args.drafts_mailbox);
  const before = newest(drafts.messages, 0).ids.reduce((a, id) => Math.max(a, id), 0);
  const msg = compose(args);
  const subject = String(safe(() => msg.subject(), args.subject || ''));
  // VERIFY: save puts an outgoing message in the account's Drafts.
  try {
    msg.save();
    safe(() => msg.close({saving: 'no'}), null);
  } catch (e) {
    msg.close({saving: 'yes'});
  }
  // The saved draft is the newest message in Drafts with this subject that
  // was not there before. Mail writes it to the server in the background.
  for (let i = 0; i < 20; i++) {
    const found = drafts.messages.whose({subject: subject})()
      .filter(m => safe(() => m.id(), 0) > before);
    if (found.length > 0) {
      const latest = found.reduce((a, m) => (m.id() > a.id() ? m : a));
      return {message_id: bare(latest.messageId())};
    }
    delay(0.5);
  }
  throw fail('internal', 'Mail saved the draft but it did not appear in ' + args.drafts_mailbox);
}

function deleteDraft(args) {
  // Only the account's Drafts mailbox is searched, so nothing else is deleted.
  const drafts = mailbox(account(args.account), args.drafts_mailbox);
  const found = drafts.messages.whose({messageId: bare(args.message_id)})();
  found.forEach(m => Mail.delete(m));
  return {deleted: found.length};
}

function sendMessage(args) {
  const msg = compose(args);
  return {sent: msg.send() !== false};
}

function moveMessage(args) {
  const acct = account(args.account);
  const from = mailbox(acct, args.mailbox);
  const to = mailbox(acct, args.to_mailbox);
  const found = from.messages.whose({messageId: bare(args.message_id)})();
  if (found.length === 0) throw fail('not-found', 'no such message in ' + args.mailbox);
  // VERIFY: move takes the destination mailbox as `to`.
  found.forEach(m => Mail.move(m, {to: to}));
  return {moved: true};
}

function setFlags(args) {
  const box = mailbox(account(args.account), args.mailbox);
  const found = box.messages.whose({messageId: bare(args.message_id)})();
  if (found.length === 0) throw fail('not-found', 'no such message in ' + args.mailbox);
  found.forEach(m => {
    if ('read' in args) m.readStatus = !!args.read;
    if ('flagged' in args) m.flaggedStatus = !!args.flagged;
  });
  return {ok: true};
}

const VERBS = {
  'mail-status': mailStatus,
  'list-mailboxes': listMailboxes,
  'list-messages': listMessages,
  'search-messages': searchMessages,
  'get-message': getMessage,
  'find-thread': findThread,
  'save-draft': saveDraft,
  'delete-draft': deleteDraft,
  'send-message': sendMessage,
  'move-message': moveMessage,
  'set-flags': setFlags,
};

function run(argv) {
  let request;
  try {
    request = JSON.parse(readStdin());
  } catch (e) {
    return JSON.stringify({ok: false, error: {code: 'bad-request', message: 'the request is not JSON'}});
  }
  const verb = VERBS[request.verb];
  if (!verb) {
    return JSON.stringify({ok: false, error: {code: 'unknown-verb', message: 'unknown verb ' + request.verb}});
  }
  try {
    if (request.verb !== 'mail-status' && !Mail.running()) {
      throw fail('mail-not-running', 'Mail is not running');
    }
    return JSON.stringify({ok: true, result: verb(request.args || {})});
  } catch (e) {
    let code = e.helperCode || 'internal';
    if (e.errorNumber === -1743) code = 'permission-denied';
    return JSON.stringify({ok: false, error: {code: code, message: String(e.message || e)}});
  }
}
