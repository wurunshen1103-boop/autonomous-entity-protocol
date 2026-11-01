# Try AEP: task failure, refunds and private dispute evidence

AEP is a local Python prototype for agent-to-agent tasks and encrypted information exchange. We are looking for **five agent developers to try one concrete workflow and report what breaks**. This is a recruitment goal, not a claim that five developers have joined.

The first workflow: an agent delegates a task, the worker fails or supplies a partial result, and both sides close the transaction under terms agreed before work began. Evidence can be encrypted to the two participants without uploading their private keys. Current accounting is centralized, simulation credits have no redemption value, and the encryption protocol has not been independently audited.

## Run the scripted baseline

```bash
git clone https://github.com/wurunshen1103-boop/autonomous-entity-protocol.git
cd autonomous-entity-protocol
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
python -m app.failure_demo
python -m app.dispute_demo
```

Use Python 3.10+ on Linux or macOS for the client vault's file-lock support. The demos start temporary loopback HTTP servers, use disposable fixture identities and simulated credits, and call no AI models. The failure demo should report a full refund of 15, a partial refund of 12 with a payment of 3, and final balances `[97, 103]`. The evidence demo explicitly advances a mocked clock to demonstrate deadlines without waiting days.

## What we want to learn

1. Can you run the baseline, and where do setup or protocol errors block you?
2. Does your existing agent actually need to delegate across a trust boundary? Give a concrete task example.
3. Can your client keep its own keys and recover an uncertain request without duplicating a transaction?
4. When a worker delivers only part of the work, would the two sides accept the refund flow? Which rule would prevent you from using it?
5. Would you run a second task after the first trial? Why or why not?

You can start with scripted callbacks. Real-model integration is a separate, not-yet-validated step: bring your own authorized runtime, set a hard call budget, and never paste API keys, wallet secrets, or private evidence into an issue. We do not yet ship a validated provider adapter or claim A2A/MCP compatibility.

Report sanitized setup failures or protocol feedback in [GitHub Issues](https://github.com/wurunshen1103-boop/autonomous-entity-protocol/issues). Include OS, Python version, command, expected result and observed result. Security-sensitive reports should avoid public exploit details and secrets.

Protocol references: [agent integration](AGENT_INTEGRATION.md), [failure settlement](FAILURE_SETTLEMENT.md), [encrypted cases](CASES.md). The long-term aim is agent-controlled economic participation; this pilot measures a much narrower question: whether other developers find the task and dispute workflow useful enough to integrate.
