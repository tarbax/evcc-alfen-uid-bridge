"""Load Home Assistant Supervisor options, then start the bridge."""

import json
import os
import runpy
import sys


def main():
    options_path = "/data/options.json"
    if os.path.isfile(options_path):
        with open(options_path, encoding="utf-8") as options_file:
            options = json.load(options_file)
        for key, value in options.items():
            if value is None:
                continue
            if isinstance(value, (dict, list)):
                os.environ[key] = json.dumps(value)
            elif isinstance(value, bool):
                os.environ[key] = str(value).lower()
            else:
                os.environ[key] = str(value)

    sys.argv = ["main.py", *sys.argv[1:]]
    runpy.run_path("/app/main.py", run_name="__main__")


if __name__ == "__main__":
    main()
