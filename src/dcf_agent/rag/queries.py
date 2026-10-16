"""Search queries in the filings' own vocabulary.

A question asks "how much principal is due in the second year"; the filing's
table says "Maturities of long-term debt" with year labels (2027, 2028, ...). The
expansion adds the terms filings use for each item and turns relative years into
the fiscal-year labels of the filing, using only what the caller knows: the
field and the filing's fiscal year end. It never sees an answer.
"""

import re
from datetime import date

# (pattern in the question, terms filings use)
TERMS: tuple[tuple[str, str], ...] = (
    (r"debt.*(repaid|due|matur|schedul)|(repaid|due|matur|schedul).*debt",
     "maturities of long-term debt principal payments schedule"),
    (r"lease.*payments.*due|lease payments", "lessee lease liability maturity undiscounted "
     "payments total lease payments less imputed interest"),
    (r"lease.*discount rate", "weighted average discount rate lease term"),
    (r"fair value.*debt|debt.*fair value", "fair value of long-term debt carrying amount"),
    (r"interest rate.*debt|debt.*interest rate", "weighted average interest rate effective rate"),
    (r"options.*outstanding|exercise price", "stock options outstanding weighted average "
     "exercise price shares under option"),
    (r"unvested|restricted stock", "nonvested restricted stock units RSU performance shares "
     "awards outstanding"),
    (r"excluded from diluted|potentially dilutive|antidilutive",
     "antidilutive anti-dilutive excluded from the computation of diluted earnings per share"),
    (r"not yet been recognized|unrecognized compensation",
     "unrecognized compensation cost nonvested awards weighted average period"),
    (r"effective income tax rate|statutory|state and local",
     "effective tax rate reconciliation statutory federal rate"),
    (r"unrecognized tax benefits", "unrecognized tax benefits uncertain tax positions"),
    (r"operating loss carryforward", "net operating loss carryforwards deferred tax assets"),
    (r"restructuring", "restructuring charges program severance"),
    (r"impairment", "impairment charges write-down"),
    (r"benefit obligation|pension", "projected benefit obligation pension plans"),
)  # fmt: skip
# Checked in order: "after year five" before "year five".
RELATIVE_YEARS: tuple[tuple[str, int | None], ...] = (
    (r"after (year )?five|after five years|beyond five", None),
    (r"next twelve months|within one year|year one|first year", 1),
    (r"second year|year two", 2),
    (r"year three|third year", 3),
    (r"year four|fourth year", 4),
    (r"year five|fifth year", 5),
)


def expand(question: str, period_end: date | None = None) -> str:
    """The question plus the filing's terms and fiscal-year labels for relative years."""
    text = question.lower()
    extra = [terms for pattern, terms in TERMS if re.search(pattern, text)]
    for pattern, offset in RELATIVE_YEARS:
        if re.search(pattern, text):
            if offset is None:
                extra.append("thereafter")
            elif period_end is not None:
                extra.append(str(period_end.year + offset))
            break
    return " ".join([question, *extra])
