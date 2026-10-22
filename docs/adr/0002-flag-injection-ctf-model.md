# ADR 0002: Arena-injected flags
Status: accepted. Authors write programs that print the global `FLAG` on the right input. A small runner reads the flag and source from a stdin header, so the flag never appears on any argv or in `/proc/<pid>/environ`. The arena owns the flag, so authors cannot leak it in descriptions and the same challenge source is safe to show the attacker.
