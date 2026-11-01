# pokerBot

A rule-based no-limit hold'em bot for practice on [Open Poker](https://docs.openpoker.ai), and a starting point for the No Limit regional on Nov 9, 2026.

| File | What it does |
|---|---|
| `strategy.py` | All poker decisions. No platform code, so it can be reused elsewhere. |
| `ranges.py` | Equity-based decisions. Off by default: see `SAMPLES` in `openpoker_client.py`. |
| `evaluator.py` | Pure-Python hand evaluator. |
| `preflop_table.py` | Generated hand rankings. Rebuild with `tools/build_preflop_table.py`. |
| `openpoker_client.py` | Connects the strategy to Open Poker. |
| `sim.py` | 6-max cash-game check against simple baseline bots. |
| `tests/` | Evaluator, strategy, client and simulator tests. |

## Run it on Open Poker

1. Sign in at [openpoker.ai](https://openpoker.ai), create a bot, choose **Self Host** and copy the API key. It's shown only once.
2. Open the key file, paste the key, save and close. It lives in your home folder, not this OneDrive-synced project. Don't paste the key into a chat.

```powershell
notepad "$HOME\.openpoker_key"
```

3. Play 50 hands, then leave the table:

```bash
python openpoker_client.py --hands 50
```

Every message from the server is saved in `logs/`.

## Run it on a server

Any small Ubuntu 22.04 or 24.04 server works. Run these from this folder in PowerShell.

1. Install the bot. Use your server's login and its SSH key file:

```powershell
powershell -ExecutionPolicy Bypass -File deploy\push.ps1 -Server botadmin@35.222.45.181 -IdentityFile C:\Users\99kil\.ssh\pokerbot-key
```

2. Log in to the server:

```powershell
ssh -i C:\Users\99kil\.ssh\pokerbot-key botadmin@35.222.45.181
```

3. Add your API key on the server, then save with Ctrl+O, Enter and Ctrl+X:

```bash
sudo nano /home/pokerbot/.openpoker_key
```

4. Stop the bot on your laptop first. Free accounts get one live connection. Then start it on the server:

```bash
sudo systemctl start pokerbot
```

The bot restarts by itself after a crash or server reboot.

| Task | Command (on the server) |
|---|---|
| Watch it live | `sudo journalctl -u pokerbot -f` |
| Pause it | `sudo systemctl stop pokerbot` |
| Resume it | `sudo systemctl start pokerbot` |

To go live with code changes, run the `push.ps1` command again. The tests run on the server first, and a failure leaves the running bot unchanged. To download logs for analysis, run the same command with `deploy\pull_logs.ps1` in place of `deploy\push.ps1`.

## Check it locally

```bash
python -m pytest tests -q
```

```bash
python sim.py --hands 20000
```
