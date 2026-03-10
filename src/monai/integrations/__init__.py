"""Platform integrations — each agent gets its own connection.

Each integration provides a dedicated client with:
- Per-agent API key management
- Rate limiting and retry logic
- Cost tracking for platform fees
"""
