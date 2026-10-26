# Skill: Deck Builder
<!-- scopes: decks, drafting -->

Build a slide deck the way the user builds their own: a Slidev project on
the theme `deck.theme` in `ea-config.json` names (Slidev's built-in
theme when it is `default`; otherwise the user's own package at
`shared/theme-<name>`), written to their playbooks, staged on their
tailnet, with a PDF. `deck_tools.py new` wires the theme in; never add
a theme by hand. Do-and-tell: build it, stage it, post the URL and the PDF
into the thread. Ask only about a decision the playbooks do not settle.

The deck lives in the EA's own workspace, `/home/ea-bot/presentations/`,
under `decks/<slug>/`. The user's own presentations tree is mounted
read-only at `/srv/ea/presentations/` (theme, playbooks, scripts, the
user's finished decks) and the figure pool at `/srv/ea/presentation-library/`.
You cannot write there, and you do not need to.

Every step is one command:

```bash
python3 /home/ea-bot/ea-system/preprocessor/deck_tools.py <verb> ...
```

## Read first, every time

1. `/srv/ea/presentations/playbooks/common.md`: the register rules, the
   figure rules, the pipeline. Binding.
2. The genre playbook: `briefing.md` for an update to a dean, chair,
   committee, sponsor or department meeting (the default for "quick
   update deck"); `talk.md` for a research talk; `lecture.md` for a class.
3. One worked exemplar of the genre: the user's most recent deck of
   that genre under `/srv/ea/presentations/decks/`, its `slides.md` and
   its `SOURCE.md`.

The rules that reach the user most often, restated once: no em-dashes
(`—`, `–`, ` -- `); no slogans, pivots ("X is not Y, it is Z"), meta
framings or forward references; titles are 1-4 word noun phrases; every
number is real and sourced; a briefing has no agenda slide, no section
dividers, no summary slide, and few or no reveals. The linter enforces
the mechanical part; you enforce the rest.

## Where the facts come from

A project room's specialist already holds the project pack. Use it, the
ledger (`ledger.py list --status open`), the journal and the filed mail
(`project_store.py pack --project <id>`) before any new search. Every
number on a slide gets a source line in the slide's notes (`SOURCES:`
block) and a row in the deck's `SOURCE.md`. A number you cannot source
stays off the canvas; say so in SOURCE.md. Never invent a figure or a
date. When two sources disagree, leave the number off and record the
disagreement.

## Steps

1. **Scope in one line.** Room, occasion, length. "Quick update deck"
   means the briefing genre, 3 to 6 slides, tables over prose, no
   reveals. If the message names none of these, take the project's
   obvious venue and say what you assumed in the reply; do not stop to
   ask.

2. **Scaffold.** The slug is `<project>-<topic>-<YYYY-MM>`, lowercase
   kebab-case.
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/deck_tools.py new \
     --slug <slug> --title "<Title>" --venue "<Room>" --date "<Month D, YYYY>" \
     [--subtitle "..."] [--description "one line for the cover info"] [--genre briefing]
   ```
   It writes `slides.md`, `SOURCE.md`, `style.css`, the theme wiring and
   the deck's `build_deck.sh`, and installs the toolchain. Never create a
   deck by hand.

3. **Write `slides.md`** with Write/Edit, cover first, then one slide per
   topic and one per decision. Speaker notes per slide are
   `POINT / ARC / SAY / SOURCES`. Record deliberate exceptions on the
   slide (`<!-- lint-ok: no-figure, no-reveal -->`), and fill
   `SOURCE.md` as you go.

4. **Lint until clean.**
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/deck_tools.py lint --slug <slug>
   ```

5. **Build and stage.**
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/deck_tools.py build --slug <slug>
   ```
   This runs the deck's `build_deck.sh`: static build, local server,
   tailnet route, PDF. The JSON line at the end carries `url` and `pdf`.
   The route is created by the mediator; a refusal names the rule.
   The PDF page follows the headmatter: `aspectRatio` sets its shape, and
   a poster adds `printWidthIn` (page width in inches) and `printDpi`
   (240 for print) so the page is the print size with no letterbox.

6. **Render and look.** Mandatory. "Builds" is not "looks right".
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/deck_tools.py render --slug <slug>
   ```
   Then `Read` every PNG it lists. Check text running past the bottom of
   the canvas (1280 px wide; the height follows the deck's `aspectRatio`,
   720 px for 16/9), a table colliding with a figure, raw LaTeX, an
   empty half canvas, a title that overstates its slide. Fix, lint,
   build again, render again. Two passes are normal.

7. **Hand back.** One short chat message: what the deck is, the slide
   count, the tailnet URL, one line on what you assumed. Attach the PDF
   into the thread:
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/slack_upload.py \
     --channel <channel id> --thread-ts <thread ts> \
     --file /home/ea-bot/presentations/decks/<slug>/public/<slug>.pdf
   ```
   Journal it: `project_store.py journal --project <id> --kind milestone
   --text "Deck <slug> staged: <url>"`. Say that the link works from the user's
   tailscale devices only, and that the PDF is the file to forward.

8. **Revisions** come back in the same thread as numbered slide notes.
   Edit the same `slides.md`, lint, build, render. Never scaffold a second
   deck for a revision; the URL stays the same.

9. **Teardown** only when asked ("take the deck down"):
   ```bash
   python3 /home/ea-bot/ea-system/preprocessor/deck_tools.py off --slug <slug>
   ```

## Figures

Briefings are usually tables; a text-only deck is legitimate and is
recorded per slide with `<!-- lint-ok: no-figure -->`. When a figure is
right, the pool comes first:

```bash
python3 /home/ea-bot/ea-system/preprocessor/deck_tools.py figures <terms> \
  --deck-glob '<Lastname>|<lab>|<project>' [--since 2022] [--min-width 1200] \
  [--copy-to decks/<slug>/public/img/]
```

Own plots go in `figures-src/make_<name>.py` (matplotlib, values from a
results JSON) and are regenerated with `build --figures`. Never redraw a
published figure; never invent a data point.

A photo or logo from the web: read its page with `fetch_url.py --url`,
take the image URL from `images`, then
`fetch_url.py --url <image URL> --save-image /home/ea-bot/presentations/decks/<slug>/public/img/<name>.jpg`
(docs/ea/web-reading.md). Put its credit line on the slide and its source
URL and license in SOURCE.md.

## Rails

- Tailnet only. The URL is reachable from the user's tailscale devices
  and nowhere else. Never call it public, and never promise a public
  link; the PDF is how a deck leaves the tailnet.
- Funnel does not exist for you. The mediator executes `tailscale serve`
  for one path and one local port and nothing else; asking for Funnel is
  refused on both sides. If the user wants a deck public, tell them it is
  their `shared/scripts/funnel-toggle.sh`, run by them.
- Do not call `tailscale`, `node`, `npm` or `build_deck.sh` yourself.
  `deck_tools.py` runs them with the right environment.
- `/srv/ea/...` is read-only. Copy an exemplar's pattern, never its
  content.
- One deck per request; one `new` per deck; revisions edit in place.
- The user's voice rules for drafted content apply to the canvas and the
  notes. Assistant-voice rules (STE) apply to your chat reply, not to
  the slides.
