# Security policy

llm-crossjury runs on your own computer and handles API keys and signed-in vendor CLIs,
so security reports are very welcome.

## Reporting a vulnerability

Please report privately through GitHub: open the **Security** tab of this repository and
choose **Report a vulnerability**. Do not open a public issue for a security problem.

Include what you found, how to reproduce it, and what an attacker could do with it. You
should get a first reply within 7 days. Fixed issues are credited in the release notes
unless you prefer otherwise.

## Supported versions

The project is pre-1.0: only the latest commit on `main` (and the latest release, once
there are releases) receives fixes.

## Scope

Most relevant are problems that let someone else:

- read or exfiltrate an API key, or a vendor CLI's sign-in;
- reach the local web UI from another machine or another website (the server binds to
  loopback, checks the `Host` header and sends a strict Content Security Policy);
- make the app run a command other than the fixed, allowlisted CLI invocations, or pass
  untrusted text to a shell;
- spend money past the budget limits without a confirmation.

How keys and CLIs are handled is described in
[ADR 0002](docs/adr/0002-cli-backend-security-boundary.md) and
[ADR 0003](docs/adr/0003-cost-control.md).

Also in scope: a question, attachment or model answer that makes a vendor CLI act
outside the restrictions in ADR 0002 (for example, reach files outside its empty working
directory). Out of scope: the content of what the vendors' models say, and attacks that
need someone who already controls your user account.
