# Google Drive Excel backups

Your connected Drive account has [Personal Options Trader Backups](https://drive.google.com/drive/folders/1CxpHsKBCJ8dxxNYbKAx6NMwEU9tojMn5). On 22 October 2026, a compact completed local sample trade and its existing verified archive were uploaded, downloaded again and checked by byte size, MD5 and SHA-256. The local queue then showed two uploaded files and zero pending files. No source records were deleted.

The Codex heartbeat **Back up paper-trading Excel reports** runs daily at **4:00 pm Asia/Kolkata** when this laptop and Codex are available. It uses the connected Google Drive account; Google credentials are not copied into the trading app, Render or GitHub. It is not a continuously running cloud-to-Drive service.

## Backup contents

- Compact summaries of completed trades in the existing local SQLite database. An explicit field allowlist includes report data, strategy legs, final P&L and observed maximum profit/loss with IST times.
- Already verified Excel files saved by the local or online companion. Existing detailed workbooks are preserved.
- No active trade is treated as final. Credentials, owner/session records, database dumps and companion tokens are excluded.

The Neon outage prevents downloading cloud-only records. Once access returns, the online companion can collect completed reports into **Documents/PaperTrading/OnlineTrades** for the next backup. Backup does not reset the monthly transfer allowance or delete database records. Storage and monthly transfer are separate limits.

## Verification and recovery

From the project folder:

```powershell
.venv\Scripts\python.exe scripts\drive_backup.py prepare
```

This reads known local sources without contacting the cloud database. It stages XLSX files and a durable manifest in `.desktop/drive-backup`, then prints pending paths, names, sizes and hashes. Source databases are opened read-only. Private configuration is never printed.

For each pending item, search the destination for the exact filename before uploading. Verify the remote destination parent, XLSX MIME type and size. Compare `md5Checksum` if metadata exposes it. The current connector omits that field, so use Drive `fetch(download_raw_file=true, include_base64=false)`, download its authenticated file reference once and compare byte size, MD5 and SHA-256 with the pending item. Do not print temporary download URLs.

Only after successful remote verification:

```powershell
.venv\Scripts\python.exe scripts\drive_backup.py confirm KEY --drive-id VERIFIED_ID --md5 VERIFIED_MD5 --size VERIFIED_SIZE
```

The command rechecks the local file before marking that exact item uploaded. Repeating preparation does not create another upload. If confirmation is interrupted, verify and reuse the existing Drive file. A changed file or mismatched checksum/size stays pending for review. Original records and archives remain intact.

Keep the private `.desktop/drive-backup` manifest for duplicate prevention. It is excluded from Git and source ZIPs. This personal destination is configured in `scripts/drive_backup.py`; another installation needs its own connected account, folder and automation.
