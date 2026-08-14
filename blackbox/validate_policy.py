from pathlib import Path

compose = Path(__file__).with_name("docker-compose.yml").read_text(encoding="utf-8")
assert "internal: true" in compose
assert "\n    ports:" not in compose
assert "TARGET_SCOPE_URL" in compose
print("compose_policy: PASS")
