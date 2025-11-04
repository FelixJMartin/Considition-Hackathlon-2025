import os
import time
from dotenv import load_dotenv
from client import get_map, post_game
from policy import generate_tick, should_move_on_to_next_tick

load_dotenv()
MAP_NAME = os.getenv("MAP_NAME", "TRAINING_MAP_1")


def main():
    map_state = get_map(MAP_NAME)
    if not map_state:
        print("Failed to fetch map!")
        raise SystemExit(1)

    final_score = 0
    good_ticks = []

    current_tick = generate_tick(map_state, 0)
    input_payload = {
        "mapName": MAP_NAME,
        "ticks": [current_tick],
    }

    total_ticks = int(map_state.get("ticks", 0))
    for i in range(total_ticks):
        while True:
            print(
                f"Playing tick: {i} with input keys: {list(input_payload.keys())}")
            t0 = time.perf_counter()
            game_response = post_game(input_payload)
            dt = (time.perf_counter() - t0) * 1000
            print(f"Tick {i} took: {dt:.2f} ms")

            if not game_response:
                print("Got no game response")
                raise SystemExit(1)

            # Mirror JS scoring aggregation
            final_score = (
                game_response.get("customerCompletionScore", 0)
                + game_response.get("kwhRevenue", 0)
                + game_response.get("score", 0)
            )

            if should_move_on_to_next_tick(game_response):
                good_ticks.append(current_tick)
                # The JS code builds the next tick from the response.map
                next_map = game_response.get("map", map_state)
                current_tick = generate_tick(next_map, i + 1)
                input_payload = {
                    "mapName": MAP_NAME,
                    "playToTick": i + 1,
                    "ticks": [*good_ticks, current_tick],
                }
                break
            else:
                # Retry same tick with updated map state
                next_map = game_response.get("map", map_state)
                current_tick = generate_tick(next_map, i)
                input_payload = {
                    "mapName": MAP_NAME,
                    "playToTick": i,
                    "ticks": [*good_ticks, current_tick],
                }

    print(f"Final score: {final_score}")


if __name__ == "__main__":
    main()
