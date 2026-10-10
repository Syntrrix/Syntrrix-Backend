from django.contrib.auth.hashers import make_password
from django.db import migrations


def seed_roles_and_admin(apps, schema_editor):
    UserRole = apps.get_model("agro_app", "UserRole")
    User = apps.get_model("agro_app", "User")

    roles = [
        (1, "Admin", "System administrators with full privileges."),
        (2, "Buyer", "Buyers who place purchase orders."),
        (3, "Seller", "Sellers who fulfill orders."),
    ]

    for role_id, name, description in roles:
        UserRole.objects.update_or_create(
            role_id=role_id,
            defaults={"name": name, "description": description},
        )

    admin_role = UserRole.objects.get(role_id=1)
    User.objects.update_or_create(
        email="admin@agrobridge.com",
        defaults={
            "full_name": "System Admin",
            "password": make_password("Admin@123"),
            "phone": "+9100000000",
            "company_name": "",
            "gst_number": "",
            "address": "",
            "status": "active",
            "role": admin_role,
        },
    )


def remove_seeded_records(apps, schema_editor):
    UserRole = apps.get_model("agro_app", "UserRole")
    User = apps.get_model("agro_app", "User")

    User.objects.filter(email="admin@agrobridge.com").delete()
    UserRole.objects.filter(role_id__in=[1, 2, 3]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("agro_app", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_roles_and_admin, remove_seeded_records),
    ]

