# Security and privacy

This is a local development library, not a hosted authentication boundary.
`user_id`/`owner` separates records but is trusted input from your application.
Authenticate users and control database access before deploying a service. Prefer
separate database files for unrelated users. Do not expose the loopback HTTP or settings ports publicly.

Database, transcripts, provider receipts and optional context captures are private
but not application-encrypted. Memory policy `off` disables new durable facts; it
does not disable conversation storage. Fact deletion preserves original messages.
Cloud providers receive the context sent for inference. SDK settings are not saved
automatically; `Settings.from_env()` explicitly opts into local/environment settings.

Never put secrets in a persona, message, issue or debug capture. Use environment
configuration or the local provider settings UI. Keep private launch tokens and
generated host bundles off public repositories.

For a suspected vulnerability, [submit a private security report](https://github.com/spearsheep/anima-brain/security/advisories/new).
Private vulnerability reporting is enabled for this repository. Do not post exploit
details, credentials or user data in a public issue.
No security-response SLA or external security audit is claimed for this alpha release.
