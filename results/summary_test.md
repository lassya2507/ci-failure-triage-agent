# Results (test split, 74 cases)

Truth mix: flaky=16, infra=14, regression=44

| system | n | category_acc | culprit_acc | fully_correct | lat_p50_s | lat_p95_s | cost_per_case_usd | llm_calls_per_case | tokens_per_case | reruns_per_case | fallback_rate | invalid_submit_rate | escalated |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| rules | 74 | 95.9% | 84.1% | 86.5% | 0.17 | 0.8 | 0.00000 | 0.0 | 0 | 0.88 | 0.0% | 0.0% |  |
| agent_gemini-3.1-flash-lite | 74 | 98.6% | 100.0% | 98.6% | 17.38 | 62.94 | 0.00389 | 6.46 | 14215 | 0.18 | 0.0% | 2.7% |  |
| hybrid_low(gemini-3.1-flash-lite) | 74 | 98.6% | 100.0% | 98.6% | 0.17 | 56.78 | 0.00149 | 2.36 | 5456 | 0.92 | 0.0% | 1.4% | 32.4% |
| hybrid_med(gemini-3.1-flash-lite) | 74 | 98.6% | 100.0% | 98.6% | 10.94 | 60.91 | 0.00237 | 4.09 | 8596 | 1.05 | 0.0% | 2.7% | 59.5% |

## Fully correct, by true category

| system | regression | test_bug | flaky | infra |
|---|---|---|---|---|
| rules | 37/44 | 0/0 | 13/16 | 14/14 |
| agent_gemini-3.1-flash-lite | 44/44 | 0/0 | 15/16 | 14/14 |
| hybrid_low(gemini-3.1-flash-lite) | 44/44 | 0/0 | 15/16 | 14/14 |
| hybrid_med(gemini-3.1-flash-lite) | 44/44 | 0/0 | 15/16 | 14/14 |

## Regressions: does the traceback point at the bug?

| system | traceback hits changed line | traceback misses changed line |
|---|---|---|
| rules | 16/16 | 21/28 |
| agent_gemini-3.1-flash-lite | 16/16 | 28/28 |
| hybrid_low(gemini-3.1-flash-lite) | 16/16 | 28/28 |
| hybrid_med(gemini-3.1-flash-lite) | 16/16 | 28/28 |

## Confusion matrices (rows = truth, columns = predicted)

**rules**

| truth \ pred | regression | test_bug | flaky | infra |
|---|---|---|---|---|
| regression | 44 | 0 | 0 | 0 |
| test_bug | 0 | 0 | 0 | 0 |
| flaky | 3 | 0 | 13 | 0 |
| infra | 0 | 0 | 0 | 14 |

**agent_gemini-3.1-flash-lite**

| truth \ pred | regression | test_bug | flaky | infra |
|---|---|---|---|---|
| regression | 44 | 0 | 0 | 0 |
| test_bug | 0 | 0 | 0 | 0 |
| flaky | 0 | 1 | 15 | 0 |
| infra | 0 | 0 | 0 | 14 |

**hybrid_low(gemini-3.1-flash-lite)**

| truth \ pred | regression | test_bug | flaky | infra |
|---|---|---|---|---|
| regression | 44 | 0 | 0 | 0 |
| test_bug | 0 | 0 | 0 | 0 |
| flaky | 0 | 1 | 15 | 0 |
| infra | 0 | 0 | 0 | 14 |

**hybrid_med(gemini-3.1-flash-lite)**

| truth \ pred | regression | test_bug | flaky | infra |
|---|---|---|---|---|
| regression | 44 | 0 | 0 | 0 |
| test_bug | 0 | 0 | 0 | 0 |
| flaky | 0 | 1 | 15 | 0 |
| infra | 0 | 0 | 0 | 14 |

