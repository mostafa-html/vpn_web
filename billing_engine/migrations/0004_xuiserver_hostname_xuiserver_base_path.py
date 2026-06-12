from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('billing_engine', '0003_xuiserver_use_ssl'),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE billing_engine_xuiserver ADD COLUMN hostname varchar(255) NOT NULL DEFAULT '';",
            reverse_sql="SELECT 1;",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE billing_engine_xuiserver ADD COLUMN base_path varchar(255) NOT NULL DEFAULT '/';",
            reverse_sql="SELECT 1;",
        ),
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AddField(
                    model_name='xuiserver',
                    name='hostname',
                    field=models.CharField(
                        blank=True, default='', max_length=255,
                        help_text='Domain name (e.g. gg.mx11.ir). Used instead of IP when set.'
                    ),
                ),
                migrations.AddField(
                    model_name='xuiserver',
                    name='base_path',
                    field=models.CharField(
                        blank=True, default='/', max_length=255,
                        help_text='3x-ui secret base path, e.g. /4bfAPdC269HYSj1c24/ (include slashes)'
                    ),
                ),
            ],
            database_operations=[],
        ),
    ]
