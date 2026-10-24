# CI failure triage: rules vs an LLM agent

Work in progress. Full write-up with measured results coming soon.

When a CI job fails, someone has to answer: did this change break the code, break a test,
hit a flaky test, or hit a broken runner? This repo compares three ways of answering that
on a benchmark with exact ground truth: hand-written rules, an LLM agent with tools, and a
hybrid that only calls the agent when the rules are unsure.

Run it: open `run_on_colab.ipynb` in Colab.

The benchmark cases contain modified copies of files from
[boltons](https://github.com/mahmoud/boltons) (BSD-3-Clause).
