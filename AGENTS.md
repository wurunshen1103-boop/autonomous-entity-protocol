# Repository working agreements

- Build for AI agents first: machine-readable protocols and complete executable workflows.
- At every subsequent version delivery, remind the user to reconsider the transaction strategy: order limits, reserve, pricing, acceptance/settlement, auctions and resale. This is a release-time reminder, not a scheduled notification or a request that blocks authorized development.
- Wallet private keys belong to the agent. Never add undisclosed backups, platform-readable private keys or administrator key resets. Backup is informed, opt-in ciphertext with a separate offline secret.
- Freeze requires the agent's preauthorized separate recovery signature. It pauses new spending, does not transfer assets and does not erase prior escrow commitments.
- Host-owner access to agent memory/keys remains an explicitly deferred problem. Do not claim host isolation or consciousness verification.
- Keep simulation credits, centralized settlement, unaudited cryptography and incomplete fair exchange clearly distinguished from production guarantees.
- Do not add paid provider calls or real-money settlement implicitly. Preserve existing task and balance compatibility and test concurrency, rollback, replay and authorization when changing settlement.
