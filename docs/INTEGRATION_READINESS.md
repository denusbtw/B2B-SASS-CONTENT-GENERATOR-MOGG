# Integration readiness

The mock run needs no external credentials. Google Drive has an opt-in adapter. The other live integrations below require separate implementation and approval. Store eventual credentials outside the repository. Recheck official requirements before live verification.

| Integration | Owner input and purpose | Configuration and verification | Current behavior and limitation |
|---|---|---|---|
| OpenAI GPT | Project/key, approved structured-output and multimodal model IDs, spend and retention policies | Secret store and provider configuration. Verify with one approved schema-constrained call under the cap | MockGPT plans deterministically. No cloud calls |
| Google Drive | Desktop OAuth client and owner consent | [Google Drive setup](GOOGLE_DRIVE.md). Verify the first archive receipt in the operator page | Optional Drive API v3 archive with drive.file. Local archive remains the source of truth. Google calls are mocked in tests |
| YouTube | OAuth project, channel consent, upload scope, quota, visibility and disclosure defaults | Per-account secret reference. Verify an approved private or unlisted test where supported | Mock receipt only |
| TikTok | Registered app, redirect URI, creator consent, approved publishing scope, account and transfer method | Per-account secret reference and optional verified HTTPS origin. Verify creator settings, test upload and status | Mock receipt only |
| Instagram | Meta app, professional account, login route, account ID, permissions and media hosting | Per-account secret reference. Verify approved media container lifecycle and publish status | Mock receipt only |
| Media origin | Owned HTTPS domain, hosting, access policy and expiry | Configure delivery provider. Verify server-side fetch and expiry | Local upload/download with authentication |
| TTS | Vendor, credential, permitted voices, locale and timing marks, consent and spending cap | Secret store and voice presets. Verify locale coverage, timing and price on one sample | Synthetic audible timing tone |
| Local LLM | Laptop endpoint, model, quantization, measured RAM/VRAM and quality | Private endpoint settings. Benchmark a specific task before enabling | No local model required |
| 2D direction | Rights-cleared examples and desired visual fidelity | Versioned asset packs. Review a short pilot with maintainers | Original geometric demo |
| Source rights | Owner or license evidence, attribution, restrictions and retention | Per-asset intake. Verify proof before reuse | Explicit declaration and evidence required for mock approval |
| Publication governance | Authorized account owners, cadence, privacy, disclosure and escalation policy | Per-account settings and approval role. Verify policy on one reviewed scheduled batch | Immutable mock approval snapshots |
| Hardware and network | Private addresses, role tokens, disk availability, host uptime and Docker/WSL readiness | Coordinator/worker environment. Verify health, authenticated claims and archive transfer from laptop B | Two processes verified on one host |
