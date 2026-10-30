# v0.3 validation — 2026-10-30

Local environment: Python 3.12. Command: `python3 -m unittest discover -s tests -v`. Result: **17 tests passed**. Offline demonstration: `python3 -m app.demo`, completed with balances 85 and 115.

Coverage includes authenticated task transitions, rejected foreign actions, cancellation refund exactly once, release/reassignment, revision/resubmission, concurrent overspending/acceptance/payout, cancellation versus acceptance, database reopen, two independent SQLite connections, and deliberately failed ledger inserts rolling back registration/payout.

HTTP tests start an actual localhost server on an ephemeral port and exercise registration, authentication, lifecycle endpoints, audit permissions, invalid JSON/UTF-8, non-object bodies, invalid/oversized lengths and content type, plus eight simultaneous task requests.

One scripted scenario creates ten demo identities and completes 100 tasks; after every task it checks conservation of balances plus held rewards and absence of negative balances. It also verifies 400 task audit records. This validates the simulation engine only; these are not ten autonomous models or independently verified research deliverables.

No production load test, security audit, public deployment, real model integration, paid inference, token issuance or real-money settlement was performed. Python 3.10–3.13 CI is configured but local testing alone does not establish remote CI success.
