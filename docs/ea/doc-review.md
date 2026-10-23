### Working documents for review (doc_review.py)

The user reviews working documents as GitHub pull requests in a private
repository, `doc_review.repo` in `~/.ea/config/github.json`. You write there
as the bot account (`bot_login`), from your own clone at
`/home/ea-bot/doc-review`. The user comments, suggests wording and merges.
The merged file is the finished Markdown that the user shares. The tool is
`python3 /home/ea-bot/ea-system/preprocessor/doc_review.py`; every verb
prints one JSON object. Use it for every step; raw git is not allowed. The
token reaches this one repository only. Only threads and reviews by
`doc_review.owner_login` count as the user's.

**What goes there.** Every working document that you write for the user to
read and revise: outlines, plans, memos, proposal sections, talk scripts,
reports. A short answer in the conversation does not.

**What never goes there.** GitHub is a third-party service. Keep confidential
material as a local file and in the Zulip loop: reviews of manuscripts or
proposals, panel material, letters of recommendation, tenure and personnel
letters, student records, and anything under a confidentiality rule or an NDA.
When you are not sure, keep it local and ask.

**Open a review.**
1. Write the draft to `~/.ea/docs/<project>/<name>.md`, in the user's voice
   (drafting-voice.md).
2. `doc_review.py open --file <that file> --title "<title>" --thread <this
   thread's key> --project <id> --purge`. It commits the file on its own
   branch, one sentence per line, pushes it, opens the pull request, reads the
   pushed copy back, and returns `pr`, `url` and `verified`.
3. With `--purge`, it deletes the local draft when `verified` is true, so the
   pull request is the only copy. Without it, `purge --pr <n> --file <that
   file>` does the same later; it refuses when the text is different.
4. Post the link in the thread, with one line: what the document is. Tell the
   user that any line can take a comment, and that Submit review sends a
   batch when it is ready.
5. Record it: `ledger.py update --id <id> --last-action "<date>: opened PR #<n>
   <url> for review"`, and a `project_store.py journal` line.

**While it is under review.** The pull request holds the current text. The
user can also edit it on GitHub. Before every change: `fetch --pr <n> --out
/home/ea-bot/.ea/state/doc-review-<n>.md`, edit that copy, then `update --pr
<n> --file <copy> --message "<what changed>"`. Never edit from an old local
copy. `update` never forces a push: when the branch moved after your fetch, it
stops, and you fetch again and redo the change. A change that the user asks
for in Zulip goes the same way.

**Working through a review.** The bot tells you when the user submits a
review.
1. `review --pr <n>` gives the open threads. Each thread has `anchor` (the
   text it is attached to), `comment`, `suggestion` (the exact replacement
   text for the anchored lines, or null) and `replies`. It also gives
   `review_notes` (the user's note for the whole review) and `pr_comments`.
2. Fetch the file, then make every change. Apply a suggestion word for word,
   in place of the anchored lines. An `outdated` thread was left on text that
   changed after the comment; find the passage from its anchor.
3. Use one `update` for the whole review.
4. For each thread that you handled: `reply --pr <n> --comment-id
   <comment_id> --body "<one line: what changed>"`, then `resolve --pr <n>
   --thread-id <thread_id>`. When you cannot tell what the user wants, reply
   with your question and leave the thread open.
5. Reply in the Zulip thread: what changed, and which threads are still open.

**Rules.**
- Never merge, never push to `main`, and never force a push. Merging is
  the user's step, and it means that the document is final. The bot posts in
  the thread when the user merges.
- Your replies on GitHub are in the assistant voice (Simplified Technical
  English). The document itself is in the user's voice.
- One pull request per document. `fetch --pr <n>` reads a merged document
  too, at its merge commit; the branch is gone after the merge. To review a
  merged document again, open a new review with another `--slug`.
- `status` lists the open reviews, with the threads left and the last review.
- "github.json names no doc_review.repo" (or no `doc_review.owner_login`):
  the config is incomplete. Tell the user; SETUP.md has the keys.
