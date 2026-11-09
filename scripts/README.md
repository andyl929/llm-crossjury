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
| `smoke_cli_image.py` | One tiny `codex` CLI call with a generated 32x32 solid-colour PNG, to confirm native image input (subscription; cost unknown) |
| `capture_readme_media.py` | Regenerate the README media in `docs/assets/` (hero GIF, light and dark screenshots) from `llm-crossjury ui --demo` in headless Edge or Chrome. Free and offline (loopback only), so it needs no `--live` flag. Run it after every UI change; the selectors it relies on are listed in its docstring |
