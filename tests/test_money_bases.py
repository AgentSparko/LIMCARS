"""The two savings bases must stay named and distinct.

lost_potential() subtracts an ANNUALISED PACE from a year of capacity. That is a
defensible choice -- both sides per-year -- but it is a pace, and an earlier
comment on the assignment claimed the opposite. The plain reading of "lost
potential" (a year of best hour minus money actually registered) is exposed as
its own field instead of silently becoming the headline.
"""
from app import counters as C


def test_both_savings_bases_are_produced():
    src = open(C.__file__, encoding="utf-8").read()
    for f in ("saved_period_cost", "saved_year_cost", "loss_year_cost",
              "loss_capacity_minus_registered_cost"):
        assert f'cand["{f}"]' in src, f"{f} is no longer produced"


def test_saved_year_cost_is_not_described_as_the_registered_sum():
    assert "not a pace invented here" not in open(C.__file__, encoding="utf-8").read(), (
        "the false comment is back: saved_year_cost is tot_cost/rec_days*365")
