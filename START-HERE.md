# Your Personal Options Trader

This is your responsive paper trading app, including its website, backend, Excel reports, and Windows laptop companion. **It cannot submit real broker orders.**

## Open it on this Windows laptop

Your laptop is already set up. Double-click the **Personal Options Trader** shortcut on your desktop, or **Open-Trading-App.cmd** in the project folder. It opens [your local app](http://127.0.0.1:8000), starts the backend and report companion in the background, and reuses your existing settings. Sign in with the password you already created.

You can close the browser and reopen the shortcut later. The services continue running while the laptop is awake. When you want to stop the app and companion, double-click **Stop-Trading-App.cmd** in the project folder. Your saved records stay in place.

Keep the whole **personal-options-trader** folder together. Do not move individual files out of it. The desktop shortcut points to this folder.

## First installation on another Windows computer

1. Install **Python 3.12 or later** from [python.org](https://www.python.org/downloads/windows/). In the installer, select **Add Python to PATH**.
2. Install **Node.js LTS** from [nodejs.org](https://nodejs.org/). Keep the default installation options. Both downloads are free. If they are already installed, skip these steps.
3. Open the project folder in File Explorer. Double-click **Setup-Windows.cmd**. It installs the app’s packages and builds the website. Wait for **Setup complete**. This needs internet access on its first run.
4. Double-click **Open-Trading-App.cmd**. It starts the local app and prepares companion pairing automatically.
5. On your first visit, create a unique password of at least 12 characters. There is no default password. The local app does not need a hosting setup token.
6. Your initial workspace contains clearly marked **sample contracts and simulated prices**. Use it to learn the controls. These examples are not historical exchange data and cannot validate a live feed.

Running setup again preserves an existing private configuration and database. **Start-Windows.cmd** remains available for a visible backend console; keep that console open while using it. It starts only the backend, so use the companion instructions below if you choose this option.

## Try the main workflow

1. Open **Strategy builder**. Select NIFTY, BANK NIFTY, SENSEX, or a sample MCX commodity.
2. Choose a strategy template. Or use **Custom** and **Add leg**.
3. Use the dropdowns to choose expiry, strike, CE/PE, buy/sell, and lots. The plus/minus strike buttons step through contracts in the catalog.
4. Select one or more paper accounts: Dhan, Kotak Neo, or 5paisa. Select **Review paper trade**, check the preview, then confirm.
5. Open **Positions** and select the trade. Try **Add lots**, **Add leg**, and **Partial exit**. Every confirmed action is journaled.
6. Choose **Exit strategy**, then confirm. Open **Trade history** to inspect the closing P&L, MFE/MAE and their times, and profit giveback.
7. Open **Excel reports**. Your completed trade generates an individual workbook. The download button saves a normal browser download. For automatic verified saving, follow the next section.

## Save reports to your laptop automatically

The desktop shortcut and **Open-Trading-App.cmd** start the paired companion automatically. On this configured laptop, verified Excel reports save to **Documents/PaperTrading/Trades**. A workbook has already been saved and reopened successfully. Reports are marked saved only after their checksum and Excel verification succeed. Keep the laptop awake; if it is offline, queued jobs resume when the companion reconnects.

For manual pairing, a custom destination, or a hosted backend, use **Start-Companion.cmd**:

1. Double-click **Start-Companion.cmd** in the main project folder.
2. Follow the prompts. For local use, accept `http://127.0.0.1:8000` as the backend. Choose your save folder or accept `Documents/PaperTrading/Trades`.
3. The companion shows a **hash**. Copy that hash into the project’s private `.env` file after `COMPANION_TOKEN_SHA256=`. A `.env` file opens in Notepad through **Open with**.
4. Stop and restart the backend. Leave the companion running. Reports are marked saved only after its checksum and Excel verification succeed.
5. If the laptop is offline, leave the jobs queued. They remain in the backend database and resume when the companion reconnects.

Never put your private `.env` file in GitHub. Online journal cleanup is disabled by default. The complete explanation is in [the export guide](docs/EXPORTS.md).

## Use the same app on your phone

The local address above only works on this laptop. To use one login and shared trades on both devices, deploy the website and backend once using [the numbered cloud guide](docs/DEPLOYMENT.md). It uses your requested GitHub → Vercel / Render → Neon setup.

Your phone and laptop must open the **same Vercel HTTPS address**, connected to the **same Render backend and Neon database**. In Android Chrome, use **Add to Home screen** to install the app shortcut. The laptop companion still needs to run on the laptop for local file saving.

As of **21 October 2026**, your source is in the private [GitHub repository](https://github.com/BOBTHETRADER1123/personal-options-trader), and the **personal-options-trader** Neon Free project has been created in Singapore. Render and Vercel deployment are still pending, so there is no shared hosted app address yet. No paid service has been selected. A free backend can sleep, so it cannot guarantee uninterrupted feed capture.

## Connect real market prices

1. Use [the broker guide](docs/BROKERS.md) to obtain market-data permission from each broker you want to use.
2. Add credentials only to the backend’s private environment settings. Never add them to the website, GitHub, reports, or this chat.
3. Import the broker’s current instrument master. It supplies genuine expiry dates, strikes, tokens and lot sizes. Live MCX contracts also require a verified monetary price multiplier.
4. Set the feed subscriptions, turn Sample mode off in **Settings**, and restart the backend.
5. Check **Broker connections** for actual source, price, receive time, and stale warnings before simulating a trade. Permission or session failures are shown; sample data is never presented as a live connection.

Your own broker accounts and permissions are required. Real-account login, live feed operation, and broker subscription costs have not been verified without them.

## If something does not open

| What happens | What to do |
| --- | --- |
| The setup window says Python or Node is missing | Install them from the official links above, then reopen setup. |
| The browser says it cannot connect | Open the desktop shortcut or Open-Trading-App.cmd and allow the app to start. Use port 8000. If you chose Start-Windows.cmd, keep its console open. |
| Another app already uses port 8000 | Close the other local server, then restart. For a different port, follow README.md. |
| Sign-in expires | Sign in again. Your server records remain saved. |
| Market feed is stale | Check the broker session, subscription and backend status. Existing marks remain visible with a warning; stale marks are not fresh execution prices. |
| Report stays pending | Keep the backend running. Check Excel reports for its error, then retry. |
| Report is ready but not saved | Reopen Open-Trading-App.cmd to start the paired companion and keep the laptop awake. For manual or hosted pairing, check the companion URL and pairing hash. |
| Excel has a file open | Close that workbook and retry. Do not delete the journal/database to resolve this. |

For verification results and remaining account-specific checks, read [TEST-REPORT.md](TEST-REPORT.md).
