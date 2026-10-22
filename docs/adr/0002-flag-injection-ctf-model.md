# ADR 0002: Arena-injected flags
Status: accepted. Authors write programs that print `os.environ["FLAG"]` on the right input. The arena owns the flag, so authors cannot leak it in descriptions and the same challenge source is safe to show the attacker.
