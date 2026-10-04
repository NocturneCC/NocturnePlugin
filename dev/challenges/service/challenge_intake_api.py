#!/usr/bin/env python3
"""Authenticated HTTP receiver for approved Discord challenge shadow events."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from flask import Flask, jsonify, request
from werkzeug.exceptions import BadRequest, RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

from challenge_direct_intake import (
    IntakeValidationError,
    active_config_document,
    ensure_award_reviews,
    insert_direct_observation,
    normalize_and_evaluate,
    reconcile_direct_observations,
    record_request_audit,
)
from challenge_shadow_common import ro_connection, rw_connection
from challenge_shadow_sync import rebuild_derived
from challenge_config import (
    config_document,
    create_draft,
    draft_diff,
    draft_document,
    publish_draft,
    save_draft,
    validate_draft,
    version_history,
)
from challenge_award_delivery import diagnostics as award_delivery_diagnostics


DEFAULT_CHALLENGES_DB = Path("/srv/projects/database/Challenges.db")
DEFAULT_MEMBERS_DB = Path("/srv/projects/database/Members.db")
DEFAULT_TOKEN_FILE = Path("/etc/nocturne/challenge-intake.token")
MAX_BODY_BYTES = 65536


class SlidingLimiter:
    def __init__(self, per_ip: int = 60, global_limit: int = 300):
        self.per_ip = per_ip
        self.global_limit = global_limit
        self.lock = threading.Lock()
        self.by_ip: dict[str, deque[float]] = defaultdict(deque)
        self.global_window: deque[float] = deque()

    def allowed(self, address: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60
        with self.lock:
            while self.global_window and self.global_window[0] < cutoff:
                self.global_window.popleft()
            window = self.by_ip[address]
            while window and window[0] < cutoff:
                window.popleft()
            if len(self.global_window) >= self.global_limit or len(window) >= self.per_ip:
                return False
            self.global_window.append(now)
            window.append(now)
            return True


def _load_token(path: Path) -> str:
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 48:
        raise RuntimeError("Challenge intake token must contain at least 48 characters")
    return token


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    app.config.update(
        CHALLENGES_DB=DEFAULT_CHALLENGES_DB,
        MEMBERS_DB=DEFAULT_MEMBERS_DB,
        TOKEN_FILE=Path(os.getenv("CHALLENGE_INTAKE_TOKEN_FILE", str(DEFAULT_TOKEN_FILE))),
        INTAKE_TOKEN=None,
        MAX_CONTENT_LENGTH=MAX_BODY_BYTES,
        RATE_LIMIT_ENABLED=True,
    )
    if config:
        app.config.update(config)
    token = str(app.config.get("INTAKE_TOKEN") or _load_token(Path(app.config["TOKEN_FILE"]))).strip()
    limiter = SlidingLimiter()
    automatic_limiter = SlidingLimiter(per_ip=10, global_limit=300)
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    def challenge_db() -> sqlite3.Connection:
        return rw_connection(Path(app.config["CHALLENGES_DB"]))

    def members_db() -> sqlite3.Connection:
        return ro_connection(Path(app.config["MEMBERS_DB"]))

    def body_hash(raw: bytes) -> str:
        return hashlib.sha256(raw).hexdigest()

    def supplied_token() -> str:
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            return header[7:].strip()
        return request.headers.get("X-Nocturne-Challenge-Token", "").strip()

    def audit_failure(outcome: str, status: int, detail: str, raw: bytes, event_id: str | None = None) -> None:
        conn = challenge_db()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record_request_audit(
                conn, outcome=outcome, http_status=status, detail_code=detail,
                body_hash=body_hash(raw) if raw else None, event_id=event_id,
                remote_address=request.remote_addr, submission_id=None,
                body_size=len(raw),
            )
            conn.commit()
        finally:
            conn.close()

    def authorized(raw: bytes):
        provided = supplied_token()
        if not provided or not hmac.compare_digest(provided, token):
            audit_failure("auth_failure", 401, "missing_or_invalid_authentication", raw)
            return False
        return True

    @app.errorhandler(RequestEntityTooLarge)
    def too_large(_error):
        if request.path == "/api/challenges/intake/observations":
            return jsonify({"state": "invalid", "reason": "request_too_large"}), 413
        audit_failure("validation_failure", 413, "payload_too_large", b"")
        return jsonify({"ok": False, "error": "payload_too_large"}), 413

    @app.get("/health")
    def health():
        conn = challenge_db()
        try:
            mode = conn.execute("SELECT setting_value FROM challenge_settings WHERE setting_key='award_mode'").fetchone()
            direct_mode = conn.execute("SELECT setting_value FROM challenge_settings WHERE setting_key='direct_intake_mode'").fetchone()
            return jsonify({
                # Intake and award delivery are deliberately separate safety
                # domains.  A live award worker must not make the append-only
                # shadow intake unavailable.
                "ok": bool(direct_mode and direct_mode[0] == "shadow"),
                "service": "nocturne-challenge-intake",
                "award_mode": mode[0] if mode else None,
                "direct_intake_mode": direct_mode[0] if direct_mode else None,
            })
        finally:
            conn.close()

    @app.get("/api/challenges/config/active")
    def active_config():
        raw = b""
        if not authorized(raw):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        conn = challenge_db()
        try:
            return jsonify({"ok": True, "config": active_config_document(conn)})
        finally:
            conn.close()

    def config_actor() -> str:
        value = str(request.headers.get("X-Challenge-Admin") or "admin-proxy").strip()
        return value[:128] or "admin-proxy"

    def internal_authorized():
        raw = request.get_data(cache=True)
        if not authorized(raw):
            return None, (jsonify({"ok": False, "error": "unauthorized"}), 401)
        return raw, None

    def config_failure(exc: Exception):
        message = str(exc)
        if isinstance(exc, LookupError):
            return jsonify({"ok":False,"error":"not_found","message":message}),404
        conflicts = {
            "draft_revision_required": "A saved draft revision is required.",
            "draft_revision_conflict": "The draft changed; reload before continuing.",
            "draft_revision_not_validated": "The latest saved revision has not been validated.",
            "draft_base_version_is_no_longer_active": "The draft is based on an inactive configuration version.",
            "no_changes_to_publish": "No changes to publish.",
            "published_boss_cannot_be_removed": "Published bosses must be deactivated rather than removed.",
            "historical_boss_cannot_be_removed": "Bosses with historical challenge data cannot be removed.",
        }
        if message in conflicts:
            return jsonify({"ok":False,"error":message,"message":conflicts[message]}),409
        return jsonify({"ok":False,"error":"invalid_config_operation","message":message}),400

    @app.get("/internal/challenges/config/published")
    def internal_config_published():
        _raw, failure = internal_authorized()
        if failure: return failure
        conn=challenge_db()
        try: return jsonify({"ok":True,"config":config_document(conn)})
        finally: conn.close()

    @app.get("/internal/challenges/config/versions")
    def internal_config_versions():
        _raw, failure = internal_authorized()
        if failure: return failure
        conn=challenge_db()
        try: return jsonify({"ok":True,"versions":version_history(conn)})
        finally: conn.close()

    @app.get("/internal/challenges/awards/diagnostics")
    def internal_award_diagnostics():
        """Protected, aggregate-only diagnostics; no delivery control exists."""
        _raw, failure = internal_authorized()
        if failure:
            return failure
        conn = challenge_db()
        try:
            return jsonify({"ok": True, **award_delivery_diagnostics(conn)})
        finally:
            conn.close()

    @app.route("/internal/challenges/config/draft",methods=["GET","POST","PUT"])
    def internal_config_draft():
        _raw, failure = internal_authorized()
        if failure: return failure
        conn=challenge_db()
        try:
            if request.method == "POST":
                result=create_draft(conn,config_actor())
            elif request.method == "PUT":
                body=request.get_json(silent=True) or {}
                result=save_draft(conn,int(body.get("draft_id")),body.get("config"),config_actor(),body.get("revision"))
            else:
                result=draft_document(conn)
            return jsonify({"ok":True,"draft":result})
        except Exception as exc:
            conn.rollback(); return config_failure(exc)
        finally: conn.close()

    @app.post("/internal/challenges/config/draft/validate")
    def internal_config_validate():
        _raw, failure = internal_authorized()
        if failure: return failure
        body=request.get_json(silent=True) or {}; conn=challenge_db()
        try:
            result=validate_draft(
                conn,
                int(body.get("draft_id")),
                config_actor(),
                body.get("revision"),
            )
            return jsonify({"ok":True,**result})
        except Exception as exc:
            conn.rollback(); return config_failure(exc)
        finally: conn.close()

    @app.post("/internal/challenges/config/draft/diff")
    def internal_config_diff():
        _raw, failure = internal_authorized()
        if failure: return failure
        body=request.get_json(silent=True) or {}; conn=challenge_db()
        try:
            result=draft_diff(
                conn,int(body.get("draft_id")),
                int(body["revision"]) if body.get("revision") is not None else None,
            )
            return jsonify({"ok":True,"diff":result})
        except Exception as exc:
            return config_failure(exc)
        finally: conn.close()

    @app.post("/internal/challenges/config/draft/publish")
    def internal_config_publish():
        _raw, failure = internal_authorized()
        if failure: return failure
        body=request.get_json(silent=True) or {}; conn=challenge_db()
        try:
            result=publish_draft(
                conn,
                int(body.get("draft_id")),
                config_actor(),
                confirmed=body.get("confirm") is True,
                expected_revision=body.get("revision"),
            )
            return jsonify({"ok":True,"config":result})
        except Exception as exc:
            conn.rollback(); return config_failure(exc)
        finally: conn.close()

    def mutate_draft_boss(operation: str, boss_key: str | None = None):
        body=request.get_json(silent=True) or {}; conn=challenge_db()
        try:
            draft=draft_document(conn,int(body.get("draft_id")))
            document=draft["config"]; bosses=document["bosses"]
            if operation == "create":
                boss=body.get("boss")
                if not isinstance(boss,dict): raise ValueError("boss object is required")
                if any(item.get("boss_key")==boss.get("boss_key") for item in bosses): raise ValueError("boss_key already exists")
                bosses.append(boss)
            elif operation == "update":
                index=next((i for i,item in enumerate(bosses) if item.get("boss_key")==boss_key),None)
                if index is None: raise LookupError("Boss not found")
                replacement=body.get("boss")
                if not isinstance(replacement,dict) or replacement.get("boss_key") != boss_key:
                    raise ValueError("boss_key is stable and may not be changed")
                bosses[index]=replacement
            elif operation in ("deactivate","reactivate"):
                boss=next((item for item in bosses if item.get("boss_key")==boss_key),None)
                if boss is None: raise LookupError("Boss not found")
                boss["active"]=operation == "reactivate"
                if operation == "deactivate": boss["submission_enabled"]=False
            elif operation == "remove":
                index=next((i for i,item in enumerate(bosses) if item.get("boss_key")==boss_key),None)
                if index is None: raise LookupError("Boss not found")
                bosses.pop(index)
            elif operation == "reorder":
                order=body.get("boss_keys")
                if not isinstance(order,list) or set(order) != {item.get("boss_key") for item in bosses}:
                    raise ValueError("boss_keys must contain every draft boss exactly once")
                positions={key:(index+1)*10 for index,key in enumerate(order)}
                for boss in bosses: boss["display_order"]=positions[boss["boss_key"]]
            result=save_draft(conn,draft["draft_id"],document,config_actor(),body.get("revision",draft["revision"]))
            return jsonify({"ok":True,"draft":result})
        except Exception as exc:
            conn.rollback(); return config_failure(exc)
        finally: conn.close()

    @app.post("/internal/challenges/config/draft/bosses")
    def internal_config_create_boss():
        _raw,failure=internal_authorized()
        return failure or mutate_draft_boss("create")

    @app.put("/internal/challenges/config/draft/bosses/<boss_key>")
    def internal_config_update_boss(boss_key):
        _raw,failure=internal_authorized()
        return failure or mutate_draft_boss("update",boss_key)

    @app.delete("/internal/challenges/config/draft/bosses/<boss_key>")
    def internal_config_remove_boss(boss_key):
        _raw,failure=internal_authorized()
        return failure or mutate_draft_boss("remove",boss_key)

    @app.post("/internal/challenges/config/draft/bosses/<boss_key>/<action>")
    def internal_config_boss_state(boss_key,action):
        _raw,failure=internal_authorized()
        if failure: return failure
        if action not in ("deactivate","reactivate"): return jsonify({"ok":False,"error":"invalid_action"}),400
        return mutate_draft_boss(action,boss_key)

    @app.post("/internal/challenges/config/draft/reorder")
    def internal_config_reorder():
        _raw,failure=internal_authorized()
        return failure or mutate_draft_boss("reorder")

    @app.get("/api/challenges/intake/diagnostics")
    def intake_diagnostics():
        raw = b""
        if not authorized(raw):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        from challenge_direct_diagnostics import diagnostics
        return jsonify(diagnostics(Path(app.config["CHALLENGES_DB"])))

    @app.post("/api/challenges/intake/approved")
    def intake_approved():
        raw = request.get_data(cache=True)
        if not authorized(raw):
            return jsonify({"ok": False, "error": "unauthorized"}), 401
        address = request.remote_addr or "unknown"
        if app.config["RATE_LIMIT_ENABLED"] and not limiter.allowed(address):
            audit_failure("rate_limited", 429, "rate_limit", raw)
            return jsonify({"ok": False, "error": "rate_limited"}), 429
        if not request.is_json:
            audit_failure("validation_failure", 415, "content_type_must_be_json", raw)
            return jsonify({"ok": False, "error": "content_type_must_be_json"}), 415
        try:
            payload = request.get_json(force=False, silent=False)
        except BadRequest:
            audit_failure("validation_failure", 400, "invalid_json", raw)
            return jsonify({"ok": False, "error": "invalid_json"}), 400
        event_id = None
        if isinstance(payload, dict):
            event_id = str(payload.get("event_id") or payload.get("submission_id") or "") or None
        challenge = challenge_db()
        members = members_db()
        try:
            challenge.execute("BEGIN IMMEDIATE")
            direct_mode = challenge.execute("SELECT setting_value FROM challenge_settings WHERE setting_key='direct_intake_mode'").fetchone()
            if not direct_mode or direct_mode[0] != "shadow":
                raise IntakeValidationError(
                    "direct_intake_disabled",
                    "direct intake is disabled by direct_intake_mode",
                    503,
                )
            normalized = normalize_and_evaluate(challenge, payload)
            submission_id, outcome, unresolved = insert_direct_observation(challenge, members, normalized)
            if outcome == "accepted":
                derived = rebuild_derived(challenge, normalized.config_version_id)
                ensure_award_reviews(challenge)
                reconciliation = reconcile_direct_observations(challenge)
                state_row = challenge.execute(
                    "SELECT reconciliation_state FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                    (submission_id,),
                ).fetchone()
                state = state_row[0] if state_row else "DIRECT_WAITING_FOR_GOOGLE"
            else:
                derived = None
                reconciliation = None
                state_row = challenge.execute(
                    "SELECT reconciliation_state FROM challenge_observation_reconciliation WHERE direct_submission_id=?",
                    (submission_id,),
                ).fetchone()
                state = "DUPLICATE_DIRECT" if outcome == "duplicate_direct" else (state_row[0] if state_row else "DIRECT_WAITING_FOR_GOOGLE")
                if outcome == "duplicate_direct" and state_row:
                    details = json.dumps({"reason":"Secondary content fingerprint replay"},sort_keys=True,separators=(",",":"))
                    challenge.execute(
                        """INSERT INTO challenge_observation_reconciliation_history
                           (reconciliation_id,direct_submission_id,google_submission_id,reconciliation_state,
                            match_method,details_json,details_sha256,recorded_at)
                           SELECT reconciliation_id,direct_submission_id,google_submission_id,'DUPLICATE_DIRECT',
                                  'content_fingerprint',?,?,CURRENT_TIMESTAMP
                             FROM challenge_observation_reconciliation WHERE direct_submission_id=?""",
                        (details,hashlib.sha256(details.encode()).hexdigest(),submission_id),
                    )
            status = 201 if outcome == "accepted" else 200
            audit_outcome = "accepted" if outcome == "accepted" else ("duplicate_direct" if outcome == "duplicate_direct" else "idempotent")
            record_request_audit(
                challenge, outcome=audit_outcome, http_status=status,
                detail_code=outcome, body_hash=body_hash(raw), event_id=normalized.event_id,
                remote_address=address, submission_id=submission_id, body_size=len(raw),
            )
            challenge.commit()
            return jsonify({
                "ok": True,
                "submission_id": submission_id,
                "event_id": normalized.event_id,
                "idempotent": outcome != "accepted",
                "duplicate_class": outcome if outcome != "accepted" else None,
                "earned_tier": normalized.earned_tier_key,
                "metric": {"type": normalized.metric_type, "value": normalized.metric_value, "unit": normalized.metric_unit},
                "identity_unresolved_count": unresolved,
                "reconciliation_state": state,
                "award_mode": "shadow",
            }), status
        except IntakeValidationError as exc:
            challenge.rollback()
            audit_failure(
                "conflict" if exc.status == 409 else "validation_failure",
                exc.status, exc.code, raw, event_id,
            )
            return jsonify({"ok": False, "error": exc.code, "message": exc.message}), exc.status
        except Exception:
            challenge.rollback()
            audit_failure("internal_error", 500, "internal_error", raw, event_id)
            app.logger.exception("Challenge direct intake failed")
            return jsonify({"ok": False, "error": "internal_error"}), 500
        finally:
            members.close()
            challenge.close()

    @app.post("/api/challenges/intake/observations")
    def intake_automatic_observation():
        """Public, bounded observation intake; never accepts approval/PB claims."""
        from challenge_automatic_intake import ObservationError, process_observation

        if request.content_length is not None and request.content_length > 8192:
            return jsonify({"state": "invalid", "reason": "request_too_large"}), 413
        raw = request.get_data(cache=False)
        if len(raw) > 8192:
            return jsonify({"state": "invalid", "reason": "request_too_large"}), 413
        if request.mimetype != "application/json":
            return jsonify({"state": "invalid", "reason": "content_type_required"}), 415
        address = request.remote_addr or "unknown"
        if app.config["RATE_LIMIT_ENABLED"] and not automatic_limiter.allowed(address):
            return jsonify({"state": "rate_limited"}), 429

        def reject_duplicate_keys(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = value
            return result

        try:
            payload = json.loads(
                raw.decode("utf-8"), object_pairs_hook=reject_duplicate_keys,
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("invalid_json_number")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return jsonify({"state": "invalid", "reason": "invalid_json"}), 400

        challenge = None
        members = None
        try:
            challenge = challenge_db()
            members = members_db()
            challenge.execute("PRAGMA busy_timeout=5000")
            challenge.execute("BEGIN IMMEDIATE")
            result = process_observation(challenge, members, payload)
            challenge.commit()
            state = result["state"]
            status = 201 if state == "accepted" else 200
            return jsonify(result), status
        except ObservationError as exc:
            if challenge is not None:
                challenge.rollback()
            if exc.state == "idempotency_conflict":
                return jsonify({"state": "idempotency_conflict"}), 409
            return jsonify({"state": "invalid", "reason": exc.reason}), 422
        except Exception:
            if challenge is not None:
                challenge.rollback()
            # Intentionally no exception text, payload, identity, or request
            # metadata in logs; the transaction failure remains fail-closed.
            app.logger.error("automatic Challenge observation failed category=processing_failure")
            return jsonify({"state": "server_failure"}), 503
        finally:
            if challenge is not None:
                challenge.close()
            if members is not None:
                members.close()

    return app


app = None if os.getenv("CHALLENGE_INTAKE_SKIP_DEFAULT_APP") == "1" else create_app()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5011)
