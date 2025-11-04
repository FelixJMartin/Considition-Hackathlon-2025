# Put ALL your algorithm here so it’s independent of networking.

def should_move_on_to_next_tick(response: dict) -> bool:
    # Start simple (like JS starter). Replace with a real acceptance test later.
    return True


def generate_customer_recommendations(map_state: dict, current_tick: int) -> list[dict]:
    """
    Return a list of customer recommendations for this tick.
    Shape should match the API’s contract (fill in when docs arrive), e.g.:
    [
      {
        "customerId": "...",
        "vehicleId": "...",
        "action": "route" | "charge",
        "route": [nodeId1, nodeId2, ...],          # if action == "route"
        "stationId": "...", "powerKw": 11.0        # if action == "charge"
      }, ...
    ]
    """
    # BASELINE: do nothing (safe no-op) until you implement routing/charging.
    return []


def generate_tick(map_state: dict, tick_index: int) -> dict:
    return {
        "tick": tick_index,
        "customerRecommendations": generate_customer_recommendations(map_state, tick_index),
    }
