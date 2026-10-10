# Stockly GitHub Actions setup

## Important
- This setup checks products from GitHub Actions, so your Mac does not need to stay on.
- `watches.json` is the cloud list for this first test. Products added only in the iPhone app will NOT automatically appear here yet.
- Scheduled GitHub Actions runs can be delayed. Public repositories get free standard hosted runner usage, but this is not a guaranteed real-time service.
- Do not commit `.env`, API keys, or Telegram bot tokens.

## Install
Copy these files into the root of your existing `StocklyBackend` GitHub repository:
- `.github/workflows/stockly-watch.yml`
- `scripts/poll_watches.py`
- `watches.json`

Keep your existing `main.py` and `requirements.txt`.

## Add GitHub Actions secrets
In the repository, go to Settings -> Secrets and variables -> Actions -> New repository secret.
Create these three secrets:
- `REEF_API_KEY`: your ReefAPI key
- `TELEGRAM_BOT_TOKEN`: your Telegram bot token
- `TELEGRAM_CHAT_ID`: your Telegram chat ID

Never paste these secrets into a source file or commit them to GitHub.

## Add products
Edit `watches.json`. Add one object per product, with `url`, `size`, `color`, and `in_stock: null` for a new product. The first successful run records the current state without sending a restock alert. Later, an alert is sent only for `false -> true`.
Example:
{
  "url": "https://www.zara.com/tr/tr/example.html?v1=123456",
  "size": "M",
  "color": "Black",
  "in_stock": null,
  "availability": "unknown",
  "last_checked": null,
  "product_name": "Product name"
}

## Test
Open the repository's Actions tab, choose "Stockly stock monitor", click Run workflow.
Review the run logs. If it succeeds, it will update watches.json with the latest stock state.
