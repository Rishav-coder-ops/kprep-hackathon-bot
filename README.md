# K-Prep's Hackathon Bot

A daily GitHub Actions workflow that discovers India-related hackathon announcements and posts new items to a Discord channel through a webhook.

## What it does

- Runs daily at 09:00 India Standard Time (GitHub Actions cron uses UTC).
- Searches Google News RSS for listings from an allowlist of known developer/event platforms.
- Filters for hackathon-related wording and India location signals.
- Deduplicates posts using `seen.json`.
- Posts Discord embeds with source links.
- Does not require your PC to be online.

## Important verification limitation

This starter version verifies that a result comes from an allowlisted publisher domain and contains India/hackathon signals. It does **not** prove that an organizer is legitimate, that registration is still open, or that the event is physically in India. The embed explicitly says "Organizer not independently verified". Always inspect the original event page before registering.

Google News RSS is a discovery layer and can miss events or return incomplete snippets. For stronger coverage, add official RSS/API feeds from each platform after checking their current terms and access rules.

## Setup

1. Create a Discord channel such as `#india-hackathons`.
2. In Discord, open **Edit Channel → Integrations → Webhooks → New Webhook**.
3. Name it `K-Prep's Hackathon Bot`, select the target channel, and copy the webhook URL. Treat it like a password.
4. Create a GitHub repository and upload `bot.py`, `requirements.txt`, `seen.json`, and `.github/workflows/daily-scan.yml`.
5. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret**.
6. Name the secret `DISCORD_WEBHOOK_URL` and paste the webhook URL as its value.
7. Open **Actions**, enable workflows if prompted, and run **Daily India hackathon scan → Run workflow** to test it.
8. The scheduled run will then happen daily at 09:00 IST.

## Local test (optional)

```bash
python -m pip install -r requirements.txt
# Set DISCORD_WEBHOOK_URL in your shell environment first.
python bot.py
```

## Configuration

- `MAX_POSTS_PER_RUN`: maximum posts in one run (default 15).
- `MAX_AGE_DAYS`: ignore older search results (default 45).
- Edit `TRUSTED_DOMAINS` and `QUERIES` in `bot.py` to adjust discovery sources.
- Edit `INDIA_SIGNALS` to add Indian cities or states that your event listings use.

## Troubleshooting

- **No posts:** run the workflow manually and inspect its logs. Search results may not currently include qualifying listings.
- **401/404 from Discord:** recreate the webhook and update the GitHub secret.
- **403 from GitHub push:** ensure workflow permissions allow read/write repository contents.
- **Duplicate posts:** do not delete `seen.json`; it stores the deduplication history.
- **Too many posts:** lower `MAX_POSTS_PER_RUN` or tighten the queries/domain allowlist.
