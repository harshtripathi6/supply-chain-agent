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


CASES = [
    case("T1", "A faster truck, a missed receiving window", "2026-10-05", 120, 16, [
        option("wait", "wait", "2026-10-06T09:00:00", 0, inspection=2),
        option("expedite", "expedite", "2026-10-05T15:30:00", 1800, inspection=2),
        option("transfer", "transfer", "2026-10-05T13:00:00", 600,
               donor_stock=400, donor_rate=10, donor_replenishment="2026-10-06T08:00:00"),
        option("substitute", "substitute", "2026-10-05T14:00:00", 900, inspection=1),
    ], topic="usable arrival receiving inspection"),
    case("T2", "A transfer that moves the shortage", "2026-10-12", 180, 14, [
        option("wait", "wait", "2026-10-13T09:00:00", 0, inspection=1),
        option("expedite", "expedite", "2026-10-12T15:00:00", 1500, inspection=1),
        option("transfer", "transfer", "2026-10-12T11:00:00", 250,
               donor_stock=200, donor_rate=20, donor_replenishment="2026-10-12T18:00:00"),
        option("substitute", "substitute", "2026-10-12T12:00:00", 700, inspection=1),
    ], topic="donor production coverage approved substitutes"),
    case("E1", "Recover the afternoon build", "2026-11-03", 90, 17, [
        option("wait", "wait", "2026-11-04T10:00:00", 0, inspection=1),
        option("expedite", "expedite", "2026-11-03T16:00:00", 1200, inspection=1),
        option("transfer", "transfer", "2026-11-03T12:00:00", 350,
               donor_stock=360, donor_rate=8, donor_replenishment="2026-11-04T08:00:00"),
        option("substitute", "substitute", "2026-11-03T13:00:00", 650, inspection=1),
    ], topic="usable arrival receiving inspection"),
    case("E2", "Two plants, one constrained component", "2026-11-09", 150, 13, [
        option("wait", "wait", "2026-11-10T08:00:00", 0, inspection=1),
        option("expedite", "expedite", "2026-11-09T14:00:00", 1700, inspection=1),
        option("transfer", "transfer", "2026-11-09T10:00:00", 200,
               donor_stock=180, donor_rate=15, donor_replenishment="2026-11-09T20:00:00"),
        option("substitute", "substitute", "2026-11-09T11:00:00", 550, inspection=1),
    ], topic="donor production coverage approved substitutes"),
    case("E3", "Premium freight earns its place", "2026-11-16", 200, 14, [
        option("wait", "wait", "2026-11-17T09:00:00", 0, inspection=1),
        option("expedite", "expedite", "2026-11-16T11:00:00", 1400, inspection=1),
        option("transfer", "transfer", "2026-11-16T12:00:00", 300,
               donor_stock=220, donor_rate=20, donor_replenishment="2026-11-16T19:00:00"),
        option("substitute", "substitute", "2026-11-16T13:00:00", 800, inspection=3),
    ], topic="usable arrival receiving inspection donor production coverage approved substitutes"),
]
TEACHING, EVALUATION = CASES[:2], CASES[2:]


def snapshot(c):
    """Explicit allowlist: no lesson theme, narrative title, outcome, or feedback."""
    result = {k: deepcopy(c[k]) for k in ("id", "plant", "component", "date",
              "shortage_quantity", "needed_at", "receiving", "options")}
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


def feedback(c, decision, outcome):
    best = min(score(evaluate(c, o["id"])) for o in c["options"] if o["approved"])
    verdict = "Confirmed" if score(outcome) == best else "Needs improvement"
    guidance = {
        "T1": "Compare when material is usable on the line, not the carrier's arrival promise. "
              "Receiving cutoffs and inspection can erase a freight advantage. Premium freight "
              "is justified when it actually prevents downtime at the lowest recovery cost.",
        "T2": "Protect production across both plants. Check donor coverage through replenishment "
              "before transferring stock. An approved substitute can be worth its premium; "
              "a transfer remains appropriate when the donor retains adequate coverage.",
    }
    return dict(source="Scripted simulated operator feedback", verdict=verdict,
                text=f"{decision['option_id']} caused {outcome['total_downtime_hours']} hours "
                     f"of combined downtime at ${outcome['recovery_cost']}. " + guidance[c["id"]])
