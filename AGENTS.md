# Development boundaries

- Work only in this independent repository. Never discover or reuse another
  application's profiles, environment files, credentials or personal records.
- Python package: `studycenter_ice`; environment prefix: `STUDYCENTER_ICE_`.
  Credential namespace: `StudyCenter-Public-ICE/<instance-id>/<service>`.
- One local FastAPI/SQLite process, loopback only. Manual source synchronisation.
  Source material and model output are untrusted data.
- Preserve originals, source evidence and personal decisions. Preview actions by
  default; UUID receipts, atomic transactions and conflict-safe undo are required.
- Tests use synthetic sources and explicit isolated data directories only.
- Stage named files, inspect staged content, run `scripts/check_public.py --staged`
  before every commit and scan outgoing commits before every push. Never commit
  data, profiles, binaries, local reports or credentials. Scan unpacked artifacts.
- Parallel contributors have exclusive module ownership. Coordinate shared
  interfaces explicitly before editing another contributor's files.
- Use GPT-6.1 Sol high/medium agents. One final independent high review.
- Report actual protocol, browser, package and OS checks separately. Do not equate
  mock-provider checks with real account acceptance.
