from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        ("agro_app", "0035_team_member_and_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="bot",
            name="assignment_agent_cursor",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="bot",
            name="assignment_team_cursor",
            field=models.PositiveIntegerField(default=0),
        ),
        migrations.AddField(
            model_name="livechatsession",
            name="assigned_agent",
            field=models.ForeignKey(
                blank=True,
                db_column="assigned_agent_id",
                limit_choices_to={"role_id": 5},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_live_chat_sessions_as_agent",
                to="agro_app.user",
            ),
        ),
        migrations.AddField(
            model_name="livechatsession",
            name="assigned_team_manager",
            field=models.ForeignKey(
                blank=True,
                db_column="assigned_team_manager_id",
                limit_choices_to={"role_id": 4},
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="assigned_live_chat_sessions_as_manager",
                to="agro_app.user",
            ),
        ),
        migrations.AddIndex(
            model_name="livechatsession",
            index=models.Index(fields=["assigned_team_manager", "created_at"], name="live_chat_se_assigne_1f6fe7_idx"),
        ),
        migrations.AddIndex(
            model_name="livechatsession",
            index=models.Index(fields=["assigned_agent", "created_at"], name="live_chat_se_assigne_0d4172_idx"),
        ),
    ]
