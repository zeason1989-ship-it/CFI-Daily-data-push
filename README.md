# CFI Daily Data Push

This repository contains the Redash-to-email automation for the CFI daily data push.

## Run

```bash
python3 scripts/redash_email_push.py
```

Required environment variables:

- `REDASH_URL`
- `REDASH_API_KEY`
- `QUERY_ID` (optional; defaults to and must remain `3157`)
- `DASHBOARD_ID`
- `DASHBOARD_NAME`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `EMAIL_FROM`
- `EMAIL_TO`

The script refreshes Redash Query `3157`, keeps only 4-digit `MMDD` date columns,
renders the latest seven valid date columns into an HTML table, and sends the
report with `SMTP_SSL`.
