# Contributing

MeshtasticPass is early-stage. Small, focused changes with tests are easiest to
review.

1. Fork the repository, create a feature branch, and keep unrelated changes out
   of the branch.
2. Use simulation mode for hardware-free development:

   ```bash
   python app.py --simulate
   ```

3. Before opening a pull request, run:

   ```bash
   python -m unittest discover -v
   python -m compileall -q .
   git diff --check
   ```

4. Explain user-visible behavior, tests, hardware assumptions, and any radio
   traffic a change can generate.

Never commit real CHAT databases, node logs, `.env` files, credentials,
Meshtastic channel keys, private certificates, or local configuration. Do not
include sensitive mesh traffic, node details, or locations in bug reports.
Use obviously synthetic data in tests and examples.

New radio features must not generate unexpected LoRa traffic. Preserve the
project's truthful-data approach: do not fabricate hop counts, delivery
confirmation, presence, acknowledgements, or other facts the available radio
data cannot support.

Dependencies in `requirements.txt` are intentionally pinned; dependency changes
should be separate, justified updates rather than incidental cleanup.

The project does not yet have a software license. This guide describes a future
contribution workflow but does not grant permission to reuse or redistribute the
code; license selection remains unresolved.
