"""python -m crittersdeck [serve | hash-password]"""

import argparse
import getpass
import sys

from .auth import hash_password


def main(argv=None):
    parser = argparse.ArgumentParser(prog="crittersdeck")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the board (default)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=7420)
    serve.add_argument("--ingest-host", default="0.0.0.0")
    serve.add_argument("--ingest-port", type=int, default=0,
                       help="also serve only POST /ingest on this port: the one to publish to the internet")
    sub.add_parser("hash-password", help="print a CRITTERSDECK_PASSWORD_HASH for a password")
    args = parser.parse_args(argv)

    if args.cmd == "hash-password":
        password = getpass.getpass("Password: ")
        if len(password) < 12:
            sys.exit("Use at least 12 characters.")
        if password != getpass.getpass("Again: "):
            sys.exit("The passwords differ.")
        print(hash_password(password))
        return

    import asyncio

    import uvicorn

    from .server import create_apps

    board, ingest = create_apps()
    servers = [uvicorn.Server(uvicorn.Config(board, host=getattr(args, "host", "127.0.0.1"),
                                             port=getattr(args, "port", 7420), proxy_headers=False,
                                             timeout_graceful_shutdown=4))]
    if getattr(args, "ingest_port", 0):
        servers.append(uvicorn.Server(uvicorn.Config(ingest, host=args.ingest_host, port=args.ingest_port,
                                                     proxy_headers=False, timeout_graceful_shutdown=4)))
    # No proxy_headers: the trusted-proxy check must see the real peer address, not X-Forwarded-For.
    # The two servers time out one after the other on shutdown: 4 s each stays under docker stop's 10 s.
    # Both servers share one event loop, so the board and the ingest app share their state.
    # Only the board server runs the app lifespan (housekeeping); the ingest app has none.

    async def run():
        await asyncio.gather(*(server.serve() for server in servers))

    asyncio.run(run())

if __name__ == "__main__":
    main()
