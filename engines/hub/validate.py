"""Validate external-upstream configuration using Python's existing startup path.

Construct and close the server without starting request handlers, maintenance,
or callback delivery. Python retains schema and retention-policy authority.
"""

import argparse
import os
import sqlite3
import sys

from brigade import fleet_command_deck, fleet_hub, fleet_hub_events
from brigade.cli import main


def validate(**options):
    try:
        token = fleet_hub._load_token(argparse.Namespace(token_file=options.pop("token_file")))
        fleet_hub_events.configure(fleet_hub_events.config_from_environment(os.environ))
        options["port"] = 0
        options["db_path"] = options["db_path"].expanduser()
        server = fleet_hub.make_server(token=token, **options)
        server.server_close()
    except (fleet_hub.FleetHubError, sqlite3.Error, fleet_command_deck.DeckConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


fleet_hub.run = validate
raise SystemExit(main(sys.argv[1:]))
