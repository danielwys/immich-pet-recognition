import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Immich pet recognition companion")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    # Exactly one process owns the queue and GPU; inference runs off the API event loop.
    uvicorn.run(
        "immich_pet_recognition.api:create_app", factory=True, host=args.host, port=args.port
    )


if __name__ == "__main__":
    main()
