# Your Personal Options Trader

This is your private paper-trading website. **Kotak Neo is the only enabled broker. The app cannot place real broker orders.** Existing 5paisa and Dhan trade histories and exports are preserved as historical records; they are not active connection or new-trade choices.

## Open the website

Open [Personal Options Trader](https://personal-options-trader.vercel.app/) on your phone or laptop and sign in with your existing app password. Both devices use the same online workspace.

The configured online setup is Vercel for the website, RamNaym server **1606** for the backend, and SQLite at `/app/data/trading.db` on its persistent volume. It does not depend on Neon or your laptop remaining switched on. The hosting plan must remain active, and broker sessions, market permissions and service connectivity must be available. No uninterrupted-uptime guarantee is implied.

If the page says **Connection unavailable**, use **Check connection** after the backend is reachable. A loading Vercel page alone does not prove the backend is healthy. Do not create a second owner account or replace the database to fix a connection error.

## Connect Kotak Neo

1. Open **Broker connections** and choose **Set up Kotak Neo**.
2. In Kotak Neo, create the Trade API application and complete TOTP registration if needed.
3. Enter the application key, registered mobile number, client code, MPIN and authenticator setup key only in the private website setup form. Never put them in chat, GitHub or frontend settings.
4. Choose **Save private setup**, then **Refresh contracts** to import Kotak's NSE, BSE and MCX derivative contracts.
5. Select a permitted contract during its exchange session. Check the actual feed status and quote timestamp before placing a paper trade.

Saving credentials does not itself prove that the broker connection works. A rejected or expired session requires Kotak's supported authentication flow again. [Connection details and limitations](docs/BROKERS.md).

## Create a paper strategy

1. Open **Option chain**, choose the exchange, underlying and expiry, and select buy or sell beside a contract. Or open **New paper trade** to start in the strategy builder.
2. Set expiry, strike, CE/PE or futures, buy/sell and lots for each leg. Only contracts in the current Kotak master can be selected.
3. Check the quote age, estimated margin, estimated charges and expiry payoff. An unknown MCX monetary conversion or stale price blocks a market paper fill.
4. Select **Review paper trade**, review the assumptions and confirm. This records a simulation inside the app; it sends no broker order.
5. Open **Positions & trades** to inspect individual legs, partial exits and full strategy exits. A live-data paper exit also requires a usable quote.

Sample mode uses fictional contracts and synthetic prices. It is separate from genuine broker-data paper trades and cannot validate a market feed. All-time trade counts distinguish saved genuine paper executions from preserved sample records.

## View records and live P&L

The backend calculates and durably records active-trade P&L at one-second intervals during the contract's verified exchange session. Each snapshot retains the original market quote timestamp and shows stale or reused prices accurately. Closing the browser does not stop recording an open paper position while the cloud service and broker connection remain available.

Trades held over several dates keep their original trade and strategy IDs. Contracts cannot trade beyond actual expiry; any authorized rollover is a separately linked transaction. The app does not automatically roll a position just because its contract expired.

## Connect the separate Google account

Open **Data & settings**, connect Google, and select the Google account you want to use for trading archives. Review Google's permission screen yourself. You can change accounts later; old files stay in the account where they were created.

Google authorization and private server settings must be configured before uploads can succeed. The app-owned cloud connection performs uploads while your laptop is off. The older Codex/laptop report companion is a separate optional workflow and is not required for cloud recording. [Google setup](docs/GOOGLE-SHEETS-LIVE.md).

## Download daily Excel history

Open **Daily Excel Downloads**. Each IST trading date has a daily report, with a separate worksheet for each individual broker-specific trade and continued sheets for positions held across dates. Large reports split into numbered workbook parts with compressed CSV supplements so records are not silently omitted.

1. Check that the export is finalized and its record count and worksheet coverage are verified.
2. Download every listed XLSX/CSV part to your Windows laptop.
3. Reopen the workbooks and compare the displayed checksum and file size with your downloaded files.
4. Keep a second independent verified copy for recovery. A single laptop download is not protection against laptop failure.

[Daily report details](docs/DAILY-REPORTS.md).

## Reuse cloud storage safely

**Delete Verified Cloud Copies** requires your explicit confirmation that a verified local backup exists. Clicking Download alone is not sufficient. This action must not delete open positions, unsynchronized records or the only remaining copy of history. Empty Google Drive Trash yourself only after verifying independent copies.

The separately configured automatic local-history archive triggers at **12 GiB**, verifies eligible older completed daily archives in Google Drive, and then removes only eligible snapshot details in bounded transactions. It retains active state, continuity anchors, essential summaries and export evidence. It does not clear the whole database, and freed SQLite pages may be reused without shrinking the file. [Archive policy](docs/GOOGLE-DRIVE-CLOUD.md).

## Check health and hosting

Use **Data & settings** to review feed status, recording gaps, CPU, RAM, persistent storage, exports and backup queues. Dashboard/log warnings do not imply email or SMS monitoring is configured. The Medium plan has a finite **15 GB** storage allowance; verify daily exports and respond to capacity alerts.

The agreed trial extension was through **28 October 2026, 11:59 PM IST**. Check the actual RamNaym plan status before relying on service after that point. No paid subscription should be created automatically.

## Optional local practice and recovery

The **Personal Options Trader** desktop shortcut or **Open-Trading-App.cmd** opens the separate laptop workspace at `http://127.0.0.1:8000`. This workspace needs the laptop to remain awake and does not automatically synchronize its trades to the cloud. Existing laptop and old Neon records remain preserved.

For a fresh local installation, install Python 3.12+ and supported Node.js, run **Setup-Windows.cmd**, then **Open-Trading-App.cmd**. Setup preserves existing private configuration and records. Use **Stop-Trading-App.cmd** to stop its local services safely.

Never upload `.env`, databases, passwords, API keys or OAuth tokens to GitHub. See [operations and recovery](docs/OPERATIONS.md), [older companion exports](docs/EXPORTS.md), and [actual verification status](TEST-REPORT.md).
