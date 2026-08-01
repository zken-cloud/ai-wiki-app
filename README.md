# ai-wiki-app

Pipeline that builds [zken-cloud/ai-wiki](https://github.com/zken-cloud/ai-wiki).
Runs daily in GitHub Actions, writes Markdown into the wiki repo, and pushes.

```
collect ──► dedupe ──► triage ──► summarise ──► render ──► commit to ai-wiki
(4 source   (seen     (keyword +  (Gemini      (Markdown
 types)      ledger)   Gemini)     Flash)       + JSON)
```

## Why it is shaped this way

- **Content lives in a separate public repo.** The wiki stays readable Markdown
  you can browse on GitHub or move elsewhere; this repo holds the machinery.
- **Cost is controlled by ordering the funnel cheapest-first.** A free keyword
  prefilter and the Hugging Face curated-bypass run before any paid call, so
  the expensive per-item summaries only ever see items that already qualified.
- **One config file is the extension point.** Adding sources or whole
  categories should never require touching Python.

## Extending it

| You want to… | Do this |
|---|---|
| Add a feed to an existing category | Add an entry under that category's `sources:` in `sources.yaml` |
| Add a whole new category | Copy a `categories:` block in `sources.yaml`, then `mkdir wiki/docs/<id>` and add it to `nav:` in `wiki/mkdocs.yml` |
| Tune what gets through | Edit that category's `filter.min_score` / `keywords` / `max_items` |
| Change how items are judged | Edit that category's `criteria:` — it is passed verbatim to the scoring model |
| Support a new *kind* of source | Add a function to `pipeline/collect.py` and register it in `ADAPTERS` |

### Source types

| Type | Use for | Notes |
|---|---|---|
| `hf_daily` | Hugging Face curated daily papers | Carries community upvotes, used as a free relevance signal |
| `arxiv` | arXiv Atom API, per category | Recall tier — catches what did not trend |
| `rss` | Any RSS/Atom feed | Parsed with `feedparser`, tolerant of malformed feeds |
| `sitemap` | Publishers with **no** feed | Reads `sitemap.xml`, fetches pages. This is how Anthropic is ingested — they publish no RSS |

## Local use

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
gcloud auth application-default login          # local credentials

.venv/bin/python run.py --check                # verify Vertex access
.venv/bin/python run.py --wiki ../ai-wiki --dry-run       # no writes, no summaries
.venv/bin/python run.py --wiki ../ai-wiki --only papers   # one category
.venv/bin/python run.py --wiki ../ai-wiki                 # full run
```

`--dry-run` still calls the (cheap) scoring model so you can see what *would*
be selected; it skips summaries, the digest, and all file writes.

## CI setup

1. Have a project admin run `scripts/setup-wif.sh`. It provisions keyless
   Workload Identity Federation — no service-account key is ever stored.
2. Set the repo variables it prints (`GCP_WIF_PROVIDER`, `GCP_SERVICE_ACCOUNT`,
   `GCP_PROJECT`).
3. Set one secret, `WIKI_DEPLOY_KEY`: the private half of an SSH deploy key
   whose public half is registered **write-enabled on `zken-cloud/ai-wiki`**.
   Cross-repo pushes have no OIDC equivalent, so this is the one stored
   credential — a deploy key is used rather than a PAT because it grants write
   to exactly one repo and has no reach over the account.

   To rotate it:
   ```bash
   ssh-keygen -t ed25519 -N "" -C "ai-wiki-app CI" -f /tmp/k
   gh api -X POST repos/zken-cloud/ai-wiki/keys -f title="ai-wiki-app CI" \
     -f key="$(cat /tmp/k.pub)" -F read_only=false
   gh secret set WIKI_DEPLOY_KEY -R zken-cloud/ai-wiki-app < /tmp/k
   rm /tmp/k /tmp/k.pub          # then delete the old key from the repo's Deploy keys
   ```

## Operational notes

- **Dedupe ledger** lives at `ai-wiki/data/seen.json`, committed with the
  content it describes (Actions runners are stateless). Entries older than 120
  days are pruned automatically.
- **`window_days: 7`**, not 3. Several high-value publishers (NCSC, Google
  Security, Meta Engineering) post weekly; a 3-day window returned zero items
  from them. The ledger makes a wide window safe — nothing publishes twice.
- **Thinking is disabled** (`thinking_budget=0`) on the triage and summary
  calls. Gemini 2.5 bills thinking against `maxOutputTokens`, which silently
  truncated structured JSON mid-string until this was set.
- **A failing source never fails the run.** Each adapter is wrapped; a dead
  feed logs an error and the run continues.
- **CISA is deliberately excluded.** Its WAF rejects any non-browser
  User-Agent, and its content is overwhelmingly ICS advisories with no AI
  dimension — not worth spoofing a browser for.

## Cost

Per day, roughly: ~15 batched triage calls on Flash-Lite, ~65 summaries on
Flash, one digest on Pro. That is a few hundred thousand tokens/day, dominated
by cheap models. GitHub Actions and Pages are free at this volume.
