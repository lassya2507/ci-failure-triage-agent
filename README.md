# CI failure triage: hand-written rules vs an LLM agent

When a CI job goes red, someone has to work out why before anyone can act on it.
Did this change break the library? Did it break a test? Did a flaky test fail?
Or is the CI machine itself broken? I wanted to measure, on cases where I actually
know the right answer, when an LLM agent is worth it for this job and when plain
rules are good enough.

So this repo has three systems answering the same question on the same benchmark:

- **rules**: the checks a CI engineer would write by hand. No LLM.
- **agent**: a Gemini model with tools (read the log, read the diff, open files,
  rerun a failing test) that has to submit a structured report.
- **hybrid**: run the rules first, and only call the agent when the rules say
  they are not confident.

## Results

Test split, Gemini 3.1 Flash-Lite, one run. The agent finished 74 of the 100 test
cases before I hit the free-tier daily quota, so every number below is on those
same 74 cases for all three systems (44 regressions, 16 flaky, 14 infra). The
other 26 (20 test bugs, 6 regressions) are still to run.

| | rules | agent | hybrid |
|---|---|---|---|
| fully correct (category, plus the right file and function when the change is at fault) | 64/74 (86.5%) | 73/74 (98.6%) | 73/74 (98.6%) |
| right category | 95.9% | 98.6% | 98.6% |
| right culprit function (regressions) | 37/44 | 44/44 | 44/44 |
| cases sent to the LLM | 0% | 100% | 32.4% |
| LLM calls per case | 0 | 6.46 | 2.36 |
| median latency | 0.17 s | 17.4 s | 0.17 s |
| p95 latency | 0.8 s | 62.9 s | 56.8 s |
| cost per case (paid-tier list price) | $0 | $0.0039 | $0.0015 |
| test reruns per case | 0.88 | 0.18 | 0.92 |

Latency here is my code plus time inside LLM calls plus recorded rerun time. It
leaves out the 37 minutes the run spent waiting on free-tier rate limits.

The full tables (per category, confusion matrices) are in `results/summary_test.md`.

## What I learned

**The rules are better than I expected, and they know when they are guessing.**
Whenever the rules said high or medium confidence, they were right: 50 out of 50.
All 10 of their mistakes came from the 24 cases they marked low confidence. That
is what makes the hybrid work. It sends only those 24 cases to the agent, gets
the same 73/74 as running the agent on everything, and makes about 63% fewer LLM
calls.

**The agent earns its cost on one specific kind of failure.** In 28 of the 44
regressions, the traceback never touches the line that was changed. The function
just returns a wrong value and some assertion further away fails. The rules have no
traceback link to follow there and often end up guessing by name; they got 21/28.
The agent read the failing test and the changed functions and got 28/28. When the traceback does point at the
changed line, both get 16/16, and the agent is just a slower, more expensive way
to get the same answer.

**A strict output check can push the model into a wrong answer.** The agent's one
miss (flaky-012) is the most interesting case in the run. It found the real cause,
an unseeded `random.sample` in the test, and wanted to blame the test file. My
grounding check only accepts culprits that are in the diff, and that test file was
not. So the check rejected the report, and on the retry the model blamed the only
changed library function instead, with a note saying the check forced it. That
flaky test also failed all three of its recorded reruns, which is why the rules
got it wrong too. Next time I would make the rejection message say "if the cause
is outside the diff, the category is probably flaky or infra" rather than just
"pick a file from the diff".

**Silent fallbacks hide outages.** My first Colab run "finished" with the agent
scoring exactly like the rules. Every Gemini call had failed on a billing error,
and the agent fell back to the rules 100 times without stopping. The fallback rate
column (100%) and LLM calls per case (0) are what gave it away. Now a key or
billing error stops the run, and a resumed run redoes those cases.

## How the benchmark works

The hard part of evaluating triage is ground truth. Real CI failures are rarely
labeled. So I made the failures myself, on a real library with a real test suite
([boltons](https://github.com/mahmoud/boltons), pinned to one commit), and every
case is an actual pytest run:

- **regression**: a one-token bug in a library function (flipped comparison,
  `and`/`or` swap, off-by-one, dropped `not`, ...), mixed into a diff with up to
  three behavior-preserving edits in other functions, so the diff looks like a
  normal commit and not just the broken line. I kept only bugs that some test
  actually catches, and checked that the extra edits alone pass the full suite.
- **test_bug**: a wrong expected value, wrong input, or flipped assertion in a
  test, plus unrelated edits.
- **flaky**: a test that depends on set ordering or unseeded randomness is already
  in the repo; the diff is harmless. I sampled CI runs until it failed, then
  recorded three real reruns of it.
- **infra**: the runner is broken (missing native module, open-file limit,
  temp-dir permission errors) through an environment hook outside the repo; the
  diff is harmless.

Since I injected each fault, I know the right category, and for regressions and
test bugs the exact function. 120 cases total: 20 dev (for smoke tests) and 100 test.
I wrote the rules before building the benchmark. Reruns are replayed from the recorded ones,
so every system sees the same rerun outcomes.

## The systems

`triage/rules.py` checks, in order: known environment-error signatures, a
traceback frame on a changed line, a frame inside a changed function, an edited
failing test, a rerun that passes, and finally a guess by name overlap (that last
step is the low-confidence one).

`triage/agent.py` is the tool loop. The model has to call a tool on every turn
and finish with `submit_report`. The report is validated with pydantic and then
checked against the diff (the blamed file and function must actually be changed).
On a rejection it gets the error back and can retry twice. If it runs out of
steps or keeps failing, the rules answer is used instead and the case is marked
as a fallback. Every LLM call and tool call goes to a JSONL trace
(`results/traces/`), which is how I debugged flaky-012.

Both systems use the same `Tools` class, so neither one can see anything the
other cannot.

## Limitations

- The faults are synthetic and come from one library. Real regressions are often
  bigger than one token, and real flaky tests are messier.
- The infra cases only cover three kinds of breakage, so they are easy for both.
- One model, one run. The agent is not deterministic, and I have not measured
  run-to-run variance yet.
- I wrote both the rules and the prompt, so neither is tuned as hard as it could
  be. I did not change either after seeing test results.
- Cost is computed from token counts and Google's paid-tier list price; the run
  itself used the free tier.
- 26 test cases are still pending (see Results).

## Run it

Open `run_on_colab.ipynb` in Colab (CPU is fine), add a Gemini key under Secrets
as `GEMINI_API_KEY`, and run the cells. Or locally:

```
pip install -r requirements.txt
python -m bench.fetch                      # clone boltons at the pinned commit
python -m pytest -q tests                  # offline unit tests (fake model)
python -m evals.run --system rules --split test
GEMINI_API_KEY=... python -m evals.run --system agent --split test --model gemini-3.1-flash-lite
python -m evals.report --split test
python -m bench.build --out bench/cases    # rebuild the benchmark (slow: thousands of pytest runs)
```

## Layout

```
bench/     benchmark builder: fault injection, test runner, the 120 cases
triage/    schema, log and diff parsing, tools, rules, agent, Gemini client
evals/     run a system over the cases, score it, build the hybrid
results/   raw per-case results, agent traces, summary tables
tests/     unit tests, including the agent's retry and fallback paths
```

The benchmark cases contain modified copies of boltons files (BSD-3-Clause).
