import argparse
from datetime import UTC, datetime
from getpass import getpass

from dotenv import load_dotenv

from app.db import SessionLocal
from app.models.auth import AuthProvider, UserCredential
from app.models.party import PartyDataClassification, PartyType
from app.models.rbac import Role, SystemUserRole
from app.models.system_user import SystemUser
from app.services import party as party_service
from app.services.auth_flow import hash_password


def parse_args():
    parser = argparse.ArgumentParser(description="Seed an admin user.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--first-name", required=True)
    parser.add_argument("--last-name", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument(
        "--password",
        help="Admin password. Omit to enter it through a non-echoing prompt.",
    )
    parser.add_argument("--force-reset", action="store_true")
    return parser.parse_args()


def seed_admin_user(
    db,
    *,
    email: str,
    first_name: str,
    last_name: str,
    username: str,
    password: str,
    force_reset: bool = False,
) -> str:
    admin_role = (
        db.query(Role).filter(Role.name == "admin", Role.is_active.is_(True)).first()
    )
    if admin_role is None:
        raise RuntimeError(
            "Active admin role not found. Run `python -m scripts.seed.seed_rbac` "
            "before seeding an admin user."
        )

    system_user = db.query(SystemUser).filter(SystemUser.email == email).first()
    username_owner = (
        db.query(UserCredential)
        .filter(UserCredential.provider == AuthProvider.local)
        .filter(UserCredential.username == username)
        .first()
    )
    if username_owner is not None and (
        system_user is None or username_owner.system_user_id != system_user.id
    ):
        raise ValueError("Admin username is already assigned to another principal.")

    if system_user is None:
        system_user = SystemUser(
            first_name=first_name,
            last_name=last_name,
            display_name=f"{first_name} {last_name}",
            email=email,
            is_active=True,
        )
        db.add(system_user)
        db.flush()
    else:
        system_user.first_name = first_name
        system_user.last_name = last_name
        system_user.display_name = f"{first_name} {last_name}"
        system_user.is_active = True

    credential = (
        db.query(UserCredential)
        .filter(UserCredential.system_user_id == system_user.id)
        .filter(UserCredential.provider == AuthProvider.local)
        .first()
    )
    created = credential is None
    password_updated_at = datetime.now(UTC)
    if credential:
        credential.username = username
        credential.password_hash = hash_password(password)
        credential.password_updated_at = password_updated_at
        credential.must_change_password = force_reset
        credential.is_active = True
        credential.failed_login_attempts = 0
        credential.locked_until = None
    else:
        credential = UserCredential(
            system_user_id=system_user.id,
            provider=AuthProvider.local,
            username=username,
            password_hash=hash_password(password),
            password_updated_at=password_updated_at,
            must_change_password=force_reset,
            is_active=True,
        )
        db.add(credential)

    role_link = (
        db.query(SystemUserRole)
        .filter(
            SystemUserRole.system_user_id == system_user.id,
            SystemUserRole.role_id == admin_role.id,
            SystemUserRole.scope_type == "",
            SystemUserRole.scope_id == "",
        )
        .first()
    )
    if role_link is None:
        db.add(
            SystemUserRole(
                system_user_id=system_user.id,
                role_id=admin_role.id,
                scope_type="",
                scope_id="",
                source="local",
            )
        )

    if system_user.person_party_id is None:
        person_party = party_service.create_party(
            db,
            party_type=PartyType.person,
            display_name=system_user.display_name or system_user.email,
            data_classification=PartyDataClassification.production,
            metadata={"bootstrap": "admin_seeder"},
        )
        party_service.bind_system_user_principal(
            db,
            system_user_id=system_user.id,
            person_party_id=person_party.id,
            source="admin_seeder",
            reason="Operator-seeded local administrator identity",
        )

    db.commit()
    return "Admin user created." if created else "Admin user updated."


def main():
    load_dotenv()
    args = parse_args()
    password = args.password or getpass("Admin password: ")
    if not password:
        raise SystemExit("Admin password cannot be empty.")
    db = SessionLocal()
    try:
        print(
            seed_admin_user(
                db,
                email=args.email,
                first_name=args.first_name,
                last_name=args.last_name,
                username=args.username,
                password=password,
                force_reset=args.force_reset,
            )
        )
    finally:
        db.close()


if __name__ == "__main__":
    main()
