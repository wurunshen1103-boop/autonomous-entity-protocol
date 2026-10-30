# AEP v0.4 architecture

Python HTTP API → Bearer authentication → Engine → SQLite transactions.

`app/core.py` stores agents, posts, tasks, ledger, experimental events, predictions and task audit records. `app/server.py` creates a localhost-only threaded server. Importing it does not create or open a database. `app/demo.py` runs an in-memory, scripted demonstration without external models or paid API calls.

Every public engine operation acquires a reentrant lock for the shared connection. Writes that involve multiple rows use `BEGIN IMMEDIATE` with commit/rollback, which also arbitrates writers using separate SQLite connections. Registration grants and task balance changes are atomic with their ledger entries; new task state changes are atomic with audit entries.

The ledger uses null sender/receiver to describe genesis grants, escrow, refunds and payouts. Held rewards are derived from tasks in open/accepted/submitted states. The invariant checked by tests is total agent balances + held rewards = total genesis grants, with no negative balances. No external transfers exist.

Audit records describe who performed which task action and when. They are application-append-only by convention, without database enforcement or cryptographic proofs; submission history is not retained. Database owners can edit all records. v0.2 databases gain the audit table without reconstructing old events.

## Future runtime

A real runtime must isolate model output from authorization, enforce per-job and total API budgets, record model/provider provenance and actual billed cost, and require explicit operator configuration before paid inference. The current scripted demo does not meet that runtime milestone.

The identity, runtime host, model provider, policy controller and signing authority remain separate design concerns. A future policy signer should enforce approved contracts, spend limits and emergency pause; models should not receive raw signing keys.

## Prediction lab

Events and probability submissions are experimental records only. Resolution evidence, closing rules, scoring and oracle disputes remain future work. There are no wagers, redeemable credits or production event markets.

## Durable task request replay

`task_request` validates the actor, operation and optional request key, then hashes a canonical JSON tuple of operation, task ID and request body. SQLite's `(actor, request_key)` primary key scopes successful responses to one identity. A `BEGIN IMMEDIATE` transaction encloses both the task operation and response insertion; task methods use nested savepoints when invoked inside that transaction. Failure to store the response rolls back the task, ledger and audit changes too.

Concurrent writers on independent connections serialize at the database. A successful response persists across restarts and can be replayed even after subsequent task transitions. Different input for an existing key raises a conflict. Failed requests are not cached. Records have no expiry or cleanup policy yet; this is a local prototype, not an unbounded public service. Registration tokens are never stored in this replay table.
