"""The guard ties numbers to the item they're said about (review, Nov 14): swapping two items'
quantities is blocked even though both numbers appear in the evidence."""
import json

from app.agent.guard import numbers_check

PLAN = json.dumps({"items": [
    {"item": "sprite", "unit": "each", "on_hand": 2.0, "on_order": 0.0, "par": 6.0, "order_qty": 10, "unit_cost": 1.0, "cost": 10.0},
    {"item": "coke", "unit": "each", "on_hand": 3.0, "on_order": 0.0, "par": 6.0, "order_qty": 9, "unit_cost": 1.0, "cost": 9.0},
], "total_cost": 19.0})
EV = f"Tool results from this conversation:\n[reorder_plan] {PLAN}"


def test_true_claims_pass():
    assert numbers_check("Order 10 sprites and 9 cokes, $19 total.", EV) is True
    assert numbers_check("Coke is at 3, par 6.", EV) is True
    assert numbers_check("Sprite: on hand 2, par 6, so order 10.", EV) is True


def test_swapped_quantities_are_blocked():
    assert numbers_check("Order 9 sprites and 10 cokes.", EV) is False
    assert numbers_check("Coke is at 2.", EV) is False                # sprite's on-hand said about coke


def test_shortfall_named_difference_still_allowed():
    assert numbers_check("Sprite is 4 below par.", EV) == "derived"     # par 6 - on hand 2: allowed, not boosted


def test_numbers_outside_item_records_unaffected():
    assert numbers_check("The total is $19.", EV) is True
    assert numbers_check("Pepperoni is at 99.", EV) is False             # not in evidence at all
