# Bounded exact line excerpts from /srv/projects/api/admin_app.py.
# Full source hash, ranges, and provenance are in source-manifest.json. Context imports/helpers remain in the shared admin app; not standalone.
CHALLENGE_CONFIG_TOKEN_FILE = Path("/etc/nocturne/challenge-intake.token")
CHALLENGE_INTERNAL_BASE = "http://127.0.0.1:5011/internal/challenges"
CHALLENGE_CONFIG_INTERNAL_BASE = "http://127.0.0.1:5011/internal/challenges/config"
CHALLENGE_DB_PATH = Path("/srv/projects/database/Challenges.db")
CHALLENGE_ARTWORK_DIR = Path("/srv/projects/website/media/challenge-bosses")
CHALLENGE_ARTWORK_URL_PREFIX = "/media/challenge-bosses"
CHALLENGE_ARTWORK_MAX_BYTES = 5 * 1024 * 1024
CHALLENGE_BOSS_ICON_DIR = Path("/srv/projects/website/media/boss_icons")
CHALLENGE_BOSS_ICON_URL_PREFIX = "/media/boss_icons"
PET_KIT_ART_DIR = Path("/srv/projects/website/media/osrs-items")
def require_noc_super_admin(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        identity = discord_admin_identity()

        if not identity:
            return error(
                "Unauthorised — Discord authentication required",
                401
            )

        if "staff" not in current_discord_roles():
            return error(
                "Forbidden — Noc Super Admin access required",
                403
            )

        return f(*args, **kwargs)

    return decorated
def register_challenge_config_admin_routes():
    """Proxy staff-authorized config operations to the loopback writer."""

    def forward(path, method="GET", base=CHALLENGE_CONFIG_INTERNAL_BASE):
        try:
            token = CHALLENGE_CONFIG_TOKEN_FILE.read_text(
                encoding="utf-8"
            ).strip()
            if len(token) < 48:
                raise RuntimeError(
                    "challenge config service credential unavailable"
                )

            identity = current_admin()
            actor = str(
                identity.get("discord_id")
                or identity.get("username")
                or "admin-proxy"
            )[:128]
            body = (
                request.get_json(silent=True)
                if method != "GET"
                else None
            )
            response = requests.request(
                method,
                base + path,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Challenge-Admin": actor,
                },
                timeout=10,
                allow_redirects=False,
            )
            try:
                payload = response.json()
            except ValueError:
                payload = {
                    "ok": False,
                    "error": "challenge_config_service_invalid_response",
                }
            return jsonify(payload), response.status_code
        except requests.RequestException:
            return jsonify({
                "ok": False,
                "error": "challenge_config_service_unavailable",
            }), 503
        except (OSError, RuntimeError):
            return jsonify({
                "ok": False,
                "error": "challenge_config_service_unavailable",
            }), 503

    @app.get("/admin/api/challenges/config/published")
    @require_noc_super_admin
    def challenge_config_published():
        return forward("/published")

    @app.get("/admin/api/challenges/config/versions")
    @require_noc_super_admin
    def challenge_config_versions():
        return forward("/versions")

    @app.get("/admin/api/challenges/awards/diagnostics")
    @require_noc_super_admin
    def challenge_award_diagnostics():
        return forward(
            "/awards/diagnostics",
            base=CHALLENGE_INTERNAL_BASE,
        )

    @app.route(
        "/admin/api/challenges/config/draft",
        methods=["GET", "POST", "PUT"],
    )
    @require_noc_super_admin
    def challenge_config_draft():
        return forward("/draft", request.method)

    @app.post("/admin/api/challenges/config/draft/validate")
    @require_noc_super_admin
    def challenge_config_validate():
        return forward("/draft/validate", "POST")

    @app.post("/admin/api/challenges/config/draft/diff")
    @require_noc_super_admin
    def challenge_config_diff():
        return forward("/draft/diff", "POST")

    @app.post("/admin/api/challenges/config/draft/publish")
    @require_noc_super_admin
    def challenge_config_publish():
        return forward("/draft/publish", "POST")

    @app.post("/admin/api/challenges/config/draft/bosses")
    @require_noc_super_admin
    def challenge_config_create_boss():
        return forward("/draft/bosses", "POST")

    @app.put(
        "/admin/api/challenges/config/draft/bosses/<boss_key>"
    )
    @require_noc_super_admin
    def challenge_config_update_boss(boss_key):
        return forward(f"/draft/bosses/{boss_key}", "PUT")

    @app.post(
        "/admin/api/challenges/config/draft/bosses/<boss_key>/<action>"
    )
    @require_noc_super_admin
    def challenge_config_boss_state(boss_key, action):
        if action not in ("deactivate", "reactivate"):
            return jsonify({
                "ok": False,
                "error": "invalid_action",
            }), 400
        return forward(
            f"/draft/bosses/{boss_key}/{action}",
            "POST",
        )

    @app.post("/admin/api/challenges/config/draft/reorder")
    @require_noc_super_admin
    def challenge_config_reorder():
        return forward("/draft/reorder", "POST")


# Writes remain in the Simon-owned loopback challenge service; this process
# supplies the existing Discord/Cottus staff authorization boundary.
register_challenge_config_admin_routes()
def _png_dimensions(path: Path):
    """Return PNG dimensions without decoding or modifying the image."""
    try:
        header = path.read_bytes()[:24]
    except OSError:
        return None
    if len(header) < 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        return None
    return int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")


@app.get("/admin/api/challenges/artwork/catalog")
@require_noc_super_admin
def challenge_artwork_catalog():
    """List reusable canonical PNG assets; this endpoint never copies files."""
    assets = []
    try:
        candidates = sorted(
            CHALLENGE_BOSS_ICON_DIR.iterdir(),
            key=lambda item: item.name.casefold(),
        )
    except OSError:
        app.logger.exception("Challenge artwork catalog could not be read")
        return error("Boss artwork catalog is temporarily unavailable.", 500)
    for path in candidates:
        if path.suffix.casefold() != ".png" or not path.is_file():
            continue
        dimensions = _png_dimensions(path)
        if not dimensions:
            continue
        display_name = re.sub(r"[_-]+", " ", path.stem).strip()
        assets.append({
            "filename": path.name,
            "url": f"{CHALLENGE_BOSS_ICON_URL_PREFIX}/{quote(path.name, safe='')}",
            "display_name": display_name,
            "width": dimensions[0],
            "height": dimensions[1],
        })
    return ok({"artwork": assets, "count": len(assets)})


@app.post("/admin/api/challenges/config/draft/artwork")
@require_noc_super_admin
def challenge_config_upload_artwork():
    """Validate and stage immutable PNG artwork for a saved config draft."""
    boss_key = str(request.form.get("boss_key") or "").strip()
    if not re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)*", boss_key):
        return error("Choose a valid saved Challenge boss.", 400)
    try:
        draft_id = int(request.form.get("draft_id") or 0)
        revision = int(request.form.get("revision") or 0)
    except (TypeError, ValueError):
        return error("Draft and revision are required.", 400)
    uploaded = request.files.get("image")
    if not uploaded or not uploaded.filename:
        return error("Choose a PNG image to upload.", 400)
    if not str(uploaded.filename).lower().endswith(".png"):
        return error("Boss artwork must be a PNG file.", 400)
    if str(uploaded.mimetype or "").lower() != "image/png":
        return error("Boss artwork must use the image/png content type.", 400)
    data = uploaded.read(CHALLENGE_ARTWORK_MAX_BYTES + 1)
    if not data:
        return error("The selected PNG is empty.", 400)
    if len(data) > CHALLENGE_ARTWORK_MAX_BYTES:
        return error("Boss artwork may not exceed 5 MB.", 413)
    if (
        len(data) < 24
        or data[:8] != b"\x89PNG\r\n\x1a\n"
        or data[12:16] != b"IHDR"
    ):
        return error("The uploaded file is not a valid PNG.", 400)
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if not (1 <= width <= 4096 and 1 <= height <= 4096):
        return error("Boss artwork dimensions must be between 1 and 4096 pixels.", 400)

    connection = sqlite3.connect(
        f"file:{CHALLENGE_DB_PATH}?mode=ro",
        uri=True,
        timeout=10,
    )
    connection.row_factory = sqlite3.Row
    try:
        draft = connection.execute(
            """SELECT draft_json,revision,state FROM challenge_config_drafts
                 WHERE draft_id=? AND state IN ('draft','validated')""",
            (draft_id,),
        ).fetchone()
    finally:
        connection.close()
    if not draft:
        return error("The editable Challenge draft was not found.", 404)
    if int(draft["revision"]) != revision:
        return error("The draft changed. Reload it before uploading artwork.", 409)
    document = json.loads(draft["draft_json"])
    if not any(item.get("boss_key") == boss_key for item in document.get("bosses", [])):
        return error("Save this boss to the draft before uploading artwork.", 409)

    sha256 = hashlib.sha256(data).hexdigest()
    filename = f"{boss_key}-{sha256[:24]}.png"
    final_path = CHALLENGE_ARTWORK_DIR / filename
    try:
        CHALLENGE_ARTWORK_DIR.mkdir(parents=True, exist_ok=True)
        if not final_path.exists():
            temporary_path = CHALLENGE_ARTWORK_DIR / f".{filename}.{os.getpid()}.uploading"
            temporary_path.write_bytes(data)
            temporary_path.chmod(0o644)
            temporary_path.replace(final_path)
    except OSError:
        app.logger.exception("Challenge artwork storage failed for boss_key=%s", boss_key)
        return jsonify({
            "ok": False,
            "error": "Boss artwork could not be stored. Please contact an administrator.",
            "error_code": "artwork_storage_unavailable",
        }), 500
    icon_url = f"{CHALLENGE_ARTWORK_URL_PREFIX}/{filename}"
    return ok({
        "boss_key": boss_key,
        "draft_id": draft_id,
        "revision": revision,
        "icon_url": icon_url,
        "sha256": sha256,
        "width": width,
        "height": height,
    })
