# FAQ

## Why not just use ChatGPT's "Memory" feature?

Different category. ChatGPT's memory is an opaque per-account
side-channel that the model writes summaries into when it decides
to. aurochs-recall stores **your** verbatim text in **your** local
SQLite file, and indexes it so you can query it. There's no
overlap with the cloud-vendor memory features.

## Does it work with claude.ai exports?

Yes. Run `recall init`, point it at your unpacked claude.ai export
zip, and `recall ingest`. Each conversation becomes a thread; each
message becomes a drawer.

## Does it work with ChatGPT exports?

Yes. Same flow. The mapping tree in `conversations.json` is
traversed depth-first for deterministic message order.

## Can I use it without running an LLM extraction step?

Yes. The seed-list linker runs without any LLM provider configured
and is enough for a useful entity graph. LLM extraction is opt-in
via `recall extract --provider <yours>` and gated on you supplying
your own API key.

## Why SQLite instead of a vector DB?

The primary index is BM25 (FTS5), not vector. SQLite handles BM25
well, has no operational overhead, and ships as a stdlib module. For
hybrid retrieval, the `[chroma]` extra adds a vector store
alongside, but the FTS5 index is the spine.

## Will it slow down as my corpus grows?

FTS5 scales well into the millions of rows for the query patterns
this package issues. The `index_state` table tracks per-file ingest
state so re-ingests are incremental. If you hit a wall, please open
an issue with your corpus size and query times — that's the kind of
data point that drives the next round of perf work.

## What about multilingual support?

The default cross-encoder is English MS-MARCO trained. The
`[multilingual]` extra installs BGE-M3 for embeddings and a
multilingual MiniLM cross-encoder for rerank. The FTS5 tokenizer is
`unicode61 remove_diacritics 2`, which handles Latin-script European
languages and a fair chunk of CJK at the BM25 layer without further
config.

## Will you support cloud sync?

No, by design. Cloud sync would require an operator-trusted
component, which is exactly what aurochs-recall is built to avoid.
If you need cross-machine sync, point `recall backup` at a directory
your existing backup system already covers (Syncthing, rsync,
Dropbox, whatever) and `recall restore` from it on the other side.

## What's the bench actually testing?

A reproduction of LongMemEval against the public corpus, plus a
template you fill in with your own corpus. Methodology is in
`bench/README.md`. Numbers without methodology are vibes.

## Where do I report a bug?

GitHub issues at
[https://github.com/skovalik/aurochs-recall/issues](https://github.com/skovalik/aurochs-recall/issues).

If it's a security-sensitive bug, email <stefan@aurochs.agency>
directly with `aurochs-recall security` in the subject.
