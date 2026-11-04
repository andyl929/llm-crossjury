# Manual scripts

Only run these by hand. pytest never collects this directory (`norecursedirs` in
`pyproject.toml`), and CI never runs it.

Scripts in this directory **may make real, billed network calls**. Every such script must:

1. require an explicit opt-in flag (`--live`),
2. print the cost estimate and ask for confirmation before the first call,
3. respect the configured budget,
4. never print API keys or credential paths.

| Script | Purpose |
|---|---|
| `smoke_live.py` | One tiny real call per configured provider, to confirm wiring (placeholder) |
