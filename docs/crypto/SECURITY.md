# Crypto Intelligence Security (V3.0.0a)

## Hard boundaries

- Public market-data HTTP GET endpoints only.
- No exchange API keys, secrets, or signed requests.
- No order placement, cancel, withdraw, transfer, or wallet APIs.
- No CCXT / trading SDKs / web3 in this slice.
- `assert_crypto_read_only` raises if `CRYPTO_INTEL_READ_ONLY` is false.
- Health probe marks critical DOWN when enabled and not read-only.

## Telegram

- Open auth (same as Weather / Opportunities).
- Never show absolute private filesystem paths or secrets.
- Error copy is sanitized (no raw exception dumps with URLs containing credentials).

## Testing

- CI uses synthetic fixtures only.
- Static forbidden-symbol scan covers `place_order`, `sign_transaction`, `private_key`, `wallet.sign`.
