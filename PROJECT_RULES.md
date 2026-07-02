# Trading Intelligence Project Rules

## Core development rules

1. GitHub is the source of truth.
2. VPS is only for running deployed code.
3. No secrets in GitHub.
4. Real keys live only in .env on VPS.
5. One commit = one finished task.
6. Every module must have one clear responsibility.
7. Do not push broken code to main.
8. Update CHANGELOG after meaningful changes.
9. Follow ROADMAP order.
10. Do not add new markets before Polymarket Weather V1.0 works.

## V1.0 scope

Only:
- Telegram bot
- Polymarket
- Weather markets
- Resolution-first strategy
- Claude as explanation layer after verified data collection

Not yet:
- Kalshi
- Crypto exchanges
- Wallet tracking
- Auto-trading

## Architecture rule

AI does not make the final decision.
Signal Engine makes the decision.
Claude explains the result.
