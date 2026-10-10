from django.db import migrations, models
import django.db.models.deletion


def seed_team_member_roles(apps, schema_editor):
    UserRole = apps.get_model("agro_app", "UserRole")
    roles = [
        (4, "Team Manager", "Seller team manager users."),
        (5, "Agent", "Seller team agent users."),
    ]
    for role_id, name, description in roles:
        UserRole.objects.update_or_create(
            role_id=role_id,
            defaults={"name": name, "description": description},
        )


def remove_team_member_roles(apps, schema_editor):
    UserRole = apps.get_model("agro_app", "UserRole")
    UserRole.objects.filter(role_id__in=[4, 5]).delete()


class Migration(migrations.Migration):
    dependencies = [
        ("agro_app", "0034_rename_online_order_seller__a1c3f1_idx_online_orde_seller__6cdf82_idx_and_more"),
    ]

    operations = [
        migrations.CreateModel(
            name="TeamMember",
            fields=[
                ("team_member_id", models.AutoField(primary_key=True, serialize=False)),
                ("member_type", models.CharField(choices=[("team_manager", "Team Manager"), ("agent", "Agent")], max_length=20)),
                ("first_name", models.CharField(max_length=80)),
                ("last_name", models.CharField(blank=True, default="", max_length=80)),
                ("city", models.CharField(blank=True, default="", max_length=120)),
                ("team_name", models.CharField(blank=True, default="", max_length=150)),
                ("team_description", models.CharField(blank=True, default="", max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "parent_team_manager",
                    models.ForeignKey(
                        blank=True,
                        db_column="parent_team_manager_id",
                        limit_choices_to={"role_id": 4},
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="managed_agents",
                        to="agro_app.user",
                    ),
                ),
                (
                    "seller_user",
                    models.ForeignKey(
                        db_column="seller_user_id",
                        limit_choices_to={"role_id": 3},
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="team_members",
                        to="agro_app.user",
                    ),
                ),
                (
                    "user",
                    models.OneToOneField(
                        db_column="user_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="team_member_profile",
                        to="agro_app.user",
                    ),
                ),
            ],
            options={
                "db_table": "team_member",
            },
        ),
        migrations.AddIndex(
            model_name="teammember",
            index=models.Index(fields=["seller_user", "member_type"], name="team_member_seller__dfc67f_idx"),
        ),
        migrations.AddIndex(
            model_name="teammember",
            index=models.Index(fields=["parent_team_manager"], name="team_member_parent__d9ce07_idx"),
        ),
        migrations.RunPython(seed_team_member_roles, remove_team_member_roles),
    ]
