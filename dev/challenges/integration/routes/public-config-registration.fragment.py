# Bounded registration excerpt; full source hash is in source-manifest.json.
# The enclosing production API application is broader and remains an external
# service integration; this fragment records the Challenge blueprint hook.
from challenge_config_api import bp as challenge_config_public_bp
app.register_blueprint(challenge_config_public_bp)
