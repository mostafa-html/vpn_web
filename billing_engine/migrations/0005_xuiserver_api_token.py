from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing_engine', '0004_xuiserver_hostname_xuiserver_base_path'),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE billing_engine_xuiserver ADD COLUMN api_token varchar(512) NOT NULL DEFAULT '';",
            reverse_sql="SELECT 1;",
        ),
        migrations.RunSQL(
            sql="UPDATE billing_engine_xuiserver SET admin_username=COALESCE(admin_username,''), admin_password=COALESCE(admin_password,'');",
            reverse_sql="SELECT 1;",
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='xuiserver',
                    name='api_token',
                    field=models.CharField(
                        blank=True, default='', max_length=512,
                        help_text='Bearer token from 3x-ui Panel Settings -> Security -> API Token'
                    ),
                ),
                migrations.AlterField(
                    model_name='xuiserver',
                    name='admin_username',
                    field=models.CharField(max_length=150, blank=True, default=''),
                ),
                migrations.AlterField(
                    model_name='xuiserver',
                    name='admin_password',
                    field=models.CharField(max_length=255, blank=True, default=''),
                ),
            ],
            database_operations=[],
        ),
    ]
