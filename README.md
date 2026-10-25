# CI failure triage: rules vs an LLM agent

When a CI run fails, the first question is why. The change broke the code, the
change broke a test, a flaky test failed, or the CI machine itself is broken. I
wanted to see when an LLM agent actually helps with this, and when a few
hand-written rules are enough.

I compared three approaches on the same 120 failures, where I know the right answer
for each one:

- **rules**: plain Python checks, no LLM
- **agent**: Gemini with tools to read the log, the diff and files, and to rerun a
  failing test. It has to return a report in a fixed JSON format.
- **hybrid**: run the rules first and only ask the agent when the rules are not sure

## Results

Gemini 3.1 Flash-Lite, one run. I hit the free-tier daily limit after 74 of the
100 test cases, so all three are compared on those same 74 (44 regressions,
16 flaky, 14 infra). The other 26 are still to run.

| | rules | agent | hybrid |
|---|---|---|---|
| fully correct | 64/74 | 73/74 | 73/74 |
| right function for regressions | 37/44 | 44/44 | 44/44 |
| cases sent to the LLM | 0% | 100% | 32% |
| LLM calls per case | 0 | 6.5 | 2.4 |
| median latency | 0.17 s | 17.4 s | 0.17 s |
| cost per case | $0 | $0.0039 | $0.0015 |

"Fully correct" means the right category, plus the right file and function when
the change is at fault. Cost is based on Google's paid-tier prices (the run itself
was on the free tier). Latency leaves out time spent waiting on rate limits. More
detail is in `results/summary_test.md`.

## What I found

The rules did better than I expected, and they were honest about when they were
guessing. Every time they said high or medium confidence they were right (50/50).
All 10 of their mistakes were in the 24 cases they marked low confidence. So the
hybrid only sends those 24 to the agent and ends up with the same score as running
the agent on everything, with about 63% fewer LLM calls.

Where the agent really helped was regressions where the traceback doesn't touch
the changed line. The function just returns a wrong value and an assertion fails
somewhere else. That was 28 of the 44 regressions. The rules got 21 of those, the
agent got all 28. When the traceback does point at the changed line, both got all
16.

The agent's one miss was interesting. On flaky-012 it found the real problem, an
unseeded `random.sample` in the test, and tried to blame the test file. My
validation only accepts files that are in the diff, so it rejected that. On the
retry the model blamed the only changed file just to pass the check, and said so
in its notes. My check pushed it from almost right to wrong. Next time I would
change the error message to suggest flaky or infra when the cause is outside the
diff.

One more thing I learned the hard way: my first run looked like the agent scored
exactly the same as the rules. It turned out every Gemini call had failed on a
billing error and the code quietly fell back to the rules 100 times. Now a key or
billing error stops the run instead.

## How the test cases were made

I used a real library, [boltons](https://github.com/mahmoud/boltons), pinned to
one commit, and ran its real test suite after breaking things on purpose:

- **regression**: a tiny bug in a library function (like `<` to `<=`, `and` to `or`,
  or an off-by-one), mixed in with up to three harmless edits elsewhere so the diff
  looks like a normal commit
- **test_bug**: a wrong expected value or input in a test
- **flaky**: a test that depends on random order already exists in the repo, and
  the diff is harmless. I ran CI until it failed and saved three real reruns.
- **infra**: the CI environment is broken (missing module, open-file limit,
  permission errors on the temp folder), and the diff is harmless

Since I made each failure myself, I know the right answer. There are 20 dev cases
I used for smoke tests and 100 test cases. I wrote the rules before building the
cases and didn't change the rules or the prompt after seeing test results.

## How it works

`triage/rules.py` goes through a fixed list of checks: known environment errors,
a traceback line inside the change, a traceback through a changed function, an
edited test, a rerun that passes. If none match, it guesses by name and marks
that as low confidence.

`triage/agent.py` is the agent loop. The model calls a tool every turn and ends
with `submit_report`. I check the report against the JSON schema and against the
diff. If it fails, the model gets the error and can try again twice. If it still
fails, the rules answer is used. Every step is saved to `results/traces/`, which
is how I figured out what happened on flaky-012.

Both use the same tools, so neither sees anything the other can't.

## Limitations

- The bugs are artificial and all from one library. Real bugs are usually bigger
  than a one-token change.
- The infra cases only cover three kinds of breakage, so both systems find them easy.
- I only ran the agent once, so I don't know how much its results vary between runs.
- 26 test cases still need to run.

## Running it

Open `run_on_colab.ipynb` in Colab, add your Gemini key under Secrets as
`GEMINI_API_KEY`, and run the cells. To run locally:

```
pip install -r requirements.txt
python -m bench.fetch
python -m pytest -q tests
python -m evals.run --system rules --split test
GEMINI_API_KEY=... python -m evals.run --system agent --split test
python -m evals.report --split test
```

The test cases include modified copies of boltons files (BSD-3-Clause).
