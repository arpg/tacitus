## Web page reading

When emails contain URLs to external programs, events, opportunities, or
documents, fetch and read the page content:

```bash
python3 /home/ea-bot/ea-system/preprocessor/fetch_url.py --url "<URL>" [--max-chars 4000] [--js]
```

Returns JSON with page title, meta description, the extracted main text,
the action links, and `images` (the page's share image first, then its
`<img>` sources with alt text). Auto-falls back to JS rendering
(Playwright) if the page is empty.

**Saving an image** (a photo for a deck, a logo):

```bash
python3 /home/ea-bot/ea-system/preprocessor/fetch_url.py --url "<image URL>" --save-image <folder>/<name>.jpg
```

It saves only a PNG, JPEG, GIF or WebP, to a new file in a deck's folder
(`/home/ea-bot/presentations/decks/<slug>/public/img/`) or `~/.ea/state`;
the suffix follows the image's real type, and the JSON says the path it
saved. A photo page is not the image: read the page first and take the
URL from its `images`. Note the source and its license or credit line as
you save it; a deck slide carries the credit.

A scheduled (cron) run has no WebFetch or WebSearch: `fetch_url.py` is its
only web reader. Every request either way is logged and reviewed daily.

**When to fetch:**
- Opportunity/program emails with a link to eligibility, deadlines, or requirements
- Scheduling emails linking to event details (dates, location, agenda)
- Forwarded announcements where the body says "check this out" with a URL
- LoR requests linking to a submission portal
- The user asks "what is this?" or "should I do this?" about a URL

**When NOT to fetch:**
- Generic footer links, unsubscribe links, social media links
- URLs already fetched in this run
- When the email body already contains sufficient detail

**Token budget:** Default max is 4000 chars (~1000 tokens). For quick
checks (just need a date or deadline), use `--max-chars 1500`.
