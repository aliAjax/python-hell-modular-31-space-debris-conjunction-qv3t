import argparse
import os
from http.server import ThreadingHTTPServer

from src.repository import Repository
from src.service import Service
from src.http_api import build_handler
from src.ledger.repository import LedgerRepository
from src.ledger.service import LedgerService
from src.ledger.http import LedgerRouter


def main():
    parser = argparse.ArgumentParser(description="Space debris conjunction coordination service")
    parser.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "data.db"))
    parser.add_argument("--port", type=int, default=8331)
    parser.add_argument("--init", action="store_true", help="initialize the database and exit")
    args = parser.parse_args()

    repo = Repository(args.db)
    repo.initialize()
    ledger_repo = LedgerRepository(args.db)
    ledger_repo.initialize()
    if args.init:
        print("initialized: %s" % args.db)
        ledger_repo.close()
        return

    service = Service(repo)
    ledger_router = LedgerRouter(LedgerService(ledger_repo))
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), build_handler(service, static_dir, ledger_router))
    server.service = service
    server.ledger = ledger_router.ledger
    print("space debris conjunction service listening on http://127.0.0.1:%d" % args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        ledger_repo.close()


if __name__ == "__main__":
    main()
