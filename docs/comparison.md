# Comparison

aurochs-recall sits in the **memory-tooling category** — software that
gives a user persistent, queryable memory across AI conversation
sessions. The category divides along a few axes that matter:

**Storage shape and reversibility.** Some tools paraphrase or summarize
your conversations into compressed "memories" and discard the rest.
Others store the verbatim text. Once a tool paraphrases, the original
phrasing is gone — and so is the citation surface. aurochs-recall
stores drawers verbatim and keeps the index, the graph, and the
access log layered on top of that immutable substrate. The drawers
are append-only; everything else is amend-and-version. If you don't
like an extraction run, you discard the run; the drawers don't move.

**Retrieval architecture.** Most tools in the category are pure
embeddings + vector search, sometimes with a reranker bolted on.
aurochs-recall ships a four-layer architecture: SQLite FTS5 (BM25)
as the primary index, dense embeddings for hybrid mode, an explicit
knowledge graph with entity-relationship semantics for connected
queries, and an access log so retrieval patterns themselves become
queryable. BM25 doesn't go out of fashion when the embedding model
changes; the cross-encoder rerank stage means hybrid recall isn't
held hostage to any one model's training distribution.

**Where the data lives.** Cloud-backed memory tools require trust in
a third-party operator and trust in their security posture. aurochs-recall
is a Python package that writes a SQLite file to your local disk. There
is no cloud component, no account, no API at any domain we control.
The bench at `bench/README.md` is published with full methodology so
you can verify the retrieval claims on your own corpus rather than
trusting our numbers. That's the entire stance: verify, back up,
restore — on your machine, with files you can read using standard
tools.
