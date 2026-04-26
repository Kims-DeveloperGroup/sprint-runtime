# Inter-Agent Communication Protocol

request_id: <YYYYMMDD-짧은ID>
intent: <plan|design|architect|implement|execute|qa|report|status|cancel|escalate>
urgency: low|normal|high
scope: <요청 요약>
artifacts: <관련 파일/리소스>

relay_transport:
- default: internal direct relay between roles
- debug: Discord relay channel (`python -m teams_runtime start --relay-transport discord`)
- relay summaries are posted to the configured relay channel after each relay
