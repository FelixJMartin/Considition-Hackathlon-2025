import os
import sys
import time
import argparse
from typing import Any, Dict, List, Optional

from client import ConsiditionClient


def should_move_on_to_next_tick(response: Dict[str, Any]) -> bool:
    """
    Basic accept rule: move on if there are no obvious errors in the response.
    You can make this smarter later (e.g., only advance if score improves).
    """
    if isinstance(response, dict) and response.get("errors"):
        print("Server reported errors; staying on this tick to try again.")
        return False
    return True


def generate_customer_recommendations(map_obj: Dict[str, Any], current_tick: int) -> List[Dict[str, Any]]:
    """
    FIRST RUN: return an empty list to validate connectivity and discover schema.
    After you inspect the game responses, implement your real logic here.
    """
    return []


def generate_tick(map_obj: Dict[str, Any], current_tick: int) -> Dict[str, Any]:
    return {
        "tick": current_tick,
        "customerRecommendations": generate_customer_recommendations(map_obj, current_tick),
    }


def pretty_print_first_state(map_obj: Dict[str, Any]) -> None:
    print("\n=== MAP / INITIAL STATE SUMMARY ===")
    print(f"name: {map_obj.get('name')}")
    print(f"ticks: {map_obj.get('ticks')}")
    print("top-level keys:", list(map_obj.keys()))
    # Print sizes of common lists if present
    for key in ["customers", "stations", "chargingStations", "events"]:
        val = map_obj.get(key)
        if isinstance(val, list):
            print(f"{key}: {len(val)}")
        elif isinstance(val, int):
            print(f"{key}: {val}")
    print("===================================\n")


def extract_game_id(game_response: Dict[str, Any]) -> Optional[str]:
    """
    Visualizer needs a game id. Different engines sometimes use slightly different field names.
    We try a few common ones and cache the first we see.
    """
    for k in ["gameId", "gameID", "id", "game_id"]:
        if k in game_response and isinstance(game_response[k], (str, int)):
            return str(game_response[k])
    # Sometimes the id may be nested (rare). Add any special-casing here if you find it.
    return None


def main():
    parser = argparse.ArgumentParser(description="Considition client runner")
    parser.add_argument("--map", dest="map_name", default=os.getenv("CONSIDITION_MAP_NAME", "Batterytown"),
                        help="Map name to play (e.g. Batterytown, Clutchfield, Turbohill)")
    parser.add_argument("--base-url", dest="base_url", default=os.getenv("CONSIDITION_BASE_URL", "http://localhost:8080"),
                        help="API base URL (default: http://localhost:8080)")
    parser.add_argument("--api-key", dest="api_key", default=os.getenv("CONSIDITION_API_KEY", "CHANGE_ME"),
                        help="API key (or set ENV CONSIDIION_API_KEY)")
    parser.add_argument("--max-ticks", dest="max_ticks", type=int, default=None,
                        help="Limit number of ticks to play (debugging)")
    args = parser.parse_args()

    api_key = args.api_key
    base_url = args.base_url
    map_name = args.map_name

    if api_key == "CHANGE_ME":
        print("ERROR: Set your API key via --api-key or CONSIDIION_API_KEY env var.")
        sys.exit(1)

    print(f"Connecting to {base_url} with map '{map_name}'...")
    client = ConsiditionClient(base_url, api_key)

    # Fetch map / initial state
    map_obj = client.get_map(map_name)
    if not map_obj:
        print("Failed to fetch map!")
        sys.exit(1)

    pretty_print_first_state(map_obj)
    total_ticks = int(map_obj.get("ticks", 0))
    if args.max_ticks is not None:
        total_ticks = min(total_ticks, args.max_ticks)

    # Prepare first tick
    good_ticks: List[Dict[str, Any]] = []
    current_tick = generate_tick(map_obj, 0)
    input_payload: Dict[str, Any] = {
        "mapName": map_name,
        "ticks": [current_tick],
    }

    print("Starting simulation...")
    final_score = 0.0
    printed_game_id = False
    last_seen_game_id: Optional[str] = None

    for i in range(total_ticks):
        while True:
            print(f"\n--- Playing tick {i} ---")
            # For debugging, keep payload short in logs
            dbg_payload = {**input_payload}
            if "ticks" in dbg_payload and isinstance(dbg_payload["ticks"], list):
                dbg_payload["ticks"] = [{"tick": t.get("tick", "?"),
                                         "customerRecommendations": f"{len(t.get('customerRecommendations', []))} rec(s)"} for t in dbg_payload["ticks"]]
            print("Request:", dbg_payload)

            start = time.perf_counter()
            game_response = client.post_game(input_payload)
            elapsed_ms = (time.perf_counter() - start) * 1000
            print(f"Response time: {elapsed_ms:.2f} ms")

            if not game_response:
                print("Got no game response")
                sys.exit(1)

            # Show top-level keys so you can discover what’s available
            print("Response keys:", list(game_response.keys()))

            # Print and cache Game ID (for Visualizer)
            gid = extract_game_id(game_response)
            if gid:
                last_seen_game_id = gid
                if not printed_game_id:
                    print(f"\n### VISUALIZER ###")
                    print(f"Game ID: {gid}")
                    print(f"API Key: {api_key}")
                    print("Open: https://visualizer.considition.com/game")
                    print(
                        "Enter the Game ID above, and the same API Key you used here.")
                    print(
                        "(Visualizer currently supports Batterytown and Clutchfield.)\n")
                    printed_game_id = True

            # Convenience score peek (actual scoring uses server logic)
            final_score = (
                float(game_response.get("customerCompletionScore", 0) or 0)
                + float(game_response.get("kwhRevenue", 0) or 0)
                + float(game_response.get("score", 0) or 0)
            )
            print(f"Score snapshot (sum of fields): {final_score}")

            # Update map/state for next decisions if server returns it
            updated_map = game_response.get("map") or map_obj

            if should_move_on_to_next_tick(game_response):
                # lock-in tick i
                good_ticks.append(current_tick)
                # build next tick i+1
                current_tick = generate_tick(updated_map, i + 1)
                input_payload = {
                    "mapName": map_name,
                    "playToTick": i + 1,
                    "ticks": [*good_ticks, current_tick],
                }
                break
            else:
                # retry same tick i (you could modify strategy here if needed)
                current_tick = generate_tick(updated_map, i)
                input_payload = {
                    "mapName": map_name,
                    "playToTick": i,
                    "ticks": [*good_ticks, current_tick],
                }

    print("\n=== FINAL ===")
    print(f"Final score snapshot: {final_score}")
    if last_seen_game_id:
        print(f"Last Game ID: {last_seen_game_id}")
        print("Open https://visualizer.considition.com/game and paste the Game ID + your API key to replay.")
    print("Done.")


if __name__ == "__main__":
    main()
