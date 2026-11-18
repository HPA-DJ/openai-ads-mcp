# openai-ads-mcp

A local MCP server for the OpenAI Ads (ChatGPT Ads) Advertiser API. Python standard library only: no packages to install and no service in the middle. It talks straight to `https://api.ads.openai.com/v1` with your own API key.

Official API docs: https://developers.openai.com/ads/llms.txt

## Setup

1. In Ads Manager, open Settings and create an API key. A key is scoped to one ad account.
2. Copy `.env.example` to `.env` and paste the key after `OPENAI_ADS_API_KEY=`. The file is git-ignored.
3. Register the server with Claude Code (user scope, so every session sees it):

   ```bash
   claude mcp add --scope user openai-ads -- python3 /absolute/path/to/openai-ads-mcp/server.py
   ```

4. Start a new session and ask for the ad account details to confirm the key works.

## Tools

Read (always on):

| Tool | What it does |
|---|---|
| `ads_account` | Account id, status, timezone, currency |
| `ads_list` | List campaigns, ad groups, ads or custom audiences |
| `ads_get` | One campaign, ad group, ad or custom audience |
| `ads_insights` | Impressions, clicks, spend, CTR, CPC, CPM at account, campaign, ad group or ad level |
| `ads_conversion_insights` | Goal conversions and attributed events |
| `ads_audit_logs` | Change history recorded by the platform |
| `ads_spend_limits` | Account spending limits |
| `ads_audience_operations` | Upload and match progress for a custom audience |

Write (only when `OPENAI_ADS_ALLOW_WRITES=1` is in `.env`):

| Tool | What it does |
|---|---|
| `ads_propose_change` | Saves a change and returns a preview with the current state. Sends nothing |
| `ads_apply_change` | Sends one proposed change, once |

Supported actions: create, update, pause and activate for campaigns, ad groups and ads, plus setting the account daily spend limit. Archive, audience membership changes, file uploads, product feeds and the bulk API are not implemented.

## Safety design

- Read-only unless the write flag is set. With the flag off the write tools are not even listed.
- Every write is two calls. The proposal is saved to `changes/` with the state before the change, expires after 30 minutes and can be applied only once.
- Every proposal and apply is appended to `audit.jsonl`.
- Creates carry an idempotency key, so a retried create cannot duplicate.
- Resource ids are validated and paths are fixed in code; there is no free-form URL tool.
- Money fields are in micros: 1,000,000 micros is 1 unit of the account currency.

## Test

```bash
python3 test_offline.py
```

Runs the server against a fake API on localhost. It does not touch a real account and does not need a key.

## Status

Built from the public docs. The offline test passes and the read calls for the account, campaigns, custom audiences and account insights have been run against a live account. The write tools and the query encoding for insights filters have only been tested against the fake API.

## Licence

MIT. See [LICENSE](LICENSE). This is an independent project and is not affiliated with or endorsed by OpenAI.
