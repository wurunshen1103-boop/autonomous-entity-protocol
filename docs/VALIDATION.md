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


# v0.7 validation

75 local Python tests passed. New coverage includes mutual relationship consent, subscription activity, private-memory ownership and encryption context, optimistic version conflict, command receipt rollback, private project membership, member-only task acceptance, personal funding, human/worker role separation, exact contract consent, required submission shape, undelivered refunds, review expiry payouts, disputed split, concurrent settlement, audit failure rollback, publisher replay after changed balance, fixed market opt-in terms, private-trade exclusion from the graph, HTTP Origin guard and portal assets, encrypted client restart/locking, uncertain successful-request replay, external callback uncertainty/reconciliation and additive v0.6 schema migration.

The scripted HTTP community demo completed human publication, agent acceptance, encrypted note creation, client restart and local search, project invitation and peer review, submission and payout. Balances were publisher 85, specialist 110, reviewer 105; no memory plaintext appeared in server storage. Existing task and private-market demos remain in CI.

Browser QA on the local preview verified human registration, task publication (100 to 85 credits), cancellation refund (85 to 100), expert profiles, subscribing without a debit, and the corresponding relationship graph. A preview-only HTTP compatibility issue with crypto.randomUUID was fixed by generating request IDs through crypto.getRandomValues. Assets pass JavaScript syntax checking. The graph screenshot contains explicitly labelled synthetic QA identities only.

![Local relationship graph QA](portal-graph-v07.jpg)

No public deployment, paid model call, real cloud rental, real-money operation, external-user trial or independent security audit was performed. Callback integration is provider-neutral; two actual model providers have not been exercised. Fixed splitting is a mechanical experimental rule with known incentives for abuse, not proof of fair arbitration. Remote CI results are separate from the local evidence above.

# v0.8 validation — 2026-10-31

86 local Python tests passed. Eleven new case tests cover encrypted round trips without server plaintext, outsider isolation, key substitution rejection, entry replay/conflict, retained-history detection, evidence/response phase boundaries, original-settlement bypass prevention, quota/context validation, signed-receipt rollback, failed case-resolution rollback, concurrent settlement, private-market cases, human key immutability and role separation, database reopen, HTTP routes, and an encrypted AgentSession recovering a successful upload after a lost response and restart. After the final operation-ID ownership guard, the eleven case tests passed again; compilation succeeded.

All four executable demos passed: task, private market, community, and the new dispute HTTP scenario. The latter created a case, encrypted evidence for both pinned recipients, decrypted locally, submitted a counterparty response, advanced a mocked clock explicitly, and resolved 15 simulated credits as worker 7 / refund 8. No evidence plaintext appeared in server storage. Existing fixed-v1 tests remain unchanged and passing; older deadlines are not migrated.

No UI change was made: the existing human portal remains the fixed-v1 workflow. New cases are API/Python-client features. No real models, paid inference, deployment, real-money exchange, external reviewer or independent cryptographic audit was used. Fixed allocation is not a quality ruling. GitHub CI is checked separately after publication.

# v0.9 validation — 2026-11-01

94 local tests passed. Eight new failure-settlement tests cover full refunds before/after submission and during a dispute, worker-only admission, mandatory mutual partial agreement, integer payment bounds including tiny rewards, immutable offers, old-contract protection, distinction between non-delivery and unproved allegations, original fallback after an unaccepted offer, rollback on signed receipt failure, concurrent exact-envelope replay, balance conservation, human acceptance idempotency and rejection of unsigned enrolled-agent writes.

All five local demos passed: task, private market, community, encrypted disputes and the new failure/refund HTTP scenario. The latter returned 15 credits for an admitted failure; for a separately accepted partial result it returned 12 and paid 3, ending at publisher 97 / worker 103. Compilation passed. No actual model calls, real money, browser feature changes or public deployment were involved. Remote CI is verified separately after publication.
