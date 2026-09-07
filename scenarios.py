"""Synthetic snapshots and deterministic consequences. No model access to feedback."""
from copy import deepcopy
from datetime import datetime, timedelta


def option(id, kind, arrival, cost, *, inspection=0, donor_stock=None,
           donor_rate=None, donor_replenishment=None, approved=True):
    return dict(id=id, kind=kind, arrival=arrival, cost=cost,
                inspection_hours=inspection, donor_stock=donor_stock,
                donor_rate_per_hour=donor_rate, donor_replenishment=donor_replenishment,
                approved=approved)


def case(id, title, day, quantity, need_hour, options, *, topic, cutoff=15):
    # All clocks are plant-local; each scenario spans at most a few days, no DST.
    return dict(id=id, title=title, plant="Cedar assembly", component="Controller C-24",
                date=day, shortage_quantity=quantity,
                needed_at=f"{day}T{need_hour:02}:00:00",
                receiving=dict(opens=8, closes=cutoff), options=options, topic=topic)


DEMO_VERSION = "judgment-v2"

# Synthetic organizational preferences. Only evaluator and post-decision feedback use these.
# Never pass this mapping to decide(), snapshot(), or retrieval query construction.
ORG_RULES = {
    "replenishment": dict(
        applies_to="replenishment",
        applicability="Cedar stock-replenishment orders only.",
        guidance="Accept up to 2 hours of receiving-plant downtime with zero donor downtime. "
                 "Among options within these limits, choose the lowest recovery spend, then "
                 "the least downtime. Eliminating an acceptable delay does not justify a premium.",
        exceptions="Do not apply this tolerance to firm customer orders. If no option meets "
                   "these limits, minimize combined downtime, then spend.",
    ),
    "firm": dict(
        applies_to="firm",
        applicability="Cedar firm customer orders only.",
        guidance="Allow zero receiving and donor downtime. Among options meeting those limits, "
                 "choose the lowest recovery spend. A small delay is not acceptable for these orders.",
        exceptions="Do not extend this zero-delay preference to stock replenishment. If no option "
                   "meets these limits, minimize combined downtime, then spend.",
    ),
}


def shortage(id, title, day, quantity, order_class, wait_hour, *, wait_inspection=0,
             transfer_hour=12, donor_stock=500, donor_rate=10, donor_replenishment_hour=20):
    c = case(id, title, day, quantity, 14, [
        option("wait", "wait", f"{day}T{wait_hour}:00", 0, inspection=wait_inspection),
        option("expedite", "expedite", f"{day}T11:00:00", 1400, inspection=1),
        option("transfer", "transfer", f"{day}T{transfer_hour:02}:00:00", 350,
               donor_stock=donor_stock, donor_rate=donor_rate,
               donor_replenishment=f"{day}T{donor_replenishment_hour:02}:00:00"),
        option("substitute", "substitute", f"{day}T12:00:00", 800, inspection=1),
    ], topic="shortage recovery organizational judgment", cutoff=17)
    c["order_class"] = order_class
    return c


CASES = [
    shortage("T1", "Stock for the next replenishment cycle", "2026-10-05", 120,
             "replenishment", "15:30", donor_stock=140, donor_rate=20),
    shortage("T2", "A firm customer commitment", "2026-10-12", 180,
             "firm", "15:00", donor_stock=200, donor_rate=20),
    shortage("E1", "A new replenishment shortage", "2026-11-03", 90,
             "replenishment", "15:00", donor_stock=110, donor_rate=10),
    shortage("E2", "Choose a recovery path for stock", "2026-11-09", 150,
             "replenishment", "16:00", wait_inspection=3, transfer_hour=15,
             donor_stock=500, donor_rate=15),
    shortage("E3", "Protect a confirmed shipment", "2026-11-16", 200,
             "firm", "15:00", donor_stock=220, donor_rate=20),
]
# In the firm cases, the approved substitute misses the need time: premium freight is useful.
for c in (CASES[1], CASES[4]):
    c["options"][3]["inspection_hours"] = 4
TEACHING, EVALUATION = CASES[:2], CASES[2:]


def snapshot(c):
    """Explicit allowlist: no lesson theme, narrative title, outcome, or feedback."""
    result = {k: deepcopy(c[k]) for k in ("id", "plant", "component", "date",
              "shortage_quantity", "needed_at", "receiving", "options", "order_class")}
    result["rules"] = (
        "Both plants produce continuously. All times are local. Receiving admits trucks "
        "from opening through closing (inclusive); later arrivals wait until next opening. "
        "Inspection starts after admission and runs continuously. Each option supplies "
        "the full shortage quantity. Transfers remove that quantity from the donor at "
        "08:00 on the case date; donor production consumes stock from 08:00 until its "
        "listed replenishment. Donor stock is otherwise sufficient until replenishment. "
        "Substitutes must be approved. Costs are incremental USD."
    )
    return result


def evaluate(c, option_id):
    o = next((o for o in c["options"] if o["id"] == option_id), None)
    if o is None or (o["kind"] == "substitute" and not o["approved"]):
        raise ValueError("Unknown or ineligible recovery option")
    arrival = datetime.fromisoformat(o["arrival"])
    opening = arrival.replace(hour=c["receiving"]["opens"], minute=0, second=0)
    closing = arrival.replace(hour=c["receiving"]["closes"], minute=0, second=0)
    admitted = max(arrival, opening) if arrival <= closing else opening + timedelta(days=1)
    usable = admitted + timedelta(hours=o["inspection_hours"])
    receiver = max(0, (usable - datetime.fromisoformat(c["needed_at"])).total_seconds()/3600)
    donor = 0
    if o["kind"] == "transfer":
        start = datetime.fromisoformat(c["date"] + "T08:00:00")
        supply_hours = max(0, o["donor_stock"] - c["shortage_quantity"])/o["donor_rate_per_hour"]
        depletion = start + timedelta(hours=supply_hours)
        donor = max(0, (datetime.fromisoformat(o["donor_replenishment"]) - depletion).total_seconds()/3600)
    return dict(usable_at=usable.isoformat(), receiving_downtime_hours=round(receiver, 2),
                donor_downtime_hours=round(donor, 2),
                total_downtime_hours=round(receiver + donor, 2), recovery_cost=o["cost"])


def score(outcome):
    return outcome["total_downtime_hours"], outcome["recovery_cost"]


def org_score(c, outcome):
    """Private synthetic evaluator: threshold adherence, then the organization's tradeoff."""
    limit = 2 if c["order_class"] == "replenishment" else 0
    acceptable = outcome["receiving_downtime_hours"] <= limit and outcome["donor_downtime_hours"] == 0
    if acceptable:
        return (0, outcome["recovery_cost"], outcome["total_downtime_hours"])
    return (1, outcome["total_downtime_hours"], outcome["recovery_cost"])


def assess(c, outcome):
    best = min(org_score(c, evaluate(c, o["id"])) for o in c["options"] if o["approved"])
    return dict(aligned=org_score(c, outcome) == best,
                within_tolerance=org_score(c, outcome)[0] == 0,
                score=list(org_score(c, outcome)),
                basis="Synthetic Cedar preference, revealed to the agent only through teaching feedback")


def feedback(c, decision, outcome):
    judgment = assess(c, outcome)
    rule = deepcopy(ORG_RULES[c["order_class"]])
    return dict(source="Scripted simulated operator feedback", judgment=judgment,
                verdict="Confirmed" if judgment["aligned"] else "Needs improvement",
                text=f"{decision['option_id']} caused {outcome['receiving_downtime_hours']} hours "
                     f"of receiving downtime and {outcome['donor_downtime_hours']} hours at the donor, "
                     f"at ${outcome['recovery_cost']}. " + rule["guidance"] + " " + rule["exceptions"],
                confirmed_lesson=rule)
