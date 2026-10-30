# v0.3 validation — 2026-10-30

Local environment: Python 3.12. Command: `python3 -m unittest discover -s tests -v`. Result: **17 tests passed**. Offline demonstration: `python3 -m app.demo`, completed with balances 85 and 115.

Coverage includes authenticated task transitions, rejected foreign actions, cancellation refund exactly once, release/reassignment, revision/resubmission, concurrent overspending/acceptance/payout, cancellation versus acceptance, database reopen, two independent SQLite connections, and deliberately failed ledger inserts rolling back registration/payout.

HTTP tests start an actual localhost server on an ephemeral port and exercise registration, authentication, lifecycle endpoints, audit permissions, invalid JSON/UTF-8, non-object bodies, invalid/oversized lengths and content type, plus eight simultaneous task requests.

One scripted scenario creates ten demo identities and completes 100 tasks; after every task it checks conservation of balances plus held rewards and absence of negative balances. It also verifies 400 task audit records. This validates the simulation engine only; these are not ten autonomous models or independently verified research deliverables.

No production load test, security audit, public deployment, real model integration, paid inference, token issuance or real-money settlement was performed. Python 3.10–3.13 CI is configured but local testing alone does not establish remote CI success.

# v0.4 validation — 2026-10-30

Local Python 3.12: **33 tests passed**. The added coverage checks identical and conflicting requests, actor isolation, canonical object ordering, every task transition replay, simultaneous identical/conflicting requests, invalid/duplicate keys, authentication before replay, rejected unsupported routes, key reuse after business failure, failure to write the replay record rolling back all business writes, independent SQLite connections, database reopen, and additive v0.3 schema migration.

The preceding v0.3 GitHub Actions run completed successfully: run `41032104834`. v0.4 remote results must be checked after pushing; local tests do not establish CI success. No paid model inference or public deployment was performed.

# v0.5 validation — 2026-10-30

40 local tests pass, including full HTTP worker workflow, persisted restart behavior, job and declared-cost limits, uncertain execution handling, state exclusivity and identity binding, closure and Brier scoring. No paid model execution was performed. Real model adapters, autonomous evidence verification and provider billing enforcement are outside this validation.


# v0.6 validation — 2026-10-30

56 local tests pass. Added coverage exercises encrypted delivery and recipient isolation, signed context/tamper rejection, expiry/network/sequence replay, concurrent inventory, two-buyer licenses, ciphertext/key-directory substitution, order binding, timeout refund, freeze authority and existing escrow settlement, encrypted backup restoration, shared task ledger policy, receipt failure rollback, participant access, enrollment immutability, HTTP legacy-spend bypass and durable restart.

`python3 -m app.private_demo` exercises actual local HTTP with client keys, encrypted purchase, decryption, settlement, backup restoration and preauthorized freeze. No real model, cloud rental or real currency was used. These tests do not establish independent security review, fair exchange, prevention of plaintext resale, host-owner isolation or resistance to a malicious database administrator. Remote CI must be checked separately after publication.
