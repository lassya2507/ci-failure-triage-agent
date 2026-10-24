"""The triage report every system (rules, agent, hybrid) has to produce.

Keeping one schema for all three systems is what makes the comparison fair:
they are graded on exactly the same fields.
"""
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class Category(str, Enum):
    REGRESSION = "regression"  # the change broke library code
    TEST_BUG = "test_bug"      # the change broke a test; the library code is fine
    FLAKY = "flaky"            # a nondeterministic test failed; unrelated to the change
    INFRA = "infra"            # the CI environment is broken; unrelated to the change


# For these two categories the change itself is at fault, so we also ask
# "which changed function did it?"
NEEDS_CULPRIT = {Category.REGRESSION, Category.TEST_BUG}


class TriageReport(BaseModel):
    category: Category
    culprit_file: Optional[str] = None
    culprit_function: Optional[str] = None
    confidence: Literal["high", "medium", "low"]
    evidence: str = Field(default="", max_length=800)
    suggested_action: str = Field(default="", max_length=400)


def names_match(predicted: Optional[str], truth: Optional[str]) -> bool:
    """Lenient function-name match: 'Cls.meth' matches 'meth' and vice versa."""
    if not predicted or not truth:
        return False
    p, t = predicted.strip(), truth.strip()
    return p == t or t.endswith("." + p) or p.endswith("." + t)


def grounding_problems(report: TriageReport, changed_functions: dict) -> list:
    """Checks that a blamed culprit actually exists in the change.

    changed_functions maps file path -> list of qualified function names that
    the diff touches. A model that blames a file or function the diff never
    touched is hallucinating, so we reject the report and tell it why.
    """
    problems = []
    if report.category not in NEEDS_CULPRIT:
        return problems
    if not report.culprit_file:
        problems.append(f"category '{report.category.value}' needs culprit_file (a file from the diff).")
        return problems
    if report.culprit_file not in changed_functions:
        problems.append(
            f"culprit_file '{report.culprit_file}' is not in the diff. Changed files: "
            + ", ".join(sorted(changed_functions))
        )
        return problems
    funcs = changed_functions[report.culprit_file]
    if not report.culprit_function:
        problems.append("culprit_function is required for this category.")
    elif not any(names_match(report.culprit_function, f) for f in funcs):
        problems.append(
            f"culprit_function '{report.culprit_function}' is not a changed function in "
            f"{report.culprit_file}. Changed functions there: {', '.join(funcs) or '(none)'}"
        )
    return problems
