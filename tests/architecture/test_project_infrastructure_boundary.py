from pathlib import Path

from app.services.sot_registry.registry import service_relationship

ROOT = Path(__file__).resolve().parents[2]


def test_infrastructure_lookup_is_shared_and_read_only():
    owner = service_relationship("network.infrastructure_catalogue")
    assert owner.contract is not None
    assert owner.module == "app.services.infrastructure_catalogue"
    assert not (ROOT / "app/services/network/infrastructure_catalogue.py").exists()
    source = (ROOT / "app/services/infrastructure_catalogue.py").read_text(
        encoding="utf-8"
    )
    assert all(token not in source for token in (".commit(", ".add(", ".delete("))
    for path in ("app/services/web_customer_lists.py", "app/web/admin/projects.py"):
        assert "infrastructure_catalogue.search(" in (ROOT / path).read_text(
            encoding="utf-8"
        )


def test_project_relationships_have_one_writer():
    for path in (
        "app/web/admin/projects.py",
        "app/services/web_projects.py",
        "app/services/installation_projects.py",
    ):
        source = (ROOT / path).read_text(encoding="utf-8")
        assert "ProjectInfrastructure(" not in source
        assert "project.infrastructure =" not in source
    owner = service_relationship("operations.project_lifecycle")
    assert "project infrastructure relationship" in owner.owns
    source = (ROOT / "app/services/installation_projects.py").read_text(
        encoding="utf-8"
    )
    assert "class EnsureProjectScope:" in source
    assert "class ProjectScopeOutcome:" in source
    assert ".commit(" not in source
