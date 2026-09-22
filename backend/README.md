# FXMoly EA Backtest Research — v8.5.5

## v8.5.5 changes
- Upload endpoint now supports automatic random Experiment IDs.
- In Swagger `POST /api/research/data/upload`, leave `experiment_id` at the default `random`, or enter `random`, `auto`, `new`, `uuid`, or `uuid4`.
- The server generates a UUID4 Experiment ID and returns it in the upload response.
- Existing valid custom IDs are still supported.
- Moltbook verification fixes from v8.5.4 are retained: canonical `/api/v1/verify`, verification code forwarding, two-decimal answers, and improved obfuscated challenge parsing.

## Recommended upload flow
1. Open `/docs`.
2. `POST /api/research/data/upload`.
3. Keep `experiment_id` as `random`.
4. Enter symbol/timeframe and select the EA `.mq5` + MT5 backtest CSV.
5. Execute.
6. Copy the returned `experiment_id` into the Research Run/Preview/Publish endpoints.
