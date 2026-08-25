# Privacy and Local Data

MeshtasticPass currently stores application state on the local user account.
These paths honor the standard XDG overrides when they are set.

| Data | Default path |
| --- | --- |
| Settings, selected serial device, font/color preferences, and favorite Node IDs | `${XDG_CONFIG_HOME:-$HOME/.config}/meshtasticpass/config.json` |
| CHAT messages, sender Node IDs/names, timestamps, delivery state, and send attempts | `${XDG_DATA_HOME:-$HOME/.local/share}/meshtasticpass/chat.db` |
| Dedicated LXTerminal presentation profile | `${XDG_CONFIG_HOME:-$HOME/.config}/lxterminal/lxterminal-meshtasticpass.conf` |
| Saved launcher project location | `${XDG_CONFIG_HOME:-$HOME/.config}/meshtasticpass/project-dir` |
| Desktop launcher | `${XDG_DATA_HOME:-$HOME/.local/share}/applications/meshtasticpass.desktop` |
| Launcher commands | `~/.local/bin/meshtasticpass-run` and `~/.local/bin/meshtasticpass-launch` |
| Managed fullscreen rule | `${XDG_CONFIG_HOME:-$HOME/.config}/labwc/rc.xml`, inside the MeshtasticPass marker block |

SQLite may create temporary `chat.db-wal` and `chat.db-shm` files beside the
CHAT database while it is open.

Node metadata synchronized from the connected radio is used in memory. There
is no separate MeshtasticPass node-cache file today, although sender Node IDs
and names associated with accepted messages are stored in CHAT history.

MeshtasticPass currently has no telemetry and does not upload CHAT history to a
MeshtasticPass server. Favorites are local preferences and are not written to
the radio. Real radio sends and receives still behave according to Meshtastic,
the radio's configuration, and the surrounding mesh network.

Node IDs, node names, message history, timestamps, and location-derived CHAT
metadata can be personally identifying or sensitive. Treat the XDG config and
data files as private. Removing the source checkout does not remove data stored
in those user directories; inspect and remove that data separately when that is
your intent.
