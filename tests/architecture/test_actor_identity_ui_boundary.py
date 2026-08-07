from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_activity_templates_do_not_render_raw_actor_identifiers():
    forbidden = {
        "templates/admin/vendors/as_built_review_detail.html": "{{ event.actor_id }}",
        "templates/admin/network/device-groups/detail.html": "{{ event.actor_id",
        "templates/admin/network/detected_outages_notify.html": "{{ r.actor_id",
        "templates/admin/network/fiber/ont_identity_review_detail.html": "{{ decision.reviewed_by }}",
        "templates/admin/network/fiber/ont_identity_reviews.html": "{{ row.review.reviewed_by }}",
        "templates/admin/network/fiber/ont_assignment_constraint_authorizations.html": "{{ row.requested_by }}",
        "templates/admin/system/control_plane.html": "{{ event.actor }}",
    }

    for relative_path, raw_rendering in forbidden.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert raw_rendering not in source, relative_path


def test_shared_actor_projection_hides_unresolved_user_uuids():
    source = (ROOT / "app" / "services" / "audit_helpers.py").read_text(
        encoding="utf-8"
    )

    assert "def resolve_actor_display_names(" in source
    assert 'or "Former or unknown user"' in source
