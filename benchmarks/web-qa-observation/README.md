# Observed host-owned web QA benchmark

`observed-localhost.json` records two selected pages from one temporary Git
fixture, collected with the installed native agent-browser adapter. It keeps
metadata, hashes and measured timings, not raw browser output or screenshots.
The positive capture received two independent visual reviews before real CLI
import/show/compare. The negative page produced console, first-party request
and accessibility findings. This is local fixture evidence, not production
health or a population performance estimate.

## Reproduce (maintainer/operator)

```sh
uv run python tools/benchmarks/web_qa_hermes_agent_browser_localhost_proof.py --output-dir .omc/qa/local-web-qa-benchmark
```

Choose a new private output directory. The proof writes a normalized plan,
sanitized receipt and reviewable PNG for each fixture. It deliberately leaves
visual review missing. Review the positive PNG against the native-DOM fixture,
bind reviewer/rubric/evidence IDs and score to its exact capture and lineage,
and put that review in a copy of the receipt. Never invent a score or rescore a
failed old capture to create another round.

```sh
uv run python tools/benchmarks/web_qa_observation_import.py --plan PLAN.json --receipt REVIEWED_RECEIPT.json --capture CAPTURE.png
```

The second program uses a temporary Git project and the real OMH CLI. It measures
import/repeat/show/compare wall time independently from adapter command time and
requires identical completed evidence with no changed files on repeat. Browser
execution belongs to the first host program; the import program makes no browser,
model or deployment calls. Screenshots remain private runtime artifacts.
