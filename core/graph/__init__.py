"""Knowledge graph layer.

Two pieces in T0:

* :mod:`core.graph.store` — basic CRUD on entities and relationships.
* :mod:`core.graph.linker` — always-on seed-entity name/alias matching.

Future patches add the LLM-extraction pipeline (``core.graph.extractor``)
and the audit-trail wrapper (``core.graph.audit``) per plan v4.
"""

from __future__ import annotations
